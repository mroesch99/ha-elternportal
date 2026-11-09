"""Extensions of the pyelternportal API for the elternportal integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import html as html_lib
import re
from typing import Any
from urllib import parse

import aiohttp
import bs4

from pyelternportal import CannotConnectException, ElternPortalAPI
from pyelternportal.const import SCHOOL_SUBJECTS
from pyelternportal.lesson import Lesson
from pyelternportal.substitution import Substitution

from .const import LOGGER

TEACHER_URL_PATH: str = "/service/schulinformationen"

_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_SPACE = re.compile(r"\s+")
_RE_INTENSIVE = re.compile(r"^i\d*$")

# Common (Bavarian) subject abbreviations missing in pyelternportal
EXTRA_SUBJECTS: dict[str, str] = {
    "Ev": "Religion Evangelisch",
    "K": "Religion Katholisch",
    "NuT": "Natur und Technik",
    "Sm": "Sport männlich",
    "Sw": "Sport weiblich",
}

# Suffix after "_" (e.g. NuT_P, M_i1)
SUBJECT_SUFFIXES: dict[str, str] = {
    "B": "Biologie",
    "I": "Informatik",
    "P": "Physik",
    "Ü": "Übung",
}


@dataclass
class SubstitutionExt(Substitution):
    """Substitution with resolved teacher and subject names."""

    original_teacher_name: str = ""
    substitute_teacher_name: str = ""
    subject_old_name: str = ""
    subject_new_name: str = ""
    weekday: str = ""


WEEKDAYS: list[str] = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]


def weekday_name(value: Any) -> str:
    """German weekday of a date (date, datetime or ISO string)."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return ""
    if isinstance(value, date):
        return WEEKDAYS[value.weekday()]
    return ""


@dataclass
class LessonExt(Lesson):
    """Lesson with resolved subject name and the room of the chosen subject."""

    subject_name: str = ""
    room_selected: str = ""


def clean_html_text(text: str | None) -> str | None:
    """Remove html line breaks / tags and normalize whitespace."""
    if not text:
        return text
    text = _RE_BR.sub(" ", text)
    text = _RE_TAG.sub("", text)
    text = html_lib.unescape(text)
    return _RE_SPACE.sub(" ", text).strip()


def parse_teachers(html: str, parser: str = "html5lib") -> dict[str, str]:
    """Parse the section 'Stundenplankürzel der Lehrkräfte' (abbreviation -> name)."""
    soup = bs4.BeautifulSoup(html, parser)
    teachers: dict[str, str] = {}
    active = False
    for row in soup.select("#asam_content .row"):
        heading = row.find("h3")
        if heading is not None:
            title = heading.get_text(strip=True).lower()
            active = "kürzel" in title and "lehr" in title
            continue
        if not active:
            continue
        key_tag = row.select_one(".col-md-4 b")
        value_tag = row.select_one(".col-md-6")
        if key_tag is None or value_tag is None:
            continue
        key = key_tag.get_text(strip=True)
        value = value_tag.get_text(" ", strip=True)
        if key and value:
            teachers[key] = value
    return teachers


def resolve_teachers(text: str | None, teachers: dict[str, str]) -> str:
    """Replace every teacher abbreviation in text by the full name."""
    if not text or not teachers:
        return text or ""
    keys = sorted(teachers, key=len, reverse=True)
    pattern = r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)"
    return re.sub(pattern, lambda m: teachers[m.group(1)], text)


def parse_subject_mapping(text: str | None) -> dict[str, str]:
    """Parse user defined subjects, one "abbreviation=name" per line."""
    subjects: dict[str, str] = {}
    for line in (text or "").replace(";", "\n").splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key and value:
            subjects[key] = value
    return subjects


def build_subjects(custom: dict[str, str] | None = None) -> dict[str, str]:
    """Default subjects (pyelternportal + extras), overridden by custom ones."""
    subjects = {s["Short"]: s["Name"] for s in SCHOOL_SUBJECTS}
    subjects.update(EXTRA_SUBJECTS)
    subjects.update(custom or {})
    return subjects


def _lookup(key: str, subjects: dict[str, str]) -> str | None:
    if key in subjects:
        return subjects[key]
    lower = key.lower()
    for k, v in subjects.items():
        if k.lower() == lower:
            return v
    return None


def resolve_subject(text: str | None, subjects: dict[str, str]) -> str:
    """Replace a subject abbreviation by the full name (fallback: unchanged)."""
    if not text:
        return text or ""
    key = text.strip()
    name = _lookup(key, subjects)
    if name is not None:
        return name
    base, sep, suffix = key.partition("_")
    if not sep:
        return text
    base_name = _lookup(base, subjects)
    if base_name is None:
        return text
    if _RE_INTENSIVE.match(suffix):
        return f"{base_name} Intensivierung"
    suffix_name = _lookup(suffix, SUBJECT_SUFFIXES)
    if suffix_name is not None:
        return f"{base_name} – {suffix_name}"
    return text


def split_alternatives(subject: str | None) -> list[str]:
    """Split "F/L" into unique alternatives ["F", "L"] (keeps the order)."""
    parts: list[str] = []
    for part in (subject or "").split("/"):
        part = part.strip()
        if part and part not in parts:
            parts.append(part)
    return parts


def find_alternatives(lessons: list[Lesson]) -> list[list[str]]:
    """Distinct subject combinations ("F/L", "K/Ev/Eth") of a timetable."""
    seen: set[frozenset[str]] = set()
    result: list[list[str]] = []
    for lesson in lessons:
        parts = split_alternatives(lesson.subject)
        key = frozenset(parts)
        if len(parts) > 1 and key not in seen:
            seen.add(key)
            result.append(parts)
    return result


def build_choice(
    combinations: list[list[str]], chosen: list[str]
) -> dict[str, list[str]]:
    """Excluded = alternatives of a combination in which another one is chosen."""
    chosen_set = set(chosen)
    excluded: set[str] = set()
    for parts in combinations:
        if chosen_set.intersection(parts):
            excluded.update(p for p in parts if p not in chosen_set)
    return {"chosen": sorted(chosen_set), "excluded": sorted(excluded)}


def resolve_lesson(
    lesson: Lesson, subjects: dict[str, str], chosen: list[str]
) -> LessonExt:
    """Apply subject names and the subject choice to a lesson."""
    raw_subjects = (lesson.subject or "").split("/")
    raw_rooms = (lesson.room or "").split("/")
    pairs: list[tuple[str, str]] = []
    for index, subject in enumerate(raw_subjects):
        subject = subject.strip()
        room = raw_rooms[index].strip() if index < len(raw_rooms) else ""
        if subject:
            pairs.append((subject, room))
    selected = [p for p in pairs if p[0] in chosen] or pairs
    names: list[str] = []
    rooms: list[str] = []
    for subject, room in selected:
        name = resolve_subject(subject, subjects)
        if name not in names:
            names.append(name)
        if room and room not in rooms:
            rooms.append(room)
    return LessonExt(
        **{k: v for k, v in asdict(lesson).items() if k in Lesson.__dataclass_fields__},
        subject_name="/".join(names),
        room_selected="/".join(rooms),
    )


def is_relevant(substitution: Substitution, excluded: list[str]) -> bool:
    """False if all subjects of a substitution are deselected alternatives."""
    if not excluded:
        return True
    excluded_lower = {e.lower() for e in excluded}
    subjects = [
        s.strip()
        for s in (substitution.subject_old, substitution.subject_new)
        if s and s.strip()
    ]
    if not subjects:
        return True
    return not all(s.lower() in excluded_lower for s in subjects)


class ElternPortalAPIExt(ElternPortalAPI):
    """ElternPortalAPI with teacher names and cleaned appointment titles."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self.substitution_teacher_names: bool = False
        self.subjects: dict[str, str] = build_subjects()
        self.subject_choices: dict[str, dict[str, list[str]]] = {}
        self.teachers: dict[str, str] = {}
        self._teachers_loaded: bool = False

    def clear_cookies(self) -> None:
        """Drop all cookies of the (dedicated) session."""
        cookie_jar = getattr(self._session, "cookie_jar", None)
        if cookie_jar is not None:
            cookie_jar.clear()

    async def async_base_online(self) -> None:
        """Start every login with a clean session.

        A stale session cookie (e.g. after a failed logout) makes the portal
        return the logged-in start page without a login form (no csrf token).
        """
        self.clear_cookies()
        try:
            await super().async_base_online()
        except CannotConnectException as ex:
            if "csrf" not in str(ex):
                raise
            LOGGER.debug("No csrf token found, logout and retry once")
            try:
                async with self._session.get(
                    parse.urljoin(self.base_url, "/logout")
                ) as response:
                    await response.read()
            except aiohttp.ClientError as logout_ex:
                LOGGER.debug("Logout before retry failed: %s", logout_ex)
            self.clear_cookies()
            await super().async_base_online()

    async def async_update_online(self) -> None:
        """Reload the teacher list once per update cycle."""
        self._teachers_loaded = False
        await super().async_update_online()

    async def async_appointment_parse(self, appointments: Any) -> None:
        """Remove html line breaks from appointment titles."""
        await super().async_appointment_parse(appointments)
        for appointment in self._student.appointments:
            appointment.title = clean_html_text(appointment.title)
            appointment.short = clean_html_text(appointment.short)

    async def async_teacher_online(self) -> None:
        """Load the teacher abbreviations (needs a logged-in session)."""
        url = parse.urljoin(self.base_url, TEACHER_URL_PATH)
        LOGGER.debug("teacher.url=%s", url)
        try:
            async with self._session.get(url) as response:
                if response.status != 200:
                    LOGGER.warning("teacher.status=%s", response.status)
                    return
                html = await response.text()
            teachers = parse_teachers(html, self._beautiful_soup_parser)
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.warning("Loading teacher names failed: %s", ex)
            return
        finally:
            self._teachers_loaded = True
        LOGGER.debug("teacher.count=%s", len(teachers))
        if teachers:
            self.teachers = teachers
        else:
            LOGGER.warning("No teacher abbreviations found on %s", url)

    async def async_load_lessons(self) -> None:
        """Load only the timetables of all students (used by the options flow)."""
        if self._demo:
            await self.async_base_demo()
            await self.async_login_demo()
            for self._student in self.students:
                await self.async_lesson_demo()
            self._student = None
            return
        await self.async_base_online()
        await self.async_login_online()
        try:
            for self._student in self.students:
                await self.async_set_child_online()
                await self.async_lesson_online()
        finally:
            self._student = None
            await self.async_logout_online()

    def subjects_name(self, abbreviation: str) -> str:
        """Full subject name of an abbreviation."""
        return resolve_subject(abbreviation, self.subjects)

    def set_custom_subjects(self, text: str | None) -> None:
        """Set user defined subject abbreviations ("abbreviation=name" lines)."""
        self.subjects = build_subjects(parse_subject_mapping(text))

    def _choice(self) -> dict[str, list[str]]:
        student_id = getattr(self._student, "student_id", None)
        return self.subject_choices.get(str(student_id), {}) if student_id else {}

    async def async_lesson_online(self) -> None:
        """Timetable with subject names and the chosen alternatives."""
        await super().async_lesson_online()
        chosen = self._choice().get("chosen", [])
        self._student.lessons = [
            resolve_lesson(lesson, self.subjects, chosen)
            for lesson in self._student.lessons
        ]

    async def async_substitution_online(self) -> None:
        """Substitution plan with resolved subject and optional teacher names."""
        if self.substitution_teacher_names and not self._teachers_loaded:
            await self.async_teacher_online()
        await super().async_substitution_online()
        resolve = self.substitution_teacher_names
        self._student.substitutions = [
            SubstitutionExt(
                **{
                    k: v
                    for k, v in asdict(s).items()
                    if k in Substitution.__dataclass_fields__
                },
                original_teacher_name=(
                    resolve_teachers(s.original_teacher, self.teachers) if resolve else ""
                ),
                substitute_teacher_name=(
                    resolve_teachers(s.substitute_teacher, self.teachers) if resolve else ""
                ),
                subject_old_name=resolve_subject(s.subject_old, self.subjects),
                subject_new_name=resolve_subject(s.subject_new, self.subjects),
                weekday=weekday_name(s.date),
            )
            for s in self._student.substitutions
        ]
        excluded = self._choice().get("excluded", [])
        if excluded:
            before = len(self._student.substitutions)
            self._student.substitutions = [
                s for s in self._student.substitutions if is_relevant(s, excluded)
            ]
            LOGGER.debug(
                "substitution.filtered=%s",
                before - len(self._student.substitutions),
            )

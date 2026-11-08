"""Extensions of the pyelternportal API for the elternportal integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import html as html_lib
import re
from typing import Any
from urllib import parse

import aiohttp
import bs4

from pyelternportal import ElternPortalAPI
from pyelternportal.substitution import Substitution

from .const import LOGGER

TEACHER_URL_PATH: str = "/service/schulinformationen"

_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_SPACE = re.compile(r"\s+")


@dataclass
class SubstitutionExt(Substitution):
    """Substitution with resolved teacher names."""

    original_teacher_name: str = ""
    substitute_teacher_name: str = ""


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


class ElternPortalAPIExt(ElternPortalAPI):
    """ElternPortalAPI with teacher names and cleaned appointment titles."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self.substitution_teacher_names: bool = False
        self.teachers: dict[str, str] = {}
        self._teachers_loaded: bool = False

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

    async def async_substitution_online(self) -> None:
        """Substitution plan with optional resolved teacher names."""
        if self.substitution_teacher_names and not self._teachers_loaded:
            await self.async_teacher_online()
        await super().async_substitution_online()
        if self.substitution_teacher_names:
            self._student.substitutions = [
                SubstitutionExt(
                    **asdict(s),
                    original_teacher_name=resolve_teachers(
                        s.original_teacher, self.teachers
                    ),
                    substitute_teacher_name=resolve_teachers(
                        s.substitute_teacher, self.teachers
                    ),
                )
                for s in self._student.substitutions
            ]

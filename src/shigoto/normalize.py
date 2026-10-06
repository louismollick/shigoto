"""Normalization: clean fields, assign a target city, drop irrelevant jobs, build dedupe keys."""

from __future__ import annotations

import re
import unicodedata

from shigoto.config import City
from shigoto.models import Job
from shigoto.text import clean, clean_description

# "CA" is deliberately absent: Indeed-style locations use it for Canada ("Toronto, ON, CA").
US_STATES = (
    "AL AK AZ AR CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_FOREIGN_WORDS = re.compile(r"\b(united states|usa|united kingdom|england)\b")
_US_STATE_CODE = re.compile(rf",\s*({'|'.join(US_STATES)})\b")  # case-sensitive: "Vancouver, WA"


def fold(text: str) -> str:
    """Lowercase and strip accents so 'Montréal' matches 'montreal'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


class CityMatcher:
    """Maps free-form location text to one of the configured target cities."""

    def __init__(self, cities: list[City]) -> None:
        self._patterns = [
            (city.name, re.compile(r"\b(" + "|".join(re.escape(fold(n)) for n in city.match_names()) + r")\b"))
            for city in cities
        ]

    def match(self, location: str) -> str | None:
        folded = fold(location)
        foreign = _FOREIGN_WORDS.search(folded) or _US_STATE_CODE.search(location)
        if foreign and "canada" not in folded:
            return None
        for name, pattern in self._patterns:
            if pattern.search(folded):
                return name
        return None


def title_matches(title: str, keywords: list[str]) -> bool:
    """Case/accent-insensitive whole-word match ("lab" doesn't match "Labourer"); a
    trailing `*` makes it a prefix ("microbiolog*" matches "Microbiologist")."""
    folded = fold(title)
    return any(
        re.search(r"\b" + re.escape(fold(k.removesuffix("*"))) + ("" if k.endswith("*") else r"\b"), folded)
        for k in keywords
    )


def normalize(job: Job, matcher: CityMatcher, title_keywords: list[str],
              exclude_title_keywords: list[str]) -> Job | None:
    """Clean a job in place and return it, or None if it's outside our cities or its title
    doesn't look relevant (no `title_keywords` prefix, or an excluded whole word)."""
    job.title = clean(job.title)
    job.company = clean(job.company)
    job.location = clean(job.location)
    job.salary = clean(job.salary)
    job.job_type = clean(job.job_type)
    job.description = clean_description(job.description)
    if not job.city:
        job.city = matcher.match(job.location) or ""
    if not job.city or not job.title:
        return None
    if not title_matches(job.title, title_keywords):
        return None
    if title_matches(job.title, exclude_title_keywords):
        return None
    return job


_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|ltee|llc|corp|corporation|co|company|plc|ulc|lp|canada|the)\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_company(company: str) -> str:
    return " ".join(_COMPANY_SUFFIXES.sub(" ", _NON_ALNUM.sub(" ", fold(company))).split())


def norm_title(title: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", fold(title)).split())


def dedupe_key(job: Job) -> str:
    """Cross-source identity: same employer, same title, same target city."""
    return f"{norm_company(job.company)}|{norm_title(job.title)}|{job.city.lower()}"

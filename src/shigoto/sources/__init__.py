"""Source adapters. Every adapter yields canonical `Job`s; the pipeline does the rest."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from shigoto.config import Config
from shigoto.models import Job
from shigoto.normalize import CityMatcher
from shigoto.sources.ats import BoardSource, SmartRecruitersSource, ashby_board, greenhouse_board, lever_board
from shigoto.sources.jobbank import JobBankSource
from shigoto.sources.jobspy_source import JobSpySource
from shigoto.sources.workday import WorkdaySource


class Source(Protocol):
    name: str

    def fetch(self) -> Iterator[Job]: ...


def build_sources(config: Config, matcher: CityMatcher) -> list[Source]:
    def is_target(location: str) -> bool:
        return matcher.match(location) is not None

    terms, boards, keywords = config.search_terms, config.boards, config.ats_title_keywords
    sources: list[Source] = [JobSpySource(s, terms, config.cities) for s in config.jobspy if s.enabled]
    if config.jobbank_enabled:
        sources.append(JobBankSource(terms))
    sources += [
        WorkdaySource(boards.workday, terms, is_target),
        SmartRecruitersSource(boards.smartrecruiters, terms, is_target),
        BoardSource("greenhouse", boards.greenhouse, keywords, greenhouse_board),
        BoardSource("lever", boards.lever, keywords, lever_board),
        BoardSource("ashby", boards.ashby, keywords, ashby_board),
    ]
    return sources

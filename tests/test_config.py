from pathlib import Path

import pytest
from pydantic import ValidationError

from shigoto.config import Config, SheetConfig, load_config


def test_shipped_config_uses_six_month_posting_limit() -> None:
    assert load_config(Path("config.yaml")).max_posting_age_days == 180


@pytest.mark.parametrize("limit", [0, -1])
def test_posting_age_must_be_positive(limit: int) -> None:
    with pytest.raises(ValidationError, match="max_posting_age_days"):
        Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(), max_posting_age_days=limit)


def test_omitted_posting_age_has_no_limit() -> None:
    assert Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig()).max_posting_age_days is None


def test_title_exceptions_must_name_an_exclusion() -> None:
    with pytest.raises(ValidationError, match="seniour"):
        Config(search_terms=[], cities=[], title_keywords=[], sheet=SheetConfig(),
               exclude_title_keywords=["senior"], exclude_title_exceptions={"seniour": ["technician"]})

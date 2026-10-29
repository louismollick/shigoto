from datetime import date
from pathlib import Path

import pytest

from shigoto.config import City, load_config
from shigoto.models import Job
from shigoto.normalize import CityMatcher, dedupe_key, normalize, rejection_reason, title_excluded, title_matches

CITIES = [
    City(name="Toronto", jobspy_location="Toronto, ON", aliases=["Mississauga"]),
    City(name="Montreal", jobspy_location="Montreal, QC", aliases=["Saint-Laurent"]),
    City(name="Vancouver", jobspy_location="Vancouver, BC", aliases=["Burnaby"]),
    City(name="Ottawa", jobspy_location="Ottawa, ON"),
]
matcher = CityMatcher(CITIES)


def job(**kw: object) -> Job:
    base: dict[str, object] = dict(source="indeed", source_id="1", url="u", title="QA Technician",
                                   company="Acme Inc.", location="Toronto, ON")
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def test_city_matching() -> None:
    assert matcher.match("Montréal, Québec, Canada") == "Montreal"
    assert matcher.match("Saint-Laurent (QC)") == "Montreal"
    assert matcher.match("Mississauga, Ontario") == "Toronto"
    assert matcher.match("Remote, Belgium; Burnaby, British Columbia, Canada") == "Vancouver"
    assert matcher.match("Vancouver, WA") is None
    assert matcher.match("Ottawa, IL, United States") is None
    assert matcher.match("Toronto, ON, CA") == "Toronto"
    assert matcher.match("Calgary, AB") is None
    assert matcher.match("Ville de Montréal") == "Montreal"


def test_title_keywords_whole_word_or_prefix() -> None:
    assert title_matches("Microbiologist II", ["microbiolog*"])
    assert title_matches("Sr. QA/QC Specialist", ["QA"])
    assert title_matches("Contrôle qualité", ["qualite"])
    assert title_matches("Lab Operations Support", ["lab"])
    assert not title_matches("AQUA engineer", ["QA"])
    assert not title_matches("Labourer", ["lab"])
    assert title_matches("Line Cook", ["cook"])
    assert not title_matches("QA Technician - Cookies", ["cook"])


def test_normalize_sets_city_and_drops() -> None:
    kw = ["QA", "food safety"]
    j = normalize(job(source="workday", location="Burnaby, BC"), matcher, kw, [], [])
    assert j is not None and j.city == "Vancouver"
    assert normalize(job(location="Calgary, AB"), matcher, kw, [], []) is None
    assert normalize(job(title="Software QA Engineer"), matcher, kw, ["software"], []) is None
    assert normalize(job(title="Line Cook"), matcher, kw, [], []) is None
    preset = normalize(job(location="Guelph, ON", city="Toronto"), matcher, kw, [], [])
    assert preset is not None and preset.city == "Toronto"


def test_dedupe_key_ignores_company_suffix_and_punctuation() -> None:
    a = job(company="Maple Leaf Foods Inc.", title="Quality Assurance Technician (Nights)", city="Toronto")
    b = job(company="maple leaf foods", title="Quality Assurance Technician - Nights", city="Toronto",
            posted_date=date(2026, 1, 1))
    assert dedupe_key(a) == dedupe_key(b)


def test_company_exclusion_uses_word_and_prefix_matching() -> None:
    assert normalize(job(company="MERIEUX NutriSciences"), matcher, ["QA"], [], ["Mérieux"]) is None
    assert normalize(job(company="MérieuxCanada"), matcher, ["QA"], [], ["merieux*"]) is None
    assert normalize(job(company="MérieuxCanada"), matcher, ["QA"], [], ["Mérieux"]) is not None
    assert normalize(job(company="Acme"), matcher, ["QA"], [], ["Mérieux"]) is not None


@pytest.mark.parametrize("separator", [";", "|", " / ", "\n"])
@pytest.mark.parametrize("reverse", [False, True])
def test_multi_location_foreign_component_does_not_veto(separator: str, reverse: bool) -> None:
    parts = ["Toronto, ON", "Boston, MA, United States"]
    if reverse:
        parts.reverse()
    location = separator.join(parts)
    assert matcher.match(location) == "Toronto"
    normalized = normalize(job(location=location, city="Toronto"), matcher, ["QA"], [], [])
    assert normalized is not None and normalized.city == "Toronto"
    assert matcher.match(separator.join(["Vancouver, WA", "Ottawa, IL"])) is None


@pytest.mark.parametrize("separator", [";", "|", " / ", "\n"])
@pytest.mark.parametrize("reverse", [False, True])
def test_multi_location_uses_configured_city_priority(separator: str, reverse: bool) -> None:
    parts = ["Toronto, ON", "Vancouver, BC"]
    if reverse:
        parts.reverse()
    location = separator.join(parts)
    assert matcher.match(location) == matcher.match("Vancouver, BC, Toronto, ON") == "Toronto"
    reversed_matcher = CityMatcher(list(reversed(CITIES)))
    assert reversed_matcher.match(location) == "Vancouver"


@pytest.mark.parametrize("location", ["Toronto, ON; Boston, MA", "Boston, MA; Toronto, ON",
                                     "Toronto, England; Vancouver, BC", "Vancouver, BC; Toronto, England"])
def test_multi_location_drops_foreign_components(location: str) -> None:
    assert matcher.match(location) == ("Vancouver" if "England" in location else "Toronto")


@pytest.mark.parametrize("location,city", [
    ("Rocky View County, AB", "Calgary"), ("Cloverdale, BC", "Vancouver"), ("Mission, BC", "Vancouver"),
    ("Bowmanville, ON", "Toronto"), ("Bradford, ON", "Toronto"), ("Bradford West Gwillimbury, ON", "Toronto"),
])
def test_new_config_aliases(location: str, city: str) -> None:
    config = load_config(Path("config.yaml"))
    assert CityMatcher(config.cities).match(location) == city


@pytest.mark.parametrize("title", ["Bioanalytical Technician", "Chemical Technologist", "Food Inspector",
                                   "Food Inspection Officer", "Preparation Technician", "Diet Technician"])
def test_recall_title_additions(title: str) -> None:
    config = load_config(Path("config.yaml"))
    assert normalize(job(title=title), CityMatcher(config.cities), config.title_keywords,
                     config.exclude_title_keywords, config.exclude_companies) is not None


@pytest.mark.parametrize(("title", "excluded"), [
    ("Senior Quality Lab Technician", False), ("Sr. QA Technician", False), ("Senior Food Technologist", False),
    ("Sr Laboratory Analyst", False), ("Senior QA Specialist", True), ("Senior Lab Manager", True),
    ("Senior Scientist", True),
])
def test_senior_bench_titles_are_kept(title: str, excluded: bool) -> None:
    config = load_config(Path("config.yaml"))
    reason = rejection_reason(job(title=title, city="Toronto"), config.title_keywords + ["specialist", "scientist"],
                              config.exclude_title_keywords, config.exclude_companies,
                              config.exclude_title_exceptions)
    assert (reason == "excluded_title") is excluded


def test_title_exception_applies_only_to_its_keyword() -> None:
    exceptions = {"senior": ["technician"]}
    assert not title_excluded("Senior Technician", ["senior"], exceptions)
    assert title_excluded("Senior Technician Supervisor", ["senior", "supervisor"], exceptions)
    assert title_excluded("Senior Technician", ["senior"])


@pytest.mark.parametrize("location,expected", [
    ("Toronto, ON, CA", "Toronto, ON, Canada"), ("Toronto, ON, Canada", "Toronto, ON, Canada"),
    ("San Diego, CA", "San Diego, CA"), ("Mississauga, ON, CA, L5N", "Mississauga, ON, CA, L5N"),
])
def test_indeed_country_suffix_has_one_spelling(location: str, expected: str) -> None:
    normalized = normalize(job(location=location, city="Toronto"), matcher, ["QA"], [], [])
    assert normalized is not None and normalized.location == expected


@pytest.mark.parametrize("job_type,dropped", [
    ("parttime", True), ("Part-time, Contract", True), ("temporary, parttime", True),
    ("parttime, fulltime", False), ("Full-time", False), ("", False), ("contract", False),
])
def test_part_time_only_job_type_is_rejected(job_type: str, dropped: bool) -> None:
    posting = job(job_type=job_type)
    assert (normalize(posting, matcher, ["QA"], [], []) is None) == dropped
    assert rejection_reason(posting, ["QA"], [], []) == ("job_type" if dropped else None)

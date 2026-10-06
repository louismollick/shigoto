from datetime import date

from shigoto.config import City
from shigoto.models import Job
from shigoto.normalize import CityMatcher, dedupe_key, normalize, title_matches

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


def test_title_keywords_are_word_prefixes() -> None:
    assert title_matches("Microbiologist II", ["microbiolog"])
    assert title_matches("Sr. QA Specialist", ["QA"])
    assert not title_matches("AQUA engineer", ["QA"])
    assert not title_matches("Labourer", ["lab tech"])


def test_normalize_sets_city_and_drops() -> None:
    j = normalize(job(source="workday", location="Burnaby, BC"), matcher, [])
    assert j is not None and j.city == "Vancouver"
    assert normalize(job(location="Calgary, AB"), matcher, []) is None
    assert normalize(job(title="Software QA Engineer"), matcher, ["software"]) is None
    preset = normalize(job(location="Guelph, ON", city="Toronto"), matcher, [])
    assert preset is not None and preset.city == "Toronto"


def test_dedupe_key_ignores_company_suffix_and_punctuation() -> None:
    a = job(company="Maple Leaf Foods Inc.", title="Quality Assurance Technician (Nights)", city="Toronto")
    b = job(company="maple leaf foods", title="Quality Assurance Technician - Nights", city="Toronto",
            posted_date=date(2026, 1, 1))
    assert dedupe_key(a) == dedupe_key(b)

from datetime import date
from urllib.parse import parse_qs, urlparse

from pytest import MonkeyPatch

from shigoto.config import SuccessFactorsBoard
from shigoto.sources import successfactors as sf

BOARD = SuccessFactorsBoard(name="Acme", url="https://jobs.example.com/Brand/search/")
TILES = '''<ul>
<li class="job-tile job-id-101" data-url="/Brand/job/Toronto-Quality-&amp;-Safety/101/">
<a class="jobTitle-link">Quality &amp; Safety</a>
<div id="job-101-section-location-value">Toronto, ON, CA</div>
<div id="job-101-section-date-value">Oct 5, 2026</div>
<a class="jobTitle-link">Quality &amp; Safety</a></li>
<li class="job-tile job-id-102" data-url="/job/Burnaby-Laboratory-Technician/102/">
<a class="jobTitle-link">Laboratory Technician</a></li>
<li class="job-tile job-id-101" data-url="/duplicate"><a class="jobTitle-link">Duplicate</a></li>
<li class="job-tile job-id-103"><a class="jobTitle-link">No URL</a></li>
</ul>'''


def test_board_base_preserves_brand() -> None:
    assert sf.board_base(BOARD.url) == "https://jobs.example.com/Brand"
    assert sf.board_base("https://jobs.example.com/Brand/go/All-Jobs/123/25/") == sf.board_base(BOARD.url)


def test_tiles_dedupe_and_location_fallback() -> None:
    jobs = sf.parse_tiles(TILES, BOARD)
    assert len(jobs) == 2
    assert jobs[0].title == "Quality & Safety"
    assert jobs[0].location == "Toronto, ON, CA"
    assert jobs[0].url == "https://jobs.example.com/Brand/job/Toronto-Quality-&-Safety/101/"
    assert jobs[0].source_id == "https://jobs.example.com/Brand:101"
    assert jobs[0].posted_date == date(2026, 10, 5)
    assert jobs[1].location == "Burnaby"
    assert sf.city_from_slug("/job/New-Westminster-Lab-Technician/1/", "Lab Technician") == "New Westminster"


def test_detail_keeps_only_description() -> None:
    description, posted = sf.parse_detail('''<nav>Menu</nav><meta itemprop="datePosted" content="2026-10-05">
<div class="jobdescription"><p>Test food samples.</p><ul><li>Use HPLC.</li></ul></div><footer>Cookies</footer>''')
    assert "Test food samples." in description and "Use HPLC." in description
    assert "Menu" not in description and "Cookies" not in description
    assert posted == date(2026, 10, 5)


def test_rmk_ignores_repeated_page_and_fetches_only_survivors(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []

    def get_text(url: str) -> str:
        calls.append(url)
        return TILES if "tile-search-results" in url else '<div class="jobdescription">Test samples.</div>'

    monkeypatch.setattr(sf._session, "get_text", get_text)
    source = sf.SuccessFactorsSource([BOARD], ["laboratory", "quality"], ["laboratory", "quality"], lambda s: "Toronto" in s)
    [job] = list(source.fetch())
    assert job.description == "Test samples."
    assert len([u for u in calls if "tile-search-results" not in u]) == 1
    assert len(calls) == 5
    assert parse_qs(urlparse(calls[1]).query)["startrow"] == ["2"]



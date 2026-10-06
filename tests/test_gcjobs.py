from urllib.parse import parse_qs, urlparse

import pytest
import requests
from pytest import MonkeyPatch

from shigoto.config import Config
from shigoto.normalize import CityMatcher
from shigoto.sources import build_sources, gcjobs as gc

RESULTS = '''<div id="searchResults"><ul>
<li class="searchResult"><strong><a href="/psrs-srfp/applicant/page1800;jsessionid=SESSION?poster=123">Laboratory Technician</a></strong>
<div class="tableCell">Closing date: 2026-10-30<br>Canadian Food Inspection Agency<br>Ottawa (Ontario)<br>Toronto (Ontario)</div>
<div class="tableCell">Various language requirements<br>$60,000 to $70,000</div></li>
<li class="searchResult"><strong><a href="page1800?poster=124">Software Developer</a></strong>
<div class="tableCell">Closing date: 2026-10-30<br>Health Canada<br>Ottawa (Ontario)</div></li>
<li class="searchResult"><strong><a href="page1800?poster=125">Laboratory Analyst</a></strong>
<div class="tableCell">Closing date: 2026-10-30<br>Health Canada<br>Montreal (Quebec)</div></li>
</ul><a href="page2440;jsessionid=SESSION?requestedPage=2&amp;fromPage=1">2</a></div>'''
DETAIL = '''<html><nav>Menu</nav><main><ol class="breadcrumb"><li>Home</li></ol>
<h1>Laboratory Technician</h1><p>Closing date: 30 October 2026</p><h2>Duties</h2><p>Test food for bacteria.</p>
<h2>Language requirements</h2><p>Bilingual imperative BBB/BBB</p><script>tracking()</script></main><footer>Copyright</footer></html>'''


def test_results_preserve_locations_language_deadline_and_stable_id() -> None:
    jobs = gc.parse_results(RESULTS)
    job = jobs[0]
    assert job.source_id == "123"
    assert "jsessionid" not in job.url and "toggleLanguage=en" in job.url
    assert job.company == "Canadian Food Inspection Agency"
    assert job.location == "Ottawa (Ontario); Toronto (Ontario)"
    assert job.salary == "$60,000 to $70,000"
    assert job.posted_date is None
    assert "Closing date: 2026-10-30" in job.description
    assert "Various language requirements" in job.description
    assert gc.next_page(RESULTS, 1) == 2
    assert gc.next_page(RESULTS, 2) is None


def test_shell_is_an_error_and_empty_results_are_valid() -> None:
    with pytest.raises(ValueError, match="results fragment"):
        gc.parse_results('<main>The page is being updated. Please wait...</main>')
    assert gc.parse_results('<div id="searchResults">No jobs found</div>') == []


def test_native_detail_keeps_language_requirements() -> None:
    description, external = gc.parse_detail(DETAIL)
    assert not external
    assert "Test food for bacteria." in description
    assert "Bilingual imperative BBB/BBB" in description
    assert all(text not in description for text in ["Menu", "Home", "tracking()", "Copyright"])


def test_external_successfactors_description() -> None:
    description, external = gc.parse_detail('<nav>Menu</nav><span class="jobdescription"><h2>Language requirements</h2><p>English</p><p>Analyze food samples.</p></span>')
    assert "Analyze food samples." in description and "Language requirements" in description
    assert "Menu" not in description and not external


def test_external_handoff() -> None:
    description, external = gc.parse_detail('''<main><h1>You will leave the GC Jobs Web site</h1>
<a href="https://jobs.example.com/posting/123">Laboratory Technician</a></main>''')
    assert external == "https://jobs.example.com/posting/123"
    assert "Laboratory Technician" in description


def test_fetch_filters_before_detail_and_stops_repeated_page(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []

    def get_text(self: gc.GCJobsSource, url: str) -> str:
        calls.append(url)
        return RESULTS if "page2440" in url else DETAIL

    monkeypatch.setattr(gc.GCJobsSource, "_get_text", get_text)
    [job] = list(gc.GCJobsSource(["laboratory"], lambda s: "Ottawa" in s).fetch())
    assert "Test food for bacteria." in job.description
    assert "Closing date: 2026-10-30" in job.description
    assert job.posted_date is None
    assert len(calls) == 3
    assert parse_qs(urlparse(calls[-1]).query)["requestedPage"] == ["2"]


def test_source_registration_respects_toggle() -> None:
    config = Config.model_validate({"search_terms": ["lab"], "cities": [{"name": "Ottawa", "jobspy_location": "Ottawa, ON"}],
                                    "title_keywords": ["lab"], "sheet": {}, "jobbank_enabled": False,
                                    "boards": {"successfactors": [{"name": "Acme", "url": "https://jobs.example.com"}]}})
    sources = build_sources(config, CityMatcher(config.cities))
    assert {"gcjobs", "successfactors"} <= {s.name for s in sources}
    config.gcjobs_enabled = False
    assert "gcjobs" not in {s.name for s in build_sources(config, CityMatcher(config.cities))}


@pytest.mark.parametrize("detail", ["fetch_failure", "<html>No posting content</html>",
                                    '<main><h1>You will leave the GC Jobs Web site</h1><a href="https://example.com/1">Job</a></main>'])
def test_detail_failure_yields_empty_description(monkeypatch: MonkeyPatch, detail: str) -> None:
    def get_text(self: gc.GCJobsSource, url: str) -> str:
        if "page2440" in url:
            return RESULTS
        if detail == "fetch_failure" or url.startswith("https://example.com"):
            raise requests.ConnectionError("unavailable")
        return detail

    monkeypatch.setattr(gc.GCJobsSource, "_get_text", get_text)
    [job] = list(gc.GCJobsSource(["laboratory"], lambda s: "Ottawa" in s).fetch())
    assert job.description == ""

from shigoto.sources.jobbank import parse_feed
from shigoto.sources.jobspy_source import build_queries
from shigoto.sources.workday import endpoint, location_filter

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title type="html"><![CDATA[quality assurance (qa) technician]]></title>
  <link rel="alternate" type="text/html" href="https://www.jobbank.gc.ca/jobsearch/jobposting/50431084"/>
  <updated>2026-10-05T13:34:00Z</updated>
  <summary type="html"><![CDATA[<strong>Job number:</strong> 3688243<br /><strong>Location:</strong> Mississauga (ON)  <br /><strong>Employer:</strong> Acme &amp; Co<br /><strong>Salary:</strong> $28.00 hourly]]></summary>
</entry>
</feed>"""


def test_jobbank_feed() -> None:
    [j] = parse_feed(FEED)
    assert (j.source_id, j.location, j.company, j.salary) == ("50431084", "Mississauga (ON)", "Acme & Co", "$28.00 hourly")
    assert j.posted_date is not None and j.posted_date.isoformat() == "2026-10-05"


def test_linkedin_combined_query() -> None:
    assert build_queries(["microbiology", "food safety"], True) == ['microbiology OR "food safety"']
    assert build_queries(["a", "b"], False) == ["a", "b"]


def test_workday_endpoint_and_facets() -> None:
    ep = endpoint("https://saputo.wd5.myworkdayjobs.com/en-US/Saputo_External_Careers")
    assert ep.jobs_api == "https://saputo.wd5.myworkdayjobs.com/wday/cxs/saputo/Saputo_External_Careers/jobs"
    country = [{"facetParameter": "locationCountry", "values": [{"descriptor": "Canada", "id": "ca1"}]}]
    assert location_filter(country, lambda s: False) == {"locationCountry": ["ca1"]}
    nested = [{"facetParameter": "locationMainGroup", "values": [
        {"descriptor": "Locations", "facetParameter": "locations", "values": [
            {"descriptor": "Canada - Burnaby", "id": "b1"}, {"descriptor": "USA - Boston", "id": "x"}]}]}]
    assert location_filter(nested, lambda s: "Burnaby" in s) == {"locations": ["b1"]}
    assert location_filter(nested, lambda s: False) is None

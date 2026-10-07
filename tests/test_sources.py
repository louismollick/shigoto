import math

import pytest

from shigoto.sources.jobbank import parse_feed
from shigoto.sources.jobspy_source import build_queries, description_salary, format_salary, row_to_job
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


@pytest.mark.parametrize(("description", "expected"), [
    ("Pay Rate: $23.25/hr", "23.25 CAD hourly"),
    ("**Pay Rate:** $23.25/hour", "23.25 CAD hourly"),
    ("Compensation: $23.25 - $25.50 per hour", "23.25 - 25.5 CAD hourly"),
    ("Earn $23.25 to $25.50 an hour", "23.25 - 25.5 CAD hourly"),
    ("Salary: $65,000–$75,000 a year", "65,000 - 75,000 CAD yearly"),
    ("Salary: CAD $65k - CAD $75k annually", "65,000 - 75,000 CAD yearly"),
    ("Pay Range $23.25—$23.25 CAD", "23.25 CAD"),
    ("Salary: $65,000 to $75,000.", "65,000 - 75,000 CAD"),
    ("Pay Rate: $23.25.", "23.25 CAD"),
    ("Wage: C$25 hourly", "25 CAD hourly"),
    ("US$25.75/hr", "25.75 USD hourly"),
    ("$25.75 USD per hour", "25.75 USD hourly"),
    ("$25.75 per hour USD", "25.75 USD hourly"),
    ("$4,000 per month", "4,000 CAD monthly"),
    ("$1,000 weekly", "1,000 CAD weekly"),
    ("$200/day", "200 CAD daily"),
    ("$500 signing bonus. Pay Rate: $23.25/hr", "23.25 CAD hourly"),
    ("$500 wellness allowance and 75% off meal kits", ""),
    ("An estimated hourly pay of $22.77 CAD at the time of posting.\n\nAfternoon Shift Premium $2.00/hour",
     "22.77 CAD hourly"),
    ("Afternoon Shift Premium $2.00/hour. Pay Rate: $23.25/hr", "23.25 CAD hourly"),
    ("Night shift differential: $1.50/hr", ""),
    ("The annual salary is $60,000.", "60,000 CAD yearly"),
    ("Annual salary starts at $64k. Compensation is based on experience", "64,000 CAD yearly"),
    ("Competitive salary and health benefits", ""),
    ("Salary: $0/hr", ""),
    ("Salary: $30 - $20/hour", ""),
    ("", ""),
])
def test_description_salary(description: str, expected: str) -> None:
    assert description_salary(description) == expected


@pytest.mark.parametrize(("lo", "hi", "expected"), [
    (23.25, 23.25, "23.25 CAD hourly"),
    (23.25, 25.5, "23.25 - 25.5 CAD hourly"),
    (23.0, None, "23 CAD hourly"),
    (None, 23.25, "23.25 CAD hourly"),
    (math.nan, math.nan, "23.25 CAD hourly"),
    (None, None, "23.25 CAD hourly"),
])
def test_jobspy_salary(lo: float | None, hi: float | None, expected: str) -> None:
    assert format_salary({
        "min_amount": lo, "max_amount": hi, "currency": "CAD", "interval": "hourly",
        "description": "Pay Rate: $23.25/hr",
    }) == expected


def test_jobspy_structured_salary_takes_priority() -> None:
    assert format_salary({
        "min_amount": 65000, "max_amount": 75000, "currency": "CAD", "interval": "yearly",
        "description": "Pay Rate: $23.25/hr",
    }) == "65,000 - 75,000 CAD yearly"


def test_jobspy_description_pay_reaches_job() -> None:
    job = row_to_job({
        "site": "indeed", "id": "in-7e2199762aa53f88", "title": "FSQA Technician",
        "job_url": "https://ca.indeed.com/viewjob?jk=7e2199762aa53f88", "company": "HelloFresh",
        "description": "Location: Calgary\nPay Rate: $23.25/hr\nAbout the Role",
        "min_amount": math.nan, "max_amount": math.nan, "currency": math.nan,
    }, "Calgary")
    assert job is not None
    assert job.salary == "23.25 CAD hourly"


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

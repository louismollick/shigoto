import json as jsonlib
import math

import pytest
import requests
from jobspy import scrape_jobs
from pytest import MonkeyPatch

from shigoto.config import SlugBoard
from shigoto.sources import ats
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
    ("$29.99 an hour", "29.99 CAD hourly"),
    ("The hourly rate for the role is $29.99 with a night shift premium of $1", "29.99 CAD hourly"),
    ("The hourly rate is $29.99", "29.99 CAD hourly"),
    ("Hourly rate: $29.99", "29.99 CAD hourly"),
    ("Hourly rate for this position: $29.99", "29.99 CAD hourly"),
    ("The annual salary for the role is $64,000", "64,000 CAD yearly"),
    ("The reimbursement rate is $0.70", ""),
    ("Night shift premium hourly rate: $1", ""),
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


@pytest.mark.parametrize("estimated", [False, True])
@pytest.mark.parametrize("bounds,expected", [
    ({"value": 22.25}, "22.25 CAD hourly"),
    ({"min": 20.5, "max": 25}, "20.5 - 25 CAD hourly"),
    ({"min": 20.5, "max": None}, "20.5 CAD hourly"),
])
def test_indeed_compensation_reaches_job(monkeypatch: MonkeyPatch, estimated: bool,
                                       bounds: dict[str, float | None], expected: str) -> None:
    session = requests.Session()

    def post(url: str, *, json: dict[str, str], **kwargs: object) -> requests.Response:
        query = json["query"]
        # Both employer pay and estimates must request Exactly as well as Range.
        assert query.count("... on Exactly {") == 2
        assert query.count("... on Range {") == 2
        salary = {"unitOfWork": "HOUR", "range": bounds}
        compensation = {
            "currencyCode": "CAD", "baseSalary": None if estimated else salary,
            "estimated": {"currencyCode": "CAD", "baseSalary": salary} if estimated else None,
        }
        job = {
            "key": "2032e128ccf2a724", "title": "QC Technician - Raw Product",
            "description": {"html": "<p>Test raw product.</p>"}, "dateOnIndeed": 1791417600000,
            "employer": None, "sourceEmployerName": None, "recruit": {"viewJobUrl": None}, "attributes": [],
            "location": {"city": "Toronto", "admin1Code": "ON", "countryCode": "CA",
                         "formatted": {"long": "Toronto, ON"}},
            "compensation": compensation,
        }
        response = requests.Response()
        response.status_code = 200
        response._content = jsonlib.dumps({"data": {"jobSearch": {
            "results": [{"job": job}], "pageInfo": {"nextCursor": None},
        }}}).encode()
        return response

    monkeypatch.setattr(session, "post", post)
    monkeypatch.setattr("jobspy.indeed.create_session", lambda *args, **kwargs: session)
    df = scrape_jobs(site_name=["indeed"], search_term="QC Technician Ya Ya Foods",
                     location="Toronto, ON", country_indeed="Canada", results_wanted=1, verbose=0)
    [row] = df.to_dict("records")
    job = row_to_job({str(key): value for key, value in row.items()}, "Toronto")
    assert job is not None and job.salary == expected


def test_jobspy_salary_without_interval_or_currency() -> None:
    assert format_salary({"min_amount": 22.25}) == "22.25 CAD"


@pytest.mark.parametrize("compensation,expected", [
    ({"min": 20.5, "max": 20.5, "currency": "CAD", "period": "HOURLY"}, "20.5 CAD hourly"),
    ({"min": 31, "max": 41, "currency": "CAD", "period": "HOURLY"}, "31 - 41 CAD hourly"),
    ({"min": 55000, "currency": "CAD", "period": "YEARLY"}, "55,000 CAD yearly"),
    ({"max": 60000, "currency": "USD", "period": "YEARLY"}, "60,000 USD yearly"),
    ({"min": 25}, "25 CAD"),
    ({}, ""), (None, ""),
])
def test_smartrecruiters_structured_salary(monkeypatch: MonkeyPatch,
                                         compensation: dict[str, object] | None, expected: str) -> None:
    monkeypatch.setattr(ats._session, "get_json", lambda url: {
        "compensation": compensation,
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Test samples.</p>"}}},
    })
    source = ats.SmartRecruitersSource([], [], [], lambda location: True)
    job = source._detail(SlugBoard(name="SGS", slug="SGS"), {"id": "1", "name": "Lab Technician"}, "Toronto")
    assert job.salary == expected
    assert "Test samples." in job.description


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

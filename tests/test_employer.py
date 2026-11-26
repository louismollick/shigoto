from __future__ import annotations

import pytest

from shigoto.employer import conflicts, employer_link, external_apply_url, requisition
from shigoto.models import SourceLink


@pytest.mark.parametrize("url,expected", [
    ("https://acme.example/job/1", "https://acme.example/job/1"),
    ("http://acme.example/job/1", "http://acme.example/job/1"),
    ("http://ca.indeed.com/job/1", ""), ("https://www.ziprecruiter.com/jobs/1", ""),
    ("javascript:alert(1)", ""), ("", ""), ("http://[broken", ""), ("not a url", ""),
])
def test_external_apply_url(url: str, expected: str) -> None:
    assert external_apply_url(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Toronto/QA_R1?source=Indeed",
     SourceLink("workday", "acme.wd3.myworkdayjobs.com:", "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Toronto/QA_R1")),
    ("https://acme.wd3.myworkdayjobs.com/Careers/job/Toronto/QA_R1/apply/autofillWithResume",
     SourceLink("workday", "acme.wd3.myworkdayjobs.com:", "https://acme.wd3.myworkdayjobs.com/Careers/job/Toronto/QA_R1")),
    ("https://boards.greenhouse.io/acme/jobs/123",
     SourceLink("greenhouse", "acme:123", "https://boards.greenhouse.io/acme/jobs/123")),
    ("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x",
     SourceLink("greenhouse", "acme:123", "https://job-boards.greenhouse.io/acme/jobs/123?gh_src=x")),
    ("https://jobs.lever.co/acme/87a54373-296e-4a28-abb2-00a601414c38?lever-source=Indeed",
     SourceLink("lever", "acme:87a54373-296e-4a28-abb2-00a601414c38",
                "https://jobs.lever.co/acme/87a54373-296e-4a28-abb2-00a601414c38?lever-source=Indeed")),
    ("https://ibanqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/119?utm_source=ziprecruiter",
     SourceLink("oracle", "CX_4:119", "https://ibanqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/119")),
    ("https://a.fa.ca3.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/CX_2001/requisitions/preview/96117",
     SourceLink("oracle", "CX_2001:96117", "https://a.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/requisitions/preview/96117")),
    ("https://a.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/fr-CA/sites/CX_1004/jobs/preview/96416",
     SourceLink("oracle", "CX_1004:96416", "https://a.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/fr-CA/sites/CX_1004/jobs/preview/96416")),
    ("https://olymel.jobs.hr.cloud.sap/job/Technicien-Quality-Assurance/1539-en_US?idpartenaire=10119",
     SourceLink("successfactors", "olymel.jobs.hr.cloud.sap:",
                "https://olymel.jobs.hr.cloud.sap/job/Technicien-Quality-Assurance/1539-en_US")),
])
def test_recognized_ats_urls(url: str, expected: SourceLink) -> None:
    assert employer_link(url) == expected


@pytest.mark.parametrize("url", [
    "https://careers.acme.example/job/1",
    "https://grnh.se/tkcmcz681us",  # a shortlink hides the board and posting IDs
    "https://a.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/job/14489",  # no site ID to ask with
    "https://acme.wd3.myworkdayjobs.com/en-US/Careers",  # a board, not a posting
    "https://jobs.lever.co/acme",
    "https://evil.example/?u=.myworkdayjobs.com/job/1",
    "ftp://acme.wd3.myworkdayjobs.com/x/job/y/z",
    "https://acme.smartrecruiters.com/job/1",
    "https://career4.successfactors.com/career?company=X",  # robots.txt blocks these pages
])
def test_unchecked_urls(url: str) -> None:
    assert employer_link(url) is None



@pytest.mark.parametrize("urls", [
    # Aggregators link one Workday requisition with or without the location, slug and /apply.
    ["https://chg.wd5.myworkdayjobs.com/en-US/CAN_External_Career_Site/job/Calgary-AB---Sons-Bakery/Quality-Assurance-Technician_R820542",
     "https://chg.wd5.myworkdayjobs.com/CAN_External_Career_Site/job/Quality-Assurance-Technician_R820542/apply?utm_source=ziprecruiter"],
    ["https://southlake.wd3.myworkdayjobs.com/en-US/Careers/job/Newmarket/Lab-Tech_JR-102075",
     "https://southlake.wd3.myworkdayjobs.com/Careers/job/Lab-Tech_JR-102075/apply"],
    ["https://element-ext-row.icims.com/jobs/20691/job?utm_source=indeed_integration",
     "https://element-ext-row.icims.com/jobs/20691/laboratory-technician-i-%28fixed-term%29/job?mode=apply"],
    ["https://boards.greenhouse.io/acme/jobs/7698887003", "https://acme.example/careers?gh_jid=7698887003"],
    ["https://jobs.dayforcehcm.com/en-CA/wafna/CANCAREERSITE/jobs/5550", "https://jobs.dayforcehcm.com/en-US/wafna/X/jobs/5550"],
    ["https://emit.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/requisitions/preview/97828",
     "https://emit.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1005/job/97828?utm_source=ziprecruiter"],
    ["https://careers.cargill.com/en/job/-/-/23251/102816420032",
     "https://careers.cargill.com/en/job/brampton/food-safety-technician/23251/102816420032?feedId=244200"],
    ["https://careers.opentext.com/us/en/job/53990", "https://careers.opentext.com/us/en/job/53990?utm_source=ziprecruiter"],
    ["https://indeed.njoyn.com/cl4/xweb/XWeb.asp?page=jobdetails&clid=74950&JobID=J0325-0598&BRID=413047",
     "https://indeed.njoyn.com/cl4/xweb/XWeb.asp?page=jobdetails&clid=74950&JobID=J0325-0598&BRID=413047&lang=1"],
    ["https://careers.blood.ca/job/Calgary-Validation-Associate-AB-T3N-1S5/607435017/?utm_source=ZipRecruiter",
     "https://careers.blood.ca/job/Calgary-Validation-Associate/607435017/"],
])
def test_requisition_matches_across_link_shapes(urls: list[str]) -> None:
    first, second = (requisition(url) for url in urls)
    assert first is not None and first == second


@pytest.mark.parametrize("first,second", [
    # A reposted Workday requisition gets a suffix: a separate posting.
    ("https://ubc.wd10.myworkdayjobs.com/x/job/RA_JR26429", "https://ubc.wd10.myworkdayjobs.com/x/job/RA_JR26429-1"),
    # Dayforce and ADP number jobs per tenant.
    ("https://jobs.dayforcehcm.com/en-CA/wafna/SITE/jobs/5550", "https://jobs.dayforcehcm.com/en-US/other/SITE/jobs/5550"),
    ("https://workforcenow.adp.com/r.html?cid=a&jobId=590689", "https://workforcenow.adp.com/r.html?cid=b&jobId=590689"),
    ("https://apply.workable.com/j/D42B350581", "https://apply.workable.com/rentokil-initial/j/5E973C934A/apply"),
    # Two UKG postings on one board share the board UUID, not the opportunity.
    ("https://recruiting.ultipro.com/REF1/JobBoard/9dc59b6d-6dab-454e-883b-4c8c6d36d52d/OpportunityDetail?opportunityId=5e6f940b-1c16-44fe-b872-3826aced90ed",
     "https://recruiting.ultipro.com/REF1/JobBoard/9dc59b6d-6dab-454e-883b-4c8c6d36d52d/OpportunityDetail?opportunityId=c245bd57-51e4-42ef-b7fb-26a18d6d4070"),
])
def test_different_requisitions_stay_apart(first: str, second: str) -> None:
    assert requisition(first) != requisition(second)


@pytest.mark.parametrize("url", [
    "https://ca.indeed.com/viewjob?jk=1", "https://grnh.se/gqezs70k8us", "https://jobs.phsa.ca/job/lab-assistant", "",
    "https://www.healthcarecan.ca/2026/10/20/combined-laboratory-x-ray-technologist/",
    "https://acme.wd3.myworkdayjobs.com/en-US/External_Site_2",
    "https://recruiting.ultipro.com/REF1/JobBoard/9dc59b6d-6dab-454e-883b-4c8c6d36d52d/OpportunityDetail?postingId=1",
    "https://careers.example.com/jobs/category/2026/123",
])
def test_no_requisition_without_a_known_job_link(url: str) -> None:
    assert requisition(url) is None


def test_conflicts_only_within_one_tenant() -> None:
    assert conflicts({"workday:acme|r1"}, {"workday:acme|r2"})
    assert not conflicts({"workday:acme|r1"}, {"workday:acme|r1", "jobvite|x"})
    assert not conflicts({"workday:acme|r1"}, {"workday:other|r2"})
    assert conflicts({"workday:acme|r1", "workday:acme|r2"}, {"workday:acme|r2"})

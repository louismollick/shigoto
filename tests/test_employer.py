from __future__ import annotations

import pytest

from shigoto.employer import employer_link, external_apply_url
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

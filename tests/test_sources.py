"""Discovery sources: parsing, pagination, error isolation, X-ray provider behavior.
All HTTP is mocked — no real network access in tests."""
import base64
import json

import httpx
import pytest

from app.pipeline import RawJob
from app.sources.ats_sources import (AshbySource, GreenhouseSource, LeverSource,
                                     SmartRecruitersSource, WorkableSource)
from app.sources.base import Source, SourceResult
from app.sources.marketplace_sources import (ArbeitnowSource, HNWhoIsHiringSource,
                                             RemotiveSource)
from app.sources.xray import (ATS_XRAY_DOMAINS, XraySource, build_query_plan,
                              make_provider)


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def gh_handler(request):
    content = base64.b64encode(b"<p>Python FastAPI backend</p>").decode()
    return httpx.Response(200, json={"name": "Acme", "jobs": [
        {"id": 101, "title": "Python Backend Developer",
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
         "updated_at": "2026-11-01T00:00:00-04:00",
         "location": {"location": "Remote"},
         "content": content},
        {"id": 102, "title": "Sales Manager",
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
         "location": {"location": "New York"}},
    ]})


@pytest.mark.asyncio
async def test_greenhouse_parses_jobs():
    source = GreenhouseSource()
    async with mock_client(gh_handler) as client:
        res = await source.discover(client, {"company_handles": {"greenhouse": ["acme"]}})
    assert len(res.jobs) == 2
    j = res.jobs[0]
    assert j.title == "Python Backend Developer"
    assert j.ats == "greenhouse" and j.ats_job_id == "101"
    assert j.company == "Acme"
    assert "Python FastAPI backend" in j.description
    assert res.jobs[1].title == "Sales Manager"


def lever_handler(request):
    return httpx.Response(200, json=[
        {"id": "abc", "text": "Python Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc",
         "applyUrl": "https://jobs.lever.co/acme/abc/apply",
         "createdAt": 1730000000000,
         "categories": {"location": "Remote", "commitment": "Full-time"},
         "workplaceType": "remote", "descriptionPlain": "Python Django"},
    ])


@pytest.mark.asyncio
async def test_lever_parses_jobs():
    source = LeverSource()
    async with mock_client(lever_handler) as client:
        res = await source.discover(client, {"company_handles": {"lever": ["acme"]}})
    assert len(res.jobs) == 1
    j = res.jobs[0]
    assert j.ats == "lever" and j.remote is True
    assert j.ats_job_id == "abc"


def ashby_handler(request):
    return httpx.Response(200, json={"name": "Acme", "jobs": [
        {"id": "a1", "title": "Django Developer", "location": "Remote",
         "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
         "applyUrl": "https://jobs.ashbyhq.com/acme/a1/application",
         "descriptionHtml": "<p>Django</p>", "isRemote": True,
         "publishedAt": "2026-11-01"},
    ]})


@pytest.mark.asyncio
async def test_ashby_parses_jobs():
    source = AshbySource()
    async with mock_client(ashby_handler) as client:
        res = await source.discover(client, {"company_handles": {"ashby": ["acme"]}})
    assert len(res.jobs) == 1
    assert res.jobs[0].ats == "ashby"
    assert res.jobs[0].apply_url.endswith("/application")


def workable_handler(request):
    return httpx.Response(200, json={"name": "Acme", "jobs": [
        {"title": "FastAPI Developer", "shortcode": "ABC123", "remote": True,
         "url": "https://apply.workable.com/acme/j/ABC123/",
         "locations": [{"city": "Lahore", "country": "Pakistan", "remote": True}],
         "published_on": "2026-11-02", "full_description": "FastAPI"},
    ]})


@pytest.mark.asyncio
async def test_workable_parses_jobs():
    source = WorkableSource()
    async with mock_client(workable_handler) as client:
        res = await source.discover(client, {"company_handles": {"workable": ["acme"]}})
    assert len(res.jobs) == 1
    j = res.jobs[0]
    assert j.ats == "workable" and j.ats_job_id == "ABC123"
    assert "Lahore" in j.location


def sr_handler(request):
    if "offset=0" in str(request.url):
        return httpx.Response(200, json={"totalFound": 1, "content": [
            {"id": "900", "name": "Python Software Engineer",
             "releasedDate": "2026-11-01", "location": {"city": "Lahore", "country": "pk"},
             "typeOfEmployment": {"label": "Full-time"}, "remote": True},
        ]})
    return httpx.Response(200, json={"totalFound": 1, "content": []})


@pytest.mark.asyncio
async def test_smartrecruiters_parses_and_stops_pagination():
    source = SmartRecruitersSource()
    async with mock_client(sr_handler) as client:
        res = await source.discover(client, {"company_handles": {"smartrecruiters": ["acme"]}})
    assert len(res.jobs) == 1
    j = res.jobs[0]
    assert j.ats == "smartrecruiters" and j.ats_job_id == "900"
    assert j.url == "https://careers.smartrecruiters.com/acme/900"


def remotive_handler(request):
    return httpx.Response(200, json={"jobs": [{
        "id": 1, "url": "https://remotive.com/jobs/1", "title": "Python Backend Engineer",
        "company_name": "Acme", "candidate_required_location": "Worldwide",
        "job_type": "full_time", "publication_date": "2026-11-01T00:00:00",
        "salary": "$60k", "description": "Python FastAPI"}]})


@pytest.mark.asyncio
async def test_remotive_marks_remote():
    async with mock_client(remotive_handler) as client:
        res = await RemotiveSource().discover(client, {})
    assert res.jobs[0].remote is True
    assert res.jobs[0].location == "Worldwide"


def arbeitnow_handler(request):
    return httpx.Response(200, json={"data": [{
        "slug": "x", "company_name": "Acme", "title": "Backend Developer",
        "description_html": "<p>Python</p>", "remote": True, "url": "https://a.com/j/1",
        "tags": ["python"], "job_types": ["full-time"], "region": "Remote",
        "created_at": 1730000000}]})


@pytest.mark.asyncio
async def test_arbeitnow_parses_jobs():
    async with mock_client(arbeitnow_handler) as client:
        res = await ArbeitnowSource().discover(client, {})
    assert res.jobs[0].title == "Backend Developer"
    assert res.jobs[0].remote is True


class BoomSource(Source):
    id = "boom"
    name = "Boom"


@pytest.mark.asyncio
async def test_source_error_isolation_returns_errors_not_raise():
    class Exploding(Source):
        id = "exploding"
        name = "Exploding"

        async def _run(self, client, cfg):
            raise RuntimeError("network exploded")

    res = await Exploding().discover(None, {})
    assert isinstance(res, SourceResult)
    assert "network exploded" in res.errors[0]
    assert res.jobs == []


def test_xray_query_plan_covers_all_required_domains_and_titles():
    plans = build_query_plan({"xray_queries_per_run": 100})
    queries = " ".join(p.query for p in plans)
    for domain in ATS_XRAY_DOMAINS:
        assert f"site:{domain}" in queries
    for title in ("Python Backend Developer", "FastAPI Developer",
                  "Django Developer", "Backend Software Engineer"):
        assert title in queries


@pytest.mark.asyncio
async def test_xray_without_credentials_records_limitation_and_no_jobs():
    async with mock_client(lambda r: httpx.Response(500)) as client:
        res = await XraySource().discover(client, {"xray_provider": "serpapi"})
    assert res.jobs == []
    assert any("no authorized search-provider credentials" in e for e in res.errors)


@pytest.mark.asyncio
async def test_xray_with_serpapi_key_parses_organic_results(monkeypatch):
    calls = []

    def serp_handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"organic_results": [
            {"title": "Python Backend Developer at Acme", "link": "https://jobs.lever.co/acme/1",
             "snippet": "Remote worldwide Python backend role"},
        ]})

    async with mock_client(serp_handler) as client:
        res = await XraySource().discover(client, {
            "xray_provider": "serpapi", "serpapi_key": "test-key",
            "xray_queries_per_run": 2, "xray_max_pages": 1,
            "xray_results_per_query": 10,
        })
    assert len(res.jobs) >= 1
    assert res.jobs[0].url == "https://jobs.lever.co/acme/1"
    assert res.jobs[0].source_id.startswith("google_xray")
    assert calls and "serpapi.com" in calls[0]


def test_provider_factory_requires_credentials():
    assert make_provider({"xray_provider": "serpapi"}) is None
    assert make_provider({"xray_provider": "google_pse"}) is None
    assert make_provider({"xray_provider": "serpapi", "serpapi_key": "k"}) is not None


def hn_handler(request):
    if "items/" in str(request.url):
        return httpx.Response(200, json={"children": [
            {"text": 'Acme | Python Backend Engineer | Remote | Full-time<p>Python, FastAPI. '
                     'Apply <a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme&#x2F;555">here</a>'},
            {"text": "Acme | Sales Lead | Berlin | ONSITE<p>Sales role"},
        ]})
    return httpx.Response(200, json={"hits": [{"objectID": "999", "title": "Ask HN: Who is hiring?"}]})


@pytest.mark.asyncio
async def test_hn_unescapes_links_and_extracts_leads():
    async with mock_client(hn_handler) as client:
        res = await HNWhoIsHiringSource().discover(client, {})
    assert len(res.jobs) == 1
    j = res.jobs[0]
    assert j.url == "https://jobs.lever.co/acme/555"
    assert "Python Backend Engineer" in j.title

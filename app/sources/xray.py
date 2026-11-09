"""Google X-ray search through a permitted search provider.

Providers:
  * SerpAPI (engine=google)   — authorized commercial API.
  * Google Programmable Search (Custom Search JSON API) — authorized Google API.

The provider is configurable (config.xray_provider). Without credentials the
limitation is recorded (never silently skipped and never fabricated) and other
discovery sources continue to operate.

Search results are DISCOVERY LEADS only. Every lead is verified against the
original employer listing before it can enter the eligible queue.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import quote_plus

import httpx

from ..pipeline import RawJob
from .base import Source, SourceResult

# Required ATS job-board domains for X-ray targeting.
ATS_XRAY_DOMAINS = [
    "boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "careers.smartrecruiters.com",
]

PRIMARY_QUERIES = [
    '"Python Backend Developer"',
    '"Python Backend Engineer"',
    '"Backend Developer" python',
    '"Backend Software Engineer" python',
    '"Python Software Engineer"',
    '"FastAPI Developer"',
    '"Django Developer"',
    '"Django REST Framework" developer',
]

SECONDARY_QUERIES = [
    '"Full Stack Developer" python',
    '"Full Stack Engineer" python',
]

REMOTE_MODIFIERS = ["", " remote", " remote worldwide", " remote Pakistan"]


@dataclass
class QueryPlan:
    ats_domain: str | None
    query: str
    page: int = 1


def build_query_plan(cfg: dict) -> list[QueryPlan]:
    """Dynamically combine ATS domain x job title x remote/freshness modifiers."""
    max_queries = int(cfg.get("xray_queries_per_run", 24))
    plans: list[QueryPlan] = []
    for domain in ATS_XRAY_DOMAINS:
        for i, title_q in enumerate(PRIMARY_QUERIES + SECONDARY_QUERIES):
            mod = REMOTE_MODIFIERS[i % len(REMOTE_MODIFIERS)] if domain.endswith(".io") else ""
            plans.append(QueryPlan(ats_domain=domain, query=f"site:{domain} {title_q} {mod}".strip()))
    for title_q in PRIMARY_QUERIES[:6]:
        plans.append(QueryPlan(ats_domain=None, query=f"{title_q} remote jobs"))
    return plans[:max_queries]


class XrayProviderBase:
    name = "base"
    def __init__(self, cfg: dict):
        self.cfg = cfg
    async def search(self, client: httpx.AsyncClient, plan: QueryPlan) -> tuple[list[dict], str, bool]:
        """Returns (organic_results, error, rate_limited)."""
        raise NotImplementedError


class SerpAPIProvider(XrayProviderBase):
    name = "serpapi"

    async def search(self, client, plan):
        params = {
            "engine": "google", "q": plan.query, "num": self.cfg.get("xray_results_per_query", 10),
            "start": (plan.page - 1) * 10,
        }
        resp = await client.get("https://serpapi.com/search.json", params=params)
        if resp.status_code in (429, 401):
            return [], f"serpapi HTTP {resp.status_code}", resp.status_code == 429
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return [], f"serpapi error: {data['error']}", False
        return data.get("organic_results") or [], "", False


class GooglePSEProvider(XrayProviderBase):
    name = "google_pse"

    async def search(self, client, plan):
        params = {
            "q": plan.query,
            "num": min(10, self.cfg.get("xray_results_per_query", 10)),
            "start": (plan.page - 1) * 10 + 1,
        }
        resp = await client.get("https://www.googleapis.com/customsearch/v1", params=params)
        if resp.status_code == 429:
            return [], "google_pse HTTP 429", True
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return [], f"google_pse error: {data['error'].get('message')}", False
        items = data.get("items") or []
        organic = [{"title": it.get("title"), "link": it.get("link"),
                    "snippet": it.get("snippet")} for it in items]
        return organic, "", False


def make_provider(cfg: dict) -> XrayProviderBase | None:
    provider_name = (cfg.get("xray_provider") or "none").lower()
    if provider_name == "serpapi" and cfg.get("serpapi_key"):
        return SerpAPIProvider(cfg)
    if provider_name == "google_pse" and cfg.get("google_pse_key") and cfg.get("google_pse_cx"):
        return GooglePSEProvider(cfg)
    return None


class XraySource(Source):
    id = "google_xray"
    name = "Google X-ray search"
    requires_credentials = True
    credential_note = "Needs SerpAPI key or Google Programmable Search key + cx."

    async def _run(self, client, cfg):
        out = SourceResult()
        provider = make_provider(cfg)
        if provider is None:
            out.errors.append(
                "X-ray skipped: no authorized search-provider credentials configured. "
                "This limitation is recorded; all other discovery sources are unaffected.")
            return out
        plans = build_query_plan(cfg)
        max_pages = int(cfg.get("xray_max_pages", 3))
        err, rl = "", False
        for plan in plans:
            for page in range(1, max_pages + 1):
                plan.page = page
                try:
                    results, err, rl = await provider.search(client, plan)
                    out.pages_processed += 1
                    if rl:
                        out.rate_limited = True
                        break
                    if err:
                        break
                except httpx.HTTPError as exc:
                    out.errors.append(f"{plan.query!r} p{page}: {type(exc).__name__}: {exc}")
                    break
                if not results:
                    break
                for r in results:
                    if not r.get("link"):
                        continue
                    out.jobs.append(RawJob(
                        title=(r.get("title") or "").split(" | ")[0].split(" – ")[0].strip(),
                        company="",  # resolved during listing verification
                        url=r.get("link"),
                        description=r.get("snippet") or "",
                        remote=None,
                        source_id=f"{self.id}:{provider.name}",
                        source_url=plan.query,
                    ))
                await asyncio.sleep(0.5)
            out.pagination_progress = f"completed {plans.index(plan) + 1}/{len(plans)} queries"
        return out

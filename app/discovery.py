"""Discovery ingestion: run all sources, verify listings, dedupe, persist, evaluate.

Every source feeds the same shared pipeline. A failing source never blocks the
others. X-ray and HN leads require live verification of the original employer
listing before they can enter the eligible application queue.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from . import config as app_config
from .db import AppStatus, Job, SourceRun, get_session, init_engine, log_event
from .eligibility import evaluate_job
from .pipeline import (RawJob, NormalizedJob, detect_ats_from_url,
                       normalize_job)
from .profile import load_exclusions
from .sources.ats_sources import ATS_SOURCES
from .sources.base import Source, SourceResult, make_client
from .sources.marketplace_sources import MARKETPLACE_SOURCES
from .sources.xray import XraySource

_exclusions_cache: dict | None = None


def _exclusions() -> dict:
    global _exclusions_cache
    if _exclusions_cache is None:
        _exclusions_cache = load_exclusions(app_config.load_config())
    return _exclusions_cache

_JOB_URL_OK = re.compile(
    r"(<html|<!doctype|job|position|opening|career|apply|greenhouse|lever|ashby|workable|smartrecruiters)", re.I)

_CONCURRENCY_SOURCES = 4


def all_sources() -> list[Source]:
    return ATS_SOURCES + MARKETPLACE_SOURCES + [XraySource()]


@dataclass
class IngestStats:
    total_raw: int = 0
    stored_new: int = 0
    duplicates: int = 0
    ineligible: int = 0
    eligible: int = 0


async def _verify_listing(client: httpx.AsyncClient, url: str) -> tuple[bool, str, str]:
    """Fetch the original employer listing page. Returns (ok, page_brief, error)."""
    if not url or not url.startswith("http"):
        return False, "", "missing or invalid URL"
    try:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; job-agent)"})
    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return False, "", f"HTTP {resp.status_code}"
    body = resp.text[:4000]
    if not _JOB_URL_OK.search(body):
        return False, body[:200], "page content does not look like a job listing"
    title_match = re.search(r"<title[^>]*>([^<]{5,200})</title>", body, re.I)
    return True, (title_match.group(1).strip() if title_match else "")[:200], ""


async def _verify_and_store(client: httpx.AsyncClient, raws: list[RawJob],
                            cfg: dict, needs_page_verification: set[str]) -> IngestStats:
    """Verify search leads against real pages, then normalize/dedupe/evaluate/store."""
    stats = IngestStats(total_raw=len(raws))
    to_verify = [(i, r) for i, r in enumerate(raws)
                 if r.source_id.split(":")[0] in needs_page_verification]
    sem = asyncio.Semaphore(6)

    async def verify_one(i: int, r: RawJob):
        async with sem:
            ok, _brief, err = await _verify_listing(client, r.apply_url or r.url)
        return (i, ok, err)

    page_verified: dict[int, bool] = {}
    failed_verify: dict[int, str] = {}
    if to_verify:
        for i, ok, err in await asyncio.gather(*[verify_one(i, r) for i, r in to_verify]):
            page_verified[i] = ok
            if not ok:
                failed_verify[i] = err

    normalized: list[tuple[int, str, NormalizedJob]] = []
    seen_keys: set[str] = set()
    for i, raw in enumerate(raws):
        n = normalize_job(raw)
        if not n.title or not n.canonical_url or not n.dedupe_key:
            continue
        if n.dedupe_key in seen_keys:
            stats.duplicates += 1
            continue
        seen_keys.add(n.dedupe_key)
        normalized.append((i, n.dedupe_key, n))

    session = get_session()
    try:
        existing_keys = {k for (k,) in session.query(Job.dedupe_key) if k}
        for i, key, n in normalized:
            if key in existing_keys:
                stats.duplicates += 1
                continue
            exclusions = _exclusions()
            ev = evaluate_job(
                n, max_age_days=int(cfg.get("job_age_max_days", 75)),
                excluded_companies=exclusions["companies"],
                excluded_title_patterns=exclusions["title_patterns"],
                onsite_cities=exclusions["preferred_onsite_cities"])
            verified = (
                bool(n.ats_job_id and n.ats in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters"))
                or i in page_verified and page_verified[i] is True
            )
            if i in failed_verify:
                fail_reason = failed_verify[i]
            else:
                fail_reason = ("", )
            job = Job(
                title=n.title, company=n.company or _company_from_url(n),
                canonical_url=n.canonical_url, apply_url=n.apply_url or n.canonical_url,
                description=n.description, location=n.location,
                remote_status=n.remote_status, country_eligible=True,
                country_evidence="; ".join(ev.reasons)[:700],
                salary=n.salary, employment_type=n.employment_type,
                published_at=n.published_at, ats=n.ats,
                source_id=n.source_id or "unknown", source_url=n.source_url,
                ats_job_id=n.ats_job_id, dedupe_key=n.dedupe_key,
                verified=verified,
                eligibility_status=ev.status,
                eligibility_score=round(ev.score, 2),
                eligibility_reasons=" | ".join(ev.reasons)[:2000])
            session.add(job)
            session.flush()
            stats.stored_new += 1
            if ev.status == "eligible":
                stats.eligible += 1
            elif ev.status == "ineligible":
                stats.ineligible += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return stats


def _company_from_url(n) -> str:
    m = re.match(r"https?://[^/]+/(?:boards\.greenhouse\.io/)?([A-Za-z0-9_-]+)/", n.canonical_url + "/")
    host = re.match(r"https?://([^/]+)", n.canonical_url or "")
    return (m.group(1) if m else (host.group(1) if host else "unknown"))[:256]


async def run_discovery_phase(cfg: dict) -> dict:
    """Execute every configured source and persist results. Returns an aggregate report."""
    init_engine(cfg["db_path"])
    started = time.monotonic()
    sources = all_sources()
    report: dict = {"sources": [], "errors": []}
    client = make_client()

    async def run_one(source: Source) -> tuple[Source, SourceResult, dict]:
        result = await source.discover(client, cfg)
        entry = {
            "source_id": source.id,
            "duration_s": result.duration_seconds,
            "results": len(result.jobs),
            "errors": result.errors[:5],
            "error_count": len(result.errors),
            "rate_limited": result.rate_limited,
            "pages": result.pages_processed,
        }
        print(f"· [{source.id}] {entry['results']} listings, "
              f"{entry['error_count']} errors, {entry['duration_s']}s")
        return source, result, entry

    try:
        got = await asyncio.gather(*[run_one(s) for s in sources])
    except BaseException:
        await client.aclose()
        raise

    # ... record health, gather raws ...
    search_leads = {XraySource.id, "hn_who_is_hiring", "arbeitnow", "weworkremotely",
                    "remoteok", "himalayas", "jobicy", "remotive"}

    for source, result, entry in got:
        session = get_session()
        try:
            session.add(SourceRun(
                source_id=source.id, duration_seconds=result.duration_seconds,
                ok=not result.errors, results_returned=len(result.jobs),
                valid_listings=sum(1 for j in result.jobs if j.title and (j.url or j.apply_url)),
                errors=" || ".join(result.errors)[:4000],
                pagination_progress=f"pages={result.pages_processed}",
                rate_limited=result.rate_limited))
            session.commit()
        finally:
            session.close()
        report["sources"].append(entry)

    all_raws: list[RawJob] = []
    cap = int(cfg.get("discover_max_jobs_per_run", 2000))
    for source, result, entry in got:
        all_raws.extend(result.jobs[:cap])

    try:
        stats = await _verify_and_store(client, all_raws, cfg, search_leads)
    finally:
        await client.aclose()
    report["ingest"] = {
        "raw_listings": stats.total_raw, "new_jobs_stored": stats.stored_new,
        "duplicates": stats.duplicates, "eligible_jobs": stats.eligible,
        "ineligible_jobs": stats.ineligible,
    }
    print(f"· [ingest] new={stats.stored_new} dup={stats.duplicates} "
          f"eligible={stats.eligible} ineligible={stats.ineligible}")
    return report

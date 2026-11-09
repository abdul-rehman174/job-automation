"""Direct ATS platform job-board sources (public board APIs, no credentials).

Greenhouse, Lever, Ashby, Workable, SmartRecruiters — driven by the configured
company-handle directory. These are real job boards belonging to real companies.
"""
from __future__ import annotations

import asyncio
import base64
import re
from urllib.parse import quote_plus

import httpx

from ..pipeline import RawJob
from .base import Source, SourceResult

_HANDLE_SLEEP = 0.4  # polite pacing between board requests


class GreenhouseSource(Source):
    id = "greenhouse_api"
    name = "Greenhouse public boards"

    async def _run(self, client, cfg):
        out = SourceResult()
        handles = (cfg.get("company_handles", {}).get("greenhouse") or [])[:40]
        for i, handle in enumerate(handles):
            out.pages_processed += 1
            try:
                resp = await client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{quote_plus(handle)}/jobs",
                    params={"content": "true"},
                )
                if resp.status_code == 429:
                    out.rate_limited = True
                    out.errors.append(f"{handle}: rate limited (HTTP 429)")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code in (403, 404):
                    out.errors.append(f"{handle}: board not accessible (HTTP {resp.status_code})")
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                out.errors.append(f"{handle}: {type(exc).__name__}: {exc}")
                continue
            for item in data.get("jobs") or []:
                content = ""
                if item.get("content"):
                    try:
                        content = base64.b64decode(item["content"].encode()).decode("utf-8", "replace")
                    except Exception:
                        content = ""
                out.jobs.append(RawJob(
                    title=item.get("title") or "",
                    company=data.get("name") or handle,
                    url=item.get("absolute_url") or "",
                    apply_url=item.get("absolute_url") or "",
                    description=content,
                    location=(item.get("location") or {}).get("location") or "",
                    ats="greenhouse",
                    ats_job_id=str(item.get("id") or ""),
                    date_str=item.get("updated_at") or "",
                ))
            if i < len(handles) - 1:
                await asyncio.sleep(_HANDLE_SLEEP)
        return out


class LeverSource(Source):
    id = "lever_api"
    name = "Lever public postings"

    async def _run(self, client, cfg):
        out = SourceResult()
        handles = (cfg.get("company_handles", {}).get("lever") or [])[:40]
        for i, handle in enumerate(handles):
            out.pages_processed += 1
            try:
                resp = await client.get(
                    f"https://api.lever.co/v0/postings/{quote_plus(handle)}",
                    params={"mode": "json"},
                )
                if resp.status_code == 429:
                    out.rate_limited = True
                    out.errors.append(f"{handle}: rate limited (HTTP 429)")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code in (403, 404):
                    out.errors.append(f"{handle}: board not accessible (HTTP {resp.status_code})")
                    continue
                resp.raise_for_status()
                postings = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                out.errors.append(f"{handle}: {type(exc).__name__}: {exc}")
                continue
            for item in postings or []:
                cats = item.get("categories") or {}
                work = (item.get("workplaceType") or "").lower()
                desc = item.get("descriptionPlain") or item.get("description") or ""
                out.jobs.append(RawJob(
                    title=item.get("text") or "",
                    company=item.get("companyDisplay") or handle,
                    url=item.get("hostedUrl") or item.get("applyUrl") or "",
                    apply_url=item.get("applyUrl") or item.get("hostedUrl") or "",
                    description=desc,
                    location=cats.get("location") or "",
                    employment_type=cats.get("commitment") or "",
                    ats="lever",
                    ats_job_id=str(item.get("id") or ""),
                    date_str=str(item.get("createdAt") or ""),
                    remote=True if work == "remote" else (None if not work else False),
                ))
            if i < len(handles) - 1:
                await asyncio.sleep(_HANDLE_SLEEP)
        return out


class AshbySource(Source):
    id = "ashby_api"
    name = "Ashby public job boards"

    async def _run(self, client, cfg):
        out = SourceResult()
        handles = (cfg.get("company_handles", {}).get("ashby") or [])[:40]
        for i, handle in enumerate(handles):
            out.pages_processed += 1
            try:
                resp = await client.get(
                    f"https://api.ashbyhq.com/posting-api/job-board/{quote_plus(handle)}",
                    params={"includeCompensation": "true"},
                )
                if resp.status_code == 429:
                    out.rate_limited = True
                    out.errors.append(f"{handle}: rate limited (HTTP 429)")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code in (403, 404):
                    out.errors.append(f"{handle}: board not accessible (HTTP {resp.status_code})")
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                out.errors.append(f"{handle}: {type(exc).__name__}: {exc}")
                continue
            for item in data.get("jobs") or []:
                loc = item.get("location") or ""
                if not loc and item.get("secondaryLocations"):
                    try:
                        loc = ", ".join(sl.get("location", "") or "" for sl in item["secondaryLocations"])
                    except (TypeError, AttributeError):
                        loc = ""
                out.jobs.append(RawJob(
                    title=item.get("title") or "",
                    company=data.get("name") or handle,
                    url=item.get("jobUrl") or item.get("applyUrl") or f"https://jobs.ashbyhq.com/{handle}",
                    apply_url=item.get("applyUrl") or item.get("jobUrl") or "",
                    description=item.get("descriptionHtml") or item.get("descriptionPlain") or "",
                    location=loc,
                    employment_type=item.get("employmentType") or "",
                    ats="ashby",
                    ats_job_id=str(item.get("id") or ""),
                    date_str=item.get("publishedAt") or item.get("createdAt") or "",
                    remote=bool(item.get("isRemote")) if item.get("isRemote") is not None else None,
                ))
            if i < len(handles) - 1:
                await asyncio.sleep(_HANDLE_SLEEP)
        return out


class WorkableSource(Source):
    id = "workable_api"
    name = "Workable widget accounts"

    async def _run(self, client, cfg):
        out = SourceResult()
        handles = (cfg.get("company_handles", {}).get("workable") or [])[:40]
        for i, handle in enumerate(handles):
            out.pages_processed += 1
            try:
                resp = await client.get(
                    f"https://apply.workable.com/api/v1/widget/accounts/{quote_plus(handle)}",
                    params={"details": "true"},
                )
                if resp.status_code == 429:
                    out.rate_limited = True
                    out.errors.append(f"{handle}: rate limited (HTTP 429)")
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code in (403, 404):
                    out.errors.append(f"{handle}: account not accessible (HTTP {resp.status_code})")
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                out.errors.append(f"{handle}: {type(exc).__name__}: {exc}")
                continue
            for item in data.get("jobs") or []:
                loc_parts = item.get("locations") or []
                remote_any = bool(item.get("remote")) or any(
                    (loc or {}).get("remote") is True for loc in loc_parts)
                if loc_parts:
                    loc_str = ", ".join(filter(None, [
                        ", ".join(filter(None, [(p.get("city") or ""), (p.get("region") or ""),
                                                (p.get("country") or "")]))
                        for p in loc_parts]))
                else:
                    loc_str = ", ".join(filter(None, [item.get("city") or "", item.get("region") or "",
                                                      item.get("state") or "", item.get("country") or ""]))
                desc = item.get("full_description") or item.get("description") or ""
                out.jobs.append(RawJob(
                    title=item.get("title") or "",
                    company=(item.get("company_name") or handle),
                    url=item.get("url") or f"https://apply.workable.com/{handle}/j/{item.get('shortcode') or ''}/",
                    apply_url=item.get("shortlink") or item.get("url") or "",
                    description=desc,
                    location=loc_str,
                    employment_type=item.get("employment_type") or "",
                    ats="workable",
                    ats_job_id=str(item.get("shortcode") or item.get("id") or ""),
                    date_str=item.get("published_on") or "",
                    remote=bool(item.get("remote")) if item.get("remote") is not None else None,
                ))
            if i < len(handles) - 1:
                await asyncio.sleep(_HANDLE_SLEEP)
        return out


class SmartRecruitersSource(Source):
    id = "smartrecruiters_api"
    name = "SmartRecruiters public postings"

    async def _run(self, client, cfg):
        out = SourceResult()
        handles = (cfg.get("company_handles", {}).get("smartrecruiters") or [])[:40]
        for i, handle in enumerate(handles):
            out.pages_processed += 1
            for page_num, page in enumerate(range(1, 4)):  # pagination up to 3 pages
                try:
                    resp = await client.get(
                        f"https://api.smartrecruiters.com/v1/companies/{quote_plus(handle)}/postings",
                        params={"limit": 50, "offset": (page - 1) * 50},
                    )
                    if resp.status_code == 429:
                        out.rate_limited = True
                        out.errors.append(f"{handle}: rate limited (HTTP 429)")
                        await asyncio.sleep(2.0)
                        break
                    if resp.status_code in (403, 404):
                        out.errors.append(f"{handle}: company not accessible (HTTP {resp.status_code})")
                        break
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    out.errors.append(f"{handle}: {type(exc).__name__}: {exc}")
                    break
                content = data.get("content") or []
                if not content:
                    break
                out.pages_processed += page_num + 1
                for item in content:
                    post_id = str(item.get("id") or "")
                    loc = item.get("location") or {}
                    loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
                    out.jobs.append(RawJob(
                        title=item.get("name") or "",
                        company=handle,
                        url=f"https://careers.smartrecruiters.com/{quote_plus(handle)}/{post_id}",
                        apply_url=f"https://careers.smartrecruiters.com/{quote_plus(handle)}/{post_id}",
                        description="",
                        location=loc_str,
                        employment_type=(item.get("typeOfEmployment") or {}).get("label") or "",
                        ats="smartrecruiters",
                        ats_job_id=post_id,
                        date_str=item.get("releasedDate") or "",
                        remote=bool(item.get("remote")) if item.get("remote") is not None else None,
                    ))
            if i < len(handles) - 1:
                await asyncio.sleep(_HANDLE_SLEEP)
        return out


ATS_SOURCES = [GreenhouseSource(), LeverSource(), AshbySource(), WorkableSource(), SmartRecruitersSource()]

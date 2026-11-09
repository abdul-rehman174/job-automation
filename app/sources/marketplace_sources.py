"""Job marketplace sources: permitted public APIs and feeds.

All requests are error-isolated; one failing endpoint never affects the others.
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import httpx

from ..pipeline import RawJob
from .base import Source, SourceResult

_TAG_STRIP = re.compile(r"<[^>]+>", re.I)


def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_STRIP.sub(" ", html or "")).strip()


class ArbeitnowSource(Source):
    id = "arbeitnow"
    name = "Arbeitnow job board API"

    async def _run(self, client, cfg):
        out = SourceResult()
        resp = await client.get("https://www.arbeitnow.com/api/job-board-api")
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data") or []:
            out.jobs.append(RawJob(
                title=item.get("title") or "",
                company=item.get("company_name") or "",
                url=item.get("url") or "",
                description=item.get("description_html") or "",
                location=item.get("region") or "",
                employment_type=", ".join(item.get("job_types") or []),
                date_str=item.get("created_at") or "",
                remote=bool(item.get("remote")) if item.get("remote") is not None else None,
                source_url="https://www.arbeitnow.com/api/job-board-api",
            ))
        return out


class RemotiveSource(Source):
    id = "remotive"
    name = "Remotive remote jobs API"

    async def _run(self, client, cfg):
        out = SourceResult()
        resp = await client.get("https://remotive.com/api/remote-jobs",
                                params={"category": "software-development", "limit": 100})
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("jobs") or []:
            out.jobs.append(RawJob(
                title=item.get("title") or "",
                company=item.get("company_name") or "",
                url=item.get("url") or "",
                apply_url=item.get("url") or "",
                description=item.get("description") or "",
                location=item.get("candidate_required_location") or "",
                salary=item.get("salary") or "",
                employment_type=item.get("job_type") or "",
                date_str=item.get("publication_date") or "",
                remote=True,
                source_url="https://remotive.com/api/remote-jobs",
            ))
        return out


class JobicySource(Source):
    id = "jobicy"
    name = "Jobicy remote jobs API"

    async def _run(self, client, cfg):
        out = SourceResult()
        for page_url in (
            "https://jobicy.com/api/v2/remote-jobs?count=50&industry=engineering",
            "https://jobicy.com/api/v2/remote-jobs?count=50&industry=design-multi",
        ):
            try:
                resp = await client.get(page_url)
                resp.raise_for_status()
            except (httpx.HTTPError, ValueError) as exc:
                out.errors.append(f"endpoint {page_url.rsplit('=',1)[-1]}: {type(exc).__name__}: {exc}")
                continue
            for item in ((resp.json() or {}).get("jobs") or []):
                out.jobs.append(RawJob(
                    title=item.get("jobTitle") or "",
                    company=item.get("companyName") or "",
                    url=item.get("url") or "",
                    description=item.get("jobExcerpt") or item.get("jobDescription") or "",
                    location=item.get("jobGeo") or "",
                    employment_type=item.get("jobType") or "",
                    date_str=item.get("jobPublishDate") or "",
                    remote=True,
                    source_url=page_url,
                ))
            out.pages_processed += 1
        return out


class RemoteOKSource(Source):
    id = "remoteok"
    name = "Remote OK public API"

    async def _run(self, client, cfg):
        out = SourceResult()
        resp = await client.get("https://remoteok.com/api",
                                headers={"User-Agent": "Mozilla/5.0 (compatible)"})
        resp.raise_for_status()
        items = resp.json()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue  # first element is a legal notice record
            out.jobs.append(RawJob(
                title=item.get("position") or "",
                company=item.get("company") or "",
                url=item.get("url") or "",
                description=_strip_html(item.get("description") or ""),
                location="remote" if (item.get("location") in (None, "", "Worldwide", "Any")
                                      or item.get("remote")) else (item.get("location") or ""),
                employment_type=", ".join(item.get("tags")[:3]) if item.get("tags") else "",
                date_str=str(item.get("date") or ""),
                salary=item.get("salary_min") and f"{item.get('salary_min')}-{item.get('salary_max')}" or "",
                remote=True,
                source_url="https://remoteok.com/api",
            ))
        return out


class HimalayasSource(Source):
    id = "himalayas"
    name = "Himalayas jobs API"

    async def _run(self, client, cfg):
        out = SourceResult()
        resp = await client.get("https://himalayas.app/jobs/api")
        resp.raise_for_status()
        items = resp.json()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            out.jobs.append(RawJob(
                title=item.get("title") or "",
                company=item.get("companyName") or (item.get("company") or {}).get("name") or "",
                url=item.get("applicationLink") or item.get("jobLink") or "",
                description=_strip_html(item.get("description") or item.get("shortDescription") or ""),
                location=item.get("locationRestrictions") and ", ".join(
                    item["locationRestrictions"]) or "",
                employment_type=item.get("contractType") or "",
                date_str=item.get("pubDate") or "",
                remote=True,
                source_url="https://himalayas.app/jobs/api",
            ))
        return out


class WWRSource(Source):
    id = "weworkremotely"
    name = "We Work Remotely RSS"

    async def _run(self, client, cfg):
        out = SourceResult()
        feed = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
        resp = await client.get(feed)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        out.pages_processed = 1
        for item_el in root.iter("item"):
            title = (item_el.findtext("title") or "").strip()
            link = (item_el.findtext("link") or "").strip()
            desc_html = item_el.findtext("description") or ""
            # WWR titles look like "Company: Job Title" (company may be hidden).
            company = ""
            job_title = title
            if ":" in title:
                company, job_title = title.split(":", 1)
            out.jobs.append(RawJob(
                title=job_title.strip(),
                company=company.strip(),
                url=link,
                description=_strip_html(desc_html),
                location="remote",
                remote=True,
                source_url=feed,
            ))
        return out


_HN_FILTER = re.compile(r"\b(python|django|fastapi|drf|backend|back[- ]end)\b", re.I)
_HN_LINK = re.compile(r"https?://[^\s)\]]+")


class HNWhoIsHiringSource(Source):
    """Hacker News 'Ask HN: Who is hiring?' — comments are leads that get verified
    against the real employer listing later (never trusted as eligibility proof)."""
    id = "hn_who_is_hiring"
    name = "HN Who Is Hiring"
    requires_credentials = False

    async def _run(self, client, cfg):
        out = SourceResult()
        resp = await client.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 1},
        )
        resp.raise_for_status()
        hits = resp.json().get("hits") or []
        if not hits:
            out.errors.append("no 'Who is hiring?' story found via Algolia")
            return out
        story_id = hits[0].get("objectID")
        resp2 = await client.get(f"https://hn.algolia.com/api/v1/items/{story_id}")
        resp2.raise_for_status()
        item = resp2.json()
        out.pages_processed = 1
        for child in item.get("children") or []:
            raw_text = child.get("text") or ""
            # HN escapes slashes in hrefs (https:&#x2F;&#x2F;...). Unescape FIRST so
            # inline links are recoverable, then extract hrefs + visible URLs.
            import html as _html
            unescaped = _html.unescape(raw_text)
            href_links = re.findall(r'href="([^"]+)"', unescaped)
            plain = _strip_html(unescaped)
            if not _HN_FILTER.search(plain):
                continue
            links = [l.strip().rstrip(".,;")
                     for l in (href_links + _HN_LINK.findall(plain))]
            url = ""
            ats_match = ""
            for candidate in links:
                dom = re.sub(r"^https?://(www\.)?", "", candidate.lower(), count=1).split("/")[0]
                if any(k in dom for k in ("greenhouse", "lever.co", "ashbyhq",
                                          "workable", "smartrecruiters")):
                    url = candidate
                    ats_match = dom
                    break
                if dom in ("github.com", "twitter.com", "x.com", "news.ycombinator.com",
                           "hn.algolia.com", "ycombinator.com", "hn.algolia") or not dom:
                    continue
                if not url:
                    url = candidate
            if not url:
                continue
            # HN posts follow "Company | Role | Location | remarks" format.
            parts = [p.strip() for p in plain.split("|") if p.strip()]
            company = parts[0][:200] if parts else ""
            title = ""
            for p in parts[1:4]:
                if _HN_FILTER.search(p) and re.search(r"(engineer|developer|develop(er)?|programmer)", p, re.I):
                    title = p[:200]
                    break
            if not title and parts:
                title = (parts[1] if len(parts) > 1 else parts[0])[:200]
            if company == title and len(parts) > 1:
                company, title = parts[0], parts[1]
            out.jobs.append(RawJob(
                title=f"{title} ({company})" if company and company != title else title or "HN import",
                company=company if company != title else "",
                url=url,
                description=plain[:2000],
                remote=True if re.search(r"\bremote\b", plain, re.I) else None,
                source_url=f"https://news.ycombinator.com/item?id={story_id}",
            ))
        return out


def _strip_html_text(text: str) -> str:
    # HN comments come HTML-escaped; unescape BEFORE stripping tags.
    import html as _html
    return _strip_html(_html.unescape(text))


MARKETPLACE_SOURCES = [
    ArbeitnowSource(),
    RemotiveSource(),
    JobicySource(),
    RemoteOKSource(),
    HimalayasSource(),
    WWRSource(),
    HNWhoIsHiringSource(),
]

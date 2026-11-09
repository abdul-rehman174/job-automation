"""Shared pipeline: extraction -> verification -> normalization -> deduplication.
Eligibility lives in app/eligibility.py.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

# ---------------------------------------------------------------- canonical URLs

_DROP_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "source", "gh_jid_cumulative",
}

def canonicalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url
    scheme = p.scheme.lower() or "https"
    host = p.netloc.lower().removeprefix("www.")
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    params = [kv for kv in parse_qsl(p.query, keep_blank_values=False)
              if kv[0].lower() not in _DROP_QUERY_PARAMS]
    # Greenhouse/Workable etc. keep job ids in the path or query; keep meaningful ones.
    path = re.sub(r"/+", "/", p.path.rstrip("/") or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, host, path, "", urlencode(params), ""))


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""
    return host.split(":")[0]


# ---------------------------------------------------------------- slug matching

_SLUG_RE = re.compile(r"[^a-z0-9]+")

def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", (value or "").lower()).strip("-")


# ---------------------------------------------------------------- normalized job

NORMALIZED_FIELDS = [
    "title", "company", "canonical_url", "apply_url", "description", "location",
    "remote_status", "salary", "employment_type", "published_at", "ats",
    "source_id", "source_url", "ats_job_id",
]


@dataclass
class RawJob:
    """Whatever a source produces before normalization."""
    title: str = ""
    company: str = ""
    url: str = ""
    apply_url: str = ""
    description: str = ""
    location: str = ""
    salary: str = ""
    employment_type: str = ""
    date_str: str = ""                 # ISO date or epoch string
    ats: str = ""                      # greenhouse|lever|ashby|workable|smartrecruiters|unknown
    source_id: str = ""                # source name, e.g. greenhouse_api
    source_url: str = ""               # where this listing row came from
    ats_job_id: str = ""
    remote: bool | None = None         # explicit source signal; None => unknown


@dataclass
class NormalizedJob(RawJob):
    canonical_url: str = ""
    dedupe_key: str = ""
    published_datetime: datetime | None = None

    def as_kwargs(self) -> dict:
        return {
            "title": self.title, "company": self.company,
            "canonical_url": self.canonical_url, "apply_url": self.apply_url,
            "description": self.description, "location": self.location,
            "remote_status": self.remote_status, "salary": self.salary,
            "employment_type": self.employment_type, "published_at": self.published_at,
            "ats": self.ats, "source_id": self.source_id, "source_url": self.source_url,
            "ats_job_id": self.ats_job_id, "dedupe_key": self.dedupe_key,
            "published_datetime": self.published_datetime,
        }


def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    s = str(date_str).strip()
    if s.isdigit():
        try:
            n = int(s)
            return datetime.fromtimestamp(n / (1000 if n > 10**12 else 1), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


_REMOTE_TOKENS = ("remote", "work from home", "wfh", "anywhere", "distributed", "worldwide")

def infer_remote_status(text: str, explicit: bool | None) -> str:
    # Source-provided remote signal wins over keyword inference.
    if explicit is True:
        return "remote"
    if explicit is False:
        return "onsite"
    t = (text or "").lower()
    if "work from anywhere" in t or "worldwide" in t or "remote, global" in t or re.search(r"\bremote\b", t):
        return "remote"
    if "hybrid" in t:
        return "hybrid"
    return "unknown"


# ---------------------------------------------------------------- extraction

_ATS_URL_PATTERNS = {
    "greenhouse": re.compile(r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|greenhouse\.io/embed", re.I),
    "lever": re.compile(r"jobs\.lever\.co|lever\.co/jobs", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com|jobs\.eu\.ashbyhq\.com", re.I),
    "workable": re.compile(r"apply\.workable\.com|jobs\.workable\.com|.*\.workable\.com", re.I),
    "smartrecruiters": re.compile(r"careers\.smartrecruiters\.com|jobs\.smartrecruiters\.com", re.I),
}


def detect_ats_from_url(url: str) -> str:
    d = (url or "").lower()
    for name, pattern in _ATS_URL_PATTERNS.items():
        if pattern.search(d):
            return name
    return ""


# ---------------------------------------------------------------- normalization

def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(_s(x) for x in v)
    return re.sub(r"\s+", " ", str(v).strip())


def normalize_job(raw: RawJob) -> NormalizedJob:
    title = _s(raw.title)
    company = _s(raw.company)
    apply_url = (raw.apply_url or raw.url or "").strip()
    canonical = canonicalize_url(apply_url or raw.url)
    if not raw.ats:
        raw.ats = detect_ats_from_url(canonical)
    company_slug = slugify(company)
    title_slug = slugify(title)
    key_domain = domain_of(canonical)
    # Canonical dedupe key: ATS id if present, else canonical URL, else company+title.
    if raw.ats and raw.ats_job_id:
        dedupe = f"atsid:{raw.ats}:{raw.ats_job_id}:{company_slug}"
    elif raw.ats and key_domain:
        dedupe = f"url:{canonical.lower()}"
    else:
        dedupe = f"ct:{company_slug}/{title_slug}"
    n = NormalizedJob(
        title=title, company=company, url=_s(raw.url), apply_url=apply_url,
        description=_s(raw.description)[:20000],
        location=_s(raw.location), salary=_s(raw.salary),
        employment_type=_s(raw.employment_type),
        date_str=_s(raw.date_str), ats=_s(raw.ats) or "unknown",
        source_id=_s(raw.source_id), source_url=_s(raw.source_url),
        ats_job_id=_s(raw.ats_job_id),
        remote=raw.remote,
    )
    n.canonical_url = canonical
    n.remote_status = infer_remote_status(f"{title} {raw.location} {raw.description[:500]}", raw.remote)
    n.published_datetime = _parse_date(raw.date_str)
    n.published_at = n.published_datetime.isoformat() if n.published_datetime else ""
    n.dedupe_key = dedupe
    return n


# ---------------------------------------------------------------- deduplication

class Deduper:
    """Tracks canonical URLs, ATS ids, and company/title identities."""
    def __init__(self):
        self._url_keys: dict[str, set[str]] = {}
        self._ats_ids: set[str] = set()
        self._company_title: dict[str, set[str]] = {}

    def add(self, key: str) -> bool:
        """Register a dedupe key. Returns True if new, False if duplicate."""
        if not key:
            return False
        if key.startswith("atsid:"):
            if key in self._ats_ids:
                return False
            self._ats_ids.add(key)
            return True
        if key.startswith("url:"):
            k = key[4:].split("/")[0] if False else key
            return self._register_multimap(self._url_keys, k)
        if key.startswith("ct:"):
            company = key.split("/", 2)[0]
            return self._register_multimap(self._company_title, company, key)
        return self._register_multimap(self._url_keys, key)

    @staticmethod
    def _register_multimap(store, first, second=None):
        if second is None:
            bucket = store.setdefault("shared", set())
            if first in bucket:
                return False
            bucket.add(first)
            return True
        bucket = store.setdefault(first, set())
        if second in bucket:
            return False
        bucket.add(second)
        return True


# ---------------------------------------------------------------- verification

LOCATION_DENY = re.compile(
    r"(only (candidates|applicants|open to) [^.;]{0,60}(us|u\.s|canada|europe|usa|north america)|"
    r"legally authorized to work in the (us|united states|uk|canada|europe)|"
    r"must (be|be located|reside|live) (in|within|located in|based) (the )?(us|usa|u\.s|north america|canada|uk|europe|remote[- ]eligible (countries|locations))|"
    r"work (authorization|eligibility) .*(required|us|usa|united states)|"
    r"applicants? must (be|be) (a )?(us|u\.s|canadian|uk) (citizen|resident)|"
    r"we do not (sponsor|provide) (visas?|sponsorship))", re.I)

LOCATION_ALLOW = re.compile(
    r"(worldwide|world[- ]wide|anywhere in the world|globally remote|global remote|"
    r"remote (friendly|first|position)|fully remote|100% remote|work from anywhere|"
    r"remote[, /-]*(worldwide|global|anywhere|pakistan)|"
    r"remote\s*[\(\-]\s*(worldwide|global|anywhere|pakistan)\s*\)?|"
    r"pakistan|no location restriction|we welcome applicants from anywhere)", re.I)

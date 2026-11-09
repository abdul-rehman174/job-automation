"""Job relevance scoring and hard eligibility rules.

A high relevance score NEVER overrides a failed hard rule (eligibility gate).
Classifications: eligible | ineligible | needs_information | manual_review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .pipeline import LOCATION_ALLOW, LOCATION_DENY, NormalizedJob

# ---------------------------------------------------------------- role catalogue

PRIMARY_ROLE_PATTERNS = [
    ("python backend developer", [r"python\s*(backend|back-end)", r"backend\s*developer.*python"]),
    ("python backend engineer", [r"python\s*(backend|back-end)\s*engineer"]),
    ("backend developer", [r"backend\s*developer|back-end\s+developer"]),
    ("backend software engineer", [r"(backend|back-end)\s+(software)?\s*engineer"]),
    ("python software engineer", [r"python\s*software\s*engineer|software\s*engineer.*python"]),
    ("fastapi developer", [r"fastapi|fast\s*api"]),
    ("django developer", [r"django"]),
    ("drf developer", [r"(django\s*rest\s*framework|\bdrf\b)"]),
]

SECONDARY_ROLE_KEYWORDS = [r"full\s*[-\s]?stack"]
PYTHON_CORE_REQUIRED = re.compile(r"\bpython\b", re.I)

_EXCLUDE_TITLE_PATTERNS = [
    r"intern(ship)?\b", r"\btrainee\b", r"\boverseas\b", r"\bdata\s*entr(y|ies)\b",
    r"\bsales\b", r"\bmarketing\b", r"\bdev\s*ops\b", r"manager\b.*director",
    r"\bscrum\s*master\b", r"\bqa\b\s*manager", r"\bteaching\b", r"\btutor\b",
    r"\bproduct\s*manager\b", r"\bproject\s*manager\b", r"\bbusiness\s*analyst\b",
    r"\brecruiter\b", r"\bandroid\b", r"\bios\b", r"\bfrontend\s*(only)?\b$",
]

_SKILL_KEYWORDS = {
    "python": [r"\bpython\b"],
    "fastapi": [r"\bfastapi\b|\bfast\s*api\b"],
    "django": [r"\bdjango\b"],
    "django_rest_framework": [r"django\s*rest\s*framework|\bdrf\b"],
    "postgres": [r"\bpostgres(ql)?\b|\bpostgresql?\b"],
    "mysql": [r"\bmysql\b|maria\s*db"],
    "redis": [r"\bredis\b"],
    "celery": [r"\bcelery\b"],
    "docker": [r"\bdocker\b|\bcontainer(s|ized)?\b|\bkubernetes?\b"],
    "linux": [r"\blinux\b|\bunix\b"],
    "rest": [r"\brest(ful)?\b|\bapi(s)?\b|\bhttp\b"],
    "auth": [r"\bauth(e|o|enti)?\w*\b|\bjwt\b|\boauth\b|\bsso\b|\bsaml\b"],
    "integrations": [r"\bintegrat(ion|ions|ings|ing)\b|\bwebhook(s)?\b|\bthird[- ]party\b"],
    "microservices": [r"\bmicroservice(s)?\b|\bservice[- ]oriented\b"],
    "pytest": [r"\bpytest\b|\bunit test(s|ing)?\b|\btest[- ]driven\b"],
    "backend_architecture": [r"\backitecture\b|\bscal(e|ing|able)\b|\bsystem design\b"],
}

_NEGATIVE_SKILLS = {
    "seo": [r"\bseo\b (expert|specialist)"],
    "salesforce": [r"\bsalesforce\b"],
    "wordpress": [r"\bwordpress\b"],
    "php": [r"\bphp\b(?!\w)"],
    "marketing": [r"\bmarketing\b"],
    "technician": [r"\btechnician\b"],
}

# Pakistan-relevant onsite cities (assistant acceptance of local opportunities).
_PAKISTAN_CITIES = re.compile(
    r"\b(karachi|lahore|islamabad|rawalpindi|faisalabad|multan|peshawar|quetta|"
    r"sialkot|gujranwala|hyderabad,?\s*pakistan| Abbottabad)\b", re.I)

_PAKISTAN_PAT = re.compile(r"\bpakistan(is|isitan|i)?\b|\bpk\b", re.I)
_SIGNALS = re.compile(
    r"\b(us|usa|u\.s\.|united states|canada|uk|united kingdom|germany|france|netherlands|"
    r"poland|india|philippines|europe|european union|latin america|latam|southeast asia|"
    r"australia|new zealand|singapore|cyprus|malta)\b", re.I)


def _us_eu_only_remote(text: str) -> bool:
    """Remote listings that restrict hiring to regions Pakistan is not part of.

    Kept deliberately conservative: only explicit restriction phrasing triggers
    it, so genuinely worldwide remote listings remain eligible.
    """
    t = text or ""
    patterns = (
        r"\b(us|u\.s\.?|usa|united states|canada|uk|united kingdom|europe|"
        r"european union|emea|latam)\b[^.;]{0,60}"
        r"(only|residents?|citizens?|legal right to work|work authorization|based)",
        r"remote\s*[\(\-–,]\s*(us|u\.s\.?|usa|united states|canada|uk|europe)\s*\)?",
        r"(us|u\.s\.?|usa|united states|canada|uk|europe)[- ]remote\b",
        r"must (be|reside|live|be located) (in|within) (the )?"
        r"(us|u\.s\.?|usa|united states|canada|uk|europe)\b",
        r"only (consider|candidates?|applicants?) [^.;]{0,40}"
        r"(us|u\.s\.?|usa|united states|canada|uk|europe)\b",
    )
    return any(re.search(p, t, re.I) for p in patterns)


@dataclass
class Evaluation:
    job_id: int = 0
    status: str = "eligible"   # eligible | ineligible | needs_information | manual_review
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)

    def as_dict(self):
        return {
            "status": self.status, "score": round(self.score, 2),
            "reasons": self.reasons, "matched_skills": self.matched_skills,
            "missing_skills": self.missing_skills,
        }


# ---------------------------------------------------------------- hard gates

def _title_is_relevant(title: str) -> tuple[bool, str]:
    t = (title or "").lower()
    for_rg = any(re.search(p, t) for _, pats in PRIMARY_ROLE_PATTERNS for p in pats)
    fs = any(re.search(p, t) for p in SECONDARY_ROLE_KEYWORDS)
    if for_rg:
        return True, "primary"
    if fs:
        return True, "secondary"
    return False, "none"


def _exclude_reason(title: str) -> str:
    t = (title or "").lower()
    for pattern in _EXCLUDE_TITLE_PATTERNS:
        if re.search(pattern, t):
            return f"excluded by title pattern ({pattern!r})"
    return ""


def country_deny(text: str) -> str | None:
    """Return a denial reason if the listing excludes Pakistan-based applicants."""
    t = text or ""
    if LOCATION_DENY.search(t):
        return LOCATION_DENY.search(t).group(0).strip()[:120]
    return None


def country_allow(text: str) -> bool:
    """Positive evidence of remote/worldwide/anywhere availability."""
    return bool(LOCATION_ALLOW.search(text or ""))



# ---------------------------------------------------------------- evaluation

_MIN_YEARS_REQ = re.compile(
    r"\b([5-9]|1\d)\+?\s*(?:years?|yrs?)(?:\s+of)?\s+"
    r"(?:professional\s+|relevant\s+|hands-on\s+|industry\s+)?"
    r"(?:experience|working|in\s+(?:python|software|backend))", re.I)


def _requires_too_much_experience(description: str) -> str | None:
    """Candidate is mid-level (2.5 yrs); skip postings requiring 5+ years."""
    m = _MIN_YEARS_REQ.search(description or "")
    if m:
        return m.group(0)[:100]
    return None


def evaluate_job(job: NormalizedJob, *, now: datetime | None = None,
                 max_age_days: int = 75,
                 excluded_companies: list[str] | None = None,
                 excluded_title_patterns: list[str] | None = None,
                 onsite_cities: list[str] | None = None) -> Evaluation:
    blob = f"{job.title} {job.description[:6000]}"
    reasons: list[str] = []
    matched: list[str] = []
    missing: list[str] = []

    # --- hard gate: employer exclusion (e.g. current employer) ---------------
    company_l = (job.company or "").lower()
    for excl in excluded_companies or []:
        if excl and excl in company_l:
            return Evaluation(0, "ineligible", 0.0, reasons=[
                f"employer '{job.company}' is on the candidate's exclusion list"])

    # --- hard gate: excluded role classes ------------------------------------
    for pat in excluded_title_patterns or []:
        try:
            if re.search(pat, job.title or "", re.I):
                return Evaluation(0, "ineligible", 0.0, reasons=[
                    f"role title matches an excluded role class ({pat!r})"])
        except re.error:
            continue

    # --- title gate: must match target catalogue.
    title_ok, title_class = _title_is_relevant(job.title)
    exc = _exclude_reason(job.title)
    if exc:
        return Evaluation(0, "ineligible", 0.0, reasons=[exc])
    if not title_ok:
        return Evaluation(0, "ineligible", 0.0, reasons=["title does not match target roles"])
    if title_class == "secondary":
        # Only accept full-stack roles where Python backend is a primary responsibility.
        python_core = bool(PYTHON_CORE_REQUIRED.search(job.description[:4000]))
        if not python_core:
            return Evaluation(0, "ineligible", 0.0, reasons=[
                "full-stack role without Python backend as primary responsibility"])
        reasons.append("secondary role: python backend primary responsibility present")
    else:
        reasons.append(f"role matches target catalog ({title_class})")

    # --- hard gate: excluded title patterns
    # --- hard gate: freshness
    now = now or datetime.now(timezone.utc)
    pub = job.published_datetime
    if pub:
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_days = (now - pub).days
        if age_days > max_age_days:
            return Evaluation(0, "ineligible", 0.0, reasons=[f"listing older than {max_age_days} days"])
        reasons.append(f"published {age_days}d ago")
    else:
        reasons.append("publication date unknown (accepted, evidence weak)")

    # --- hard gate: required experience vs candidate's verified 2.5 years ----
    too_much = _requires_too_much_experience(job.description)
    if too_much:
        return Evaluation(0, "ineligible", 0.0, reasons=[
            f"posting requires more experience than verified candidate profile ({too_much})"])

    # --- hard gate: country eligibility (verified, never assumed)
    blob_full = f"{job.title} {job.location} {job.description[:2000]}"
    deny = country_deny(blob_full)
    if deny:
        return Evaluation(0, "ineligible", 0.0, reasons=[
            f"country restriction excludes Pakistan-based applicants: {deny}"])

    if job.remote_status == "remote":
        # Region restrictions win over weak availability phrasing.
        if _us_eu_only_remote(blob_full):
            return Evaluation(0, "ineligible", 0.0, reasons=[
                "remote listing restricted to specific region without evidence Pakistan is accepted"])
        reasons.append("remote listing; positive/neutral availability evidence")
    elif job.remote_status in {"hybrid", "onsite"}:
        cities = onsite_cities or ["Bahawalpur", "Lahore", "Multan"]
        loc_blob = f"{job.location} {job.description[:2000]}".lower()
        if any(c.lower() in loc_blob for c in cities):
            reasons.append("local opportunity in a candidate-preferred city")
        else:
            return Evaluation(0, "ineligible", 0.0, reasons=[
                f"{job.remote_status} role outside the candidate's preferred cities "
                f"({', '.join(cities)})"])
    else:
        if country_allow(blob_full):
            reasons.append("remote status inferred from description (worldwide/anywhere)")
        else:
            return Evaluation(0, "needs_information", 0.0, reasons=[
                "remote status unknown and no positive location eligibility evidence"])

    # --- relevance score
    score = 0.0
    for skill, patterns in _SKILL_KEYWORDS.items():
        if any(re.search(p, blob, re.I) for p in patterns):
            matched.append(skill)
            score += 1.0
        else:
            missing.append(skill)
    for skill, patterns in _NEGATIVE_SKILLS.items():
        if any(re.search(p, blob, re.I) for p in patterns):
            score -= 1.5
            reasons.append(f"negative signal present in description ({skill!r})")

    norm = max(score, 0.0) / len(_SKILL_KEYWORDS)
    score_norm = min(norm * 10, 10)
    # Bonus for explicit domain concentration.
    if re.search(r"\bbackend|back-end\b", blob, re.I):
        score_norm += 1.0
    return Evaluation(0, "eligible", score_norm, reasons=reasons,
                      matched_skills=matched, missing_skills=missing)


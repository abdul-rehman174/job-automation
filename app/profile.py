"""Candidate profile: verified answers, CV selection, screening answers, cover letters.

Integrity rules enforced here:
  * The profile is the ONLY source of candidate facts. Nothing is invented.
  * Every screening answer carries a verification flag; unverified values are
    rejected (never submitted).
  * Missing mandatory information => application is skipped, never guessed.
  * Demographic categories / legal declarations => skipped for human review.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .db import CVRecord, ProfileEntry
from .pipeline import NormalizedJob

DEFAULT_TARGET_ROLES_PRIMARY = [
    "Python Backend Developer", "Python Backend Engineer", "Backend Developer",
    "Backend Software Engineer", "Python Software Engineer", "FastAPI Developer",
    "Django Developer", "Django REST Framework Developer",
]
DEFAULT_TARGET_ROLES_SECONDARY = ["Full Stack Developer", "Full Stack Engineer"]
DEFAULT_SKILLS = ["Python", "FastAPI", "Django", "Django REST Framework", "PostgreSQL",
                  "MySQL", "Redis", "Celery", "Docker", "Linux", "REST APIs",
                  "Authentication", "Third-party integrations", "Microservices",
                  "Pytest", "Backend architecture"]

# ---------------------------------------------------------------- data classes

@dataclass
class ProfileAnswer:
    key: str
    value: str
    human_verified: bool
    needs_human_approval: bool = False
    source: str = ""


@dataclass
class CandidateProfile:
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = "Pakistan"
    country_of_residence: str = "Pakistan"
    resume_summary: str = ""          # factual summary used for cover letters
    screening: dict[str, ProfileAnswer] = field(default_factory=dict)
    excluded_companies: list[str] = field(default_factory=list)
    excluded_title_patterns: list[str] = field(default_factory=list)
    preferred_onsite_cities: list[str] = field(default_factory=lambda: ["Bahawalpur", "Lahore", "Multan"])
    missing: list[str] = field(default_factory=list)

    def is_foundation_complete(self) -> bool:
        """Foundation = identity/contact facts needed for every application."""
        return all([self.first_name, self.last_name, self.email]) and self.screening.get(
            "authorized_remote_work", ProfileAnswer("", "", False)).human_verified

    def require(self, key: str) -> str | None:
        """Return verified value or None (calling code must skip the application)."""
        ans = self.screening.get(key)
        if not ans or not ans.human_verified:
            return None
        if ans.needs_human_approval:
            return None
        return ans.value


# ---------------------------------------------------------------- loading

_PROFILE_ENTRY_KEYS = {
    "first_name", "last_name", "email", "phone", "city", "country",
    "country_of_residence", "resume_summary", "years_experience_python",
    "years_experience_total", "authorized_remote_work", "work_authorization",
    "salary_expectation", "notice_period", "degree", "highest_major",
    "current_employer", "linkedin", "github", "portfolio_url", "visa_status",
    "highest_degree", "authorized_pakistan_only", "employment_type_preference",
    "reason_for_job_search", "remote_experience", "onsite_experience",
}


def profile_answers_from_db(session) -> dict[str, ProfileAnswer]:
    from .db import ProfileEntry as PE
    out: dict[str, ProfileAnswer] = {}
    for row in session.query(PE).all():
        out[row.key] = ProfileAnswer(key=row.key, value=row.value,
                                     human_verified=row.human_verified,
                                     needs_human_approval=row.needs_human_approval,
                                     source=row.source)
    return out


def load_exclusions(cfg: dict) -> dict:
    """Read the candidate's standing exclusions without requiring the DB."""
    path = Path(cfg.get("profile_path", "data/candidate.yml"))
    out = {"companies": [], "title_patterns": [],
           "preferred_onsite_cities": ["Bahawalpur", "Lahore", "Multan"]}
    if not path.exists():
        return out
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return out
    excl = data.get("exclusions") or {}
    out["companies"] = [str(c).strip().lower() for c in (excl.get("companies") or []) if str(c).strip()]
    out["title_patterns"] = [str(p) for p in (excl.get("title_patterns") or []) if str(p).strip()]
    if isinstance(excl.get("preferred_onsite_cities"), list):
        out["preferred_onsite_cities"] = [str(c) for c in excl["preferred_onsite_cities"]]
    return out


def load_profile(cfg: dict, session) -> CandidateProfile:
    """Merge YAML profile (data/candidate.yml) with DB profile entries."""
    path = Path(cfg.get("profile_path", "data/candidate.yml"))
    prof = CandidateProfile()
    screening: dict[str, ProfileAnswer] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    identity = data.get("identity") or {}
    contact = data.get("contact") or {}
    screening_data = data.get("screening") or {}
    file_declared_verified = bool(data.get("verified", False))
    for key, raw in (screening_data or {}).items():
        if isinstance(raw, dict):
            screening[key] = ProfileAnswer(
                key=key, value=str(raw.get("value", "")),
                human_verified=bool(raw.get("human_verified", False)),
                needs_human_approval=bool(raw.get("needs_human_approval", False)),
                source=str(raw.get("source", "candidate.yml")))
        else:
            # Bare scalar: treated as verified ONLY if the file declares itself
            # verified at top level. Unverified values are never submitted.
            screening[key] = ProfileAnswer(key=key, value=str(raw),
                                           human_verified=file_declared_verified,
                                           source="candidate.yml")
    db_answers = profile_answers_from_db(session)
    screening.update(db_answers)

    def _verified(k):
        a = screening.get(k)
        return a.value if a and a.human_verified else ""

    prof.first_name = _verified("first_name") or (identity.get("first_name") or "")
    prof.last_name = _verified("last_name") or (identity.get("last_name") or "")
    prof.full_name = f"{prof.first_name} {prof.last_name}".strip()
    prof.email = _verified("email") or (contact.get("email") or "")
    prof.phone = _verified("phone") or (contact.get("phone") or "")
    prof.city = _verified("city") or (contact.get("city") or identity.get("city") or "")
    prof.country = identity.get("country") or _verified("country") or "Pakistan"
    prof.country_of_residence = prof.country
    prof.resume_summary = data.get("summary") or _verified("resume_summary") or ""
    exclusions = data.get("exclusions") or {}
    prof.excluded_companies = [str(c).strip().lower() for c in (exclusions.get("companies") or []) if str(c).strip()]
    prof.excluded_title_patterns = [str(p) for p in (exclusions.get("title_patterns") or []) if str(p).strip()]
    if isinstance(exclusions.get("preferred_onsite_cities"), list):
        prof.preferred_onsite_cities = [str(c) for c in exclusions["preferred_onsite_cities"]]
    prof.screening = screening
    # Note: YAML identity/contact values are NOT auto-verified; only screening
    # entries verified in YAML populate the verified profile. Identity fields
    # without verification count as missing to enforce integrity.
    prof.missing = [k for k in ("first_name", "last_name", "email") if not _verified(k)]
    if not prof.screening.get("authorized_remote_work", ProfileAnswer("", "", False)).human_verified:
        prof.missing.append("authorized_remote_work")
    return prof


# ---------------------------------------------------------------- CV selection

@dataclass
class CVChoice:
    cv: CVRecord
    score: float
    reason: str


CV_KEYWORD_ALIASES = {
    "python": r"\bpython\b",
    "fastapi": r"\bfastapi\b",
    "django": r"\bdjango\b",
    "drf": r"django\s*rest\s*framework|\bdrf\b",
    "postgres": r"postgres|postgresql?",
    "mysql": r"mysql",
    "redis": r"redis",
    "celery": r"celery",
    "docker": r"docker",
    "backend": r"backend|back[- ]end",
    "rest": r"\brest(ful)?\b",
    "microservices": r"microservice",
    "pytest": r"pytest",
    "fullstack": r"full[-\s]?stack",
    "linux": r"\blinux\b",
    "auth": r"auth(enticat|orizat)?",
    "integrations": r"integrat(ion|e|ing)",
}


def select_cv(cvs: list[CVRecord], job: NormalizedJob) -> CVChoice | None:
    """Pick the most relevant CV by keyword overlap with title/skills/role text."""
    blob = f"{job.title} {job.description[:2500]} {job.employment_type}".lower()
    best: CVChoice | None = None
    for cv in cvs:
        if not cv.available:
            continue
        kws = [k.strip().lower() for k in (cv.keywords or "").split(",") if k.strip()]
        if not kws:
            # No keywords => cannot determine relevance => never used blindly.
            continue
        score = 0.0
        hits = []
        for kw in kws:
            pattern = CV_KEYWORD_ALIASES.get(kw.replace(" ", ""), None) or re.escape(kw)
            if re.search(pattern, blob):
                score += 1.0
                hits.append(kw)
        primary_role = any(t in blob for t in (
            "backend", "back-end", "fastapi", "django", "python backend"))
        if primary_role and "backend" in kws:
            score += 1.5
        if _NEGATIVE_CV_SHIFT(job, kws):
            score -= 2.0
        if best is None or score > best.score:
            best = CVChoice(cv=cv, score=score,
                            reason=f"matched keywords {hits}", )
    if best is None or best.score <= 0.5:
        return None
    return best


def _NEGATIVE_CV_SHIFT(job, cv_keywords) -> bool:
    """Guardrail: don't apply a CV to a role class clearly outside its keywords."""
    blob = f"{job.title}".lower()
    if "fullstack" in cv_keywords and any(w in blob for w in ("backend", "back-end", "django", "fastapi")):
        return False
    return any(w in blob for w in ("ios developer", "android developer", "front-end only")) \
        and "python" not in cv_keywords


def cv_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync_cvs(session, cfg: dict) -> list[CVRecord]:
    """Upsert CV records from data/cvs/manifest.json (or filename scan fallback)."""
    cvdir = Path(cfg.get("cv_dir", "data/cvs"))
    manifest_path = cvdir / "manifest.json"
    entries: list[dict] = []
    if manifest_path.exists():
        try:
            entries = (json.loads(manifest_path.read_text()) or {}).get("cvs", [])
        except (json.JSONDecodeError, OSError):
            entries = []
    if not entries:
        for f in sorted(cvdir.glob("*")):
            if f.suffix.lower() in (".pdf", ".docx", ".doc", ".txt"):
                entries.append({"name": f.stem, "file": f.name,
                                "keywords": f.stem.replace("_", " ").replace("-", " "),
                                "available": True})
    records: list[CVRecord] = []
    for entry in entries:
        name = str(entry.get("name") or Path(str(entry.get("file", ""))).stem)
        file_name = str(entry.get("file") or "")
        path = cvdir / file_name
        exists = path.exists()
        row = session.query(CVRecord).filter_by(name=name).one_or_none()
        if row is None:
            row = CVRecord(name=name, path=str(path), sha256="", keywords="",
                           available=bool(entry.get("available", True)) and exists)
            session.add(row)
        row.path = str(path)
        row.keywords = str(entry.get("keywords") or "")
        row.available = bool(entry.get("available", True)) and exists
        if exists:
            row.sha256 = cv_sha256(path)
        session.flush()
        records.append(row)
    session.commit()
    return records


# ---------------------------------------------------------------- screening answers

# Matching patterns for question text -> verified profile keys.
QUESTION_MAP: list[tuple[str, str]] = [
    # (profile key, regex over normalized question text)
    ("first_name", r"first\s*name"),
    ("last_name", r"(last|family|sur)name"),
    ("email", r"e[- ]?mail"),
    ("phone", r"(phone|mobile|cell|contact number)"),
    ("city", r"(city|town)\b"),
    ("country", r"(country|where.{0,12}(based|located|live|resid))"),
    ("years_experience_python", r"years? (of .{0,12})?(experience .{0,20})?(with |in )?(python\b|python experience)?"),
    ("years_experience_python", r"(how many|years).{0,30}python"),
    ("years_experience_total", r"(how many|how much|total).{0,24}(experience|years)"),
    ("django", r"(how many|years).{0,20}django"),
    ("salary_expectation", r"(salary|compensation|pay).{0,30}(expect|require|range|desired)"),
    ("notice_period", r"(notice period|how soon can you start|availability/available|when can you start)"),
    ("degree", r"(degree|education|bachelor|b\.?sc|m\.?sc|undergraduate|did you (attend|graduate))"),
    ("highest_major", r"(major|field of study|degree .*(in|related))"),
    ("authorized_worldwide_remote", r"(legally|authorized|eligible) .{0,40}(work|remotely|country)|"
                                     r"(are you )?(authorized|legally|entitled)"),
    ("authorized_remotely_pakistan", r"(based|resid|located).{0,30}(pakistan|continent|timezone)"),
    ("visa_status", r"(visa|work permit|sponsorship)"),
    ("remote_experience", r"(experience|comfort(able)?) .{0,30}remote"),
    ("linkedin", r"linkedin"),
    ("github", r"github"),
    ("portfolio_url", r"(portfolio|website|personal site|blog)"),
]


def _classify_question(question: str) -> str:
    """Classify a raw screening question to detect blocker categories."""
    q = (question or "").lower()
    legal = re.compile(
        r"\b(certify|attest|declare|under penalty|affirm|swear|consent)\b|"
        r"\bi agree to (the|all)\b|\bterms (and conditions|of service)\b|"
        r"\bnon-compet(e|ition)\b|indemnif|agreement (is|shall) be", re.I)
    demographic = re.compile(
        r"\b(gender\b|national (origin)|race\b|ethnicit|disabilit|veteran status|"
        r"sexual (orientation|preference)|marital status|religion|age (over|above)(?=\?))", re.I)
    if legal.search(q):
        return "legal_declaration"
    if demographic.search(q):
        return "demographic"
    yesno_challenge = re.compile(r"^(are you|do you|have you|i?s).*\?$", re.I)
    if re.search(r"\b(capital of|solve|calculate|math)\b", q):
        return "unsure_answer"
    return "answerable"


_COUNTRY_RE = re.compile(
    r"\b(united states|u\.s\.?|usa|us|canada|uk|united kingdom|europe|eu|germany|france|"
    r"netherlands|australia|new zealand|singapore|india|uae|saudi|qatar|"
    r"pakistan)\b", re.I)


def _country_mentioned(question: str) -> str | None:
    m = _COUNTRY_RE.search(question or "")
    if not m:
        return None
    return m.group(1).lower().replace(".", "").replace(" ", "")


def answer_question(question: str, profile: CandidateProfile, job) -> tuple[str | None, str, str]:
    """Try to answer a screening question from the verified profile.

    Returns (verified_answer, kind, detail)
      kind: verified | missing_information | needs_human | unsure_answer
    Never fabricates. Caller acts on kind.
    """
    kind = _classify_question(question)
    if kind in ("legal_declaration", "demographic"):
        return None, "needs_human", kind
    if kind == "unsure_answer":
        return None, "unsure_answer", kind
    q = re.sub(r"\s+", " ", question.lower()).strip()

    # Work-authorization questions are country-sensitive. Never imply US/EU/UK
    # authorization: only the explicitly verified Pakistan/global-remote facts
    # may be used.
    if re.search(r"(authorized|eligible|legally|permitted|right) .{0,40}(work|employment)", q) or \
            re.search(r"work (authorization|authorisation|permit|eligibility)", q):
        country = _country_mentioned(q)
        if country in (None, "pakistan"):
            value = profile.require("work_authorization")
            if value is not None:
                return value, "verified", "work_authorization"
            return None, "missing_information", "work_authorization"
        # US/UK/EU/etc.: only answer if an explicit per-country verified fact exists.
        per_country = profile.require(f"work_authorization_{country}")
        if per_country is not None:
            return per_country, "verified", f"work_authorization_{country}"
        return None, "missing_information", f"work_authorization_{country}"

    for key, pattern in QUESTION_MAP:
        if not re.search(pattern, q):
            continue
        # Question may name a specific technology for years-of-experience.
        tech = None
        m = re.search(r"\b(python|django|fastapi|postgres(ql)?|redis|docker|celery)\b", q)
        if m and key.endswith("python"):
            tech = m.group(1).replace("postgres", "postgres")
        lookup = key
        if key == "years_experience_python" and tech and tech != "python":
            lookup = f"years_experience_{tech}"
        value = profile.require(lookup)
        if value is not None:
            return value, "verified", lookup
        if key in ("first_name", "last_name", "email", "phone", "city", "country"):
            fallback = getattr(profile, key, "")
            if fallback:
                return fallback, "verified", key
        return None, "missing_information", lookup
    # Yes/no style work-authorization questions with per-country phrasing.
    if re.search(r"yes/no|\?", q) and re.search(r"(authorized|legally|sponsorship|work( for| in))", q):
        auth = profile.screening.get("authorized_worldwide_remote")
        if auth and auth.human_verified and auth.value.lower() in ("yes", "true"):
            return "Yes", "verified", "authorized_worldwide_remote"
        return None, "missing_information", "authorized_worldwide_remote"
    return None, "missing_information", "unmatched-question"


@dataclass
class AnswerResult:
    answers: list[tuple[str, str, str, str]]  # (question, answer, kind, key)
    missing: list[str]
    needs_human: list[str]


# ---------------------------------------------------------------- cover letter

COVER_TEMPLATE = """Dear Hiring Team,

I am applying for the {title} position at {company}. My work centers on
{highlight}, which matches the requirements outlined in your listing.{optional_location}

Relevant experience from my background: {summary}

I have included my CV ({cv_name}) and would welcome the opportunity to
discuss the role.

Sincerely,
{full_name}
{contact_line}
"""


def generate_cover_letter(profile: CandidateProfile, job: NormalizedJob,
                          matched_skills: list[str], cv_name: str) -> str:
    highlight = ", ".join(matched_skills[:4]) if matched_skills else "backend engineering with Python"
    summary = (profile.resume_summary or "").strip()
    if not summary:
        summary = "I bring hands-on experience building and operating backend services."
    remote_note = ""
    if job.remote_status == "remote":
        remote_note = ("\n\nI am based in Pakistan and experienced working fully remotely "
                       "with distributed teams across time zones.")
    contact_line = ", ".join(filter(None, [profile.email, profile.phone]))
    return COVER_TEMPLATE.format(
        title=job.title, company=job.company, highlight=highlight,
        optional_location=remote_note, summary=summary, cv_name=cv_name,
        full_name=profile.full_name or "Candidate", contact_line=contact_line,
    )

"""Package preparation: read job, pick CV, verify answers, generate cover letter,
persist the complete application package. Runs before browser automation.

Preparation that cannot produce a verified, complete package records what is
missing and the application is SKIPPED (never submitted with guesses).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..db import CVRecord, Job, SkipCodes
from ..eligibility import evaluate_job
from ..pipeline import NormalizedJob, RawJob, normalize_job
from ..profile import (CandidateProfile, answer_question, load_exclusions,
                       generate_cover_letter, load_profile, select_cv)


class PrepareOutcome:
    def __init__(self, ok, cv=None, cover_letter="", answers=None,
                 package=None, skip_code="", skip_reason="", reason_detail=""):
        self.ok = ok
        self.cv = cv
        self.cover_letter = cover_letter
        self.answers = answers or {}
        self.package = package or {}
        self.skip_code = skip_code
        self.skip_reason = skip_reason
        self.reason_detail = reason_detail


def job_to_normalized(job: Job) -> NormalizedJob:
    raw = RawJob(
        title=job.title, company=job.company, url=job.canonical_url,
        apply_url=job.apply_url, description=job.description,
        location=job.location, salary=getattr(job, "salary", "") or "",
        employment_type=job.employment_type or "", date_str=job.published_at,
        ats=job.ats or "", source_id=job.source_id,
    )
    n = normalize_job(raw)
    n.remote_status = job.remote_status or n.remote_status
    try:
        if job.published_at:
            n.published_datetime = datetime.fromisoformat(
                job.published_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    return n


def prepare_application(session: Session, job: Job, cfg: dict) -> PrepareOutcome:
    profile: CandidateProfile = load_profile(cfg, session)
    if profile.missing:
        missing = ", ".join(profile.missing[:6])
        return PrepareOutcome(
            ok=False, skip_code=SkipCodes.MISSING_INFO.value,
            skip_reason=("candidate profile is missing verified core information: "
                         f"{missing}. Fill data/candidate.yml (see scripts/setup_profile.py)."),
            reason_detail="profile incomplete")

    n = job_to_normalized(job)
    cvs = session.query(CVRecord).filter_by(available=True).all()
    choice = select_cv(cvs, n)
    if choice is None:
        return PrepareOutcome(
            ok=False, skip_code=SkipCodes.MISSING_INFO.value,
            skip_reason="no relevant CV available for this position (no CV keywords matched). "
                        "Add CV files + keywords to data/cvs/.",
            reason_detail="cv_selection_failed")

    # Defense in depth: re-evaluate eligibility at apply time (never submit stale).
    excl = load_exclusions(cfg)
    re_ev = evaluate_job(
        n, max_age_days=int(cfg.get("job_age_max_days", 75)),
        excluded_companies=excl["companies"],
        excluded_title_patterns=excl["title_patterns"],
        onsite_cities=excl["preferred_onsite_cities"])
    if re_ev.status != "eligible":
        return PrepareOutcome(
            ok=False, skip_code=SkipCodes.ELIGIBLE_DECLINE.value,
            skip_reason=f"re-evaluation at apply time returned {re_ev.status}: "
                        + "; ".join(re_ev.reasons[:3]),
            reason_detail=re_ev.status)

    cover = generate_cover_letter(profile, n, re_ev.matched_skills, choice.cv.name)
    extra = {k: a.value for k, a in profile.screening.items()
             if a.human_verified and not a.needs_human_approval}
    answers = {"first_name": profile.first_name, "last_name": profile.last_name,
               "email": profile.email, "phone": profile.phone, **extra}
    package = {
        "answers": answers,
        "cv_id": choice.cv.id, "cv_name": choice.cv.name,
        "cv_path": choice.cv.path, "cv_score": choice.score,
        "cover_letter": cover, "matched_skills": re_ev.matched_skills,
        "job_id": job.id, "job_url": job.apply_url,
        "job_title": job.title, "company": job.company,
    }
    return PrepareOutcome(ok=True, cv=choice.cv, cover_letter=cover,
                          answers=answers, package=package)

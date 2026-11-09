"""Profile: verified answers, CV selection, cover letters, CV manifest sync."""
from pathlib import Path

from app.db import CVRecord
from app.pipeline import RawJob, normalize_job
from app.profile import (answer_question, generate_cover_letter, load_profile,
                         select_cv, sync_cvs)


def make_job(title="Python Backend Developer", desc="Python FastAPI Django PostgreSQL Redis Celery Docker REST microservices authentication"):
    return normalize_job(RawJob(title=title, company="Acme",
                                url="https://jobs.lever.co/acme/1",
                                description=desc, ats="lever", ats_job_id="1"))


def test_profile_loads_verified_facts(cfg, session):
    prof = load_profile(cfg, session)
    assert prof.first_name == "Abdul"
    assert prof.email == "abdulrehmanshafy101@gmail.com"
    assert prof.country == "Pakistan"
    assert prof.missing == []
    assert "enigmatix" in prof.excluded_companies


def test_unverified_profile_is_incomplete(tmp_path, session):
    bad = tmp_path / "bad.yml"
    bad.write_text("""
screening:
  first_name: { value: Abdul, human_verified: false }
  last_name: { value: Rehman, human_verified: false }
  email: { value: a@b.c, human_verified: false }
""")
    prof = load_profile({"profile_path": str(bad), "cv_dir": str(tmp_path)}, session)
    assert "first_name" in prof.missing
    assert prof.first_name == ""


def test_sync_cvs_registers_manifest_and_disables_missing(cfg, session):
    rows = sync_cvs(session, cfg)
    names = {r.name: r for r in rows}
    assert names["Backend"].available is True
    assert names["Odoo"].available is False  # file missing
    assert names["Backend"].sha256


def test_cv_selection_prefers_matching_keywords(cfg, session):
    sync_cvs(session, cfg)
    cvs = session.query(CVRecord).filter_by(available=True).all()
    fastapi_job = make_job()
    choice = select_cv(cvs, fastapi_job)
    assert choice is not None
    assert choice.cv.name in ("Backend", "Django")
    django_job = make_job(title="Django Developer",
                          desc="Python Django DRF MySQL pytest backend")
    choice2 = select_cv(cvs, django_job)
    assert choice2 is not None and choice2.cv.name == "Django"


def test_cv_selection_returns_none_when_irrelevant(cfg, session):
    cv = CVRecord(name="Design", path="/x.pdf", sha256="x",
                  keywords="graphic design, figma", available=True)
    session.add(cv)
    session.commit()
    choice = select_cv([cv], make_job(title="iOS Developer", desc="Swift UIKit"))
    assert choice is None


def test_answer_question_verified_years(cfg, session):
    prof = load_profile(cfg, session)
    ans, kind, key = answer_question("How many years of experience do you have with Python?",
                                     prof, make_job())
    assert (ans, kind, key) == ("2.5", "verified", "years_experience_python")


def test_answer_question_salary_missing(cfg, session):
    prof = load_profile(cfg, session)
    ans, kind, key = answer_question("What are your salary expectations?", prof, make_job())
    assert ans is None and kind == "missing_information"
    assert key == "salary_expectation"


def test_answer_question_us_authorization_never_claims_yes(cfg, session):
    prof = load_profile(cfg, session)
    ans, kind, key = answer_question(
        "Are you legally authorized to work in the United States?", prof, make_job())
    assert ans is None and kind == "missing_information"
    assert "unitedstates" in key


def test_answer_question_pakistan_authorization_ok(cfg, session):
    prof = load_profile(cfg, session)
    ans, kind, key = answer_question(
        "Are you authorized to work in Pakistan?", prof, make_job())
    assert ans and kind == "verified"


def test_answer_question_legal_declaration_requires_human(cfg, session):
    prof = load_profile(cfg, session)
    ans, kind, _ = answer_question(
        "I certify that the information provided is true and complete.", prof, make_job())
    assert ans is None and kind == "needs_human"


def test_answer_question_demographic_requires_human(cfg, session):
    prof = load_profile(cfg, session)
    ans, kind, _ = answer_question("What is your gender?", prof, make_job())
    assert ans is None and kind == "needs_human"


def test_cover_letter_is_factual_and_job_specific(cfg, session):
    prof = load_profile(cfg, session)
    job = make_job()
    letter = generate_cover_letter(prof, job, ["python", "fastapi"], "Backend")
    assert "Python Backend Developer" in letter
    assert "Acme" in letter
    assert "Abdul Rehman" in letter
    assert "Python backend engineer" in letter

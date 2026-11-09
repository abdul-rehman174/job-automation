"""Eligibility: Pakistan rules, exclusions, seniority, freshness, scoring."""
import datetime as dt

from app.eligibility import evaluate_job
from app.pipeline import RawJob, normalize_job

NOW = dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc)

FULL_DESC = (
    "We are hiring a backend engineer. Python, FastAPI, Django, PostgreSQL, Redis, "
    "Celery, Docker, Linux, REST APIs, authentication, integrations, microservices, "
    "pytest, backend architecture. Fully remote worldwide."
)


def job(title="Python Backend Developer", company="Acme", desc=FULL_DESC,
        location="", remote=None, url="https://jobs.lever.co/acme/1", days_ago=3,
        ats_job_id="1"):
    return normalize_job(RawJob(
        title=title, company=company, url=url, description=desc, location=location,
        remote=remote, ats="lever", ats_job_id=ats_job_id,
        date_str=(NOW - dt.timedelta(days=days_ago)).isoformat()))


def test_worldwide_remote_python_backend_is_eligible():
    ev = evaluate_job(job(), now=NOW)
    assert ev.status == "eligible"
    assert ev.score >= 7
    assert "python" in ev.matched_skills


def test_us_work_authorization_requirement_is_ineligible():
    desc = FULL_DESC + " Must be legally authorized to work in the United States."
    ev = evaluate_job(job(desc=desc), now=NOW)
    assert ev.status == "ineligible"
    assert "country restriction" in ev.reasons[0]


def test_onsite_outside_pakistan_is_ineligible():
    ev = evaluate_job(job(location="Berlin, Germany", remote=False), now=NOW)
    assert ev.status == "ineligible"


def test_onsite_lahore_is_eligible():
    ev = evaluate_job(job(location="Lahore, Pakistan", remote=False), now=NOW)
    assert ev.status == "eligible"


def test_senior_experience_requirement_is_ineligible():
    desc = FULL_DESC + " Requires 5+ years of professional experience."
    ev = evaluate_job(job(desc=desc), now=NOW)
    assert ev.status == "ineligible"
    assert "more experience" in ev.reasons[0]


def test_excluded_employer_is_ineligible():
    ev = evaluate_job(job(company="Enigmatix Software House"), now=NOW,
                      excluded_companies=["enigmatix software house"])
    assert ev.status == "ineligible"
    assert "exclusion list" in ev.reasons[0]


def test_excluded_role_class_via_yaml_patterns():
    ev = evaluate_job(job(title="Odoo Developer"), now=NOW,
                      excluded_title_patterns=["\\bodoo\\b"])
    assert ev.status == "ineligible"


def test_fullstack_without_python_is_ineligible():
    ev = evaluate_job(job(title="Full Stack Developer",
                          desc="React and Node.js only. Remote worldwide."), now=NOW)
    assert ev.status == "ineligible"


def test_fullstack_with_python_backend_is_eligible():
    ev = evaluate_job(job(title="Full Stack Developer", desc=FULL_DESC), now=NOW)
    assert ev.status == "eligible"


def test_stale_listing_is_ineligible():
    ev = evaluate_job(job(days_ago=90), now=NOW)
    assert ev.status == "ineligible"
    assert "older than" in ev.reasons[0]


def test_no_remote_signal_and_no_location_is_needs_information():
    ev = evaluate_job(job(location="", remote=False), now=NOW)
    assert ev.status == "ineligible"  # explicit onsite elsewhere
    ev2 = evaluate_job(job(location="", remote=None,
                           desc="Python backend role. Apply now."), now=NOW)
    assert ev2.status == "needs_information"


def test_us_only_remote_is_ineligible_without_allow_evidence():
    desc = "Python backend. Remote - US only. Applicants must be authorized to work in the US."
    ev = evaluate_job(job(desc=desc, location="Remote, US", remote=True), now=NOW)
    assert ev.status == "ineligible"


def test_high_score_never_overrides_failed_eligibility():
    desc = FULL_DESC + " Must reside in Canada."
    ev = evaluate_job(job(desc=desc), now=NOW)
    assert ev.status == "ineligible"
    assert ev.score == 0.0

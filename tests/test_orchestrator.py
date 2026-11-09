"""Orchestrator: independent queue execution, blocker skips, validation gate,
duplicate protection, restart recovery."""
import asyncio
import json

import pytest

from app.apply_engine.detect import Blocker
from app.apply_engine.prepare import prepare_application
from app.db import AppStatus, Application, Job, RunRecord, SkipCodes, get_session
from app.orchestrator import (Orchestrator, _record_outcome,
                              recover_interrupted_work)
from app.profile import sync_cvs

FULL = ("Python FastAPI Django PostgreSQL Redis Celery Docker Linux REST APIs "
        "authentication integrations microservices pytest backend architecture. "
        "Fully remote worldwide.")


def add_job(session, *, ats="greenhouse", title="Python Backend Developer",
            company="Acme", url="https://boards.greenhouse.io/acme/jobs/1",
            verified=True, eligibility="eligible"):
    job = Job(title=title, company=company, canonical_url=url, apply_url=url,
              description=FULL, location="Remote", remote_status="remote",
              ats=ats, ats_job_id="1", dedupe_key=url, source_id="test",
              verified=verified, eligibility_status=eligibility,
              eligibility_score=8.0)
    session.add(job)
    session.commit()
    return job.id


def make_app(session, job_id, status=AppStatus.eligible):
    app = Application(job_id=job_id, status=status.value)
    session.add(app)
    session.commit()
    return app.id


@pytest.fixture()
def orch(cfg, session):
    sync_cvs(session, cfg)
    o = Orchestrator()
    o.cfg = cfg
    return o


def test_recovery_marks_ambiguous_inflight_for_manual_review(session):
    job_id = add_job(session)
    app_id = make_app(session, job_id, AppStatus.applying)
    stale = RunRecord(state="running", stage="apply")
    session.add(stale)
    session.commit()

    note = recover_interrupted_work(session)
    session.expire_all()
    app = session.get(Application, app_id)
    assert app.status == AppStatus.needs_manual_review.value
    assert app.skip_code == "interrupted_by_restart"
    assert session.get(RunRecord, stale.id).state == "stopped"
    assert "manual review" in note


def test_recovery_requeues_preparation_states(session):
    job_id = add_job(session)
    app_id = make_app(session, job_id, AppStatus.preparing)
    recover_interrupted_work(session)
    session.expire_all()
    assert session.get(Application, app_id).status == AppStatus.eligible.value


@pytest.mark.asyncio
async def test_single_application_submitted_on_validated_ats(orch, session):
    job_id = add_job(session)
    app_id = make_app(session, job_id)
    orch._run_browser_flow = _fake_flow_submitted
    await orch.process_single_application(app_id, {"greenhouse": "production_validated"})
    session.expire_all()
    app = session.get(Application, app_id)
    assert app.status == AppStatus.submitted.value
    assert app.confirmation_text.startswith("Thank you")
    assert app.confirmation_url
    assert app.cv_name == "Backend" or app.cv_name == "Django"
    assert json.loads(app.prepared_json)["job_id"] == job_id


async def _fake_flow_submitted(cfg, url, platform, prep, outcome, app_id):
    outcome.submitted = True
    outcome.confirmation_text = "Thank you for your application"
    outcome.confirmation_url = "https://boards.greenhouse.io/acme/confirm/1"
    outcome.confirmation_slug = "thank you"


def _fake_flow_blocked(code, reason):
    async def fake(cfg, url, platform, prep, outcome, app_id):
        outcome.skipped = Blocker(code, reason)
        return False
    return fake


@pytest.mark.asyncio
async def test_captcha_application_skips_without_stopping_others(orch, session):
    job_ids = [
        add_job(session, url="https://boards.greenhouse.io/acme/jobs/1"),
        add_job(session, ats="lever", url="https://jobs.lever.co/acme/2"),
        add_job(session, ats="ashby", url="https://jobs.ashbyhq.com/acme/3"),
        add_job(session, ats="workable", url="https://apply.workable.com/acme/j/4/"),
        add_job(session, ats="smartrecruiters", url="https://careers.smartrecruiters.com/acme/5"),
    ]
    app_ids = [make_app(session, j) for j in job_ids]

    async def flow(cfg, url, platform, prep, outcome, app_id):
        if platform == "lever":
            outcome.skipped = Blocker("captcha", "human verification challenge detected")
            return False
        if platform == "workable":
            raise RuntimeError("browser crashed for one application")
        outcome.submitted = True
        outcome.confirmation_text = "Thanks for applying"
        outcome.confirmation_url = url + "/confirm"
        outcome.confirmation_slug = "thanks"
        return True

    orch._run_browser_flow = flow
    validation = {"greenhouse": "production_validated", "lever": "production_validated",
                  "workable": "production_validated",
                  "smartrecruiters": "production_validated"}
    await asyncio.gather(*[orch.process_single_application(a, validation) for a in app_ids])

    session.expire_all()
    statuses = {a.id: a.status for a in session.query(Application).all()}
    assert statuses[app_ids[0]] == AppStatus.submitted.value
    assert statuses[app_ids[1]] == AppStatus.skipped.value
    assert session.get(Application, app_ids[1]).skip_code == SkipCodes.CAPTCHA.value
    assert statuses[app_ids[2]] == AppStatus.skipped.value       # unvalidated
    assert session.get(Application, app_ids[2]).skip_code == SkipCodes.ATS_UNVALIDATED.value
    assert statuses[app_ids[3]] == AppStatus.needs_manual_review.value  # engine crash isolated
    assert statuses[app_ids[4]] == AppStatus.submitted.value
    # packages preserved even for skipped/unvalidated
    assert session.get(Application, app_ids[2]).prepared_json
    assert session.get(Application, app_ids[2]).cv_name


@pytest.mark.asyncio
async def test_unvalidated_ats_skips_before_browser(orch, session):
    job_id = add_job(session, ats="ashby", url="https://jobs.ashbyhq.com/acme/1")
    app_id = make_app(session, job_id)
    called = {"browser": False}

    async def flow(cfg, url, platform, prep, outcome, app_id):
        called["browser"] = True
        return True

    orch._run_browser_flow = flow
    await orch.process_single_application(app_id, {"ashby": "implemented"})
    session.expire_all()
    app = session.get(Application, app_id)
    assert app.status == AppStatus.skipped.value
    assert app.skip_code == SkipCodes.ATS_UNVALIDATED.value
    assert app.prepared_json and app.cover_letter
    assert called["browser"] is False


def test_validated_ats_requeues_previous_validation_skips(orch, session):
    job_id = add_job(session)
    app_id = make_app(session, job_id, AppStatus.skipped)
    app = session.get(Application, app_id)
    app.skip_code = SkipCodes.ATS_UNVALIDATED.value
    app.skip_reason = "ats not validated"
    app.finished_at = None
    session.commit()

    # Still unvalidated: must stay skipped (never auto-retry after validation).
    assert orch._requeue_retryable(session, {}) == 0
    session.expire_all()
    assert session.get(Application, app_id).status == AppStatus.skipped.value

    # Now production-validated: the application re-enters the queue.
    assert orch._requeue_retryable(session, {"greenhouse": "production_validated"}) == 1
    session.expire_all()
    app = session.get(Application, app_id)
    assert app.status == AppStatus.eligible.value
    assert app.skip_code == "" and app.skip_reason == ""


def test_requeue_never_retries_captcha_or_manual_review(orch, session):
    j1 = add_job(session, url="https://boards.greenhouse.io/acme/jobs/1")
    j2 = add_job(session, url="https://boards.greenhouse.io/acme/jobs/2")
    a1 = make_app(session, j1, AppStatus.skipped)
    a2 = make_app(session, j2, AppStatus.needs_manual_review)
    session.get(Application, a1).skip_code = SkipCodes.CAPTCHA.value
    session.get(Application, a2).skip_code = "unconfirmable_submission"
    session.commit()
    assert orch._requeue_retryable(session, {"greenhouse": "production_validated"}) == 0


@pytest.mark.asyncio
async def test_missing_browser_does_not_burn_the_queue(orch, session):
    job_id = add_job(session)
    app_id = make_app(session, job_id)

    async def no_start():
        orch.browser.unavailable_reason = "Executable doesn't exist: chromium"
        return False

    orch.browser.start = no_start
    out = await orch._process_queue(orch.cfg)
    assert out["browser_unavailable"] == 1
    session.expire_all()
    assert session.get(Application, app_id).status == AppStatus.eligible.value


@pytest.mark.asyncio
async def test_unsupported_platform_skips(orch, session):
    job_id = add_job(session, ats="workday", url="https://acme.workday.com/job/1")
    app_id = make_app(session, job_id)
    await orch.process_single_application(app_id, {})
    session.expire_all()
    app = session.get(Application, app_id)
    assert app.status == AppStatus.skipped.value
    assert app.skip_code == SkipCodes.ATS_UNSUPPORTED.value


@pytest.mark.asyncio
async def test_unverified_job_skips(orch, session):
    job_id = add_job(session, verified=False)
    app_id = make_app(session, job_id)
    await orch.process_single_application(app_id, {"greenhouse": "production_validated"})
    session.expire_all()
    assert session.get(Application, app_id).status == AppStatus.skipped.value


@pytest.mark.asyncio
async def test_missing_profile_information_skips(cfg, session):
    # Break the profile deliberately: no verified email.
    import yaml
    data = yaml.safe_load(open(cfg["profile_path"]))
    data["screening"]["email"]["human_verified"] = False
    open(cfg["profile_path"], "w").write(yaml.safe_dump(data))
    sync_cvs(session, cfg)
    job_id = add_job(session)
    app_id = make_app(session, job_id)
    o = Orchestrator()
    o.cfg = cfg
    await o.process_single_application(app_id, {"greenhouse": "production_validated"})
    session.expire_all()
    app = session.get(Application, app_id)
    assert app.status == AppStatus.skipped.value
    assert app.skip_code == SkipCodes.MISSING_INFO.value
    assert "profile" in app.skip_reason.lower()


def test_duplicate_application_rows_are_impossible(session):
    job_id = add_job(session)
    make_app(session, job_id)
    with pytest.raises(Exception):
        make_app(session, job_id)


def test_prepare_requires_relevant_cv(session, cfg):
    # No CV sync => no records => prepare must refuse.
    job_id = add_job(session)
    job = session.get(Job, job_id)
    out = prepare_application(session, job, cfg)
    assert out.ok is False
    assert out.skip_code == SkipCodes.MISSING_INFO.value

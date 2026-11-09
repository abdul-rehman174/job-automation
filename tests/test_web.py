"""Dashboard/API: statistics must match database records exactly."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import (AppStatus, Application, Event, Job, SourceRun, get_session)
from app import web


@pytest.fixture()
def client(cfg, session):
    original = web.load_config
    web.load_config = lambda: cfg
    web.init_engine(cfg["db_path"])
    yield TestClient(web.app)
    web.load_config = original


def add_job(session, *, title="Python Backend Developer", company="Acme",
            status="eligible", ats="greenhouse", n=1):
    url = f"https://boards.greenhouse.io/acme/jobs/{n}"
    job = Job(title=title, company=company, canonical_url=url, apply_url=url,
              description="Python FastAPI backend", location="Remote",
              remote_status="remote", ats=ats, ats_job_id=str(n), dedupe_key=url,
              source_id="test", verified=True, eligibility_status=status,
              eligibility_score=8.0)
    session.add(job)
    session.commit()
    return job.id


def add_app(session, job_id, status, **kw):
    app = Application(job_id=job_id, status=status, **kw)
    session.add(app)
    session.commit()
    return app.id


def test_status_counts_match_database(client, session):
    j1 = add_job(session, n=1)
    j2 = add_job(session, n=2, status="ineligible", title="Sales Manager")
    j3 = add_job(session, n=3, status="needs_information", title="Cloud Engineer")
    add_app(session, j1, AppStatus.submitted.value, confirmation_text="Thank you")
    add_app(session, j2, AppStatus.skipped.value, skip_code="captcha",
            skip_reason="human verification")
    add_app(session, j3, AppStatus.needs_manual_review.value,
            skip_code="legal_declaration", skip_reason="legal declaration present")
    session.add(Event(level="info", scope="test", message="hello"))
    session.add(SourceRun(source_id="greenhouse_api", ok=True, results_returned=5,
                          valid_listings=3, duration_seconds=1.2))
    session.commit()

    data = client.get("/api/status").json()
    c = data["counts"]
    assert c["jobs_discovered"] == 3
    assert c["jobs_eligible"] == 1
    assert c["jobs_ineligible"] == 1
    assert c["jobs_needs_information"] == 1
    assert c["submitted"] == 1
    assert c["skipped"] == 1
    assert c["needs_manual_review"] == 1
    assert data["profile_configured"] is True
    assert data["sources"][0]["source_id"] == "greenhouse_api"


def test_applications_and_skipped_endpoints(client, session):
    j1 = add_job(session, n=1)
    j2 = add_job(session, n=2, title="Django Developer")
    j3 = add_job(session, n=3, title="FastAPI Engineer")
    add_app(session, j1, AppStatus.submitted.value, cv_name="Backend",
            confirmation_text="Thanks", confirmation_url="https://x/confirm",
            prepared_json=json.dumps({"answers": {}}))
    add_app(session, j2, AppStatus.skipped.value, skip_code="captcha",
            skip_reason="captcha blocked", cv_name="Django",
            prepared_json=json.dumps({"answers": {}}))
    add_app(session, j3, AppStatus.needs_manual_review.value,
            skip_code="legal_declaration", skip_reason="legal declaration")

    apps = client.get("/api/applications?status=submitted").json()["applications"]
    assert len(apps) == 1
    assert apps[0]["company"] == "Acme"
    assert apps[0]["confirmation_url"] == "https://x/confirm"

    skipped = client.get("/api/skipped").json()["skipped"]
    ids = {s["position"] for s in skipped}
    assert ids == {"Django Developer", "FastAPI Engineer"}
    by_pos = {s["position"]: s for s in skipped}
    assert by_pos["Django Developer"]["prepared"] is True
    assert by_pos["Django Developer"]["cv"] == "Django"


def test_events_endpoint(client, session):
    session.add(Event(level="warn", scope="apply", message="app skipped"))
    session.commit()
    rows = client.get("/api/events").json()["events"]
    assert rows[0]["message"] == "app skipped"
    assert rows[0]["level"] == "warn"


def test_index_serves_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Job Agent" in r.text
    assert "START" in r.text


def test_control_endpoints_are_wired(client, monkeypatch):
    calls = []

    class FakeOrch:
        async def start(self):
            calls.append("start")
            return {"ok": True, "state": "running"}

        async def pause(self):
            calls.append("pause")
            return {"ok": True, "state": "paused"}

        async def resume(self):
            calls.append("resume")
            return {"ok": True, "state": "running"}

        async def stop(self):
            calls.append("stop")
            return {"ok": True, "state": "stopping"}

    monkeypatch.setattr(web, "get_orchestrator", lambda: FakeOrch())
    assert client.post("/api/start").json()["state"] == "running"
    assert client.post("/api/pause").json()["state"] == "paused"
    assert client.post("/api/resume").json()["state"] == "running"
    assert client.post("/api/stop").json()["state"] == "stopping"
    assert calls == ["start", "pause", "resume", "stop"]

"""FastAPI dashboard + control API.

Serves the monitoring UI and the START / PAUSE / RESUME / STOP controls.
All statistics are computed from the real database — no fake counters.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import DATA_DIR, load_config, missing_credentials_report
from .db import (AppStatus, Application, Event, Job, SourceRun, get_session,
                 init_engine)
from .orchestrator import get_orchestrator

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="Job Agent", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

_cfg = load_config()
init_engine(_cfg["db_path"])


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


# ---------------------------------------------------------------- controls

@app.post("/api/start")
async def start():
    return await get_orchestrator().start()


@app.post("/api/pause")
async def pause():
    return await get_orchestrator().pause()


@app.post("/api/resume")
async def resume():
    return await get_orchestrator().resume()


@app.post("/api/stop")
async def stop():
    return await get_orchestrator().stop()


# ---------------------------------------------------------------- monitoring

@app.get("/api/status")
def status():
    cfg = load_config()
    orch = get_orchestrator()
    session = get_session()
    try:
        payload = orch.status()
        payload["limitations"] = missing_credentials_report(cfg)
        payload["profile_configured"] = _profile_configured(cfg)
        payload["ats_validation"] = cfg.get("ats_validation") or {}
        payload["sources"] = _source_health(session)
        payload["db_path"] = cfg["db_path"]
        return payload
    finally:
        session.close()


def _profile_configured(cfg) -> bool:
    try:
        from .profile import load_profile
        session = get_session()
        try:
            prof = load_profile(cfg, session)
            return not prof.missing
        finally:
            session.close()
    except Exception:
        return False


def _source_health(session) -> list[dict]:
    rows = session.query(SourceRun).order_by(SourceRun.id.desc()).limit(120).all()
    latest: dict[str, SourceRun] = {}
    for r in rows:
        if r.source_id not in latest:
            latest[r.source_id] = r
    out = []
    for sid, r in latest.items():
        out.append({
            "source_id": sid,
            "last_run": r.executed_at.isoformat() if r.executed_at else "",
            "duration_s": r.duration_seconds,
            "results": r.results_returned,
            "valid": r.valid_listings,
            "ok": r.ok,
            "errors": (r.errors or "")[:300],
            "rate_limited": r.rate_limited,
        })
    return sorted(out, key=lambda x: x["source_id"])


@app.get("/api/applications")
def applications(status: str = Query(default=""), limit: int = Query(default=200, le=1000)):
    session = get_session()
    try:
        q = session.query(Application, Job).join(Job, Application.job_id == Job.id)
        if status:
            q = q.filter(Application.status == status)
        rows = q.order_by(Application.id.desc()).limit(limit).all()
        return {"applications": [_app_row(a, j) for a, j in rows]}
    finally:
        session.close()


@app.get("/api/skipped")
def skipped():
    session = get_session()
    try:
        rows = (session.query(Application, Job).join(Job, Application.job_id == Job.id)
                .filter(Application.status.in_((AppStatus.skipped.value,
                                                AppStatus.needs_manual_review.value)))
                .order_by(Application.id.desc()).limit(500).all())
        return {"skipped": [_app_row(a, j) for a, j in rows]}
    finally:
        session.close()


@app.get("/api/events")
def events(limit: int = Query(default=80, le=500)):
    session = get_session()
    try:
        rows = session.query(Event).order_by(Event.id.desc()).limit(limit).all()
        return {"events": [{"id": e.id, "ts": e.ts.isoformat() if e.ts else "",
                            "level": e.level, "scope": e.scope,
                            "message": e.message} for e in rows]}
    finally:
        session.close()


@app.get("/api/jobs")
def jobs(eligibility: str = Query(default=""), limit: int = Query(default=200, le=1000)):
    session = get_session()
    try:
        q = session.query(Job)
        if eligibility:
            q = q.filter(Job.eligibility_status == eligibility)
        rows = q.order_by(Job.id.desc()).limit(limit).all()
        return {"jobs": [{"id": j.id, "title": j.title, "company": j.company,
                          "url": j.apply_url, "location": j.location,
                          "remote": j.remote_status, "ats": j.ats,
                          "source": j.source_id, "verified": j.verified,
                          "score": j.eligibility_score,
                          "status": j.eligibility_status,
                          "reasons": (j.eligibility_reasons or "")[:400],
                          "discovered": j.discovered_at.isoformat() if j.discovered_at else ""}
                         for j in rows]}
    finally:
        session.close()


@app.get("/api/report")
def report():
    return {"report": get_orchestrator().report}


def _app_row(a: Application, j: Job) -> dict:
    return {
        "id": a.id, "job_id": j.id,
        "company": j.company, "position": j.title,
        "apply_url": j.apply_url, "source": j.source_id, "ats": j.ats,
        "status": a.status, "skip_code": a.skip_code, "skip_reason": a.skip_reason,
        "error": a.error, "cv": a.cv_name, "cover_letter": (a.cover_letter or "")[:3000],
        "confirmation_text": a.confirmation_text,
        "confirmation_url": a.confirmation_url,
        "evidence_dir": a.evidence_dir,
        "date": a.finished_at.isoformat() if a.finished_at else
                (a.started_at.isoformat() if a.started_at else ""),
        "attempts": a.attempts,
        "prepared": bool(a.prepared_json),
        "eligibility_score": j.eligibility_score,
    }


def run(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn
    uvicorn.run("app.web:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()

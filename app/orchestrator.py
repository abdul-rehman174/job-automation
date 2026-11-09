"""Autonomous orchestrator: START / PAUSE / RESUME / STOP, queue management,
per-application error isolation, restart recovery, and the final run report.

Design rules enforced (product spec):

1. The global workflow NEVER stops because of one application. Every
   application runs as an independent task with its own try/except. Blocked,
   failed, or skipped applications are recorded and the queue continues.

2. No per-job approval exists anywhere. Eligible jobs on validated ATS
   platforms are submitted automatically.

3. Submissions only happen on ATS platforms with `production_validated`
   validation status (config key `ats_validation`, per spec §8). Anything
   less is SKIPPED as `ats_not_validated`, preserving the prepared package
   and original URL for manual review.

4. Duplicate protection: one Application row per job (unique constraint),
   status checks before every submission, and idempotent recovery.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select

from .apply_engine.adapters import FillOutcome
from .apply_engine.adapters import STATUS as ATS_STATUS_LEVELS
from .apply_engine.browser import BrowserManager
from .apply_engine.prepare import prepare_application
from .config import load_config
from .db import (AppStatus, Application, EvidenceRecord, Job, RunRecord, SkipCodes,
                 get_session, init_engine, log_and_print)


def _now():
    return datetime.now(timezone.utc)


def _utc_stamp() -> str:
    return _now().strftime("%Y%m%d_%H%M%S")


def _mark(session, app: Application, status: AppStatus, code: str, reason: str) -> None:
    """Set application terminal state (skip / fail / review) safely."""
    if app is None:
        return
    app.status = status.value
    app.skip_code = (code or "")[:60]
    if status == AppStatus.needs_manual_review and not app.skip_reason:
        app.skip_reason = reason
    elif status != AppStatus.submitted:
        app.skip_reason = (reason or "")[:2000]
    app.finished_at = _now()


def recover_interrupted_work(session) -> str:
    """On START: recover applications left mid-flight by a previous crash.

    Idempotent rule: never auto-resubmit something whose outcome is unknown.
      - status applying => outcome ambiguous => needs_manual_review.
      - statuses (evaluating/preparing) => reset back to eligible.
      - stale running runs => marked stopped.
    """
    stale_runs = session.query(RunRecord).filter(RunRecord.state.in_(("running", "paused"))).all()
    notes = []
    for run in stale_runs:
        note = f"run #{run.id} interrupted (restart detected); marked stopped"
        run.state = "stopped"
        run.finished_at = _now()
        if run.note:
            note += f"; previous note: {run.note[:200]}"
        run.note = note[:2000]
        notes.append(note)

    applying = session.query(Application).filter_by(status=AppStatus.applying.value).all()
    for app in applying:
        # Ambiguous outcome: may or may not have been submitted before the crash.
        _mark(session, app, AppStatus.needs_manual_review, "interrupted_by_restart",
              "application was in progress when the process restarted; "
              "outcome unknown - review before any resubmission")
    preparing = session.query(Application).filter(
        Application.status.in_((AppStatus.evaluating.value, AppStatus.preparing.value))).all()
    for app in preparing:
        app.status = AppStatus.eligible.value
        app.skip_reason = ""
        app.skip_code = ""
    session.commit()
    return "; ".join(notes) + (f"; recovered {len(applying)} in-flight -> manual review" if applying else "")


class Orchestrator:
    """One run at a time per process. The UI calls start/pause/resume/stop."""

    def __init__(self):
        self.cfg: dict = {}
        self._task: asyncio.Task | None = None
        self.state = "idle"  # idle|running|paused|stopping|finished|stopped|error
        self.stage = "idle"
        self.run_id: int | None = None
        self.report: dict = {}
        self.notes: list[str] = []
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stopping = False
        self.browser = BrowserManager({})
        self._host_guard = asyncio.Lock()
        self._host_last: dict[str, float] = {}
    # ---------------------------------------------------------------- control

    def _config(self) -> dict:
        if not self.cfg:
            self.cfg = load_config()
        return self.cfg

    async def start(self) -> dict:
        if self.state in ("running", "paused"):
            return {"ok": False, "reason": "a run is already active", "state": self.state}
        if self._task and not self._task.done() and not self._task.cancelled():
            return {"ok": False, "reason": "previous run is still winding down", "state": self.state}
        cfg = self._config()
        init_engine(cfg["db_path"])
        self.browser = BrowserManager(cfg)
        self._stopping = False
        self._pause_event.set()
        self.state = "running"
        self.stage = "starting"
        self.report = {}
        self.notes = []
        self._task = asyncio.create_task(self._main())
        return {"ok": True, "state": self.state}

    async def pause(self) -> dict:
        if self.state != "running":
            return {"ok": False, "reason": "not running", "state": self.state}
        self._pause_event.clear()
        self.state = "paused"
        self._run_event("paused")
        return {"ok": True, "state": self.state}

    async def resume(self) -> dict:
        if self.state != "paused":
            return {"ok": False, "reason": "not paused", "state": self.state}
        self._pause_event.set()
        self.state = "running"
        self._run_event("running")
        return {"ok": True, "state": self.state}

    async def stop(self) -> dict:
        if self.state in ("idle", "finished", "stopped", "error"):
            return {"ok": False, "reason": "no active run", "state": self.state}
        self._stopping = True
        self._pause_event.set()  # unblock paused workers so they can exit
        self.state = "stopping"
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=0)
            except (asyncio.TimeoutError, asyncio.InvalidStateError):
                pass
        return {"ok": True, "state": self.state}

    def status(self) -> dict:
        session = get_session()
        try:
            counts = self._counts(session)
        finally:
            session.close()
        return {
            "state": self.state, "stage": self.stage, "run_id": self.run_id,
            "counts": counts, "report": self.report, "notes": self.notes[-30:],
        }

    def _run_event(self, state: str) -> None:
        if self.run_id is None:
            return
        session = get_session()
        try:
            run = session.get(RunRecord, self.run_id)
            if run:
                run.state = state
                session.commit()
        finally:
            session.close()

    def _set_stage(self, stage: str) -> None:
        self.stage = stage
        if self.run_id is None:
            return
        session = get_session()
        try:
            run = session.get(RunRecord, self.run_id)
            if run:
                run.stage = stage
                session.commit()
        finally:
            session.close()

    # ---------------------------------------------------------------- helpers

    async def _wait_if_paused(self) -> None:
        if self._pause_event.is_set():
            return
        log_and_print_text("PAUSED: workflow waiting; press RESUME to continue")
        while not self._pause_event.is_set() and not self._stopping:
            await asyncio.sleep(0.4)

    async def _pace_host(self, url: str) -> None:
        """Politeness pacing: minimum seconds between hits to the same host."""
        min_wait = float(self._config().get("per_domain_min_seconds", 8))
        try:
            host = (urlparse(url).netloc or "").lower()
        except ValueError:
            host = ""
        if not host:
            return
        async with self._host_guard:
            last = self._host_last.get(host)
            wait = 0.0
            if last is not None:
                wait = max(0.0, min_wait - (time.time() - last))
            self._host_last[host] = time.time() + wait
        if wait > 0:
            await asyncio.sleep(wait)

    # ---------------------------------------------------------------- main

    async def _main(self) -> None:
        cfg = self._config()
        run_id = None
        report: dict = {"notes": []}
        try:
            session = get_session()
            try:
                recovery_note = recover_interrupted_work(session)
                from .profile import sync_cvs
                sync_cvs(session, cfg)
                run = RunRecord(state="running", stage="discover", note=recovery_note[:2000])
                session.add(run)
                session.commit()
                run_id = run.id
                self.run_id = run_id
                report["recovery"] = recovery_note
            finally:
                session.close()

            await self._wait_if_paused()
            if self._stopping:
                raise _StopRequested()

            # ---- Phase 1: discovery -------------------------------------------
            self._set_stage("discover")
            log_and_print_text("RUN START: discovering jobs across all configured sources")
            from .discovery import run_discovery_phase
            try:
                dreport = await run_discovery_phase(cfg)
            except Exception as exc:
                # A total discovery failure must not prevent applying to the
                # existing verified queue.
                dreport = {"error": f"{type(exc).__name__}: {exc}", "sources": []}
                log_and_print_text(f"discovery phase error (queue still processed): {exc}",
                                   level="error")
            report["discovery"] = dreport
            await self._wait_if_paused()
            if self._stopping:
                raise _StopRequested()

            # ---- Phase 2: application queue ------------------------------------
            self._set_stage("apply")
            app_report = await self._process_queue(cfg)
            report["applications"] = app_report

            # ---- Phase 3: finalize ----------------------------------------------
            self._set_stage("finalize")
            session = get_session()
            try:
                counts = self._counts(session)
            finally:
                session.close()
            report["counts"] = counts
            self.report = report
            final_state = "stopped" if self._stopping else "finished"
            self._set_run_state(final_state)
            self.state = final_state
            log_and_print_text(f"RUN COMPLETE: {stats_line(counts)}")
        except _StopRequested:
            self._set_run_state("stopped")
            self.report = report
            self.state = "stopped"
            log_and_print_text("RUN STOPPED by user")
        except asyncio.CancelledError:
            self._set_run_state("stopped")
            self.state = "stopped"
            raise
        except Exception as exc:
            log_and_print_text(f"orchestrator workflow error (run ends safely): {exc}\n"
                               f"{traceback.format_exc(limit=4)}", level="error")
            self.report = {**report, "workflow_error": f"{type(exc).__name__}: {exc}"}
            self._set_run_state("error", note=str(exc))
            self.state = "error"
        finally:
            await self.browser.stop()

    def _set_run_state(self, state: str, note: str = "") -> None:
        if self.run_id is None:
            return
        session = get_session()
        try:
            run = session.get(RunRecord, self.run_id)
            if run:
                run.state = state
                run.finished_at = _now() if state in ("finished", "stopped", "error") else run.finished_at
                if note:
                    run.note = (f"{run.note}; {note}")[:2000]
                session.commit()
        finally:
            session.close()

    # ---------------------------------------------------------------- queue

    async def _process_queue(self, cfg: dict) -> dict:
        validation_cfg = (cfg.get("ats_validation") or {})
        session = get_session()
        try:
            # Retry applications that were only blocked by ATS validation: once a
            # real portal is production-validated, they re-enter the queue.
            requeued = self._requeue_retryable(session, validation_cfg)
            # Only eligible + verified jobs, and only where no Application exists yet.
            rows = (
                session.query(Job.id, Job.ats)
                .filter(Job.eligibility_status == "eligible")
                .filter(Job.verified.is_(True))
                .outerjoin(Application, Application.job_id == Job.id)
                .filter(Application.id.is_(None))
                .order_by(Job.eligibility_score.desc())
                .limit(max(1, int(cfg.get("max_applications_per_run", 150))))
                .all()
            )
            created = []
            for job_id, _ats in rows:
                existing = session.query(Application).filter_by(job_id=job_id).first()
                if existing:
                    continue
                app = Application(job_id=job_id, status=AppStatus.eligible.value)
                session.add(app)
                session.flush()
                created.append(app.id)
            session.commit()
            pending = [a for (a,) in session.query(Application.id)
                       .filter_by(status=AppStatus.eligible.value).all()]
        finally:
            session.close()

        log_and_print_text(f"APPLICATION QUEUE: {len(pending)} applications to process"
                           + (f" ({requeued} re-queued after ATS validation)" if requeued else ""))
        out = {"pending": len(pending), "created": len(created), "requeued": requeued}
        if not pending:
            return out

        if not self.browser.started:
            if not await self.browser.start():
                # Do NOT burn the queue: applications stay eligible and will be
                # processed on a later START once browser automation works.
                reason = (f"browser automation unavailable: {self.browser.unavailable_reason}. "
                          f"{len(pending)} applications left queued (not skipped).")
                log_and_print_text(reason, level="error", scope="apply")
                out["browser_unavailable"] = len(pending)
                out["browser_reason"] = self.browser.unavailable_reason
                return out

        concurrency = max(1, int(cfg.get("concurrency", 2)))
        sem = asyncio.Semaphore(concurrency)

        async def worker(app_id: int) -> None:
            async with sem:
                await self._wait_if_paused()
                if self._stopping:
                    _requeue_left(app_id)
                    return
                try:
                    await self.process_single_application(app_id, validation_cfg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _record_engine_failure(app_id, exc)

        await asyncio.gather(*[worker(i) for i in pending])
        await self.browser.stop()
        session = get_session()
        try:
            counts = self._counts(session)
        finally:
            session.close()
        out.update(counts)
        return out

    def _requeue_retryable(self, session, validation_cfg: dict) -> int:
        """Re-queue skips whose only blocker was an unvalidated ATS.

        Runs after `scripts/validate_ats.py` marks a platform
        `production_validated`. Conservative: only this exact skip code is
        retried; CAPTCHA/legal/manual-review outcomes are never auto-retried.
        """
        required = ATS_STATUS_LEVELS.get("production_validated", 4)
        rows = (session.query(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .filter(Application.status == AppStatus.skipped.value)
                .filter(Application.skip_code == SkipCodes.ATS_UNVALIDATED.value)
                .all())
        count = 0
        for app, job in rows:
            platform = (job.ats or "").lower()
            status = (validation_cfg.get(platform) or "unvalidated").lower()
            if ATS_STATUS_LEVELS.get(status, 0) >= required:
                app.status = AppStatus.eligible.value
                app.skip_code = ""
                app.skip_reason = ""
                app.finished_at = None
                count += 1
        if count:
            session.commit()
        return count

    # ---------------------------------------------------------------- one application

    async def process_single_application(self, app_id: int, validation_cfg: dict) -> None:
        """Fully independent execution of ONE application. Never raises."""
        cfg = self._config()
        session = get_session()
        try:
            app = session.get(Application, app_id)
            if app is None or app.status != AppStatus.eligible.value:
                return
            job = session.get(Job, app.job_id)
            if job is None or not job.verified:
                _mark(session, app, AppStatus.skipped, SkipCodes.NOT_FOUND.value,
                      "job record missing or unverified at apply time")
                session.commit()
                return

            app.status = AppStatus.applying.value
            app.started_at = _now()
            app.attempts += 1
            session.commit()

            # ---- platform + validation gate ----------------------------------
            platform = (job.ats or "").lower()
            if platform not in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters"):
                _mark(session, app, AppStatus.skipped, SkipCodes.ATS_UNSUPPORTED.value,
                      f"platform '{platform or 'unknown'}' is not supported by an implemented adapter")
                session.commit()
                log_and_print_text(f"app #{app_id} skipped: unsupported platform", scope="apply")
                return
            status = (validation_cfg.get(platform) or "unvalidated").lower()

            # ---- prepare -----------------------------------------------------
            prep = prepare_application(session, job, cfg)
            if not prep.ok:
                _mark(session, app, AppStatus.skipped, prep.skip_code,
                      prep.skip_reason + (f" [{prep.reason_detail}]" if prep.reason_detail else ""))
                session.commit()
                log_and_print_text(f"app #{app_id} skipped at prepare: {(prep.skip_reason or '')[:140]}",
                                   level="warn", scope="apply")
                return
            app.prepared_json = json.dumps(prep.package)[:100000]
            app.cv_id = prep.cv.id
            app.cv_name = prep.cv.name
            app.cover_letter = (prep.cover_letter or "")[:20000]
            session.commit()
            ev_dir = Path(cfg.get("data_dir", "data")) / "evidence" / str(app_id)
            ev_dir.mkdir(parents=True, exist_ok=True)
            (ev_dir / "application_package.json").write_text(json.dumps(prep.package, indent=2))
            (ev_dir / "cover_letter.txt").write_text(prep.cover_letter)
            app.evidence_dir = str(ev_dir)
            session.commit()

            # ---- validation gate (AFTER preparation: package preserved) ------
            if ATS_STATUS_LEVELS.get(status, 0) < ATS_STATUS_LEVELS.get("production_validated", 4):
                _mark(session, app, AppStatus.skipped, SkipCodes.ATS_UNVALIDATED.value,
                      f"ATS '{platform}' validation status is '{status}'. Automatic submission "
                      "requires real-portal validation (scripts/validate_ats.py). Prepared "
                      "package + original URL preserved for manual review.")
                session.commit()
                log_and_print_text(f"app #{app_id} skipped: ATS '{platform}' '{status}' -> manual review path",
                                   level="warn", scope="apply")
                return
        finally:
            session.close()

        # ---- browser automation (real portal) ----------------------------------
        await self._pace_host(job.apply_url)
        outcome = FillOutcome()
        try:
            adapter_ready = await self._run_browser_flow(cfg, job.apply_url, platform, prep, outcome, app_id)
        except Exception as exc:  # engine-level isolation, per application
            session = get_session()
            try:
                app = session.get(Application, app_id)
                _mark(session, app, AppStatus.needs_manual_review, "engine_error",
                      f"browser automation error: {type(exc).__name__}: {exc}")
                session.commit()
            finally:
                session.close()
            log_and_print_text(f"app #{app_id} engine error (isolated): {exc}", level="error", scope="apply")
            return

        session = get_session()
        try:
            app = session.get(Application, app_id)
            _record_outcome(session, app, outcome, ev_dir)
            session.commit()
            if outcome.skipped:
                log_and_print_text(f"app #{app_id} skipped: {outcome.skipped.reason}", level="warn", scope="apply")
            elif outcome.submitted:
                log_and_print_text(f"app #{app_id} SUBMITTED with confirmation", scope="apply")
            else:
                log_and_print_text(f"app #{app_id} ended: {outcome.error or 'ambiguous outcome'}",
                                   level="warn", scope="apply")
        finally:
            session.close()

    async def _run_browser_flow(self, cfg: dict, url: str, platform: str, prep, outcome: FillOutcome, app_id: int) -> bool:
        from .apply_engine.adapters import ADAPTERS
        adapter_cls = ADAPTERS.get(platform)
        if adapter_cls is None:
            outcome.skipped = _b("ats_unsupported", f"no adapter for platform {platform}")
            return False
        context = None
        page = None
        try:
            context = await self.browser.new_context()
            page = await context.new_page()
            timeout_ms = int(cfg.get("page_commit_timeout_seconds", 45)) * 1000
            app_cfg = {"job": {"apply_url": url}, "package": prep.package, "timeout_ms": timeout_ms}
            adapter = adapter_cls(page, app_cfg)
            if not await adapter.open():
                outcome.skipped = _b("form_not_reliably_parsable",
                                     f"application page failed to load: {(adapter.outcome.error or '')[:200]}")
                return False
            await screenshot(page, self._evidence_dir(cfg, app_id), "01_opened.png", app_id, self)
            if not await adapter.check_platform_and_blockers():
                outcome.skipped = adapter.outcome.skipped
                await screenshot(page, self._evidence_dir(cfg, app_id), "02_blocker.png", app_id, self)
                return False
            # --- validate employer page again before submitting (spec §7 step 2) ---
            if not page.url or "about:blank" in page.url:
                outcome.skipped = _b("form_not_reliably_parsable", "navigation failed, empty page")
                return False
            filled_ok = await adapter.fill_out()
            await screenshot(page, self._evidence_dir(cfg, app_id), "03_filled.png", app_id, self)
            if not filled_ok:
                outcome.skipped = adapter.outcome.skipped
                return False
            submitted = await adapter.submit()
            await screenshot(page, self._evidence_dir(cfg, app_id), "04_after_submit.png", app_id, self)
            if submitted:
                outcome.submitted = True
                outcome.confirmation_url = adapter.outcome.confirmation_url
                outcome.confirmation_text = adapter.outcome.confirmation_text
                outcome.confirmation_slug = adapter.outcome.confirmation_slug
            else:
                # ambiguous: click happened but no credible confirmation found
                outcome.error = "click attempted but no credible employer confirmation detected"
                outcome.fields_filled = adapter.outcome.fields_filled
            return True
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    def _evidence_dir(self, cfg: dict, app_id: int) -> str:
        d = Path(cfg.get("evidence_dir", "data/evidence")) / str(app_id)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)


class _StopRequested(Exception):
    pass


def _b(code: str, reason: str):
    from .apply_engine.detect import Blocker
    return Blocker(code, reason)


def _record_outcome(session, app: Application, outcome: FillOutcome, ev_dir: str = "") -> None:
    if outcome.skipped is not None:
        code_map = {
            "captcha": SkipCodes.CAPTCHA,
            "otp": SkipCodes.OTP,
            "login_required": SkipCodes.LOGIN_REQUIRED,
            "missing_information": SkipCodes.MISSING_INFO,
            "legal_declaration": SkipCodes.LEGAL_DECLARATION,
            "demographic": SkipCodes.HUMAN_REQUIRED,
            "demographic_questions": SkipCodes.HUMAN_REQUIRED,
            "ats_unsupported": SkipCodes.ATS_UNSUPPORTED,
            "ats_unvalidated": SkipCodes.ATS_UNVALIDATED,
            "form_not_reliably_parsable": SkipCodes.FORM_UNPARSABLE,
            "job_expired_or_missing": SkipCodes.NOT_FOUND,
            "human_verification_required": SkipCodes.HUMAN_REQUIRED,
        }
        code = code_map.get(outcome.skipped.code, SkipCodes.HUMAN_REQUIRED)
        _mark(session, app, AppStatus.skipped, code.value,
              f"{outcome.skipped.reason} (detail: {outcome.skipped.detail})")
        return
    if outcome.submitted and outcome.confirmation_slug:
        app.status = AppStatus.submitted.value
        app.confirmation_text = (outcome.confirmation_text or "")[:2000]
        app.confirmation_url = (outcome.confirmation_url or "")[:1000]
        app.confirmation_slug = (outcome.confirmation_slug or "")[:120]
        app.error = ""
        session.add(EvidenceRecord(application_id=app.id, kind="confirm",
                                   path=str(Path(ev_dir) / "04_after_submit.png"),
                                   note=f"confirmation matched: {outcome.confirmation_slug}"))
        app.finished_at = _now()
        return
    # clicked but ambiguous outcome - do NOT claim submitted
    if outcome.error:
        _mark(session, app, AppStatus.needs_manual_review, "unconfirmable_submission",
              f"submitted click occurred but confirmation could not be verified: {outcome.error}")
        app.error = outcome.error[:1000]
        session.add(EvidenceRecord(application_id=app.id, kind="page_text",
                                   path=str(Path(ev_dir) / "04_after_submit.png"),
                                   note=outcome.error))
    else:
        _mark(session, app, AppStatus.needs_manual_review, "unconfirmable_submission",
              "outcome ambiguous after submit - verify manually before assuming success")


def _record_engine_failure(app_id: int, exc: Exception) -> None:
    session = get_session()
    try:
        app = session.get(Application, app_id)
        if app and app.status in (AppStatus.applying.value, AppStatus.eligible.value):
            _mark(session, app, AppStatus.needs_manual_review, "engine_error",
                  f"unexpected engine exception: {type(exc).__name__}: {exc}")
        session.commit()
        log_and_print_text(f"app #{app_id} worker exception (isolated): {exc}", level="error", scope="apply")
    finally:
        session.close()


def _requeue_left(app_id: int) -> None:
    """STOP was requested before this application ran: keep it queued as eligible."""
    session = get_session()
    try:
        app = session.get(Application, app_id)
        if app and app.status == AppStatus.eligible.value:
            app.skip_reason = "queued for next run (stop requested)"
            session.commit()
    finally:
        session.close()


async def screenshot(page, out_dir, name, app_id, orch) -> None:
    await _screenshot_async(page, out_dir, name, app_id, orch)


async def _screenshot_async(page, out_dir: str, name: str, app_id: int, orch) -> None:
    try:
        path = Path(out_dir) / name
        await page.screenshot(path=str(path), full_page=False, timeout=8000)
        session = get_session()
        try:
            session.add(EvidenceRecord(application_id=app_id, kind="screenshot",
                                       path=str(path), note=name))
            session.commit()
        finally:
            session.close()
    except Exception:
        pass


def stats_line(counts: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def log_and_print_text(message: str, level: str = "info", scope: str = "orchestrator") -> None:
    print(f"· [{scope}] {message}")
    from .db import Event
    session = get_session()
    try:
        session.add(Event(level=level, scope=scope, message=message[:4000]))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


_singleton: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _singleton
    if _singleton is None:
        _singleton = Orchestrator()
    return _singleton


# Orchestrator._counts ------------------------------------------------------------

def _counts_impl(self) -> dict:
    """Importable reference; the bound method mirrors this logic."""
    return {}


def _attach_counts():
    def _counts(self, session) -> dict:
        q = session.query(Application.status, func.count(Application.id)).group_by(Application.status)
        counts = {AppStatus.submitted.value: 0, AppStatus.skipped.value: 0,
                  AppStatus.needs_manual_review.value: 0, AppStatus.eligible.value: 0,
                  AppStatus.ineligible.value: 0, AppStatus.failed.value: 0,
                  AppStatus.duplicate.value: 0}
        for status, n in q:
            counts[status] = int(n)
        jobs_total = session.query(func.count(Job.id)).scalar() or 0
        jobs_eligible = session.query(func.count(Job.id)).filter(
            Job.eligibility_status == "eligible").scalar() or 0
        jobs_ineligible = session.query(func.count(Job.id)).filter(
            Job.eligibility_status == "ineligible").scalar() or 0
        jobs_needs_info = session.query(func.count(Job.id)).filter(
            Job.eligibility_status.in_(("needs_information", "manual_review"))).scalar() or 0
        counts["jobs_discovered"] = int(jobs_total)
        counts["jobs_eligible"] = int(jobs_eligible)
        counts["jobs_ineligible"] = int(jobs_ineligible)
        counts["jobs_needs_information"] = int(jobs_needs_info)
        from sqlalchemy import and_
        counts["applications_prepared"] = session.query(func.count(Application.id)).filter(
            and_(Application.prepared_json.isnot(None), Application.cv_name != "")).scalar() or 0
        counts["applications_failed"] = session.query(func.count(Application.id)).filter(
            Application.error.isnot(None), Application.error != "").scalar() or 0
        # submitted + needs_verification
        counts["needs_verification"] = session.query(func.count(Application.id)).filter(
            Application.status == AppStatus.needs_verification.value).scalar() or 0
        return counts

    Orchestrator._counts = _counts


_attach_counts()

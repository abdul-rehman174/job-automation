"""Real ATS validation workflow (spec §8).

Validation is evidence-based, never assumed:

  implemented        -> code exists for the platform
  mock_tested        -> automated tests exercise the adapter against fixtures
  real_portal_tested -> the adapter opened a REAL employer application page,
                        detected the platform/form, mapped fields and uploaded
                        the CV (without clicking submit). Requires --confirm.
  production_validated -> one real application was actually submitted and a
                        credible employer confirmation was detected.

This script WILL open a real employer page in a visible browser. It does NOT
submit unless you pass --submit. Only `production_validated` platforms are
allowed to submit during autonomous runs.

Usage:
  python -m scripts.validate_ats greenhouse --job-url "https://boards.greenhouse.io/..."
  python -m scripts.validate_ats greenhouse --job-url "..." --submit --confirm
  python -m scripts.validate_ats --show
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.config import load_config, save_config  # noqa: E402
from app.apply_engine.adapters import ADAPTERS   # noqa: E402
from app.apply_engine.browser import BrowserManager  # noqa: E402
from app.apply_engine.detect import detect_platform  # noqa: E402
from app.db import Application, Job, get_session, init_engine  # noqa: E402
from app.profile import load_profile, select_cv  # noqa: E402


async def validate(platform: str, job_url: str, submit: bool, confirm: bool) -> None:
    cfg = load_config()
    init_engine(cfg["db_path"])
    platform = platform.lower()
    if platform not in ADAPTERS:
        print(f"Unknown platform '{platform}'. Implemented: {', '.join(ADAPTERS)}")
        return
    if not confirm:
        print("Refusing to open a real portal without --confirm (safety).")
        return

    session = get_session()
    try:
        profile = load_profile(cfg, session)
        if profile.missing:
            print("Profile incomplete; fill data/candidate.yml first. Missing:",
                  ", ".join(profile.missing))
            return
        from app.db import CVRecord
        cvs = session.query(CVRecord).filter_by(available=True).all()
        if not cvs:
            print("No available CVs. Run python -m scripts.setup_profile first.")
            return
        # Use the first CV only to verify the upload control; no real application row.
        cv = cvs[0]
        package = {"answers": {"first_name": profile.first_name,
                               "last_name": profile.last_name,
                               "email": profile.email, "phone": profile.phone},
                   "cv_path": cv.path, "cv_name": cv.name, "cover_letter": ""}
    finally:
        session.close()

    browser = BrowserManager({**cfg, "browser_headless": False})
    if not await browser.start():
        print("Browser failed to start:", browser.unavailable_reason)
        return
    print(f"\nValidation: {platform} | {job_url}")
    print("A visible Chromium window will open. CAPTCHA/login legs will be reported, not bypassed.\n")
    context = await browser.new_context()
    page = await context.new_page()
    outcome = None
    try:
        cfg_local = {"job": {"apply_url": job_url}, "package": package, "timeout_ms": 60000}
        adapter = ADAPTERS[platform](page, cfg_local)
        if not await adapter.open():
            print("RESULT: page did not load ->", adapter.outcome.error)
            return
        detected = detect_platform(page.url)
        print("Detected platform from final URL:", detected or "unknown")
        ok = await adapter.check_platform_and_blockers()
        if not ok:
            blk = adapter.outcome.skipped
            if blk:
                print(f"RESULT: blocked ({blk.code}): {blk.reason}")
                print("This is a real portal limitation; the agent will skip such applications.")
            else:
                print("RESULT: form not detected")
            return
        print("Form detected. Filling fields (no submission yet) ...")
        filled = await adapter.fill_out()
        print("Fields filled so far:", ", ".join(adapter.outcome.fields_filled) or "none")
        if not filled:
            blk = adapter.outcome.skipped
            print("RESULT: fill failed ->", (blk.reason if blk else "unknown reason"))
            return
        if submit:
            print("\n*** SUBMIT MODE ***")
            if input("Type SUBMIT to click the real submit button: ").strip() != "SUBMIT":
                print("Aborted before submitting; cannot claim production validation.")
                return
            submitted = await adapter.submit()
            if submitted:
                print("RESULT: SUBMITTED with confirmation:", adapter.outcome.confirmation_text)
                status = "production_validated"
            else:
                print("RESULT: click attempted but no credible confirmation; NOT validated.")
                status = "real_portal_tested"
        else:
            print("\nRESULT: form filled and CV upload exercised; not submitted (dry run).")
            status = "real_portal_tested"
        # Persist the validation status in config.
        cfg2 = load_config()
        ats = dict(cfg2.get("ats_validation") or {})
        ats[platform] = status
        save_config({"ats_validation": ats})
        print(f"Validation status for {platform}: {status}")
    finally:
        await context.close()
        await browser.stop()


def show() -> None:
    cfg = load_config()
    print("ATS validation statuses (config):")
    statuses = cfg.get("ats_validation") or {}
    for p in ADAPTERS:
        print(f"  {p:18s} {statuses.get(p, 'unvalidated')}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--show":
        show()
        sys.exit(0)
    platform = args[0]
    job_url = ""
    submit = "--submit" in args
    confirm = "--confirm" in args
    if "--job-url" in args:
        job_url = args[args.index("--job-url") + 1]
    if not job_url:
        print("Provide --job-url <real employer application URL>")
        sys.exit(2)
    asyncio.run(validate(platform, job_url, submit, confirm))

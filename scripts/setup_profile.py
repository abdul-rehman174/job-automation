"""Create or update the verified candidate profile (data/candidate.yml) and CV library.

The bundled profile has been seeded from the candidate's existing CVs and
records. Run this script to:
  * review each fact and mark it human-verified,
  * fill anything missing,
  * register CV files from data/cvs/ into the database,
  * set salary expectation when you're ready (deliberately unverified by default).

Usage:  python -m scripts.setup_profile
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config                      # noqa: E402
from app.db import get_session, init_engine            # noqa: E402
from app.profile import load_profile, sync_cvs         # noqa: E402


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        val = ""
    return val or default


def main() -> None:
    cfg = load_config()
    init_engine(cfg["db_path"])
    path = Path(cfg["profile_path"])
    if not path.exists():
        print(f"No profile at {path}. Run from the project root; seeded profile should exist.")
        return
    data = yaml.safe_load(path.read_text()) or {}
    screening = data.setdefault("screening", {})

    print("\n=== Candidate profile review (Enter keeps current value) ===\n")
    fields = [
        ("first_name", "First name"),
        ("last_name", "Last name"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("city", "City"),
        ("country", "Country"),
        ("notice_period", "Notice period"),
        ("degree", "Degree"),
        ("salary_expectation", "Salary expectation (answering this enables forms that require it)"),
    ]
    for key, label in fields:
        entry = screening.get(key)
        current = ""
        if isinstance(entry, dict):
            current = str(entry.get("value", ""))
        elif entry is not None:
            current = str(entry)
        val = _ask(label, current)
        if not val:
            continue
        if isinstance(entry, dict):
            entry["value"] = val
            entry["human_verified"] = True
            entry.setdefault("source", "setup_profile (candidate confirmed)")
        else:
            screening[key] = {"value": val, "human_verified": True,
                              "source": "setup_profile (candidate confirmed)"}
    # Salary deliberately defaults to unverified unless the candidate entered it.
    if screening.get("salary_expectation", {}).get("human_verified"):
        screening.get("salary_expectation_note", {})["human_verified"] = True

    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    print(f"\nProfile saved: {path}")

    session = get_session()
    try:
        cvs = sync_cvs(session, cfg)
        print(f"CV library: {len(cvs)} records")
        for cv in cvs:
            status = "available" if cv.available else "disabled/missing"
            print(f"  - {cv.name}: {status} ({cv.path})")
        prof = load_profile(cfg, session)
        if prof.missing:
            print("\nStill missing verified core fields:", ", ".join(prof.missing))
            print("Applications needing these fields will be skipped, never guessed.")
        else:
            print("\nProfile complete for autonomous preparation.")
    finally:
        session.close()


if __name__ == "__main__":
    main()

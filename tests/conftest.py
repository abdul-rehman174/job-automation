"""Test fixtures. All tests use isolated databases/directories — never real
employer portals, never the production database."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Base, init_engine, get_session  # noqa: E402


@pytest.fixture()
def tmpdb(tmp_path):
    db = tmp_path / "test.db"
    init_engine(str(db))
    yield str(db)
    # dispose engine so files can be removed on Windows/macOS
    from app import db as dbmod
    if dbmod._engine is not None:
        dbmod._engine.dispose()


@pytest.fixture()
def session(tmpdb):
    s = get_session()
    yield s
    s.close()


@pytest.fixture()
def candidate_yaml(tmp_path):
    path = tmp_path / "candidate.yml"
    path.write_text("""
verified: true
identity:
  first_name: Abdul
  last_name: Rehman
  city: Bahawalpur
  country: Pakistan
contact:
  email: abdulrehmanshafy101@gmail.com
  phone: "+92 309 0070149"
summary: "Python backend engineer with 2.5 years of experience."
screening:
  first_name: { value: Abdul, human_verified: true }
  last_name: { value: Rehman, human_verified: true }
  email: { value: abdulrehmanshafy101@gmail.com, human_verified: true }
  phone: { value: "+92 309 0070149", human_verified: true }
  city: { value: Bahawalpur, human_verified: true }
  country: { value: Pakistan, human_verified: true }
  years_experience_python: { value: "2.5", human_verified: true }
  years_experience_total: { value: "2.5", human_verified: true }
  degree: { value: "B.S. Software Engineering", human_verified: true }
  notice_period: { value: "2 weeks", human_verified: true }
  authorized_remote_work: { value: yes, human_verified: true }
  authorized_worldwide_remote: { value: yes, human_verified: true }
  work_authorization: { value: "Pakistan citizen; remote contractor", human_verified: true }
  salary_expectation: { value: "", human_verified: false }
exclusions:
  companies: [Enigmatix]
  title_patterns: ['\\bodoo\\b', '\\berp\\b']
""")
    return path


@pytest.fixture()
def cvdir(tmp_path):
    d = tmp_path / "cvs"
    d.mkdir()
    (d / "cv_backend.pdf").write_bytes(b"%PDF-1.4 fake backend cv")
    (d / "cv_django.pdf").write_bytes(b"%PDF-1.4 fake django cv")
    (d / "manifest.json").write_text(json.dumps({"cvs": [
        {"name": "Backend", "file": "cv_backend.pdf",
         "keywords": "python, backend, fastapi, postgres, redis, celery, docker, rest, microservices, auth",
         "available": True},
        {"name": "Django", "file": "cv_django.pdf",
         "keywords": "python, backend, django, drf, postgres, mysql, rest, pytest",
         "available": True},
        {"name": "Odoo", "file": "missing_odoo.pdf", "keywords": "odoo, erp",
         "available": True},
    ]}))
    return d


@pytest.fixture()
def cfg(tmp_path, candidate_yaml, cvdir, tmpdb):
    return {
        "db_path": tmpdb,
        "profile_path": str(candidate_yaml),
        "cv_dir": str(cvdir),
        "job_age_max_days": 75,
        "concurrency": 1,
        "max_applications_per_run": 20,
        "browser_headless": True,
        "xray_provider": "none",
        "per_domain_min_seconds": 0,
    }

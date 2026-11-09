"""Configuration management.

Config is layered: defaults <- data/config.json <- environment variables.

Passwords, tokens and profile data are NEVER written to logs. Keys live in
data/config.json (created with 0600 permissions) or in environment variables
(the latter takes precedence and is never persisted by the app).
"""
import json
import os
import stat
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "jobagent.db"
CV_DIR = DATA_DIR / "cvs"
LOG_PATH = DATA_DIR / "agent.log"

DEFAULTS = {
    # --- storage ---
    "db_path": str(DB_PATH),
    "cv_dir": str(CV_DIR),
    "profile_path": str(DATA_DIR / "candidate.yml"),

    # --- execution policy ---
    # Concurrency / safety limits for the autonomous run.
    "concurrency": 2,                 # parallel application workers
    "per_domain_min_seconds": 8,      # polite pacing per employer domain
    "discover_max_jobs_per_run": 2000,
    "max_applications_per_run": 150,  # capacity guard, not a quota target
    "job_age_max_days": 75,
    "page_commit_timeout_seconds": 45,

    # --- search provider (Google X-ray) ---
    # "serpapi" or "google_pse". If no credentials are configured, X-ray is
    # recorded as a limitation and discovery continues with other sources.
    "xray_provider": "serpapi",       # serpapi | google_pse | none
    "serpapi_key_env": "SERPAPI_KEY",
    "google_pse_key_env": "GOOGLE_PSE_KEY",
    "google_pse_cx_env": "GOOGLE_PSE_CX",
    "xray_queries_per_run": 24,
    "xray_results_per_query": 10,
    "xray_max_pages": 3,

    # --- ATS company directory (handles of real public job boards) ---
    # Starter directory; extend freely. Source health is tracked per handle.
    "company_handles": {
        # Handles verified against the live public APIs on 2026-11-08.
        "greenhouse": ["databricks", "stripe", "chime", "flexport", "elastic",
                       "twilio", "checkr", "coinbase", "airbnb"],
        "lever": ["spotify", "wealthfront"],
        "ashby": ["ashby", "clerk", "resend", "linear", "sardine", "ramp"],
        "workable": ["trellis", "kissflow", "recurly", "assembly", "superside",
                     "hired", "broadway", "dixa"],
        "smartrecruiters": ["visa", "turing", "wakefit"],
    },

    # --- application automation ---
    "browser_headless": True,

    # --- security ---
    "log_redaction": True,
}

_SECRET_KEYS = {"serpapi_key", "google_pse_key", "google_pse_cx"}


def _load_file_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _resolve_secrets(cfg: dict) -> None:
    """Fill secret values from environment variables; never persist them."""
    for field in ("xray", ):
        pass
    if cfg.get("xray_provider") == "serpapi":
        env_name = cfg.get("serpapi_key_env", "SERPAPI_KEY")
        val = os.environ.get(env_name)
        if val:
            cfg["serpapi_key"] = val
    if cfg.get("xray_provider") == "google_pse":
        for field, default_env in (
            ("google_pse_key", "GOOGLE_PSE_KEY"),
            ("google_pse_cx", "GOOGLE_PSE_CX"),
        ):
            env_name = cfg.get(default_env.lower() + "_env") or default_env
            val = os.environ.get(env_name)
            if val:
                cfg[default_env.lower().replace("_env", "")] = val


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(_load_file_config())
    cfg["db_path"] = os.environ.get("JOBAGENT_DB", cfg["db_path"])
    cfg["profile_path"] = os.environ.get("JOBAGENT_PROFILE", cfg["profile_path"])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CV_DIR.mkdir(parents=True, exist_ok=True)
    for env_key, cfg_key in (
        ("SERPAPI_KEY", "serpapi_key"),
        ("GOOGLE_PSE_KEY", "google_pse_key"),
        ("GOOGLE_PSE_CX", "google_pse_cx"),
    ):
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]
    _resolve_secrets(cfg)
    return cfg


def save_config(updates: dict) -> dict:
    """Persist non-secret updates. Secrets are stored with 0600 if explicitly saved."""
    current = _load_file_config()
    merged = dict(DEFAULTS)
    merged.update(current)
    for key, value in updates.items():
        merged[key] = value
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({k: v for k, v in merged.items() if k not in _SECRET_KEYS or v}, indent=2)
    CONFIG_PATH.write_text(text)
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return load_config()


def missing_credentials_report(cfg: dict) -> list:
    """Factories of an honest limitations report."""
    missing = []
    provider = cfg.get("xray_provider", "none")
    if provider == "serpapi" and not cfg.get("serpapi_key"):
        missing.append(
            "Google X-ray: SERPAPI_KEY is not configured. X-ray discovery is "
            "disabled; all other discovery sources operate normally."
        )
    elif provider == "google_pse" and (not cfg.get("google_pse_key") or not cfg.get("google_pse_cx")):
        missing.append(
            "Google X-ray: GOOGLE_PSE_KEY / GOOGLE_PSE_CX are not configured. "
            "X-ray discovery is disabled; all other discovery sources operate normally."
        )
    elif provider == "none":
        missing.append("Google X-ray: no search provider configured; X-ray discovery is disabled.")
    return missing


def redact(value) -> str:
    if not value:
        return "<missing>"
    s = str(value)
    if len(s) <= 6:
        return "***"
    return s[:3] + "***" + s[-2:]

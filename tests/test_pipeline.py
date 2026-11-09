"""Pipeline: canonicalization, normalization, dedupe, date/remote inference."""
import datetime as dt

from app.pipeline import (Deduper, RawJob, canonicalize_url, detect_ats_from_url,
                          infer_remote_status, normalize_job)


def test_canonicalize_strips_tracking_and_fragments():
    url = "https://Jobs.Lever.co/Acme/1234/?utm_source=x&utm_medium=y&ref=z#apply"
    assert canonicalize_url(url) == "https://jobs.lever.co/Acme/1234"


def test_canonicalize_drops_www_and_default_port():
    assert canonicalize_url("https://www.example.com:443/jobs/1") == "https://example.com/jobs/1"


def test_ats_detection_from_url():
    assert detect_ats_from_url("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_ats_from_url("https://jobs.lever.co/acme/1") == "lever"
    assert detect_ats_from_url("https://jobs.ashbyhq.com/acme/1") == "ashby"
    assert detect_ats_from_url("https://apply.workable.com/acme/j/ABC/") == "workable"
    assert detect_ats_from_url("https://careers.smartrecruiters.com/Acme/1") == "smartrecruiters"
    assert detect_ats_from_url("https://example.com/careers") == ""


def test_normalize_handles_list_fields_and_html():
    raw = RawJob(title="  Python   Backend Developer ", company="Acme",
                 url="https://jobs.lever.co/acme/1",
                 description="<p>Build APIs</p>",
                 employment_type=["Full-time", "Remote"],
                 date_str="2026-10-01T00:00:00Z", ats_job_id="1",
                 ats="lever")
    j = normalize_job(raw)
    assert j.title == "Python Backend Developer"
    assert j.employment_type == "Full-time, Remote"
    assert j.published_datetime is not None
    assert j.dedupe_key == "atsid:lever:1:acme"


def test_normalize_dedupe_key_falls_back_to_company_title():
    raw = RawJob(title="Backend Developer", company="Foo Inc",
                 url="https://foo.example/careers/1",
                 date_str="2026-10-01")
    j = normalize_job(raw)
    assert j.dedupe_key.startswith("ct:foo-inc/")


def test_deduper_rejects_duplicates_by_all_three_identities():
    d = Deduper()
    assert d.add("atsid:lever:1:acme") is True
    assert d.add("atsid:lever:1:acme") is False
    assert d.add("url:https://example.com/job/1") is True
    assert d.add("url:https://example.com/job/1") is False
    assert d.add("ct:acme/python-backend") is True
    assert d.add("ct:acme/python-backend") is False


def test_remote_inference():
    assert infer_remote_status("Remote - Worldwide", None) == "remote"
    assert infer_remote_status("Berlin office", False) == "onsite"
    assert infer_remote_status("Berlin office", None) == "unknown"
    assert infer_remote_status("Work from anywhere", None) == "remote"
    assert infer_remote_status("Hybrid role in Lahore", None) == "hybrid"

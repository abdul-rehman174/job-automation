"""ATS platform detection, blocker detection, confirmation detection."""
import pytest

from app.apply_engine.detect import (detect_blockers, detect_confirmation,
                                     detect_platform)
from app.apply_engine.adapters import _answer_for


def test_platform_detection():
    assert detect_platform("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_platform("https://job-boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_platform("https://jobs.lever.co/acme/1/apply") == "lever"
    assert detect_platform("https://jobs.ashbyhq.com/acme/1/application") == "ashby"
    assert detect_platform("https://apply.workable.com/acme/j/ABC/") == "workable"
    assert detect_platform("https://careers.smartrecruiters.com/acme/1") == "smartrecruiters"
    assert detect_platform("https://custom-employer.example/apply") == "unknown"


def test_captcha_detection():
    html = '<div class="g-recaptcha" data-sitekey="x"></div><script src="https://www.google.com/recaptcha/api.js"></script>'
    b = detect_blockers(html)
    assert b and b.code == "captcha"


def test_hcaptcha_and_turnstile_detection():
    assert detect_blockers('<script src="https://js.hcaptcha.com/1/api.js"></script>').code == "captcha"
    assert detect_blockers('<div class="cf-turnstile" data-sitekey="x"></div>').code == "captcha"


def test_otp_detection():
    b = detect_blockers("<p>Enter the verification code sent to your email.</p>")
    assert b and b.code == "otp"


def test_login_detection():
    b = detect_blockers("<h1>Sign in to continue</h1><p>You must log in to apply.</p>")
    assert b and b.code == "login_required"


def test_legal_declaration_detection():
    b = detect_blockers("<label><input type='checkbox'> I certify that the information provided is true.</label>")
    assert b and b.code == "legal_declaration"


def test_demographic_detection():
    b = detect_blockers("What is your gender? <select>...</select>")
    assert b and b.code == "demographic"


def test_clean_form_has_no_blockers():
    html = '<form><label>First Name</label><input name="first_name"><label>Email</label><input type="email"></form>'
    assert detect_blockers(html) is None


def test_confirmation_detection_positive():
    slug, text = detect_confirmation("Thank you for your application! We received it.")
    assert slug is not None and "Thank you" in text


def test_confirmation_detection_negative():
    slug, text = detect_confirmation("Please review your answers below and click submit.")
    assert slug is None and text == ""


def test_answer_mapping_never_claims_us_authorization():
    ans, key = _answer_for("Are you legally authorized to work in the United States?",
                           {"answers": {"work_authorization": "Pakistan citizen"}})
    assert ans is None
    assert key == "work_authorization_unitedstates"


def test_answer_mapping_pakistan_uses_generic():
    ans, key = _answer_for("Are you authorized to work in Pakistan?",
                           {"answers": {"work_authorization": "Pakistan citizen"}})
    assert ans == "Pakistan citizen"
    assert key == "work_authorization"


def test_answer_mapping_resume_upload_is_not_a_text_answer():
    ans, key = _answer_for("Upload your CV / Resume", {"answers": {}})
    assert ans is None and key == ""

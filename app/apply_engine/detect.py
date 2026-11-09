"""Application-side detection: ATS platform + blockers (CAPTCHA / login / OTP /
legal declarations / unparsable forms).

Detection only — never bypasses anti-bot systems. A detected blocker causes the
individual application to be SKIPPED with a recorded reason; the global queue
keeps running.
"""
from __future__ import annotations

import re

_URL_PLATFORM = [
    (re.compile(r"boards\.greenhouse\.io|job-boards\.greenhouse\.io", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com|jobs\.eu\.ashbyhq\.com", re.I), "ashby"),
    (re.compile(r"apply\.workable\.com|jobs\.workable\.com", re.I), "workable"),
    (re.compile(r"careers\.smartrecruiters\.com|jobs\.smartrecruiters\.com", re.I), "smartrecruiters"),
]


def detect_platform(url: str) -> str:
    for pattern, name in _URL_PLATFORM:
        if pattern.search(url or ""):
            return name
    return "unknown"


_CAPTCHA_MARKERS = [
    (re.compile(r"recaptcha|google\.com/recaptcha|gstatic\.com/recaptcha", re.I), "google reCAPTCHA"),
    (re.compile(r"hcaptcha\.com|hcaptcha-challenge", re.I), "hCaptcha"),
    (re.compile(r"challenges\.cloudflare\.com|turnstile", re.I), "Cloudflare Turnstile"),
    (re.compile(r"captcha[-_]?(image|input|challenge|token)(?!\w)", re.I), "generic CAPTCHA widget"),
    (re.compile(r"id=\"recaptcha_response\"|name=\"captcha\"", re.I), "captcha field"),
]

_LOGIN_MARKERS = [
    re.compile(r"sign in to (continue|apply)|log ?in (to|required)|register.{0,20}(account|before applying)", re.I),
    re.compile(r"(authentication|login) required", re.I),
    re.compile(r"oauth|signin with (google|linkedin)", re.I),
    re.compile(r"you (must|need to) (sign in|log in|create an account) to apply", re.I),
]

_OTP_MARKERS = [
    re.compile(r"(verification|security) (code|pin) (was|has been)? (sent|emailed|mailed)", re.I),
    re.compile(r"enter( the)? (verification|one[- ]time|otp) code", re.I),
    re.compile(r"one[- ]time password|one[- ]time (code|passcode)", re.I),
    re.compile(r"code (sent to|sent via) (your )?(email|phone)", re.I),
]

_LEGAL_DECLARATION_MARKERS = [
    re.compile(r"\bi certify\b|\bi attest\b|\bi (hereby )?declare\b|\bunder penalty\b", re.I),
    re.compile(r"\bby (submitting|applying),? i agree\b|\bi agree to the (terms|non|policy|agreement|use)\b", re.I),
    re.compile(r"\bnon-compet(e|ition) (clause|agreement|provisions?)\b", re.I),
    re.compile(r"\bconsent to\b.{0,60}\b(background|credit|reference check|processing)\b", re.I),
    re.compile(r"\bvoluntary self[- ]?(identif|disclos)\w*", re.I),
    re.compile(r"\b(veteran status|disability|ethnic race|gender,? race)\b.{0,40}\b(identif|disclos)", re.I),
    re.compile(r"\b(e[\s-]?signature|sign (here|electronically))\b.{0,40}\b(apply|offer|contract|agree)", re.I),
]

_DEMOGRAPHIC_MARKERS = [
    re.compile(r"what is your (gender|race|ethnicity)|select your (gender|race|ethnicity)", re.I),
    re.compile(r"disability status|veteran status|do you (have|have a) (a )?disabilit", re.I),
    re.compile(r"sexual orientation|date of birth\s*\?", re.I),
]


class Blocker:
    code: str
    reason: str
    detail: str

    def __init__(self, code: str, reason: str, detail: str = ""):
        self.code = code
        self.reason = reason
        self.detail = detail

    def __repr__(self):
        return f"Blocker({self.code}: {self.reason})"


def detect_blockers(page_html: str, *, include_demographic: bool = True) -> Blocker | None:
    """Scan raw application-page HTML for blockers. Returns the first one."""
    html = page_html or ""
    if not html:
        return None
    for pattern, name in _CAPTCHA_MARKERS:
        if pattern.search(html):
            return Blocker("captcha", f"human verification challenge detected ({name})",
                           detail=name)
    for pattern in _LEGAL_DECLARATION_MARKERS:
        m = pattern.search(html)
        if m:
            return Blocker("legal_declaration", "legal attestation required", m.group(0)[:160])
    for pattern in _OTP_MARKERS:
        m = pattern.search(html)
        if m:
            return Blocker("otp", "OTP / verification code required", m.group(0)[:120])
    for pattern in _LOGIN_MARKERS:
        m = pattern.search(html)
        if m:
            return Blocker("login_required", "explicit login wall detected", m.group(0)[:120])
    for pattern in _DEMOGRAPHIC_MARKERS:
        m = pattern.search(html)
        if m:
            return Blocker("demographic", "demographic/voluntary disclosure question", m.group(0)[:160])
    return None


# Confirmation patterns (credible employer confirmation of receipt)
CONFIRMATION_PATTERNS = [
    re.compile(r"thank you for (your )?(application|applying|interest)", re.I),
    re.compile(r"your application (has been|was) (received|submitted)", re.I),
    re.compile(r"application (received|submitted|successfully submitted)", re.I),
    re.compile(r"thanks for applying", re.I),
    re.compile(r"(we|your) submission (has been|was) received", re.I),
    re.compile(r"successfully applied", re.I),
    re.compile(r"you( have|'ve)? (successfully )?applied", re.I),
]


def detect_confirmation(page_text: str) -> tuple[str | None, str]:
    """Returns (matched-slug, matched text) or (None, '')."""
    text = (page_text or "")[:20000]
    for pattern in CONFIRMATION_PATTERNS:
        m = pattern.search(text)
        if m:
            return pattern.pattern[:80], m.group(0)
    return None, ""

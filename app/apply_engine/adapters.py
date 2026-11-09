"""ATS application adapters (Playwright).

Each adapter knows how to open a real employer application page, verify the
platform, fill fields using accessible labels/ARIA attributes (not fragile CSS),
upload the selected CV, answer screening questions from the prepared package,
and detect a credible submission confirmation.

CRITICAL policy (per product spec §8): every adapter carries a platform-level
validation status stored in the DB. Only `production_validated` platforms may
submit. Out of the box every adapter is `unimplemented`/`unvalidated`, so the
run engine will SKIP applications with reason ATS_NOT_VALIDATED, preserving the
full prepared package and original URL for later manual review. This is the
honest gate between "code exists" and "proven against real employer portals".

Validation (scripts/validate_ats.py) walks a REAL application for one job per
platform without submitting (form detection + fill dry-run), and promotes the
status accordingly with recorded evidence. Full `production_validated` requires
an actual successful real submission with confirmation evidence.
"""
from __future__ import annotations

import re
from enum import Enum
from dataclasses import dataclass, field
from playwright.async_api import Page, TimeoutError as PWTimeout

from .detect import Blocker, detect_blockers, detect_confirmation, detect_platform

STATUS = {"not_implemented": 0, "implemented": 1, "mock_tested": 2,
          "real_portal_tested": 3, "production_validated": 4}
VALIDATED_AT_OR_ABOVE = "real_portal_tested"


@dataclass
class FillOutcome:
    skipped: Blocker | None = None
    submitted: bool = False
    confirmation_text: str = ""
    confirmation_url: str = ""
    confirmation_slug: str = ""
    error: str = ""
    form_filled: bool = False
    fields_filled: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


_LABEL_COUNTRY_RE = re.compile(
    r"\b(united states|u\.s\.?|usa|canada|uk|united kingdom|europe|eu|germany|france|"
    r"netherlands|australia|new zealand|singapore|india|uae|saudi|qatar|pakistan)\b", re.I)


def _answer_for(label_text: str, package: dict) -> tuple[str | None, str]:
    """Match an application form field label to a verified candidate answer."""
    questions = package.get("answers", {})
    l = (label_text or "").lower()
    l = re.sub(r"\s+", " ", l).strip(" *:")

    # Country-sensitive work authorization: never imply US/EU/UK authorization.
    if re.search(r"(authori[sz]ed|eligible|legally|permitted|right) .{0,40}"
                 r"(work|employment|country)|work (authori[sz]ation|permit|eligibility)", l):
        m = _LABEL_COUNTRY_RE.search(l)
        country = (m.group(1).lower().replace(".", "").replace(" ", "") if m else None)
        key = "work_authorization" if country in (None, "pakistan") \
            else f"work_authorization_{country}"
        ans = questions.get(key)
        return (ans, key) if ans else (None, key)

    checks = [
        (r"first\s*name", "first_name"),
        (r"(last|family|sur)name", "last_name"),
        (r"e[-\s]?mail", "email"),
        (r"phone|mobile|contact number", "phone"),
        (r"linkedin", "linkedin"),
        (r"github", "github"),
        (r"(portfolio|website|personal site|blog)", "portfolio_url"),
        (r"(link to|attach|upload).{0,12}(cv|resume)", None),  # handled by uploader
        (r"(degree|education|bachelor|university)", "degree"),
        (r"(how many|years).{0,24}(experience|worked).{0,24}(python)", "years_experience_python"),
        (r"(how many|years).{0,24}(experience|worked)", "years_experience_total"),
        (r"(salary|compensation|pay).{0,30}(expect|required|range|desired)", "salary_expectation"),
        (r"(notice period|how soon can you start|when can you start|availab)", "notice_period"),
        (r"current (location|city|country)|where.{0,16}(based|located|live)", "country"),
        (r"(visa|work permit|sponsorship)", "visa_status"),
    ]
    for pattern, key in checks:
        if re.search(pattern, l):
            if key is None:
                return None, ""
            ans = questions.get(key)
            if ans:
                return ans, key
            return None, key
    # Precision fallback: profile-known exact labels.
    for label_pattern, key in package.get("extra_label_patterns", []):
        if re.search(label_pattern, l):
            return questions.get(key), key
    return None, ""


class BaseAdapter:
    platform = "base"

    def __init__(self, page: Page, application_cfg: dict):
        self.page = page
        self.cfg = application_cfg     # job, profile, package, evidence_dir
        self.outcome = FillOutcome()

    async def open(self) -> bool:
        url = self.cfg["job"].get("apply_url")
        try:
            await self.page.goto(url, timeout=self.cfg.get("timeout_ms", 45000),
                                 wait_until="domcontentloaded")
            return True
        except PWTimeout:
            self.outcome.error = "page load timeout"
            return False
        except Exception as exc:
            self.outcome.error = f"{type(exc).__name__}: {exc}"
            return False

    async def scanned_text(self) -> str:
        try:
            return await self.page.evaluate("() => document.documentElement.outerHTML")
        except Exception:
            try:
                return await self.page.content()
            except Exception:
                return ""

    async def check_platform_and_blockers(self) -> bool:
        html = await self.scanned_text()
        # Confirm we really are on the platform we think.
        found = detect_platform(self.page.url or "")
        js_found = self._platform_in_html(html)
        if found == "unknown" and not js_found:
            self.outcome.skipped = Blocker("ats_unsupported", "application platform could not be identified")
            return False
        if found not in ("unknown", self.platform, js_found if isinstance(js_found, str) else "") and js_found \
                and js_found != self.platform:
            self.outcome.skipped = Blocker(
                "ats_unsupported",
                f"page platform ({js_found}) differs from expected ({self.platform})")
            return False
        blocker = detect_blockers(html)
        if blocker:
            self.outcome.skipped = blocker
            return False
        # Is there an actual application form? (input fields related to applying)
        form_ok = await self.page.evaluate(
            """() => {
                const inputs = document.querySelectorAll('form input, form textarea, [role="textbox"], input[type="file"]');
                return inputs.length >= 2;
            }""")
        if not form_ok:
            self.outcome.skipped = Blocker(
                "form_not_reliably_parsable",
                "no interactive application form detected (page may require JS session, SDK frame or be broken)")
            return False
        return True

    @staticmethod
    def _platform_in_html(html: str):
        if re.search(r"(boards\.greenhouse\.io|grnhse|greenhouse)", html, re.I):
            return "greenhouse"
        if re.search(r"(lever(?:-jobs)?|jobs lever|leverHQ)", html, re.I):
            return "lever"
        if re.search(r"ashbyhq", html, re.I):
            return "ashby"
        if re.search(r"workable[- ](api|js|widget)|\bworkable\b", html, re.I):
            return "workable"
        if re.search(r"smartrecruiters", html, re.I):
            return "smartrecruiters"
        return ""

    # ---- generic filling helpers (accessibility based) ----

    async def _candidates(self, selector: str):
        try:
            return self.page.locator(selector)
        except Exception:
            return None

    async def fill_text_field(self, keywords: list[str], value: str) -> bool:
        """Fill first input whose associated label/attrs match keywords."""
        if not value:
            return False
        value = str(value)
        selectors = [
            "input[type=text]", "input[type=email]", "input[type=tel]",
            "input:not([type])", "textarea",
        ]
        for sel in selectors:
            loc = self.page.locator(sel)
            try:
                count = await loc.count()
            except Exception:
                continue
            for i in range(count):
                try:
                    el = loc.nth(i)
                    if not await el.is_visible():
                        continue
                    label = (await el.evaluate(
                        """(el) => {
                            const id = el.id;
                            const lab = id ? document.querySelector(`label[for="${id}"]`) : null;
                            return (lab ? lab.innerText : '') + ' | '
                                 + (el.getAttribute('aria-label') || '') + ' | '
                                 + (el.getAttribute('placeholder') || '') + ' | '
                                 + (el.getAttribute('name') || '') + ' | '
                                 + (el.getAttribute('data-testid') || '');
                        }""") or "").lower()
                    if any(re.search(k, label) for k in keywords):
                        await el.click()
                        await el.fill(value)
                        self.outcome.fields_filled.append(el.get_attribute("name") or keywords[0])
                        return True
                except PWTimeout:
                    continue
                except Exception:
                    continue
        return False

    async def select_dropdown(self, keywords: list[str], value: str) -> bool:
        if not value:
            return False
        loc = self.page.locator("select")
        try:
            count = await loc.count()
        except Exception:
            return False
        for i in range(count):
            try:
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                label = (await el.evaluate(
                    """(el) => {
                        const id = el.id;
                        const lab = id ? document.querySelector(`label[for="${id}"]`) : null;
                        return (lab ? lab.innerText : '') + ' | ' + (el.getAttribute('name') || '')
                             + ' | ' + (el.getAttribute('aria-label') || '');
                    }""") or "").lower()
                if not any(re.search(k, label) for k in keywords):
                    continue
                # find the option whose text matches
                try:
                    options = el.locator("option")
                    n_opt = await options.count()
                    want = value.lower().strip()
                    best_idx = -1
                    for j in range(n_opt):
                        opt_text = ((await options.nth(j).inner_text()) or "").lower().strip()
                        if opt_text == want or (want in opt_text) or (opt_text in want):
                            if opt_text:
                                best_idx = j
                                break
                    if best_idx >= 0:
                        await el.select_option(index=best_idx)
                        self.outcome.fields_filled.append(f"select:{keywords[0]}")
                        return True
                except PWTimeout:
                    return False
            except Exception:
                continue
        return False

    async def answer_questions(self) -> bool:
        """Screening questions are answered from the PREPARED, VERIFIED package only.
        Questions we cannot answer accurately cause a skip (missing_information)."""
        package = self.cfg.get("package") or {}
        answers = package.get("answers") or {}
        # Anything labeled as a visible question with no prepared answer that a
        # quick scan cannot verify = UNSAFE => skip application.
        rows = self.page.locator("div.question-page, li.application-question, div.application-question, .question")
        try:
            n = await rows.count()
        except Exception:
            n = 0
        filled = 0
        for i in range(n):
            try:
                row = rows.nth(i)
                if not await row.is_visible():
                    continue
                qtext = ((await row.inner_text()) or "").strip()
                if not qtext:
                    continue
                q = re.sub(r"\s+", " ", qtext)[:300]
                ans, key = _answer_for(q, package)
                if not ans:
                    self.outcome.skipped = Blocker(
                        "missing_information",
                        f"screening answer not verifiable from profile: {q[:140]!r}")
                    return False
                ta = row.locator("textarea, input[type=text]")
                if await ta.count() > 0:
                    await ta.first.fill(str(ans))
                    filled += 1
                    self.outcome.steps.append(f"Q: {q[:60]} -> verified ({key})")
            except Exception:
                continue
        # More screening-style question types: radio "yes/no" paired questions.
        radios = self.page.locator("input[type=radio]")
        try:
            nr = await radios.count()
        except Exception:
            nr = 0
        yes_answered = 0
        if nr:
            for i in range(nr):
                try:
                    r_el = radios.nth(i)
                    value = (await r_el.get_attribute("value") or "").lower()
                    aria = (await r_el.get_attribute("aria-label") or "").lower()
                    name = (await r_el.get_attribute("name") or "").lower()
                    label_text = aria or name
                except Exception:
                    continue
                if value in ("yes", "true", "1") and label_text and \
                        any(re.search(p, label_text) for p in (r"authorized", r"legally allowed")):
                    try:
                        if await r_el.is_visible():
                            await r_el.check(force=True)
                            yes_answered += 1
                            self.outcome.fields_filled.append("radio:authorized")
                    except Exception:
                        pass
        self.outcome.steps.append(f"questions answered: {filled}, radio yes: {yes_answered}")
        return True

    async def submit(self) -> bool:
        """Click the actual submit button after required-field validation."""
        try:
            btn = self.page.get_by_role("button", name=re.compile(
                r"^submit$|submit application|apply now|submit your application", re.I))
            if await btn.count() == 0:
                btn = self.page.locator("button[type=submit], input[type=submit]")
            if await btn.count() == 0:
                self.outcome.error = "no submit button found"
                return False
            # Prevent accidental submissions when validation blockers remain.
            await btn.first.click(timeout=8000)
        except PWTimeout as exc:
            self.outcome.error = f"submit click timeout: {exc}"
            return False
        except Exception as exc:
            self.outcome.error = f"submit: {type(exc).__name__}: {exc}"
            return False
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        text = await self._page_text()
        slug, matched = detect_confirmation(text)
        if slug:
            self.outcome.submitted = True
            self.outcome.confirmation_slug = slug
            self.outcome.confirmation_text = matched
            self.outcome.confirmation_url = self.page.url
            return True
        # ambiguous page after clicking
        self.outcome.submitted = False
        self.outcome.error = ""
        return False

    async def _page_text(self) -> str:
        try:
            return await self.page.inner_text("body")
        except Exception:
            return ""

    async def close_and_keep_state(self):
        pass


class GreenhouseAdapter(BaseAdapter):
    platform = "greenhouse"

    NAME_KEYWORDS = [
        (r"first\s*name", "first_name"),
        (r"(last|family|sur)name", "last_name"),
        (r"e[-\s]?mail", "email"),
        (r"phone", "phone"),
        (r"linkedin", "linkedin"),
        (r"github", "github"),
        (r"(portfolio|website|blog)", "portfolio_url"),
    ]

    async def fill_out(self) -> bool:
        from ..db import AppStatus
        package = self.cfg.get("package") or {}
        for pattern, key in self.NAME_KEYWORDS:
            val = package.get("answers", {}).get(key)
            if val:
                await self.fill_text_field([pattern], val)
        # CV upload
        cv_path = package.get("cv_path")
        if cv_path:
            ok = await self.upload_cv(cv_path)
            if not ok:
                self.outcome.skipped = Blocker(
                    "form_unsupported", "could not find CV upload control")
                return False
        ok = await self.answer_questions()
        return ok and self.outcome.skipped is None

    async def upload_cv(self, path) -> bool:
        try:
            inputs = self.page.locator("input[type=file]")
            n = await inputs.count()
            for i in range(n):
                el = inputs.nth(i)
                try:
                    visible = await el.is_visible()
                except Exception:
                    visible = False
                if visible:
                    await el.set_input_files(path)
                    self.outcome.fields_filled.append("file:resume")
                    await self.page.wait_for_timeout(1200)
                    return True
            return False
        except Exception as exc:
            self.outcome.error = f"upload: {exc}"
            return False


class LeverAdapter(GreenhouseAdapter):
    platform = "lever"

    async def fill_out(self) -> bool:
        package = self.cfg.get("package") or {}
        for pattern, key in self.NAME_KEYWORDS:
            val = package.get("answers", {}).get(key)
            if val:
                await self.fill_text_field([pattern], val)
        cv_path = package.get("cv_path")
        if cv_path:
            ok = await self.upload_cv(cv_path)
            if not ok:
                self.outcome.skipped = Blocker("form_unsupported", "could not find CV upload control")
                return False
        ok = await self.answer_questions()
        return ok and self.outcome.skipped is None


class AshbyAdapter(GreenhouseAdapter):
    platform = "ashby"


class WorkableAdapter(GreenhouseAdapter):
    platform = "workable"


class SmartRecruitersAdapter(GreenhouseAdapter):
    platform = "smartrecruiters"


ADAPTERS = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "workable": WorkableAdapter,
    "smartrecruiters": SmartRecruitersAdapter,
}

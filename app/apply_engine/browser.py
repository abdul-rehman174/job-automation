"""Playwright browser lifecycle for the application engine.

One shared browser; isolated per-application browser contexts (cookies /
storage do not leak between applications). If Playwright is missing, the
engine degrades honestly: applications are skipped with a recorded
technical limitation — the queue keeps running.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .adapters import ADAPTERS, BaseAdapter, FillOutcome


class BrowserManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._pw = None
        self._browser = None
        self.available = True      # dependency installed / last launch succeeded
        self.started = False       # an actual browser process is running
        self.unavailable_reason = ""

    async def start(self):
        if self.started:
            return True
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            self.available = False
            self.unavailable_reason = f"Playwright not installed: {exc}"
            return False
        try:
            self._pw = await async_playwright().start()
            headless = bool(self.cfg.get("browser_headless", True))
            self._browser = await self._pw.chromium.launch(headless=headless)
            self.started = True
            self.available = True
            return True
        except Exception as exc:
            self.available = False
            self.unavailable_reason = f"browser launch failed: {type(exc).__name__}: {exc}"
            return False

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self.started = False

    def get_adapter_for(self, platform: str, page, app_cfg: dict) -> BaseAdapter | None:
        cls = ADAPTERS.get(platform, None)
        return cls(page, app_cfg) if cls else None

    async def new_context(self):
        """Isolated context per application: cookies/session never leak between apps."""
        if not self._browser:
            raise RuntimeError("browser is not running")
        return await self._browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="en-US",
        )

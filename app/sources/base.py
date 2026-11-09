"""Discovery source framework.

Every source produces RawJob rows that flow through the same pipeline:
Source -> Extraction -> Verification -> Normalization -> Dedup -> Eligibility -> DB.

A failing source MUST NOT affect other sources: discover() wraps everything in
its own error isolation and returns partial results plus recorded errors.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from dataclasses import dataclass, field

import httpx

from ..pipeline import RawJob

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 job-automation-agent/1.0 (contact: candidate application agent)")


@dataclass
class SourceResult:
    jobs: list[RawJob] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_processed: int = 0
    rate_limited: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


class Source:
    id: str = "base"
    name: str = "Base source"
    requires_credentials: bool = False
    credential_note: str = ""

    async def _run(self, client: httpx.AsyncClient, cfg: dict) -> SourceResult:
        raise NotImplementedError

    async def discover(self, client: httpx.AsyncClient, cfg: dict) -> SourceResult:
        started = time.monotonic()
        try:
            result = await self._run(client, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # full error isolation per source
            result = SourceResult(errors=[f"{type(exc).__name__}: {exc} :: {traceback.format_exc(limit=2)}"])
        result.duration_seconds = round(time.monotonic() - started, 2)
        for j in result.jobs:
            if not j.source_id:
                j.source_id = self.id
        return result


SourceResult.duration_seconds = 0.0


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8"},
        follow_redirects=True,
    )


def rate_limit_wait(seconds: float = 0.6) -> None:
    time.sleep(seconds) if False else None  # placeholder; async sleep used inside sources

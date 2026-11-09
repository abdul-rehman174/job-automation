"""Probe candidate ATS company handles against live public APIs; keep only valid ones."""
import asyncio
import httpx
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

CANDIDATES = {
    "greenhouse": ["databricks", "stripe", "doordash", "toasttab", "chime", "flexport",
                   "benchling", "elastic", "twilio", "checkr", "1stdibs", "zapier",
                   "coinbase", "airbnb"],
    "lever": ["vercel", "spotify", "netflix-tech-blog", "plaid", "robinhood", "sentry",
              "coinbase", "brex", "stripe", "twitch", "wealthfront", "pinterest"],
    "ashby": ["ashby", "clerk", "convex", "resend", "linear", "sardine", "cron", "ramp",
              "fireflies", "greenhouse"],
    "workable": ["workable", "spellbound", "engineering", "postgresql"],
    "smartrecruiters": ["visa", "turing", "wakefit", "arvato", "groupm", "colgatepalmo?江河"],
}

async def probe(client, kind, handle):
    url = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{handle}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{handle}",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{handle}?details=false",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{handle}/postings?limit=1",
    }[kind]
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as e:
        return handle, type(e).__name__
    if r.status_code == 200:
        try:
            j = r.json()
            n = len(j.get("jobs", [])) if isinstance(j, dict) and isinstance(j.get("jobs"), list) else \
                len(j) if isinstance(j, list) else "?"
            return handle, f"OK({n})"
        except Exception:
            return handle, "OK(nonjson)"
    return handle, f"HTTP {r.status_code}"

async def main():
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        for kind, handles in CANDIDATES.items():
            results = await asyncio.gather(*[probe(c, kind, h) for h in handles])
            print(kind.upper())
            for h, status in results:
                print("  ", h, status)

asyncio.run(main())

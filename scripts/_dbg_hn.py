"""Debug HN who-is-hiring source + probe more Workable/SmartRecruiters handles."""
import asyncio
import sys, pathlib, json, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from app.sources.marketplace_sources import HNWhoIsHiringSource

async def main():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        # 1. HN story diagnostics
        r = await c.get("https://hn.algolia.com/api/v1/search_by_date",
                        params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 3})
        for hit in r.json().get("hits", []):
            print("story:", hit["objectID"], hit.get("title"), hit.get("createdAt"))
        if r.json()["hits"]:
            sid = r.json()["hits"][0]["objectID"]
            item = (await c.get(f"https://hn.algolia.com/api/v1/items/{sid}")).json()
            kids = item.get("children", [])
            print("children:", len(kids))
            txt = [k.get("text", "") for k in kids]
            haslinks = sum(1 for t in txt if "http" in (t or ""))
            haspython = sum(1 for t in txt if re.search(r"python|django|fastapi", t or "", re.I))
            print("with links:", haslinks, "with python-ish:", haspython)
            src = HNWhoIsHiringSource()
            res = await src.discover(c, {})
            print("HN source jobs:", len(res.jobs), "errors:", res.errors[:2])
            if res.jobs:
                print("example:", res.jobs[0].url, "|", res.jobs[0].title[:80])
        # 2. workable handles
        for h in ["trellis", "relocate", "northwind", "kissflow", "recurly", "assembly",
                  "geckoboard", "superside", "instabase", "hired", "broadway", "dixa"]:
            r2 = await c.get(f"https://apply.workable.com/api/v1/widget/accounts/{h}?details=false")
            n = "?"
            if r2.status_code == 200:
                n = r2.json().get("num_jobs", "?")
            print("workable", h, r2.status_code, n)
        # 3. smartrecruiters counts
        for h in ["visa", "turing", "wakefit", "arvato", "groupm"]:
            r3 = await c.get(f"https://api.smartrecruiters.com/v1/companies/{h}/postings?limit=1")
            if r3.status_code == 200:
                print("sr", h, "totalFound:", r3.json().get("totalFound"))
            else:
                print("sr", h, r3.status_code)

asyncio.run(main())

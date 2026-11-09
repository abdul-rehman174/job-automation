"""Ad-hoc smoke test for the discovery phase."""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import app.discovery as d

r = asyncio.run(d.run_discovery_phase(
    {"db_path": "/tmp/smoke.db", "xray_provider": "none",
     "discover_max_jobs_per_run": 200,
     "company_handles": {"greenhouse": ["mux"], "lever": ["nozzle"]}}))
print(json.dumps(r["ingest"]))
for s in r["sources"]:
    errs = s["errors"][:1] or [""]
    print(" ", s["source_id"], "jobs=", s["results"], "errs=", s["error_count"], errs[0][:120])
import sqlite3
c = sqlite3.connect("/tmp/smoke.db")
print("by source:", c.execute("select source_id, count(*) from jobs group by source_id").fetchall())
print("eligible:", c.execute("select count(*) from jobs where eligibility_status='eligible'").fetchone())
print("eligible+verified:",
      c.execute("select count(*) from jobs where eligibility_status='eligible' and verified=1").fetchone())

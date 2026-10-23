"""Dowod na wylot: klik -> runner -> proces -> zdarzenie -> widok runu (BRO-99)."""
import asyncio
import hashlib
import os
import sys

sys.path.insert(0, "src")
os.environ.setdefault("COCKPIT_DATABASE_URL", "sqlite+aiosqlite:////tmp/demo.db")

import httpx
import uvicorn

from cockpit.db.models import Runner as RunnerRow
from cockpit.db.models import Tenant
from cockpit.db.session import SessionLocal, create_all
from runner.client import Runner

BASE = "http://127.0.0.1:8799"
TOKEN = "demo-token"


async def seed():
    await create_all()
    async with SessionLocal() as s:
        t = Tenant(name="Browarek")
        s.add(t)
        await s.flush()
        s.add(RunnerRow(tenant_id=t.id, name="macbook",
                        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest()))
        await s.commit()


async def main():
    if os.path.exists("/tmp/demo.db"):
        os.remove("/tmp/demo.db")
    await seed()

    config = uvicorn.Config("cockpit.api.app:app", host="127.0.0.1", port=8799,
                            log_level="error")
    server = uvicorn.Server(config)
    serve = asyncio.create_task(server.serve())
    await asyncio.sleep(1.2)

    listen = asyncio.create_task(Runner(BASE, TOKEN, "macbook").listen())
    await asyncio.sleep(1.0)

    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/runs",
                         json={"workflow": "szkielet",
                               "command": ["python3", "-c",
                                           "print('krok wykonany na maszynie runnera')"]})
        run_id = r.json()["run_id"]
        print("run utworzony:", run_id)
        await asyncio.sleep(2.0)
        szczegoly = (await c.get(f"{BASE}/api/runs/{run_id}")).json()

    print("status:", szczegoly["status"])
    for e in szczegoly["events"]:
        print(f"  [{e['seq']}] {e['kind']}: {e['text']}")

    listen.cancel()
    server.should_exit = True
    await serve
    ok = szczegoly["status"] == "to_accept" and any(
        "krok wykonany" in e["text"] for e in szczegoly["events"])
    print("\nWYNIK:", "szkielet dziala na wylot" if ok else "BLAD")
    return 0 if ok else 1


raise SystemExit(asyncio.run(main()))

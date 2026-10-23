"""Runner: laczy sie WYCHODZACO, wiec uzytkownik nie otwiera zadnego portu.

SSE w dol po polecenia, POST w gore ze zdarzeniami (BRO-86).
Procesy w osobnej grupie, zeby zabicie nie zostawialo sierot (BRO-104).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time

import httpx


class Runner:
    def __init__(self, base_url: str, token: str, name: str = "lokalny") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.name = name
        self.runner_id: str | None = None
        self._headers = {"Authorization": f"Bearer {token}"}

    async def register(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            f"{self.base_url}/api/runners/register",
            json={"name": self.name, "version": "0.1.0", "backends": ["claude"]},
            headers=self._headers,
        )
        r.raise_for_status()
        self.runner_id = r.json()["runner_id"]

    async def send_event(self, client: httpx.AsyncClient, **kw) -> None:
        await client.post(f"{self.base_url}/api/events", json=kw, headers=self._headers)

    async def run_command(
        self, client: httpx.AsyncClient, run_id: str, step_id: str, command: list[str]
    ) -> None:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,          # wlasna grupa procesow
        )
        try:
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            raise

        ms = int((time.monotonic() - started) * 1000)
        for line in out.decode(errors="replace").splitlines():
            await self.send_event(client, run_id=run_id, step_id=step_id,
                                  kind="output", text=line)
        ok = proc.returncode == 0
        await self.send_event(
            client, run_id=run_id, step_id=step_id,
            kind="step_finished" if ok else "step_failed",
            text=f"kod wyjscia {proc.returncode}",
            duration_ms=ms,
            cost_usd=None,                   # subskrypcja nie podaje ceny
            cost_source="unavailable",
            status="to_accept" if ok else "failed",
        )

    async def listen(self) -> None:
        async with httpx.AsyncClient(timeout=None) as client:
            await self.register(client)
            async with client.stream(
                "GET", f"{self.base_url}/api/runners/stream", headers=self._headers
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    cmd = json.loads(line.removeprefix("data:").strip())
                    if cmd.get("type") == "run_step":
                        await self.run_command(
                            client, cmd["run_id"], cmd["step_id"], cmd["command"]
                        )

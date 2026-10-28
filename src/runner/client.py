"""Runner: laczy sie WYCHODZACO, wiec uzytkownik nie otwiera zadnego portu.

SSE w dol po polecenia, POST w gore ze zdarzeniami (BRO-86).
Procesy w osobnej grupie, zeby zabicie nie zostawialo sierot (BRO-104).
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import shutil
import signal
import sys
import time

import httpx


class StrumienZamkniety(Exception):
    """Kokpit zamknal strumien polecen. Runner nie moze wtedy wrocic cicho
    (kod 0 sugerowalby udana prace); ponowne laczenie to BRO-75."""


class TokenOdrzucony(Exception):
    """Kokpit odpowiedzial 401. Ponawianie nic nie da - trzeba nowego tokenu."""


def wykryj_backendy() -> list[dict]:
    """Backendy dostepne na tej maszynie. Wartosc klucza nie opuszcza funkcji:
    uzywamy jej tylko jako bool."""
    if not shutil.which("claude"):
        return []
    auth = "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "subscription"
    return [{"nazwa": "claude", "auth": auth}]


def wersja_runnera() -> str:
    try:
        return importlib.metadata.version("harness-cockpit")
    except importlib.metadata.PackageNotFoundError:
        return "0+nieznana"     # pakiet niezainstalowany - nie udajemy realnej wersji


class Runner:
    def __init__(
        self,
        base_url: str,
        token: str,
        name: str = "lokalny",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.name = name
        self._transport = transport
        self.runner_id: str | None = None
        self._headers = {"Authorization": f"Bearer {token}"}

    async def register(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            f"{self.base_url}/api/runners/register",
            json={
                "name": self.name,
                "version": wersja_runnera(),
                "backends": wykryj_backendy(),
            },
            headers=self._headers,
        )
        if r.status_code == 401:
            raise TokenOdrzucony()
        r.raise_for_status()
        self.runner_id = r.json()["runner_id"]

    async def send_event(self, client: httpx.AsyncClient, **kw) -> bool:
        """True tylko dla 2xx. Odrzucenie trafia na stderr: rodzaj, run_id, kod.
        Nigdy token, naglowki ani tresc odpowiedzi."""
        r = await client.post(f"{self.base_url}/api/events", json=kw, headers=self._headers)
        if not r.is_success:
            print(
                f"zdarzenie odrzucone: kind={kw.get('kind')} "
                f"run_id={kw.get('run_id')} http={r.status_code}",
                file=sys.stderr,
            )
        return r.is_success

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
        wyslane_output = 0
        for line in out.decode(errors="replace").splitlines():
            wyslane_output += 1              # proba, takze nieudana
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
            wyslane_output=wyslane_output,
        )

    async def listen(self) -> None:
        async with httpx.AsyncClient(timeout=None, transport=self._transport) as client:
            await self.register(client)
            async with client.stream(
                "GET", f"{self.base_url}/api/runners/stream", headers=self._headers
            ) as resp:
                if resp.status_code == 401:
                    raise TokenOdrzucony()
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    cmd = json.loads(line.removeprefix("data:").strip())
                    if cmd.get("type") == "polaczono":
                        print("strumien otwarty", file=sys.stderr)
                    elif cmd.get("type") == "run_step":
                        await self.run_command(
                            client, cmd["run_id"], cmd["step_id"], cmd["command"]
                        )
        raise StrumienZamkniety("kokpit zamknal strumien polecen")

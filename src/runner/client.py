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
import uuid
from collections.abc import Awaitable, Callable

import httpx

from runner.bufor import Bufor, odczekanie
from runner.heartbeat import Heartbeat

CZAS_NA_ZAKONCZENIE_S = 5     # tyle krok ma na SIGTERM, potem SIGKILL


class StrumienZamkniety(Exception):
    """Kokpit zamknal strumien polecen. Sygnal wewnetrzny: `listen()` laczy sie wtedy
    ponownie, proces runnera sie nie konczy."""


class KokpitNiezgodny(Exception):
    """Odpowiedz kokpitu lamie kontrakt (np. brak czestotliwosci heartbeatu).
    Ponawianie nic nie da - kokpit i runner maja rozne wersje."""


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
        bufor: Bufor | None = None,
        odczekaj: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.name = name
        self._transport = transport
        self.runner_id: str | None = None
        self._headers = {"Authorization": f"Bearer {token}"}
        self.heartbeat_co_s: int | None = None
        self._procesy: set[asyncio.subprocess.Process] = set()
        self._heartbeat: Heartbeat | None = None
        self._zatrzymany = asyncio.Event()
        self._zadanie: asyncio.Task | None = None
        self._bufor = bufor if bufor is not None else Bufor()
        self._odczekaj = odczekaj if odczekaj is not None else self._czekaj
        self._proba_laczenia = 0

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
        tresc = r.json()
        co_s = tresc.get("heartbeat_co_s")
        # bool jest podklasa int; czestotliwosc dyktuje kokpit, runner nie ma wlasnej
        if isinstance(co_s, bool) or not isinstance(co_s, int) or co_s <= 0:
            raise KokpitNiezgodny("odpowiedz rejestracji bez poprawnego heartbeat_co_s")
        self.runner_id = tresc["runner_id"]
        self.heartbeat_co_s = co_s

    async def _czekaj(self, sekundy: float) -> None:
        """Domyslne odczekanie: przerywane przez `stop()`."""
        try:
            await asyncio.wait_for(self._zatrzymany.wait(), sekundy)
        except TimeoutError:
            pass                            # uplynal czas - o to chodzilo

    async def dosylaj(
        self, client: httpx.AsyncClient, wyniki: dict[str, bool] | None = None,
    ) -> None:
        """Wysyla zdarzenia z bufora po kolei (FIFO) i usuwa potwierdzone. Awaria kokpitu
        wstrzymuje kolejke do jego powrotu. `wyniki` dostaje event_id -> True dla zdarzen przyjetych (2xx)."""
        for wpis in self._bufor.niepotwierdzone():
            proba = 0
            while True:
                try:
                    r = await client.post(
                        f"{self.base_url}/api/events", json=wpis, headers=self._headers
                    )
                except httpx.TransportError as e:
                    przyczyna = type(e).__name__     # tresc wyjatku moze niesc adres albo naglowki
                else:
                    kod = r.status_code
                    if r.is_success:
                        self._bufor.potwierdz(wpis["run_id"], wpis["event_id"])
                        if wyniki is not None:
                            wyniki[wpis["event_id"]] = True
                        break
                    if kod == 401:
                        raise TokenOdrzucony()      # zdarzenie zostaje w buforze
                    if kod < 500:
                        # ponowienie nic nie zmieni (4xx, takze nieobslugiwane 3xx)
                        print(
                            f"zdarzenie utracone: kind={wpis.get('kind')} "
                            f"run_id={wpis['run_id']} http={kod}",
                            file=sys.stderr,
                        )
                        self._bufor.potwierdz(wpis["run_id"], wpis["event_id"])
                        break
                    przyczyna = f"http={kod}"
                sekundy = odczekanie(proba)
                print(
                    f"kokpit niedostepny ({przyczyna}) - ponawiam za {sekundy} s",
                    file=sys.stderr,
                )
                await self._odczekaj(sekundy)
                if self._zatrzymany.is_set():
                    return                  # wpisy zostaja w buforze do nastepnego startu
                proba += 1

    async def send_event(self, client: httpx.AsyncClient, **kw) -> bool:
        """True tylko dla 2xx tego zdarzenia. Nigdy token, naglowki ani tresc odpowiedzi
        w logu."""
        # event_id nadaje runner; ponowienie z tym samym nie tworzy duplikatu (HAR-39)
        kw.setdefault("event_id", str(uuid.uuid4()))
        self._bufor.dopisz(kw)
        wyniki: dict[str, bool] = {}
        await self.dosylaj(client, wyniki)
        return wyniki.get(kw["event_id"], False)

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
        self._procesy.add(proc)
        try:
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            self._zabij_grupe(proc, signal.SIGTERM)
            raise
        finally:
            self._procesy.discard(proc)

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

    @staticmethod
    def _zabij_grupe(proc: asyncio.subprocess.Process, sygnal: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sygnal)     # start_new_session: pgid == pid
        except ProcessLookupError:
            pass                            # proces juz nie zyje

    async def _zakoncz_krok(self, proc: asyncio.subprocess.Process) -> None:
        self._zabij_grupe(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), CZAS_NA_ZAKONCZENIE_S)
        except TimeoutError:
            self._zabij_grupe(proc, signal.SIGKILL)
            await proc.wait()               # odbieramy proces: bez zombie

    async def stop(self) -> None:
        """Idempotentny: kolejne wywolanie wraca od razu."""
        if self._zatrzymany.is_set():
            return
        self._zatrzymany.set()
        if self._heartbeat is not None:
            await self._heartbeat.stop()
        await asyncio.gather(*(self._zakoncz_krok(p) for p in list(self._procesy)))
        zadanie = self._zadanie
        if zadanie is not None and zadanie is not asyncio.current_task() and not zadanie.done():
            zadanie.cancel()

    async def listen(self) -> None:
        self._zadanie = asyncio.current_task()
        try:
            while not self._zatrzymany.is_set():
                try:
                    await self._nasluchuj()
                except StrumienZamkniety:
                    przyczyna = "kokpit zamknal strumien"
                except httpx.TransportError as e:
                    przyczyna = type(e).__name__
                except httpx.HTTPStatusError as e:
                    if e.response.status_code < 500:
                        raise
                    przyczyna = f"http={e.response.status_code}"
                if self._zatrzymany.is_set():
                    break
                sekundy = odczekanie(self._proba_laczenia)
                print(
                    f"strumien polecen przerwany ({przyczyna}) - lacze ponownie za {sekundy} s",
                    file=sys.stderr,
                )
                await self._odczekaj(sekundy)
                self._proba_laczenia += 1
        except asyncio.CancelledError:
            if not self._zatrzymany.is_set():
                raise                       # cudze anulowanie nie jest nasze do polkniecia
            asyncio.current_task().uncancel()
        return None

    async def _nasluchuj(self) -> None:
        async with httpx.AsyncClient(timeout=None, transport=self._transport) as client:
            await self.register(client)
            await self.dosylaj(client)      # zalegle z poprzedniego polaczenia albo startu
            if self._zatrzymany.is_set():
                return
            self._heartbeat = Heartbeat(
                client, f"{self.base_url}/api/runners/heartbeat",
                self._headers, self.heartbeat_co_s,
            )
            try:
                self._heartbeat.start()
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
                            self._proba_laczenia = 0
                        elif cmd.get("type") == "run_step":
                            await self.run_command(
                                client, cmd["run_id"], cmd["step_id"], cmd["command"]
                            )
            finally:
                await self._heartbeat.stop()
        raise StrumienZamkniety("kokpit zamknal strumien polecen")

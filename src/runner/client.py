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

from runner.bufor import BrakMiejsca, Bufor, min_wolne_mb, odczekanie, wolne_mb
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
        self._sprzatanie: dict[asyncio.subprocess.Process, asyncio.Task] = {}
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

    async def _wyslij_bezposrednio(self, client: httpx.AsyncClient, **kw) -> bool:
        """Jedna proba POST z pominieciem bufora: przy braku miejsca bufor sam nie zadziala,
        a ponowienie opoznialoby przerwanie runu. Nigdy token ani tresc odpowiedzi w logu."""
        kw.setdefault("event_id", str(uuid.uuid4()))
        try:
            r = await client.post(f"{self.base_url}/api/events", json=kw, headers=self._headers)
        except httpx.TransportError as e:
            przyczyna = type(e).__name__
        else:
            if r.is_success:
                return True
            if r.status_code == 401:
                raise TokenOdrzucony()
            przyczyna = f"http={r.status_code}"
        print(
            f"zdarzenie {kw.get('kind')} niewyslane: run_id={kw.get('run_id')} ({przyczyna})",
            file=sys.stderr,
        )
        return False

    def _powod_nie_startu(self) -> str | None:
        """Tekst przerwania, gdy krok nie moze wystartowac z powodu dysku; inaczej None."""
        try:
            prog = min_wolne_mb()
        except ValueError as e:
            return f"zla konfiguracja: {e}"
        try:
            wolne = wolne_mb(self._bufor.katalog)
        except OSError as e:
            return f"nie mozna sprawdzic wolnego miejsca: {type(e).__name__}"
        if wolne < prog:
            return f"brak miejsca: {wolne} MB wolne, wymagane {prog} MB"
        return None

    async def run_command(
        self, client: httpx.AsyncClient, run_id: str, step_id: str, command: list[str]
    ) -> None:
        powod = self._powod_nie_startu()
        if powod is not None:
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted", text=powod)
            return
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,          # wlasna grupa procesow
        )
        self._procesy.add(proc)
        try:
            if self._zatrzymany.is_set():
                # stop() mogl juz skonczyc sprzatanie: bez tego krok ignorujacy SIGTERM zostalby sierota
                await self._zakoncz_krok(proc)
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            await self._zakoncz_krok(proc)
            raise
        finally:
            self._procesy.discard(proc)
            self._sprzatanie.pop(proc, None)

        ms = int((time.monotonic() - started) * 1000)
        wyslane_output = 0
        try:
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
        except BrakMiejsca:
            # bez miejsca zaden krok nie zapisze zdarzen; nie stop(): nasluch i heartbeat zyja
            await asyncio.gather(*(self._zakoncz_krok(p) for p in list(self._procesy)))
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted",
                text="brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)",
            )

    @staticmethod
    def _zabij_grupe(proc: asyncio.subprocess.Process, sygnal: signal.Signals) -> None:
        if proc.returncode is not None:
            return                          # odebrany: pid moze nalezec juz do obcej grupy
        try:
            os.killpg(proc.pid, sygnal)     # start_new_session: pgid == pid
        except ProcessLookupError:
            pass                            # proces juz nie zyje

    async def _zakoncz_krok(self, proc: asyncio.subprocess.Process) -> None:
        # jeden proces = jedno sprzatanie: rownolegle SIGKILL i wait() tego samego procesu
        # konkuruja o pid. Shield: anulowanie czekajacego nie przerywa zabicia i odebrania.
        zadanie = self._sprzatanie.get(proc)
        if zadanie is None:
            zadanie = asyncio.ensure_future(self._sprzataj(proc))
            self._sprzatanie[proc] = zadanie
        await asyncio.shield(zadanie)

    async def _sprzataj(self, proc: asyncio.subprocess.Process) -> None:
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
        # flaga przerywa odczekanie przed ponownym polaczeniem i blokuje kolejna probe
        self._zatrzymany.set()
        # nasluch odcinamy pierwszy, zeby w trakcie stop() nie przyjmowac nowych krokow;
        # kroki, ktore mimo to wystartuja, sprzatajaja petla ponizej i galaz _zatrzymany
        zadanie = self._zadanie
        if zadanie is not None and zadanie is not asyncio.current_task() and not zadanie.done():
            zadanie.cancel()
        if self._heartbeat is not None:
            await self._heartbeat.stop()
        # petla, bo krok moze zostac przyjety w trakcie sprzatania innego; _procesy oprozniaja
        # run_command w finally, wiec krotka przerwa oddaje im sterowanie zamiast kreci petle
        while self._procesy:
            await asyncio.gather(*(self._zakoncz_krok(p) for p in list(self._procesy)))
            await asyncio.sleep(0.01)

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

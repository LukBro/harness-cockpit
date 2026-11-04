"""Runner: laczy sie WYCHODZACO, wiec uzytkownik nie otwiera zadnego portu.

SSE w dol po polecenia, POST w gore ze zdarzeniami (BRO-86).
Procesy w osobnej grupie, zeby zabicie nie zostawialo sierot (BRO-104).
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

import httpx

from runner import operacje, silnik
from runner.bufor import BrakMiejsca, Bufor, min_wolne_mb, odczekanie, wolne_mb
from runner.heartbeat import Heartbeat

CZAS_NA_ZAKONCZENIE_S = 5     # tyle krok ma na SIGTERM, potem SIGKILL

# Limity czasu polaczen HTTP (HAR-198). Bez nich martwe polaczenie (zerwana siec bez RST)
# wisi w nieskonczonosc. Przekroczenie to httpx.TransportError, obslugiwany istniejacym
# ponawianiem bufora (POST-y) i backoffem w `listen` (strumien).
CZAS_POLACZENIA_S = 10          # nawiazanie polaczenia (POST-y i strumien)
CZAS_ODCZYTU_POST_S = 30        # odpowiedz na POST zdarzenia/zameldowania/odpowiedzi
CZAS_ODCZYTU_STRUMIENIA_S = 45  # 3x ping SSE kokpitu (co 15 s): dluzsza cisza = martwe

# Operacje kroku runu (HAR-159). Nowa operacja = nowy wpis. Lista jest LOKALNA: kokpit
# podaje tylko nazwe operacji, a argv powstaje tu, nigdy z danych kokpitu.
OPERACJE_RUNU: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "szkielet": ("echo", "szkielet dziala"),
})
POWOD_SPOZA_LISTY = "operacja spoza listy dozwolonych"


def argv_operacji(cmd: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Argv z lokalnej listy dla polecenia `run_step`; None, gdy polecenia nie wolno wykonac.

    Odrzuca polecenie z kluczem `command` (stary protokol), z nazwa niebedaca napisem
    i z nazwa spoza `OPERACJE_RUNU`."""
    if "command" in cmd:
        return None
    nazwa = cmd.get("operacja")
    if not isinstance(nazwa, str):
        return None
    return OPERACJE_RUNU.get(nazwa)


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


_ZMIENNA_PROB = "COCKPIT_MAX_PROB_ZDARZENIA"
_DOMYSLNE_MAKS_PROB = 10
_CYFRY_ASCII = re.compile(r"[0-9]+")


def maks_prob_bledu_serwera() -> int:
    """Ile odpowiedzi 500 na to samo zdarzenie, zanim runner uzna je za utracone.
    Czytane przy kazdym wywolaniu; pusta albo niepoprawna wartosc to blad, nie domyslna."""
    surowa = os.environ.get(_ZMIENNA_PROB)
    if surowa is None:
        return _DOMYSLNE_MAKS_PROB
    if not _CYFRY_ASCII.fullmatch(surowa) or int(surowa) < 1:
        raise ValueError(f"{_ZMIENNA_PROB} musi byc dodatnia liczba calkowita (cyfry ASCII)")
    return int(surowa)


_limit_prob_z_srodowiska = maks_prob_bledu_serwera   # parametr Runner.__init__ przesłania nazwę


class Runner:
    def __init__(
        self,
        base_url: str,
        token: str,
        name: str = "lokalny",
        transport: httpx.AsyncBaseTransport | None = None,
        bufor: Bufor | None = None,
        odczekaj: Callable[[float], Awaitable[None]] | None = None,
        maks_prob_bledu_serwera: int | None = None,
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
        self._maks_prob_bledu_serwera = (
            maks_prob_bledu_serwera if maks_prob_bledu_serwera is not None
            else _limit_prob_z_srodowiska()
        )
        self._proba_laczenia = 0
        self._zapytania: set[asyncio.Task] = set()
        # runy workflow (HAR-80): osobne zadania, zyja niezaleznie od strumienia polecen
        self._runy: set[asyncio.Task] = set()
        self._zapis_zdarzen = asyncio.Lock()    # jeden zapis do bufora naraz: kolejnosc zdarzen

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
            bledy_serwera = 0
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
                    if kod == 500:
                        # WHY: 500 to blad wewnetrzny - tak konczy sie DataError trwale
                        # odrzucanego zdarzenia. 502/503/504 i TransportError to
                        # niedostepnosc kokpitu: ich nie limitujemy, zeby restart kokpitu
                        # nie gubil zdarzen.
                        bledy_serwera += 1
                        if bledy_serwera >= self._maks_prob_bledu_serwera:
                            print(
                                f"zdarzenie utracone: kind={wpis.get('kind')} "
                                f"run_id={wpis['run_id']} http=500 "
                                f"po {bledy_serwera} probach",
                                file=sys.stderr,
                            )
                            self._bufor.potwierdz(wpis["run_id"], wpis["event_id"])
                            break
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

    async def _zglos_koniec_runu(
        self, client: httpx.AsyncClient, run_id: str, step_id: str,
        kind_kroku: str, krok_przyjety: bool,
    ) -> None:
        """Koniec runu zglasza runner osobno, tylko po przyjetym zdarzeniu kroku (HAR-78).
        BrakMiejsca z bufora przechodzi do wolajacego."""
        if not krok_przyjety:
            # WHY: kokpit wylicza status runu ze zdarzen krokow; bez przyjetego dowodu
            # run_finished dalby "do odbioru" mimo nieudanego kroku (evidence_required).
            print(
                f"koniec runu niewyslany: run_id={run_id} - "
                f"zdarzenie kroku {kind_kroku} nie zostalo przyjete",
                file=sys.stderr,
            )
            return
        await self.send_event(
            client, run_id=run_id, step_id=step_id, kind="run_finished", text="",
            cost_usd=None, cost_source="unavailable",
        )

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
            kind_kroku = "step_finished" if ok else "step_failed"
            krok_przyjety = await self.send_event(
                client, run_id=run_id, step_id=step_id,
                kind=kind_kroku,
                text=f"kod wyjscia {proc.returncode}",
                duration_ms=ms,
                cost_usd=None,                   # subskrypcja nie podaje ceny
                cost_source="unavailable",
                wyslane_output=wyslane_output,
            )
            await self._zglos_koniec_runu(client, run_id, step_id, kind_kroku, krok_przyjety)
        except BrakMiejsca:
            # bez miejsca zaden krok nie zapisze zdarzen; nie stop(): nasluch i heartbeat zyja
            await asyncio.gather(*(self._zakoncz_krok(p) for p in list(self._procesy)))
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted",
                text="brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)",
            )

    async def _odpowiedz_na_zapytanie(self, client: httpx.AsyncClient, cmd: dict) -> None:
        """Wykonuje operacje i odsyla wynik POST-em. Nigdy token, naglowki ani tresc odpowiedzi w logu."""
        wynik = await operacje.wykonaj(cmd)
        url = f"{self.base_url}/api/runners/odpowiedzi"
        try:
            try:
                r = await client.post(url, json=wynik, headers=self._headers)
            except (TypeError, ValueError) as e:
                # httpx serializuje wynik przed wyslaniem; bez str(e) i reprezentacji
                # wyniku: moga niesc sekret (HAR-148)
                blad = {"req_id": cmd.get("req_id"), "ok": False,
                        "blad": f"wynik operacji nie daje sie zapisac jako JSON ({type(e).__name__})",
                        "kod": "blad_operacji"}
                r = await client.post(url, json=blad, headers=self._headers)
        except httpx.TransportError as e:
            przyczyna = type(e).__name__
        else:
            if r.status_code < 400:
                return
            przyczyna = f"http={r.status_code}"
        print(f"odpowiedz na zapytanie {cmd.get('req_id')} niewyslana ({przyczyna})",
              file=sys.stderr)

    async def _anuluj_zapytania(self) -> None:
        zadania = list(self._zapytania)
        for zadanie in zadania:
            zadanie.cancel()
        if zadania:
            await asyncio.wait(zadania)     # odczekanie, zeby po powrocie nie zostaly zadania

    async def _anuluj_runy(self) -> None:
        zadania = list(self._runy)
        for zadanie in zadania:
            zadanie.cancel()
        if zadania:
            await asyncio.wait(zadania)

    def _klient_http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(CZAS_ODCZYTU_POST_S, connect=CZAS_POLACZENIA_S),
            transport=self._transport,
        )

    async def _run_workflow(self, cmd: dict) -> None:
        """Run workflow w wlasnym kliencie HTTP: zerwanie strumienia polecen nie przerywa
        runu (BRO-75), a zdarzenia silnika ida do kokpitu przez `send_event`."""
        async with self._klient_http() as client:

            async def emit(zdarzenie: dict) -> None:
                async with self._zapis_zdarzen:
                    await self.send_event(client, **zdarzenie)

            try:
                await silnik.wykonaj_run(cmd, emit)
            except BrakMiejsca:
                # silnik anulowal kroki w `finally`; kokpit dostaje przerwanie poza buforem
                await self._wyslij_bezposrednio(
                    client, run_id=cmd.get("run_id"), step_id="run", kind="step_aborted",
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
        await self._anuluj_zapytania()
        await self._anuluj_runy()
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

    async def _krok_z_polecenia(self, client: httpx.AsyncClient, cmd: dict) -> None:
        """Rozwiazuje nazwe operacji na argv z lokalnej listy; polecenia spoza listy
        konczy `step_failed` bez startu procesu, a po jego przyjeciu `run_finished`."""
        argv = argv_operacji(cmd)
        if argv is not None:
            await self.run_command(client, cmd["run_id"], cmd["step_id"], list(argv))
            return
        run_id, step_id = cmd.get("run_id"), cmd.get("step_id")
        if not isinstance(run_id, str) or not isinstance(step_id, str):
            print("polecenie bez run_id/step_id odrzucone: operacja spoza listy", file=sys.stderr)
            return
        try:
            odmowa_przyjeta = await self.send_event(
                client, run_id=run_id, step_id=step_id,
                kind="step_failed", text=POWOD_SPOZA_LISTY)
            # bez run_finished run z odmowa zostalby w kokpicie "w toku" na zawsze (HAR-78)
            await self._zglos_koniec_runu(
                client, run_id, step_id, "step_failed", odmowa_przyjeta)
        except BrakMiejsca:
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted",
                text="brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)",
            )

    async def _nasluchuj(self) -> None:
        async with self._klient_http() as client:
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
                    "GET", f"{self.base_url}/api/runners/stream", headers=self._headers,
                    timeout=httpx.Timeout(
                        CZAS_ODCZYTU_STRUMIENIA_S, connect=CZAS_POLACZENIA_S),
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
                            await self._krok_z_polecenia(client, cmd)
                        elif cmd.get("type") == "run_workflow":
                            # osobne zadanie: run trwa dlugo, a strumien czytamy dalej
                            run = asyncio.create_task(self._run_workflow(cmd))
                            self._runy.add(run)
                            run.add_done_callback(self._runy.discard)
                        elif "req_id" in cmd:
                            # osobne zadanie: wolna operacja nie wstrzymuje czytania strumienia
                            zadanie = asyncio.create_task(
                                self._odpowiedz_na_zapytanie(client, cmd))
                            self._zapytania.add(zadanie)
                            zadanie.add_done_callback(self._zapytania.discard)
            finally:
                await self._anuluj_zapytania()
                await self._heartbeat.stop()
        raise StrumienZamkniety("kokpit zamknal strumien polecen")

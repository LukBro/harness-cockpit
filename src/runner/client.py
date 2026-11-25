"""Runner: laczy sie WYCHODZACO, wiec uzytkownik nie otwiera zadnego portu.

SSE w dol po polecenia, POST w gore ze zdarzeniami (BRO-86).
Procesy w osobnej grupie, zeby zabicie nie zostawialo sierot (BRO-104).
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import math
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
from pydantic import ValidationError

from cockpit.api.schematy import EventIn
from cockpit.services.errors import BladDziedziny
from runner import operacje, silnik
from runner.bufor import BladBufora, BrakMiejsca, Bufor, min_wolne_mb, odczekanie, wolne_mb
from runner.heartbeat import Heartbeat

LIMIT_ANULOWANYCH = 1000      # pamietane runy anulowane (ignorowanie spoznionego resume)
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
POWOD_BRAK_STEP_ID = "polecenie run_step bez poprawnego step_id"
POWOD_BUFOR_NIEDOSTEPNY = "bufor zdarzen runnera niedostepny: zapis albo odczyt nie powiodl sie"
TEKST_ANULOWANIA = "anulowany przez człowieka"


# Klucz wiersza kroku w kokpicie bez runu: (step_id, agent_id, attempt); brak agent_id to "",
# brak attempt to 1 (jak `zastosuj_zdarzenie_kroku`, ktory czyta takze jawne None jako 1), wiec
# w krotce nigdy None i sorted nie rzuca TypeError.
TozsamoscKroku = tuple[str, str, int]


def _tozsamosc_kroku(zdarzenie: Mapping[str, Any]) -> TozsamoscKroku | None:
    """Tozsamosc kroku ze zdarzenia silnika; `None` w agent_id i attempt to brak pola (domyslne
    "" i 1), pole o zlym typie to `None` zamiast tozsamosci (krok nie jest sledzony)."""
    step_id = zdarzenie.get("step_id")
    agent_id = zdarzenie.get("agent_id")
    attempt = zdarzenie.get("attempt")
    agent_id = "" if agent_id is None else agent_id
    attempt = 1 if attempt is None else attempt
    if (not isinstance(step_id, str) or not isinstance(agent_id, str)
            or not isinstance(attempt, int) or isinstance(attempt, bool)):
        return None
    return (step_id, agent_id, attempt)


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


def _liczba_skonczona(wartosc: object) -> float | None:
    """Skonczona liczba (int albo float, nie bool) jako float; inaczej None. Int ponad zakres
    float (OverflowError) to tez None."""
    if isinstance(wartosc, bool) or not isinstance(wartosc, (int, float)):
        return None
    try:
        liczba = float(wartosc)
    except OverflowError:
        return None
    return liczba if math.isfinite(liczba) else None


def _odmowa_zapytania(cmd: Mapping[str, Any], teraz: float) -> tuple[str, str] | None:
    """(kod, komunikat), gdy zapytania kokpitu nie wolno wykonac: brak albo zly `nadano_o` /
    `wazne_s`, albo wiek ponad limit waznosci. Komunikaty stale: bez tresci zapytania."""
    nadano_o = _liczba_skonczona(cmd.get("nadano_o"))
    if nadano_o is None:
        return ("zly_czas_nadania", "zapytanie bez poprawnego czasu nadania (nadano_o)")
    wazne_s = _liczba_skonczona(cmd.get("wazne_s"))
    if wazne_s is None or wazne_s <= 0:
        return ("zly_limit_waznosci", "zapytanie bez poprawnego limitu waznosci (wazne_s)")
    if teraz - nadano_o > wazne_s:
        return ("przeterminowane", "zapytanie przeterminowane: kokpit nie czeka juz na odpowiedz")
    return None


class StrumienZamkniety(Exception):
    """Kokpit zamknal strumien polecen. Sygnal wewnetrzny: `listen()` laczy sie wtedy
    ponownie, proces runnera sie nie konczy."""


class NiewaznePoleZdarzenia(ValueError):
    """Zdarzenie lamie limity `EventIn` (HAR-242): kokpit odpowiedzialby 4xx i zdarzenie
    przepadloby po wyslaniu. Nie trafia do bufora ani na siec, nie jest ponawiane."""


def _sprawdz_zdarzenie(kw: Mapping[str, Any]) -> None:
    """Ta sama walidacja co w kokpicie, przed buforem i POST; blad to `NiewaznePoleZdarzenia`."""
    try:
        EventIn.model_validate(kw)
    except ValidationError as e:
        opisy = "; ".join(
            f"{'.'.join(str(c) for c in b['loc'])}: {b['msg']}" for b in e.errors())
        raise NiewaznePoleZdarzenia(f"zdarzenie {kw.get('kind')}: {opisy}") from e


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
        self._token_odrzucony: TokenOdrzucony | None = None   # 401 w zadaniu runu -> listen()
        self._zapytania: set[asyncio.Task] = set()
        # zadania runu (HAR-80, HAR-126): run_workflow, resume i run_step, po `run_id`; zyja
        # niezaleznie od strumienia polecen, a `cancel_run` anuluje zadanie z tej mapy
        self._runy: dict[str, asyncio.Task] = {}
        # runy anulowane przez czlowieka (najnowsze na koncu): spozniony `resume` jest ignorowany
        self._anulowane: dict[str, None] = {}
        # zadania anulowane przez `cancel_run` (nie przez stop()): tylko one zglaszaja `step_aborted`
        self._anulowane_przez_czlowieka: set[asyncio.Task] = set()
        # kroki trwajace per run (step_started bez konca; run_step od polecenia do konca kroku):
        # to je zglasza `step_aborted` po anulowaniu, zeby StepRun nie zostal `running`
        self._kroki_w_toku: dict[str, set[TozsamoscKroku]] = {}
        # zamykanie checkpointu anulowanego runu (zadania w tle; stop() je odczekuje)
        self._zamykania: set[asyncio.Task] = set()
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
            # asyncio.timeout zamiast wait_for: na Pythonie 3.11 wait_for polyka
            # CancelledError, gdy wewnetrzne czekanie konczy sie rownoczesnie z anulowaniem
            # (naprawione w 3.12) - wolajacy nie dowiedzialby sie o anulowaniu (HAR-224)
            async with asyncio.timeout(sekundy):
                await self._zatrzymany.wait()
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
        _sprawdz_zdarzenie(kw)
        self._bufor.dopisz(kw)
        wyniki: dict[str, bool] = {}
        await self.dosylaj(client, wyniki)
        return wyniki.get(kw["event_id"], False)

    async def _wyslij_bezposrednio(self, client: httpx.AsyncClient, **kw) -> bool:
        """Jedna proba POST z pominieciem bufora: przy braku miejsca bufor sam nie zadziala,
        a ponowienie opoznialoby przerwanie runu. Nigdy token ani tresc odpowiedzi w logu."""
        kw.setdefault("event_id", str(uuid.uuid4()))
        _sprawdz_zdarzenie(kw)
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

    async def _zglos_niewazne_pole(
        self, client: httpx.AsyncClient, run_id: object, blad: NiewaznePoleZdarzenia,
    ) -> None:
        """Zdarzenie zlamalo limit `EventIn` (HAR-242): kokpit dostaje poprawny `step_failed`
        przez bufor (ponawianie) i `run_finished` dopiero po jego przyjeciu (HAR-78), z krokiem
        `run` i tekstem obcietym do limitu tekstu. BrakMiejsca z bufora konczy sie `step_aborted`
        poza buforem (jak u wolajacych). Gdy limit lamie sam `run_id`, nie ma do kogo zglosic -
        zostaje komunikat na stderr."""
        try:
            try:
                przyjety = await self.send_event(
                    client, run_id=run_id, step_id="run", kind="step_failed",
                    text=f"zdarzenie przekracza limity pol: {blad}"[:2000],
                    cost_usd=None, cost_source="unavailable")
                await self._zglos_koniec_runu(client, run_id, "run", "step_failed", przyjety)
            except BrakMiejsca:
                await self._wyslij_bezposrednio(
                    client, run_id=run_id, step_id="run", kind="step_aborted",
                    text="brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)",
                )
        except NiewaznePoleZdarzenia:
            print(f"koniec runu niewyslany: run_id={str(run_id)[:36]!r} - {blad}",
                  file=sys.stderr)

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
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,          # wlasna grupa procesow
            )
        except OSError as e:
            # brak programu, brak praw: bez tego run zostalby w `running` bez konca kroku
            await self._zglos_porazke_kroku(
                client, run_id, step_id,
                f"nie mozna uruchomic operacji: {type(e).__name__}")
            return
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
        except BladBufora:
            # bufor zepsuty z innej przyczyny niz brak miejsca (prawa, limit, uszkodzenie)
            await asyncio.gather(*(self._zakoncz_krok(p) for p in list(self._procesy)))
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted",
                text=POWOD_BUFOR_NIEDOSTEPNY,
            )

    async def _zglos_porazke_kroku(
        self, client: httpx.AsyncClient, run_id: str, step_id: str, tekst: str,
    ) -> None:
        """`step_failed` przez bufor i po jego przyjeciu `run_finished` (HAR-78). Gdy bufor
        nie dziala (takze brak miejsca), kokpit dostaje `step_aborted` poza buforem."""
        try:
            przyjety = await self.send_event(
                client, run_id=run_id, step_id=step_id, kind="step_failed", text=tekst,
                cost_usd=None, cost_source="unavailable")
            await self._zglos_koniec_runu(client, run_id, step_id, "step_failed", przyjety)
        except BladBufora:
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted",
                text=POWOD_BUFOR_NIEDOSTEPNY,
            )

    async def _odpowiedz_na_zapytanie(self, client: httpx.AsyncClient, cmd: dict) -> None:
        """Wykonuje operacje i odsyla wynik POST-em. Nigdy token, naglowki ani tresc odpowiedzi w logu.
        Zapytanie bez poprawnego czasu nadania i limitu waznosci albo przeterminowane (kokpit juz
        na nie nie czeka) jest odrzucane BEZ wykonania (HAR-201)."""
        odmowa = _odmowa_zapytania(cmd, time.time())
        if odmowa is not None:
            kod, blad = odmowa
            wynik = {"req_id": cmd.get("req_id"), "ok": False, "blad": blad, "kod": kod}
        else:
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

    async def _pilnuj_zadania(
        self, run_id: str, cmd: dict, korutyna: Awaitable[None],
    ) -> None:
        """Wspolny punkt dla `run_step`, `run_workflow` i `resume`: wyjatek zadania nie moze
        zostawic runu w `running` bez konca kroku i runu (evidence_required).
        `CancelledError` przechodzi bez konca kroku i runu (stan `aborted` zapisal kokpit); po
        `cancel_run` (nie po stop()) trwajace kroki dostaja `step_aborted`, a anulowanie leci dalej.
        `TokenOdrzucony` idzie do petli nasluchu (kod 2 z komunikatem o nowym tokenie)."""
        try:
            await korutyna
        except asyncio.CancelledError:
            if asyncio.current_task() in self._anulowane_przez_czlowieka:
                await self._zglos_anulowanie_krokow(run_id)
            raise
        except TokenOdrzucony as e:
            self._token_odrzucony = e
            print(f"zadanie runu {run_id[:36]!r} zakonczone odmowa tokenu: {type(e).__name__}",
                  file=sys.stderr)
            zadanie = self._zadanie
            if zadanie is not None and not zadanie.done():
                zadanie.cancel()            # listen() podnosi zapamietany wyjatek
        except Exception as e:  # noqa: BLE001
            # WHY szeroki: granica zadania w tle - kazdy inny wyjatek (kod silnika, biblioteka)
            # konczy sie tak samo: zdarzeniem konca i logiem; wlasciwego typu nie znamy z gory.
            step_id = cmd.get("step_id")
            nazwa_typu = type(e).__name__       # bez tresci i tracebacku: moga niesc sekret
            try:
                async with self._klient_http() as client:
                    await self._zglos_porazke_kroku(
                        client, run_id, step_id if isinstance(step_id, str) and step_id else "run",
                        f"zadanie runu zakonczone bledem: {nazwa_typu}")
            except NiewaznePoleZdarzenia:
                pass                            # run_id/step_id lamie limity: zostaje log
            except TokenOdrzucony as t:
                self._token_odrzucony = t
                if self._zadanie is not None and not self._zadanie.done():
                    self._zadanie.cancel()
            print(f"zadanie runu {run_id[:36]!r} zakonczone bledem: {nazwa_typu}",
                  file=sys.stderr)

    async def _zglos_anulowanie_krokow(self, run_id: str) -> None:
        """`step_aborted` dla kazdego trwajacego kroku anulowanego runu, w nowym kliencie HTTP
        (klient zadania jest juz zamkniety) i poza buforem: jedna proba, bez ponawiania, zeby
        anulowanie sie nie zawieszalo. Bez `step_finished`, `step_failed` i `run_finished`."""
        kroki = sorted(self._kroki_w_toku.get(run_id, ()))
        if not kroki:
            return
        try:
            async with self._klient_http() as client:
                for step_id, agent_id, attempt in kroki:
                    try:
                        # ta sama tozsamosc co `step_started` (wiersz kroku: run, krok, agent,
                        # proba): inaczej kokpit zalozylby drugi wiersz, a pierwszy zostalby `running`
                        await self._wyslij_bezposrednio(
                            client, run_id=run_id, step_id=step_id, agent_id=agent_id,
                            attempt=attempt, kind="step_aborted", text=TEKST_ANULOWANIA)
                    except NiewaznePoleZdarzenia:
                        print(f"step_aborted niewyslany: run {run_id[:36]!r} - pola ponad limit",
                              file=sys.stderr)
        except TokenOdrzucony as t:
            self._token_odrzucony = t
            if self._zadanie is not None and not self._zadanie.done():
                self._zadanie.cancel()

    def _sledz_start_kroku(self, run_id: object, zdarzenie: Mapping[str, Any]) -> None:
        """Przed wyslaniem `step_started`: krok trwa od tej chwili (anulowanie w trakcie wysylki
        tez ma go zglosic). Run bez poprawnego `run_id` nie jest sledzony."""
        tozsamosc = _tozsamosc_kroku(zdarzenie)
        if (zdarzenie.get("kind") == "step_started" and isinstance(run_id, str)
                and tozsamosc is not None):
            self._kroki_w_toku.setdefault(run_id, set()).add(tozsamosc)

    def _sledz_koniec_kroku(self, run_id: object, zdarzenie: Mapping[str, Any]) -> None:
        """Po PRZYJECIU konca kroku przez kokpit (wolajacy wola to tylko, gdy `send_event` dal
        True): krok (ta sama tozsamosc co przy starcie) nie jest w toku. Odrzucony koniec kroku
        zostawia go w `_kroki_w_toku`, wiec anulowanie zglosi `step_aborted`."""
        tozsamosc = _tozsamosc_kroku(zdarzenie)
        if (zdarzenie.get("kind") in ("step_finished", "step_failed") and isinstance(run_id, str)
                and tozsamosc is not None):
            self._kroki_w_toku.get(run_id, set()).discard(tozsamosc)

    def _zarejestruj_run(self, run_id: str, cmd: dict, korutyna: Awaitable[None]) -> None:
        """Zadanie runu w mapie `run_id -> zadanie`; po jego koncu wpis znika, o ile nie
        zajelo go nowsze zadanie tego runu (np. `resume` po `run_workflow`)."""
        zadanie = asyncio.create_task(self._pilnuj_zadania(run_id, cmd, korutyna))
        self._runy[run_id] = zadanie

        def po_zakonczeniu(z: asyncio.Task) -> None:
            self._anulowane_przez_czlowieka.discard(z)
            if self._runy.get(run_id) is z:
                del self._runy[run_id]
                self._kroki_w_toku.pop(run_id, None)
            if not z.cancelled() and z.exception() is not None:
                # bez tego wyjatek zadania ginie bez sladu; nazwa typu, bez tresci (moze niesc sekret)
                print(f"zadanie runu {run_id[:36]!r} zakonczone bledem: "
                      f"{type(z.exception()).__name__}", file=sys.stderr)

        zadanie.add_done_callback(po_zakonczeniu)

    def _anuluj_run(self, cmd: dict) -> None:
        """`cancel_run`: anuluje zadanie runu (`CancelledError` -> `_zakoncz_krok` zabija grupe
        procesow), potem trwajace kroki dostaja `step_aborted` (`_pilnuj_zadania`). Runner nie
        zglasza konca kroku ani runu: stan `aborted` zapisal kokpit. Checkpoint runu zostaje
        zamkniety (bez `waiting_on`), takze dla runu bez zadania (`waiting`): inaczej `resume`
        po restarcie runnera wznowilby anulowany run. Run zakonczony albo nieznany to nie blad."""
        run_id = cmd.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            print(f"cancel_run odrzucone: niepoprawne polecenie {cmd!r}", file=sys.stderr)
            return
        # mutant "" jest rownowazny (slownik jest uporzadkowanym zbiorem, czytane sa tylko klucze)
        self._anulowane[run_id] = None  # pragma: no mutate
        while len(self._anulowane) > LIMIT_ANULOWANYCH:
            del self._anulowane[next(iter(self._anulowane))]
        zadanie = self._runy.get(run_id)
        if zadanie is None:
            print(f"cancel_run: run {run_id[:36]!r} nie ma zadania u runnera", file=sys.stderr)
        else:
            self._anulowane_przez_czlowieka.add(zadanie)
            zadanie.cancel()
        zamykanie = asyncio.create_task(self._zamknij_checkpoint(run_id, zadanie))
        self._zamykania.add(zamykanie)
        zamykanie.add_done_callback(self._zamykania.discard)

    async def _zamknij_checkpoint(self, run_id: str, zadanie: asyncio.Task | None) -> None:
        """Checkpoint anulowanego runu bez `waiting_on`, pod ta sama blokada co `wznow_run`.
        Zadanie runu dostaje czas na koniec; zapisy checkpointu silnika koncza sie przed wyjsciem
        anulowania z zadania i ida pod ta sama blokada runu (`silnik._zapis_do_konca`,
        `_zapisz`), wiec zamkniecie ich nie wyprzedzi. Blad dziedziny (zapis, odczyt, katalog runu) to log z typem bledu: anulowanie
        nie wywraca petli polecen. Brak checkpointu (run nieznany albo zakonczony) to nie blad."""
        if zadanie is not None:
            await asyncio.wait([zadanie])
        try:
            await silnik.zamknij_checkpoint(run_id)
        except BladDziedziny as e:
            print(f"cancel_run: checkpoint runu {run_id[:36]!r} niezamkniety: {type(e).__name__}",
                  file=sys.stderr)

    async def _krok_runu(self, cmd: dict) -> None:
        """Krok `run_step` w wlasnym kliencie HTTP, jak `_run_workflow`: zamkniecie strumienia
        polecen nie zrywa klienta kroku. Krok trwa od polecenia do powrotu z `_krok_z_polecenia`;
        przy `CancelledError` zostaje w `_kroki_w_toku` dla `_zglos_anulowanie_krokow`."""
        run_id, step_id = cmd.get("run_id"), cmd.get("step_id")
        if not (isinstance(run_id, str) and run_id and isinstance(step_id, str) and step_id):
            # zle polecenie: `_krok_z_polecenia` zglosi je sam, nie ma kroku do anulowania
            async with self._klient_http() as client:
                await self._krok_z_polecenia(client, cmd)
            return
        # `run_command` wysyla zdarzenia kroku bez agent_id i attempt: kokpit czyta je jako ("", 1)
        tozsamosc = (step_id, "", 1)
        self._kroki_w_toku.setdefault(run_id, set()).add(tozsamosc)
        async with self._klient_http() as client:
            await self._krok_z_polecenia(client, cmd)
        self._kroki_w_toku.get(run_id, set()).discard(tozsamosc)

    async def _anuluj_runy(self) -> None:
        zadania = list(self._runy.values())
        for zadanie in zadania:
            zadanie.cancel()
        if zadania:
            await asyncio.wait(zadania)
        zamykania = list(self._zamykania)       # krotkie: zamkniecie checkpointu dobiega konca
        if zamykania:
            await asyncio.wait(zamykania)

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
                run_id = cmd.get("run_id")
                self._sledz_start_kroku(run_id, zdarzenie)
                async with self._zapis_zdarzen:
                    przyjete = await self.send_event(client, **zdarzenie)
                if przyjete:
                    self._sledz_koniec_kroku(run_id, zdarzenie)

            try:
                await silnik.wykonaj_run(cmd, emit)
            except NiewaznePoleZdarzenia as e:
                await self._zglos_niewazne_pole(client, cmd.get("run_id"), e)
            except BrakMiejsca:
                # silnik anulowal kroki w `finally`; kokpit dostaje przerwanie poza buforem
                await self._wyslij_bezposrednio(
                    client, run_id=cmd.get("run_id"), step_id="run", kind="step_aborted",
                    text="brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)",
                )
            except BladBufora as e:
                # bufor zepsuty z innej przyczyny niz brak miejsca (BrakMiejsca jest wyzej);
                # `step_failed` i `run_finished` poszlyby przez ten sam bufor, wiec przerwanie
                # idzie poza nim
                print(f"run {str(cmd.get('run_id'))[:36]!r}: bufor zdarzen niedostepny: "
                      f"{type(e).__name__}", file=sys.stderr)
                await self._wyslij_bezposrednio(
                    client, run_id=cmd.get("run_id"), step_id="run", kind="step_aborted",
                    text=POWOD_BUFOR_NIEDOSTEPNY,
                )

    @staticmethod
    def _decyzje_z_polecenia(cmd: dict) -> tuple[str, str, str, str | None] | None:
        """(run_id, step_id, decyzja, powod) z polecenia `resume`; zle polecenie to `None`."""
        run_id, step_id = cmd.get("run_id"), cmd.get("step_id")
        decyzja, powod = cmd.get("decyzja"), cmd.get("powod")
        if (
            not isinstance(run_id, str) or not run_id
            or not isinstance(step_id, str) or not step_id
            or decyzja not in ("approve", "reject")
            or not (powod is None or isinstance(powod, str))
        ):
            return None
        return run_id, step_id, decyzja, powod

    async def _resume(self, cmd: dict) -> None:
        """Wznowienie runu po decyzji (HAR-173) w wlasnym kliencie HTTP, jak `_run_workflow`."""
        # polecenie jest już zwalidowane w `_nasluchuj` (przed rejestracją zadania)
        run_id, step_id, decyzja, powod = self._decyzje_z_polecenia(cmd)  # type: ignore[misc]
        async with self._klient_http() as client:

            async def emit(zdarzenie: dict) -> None:
                self._sledz_start_kroku(run_id, zdarzenie)
                async with self._zapis_zdarzen:
                    przyjete = await self.send_event(client, **zdarzenie)
                if przyjete:
                    self._sledz_koniec_kroku(run_id, zdarzenie)

            try:
                await silnik.wznow_run(run_id, step_id, decyzja, emit, powod=powod)
            except NiewaznePoleZdarzenia as e:
                await self._zglos_niewazne_pole(client, run_id, e)
            except BrakMiejsca:
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
                    # mutant `return` jest rownowazny: po petli `while` funkcja robi tylko
                    # `return None`, a w `try` nie ma `finally`
                    break  # pragma: no mutate
                sekundy = odczekanie(self._proba_laczenia)
                print(
                    f"strumien polecen przerwany ({przyczyna}) - lacze ponownie za {sekundy} s",
                    file=sys.stderr,
                )
                await self._odczekaj(sekundy)
                self._proba_laczenia += 1
        except asyncio.CancelledError:
            if self._token_odrzucony is not None and not self._zatrzymany.is_set():
                # zadanie runu dostalo 401: sprzatamy kroki i konczymy jak przy 401 w petli
                token_odrzucony = self._token_odrzucony
                asyncio.current_task().uncancel()
                await self.stop()
                raise token_odrzucony
            if not self._zatrzymany.is_set():
                raise                       # cudze anulowanie nie jest nasze do polkniecia
            asyncio.current_task().uncancel()
        return None

    async def _krok_z_polecenia(self, client: httpx.AsyncClient, cmd: dict) -> None:
        """Rozwiazuje nazwe operacji na argv z lokalnej listy; polecenia spoza listy
        konczy `step_failed` bez startu procesu, a po jego przyjeciu `run_finished`."""
        run_id, step_id = cmd.get("run_id"), cmd.get("step_id")
        if not isinstance(run_id, str) or not run_id:
            print("polecenie run_step bez poprawnego run_id odrzucone", file=sys.stderr)
            return                      # nie ma runu, ktory dalo by zakonczyc
        if not isinstance(step_id, str) or not step_id:
            # HAR-126: run istnieje, wiec konczymy go zdarzeniami; wartosci step_id nie ma w tekscie
            try:
                await self._zglos_porazke_kroku(client, run_id, "run", POWOD_BRAK_STEP_ID)
            except NiewaznePoleZdarzenia as e:
                # mutant `client=None` jest rownowazny: tu NiewaznePoleZdarzenia z
                # `_zglos_porazke_kroku` moze byc tylko run_id ponad 36 znakow (reszta pol
                # zdarzenia jest stala i poprawna), a wtedy `send_event` w `_zglos_niewazne_pole`
                # rzuca w `_sprawdz_zdarzenie` przed uzyciem `client`, wiec klient nie jest dotykany
                await self._zglos_niewazne_pole(client, run_id, e)  # pragma: no mutate
            return
        argv = argv_operacji(cmd)
        if argv is not None:
            try:
                await self.run_command(client, run_id, step_id, list(argv))
            except NiewaznePoleZdarzenia as e:
                await self._zglos_niewazne_pole(client, run_id, e)
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
        except BladBufora:
            await self._wyslij_bezposrednio(
                client, run_id=run_id, step_id=step_id, kind="step_aborted",
                text=POWOD_BUFOR_NIEDOSTEPNY,
            )
        except NiewaznePoleZdarzenia as e:
            await self._zglos_niewazne_pole(client, run_id, e)

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
                            run_id = cmd.get("run_id")
                            if isinstance(run_id, str) and run_id:
                                # osobne zadanie: krok trwa, a polecenie cancel_run ma dojsc;
                                # brak poprawnego step_id rozstrzyga `_krok_z_polecenia`
                                self._zarejestruj_run(run_id, cmd, self._krok_runu(cmd))
                            else:
                                print("polecenie run_step bez poprawnego run_id odrzucone",
                                      file=sys.stderr)
                        elif cmd.get("type") == "run_workflow":
                            # osobne zadanie: run trwa dlugo, a strumien czytamy dalej
                            run_id = cmd.get("run_id")
                            if isinstance(run_id, str) and run_id:
                                self._zarejestruj_run(run_id, cmd, self._run_workflow(cmd))
                            else:
                                print("polecenie run_workflow bez poprawnego run_id odrzucone",
                                      file=sys.stderr)
                        elif cmd.get("type") == "cancel_run":
                            self._anuluj_run(cmd)
                        elif cmd.get("type") == "resume":
                            run_id = cmd.get("run_id")
                            if isinstance(run_id, str) and run_id in self._anulowane:
                                print(f"resume ignorowane: run {run_id[:36]!r} anulowany",
                                      file=sys.stderr)
                            elif self._decyzje_z_polecenia(cmd) is None:
                                print(f"resume odrzucone: niepoprawne polecenie {cmd!r}",
                                      file=sys.stderr)
                            else:
                                self._zarejestruj_run(run_id, cmd, self._resume(cmd))
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

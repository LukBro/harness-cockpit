"""Hooki kroku zapisywane do pliku przypisanego do kroku, bez portu (HAR-62, rozstrzygniecie D1).

Hook Claude Code (typ `command`, nie `http`) dopisuje surowe zdarzenie ze stdin jako linie
NDJSON do `<katalog_stanu>/hooks/<run_id>/<step_id>.ndjson`. Runner czyta ten plik
przyrostowo, normalizuje kernelem i dokleja `run_id` oraz `step_id` ZE SCIEZKI (z konstruktora
czytnika) - nigdy z tresci zdarzenia i nigdy z czasu. Zaden port ani gniazdo nie jest
otwierane. Sciezka pliku trafia do procesu agenta przez zmienna `COCKPIT_HOOK_FILE`
(ustawia ja silnik), nigdy przez argv ani w tresci polecenia hooka.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cockpit.kernel.events import normalize
from cockpit.services.errors import BladDziedziny
from cockpit.services.invariants import Rola
from runner import bufor
from runner.bufor import BladBufora
from runner.maskowanie import maskuj_komende

ZMIENNA_PLIKU = "COCKPIT_HOOK_FILE"
_RUN_ID = bufor._RUN_ID  # ten sam obiekt wzorca co w buforze, nie kopia

# HAR-175: straz roli to hook PreToolUse uruchamiany jako `python -I <plik>` (nie `-m`), wiec
# dziala bez instalacji pakietu; sciezka liczona z pliku tego modulu, tak samo pod mutmutem.
_STRAZ_ROLI = Path(__file__).resolve().parent / "straz_roli.py"
_MATCHER_STRAZY = "Bash|Write|Edit|MultiEdit|NotebookEdit"
_PREFIKS_POWTORZENIA = "agent powtarza zablokowaną komendę: "

# Zdarzenia zgodne z deploy/hooks.example.json; runner nie czyta tego pliku w runtime
# (wheel go nie zawiera), spojnosc pilnuje test.
ZDARZENIA: dict[str, dict[str, str]] = {
    "SessionStart": {},
    "SubagentStart": {},
    "SubagentStop": {},
    "Stop": {},
    "SessionEnd": {},
    "PreToolUse": {"matcher": "Bash", "if": "Bash(*phase_timer*)"},
    "PostToolUse": {"matcher": "Bash", "if": "Bash(*phase_timer*)"},
    "PostToolUseFailure": {"matcher": "Bash", "if": "Bash(*phase_timer*)"},
}

# WHY: jeden os.write na pliku z O_APPEND - rownolegle hooki nie przeplataja linii.
# Bez O_CREAT: plik tworzy runner (`plik_hookow`); brak pliku = zdarzenie tracone, kod 0.
# `except BaseException` + `|| true`: hook NIGDY nie moze zepsuc agenta kodem != 0.
_KOD_HOOKA = f"""\
try:
 import os,sys
 d=sys.stdin.buffer.read().replace(b"\\r",b" ").replace(b"\\n",b" ").strip()
 if d:
  f=os.open(os.environ["{ZMIENNA_PLIKU}"],os.O_WRONLY|os.O_APPEND)
  try:os.write(f,d+b"\\n")
  finally:os.close(f)
except BaseException:pass
"""


class BladPlikuHookow(BladDziedziny):
    """Zly identyfikator kroku/runu, sciezka wzgledna albo plik hookow, ktorego nie da sie przygotowac."""


def _sprawdz_id(nazwa: str, wartosc: object) -> str:
    # WHY: run_id i step_id trafiaja do sciezki; bez fullmatch "../x" wyszloby poza hooks/
    if not isinstance(wartosc, str) or not _RUN_ID.fullmatch(wartosc):
        raise BladPlikuHookow(f"niedozwolony {nazwa} hookow")
    return wartosc


def plik_hookow(katalog_stanu: Path | None, run_id: str, step_id: str) -> Path:
    """Przygotowuje (idempotentnie) plik hookow kroku i zwraca jego sciezke."""
    run = _sprawdz_id("run_id", run_id)
    krok = _sprawdz_id("step_id", step_id)
    baza = katalog_stanu if katalog_stanu is not None else bufor.katalog_stanu()
    katalog_hookow = baza / "hooks"
    katalog_runu = katalog_hookow / run
    plik = katalog_runu / f"{krok}.ndjson"
    try:
        for k in (katalog_hookow, katalog_runu):
            k.mkdir(mode=0o700, parents=True, exist_ok=True)
            k.chmod(0o700)  # umask i wczesniej istniejacy katalog nie moga poszerzyc praw
        fd = os.open(plik, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError as exc:
        raise BladPlikuHookow(f"plik hookow kroku: {type(exc).__name__}") from exc
    return plik


def plik_ustawien(
    katalog_stanu: Path, run_id: str, step_id: str, ustawienia: Mapping[str, Any]
) -> Path:
    """Zapisuje ustawienia kroku (`claude --settings`) obok pliku hookow; zwraca sciezke pliku.

    Plik lezy w katalogu stanu runnera, poza repozytorium (run_dir_outside_repo), z prawami 0600.
    """
    run = _sprawdz_id("run_id", run_id)
    krok = _sprawdz_id("step_id", step_id)
    katalog_hookow = katalog_stanu / "hooks"
    katalog_runu = katalog_hookow / run
    plik = katalog_runu / f"{krok}.settings.json"
    tresc = json.dumps(ustawienia).encode()
    try:
        for k in (katalog_hookow, katalog_runu):
            k.mkdir(mode=0o700, parents=True, exist_ok=True)
            k.chmod(0o700)
        fd = os.open(plik, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)  # umask i wczesniej istniejacy plik nie moga poszerzyc praw
            os.write(fd, tresc)
        finally:
            os.close(fd)
    except OSError as exc:
        raise BladPlikuHookow(f"plik ustawien kroku: {type(exc).__name__}") from exc
    return plik


# HAR-219: straz, ktora nie wystartowala (zepsuty import, brak interpretera), konczy sie kodem != 0
# i bez JSON-a deny; Claude Code przepuszcza wtedy narzedzie, wiec powloka zamienia kazdy
# taki kod na 2 (blokada). stdout strazy (JSON deny) przechodzi bez zmian, stdin jest dziedziczony.
_GALAZ_AWARII_STRAZY = (
    '; kod=$?; if [ "$kod" -ne 0 ]; then echo "straż roli: awaria straży (kod $kod)" >&2; '
    "exit 2; fi"
)


def _polecenie_strazy(rola: Rola, worktree: Path, wykonywalny: str = sys.executable) -> str:
    """Linia `sh` uruchamiajaca straz roli z galezia awarii (kod != 0 -> kod 2)."""
    straz = (
        f"{shlex.quote(wykonywalny)} -I {shlex.quote(str(_STRAZ_ROLI))} "
        f"--rola {shlex.quote(rola.value)} --worktree {shlex.quote(str(worktree))}"
    )
    return straz + _GALAZ_AWARII_STRAZY


def ustawienia_hookow(
    plik: Path, *, rola: Rola | None = None, worktree: Path | None = None
) -> dict[str, Any]:
    """Ustawienia hookow dla agenta; polecenie bierze sciezke ze zmiennej, nie z tekstu.

    Z `rola` dokladamy do PreToolUse strazy roli (HAR-175); `worktree` jest wtedy wymagany
    i bezwzgledny. Bez `rola` wynik jest taki jak przed HAR-175, a `worktree` jest ignorowany.
    """
    # WHY: wzgledna sciezka rozwinelaby sie wzgledem cwd agenta, czyli w repozytorium projektu
    if not plik.is_absolute():
        raise BladPlikuHookow("sciezka pliku hookow musi byc bezwzgledna")
    if rola is not None and (worktree is None or not worktree.is_absolute()):
        raise BladPlikuHookow("worktree strazy roli musi byc sciezka bezwzgledna")
    polecenie = f"{shlex.quote(sys.executable)} -c {shlex.quote(_KOD_HOOKA)} || true"
    hooki: dict[str, list[dict[str, Any]]] = {}
    for zdarzenie, opcje in ZDARZENIA.items():
        hook: dict[str, Any] = {"type": "command", "command": polecenie}
        if "if" in opcje:
            hook["if"] = opcje["if"]
        grupa: dict[str, Any] = {"hooks": [hook]}
        if "matcher" in opcje:
            grupa["matcher"] = opcje["matcher"]
        hooki[zdarzenie] = [grupa]
    if rola is not None and worktree is not None:
        # WHY brak `|| true` i `if`: odmowa idzie kodem 0 i JSON-em na stdout, a straz jest
        # fail-closed sama - zdjecie kodu wyjscia albo warunek wylaczylyby ja po cichu.
        hooki["PreToolUse"].append({
            "matcher": _MATCHER_STRAZY,
            "hooks": [{"type": "command", "command": _polecenie_strazy(rola, worktree)}],
        })
    return {"hooks": hooki}


class CzytnikHookow:
    """Przyrostowy czytnik pliku hookow jednego kroku."""

    def __init__(
        self, plik: Path, run_id: str, step_id: str, *, interwal: float = 0.2
    ) -> None:
        self.plik = plik
        self.run_id = run_id
        self.step_id = step_id
        self.interwal = interwal
        self.kolejka: asyncio.Queue[dict] = asyncio.Queue()
        self._przesuniecie = 0
        self._zadanie: asyncio.Task | None = None
        self._zatrzymany = False

    def _czytaj(self) -> tuple[bytes, int] | None:
        """(bajty od przesuniecia, rozmiar pliku) albo None, gdy pliku nie da sie przeczytac."""
        try:
            with self.plik.open("rb") as f:
                rozmiar = os.fstat(f.fileno()).st_size
                f.seek(self._przesuniecie if rozmiar >= self._przesuniecie else 0)
                return f.read(), rozmiar
        except FileNotFoundError:
            return None
        except OSError as exc:
            # tylko nazwa klasy: tresc wyjatku moze niesc sciezke
            print(
                f"hooki: blad odczytu pliku kroku {self.step_id}: {type(exc).__name__}",
                file=sys.stderr,
            )
            return None

    def _nieparsowalna(self, bajt: int) -> None:
        # bez tresci linii: payload moze niesc dane
        print(
            f"hooki: pominieta nieparsowalna linia kroku {self.step_id} (bajt {bajt})",
            file=sys.stderr,
        )

    def odczytaj_nowe(self) -> list[dict]:
        """Nowe pelne linie od ostatniego odczytu; niepelny ogon czeka na dokonczenie."""
        odczyt = self._czytaj()
        if odczyt is None:
            return []
        dane, rozmiar = odczyt
        if rozmiar < self._przesuniecie:
            print(
                f"hooki: plik kroku {self.step_id} obciety, czytam od poczatku",
                file=sys.stderr,
            )
            self._przesuniecie = 0

        # WHY: dzielimy po b"\n", nie splitlines(): tresc zdarzenia moze zawierac np. U+2028
        # ogon = niepelna linia bez "\n"; czeka na dokonczenie, przesuniecie staje przed nim
        *segmenty, ogon = dane.split(b"\n")
        poczatek = self._przesuniecie
        self._przesuniecie += len(dane) - len(ogon)

        zdarzenia: list[dict] = []
        for segment in segmenty:
            bajt = poczatek
            poczatek += len(segment) + 1
            if not segment.strip():
                continue
            try:
                payload = json.loads(segment.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                self._nieparsowalna(bajt)
                continue
            if not isinstance(payload, dict):
                self._nieparsowalna(bajt)
                continue
            try:
                zdarzenie = normalize(payload).to_dict()
            except (AttributeError, TypeError, ValueError):
                self._nieparsowalna(bajt)
                continue
            # WHY: tozsamosc kroku ze sciezki pliku, nigdy z payloadu
            zdarzenie = zdarzenie | {"run_id": self.run_id, "step_id": self.step_id}
            self.kolejka.put_nowait(zdarzenie)
            zdarzenia.append(zdarzenie)
        return zdarzenia

    def start(self) -> None:
        if self._zadanie is None or self._zadanie.done():
            self._zatrzymany = False
            self._zadanie = asyncio.create_task(self._petla())

    async def _petla(self) -> None:
        # CancelledError nie jest lapane: zatrzymaj() przerywa takze oczekiwanie w sleep
        while True:
            self.odczytaj_nowe()
            await asyncio.sleep(self.interwal)

    async def zatrzymaj(self) -> None:
        """Idempotentne; bez ostatniego odczytu - nieprzeczytane zdarzenia tylko liczymy w logu."""
        if self._zatrzymany:
            return
        self._zatrzymany = True
        zadanie, self._zadanie = self._zadanie, None
        if zadanie is not None:
            zadanie.cancel()
            await asyncio.wait({zadanie})
        odczyt = self._czytaj()
        if odczyt is None:
            return
        nieprzeczytane = sum(1 for s in odczyt[0].split(b"\n") if s.strip())
        if nieprzeczytane > 0:
            print(
                f"hooki: nieprzeczytane zdarzenia kroku {self.step_id}: {nieprzeczytane}",
                file=sys.stderr,
            )


# WHY 100: agent_id i agent_type trafiaja do kolumn String(100); dluzsza wartosc dalaby
# DataError po stronie bazy (500), a zdarzenie przepadloby mimo dosylki.
_MAKS_ID_AGENTA = 100

# WHY 50: kolumna Event.kind to String(50); dluzszy kind to DataError (500) po stronie
# kokpitu, a bufor ponawialby go bez konca. Runner nie importuje cockpit.db - spojnosc
# pilnuje test.
_MAKS_KIND = 50


class BladZdarzeniaHooka(BladDziedziny):
    """Zdarzenie hooka, ktorego kokpit nigdy nie przyjmie (zly typ pola, za dlugi kind)."""

    def __init__(self, pole: str) -> None:
        # tylko nazwa pola: wartosc moze niesc dane z payloadu
        super().__init__(f"zdarzenie hooka: niedozwolone pole {pole}")
        self.pole = pole


def _napis_lub_brak(ev: dict, pole: str) -> str | None:
    wartosc = ev.get(pole)
    if wartosc is not None and not isinstance(wartosc, str):
        raise BladZdarzeniaHooka(pole)
    return wartosc


def _dane_odmowy(ev: dict) -> tuple[str, str, str]:
    """(narzedzie, komenda, rola) ze zdarzenia ToolDenied; zly typ pola to BladZdarzeniaHooka."""
    extra = ev.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    pola = []
    for nazwa, zrodlo in (("tool", ev), ("command", extra), ("role", extra)):
        wartosc = zrodlo.get(nazwa)
        if wartosc is not None and not isinstance(wartosc, str):
            raise BladZdarzeniaHooka(nazwa)
        pola.append(wartosc or "")
    # HAR-219: komenda niesie sekrety (np. `GH_TOKEN=...`); maskujemy ja u zrodla, wiec tekst
    # odmowy, tekst powtorzenia i klucz licznika powtorzen widza ten sam zamaskowany napis
    return pola[0], maskuj_komende(pola[1]), pola[2]


def _tekst_odmowy(ev: dict) -> str:
    narzedzie, komenda, rola = _dane_odmowy(ev)
    return f"{narzedzie}: {komenda} [role={rola}]"


def do_wysylki(ev: dict, run_id: str, step_id: str) -> dict:
    """Zdarzenie hooka -> pola wysylane w gore: same metadane, nigdy tresc.

    Wynik budowany jawnie z bialej listy kluczy (nie `{**ev}`): `extra`, `tool_input`,
    `tool_response` i reszta tresci nie maja jak wyjsc. Brak pomiaru to `None`, nie 0.
    Zdarzenie, ktore kokpit odrzuci zawsze, to `BladZdarzeniaHooka` - nie trafia do bufora.
    """
    event = ev.get("event")
    if not isinstance(event, str):
        raise BladZdarzeniaHooka("event")
    agent_id = _napis_lub_brak(ev, "agent_id")
    agent_type = _napis_lub_brak(ev, "agent_type")
    tool = _napis_lub_brak(ev, "tool")
    phase = _napis_lub_brak(ev, "phase")
    kind = "hook:" + event
    if len(kind) > _MAKS_KIND:
        raise BladZdarzeniaHooka("kind")
    ms = ev.get("ms")
    text = tool or phase or ""
    if event == "ToolDenied":
        text = _tekst_odmowy(ev)
    return {
        "kind": kind,
        "run_id": run_id,
        "step_id": step_id,
        # None -> "": EventIn.agent_id to str, null daje 422 i zdarzenie przepada
        "agent_id": (agent_id or "")[:_MAKS_ID_AGENTA],
        "agent_type": (agent_type or "")[:_MAKS_ID_AGENTA],
        # measured_or_null: bool jest podklasa int, ale to nie jest pomiar
        "duration_ms": ms if isinstance(ms, int) and not isinstance(ms, bool) else None,
        "text": text,
    }


async def przekazuj(czytnik: CzytnikHookow, wyslij) -> None:
    """Przekazuje zdarzenia z kolejki czytnika w gore, same metadane (HAR-66).

    Produkcyjnie `wyslij = functools.partial(runner.send_event, client)`; zdarzenia
    nie niosa event_id - nadaje go `send_event`. Konczy sie tylko przez anulowanie.
    """
    odmowy: dict[tuple[str, str], int] = {}  # licznik odmow (narzedzie, komenda) w tym kroku
    while True:
        ev = await czytnik.kolejka.get()
        try:
            wysylka = do_wysylki(ev, czytnik.run_id, czytnik.step_id)
            await wyslij(**wysylka)
            if ev.get("event") == "ToolDenied":
                narzedzie, komenda, _ = _dane_odmowy(ev)
                klucz = (narzedzie, komenda)
                odmowy[klucz] = odmowy.get(klucz, 0) + 1
                if odmowy[klucz] >= 2:
                    await wyslij(**(wysylka | {
                        "kind": "hook:ToolDeniedRepeat",
                        "text": f"{_PREFIKS_POWTORZENIA}{narzedzie}: {komenda}",
                    }))
        except BladZdarzeniaHooka as e:
            # tylko klasa i nazwa pola: wartosc z payloadu nie trafia do logu
            print(
                f"hooki: zdarzenie kroku {czytnik.step_id} odrzucone: "
                f"{type(e).__name__} (pole {e.pole})",
                file=sys.stderr,
            )
        except BladBufora as e:
            # tylko nazwa klasy: tresc wyjatku albo zdarzenia nie trafia do logu
            print(
                f"hooki: zdarzenie kroku {czytnik.step_id} nie weszlo do bufora: "
                f"{type(e).__name__}",
                file=sys.stderr,
            )
        finally:
            czytnik.kolejka.task_done()

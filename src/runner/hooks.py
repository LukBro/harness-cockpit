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
from pathlib import Path
from typing import Any

from cockpit.kernel.events import normalize
from cockpit.services.errors import BladDziedziny
from runner import bufor
from runner.bufor import BladBufora

ZMIENNA_PLIKU = "COCKPIT_HOOK_FILE"
_RUN_ID = bufor._RUN_ID  # ten sam obiekt wzorca co w buforze, nie kopia

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


def ustawienia_hookow(plik: Path) -> dict[str, Any]:
    """Ustawienia hookow dla agenta; polecenie bierze sciezke ze zmiennej, nie z tekstu."""
    # WHY: wzgledna sciezka rozwinelaby sie wzgledem cwd agenta, czyli w repozytorium projektu
    if not plik.is_absolute():
        raise BladPlikuHookow("sciezka pliku hookow musi byc bezwzgledna")
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


def do_wysylki(ev: dict, run_id: str, step_id: str) -> dict:
    """Zdarzenie hooka -> pola wysylane w gore: same metadane, nigdy tresc.

    Wynik budowany jawnie z bialej listy kluczy (nie `{**ev}`): `extra`, `tool_input`,
    `tool_response` i reszta tresci nie maja jak wyjsc. Brak pomiaru to `None`, nie 0.
    """
    ms = ev.get("ms")
    return {
        "kind": "hook:" + ev["event"],
        "run_id": run_id,
        "step_id": step_id,
        # None -> "": EventIn.agent_id to str, null daje 422 i zdarzenie przepada
        "agent_id": (ev.get("agent_id") or "")[:_MAKS_ID_AGENTA],
        "agent_type": (ev.get("agent_type") or "")[:_MAKS_ID_AGENTA],
        # measured_or_null: bool jest podklasa int, ale to nie jest pomiar
        "duration_ms": ms if isinstance(ms, int) and not isinstance(ms, bool) else None,
        "text": ev.get("tool") or ev.get("phase") or "",
    }


async def przekazuj(czytnik: CzytnikHookow, wyslij) -> None:
    """Przekazuje zdarzenia z kolejki czytnika w gore, same metadane (HAR-66).

    Produkcyjnie `wyslij = functools.partial(runner.send_event, client)`; zdarzenia
    nie niosa event_id - nadaje go `send_event`. Konczy sie tylko przez anulowanie.
    """
    while True:
        ev = await czytnik.kolejka.get()
        try:
            await wyslij(**do_wysylki(ev, czytnik.run_id, czytnik.step_id))
        except BladBufora as e:
            # tylko nazwa klasy: tresc wyjatku albo zdarzenia nie trafia do logu
            print(
                f"hooki: zdarzenie kroku {czytnik.step_id} nie weszlo do bufora: "
                f"{type(e).__name__}",
                file=sys.stderr,
            )
        finally:
            czytnik.kolejka.task_done()

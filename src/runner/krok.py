"""Wykonanie kroku u runnera: status z kodu wyjscia i artefaktow produces (HAR-42).

Proces agenta dostaje wlasna grupe procesow; gdy krok nie konczy sie sam
(anulowanie, wyjatek odbiorcy zdarzen), cala grupa ginie. Polecenie jest
wstrzykiwane - testy podstawiaja atrape zamiast `claude`.
"""
from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cockpit.services.invariants import profil_roli
from cockpit.services.walidacja import dowod_z_pliku, migawka, sprawdz_tylko_odczyt
from runner.agent import SpecKroku

# Katalog runu to jedyne miejsce, w ktorym rola bez zapisu moze pisac (werdykty).
KATALOG_RUNU = Path(".cockpit") / "_run"
ROZMIAR_KAWALKA = 64 * 1024


@dataclass(frozen=True)
class WynikKroku:
    status: Literal["zakonczony", "nieudany"]
    powod: str | None
    kod_wyjscia: int | None


async def _emituj_wyjscie(
    proc: asyncio.subprocess.Process,
    spec: SpecKroku,
    emit: Callable[[dict], Awaitable[None]],
) -> None:
    """Linie wyjscia po kolei do emit.

    Wlasne dzielenie na linie zamiast readline(): readline rzuca na linii dluzszej
    niz limit StreamReadera, a agent moze taka wypisac.
    """
    assert proc.stdout is not None

    async def wyslij(surowa: bytes) -> None:
        await emit({
            "kind": "output",
            "run_id": spec.run_id,
            "step_id": spec.step_id,
            "agent_id": spec.agent_id,
            "text": surowa.decode(errors="replace"),
        })

    reszta = b""
    while kawalek := await proc.stdout.read(ROZMIAR_KAWALKA):
        reszta += kawalek
        *linie, reszta = reszta.split(b"\n")
        for linia in linie:
            await wyslij(linia.removesuffix(b"\r"))
    if reszta:
        await wyslij(reszta)


async def _zabij_grupe(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            # start_new_session: pgid == pid lidera, a getpgid padlby po jego smierci
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


def _porazka(powod: str, kod: int | None) -> WynikKroku:
    return WynikKroku("nieudany", powod, kod)


async def wykonaj_krok(
    spec: SpecKroku,
    polecenie: list[str],
    katalog: Path,
    emit: Callable[[dict], Awaitable[None]],
) -> WynikKroku:
    """Uruchamia polecenie kroku i ocenia wynik: kod wyjscia, artefakty, tylko-odczyt."""
    tylko_odczyt = not profil_roli(spec.rola).zapis
    przed = None
    if tylko_odczyt:
        try:
            przed = await asyncio.to_thread(migawka, katalog, [KATALOG_RUNU])
        except OSError as blad:
            return _porazka(f"nie można wykonać migawki katalogu: {blad}", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            *polecenie,
            cwd=katalog,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as blad:
        return _porazka(f"nie można uruchomić polecenia: {blad}", None)

    try:
        await _emituj_wyjscie(proc, spec, emit)
        kod = await proc.wait()
    finally:
        await _zabij_grupe(proc)

    if kod != 0:
        return _porazka(f"kod wyjścia {kod}", kod)

    for nazwa in spec.wyjscia:
        # os.path.isfile zwraca False przy dowolnym OSError; Path.is_file przed
        # Pythonem 3.14 przepuszcza np. EACCES. Niedostepny artefakt = brak dowodu.
        if not os.path.isfile(katalog / nazwa):
            return _porazka(f"brak artefaktu {nazwa}", kod)
        try:
            naruszenie = dowod_z_pliku(katalog / nazwa)
        except OSError as blad:
            return _porazka(f"nie można odczytać artefaktu {nazwa}: {blad}", kod)
        if naruszenie is not None:
            return _porazka(str(naruszenie), kod)

    if przed is not None:
        try:
            naruszenie = await asyncio.to_thread(
                sprawdz_tylko_odczyt, przed, katalog, [KATALOG_RUNU]
            )
        except OSError as blad:
            return _porazka(
                f"nie można sprawdzić niezmiennika reviewer_readonly: {blad}", kod
            )
        if naruszenie is not None:
            return _porazka(str(naruszenie), kod)

    return WynikKroku("zakonczony", None, kod)

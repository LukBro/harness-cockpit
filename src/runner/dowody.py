"""Krok systemowy `system: check`: dowod silnika zamiast slowa agenta (HAR-177).

Silnik bez LLM uruchamia komende capability z `.cockpit/project.yaml` w worktree i zapisuje w
katalogu kroku w katalogu runu (poza repozytorium) `dowod-<capability>.json`: capability, argv,
kod wyjscia, ogon wyjscia, sha256 PELNEGO wyjscia, czasy i `source: engine`. Recenzent dostaje
ten plik przez `consumes`, wiec nie musi wierzyc agentowi na slowo.

Komenda idzie przez `create_subprocess_exec` we wlasnej grupie procesow (limit czasu i
anulowanie zabijaja cala grupe), a wyjscie jest czytane strumieniowo: w pamieci zostaje tylko
ogon i skrot. Kod 0 z pustym wyjsciem to brak dowodu (`evidence_required`), nigdy sukces.
Zdarzenie kroku niesie wylacznie metadane dowodu: bez ogona wyjscia i bez argv (HAR-66, argv
moze miec sekret).
"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import shlex
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cockpit.services.kroki_systemowe import nazwa_dowodu
from cockpit.services.walidacja import dowod_z_komendy, sprawdz_katalog_runu
from runner.krok import ROZMIAR_KAWALKA, WynikKroku, _zapisz_atomowo

LIMIT_CHECKU_S = 600.0
LIMIT_OGONA = 4000      # bajtow wyjscia w `stdout_tail`
ZRODLO = "engine"
# Po zabiciu grupy czytnik wyjscia konczy na EOF; potomek, ktory uciekl z grupy i trzyma potok,
# nie moze zawiesic kroku.
LIMIT_DRENAZU_S = 5.0
# Opcje zapisu pliku dowodu: polskie znaki doslownie, wciecie 2.
OPCJE_JSON = {"ensure_ascii": False, "indent": 2}


@dataclass
class _Wyjscie:
    """Stan czytnika: skrot calego wyjscia, ogon i flaga znaku innego niz bialy.

    Pustosc to same biale znaki Unicode (`str.isspace`, np. U+00A0), liczona na tekscie
    dekodowanym przyrostowo (znak rozciety na granicy kawalkow nie daje falszywej tresci);
    bajty nie-UTF-8 to U+FFFD, czyli tresc. Dekoder pracuje tylko dopoki `ma_tresc` jest False:
    po znalezieniu tresci wynik jest przesadzony, a skrot i ogon nadal liczone z pelnych bajtow.
    """

    skrot: Any = field(default_factory=hashlib.sha256)
    ogon: bytes = b""
    ma_tresc: bool = False
    dekoder: Any = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))


async def _czytaj(strumien: asyncio.StreamReader, wyjscie: _Wyjscie) -> None:
    while kawalek := await strumien.read(ROZMIAR_KAWALKA):
        wyjscie.skrot.update(kawalek)
        wyjscie.ogon = (wyjscie.ogon + kawalek)[-LIMIT_OGONA:]
        if not wyjscie.ma_tresc:
            wyjscie.ma_tresc = bool(wyjscie.dekoder.decode(kawalek).strip())
    if not wyjscie.ma_tresc:
        # niepelny znak na koncu wyjscia to U+FFFD, nie cicha utrata bajtu
        wyjscie.ma_tresc = bool(wyjscie.dekoder.decode(b"", final=True).strip())


async def _do_konca(proc: asyncio.subprocess.Process, wyjscie: _Wyjscie) -> int:
    assert proc.stdout is not None
    await _czytaj(proc.stdout, wyjscie)
    return await proc.wait()


def _zabij_grupe(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL grupy procesow komendy; grupa, ktorej juz nie ma, nie jest bledem."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _uruchom(
    proc: asyncio.subprocess.Process, wyjscie: _Wyjscie, limit_s: float,
) -> bool:
    """Czeka na koniec komendy i jej wyjscia; po limicie zabija grupe. True = przekroczony limit."""
    zadanie = asyncio.ensure_future(_do_konca(proc, wyjscie))
    try:
        await asyncio.wait({zadanie}, timeout=limit_s)
        przekroczony = not zadanie.done()
        if przekroczony:
            _zabij_grupe(proc)
            await asyncio.wait({zadanie}, timeout=LIMIT_DRENAZU_S)
    finally:
        # anulowanie zadania checku: komenda i jej potomkowie nie zostaja sierotami
        if proc.returncode is None:
            _zabij_grupe(proc)
        zadanie.cancel()
        await proc.wait()
    return przekroczony


def _porazka(capability: str, powod: str, kod: int | None) -> WynikKroku:
    return WynikKroku("nieudany", f"check {capability}: {powod}", kod)


async def wykonaj_check(
    worktree: Path, capability: str, komenda: str, katalog_kroku: Path,
    *, limit_s: float = LIMIT_CHECKU_S,
) -> WynikKroku:
    """Uruchamia `komenda` w `worktree` i zapisuje `dowod-<capability>.json` w `katalog_kroku`.

    Komenda nieczytelna, katalog kroku w repozytorium i blad startu to porazka bez pliku
    dowodu; kazdy zakonczony proces (kod 0, kod != 0, pusty wynik, limit czasu) zostawia dowod.
    Tresci komendy nie ma w komunikatach."""
    try:
        argv = shlex.split(komenda)
    except ValueError:
        # mutant None jest rownowazny (nastepna linia `if not argv:` traktuje None i [] tak samo)
        argv = []  # pragma: no mutate
    if not argv:
        return _porazka(capability, "komenda nieczytelna", None)
    naruszenie = sprawdz_katalog_runu(katalog_kroku, worktree)
    if naruszenie is not None:
        return WynikKroku("nieudany", str(naruszenie), None)

    start = datetime.now(timezone.utc).isoformat()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=worktree, start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as blad:
        return _porazka(
            capability, f"nie uruchomiono komendy ({type(blad).__name__})", None)
    wyjscie = _Wyjscie()
    przekroczony = await _uruchom(proc, wyjscie, limit_s)
    koniec = datetime.now(timezone.utc).isoformat()

    kod = None if przekroczony else proc.returncode
    metadane = {
        "capability": capability, "exit_code": kod, "sha256": wyjscie.skrot.hexdigest(),
        "started_at": start, "finished_at": koniec, "source": ZRODLO,
    }
    plik = nazwa_dowodu(capability)
    # mutanty UTF-8 i brak kodeka sa rownowazne (domyslny kodek bytes.decode to utf-8,
    # nazwa bez rozroznienia wielkosci liter); wariant errors i XXutf-8XX chroni reczny mutant
    ogon = wyjscie.ogon.decode("utf-8", errors="replace")  # pragma: no mutate
    zapis = {
        "capability": capability, "argv": argv, "exit_code": kod,
        "stdout_tail": ogon,
        "sha256": metadane["sha256"], "started_at": start, "finished_at": koniec,
        "source": ZRODLO,
    }
    tekst = json.dumps(zapis, **OPCJE_JSON)
    # mutant UTF-8 jest rownowazny (nazwa kodeka bez rozroznienia wielkosci liter);
    # wariant XXutf-8XX chroni reczny mutant
    tresc = tekst.encode("utf-8")  # pragma: no mutate
    try:
        await asyncio.to_thread(_zapisz_atomowo, katalog_kroku / plik, tresc)
    except OSError as blad:
        return _porazka(capability, f"nie zapisano dowodu ({type(blad).__name__})", kod)
    dowod = {**metadane, "plik": plik}

    if przekroczony:
        return WynikKroku(
            "nieudany", f"check {capability}: przekroczony limit czasu {limit_s:g} s", None,
            dowod=dowod)
    if kod != 0:
        return WynikKroku(
            "nieudany", f"check {capability}: kod wyjścia {kod}", kod, dowod=dowod)
    if not wyjscie.ma_tresc:
        # kod 0 bez zadnego wyniku to brak dowodu, nie zielony check
        brak = dowod_z_komendy("")
        return WynikKroku("nieudany", f"check {capability}: {brak}", kod, dowod=dowod)
    return WynikKroku("zakonczony", None, kod, dowod=dowod)

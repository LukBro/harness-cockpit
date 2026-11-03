"""Wykonanie kroku u runnera: status z kodu wyjscia i artefaktow produces (HAR-42).

Agent dziala pod straznikiem (`runner.straznik`, HAR-85) we wlasnej grupie
procesow. Runner trzyma otwarty potok do stdin straznika do konca kroku: gdy krok
nie konczy sie sam (anulowanie, wyjatek odbiorcy zdarzen) albo runner umrze, EOF
kaze straznikowi zabic grupe agenta (SIGTERM, po 5 s SIGKILL). Blad startu agenta
straznik zglasza osobnym potokiem statusu, nie kodem wyjscia. Polecenie jest
wstrzykiwane - testy podstawiaja atrape zamiast `claude`.
"""
from __future__ import annotations

import asyncio
import io
import os
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cockpit.services.invariants import profil_roli
from cockpit.services.walidacja import (
    Naruszenie,
    Niezmiennik,
    dowod_z_pliku,
    migawka,
    sprawdz_katalog_runu,
    sprawdz_tylko_odczyt,
)
from runner.agent import SpecKroku
from runner.straznik import ZMIENNA_PYTHONPATH_AGENTA

# Katalog runu to jedyne miejsce, w ktorym rola bez zapisu moze pisac (werdykty).
KATALOG_RUNU = Path(".cockpit") / "_run"
ROZMIAR_KAWALKA = 64 * 1024
# Najwiekszy wynik agenta (stdout), jaki runner zapisze jako artefakt roli bez zapisu.
LIMIT_ARTEFAKTU = 8 * 1024 * 1024
# Straznik daje agentowi 5 s na SIGTERM, potem SIGKILL; runner czeka dluzej.
LIMIT_NA_STRAZNIKA_S = 8
# Katalog z pakietem `runner` biezacego drzewa (bez resolve: pod mutmut sciezka
# wskazuje kopie w `mutants/src/`). Trafia na poczatek PYTHONPATH straznika, zeby
# importowal kod z tego drzewa, a nie z editable install glownego checkoutu.
KATALOG_PAKIETU = Path(__file__).parent.parent
# Straznik startuje z korzenia (katalog z pyproject.toml; pod mutmut `mutants/`):
# zmutowany kod czyta konfiguracje mutmuta z cwd procesu i bez pyproject.toml pada
# (HAR-50). Agentowi cwd przekazuje `--cwd`.
KATALOG_KORZENIA = KATALOG_PAKIETU.parent


def _srodowisko_straznika() -> dict[str, str]:
    """Srodowisko runnera z pakietem biezacego drzewa przed istniejacym PYTHONPATH.

    Dla straznika. Agent dostaje pierwotny PYTHONPATH przez ZMIENNA_PYTHONPATH_AGENTA.
    """
    sciezki = [str(KATALOG_PAKIETU)]
    if istniejace := os.environ.get("PYTHONPATH"):
        sciezki.append(istniejace)
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(sciezki),
        # Straznik oddaje agentowi pierwotny PYTHONPATH (pusty = brak).
        ZMIENNA_PYTHONPATH_AGENTA: os.environ.get("PYTHONPATH", ""),
    }


@dataclass(frozen=True)
class WynikKroku:
    status: Literal["zakonczony", "nieudany"]
    powod: str | None
    kod_wyjscia: int | None


async def _emituj_strumien(
    strumien: asyncio.StreamReader,
    spec: SpecKroku,
    emit: Callable[[dict], Awaitable[None]],
    bufor: bytearray | None,
    limit: int,
) -> None:
    """Linie strumienia po kolei do emit; z `bufor` zbiera surowe bajty (do limit + 1).

    Wlasne dzielenie na linie zamiast readline(): readline rzuca na linii dluzszej
    niz limit StreamReadera, a agent moze taka wypisac.
    """

    async def wyslij(surowa: bytes) -> None:
        await emit({
            "kind": "output",
            "run_id": spec.run_id,
            "step_id": spec.step_id,
            "agent_id": spec.agent_id,
            "text": surowa.decode(errors="replace"),
        })

    reszta = b""
    while kawalek := await strumien.read(ROZMIAR_KAWALKA):
        if bufor is not None:
            # limit + 1 bajtow wystarcza, by poznac przekroczenie; pamiec zostaje ograniczona
            bufor += kawalek[: limit + 1 - len(bufor)]
        reszta += kawalek
        *linie, reszta = reszta.split(b"\n")
        for linia in linie:
            await wyslij(linia.removesuffix(b"\r"))
    if reszta:
        await wyslij(reszta)


async def _emituj_wyjscie(
    proc: asyncio.subprocess.Process,
    spec: SpecKroku,
    emit: Callable[[dict], Awaitable[None]],
    bufor: bytearray | None,
    limit: int,
) -> None:
    """Wyjscie agenta do emit. Z `bufor` stdout i stderr sa osobnymi potokami czytanymi
    rownolegle (pelny stderr nie moze zablokowac agenta), a do bufora trafia tylko stdout.
    Bez `bufor` stdout niesie oba kanaly scalone."""
    assert proc.stdout is not None
    strumienie = [(proc.stdout, bufor)]
    if bufor is not None:
        assert proc.stderr is not None
        strumienie.append((proc.stderr, None))
    zadania = [
        asyncio.ensure_future(_emituj_strumien(s, spec, emit, b, limit)) for s, b in strumienie
    ]
    try:
        await asyncio.gather(*zadania)
    finally:
        # wyjatek jednego czytnika albo anulowanie nie zostawia drugiego w tle
        for zadanie in zadania:
            zadanie.cancel()


async def _czekaj_na_straznika(proc: asyncio.subprocess.Process) -> None:
    """EOF na stdin straznika, potem czekanie z limitem na jego wyjscie.

    Ostatecznosc po limicie: SIGKILL grupy straznika (agent ma wlasna grupe, wiec
    ginie tylko straznik).
    """
    if proc.stdin is not None:
        proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), LIMIT_NA_STRAZNIKA_S)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()


async def _status_startu(plik) -> str | None:
    """Komunikat bledu startu agenta od straznika; None = start udany (EOF bez danych)."""
    czytnik = asyncio.StreamReader()
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(czytnik), plik
        )
        try:
            dane = await czytnik.read()
        finally:
            transport.close()
    finally:
        plik.close()
    # fsdecode nie rzuca (surrogateescape); straznik i tak pisze poprawny UTF-8
    return os.fsdecode(dane) if dane else None


def _porazka(powod: str, kod: int | None) -> WynikKroku:
    return WynikKroku("nieudany", powod, kod)


def _katalog_artefaktu(
    spec: SpecKroku, katalog: Path, katalog_runu: Path | None
) -> tuple[Path | None, str | None]:
    """(katalog kroku w katalogu runu, powod porazki) dla roli bez zapisu z artefaktem.

    Sprawdzenia przed startem agenta, w tej kolejnosci: liczba artefaktow, katalog
    runu, nazwa artefaktu, identyfikator kroku, polozenie poza repozytorium.
    """
    rola = spec.rola.value
    if len(spec.wyjscia) > 1:
        return None, (
            f"rola {rola} oddaje jeden artefakt, zadeklarowano "
            f"{len(spec.wyjscia)}: {', '.join(spec.wyjscia)}"
        )
    nazwa = spec.wyjscia[0]
    if katalog_runu is None:
        return None, f"brak katalogu runu dla artefaktu {nazwa} roli {rola}"
    sciezka = Path(nazwa)
    if sciezka.is_absolute() or ".." in sciezka.parts or not sciezka.parts:
        return None, f"niedozwolona ścieżka artefaktu: {nazwa!r}"
    krok = spec.step_id
    if krok in ("", ".", "..") or "/" in krok or "\\" in krok:
        return None, f"niedozwolony identyfikator kroku: {krok!r}"
    katalog_kroku = katalog_runu / krok
    naruszenie = sprawdz_katalog_runu(katalog_kroku, katalog)
    if naruszenie is not None:
        return None, str(naruszenie)
    return katalog_kroku, None


def _zapisz_atomowo(cel: Path, tresc: bytes) -> None:
    """Plik tymczasowy w katalogu docelowym, potem os.replace: czytelnik widzi caly plik albo stary."""
    cel.parent.mkdir(parents=True, exist_ok=True)
    deskryptor, tymczasowy = tempfile.mkstemp(dir=cel.parent)
    try:
        with os.fdopen(deskryptor, "wb") as plik:
            plik.write(tresc)
        os.replace(tymczasowy, cel)
    except BaseException:
        os.unlink(tymczasowy)
        raise


async def wykonaj_krok(
    spec: SpecKroku,
    polecenie: list[str],
    katalog: Path,
    emit: Callable[[dict], Awaitable[None]],
    *,
    katalog_runu: Path | None = None,
) -> WynikKroku:
    """Uruchamia polecenie kroku i ocenia wynik: kod wyjscia, artefakty, tylko-odczyt.

    Rola bez zapisu z artefaktem w `wyjscia` oddaje go jako wynik agenta (stdout);
    runner zapisuje go w `katalog_runu/<step_id>/`, poza repozytorium (HAR-160).
    """
    tylko_odczyt = not profil_roli(spec.rola).zapis
    artefakt_z_wyniku = tylko_odczyt and bool(spec.wyjscia)
    katalog_dowodu = katalog
    if artefakt_z_wyniku:
        katalog_kroku, powod = _katalog_artefaktu(spec, katalog, katalog_runu)
        if katalog_kroku is None:
            return _porazka(str(powod), None)
        katalog_dowodu = katalog_kroku
    przed = None
    if tylko_odczyt:
        try:
            przed = await asyncio.to_thread(migawka, katalog, [KATALOG_RUNU])
        except OSError as blad:
            return _porazka(f"nie można wykonać migawki katalogu: {blad}", None)

    odczyt, zapis = os.pipe()
    # FileIO: binarny odczyt bez bufora; connect_read_pipe i tak czyta przez fileno()
    plik_statusu = io.FileIO(odczyt)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "runner.straznik",
            "--status-fd", str(zapis),
            "--cwd", os.path.abspath(katalog),
            "--",
            *polecenie,
            cwd=KATALOG_KORZENIA,
            env=_srodowisko_straznika(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.PIPE if artefakt_z_wyniku else asyncio.subprocess.STDOUT
            ),
            start_new_session=True,
            pass_fds=(zapis,),
        )
    except OSError as blad:
        plik_statusu.close()
        return _porazka(f"nie można uruchomić polecenia: {blad}", None)
    except BaseException:
        # Anulowanie w trakcie startu: deskryptor odczytu nie moze wyciec.
        plik_statusu.close()
        raise
    finally:
        os.close(zapis)

    wynik_agenta = bytearray() if artefakt_z_wyniku else None
    try:
        blad_startu = await _status_startu(plik_statusu)
        if blad_startu is not None:
            return _porazka(f"nie można uruchomić polecenia: {blad_startu}", None)
        await _emituj_wyjscie(proc, spec, emit, wynik_agenta, LIMIT_ARTEFAKTU)
        kod = await proc.wait()
    finally:
        await _czekaj_na_straznika(proc)

    if kod != 0:
        return _porazka(f"kod wyjścia {kod}", kod)

    if wynik_agenta is not None:
        nazwa = spec.wyjscia[0]
        if len(wynik_agenta) > LIMIT_ARTEFAKTU:
            return _porazka(f"wynik agenta przekracza {LIMIT_ARTEFAKTU} bajtów", kod)
        tekst = wynik_agenta.decode(errors="replace")
        if not tekst.strip():
            return _porazka(str(Naruszenie(
                Niezmiennik.evidence_required, f"pusty wynik agenta, brak artefaktu {nazwa}"
            )), kod)
        try:
            await asyncio.to_thread(
                _zapisz_atomowo, katalog_dowodu / nazwa, tekst.encode()
            )
        except OSError as blad:
            return _porazka(f"nie można zapisać artefaktu {nazwa}: {blad}", kod)

    for nazwa in spec.wyjscia:
        # os.path.isfile zwraca False przy dowolnym OSError; Path.is_file przed
        # Pythonem 3.14 przepuszcza np. EACCES. Niedostepny artefakt = brak dowodu.
        if not os.path.isfile(katalog_dowodu / nazwa):
            return _porazka(f"brak artefaktu {nazwa}", kod)
        try:
            naruszenie = dowod_z_pliku(katalog_dowodu / nazwa)
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

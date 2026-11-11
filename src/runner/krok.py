"""Wykonanie kroku u runnera: status z kodu wyjscia i artefaktow produces (HAR-42).

Agent dziala pod straznikiem (`runner.straznik`, HAR-85) we wlasnej grupie
procesow. Runner trzyma otwarty potok do stdin straznika do konca kroku: gdy krok
nie konczy sie sam (anulowanie, wyjatek odbiorcy zdarzen) albo runner umrze, EOF
kaze straznikowi zabic grupe agenta (SIGTERM, po 5 s SIGKILL). Blad startu agenta
straznik zglasza osobnym potokiem statusu, nie kodem wyjscia. Polecenie jest
wstrzykiwane - testy podstawiaja atrape zamiast `claude`.

Tryb agenta (HAR-174, `prompt` podany): prompt leci plikiem w katalogu runu jako stdin agenta
(`--stdin-plik` straznika), a stdout agenta (`claude -p --output-format stream-json`) to NDJSON
czytany linia po linii. Koszt, tokeny, tury i session_id pochodza z ostatniego zdarzenia
`result`; brak pomiaru to None, nigdy zero (measured_or_null). Bez `prompt` wyjscie jest surowe.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import signal
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cockpit.services.invariants import profil_roli
from cockpit.services.walidacja import (
    KontraktWerdyktu,
    Naruszenie,
    Niezmiennik,
    dowod_z_pliku,
    migawka,
    sprawdz_katalog_runu,
    sprawdz_tylko_odczyt,
    sprawdz_werdykt,
)
from cockpit.services.kroki_systemowe import PLIK_LINII
from runner.agent import SpecKroku
from runner.kotwice import KODOWANIE, PLIK_NOTATKI, sprawdz_kotwice, wczytaj_zmienione_linie, wyciagnij_kotwice
from runner.straznik import (
    CZAS_NA_ZAKONCZENIE_S,
    ZMIENNA_BRAK_PYTHONPATH_AGENTA,
    ZMIENNA_PYTHONPATH_AGENTA,
)

# Katalog runu to jedyne miejsce, w ktorym rola bez zapisu moze pisac (werdykty).
KATALOG_RUNU = Path(".cockpit") / "_run"
ROZMIAR_KAWALKA = 64 * 1024
# Najwiekszy wynik agenta (stdout), jaki runner zapisze jako artefakt roli bez zapisu.
LIMIT_ARTEFAKTU = 8 * 1024 * 1024
# Najdluzsza linia NDJSON agenta (w bajtach, bez znaku nowej linii); dluzsza to blad kroku.
LIMIT_LINII_AGENTA = 8 * 1024 * 1024
# Ile znakow niezrozumialej linii trafia do powodu porazki.
DLUGOSC_FRAGMENTU_LINII = 200
KATALOG_PROMPTOW = "prompty"
INT4_MAX = 2_147_483_647
LIMIT_SESSION_ID = 100   # kolumna step_run.session_id to String(100)
PODTYPY_BUDZETU = frozenset({"error_max_budget_usd", "error_max_turns"})
# Straznik daje agentowi CZAS_NA_ZAKONCZENIE_S na SIGTERM, potem SIGKILL. Runner czeka
# dwukrotnosc karencji z zapasem: zabity przed swoim SIGKILL-em straznik zostawilby agenta
# ignorujacego SIGTERM jako sierote (HAR-227). Petla 20x przy load ~550 (12 rdzeni)
# pokazala, ze wykrycie EOF i planista straznika zjadaja kilka sekund ponad karencje,
# wiec zapas 5 s nie wystarczal (limit 20 s).
ZAPAS_NA_STRAZNIKA_S = 10
LIMIT_NA_STRAZNIKA_S = 2 * CZAS_NA_ZAKONCZENIE_S + ZAPAS_NA_STRAZNIKA_S
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

    Dla straznika. Agent dostaje pierwotny PYTHONPATH przez ZMIENNA_PYTHONPATH_AGENTA
    (takze pusty); brak PYTHONPATH u runnera to ZMIENNA_BRAK_PYTHONPATH_AGENTA.
    """
    sciezki = [str(KATALOG_PAKIETU)]
    if istniejace := os.environ.get("PYTHONPATH"):
        sciezki.append(istniejace)
    srodowisko = {
        k: v
        for k, v in os.environ.items()
        if k not in (ZMIENNA_PYTHONPATH_AGENTA, ZMIENNA_BRAK_PYTHONPATH_AGENTA)
    }
    srodowisko["PYTHONPATH"] = os.pathsep.join(sciezki)
    if "PYTHONPATH" in os.environ:
        srodowisko[ZMIENNA_PYTHONPATH_AGENTA] = os.environ["PYTHONPATH"]
    else:
        srodowisko[ZMIENNA_BRAK_PYTHONPATH_AGENTA] = "1"
    return srodowisko


@dataclass(frozen=True)
class PomiarKroku:
    """Pomiar kroku z wyniku agenta; None = brak pomiaru (nigdy zero)."""

    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    num_turns: int | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class WynikKroku:
    status: Literal["zakonczony", "nieudany"]
    powod: str | None
    kod_wyjscia: int | None
    pomiar: PomiarKroku = PomiarKroku()
    # kotwice `plik:linia` z notatki zmiany (HAR-176); puste dla kazdego innego kroku
    kotwice: tuple[str, ...] = ()
    # werdykt z linii artefaktu wedlug kontraktu skilla (HAR-177); None bez kontraktu
    werdykt: str | None = None
    # metadane dowodu silnika kroku `check` (HAR-177), bez wyjscia komendy i argv
    dowod: Mapping[str, object] | None = None


@dataclass
class _StanStrumienia:
    """Co parser NDJSON wyniosl z wyjscia agenta: pierwszy blad linii i ostatni `result`."""

    blad_linii: str | None = None
    wynik: dict[str, Any] | None = None


async def _emituj_strumien(
    strumien: asyncio.StreamReader,
    spec: SpecKroku,
    emit: Callable[[dict], Awaitable[None]],
    bufor: bytearray | None = None,
    limit: int | None = None,
) -> None:
    """Linie strumienia po kolei do emit; z `bufor` zbiera surowe bajty (do limit + 1;
    `limit` czyta tylko zbieranie, wiec bez bufora zostaje nieustawiony).

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


async def _czytaj_zdarzenia_agenta(
    strumien: asyncio.StreamReader,
    spec: SpecKroku,
    emit: Callable[[dict], Awaitable[None]],
    stan: _StanStrumienia,
) -> None:
    """Parser NDJSON stdout agenta. Po bledzie linii czyta wyjscie do konca bez parsowania,
    zeby agent nie zawisl na pelnym potoku."""
    limit = LIMIT_LINII_AGENTA     # odczyt w chwili wywolania (testy podmieniaja stala)

    async def przetworz(surowa: bytes) -> None:
        if stan.blad_linii is not None:
            return
        tekst = surowa.decode(errors="replace")
        if not tekst.strip():
            return
        niezrozumiala = (
            f"niezrozumiała linia wyjścia agenta: {tekst[:DLUGOSC_FRAGMENTU_LINII]}"
        )
        try:
            zdarzenie = json.loads(tekst)
        except (ValueError, RecursionError):
            stan.blad_linii = niezrozumiala
            return
        if not isinstance(zdarzenie, dict) or not isinstance(zdarzenie.get("type"), str):
            stan.blad_linii = niezrozumiala
            return
        if zdarzenie["type"] == "result":
            stan.wynik = zdarzenie
        elif zdarzenie["type"] == "assistant":
            wiadomosc = zdarzenie.get("message")
            tresc = wiadomosc.get("content") if isinstance(wiadomosc, dict) else None
            if not isinstance(tresc, list):
                stan.blad_linii = niezrozumiala
                return
            for element in tresc:
                if (isinstance(element, dict) and element.get("type") == "text"
                        and isinstance(element.get("text"), str)):
                    for linia in element["text"].splitlines():
                        await emit({
                            "kind": "output", "run_id": spec.run_id, "step_id": spec.step_id,
                            "agent_id": spec.agent_id, "text": linia,
                        })

    async def linia_gotowa(surowa: bytes) -> None:
        if len(surowa) > limit:
            if stan.blad_linii is None:
                stan.blad_linii = f"linia wyjścia agenta przekracza {limit} bajtów"
            return
        await przetworz(surowa)

    reszta = bytearray()
    while kawalek := await strumien.read(ROZMIAR_KAWALKA):
        if stan.blad_linii is not None:
            continue       # po bledzie tylko opróżniamy potok; nic nie jest buforowane
        *pelne, ogon = kawalek.split(b"\n")
        for segment in pelne:
            reszta += segment
            await linia_gotowa(bytes(reszta))
            reszta.clear()
        reszta += ogon
        if len(reszta) > limit:
            await linia_gotowa(bytes(reszta))
            reszta.clear()
    if reszta:
        await linia_gotowa(bytes(reszta))


async def _czytaj_wyjscie_agenta(
    proc: asyncio.subprocess.Process,
    spec: SpecKroku,
    emit: Callable[[dict], Awaitable[None]],
    stan: _StanStrumienia,
) -> None:
    """stdout przez parser NDJSON, stderr jako `output`; oba czytane rownolegle."""
    zadania = [
        asyncio.ensure_future(_czytaj_zdarzenia_agenta(proc.stdout, spec, emit, stan)),
        asyncio.ensure_future(_emituj_strumien(proc.stderr, spec, emit)),
    ]
    try:
        await asyncio.gather(*zadania)
    finally:
        for zadanie in zadania:
            zadanie.cancel()


def _liczba_calkowita(wartosc: object) -> int | None:
    """int4 >= 0; bool, ulamek, napis i liczba spoza zakresu to brak pomiaru."""
    if isinstance(wartosc, bool) or not isinstance(wartosc, int):
        return None
    return wartosc if 0 <= wartosc <= INT4_MAX else None


def _koszt(wartosc: object) -> float | None:
    if isinstance(wartosc, bool) or not isinstance(wartosc, (int, float)):
        return None
    try:
        koszt = float(wartosc)
    except OverflowError:
        return None
    return koszt if math.isfinite(koszt) and koszt >= 0 else None


def _pomiar_z_wyniku(wynik: dict[str, Any] | None) -> PomiarKroku:
    if wynik is None:
        return PomiarKroku()
    usage = wynik.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    sesja = wynik.get("session_id")
    return PomiarKroku(
        cost_usd=_koszt(wynik.get("total_cost_usd")),
        tokens_in=_liczba_calkowita(usage.get("input_tokens")),
        tokens_out=_liczba_calkowita(usage.get("output_tokens")),
        num_turns=_liczba_calkowita(wynik.get("num_turns")),
        session_id=sesja if isinstance(sesja, str) and 0 < len(sesja) <= LIMIT_SESSION_ID else None,
    )


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


def _porazka(
    powod: str, kod: int | None, pomiar: PomiarKroku = PomiarKroku()
) -> WynikKroku:
    return WynikKroku("nieudany", powod, kod, pomiar=pomiar)


def _blad_identyfikatora(krok: str) -> str | None:
    """Powod odrzucenia id kroku uzywanego jako nazwa pliku albo katalogu."""
    if krok in ("", ".", "..") or "/" in krok or "\\" in krok:
        return f"niedozwolony identyfikator kroku: {krok!r}"
    return None


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
    if (powod := _blad_identyfikatora(spec.step_id)) is not None:
        return None, powod
    katalog_kroku = katalog_runu / spec.step_id
    naruszenie = sprawdz_katalog_runu(katalog_kroku, katalog)
    if naruszenie is not None:
        return None, str(naruszenie)
    return katalog_kroku, None


def _ocen_notatke(
    plik: Path, plik_linii: Path | None
) -> tuple[str | None, tuple[str, ...]]:
    """(powod porazki, kotwice) notatki zmiany wzgledem `zmienione-linie.json` kroku diff.

    Brak czytelnego pliku zmienionych linii to brak dowodu: notatki nie da sie ocenic,
    wiec krok nie przechodzi (evidence_required), zamiast przejsc bez sprawdzenia."""
    zmienione = wczytaj_zmienione_linie(plik_linii)
    if zmienione is None:
        return str(Naruszenie(
            Niezmiennik.evidence_required,
            f"brak czytelnego {PLIK_LINII} dla {PLIK_NOTATKI}",
        )), ()
    try:
        notatka = plik.read_text(encoding=KODOWANIE, errors="replace")
    except OSError as blad:
        return f"nie można odczytać artefaktu {PLIK_NOTATKI}: {blad}", ()
    naruszenia = sprawdz_kotwice(notatka, zmienione)
    if naruszenia:
        return "; ".join(str(n) for n in naruszenia), ()
    return None, tuple(wyciagnij_kotwice(notatka))


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


async def _zapisz_prompt(
    spec: SpecKroku, katalog: Path, katalog_runu: Path | None, prompt: str
) -> tuple[Path | None, str | None]:
    """(plik promptu w katalogu runu, powod porazki); plik leci do agenta jako stdin."""
    if katalog_runu is None:
        return None, "brak katalogu runu dla pliku promptu"
    if (powod := _blad_identyfikatora(spec.step_id)) is not None:
        return None, powod
    katalog_promptow = katalog_runu / KATALOG_PROMPTOW
    naruszenie = sprawdz_katalog_runu(katalog_promptow, katalog)
    if naruszenie is not None:
        return None, str(naruszenie)
    plik = katalog_promptow / f"{spec.step_id}.txt"
    try:
        await asyncio.to_thread(_zapisz_atomowo, plik, prompt.encode())
    except OSError as blad:
        return None, f"nie można zapisać pliku promptu: {blad}"
    return plik, None


async def wykonaj_krok(
    spec: SpecKroku,
    polecenie: list[str],
    katalog: Path,
    emit: Callable[[dict], Awaitable[None]],
    *,
    katalog_runu: Path | None = None,
    prompt: str | None = None,
    zmienione_linie: Path | None = None,
    werdykt: KontraktWerdyktu | None = None,
) -> WynikKroku:
    """Uruchamia polecenie kroku i ocenia wynik: kod wyjscia, artefakty, tylko-odczyt.

    Gdy `wyjscia` zawieraja `notatka-zmiany.md`, kotwice jej ryzyk sprawdza `kotwice`
    wzgledem `zmienione_linie` (plik `zmienione-linie.json` kroku diff, HAR-176).

    Z kontraktem `werdykt` linia werdyktu pierwszego artefaktu rozstrzyga o wyniku: wartosc
    z `fail` to porazka, brak linii albo wartosc spoza list to `evidence_required`.

    Rola bez zapisu z artefaktem w `wyjscia` oddaje go jako wynik agenta (stdout);
    runner zapisuje go w `katalog_runu/<step_id>/`, poza repozytorium (HAR-160).

    Z `prompt` dziala tryb agenta (stream-json, pomiar kroku): kolejnosc oceny to linia
    niezrozumiala, wyczerpany budzet, kod wyjscia, brak `result`, blad zgloszony przez
    agenta, artefakty. Bez `prompt` wyjscie jest surowe i pomiar pusty.
    """
    tryb_agenta = prompt is not None
    tylko_odczyt = not profil_roli(spec.rola).zapis
    artefakt_z_wyniku = tylko_odczyt and bool(spec.wyjscia)
    katalog_dowodu = katalog
    if artefakt_z_wyniku:
        katalog_kroku, powod = _katalog_artefaktu(spec, katalog, katalog_runu)
        if katalog_kroku is None:
            return _porazka(str(powod), None)
        katalog_dowodu = katalog_kroku
    opcje_stdin: list[str] = []
    if prompt is not None:
        plik_promptu, powod = await _zapisz_prompt(spec, katalog, katalog_runu, prompt)
        if plik_promptu is None:
            return _porazka(str(powod), None)
        opcje_stdin = ["--stdin-plik", os.path.abspath(plik_promptu)]
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
            *opcje_stdin,
            "--",
            *polecenie,
            cwd=KATALOG_KORZENIA,
            env=_srodowisko_straznika(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.PIPE
                if artefakt_z_wyniku or tryb_agenta
                else asyncio.subprocess.STDOUT
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

    wynik_agenta = bytearray() if artefakt_z_wyniku and not tryb_agenta else None
    stan = _StanStrumienia()
    try:
        blad_startu = await _status_startu(plik_statusu)
        if blad_startu is not None:
            return _porazka(f"nie można uruchomić polecenia: {blad_startu}", None)
        if tryb_agenta:
            await _czytaj_wyjscie_agenta(proc, spec, emit, stan)
        else:
            await _emituj_wyjscie(proc, spec, emit, wynik_agenta, LIMIT_ARTEFAKTU)
        kod = await proc.wait()
    finally:
        await _czekaj_na_straznika(proc)

    pomiar = _pomiar_z_wyniku(stan.wynik)     # dla trybu surowego stan jest pusty
    podtyp = stan.wynik.get("subtype") if stan.wynik is not None else None
    if stan.blad_linii is not None:
        return _porazka(stan.blad_linii, kod, pomiar)
    if isinstance(podtyp, str) and podtyp in PODTYPY_BUDZETU:
        return _porazka(f"budżet kroku wyczerpany ({podtyp})", kod, pomiar)

    if kod != 0:
        return _porazka(f"kod wyjścia {kod}", kod, pomiar)

    if tryb_agenta:
        if stan.wynik is None:
            return _porazka(str(Naruszenie(
                Niezmiennik.evidence_required, "brak zdarzenia result w wyjściu agenta"
            )), kod)
        if stan.wynik.get("is_error") is True or podtyp != "success":
            return _porazka(f"agent zakończył się błędem: {podtyp}", kod, pomiar)
        if artefakt_z_wyniku:
            nazwa = spec.wyjscia[0]
            tresc = stan.wynik.get("result")
            tresc = tresc if isinstance(tresc, str) else ""
            dane = tresc.encode(errors="replace")
            if len(dane) > LIMIT_ARTEFAKTU:
                return _porazka(f"wynik agenta przekracza {LIMIT_ARTEFAKTU} bajtów", kod, pomiar)
            if not tresc.strip():
                return _porazka(str(Naruszenie(
                    Niezmiennik.evidence_required, f"pusty wynik agenta, brak artefaktu {nazwa}"
                )), kod, pomiar)
            try:
                await asyncio.to_thread(_zapisz_atomowo, katalog_dowodu / nazwa, dane)
            except OSError as blad:
                return _porazka(f"nie można zapisać artefaktu {nazwa}: {blad}", kod, pomiar)

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
            return _porazka(f"brak artefaktu {nazwa}", kod, pomiar)
        try:
            naruszenie = dowod_z_pliku(katalog_dowodu / nazwa)
        except OSError as blad:
            return _porazka(f"nie można odczytać artefaktu {nazwa}: {blad}", kod, pomiar)
        if naruszenie is not None:
            return _porazka(str(naruszenie), kod, pomiar)

    if przed is not None:
        try:
            naruszenie = await asyncio.to_thread(
                sprawdz_tylko_odczyt, przed, katalog, [KATALOG_RUNU]
            )
        except OSError as blad:
            return _porazka(
                f"nie można sprawdzić niezmiennika reviewer_readonly: {blad}", kod, pomiar
            )
        if naruszenie is not None:
            return _porazka(str(naruszenie), kod, pomiar)

    wartosc_werdyktu = None
    if werdykt is not None:
        ocena = sprawdz_werdykt(katalog_dowodu / spec.wyjscia[0], werdykt)
        if isinstance(ocena, Naruszenie):
            return _porazka(str(ocena), kod, pomiar)
        if not ocena.zdany:
            return WynikKroku(
                "nieudany", f"werdykt: {ocena.wartosc}", kod, pomiar=pomiar,
                werdykt=ocena.wartosc)
        wartosc_werdyktu = ocena.wartosc

    if PLIK_NOTATKI in spec.wyjscia:
        powod, kotwice = await asyncio.to_thread(
            _ocen_notatke, katalog_dowodu / PLIK_NOTATKI, zmienione_linie
        )
        if powod is not None:
            return _porazka(powod, kod, pomiar)
        return WynikKroku(
            "zakonczony", None, kod, pomiar=pomiar, kotwice=kotwice, werdykt=wartosc_werdyktu)

    return WynikKroku("zakonczony", None, kod, pomiar=pomiar, werdykt=wartosc_werdyktu)

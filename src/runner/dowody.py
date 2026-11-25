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

from cockpit.services.errors import BladDziedziny
from cockpit.services.invariants import Niezmiennik
from cockpit.services.kroki_systemowe import CAPABILITY_MUTATE_DIFF, PLIK_MUTANTOW, nazwa_dowodu
from cockpit.services.walidacja import Naruszenie, dowod_z_komendy, sprawdz_katalog_runu
from runner.krok import ROZMIAR_KAWALKA, WynikKroku, _zapisz_atomowo

LIMIT_CHECKU_S = 600.0
# Mutacje zmienionych funkcji trwaja dluzej niz zwykly check; limit dotyczy calej komendy raportu.
LIMIT_MUTATE_DIFF_S = 3600.0
# Raport mutantow czytamy w calosci (nie tylko ogon); wiekszy to blad, nie obciety raport.
LIMIT_RAPORTU = 8 * 1024 * 1024
# Kody narzedzia w raporcie: 0 czysto, 1 sa przezyle, 3 zmiany poza zasiegiem mutmuta (ostrzezenie).
KODY_NARZEDZIA = (0, 1, 3)
KLUCZE_RAPORTU = frozenset({"zbadane", "przezyle", "kod_narzedzia"})
# `poza_zasiegiem`: linie do recznego sprawdzenia (niepusta przy kodzie 3, dozwolona przy 0 i 1). Klucz jest opcjonalny na wejsciu (brak =
# pusta lista, zgodnosc wsteczna), a `mutanty.json` zawsze go zapisuje.
KLUCZ_POZA_ZASIEGIEM = "poza_zasiegiem"
KLUCZE_PRZEZYLEGO = frozenset({"plik", "linia", "opis_mutanta"})
KLUCZE_POZA_ZASIEGIEM = frozenset({"plik", "linia"})
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
    # cale wyjscie do parsowania raportu (HAR-168); `None` = nie zbieramy, `przepelnione` = ponad
    # `LIMIT_RAPORTU` (bufor wtedy pusty: wynik jest bledem, nie obcietym raportem)
    pelne: bytearray | None = None
    przepelnione: bool = False


async def _czytaj(strumien: asyncio.StreamReader, wyjscie: _Wyjscie) -> None:
    while kawalek := await strumien.read(ROZMIAR_KAWALKA):
        wyjscie.skrot.update(kawalek)
        wyjscie.ogon = (wyjscie.ogon + kawalek)[-LIMIT_OGONA:]
        if wyjscie.pelne is not None and not wyjscie.przepelnione:
            wyjscie.pelne += kawalek
            if len(wyjscie.pelne) > LIMIT_RAPORTU:
                wyjscie.przepelnione = True
                wyjscie.pelne.clear()
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
    return WynikKroku(status="nieudany", powod=f"check {capability}: {powod}", kod_wyjscia=kod)


async def wykonaj_check(
    worktree: Path, capability: str, komenda: str, katalog_kroku: Path,
    *, limit_s: float = LIMIT_CHECKU_S,
) -> WynikKroku:
    """Uruchamia `komenda` w `worktree` i zapisuje `dowod-<capability>.json` w `katalog_kroku`.

    Komenda nieczytelna, katalog kroku w repozytorium i blad startu to porazka bez pliku
    dowodu; kazdy zakonczony proces (kod 0, kod != 0, pusty wynik, limit czasu) zostawia dowod.
    Tresci komendy nie ma w komunikatach."""
    wynik, _ = await _wykonaj_komende(
        worktree, capability, komenda, katalog_kroku, limit_s, pelne=False)
    return wynik


async def _wykonaj_komende(
    worktree: Path, capability: str, komenda: str, katalog_kroku: Path, limit_s: float,
    *, pelne: bool,
) -> tuple[WynikKroku, _Wyjscie | None]:
    """Wspolny rdzen checku: wynik kroku i wyjscie komendy (`None`, gdy komenda nie ruszyla).

    Z `pelne` czytnik zachowuje cale wyjscie (do limitu `LIMIT_RAPORTU`), nie tylko ogon."""
    try:
        argv = shlex.split(komenda)
    except ValueError:
        # mutant None jest rownowazny (nastepna linia `if not argv:` traktuje None i [] tak samo)
        argv = []  # pragma: no mutate
    if not argv:
        return _porazka(capability, "komenda nieczytelna", None), None
    naruszenie = sprawdz_katalog_runu(katalog_kroku, worktree)
    if naruszenie is not None:
        return WynikKroku(status="nieudany", powod=str(naruszenie), kod_wyjscia=None), None

    start = datetime.now(timezone.utc).isoformat()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=worktree, start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as blad:
        return _porazka(
            capability, f"nie uruchomiono komendy ({type(blad).__name__})", None), None
    wyjscie = _Wyjscie(pelne=bytearray() if pelne else None)
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
        return _porazka(capability, f"nie zapisano dowodu ({type(blad).__name__})", kod), None
    dowod = {**metadane, "plik": plik}

    if przekroczony:
        return WynikKroku(
            status="nieudany",
            powod=f"check {capability}: przekroczony limit czasu {limit_s:g} s",
            kod_wyjscia=None, dowod=dowod), wyjscie
    if kod != 0:
        return WynikKroku(
            status="nieudany", powod=f"check {capability}: kod wyjścia {kod}", kod_wyjscia=kod,
            dowod=dowod), wyjscie
    if not wyjscie.ma_tresc:
        # kod 0 bez zadnego wyniku to brak dowodu, nie zielony check
        brak = dowod_z_komendy("")
        return WynikKroku(
            status="nieudany", powod=f"check {capability}: {brak}", kod_wyjscia=kod,
            dowod=dowod), wyjscie
    return WynikKroku(status="zakonczony", powod=None, kod_wyjscia=kod, dowod=dowod), wyjscie


# --- capability mutate_diff (HAR-168) ------------------------------------------------------------


class BladRaportuMutantow(BladDziedziny):
    """Wyjscie komendy `mutate_diff` nie jest raportem o znanym ksztalcie; tekst to powod."""


class BladZmienionychLinii(BladDziedziny):
    """`zmienione-linie.json` z kroku `diff` nieczytelny albo o nieznanym ksztalcie."""


def _calkowita(wartosc: object, minimum: int) -> bool:
    # `type is int`: bool (podklasa int) nie jest liczba w raporcie
    return type(wartosc) is int and wartosc >= minimum


def _przezyly(wpis: object, numer: int) -> dict:
    if not isinstance(wpis, dict) or set(wpis) != KLUCZE_PRZEZYLEGO:
        raise BladRaportuMutantow(f"przezyle[{numer}]: oczekiwano obiektu z kluczami plik, linia, opis_mutanta")
    plik, linia, opis = wpis["plik"], wpis["linia"], wpis["opis_mutanta"]
    if not isinstance(plik, str) or not plik:
        raise BladRaportuMutantow(f"przezyle[{numer}].plik: oczekiwano niepustego napisu")
    if not _calkowita(linia, 1):
        raise BladRaportuMutantow(f"przezyle[{numer}].linia: oczekiwano liczby calkowitej >= 1")
    if not isinstance(opis, str):
        raise BladRaportuMutantow(f"przezyle[{numer}].opis_mutanta: oczekiwano napisu")
    return {"plik": plik, "linia": linia, "opis_mutanta": opis}


def _poza_zasiegiem(wpis: object, numer: int) -> dict:
    if not isinstance(wpis, dict) or set(wpis) != KLUCZE_POZA_ZASIEGIEM:
        raise BladRaportuMutantow(f"poza_zasiegiem[{numer}]: oczekiwano obiektu z kluczami plik, linia")
    plik, linia = wpis["plik"], wpis["linia"]
    if not isinstance(plik, str) or not plik:
        raise BladRaportuMutantow(f"poza_zasiegiem[{numer}].plik: oczekiwano niepustego napisu")
    if not _calkowita(linia, 1):
        raise BladRaportuMutantow(f"poza_zasiegiem[{numer}].linia: oczekiwano liczby calkowitej >= 1")
    return {"plik": plik, "linia": linia}


def _sprawdz_raport(raport: object) -> dict:
    if (
        not isinstance(raport, dict)
        or not KLUCZE_RAPORTU <= set(raport) <= KLUCZE_RAPORTU | {KLUCZ_POZA_ZASIEGIEM}
    ):
        raise BladRaportuMutantow(
            "raport mutantow: oczekiwano obiektu z kluczami zbadane, przezyle, kod_narzedzia "
            "i opcjonalnie poza_zasiegiem")
    if not _calkowita(raport["zbadane"], 0):
        raise BladRaportuMutantow("raport mutantow: zbadane musi byc liczba calkowita >= 0")
    if not _calkowita(raport["kod_narzedzia"], 0) or raport["kod_narzedzia"] not in KODY_NARZEDZIA:
        raise BladRaportuMutantow(
            f"raport mutantow: kod_narzedzia musi byc jednym z {list(KODY_NARZEDZIA)}")
    if not isinstance(raport["przezyle"], list):
        raise BladRaportuMutantow("raport mutantow: przezyle musi byc lista")
    przezyle = [_przezyly(w, i) for i, w in enumerate(raport["przezyle"])]
    surowe = raport.get(KLUCZ_POZA_ZASIEGIEM, [])
    if not isinstance(surowe, list):
        raise BladRaportuMutantow("raport mutantow: poza_zasiegiem musi byc lista")
    poza = [_poza_zasiegiem(w, i) for i, w in enumerate(surowe)]
    # kod 3 to "zmiany poza zasiegiem mutmuta": bez listy linii do sprawdzenia recznie to brak dowodu
    if raport["kod_narzedzia"] == 3 and not poza:
        raise BladRaportuMutantow("raport mutantow: kod_narzedzia 3 wymaga niepustej listy poza_zasiegiem")
    # przy kodach 0 i 1 lista jest dozwolona (kod 1 ma pierwszenstwo przed 3, wiec zmiany poza zasiegiem
    # idą do raportu takze wtedy); `KODY_NARZEDZIA` juz odrzucil kazdy inny kod
    return {
        "zbadane": raport["zbadane"], "przezyle": przezyle, "kod_narzedzia": raport["kod_narzedzia"],
        KLUCZ_POZA_ZASIEGIEM: poza}


def parsuj_raport_mutantow(tekst: str) -> dict:
    """Raport `{"zbadane", "przezyle", "kod_narzedzia"[, "poza_zasiegiem"]}` z wyjscia komendy, z kontrola typow i kluczy.

    Stderr komendy laczy sie ze stdout, wiec raport moze miec przed soba ostrzezenia: szukamy
    obiektu JSON od kazdego `{`, bierzemy pierwszy o poprawnym ksztalcie. Brak takiego to
    `BladRaportuMutantow` z powodem pierwszego obiektu o zlym ksztalcie."""
    dekoder = json.JSONDecoder()
    pierwszy_blad: BladRaportuMutantow | None = None
    poz = tekst.find("{")
    while poz != -1:
        try:
            obiekt, koniec = dekoder.raw_decode(tekst, poz)
        except ValueError:
            poz = tekst.find("{", poz + 1)
            continue
        except RecursionError as blad:
            raise BladRaportuMutantow("raport mutantow: zbyt gleboko zagniezdzony JSON") from blad
        try:
            return _sprawdz_raport(obiekt)
        except BladRaportuMutantow as blad:
            pierwszy_blad = pierwszy_blad or blad
        poz = tekst.find("{", koniec)
    raise pierwszy_blad or BladRaportuMutantow("raport mutantow: w wyjsciu komendy nie ma obiektu JSON")


def _czy_sa_zmienione_linie(plik: Path) -> bool:
    """Czy diff ma choc jeden wpis pliku (`zmienione-linie.json` kroku `diff`); blad to `BladZmienionychLinii`."""
    try:
        tekst = plik.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as blad:
        raise BladZmienionychLinii(f"nie odczytano zmienionych linii ({type(blad).__name__})") from blad
    try:
        wpisy = json.loads(tekst)
    except (ValueError, RecursionError) as blad:
        raise BladZmienionychLinii(f"zmienione linie nie sa JSON-em ({type(blad).__name__})") from blad
    if not isinstance(wpisy, list) or not all(
        isinstance(w, dict) and isinstance(w.get("linie"), list) for w in wpisy
    ):
        raise BladZmienionychLinii("zmienione linie: oczekiwano listy obiektow z lista linie")
    # niepusty diff = niepusta lista wpisow; wpis z pusta lista `linie` (usuniecie pliku, sama zmiana
    # trybu) tez jest zmiana, wiec zero zbadanych mutantow przy nim to brak dowodu, nie zielony wynik
    return bool(wpisy)


async def wykonaj_mutate_diff(
    worktree: Path, komenda: str, katalog_kroku: Path, plik_linii: Path,
    *, fail_on_survivors: bool = False, limit_s: float = LIMIT_MUTATE_DIFF_S,
) -> WynikKroku:
    """Krok `check` z capability `mutate_diff`: raport przezylych mutantow jako dowod.

    Komenda wypisuje raport JSON na stdout; silnik parsuje go z kontrola typow i zapisuje
    `mutanty.json` obok `dowod-mutate_diff.json`. Zero zbadanych mutantow przy niepustym diffie
    (`plik_linii`: zmienione-linie.json) to `evidence_required`; przezyle mutanty oblewaja krok
    tylko z `fail_on_survivors` (lista zostaje w `mutanty.json`)."""
    capability = CAPABILITY_MUTATE_DIFF
    wynik, wyjscie = await _wykonaj_komende(
        worktree, capability, komenda, katalog_kroku, limit_s, pelne=True)
    # nieudany check (kod != 0, limit, pusty wynik, brak startu) zostaje bez zmian i bez mutanty.json
    if wynik.status != "zakonczony" or wyjscie is None or wyjscie.pelne is None:
        return wynik

    def porazka(powod: object) -> WynikKroku:
        return WynikKroku(
            status="nieudany", powod=f"check {capability}: {powod}",
            kod_wyjscia=wynik.kod_wyjscia, dowod=wynik.dowod)

    if wyjscie.przepelnione:
        return porazka(f"raport mutantów większy niż {LIMIT_RAPORTU} bajtów")
    try:
        tekst = wyjscie.pelne.decode("utf-8")
    except UnicodeDecodeError:
        return porazka("raport mutantów nie jest poprawnym UTF-8")
    try:
        raport = parsuj_raport_mutantow(tekst)
    except BladRaportuMutantow as blad:
        return porazka(blad)
    tresc = json.dumps(raport, **OPCJE_JSON).encode("utf-8")
    try:
        await asyncio.to_thread(_zapisz_atomowo, katalog_kroku / PLIK_MUTANTOW, tresc)
    except OSError as blad:
        return porazka(f"nie zapisano raportu mutantów ({type(blad).__name__})")

    if raport["zbadane"] == 0:
        try:
            sa_linie = await asyncio.to_thread(_czy_sa_zmienione_linie, plik_linii)
        except BladZmienionychLinii as blad:
            return porazka(blad)
        if sa_linie:
            brak = Naruszenie(
                Niezmiennik.evidence_required, "zero zbadanych mutantów przy zmienionych liniach")
            return porazka(brak)
    if fail_on_survivors and raport["przezyle"]:
        return porazka(f"przeżyłe mutanty: {len(raport['przezyle'])}")
    return wynik

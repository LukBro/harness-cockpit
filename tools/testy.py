"""pytest z limitem czasu bez postepu (HAR-240).

    python tools/testy.py [--bez-postepu SEKUNDY] [--dotkniete] [argumenty pytesta ...]

Uruchamia `python -m pytest <argumenty>` w NOWEJ grupie procesow (regula `testy-podprocesow`),
przekazuje jego wyjscie na zywo i przerywa caly przebieg, gdy przez `--bez-postepu` sekund
(domyslnie 1200 = 20 min) nie przyszla ani nowa linia wyniku, ani start ani koniec testu.
Limit liczy sie od ostatniego postepu, nie od startu calosci: dlugi, ale idacy przebieg nie jest
przerywany. Start i koniec testow raportuje wtyczka `_testy_zdarzenia` (takze pod xdist),
dzieki czemu po przerwaniu wiadomo, ktore testy byly w toku.

`--dotkniete` (HAR-256) dodaje `--testmon`: po zmianie kodu leca tylko testy, ktore go dotykaja.
Wypisuje `wybrane: N` i `pominiete: M`. Baza testmona lezy POZA repo (TESTMON_DATAFILE, domyslnie
~/.cache/cockpit-testy/<skrot korzenia>/.testmondata); odcisk plikow-wyzwalaczy obok niej. Pelny przebieg
(i odbudowa bazy) wymuszaja: brak/uszkodzona baza, brak odcisku, zmiana conftest.py, pyproject.toml,
alembic/, .cockpit/, deploy/, web/openapi.json albo wersji zaleznosci. Zero wybranych testow przy
niepustym diffie .py (`git diff --name-only HEAD`) to blad (kod 2, "brak dowodu"), nie zielony wynik.
OGRANICZENIE: testmon sledzi tylko kod importowany w procesie pytesta; `tools/*.py` wywolywane
podprocesem (testy narzedzi) sa dla niego niewidoczne - zmiana skryptu nie wybierze jego testow.

Kody wyjscia: kod pytesta, gdy przebieg sie zakonczyl sam; 2 przy zawieszeniu (stdout:
`zawieszenie: <node id>`, po jednej linii na test w toku) albo bledzie narzedzia (stderr);
143 po SIGTERM do skryptu. Pytest zabity sygnalem (brak kodu) to blad narzedzia, nie zielony wynik.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
LIMIT_DOMYSLNY = 1200.0
KROK_SPRAWDZANIA = 0.1
CZEKANIE_PO_TERM = 5.0
KOD_ZAWIESZENIA = 2
KOD_BLEDU = 2
WTYCZKA = "_testy_zdarzenia"
ZMIENNA_ZDARZEN = "COCKPIT_TESTY_ZDARZENIA"


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


def _limit(tekst: str) -> float:
    try:
        wartosc = float(tekst)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--bez-postepu musi byc liczba sekund (jest: {tekst!r})") from None
    if not wartosc > 0 or wartosc == float("inf"):
        raise argparse.ArgumentTypeError(f"--bez-postepu musi byc skonczona liczba > 0 (jest: {tekst!r})")
    return wartosc


class _Stan:
    """Wspolny stan postepu: czas ostatniego postepu i testy w toku."""

    def __init__(self) -> None:
        self.ostatni_postep = time.monotonic()
        self.w_toku: dict[str, None] = {}
        self.blokada = threading.Lock()

    def postep(self) -> None:
        with self.blokada:
            self.ostatni_postep = time.monotonic()

    def bez_postepu_od(self) -> float:
        with self.blokada:
            return time.monotonic() - self.ostatni_postep

    def zdarzenie(self, znak: str, nodeid: str) -> None:
        with self.blokada:
            if znak == "S":
                self.w_toku[nodeid] = None
            else:
                self.w_toku.pop(nodeid, None)
            self.ostatni_postep = time.monotonic()

    def testy_w_toku(self) -> list[str]:
        with self.blokada:
            return list(self.w_toku)


def _czytaj_wyjscie(
    strumien: object, stan: _Stan, ogon: list[str] | None = None, dezaktywacje: list[str] | None = None
) -> None:
    for surowa in strumien:  # type: ignore[attr-defined]
        sys.stdout.buffer.write(surowa)
        sys.stdout.buffer.flush()
        if ogon is not None:
            linia = surowa.decode("utf-8", errors="replace")
            ogon.append(linia)
            del ogon[:-40]
            # naglowek testmona wypada z ogona w dlugim przebiegu, wiec komunikat o dezaktywacji lapiemy w locie
            trafienie = DEZAKTYWACJA_TESTMONA.search(linia)
            if trafienie and dezaktywacje is not None and not dezaktywacje:
                dezaktywacje.append(trafienie.group(0))
        stan.postep()


def _wczytaj_zdarzenia(sciezka: Path, przesuniecie: int, stan: _Stan) -> int:
    """Czyta nowe, KOMPLETNE linie pliku zdarzen; zwraca nowe przesuniecie."""
    with open(sciezka, "rb") as plik:
        plik.seek(przesuniecie)
        dane = plik.read()
    koniec = dane.rfind(b"\n")
    if koniec < 0:
        return przesuniecie
    for linia in dane[: koniec + 1].decode("utf-8", errors="replace").splitlines():
        znak, _, nodeid = linia.partition(" ")
        if znak in ("S", "K") and nodeid:
            stan.zdarzenie(znak, nodeid)
    return przesuniecie + koniec + 1


def _zabij_grupe(proc: subprocess.Popen, sygnal: int) -> None:
    try:
        os.killpg(proc.pid, sygnal)
    except ProcessLookupError:
        pass


def _zakoncz_grupe(proc: subprocess.Popen) -> None:
    """SIGTERM do WLASNEJ grupy pytesta, po `CZEKANIE_PO_TERM` SIGKILL; potem koniec lidera."""
    _zabij_grupe(proc, signal.SIGTERM)
    try:
        proc.wait(CZEKANIE_PO_TERM)
    except subprocess.TimeoutExpired:
        pass
    _zabij_grupe(proc, signal.SIGKILL)
    proc.wait()


def _srodowisko(plik_zdarzen: Path) -> dict[str, str]:
    srodowisko = dict(os.environ)
    narzedzia = str(Path(__file__).resolve().parent)
    stare = srodowisko.get("PYTHONPATH")
    srodowisko["PYTHONPATH"] = os.pathsep.join([narzedzia, *([stare] if stare else [])])
    srodowisko[ZMIENNA_ZDARZEN] = str(plik_zdarzen)
    return srodowisko


def uruchom(
    argumenty_pytesta: list[str],
    limit: float,
    cwd: Path,
    ogon: list[str] | None = None,
    dezaktywacje: list[str] | None = None,
) -> int:
    with tempfile.TemporaryDirectory(prefix="cockpit-testy-") as katalog:
        plik_zdarzen = Path(katalog) / "zdarzenia.log"
        plik_zdarzen.touch()
        polecenie = [sys.executable, "-m", "pytest", "-p", WTYCZKA, *argumenty_pytesta]
        try:
            proc = subprocess.Popen(
                polecenie,
                cwd=cwd,
                env=_srodowisko(plik_zdarzen),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as blad:
            raise BladNarzedzia(f"nie udalo sie uruchomic pytesta: {blad}") from blad
        stan = _Stan()
        czytnik = threading.Thread(target=_czytaj_wyjscie, args=(proc.stdout, stan, ogon, dezaktywacje), daemon=True)
        przesuniecie = 0
        try:
            czytnik.start()
            while proc.poll() is None:
                przesuniecie = _wczytaj_zdarzenia(plik_zdarzen, przesuniecie, stan)
                if stan.bez_postepu_od() >= limit:
                    w_toku = stan.testy_w_toku()
                    _zakoncz_grupe(proc)
                    czytnik.join(CZEKANIE_PO_TERM)
                    _wypisz_zawieszenie(w_toku, limit)
                    return KOD_ZAWIESZENIA
                time.sleep(KROK_SPRAWDZANIA)
        except BaseException:
            _zakoncz_grupe(proc)
            raise
        # Potomek, ktory przezyl lidera grupy, trzyma koniec potoku: zabijamy grupe PRZED czekaniem na
        # czytnik, a samo czekanie ma limit - skrypt lapiacy zawieszenia nie moze sam sie zawiesic.
        _zabij_grupe(proc, signal.SIGKILL)
        czytnik.join(CZEKANIE_PO_TERM)
        if proc.returncode < 0:
            raise BladNarzedzia(f"pytest zabity sygnalem {-proc.returncode} - brak wyniku testow")
        return proc.returncode


def _wypisz_zawieszenie(w_toku: list[str], limit: float) -> None:
    print(f"testy.py: brak postepu przez {limit:g} s - przerywam przebieg", flush=True)
    if not w_toku:
        print("zawieszenie: brak testu w toku (zbieranie testow albo fixture poza testem)", flush=True)
        return
    for nodeid in w_toku:
        print(f"zawieszenie: {nodeid}", flush=True)


# --- tryb --dotkniete (HAR-256) -------------------------------------------------------------------

# powod -> pliki; katalogi liczymy rekurencyjnie, wiec nowy plik tez wyzwala pelny przebieg
WYZWALACZE: dict[str, tuple[str, ...]] = {
    "conftest.py": ("conftest.py", "tests"),
    "pyproject.toml": ("pyproject.toml",),
    "alembic": ("alembic",),
    ".cockpit": (".cockpit",),
    "deploy": ("deploy",),
    "web/openapi.json": ("web/openapi.json",),
}


def _blad_katalogu(blad: OSError) -> None:
    raise BladNarzedzia(f"nie mozna przejrzec katalogu wyzwalaczy: {blad}") from blad


def _pliki(korzen: Path, nazwa: str, sciezki: tuple[str, ...]) -> list[Path]:
    wynik: list[Path] = []
    for sciezka in sciezki:
        cel = korzen / sciezka
        try:
            katalog_czy_plik = (cel.is_dir(), cel.is_file())
        except OSError as blad:
            raise BladNarzedzia(f"nie mozna sprawdzic wyzwalacza {cel}: {blad}") from blad
        if katalog_czy_plik[0]:
            for katalog, podkatalogi, pliki in os.walk(cel, onerror=_blad_katalogu):
                podkatalogi[:] = [p for p in podkatalogi if p != "__pycache__"]
                wynik += [Path(katalog) / p for p in pliki if nazwa != "conftest.py" or p == "conftest.py"]
        elif katalog_czy_plik[1]:
            wynik.append(cel)
    return sorted(wynik)


def _odcisk(korzen: Path, wersje: Mapping[str, str] | None) -> dict[str, str]:
    if wersje is None:
        wersje = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata["Name"]}
    odcisk = {}
    for nazwa, sciezki in WYZWALACZE.items():
        skrot = hashlib.sha256()
        for plik in _pliki(korzen, nazwa, sciezki):
            try:
                tresc = plik.read_bytes()
            except OSError as blad:
                raise BladNarzedzia(f"nie mozna odczytac wyzwalacza {plik}: {blad}") from blad
            skrot.update(str(plik.relative_to(korzen)).encode() + b"\0" + tresc + b"\0")
        odcisk[nazwa] = skrot.hexdigest()
    odcisk["zaleznosci"] = hashlib.sha256(json.dumps(sorted(wersje.items())).encode()).hexdigest()
    return odcisk


def plik_odcisku(baza: Path) -> Path:
    return baza.with_name(baza.name + ".odcisk.json")


def zapisz_odcisk(korzen: Path, baza: Path, *, wersje: Mapping[str, str] | None = None) -> None:
    odcisk = _odcisk(korzen, wersje)
    try:
        plik_odcisku(baza).write_text(json.dumps(odcisk), encoding="utf-8")
    except OSError as blad:
        raise BladNarzedzia(f"nie mozna zapisac odcisku wyzwalaczy: {blad}") from blad


def _baza_zdrowa(baza: Path) -> bool:
    try:
        polaczenie = sqlite3.connect(f"file:{baza}?mode=ro", uri=True)
        try:
            return polaczenie.execute("select count(*) from sqlite_master").fetchone()[0] > 0
        finally:
            polaczenie.close()
    except sqlite3.DatabaseError:
        return False


def powod_pelnego_przebiegu(korzen: Path, baza: Path, *, wersje: Mapping[str, str] | None = None) -> str | None:
    """Powod, dla ktorego testmon nie moze wybierac testow (pelny przebieg), albo None."""
    if not baza.is_file():
        return f"brak bazy testmona ({baza})"
    if not _baza_zdrowa(baza):
        return f"baza testmona uszkodzona ({baza})"
    try:
        zapisany = json.loads(plik_odcisku(baza).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        zapisany = None
    if not isinstance(zapisany, dict):
        return "brak odcisku plikow-wyzwalaczy"
    for klucz, skrot in _odcisk(korzen, wersje).items():
        if zapisany.get(klucz) != skrot:
            return f"zmiana: {klucz}"
    return None


# Teksty testmona 2.3.0 (testmon/configure.py, `_formulate_deactivation`): "<co> automatically deactivated because
# <powod>" dla collection i selection. Naglowek ginie pod `-q`, wiec decyzje podejmuje sonda parsera (`_powody_bez_selekcji`);
# ten wzorzec lapie komunikat z wyjscia przebiegu jako siatke dolna.
DEZAKTYWACJA_TESTMONA = re.compile(r"(?:selection|collection) automatically deactivated because [^\n]*")
LIMIT_SONDY = 120.0
ZNACZNIK_SONDY = "SONDA-POWODY:"
# Sonda: TEN SAM parser, ktory zobaczy realny przebieg (`_pytest.config._prepareconfig` rozwija `@plik`, skladnie `-qo x=y`,
# `-o`/`--override-ini` i ini), i ta sama funkcja testmona, ktora decyduje o selekcji. Skrypt nie zgaduje po formie argumentow.
_SKRYPT_SONDY = f"""
import json, sys
from _pytest.config import _prepareconfig
from testmon.configure import _get_noselect_reasons, _get_notestmon_reasons
config = _prepareconfig(sys.argv[1:])
opcje = vars(config.option)
print({ZNACZNIK_SONDY!r} + json.dumps({{
    "noselect": _get_noselect_reasons(opcje),
    "notestmon": _get_notestmon_reasons(opcje),
    "forceselect": bool(opcje.get("testmon_forceselect")),
    "keyword": opcje.get("keyword") or None,
    "markexpr": opcje.get("markexpr") or None,
    "lf": bool(opcje.get("lf")),
    "recznie": [str(a) for a in (opcje.get("file_or_dir") or []) if "::" in str(a)],
}}))
"""
RODZAJE_WYBRANYCH = frozenset({"passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"})
RODZAJE_IGNOROWANE = frozenset({"warning", "warnings", "rerun", "reruns"})


def _liczby_z_podsumowania(ogon: list[str]) -> tuple[int, int | None] | None:
    """(wybrane, pominiete) z ostatniej linii podsumowania pytesta albo None; pominiete None = brak "deselected"
    w podsumowaniu (pod xdist go nie ma), czyli brak pomiaru - NIE zero (measured_or_null)."""
    for linia in reversed(ogon):
        trafienie = re.fullmatch(r"((?:\d+ \w+(?:, )?)+) in [\d.]+s.*", linia.strip("= \n"))
        if trafienie:
            wybrane = 0
            pominiete: int | None = None
            for n, rodzaj in re.findall(r"(\d+) (\w+)", trafienie.group(1)):
                if rodzaj in RODZAJE_WYBRANYCH:
                    wybrane += int(n)
                elif rodzaj == "deselected":
                    pominiete = (pominiete or 0) + int(n)
                elif rodzaj not in RODZAJE_IGNOROWANE:
                    raise BladNarzedzia(f"brak dowodu: nieznany rodzaj w podsumowaniu pytesta: {rodzaj!r}")
            return wybrane, pominiete
    return None


# opcje, ktorych nie przenosimy do zliczania: -n/-q/-p/-v nie zmieniaja zbioru testow, a -n i -q psuly by wyjscie
_BEZ_WARTOSCI_ZLICZANIA = re.compile(r"-q+|-v+|-n\w+|-p\S+|--numprocesses=\S+")
_Z_WARTOSCIA_ZLICZANIA = frozenset({"-n", "-p", "--numprocesses"})


def _argumenty_do_zliczania(argumenty: list[str]) -> list[str]:
    wynik: list[str] = []
    pomin = False
    for argument in argumenty:
        if pomin:
            pomin = False
        elif argument in _Z_WARTOSCIA_ZLICZANIA:
            pomin = True
        elif not _BEZ_WARTOSCI_ZLICZANIA.fullmatch(argument):
            wynik.append(argument)
    return wynik


def _liczba_pominietych(wybrane: int, wlasne: list[str], argumenty: list[str], limit: float, cwd: Path) -> int:
    """pominiete = wszystkie testy (osobny przebieg --collect-only bez selekcji) - wybrane; nigdy zero z braku pomiaru."""
    ogon: list[str] = []
    kod = uruchom(
        ["--testmon-noselect", "--collect-only", "-q", *wlasne, *_argumenty_do_zliczania(argumenty)], limit, cwd, ogon
    )
    wszystkie = None
    brak_testow = False
    for linia in reversed(ogon):
        tekst = linia.strip("= \n")
        trafienie = re.fullmatch(r"(\d+) tests? collected(?: in [\d.]+s)?", tekst)
        if trafienie:
            wszystkie = int(trafienie.group(1))
            break
        if re.fullmatch(r"no tests collected(?: in [\d.]+s)?", tekst):
            wszystkie, brak_testow = 0, True  # projekt bez testow: pytest konczy kodem 5, to dowod, ze calosc = 0
            break
    if kod != (5 if brak_testow else 0) or wszystkie is None or wszystkie < wybrane:
        raise BladNarzedzia(
            f"brak dowodu: nie udalo sie ustalic liczby pominietych (kod {kod}, wszystkie: {wszystkie}, wybrane: {wybrane})"
        )
    return wszystkie - wybrane


def _zmienione_py(cwd: Path) -> list[str]:
    try:
        wynik = subprocess.run(
            ["git", "diff", "--name-only", "-z", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as blad:
        raise BladNarzedzia(f"brak dowodu: nie udalo sie odczytac diffu: {blad}") from blad
    if wynik.returncode != 0:
        raise BladNarzedzia(f"brak dowodu: git diff zakonczyl sie kodem {wynik.returncode}: {wynik.stderr.strip()}")
    return [n for n in wynik.stdout.split("\0") if n.endswith(".py")]


def _powody_bez_selekcji(argumenty_pytesta: list[str], cwd: Path) -> list[str]:
    """Powody, dla ktorych testmon wylaczy selekcje przy tych argumentach (pusta lista = selekcja dziala).

    Decyduje parser pytesta i `testmon.configure._get_noselect_reasons` w podprocesie z `cwd` = korzen (ini, rootdir i
    wtyczki jak w realnym przebiegu); kazdy blad sondy to brak dowodu, nigdy "selekcja dziala"."""
    try:
        wynik = subprocess.run(
            [sys.executable, "-c", _SKRYPT_SONDY, *argumenty_pytesta],
            cwd=cwd,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LIMIT_SONDY,
        )
    except subprocess.TimeoutExpired as blad:
        raise BladNarzedzia(f"brak dowodu: sonda parsera pytesta przekroczyla limit {LIMIT_SONDY:g} s: {blad}") from blad
    except OSError as blad:
        raise BladNarzedzia(f"brak dowodu: nie udalo sie uruchomic sondy parsera pytesta: {blad}") from blad
    if wynik.returncode != 0:
        szczegoly = (wynik.stderr.strip().splitlines() or ["brak komunikatu"])[-1]
        raise BladNarzedzia(f"brak dowodu: sonda parsera pytesta zakonczyla sie kodem {wynik.returncode}: {szczegoly}")
    odpowiedzi = [wiersz[len(ZNACZNIK_SONDY) :] for wiersz in wynik.stdout.splitlines() if wiersz.startswith(ZNACZNIK_SONDY)]
    if len(odpowiedzi) != 1:
        raise BladNarzedzia(f"brak dowodu: sonda parsera pytesta nie podala powodow (odpowiedzi: {len(odpowiedzi)})")
    try:
        werdykt = json.loads(odpowiedzi[0])
    except ValueError as blad:
        raise BladNarzedzia(f"brak dowodu: niezrozumiala odpowiedz sondy parsera pytesta: {odpowiedzi[0]!r}") from blad
    try:
        powody = werdykt["noselect"]
        wylaczony = werdykt["notestmon"]
        forceselect = werdykt["forceselect"]
        keyword = werdykt["keyword"]
        markexpr = werdykt["markexpr"]
        lf = werdykt["lf"]
        recznie = werdykt["recznie"]
    except (KeyError, TypeError) as blad:
        raise BladNarzedzia(f"brak dowodu: niezrozumiala odpowiedz sondy parsera pytesta: {odpowiedzi[0]!r}") from blad
    if (
        not isinstance(powody, list)
        or not all(p is None or isinstance(p, str) for p in powody)
        or not (wylaczony is None or isinstance(wylaczony, str))
        or not isinstance(forceselect, bool)
        or not (keyword is None or isinstance(keyword, str))
        or not (markexpr is None or isinstance(markexpr, str))
        or not isinstance(lf, bool)
        or not isinstance(recznie, list)
        or not all(isinstance(s, str) for s in recznie)
    ):
        raise BladNarzedzia(f"brak dowodu: niezrozumiala odpowiedz sondy parsera pytesta: {odpowiedzi[0]!r}")
    selektory = [
        *([f"-k {keyword}"] if keyword else []),
        *([f"-m {markexpr}"] if markexpr else []),
        *(["--lf"] if lf else []),
        *recznie,
    ]
    # `--no-testmon` daje pusta liste noselect, ale `_get_notestmon_reasons` wylacza testmona calkiem (pelny przebieg)
    if wylaczony:
        raise BladNarzedzia(f"brak dowodu: testmon wylaczony ({wylaczony}) - uruchom bez tych argumentow")
    # `--testmon-forceselect` zwraca [] przed sprawdzeniem selektorow, a selektor pytesta odsiewa testy dalej
    if forceselect and selektory:
        raise BladNarzedzia(
            f"brak dowodu: --testmon-forceselect z selektorem pytesta ({', '.join(selektory)}) - uruchom bez tych argumentow"
        )
    # None to powod bez tekstu (`--testmon-noselect` podany jawnie w argumentach)
    return [p if p is not None else "--testmon-noselect" for p in powody]


def uruchom_dotkniete(argumenty_pytesta: list[str], limit: float, cwd: Path) -> int:
    domyslna = Path.home() / ".cache" / "cockpit-testy" / hashlib.sha1(str(cwd).encode()).hexdigest()[:12] / ".testmondata"
    baza = Path(os.environ.get("TESTMON_DATAFILE") or domyslna).resolve()
    if baza.is_relative_to(cwd.resolve()):
        raise BladNarzedzia(f"baza testmona nie moze lezec w repozytorium ({baza})")
    try:
        baza.parent.mkdir(parents=True, exist_ok=True)
    except OSError as blad:
        raise BladNarzedzia(f"nie mozna utworzyc katalogu bazy testmona {baza.parent}: {blad}") from blad
    os.environ["TESTMON_DATAFILE"] = str(baza)
    # pytest dokleja PYTEST_ADDOPTS, a `-o addopts=` go nie czyści; np. "-m unit" wyłączyłoby selekcję testmona po cichu
    os.environ.pop("PYTEST_ADDOPTS", None)
    powod = powod_pelnego_przebiegu(cwd, baza)
    if powod:
        print(f"testy.py: pelny przebieg - {powod}", flush=True)
        if baza.is_file() and not _baza_zdrowa(baza):
            try:
                baza.unlink()
            except OSError as blad:
                raise BladNarzedzia(f"nie mozna usunac uszkodzonej bazy testmona {baza}: {blad}") from blad
    tryb = "--testmon-noselect" if powod else "--testmon"
    # `-m` z addopts wylacza selekcje testmona (uruchamia wszystko), wiec addopts kasujemy, a e2e pomijamy sciezka.
    # `--ignore` tylko dla istniejacego katalogu: w mini-repo bez niego opcja bylaby bezsensowna.
    wlasne = ["-o", "addopts="]
    if (cwd / "tests" / "e2e").is_dir():
        wlasne.append("--ignore=tests/e2e")
    if not powod:
        # Decyzja "czy testmon wylaczy selekcje" pochodzi z parsera pytesta i testmona (sonda na tych samych argumentach
        # i w tej samej kolejnosci co realny przebieg), nie z dopasowania tekstu argumentow: forma opcji (`-qo`, `-o=`,
        # `@plik`) nie ma znaczenia. Pelny przebieg (`--testmon-noselect`) nie potrzebuje sondy: nic nie selekcjonuje.
        powody = _powody_bez_selekcji([tryb, *wlasne, *argumenty_pytesta], cwd)
        if powody:
            raise BladNarzedzia(f"brak dowodu: testmon wylaczyl selekcje ({powody[0]}) - uruchom bez tych argumentow")
    ogon: list[str] = []
    dezaktywacje: list[str] = []
    kod = uruchom([tryb, *wlasne, *argumenty_pytesta], limit, cwd, ogon, dezaktywacje)
    if not powod and dezaktywacje:
        raise BladNarzedzia(f"brak dowodu: testmon wylaczyl selekcje ({dezaktywacje[0].strip(' ,')})")
    liczby = _liczby_z_podsumowania(ogon)
    if liczby is None:
        if kod not in (0, 5):
            return kod
        if powod:
            raise BladNarzedzia("brak dowodu: pelny przebieg bez podsumowania z liczba testow")
        liczby = (0, None)  # "no tests ran": testmon nie wybral niczego; ocenia go diff ponizej
    wybrane, pominiete = liczby
    if pominiete is None:
        # pelny przebieg niczego nie pomija; przy selekcji brak "deselected" (xdist) trzeba zmierzyc osobno
        pominiete = 0 if powod else _liczba_pominietych(wybrane, wlasne, argumenty_pytesta, limit, cwd)
    print(f"wybrane: {wybrane}\npominiete: {pominiete}", flush=True)
    if kod in (0, 5) and wybrane == 0 and not powod:
        zmienione = _zmienione_py(cwd)
        if zmienione:
            raise BladNarzedzia(f"brak dowodu: zero wybranych testow mimo zmian w .py ({', '.join(zmienione[:5])})")
        kod = 0  # pusty diff .py: nic do sprawdzenia, kod 5 pytesta ("no tests ran") nie jest bledem
    if kod == 0:
        zapisz_odcisk(cwd, baza)
    return kod


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="pytest z limitem czasu bez postepu; pozostale argumenty ida do pytesta",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--bez-postepu",
        type=_limit,
        default=LIMIT_DOMYSLNY,
        metavar="SEKUNDY",
        help=f"limit czasu bez nowej linii wyniku ani startu testu (domyslnie {LIMIT_DOMYSLNY:g})",
    )
    parser.add_argument(
        "--dotkniete",
        action="store_true",
        help="tylko testy dotkniete zmiana (pytest-testmon); testy tools/*.py wywolywane podprocesem sa "
        "dla testmona niewidoczne - zmiana skryptu nie wybierze ich testow",
    )
    argumenty, pytest_argumenty = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    def _na_term(_numer: int, _ramka: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _na_term)
    try:
        wykonaj = uruchom_dotkniete if argumenty.dotkniete else uruchom
        return wykonaj(pytest_argumenty, argumenty.bez_postepu, KORZEN)
    except BladNarzedzia as blad:
        print(f"BLAD: {blad}", file=sys.stderr)
        return KOD_BLEDU
    except OSError as blad:
        # Katalog tymczasowy, plik zdarzen (pelny dysk, brak praw do TMPDIR): brak dowodu, nie czerwony test.
        print(f"BLAD: operacja na pliku nie powiodla sie: {blad}", file=sys.stderr)
        return KOD_BLEDU


if __name__ == "__main__":
    sys.exit(main())

"""Statystyki mutmuta liczone w shardach i scalane w jeden plik (HAR-255).
Podkomendy: `shard` liczy statystyki podzbioru testow i zapisuje dowod `shard-<n>.json`;
`scal` sklada dowody w `mutants/mutmut-stats.json`; `mutmut-run` uruchamia `mutmut run` tak,
ze zamiast "clean tests" sprawdza dowody shardow.
Kody wyjscia: 0 - sukces, 2 - blad narzedzia albo brak dowodu (komunikat `blad: ...` na stderr).
Brak dowodu (brak pliku shardu, inny commit albo zrodla, kod pytesta, brakujacy albo powtorzony
test) to kod 2, nigdy zielony wynik. Shardy zawsze licza statystyki od zera i nie wczytuja
starego pliku statystyk mutmuta.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import process_time
from types import SimpleNamespace
from typing import Any

WZORZEC_PLIKU = re.compile(r"shard-(\d+)\.json")


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


class BladDowodu(BladNarzedzia):
    """Brak dowodu: komunikat zawsze zaczyna sie od `brak dowodu: `."""

    def __init__(self, komunikat: str) -> None:
        super().__init__(f"brak dowodu: {komunikat}")


# --- podzial testow ------------------------------------------------------------------------------


def podziel_testy(testy: Iterable[str], indeks: int, liczba: int) -> list[str]:
    """Co `liczba`-ty test z posortowanej listy bez powtorzen, od pozycji `indeks`."""
    if liczba < 1:
        raise BladNarzedzia(f"liczba shardow musi byc co najmniej 1, jest {liczba}")
    if not 0 <= indeks < liczba:
        raise BladNarzedzia(f"indeks shardu {indeks} poza zakresem 0..{liczba - 1}")
    return sorted(set(testy))[indeks::liczba]


# --- odcisk zrodel i konfiguracji ------------------------------------------------------------------


def _konfiguracja_mutmuta(pyproject: Path) -> dict[str, Any]:
    try:
        with pyproject.open("rb") as plik:
            dane = tomllib.load(plik)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as blad:
        raise BladNarzedzia(f"{pyproject}: nie da sie odczytac konfiguracji: {blad}") from blad
    narzedzia = dane.get("tool")
    mutmut = narzedzia.get("mutmut") if isinstance(narzedzia, dict) else None
    if not isinstance(mutmut, dict):
        raise BladNarzedzia(f"{pyproject}: brak sekcji [tool.mutmut]")
    zrodla = mutmut.get("source_paths")
    if not isinstance(zrodla, list) or not all(isinstance(s, str) for s in zrodla):
        raise BladNarzedzia(f"{pyproject}: [tool.mutmut] source_paths nie jest lista napisow")
    kopiowane = mutmut.get("also_copy", [])
    if not isinstance(kopiowane, list) or not all(isinstance(s, str) for s in kopiowane):
        raise BladNarzedzia(f"{pyproject}: [tool.mutmut] also_copy nie jest lista napisow")
    # Wartosci spoza JSON (np. daty z TOML) zamieniamy na napis, zeby odcisk nie wywrocil sie na typie.
    return json.loads(json.dumps(mutmut, default=str))


def _wpisy_mutmuta(korzen: Path) -> list[str]:
    """`source_paths` i `also_copy` wedlug konfiguracji mutmuta (takze to, co mutmut dokleja sam)."""
    with _w_katalogu(korzen):
        mm = _mutmut(tylko_konfiguracja=True)
        try:
            konfiguracja = mm.config()
            return [str(p) for p in [*konfiguracja.source_paths, *konfiguracja.also_copy]]
        except (OSError, ValueError) as blad_konfiguracji:
            raise BladNarzedzia(f"konfiguracja mutmuta: {blad_konfiguracji}") from blad_konfiguracji


def _pliki(korzen: Path, korzenie: list[str]) -> set[Path]:
    """Pliki objete przez mutmuta (sciezki wzgledem korzenia), bez skompilowanych `__pycache__`.
    Nieczytelny katalog to blad, nie pominiecie: odcisk nigdy nie powstaje z czesci plikow."""

    def zglos(blad_katalogu: OSError) -> None:
        nazwa = blad_katalogu.filename
        wzgledna = Path(nazwa).relative_to(korzen).as_posix() if nazwa else str(korzen)
        raise BladNarzedzia(f"{wzgledna}: nie da sie odczytac: {blad_katalogu}") from blad_katalogu

    wynik: set[Path] = set()
    for wpis in korzenie:
        cel = korzen / wpis
        try:
            cel.stat()
        except FileNotFoundError:
            continue
        except OSError as blad_wpisu:
            raise BladNarzedzia(f"{wpis}: nie da sie odczytac: {blad_wpisu}") from blad_wpisu
        if cel.is_file():
            wynik.add(Path(wpis))
        elif cel.is_dir():
            for katalog, _, nazwy in os.walk(cel, onerror=zglos):
                for nazwa in nazwy:
                    plik = Path(katalog) / nazwa
                    if plik.is_file():
                        sciezka = plik.relative_to(korzen)
                        if "__pycache__" in sciezka.parts:
                            continue
                        wynik.add(sciezka)
    return wynik


def odcisk_zrodel(korzen: Path) -> str:
    """Skrot sciezek i tresci plikow, ktore mutmut kopiuje do `mutants/`: `source_paths` i `also_copy`
    wedlug konfiguracji mutmuta (z plikami dokladanymi przez niego, m.in. pyproject.toml; `tests/` pochodzi
    z `also_copy`, bo mutmut dokleja go sam, mutmut/configuration.py:186), a do tego sekcja `[tool.mutmut]`.
    Nie zalezy od czasu modyfikacji ani od kolejnosci przegladania."""
    konfiguracja = _konfiguracja_mutmuta(korzen / "pyproject.toml")
    korzenie = _wpisy_mutmuta(korzen)
    skrot = hashlib.sha256()
    for sciezka in sorted(_pliki(korzen, korzenie)):
        skrot.update(sciezka.as_posix().encode("utf-8") + b"\0")
        try:
            skrot.update((korzen / sciezka).read_bytes() + b"\0")
        except OSError as blad_odczytu:
            raise BladNarzedzia(f"{sciezka.as_posix()}: nie da sie odczytac: {blad_odczytu}") from blad_odczytu
    skrot.update(json.dumps(konfiguracja, sort_keys=True).encode("utf-8"))
    return skrot.hexdigest()


# --- dowody shardow --------------------------------------------------------------------------------


def _calkowita(wartosc: object) -> bool:
    return isinstance(wartosc, int) and not isinstance(wartosc, bool)


def _liczba(wartosc: object) -> bool:
    return isinstance(wartosc, (int, float)) and not isinstance(wartosc, bool)


def _napis(wartosc: object) -> bool:
    return isinstance(wartosc, str)


def _lista_napisow(wartosc: object) -> bool:
    return isinstance(wartosc, list) and all(isinstance(t, str) for t in wartosc)


def _mapa_napisow(wartosc: object) -> bool:
    return isinstance(wartosc, dict) and all(isinstance(v, str) for v in wartosc.values())


def _mapa_list_napisow(wartosc: object) -> bool:
    return isinstance(wartosc, dict) and all(_lista_napisow(v) for v in wartosc.values())


def _mapa_liczb(wartosc: object) -> bool:
    return isinstance(wartosc, dict) and all(_liczba(v) for v in wartosc.values())


POLA_DOWODU: dict[str, Callable[[object], bool]] = {
    "indeks": _calkowita,
    "liczba_shardow": _calkowita,
    "sha": _napis,
    "odcisk": _napis,
    "testy": _lista_napisow,
    "kod_pytesta": _calkowita,
    "tests_by_mangled_function_name": _mapa_list_napisow,
    "duration_by_test": _mapa_liczb,
    "stats_time": _liczba,
    "function_hashes": _mapa_napisow,
    "function_dependencies": _mapa_list_napisow,
}


def _wczytaj_plik_dowodu(plik: Path) -> dict[str, Any]:
    """Jeden plik `shard-<n>.json`: dowod o poprawnym ksztalcie pol."""
    try:
        dane = json.loads(plik.read_text(encoding="utf-8"))
    # WHY: UnicodeDecodeError i JSONDecodeError to podklasy ValueError; ValueError obejmuje tez
    # liczbe powyzej 4300 cyfr (limit konwersji int w Pythonie 3.11+), ktora inaczej uciekalaby tracebackiem.
    except (OSError, RecursionError, ValueError) as blad:
        raise BladDowodu(f"{plik.name}: nie da sie odczytac: {blad}") from blad
    if not isinstance(dane, dict):
        raise BladDowodu(f"{plik.name}: oczekiwano obiektu JSON")
    for pole, poprawny in POLA_DOWODU.items():
        if pole not in dane:
            raise BladDowodu(f"{plik.name}: brak pola '{pole}'")
        if not poprawny(dane[pole]):
            raise BladDowodu(f"{plik.name}: pole '{pole}' ma zly typ")
    # Czasy i mapa funkcji dotycza tylko testow tego shardu.
    if set(dane["duration_by_test"]) != set(dane["testy"]):
        raise BladDowodu(f"{plik.name}: duration_by_test nie zgadza sie z lista testow")
    testy_z_mapy = {t for lista in dane["tests_by_mangled_function_name"].values() for t in lista}
    if not testy_z_mapy <= set(dane["testy"]):
        raise BladDowodu(f"{plik.name}: tests_by_mangled_function_name wskazuje test spoza listy testow shardu")
    return dane


def wczytaj_dowody(katalog: Path, *, sha: str, odcisk: str) -> list[dict[str, Any]]:
    """Dowody wszystkich shardow wedlug numeru; kazdy brak albo niezgodnosc to `BladDowodu`."""
    try:
        jest_katalogiem = katalog.is_dir()
    except OSError as blad_stat:
        raise BladDowodu(f"katalog dowodow {katalog}: nie da sie sprawdzic: {blad_stat}") from blad_stat
    if not jest_katalogiem:
        raise BladDowodu(f"katalog dowodow {katalog} nie istnieje")
    try:
        pliki = sorted(katalog.iterdir())
    except OSError as blad_listy:
        raise BladDowodu(f"katalog dowodow {katalog}: nie da sie wylistowac: {blad_listy}") from blad_listy
    wczytane: dict[int, dict[str, Any]] = {}
    for plik in pliki:
        if not fnmatch.fnmatchcase(plik.name, "shard-*.json"):
            continue
        dopasowanie = WZORZEC_PLIKU.fullmatch(plik.name)
        if dopasowanie is not None:
            numer = int(dopasowanie.group(1))
            if plik.name != f"shard-{numer}.json":
                raise BladDowodu(f"{plik.name}: nazwa pliku nie jest postaci shard-N.json")
            wczytane[numer] = _wczytaj_plik_dowodu(plik)
        else:
            raise BladDowodu(f"{plik.name}: nazwa pliku nie jest postaci shard-N.json")
    if not wczytane:
        raise BladDowodu(f"katalog dowodow {katalog} nie zawiera plikow shard-N.json")
    kolejne = sorted(wczytane.items())
    liczby = {dowod["liczba_shardow"] for _, dowod in kolejne}
    if len(liczby) != 1:
        raise BladDowodu(f"pliki shardow zglaszaja rozne liczby shardow: {sorted(liczby)}")
    (liczba,) = liczby
    for numer in range(liczba):
        if numer not in wczytane:
            raise BladDowodu(f"brak pliku shardu {numer} z {liczba}")
    for numer, dowod in kolejne:
        nazwa = f"shard-{numer}.json"
        if dowod["indeks"] != numer:
            raise BladDowodu(f"{nazwa}: indeks w pliku to {dowod['indeks']}")
        if not 0 <= numer < liczba:
            raise BladDowodu(f"{nazwa}: indeks {numer} poza zakresem 0..{liczba - 1}")
        if dowod["sha"] != sha:
            raise BladDowodu(f"{nazwa}: sha {dowod['sha']}, a job koncowy ma {sha}")
        if dowod["odcisk"] != odcisk:
            raise BladDowodu(f"{nazwa}: odcisk zrodel {dowod['odcisk']}, a job koncowy ma {odcisk}")
        if dowod["kod_pytesta"] != 0:
            raise BladDowodu(f"{nazwa}: pytest zakonczyl sie kodem {dowod['kod_pytesta']}")
        if not dowod["testy"]:
            raise BladDowodu(f"{nazwa}: shard nie uruchomil zadnego testu")
    return [dowod for _, dowod in kolejne]


def sprawdz_dowody_shardow(
    katalog: Path, *, sha: str, odcisk: str, wszystkie_testy: Iterable[str], testy_zmian: Iterable[str]
) -> None:
    """Dowody pokrywaja dokladnie wszystkie testy joba koncowego, bez duplikatow, i testy zmian."""
    dowody = wczytaj_dowody(katalog, sha=sha, odcisk=odcisk)
    uruchomione = [test for dowod in dowody for test in dowod["testy"]]
    powtorzone = sorted(test for test, ile in Counter(uruchomione).items() if ile > 1)
    if powtorzone:
        raise BladDowodu(f"test uruchomiony w kilku shardach: {', '.join(powtorzone)}")
    suma = set(uruchomione)
    bez_shardu = sorted(set(wszystkie_testy) - suma)
    if bez_shardu:
        raise BladDowodu(f"testy z listy joba koncowego bez shardu: {', '.join(bez_shardu)}")
    spoza_listy = sorted(suma - set(wszystkie_testy))
    if spoza_listy:
        raise BladDowodu(f"shardy uruchomily testy spoza listy joba koncowego: {', '.join(spoza_listy)}")
    bez_dowodu = sorted(set(testy_zmian) - suma)
    if bez_dowodu:
        raise BladDowodu(f"testy zmienionych funkcji bez dowodu: {', '.join(bez_dowodu)}")


# --- zlozenie statystyk i podmiana clean tests -------------------------------------------------------


def zloz_statystyki(dowody: list[dict[str, Any]], *, config_fingerprint: dict[str, str]) -> dict[str, Any]:
    """Zawartosc `mutants/mutmut-stats.json` w ksztalcie, ktory przyjmuje `load_stats` mutmuta 3.8.0."""
    mapa: defaultdict[str, set[str]] = defaultdict(set)
    zaleznosci: defaultdict[str, set[str]] = defaultdict(set)
    czasy: dict[str, float] = {}
    hashe: dict[str, str] = {}
    for dowod in dowody:
        for funkcja, testy in dowod["tests_by_mangled_function_name"].items():
            mapa[funkcja].update(testy)
        for funkcja, wolajacy in dowod["function_dependencies"].items():
            zaleznosci[funkcja].update(wolajacy)
        czasy.update(dowod["duration_by_test"])
        hashe.update(dowod["function_hashes"])
    return {
        "tests_by_mangled_function_name": {k: sorted(v) for k, v in sorted(mapa.items())},
        "duration_by_test": czasy,
        "stats_time": sum(dowod["stats_time"] for dowod in dowody),
        "function_hashes": hashe,
        "function_dependencies": {k: sorted(v) for k, v in sorted(zaleznosci.items())},
        "config_fingerprint": dict(config_fingerprint),
        "watched_file_hashes": {},
        "git_commit": dowody[0]["sha"],
    }


def podmien_clean_tests(runner: Any, *, katalog: Path, sha: str, odcisk: str) -> None:
    """Zastepuje `run_clean_tests` runnera mutmuta sprawdzeniem dowodow shardow (kod 0 po przejsciu)."""

    # Druga zbiorka pytesta w jednym procesie zmienia node id z backslashem (escapuje je drugi raz),
    # wiec wynik pierwszego wolania (mutmut w collect_or_load_stats) zapamietujemy i oddajemy dalej.
    oryginal = runner.list_all_tests
    zapamietany: list[Any] = []

    def list_all_tests_z_bufora() -> Any:
        if not zapamietany:
            zapamietany.append(oryginal())
        return zapamietany[0]

    runner.list_all_tests = list_all_tests_z_bufora

    def clean_tests_z_dowodami(tests: Iterable[str]) -> int:
        sprawdz_dowody_shardow(
            katalog,
            sha=sha,
            odcisk=odcisk,
            wszystkie_testy=runner.list_all_tests().ids,
            testy_zmian=tests,
        )
        return 0
    runner.run_clean_tests = clean_tests_z_dowodami


# --- git i mutmut ----------------------------------------------------------------------------------


def _sha_commita(korzen: Path) -> str:
    try:
        wynik = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=korzen, check=False
        )
    except OSError as blad:
        raise BladDowodu(f"git rev-parse HEAD nie powiodl sie: {blad}") from blad
    if wynik.returncode != 0:
        raise BladDowodu(f"git rev-parse HEAD nie powiodl sie: {wynik.stderr.strip()}")
    return wynik.stdout.strip()


@contextmanager
def _w_katalogu(katalog: Path) -> Iterator[None]:
    """Mutmut czyta konfiguracje i `mutants/` wzgledem katalogu biezacego."""
    poprzedni = Path.cwd()
    try:
        os.chdir(katalog)
    except OSError as blad_katalogu:
        raise BladNarzedzia(f"{katalog}: nie da sie wejsc do katalogu: {blad_katalogu}") from blad_katalogu
    try:
        yield
    finally:
        os.chdir(poprzedni)


def _mutmut(*, tylko_konfiguracja: bool = False) -> SimpleNamespace:
    """Moduly mutmuta 3.8.0 (import dopiero po wejsciu do katalogu projektu)."""
    if tylko_konfiguracja:
        try:
            from mutmut.configuration import config, reset_config
        except ImportError as blad_importu:
            raise BladNarzedzia(
                f"brak mutmuta w interpreterze ({blad_importu}): uruchom interpreterem z mutmut"
            ) from blad_importu
        reset_config()
        return SimpleNamespace(config=config)
    try:
        from mutmut import __main__ as glowny
        from mutmut.configuration import config, reset_config
        from mutmut.mutation.trampoline import set_mutant_under_test
        from mutmut.runners.harness import CollectTestsFailedException
        from mutmut.state import reset_state, state
        from mutmut.workers.isolation import run_in_fork_with_result
    except ImportError as blad_importu:
        raise BladNarzedzia(
            f"brak mutmuta w interpreterze ({blad_importu}): uruchom interpreterem z mutmut"
        ) from blad_importu
    reset_config()
    reset_state()
    return SimpleNamespace(
        glowny=glowny,
        config=config,
        state=state,
        set_mutant_under_test=set_mutant_under_test,
        blad_zebrania_testow=CollectTestsFailedException,
        w_dziecku=run_in_fork_with_result,
    )


def _odcisk_konfiguracji_mutmuta(korzen: Path) -> dict[str, str]:
    """Odcisk konfiguracji liczony przez samego mutmuta: niezmodyfikowany, bo inny uznalby zmiane."""
    with _w_katalogu(korzen):
        try:
            # Import mutmut.__main__ laduje konfiguracje, wiec jej bledy lecą juz z importu.
            mm = _mutmut()
            return mm.config().config_fingerprint()
        except (OSError, ValueError) as blad_konfiguracji:
            raise BladNarzedzia(f"konfiguracja mutmuta: {blad_konfiguracji}") from blad_konfiguracji


def _dowod_shardu(args: argparse.Namespace, sha: str, odcisk: str) -> dict[str, Any]:
    """Statystyki podzbioru testow liczone od zera w `mutants/` (cwd = korzen projektu)."""
    try:
        mm = _mutmut()  # import mutmut.__main__ laduje konfiguracje: jej bledy leca stad
        Path("mutants").mkdir(exist_ok=True)
        mm.set_mutant_under_test("mutant_generation")
        mm.glowny.copy_src_dir()
        mm.glowny.copy_also_copy_files()
        mm.glowny.setup_source_paths()
        mm.glowny.store_lines_covered_by_tests()
        mm.glowny.create_mutants(os.cpu_count() or 4)
    except (OSError, ValueError) as blad_przygotowania:
        raise BladNarzedzia(f"przygotowanie mutants/: {blad_przygotowania}") from blad_przygotowania
    try:
        glebokosc = mm.config().dependency_tracking_depth
    except (OSError, ValueError) as blad_konfiguracji:
        raise BladNarzedzia(f"konfiguracja mutmuta: {blad_konfiguracji}") from blad_konfiguracji
    runner = mm.glowny.get_mutant_runner(1)
    mm.set_mutant_under_test("list_all_tests")
    # Lista testow idzie z forkowanego dziecka: druga zbiorka pytesta w jednym procesie escapuje drugi raz
    # backslashe w node id (`a\nb` -> `a\\nb`), wiec statystyki ponizej musza byc PIERWSZA zbiorka rodzica.
    try:
        wszystkie = sorted(mm.w_dziecku(lambda: sorted(runner.list_all_tests().ids)))
    except (mm.blad_zebrania_testow, ChildProcessError) as blad:
        raise BladNarzedzia("nie udalo sie zebrac listy testow (pytest --collect-only)") from blad
    podzbior = podziel_testy(wszystkie, args.indeks, args.liczba)
    if not podzbior:
        raise BladDowodu(f"shard {args.indeks} z {args.liczba} nie ma zadnego testu")
    mm.set_mutant_under_test("stats")
    os.environ["PY_IGNORE_IMPORTMISMATCH"] = "1"
    os.environ["MUTMUT_DEPENDENCY_DEPTH"] = str(glebokosc)
    start = process_time()
    kod = runner.collect_stats(podzbior)
    czas = process_time() - start
    if kod != 0:
        raise BladDowodu(f"pytest zakonczyl sie kodem {kod}")
    stan = mm.state()
    return {
        "indeks": args.indeks,
        "liczba_shardow": args.liczba,
        "sha": sha,
        "odcisk": odcisk,
        "testy": podzbior,
        "kod_pytesta": kod,
        "tests_by_mangled_function_name": {
            k: sorted(v) for k, v in sorted(stan.tests_by_mangled_function_name.items())
        },
        "duration_by_test": dict(stan.duration_by_test),
        "stats_time": czas,
        "function_hashes": dict(stan.current_function_hashes),
        "function_dependencies": {k: sorted(v) for k, v in sorted(stan.function_dependencies.items())},
    }


# --- podkomendy ------------------------------------------------------------------------------------


def _shard(args: argparse.Namespace) -> int:
    podziel_testy([], args.indeks, args.liczba)  # argumenty sprawdzone, zanim cokolwiek ruszymy
    korzen = args.korzen.absolute()
    katalog = args.katalog_dowodow.absolute()
    sha = _sha_commita(korzen)
    odcisk = odcisk_zrodel(korzen)
    with _w_katalogu(korzen):
        dowod = _dowod_shardu(args, sha, odcisk)
    cel = katalog / f"shard-{args.indeks}.json"
    try:
        katalog.mkdir(parents=True, exist_ok=True)
        cel.write_text(json.dumps(dowod, indent=2), encoding="utf-8")
    except OSError as blad_zapisu:
        raise BladNarzedzia(f"{cel}: nie da sie zapisac: {blad_zapisu}") from blad_zapisu
    print(f"zapisano dowod shardu {args.indeks} z {args.liczba}: {cel}", file=sys.stderr)
    return 0


def _scal(args: argparse.Namespace) -> int:
    korzen = args.korzen.absolute()
    sha = _sha_commita(korzen)
    odcisk = odcisk_zrodel(korzen)
    dowody = wczytaj_dowody(args.katalog_dowodow.absolute(), sha=sha, odcisk=odcisk)
    statystyki = zloz_statystyki(dowody, config_fingerprint=_odcisk_konfiguracji_mutmuta(korzen))
    cel = korzen / "mutants" / "mutmut-stats.json"
    try:
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(json.dumps(statystyki, indent=4), encoding="utf-8")
    except OSError as blad_zapisu:
        raise BladNarzedzia(f"{cel}: nie da sie zapisac: {blad_zapisu}") from blad_zapisu
    print(f"scalono {len(dowody)} shardow w {cel}", file=sys.stderr)
    return 0


def _mutmut_run(args: argparse.Namespace) -> int:
    korzen = args.korzen.absolute()
    katalog = args.katalog_dowodow.absolute()
    sha = _sha_commita(korzen)
    odcisk = odcisk_zrodel(korzen)
    with _w_katalogu(korzen):
        try:
            mm = _mutmut()
        except (OSError, ValueError) as blad_konfiguracji:
            raise BladNarzedzia(f"konfiguracja mutmuta: {blad_konfiguracji}") from blad_konfiguracji
        oryginal = mm.glowny.get_mutant_runner

        def runner_z_dowodami(*argumenty: Any, **nazwane: Any) -> Any:
            runner = oryginal(*argumenty, **nazwane)
            podmien_clean_tests(runner, katalog=katalog, sha=sha, odcisk=odcisk)
            return runner

        mm.glowny.get_mutant_runner = runner_z_dowodami
        try:
            mm.glowny._run(tuple(args.wzorce), None)
        finally:
            mm.glowny.get_mutant_runner = oryginal
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    podkomendy = parser.add_subparsers(dest="podkomenda", required=True)
    for nazwa, funkcja, opis in [
        ("shard", _shard, "statystyki podzbioru testow -> shard-<indeks>.json"),
        ("scal", _scal, "dowody shardow -> mutants/mutmut-stats.json"),
        ("mutmut-run", _mutmut_run, "mutmut run z dowodami zamiast clean tests"),
    ]:
        podparser = podkomendy.add_parser(nazwa, help=opis)
        podparser.set_defaults(funkcja=funkcja)
        podparser.add_argument("--korzen", type=Path, default=Path.cwd(), help="korzen repozytorium")
        podparser.add_argument("--katalog-dowodow", type=Path, required=True, help="katalog plikow shard-N.json")
        if nazwa == "shard":
            podparser.add_argument("--indeks", type=int, required=True, help="numer shardu od 0")
            podparser.add_argument("--liczba", type=int, required=True, help="liczba shardow")
        if nazwa == "mutmut-run":
            podparser.add_argument("wzorce", nargs="+", help="wzorce mutantow mutmuta")
    args = parser.parse_args(argv)
    try:
        return args.funkcja(args)
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

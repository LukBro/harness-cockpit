"""Mutanty zmienionych funkcji przed oddaniem pracy (HAR-133).

Mapuje diff wzgledem bazy na wzorce mutantow mutmuta (tylko zmienione funkcje i
metody), uruchamia na nich mutmuta i wypisuje przezywajace mutanty.
Kody wyjscia: 0 - czysto (albo brak zmienionych funkcji), 1 - sa survivory,
2 - blad narzedzia. Skrypt niczego nie usuwa ani nie czysci cache `mutants/`.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ZNAK = "ǁ"  # separator klasy i metody w nazwach mutantow mutmuta
STATUSY_SURVIVORA = {"survived", "no tests"}
NAGLOWEK_HUNKA = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


def zmienione_linie(diff: str) -> dict[str, set[int]]:
    """Parsuje `git diff -U0`: sciezka nowej strony -> numery zmienionych linii."""
    wynik: dict[str, set[int]] = {}
    plik: str | None = None
    w_naglowku = False
    for linia in diff.splitlines():
        if linia.startswith("diff --git "):
            w_naglowku = True
            plik = None
        elif w_naglowku and linia.startswith("+++ "):
            cel = linia[4:]
            if cel.startswith("b/"):
                plik = cel[2:]
            elif cel == "/dev/null":
                plik = None
            else:
                raise BladNarzedzia(
                    f"naglowek diff bez prefiksu b/: {linia!r} "
                    "(sprawdz diff.noprefix i diff.mnemonicPrefix)"
                )
        elif linia.startswith("@@"):
            w_naglowku = False
            hunk = NAGLOWEK_HUNKA.match(linia)
            if hunk is None or plik is None:
                continue
            start = int(hunk.group(1))
            ile = 1 if hunk.group(2) is None else int(hunk.group(2))
            if ile > 0:
                wynik.setdefault(plik, set()).update(range(start, start + ile))
    return wynik


def modul_ze_sciezki(sciezka: str) -> str:
    """Nazwa modulu jak w `get_mutant_name` mutmuta 3.8."""
    nazwa = sciezka.removesuffix(".py").replace("/", ".")
    nazwa = nazwa.removeprefix("src.")
    return nazwa.removesuffix(".__init__")


def _zakres(wezel: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    start = min([wezel.lineno, *(d.lineno for d in wezel.decorator_list)])
    return start, wezel.end_lineno or wezel.lineno


def wzorce_dla_pliku(sciezka: str, zrodlo: str, linie: set[int]) -> list[str]:
    """Wzorce mutantow funkcji i metod, ktorych zakres przecina zmienione linie."""
    try:
        drzewo = ast.parse(zrodlo, filename=sciezka)
    except (SyntaxError, ValueError) as blad:
        raise BladNarzedzia(f"{sciezka}: nie da sie sparsowac: {blad}") from blad
    modul = modul_ze_sciezki(sciezka)
    funkcje = (ast.FunctionDef, ast.AsyncFunctionDef)
    wynik: set[str] = set()

    def dotkniete(wezel: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        start, koniec = _zakres(wezel)
        return any(start <= n <= koniec for n in linie)

    for wezel in drzewo.body:
        if isinstance(wezel, funkcje):
            if dotkniete(wezel):
                wynik.add(f"{modul}.x_{wezel.name}__mutmut_*")
        elif isinstance(wezel, ast.ClassDef):
            for metoda in wezel.body:
                if isinstance(metoda, funkcje) and dotkniete(metoda):
                    wynik.add(f"{modul}.x{ZNAK}{wezel.name}{ZNAK}{metoda.name}__mutmut_*")
    return sorted(wynik)


def _w_zrodlach(sciezka: str, source_paths: list[str]) -> bool:
    return any(
        sciezka == wpis.rstrip("/") or sciezka.startswith(wpis.rstrip("/") + "/")
        for wpis in source_paths
    )


def wzorce(zmiany: dict[str, set[int]], source_paths: list[str], korzen: Path) -> list[str]:
    """Wzorce mutantow dla zmian w plikach `.py` z `source_paths`."""
    wynik: set[str] = set()
    for sciezka, linie in zmiany.items():
        if not sciezka.endswith(".py") or not _w_zrodlach(sciezka, source_paths):
            continue
        try:
            zrodlo = (korzen / sciezka).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odczytac: {blad}") from blad
        wynik.update(wzorce_dla_pliku(sciezka, zrodlo, linie))
    return sorted(wynik)


def source_paths_z_pyproject(pyproject: Path) -> list[str]:
    """Czyta `[tool.mutmut] source_paths` z pyproject.toml."""
    try:
        with pyproject.open("rb") as plik:
            dane = tomllib.load(plik)
    except (OSError, tomllib.TOMLDecodeError) as blad:
        raise BladNarzedzia(f"{pyproject}: nie da sie odczytac konfiguracji: {blad}") from blad
    narzedzia = dane.get("tool", {})
    if not isinstance(narzedzia, dict):
        raise BladNarzedzia(f"{pyproject}: [tool] nie jest tabela")
    mutmut = narzedzia.get("mutmut", {})
    if not isinstance(mutmut, dict):
        raise BladNarzedzia(f"{pyproject}: [tool.mutmut] nie jest tabela")
    sciezki = mutmut.get("source_paths")
    if not isinstance(sciezki, list) or not all(isinstance(s, str) for s in sciezki):
        raise BladNarzedzia(f"{pyproject}: brak listy [tool.mutmut] source_paths")
    return sciezki


def przezywajace(wyniki: str, wzorce: list[str]) -> list[str]:
    """Klucze z `mutmut results` pasujace do wzorcow o statusie survived / no tests."""
    klucze: set[str] = set()
    for linia in wyniki.splitlines():
        klucz, _, status = linia.strip().rpartition(": ")
        if "__mutmut_" not in klucz or status.strip() not in STATUSY_SURVIVORA:
            continue
        if any(fnmatch.fnmatchcase(klucz, w) for w in wzorce):
            klucze.add(klucz)
    return sorted(klucze)


def uruchom(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Jedyne miejsce uruchamiania procesow (bez powloki)."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie uruchomic {argv[0]}: {blad}") from blad


def _koncowka(wyjscie: subprocess.CompletedProcess[str], ile: int = 20) -> str:
    return "\n".join(f"{wyjscie.stdout}\n{wyjscie.stderr}".strip().splitlines()[-ile:])


def _nowe_pliki(wyjscie: str, korzen: Path) -> dict[str, set[int]]:
    """Kazda linia nowego nieSledzonego pliku liczy sie jako zmieniona."""
    zmiany: dict[str, set[int]] = {}
    for sciezka in wyjscie.splitlines():
        sciezka = sciezka.strip()
        if not sciezka.endswith(".py"):
            continue
        try:
            tresc = (korzen / sciezka).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odczytac: {blad}") from blad
        zmiany[sciezka] = set(range(1, len(tresc.splitlines()) + 1))
    return zmiany


def _przebieg(baza: str, korzen: Path) -> int:
    source_paths = source_paths_z_pyproject(korzen / "pyproject.toml")

    merge = uruchom(["git", "merge-base", baza, "HEAD"])
    if merge.returncode != 0:
        raise BladNarzedzia(f"nieznana baza {baza!r}: {merge.stderr.strip()}")
    sha = merge.stdout.strip()

    roznica = uruchom(
        [
            "git",
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            sha,
            "--",
            *source_paths,
        ]
    )
    if roznica.returncode != 0:
        raise BladNarzedzia(f"git diff nie powiodl sie: {roznica.stderr.strip()}")
    nowe = uruchom(["git", "ls-files", "--others", "--exclude-standard", "--", *source_paths])
    if nowe.returncode != 0:
        raise BladNarzedzia(f"git ls-files nie powiodl sie: {nowe.stderr.strip()}")

    zmiany = zmienione_linie(roznica.stdout)
    for sciezka, linie in _nowe_pliki(nowe.stdout, korzen).items():
        zmiany.setdefault(sciezka, set()).update(linie)

    lista = wzorce(zmiany, source_paths, korzen)
    if not lista:
        print("brak zmienionych funkcji - nie ma czego mutowac")
        return 0

    bieg = uruchom([sys.executable, "-m", "mutmut", "run", *lista])
    if bieg.returncode != 0:
        if "nothing matches" in f"{bieg.stdout}\n{bieg.stderr}":
            print("mutmut nie wygenerowal mutantow dla zmienionych funkcji")
            return 0
        raise BladNarzedzia(f"mutmut run zakonczyl sie bledem:\n{_koncowka(bieg)}")

    wyniki = uruchom([sys.executable, "-m", "mutmut", "results"])
    if wyniki.returncode != 0:
        raise BladNarzedzia(f"mutmut results zakonczyl sie bledem:\n{_koncowka(wyniki)}")

    survivory = przezywajace(wyniki.stdout, lista)
    if not survivory:
        print("brak przezywajacych mutantow w zmienionych funkcjach", file=sys.stderr)
        return 0
    for klucz in survivory:
        print(klucz)
    print(
        "Przezywajace mutanty: `python -m mutmut show <klucz>` pokazuje mutacje.\n"
        "Uwaga: skrypt nie czysci cache `mutants/`; po zmianie samych testow wynik moze pochodzic "
        "ze starego przebiegu - uruchom skrypt w swiezym klonie repozytorium w nowym katalogu "
        "tymczasowym (z dokopiowanymi niescommitowanymi zmianami), nie usuwaj `mutants/` w sesji agenta.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baza", default="origin/main", help="baza porownania (merge-base z HEAD)")
    parser.add_argument("--korzen", type=Path, default=Path.cwd(), help="korzen repozytorium")
    args = parser.parse_args(argv)
    try:
        return _przebieg(args.baza, args.korzen)
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

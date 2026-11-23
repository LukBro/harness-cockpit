"""Pelny przebieg mutmuta (mutation-main) w rownoleglych shardach po plikach zrodel.
Podkomendy:
- `wzorce --indeks I --liczba N`: wzorce mutantow plikow shardu I (po jednym w linii) dla `mutmut run`;
- `wyniki --indeks I --liczba N --raport PLIK`: linie `mutmut results --all true` z mutantami shardu I;
- `scal --liczba N --katalog K`: skleja `K/shard-<i>.txt` w jeden raport na stdout.
Kody wyjscia: 0 - sukces, 2 - blad narzedzia albo brak dowodu (komunikat `blad: ...` na stderr).
Brak dowodu: shard bez mutantow, mutant shardu niesprawdzony, brak pliku shardu, mutant w dwoch
shardach. Wtedy kod 2, nigdy zielony wynik.
Pliki dzieli mutmut (`walk_mutatable_files`), nie wlasny parser; przydzial jest zachlanny po liczbie
linii (najwiekszy plik do najlzejszego shardu), wiec shardy sa podobnej dlugosci i stale miedzy runami.
Cache mutmuta nie ma tu sensu: `mutmut run <wzorce>` sprawdza nazwane mutanty od nowa
(mutmut 3.8.0 `__main__.py`: `if not mutant_names and result is not None: continue`).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mutanty_zmian import ZNAK, modul_ze_sciezki
from statystyki_mutmuta import BladDowodu, BladNarzedzia, _mutmut, _w_katalogu, podziel_testy

NIESPRAWDZONY = "not checked"


def przydziel(linie_plikow: dict[str, int], liczba: int) -> list[list[str]]:
    """Pliki shardow: najwiekszy plik idzie do shardu z najmniejsza suma linii (remis: nizszy indeks)."""
    podziel_testy([], 0, liczba)  # ta sama walidacja liczby shardow co w statystykach
    shardy: list[list[str]] = [[] for _ in range(liczba)]
    sumy = [0] * liczba
    for plik in sorted(linie_plikow, key=lambda p: (-linie_plikow[p], p)):
        cel = min(range(liczba), key=lambda i: (sumy[i], i))
        shardy[cel].append(plik)
        sumy[cel] += linie_plikow[plik]
    return shardy


def wzorce(pliki: list[str]) -> list[str]:
    """Wzorce `mutmut run` dla funkcji i metod modulow z listy plikow."""
    return [w for p in pliki for w in (f"{modul_ze_sciezki(p)}.x_*", f"{modul_ze_sciezki(p)}.x{ZNAK}*")]


def _modul_mutanta(klucz: str) -> str:
    return klucz.rsplit(".", 1)[0]


def wyniki_shardu(raport: str, pliki: list[str]) -> list[str]:
    """Linie raportu z mutantami modulow shardu; kazdy musi byc sprawdzony."""
    moduly = {modul_ze_sciezki(p) for p in pliki}
    wynik = []
    for linia in raport.splitlines():
        klucz, _, status = linia.strip().rpartition(": ")
        if "__mutmut_" not in klucz or _modul_mutanta(klucz) not in moduly:
            continue
        if status == NIESPRAWDZONY:
            raise BladDowodu(f"mutant shardu {klucz} nie zostal sprawdzony")
        wynik.append(linia.strip())
    if not wynik:
        raise BladDowodu("shard nie ma zadnego mutanta w raporcie mutmuta")
    return wynik


def scal(katalog: Path, liczba: int) -> list[str]:
    """Linie wszystkich shardow; brak shardu albo mutant w dwoch shardach to brak dowodu."""
    podziel_testy([], 0, liczba)
    widziane: dict[str, int] = {}
    wynik = []
    for indeks in range(liczba):
        plik = katalog / f"shard-{indeks}.txt"
        try:
            tresc = plik.read_text(encoding="utf-8")
        except OSError as blad:
            raise BladDowodu(f"{plik}: brak wynikow shardu {indeks}: {blad}") from blad
        linie = [linia for linia in tresc.splitlines() if linia.strip()]
        if not linie:
            raise BladDowodu(f"{plik}: pusty wynik shardu {indeks}")
        for linia in linie:
            klucz = linia.rpartition(": ")[0]
            if klucz in widziane:
                raise BladDowodu(f"mutant {klucz} w shardach {widziane[klucz]} i {indeks}")
            widziane[klucz] = indeks
            wynik.append(linia)
    return wynik


def _pliki_mutmuta(korzen: Path) -> dict[str, int]:
    """Pliki, ktore mutmut mutuje, z liczba linii (sciezki wzgledem korzenia)."""
    with _w_katalogu(korzen):
        mm = _mutmut(tylko_konfiguracja=True)
        try:
            from mutmut.utils.file_utils import walk_mutatable_files

            mm.config()
            pliki = [Path(p).as_posix() for p in walk_mutatable_files()]
            return {p: len(Path(p).read_text(encoding="utf-8").splitlines()) for p in pliki}
        except (OSError, UnicodeDecodeError, ValueError) as blad:
            raise BladNarzedzia(f"pliki mutmuta: {blad}") from blad


def _pliki_shardu(args: argparse.Namespace) -> list[str]:
    podziel_testy([], args.indeks, args.liczba)  # zly indeks albo liczba: blad, zanim ruszymy mutmuta
    pliki = przydziel(_pliki_mutmuta(args.korzen.absolute()), args.liczba)[args.indeks]
    if not pliki:
        raise BladDowodu(f"shard {args.indeks} z {args.liczba} nie dostal zadnego pliku zrodel")
    return pliki


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    podkomendy = parser.add_subparsers(dest="podkomenda", required=True)
    for nazwa in ("wzorce", "wyniki", "scal"):
        podparser = podkomendy.add_parser(nazwa)
        podparser.add_argument("--liczba", type=int, required=True, help="liczba shardow")
        if nazwa == "scal":
            podparser.add_argument("--katalog", type=Path, required=True, help="katalog plikow shard-N.txt")
            continue
        podparser.add_argument("--indeks", type=int, required=True, help="numer shardu od 0")
        podparser.add_argument("--korzen", type=Path, default=Path.cwd(), help="korzen repozytorium")
        if nazwa == "wyniki":
            podparser.add_argument("--raport", type=Path, required=True, help="wyjscie `mutmut results --all true`")
    args = parser.parse_args(argv)
    try:
        if args.podkomenda == "scal":
            linie = scal(args.katalog, args.liczba)
        elif args.podkomenda == "wzorce":
            linie = wzorce(_pliki_shardu(args))
        else:
            try:
                raport = args.raport.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as blad:
                raise BladNarzedzia(f"{args.raport}: nie da sie odczytac raportu: {blad}") from blad
            linie = wyniki_shardu(raport, _pliki_shardu(args))
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2
    print("\n".join(linie))
    return 0


if __name__ == "__main__":
    sys.exit(main())

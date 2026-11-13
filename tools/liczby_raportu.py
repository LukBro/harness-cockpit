"""Liczby do raportu agenta z pliku i diffu, nie z pamieci (HAR-240, regula `raport-zgodny-z-diffem`).

    python tools/liczby_raportu.py [--ticket HAR-240] [--baza origin/main] [--pliki LISTA]

Dla biezacego drzewa roboczego (zmiany commitowane, niecommitowane i nowe pliki) wzgledem
`--baza` wypisuje markdown do wklejenia w raport:
- liczbe wierszy `[[mutant]]` i `[[rownowazny]]` w `tests/mutanty_reczne/<ticket>.toml`,
- nowe i usuniete testy (node id liczone z drzewa skladni plikow `tests/**/test_*.py`),
- zmienione tabele INNYCH ticketow, kazda z dopiskiem `wymaga STOP:`,
- gdy podano `--pliki`: zmienione pliki spoza listy (sekcja ticketu "Pliki dotykane"), tez `wymaga STOP:`.
`--pliki` to lista sciezek lub wzorcow (fnmatch) rozdzielona przecinkami albo spacjami; wpis konczacy
sie `/` obejmuje caly katalog; tabela biezacego ticketu jest zawsze dozwolona.
`--ticket` domyslnie wynika z nazwy galezi (pierwszy klucz `HAR-<liczba>`).

Kody wyjscia: 0 wypisano liczby, 2 blad (brak dowodu): nieznana baza, git z bledem, brak albo zla
tabela ticketu, nieczytelny plik testowy, nieznany klucz ticketu. Nigdy 1.
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

KORZEN = Path(__file__).resolve().parents[1]
KATALOG_TABEL = "tests/mutanty_reczne"
KLUCZ_TICKETU = re.compile(r"HAR-[0-9]+")
KOD_BLEDU = 2
DOPISEK_STOP = "wymaga STOP:"


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


def _git(argumenty: list[str], korzen: Path) -> bytes:
    # Format wymuszony flagami: konfiguracja uzytkownika (diff.noprefix, renames) nie zmienia wyniku.
    try:
        wynik = subprocess.run(
            ["git", *argumenty], cwd=korzen, capture_output=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as blad:
        raise BladNarzedzia(f"git {' '.join(argumenty)}: {blad}") from blad
    if wynik.returncode != 0:
        komunikat = wynik.stderr.decode("utf-8", errors="replace").strip()
        raise BladNarzedzia(f"git {' '.join(argumenty)} zakonczyl sie kodem {wynik.returncode}: {komunikat}")
    return wynik.stdout


def ticket_z_galezi(korzen: Path) -> str:
    galaz = _git(["branch", "--show-current"], korzen).decode("utf-8").strip()
    trafienie = KLUCZ_TICKETU.search(galaz)
    if trafienie is None:
        raise BladNarzedzia(f"nie ustalono ticketu z galezi {galaz!r} - podaj --ticket HAR-<liczba>")
    return trafienie.group(0)


def zmienione_pliki(korzen: Path, baza: str) -> tuple[list[str], list[str]]:
    """(zmienione albo nowe, usuniete) wzgledem `baza`: drzewo robocze plus pliki nieSledzone."""
    punkt = _git(["merge-base", baza, "HEAD"], korzen).decode("utf-8").strip()
    if not punkt:
        raise BladNarzedzia(f"git merge-base {baza} HEAD nie zwrocil commita")
    roznice = _git(["diff", "--no-ext-diff", "--no-renames", "--name-status", "-z", punkt], korzen)
    pola = roznice.decode("utf-8").split("\0")
    if pola and pola[-1] == "":
        pola.pop()
    if len(pola) % 2 != 0:
        raise BladNarzedzia("niezrozumiale wyjscie git diff --name-status -z")
    zmienione: list[str] = []
    usuniete: list[str] = []
    for status, sciezka in zip(pola[0::2], pola[1::2], strict=True):
        if status == "D":
            usuniete.append(sciezka)
        elif status[:1] in ("A", "M", "T"):
            zmienione.append(sciezka)
        else:
            raise BladNarzedzia(f"nieznany status zmiany {status!r} pliku {sciezka}")
    nowe = _git(["ls-files", "-z", "--others", "--exclude-standard"], korzen).decode("utf-8")
    zmienione.extend(s for s in nowe.split("\0") if s)
    return sorted(set(zmienione)), sorted(set(usuniete))


def _liczby_tabeli(korzen: Path, ticket: str) -> tuple[int, int]:
    sciezka = korzen / KATALOG_TABEL / f"{ticket}.toml"
    try:
        dane = tomllib.loads(sciezka.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as blad:
        raise BladNarzedzia(f"brak albo nieczytelna tabela {sciezka.relative_to(korzen)}: {blad}") from blad
    wiersze = []
    for nazwa in ("mutant", "rownowazny"):
        wartosc = dane.get(nazwa, [])
        if not isinstance(wartosc, list):
            raise BladNarzedzia(f"{sciezka.name}: [{nazwa}] musi byc lista tabel")
        wiersze.append(len(wartosc))
    return wiersze[0], wiersze[1]


def _node_id_testow(zrodlo: str, plik: str) -> set[str]:
    try:
        drzewo = ast.parse(zrodlo)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as blad:
        raise BladNarzedzia(f"{plik}: nie da sie sparsowac pliku testowego: {blad}") from blad
    wynik: set[str] = set()

    def idz(wezly: list[ast.stmt], przedrostek: str) -> None:
        for wezel in wezly:
            if isinstance(wezel, ast.ClassDef) and wezel.name.startswith("Test"):
                idz(wezel.body, f"{przedrostek}::{wezel.name}")
            elif isinstance(wezel, ast.FunctionDef | ast.AsyncFunctionDef) and wezel.name.startswith("test"):
                wynik.add(f"{przedrostek}::{wezel.name}")

    idz(drzewo.body, plik)
    return wynik


def _jest_plikiem_testowym(sciezka: str) -> bool:
    nazwa = sciezka.rsplit("/", 1)[-1]
    return sciezka.startswith("tests/") and nazwa.startswith("test_") and nazwa.endswith(".py")


def _tresc_w_bazie(korzen: Path, punkt: str, plik: str) -> str:
    return _git(["show", f"{punkt}:{plik}"], korzen).decode("utf-8")


def _czy_w_bazie(korzen: Path, punkt: str, plik: str) -> bool:
    # ls-tree: kod 0 i pusty wynik to "pliku nie ma w bazie"; kazdy blad gita to BladNarzedzia z _git.
    return bool(_git(["ls-tree", "--name-only", "-z", punkt, "--", plik], korzen))


def testy_nowe_i_usuniete(korzen: Path, baza: str, zmienione: list[str], usuniete: list[str]) -> tuple[list[str], list[str]]:
    punkt = _git(["merge-base", baza, "HEAD"], korzen).decode("utf-8").strip()
    nowe: set[str] = set()
    znikniete: set[str] = set()
    for plik in zmienione:
        if not _jest_plikiem_testowym(plik):
            continue
        teraz = _node_id_testow((korzen / plik).read_text(encoding="utf-8"), plik)
        przedtem = (
            _node_id_testow(_tresc_w_bazie(korzen, punkt, plik), plik) if _czy_w_bazie(korzen, punkt, plik) else set()
        )
        nowe |= teraz - przedtem
        znikniete |= przedtem - teraz
    for plik in usuniete:
        if _jest_plikiem_testowym(plik):
            znikniete |= _node_id_testow(_tresc_w_bazie(korzen, punkt, plik), plik)
    return sorted(nowe), sorted(znikniete)


def cudze_tabele(zmienione: list[str], usuniete: list[str], ticket: str) -> list[str]:
    wlasna = f"{KATALOG_TABEL}/{ticket}.toml"
    return sorted(
        s
        for s in [*zmienione, *usuniete]
        if s.startswith(f"{KATALOG_TABEL}/") and s.endswith(".toml") and s != wlasna
    )


def wzorce_z_listy(lista: str) -> list[str]:
    wzorce = [w for w in re.split(r"[,\s]+", lista) if w]
    if not wzorce:
        raise BladNarzedzia("--pliki: lista jest pusta")
    return wzorce


def _dozwolony(sciezka: str, wzorce: list[str]) -> bool:
    for wzorzec in wzorce:
        if wzorzec.endswith("/"):
            if sciezka.startswith(wzorzec):
                return True
        elif sciezka == wzorzec or fnmatch.fnmatchcase(sciezka, wzorzec):
            return True
    return False


def pliki_spoza_listy(zmienione: list[str], usuniete: list[str], wzorce: list[str], ticket: str) -> list[str]:
    wlasna = f"{KATALOG_TABEL}/{ticket}.toml"
    return sorted(s for s in [*zmienione, *usuniete] if s != wlasna and not _dozwolony(s, wzorce))


def _lista(naglowek: str, pozycje: list[str], dopisek: str = "") -> list[str]:
    linie = [f"**{naglowek}:** {len(pozycje)}"]
    linie.extend(f"- `{p}`{dopisek}" for p in pozycje)
    return linie


def raport(korzen: Path, ticket: str, baza: str, wzorce: list[str] | None) -> str:
    if not KLUCZ_TICKETU.fullmatch(ticket):
        raise BladNarzedzia(f"zly klucz ticketu {ticket!r} (oczekiwano HAR-<liczba>)")
    mutanty, rownowazne = _liczby_tabeli(korzen, ticket)
    zmienione, usuniete = zmienione_pliki(korzen, baza)
    nowe, znikniete = testy_nowe_i_usuniete(korzen, baza, zmienione, usuniete)
    linie = [f"## Liczby z pliku i diffu ({ticket}, baza {baza})", ""]
    linie.append(f"- wiersze `[[mutant]]` w `{KATALOG_TABEL}/{ticket}.toml`: {mutanty}")
    linie.append(f"- wiersze `[[rownowazny]]` w tej tabeli: {rownowazne}")
    linie.append("")
    linie.extend(_lista("Nowe testy", nowe))
    linie.append("")
    linie.extend(_lista("Usuniete testy", znikniete))
    linie.append("")
    linie.extend(_lista("Zmienione tabele innych ticketow", cudze_tabele(zmienione, usuniete, ticket), f" - {DOPISEK_STOP}"))
    if wzorce is not None:
        linie.append("")
        linie.extend(
            _lista(
                "Zmienione pliki spoza listy Pliki dotykane",
                pliki_spoza_listy(zmienione, usuniete, wzorce, ticket),
                f" - {DOPISEK_STOP}",
            )
        )
    return "\n".join(linie)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="liczby do raportu z pliku i diffu", allow_abbrev=False)
    parser.add_argument("--ticket", default=None, help="klucz ticketu (domyslnie z nazwy galezi)")
    parser.add_argument("--baza", default="origin/main", help="ref bazowy diffu (domyslnie origin/main)")
    parser.add_argument("--pliki", default=None, help="lista plikow z sekcji Pliki dotykane (przecinki albo spacje)")
    parser.add_argument("--korzen", type=Path, default=KORZEN, help="korzen repozytorium (domyslnie ten skrypt)")
    argumenty = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        ticket = argumenty.ticket if argumenty.ticket is not None else ticket_z_galezi(argumenty.korzen)
        wzorce = wzorce_z_listy(argumenty.pliki) if argumenty.pliki is not None else None
        print(raport(argumenty.korzen, ticket, argumenty.baza, wzorce))
    except BladNarzedzia as blad:
        print(f"BLAD: {blad}", file=sys.stderr)
        return KOD_BLEDU
    except (OSError, UnicodeDecodeError) as blad:
        print(f"BLAD: nieczytelny plik: {blad}", file=sys.stderr)
        return KOD_BLEDU
    return 0


if __name__ == "__main__":
    sys.exit(main())

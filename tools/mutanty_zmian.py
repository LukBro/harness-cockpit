"""Mutanty zmienionych funkcji przed oddaniem pracy (HAR-133).

Mapuje diff wzgledem bazy na wzorce mutantow mutmuta (tylko zmienione funkcje i
metody), uruchamia na nich mutmuta i wypisuje przezywajace mutanty.
Kody wyjscia: 0 - wszystkie mutanty zmienionych funkcji zabite (albo brak zmienionych
funkcji) i brak zmian poza zasiegiem mutmuta, 1 - sa survivory, 2 - blad narzedzia,
3 - zmiany poza zasiegiem mutmuta (lista plik:linia; to nie jest "czysto": dopisz reczny
mutant i test). Poza zasiegiem: pliki `.py` pod `src/` spoza `source_paths` oraz linie
kodu poza funkcjami i metodami w `source_paths` (bez pustych linii, komentarzy,
docstringow i importow), funkcje i metody (takze zagniezdzone, takze w mutowanej funkcji) o
nazwie z `NEVER_MUTATE_FUNCTION_NAMES` mutmuta albo z dekoratorem innym niz pojedynczy goly
staticmethod/classmethod, oraz linie wylaczone pragma `# pragma: no mutate` wedlug
`PragmaVisitor` mutmuta (zbior i nazwy czytane z zainstalowanego mutmuta, nie z kopii).
Priorytet kodow: 2 > 1 > 3 > 0.
Skrypt niczego nie usuwa ani nie czysci cache `mutants/`.

HAR-236 (opcjonalne flagi; bez nich kody i stdout bez zmian):
- `--max-children N` (N >= 1) idzie do `mutmut run --max-children N`.
- `--od-przebiegu KATALOG` (tryb przyrostowy): w KATALOGU lezy `przebieg.json` (katalog
  tymczasowy zadania, nigdy `mutants/` ani repozytorium). Do mutmuta ida tylko funkcje, ktorych
  odcisk (sha256 tekstu z dekoratorami) rozni sie od zapisanego; przezywajace pozostalych biora sie
  z zapisu. Zmiana plikow pod `tests/` (takze nieSledzonych) albo `pyproject.toml` albo
  brak/uszkodzenie zapisu to pelny przebieg. Zapis powstaje po kodzie 0, 1 albo 3, nie po 2.
  Znany kompromis: odcisk obejmuje tekst funkcji, nie jej zaleznosci.
- Na stderr idzie czas faz (`czas fazy <faza>: <X.X> s|brak pomiaru`): wybor-funkcji, statystyki,
  mutanty, wyniki. Czasy statystyk i mutantow sa czytane ze znacznikow w wyjsciu `mutmut run`
  (`Running stats`, potem pierwszy z `Running clean tests` / `Running mutation testing`);
  bez znacznika statystyk ta faza to `brak pomiaru`, a bez zadnego znacznika `mutanty` to caly bieg.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

ZNAK = "ǁ"  # separator klasy i metody w nazwach mutantow mutmuta
STATUSY_SURVIVORA = {"survived", "no tests"}
NAGLOWEK_BEZ_MUTANTOW = "bez mutantów — sprawdź ręcznie"
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
    return sorted(wzorce_z_odciskami_dla_pliku(sciezka, zrodlo, linie))


def wzorce_z_odciskami_dla_pliku(sciezka: str, zrodlo: str, linie: set[int]) -> dict[str, str]:
    """Wzorzec mutantow -> sha256 tekstu funkcji z dekoratorami (HAR-236)."""
    try:
        drzewo = ast.parse(zrodlo, filename=sciezka)
    except (SyntaxError, ValueError) as blad:
        raise BladNarzedzia(f"{sciezka}: nie da sie sparsowac: {blad}") from blad
    modul = modul_ze_sciezki(sciezka)
    funkcje = (ast.FunctionDef, ast.AsyncFunctionDef)
    wynik: dict[str, str] = {}
    fizyczne = zrodlo.splitlines()

    def odcisk(wezel: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        start, koniec = _zakres(wezel)
        tekst = "\n".join(fizyczne[start - 1 : koniec])
        return hashlib.sha256(tekst.encode("utf-8")).hexdigest()

    def dotkniete(wezel: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        start, koniec = _zakres(wezel)
        return any(start <= n <= koniec for n in linie)

    for wezel in drzewo.body:
        if isinstance(wezel, funkcje):
            if _mutowana(wezel) and dotkniete(wezel):
                wynik[f"{modul}.x_{wezel.name}__mutmut_*"] = odcisk(wezel)
        elif isinstance(wezel, ast.ClassDef):
            for metoda in wezel.body:
                if isinstance(metoda, funkcje) and _mutowana(metoda) and dotkniete(metoda):
                    wzorzec = f"{modul}.x{ZNAK}{wezel.name}{ZNAK}{metoda.name}__mutmut_*"
                    wynik[wzorzec] = odcisk(metoda)
    return wynik


def _w_zrodlach(sciezka: str, source_paths: list[str]) -> bool:
    return any(
        sciezka == wpis.rstrip("/") or sciezka.startswith(wpis.rstrip("/") + "/")
        for wpis in source_paths
    )


def wzorce(zmiany: dict[str, set[int]], source_paths: list[str], korzen: Path) -> list[str]:
    """Wzorce mutantow dla zmian w plikach `.py` z `source_paths`."""
    return sorted(wzorce_z_odciskami(zmiany, source_paths, korzen))


def wzorce_z_odciskami(
    zmiany: dict[str, set[int]], source_paths: list[str], korzen: Path
) -> dict[str, str]:
    """Jak `wzorce`, z odciskiem tekstu kazdej funkcji (wzorzec -> sha256)."""
    wynik: dict[str, str] = {}
    for sciezka, linie in zmiany.items():
        if not sciezka.endswith(".py") or not _w_zrodlach(sciezka, source_paths):
            continue
        try:
            zrodlo = (korzen / sciezka).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odczytac: {blad}") from blad
        wynik.update(wzorce_z_odciskami_dla_pliku(sciezka, zrodlo, linie))
    return wynik


def _mutowana(wezel: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if wezel.name in _reguly_mutmuta()[0]:
        return False
    # mutmut 3.8.0 pomija funkcje z dekoratorem, chyba ze jest nim goly staticmethod/classmethod
    # (mutmut/mutation/file_mutation.py, "ignore decorated functions").
    if not wezel.decorator_list:
        return True
    dekorator = wezel.decorator_list[0]
    return (
        len(wezel.decorator_list) == 1
        and isinstance(dekorator, ast.Name)
        and dekorator.id in {"staticmethod", "classmethod"}
    )


def _reguly_mutmuta():
    """Zbior nazw, klasa wizytatora i wyjatek pragm z zainstalowanego mutmuta (zrodlo prawdy)."""
    try:
        from mutmut.mutation.file_mutation import NEVER_MUTATE_FUNCTION_NAMES
        from mutmut.mutation.pragma_handling import PragmaParseError, PragmaVisitor
    except ImportError as blad:
        raise BladNarzedzia(
            f"brak mutmuta w interpreterze ({blad}): uruchom interpreterem z mutmut"
        ) from blad
    return NEVER_MUTATE_FUNCTION_NAMES, PragmaVisitor, PragmaParseError


def _linie_pragmy(zrodlo: str) -> set[int]:
    """Linie, w ktorych mutmut nie mutuje wezlow startujacych (`# pragma: no mutate`).

    Wprost z `PragmaVisitor` mutmuta: `no_mutate_lines | ignore_node_lines`, jak w
    `FileMutationVisitor._should_mutate_node`. Wzorce `do_not_mutate` z konfiguracji sa poza
    tym zbiorem (nie dotycza pragmy).
    """
    _, odwiedzajacy_klasa, blad_pragmy = _reguly_mutmuta()
    try:
        import libcst
    except ImportError as blad:
        raise BladNarzedzia(
            f"brak libcst w interpreterze: uruchom interpreterem z mutmut i libcst ({blad})"
        ) from blad
    odwiedzajacy = odwiedzajacy_klasa("<zrodlo>")
    try:
        libcst.MetadataWrapper(libcst.parse_module(zrodlo)).visit(odwiedzajacy)
    except libcst.ParserSyntaxError as blad:
        raise BladNarzedzia(f"nie da sie sparsowac: {blad}") from blad
    except blad_pragmy as blad:
        raise BladNarzedzia(f"bledna pragma no mutate: {blad}") from blad
    return odwiedzajacy.no_mutate_lines | odwiedzajacy.ignore_node_lines


def linie_bez_mutantow(zrodlo: str, linie: set[int], *, w_zrodlach: bool) -> list[int]:
    """Zmienione linie kodu, dla ktorych mutmut nie ma mutantow (posortowane)."""
    try:
        drzewo = ast.parse(zrodlo)
    except (SyntaxError, ValueError) as blad:
        raise BladNarzedzia(f"nie da sie sparsowac: {blad}") from blad
    funkcje = (ast.FunctionDef, ast.AsyncFunctionDef)
    pominiete: list[tuple[int, int]] = []
    mutowane: list[tuple[int, int]] = []
    nie_mutowane: list[tuple[int, int]] = []

    for wezel in ast.walk(drzewo):
        if isinstance(wezel, funkcje) and not _mutowana(wezel):
            nie_mutowane.append(_zakres(wezel))
        if isinstance(wezel, (ast.Import, ast.ImportFrom)):
            pominiete.append((wezel.lineno, wezel.end_lineno or wezel.lineno))
        if isinstance(wezel, (ast.Module, ast.ClassDef, *funkcje)) and wezel.body:
            pierwsza = wezel.body[0]
            if (
                isinstance(pierwsza, ast.Expr)
                and isinstance(pierwsza.value, ast.Constant)
                and isinstance(pierwsza.value.value, str)
            ):
                pominiete.append((pierwsza.lineno, pierwsza.end_lineno or pierwsza.lineno))
    if w_zrodlach:
        for wezel in drzewo.body:
            if isinstance(wezel, funkcje) and _mutowana(wezel):
                mutowane.append(_zakres(wezel))
            elif isinstance(wezel, ast.ClassDef):
                mutowane.extend(
                    _zakres(m) for m in wezel.body if isinstance(m, funkcje) and _mutowana(m)
                )
    bez_pragmy = _linie_pragmy(zrodlo)

    fizyczne = zrodlo.splitlines()
    wynik: list[int] = []
    for numer in sorted(linie):
        if not 1 <= numer <= len(fizyczne):
            continue
        tekst = fizyczne[numer - 1].strip()
        if not tekst:
            continue
        # sam komentarz nie ma kodu, chyba ze mutmut wpisal te linie do pomijanych (osobna
        # linia `# pragma: no mutate block|start|end`): zmiana takiej linii wylacza mutacje
        # kodu obok, wiec idzie do "sprawdz recznie"
        if tekst.startswith("#") and numer not in bez_pragmy:
            continue
        if any(start <= numer <= koniec for start, koniec in pominiete):
            continue
        w_mutowanej = any(start <= numer <= koniec for start, koniec in mutowane)
        w_nie_mutowanej = any(start <= numer <= koniec for start, koniec in nie_mutowane)
        if w_mutowanej and numer not in bez_pragmy and not w_nie_mutowanej:
            continue
        wynik.append(numer)
    return wynik


def bez_mutantow(zmiany: dict[str, set[int]], source_paths: list[str], korzen: Path) -> list[str]:
    """Wpisy `plik:linia` dla zmian pod `src/`, ktorych mutmut nie mutuje."""
    wpisy: list[tuple[str, int]] = []
    for sciezka, linie in zmiany.items():
        if not sciezka.endswith(".py") or not sciezka.startswith("src/"):
            continue
        try:
            zrodlo = (korzen / sciezka).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odczytac: {blad}") from blad
        try:
            numery = linie_bez_mutantow(
                zrodlo, linie, w_zrodlach=_w_zrodlach(sciezka, source_paths)
            )
        except BladNarzedzia as blad:
            raise BladNarzedzia(f"{sciezka}: {blad}") from blad
        wpisy.extend((sciezka, numer) for numer in numery)
    return [f"{plik}:{numer}" for plik, numer in sorted(wpisy)]


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


def uruchom(
    argv: list[str], na_linie: Callable[[str], None] | None = None
) -> subprocess.CompletedProcess[str]:
    """Jedyne miejsce uruchamiania procesow (bez powloki).

    Z `na_linie` proces jest czytany na biezaco: kazda linia wyjscia (stderr polaczony ze stdout,
    wynik niesie je w `stdout`) trafia do `na_linie`, zanim proces sie skonczy.
    """
    if na_linie is None:
        try:
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as blad:
            raise BladNarzedzia(f"nie da sie uruchomic {argv[0]}: {blad}") from blad
    try:
        proces = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie uruchomic {argv[0]}: {blad}") from blad
    zebrane: list[str] = []
    try:
        while True:
            try:
                linia = proces.stdout.readline()
            except OSError as blad:
                raise BladNarzedzia(f"nie da sie odczytac wyjscia {argv[0]}: {blad}") from blad
            if not linia:
                break
            zebrane.append(linia)
            na_linie(linia)
        kod = proces.wait()
    finally:
        if proces.poll() is None:
            proces.kill()
            proces.wait()
        proces.stdout.close()
    return subprocess.CompletedProcess(argv, kod, stdout="".join(zebrane), stderr="")


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


FAZY = ("wybor-funkcji", "statystyki", "mutanty", "wyniki")
ZNACZNIK_STATYSTYK = "Running stats"
ZNACZNIKI_KONCA_STATYSTYK = ("Running clean tests", "Running mutation testing")
PLIK_ZAPISU = "przebieg.json"
WERSJA_ZAPISU = 1


class _Czasy:
    """Czasy faz; faza bez pomiaru to brak wpisu, nigdy zero (measured_or_null)."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.fazy: dict[str, float] = {}
        self.start_statystyk: float | None = None
        self.koniec_statystyk: float | None = None

    def na_linie(self, linia: str) -> None:
        """Znaczniki faz w wyjsciu `mutmut run` (moga stac w srodku linii)."""
        if self.koniec_statystyk is not None:
            return
        if ZNACZNIK_STATYSTYK in linia:
            self.start_statystyk = time.monotonic()
        elif any(znacznik in linia for znacznik in ZNACZNIKI_KONCA_STATYSTYK):
            self.koniec_statystyk = time.monotonic()

    def zmierz_bieg(self, od: float) -> None:
        """Statystyki: miedzy znacznikami; mutanty: od konca statystyk (bez znacznika: caly bieg)."""
        if self.start_statystyk is not None and self.koniec_statystyk is not None:
            self.fazy["statystyki"] = self.koniec_statystyk - self.start_statystyk
        self.zmierz("mutanty", od if self.koniec_statystyk is None else self.koniec_statystyk)

    def zmierz(self, faza: str, od: float) -> None:
        self.fazy[faza] = time.monotonic() - od

    def wypisz(self) -> None:
        for faza in FAZY:
            sekundy = self.fazy.get(faza)
            wartosc = "brak pomiaru" if sekundy is None else f"{sekundy:.1f} s"
            print(f"czas fazy {faza}: {wartosc}", file=sys.stderr)


def odcisk_testow(korzen: Path) -> str:
    """sha256 sciezek i tresci plikow pod `tests/` (takze nieSledzonych) oraz `pyproject.toml`."""
    lista = uruchom(["git", "ls-files", "-z", "-co", "--exclude-standard", "--", "tests"])
    if lista.returncode != 0:
        raise BladNarzedzia(f"git ls-files tests nie powiodl sie: {lista.stderr.strip()}")
    skrot = hashlib.sha256()
    for sciezka in sorted({s for s in lista.stdout.split("\0") if s} | {"pyproject.toml"}):
        try:
            tresc = (korzen / sciezka).read_bytes()
        except FileNotFoundError:
            continue  # sledzony, ale usuniety z dysku: brak pliku to tez stan (inny odcisk)
        except OSError as blad:
            raise BladNarzedzia(f"{sciezka}: nie da sie odczytac: {blad}") from blad
        skrot.update(sciezka.encode("utf-8") + b"\0" + hashlib.sha256(tresc).digest())
    return skrot.hexdigest()


def _poprawny_zapis(zapis: object) -> bool:
    if not isinstance(zapis, dict):
        return False
    wersja = zapis.get("wersja")
    # JSON `true` == 1 w Pythonie, ale to nie wersja zapisu
    if type(wersja) is not int or wersja != WERSJA_ZAPISU:
        return False
    funkcje = zapis.get("funkcje")
    if not isinstance(zapis.get("odcisk_testow"), str) or not isinstance(funkcje, dict):
        return False
    return all(
        isinstance(wpis, dict)
        and isinstance(wpis.get("odcisk"), str)
        and isinstance(wpis.get("przezywajace"), list)
        and all(isinstance(m, str) for m in wpis["przezywajace"])
        for wpis in funkcje.values()
    )


def wczytaj_zapis(plik: Path) -> dict | None:
    """Zapis przebiegu albo None (brak, nieczytelny, obca wersja = pelny przebieg)."""
    try:
        zapis = json.loads(plik.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # ValueError: tez JSONDecodeError i Unicode
        return None
    return zapis if _poprawny_zapis(zapis) else None


def zapisz_przebieg(plik: Path, odcisk: str, funkcje: dict[str, dict]) -> None:
    """Zapis do pliku tymczasowego obok, potem `os.replace`; `OSError` to blad narzedzia."""
    tresc = json.dumps(
        {"wersja": WERSJA_ZAPISU, "odcisk_testow": odcisk, "funkcje": funkcje},
        ensure_ascii=False,
        sort_keys=True,
    )
    tymczasowy = plik.with_name(f"{plik.name}.{os.getpid()}.tmp")
    try:
        plik.parent.mkdir(parents=True, exist_ok=True)
        tymczasowy.write_text(tresc + "\n", encoding="utf-8")
        os.replace(tymczasowy, plik)
    except OSError as blad:
        try:
            tymczasowy.unlink(missing_ok=True)
        except OSError:  # sprzatanie nie moze przerwac tlumaczenia bledu (np. rodzic to plik)
            pass
        raise BladNarzedzia(f"{plik}: nie da sie zapisac przebiegu: {blad}") from blad


def _wybierz_do_mutowania(
    lista: list[str], odciski: dict[str, str], odcisk_testow_teraz: str, plik: Path
) -> tuple[list[str], dict[str, list[str]]]:
    """(funkcje do zmutowania, przezywajace pozostalych z zapisu); bez waznego zapisu: wszystkie."""
    zapis = wczytaj_zapis(plik)
    if zapis is None or zapis["odcisk_testow"] != odcisk_testow_teraz:
        return lista, {}
    poprzednie = zapis["funkcje"]
    do_mutowania = [w for w in lista if poprzednie.get(w, {}).get("odcisk") != odciski[w]]
    return do_mutowania, {w: poprzednie[w]["przezywajace"] for w in lista if w not in do_mutowania}


def _przebieg(
    baza: str,
    korzen: Path,
    czasy: _Czasy,
    max_children: int | None = None,
    od_przebiegu: Path | None = None,
) -> int:
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
            "src/",
            *source_paths,
        ]
    )
    if roznica.returncode != 0:
        raise BladNarzedzia(f"git diff nie powiodl sie: {roznica.stderr.strip()}")
    nowe = uruchom(["git", "ls-files", "--others", "--exclude-standard", "--", "src/", *source_paths])
    if nowe.returncode != 0:
        raise BladNarzedzia(f"git ls-files nie powiodl sie: {nowe.stderr.strip()}")

    zmiany = zmienione_linie(roznica.stdout)
    for sciezka, linie in _nowe_pliki(nowe.stdout, korzen).items():
        zmiany.setdefault(sciezka, set()).update(linie)

    poza_zasiegiem = bez_mutantow(zmiany, source_paths, korzen)
    odciski = wzorce_z_odciskami(zmiany, source_paths, korzen)
    lista = sorted(odciski)
    do_mutowania, z_zapisu = lista, {}
    plik_zapisu = None if od_przebiegu is None else od_przebiegu / PLIK_ZAPISU
    odcisk_testow_teraz = ""
    if plik_zapisu is not None:
        odcisk_testow_teraz = odcisk_testow(korzen)
        do_mutowania, z_zapisu = _wybierz_do_mutowania(
            lista, odciski, odcisk_testow_teraz, plik_zapisu
        )
    czasy.zmierz("wybor-funkcji", czasy.start)

    nowe_przezywajace: list[str] = []
    if not lista and plik_zapisu is None:
        if not poza_zasiegiem:
            print("brak zmienionych funkcji - nie ma czego mutowac", file=sys.stderr)
        return _zakoncz([], poza_zasiegiem)
    if do_mutowania:
        polecenie = [sys.executable, "-m", "mutmut", "run"]
        if max_children is not None:
            polecenie += ["--max-children", str(max_children)]
        start = time.monotonic()
        bieg = uruchom([*polecenie, *do_mutowania], na_linie=czasy.na_linie)
        czasy.zmierz_bieg(start)
        if bieg.returncode == 0:
            start = time.monotonic()
            wyniki = uruchom([sys.executable, "-m", "mutmut", "results"])
            czasy.zmierz("wyniki", start)
            if wyniki.returncode != 0:
                raise BladNarzedzia(f"mutmut results zakonczyl sie bledem:\n{_koncowka(wyniki)}")
            nowe_przezywajace = przezywajace(wyniki.stdout, do_mutowania)
        elif "nothing matches" in f"{bieg.stdout}\n{bieg.stderr}":
            print("mutmut nie wygenerowal mutantow dla zmienionych funkcji", file=sys.stderr)
        else:
            raise BladNarzedzia(f"mutmut run zakonczyl sie bledem:\n{_koncowka(bieg)}")

    survivory = sorted({*nowe_przezywajace, *(m for ms in z_zapisu.values() for m in ms)})
    if plik_zapisu is not None:
        funkcje = {
            w: {
                "odcisk": odciski[w],
                "przezywajace": z_zapisu[w]
                if w in z_zapisu
                else [m for m in nowe_przezywajace if fnmatch.fnmatchcase(m, w)],
            }
            for w in lista
        }
        zapisz_przebieg(plik_zapisu, odcisk_testow_teraz, funkcje)
    if do_mutowania and not survivory and not poza_zasiegiem:
        print("brak przezywajacych mutantow w zmienionych funkcjach", file=sys.stderr)
    return _zakoncz(survivory, poza_zasiegiem)


def _zakoncz(survivory: list[str], poza_zasiegiem: list[str]) -> int:
    """Wspolne zakonczenie: wypisuje wyniki i zwraca kod (1 > 3 > 0)."""
    if survivory:
        for klucz in survivory:
            print(klucz)
        if poza_zasiegiem:
            print("\n".join([NAGLOWEK_BEZ_MUTANTOW, *poza_zasiegiem]), file=sys.stderr)
        print(
            "Przezywajace mutanty: `python -m mutmut show <klucz>` pokazuje mutacje.\n"
            "Uwaga: skrypt nie czysci cache `mutants/`; po zmianie samych testow wynik moze "
            "pochodzic ze starego przebiegu - uruchom skrypt w swiezym klonie repozytorium w "
            "nowym katalogu tymczasowym (z dokopiowanymi niescommitowanymi zmianami), nie "
            "usuwaj `mutants/` w sesji agenta.",
            file=sys.stderr,
        )
        return 1
    if poza_zasiegiem:
        print("\n".join([NAGLOWEK_BEZ_MUTANTOW, *poza_zasiegiem]))
        print(
            "Mutmut tych zmian nie mutuje: dopisz reczny mutant i test - "
            ".claude/rules/test-odroznia-regule.md",
            file=sys.stderr,
        )
        return 3
    return 0


def _liczba_dzieci(tekst: str) -> int:
    if not (tekst.isascii() and tekst.isdigit()) or int(tekst) < 1:
        raise argparse.ArgumentTypeError(f"oczekiwano liczby calkowitej >= 1, jest {tekst!r}")
    return int(tekst)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baza", default="origin/main", help="baza porownania (merge-base z HEAD)")
    parser.add_argument("--korzen", type=Path, default=Path.cwd(), help="korzen repozytorium")
    parser.add_argument(
        "--max-children", type=_liczba_dzieci, default=None, metavar="N",
        help="liczba rownoleglych procesow mutmuta (mutmut run --max-children N)",
    )
    parser.add_argument(
        "--od-przebiegu", type=Path, default=None, metavar="KATALOG",
        help=f"tryb przyrostowy: katalog z zapisem przebiegu ({PLIK_ZAPISU})",
    )
    args = parser.parse_args(argv)
    czasy = _Czasy()
    try:
        return _przebieg(args.baza, args.korzen, czasy, args.max_children, args.od_przebiegu)
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2
    finally:
        czasy.wypisz()


if __name__ == "__main__":
    sys.exit(main())

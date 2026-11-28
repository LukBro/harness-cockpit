"""Bramki, ktore nie moga zawiesc (HAR-246): ocena komendy checku, jej wyniku i plikow repozytorium.

Ocena tylko czyta: niczego nie uruchamia i nie zapisuje. Sygnal jest ostrzezeniem dla czlowieka
i nie zmienia wyniku kroku. Plik, ktorego nie da sie ocenic, daje sygnal `nie oceniono skryptu`,
nigdy cisze.

shortcut: tylko `sh`/`bash`, skrypty `*.sh` i `package.json` oraz konfiguracja w korzeniu repozytorium;
`Makefile`, `--prefix`, workspaces i `--config` ruffa nie sa czytane - dolozyc, gdy takie bramki sie pojawia.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner.maskowanie import maskuj_komende

LIMIT_CYTATU = 200
LIMIT_PLIKU = 1024 * 1024       # bajtow; wiekszy plik to `nie oceniono skryptu`
LIMIT_ZAGNIEZDZENIA = 5         # skrypt wolajacy skrypt wolajacy skrypt...

BRAK_TESTOW = "brak zebranych testów"
PRZECHODZA_BEZ = "testy przechodzą bez testów"
WYMUSZONE_ZERO = "kod wyjścia wymuszony na zero"
WSZYSTKIE_POMINIETE = "wszystkie testy pominięte"
BLAD_IGNOROWANY = "błąd kroku ignorowany"
ATRAPA_NPM = "atrapa npm init"
BEZ_ASERCJI = "testy bez asercji"
LINT_BEZ_REGUL = "lint bez reguł"
NIE_OCENIONO = "nie oceniono skryptu"

POPRAWKI = {
    BRAK_TESTOW: "wskaż komendzie katalog z testami albo dodaj test, który pytest zbierze.",
    PRZECHODZA_BEZ: "usuń --passWithNoTests, żeby brak testów oblewał bramkę.",
    WYMUSZONE_ZERO: "usuń wymuszenie kodu 0, żeby błąd komendy oblewał bramkę.",
    WSZYSTKIE_POMINIETE: "usuń warunek pomijania albo uruchom testy tam, gdzie mogą się wykonać.",
    BLAD_IGNOROWANY: "usuń continue-on-error: true.",
    ATRAPA_NPM: "zastąp skrypt z npm init prawdziwą komendą testów.",
    BEZ_ASERCJI: "dodaj do testów asercje sprawdzające wynik.",
    LINT_BEZ_REGUL: "włącz w konfiguracji lintera co najmniej jedną regułę.",
    NIE_OCENIONO: "sprawdź ten plik ręcznie albo wskaż czytelny plik wewnątrz repozytorium.",
}

NPM_BEZ_TESTOW = "Error: no test specified"
_NPM = re.compile(re.escape(NPM_BEZ_TESTOW))
_BEZ_TESTOW_PYTEST = re.compile("no tests ran")
# tylko tekst powloki: silnik uruchamia komende bez powloki, wiec tam to zwykle argumenty
_ZERO_W_POWLOCE = re.compile(r"\|\|\s*(?:true|:)(?=\s|$|[;&|)])|;\s*exit\s+0(?!\w)")
_OSTATNIE_EXIT_0 = re.compile(r"\bexit\s+0\s*;?\s*(?:#.*)?\Z")
_LINIA_EXIT_0 = re.compile(r"\s*exit\s+0\s*;?\s*(?:#.*)?")
_IGNOROWANY = re.compile(r"continue-on-error:\s*true\b")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PODSUMOWANIE_PYTEST = re.compile(r"[=\s]*(\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d")
_PODSUMOWANIE_JS = re.compile(r"\s*Tests:?\s+(\d.*)")
_WYKONANE = re.compile(r"\d+ (?:passed|failed|errors?|xpassed|xfailed)\b")
_POMINIETE = re.compile(r"[1-9]\d* skipped\b")

_PRZYPISANIE = re.compile(r"\w+=")
_PYTHON = re.compile(r"python[\d.]*")
_SEPARATOR = frozenset("();<>|&")
_JEZYK_TESTOW = {"pytest": "py", "jest": "js", "vitest": "js"}
_PLIKI_TESTOW = {
    "py": re.compile(r"test_.*\.py|.*_test\.py"),
    "js": re.compile(r".*\.(?:test|spec)\.[cm]?[jt]sx?"),
}
_ASERCJE = ("assert", "expect(", "should")


@dataclass(frozen=True)
class Sygnal:
    nazwa: str
    cytat: str      # fragment komendy, wyjscia albo pliku po maskowaniu, najwyzej LIMIT_CYTATU znakow
    poprawka: str


class _NieOceniono(Exception):
    """Pliku nie da sie przeczytac albo zrozumiec."""


def _cytat(tekst: str, wzorzec: re.Pattern[str] | None = None) -> str:
    """Fragment tekstu z dopasowaniem `wzorzec`. Najpierw maskowanie CALEGO tekstu, potem przyciecie:
    odwrotna kolejnosc odcielaby nazwe zmiennej i zostawila ogon sekretu."""
    czysty = maskuj_komende(tekst)
    trafienie = wzorzec.search(czysty) if wzorzec is not None else None
    poczatek = 0
    if trafienie is not None:
        linia = czysty.rfind("\n", 0, trafienie.start()) + 1
        poczatek = max(linia, trafienie.end() - LIMIT_CYTATU)
    fragment = czysty[poczatek:poczatek + LIMIT_CYTATU]
    # jedna linia bez znakow, ktorych baza i JSON nie przyjma (NUL, samotny surogat)
    fragment = " ".join(fragment.replace("\x00", "?").split())
    return fragment.encode("utf-8", errors="replace").decode("utf-8")


def _czytaj(repo: Path, sciezka: str | Path) -> str:
    """Tekst zwyklego pliku lezacego (po rozwinieciu dowiazan) wewnatrz `repo`."""
    try:
        korzen = Path(os.path.realpath(repo))
        plik = Path(os.path.realpath(korzen / sciezka))
        if not plik.is_relative_to(korzen):
            raise _NieOceniono("poza repozytorium")
        # O_NONBLOCK: otwarcie potoku nazwanego nie czeka na piszacego
        with open(plik, "rb", opener=lambda s, f: os.open(s, f | os.O_NONBLOCK)) as strumien:
            if not stat.S_ISREG(os.fstat(strumien.fileno()).st_mode):
                raise _NieOceniono("to nie zwykly plik")
            dane = strumien.read(LIMIT_PLIKU + 1)
        if len(dane) > LIMIT_PLIKU:
            raise _NieOceniono("za duzy plik")
        return dane.decode("utf-8")
    except (OSError, ValueError) as blad:      # ValueError: bajt NUL w sciezce, nie UTF-8
        raise _NieOceniono(type(blad).__name__) from blad


def _parsuj(parser: Callable[[str], Any], tekst: str) -> Any:
    try:
        return parser(tekst)
    except (ValueError, RecursionError) as blad:
        raise _NieOceniono(type(blad).__name__) from blad


def _slownik(wartosc: object) -> dict:
    if not isinstance(wartosc, dict):
        raise _NieOceniono("zly ksztalt")
    return wartosc


def _program(argv: Sequence[str]) -> tuple[str, list[str]]:
    """(program, argumenty) prostej komendy: bez przypisan zmiennych, `npx` i `python -m`."""
    reszta = list(argv)
    while reszta:
        slowo = reszta[0]
        nazwa = slowo.rsplit("/", 1)[-1]
        if _PRZYPISANIE.match(slowo) or nazwa == "npx":
            del reszta[0]
        elif _PYTHON.fullmatch(nazwa) and "-m" in reszta:
            m = reszta.index("-m")
            if not all(a.startswith("-") for a in reszta[1:m]):
                break       # `-m` nalezy do skryptu, nie do interpretera
            del reszta[:m + 1]
        else:
            break
    return (reszta[0], reszta[1:]) if reszta else ("", [])


def _proste_komendy(linia: str) -> list[list[str]]:
    lekser = shlex.shlex(linia, posix=True, punctuation_chars=True)
    lekser.whitespace_split = True
    try:
        slowa = list(lekser)
    except ValueError:      # cudzyslow przez kilka linii: zostaja slowa rozdzielone spacjami
        slowa = linia.split()
    komendy: list[list[str]] = [[]]
    for slowo in slowa:
        if slowo and set(slowo) <= _SEPARATOR:
            komendy.append([])
        else:
            komendy[-1].append(slowo)
    return [k for k in komendy if k]


class _Ocena:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.sygnaly: dict[str, Sygnal] = {}     # po jednym sygnale kazdej nazwy
        self.jezyki: dict[str, str] = {}         # jezyk testow -> tekst z komenda testowa
        self.pytest = False
        self.ruff: str | None = None
        self._widziane: set[str] = set()

    def dodaj(self, nazwa: str, tekst: str, wzorzec: re.Pattern[str] | None = None) -> None:
        if nazwa not in self.sygnaly:
            self.sygnaly[nazwa] = Sygnal(nazwa, _cytat(tekst, wzorzec), POPRAWKI[nazwa])

    def komenda(self, argv: Sequence[str], tekst: str, glebokosc: int) -> None:
        """Prosta komenda (argv) z tekstu `tekst`; rozwija skrypty, ktore uruchamia."""
        if "--exit-zero" in argv:
            self.dodaj(WYMUSZONE_ZERO, tekst, re.compile("--exit-zero"))
        if "--passWithNoTests" in argv:
            self.dodaj(PRZECHODZA_BEZ, tekst, re.compile("--passWithNoTests"))
        program, argumenty = _program(argv)
        nazwa = program.rsplit("/", 1)[-1]
        if jezyk := _JEZYK_TESTOW.get(nazwa):
            self.jezyki.setdefault(jezyk, tekst)
            self.pytest = self.pytest or jezyk == "py"
        if nazwa == "ruff":
            self.ruff = tekst
        elif nazwa == "eslint":
            if ({"--no-eslintrc", "--no-config-lookup"} & set(argumenty)
                    and not any(a.split("=", 1)[0] == "--rule" for a in argumenty)):
                self.dodaj(LINT_BEZ_REGUL, tekst, re.compile("--no-(?:eslintrc|config-lookup)"))
        elif nazwa in ("sh", "bash"):
            self._sh(argumenty, glebokosc)
        elif nazwa in ("npm", "pnpm", "yarn"):
            slowa = [a for a in argumenty if not a.startswith("-")]
            if slowa[:1] == ["test"]:
                self._skrypt_npm("test", tekst, glebokosc)
            elif slowa[:1] == ["run"] and len(slowa) > 1:
                self._skrypt_npm(slowa[1], tekst, glebokosc)
        elif "/" in program and program.endswith(".sh"):
            self._plik(program, glebokosc)

    def _sh(self, argumenty: Sequence[str], glebokosc: int) -> None:
        z_tekstu = False
        for argument in argumenty:
            if argument.startswith("-"):
                z_tekstu = z_tekstu or (not argument.startswith("--") and "c" in argument)
            else:
                if z_tekstu:
                    self.powloka(argument, glebokosc + 1)
                else:
                    self._plik(argument, glebokosc)
                return

    def _plik(self, sciezka: str, glebokosc: int) -> None:
        try:
            tekst = _czytaj(self.repo, sciezka)
        except _NieOceniono:
            self.dodaj(NIE_OCENIONO, sciezka)
            return
        self.powloka(tekst, glebokosc + 1)

    def _skrypt_npm(self, nazwa: str, tekst: str, glebokosc: int) -> None:
        try:
            pakiet = _slownik(_parsuj(json.loads, _czytaj(self.repo, "package.json")))
            skrypt = _slownik(pakiet.get("scripts")).get(nazwa)
            if not isinstance(skrypt, str):
                raise _NieOceniono("brak skryptu")
        except _NieOceniono:
            self.dodaj(NIE_OCENIONO, f"package.json dla komendy: {tekst}")
            return
        if NPM_BEZ_TESTOW in skrypt:
            self.dodaj(ATRAPA_NPM, skrypt, _NPM)
        self.powloka(skrypt, glebokosc + 1)

    def powloka(self, tekst: str, glebokosc: int) -> None:
        """Tekst powloki: argument `-c`, plik skryptu albo skrypt z package.json."""
        if tekst in self._widziane:
            return
        if glebokosc > LIMIT_ZAGNIEZDZENIA:
            self.dodaj(NIE_OCENIONO, tekst)
            return
        self._widziane.add(tekst)
        linie = [w for w in tekst.split("\n") if w.strip() and not w.lstrip().startswith("#")]
        czysty = "\n".join(linie)
        if _ZERO_W_POWLOCE.search(czysty):
            self.dodaj(WYMUSZONE_ZERO, czysty, _ZERO_W_POWLOCE)
        elif linie and _LINIA_EXIT_0.fullmatch(linie[-1]):
            self.dodaj(WYMUSZONE_ZERO, czysty, _OSTATNIE_EXIT_0)
        if _IGNOROWANY.search(czysty):
            self.dodaj(BLAD_IGNOROWANY, czysty, _IGNOROWANY)
        for linia in czysty.replace("\\\n", " ").split("\n"):
            for argv in _proste_komendy(linia):
                self.komenda(argv, czysty, glebokosc)

    def wyjscie(self, komenda: str, kod_wyjscia: int | None, wyjscie: str) -> None:
        if self.pytest and kod_wyjscia == 5:
            self.dodaj(BRAK_TESTOW, komenda)
        wyjscie = _ANSI.sub("", wyjscie)
        # z wyjscia cytujemy samo dopasowanie: ogon moze zaczynac sie w srodku sekretu
        if _BEZ_TESTOW_PYTEST.search(wyjscie):
            self.dodaj(BRAK_TESTOW, _BEZ_TESTOW_PYTEST.pattern)
        if NPM_BEZ_TESTOW in wyjscie:
            self.dodaj(ATRAPA_NPM, NPM_BEZ_TESTOW)
        podsumowania = [
            m.group(1) for linia in wyjscie.splitlines()
            if (m := _PODSUMOWANIE_PYTEST.match(linia) or _PODSUMOWANIE_JS.fullmatch(linia))]
        if not any(_WYKONANE.search(p) for p in podsumowania):
            for podsumowanie in podsumowania:
                if pominiete := _POMINIETE.search(podsumowanie):
                    self.dodaj(WSZYSTKIE_POMINIETE, pominiete.group())

    def asercje(self, jezyk: str, tekst: str) -> None:
        """Sygnal, gdy sa pliki testow i zaden nie ma asercji; nieczytelny katalog albo plik
        bez znalezionej gdzie indziej asercji to `nie oceniono`, nie `bez asercji`."""
        znalezione: list[str] = []
        nieocenione: list[str] = []

        def blad_katalogu(blad: OSError) -> None:
            nieocenione.append(os.path.relpath(blad.filename, self.repo))

        for katalog, podkatalogi, pliki in os.walk(self.repo, onerror=blad_katalogu):
            # cudze testy (zaleznosci, `.venv`) nie sa testami tej bramki
            podkatalogi[:] = sorted(
                d for d in podkatalogi if not d.startswith(".") and d != "node_modules")
            for nazwa in sorted(pliki):
                if not _PLIKI_TESTOW[jezyk].fullmatch(nazwa):
                    continue
                plik = Path(katalog) / nazwa
                try:
                    tresc = _czytaj(self.repo, plik.relative_to(self.repo))
                except _NieOceniono:
                    nieocenione.append(str(plik.relative_to(self.repo)))
                    continue
                if any(a in tresc for a in _ASERCJE):
                    return
                znalezione.append(str(plik.relative_to(self.repo)))
        if nieocenione:
            self.dodaj(NIE_OCENIONO, nieocenione[0])
        elif znalezione:
            self.dodaj(BEZ_ASERCJI, f"{tekst} (pliki testów: {', '.join(znalezione)})")

    def konfiguracja_ruff(self, tekst: str) -> None:
        """`select = []` w pierwszym istniejacym pliku konfiguracji ruffa z korzenia repozytorium."""
        for nazwa in ("ruff.toml", "pyproject.toml"):
            try:
                os.lstat(self.repo / nazwa)     # `lexists` kazdy OSError uznaje za brak pliku
            except FileNotFoundError:
                continue
            except OSError:
                self.dodaj(NIE_OCENIONO, nazwa)
                return
            try:
                dane = _parsuj(tomllib.loads, _czytaj(self.repo, nazwa))
                if nazwa == "pyproject.toml":
                    dane = _slownik(_slownik(dane.get("tool", {})).get("ruff", {}))
                lint = _slownik(dane.get("lint", {}))
            except _NieOceniono:
                self.dodaj(NIE_OCENIONO, nazwa)
                return
            if [] in (dane.get("select"), lint.get("select")):
                self.dodaj(LINT_BEZ_REGUL, f"{nazwa}: select = [] ({tekst})")
            return


def ocen_bramke(komenda: str, kod_wyjscia: int | None, wyjscie: str, repo: Path) -> list[Sygnal]:
    """Sygnaly, ze bramka `komenda` nie mogla zawiesc; `wyjscie` to ogon wyjscia komendy."""
    ocena = _Ocena(repo)
    if _IGNOROWANY.search(komenda):
        ocena.dodaj(BLAD_IGNOROWANY, komenda, _IGNOROWANY)
    try:
        argv = shlex.split(komenda)
    except ValueError:
        argv = []       # check sam oblewa krok powodem `komenda nieczytelna`
    if argv:
        ocena.komenda(argv, komenda, 0)
    ocena.wyjscie(komenda, kod_wyjscia, wyjscie)
    for jezyk, tekst in ocena.jezyki.items():
        ocena.asercje(jezyk, tekst)
    if ocena.ruff is not None:
        ocena.konfiguracja_ruff(ocena.ruff)
    return list(ocena.sygnaly.values())

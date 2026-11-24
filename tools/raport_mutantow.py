"""Raport przezylych mutantow zmienionych linii jako JSON na stdout (HAR-168, capability mutate_diff).

Cienka nakladka na `tools/mutanty_zmian.py` i mutmuta, wolana przez krok `system: check`
z `capability: mutate_diff` (komenda w `.cockpit/project.yaml`). Kolejnosc:
1. `python tools/mutanty_zmian.py --baza HEAD` - mutuje zmienione funkcje; kod 0, 1 i 3 to wynik
   (1 = sa przezyle, 3 = zmiany poza zasiegiem mutmuta, trafia do raportu jako `kod_narzedzia`),
   kazdy inny kod (takze 2) to kod 2 nakladki,
2. `python -m mutmut results --all true` - statusy mutantow; zbadane to mutanty ze wzorcow zmienionych
   funkcji o statusie z `ROZSTRZYGNIETE` (nie `not checked`, `skipped`, `check was interrupted by
   user`; status spoza znanych to kod 2); przezyle z tego wyniku musza zgadzac sie z kluczami ze
   stdout narzedzia (kod 1), inaczej kod 2 (stary cache `mutants/`); lista `poza_zasiegiem` to
   `mutanty_zmian.bez_mutantow` na tych samych zmianach, przy KAZDYM kodzie (przy kodzie 1 narzedzie
   daje ja tylko na stderr; kod priorytetu 2 > 1 > 3 > 0). Kod 3 wymaga zgodnosci ze stdout narzedzia,
   kod 0 z niepusta lista to kod 2 nakladki,
3. `python -m mutmut show <klucz>` dla kazdego przezylego (`survived`, `no tests`).

Stdout to jeden dokument `{"zbadane": int, "przezyle": [{"plik","linia","opis_mutanta"}],
"kod_narzedzia": int, "poza_zasiegiem": [{"plik","linia"}]}` (lista niepusta zawsze przy kodzie 3,
mozliwa przy kodzie 1);
`plik` to sciezka zrodla w repozytorium (`src/...py`), `linia` to prawdziwy
numer pierwszej zmienionej linii w tym pliku. `mutmut show` liczy `@@ -N` od kodu samej funkcji, wiec
numer jest przeliczany: znajdujemy funkcje w AST pliku zrodlowego i sprawdzamy, ze stara strona hunka
zgadza sie z tekstem pliku. Brak mozliwosci ustalenia (nieczytelny plik, brak funkcji, niezgodny
tekst, niesparsowany hunk) to kod 2 z komunikatem na stderr i pustym stdout: brak dowodu to blad.

Kody wyjscia: 0 raport wypisany (takze z przezylymi), 2 blad narzedzia albo brak dowodu.
Skrypt niczego nie usuwa i nie czysci cache `mutants/`.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

import mutanty_zmian
from mutanty_zmian import BladNarzedzia

KODY_TOOLA = (0, 1, 3)
# Statusy `mutmut results` to wartosci `status_by_exit_code` w `mutmut/stats.py` (mutmut 3.x).
# Zbadany (rozstrzygniety) to mutant z werdyktem mutmuta o mutancie:
# - killed (kody 1, 3), survived (0), no tests (5, 33: testy nie wykonuja tego kodu), timeout (36,
#   SIGXCPU, 255), caught by type check (37), segfault (-11, -9: proces testow padl na tym mutancie)
#   to werdykty;
# - `suspicious` NIE jest werdyktem: `status_by_exit_code` to defaultdict z domyslna wartoscia
#   "suspicious" dla kazdego kodu spoza mapy (stats.py:15-16), a `workers/isolation.py:590-595`
#   przekazuje kod wyjscia runnera testow do `os._exit`; `ui/browse.py:160-163` opisuje go jako
#   "unknown exit code". Nieznany kod to brak werdyktu (`evidence_required`), wiec nie liczy sie do
#   `zbadane`;
# - `not checked` (None), `skipped` (34; mutmut liczy `tested = total - skipped`, `__main__.py`) i
#   `check was interrupted by user` (2) to brak uruchomienia albo przerwanie, czyli bez werdyktu.
# Status spoza obu zbiorow to blad narzedzia (`_statusy`).
ROZSTRZYGNIETE = frozenset({
    "killed", "survived", "timeout", "no tests", "caught by type check", "segfault",
})
NIEZBADANE = frozenset({"not checked", "skipped", "check was interrupted by user", "suspicious"})
PREFIKS_MUTANTOW = "mutants/"
NAGLOWEK_HUNKA = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def uruchom(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Jedyne miejsce uruchamiania procesow (bez powloki); nie do uruchomienia to `BladNarzedzia`."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie uruchomic {argv[0]}: {blad}") from blad


def _koncowka(wyjscie: subprocess.CompletedProcess[str], ile: int = 10) -> str:
    return "\n".join(f"{wyjscie.stdout}\n{wyjscie.stderr}".strip().splitlines()[-ile:])


def _git(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Git przez `mutanty_zmian.uruchom` (strumien dekodowany scisle jako UTF-8): bajty spoza UTF-8
    w zmienionej linii to `BladNarzedzia`, nie traceback."""
    try:
        return mutanty_zmian.uruchom(argv)
    except UnicodeDecodeError as blad:
        raise BladNarzedzia(f"{' '.join(argv[:2])}: wyjscie gita nie jest poprawnym UTF-8: {blad}") from blad


def _zmiany(baza: str, korzen: Path) -> tuple[dict[str, set[int]], list[str]]:
    """(zmienione linie po plikach, `source_paths`) wzgledem `baza`, jak w `mutanty_zmian._przebieg`."""
    source_paths = mutanty_zmian.source_paths_z_pyproject(korzen / "pyproject.toml")
    merge = _git(["git", "merge-base", baza, "HEAD"])
    if merge.returncode != 0:
        raise BladNarzedzia(f"nieznana baza {baza!r}: {merge.stderr.strip()}")
    roznica = _git([
        "git", "diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
        merge.stdout.strip(), "--", "src/", *source_paths,
    ])
    if roznica.returncode != 0:
        raise BladNarzedzia(f"git diff nie powiodl sie: {roznica.stderr.strip()}")
    nowe = _git(["git", "ls-files", "--others", "--exclude-standard", "--", "src/", *source_paths])
    if nowe.returncode != 0:
        raise BladNarzedzia(f"git ls-files nie powiodl sie: {nowe.stderr.strip()}")
    zmiany = mutanty_zmian.zmienione_linie(roznica.stdout)
    for sciezka, linie in mutanty_zmian._nowe_pliki(nowe.stdout, korzen).items():
        zmiany.setdefault(sciezka, set()).update(linie)
    return zmiany, source_paths


def wzorce_zmian(baza: str, korzen: Path) -> list[str]:
    """Wzorce mutantow zmienionych funkcji wzgledem `baza` (te same co w `mutanty_zmian.py`)."""
    zmiany, source_paths = _zmiany(baza, korzen)
    return mutanty_zmian.wzorce(zmiany, source_paths, korzen)


def lista_poza_zasiegiem(baza: str, korzen: Path) -> list[dict]:
    """`{plik, linia}` zmian, ktorych mutmut nie mutuje (`mutanty_zmian.bez_mutantow` na tych samych
    zmianach co `wzorce_zmian`). Nie zalezy od kodu wyjscia narzedzia: przy kodzie 1 narzedzie
    wypisuje te liste tylko na stderr, wiec nakladka wyznacza ja sama."""
    zmiany, source_paths = _zmiany(baza, korzen)
    return _wpisy_poza_zasiegiem(mutanty_zmian.bez_mutantow(zmiany, source_paths, korzen))


def _wpisy_poza_zasiegiem(linie: list[str]) -> list[dict]:
    wpisy = []
    for linia in linie:
        plik, _, numer = linia.rpartition(":")
        if not plik or not (numer.isascii() and numer.isdigit()) or int(numer) < 1:
            raise BladNarzedzia(f"niesparsowany wpis poza zasiegiem: {linia!r}")
        wpisy.append({"plik": plik, "linia": int(numer)})
    return wpisy


def _statusy(wyniki: str, wzorce: list[str]) -> dict[str, str]:
    """Klucz -> status z `mutmut results` dla mutantow pasujacych do wzorcow zmienionych funkcji."""
    statusy: dict[str, str] = {}
    for linia in wyniki.splitlines():
        klucz, _, status = linia.strip().rpartition(": ")
        if "__mutmut_" not in klucz:
            continue
        if any(fnmatch.fnmatchcase(klucz, wzorzec) for wzorzec in wzorce):
            status = status.strip()
            if status not in ROZSTRZYGNIETE | NIEZBADANE:
                raise BladNarzedzia(f"{klucz}: nieznany status mutmuta {status!r}; nie ustalono, czy zbadany")
            statusy[klucz] = status
    return statusy


def klucze_ze_stdout(stdout: str) -> set[str]:
    """Klucze przezylych z stdout `mutanty_zmian.py` przy kodzie 1 (po jednym w linii)."""
    klucze = {linia.strip() for linia in stdout.splitlines() if "__mutmut_" in linia}
    if not klucze:
        raise BladNarzedzia("mutanty_zmian.py zakonczyl sie kodem 1 bez kluczy przezylych mutantow na stdout")
    return klucze


def poza_zasiegiem(stdout: str) -> list[dict]:
    """Lista `{plik, linia}` z stdout `mutanty_zmian.py` przy kodzie 3 (naglowek, potem `plik:linia`)."""
    linie = [linia.strip() for linia in stdout.splitlines() if linia.strip()]
    if not linie or linie[0] != mutanty_zmian.NAGLOWEK_BEZ_MUTANTOW or len(linie) < 2:
        raise BladNarzedzia("mutanty_zmian.py zakonczyl sie kodem 3 bez listy zmian poza zasiegiem na stdout")
    return _wpisy_poza_zasiegiem(linie[1:])


class _Hunk:
    """Hunk z `mutmut show`: poczatek starej strony, stare linie, liczba starych linii przed
    pierwsza zmiana i zmienione linie (z prefiksem `-`/`+`)."""

    def __init__(self, start: int) -> None:
        self.start = start
        self.stare: list[str] = []
        self.pierwsza_zmiana = 0
        self.zmienione: list[str] = []


def _parsuj_pokaz(klucz: str, tekst: str) -> tuple[str, list[_Hunk]]:
    """(sciezka pliku z `+++`, hunki) z wyjscia `mutmut show`; kazdy brak ksztaltu to `BladNarzedzia`."""
    plik: str | None = None
    hunki: list[_Hunk] = []
    for linia in tekst.splitlines():
        if linia.startswith("@@"):
            dopasowanie = NAGLOWEK_HUNKA.match(linia)
            if dopasowanie is None:
                raise BladNarzedzia(f"{klucz}: niesparsowany naglowek hunka: {linia!r}")
            hunki.append(_Hunk(int(dopasowanie.group(1))))
        elif not hunki:
            if linia.startswith("+++ "):
                plik = linia[4:].strip()
        else:
            hunk = hunki[-1]
            znak, tresc = linia[:1], linia[1:]
            if znak in (" ", ""):
                hunk.stare.append(tresc)
            elif znak == "-":
                if not hunk.zmienione:
                    hunk.pierwsza_zmiana = len(hunk.stare)
                hunk.stare.append(tresc)
                hunk.zmienione.append(linia)
            elif znak == "+":
                if not hunk.zmienione:
                    hunk.pierwsza_zmiana = len(hunk.stare)
                hunk.zmienione.append(linia)
            elif znak != "\\":
                raise BladNarzedzia(f"{klucz}: nieznana linia hunka: {linia!r}")
    if not plik:
        raise BladNarzedzia(f"{klucz}: w wyjsciu mutmut show nie ma naglowka +++ z plikiem")
    if not hunki:
        raise BladNarzedzia(f"{klucz}: w wyjsciu mutmut show nie ma hunka diffu")
    if not any(h.zmienione for h in hunki):
        raise BladNarzedzia(f"{klucz}: hunk mutmut show nie ma zmienionych linii")
    return plik.removeprefix(PREFIKS_MUTANTOW), hunki


def _nazwy_z_klucza(klucz: str) -> tuple[str | None, str]:
    """(klasa albo None, nazwa funkcji) z klucza mutanta `modul.x_f__mutmut_N` / `modul.xǁKǁm__mutmut_N`."""
    nazwa = klucz.rpartition("__mutmut_")[0].rpartition(".")[2]
    if nazwa.startswith("x" + mutanty_zmian.ZNAK):
        czesci = nazwa.split(mutanty_zmian.ZNAK)
        if len(czesci) == 3 and all(czesci):
            return czesci[1], czesci[2]
    elif nazwa.startswith("x_") and len(nazwa) > 2:
        return None, nazwa[2:]
    raise BladNarzedzia(f"{klucz}: nie umiem odczytac nazwy funkcji z klucza mutanta")


def _funkcja(
    drzewo: ast.Module, klasa: str | None, nazwa: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    funkcje = (ast.FunctionDef, ast.AsyncFunctionDef)
    for wezel in drzewo.body:
        if klasa is None and isinstance(wezel, funkcje) and wezel.name == nazwa:
            return wezel
        if klasa is not None and isinstance(wezel, ast.ClassDef) and wezel.name == klasa:
            for metoda in wezel.body:
                if isinstance(metoda, funkcje) and metoda.name == nazwa:
                    return metoda
    return None


def _kandydaci_poczatku(
    fizyczne: list[str], wezel: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[int]:
    """Numery linii, od ktorych moze zaczynac sie kod funkcji w `mutmut show`.

    `mutmut show` drukuje funkcje bez pustych linii na poczatku, ale z komentarzami nad nia i
    (zaleznie od wersji) z dekoratorem: kolejnosc to linia `def`, poczatek z dekoratorami, potem
    kolejne linie komentarzy w gore."""
    start, _ = mutanty_zmian._zakres(wezel)
    kandydaci = [wezel.lineno, start]
    numer = start - 1
    while numer >= 1 and (not fizyczne[numer - 1].strip() or fizyczne[numer - 1].lstrip().startswith("#")):
        if fizyczne[numer - 1].strip():
            kandydaci.append(numer)
        numer -= 1
    return list(dict.fromkeys(kandydaci))


def ustal_linie(korzen: Path, plik: str, klucz: str, hunki: list[_Hunk]) -> int:
    """Prawdziwy numer pierwszej zmienionej linii w pliku zrodlowym.

    `@@ -N` z `mutmut show` jest wzgledne do kodu samej funkcji. Funkcje znajdujemy w AST pliku
    zrodlowego (z klucza mutanta), a numer przeliczamy tylko wtedy, gdy cala stara strona hunka
    zgadza sie (po `strip`) z tekstem pliku od jednego z kandydatow poczatku funkcji."""
    try:
        zrodlo = mutanty_zmian.zrodlo_z_dysku(korzen, plik)
    except (OSError, UnicodeDecodeError) as blad:
        raise BladNarzedzia(f"{klucz}: nie da sie odczytac {plik}: {blad}") from blad
    try:
        drzewo = ast.parse(zrodlo, filename=plik)
    except (SyntaxError, ValueError) as blad:
        raise BladNarzedzia(f"{klucz}: {plik}: nie da sie sparsowac: {blad}") from blad
    klasa, nazwa = _nazwy_z_klucza(klucz)
    wezel = _funkcja(drzewo, klasa, nazwa)
    if wezel is None:
        raise BladNarzedzia(f"{klucz}: w {plik} nie ma funkcji {nazwa}")
    fizyczne = mutanty_zmian.linie_zrodla(zrodlo)
    hunk = next(h for h in hunki if h.zmienione)   # `_parsuj_pokaz` gwarantuje taki hunk
    if hunk.start < 1:
        raise BladNarzedzia(f"{klucz}: poczatek hunka {hunk.start} nie wskazuje linii funkcji")
    for poczatek in _kandydaci_poczatku(fizyczne, wezel):
        pierwsza = poczatek + hunk.start - 1
        if pierwsza + len(hunk.stare) - 1 > len(fizyczne):
            continue
        tekst = fizyczne[pierwsza - 1 : pierwsza - 1 + len(hunk.stare)]
        if [t.strip() for t in tekst] == [s.strip() for s in hunk.stare]:
            return pierwsza + hunk.pierwsza_zmiana
    raise BladNarzedzia(
        f"{klucz}: hunk mutmut show nie zgadza sie z tekstem {plik}; nie ustalono numeru linii")


def opisz_przezyly(korzen: Path, klucz: str, pokaz: str) -> dict:
    """Wpis `{plik, linia, opis_mutanta}` przezylego mutanta z wyjscia `mutmut show`."""
    plik, hunki = _parsuj_pokaz(klucz, pokaz)
    linia = ustal_linie(korzen, plik, klucz, hunki)
    opis = "\n".join(zmieniona for h in hunki for zmieniona in h.zmienione)
    return {"plik": plik, "linia": linia, "opis_mutanta": opis}


def _raport(baza: str, korzen: Path) -> dict:
    zmian = uruchom([
        sys.executable, str(Path(__file__).with_name("mutanty_zmian.py")), "--baza", baza])
    if zmian.returncode not in KODY_TOOLA:
        raise BladNarzedzia(
            f"mutanty_zmian.py zakonczyl sie kodem {zmian.returncode}:\n{_koncowka(zmian)}")
    wzorce = wzorce_zmian(baza, korzen)
    statusy: dict[str, str] = {}
    if wzorce:
        wyniki = uruchom([sys.executable, "-m", "mutmut", "results", "--all", "true"])
        if wyniki.returncode != 0:
            raise BladNarzedzia(f"mutmut results zakonczyl sie bledem:\n{_koncowka(wyniki)}")
        statusy = _statusy(wyniki.stdout, wzorce)
    zbadane = [k for k, status in statusy.items() if status in ROZSTRZYGNIETE]
    # stary cache `mutants/` moze dac przezyle, ktorych swiezy przebieg nie widzial (albo odwrotnie):
    # klucze ze stdout narzedzia musza zgadzac sie z `mutmut results`
    z_narzedzia = klucze_ze_stdout(zmian.stdout) if zmian.returncode == 1 else set()
    z_wynikow = {k for k, status in statusy.items() if status in mutanty_zmian.STATUSY_SURVIVORA}
    if z_narzedzia != z_wynikow:
        raise BladNarzedzia(
            "przezyle mutanty z mutanty_zmian.py i z mutmut results sie roznia (stary cache mutants/?): "
            f"tylko w narzedziu {sorted(z_narzedzia - z_wynikow)[:3]}, "
            f"tylko w wynikach {sorted(z_wynikow - z_narzedzia)[:3]}")
    # lista "sprawdz recznie" przy KAZDYM kodzie: przy kodzie 1 narzedzie wypisuje ja tylko na stderr
    poza = lista_poza_zasiegiem(baza, korzen)
    if zmian.returncode == 3:
        # kod 3 <=> brak przezylych i niepusta lista; stdout narzedzia musi zgadzac sie z wyliczeniem
        if poza_zasiegiem(zmian.stdout) != poza:
            raise BladNarzedzia("lista poza zasiegiem z mutanty_zmian.py i z nakladki sie roznia")
    elif zmian.returncode == 0 and poza:
        raise BladNarzedzia("mutanty_zmian.py zakonczyl sie kodem 0, a zmiany poza zasiegiem istnieja (oczekiwano 3)")
    przezyle = []
    for klucz in sorted(k for k in zbadane if statusy[k] in mutanty_zmian.STATUSY_SURVIVORA):
        pokaz = uruchom([sys.executable, "-m", "mutmut", "show", klucz])
        if pokaz.returncode != 0:
            raise BladNarzedzia(f"mutmut show {klucz} zakonczyl sie bledem:\n{_koncowka(pokaz)}")
        przezyle.append(opisz_przezyly(korzen, klucz, pokaz.stdout))
    return {
        "zbadane": len(zbadane), "przezyle": przezyle, "kod_narzedzia": zmian.returncode,
        "poza_zasiegiem": poza}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baza", default="HEAD", help="baza porownania (domyslnie HEAD: zmiany niezacommitowane)")
    args = parser.parse_args(argv)
    try:
        raport = _raport(args.baza, Path.cwd())
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2
    print(json.dumps(raport, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

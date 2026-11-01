"""Wykonywalna tabela recznych mutantow (HAR-150).

Czyta `tests/mutanty_reczne/<ticket>.toml` (wpisy `[[mutant]]` z polami `plik`, `szukaj`,
`zamien`, `test`), naklada kazda mutacje w NOWEJ kopii repozytorium w katalogu tymczasowym
i sprawdza, ze wskazany test oblewa. Najpierw ten sam test musi przejsc na kopii bez
mutacji - inaczej czerwony test "zabilby" kazdego mutanta.
Kody wyjscia: 0 - wszystkie mutanty zabite, 1 - sa przezywajace (stdout: po jednej linii
`plik: szukaj → zamien`), 2 - blad narzedzia (brak dowodu: zla tabela, `szukaj` nie raz,
test czerwony bez mutacji, kod pytesta inny niz 0/1, przekroczony limit czasu).
Priorytet kodow: 2 > 1 > 0. Repozytorium uzytkownika nie jest modyfikowane.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

POLA = ("plik", "szukaj", "zamien", "test")
LIMIT_DOMYSLNY = 300.0
CZEKANIE_PO_ZABICIU = 10.0


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


@dataclass(frozen=True)
class Mutant:
    plik: str
    szukaj: str
    zamien: str
    test: str


def _mutant_z_wpisu(wpis: object, numer: int, tabela: Path) -> Mutant:
    gdzie = f"{tabela}: wpis {numer}"
    if not isinstance(wpis, dict):
        raise BladNarzedzia(f"{gdzie}: wpis nie jest tabela")
    nieznane = sorted(set(wpis) - set(POLA))
    if nieznane:
        raise BladNarzedzia(f"{gdzie}: nieznany klucz {nieznane[0]!r}")
    for pole in POLA:
        if pole not in wpis:
            raise BladNarzedzia(f"{gdzie}: brak pola {pole!r}")
        if not isinstance(wpis[pole], str) or not wpis[pole]:
            raise BladNarzedzia(f"{gdzie}: pole {pole!r} musi byc niepustym napisem")
    sciezka = Path(wpis["plik"])
    if sciezka.is_absolute() or ".." in sciezka.parts:
        raise BladNarzedzia(f"{gdzie}: plik {wpis['plik']!r} musi byc sciezka wzgledna w repozytorium")
    if wpis["szukaj"] == wpis["zamien"]:
        raise BladNarzedzia(f"{gdzie}: zamien jest rowne szukaj (mutant tozsamosciowy)")
    return Mutant(wpis["plik"], wpis["szukaj"], wpis["zamien"], wpis["test"])


def wczytaj_tabele(sciezka: Path) -> list[Mutant]:
    """Czyta i waliduje tabele; kazde naruszenie to `BladNarzedzia`."""
    try:
        with sciezka.open("rb") as plik:
            dane = tomllib.load(plik)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as blad:
        raise BladNarzedzia(f"{sciezka}: nie da sie odczytac tabeli: {blad}") from blad
    nieznane = sorted(set(dane) - {"mutant"})
    if nieznane:
        raise BladNarzedzia(f"{sciezka}: nieznany klucz najwyzszego poziomu {nieznane[0]!r}")
    wpisy = dane.get("mutant")
    if not isinstance(wpisy, list):
        raise BladNarzedzia(f"{sciezka}: brak listy [[mutant]]")
    if not wpisy:
        raise BladNarzedzia(f"{sciezka}: pusta tabela - brak dowodu")
    return [_mutant_z_wpisu(wpis, numer, sciezka) for numer, wpis in enumerate(wpisy, start=1)]


def uruchom(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Uruchamia polecenie (git) bez powloki."""
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie uruchomic {argv[0]}: {blad}") from blad


def skopiuj_repo(korzen: Path, cel: Path) -> None:
    """Kopiuje sledzone i nieslezone nieignorowane pliki repozytorium (bez `.git`)."""
    lista = uruchom(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], korzen)
    if lista.returncode != 0:
        raise BladNarzedzia(f"git ls-files nie powiodl sie: {lista.stderr.strip()}")
    for wzgledna in lista.stdout.split("\0"):
        if not wzgledna:
            continue
        zrodlo = korzen / wzgledna
        try:
            if not zrodlo.is_file():
                continue
            docelowy = cel / wzgledna
            docelowy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(zrodlo, docelowy)
        except OSError as blad:
            raise BladNarzedzia(f"{wzgledna}: nie da sie skopiowac: {blad}") from blad


def zastosuj_mutacje(kopia: Path, mutant: Mutant) -> None:
    """Zamienia `szukaj` na `zamien` w pliku kopii; `szukaj` musi wystapic dokladnie raz."""
    sciezka = kopia / mutant.plik
    try:
        tresc = sciezka.read_bytes()
    except OSError as blad:
        raise BladNarzedzia(f"{mutant.plik}: nie da sie odczytac: {blad}") from blad
    try:
        tresc.decode("utf-8")
    except UnicodeDecodeError as blad:
        raise BladNarzedzia(f"{mutant.plik}: to nie jest UTF-8: {blad}") from blad
    szukaj = mutant.szukaj.encode("utf-8")
    ile = tresc.count(szukaj)
    if ile != 1:
        raise BladNarzedzia(
            f"{mutant.plik}: szukaj {mutant.szukaj!r} wystepuje {ile} razy, a ma dokladnie raz"
        )
    try:
        sciezka.write_bytes(tresc.replace(szukaj, mutant.zamien.encode("utf-8"), 1))
    except OSError as blad:
        raise BladNarzedzia(f"{mutant.plik}: nie da sie zapisac: {blad}") from blad


def uruchom_test(kopia: Path, test: str, limit: float) -> int:
    """Uruchamia jeden test pytesta w kopii i zwraca jego kod wyjscia."""
    # `-o addopts=` wylacza `-m "not ..."` z konfiguracji projektu: wybrany test ma sie uruchomic.
    polecenie = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", test]
    stare = os.environ.get("PYTHONPATH")
    # Kopia na poczatku, zeby instalacja editable nie wskazywala na oryginal.
    sciezki = os.pathsep.join([str(kopia / "src"), *([stare] if stare else [])])
    srodowisko = {**os.environ, "PYTHONPATH": sciezki}
    try:
        proces = subprocess.Popen(
            polecenie,
            cwd=kopia,
            env=srodowisko,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie uruchomic pytesta w {kopia}: {blad}") from blad
    try:
        proces.communicate(timeout=limit)
    except subprocess.TimeoutExpired as blad:
        # Nowa sesja: pgid == pid, wiec zabijamy wylacznie wlasna grupe.
        try:
            os.killpg(proces.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proces.communicate(timeout=CZEKANIE_PO_ZABICIU)
        except subprocess.TimeoutExpired:
            pass
        raise BladNarzedzia(f"przekroczony czas {limit} s dla testu {test}") from blad
    return proces.returncode


def ocen_kod_testu(kod: int) -> bool:
    """True - mutant zabity (test oblal), False - przezyl; inny kod to brak sygnalu."""
    if kod == 1:
        return True
    if kod == 0:
        return False
    raise BladNarzedzia(
        f"pytest zakonczyl sie kodem {kod} (nie 0 ani 1): brak dowodu - zly node id, "
        "blad skladni po mutacji, przerwanie albo nic nie zebrano"
    )


def _zabity(korzen: Path, mutant: Mutant, limit: float) -> bool:
    try:
        kopia = Path(tempfile.mkdtemp(prefix="mutanty-reczne-"))
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie utworzyc katalogu tymczasowego: {blad}") from blad
    try:
        skopiuj_repo(korzen, kopia)
        if uruchom_test(kopia, mutant.test, limit) != 0:
            raise BladNarzedzia(
                f"{mutant.test}: test nie przechodzi bez mutacji - brak dowodu"
            )
        zastosuj_mutacje(kopia, mutant)
        return ocen_kod_testu(uruchom_test(kopia, mutant.test, limit))
    finally:
        try:
            shutil.rmtree(kopia)
        except OSError as blad:
            raise BladNarzedzia(f"nie da sie usunac katalogu tymczasowego {kopia}: {blad}") from blad


def _limit(tekst: str) -> float:
    try:
        wartosc = float(tekst)
    except ValueError as blad:
        raise argparse.ArgumentTypeError(f"{tekst!r} nie jest liczba") from blad
    if not math.isfinite(wartosc) or wartosc <= 0:
        raise argparse.ArgumentTypeError(f"limit musi byc skonczona liczba > 0, jest {tekst!r}")
    return wartosc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tabele", nargs="+", type=Path, help="tests/mutanty_reczne/<ticket>.toml")
    parser.add_argument("--korzen", type=Path, default=None, help="korzen repozytorium (domyslnie cwd)")
    parser.add_argument("--limit", type=_limit, default=LIMIT_DOMYSLNY, help="limit czasu testu w sekundach")
    args = parser.parse_args(argv)
    korzen = args.korzen if args.korzen is not None else Path.cwd()
    try:
        mutanty = [m for tabela in args.tabele for m in wczytaj_tabele(korzen / tabela)]
        przezywajace = [m for m in mutanty if not _zabity(korzen, m, args.limit)]
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2
    for m in przezywajace:
        szukaj, zamien = (t.replace("\n", "\\n") for t in (m.szukaj, m.zamien))
        print(f"{m.plik}: {szukaj} → {zamien}")
    return 1 if przezywajace else 0


if __name__ == "__main__":
    sys.exit(main())

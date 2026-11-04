"""Wykonywalna tabela recznych mutantow (HAR-150).

Czyta `tests/mutanty_reczne/<ticket>.toml` (wpisy `[[mutant]]` z polami `plik`, `szukaj`,
`zamien`, `test`), naklada kazda mutacje w NOWEJ kopii repozytorium w katalogu tymczasowym
i sprawdza, ze wskazany test oblewa. Najpierw ten sam test musi przejsc na kopii bez
mutacji - inaczej czerwony test "zabilby" kazdego mutanta.
Kody wyjscia: 0 - wszystkie mutanty zabite, 1 - sa przezywajace (stdout: po jednej linii
`plik: szukaj → zamien`), 2 - blad narzedzia (brak dowodu: zla tabela, `szukaj` nie raz,
test czerwony bez mutacji, kod pytesta inny niz 0/1, przekroczony limit czasu, test
pominiety albo niezebrany (zero testow, brak raportu JUnit) w ktorymkolwiek przebiegu,
blad budowania frontu).
Priorytet kodow: 2 > 1 > 0. Repozytorium uzytkownika nie jest modyfikowane.
HAR-156: mutant z `plik` pod `web/` albo z testem pod `tests/e2e/` dostaje w kopii zbudowany
front (`npm ci` raz na kopie, `npm run build` do `src/cockpit/static`); po mutacji pliku pod
`web/` front jest budowany ponownie. `--czy-front <tabele>` tylko wypisuje `front=1`/`front=0`.
Pominiety test nigdy nie liczy sie jako "przeszedl" ani "zabil mutanta".
HAR-184: `zamien` moze byc pustym napisem (mutacja usuwajaca fragment `szukaj`); `plik`, `szukaj`
i `test` pozostaja niepuste.
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

POLA = ("plik", "szukaj", "zamien", "test")
LIMIT_DOMYSLNY = 300.0
LIMIT_BUDOWY = 900.0
CZEKANIE_PO_ZABICIU = 10.0
RAPORT_JUNIT = ".mutanty-reczne-junit.xml"
ZMIENNA_WYMAGANIA_BAZY = "COCKPIT_WYMAGAJ_BAZY"
KONCOWKA_WYJSCIA = 2000


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
        if not isinstance(wpis[pole], str):
            raise BladNarzedzia(f"{gdzie}: pole {pole!r} musi byc napisem")
        if not wpis[pole] and pole != "zamien":
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


def _uruchom_proces(
    polecenie: list[str], cwd: Path, srodowisko: dict[str, str], limit: float, co: str
) -> tuple[int, str]:
    """Uruchamia proces w nowej grupie; zwraca kod i polaczone stdout+stderr.

    Po limicie ginie cala grupa procesow (zero sierot), a wynik to `BladNarzedzia`."""
    try:
        proces = subprocess.Popen(
            polecenie,
            cwd=cwd,
            env=srodowisko,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie uruchomic {co} w {cwd}: {blad}") from blad
    try:
        wyjscie, _ = proces.communicate(timeout=limit)
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
        raise BladNarzedzia(f"przekroczony czas {limit} s dla {co}") from blad
    return proces.returncode, wyjscie.decode("utf-8", errors="replace")


def sprawdz_raport_junit(sciezka: Path, test: str) -> None:
    """Raport JUnit musi dowodzic, ze test sie uruchomil i go nie pominieto."""
    try:
        korzen = ET.parse(sciezka).getroot()
    except OSError as blad:
        raise BladNarzedzia(f"{test}: brak raportu JUnit {sciezka}: brak dowodu ({blad})") from blad
    except ET.ParseError as blad:
        raise BladNarzedzia(f"{test}: raport JUnit nie jest poprawnym XML: brak dowodu ({blad})") from blad
    try:
        uruchomione = sum(int(s.get("tests", "0")) for s in korzen.iter("testsuite"))
        pominiete = sum(int(s.get("skipped", "0")) for s in korzen.iter("testsuite"))
    except ValueError as blad:
        raise BladNarzedzia(f"{test}: raport JUnit ma liczniki nie bedace liczbami: brak dowodu ({blad})") from blad
    if uruchomione == 0:
        raise BladNarzedzia(f"{test}: nic nie uruchomiono wedlug raportu JUnit - brak dowodu")
    if pominiete > 0:
        raise BladNarzedzia(f"{test}: test pominięty - brak dowodu (pominiecie to nie przejscie ani zabity mutant)")


def pod_webem(plik: str) -> bool:
    """True tylko dla sciezki, ktorej pierwsza czescia jest `web` (nie `webx`, nie `src/web`)."""
    return Path(plik).parts[:1] == ("web",)


def wymaga_frontu(mutant: Mutant) -> bool:
    """True, gdy mutant dotyka `web/` albo `tests/e2e/` (plik lub test) - potrzebny zbudowany front."""
    plik_testu = mutant.test.split("::", 1)[0]
    return (
        pod_webem(mutant.plik)
        or Path(mutant.plik).parts[:2] == ("tests", "e2e")
        or Path(plik_testu).parts[:2] == ("tests", "e2e")
    )


def _npm(kopia: Path, argumenty: list[str], limit: float) -> None:
    polecenie = " ".join(["npm", *argumenty])
    kod, wyjscie = _uruchom_proces(["npm", *argumenty], kopia / "web", dict(os.environ), limit, polecenie)
    if kod != 0:
        raise BladNarzedzia(f"{polecenie} zakonczyl sie kodem {kod}: {wyjscie[-KONCOWKA_WYJSCIA:]}")


def zbuduj_front(kopia: Path, limit: float) -> None:
    """Buduje front w kopii do `src/cockpit/static`; `npm ci` tylko gdy brak `web/node_modules`."""
    try:
        zainstalowane = (kopia / "web" / "node_modules").is_dir()
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie sprawdzic web/node_modules w {kopia}: {blad}") from blad
    if not zainstalowane:
        _npm(kopia, ["ci"], limit)
    _npm(kopia, ["run", "build"], limit)
    indeks = kopia / "src" / "cockpit" / "static" / "index.html"
    try:
        istnieje = indeks.is_file()
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie sprawdzic {indeks}: {blad}") from blad
    if not istnieje:
        raise BladNarzedzia(f"build nie zostawil index.html w {indeks.parent} - brak dowodu")


def uruchom_test(kopia: Path, test: str, limit: float) -> int:
    """Uruchamia jeden test pytesta w kopii i zwraca jego kod wyjscia.

    Kod 0 i 1 jest potwierdzany raportem JUnit: pominiety albo niezebrany test to `BladNarzedzia`."""
    raport = kopia / RAPORT_JUNIT
    # `-o addopts=` wylacza `-m "not ..."` z konfiguracji projektu: wybrany test ma sie uruchomic.
    polecenie = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
        f"--junitxml={raport}", test,
    ]
    # Stary raport z poprzedniego przebiegu w tej kopii nie moze udowadniac tego przebiegu.
    try:
        raport.unlink(missing_ok=True)
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie usunac starego raportu {raport}: {blad}") from blad
    stare = os.environ.get("PYTHONPATH")
    # Kopia na poczatku, zeby instalacja editable nie wskazywala na oryginal.
    sciezki = os.pathsep.join([str(kopia / "src"), *([stare] if stare else [])])
    # Bez bajtkodu: mutacja tej samej dlugosci zapisana w tej samej sekundzie co oryginal
    # wygladalaby dla Pythona na niezmieniona i test uruchomilby `.pyc` sprzed mutacji.
    srodowisko = {**os.environ, "PYTHONPATH": sciezki, "PYTHONDONTWRITEBYTECODE": "1"}
    # Z ta zmienna skip testu `baza` jest porazka, czyli falszywie "zabity" mutant.
    srodowisko.pop(ZMIENNA_WYMAGANIA_BAZY, None)
    kod, _ = _uruchom_proces(polecenie, kopia, srodowisko, limit, f"testu {test}")
    if kod in (0, 1):
        sprawdz_raport_junit(raport, test)
    return kod


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


def _zabity(korzen: Path, mutant: Mutant, limit: float, limit_budowy: float) -> bool:
    try:
        kopia = Path(tempfile.mkdtemp(prefix="mutanty-reczne-"))
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie utworzyc katalogu tymczasowego: {blad}") from blad
    try:
        skopiuj_repo(korzen, kopia)
        # Test e2e potrzebuje frontu juz w przebiegu bazowym (inaczej czerwony bez mutacji).
        if wymaga_frontu(mutant):
            zbuduj_front(kopia, limit_budowy)
        if uruchom_test(kopia, mutant.test, limit) != 0:
            raise BladNarzedzia(
                f"{mutant.test}: test nie przechodzi bez mutacji - brak dowodu"
            )
        zastosuj_mutacje(kopia, mutant)
        # Mutacja w web/ jest widoczna dla testu dopiero po ponownym zbudowaniu frontu.
        if pod_webem(mutant.plik):
            zbuduj_front(kopia, limit_budowy)
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
    parser.add_argument(
        "--limit-budowy", type=_limit, default=LIMIT_BUDOWY, help="limit czasu jednego polecenia npm w sekundach"
    )
    parser.add_argument(
        "--czy-front",
        action="store_true",
        help="tylko wypisz front=1 (jest mutant frontu/e2e) albo front=0; niczego nie uruchamia",
    )
    args = parser.parse_args(argv)
    korzen = args.korzen if args.korzen is not None else Path.cwd()
    try:
        mutanty = [m for tabela in args.tabele for m in wczytaj_tabele(korzen / tabela)]
        if args.czy_front:
            print(f"front={int(any(wymaga_frontu(m) for m in mutanty))}")
            return 0
        przezywajace = [m for m in mutanty if not _zabity(korzen, m, args.limit, args.limit_budowy)]
    except BladNarzedzia as blad:
        print(f"blad: {blad}", file=sys.stderr)
        return 2
    for m in przezywajace:
        szukaj, zamien = (t.replace("\n", "\\n") for t in (m.szukaj, m.zamien))
        print(f"{m.plik}: {szukaj} → {zamien}")
    return 1 if przezywajace else 0


if __name__ == "__main__":
    sys.exit(main())

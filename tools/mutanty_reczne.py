"""Wykonywalna tabela recznych mutantow (HAR-150).

Czyta `tests/mutanty_reczne/<ticket>.toml` (wpisy `[[mutant]]` z polami `plik`, `szukaj`,
`zamien`, `test`), naklada kazda mutacje w NOWEJ kopii repozytorium w katalogu tymczasowym
i sprawdza, ze wskazany test oblewa. Najpierw ten sam test musi przejsc na kopii bez
mutacji - inaczej czerwony test "zabilby" kazdego mutanta.
Kody wyjscia: 0 - wszystkie mutanty zabite, 1 - sa przezywajace (stdout: po jednej linii
`plik: szukaj → zamien`), 2 - blad narzedzia (brak dowodu: zla tabela, `szukaj` nie raz,
test czerwony bez mutacji (kod 1), kod pytesta inny niz 0/1 w przebiegu bazowym albo po
mutacji (np. zly node id; HAR-223: komunikat o node id i kodzie pytesta, nie "czerwony"),
przekroczony limit czasu, test
pominiety albo niezebrany (zero testow, brak raportu JUnit) w ktorymkolwiek przebiegu,
blad budowania frontu).
Priorytet kodow: 2 > 1 > 0. Repozytorium uzytkownika nie jest modyfikowane.
HAR-156: mutant z `plik` pod `web/` albo z testem pod `tests/e2e/` dostaje w kopii zbudowany
front (`npm ci` raz na kopie, `npm run build` do `src/cockpit/static`); po mutacji pliku pod
`web/` front jest budowany ponownie. `--czy-front <tabele>` tylko wypisuje `front=1`/`front=0`.
Pominiety test nigdy nie liczy sie jako "przeszedl" ani "zabil mutanta".
HAR-225: `--sprawdz-wszystkie [--korzen K] [--limit S]` sprawdza WSZYSTKIE tabele
`K/tests/mutanty_reczne/*.toml` (bez tabel pozycyjnych i bez `--czy-front`) i niczego nie
wykonuje: nie kopiuje repo, nie naklada mutacji, nie uruchamia testow (tylko jedna kolekcja
pytesta `--collect-only`). Wiersz jest poprawny, gdy wpis przechodzi walidacje, `szukaj`
wystepuje w `plik` dokladnie raz (ta sama regula co przy nakladaniu mutacji) i node id testu
istnieje (dokladnie albo jako `node_id[...]`). Bledy sa zbierane, po jednym w linii na stderr:
`tests/mutanty_reczne/X.toml #n: powod` (wiersz) albo `tests/mutanty_reczne/X.toml: powod`
(cala tabela). Kody wyjscia: 0 - sprawdzono > 0 wierszy bez bledow (stdout: `sprawdzono N
wierszy w M tabelach`), 2 - kazdy blad wiersza lub tabeli, brak tabel, zero wierszy, kolekcja
pytesta z kodem innym niz 0 (blad importu, nic nie zebrano, przekroczony limit); nigdy 1.
HAR-184: `zamien` moze byc pustym napisem (mutacja usuwajaca fragment `szukaj`); `plik`, `szukaj`
i `test` pozostaja niepuste.
HAR-228: `--pokrycie-kod3 LISTA [--korzen K] [TABELA ...]` jest trybem statycznym (nie kopiuje repo,
nie uruchamia testow). LISTA to wyjscie kodu 3 `tools/mutanty_zmian.py` (pierwsza linia
`bez mutantów — sprawdź ręcznie`, dalej `plik:linia`). Linia jest pokryta, gdy w ktorejs z podanych
tabel jest `[[mutant]]` tego pliku, ktorego jedyne wystapienie `szukaj` obejmuje te linie, albo
`[[rownowazny]]` (pola `plik`, `linia_tekst`, `uzasadnienie`) z tekstem tej linii. Kody wyjscia:
0 - wszystkie pokryte (stdout: `pokryto N linii bez mutantów`), 1 - sa niepokryte (stdout: po
jednej `plik:linia`), 2 - blad narzedzia (zla lista, tabela, plik albo numer linii); 2 > 1 > 0.
Wpisy `[[rownowazny]]` sa walidowane we wszystkich trybach, ale nigdzie nie sa wykonywane.
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
POLA_ROWNOWAZNEGO = ("plik", "linia_tekst", "uzasadnienie")
# Ta sama tresc co NAGLOWEK_BEZ_MUTANTOW w tools/mutanty_zmian.py (tamten modul importuje mutmuta).
NAGLOWEK_BEZ_MUTANTOW = "bez mutantów — sprawdź ręcznie"
LIMIT_DOMYSLNY = 300.0
LIMIT_BUDOWY = 900.0
CZEKANIE_PO_ZABICIU = 10.0
RAPORT_JUNIT = ".mutanty-reczne-junit.xml"
ZMIENNA_WYMAGANIA_BAZY = "COCKPIT_WYMAGAJ_BAZY"
KONCOWKA_WYJSCIA = 2000
KONCOWKA_KOLEKCJI = 2000
KOD_BLEDU_NARZEDZIA = 2


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


@dataclass(frozen=True)
class Mutant:
    plik: str
    szukaj: str
    zamien: str
    test: str


@dataclass(frozen=True)
class Rownowazny:
    plik: str
    linia_tekst: str
    uzasadnienie: str


def _wymagaj_sciezki_wzglednej(plik: str) -> None:
    sciezka = Path(plik)
    if sciezka.is_absolute() or ".." in sciezka.parts:
        raise BladNarzedzia(f"plik {plik!r} musi byc sciezka wzgledna w repozytorium")


def _wpis_na_mutanta(wpis: object) -> Mutant:
    """Waliduje jeden wpis; powod bledu bez nazwy tabeli i numeru."""
    if not isinstance(wpis, dict):
        raise BladNarzedzia("wpis nie jest tabela")
    nieznane = sorted(set(wpis) - set(POLA))
    if nieznane:
        raise BladNarzedzia(f"nieznany klucz {nieznane[0]!r}")
    for pole in POLA:
        if pole not in wpis:
            raise BladNarzedzia(f"brak pola {pole!r}")
        if not isinstance(wpis[pole], str):
            raise BladNarzedzia(f"pole {pole!r} musi byc napisem")
        if not wpis[pole] and pole != "zamien":
            raise BladNarzedzia(f"pole {pole!r} musi byc niepustym napisem")
    _wymagaj_sciezki_wzglednej(wpis["plik"])
    if wpis["szukaj"] == wpis["zamien"]:
        raise BladNarzedzia("zamien jest rowne szukaj (mutant tozsamosciowy)")
    return Mutant(wpis["plik"], wpis["szukaj"], wpis["zamien"], wpis["test"])


def _wpis_na_rownowaznego(wpis: object) -> Rownowazny:
    """Waliduje jeden wpis `[[rownowazny]]`; powod bledu bez nazwy tabeli i numeru."""
    if not isinstance(wpis, dict):
        raise BladNarzedzia("wpis rownowazny nie jest tabela")
    nieznane = sorted(set(wpis) - set(POLA_ROWNOWAZNEGO))
    if nieznane:
        raise BladNarzedzia(f"wpis rownowazny: nieznany klucz {nieznane[0]!r}")
    for pole in POLA_ROWNOWAZNEGO:
        if pole not in wpis:
            raise BladNarzedzia(f"wpis rownowazny: brak pola {pole!r}")
        wartosc = wpis[pole]
        if not isinstance(wartosc, str):
            raise BladNarzedzia(f"wpis rownowazny: {pole!r} nie jest tekstem")
        if not wartosc.strip():
            raise BladNarzedzia(f"wpis rownowazny: {pole!r} jest pusty")
    _wymagaj_sciezki_wzglednej(wpis["plik"])
    return Rownowazny(wpis["plik"], wpis["linia_tekst"], wpis["uzasadnienie"])


def _mutant_z_wpisu(wpis: object, numer: int, tabela: Path) -> Mutant:
    try:
        return _wpis_na_mutanta(wpis)
    except BladNarzedzia as blad:
        raise BladNarzedzia(f"{tabela}: wpis {numer}: {blad}") from blad


def _surowe_wpisy(sciezka: Path) -> tuple[list[object], list[object]]:
    """Czyta tabele i zwraca wpisy `[[mutant]]` oraz `[[rownowazny]]`; powod bledu bez nazwy tabeli."""
    try:
        with sciezka.open("rb") as plik:
            dane = tomllib.load(plik)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as blad:
        raise BladNarzedzia(f"nie da sie odczytac tabeli: {blad}") from blad
    nieznane = sorted(set(dane) - {"mutant", "rownowazny"})
    if nieznane:
        raise BladNarzedzia(f"nieznany klucz najwyzszego poziomu {nieznane[0]!r}")
    wpisy = dane.get("mutant")
    if not isinstance(wpisy, list):
        raise BladNarzedzia("brak listy [[mutant]]")
    if not wpisy:
        raise BladNarzedzia("pusta tabela - brak dowodu")
    rownowazne = dane.get("rownowazny", [])
    if not isinstance(rownowazne, list):
        raise BladNarzedzia("rownowazny musi byc lista [[rownowazny]]")
    return wpisy, rownowazne


def wczytaj_tabele_z_rownowaznymi(sciezka: Path) -> tuple[list[Mutant], list[Rownowazny]]:
    """Czyta i waliduje tabele (`[[mutant]]` i `[[rownowazny]]`); kazde naruszenie to `BladNarzedzia`."""
    try:
        wpisy, rownowazne = _surowe_wpisy(sciezka)
    except BladNarzedzia as blad:
        raise BladNarzedzia(f"{sciezka}: {blad}") from blad
    mutanty = [_mutant_z_wpisu(wpis, numer, sciezka) for numer, wpis in enumerate(wpisy, start=1)]
    wpisane: list[Rownowazny] = []
    for numer, wpis in enumerate(rownowazne, start=1):
        try:
            wpisane.append(_wpis_na_rownowaznego(wpis))
        except BladNarzedzia as blad:
            raise BladNarzedzia(f"{sciezka}: rownowazny {numer}: {blad}") from blad
    return mutanty, wpisane


def wczytaj_tabele(sciezka: Path) -> list[Mutant]:
    """Czyta i waliduje tabele; zwraca wiersze `[[mutant]]` (wpisy `[[rownowazny]]` tylko walidowane)."""
    return wczytaj_tabele_z_rownowaznymi(sciezka)[0]


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


def _odczytaj_utf8(sciezka: Path, nazwa: str) -> bytes:
    """Bajty pliku UTF-8; brak pliku, brak praw albo inne kodowanie to `BladNarzedzia`."""
    try:
        tresc = sciezka.read_bytes()
    except OSError as blad:
        raise BladNarzedzia(f"{nazwa}: nie da sie odczytac: {blad}") from blad
    try:
        tresc.decode("utf-8")
    except UnicodeDecodeError as blad:
        raise BladNarzedzia(f"{nazwa}: to nie jest UTF-8: {blad}") from blad
    return tresc


def _wymagaj_dokladnie_raz(tresc: bytes, mutant: Mutant) -> bytes:
    """Zwraca `szukaj` jako bajty; jedna definicja "dokladnie raz" dla mutacji i dla sprawdzenia tabel."""
    szukaj = mutant.szukaj.encode("utf-8")
    ile = tresc.count(szukaj)
    if ile != 1:
        raise BladNarzedzia(
            f"{mutant.plik}: szukaj {mutant.szukaj!r} wystepuje {ile} razy, a ma dokladnie raz"
        )
    return szukaj


def zastosuj_mutacje(kopia: Path, mutant: Mutant) -> None:
    """Zamienia `szukaj` na `zamien` w pliku kopii; `szukaj` musi wystapic dokladnie raz."""
    sciezka = kopia / mutant.plik
    tresc = _odczytaj_utf8(sciezka, mutant.plik)
    szukaj = _wymagaj_dokladnie_raz(tresc, mutant)
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
        # HAR-223: kod 1 to czerwony test; kazdy inny niz 0 (np. 4 = nieistniejacy node id,
        # jak `pending` zamiast `chromium-pending` w HAR-81) to zly node id albo nic nie zebrano.
        kod_bazowy = uruchom_test(kopia, mutant.test, limit)
        if kod_bazowy == 1:
            raise BladNarzedzia(
                f"{mutant.test}: test nie przechodzi bez mutacji - brak dowodu"
            )
        if kod_bazowy != 0:
            raise BladNarzedzia(
                f"{mutant.test}: pytest zakonczyl sie kodem {kod_bazowy} bez mutacji (nie 0 ani 1): "
                "brak dowodu - zly node id albo nic nie zebrano; sprawdz node id "
                "(`pytest --collect-only -q <plik testu>`)"
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


KATALOG_TABEL = ("tests", "mutanty_reczne")


def _tabele_repozytorium(korzen: Path) -> list[Path]:
    """Wszystkie `*.toml` z katalogu tabel, posortowane po nazwie; brak tabel to `BladNarzedzia`."""
    katalog = korzen.joinpath(*KATALOG_TABEL)
    try:
        tabele = sorted((k for k in katalog.iterdir() if k.name.endswith(".toml")), key=lambda k: k.name)
    except OSError as blad:
        raise BladNarzedzia(f"nie da sie odczytac katalogu tabel {katalog}: {blad}") from blad
    if not tabele:
        raise BladNarzedzia(f"brak tabel *.toml w {katalog} - brak dowodu")
    return tabele


def _wiersze_i_bledy_tabeli(sciezka: Path, nazwa: str) -> tuple[list[tuple[int, Mutant]], list[str]]:
    """Poprawne wiersze (z numerem wpisu od 1) i zebrane bledy jednej tabeli."""
    try:
        return list(enumerate(wczytaj_tabele(sciezka), start=1)), []
    except BladNarzedzia:
        pass
    # Tabela ma blad: wczytaj_tabele przerywa na pierwszym, a tu zbieramy wszystkie.
    try:
        wpisy, rownowazne = _surowe_wpisy(sciezka)
    except BladNarzedzia as blad:
        return [], [f"{nazwa}: {blad}"]
    wiersze: list[tuple[int, Mutant]] = []
    bledy: list[str] = []
    for numer, wpis in enumerate(wpisy, start=1):
        try:
            wiersze.append((numer, _wpis_na_mutanta(wpis)))
        except BladNarzedzia as blad:
            bledy.append(f"{nazwa} #{numer}: {blad}")
    for numer, wpis in enumerate(rownowazne, start=1):
        try:
            _wpis_na_rownowaznego(wpis)
        except BladNarzedzia as blad:
            bledy.append(f"{nazwa} rownowazny #{numer}: {blad}")
    return wiersze, bledy


def _blad_pliku_i_licznika(korzen: Path, mutant: Mutant, tresci: dict[str, bytes | BladNarzedzia]) -> str | None:
    """Powod bledu wiersza: plik nieczytelny albo `szukaj` nie dokladnie raz; None, gdy jest dobrze."""
    if mutant.plik not in tresci:
        try:
            tresci[mutant.plik] = _odczytaj_utf8(korzen / mutant.plik, mutant.plik)
        except BladNarzedzia as blad:
            tresci[mutant.plik] = blad
    tresc = tresci[mutant.plik]
    if isinstance(tresc, BladNarzedzia):
        return str(tresc)
    try:
        _wymagaj_dokladnie_raz(tresc, mutant)
    except BladNarzedzia as blad:
        return str(blad)
    return None


def _plik_testu(test: str) -> str:
    """Czesc node id przed pierwszym `::`."""
    return test.partition("::")[0]


def _blad_pliku_testu(korzen: Path, test: str) -> str | None:
    """Powod bledu, gdy pliku testu z node id nie ma na dysku (bez wolania pytesta); None, gdy jest."""
    plik_testu = _plik_testu(test)
    try:
        istnieje = (korzen / plik_testu).is_file()
    except OSError as blad:
        return f"{plik_testu}: nie da sie sprawdzic pliku testu: {blad}"
    return None if istnieje else f"test {test} nie istnieje: brak pliku {plik_testu}"


def _zbierz_node_id(korzen: Path, pliki: list[str], limit: float) -> set[str]:
    """Jedna kolekcja pytesta (`--collect-only`) dla plikow testow; kod inny niz 0 to `BladNarzedzia`."""
    stare = os.environ.get("PYTHONPATH")
    sciezki = os.pathsep.join([str(korzen / "src"), *([stare] if stare else [])])
    srodowisko = {**os.environ, "PYTHONPATH": sciezki}
    # Kolekcja nie zostawia bajtkodu w repozytorium uzytkownika.
    srodowisko["PYTHONDONTWRITEBYTECODE"] = "1"
    polecenie = [
        sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
        *pliki,
    ]  # fmt: skip
    kod, wyjscie = _uruchom_proces(polecenie, korzen, srodowisko, limit, "kolekcji testow")
    if kod:
        raise BladNarzedzia(
            f"kolekcja testow zakonczyla sie kodem {kod} (nie 0): brak dowodu, ze node id istnieja: "
            f"{wyjscie[-KONCOWKA_KOLEKCJI:]}"
        )
    return {linia.strip() for linia in wyjscie.splitlines() if "::" in linia}


def _istnieje_node_id(test: str, zebrane: set[str]) -> bool:
    """Dokladnie na liscie albo jako funkcja parametryzowana `test[...]` zapisana bez nawiasu."""
    return test in zebrane or any(z.startswith(f"{test}[") for z in zebrane)


def sprawdz_wszystkie_tabele(korzen: Path, limit: float, bledy: list[str]) -> tuple[int, int]:
    """Sprawdza wszystkie tabele bez nakladania mutacji; bledy dopisuje do `bledy`.

    Zwraca (liczba wierszy, liczba tabel). Brak tabel, zero wierszy i kolekcja z kodem != 0 to `BladNarzedzia`."""
    tabele = _tabele_repozytorium(korzen)
    wiersze: list[tuple[str, int, Mutant]] = []
    for tabela in tabele:
        nazwa = tabela.relative_to(korzen).as_posix()
        poprawne, bledy_tabeli = _wiersze_i_bledy_tabeli(tabela, nazwa)
        bledy.extend(bledy_tabeli)
        wiersze.extend((nazwa, numer, mutant) for numer, mutant in poprawne)
    tresci: dict[str, bytes | BladNarzedzia] = {}
    do_kolekcji: list[tuple[str, int, Mutant]] = []
    for nazwa, numer, mutant in wiersze:
        powod_pliku = _blad_pliku_i_licznika(korzen, mutant, tresci)
        if powod_pliku is not None:
            bledy.append(f"{nazwa} #{numer}: {powod_pliku}")
        powod_testu = _blad_pliku_testu(korzen, mutant.test)
        if powod_testu is not None:
            bledy.append(f"{nazwa} #{numer}: {powod_testu}")
        else:
            do_kolekcji.append((nazwa, numer, mutant))
    if do_kolekcji:
        pliki = sorted({_plik_testu(mutant.test) for _, _, mutant in do_kolekcji})
        zebrane = _zbierz_node_id(korzen, pliki, limit)
        for nazwa, numer, mutant in do_kolekcji:
            if not _istnieje_node_id(mutant.test, zebrane):
                bledy.append(f"{nazwa} #{numer}: test {mutant.test} nie istnieje wsrod zebranych testow")
    if not bledy and not wiersze:
        raise BladNarzedzia("zero sprawdzonych wierszy - brak dowodu")
    return len(wiersze), len(tabele)


def _wpis_listy_kodu_3(linia: str, numer: int) -> tuple[str, int]:
    """Jeden wpis `plik:linia` listy; `numer` to numer linii listy (naglowek to linia 1)."""
    plik, dwukropek, cyfry = linia.rpartition(":")
    if not dwukropek:
        raise BladNarzedzia(f"lista kodu 3, linia {numer}: wpis {linia!r} nie ma postaci plik:linia")
    sciezka = Path(plik)
    if not plik or sciezka.is_absolute() or ".." in sciezka.parts:
        raise BladNarzedzia(
            f"lista kodu 3, linia {numer}: plik {plik!r} musi byc niepusta sciezka wzgledna bez segmentu '..'"
        )
    if not (cyfry.isascii() and cyfry.isdigit()) or int(cyfry) < 1:
        raise BladNarzedzia(
            f"lista kodu 3, linia {numer}: numer linii {cyfry!r} musi byc liczba calkowita >= 1 (same cyfry ASCII)"
        )
    return plik, int(cyfry)


def wczytaj_liste_kodu_3(sciezka: Path) -> list[tuple[str, int]]:
    """Wpisy `(plik, linia)` z pliku z wyjsciem kodu 3; kazde odstepstwo od formatu to `BladNarzedzia`."""
    linie = _odczytaj_utf8(sciezka, f"lista kodu 3 {sciezka}").decode("utf-8").splitlines()
    if linie[:1] != [NAGLOWEK_BEZ_MUTANTOW]:
        raise BladNarzedzia(f"lista kodu 3: pierwsza linia musi byc dokladnie {NAGLOWEK_BEZ_MUTANTOW!r}")
    wpisy = [_wpis_listy_kodu_3(linia, numer) for numer, linia in enumerate(linie[1:], start=2)]
    if not wpisy:
        raise BladNarzedzia("brak dowodu: lista kodu 3 bez wpisów")
    return wpisy


def _zakres_linii_szukaj(tresc: bytes, mutant: Mutant) -> range:
    """Linie pliku (od 1) obejmowane przez jedyne wystapienie `szukaj`: od pierwszej do ostatniego znaku."""
    szukaj = _wymagaj_dokladnie_raz(tresc, mutant)
    poczatek = tresc.index(szukaj)
    pierwsza = tresc[:poczatek].count(b"\n") + 1
    ostatnia = pierwsza + tresc[poczatek : poczatek + len(szukaj) - 1].count(b"\n")
    return range(pierwsza, ostatnia + 1)


def niepokryte_linie_kodu_3(
    korzen: Path, wpisy: list[tuple[str, int]], mutanty: list[Mutant], rownowazne: list[Rownowazny]
) -> list[tuple[str, int]]:
    """Wpisy listy bez wiersza `[[mutant]]` obejmujacego linie i bez pasujacego `[[rownowazny]]`."""
    linie_plikow: dict[str, list[str]] = {}
    tresci: dict[str, bytes] = {}
    for plik, numer in wpisy:
        if plik not in tresci:
            tresci[plik] = _odczytaj_utf8(korzen / plik, plik)
            linie_plikow[plik] = tresci[plik].decode("utf-8").split("\n")
            if linie_plikow[plik][-1] == "":
                linie_plikow[plik].pop()
        if numer > len(linie_plikow[plik]):
            raise BladNarzedzia(f"{plik}:{numer}: plik ma tylko {len(linie_plikow[plik])} linii")
    zakresy = [(m.plik, _zakres_linii_szukaj(tresci[m.plik], m)) for m in mutanty if m.plik in tresci]
    niepokryte: list[tuple[str, int]] = []
    for plik, numer in wpisy:
        przez_mutant = any(p == plik and numer in zakres for p, zakres in zakresy)
        tekst = linie_plikow[plik][numer - 1].strip()
        przez_rownowazny = any(r.plik == plik and r.linia_tekst.strip() == tekst for r in rownowazne)
        if not (przez_mutant or przez_rownowazny):
            niepokryte.append((plik, numer))
    return niepokryte


def pokrycie_kod3(korzen: Path, lista: Path, tabele: list[Path]) -> int:
    """Tryb `--pokrycie-kod3`: zwraca kod wyjscia 0/1; blad narzedzia to `BladNarzedzia`."""
    wpisy = wczytaj_liste_kodu_3(korzen / lista)
    mutanty: list[Mutant] = []
    rownowazne: list[Rownowazny] = []
    for tabela in tabele:
        z_tabeli, rownowazne_z_tabeli = wczytaj_tabele_z_rownowaznymi(korzen / tabela)
        mutanty.extend(z_tabeli)
        rownowazne.extend(rownowazne_z_tabeli)
    niepokryte = niepokryte_linie_kodu_3(korzen, wpisy, mutanty, rownowazne)
    for plik, numer in niepokryte:
        print(f"{plik}:{numer}")
    if niepokryte:
        print(
            f"brak dowodu: {len(niepokryte)} linii bez wiersza [[mutant]] i bez [[rownowazny]]"
            " - .claude/rules/test-odroznia-regule.md",
            file=sys.stderr,
        )
        return 1
    print(f"pokryto {len(wpisy)} linii bez mutantów")
    return 0


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
    parser.add_argument("tabele", nargs="*", type=Path, help="tests/mutanty_reczne/<ticket>.toml")
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
    parser.add_argument(
        "--sprawdz-wszystkie",
        action="store_const",
        const=True,
        default=False,
        help="HAR-225: sprawdz wszystkie tabele z tests/mutanty_reczne (szukaj raz, test istnieje), bez mutacji",
    )
    parser.add_argument(
        "--pokrycie-kod3",
        type=Path,
        default=None,
        metavar="LISTA",
        help="HAR-228: sprawdz, ze kazda linia z wyjscia kodu 3 mutanty_zmian.py ma wiersz [[mutant]] albo [[rownowazny]]",
    )
    args = parser.parse_args(argv)
    if args.pokrycie_kod3 is not None and (args.sprawdz_wszystkie or args.czy_front):
        parser.error("--pokrycie-kod3 nie laczy sie z --sprawdz-wszystkie ani z --czy-front")
    if args.sprawdz_wszystkie and (args.tabele or args.czy_front):
        parser.error("--sprawdz-wszystkie nie laczy sie z tabelami pozycyjnymi ani z --czy-front")
    if not args.sprawdz_wszystkie and not args.tabele:
        if args.pokrycie_kod3 is None:
            parser.error("podaj co najmniej jedna tabele (albo --sprawdz-wszystkie): brak tabeli to brak dowodu")
    korzen = args.korzen if args.korzen is not None else Path.cwd()
    if args.sprawdz_wszystkie:
        bledy: list[str] = []
        try:
            wiersze, tabele = sprawdz_wszystkie_tabele(korzen, args.limit, bledy)
        except BladNarzedzia as blad:
            bledy.append(f"blad: {blad}")
        for linia in bledy:
            print(linia, file=sys.stderr)
        if bledy:
            return KOD_BLEDU_NARZEDZIA
        print(f"sprawdzono {wiersze} wierszy w {tabele} tabelach")
        return 0
    try:
        if args.pokrycie_kod3 is not None:
            return pokrycie_kod3(korzen, args.pokrycie_kod3, args.tabele)
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

"""Bufor zdarzen runnera na dysku (HAR-45).

Zdarzenie trafia do bufora PRZED wysylka i znika dopiero po potwierdzeniu przez kokpit,
wiec znikniecie kokpitu albo restart runnera niczego nie gubi. Bufor lezy w katalogu
stanu uzytkownika, poza repozytorium (`run_dir_outside_repo`). Zapisujemy wylacznie
cialo zdarzenia: token i naglowki nigdy nie trafiaja na dysk.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ODCZEKANIE_MAX_S = 30.0
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")      # nazwa pliku: bez "/", ".." i kropek
PROG_WOLNEGO_MIEJSCA_MB = 200
_ZMIENNA_PROGU = "COCKPIT_MIN_FREE_MB"
_CYFRY_ASCII = re.compile(r"[0-9]+")                # fullmatch: "$" przepuscilby "5\n"


class BladBufora(Exception):
    """Bufor nie daje sie odczytac ani zapisac (dysk, prawa, uszkodzona struktura)."""


class BrakMiejsca(BladBufora):
    """Zapis bufora nie powiodl sie, bo na dysku nie ma miejsca (ENOSPC)."""


def _blad_zapisu(nazwa: str, e: OSError) -> BladBufora:
    klasa = BrakMiejsca if e.errno == errno.ENOSPC else BladBufora
    return klasa(f"zapis {nazwa}: {type(e).__name__}")


def min_wolne_mb() -> int:
    """Prog czytany przy kazdym wywolaniu, zeby zmiana zmiennej dzialala bez restartu."""
    surowa = os.environ.get(_ZMIENNA_PROGU)
    if not surowa:
        return PROG_WOLNEGO_MIEJSCA_MB
    if not _CYFRY_ASCII.fullmatch(surowa):
        raise ValueError(f"{_ZMIENNA_PROGU} musi byc nieujemna liczba calkowita (cyfry ASCII)")
    return int(surowa)


def wolne_mb(katalog: Path) -> int:
    """Katalog bufora moze jeszcze nie istniec - mierzymy najblizszego istniejacego przodka."""
    mierzony = katalog
    while not mierzony.exists():
        if mierzony.parent == mierzony:
            # wzgledna sciezka bez istniejacego przodka (np. usuniety cwd): bez tego
            # petla nigdy by sie nie skonczyla i zablokowala petle zdarzen runnera
            raise FileNotFoundError(errno.ENOENT, "brak istniejacego katalogu nad katalogiem stanu")
        mierzony = mierzony.parent
    return shutil.disk_usage(mierzony).free // (1024 * 1024)


def odczekanie(proba: int) -> float:
    return min(2.0**proba, ODCZEKANIE_MAX_S)


def katalog_stanu() -> Path:
    z_srodowiska = os.environ.get("COCKPIT_STATE_DIR")
    if z_srodowiska:
        return Path(z_srodowiska)
    return Path.home() / ".local" / "state" / "cockpit"


def _wpis(linia: str) -> dict | None:
    """Obiekt JSON z `run_id` i `event_id`; wszystko inne (np. linia obcieta po awarii) to None."""
    try:
        wpis = json.loads(linia)
    except (ValueError, RecursionError):
        return None
    if (
        isinstance(wpis, dict)
        and isinstance(wpis.get("run_id"), str)
        and isinstance(wpis.get("event_id"), str)
    ):
        return wpis
    return None


def _identyfikatory(linie: list[str]) -> set[str]:
    return {wpis["event_id"] for linia in linie if (wpis := _wpis(linia)) is not None}


def _potwierdzone(dziennik: Path) -> set[str]:
    """Identyfikatory z dziennika potwierdzen; linia, ktora nie jest napisem JSON, jest pomijana."""
    try:
        tekst = dziennik.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeDecodeError) as e:
        raise BladBufora(f"odczyt {dziennik.name}: {type(e).__name__}") from e
    wynik: set[str] = set()
    for linia in _linie(tekst):
        try:
            wpis = json.loads(linia)
        except (ValueError, RecursionError):
            continue
        if isinstance(wpis, str):
            wynik.add(wpis)
    return wynik


def _dopisz_do_pliku(plik: Path, dane: bytes) -> None:
    """Trwale dopisanie linii: po bledzie plik wraca do poprzedniego rozmiaru. Rzuca `OSError`."""
    fd = os.open(plik, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)               # umask nie moze poszerzyc praw
        rozmiar = os.fstat(fd).st_size
        # obcieta po awarii ostatnia linia skleilaby sie z nowym wpisem
        if rozmiar and os.pread(fd, 1, rozmiar - 1) != b"\n":
            dane = b"\n" + dane
        try:
            # surowy deskryptor, bez bufora Pythona: po nieudanym zapisie nic nie
            # czeka w buforze na ponowna probe przy zamknieciu pliku
            zapisano = 0
            while zapisano < len(dane):
                zapisano += os.write(fd, dane[zapisano:])
            os.fsync(fd)
        except OSError:
            # nieudane zdarzenie nie moze zostac w buforze: po restarcie wrocilby
            # do kokpitu jako dosylka runu, ktory juz przerwalismy
            try:
                os.ftruncate(fd, rozmiar)
            except OSError:
                pass                # dysk pelny: zostaje obciety wpis, czytnik go pomija
            raise
    finally:
        os.close(fd)


@dataclass
class _Stan:
    """Pamiec o pliku zdarzen runu: tylko podpowiada, kiedy go przepisac."""

    inode: int
    przeczytano: int                       # bajty do konca ostatniej pelnej linii
    zdarzenia: set[str]
    potwierdzone: set[str]

    def pora_przepisac(self, event_id: str) -> bool:
        """Prog: potwierdzonych (razem z biezacym) co najmniej tyle, ile czekajacych."""
        potwierdzonych = len(self.potwierdzone) + (event_id not in self.potwierdzone)
        return 2 * potwierdzonych >= len(self.zdarzenia)


class Bufor:
    def __init__(self, katalog: Path | None = None) -> None:
        self.katalog = katalog if katalog is not None else katalog_stanu() / "bufor"
        self._stany: dict[str, _Stan] = {}

    def _plik(self, run_id: object) -> Path:
        # run_id pochodzi z kokpitu: bez sprawdzenia "../x" wyszloby poza katalog bufora
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise BladBufora("niedozwolony run_id zdarzenia")
        return self.katalog / f"{run_id}.ndjson"

    def dopisz(self, zdarzenie: dict) -> None:
        plik = self._plik(zdarzenie.get("run_id"))
        if not isinstance(zdarzenie.get("event_id"), str) or not zdarzenie["event_id"]:
            raise BladBufora("zdarzenie bez event_id")
        dane = (json.dumps(zdarzenie, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.katalog.mkdir(mode=0o700, parents=True, exist_ok=True)
            _dopisz_do_pliku(plik, dane)
        except OSError as e:
            raise _blad_zapisu(plik.name, e) from e

    def potwierdz(self, run_id: str, event_id: str) -> None:
        """Dopisuje potwierdzenie do `<run_id>.ack`; plik zdarzen przepisuje dopiero po progu,
        wiec koszt jednego potwierdzenia jest zamortyzowany staly."""
        plik = self._plik(run_id)
        dziennik = plik.with_suffix(".ack")
        stan = self._odswiez(plik)
        if stan is None or event_id not in stan.zdarzenia:
            wczytane = self._wczytaj(plik)
            if wczytane is None or event_id not in wczytane[0].zdarzenia:
                return
            stan = wczytane[0]
        if stan.pora_przepisac(event_id):
            # o przepisaniu i usunieciu decyduje to, co jest teraz na dysku, nigdy pamiec
            wczytane = self._wczytaj(plik)
            if wczytane is None or event_id not in wczytane[0].zdarzenia:
                return
            stan, linie = wczytane
            if stan.pora_przepisac(event_id):
                self._przepisz(plik, linie, stan.potwierdzone | {event_id})
                return
        if event_id in stan.potwierdzone:
            return
        try:
            _dopisz_do_pliku(dziennik, (json.dumps(event_id) + "\n").encode("utf-8"))
        except OSError as e:
            raise _blad_zapisu(dziennik.name, e) from e
        stan.potwierdzone.add(event_id)

    def _wczytaj(self, plik: Path) -> tuple[_Stan, list[str]] | None:
        """Pelny odczyt pliku zdarzen i dziennika; None, gdy pliku zdarzen nie ma."""
        self._stany.pop(plik.name, None)
        try:
            with open(plik, "rb") as f:
                inode = os.fstat(f.fileno()).st_ino
                dane = f.read()
            linie = _linie(dane.decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            raise BladBufora(f"odczyt {plik.name}: {type(e).__name__}") from e
        zdarzenia = _identyfikatory(linie)
        # osierocone wpisy dziennika (spoza pliku) nie licza sie do progu
        potwierdzone = _potwierdzone(plik.with_suffix(".ack")) & zdarzenia
        stan = _Stan(inode, dane.rfind(b"\n") + 1, zdarzenia, potwierdzone)
        self._stany[plik.name] = stan
        return stan, linie

    def _odswiez(self, plik: Path) -> _Stan | None:
        """Doczytuje pelne linie dopisane od ostatniego odczytu; None = potrzebny pelny odczyt."""
        stan = self._stany.get(plik.name)
        if stan is None:
            return None
        try:
            with open(plik, "rb") as f:
                opis = os.fstat(f.fileno())
                if opis.st_ino != stan.inode or opis.st_size < stan.przeczytano:
                    return None
                f.seek(stan.przeczytano)
                ogon = f.read()
            koniec = ogon.rfind(b"\n") + 1
            stan.zdarzenia |= _identyfikatory(_linie(ogon[:koniec].decode("utf-8")))
            stan.przeczytano += koniec
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            raise BladBufora(f"odczyt {plik.name}: {type(e).__name__}") from e
        return stan

    def _przepisz(self, plik: Path, linie: list[str], potwierdzone: set[str]) -> None:
        """Kompaktacja. Awaria miedzy krokami moze skonczyc sie tylko ponowna wysylka."""
        # shortcut: dwa PROCESY runnera na jednym katalogu moga zgubic zdarzenie dopisane
        # miedzy odczytem a `replace`; blokada pliku, gdy bedzie to wspierany uklad
        self._stany.pop(plik.name, None)
        pozostale = [
            linia for linia in linie
            if (wpis := _wpis(linia)) is None or wpis["event_id"] not in potwierdzone
        ]
        dziennik = plik.with_suffix(".ack")
        tmp = plik.with_name(f".{plik.name}.{os.getpid()}.tmp")
        try:
            if not pozostale:
                # najpierw dziennik: osierocony polknalby nowe zdarzenie o tym samym id
                dziennik.unlink(missing_ok=True)
                plik.unlink(missing_ok=True)
                return
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                os.fchmod(fd, 0o600)
                f.write(("\n".join(pozostale) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(fd)
            os.replace(tmp, plik)
            # dopiero po `replace`: zostawione wpisy nie wskazuja juz na zadne zdarzenie
            dziennik.unlink(missing_ok=True)
        except OSError as e:
            with contextlib.suppress(OSError):  # sprzatanie nie moze zaslonic pierwotnego bledu
                tmp.unlink(missing_ok=True)
            raise _blad_zapisu(plik.name, e) from e

    def niepotwierdzone(self) -> list[dict]:
        try:
            nazwy = sorted(n for n in os.listdir(self.katalog) if n.endswith(".ndjson"))
        except FileNotFoundError:
            return []
        except OSError as e:
            raise BladBufora(f"katalog bufora: {type(e).__name__}") from e
        wynik: list[dict] = []
        for nazwa in nazwy:
            try:
                tekst = (self.katalog / nazwa).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise BladBufora(f"odczyt {nazwa}: {type(e).__name__}") from e
            potwierdzone = _potwierdzone((self.katalog / nazwa).with_suffix(".ack"))
            for numer, linia in enumerate(_linie(tekst), start=1):
                wpis = _wpis(linia)
                if wpis is None:
                    # bez tresci linii: moze niesc dane zdarzenia
                    print(f"bufor: pominiety uszkodzony wpis {nazwa}:{numer}", file=sys.stderr)
                elif wpis["event_id"] not in potwierdzone:
                    wynik.append(wpis)
        return wynik


def _linie(tekst: str) -> list[str]:
    """Dzielimy po "\\n", nie `splitlines()`: tekst zdarzenia moze zawierac np. U+2028."""
    linie = tekst.split("\n")
    if linie[-1] == "":
        linie.pop()
    return linie

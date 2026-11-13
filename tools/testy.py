"""pytest z limitem czasu bez postepu (HAR-240).

    python tools/testy.py [--bez-postepu SEKUNDY] [argumenty pytesta ...]

Uruchamia `python -m pytest <argumenty>` w NOWEJ grupie procesow (regula `testy-podprocesow`),
przekazuje jego wyjscie na zywo i przerywa caly przebieg, gdy przez `--bez-postepu` sekund
(domyslnie 1200 = 20 min) nie przyszla ani nowa linia wyniku, ani start ani koniec testu.
Limit liczy sie od ostatniego postepu, nie od startu calosci: dlugi, ale idacy przebieg nie jest
przerywany. Start i koniec testow raportuje wtyczka `_testy_zdarzenia` (takze pod xdist),
dzieki czemu po przerwaniu wiadomo, ktore testy byly w toku.

Kody wyjscia: kod pytesta, gdy przebieg sie zakonczyl sam; 2 przy zawieszeniu (stdout:
`zawieszenie: <node id>`, po jednej linii na test w toku) albo bledzie narzedzia (stderr);
143 po SIGTERM do skryptu. Pytest zabity sygnalem (brak kodu) to blad narzedzia, nie zielony wynik.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
LIMIT_DOMYSLNY = 1200.0
KROK_SPRAWDZANIA = 0.1
CZEKANIE_PO_TERM = 5.0
KOD_ZAWIESZENIA = 2
KOD_BLEDU = 2
WTYCZKA = "_testy_zdarzenia"
ZMIENNA_ZDARZEN = "COCKPIT_TESTY_ZDARZENIA"


class BladNarzedzia(Exception):
    """Blad dziedziny skryptu (kod wyjscia 2)."""


def _limit(tekst: str) -> float:
    try:
        wartosc = float(tekst)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--bez-postepu musi byc liczba sekund (jest: {tekst!r})") from None
    if not wartosc > 0 or wartosc == float("inf"):
        raise argparse.ArgumentTypeError(f"--bez-postepu musi byc skonczona liczba > 0 (jest: {tekst!r})")
    return wartosc


class _Stan:
    """Wspolny stan postepu: czas ostatniego postepu i testy w toku."""

    def __init__(self) -> None:
        self.ostatni_postep = time.monotonic()
        self.w_toku: dict[str, None] = {}
        self.blokada = threading.Lock()

    def postep(self) -> None:
        with self.blokada:
            self.ostatni_postep = time.monotonic()

    def bez_postepu_od(self) -> float:
        with self.blokada:
            return time.monotonic() - self.ostatni_postep

    def zdarzenie(self, znak: str, nodeid: str) -> None:
        with self.blokada:
            if znak == "S":
                self.w_toku[nodeid] = None
            else:
                self.w_toku.pop(nodeid, None)
            self.ostatni_postep = time.monotonic()

    def testy_w_toku(self) -> list[str]:
        with self.blokada:
            return list(self.w_toku)


def _czytaj_wyjscie(strumien: object, stan: _Stan) -> None:
    for surowa in strumien:  # type: ignore[attr-defined]
        sys.stdout.buffer.write(surowa)
        sys.stdout.buffer.flush()
        stan.postep()


def _wczytaj_zdarzenia(sciezka: Path, przesuniecie: int, stan: _Stan) -> int:
    """Czyta nowe, KOMPLETNE linie pliku zdarzen; zwraca nowe przesuniecie."""
    with open(sciezka, "rb") as plik:
        plik.seek(przesuniecie)
        dane = plik.read()
    koniec = dane.rfind(b"\n")
    if koniec < 0:
        return przesuniecie
    for linia in dane[: koniec + 1].decode("utf-8", errors="replace").splitlines():
        znak, _, nodeid = linia.partition(" ")
        if znak in ("S", "K") and nodeid:
            stan.zdarzenie(znak, nodeid)
    return przesuniecie + koniec + 1


def _zabij_grupe(proc: subprocess.Popen, sygnal: int) -> None:
    try:
        os.killpg(proc.pid, sygnal)
    except ProcessLookupError:
        pass


def _zakoncz_grupe(proc: subprocess.Popen) -> None:
    """SIGTERM do WLASNEJ grupy pytesta, po `CZEKANIE_PO_TERM` SIGKILL; potem koniec lidera."""
    _zabij_grupe(proc, signal.SIGTERM)
    try:
        proc.wait(CZEKANIE_PO_TERM)
    except subprocess.TimeoutExpired:
        pass
    _zabij_grupe(proc, signal.SIGKILL)
    proc.wait()


def _srodowisko(plik_zdarzen: Path) -> dict[str, str]:
    srodowisko = dict(os.environ)
    narzedzia = str(Path(__file__).resolve().parent)
    stare = srodowisko.get("PYTHONPATH")
    srodowisko["PYTHONPATH"] = os.pathsep.join([narzedzia, *([stare] if stare else [])])
    srodowisko[ZMIENNA_ZDARZEN] = str(plik_zdarzen)
    return srodowisko


def uruchom(argumenty_pytesta: list[str], limit: float, cwd: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="cockpit-testy-") as katalog:
        plik_zdarzen = Path(katalog) / "zdarzenia.log"
        plik_zdarzen.touch()
        polecenie = [sys.executable, "-m", "pytest", "-p", WTYCZKA, *argumenty_pytesta]
        try:
            proc = subprocess.Popen(
                polecenie,
                cwd=cwd,
                env=_srodowisko(plik_zdarzen),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as blad:
            raise BladNarzedzia(f"nie udalo sie uruchomic pytesta: {blad}") from blad
        stan = _Stan()
        czytnik = threading.Thread(target=_czytaj_wyjscie, args=(proc.stdout, stan), daemon=True)
        przesuniecie = 0
        try:
            czytnik.start()
            while proc.poll() is None:
                przesuniecie = _wczytaj_zdarzenia(plik_zdarzen, przesuniecie, stan)
                if stan.bez_postepu_od() >= limit:
                    w_toku = stan.testy_w_toku()
                    _zakoncz_grupe(proc)
                    czytnik.join(CZEKANIE_PO_TERM)
                    _wypisz_zawieszenie(w_toku, limit)
                    return KOD_ZAWIESZENIA
                time.sleep(KROK_SPRAWDZANIA)
        except BaseException:
            _zakoncz_grupe(proc)
            raise
        # Potomek, ktory przezyl lidera grupy, trzyma koniec potoku: zabijamy grupe PRZED czekaniem na
        # czytnik, a samo czekanie ma limit - skrypt lapiacy zawieszenia nie moze sam sie zawiesic.
        _zabij_grupe(proc, signal.SIGKILL)
        czytnik.join(CZEKANIE_PO_TERM)
        if proc.returncode < 0:
            raise BladNarzedzia(f"pytest zabity sygnalem {-proc.returncode} - brak wyniku testow")
        return proc.returncode


def _wypisz_zawieszenie(w_toku: list[str], limit: float) -> None:
    print(f"testy.py: brak postepu przez {limit:g} s - przerywam przebieg", flush=True)
    if not w_toku:
        print("zawieszenie: brak testu w toku (zbieranie testow albo fixture poza testem)", flush=True)
        return
    for nodeid in w_toku:
        print(f"zawieszenie: {nodeid}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="pytest z limitem czasu bez postepu; pozostale argumenty ida do pytesta",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--bez-postepu",
        type=_limit,
        default=LIMIT_DOMYSLNY,
        metavar="SEKUNDY",
        help=f"limit czasu bez nowej linii wyniku ani startu testu (domyslnie {LIMIT_DOMYSLNY:g})",
    )
    argumenty, pytest_argumenty = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    def _na_term(_numer: int, _ramka: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _na_term)
    try:
        return uruchom(pytest_argumenty, argumenty.bez_postepu, KORZEN)
    except BladNarzedzia as blad:
        print(f"BLAD: {blad}", file=sys.stderr)
        return KOD_BLEDU
    except OSError as blad:
        # Katalog tymczasowy, plik zdarzen (pelny dysk, brak praw do TMPDIR): brak dowodu, nie czerwony test.
        print(f"BLAD: operacja na pliku nie powiodla sie: {blad}", file=sys.stderr)
        return KOD_BLEDU


if __name__ == "__main__":
    sys.exit(main())

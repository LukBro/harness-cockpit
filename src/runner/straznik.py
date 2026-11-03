"""Straznik agenta: zabija grupe agenta, gdy runner umrze (HAR-85).

    python -m runner.straznik [--status-fd N] --cwd <katalog> -- <argv agenta>

`start_new_session=True` odcina agenta od sygnalow runnera, wiec po SIGKILL runnera
agent zostawalby sierota. Straznik jest procesem posrednim: agent dziala w jego
wlasnej grupie (pid == pgid), a straznik czyta swoje stdin (potok od runnera).
EOF na stdin (runner zamknal potok albo umarl) lub SIGTERM -> SIGTERM do grupy
agenta, po CZAS_NA_ZAKONCZENIE_S SIGKILL, wyjscie.

Kod wyjscia: kod agenta; agent zabity sygnalem N -> 128+N. stdout/stderr agenta to
deskryptory straznika (dziedziczone), wiec wyjscie przechodzi bez zmian.

Blad startu agenta nie idzie kodem wyjscia (agent tez moglby go zwrocic): gdy podano
`--status-fd`, straznik zapisuje tam komunikat bledu; udany start zamyka deskryptor
bez danych. Bez `--status-fd` blad idzie na stderr i kod 127.

Srodowisko agenta: to, ktore mial runner przed dodaniem PYTHONPATH kokpitu
(`_srodowisko_agenta`, zmienna `RUNNER_PYTHONPATH_AGENTA`).

Tylko stdlib, bez logging. Nie uruchamiac jako `python src/runner/straznik.py`:
sys.path[0] = src/runner zasloniloby stdlib modulem runner/secrets.py.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping

CZAS_NA_ZAKONCZENIE_S = 5     # tyle agent ma na SIGTERM, potem SIGKILL
KOD_BLEDU_STARTU = 127
KROK_PETLI_S = 0.05
# Runner dopisuje katalog kokpitu do PYTHONPATH straznika (zeby ten importowal kod
# z biezacego drzewa). Pierwotna wartosc przychodzi tu, a agent dostaje ja z powrotem:
# inaczej narzedzia Pythona w repozytorium uzytkownika widzialyby pakiety kokpitu,
# a wyjscie kroku roznilby sie od uruchomienia bez straznika.
ZMIENNA_PYTHONPATH_AGENTA = "RUNNER_PYTHONPATH_AGENTA"


def _srodowisko_agenta(srodowisko: Mapping[str, str]) -> dict[str, str]:
    """Kopia srodowiska dla agenta: PYTHONPATH sprzed dodania katalogu kokpitu.

    Bez zmiennej ZMIENNA_PYTHONPATH_AGENTA (straznik uruchomiony recznie) srodowisko
    zostaje bez zmian. Pusta wartosc = runner nie mial PYTHONPATH.
    """
    kopia = dict(srodowisko)
    if ZMIENNA_PYTHONPATH_AGENTA not in kopia:
        return kopia
    pierwotny = kopia.pop(ZMIENNA_PYTHONPATH_AGENTA)
    if pierwotny:
        kopia["PYTHONPATH"] = pierwotny
    else:
        kopia.pop("PYTHONPATH", None)
    return kopia


def _kod_wyjscia(zwrocony: int) -> int:
    """Popen daje -N dla procesu zabitego sygnalem N; powloka mowi 128+N."""
    if zwrocony < 0:
        return 128 - zwrocony
    return zwrocony


def _grupa_istnieje(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        # macOS: grupa po odebraniu lidera daje PermissionError, nie ProcessLookupError
        return False
    return True


def _wyslij_sygnal(pgid: int, sygnal: int) -> None:
    try:
        os.killpg(pgid, sygnal)
    except (ProcessLookupError, PermissionError):
        pass


def _zakoncz_grupe(agent: subprocess.Popen) -> int:
    """SIGTERM do grupy, po karencji SIGKILL. Zwraca kod wyjscia agenta."""
    pgid = agent.pid  # start_new_session: pgid == pid lidera
    _wyslij_sygnal(pgid, signal.SIGTERM)
    koniec = time.monotonic() + CZAS_NA_ZAKONCZENIE_S
    while time.monotonic() < koniec:
        agent.poll()  # odbiera lidera, inaczej zombie trzyma grupe przy zyciu
        if not _grupa_istnieje(pgid):
            break
        time.sleep(KROK_PETLI_S)
    # takze po smierci lidera: wnuki mogly przezyc SIGTERM
    _wyslij_sygnal(pgid, signal.SIGKILL)
    return _kod_wyjscia(agent.wait())


def _czytaj_do_eof(zdarzenie: threading.Event, fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass  # zamkniete albo bledne stdin = runner nieosiagalny
    finally:
        zdarzenie.set()


def _zglos_blad_startu(status_fd: int | None, blad: OSError) -> int:
    komunikat = str(blad)
    if status_fd is None:
        print(f"straznik: nie można uruchomić polecenia: {komunikat}", file=sys.stderr)
        return KOD_BLEDU_STARTU
    try:
        os.write(status_fd, komunikat.encode(errors="replace"))
    finally:
        os.close(status_fd)
    return KOD_BLEDU_STARTU


def _uzbroj(zdarzenie: threading.Event, fd_wejscia: int) -> None:
    signal.signal(signal.SIGTERM, lambda numer, ramka: zdarzenie.set())
    threading.Thread(target=_czytaj_do_eof, args=(zdarzenie, fd_wejscia), daemon=True).start()


def _uruchom(
    argv_agenta: list[str],
    katalog: str,
    status_fd: int | None,
    zdarzenie: threading.Event,
) -> int:
    try:
        agent = subprocess.Popen(
            argv_agenta,
            cwd=katalog,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            env=_srodowisko_agenta(os.environ),
        )
    except OSError as blad:
        return _zglos_blad_startu(status_fd, blad)
    if status_fd is not None:
        os.close(status_fd)

    while True:
        zwrocony = agent.poll()
        if zwrocony is not None:
            return _kod_wyjscia(zwrocony)
        if zdarzenie.wait(KROK_PETLI_S):
            return _zakoncz_grupe(agent)


def main(argv: list[str], fd_wejscia: int = 0) -> int:
    if "--" not in argv:
        print("straznik: brak '--' przed poleceniem agenta", file=sys.stderr)
        return 2
    granica = argv.index("--")
    parser = argparse.ArgumentParser(prog="runner.straznik")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--status-fd", type=int, default=None)
    opcje = parser.parse_args(argv[:granica])
    polecenie = argv[granica + 1:]
    if not polecenie:
        print("straznik: puste polecenie agenta", file=sys.stderr)
        return 2
    zdarzenie = threading.Event()
    _uzbroj(zdarzenie, fd_wejscia)
    return _uruchom(polecenie, opcje.cwd, opcje.status_fd, zdarzenie)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

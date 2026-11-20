#!/usr/bin/env python3
"""Kolejka testow miedzy worktree jednego repo (MON-251). Tylko stdlib.

Sygnatura:
    python3 <katalog>/scripts/test_lock.py [--timeout <sekundy>] [--sloty <N>] -- <komenda> [argumenty...]

Skrypt bierze wylaczny `flock` na `<absolutny git-common-dir>/monolynx/test.lock`, uruchamia komende
(argv po `--`, bez powloki) i zwalnia blokade po jej zakonczeniu. Wszystkie worktree jednego repo maja
ten sam git-common-dir, wiec dwa przebiegi testow z roznych worktree nie biegna naraz na wspolnej bazie.
Oczekujacy czeka w kolejce jadra (blokujacy `flock`), nie w petli `LOCK_NB` + `sleep`: kolejnosc
wybudzania zalezy od jadra, wiec kolejka jest "w praktyce FIFO", bez gwarancji.

Opcje:
    --timeout <s>   maksymalny czas czekania na blokade, liczba calkowita 0..86400 (domyslnie 3600 = 60 min).
                    0 = jedna proba bez czekania. Dotyczy tylko czekania na blokade, nie czasu komendy.
    --sloty <N>     semafor (audyt 2026-11-19, krok 7): najwyzej N komend naraz, liczba calkowita 1..64
                    (domyslnie 1 = dotychczasowa blokada `test.lock` z kolejka jadra). Przy N > 1 slotami sa
                    pliki `test.lock.<i>`; oczekujacy sprawdza je co 0,5 s, wiec kolejnosc nie jest FIFO.

Kody wyjscia:
    0..N   kod wyjscia komendy 1:1
    128+N  komenda zabita sygnalem N
    127    komendy nie ma (FileNotFoundError)
    126    komendy nie da sie uruchomic (PermissionError, brak uprawnienia do wykonania)
    75     timeout czekania na blokade, komenda NIE zostala uruchomiona
    64     blad uzycia: brak `--`, pusta komenda, `--timeout` nieliczbowy/ujemny/ulamkowy/bez wartosci/powyzej 86400,
           `--sloty` poza 1..64, nieznana flaga przed `--`, katalog poza repo git albo blad gita (komunikat `test_lock: <powod>`)

Zamrozone linie na stderr (kontrakt; skille i testy dopasowuja je doslownie):
    TEST-LOCK: acquired
    TEST-LOCK: acquired slot=<i>             (tylko przy --sloty > 1)
    TEST-LOCK: waiting holder=<h0>,<h1>,...   (przy --sloty > 1, po jednym wpisie na slot)
    TEST-LOCK: waiting holder=<pid>/<nazwa-katalogu-worktree>
    TEST-LOCK: waiting holder=unknown
    TEST-LOCK: timeout

Ograniczenia:
- Komenda zwracajaca sama 64 albo 75 jest odroznialna od bledu wrappera tylko po liniach `TEST-LOCK:`
  i `test_lock:` na stderr.
- Po SIGKILL wrappera blokada znika (jadro zamyka deskryptor), a dziecko moze biec dalej bez blokady.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time

EXIT_USAGE = 64
EXIT_TIMEOUT = 75
EXIT_CANNOT_EXECUTE = 126
EXIT_NOT_FOUND = 127
DEFAULT_TIMEOUT = 3600
# setitimer odrzuca bardzo duze wartosci (ItimerError/OverflowError), wiec zakres pilnujemy w parsowaniu.
MAX_TIMEOUT = 86400
MAX_SLOTY = 64
ODSTEP_PROB_S = 0.5
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class UsageError(Exception):
    pass


class LockTimeoutError(Exception):
    pass


def _git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageError(f"nie udało się uruchomić gita: {exc}") from exc
    if result.returncode != 0:
        raise UsageError(f"git {' '.join(args)} zakończył się kodem {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def lock_path(cwd: str) -> str:
    common = _git(cwd, "rev-parse", "--git-common-dir")
    if not common:
        raise UsageError("git nie zwrócił git-common-dir")
    # Wynik bywa wzgledny wobec cwd (`.git` w glownym checkoucie), a worktree dostaje sciezke absolutna.
    return os.path.join(os.path.abspath(os.path.join(cwd, common)), "monolynx", "test.lock")


def _parse_args(argv: list[str]) -> tuple[int, int, list[str]]:
    timeout = DEFAULT_TIMEOUT
    sloty = 1
    index = 0
    while index < len(argv) and argv[index] != "--":
        if argv[index] not in ("--timeout", "--sloty"):
            raise UsageError(f"nieznana opcja: {argv[index]}")
        if index + 1 >= len(argv) or not re.fullmatch(r"[0-9]+", argv[index + 1]):
            raise UsageError(f"{argv[index]} wymaga nieujemnej liczby całkowitej")
        wartosc = int(argv[index + 1])
        if argv[index] == "--sloty":
            if not 1 <= wartosc <= MAX_SLOTY:
                raise UsageError(f"--sloty musi być w zakresie 1..{MAX_SLOTY}")
            sloty = wartosc
        else:
            if wartosc > MAX_TIMEOUT:
                raise UsageError(f"--timeout nie może przekraczać {MAX_TIMEOUT} s (24 h)")
            timeout = wartosc
        index += 2
    if index >= len(argv):
        raise UsageError("brak `--` przed komendą")
    command = argv[index + 1 :]
    if not command:
        raise UsageError("pusta komenda po `--`")
    return timeout, sloty, command


def _read_holder(fd: int) -> str:
    try:
        raw = os.pread(fd, 4096, 0)
        data = json.loads(raw.decode("utf-8"))
        pid = data["pid"]
        worktree = data["worktree"]
        if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(worktree, str):
            return f"{pid}/{os.path.basename(worktree.rstrip('/')) or '?'}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "unknown"


def _on_alarm(_signum: int, _frame: object) -> None:
    raise LockTimeoutError


def _acquire(fd: int, timeout: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        pass
    if timeout == 0:
        return False
    print(f"TEST-LOCK: waiting holder={_read_holder(fd)}", file=sys.stderr, flush=True)
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except LockTimeoutError:
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _acquire_slot(fds: list[int], timeout: int) -> int | None:
    """Indeks zajetego slotu albo None po timeoucie; proby co ODSTEP_PROB_S (flock nie czeka na kilka plikow)."""
    koniec = time.monotonic() + timeout
    pierwsza = True
    while True:
        for i, fd in enumerate(fds):
            if _try_lock(fd):
                return i
        if time.monotonic() >= koniec:
            return None
        if pierwsza:
            holders = ",".join(_read_holder(fd) for fd in fds)
            print(f"TEST-LOCK: waiting holder={holders}", file=sys.stderr, flush=True)
            pierwsza = False
        time.sleep(min(ODSTEP_PROB_S, max(0.0, koniec - time.monotonic())))


def _run(command: list[str]) -> int:
    try:
        child = subprocess.Popen(command)
    except FileNotFoundError:
        print(f"test_lock: nie znaleziono komendy: {command[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except OSError as exc:
        print(f"test_lock: nie można uruchomić komendy {command[0]}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_EXECUTE

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    previous = {sig: signal.signal(sig, forward) for sig in FORWARDED_SIGNALS}
    try:
        code = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 128 - code if code < 0 else code


def main(argv: list[str]) -> int:
    try:
        timeout, sloty, command = _parse_args(argv)
        cwd = os.getcwd()
        path = lock_path(cwd)
        try:
            toplevel = _git(cwd, "rev-parse", "--show-toplevel")
        except UsageError:
            toplevel = cwd
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Bez O_TRUNC: oczekujacy nie moze skasowac wpisu holdera, ktory czyta.
        sciezki = [path] if sloty == 1 else [f"{path}.{i}" for i in range(sloty)]
        fds: list[int] = []
        try:
            for s in sciezki:
                fds.append(os.open(s, os.O_RDWR | os.O_CREAT, 0o644))
        except OSError:
            for f in fds:
                os.close(f)
            raise
    except UsageError as exc:
        print(f"test_lock: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"test_lock: nie można otworzyć pliku blokady: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if sloty == 1:
            slot = 0 if _acquire(fds[0], timeout) else None
        else:
            slot = _acquire_slot(fds, timeout)
        if slot is None:
            print("TEST-LOCK: timeout", file=sys.stderr, flush=True)
            return EXIT_TIMEOUT
        fd = fds[slot]
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps({"pid": os.getpid(), "worktree": toplevel}).encode("utf-8"), 0)
        except OSError:
            pass
        print("TEST-LOCK: acquired" + (f" slot={slot}" if sloty > 1 else ""), file=sys.stderr, flush=True)
        try:
            return _run(command)
        finally:
            # Wpis czyscimy przed zwolnieniem, zeby oczekujacy nie zglosil nieaktualnego holdera.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
    finally:
        for f in fds:
            os.close(f)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

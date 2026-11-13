#!/usr/bin/env python3
"""Kolejka testow miedzy worktree jednego repo (MON-251). Tylko stdlib.

Sygnatura:
    python3 <katalog>/scripts/test_lock.py [--timeout <sekundy>] -- <komenda> [argumenty...]

Skrypt bierze wylaczny `flock` na `<absolutny git-common-dir>/monolynx/test.lock`, uruchamia komende
(argv po `--`, bez powloki) i zwalnia blokade po jej zakonczeniu. Wszystkie worktree jednego repo maja
ten sam git-common-dir, wiec dwa przebiegi testow z roznych worktree nie biegna naraz na wspolnej bazie.
Oczekujacy czeka w kolejce jadra (blokujacy `flock`), nie w petli `LOCK_NB` + `sleep`: kolejnosc
wybudzania zalezy od jadra, wiec kolejka jest "w praktyce FIFO", bez gwarancji.

Opcje:
    --timeout <s>   maksymalny czas czekania na blokade, liczba calkowita 0..86400 (domyslnie 3600 = 60 min).
                    0 = jedna proba bez czekania. Dotyczy tylko czekania na blokade, nie czasu komendy.

Kody wyjscia:
    0..N   kod wyjscia komendy 1:1
    128+N  komenda zabita sygnalem N
    127    komendy nie ma (FileNotFoundError)
    126    komendy nie da sie uruchomic (PermissionError, brak uprawnienia do wykonania)
    75     timeout czekania na blokade, komenda NIE zostala uruchomiona
    64     blad uzycia: brak `--`, pusta komenda, `--timeout` nieliczbowy/ujemny/ulamkowy/bez wartosci/powyzej 86400,
           nieznana flaga przed `--`, katalog poza repo git albo blad gita (komunikat `test_lock: <powod>`)

Zamrozone linie na stderr (kontrakt; skille i testy dopasowuja je doslownie):
    TEST-LOCK: acquired
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

EXIT_USAGE = 64
EXIT_TIMEOUT = 75
EXIT_CANNOT_EXECUTE = 126
EXIT_NOT_FOUND = 127
DEFAULT_TIMEOUT = 3600
# setitimer odrzuca bardzo duze wartosci (ItimerError/OverflowError), wiec zakres pilnujemy w parsowaniu.
MAX_TIMEOUT = 86400
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


def _parse_args(argv: list[str]) -> tuple[int, list[str]]:
    timeout = DEFAULT_TIMEOUT
    index = 0
    while index < len(argv) and argv[index] != "--":
        if argv[index] != "--timeout":
            raise UsageError(f"nieznana opcja: {argv[index]}")
        if index + 1 >= len(argv) or not re.fullmatch(r"[0-9]+", argv[index + 1]):
            raise UsageError("--timeout wymaga nieujemnej liczby całkowitej")
        timeout = int(argv[index + 1])
        if timeout > MAX_TIMEOUT:
            raise UsageError(f"--timeout nie może przekraczać {MAX_TIMEOUT} s (24 h)")
        index += 2
    if index >= len(argv):
        raise UsageError("brak `--` przed komendą")
    command = argv[index + 1 :]
    if not command:
        raise UsageError("pusta komenda po `--`")
    return timeout, command


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
        timeout, command = _parse_args(argv)
        cwd = os.getcwd()
        path = lock_path(cwd)
        try:
            toplevel = _git(cwd, "rev-parse", "--show-toplevel")
        except UsageError:
            toplevel = cwd
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Bez O_TRUNC: oczekujacy nie moze skasowac wpisu holdera, ktory czyta.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except UsageError as exc:
        print(f"test_lock: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"test_lock: nie można otworzyć pliku blokady: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if not _acquire(fd, timeout):
            print("TEST-LOCK: timeout", file=sys.stderr, flush=True)
            return EXIT_TIMEOUT
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps({"pid": os.getpid(), "worktree": toplevel}).encode("utf-8"), 0)
        except OSError:
            pass
        print("TEST-LOCK: acquired", file=sys.stderr, flush=True)
        try:
            return _run(command)
        finally:
            # Wpis czyscimy przed zwolnieniem, zeby oczekujacy nie zglosil nieaktualnego holdera.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

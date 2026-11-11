#!/usr/bin/env python3
"""Wybor backendu krytyka (Codex albo Claude) i uruchomienie `codex exec` (MON-274).

Skill `work` uruchamia `preflight` raz na starcie, a gdy backend to Codex, wola
`run` zamiast subagenta krytyka. Skrypt niczego nie zapisuje na platformie, nie
interpretuje werdyktu (APPROVED / NEEDS WORK ocenia Team Manager) i nie wywoluje
`codex` poza dwiema sondami i `codex exec`.

Wywolanie:

    critic_backend.py [preflight]
    critic_backend.py run --prompt-file P --out O --cwd D [--timeout S]

`preflight` (takze bez argumentow): zero albo jedna linia `CRITIC-HINT: ...`
(powod wyboru Claude'a przy `backend=claude` albo niepotwierdzona sonda w trybie
wymuszonym), potem ZAMROZONA ostatnia linia stdout:

    CRITIC: backend=codex|claude codex=<wersja|none> auth=ok|missing model=<id|inherit>

- sondy: `shutil.which("codex")`, `codex --version`, `codex login status`
  (timeout kazdej 30 s, stala `_PROBE_TIMEOUT`); zalogowany = kod 0 i `Logged in` w stdout+stderr
- sonda, ktora nie odpowiedziala w czasie albo nie wystartowala, ma wlasna podpowiedz
  (`Sonda codex --version|login status nie odpowiedziala w N s ...`), osobna od
  podpowiedzi `Nie udalo sie odczytac wersji` i `Codex nie jest zalogowany` (komenda
  odpowiedziala, ale bez wersji albo bez potwierdzenia logowania); pola zamrozonej
  linii (`codex=none`, `auth=missing`) sie nie zmieniaja
- `MONOLYNX_CRITIC` (po strip i lower): pusta, `auto` -> detekcja: `backend=codex`
  tylko gdy binarka jest, wersja odczytana, zalogowany i model poprawny, inaczej
  `backend=claude`; `codex` -> tryb WYMUSZONY: `backend=codex`, gdy binarka jest
  i model poprawny, a sondy tylko wypelniaja pola `codex=` i `auth=` (sondy bywaja
  niestabilne, a uzytkownik wie, ze ma Codex); gdy sonda nie potwierdzila stanu
  (brak odpowiedzi, nieczytelna wersja, brak `Logged in`), idzie jedna linia
  `CRITIC-HINT: Tryb wymuszony (MONOLYNX_CRITIC=codex): <niepotwierdzone, po "; "> - krytykiem pozostaje Codex.`;
  brak binarki albo niepoprawny model w trybie wymuszonym -> `backend=claude`;
  `claude` -> Claude (sondy dalej sie wykonuja, wiec `codex=` i `auth=` sa
  prawdziwe); kazda inna wartosc -> Claude (wariant bezpieczny) z podpowiedzia
- `MONOLYNX_CRITIC_CODEX_MODEL`: pusta -> `model=inherit` (model z
  `~/.codex/config.toml`), poprawna wg `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$` ->
  `model=<id>`, niepoprawna -> Claude i `model=inherit`

`run` wola `codex exec -C D --sandbox read-only -c model_reasoning_effort="high"
[-m MODEL] -o O -` z promptem z pliku P na stdin (bez `--worktree` i `--json`,
bez powloki). `--timeout`: liczba calkowita 30..3600 s, domyslnie 900. Walidacja
wejscia (prompt, `--cwd`, `--out`) i wylacznika idzie PRZED jakimkolwiek efektem
ubocznym; dopiero po niej stary plik `--out` jest usuwany. Po `--timeout` grupa
procesow dostaje SIGTERM, po 5 s SIGKILL. Werdykt (plik `--out`) musi zawierac
`**Code Review**` i punktacje `N/100` z N w 0..100; odrzucona albo nieczytelna
tresc trafia do `<out>.rejected` (gdy przeniesienie sie nie uda, `<out>` jest
usuwany). Po bledzie wykrytym po starcie codex plik `<out>` nie istnieje. ZAMROZONA ostatnia linia stdout:

    CRITIC-RUN: status=ok out=<sciezka>
    CRITIC-RUN: status=error reason=timeout|exit|verdict|input|disabled|missing

Stdout i stderr procesu `codex` nie trafiaja na stdout skryptu.

Kody wyjscia: `preflight` zawsze 0; `run` 0 sukces, 2 blad (powod w linii
`CRITIC-RUN:` i po polsku na stderr), 64 blad uzycia (nieznana podkomenda albo
flaga, brak wymaganej flagi, `--timeout` poza zakresem; stderr zaczyna sie od
`critic_backend: `, linii `CRITIC-RUN:` brak). Nigdy traceback.

Wyjscie i sygnatura Codex pochodza z `tests/fixtures/codex/` (codex-cli 0.160.0,
opis w README tego katalogu): `--version` -> `codex-cli 0.160.0`,
`login status` -> `Logged in using ChatGPT`, flagi `exec` z `exec-help.0.160.0.txt`,
plik `-o` z `exec-critic.0.160.0.stdout.txt`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

MIN_TIMEOUT = 30
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 900
KILL_GRACE_SECONDS = 5
_PROBE_TIMEOUT = 30

_COMMAND = "codex"
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_VERSION_IN_OUTPUT = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# WHY: `codex login status` zalogowanego konta wypisuje `Logged in using ChatGPT`
# (tests/fixtures/codex/login-status.0.160.0.txt). Porownanie jest case-sensitive,
# bo `Not logged in` zawiera `logged in` malymi, a nie zawiera `Logged in`.
_LOGGED_IN = "Logged in"
_HEADER = "**Code Review**"
_SCORE = re.compile(r"\b(\d{1,3})/100\b")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_STDERR_TAIL_LINES = 10
_TIMEOUT_DIGITS = re.compile(r"[0-9]{1,5}", re.ASCII)

_HINT_SWITCH_CLAUDE = "CRITIC-HINT: Wyłącznik MONOLYNX_CRITIC=claude - krytykiem będzie Claude."
_HINT_NO_BINARY = "CRITIC-HINT: Brak polecenia codex w PATH - krytykiem będzie Claude."
_HINT_NO_VERSION = "CRITIC-HINT: Nie udało się odczytać wersji codex (codex --version) - krytykiem będzie Claude."
_HINT_VERSION_SILENT = "CRITIC-HINT: Sonda codex --version nie odpowiedziała w {seconds} s albo nie dała się uruchomić - krytykiem będzie Claude."
_HINT_LOGIN_SILENT = (
    "CRITIC-HINT: Sonda codex login status nie odpowiedziała w {seconds} s albo nie dała się uruchomić"
    " - stan logowania nieznany, krytykiem będzie Claude."
)
_HINT_NO_AUTH = "CRITIC-HINT: Codex nie jest zalogowany - krytykiem będzie Claude. Zaloguj: codex login"
_HINT_FORCED = "CRITIC-HINT: Tryb wymuszony (MONOLYNX_CRITIC=codex): {unconfirmed} - krytykiem pozostaje Codex."
_UNCONFIRMED_VERSION_SILENT = "sonda codex --version nie odpowiedziała w {seconds} s albo nie dała się uruchomić"
_UNCONFIRMED_VERSION = "nie udało się odczytać wersji codex (codex --version)"
_UNCONFIRMED_LOGIN_SILENT = "sonda codex login status nie odpowiedziała w {seconds} s albo nie dała się uruchomić"
_UNCONFIRMED_LOGIN = "codex login status nie potwierdził logowania"
_HINT_BAD_MODEL = (
    "CRITIC-HINT: Niepoprawna wartość MONOLYNX_CRITIC_CODEX_MODEL"
    " (litery, cyfry i . _ : -, od 1 do 64 znaków, zaczyna się od litery lub cyfry) - krytykiem będzie Claude."
)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse wychodzi sam kodem 2, a 2 w tym skrypcie znaczy blad pracy, nie uzycia."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _timeout_value(raw: str) -> int:
    # Cyfry ASCII: `int()` przyjmuje tez spacje, `_` i cyfry spoza ASCII.
    if _TIMEOUT_DIGITS.fullmatch(raw) is None:
        raise argparse.ArgumentTypeError(f"--timeout musi być liczbą całkowitą, dostałem {raw!r}")
    value = int(raw)
    if not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(f"--timeout poza zakresem {MIN_TIMEOUT}..{MAX_TIMEOUT}: {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="critic_backend", add_help=False)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("preflight", add_help=False)
    run = commands.add_parser("run", add_help=False, allow_abbrev=False)
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--cwd", required=True)
    run.add_argument("--timeout", type=_timeout_value, default=DEFAULT_TIMEOUT)
    return parser


def _probe(binary: str, args: list[str]) -> tuple[int, str] | None:
    """(kod wyjscia, stdout+stderr) albo None, gdy proces nie wystartowal lub przekroczyl czas."""
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode, result.stdout + result.stderr


def _switch() -> tuple[str, str]:
    """(tryb, wartosc po strip): tryb to `detect`, `forced`, `claude` albo `unknown`."""
    value = os.environ.get("MONOLYNX_CRITIC", "").strip()
    lowered = value.lower()
    if lowered in ("", "auto"):
        return "detect", value
    if lowered == "codex":
        return "forced", value
    if lowered == "claude":
        return "claude", value
    # Nieznany literal mapuje sie na Claude'a: skutek odwracalny, a Codex zuzywa limit konta.
    return "unknown", value


def _model() -> tuple[str | None, bool]:
    """(id modelu albo None gdy nieustawiony, czy wartosc jest poprawna)."""
    value = os.environ.get("MONOLYNX_CRITIC_CODEX_MODEL", "").strip()
    if not value:
        return None, True
    if _MODEL_PATTERN.fullmatch(value) is None:
        return None, False
    return value, True


def _preflight() -> int:
    mode, switch_value = _switch()
    model, model_ok = _model()

    binary = shutil.which(_COMMAND)
    version = "none"
    logged_in = False
    version_silent = login_silent = False
    if binary is not None:
        outcome = _probe(binary, ["--version"])
        version_silent = outcome is None
        match = _VERSION_IN_OUTPUT.search(outcome[1]) if outcome is not None and outcome[0] == 0 else None
        if match is not None:
            version = ".".join(match.groups())
        # Tryb wymuszony sprawdza logowanie takze bez wersji, zeby `auth=` zawsze bylo prawdziwe.
        if match is not None or mode == "forced":
            login = _probe(binary, ["login", "status"])
            login_silent = login is None
            logged_in = login is not None and login[0] == 0 and _LOGGED_IN in login[1]

    backend = "claude"
    hint: str | None = None
    if mode == "claude":
        hint = _HINT_SWITCH_CLAUDE
    elif mode == "unknown":
        hint = f"CRITIC-HINT: Nieznana wartość MONOLYNX_CRITIC={switch_value!r} (dozwolone: auto, codex, claude) - krytykiem będzie Claude."
    elif binary is None:
        hint = _HINT_NO_BINARY
    elif mode == "forced":
        if not model_ok:
            hint = _HINT_BAD_MODEL
        else:
            backend = "codex"
            unconfirmed = []
            if version_silent:
                unconfirmed.append(_UNCONFIRMED_VERSION_SILENT.format(seconds=_PROBE_TIMEOUT))
            elif version == "none":
                unconfirmed.append(_UNCONFIRMED_VERSION)
            if login_silent:
                unconfirmed.append(_UNCONFIRMED_LOGIN_SILENT.format(seconds=_PROBE_TIMEOUT))
            elif not logged_in:
                unconfirmed.append(_UNCONFIRMED_LOGIN)
            if unconfirmed:
                hint = _HINT_FORCED.format(unconfirmed="; ".join(unconfirmed))
    elif version_silent:
        hint = _HINT_VERSION_SILENT.format(seconds=_PROBE_TIMEOUT)
    elif version == "none":
        hint = _HINT_NO_VERSION
    elif login_silent:
        hint = _HINT_LOGIN_SILENT.format(seconds=_PROBE_TIMEOUT)
    elif not logged_in:
        hint = _HINT_NO_AUTH
    elif not model_ok:
        hint = _HINT_BAD_MODEL
    else:
        backend = "codex"

    if hint:
        print(hint)
    print(f"CRITIC: backend={backend} codex={version} auth={'ok' if logged_in else 'missing'} model={model or 'inherit'}")
    return 0


def _run_error(reason: str, message: str) -> int:
    print(f"critic_backend: {message}", file=sys.stderr)
    print(f"CRITIC-RUN: status=error reason={reason}")
    return 2


def _same_file(first: Path, second: Path) -> bool:
    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
        return first.resolve() == second.resolve()
    except OSError:
        return False


def _read_prompt(path: Path) -> bytes | str:
    """Bajty promptu albo komunikat bledu po polsku."""
    if not path.exists():
        return f"plik promptu nie istnieje: {path}"
    if path.is_dir():
        return f"--prompt-file wskazuje katalog: {path}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"nie można odczytać pliku promptu {path}: {exc.strerror or exc}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"plik promptu nie jest w UTF-8: {path}"
    if not text.strip():
        return f"plik promptu jest pusty: {path}"
    return raw


def _remove(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM do grupy, do KILL_GRACE_SECONDS czekania, potem SIGKILL; proces zawsze zebrany."""
    # start_new_session=True: pgid grupy rowny pid lidera.
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE_SECONDS)
    # Potomek ignorujacy SIGTERM moze przezyc lidera, wiec SIGKILL idzie do grupy takze po jego wyjsciu.
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _stderr_tail(raw: bytes) -> str:
    # stderr Codex ma sekwencje ANSI i szum rmcp: zdejmujemy kody i zostawiamy ogon.
    lines = [line for line in _ANSI.sub("", raw.decode("utf-8", errors="replace")).splitlines() if line.strip()]
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def _discard_verdict(out: Path) -> str:
    """Odsuwa `out` do `<out>.rejected`; gdy to sie nie uda, usuwa `out`. Zwraca opis kopii do komunikatu."""
    rejected = Path(f"{out}.rejected")
    try:
        os.replace(out, rejected)
    except OSError:
        # Po bledzie `out` nie moze istniec, nawet kosztem kopii (brak uprawnien, inny system plikow).
        _remove(out)
        return "kopii nie udało się zachować"
    return f"kopia: {rejected}"


def _validate_verdict(out: Path) -> str | None:
    """None gdy werdykt poprawny, inaczej komunikat po polsku; po bledzie `out` nie istnieje, a odrzucona tresc trafia do `<out>.rejected`."""
    if not out.is_file():
        _remove(out)
        return "codex nie zapisał pliku werdyktu"
    try:
        text = out.read_bytes().decode("utf-8")
    except OSError as exc:
        return f"nie można odczytać pliku werdyktu ({exc.strerror or exc}), {_discard_verdict(out)}"
    except UnicodeDecodeError:
        text = None
    if text is not None and _HEADER in text and any(int(m.group(1)) <= 100 for m in _SCORE.finditer(text)):
        return None
    return f"werdykt nie ma nagłówka {_HEADER} i punktacji N/100 (N w 0..100), {_discard_verdict(out)}"


def _run(args: argparse.Namespace) -> int:
    prompt_path, cwd, out = Path(args.prompt_file), Path(args.cwd), Path(os.path.abspath(args.out))

    prompt = _read_prompt(prompt_path)
    if isinstance(prompt, str):
        return _run_error("input", prompt)
    if not cwd.is_dir():
        return _run_error("input", f"--cwd nie jest katalogiem: {cwd}")
    if _same_file(out, prompt_path):
        return _run_error("input", "--out wskazuje ten sam plik co --prompt-file")
    if out.is_dir():
        return _run_error("input", f"--out wskazuje katalog: {out}")
    if not out.parent.is_dir():
        return _run_error("input", f"katalog nadrzędny --out nie istnieje: {out.parent}")

    mode, _ = _switch()
    if mode in ("claude", "unknown"):
        return _run_error("disabled", "krytyk Codex wyłączony przez MONOLYNX_CRITIC")
    model, model_ok = _model()
    if not model_ok:
        return _run_error("input", "niepoprawna wartość MONOLYNX_CRITIC_CODEX_MODEL")
    binary = shutil.which(_COMMAND)
    if binary is None:
        return _run_error("missing", "brak polecenia codex w PATH")

    # Stary werdykt nie moze zostac uznany za nowy; kasowanie dopiero po pelnej walidacji wejscia.
    if not (_remove(out) and _remove(Path(f"{out}.rejected"))):
        return _run_error("input", f"nie można usunąć poprzedniego wyniku: {out}")

    argv = [
        binary,
        "exec",
        "-C",
        os.path.abspath(args.cwd),
        "--sandbox",
        "read-only",
        "-c",
        'model_reasoning_effort="high"',
        *(["-m", model] if model else []),
        "-o",
        str(out),
        "-",
    ]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return _run_error("missing", f"nie można uruchomić codex: {exc.strerror or exc}")
    with proc:
        try:
            _, stderr = proc.communicate(input=prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            _remove(out)
            return _run_error("timeout", f"codex przekroczył limit {args.timeout} s, grupa procesów zatrzymana")
    if proc.returncode != 0:
        _remove(out)
        tail = _stderr_tail(stderr)
        return _run_error("exit", f"codex zakończył się kodem {proc.returncode}" + (f"\n{tail}" if tail else ""))

    problem = _validate_verdict(out)
    if problem is not None:
        return _run_error("verdict", problem)
    print(f"CRITIC-RUN: status=ok out={out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Podpowiedzi i komunikaty maja polskie diakrytyki; w ASCII-owym locale print rzucilby UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")
    try:
        args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    except _UsageError as exc:
        print(f"critic_backend: {exc}", file=sys.stderr)
        return 64
    if args.command == "run":
        return _run(args)
    return _preflight()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Preflight transportu skilli Monolynx: CLI `monolynx` albo MCP (MON-232).

Skill uruchamia ten skrypt raz na starcie i z jego wyniku wie, czy operacje
wykonuje przez CLI (`monolynx_op.py`), czy przez tools MCP. Skrypt niczego nie
zapisuje na platformie, nie wypisuje sekretow (ani wyniku `auth whoami`), a brak
sieci nigdy nie jest bledem.

Wywolanie:

    monolynx_transport.py [--session-id ID]

Wyjscie stdout: zero albo jedna linia podpowiedzi `TRANSPORT-HINT: ...`, potem
ZAMROZONA ostatnia linia:

    TRANSPORT: mode=cli|mcp cli=<wersja|none> auth=ok|missing latest=<wersja|unknown> update=yes|no install=brew|pipx|uv|unknown|none

- `mode=cli` tylko gdy CLI jest w PATH i `auth whoami` konczy sie kodem 0;
  w kazdym innym stanie `mode=mcp`
- podpowiedz (priorytet: brak CLI, brak logowania, nowsza wersja) pojawia sie
  najwyzej raz na sesje (marker `~/.cache/monolynx/transport-hint/<id sesji>`);
  id: `--session-id`, potem env `CLAUDE_CODE_SESSION_ID`, potem staly klucz
  `no-session` wygasajacy po 6 h (klient bez id sesji w env, np. Codex).
  Linia `TRANSPORT:` jest zawsze
- `MONOLYNX_SPRINT_RUN=1` (sesje potomne sprint-run): zadnej podpowiedzi
- `MONOLYNX_TRANSPORT=mcp`: `mode=mcp`, bez sieci i bez `auth whoami`,
  `auth=missing latest=unknown update=no`, bez podpowiedzi
- `latest`: `monolynx -o json version --check`, a gdy to nie daje wyniku
  (CLI 0.1.0 nie ma komendy `version`) cache `~/.cache/monolynx/cli-latest.json`
  (24 h), a potem PyPI (timeout 2 s). Brak sieci: `latest=unknown update=no`
- `install` z `os.path.realpath` binarki wskazuje komende upgrade w podpowiedzi

Kody wyjscia: 0 zawsze (takze offline i bez CLI), 64 blad uzycia (nieznany
argument, `--session-id` bez wartosci). Nigdy traceback.

Wyjscie CLI w tym skrypcie pochodzi z `tests/fixtures/monolynx/` (CLI 0.1.0):
`--version` wypisuje samo `0.1.0`, `auth whoami` bez tokenu konczy sie kodem 3,
a `version --check` w 0.1.0 daje kod 2 (`No such command`).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi/monolynx-cli/json"

_CLI = "monolynx"
# Limit jednego wywolania CLI; `--timeout 5` w whoami ogranicza tylko zapytanie HTTP samego CLI.
_CLI_PROCESS_TIMEOUT = 10
_LATEST_CACHE_TTL = 24 * 3600
_NO_SESSION_KEY = "no-session"
_NO_SESSION_TTL = 6 * 3600
_MAX_PYPI_BYTES = 2_000_000

_VERSION_IN_OUTPUT = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_VERSION_STRICT = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_HINT_NO_CLI = (
    "TRANSPORT-HINT: CLI monolynx nie jest zainstalowane - pracuję przez MCP."
    " Instalacja: pipx install monolynx-cli (albo: brew install monolynx/tap/monolynx)"
)
_HINT_NO_AUTH = "TRANSPORT-HINT: CLI monolynx nie jest zalogowane - pracuję przez MCP. Zaloguj: monolynx auth login"

# Fragment sciezki binarki (po realpath) -> installer. Kolejnosc ma znaczenie przy sciezce
# pasujacej do kilku wzorcow: pipx i uv pierwsze, bo ich venvy mieszkaja tez pod
# `/opt/homebrew/` (np. `/opt/homebrew/pipx/venvs/...`) i brew dalby bledne `brew upgrade`.
_INSTALLERS = (
    (("/pipx/venvs/",), "pipx"),
    (("/uv/tools/",), "uv"),
    (("/Cellar/", "/homebrew/"), "brew"),
)
_UPGRADE_COMMANDS = {
    "brew": "brew upgrade monolynx",
    "pipx": "pipx upgrade monolynx-cli",
    "uv": "uv tool upgrade monolynx-cli",
    "unknown": "pip install -U monolynx-cli",
}


def _cache_dir() -> Path:
    return Path.home() / ".cache" / "monolynx"


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_STRICT.match(value.strip())
    return None if match is None else (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _run_cli(binary: str, args: list[str]) -> tuple[int, str] | None:
    """(kod wyjscia, stdout) albo None, gdy proces nie wystartowal lub przekroczyl czas."""
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_CLI_PROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode, result.stdout


def fetch_pypi_latest(timeout: float = 2.0) -> str | None:
    """`info.version` z PyPI JSON albo None przy kazdym bledzie (siec, timeout, ksztalt odpowiedzi)."""
    request = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(_MAX_PYPI_BYTES))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    info = payload.get("info") if isinstance(payload, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) else None


def _read_latest_cache() -> str | None:
    path = _cache_dir() / "cli-latest.json"
    try:
        if time.time() - path.stat().st_mtime >= _LATEST_CACHE_TTL:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    latest = data.get("latest") if isinstance(data, dict) else None
    return latest if isinstance(latest, str) else None


def _write_latest_cache(latest: str) -> None:
    path = _cache_dir() / "cli-latest.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"latest": latest}), encoding="utf-8")
    except OSError:
        pass


def _latest_from_cli(binary: str) -> str | None:
    """`latest` z `version --check`; None gdy komendy nie ma, kod != 0 albo JSON bez wartosci."""
    outcome = _run_cli(binary, ["-o", "json", "version", "--check"])
    if outcome is None or outcome[0] != 0:
        return None
    try:
        data = json.loads(outcome[1])
    except ValueError:
        return None
    latest = data.get("latest") if isinstance(data, dict) else None
    return latest if isinstance(latest, str) else None


def _resolve_latest(binary: str | None) -> str:
    """Wersja z CLI, cache albo PyPI; zwraca "unknown", gdy zadne zrodlo nie dalo poprawnej wersji."""
    latest = _latest_from_cli(binary) if binary else None
    if latest is None:
        latest = _read_latest_cache()
    if latest is None:
        latest = fetch_pypi_latest()
        if latest is not None and _version_tuple(latest) is not None:
            _write_latest_cache(latest)
    return latest if latest is not None and _version_tuple(latest) is not None else "unknown"


def _installer(binary: str) -> str:
    real = os.path.realpath(binary).replace("\\", "/")
    for fragments, name in _INSTALLERS:
        if any(fragment in real for fragment in fragments):
            return name
    return "unknown"


def _hint_marker(session_id: str | None) -> tuple[Path, int | None]:
    """(sciezka markera, TTL w sekundach albo None gdy marker nie wygasa)."""
    key = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if key:
        return _cache_dir() / "transport-hint" / re.sub(r"[^A-Za-z0-9_-]", "_", key), None
    return _cache_dir() / "transport-hint" / _NO_SESSION_KEY, _NO_SESSION_TTL


def _hint_already_shown(marker: Path, ttl: int | None) -> bool:
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return False
    return ttl is None or time.time() - mtime < ttl


def _mark_hint_shown(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        os.utime(marker, None)
    except OSError:
        pass


def _parse_args(argv: list[str]) -> str | None:
    """Id sesji z `--session-id`; ValueError przy bledzie uzycia."""
    session_id: str | None = None
    index = 0
    while index < len(argv):
        if argv[index] != "--session-id":
            raise ValueError(f"nieznany argument: {argv[index]!r}")
        if index + 1 >= len(argv):
            raise ValueError("--session-id bez wartosci")
        session_id = argv[index + 1].strip() or None
        index += 2
    return session_id


def _emit(hint: str | None, mode: str, cli: str, auth: str, latest: str, update: str, install: str) -> None:
    if hint:
        print(hint)
    print(f"TRANSPORT: mode={mode} cli={cli} auth={auth} latest={latest} update={update} install={install}")


def main(argv: list[str] | None = None) -> int:
    # Podpowiedz ma polskie diakrytyki; stdout w ASCII-owym locale inaczej rzucilby UnicodeEncodeError.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8")
    try:
        session_id = _parse_args(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print(f"monolynx_transport: {exc}", file=sys.stderr)
        return 64

    binary = shutil.which(_CLI)
    cli_version = "none"
    if binary is not None:
        outcome = _run_cli(binary, ["--version"])
        match = _VERSION_IN_OUTPUT.search(outcome[1]) if outcome is not None and outcome[0] == 0 else None
        if match is None:
            binary = None
        else:
            cli_version = ".".join(match.groups())
    install = _installer(binary) if binary else "none"

    if os.environ.get("MONOLYNX_TRANSPORT", "").strip().lower() == "mcp":
        _emit(None, "mcp", cli_version, "missing", "unknown", "no", install)
        return 0

    auth_ok = False
    if binary:
        whoami = _run_cli(binary, ["--timeout", "5", "-o", "json", "auth", "whoami"])
        auth_ok = whoami is not None and whoami[0] == 0
    latest = _resolve_latest(binary)
    current, newest = _version_tuple(cli_version), _version_tuple(latest)
    update = current is not None and newest is not None and newest > current

    hint = None
    if binary is None:
        hint = _HINT_NO_CLI
    elif not auth_ok:
        hint = _HINT_NO_AUTH
    elif update:
        hint = f"TRANSPORT-HINT: Dostępna nowsza wersja CLI monolynx ({cli_version} -> {latest}). Aktualizacja: {_UPGRADE_COMMANDS[install]}"

    if hint and os.environ.get("MONOLYNX_SPRINT_RUN", "").strip() == "1":
        hint = None
    if hint:
        marker, ttl = _hint_marker(session_id)
        if _hint_already_shown(marker, ttl):
            hint = None
        else:
            _mark_hint_shown(marker)

    _emit(hint, "cli" if auth_ok else "mcp", cli_version, "ok" if auth_ok else "missing", latest, "yes" if update else "no", install)
    return 0


if __name__ == "__main__":
    sys.exit(main())

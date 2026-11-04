"""Straz roli: hook PreToolUse Claude Code (HAR-175).

Claude Code uruchamia ten plik jako `python -I <sciezka> --rola <rola> --worktree <sciezka>`
i podaje zdarzenie na stdin (JSON). Odmowa to JSON `permissionDecision: deny` na stdout i kod 0;
brak odmowy to pusty stdout i kod 0. Hook jest fail-closed: kazdy blad (argumenty, stdin, wyjatek
z `decyzja`) konczy sie odmowa z powodem zaczynajacym sie od "straż roli:".

Reguly (niezmienniki z CLAUDE.md):
- no_git_write: w kazdej roli odmowa dla `git commit`, `git push` (sprawdza `waliduj_definicje`),
  `git reset --hard` i `git checkout`;
- reviewer_readonly: rola bez prawa zapisu nie uzywa Write/Edit/MultiEdit/NotebookEdit, a Bash
  tylko z bialej listy polecenia odczytu i bez metaznakow powloki;
- kazda rola: zapis poza worktree jest odmowiony (po `realpath`, wiec `..` i dowiazania nie wyprowadza).

Odmowa dopisuje jedna linie NDJSON `ToolDenied` do pliku z `COCKPIT_HOOK_FILE` (bez O_CREAT);
blad zapisu nie zmienia decyzji.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # uruchomienie jako skrypt (`python -I plik`): katalog `src/` nie jest na sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cockpit.services.invariants import Rola, profil_roli  # noqa: E402
from cockpit.services.walidacja import (  # noqa: E402
    _OPCJE_Z_ARGUMENTEM,
    _ZNAKI_POWLOKI,
    _to_git,
    waliduj_definicje,
)

ZMIENNA_PLIKU = "COCKPIT_HOOK_FILE"
_PREFIKS = "straż roli: "
_MAKS_OPISU = 200

# Narzedzie zapisu -> klucz sciezki w `tool_input`.
_NARZEDZIA_ZAPISU: Mapping[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Biala lista Basha dla roli bez prawa zapisu: pierwszy token polecenia.
_ODCZYT = frozenset({
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "wc",
    "pwd",
})
# ... albo `git <podpolecenie>` z tej listy (dokladny token).
_GIT_ODCZYT = frozenset({
    "status",
    "diff",
    "log",
    "show",
})
# Metaznaki powloki: przekierowania, laczenie polecen, podstawienia, nowa linia.
_METAZNAKI = (
    ">",
    "<",
    "|",
    ";",
    "&",
    "`",
    "$(",
    "\n",
)


class _BladStrazy(Exception):
    """Blad wejscia hooka (argumenty, stdin), ktory konczy sie odmowa."""


def _zakazany_git(komenda: str) -> str | None:
    """`git checkout` albo `git reset --hard` (ta sama tokenizacja co zakaz commit/push)."""
    tokeny = komenda.lower().translate(_ZNAKI_POWLOKI).split()
    for i, token in enumerate(tokeny):
        if not _to_git(token):
            continue
        j = i + 1
        while j < len(tokeny) and tokeny[j].startswith("-"):
            j += 2 if tokeny[j] in _OPCJE_Z_ARGUMENTEM else 1
        if tokeny[j:j + 1] == ["checkout"]:
            return "git checkout"
        if tokeny[j:j + 1] == ["reset"] and "--hard" in tokeny[j + 1:]:
            return "git reset --hard"
    return None


def _odczyt_z_bialej_listy(komenda: str) -> bool:
    if any(znak in komenda for znak in _METAZNAKI):
        return False
    try:
        tokeny = shlex.split(komenda)
    except ValueError:  # niezamkniety cudzyslow: nie umiemy ocenic, wiec odmowa
        return False
    if not tokeny:
        return False
    if tokeny[0] == "git":
        return len(tokeny) > 1 and tokeny[1] in _GIT_ODCZYT
    return tokeny[0] in _ODCZYT


def decyzja(zdarzenie: Mapping[str, Any], rola: Rola, worktree: Path) -> str | None:
    """Powod odmowy albo None. Czysta: bez I/O poza `realpath`; nieznane pola to odmowa."""
    narzedzie = zdarzenie.get("tool_name")
    wejscie = zdarzenie.get("tool_input")

    if narzedzie == "Bash":
        komenda = wejscie.get("command") if isinstance(wejscie, Mapping) else None
        if not isinstance(komenda, str):
            return f"{_PREFIKS}polecenie Bash bez komendy w postaci napisu"
        naruszenia = waliduj_definicje({"command": komenda})
        if naruszenia:
            return _PREFIKS + str(naruszenia[0])
        git = _zakazany_git(komenda)
        if git is not None:
            return (
                f"{_PREFIKS}niezmiennik no_git_write: silnik nie zmienia historii "
                f"ani drzewa gita ({git})"
            )
        if not profil_roli(rola).zapis and not _odczyt_z_bialej_listy(komenda):
            return (
                f"{_PREFIKS}niezmiennik reviewer_readonly: rola {rola.value} "
                "uruchamia tylko polecenia odczytu bez metaznaków powłoki"
            )
        return None

    klucz = _NARZEDZIA_ZAPISU.get(str(narzedzie))
    if klucz is None:
        return None
    if not profil_roli(rola).zapis:
        return (
            f"{_PREFIKS}niezmiennik reviewer_readonly: rola {rola.value} "
            f"nie ma prawa zapisu ({narzedzie})"
        )
    sciezka = wejscie.get(klucz) if isinstance(wejscie, Mapping) else None
    if not isinstance(sciezka, str) or "\x00" in sciezka:
        return f"{_PREFIKS}{narzedzie} bez poprawnej ścieżki {klucz}"
    cwd = zdarzenie.get("cwd")
    baza = Path(cwd) if isinstance(cwd, str) else worktree
    cel = Path(os.path.realpath(baza / sciezka))
    if not cel.is_relative_to(Path(os.path.realpath(worktree))):
        return f"{_PREFIKS}zapis poza worktree: {sciezka}"
    return None


def _argumenty(argv: Sequence[str]) -> tuple[Rola, Path]:
    if len(argv) != 4 or {argv[0], argv[2]} != {"--rola", "--worktree"}:
        raise _BladStrazy("wymagane argumenty: --rola <rola> --worktree <ścieżka>")
    wartosci = {argv[0]: argv[1], argv[2]: argv[3]}
    try:
        rola = Rola(wartosci["--rola"])
    except ValueError:
        raise _BladStrazy(f"nieznana rola {wartosci['--rola']!r}") from None
    return rola, Path(wartosci["--worktree"])


def _wejscie() -> dict[str, Any]:
    try:
        zdarzenie = json.loads(sys.stdin.buffer.read().decode())
    except (OSError, ValueError, RecursionError):  # ValueError: UnicodeDecodeError i JSONDecodeError
        raise _BladStrazy("nieczytelne wejście hooka (stdin nie jest JSON w UTF-8)") from None
    if not isinstance(zdarzenie, dict):
        raise _BladStrazy("wejście hooka nie jest obiektem JSON")
    return zdarzenie


def _napis(wartosc: object) -> str:
    return wartosc if isinstance(wartosc, str) else ""


def _opis_wywolania(zdarzenie: Mapping[str, Any]) -> str:
    """Komenda Basha albo sciezka zapisu, ucieta; nigdy tresc zapisywanego pliku."""
    narzedzie = zdarzenie.get("tool_name")
    wejscie = zdarzenie.get("tool_input")
    if not isinstance(wejscie, Mapping):
        return ""
    klucz = "command" if narzedzie == "Bash" else _NARZEDZIA_ZAPISU.get(str(narzedzie))
    if klucz is None:
        return ""
    return _napis(wejscie.get(klucz))[:_MAKS_OPISU]


def _zapisz_odmowe(zdarzenie: Mapping[str, Any], rola: Rola, powod: str) -> None:
    """Jedna linia NDJSON `ToolDenied` do pliku hookow kroku. Blad zapisu nie zmienia decyzji."""
    sciezka = os.environ.get(ZMIENNA_PLIKU)
    if not sciezka:
        return
    linia = json.dumps({
        "hook_event_name": "ToolDenied",
        "tool_name": _napis(zdarzenie.get("tool_name")),
        "command": _opis_wywolania(zdarzenie),
        "role": rola.value,
        "reason": powod,
        "session_id": _napis(zdarzenie.get("session_id")),
    })
    try:
        # bez O_CREAT: plik tworzy runner; jeden os.write na O_APPEND nie przeplata linii
        fd = os.open(sciezka, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, linia.encode() + b"\n")
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"{_PREFIKS}nie zapisano zdarzenia odmowy: {type(exc).__name__}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argumenty = sys.argv[1:] if argv is None else argv
    wejscie: tuple[Rola, dict[str, Any]] | None = None
    try:
        rola, worktree = _argumenty(argumenty)
        zdarzenie = _wejscie()
        wejscie = (rola, zdarzenie)
        powod = decyzja(zdarzenie, rola, worktree)
    except _BladStrazy as blad:
        powod = f"{_PREFIKS}{blad}"
    except Exception as blad:  # hook jest fail-closed: kazdy blad to odmowa
        powod = f"{_PREFIKS}nieoczekiwany błąd ({type(blad).__name__})"
    if powod is None:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": powod,
    }}))
    sys.stdout.flush()
    if wejscie is not None:
        _zapisz_odmowe(wejscie[1], wejscie[0], powod)
    return 0


if __name__ == "__main__":
    sys.exit(main())

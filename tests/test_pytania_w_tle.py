"""Hook pytania_w_tle: sesja w tle nie pyta człowieka (AskUserQuestion -> odmowa z protokołem
DECYZJA-POTRZEBNA), sesja interaktywna działa bez zmian, nieczytelne wejście w tle = odmowa.
Prawdziwy skrypt hooka uruchamiany jako proces, z korzenia repozytorium."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from procesy import uruchom_proces_sync, zakoncz_proces_sync  # noqa: E402

KORZEN = Path(__file__).resolve().parents[1]
HOOK = KORZEN / ".claude" / "hooks" / "pytania_w_tle.py"
PROTOKOL_POCZATEK = "Sesja w tle nie pyta człowieka. Zamiast AskUserQuestion zakończ turę wiadomością"


def hook(stdin: str, rodzaj: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_KIND"}
    if rodzaj is not None:
        env["CLAUDE_CODE_SESSION_KIND"] = rodzaj
    proc = uruchom_proces_sync([sys.executable, str(HOOK)], cwd=KORZEN, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, _ = proc.communicate(stdin, timeout=20)
    finally:
        zakoncz_proces_sync(proc, nazwa="hook pytania_w_tle")
    assert proc.returncode == 0
    return out


def odmowa(out: str) -> bool:
    d = json.loads(out)["hookSpecificOutput"]
    assert d["hookEventName"] == "PreToolUse"
    assert d["permissionDecisionReason"].startswith(PROTOKOL_POCZATEK)
    assert "DECYZJA-POTRZEBNA" in d["permissionDecisionReason"]
    return d["permissionDecision"] == "deny"


def test_sesja_w_tle_nie_pyta_czlowieka_tylko_dostaje_protokol_decyzji():
    assert odmowa(hook('{"tool_name": "AskUserQuestion"}', "bg"))


@pytest.mark.parametrize("rodzaj", [None, "cli", ""])
def test_sesja_interaktywna_pyta_czlowieka_bez_zmian(rodzaj):
    assert hook('{"tool_name": "AskUserQuestion"}', rodzaj) == ""


def test_inne_narzedzie_w_sesji_w_tle_przechodzi_bez_decyzji_hooka():
    assert hook('{"tool_name": "Bash"}', "bg") == ""


@pytest.mark.parametrize("wejscie", ["to nie json", "[]", "{}", '{"tool_name": 1}', ""],
                         ids=["nie-json", "lista", "brak-tool_name", "tool_name-nie-napis", "puste"])
def test_nieczytelne_wejscie_w_sesji_w_tle_to_odmowa(wejscie):
    assert odmowa(hook(wejscie, "bg"))


def hook_bajty(stdin, rodzaj: str = "bg") -> str:
    """Wejście jako bajty albo deskryptor pliku (np. tylko do zapisu: odczyt rzuca OSError)."""
    env = {**os.environ, "CLAUDE_CODE_SESSION_KIND": rodzaj}
    jako_fd = isinstance(stdin, int)
    proc = uruchom_proces_sync([sys.executable, str(HOOK)], cwd=KORZEN, env=env,
                               stdin=stdin if jako_fd else subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(None if jako_fd else stdin, timeout=20)
    finally:
        zakoncz_proces_sync(proc, nazwa="hook pytania_w_tle")
    assert proc.returncode == 0, err
    return out.decode()


def test_wejscie_spoza_utf8_w_sesji_w_tle_to_odmowa_nie_traceback():
    assert odmowa(hook_bajty(b"\xff\xfe"))


def test_blad_odczytu_wejscia_w_sesji_w_tle_to_odmowa(tmp_path):
    # stdin otwarty tylko do zapisu: Python startuje, a read() rzuca OSError (EBADF)
    fd = os.open(tmp_path / "tylko-zapis", os.O_WRONLY | os.O_CREAT)
    try:
        assert odmowa(hook_bajty(fd))
    finally:
        os.close(fd)

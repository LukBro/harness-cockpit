"""Straz wrazliwych sciezek w CI (.github/workflows/straz-sciezek.yml).

Czytamy prawdziwy workflow i uruchamiamy skrypt kroku powloka jak GitHub Actions;
`gh` zastepuje atrapa na PATH, ktora wypisuje liste zmienionych plikow ze srodowiska.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOW = KORZEN / ".github" / "workflows" / "straz-sciezek.yml"
ATRAPA_GH = '#!/bin/sh\nprintf "%s" "$ATRAPA_PLIKI"\n'


def _workflow() -> dict:
    return YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))


def _krok() -> dict:
    (zadanie,) = _workflow()["jobs"].values()
    (krok,) = [k for k in zadanie["steps"] if "run" in k]
    return krok


def _uruchom(tmp_path: Path, pliki: list[str]) -> subprocess.CompletedProcess:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text(ATRAPA_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "PR": "1",
           "REPO": "LukBro/harness-cockpit", "ATRAPA_PLIKI": "".join(p + "\n" for p in pliki)}
    return subprocess.run(["bash", "-c", _krok()["run"]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


def test_straz_czyta_workflow_z_galezi_docelowej_a_nie_z_pr():
    assert list(_workflow()["on"]) == ["pull_request_target"]


def test_straz_nie_pobiera_ani_nie_uruchamia_kodu_z_pr():
    (zadanie,) = _workflow()["jobs"].values()
    assert not any("uses" in k for k in zadanie["steps"])


def test_straz_ma_tylko_uprawnienia_do_odczytu():
    assert set(_workflow()["permissions"].values()) == {"read"}


@pytest.mark.parametrize("sciezka", [
    ".github/workflows/ci.yml", ".claude/settings.json", ".claude/settings.local.json",
    ".claude/hooks/straz.py", "pyproject.toml", "alembic/versions/0008_x.py", "alembic.ini",
    "src/runner/secrets.py", "CLAUDE.md",
])
def test_pr_zmieniajacy_wrazliwa_sciezke_oblewa_i_ja_wymienia(tmp_path, sciezka):
    wynik = _uruchom(tmp_path, ["src/cockpit/api/app.py", sciezka])
    assert wynik.returncode == 1
    assert sciezka in wynik.stdout
    assert "src/cockpit/api/app.py" not in wynik.stdout


@pytest.mark.parametrize("sciezka", [
    "src/cockpit/services/runs.py", "tests/test_x.py", ".claude/rules/nowa.md",
    ".claude/agent-memory/test-writer/MEMORY.md", "web/src/App.tsx", "src/runner/secrets_test.py",
    "docs/pyproject.toml", "alembic/env.py",
])
def test_pr_ze_zwyklym_kodem_przechodzi(tmp_path, sciezka):
    wynik = _uruchom(tmp_path, [sciezka])
    assert wynik.returncode == 0, wynik.stdout + wynik.stderr


def test_brak_listy_zmienionych_plikow_to_porazka(tmp_path):
    wynik = _uruchom(tmp_path, [])
    assert wynik.returncode == 1
    assert "brak dowodu" in wynik.stdout

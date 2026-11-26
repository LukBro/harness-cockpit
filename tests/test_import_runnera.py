"""HAR-204: runner nie buduje ustawien kokpitu, wiec `.env` z cwd go nie wywraca i nie wycieka.

Import idzie w osobnym procesie, bo w procesie pytesta modul ustawien jest juz zaladowany.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cockpit.settings import Settings
from procesy import uruchom_i_zbierz
from runner.krok import LIMIT_TEKSTU_ZDARZENIA

KORZEN = Path(__file__).resolve().parent.parent
TOKEN = "token-har204-nie-moze-wyciec-7f3a91"


def test_limit_tekstu_zdarzenia_runnera_jest_rowny_domyslnemu_limitowi_kokpitu():
    assert LIMIT_TEKSTU_ZDARZENIA == Settings.model_fields["limit_tekstu_zdarzenia"].default


@pytest.mark.parametrize("modul", ["runner.client", "runner.krok"])
def test_runner_startuje_z_tokenem_w_pliku_env_i_nie_wypisuje_go(tmp_path: Path, modul: str):
    (tmp_path / ".env").write_text(f"COCKPIT_RUNNER_TOKEN={TOKEN}\n")

    wynik = uruchom_i_zbierz(
        [sys.executable, "-c", f"import {modul}"],
        cwd=tmp_path,                   # `.env` jest czytany z biezacego katalogu
        env={**os.environ, "PYTHONPATH": str(KORZEN / "src")},
        capture_output=True, text=True, timeout=60,
    )

    assert TOKEN not in wynik.stdout + wynik.stderr
    assert wynik.returncode == 0, wynik.stderr

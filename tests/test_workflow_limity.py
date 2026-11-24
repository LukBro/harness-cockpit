"""Limity czasu i anulowanie przebiegow CI (audyt procesu 2026-11-19).

Job bez `timeout-minutes` wisi do domyslnych 360 min (HAR-106: mutation anulowany po 360 + 360 min), a kolejny
push do PR nie anulowal poprzedniego przebiegu, wiec CI liczylo nieaktualne SHA. Push na main nie moze byc
anulowany ani kolejkowany - kazdy commit na main ma swoj przebieg.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOWY = ["ci.yml", "mutation.yml"]


def _workflow(nazwa: str) -> dict:
    return YAML(typ="safe").load((KORZEN / ".github" / "workflows" / nazwa).read_text(encoding="utf-8"))


def test_kazdy_job_ci_i_mutacji_ma_limit_czasu():
    bez_limitu = [f"{n}:{job}" for n in WORKFLOWY for job, opis in _workflow(n)["jobs"].items()
                  if not isinstance(opis.get("timeout-minutes"), int)]
    assert bez_limitu == []


def test_nowy_push_do_pr_anuluje_poprzedni_przebieg_a_push_na_main_ma_wlasna_grupe():
    for n in WORKFLOWY:
        konk = _workflow(n)["concurrency"]
        assert konk["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}", n
        # numer PR laczy przebiegi jednego PR; poza PR grupa to run_id, czyli zawsze nowa
        assert konk["group"] == "${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}", n


def test_testy_w_ci_ida_przez_narzedzie_ktore_nazywa_zawieszony_test():
    kroki = _workflow("ci.yml")["jobs"]["python"]["steps"]
    polecenia = [k.get("run", "") for k in kroki]
    assert any(p.startswith("python tools/testy.py --bez-postepu 600 ") for p in polecenia), polecenia
    assert not any(p.strip().startswith("pytest ") for p in polecenia), polecenia


@pytest.mark.parametrize(
    ("wybor_testow", "oczekiwane_polecenie"),
    [
        ('-m "baza and not e2e"', 'python tools/testy.py --bez-postepu 600 -v -rs -m "baza and not e2e"'),
        ("-m e2e", "python tools/testy.py --bez-postepu 600 -m e2e"),
    ],
)
def test_krok_testow_joba_e2e_ma_limit_na_pojedynczy_test_i_zachowuje_wybor_testow_i_wymog_bazy(
    wybor_testow: str, oczekiwane_polecenie: str
):
    kroki = _workflow("ci.yml")["jobs"]["e2e"]["steps"]
    kroki_z_wyborem = [k for k in kroki if wybor_testow in k.get("run", "")]
    assert len(kroki_z_wyborem) == 1, [k.get("run") for k in kroki]
    krok = kroki_z_wyborem[0]
    assert re.match(r"python tools/testy\.py --bez-postepu \d+ ", krok["run"]), krok["run"]
    assert krok["run"] == oczekiwane_polecenie
    assert krok["env"]["COCKPIT_WYMAGAJ_BAZY"] == "1"

"""Zapadka baseline mutacji (cicd/mutation_baseline.py): kiedy wynik zostaje nowa podloga jakosci (HAR-20).

Skrypt jest samodzielny (nie pakiet), wiec ladujemy go z pliku i wolamy `main(argv)` w procesie.
Pliki podsumowania i baseline powstaja w tmp_path; skutek czytamy z pliku baseline (bajty), nie z samego raportu.
"""
import importlib.util
import json
from pathlib import Path

import pytest

SKRYPT = Path(__file__).resolve().parents[1] / "cicd" / "mutation_baseline.py"
SHA = "3f2a9c1d4e5b60718293a4b5c6d7e8f901234567"
SCORE_BASELINE = 60.0


def _zaladuj():
    spec = importlib.util.spec_from_file_location("mutation_baseline", SKRYPT)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


zapadka = _zaladuj()


def _podsumowanie(tmp_path: Path, score: float | None, total: int = 10) -> Path:
    zabite = 0 if score is None else round(total * score / 100)
    plik = tmp_path / "podsumowanie.json"
    plik.write_text(
        json.dumps({"score": score, "total": total, "killed": zabite, "survived": total - zabite}),
        encoding="utf-8",
    )
    return plik


def _baseline(tmp_path: Path, score: float = SCORE_BASELINE, scope: str = "diff") -> Path:
    plik = tmp_path / "baseline.json"
    dane = {
        "score": score,
        "survived": 4,
        "total": 10,
        "scope": scope,
        "measured_at": "2026-01-01",
        "commit": "0000000000000000000000000000000000000000",
        "tool": "mutmut 3.8.0",
    }
    plik.write_text(json.dumps(dane, indent=2) + "\n", encoding="utf-8")
    return plik


def _uruchom(capsys, podsumowanie: Path, baseline: Path, *dodatkowe: str):
    kod = zapadka.main(["--summary", str(podsumowanie), "--baseline", str(baseline), *dodatkowe])
    return kod, capsys.readouterr()


def _linia_statusu(wyjscie: str) -> str:
    return next(linia for linia in wyjscie.splitlines() if linia.startswith("MUTATION-BASELINE:"))


def _zapisany(plik: Path) -> dict:
    return json.loads(plik.read_text(encoding="utf-8"))


def test_pierwszy_pomiar_zapisuje_baseline_z_commitem(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 70.0), baseline, "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=first ")
    zapisany = _zapisany(baseline)
    assert zapisany["score"] == 70.0
    assert zapisany["commit"] == SHA


def test_wzrost_wyniku_podnosi_baseline_i_zapisuje_commit(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 80.0), baseline, "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=up ")
    zapisany = _zapisany(baseline)
    assert zapisany["score"] == 80.0
    assert zapisany["commit"] == SHA


def test_wynik_rowny_baseline_nie_zmienia_pliku(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, SCORE_BASELINE), baseline, "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=same ")
    assert baseline.read_bytes() == przed


def test_spadek_wyniku_nie_nadpisuje_baseline(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 30.0), baseline, "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=down ")
    assert "UWAGA" in wyjscie.out
    assert baseline.read_bytes() == przed


def test_wynik_z_innego_zakresu_nie_nadpisuje_baseline_nawet_gdy_jest_wyzszy(tmp_path, capsys):
    # Baseline z zakresu full, przebieg diff z wyzszym wynikiem: bez gałęzi zakresu byłoby "up" i zapis.
    baseline = _baseline(tmp_path, score=SCORE_BASELINE, scope="full")
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 90.0), baseline, "--scope", "diff", "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=scope-mismatch ")
    assert baseline.read_bytes() == przed


def test_przebieg_bez_mutantow_nie_nadpisuje_istniejacego_baseline(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, None, total=0), baseline, "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=no-mutants ")
    assert baseline.read_bytes() == przed


def test_przebieg_bez_mutantow_i_bez_baseline_nie_tworzy_pliku(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, None, total=0), baseline, "--commit", SHA)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=no-mutants ")
    assert not baseline.exists()


def test_pierwszy_pomiar_bez_commita_jest_odmowa_i_nie_tworzy_baseline(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 70.0), baseline)

    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err.startswith("mutation_baseline: ")
    assert "status=first" in wyjscie.err
    assert "--commit" in wyjscie.err
    assert not baseline.exists()


def test_wzrost_wyniku_bez_commita_jest_odmowa_i_zostawia_baseline(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 80.0), baseline)

    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err.startswith("mutation_baseline: ")
    assert "status=up" in wyjscie.err
    assert "--commit" in wyjscie.err
    assert baseline.read_bytes() == przed


def test_pusty_commit_jest_traktowany_jak_brak_commita(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 70.0), baseline, "--commit", "")

    assert kod == 2
    assert wyjscie.out == ""
    assert "--commit" in wyjscie.err
    assert not baseline.exists()


def test_proba_na_sucho_bez_commita_nie_jest_odmowa_i_nie_zapisuje(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 80.0), baseline, "--dry-run")

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=up ")
    assert baseline.read_bytes() == przed


def test_spadek_wyniku_bez_commita_nie_jest_odmowa_bo_nic_nie_jest_zapisywane(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, 30.0), baseline)

    assert kod == 0
    assert _linia_statusu(wyjscie.out).startswith("MUTATION-BASELINE: status=down ")
    assert baseline.read_bytes() == przed


@pytest.mark.parametrize("score, status", [(60.0, "same"), (30.0, "down")])
def test_statusy_bez_zapisu_nie_wymagaja_commita(tmp_path, capsys, score, status):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)

    kod, wyjscie = _uruchom(capsys, _podsumowanie(tmp_path, score), baseline)

    assert kod == 0
    assert f"status={status} " in _linia_statusu(wyjscie.out)

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


# =============================================================================
# HAR-233: porownanie z wynikiem poprzedniego zielonego przebiegu zamiast z plikiem baseline
# =============================================================================

# Prawdziwy, stary baseline z repozytorium: wynik nad nim, ale pod poprzednim przebiegiem, rozni nowa regule od starej.
STARY_BASELINE = json.loads((SKRYPT.parents[1] / "cicd" / "mutation-baseline.json").read_text(encoding="utf-8"))["score"]
SCORE_POPRZEDNI = 97.0
SCORE_BIEZACY_POD_POPRZEDNIM = 95.0
SCORE_BIEZACY_NAD_POPRZEDNIM = 98.5
KOMUNIKAT_BRAK_WYNIKU = (
    "mutation_baseline: poprzedni wynik: brak wyniku (score=null) - poprzedni przebieg nie zmierzyl mutantow\n"
)


def _poprzedni(
    tmp_path: Path, score: float | None = SCORE_POPRZEDNI, *, survived: int = 3, total: int = 100
) -> Path:
    plik = tmp_path / "poprzedni-summary.json"
    zabite = 0 if score is None else total - survived
    plik.write_text(
        json.dumps({"score": score, "total": total, "killed": zabite, "survived": survived}), encoding="utf-8"
    )
    return plik


def _uruchom_poprzedni(capsys, podsumowanie: Path, poprzedni: Path, *dodatkowe: str):
    kod = zapadka.main(["--summary", str(podsumowanie), "--previous-summary", str(poprzedni), *dodatkowe])
    return kod, capsys.readouterr()


def _ostatnia_linia(wyjscie: str) -> str:
    return wyjscie.strip().splitlines()[-1]


def _biezacy(tmp_path: Path, score: float | None, total: int = 100) -> Path:
    return _podsumowanie(tmp_path, score, total=total)


def test_wynik_nizszy_od_poprzedniego_przebiegu_jest_spadkiem_nawet_gdy_przewyzsza_stary_baseline(tmp_path, capsys):
    assert STARY_BASELINE < SCORE_BIEZACY_POD_POPRZEDNIM < SCORE_POPRZEDNI

    kod, wyjscie = _uruchom_poprzedni(
        capsys, _biezacy(tmp_path, SCORE_BIEZACY_POD_POPRZEDNIM), _poprzedni(tmp_path), "--scope", "full"
    )

    assert kod == 0
    assert _ostatnia_linia(wyjscie.out) == (
        "MUTATION-BASELINE: status=down score=95.0 baseline=97.0 delta=-2.0 survived=5 total=100 scope=full"
    )
    assert "UWAGA" in wyjscie.out


def test_wynik_wyzszy_od_poprzedniego_przebiegu_jest_wzrostem_z_poprzednim_wynikiem_jako_odniesieniem(
    tmp_path, capsys
):
    kod, wyjscie = _uruchom_poprzedni(
        capsys, _biezacy(tmp_path, SCORE_BIEZACY_NAD_POPRZEDNIM), _poprzedni(tmp_path), "--scope", "full"
    )

    assert kod == 0
    assert _ostatnia_linia(wyjscie.out).startswith("MUTATION-BASELINE: status=up score=98.5 baseline=97.0 delta=1.5 ")


def test_wynik_rowny_poprzedniemu_przebiegowi_jest_bez_zmian(tmp_path, capsys):
    kod, wyjscie = _uruchom_poprzedni(
        capsys, _biezacy(tmp_path, SCORE_POPRZEDNI), _poprzedni(tmp_path), "--scope", "full"
    )

    assert kod == 0
    assert _ostatnia_linia(wyjscie.out).startswith("MUTATION-BASELINE: status=same score=97.0 baseline=97.0 ")


def test_porownanie_z_poprzednim_przebiegiem_nigdy_nie_zapisuje_pliku_takze_przy_wzroscie_wyniku(
    tmp_path, capsys, monkeypatch
):
    praca = tmp_path / "praca"
    praca.mkdir()
    monkeypatch.chdir(praca)
    biezacy = _biezacy(tmp_path, SCORE_BIEZACY_NAD_POPRZEDNIM)
    poprzedni = _poprzedni(tmp_path)
    pliki_przed = set(tmp_path.rglob("*"))
    tresc_przed = {sciezka: sciezka.read_bytes() for sciezka in pliki_przed if sciezka.is_file()}

    kod, wyjscie = _uruchom_poprzedni(capsys, biezacy, poprzedni, "--scope", "full")

    assert kod == 0
    assert _ostatnia_linia(wyjscie.out).startswith("MUTATION-BASELINE: status=up ")
    assert set(tmp_path.rglob("*")) == pliki_przed
    assert {sciezka: sciezka.read_bytes() for sciezka in pliki_przed if sciezka.is_file()} == tresc_przed
    assert "Nowy baseline zapisany" not in wyjscie.out
    assert "Tryb dry-run" not in wyjscie.out
    assert "baseline: nie zapisywany - porownanie z poprzednim przebiegiem" in wyjscie.out.splitlines()


def test_porownanie_z_poprzednim_przebiegiem_nie_zapisuje_pliku_takze_gdy_podano_commit(
    tmp_path, capsys, monkeypatch
):
    praca = tmp_path / "praca"
    praca.mkdir()
    monkeypatch.chdir(praca)
    biezacy = _biezacy(tmp_path, SCORE_BIEZACY_NAD_POPRZEDNIM)
    poprzedni = _poprzedni(tmp_path)
    pliki_przed = set(tmp_path.rglob("*"))

    kod, wyjscie = _uruchom_poprzedni(capsys, biezacy, poprzedni, "--scope", "full", "--commit", SHA)

    assert kod == 0
    assert _ostatnia_linia(wyjscie.out).startswith("MUTATION-BASELINE: status=up ")
    assert set(tmp_path.rglob("*")) == pliki_przed


def test_porownanie_z_poprzednim_przebiegiem_nie_wymaga_commita_nawet_przy_wzroscie(tmp_path, capsys):
    kod, wyjscie = _uruchom_poprzedni(
        capsys, _biezacy(tmp_path, SCORE_BIEZACY_NAD_POPRZEDNIM), _poprzedni(tmp_path), "--scope", "full"
    )

    assert kod == 0
    assert wyjscie.err == ""


def test_zakres_przebiegu_z_argumentu_trafia_do_linii_statusu_przy_porownaniu_z_poprzednim(tmp_path, capsys):
    kod, wyjscie = _uruchom_poprzedni(capsys, _biezacy(tmp_path, SCORE_BIEZACY_NAD_POPRZEDNIM), _poprzedni(tmp_path))

    assert kod == 0
    # Bez --scope domyslny zakres to diff; poprzedni wynik dziedziczy zakres przebiegu, wiec nie ma "scope-mismatch".
    assert _ostatnia_linia(wyjscie.out).endswith(" scope=diff")
    assert " status=up " in _ostatnia_linia(wyjscie.out)


def test_przebieg_bez_mutantow_z_poprzednim_wynikiem_nie_jest_porownaniem_i_pokazuje_poprzedni_wynik(
    tmp_path, capsys
):
    kod, wyjscie = _uruchom_poprzedni(
        capsys, _biezacy(tmp_path, None, total=0), _poprzedni(tmp_path), "--scope", "full"
    )

    assert kod == 0
    assert _ostatnia_linia(wyjscie.out) == (
        "MUTATION-BASELINE: status=no-mutants score=none baseline=97.0 delta=none survived=0 total=0 scope=full"
    )


def test_brak_pliku_poprzedniego_wyniku_jest_bledem_wejscia_a_nie_pierwszym_pomiarem(tmp_path, capsys):
    brak = tmp_path / "poprzedni" / "mutation-summary.json"

    kod, wyjscie = _uruchom_poprzedni(capsys, _biezacy(tmp_path, 99.0), brak, "--scope", "full")

    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err == f"mutation_baseline: poprzedni wynik: nie znaleziono pliku '{brak}'\n"


def test_poprzedni_wynik_bez_pomiaru_jest_bledem_wejscia_a_nie_zielonym_porownaniem(tmp_path, capsys):
    kod, wyjscie = _uruchom_poprzedni(
        capsys, _biezacy(tmp_path, 99.0), _poprzedni(tmp_path, score=None, survived=0, total=0), "--scope", "full"
    )

    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err == KOMUNIKAT_BRAK_WYNIKU


def test_poprzedni_wynik_z_zepsutym_json_jest_bledem_wejscia_z_etykieta_poprzedniego_wyniku(tmp_path, capsys):
    zepsuty = tmp_path / "zepsuty.json"
    zepsuty.write_text("{to nie jest json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError) as przyczyna:
        json.loads("{to nie jest json")

    kod, wyjscie = _uruchom_poprzedni(capsys, _biezacy(tmp_path, 99.0), zepsuty, "--scope", "full")

    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err == f"mutation_baseline: poprzedni wynik: niepoprawny JSON w '{zepsuty}': {przyczyna.value}\n"


@pytest.mark.parametrize(
    ("tresc", "komunikat"),
    [
        pytest.param('["nie", "obiekt"]', "poprzedni wynik: oczekiwano obiektu JSON", id="nie_obiekt"),
        pytest.param(
            '{"score": "97", "total": 10, "killed": 9, "survived": 1}',
            "poprzedni wynik: klucz 'score' musi byc liczba albo null",
            id="score_napis",
        ),
        pytest.param(
            '{"total": 10, "killed": 9, "survived": 1}',
            "poprzedni wynik: brak wymaganego klucza 'score'",
            id="brak_score",
        ),
    ],
)
def test_poprzedni_wynik_o_zlym_ksztalcie_jest_bledem_wejscia_z_etykieta_poprzedniego_wyniku(
    tmp_path, capsys, tresc, komunikat
):
    zly = tmp_path / "zly-ksztalt.json"
    zly.write_text(tresc, encoding="utf-8")

    kod, wyjscie = _uruchom_poprzedni(capsys, _biezacy(tmp_path, 99.0), zly, "--scope", "full")

    assert kod == 2
    assert wyjscie.out == ""
    assert wyjscie.err == f"mutation_baseline: {komunikat}\n"


def test_wczytanie_poprzedniego_wyniku_zwraca_baseline_z_wynikiem_licznikami_i_zakresem_z_argumentu(tmp_path):
    plik = _poprzedni(tmp_path, score=SCORE_POPRZEDNI, survived=3, total=100)

    assert zapadka.load_previous_summary(plik, "full") == {
        "score": 97.0,
        "survived": 3,
        "total": 100,
        "scope": "full",
    }
    assert zapadka.load_previous_summary(plik, "diff")["scope"] == "diff"


def test_wczytanie_poprzedniego_wyniku_bez_pomiaru_rzuca_blad_wejscia_z_pelnym_komunikatem(tmp_path):
    plik = _poprzedni(tmp_path, score=None, survived=0, total=0)

    with pytest.raises(zapadka._InputError) as blad:
        zapadka.load_previous_summary(plik, "full")

    assert str(blad.value) == (
        "poprzedni wynik: brak wyniku (score=null) - poprzedni przebieg nie zmierzyl mutantow"
    )


def test_wczytanie_brakujacego_poprzedniego_wyniku_rzuca_blad_wejscia_zamiast_zwracac_none(tmp_path):
    brak = tmp_path / "nie-ma.json"

    with pytest.raises(zapadka._InputError) as blad:
        zapadka.load_previous_summary(brak, "full")

    assert str(blad.value) == f"poprzedni wynik: nie znaleziono pliku '{brak}'"


def test_brak_zrodla_porownania_jest_bledem_uzycia(tmp_path, capsys):
    kod = zapadka.main(["--summary", str(_biezacy(tmp_path, 99.0)), "--scope", "full"])
    wyjscie = capsys.readouterr()

    assert kod == 64
    assert wyjscie.out == ""
    assert wyjscie.err.startswith("mutation_baseline: ")
    assert "--baseline" in wyjscie.err
    assert "--previous-summary" in wyjscie.err


def test_baseline_i_poprzedni_wynik_naraz_to_blad_uzycia_i_baseline_zostaje_nietkniety(tmp_path, capsys):
    baseline = _baseline(tmp_path, score=SCORE_BASELINE)
    przed = baseline.read_bytes()

    kod = zapadka.main(
        [
            "--summary", str(_biezacy(tmp_path, 99.0)),
            "--baseline", str(baseline),
            "--previous-summary", str(_poprzedni(tmp_path)),
            "--scope", "full",
            "--commit", SHA,
        ]
    )
    wyjscie = capsys.readouterr()

    assert kod == 64
    assert wyjscie.out == ""
    assert wyjscie.err.startswith("mutation_baseline: ")
    assert baseline.read_bytes() == przed

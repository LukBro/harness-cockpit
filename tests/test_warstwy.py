"""Reguly architektury sprawdzane testem, nie dyscyplina."""
from cockpit.services.invariants import WSZYSTKIE, Niezmiennik
from cockpit.services.walidacja import SPRAWDZENIA

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from check_layers import sprawdz  # noqa: E402


def test_warstwa_api_nie_dotyka_bazy():
    assert sprawdz() == []


def test_lista_niezmiennikow_ma_siedem_pozycji_i_kazda_ma_sprawdzenie():
    """Dopisanie niezmiennika bez sprawdzenia w walidatorze ma wywalic test."""
    assert len(WSZYSTKIE) == 7
    assert Niezmiennik.measured_or_null in WSZYSTKIE
    assert Niezmiennik.evidence_required in WSZYSTKIE
    assert set(SPRAWDZENIA) == WSZYSTKIE
    for niezmiennik, sprawdzenia in SPRAWDZENIA.items():
        assert len(sprawdzenia) > 0, f"niezmiennik {niezmiennik} nie ma zadnego sprawdzenia"

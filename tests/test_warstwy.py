"""Reguly architektury sprawdzane testem, nie dyscyplina."""
from cockpit.services.invariants import WSZYSTKIE, Niezmiennik

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from check_layers import sprawdz  # noqa: E402


def test_warstwa_api_nie_dotyka_bazy():
    assert sprawdz() == []


def test_lista_niezmiennikow_ma_szesc_pozycji():
    """Dopisanie niezmiennika bez sprawdzenia w walidatorze ma wywalic test."""
    assert len(WSZYSTKIE) == 6
    assert Niezmiennik.measured_or_null in WSZYSTKIE

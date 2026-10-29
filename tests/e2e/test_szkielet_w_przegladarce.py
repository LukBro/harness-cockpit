"""Przelot szkieletu w prawdziwej przegladarce na PostgreSQL (HAR-3)."""
from __future__ import annotations

import subprocess
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie."
LIMIT_WYNIKU_MS = 3000


def _id_runu(page) -> str:
    return parse_qs(urlparse(page.url).query)["run"][0]


def test_klikniecie_uruchom_pokazuje_wynik_procesu(runner_online, zalogowana_strona):
    page = zalogowana_strona

    page.get_by_role("button", name="Uruchom").click()

    expect(page.locator("li", has_text="szkielet dziala")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS
    )
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_przeladowanie_strony_pokazuje_ten_sam_run_i_te_same_zdarzenia(
    runner_online, zalogowana_strona
):
    page = zalogowana_strona
    page.get_by_role("button", name="Uruchom").click()
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    id_przed = _id_runu(page)
    zdarzenia_przed = page.locator("li").all_inner_texts()
    assert zdarzenia_przed, "run nie pokazal zadnego zdarzenia przed przeladowaniem"

    page.reload()

    expect(page.get_by_text(f"run {id_przed}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.locator("li")).to_have_text(zdarzenia_przed, timeout=LIMIT_WYNIKU_MS)
    assert _id_runu(page) == id_przed


def test_zatrzymany_runner_daje_komunikat_offline_i_gotowy_przycisk(
    zatrzymaj_runnera, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    runow_przed_kliknieciem = zatrzymaj_runnera()

    page.get_by_role("button", name="Uruchom").click()

    expect(page.get_by_text(KOMUNIKAT_OFFLINE)).to_be_visible()
    expect(page.get_by_role("button", name="Uruchom")).to_be_enabled()
    assert liczba_runow() == runow_przed_kliknieciem, (
        "przy runnerze offline klikniecie nie moze zostawic runu w bazie"
    )


def test_token_runnera_nie_jest_widoczny_w_argumentach_procesu(runner_online, token_runnera):
    argumenty = subprocess.run(
        ["ps", "-o", "args=", "-p", str(runner_online.pid)],
        capture_output=True, text=True, check=True,
    ).stdout

    assert "runner" in argumenty, "ps nie pokazal procesu runnera"
    assert token_runnera not in argumenty

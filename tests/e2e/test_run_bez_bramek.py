"""Widok runu w przegladarce: pokrycie bramkami i oznaczenie runu, ktorego nic nie sprawdzilo (HAR-178).

Zdarzenia wysyla test (jak prawdziwy runner: Bearer + POST /api/events), wiec proces runnera
nie jest potrzebny. Run, w ktorym pominieto wszystkie bramki, dalej jest "do odbioru":
komunikat nie zmienia statusu i niczego nie blokuje.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_WYNIKU_MS = 3000
BRAK_CAPABILITY_LINT = "brak_capability: brak capability lint"
BRAK_CAPABILITY_TEST = "brak_capability: brak capability test_full"


def _utworz_run_w_toku() -> str:
    """Run zespolu zasianego w bazie (status `running`), przypisany do jego runnera."""
    run_id = str(uuid.uuid4())

    async def _wstaw():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, "
                         "status, created_at) "
                         "SELECT :id, tenant_id, id, 'local', 'szkielet', 'running', now() "
                         "FROM runner LIMIT 1"),
                    {"id": run_id},
                )
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pula:
        pula.submit(lambda: asyncio.run(_wstaw())).result()
    return run_id


def _zdarzenie(adres, token, run_id, step_id, kind, *, tekst="", kroki_weryfikujace=None):
    tresc = {"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": step_id,
             "agent_id": "", "kind": kind, "text": tekst}
    if kroki_weryfikujace is not None:
        tresc["kroki_weryfikujace"] = kroki_weryfikujace
    odpowiedz = httpx.post(
        f"{adres}/api/events", headers={"Authorization": f"Bearer {token}"},
        json=tresc, timeout=5,
    )
    assert odpowiedz.status_code == 200, odpowiedz.text


def _wykonany(adres, token, run_id, krok):
    _zdarzenie(adres, token, run_id, krok, "step_started")
    _zdarzenie(adres, token, run_id, krok, "step_finished")


def _pominiety(adres, token, run_id, krok, powod):
    _zdarzenie(adres, token, run_id, krok, "step_skipped", tekst=powod)


def _otworz_run(page, adres, run_id):
    page.goto(f"{adres}/?run={run_id}")
    expect(page.get_by_text(re.compile(r"status: \w+"))).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def _run_niesprawdzony(adres, token) -> str:
    run_id = _utworz_run_w_toku()
    _pominiety(adres, token, run_id, "lint", BRAK_CAPABILITY_LINT)
    _pominiety(adres, token, run_id, "test", BRAK_CAPABILITY_TEST)
    _zdarzenie(adres, token, run_id, "run", "run_finished", kroki_weryfikujace=["lint", "test"])
    return run_id


def test_run_ktorego_nic_nie_sprawdzilo_pokazuje_komunikat_o_pominietych_bramkach(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_niesprawdzony(adres_kokpitu, token_runnera)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Nic tego nie sprawdziło: pominięto 2 z 2 bramek", exact=True)
           ).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_run_ktorego_nic_nie_sprawdzilo_pokazuje_przy_kazdej_bramce_powod_pominiecia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_niesprawdzony(adres_kokpitu, token_runnera)

    _otworz_run(page, adres_kokpitu, run_id)

    powody = page.locator("li")
    expect(powody.filter(has_text="lint: brak_capability")).to_have_count(
        1, timeout=LIMIT_WYNIKU_MS)
    expect(powody.filter(has_text="test: brak_capability")).to_have_count(1)


def test_run_ktorego_nic_nie_sprawdzilo_jest_dalej_do_odbioru(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_niesprawdzony(adres_kokpitu, token_runnera)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.locator("body")).to_contain_text("do odbioru", timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("Nic tego nie sprawdziło", exact=False)).to_be_visible()
    assert "gotowe" not in page.locator("body").inner_text().lower()


def test_run_z_wykonanymi_bramkami_pokazuje_ile_sprawdzono_i_nie_ostrzega(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _wykonany(adres_kokpitu, token_runnera, run_id, "lint")
    _wykonany(adres_kokpitu, token_runnera, run_id, "test")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "run", "run_finished",
               kroki_weryfikujace=["lint", "test"])

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Sprawdzono 2 z 2 bramek", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    assert "Nic tego nie sprawdziło" not in page.locator("body").inner_text()
    expect(page.locator("li").filter(has_text="brak_capability")).to_have_count(0)


def test_run_z_czesciowo_wykonanymi_bramkami_pokazuje_ile_sprawdzono_i_powod_pominiecia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _wykonany(adres_kokpitu, token_runnera, run_id, "lint")
    _pominiety(adres_kokpitu, token_runnera, run_id, "test", BRAK_CAPABILITY_TEST)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "run", "run_finished",
               kroki_weryfikujace=["lint", "test"])

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Sprawdzono 1 z 2 bramek", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    expect(page.locator("li").filter(has_text="test: brak_capability")).to_have_count(1)
    assert "Nic tego nie sprawdziło" not in page.locator("body").inner_text()


def test_run_w_toku_nie_pokazuje_sekcji_pokrycia_ani_zera_bramek(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _pominiety(adres_kokpitu, token_runnera, run_id, "lint", BRAK_CAPABILITY_LINT)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Stan runu: w toku", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    tresc = page.locator("body").inner_text()
    assert "bramek" not in tresc
    assert "0 z 0" not in tresc
    assert "Nic tego nie sprawdziło" not in tresc


def test_run_zakonczony_bez_listy_krokow_weryfikujacych_nie_pokazuje_sekcji_pokrycia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _wykonany(adres_kokpitu, token_runnera, run_id, "lint")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "run", "run_finished")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    tresc = page.locator("body").inner_text()
    assert "bramek" not in tresc
    assert "0 z 0" not in tresc


def test_przejscie_z_runu_niesprawdzonego_na_run_w_toku_zdejmuje_komunikat_bez_przeladowania(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a = _run_niesprawdzony(adres_kokpitu, token_runnera)
    run_b = str(uuid.uuid4())
    _otworz_run(page, adres_kokpitu, run_a)
    komunikat = page.get_by_text("Nic tego nie sprawdziło: pominięto 2 z 2 bramek", exact=True)
    expect(komunikat).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    page.evaluate("window.__bez_przeladowania = true")
    # run B powstaje przez "Uruchom" (odpowiedz podstawiona): w toku, bez pokrycia
    szczegoly_b = {"id": run_b, "workflow": "szkielet", "status": "running",
                   "runner_kind": "local", "reason": None, "events": [], "steps": [],
                   "pokrycie": None}
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": run_b})
               if r.request.method == "POST" else r.continue_())
    page.route(f"**/api/runs/{run_b}", lambda r: r.fulfill(json=szczegoly_b))
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_enabled(timeout=2 * LIMIT_WYNIKU_MS)

    przycisk.click()

    expect(page.get_by_text(f"run {run_b}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(komunikat).to_have_count(0)
    assert "bramek" not in page.locator("body").inner_text()
    assert page.evaluate("window.__bez_przeladowania") is True, "strona zostala przeladowana"


def test_komunikat_o_braku_sprawdzenia_jest_alertem_a_komunikat_o_sprawdzeniu_nim_nie_jest(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    niesprawdzony = _run_niesprawdzony(adres_kokpitu, token_runnera)
    sprawdzony = _utworz_run_w_toku()
    _wykonany(adres_kokpitu, token_runnera, sprawdzony, "lint")
    _zdarzenie(adres_kokpitu, token_runnera, sprawdzony, "run", "run_finished",
               kroki_weryfikujace=["lint"])

    _otworz_run(page, adres_kokpitu, niesprawdzony)
    alert = page.get_by_role("alert").filter(has_text="Nic tego nie sprawdziło")
    expect(alert).to_have_count(1, timeout=LIMIT_WYNIKU_MS)

    _otworz_run(page, adres_kokpitu, sprawdzony)
    expect(page.get_by_text("Sprawdzono 1 z 1 bramek", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_role("alert").filter(has_text="Sprawdzono")).to_have_count(0)


def test_komunikat_o_braku_sprawdzenia_liczy_pominiete_osobno_od_zaplanowanych(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    # 3 bramki zaplanowane, 0 wykonanych; pominiecie przez nieudana zaleznosc nie jest
    # nazwanym pominieciem, wiec pominiete = 2, a zaplanowane = 3
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "przygotowanie", "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "przygotowanie", "step_failed",
               tekst="kod wyjscia 1")
    _pominiety(adres_kokpitu, token_runnera, run_id, "lint", BRAK_CAPABILITY_LINT)
    _pominiety(adres_kokpitu, token_runnera, run_id, "test", BRAK_CAPABILITY_TEST)
    _pominiety(adres_kokpitu, token_runnera, run_id, "build",
               "zaleznosc_nieudana: zależność nieudana: przygotowanie")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "run", "run_finished",
               kroki_weryfikujace=["lint", "test", "build"])

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Nic tego nie sprawdziło: pominięto 2 z 3 bramek", exact=True)
           ).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_run_sprawdzony_bez_powodow_pominiec_nie_ma_w_sekcji_pokrycia_listy(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _wykonany(adres_kokpitu, token_runnera, run_id, "lint")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "run", "run_finished",
               kroki_weryfikujace=["lint"])

    _otworz_run(page, adres_kokpitu, run_id)

    komunikat = page.get_by_text("Sprawdzono 1 z 1 bramek", exact=True)
    expect(komunikat).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(komunikat.locator("xpath=..").locator("ul")).to_have_count(0)


def test_komunikat_poprzedniego_runu_znika_zanim_nadejdzie_odpowiedz_o_nowym_runie(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a = _run_niesprawdzony(adres_kokpitu, token_runnera)
    run_b = str(uuid.uuid4())
    _otworz_run(page, adres_kokpitu, run_a)
    komunikat = page.get_by_text("Nic tego nie sprawdziło: pominięto 2 z 2 bramek", exact=True)
    expect(komunikat).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    zatrzymane = []      # odpowiedz o runie B wstrzymana: nie nadejdzie przed asercja
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": run_b})
               if r.request.method == "POST" else r.continue_())
    page.route(f"**/api/runs/{run_b}", lambda r: zatrzymane.append(r))
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_enabled(timeout=2 * LIMIT_WYNIKU_MS)

    przycisk.click()

    expect(page.get_by_text(f"run {run_b}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    for _ in range(30):
        if zatrzymane:
            break
        page.wait_for_timeout(100)
    assert zatrzymane, "pobranie szczegolow runu B nie ruszylo"
    expect(komunikat).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    assert "bramek" not in page.locator("body").inner_text()
    zatrzymane[0].fulfill(json={
        "id": run_b, "workflow": "szkielet", "status": "running", "runner_kind": "local",
        "reason": None, "events": [], "steps": [], "pokrycie": None})


def test_odpowiedz_serwera_bez_pola_pokrycia_nie_psuje_widoku_runu_i_nie_pokazuje_sekcji(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = str(uuid.uuid4())
    szczegoly = {"id": run_id, "workflow": "szkielet", "status": "running",
                 "runner_kind": "local", "reason": None, "events": [], "steps": []}
    page.route(f"**/api/runs/{run_id}", lambda r: r.fulfill(json=szczegoly))

    page.goto(f"{adres_kokpitu}/?run={run_id}")

    expect(page.get_by_text("Stan runu: w toku", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    assert "bramek" not in page.locator("body").inner_text()

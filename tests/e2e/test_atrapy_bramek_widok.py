"""HAR-246: widok runu przy bramce, ktora nie moze zawiesc - ostrzezenie i "uruchom mimo to".

Zdarzenia wysyla test (jak prawdziwy runner: Bearer + POST /api/events). Brak ostrzezenia
niczego nie dopisuje: kokpit NIGDY nie twierdzi, ze bramka jest aktywna.
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

from test_run_bez_bramek import _otworz_run, _utworz_run_w_toku

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_MS = 5000
OSTRZEZENIE = ("atrapa bramki — kod wyjścia wymuszony na zero: „pytest || true”. "
               "Co poprawić: usuń || true.")
POMINIETE = "ostrzeżenie pominięte przez człowieka"
BURSZTYN = "rgb(138, 90, 0)"     # #8A5A00
# zielony = skladowa G wyraznie ponad R i B (kolor tekstu, tla i ramki kazdego elementu widoku)
ZIELONE_ELEMENTY = """(main) => [main, ...main.querySelectorAll('*')].flatMap((e) => {
  const s = getComputedStyle(e);
  return [s.color, s.backgroundColor, s.borderTopColor].filter((k) => {
    const [r, g, b, a = 1] = (k.match(/[\\d.]+/g) || []).map(Number);
    return a > 0 && g - Math.max(r, b) >= 10;
  }).map((k) => `${e.tagName}: ${k}`);
})"""


def _zdarzenie(adres, token, run_id, step_id, kind, *, tekst="", proba=1):
    odpowiedz = httpx.post(
        f"{adres}/api/events", headers={"Authorization": f"Bearer {token}"},
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": step_id,
              "agent_id": step_id, "kind": kind, "text": tekst, "attempt": proba},
        timeout=5,
    )
    assert odpowiedz.status_code == 200, odpowiedz.text


def _bramka(adres, token, run_id, krok="test", ostrzezenie: str | None = OSTRZEZENIE, proba=1):
    _zdarzenie(adres, token, run_id, krok, "step_started", proba=proba)
    if ostrzezenie is not None:
        _zdarzenie(adres, token, run_id, krok, "warning", tekst=ostrzezenie, proba=proba)
    _zdarzenie(adres, token, run_id, krok, "step_finished", proba=proba)


def _liczba_pominiec(run_id: str, krok: str) -> int:
    async def _pytaj():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.connect() as conn:
                return (await conn.execute(
                    text("SELECT count(*) FROM event WHERE run_id = :run AND step_id = :krok "
                         "AND kind = 'atrapa_pominieta'"),
                    {"run": run_id, "krok": krok})).scalar_one()
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pula:
        return pula.submit(lambda: asyncio.run(_pytaj())).result(timeout=60)


def _tabela(page):
    return page.get_by_role("table", name="Kroki runu")


def _przycisk(page, krok="test"):
    return page.get_by_role("button", name=f"Uruchom mimo to — krok {krok}", exact=True)


def _nie_twierdzi_ze_bramka_dziala(page):
    tresc = page.locator("main").inner_text()
    assert "bramka aktywna" not in tresc.lower()
    assert not re.search("[✓✔☑✅]", tresc)
    assert page.locator("main").evaluate(ZIELONE_ELEMENTY) == []


def test_ostrzezenie_o_atrapie_jest_raz_przy_ostatnim_wierszu_kroku_bramki(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id, proba=1)
    _bramka(adres_kokpitu, token_runnera, run_id, ostrzezenie=None, proba=2)

    _otworz_run(page, adres_kokpitu, run_id)

    w_tabeli = _tabela(page).get_by_text(OSTRZEZENIE, exact=False)
    expect(w_tabeli).to_have_count(1, timeout=LIMIT_MS)
    wiersz = _tabela(page).get_by_role("row").filter(has_text="próba 2")
    expect(wiersz).to_contain_text(OSTRZEZENIE)
    expect(w_tabeli).to_have_css("color", BURSZTYN)
    expect(_przycisk(page)).to_have_count(1)
    expect(wiersz.get_by_role("button", name="Uruchom mimo to — krok test", exact=True)
           ).to_be_visible()
    # przycisk rownoleglego runu to inna akcja: nie moze nazywac sie tak samo
    expect(page.get_by_role("button", name="Uruchom mimo to", exact=True)).to_have_count(0)
    _nie_twierdzi_ze_bramka_dziala(page)


def test_uruchom_mimo_to_zapisuje_pominiecie_i_zostawia_ostrzezenie_widoczne(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    expect(_przycisk(page)).to_be_visible(timeout=LIMIT_MS)
    expect(page.get_by_text(POMINIETE, exact=False)).to_have_count(0)

    _przycisk(page).click()

    expect(_przycisk(page)).to_have_count(0, timeout=LIMIT_MS)
    expect(_tabela(page).get_by_text(POMINIETE, exact=False)).to_be_visible()
    expect(_tabela(page).get_by_text(OSTRZEZENIE, exact=False)).to_be_visible()
    assert _liczba_pominiec(run_id, "test") == 1
    _nie_twierdzi_ze_bramka_dziala(page)

    page.reload()
    expect(_tabela(page).get_by_text(POMINIETE, exact=False)).to_be_visible(timeout=LIMIT_MS)
    expect(_przycisk(page)).to_have_count(0)


def test_pominiecie_jednej_bramki_nie_pomija_drugiej(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id, "lint")
    _bramka(adres_kokpitu, token_runnera, run_id, "test")
    _otworz_run(page, adres_kokpitu, run_id)

    _przycisk(page, "lint").click(timeout=LIMIT_MS)

    expect(_przycisk(page, "lint")).to_have_count(0, timeout=LIMIT_MS)
    expect(_przycisk(page, "test")).to_be_visible()
    expect(page.get_by_text(POMINIETE, exact=False)).to_have_count(1)
    assert (_liczba_pominiec(run_id, "lint"), _liczba_pominiec(run_id, "test")) == (1, 0)


def test_nowe_ostrzezenie_po_pominieciu_przywraca_przycisk(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    _przycisk(page).click(timeout=LIMIT_MS)
    expect(_przycisk(page)).to_have_count(0, timeout=LIMIT_MS)

    _bramka(adres_kokpitu, token_runnera, run_id, proba=2)

    expect(_przycisk(page)).to_be_visible(timeout=LIMIT_MS)
    expect(page.get_by_text(POMINIETE, exact=False)).to_have_count(0)


def test_odrzucone_pominiecie_pokazuje_blad_i_nie_udaje_ze_pominieto(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    page.route("**/pominiecie-ostrzezenia", lambda r: r.fulfill(
        status=409, json={"detail": "nie ma ostrzeżenia do pominięcia"}))

    _przycisk(page).click(timeout=LIMIT_MS)

    expect(page.get_by_role("alert").filter(has_text="nie ma ostrzeżenia do pominięcia")
           ).to_be_visible(timeout=LIMIT_MS)
    expect(_przycisk(page)).to_be_visible()
    expect(page.get_by_text(POMINIETE, exact=False)).to_have_count(0)
    assert _liczba_pominiec(run_id, "test") == 0


def test_zwykle_wyjscie_z_tekstem_atrapy_nie_jest_ostrzezeniem_przy_kroku(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "test", "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "test", "output", tekst=OSTRZEZENIE)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "test", "step_finished")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_role("listitem").filter(has_text=OSTRZEZENIE)).to_be_visible(
        timeout=LIMIT_MS)
    expect(_tabela(page).get_by_role("row").filter(has_text="próba 1")).to_be_visible()
    expect(_tabela(page).get_by_text(OSTRZEZENIE, exact=False)).to_have_count(0)
    expect(_przycisk(page)).to_have_count(0)


def test_wszystkie_ostrzezenia_kroku_sa_widoczne_a_przycisk_jest_jeden(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    drugie = ("atrapa bramki — testy przechodzą bez testów: „--passWithNoTests”. "
              "Co poprawić: usuń flagę.")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "test", "step_started")
    for tekst in (OSTRZEZENIE, drugie):
        _zdarzenie(adres_kokpitu, token_runnera, run_id, "test", "warning", tekst=tekst)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(_tabela(page).get_by_text(drugie, exact=False)).to_be_visible(timeout=LIMIT_MS)
    expect(_tabela(page).get_by_text(OSTRZEZENIE, exact=False)).to_be_visible()
    expect(_przycisk(page)).to_have_count(1)


def test_pominiecie_w_dzienniku_zdarzen_wskazuje_krok(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)

    _przycisk(page).click(timeout=LIMIT_MS)

    expect(page.get_by_role("listitem").filter(has_text="atrapa_pominieta")).to_have_text(
        "atrapa_pominieta krok test", timeout=LIMIT_MS)


def test_sama_odpowiedz_na_pominiecie_bez_zdarzenia_nie_udaje_ze_pominieto(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    page.route("**/pominiecie-ostrzezenia", lambda r: r.fulfill(
        json={"run_id": run_id, "step_id": "test"}))

    with page.expect_response("**/pominiecie-ostrzezenia", timeout=LIMIT_MS):
        _przycisk(page).click(timeout=LIMIT_MS)

    expect(_przycisk(page)).to_be_enabled(timeout=LIMIT_MS)
    expect(page.get_by_text(POMINIETE, exact=False)).to_have_count(0)


@pytest.mark.parametrize(
    ("trasa", "komunikat"),
    [
        (lambda r: r.fulfill(status=409, json={"detail": "nic do pominięcia"}),
         "Pominięcie nieprzyjęte: nic do pominięcia"),
        (lambda r: r.fulfill(status=500, json={}), "Pominięcie nieprzyjęte: błąd 500"),
        (lambda r: r.abort(), "Pominięcie nieprzyjęte: Brak połączenia z kokpitem."),
    ],
    ids=["z powodem", "bez powodu", "brak polaczenia"])
def test_nieudane_pominiecie_nazywa_przyczyne_a_udane_ponowienie_zdejmuje_komunikat(
    zalogowana_strona, adres_kokpitu, token_runnera, trasa, komunikat
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    page.route("**/pominiecie-ostrzezenia", trasa, times=1)
    _przycisk(page).click(timeout=LIMIT_MS)
    alert = page.get_by_role("alert").filter(has_text="Pominięcie nieprzyjęte")
    expect(alert).to_have_text(komunikat, timeout=LIMIT_MS)

    _przycisk(page).click(timeout=LIMIT_MS)

    expect(page.get_by_text(POMINIETE, exact=False)).to_be_visible(timeout=LIMIT_MS)
    expect(alert).to_have_count(0)
    assert _liczba_pominiec(run_id, "test") == 1


def test_w_trakcie_wysylania_pominiecia_nie_da_sie_wyslac_go_drugi_raz(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    zatrzymane = []
    page.route("**/pominiecie-ostrzezenia", lambda r: zatrzymane.append(r))

    _przycisk(page).click(timeout=LIMIT_MS)

    expect(_przycisk(page)).to_be_disabled(timeout=LIMIT_MS)
    for _ in range(30):
        if zatrzymane:
            break
        page.wait_for_timeout(100)
    assert len(zatrzymane) == 1
    zatrzymane[0].fulfill(status=409, json={"detail": "nic do pominięcia"})
    expect(_przycisk(page)).to_be_enabled(timeout=LIMIT_MS)


@pytest.mark.parametrize("stan", ["po bledzie", "w trakcie wysylania"])
def test_przejscie_na_inny_run_nie_przenosi_stanu_pominiecia(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera, stan
):
    page = zalogowana_strona
    run_a = _utworz_run_w_toku()
    run_b = str(uuid.uuid4())
    _bramka(adres_kokpitu, token_runnera, run_a)
    _otworz_run(page, adres_kokpitu, run_a)
    zatrzymane = []

    def pominiecie(r):
        if stan == "po bledzie":
            r.fulfill(status=409, json={"detail": "nic do pominięcia"})
        else:
            zatrzymane.append(r)

    page.route("**/pominiecie-ostrzezenia", pominiecie)
    _przycisk(page).click(timeout=LIMIT_MS)
    alert = page.get_by_role("alert").filter(has_text="Pominięcie nieprzyjęte")
    if stan == "po bledzie":
        expect(alert).to_be_visible(timeout=LIMIT_MS)
    else:
        expect(_przycisk(page)).to_be_disabled(timeout=LIMIT_MS)
    szczegoly_b = {
        "id": run_b, "workflow": "szkielet", "status": "running", "runner_kind": "local",
        "reason": None, "pokrycie": None,
        "events": [{"seq": 1, "kind": "warning", "text": OSTRZEZENIE, "step_id": "test",
                    "agent_type": "", "text_truncated": False, "text_full_len": None,
                    "at": "2026-01-01T00:00:00+00:00"}],
        "steps": [{"step_id": "test", "agent_id": "test", "attempt": 1, "status": "finished",
                   "reason": None, "cost_usd": None, "tokens_in": None, "tokens_out": None,
                   "num_turns": None, "session_id": None}],
    }
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": run_b})
               if r.request.method == "POST" else r.continue_())
    page.route(f"**/api/runs/{run_b}", lambda r: r.fulfill(json=szczegoly_b))
    uruchom = page.get_by_role("button", name="Uruchom", exact=True)
    expect(uruchom).to_be_enabled(timeout=2 * LIMIT_MS)

    uruchom.click()

    expect(page.get_by_text(f"run {run_b}")).to_be_visible(timeout=LIMIT_MS)
    expect(_przycisk(page)).to_be_enabled(timeout=LIMIT_MS)
    expect(alert).to_have_count(0)


@pytest.mark.parametrize(
    "ostrzezenie", [None, "test: pozorny_konsensus: jeden dostawca"],
    ids=["bez ostrzezen", "ostrzezenie inne niz atrapa"])
def test_brak_sygnalu_atrapy_nie_jest_pokazany_jako_bramka_aktywna(
    zalogowana_strona, adres_kokpitu, token_runnera, ostrzezenie
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _bramka(adres_kokpitu, token_runnera, run_id, ostrzezenie=ostrzezenie)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "test", "run_finished")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = _tabela(page).get_by_role("row").filter(has_text="próba 1")
    expect(wiersz).to_be_visible(timeout=LIMIT_MS)
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_MS)
    # wiersz kroku ma dokladnie piec komorek z dotychczasowa trescia, bez dopiskow
    expect(wiersz.get_by_role("cell")).to_have_text(
        ["test", "test", "próba 1", "zakończony", "—"])
    expect(page.get_by_role("button", name=re.compile("Uruchom mimo to"))).to_have_count(0)
    expect(page.get_by_text(POMINIETE, exact=False)).to_have_count(0)
    _nie_twierdzi_ze_bramka_dziala(page)

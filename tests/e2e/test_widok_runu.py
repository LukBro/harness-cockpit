"""Widok runu w przegladarce: kroki, subagenci, proby, powody i stan "do odbioru" (HAR-81).

Zdarzenia wysyla test (jak prawdziwy runner: Bearer + POST /api/events), wiec proces
runnera nie jest potrzebny.
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
OKNO_NA_POST_MS = 1000
TABELA_KROKOW = "table[aria-label='Kroki runu']"
WIERSZE = f"{TABELA_KROKOW} tbody tr"
# tresc strony bez tabeli KROKOW (tabela runnerow zostaje): sprawdza napis statusu runu
TRESC_BEZ_TABELI = (
    "() => { const k = document.body.cloneNode(true); "
    f'k.querySelectorAll("{TABELA_KROKOW}").forEach(t => t.remove()); return k.innerText; }}'
)


STATUSY_RUNU = ("pending", "running", "waiting", "to_accept", "failed", "skipped", "aborted",
                "rejected")


def _utworz_run_w_toku(status: str = "running") -> str:
    """Run zespolu zasianego w bazie (domyslnie `running`), przypisany do jego runnera."""
    assert status in STATUSY_RUNU    # trafia do SQL jako literal: enum nie przyjmie parametru tekstowego
    run_id = str(uuid.uuid4())

    async def _wstaw():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, "
                         "status, created_at) "
                         f"SELECT :id, tenant_id, id, 'local', 'szkielet', '{status}', now() "
                         "FROM runner LIMIT 1"),
                    {"id": run_id},
                )
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pula:
        pula.submit(lambda: asyncio.run(_wstaw())).result()
    return run_id


def _zdarzenie(adres, token, run_id, step_id, kind, *, agent_id="", tekst="", proba=None):
    tresc = {"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": step_id,
             "agent_id": agent_id, "kind": kind, "text": tekst}
    if proba is not None:
        tresc["attempt"] = proba
    odpowiedz = httpx.post(
        f"{adres}/api/events", headers={"Authorization": f"Bearer {token}"},
        json=tresc, timeout=5,
    )
    assert odpowiedz.status_code == 200, odpowiedz.text


def _otworz_run(page, adres, run_id):
    page.goto(f"{adres}/?run={run_id}")
    expect(page.get_by_text(re.compile(r"status: \w+"))).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_krok_z_trzema_subagentami_pokazuje_trzy_wiersze_z_ich_identyfikatorami(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    for agent in ("sub-a", "sub-b", "sub-c"):
        _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_started", agent_id=agent)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.locator(WIERSZE)).to_have_count(3, timeout=LIMIT_WYNIKU_MS)
    for agent in ("sub-a", "sub-b", "sub-c"):
        wiersz = page.locator(WIERSZE).filter(has_text=agent)
        expect(wiersz).to_have_count(1)
        expect(wiersz).to_contain_text("s1")
        expect(wiersz).to_contain_text("w toku")


def test_krok_pominiety_ma_stan_pominiety_i_powod_z_tekstu_zdarzenia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_finished")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped",
               tekst="brak_capability: brak narzedzia git")

    _otworz_run(page, adres_kokpitu, run_id)

    # powod pominiecia backend zostawia pusty; front bierze go z tekstu zdarzenia step_skipped
    pominiety = page.locator(WIERSZE).filter(has_text="s2")
    expect(pominiety).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(pominiety).to_contain_text("pominięty")
    expect(pominiety).to_contain_text("brak_capability: brak narzedzia git")
    # powod pominietego kroku nie przecieka do wiersza kroku zakonczonego
    zakonczony = page.locator(WIERSZE).filter(has_text="s1")
    expect(zakonczony).to_contain_text("zakończony")
    expect(zakonczony).not_to_contain_text("brak_capability")


def test_krok_nieudany_pokazuje_stan_nieudany_z_powodem_porazki(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_failed", tekst="kod wyjscia 3")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s3")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(wiersz).to_contain_text("nieudany")
    expect(wiersz).to_contain_text("kod wyjscia 3")


def test_powtorzony_krok_pokazuje_numer_proby_drugiej_obok_pierwszej(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_started", proba=1)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_failed",
               tekst="kod wyjscia 3", proba=1)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_started", proba=2)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_finished", proba=2)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.locator(WIERSZE)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    pierwsza = page.locator(WIERSZE).filter(has_text="próba 1")
    druga = page.locator(WIERSZE).filter(has_text="próba 2")
    expect(pierwsza).to_have_count(1)
    expect(pierwsza).to_contain_text("nieudany")
    expect(druga).to_have_count(1)
    expect(druga).to_contain_text("zakończony")
    expect(druga).not_to_contain_text("kod wyjscia 3")


def test_run_zakonczony_sukcesem_pokazuje_do_odbioru_i_nigdy_gotowe(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_finished")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s0", "run_finished")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.locator("body")).to_contain_text("do odbioru", timeout=LIMIT_WYNIKU_MS)
    tresc = page.locator("body").inner_text()
    assert "gotowe" not in tresc.lower(), "run do odbioru nie moze byc nazwany gotowym"


def test_run_z_krokiem_nieudanym_pokazuje_nieudany_z_powodem_runu_i_nie_do_odbioru(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_failed", tekst="kod wyjscia 3")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s0", "run_finished")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("status: failed")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("nieudane kroki: s3")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    # napis statusu runu po polsku, poza tabela krokow (tam tez jest "nieudany")
    assert "nieudany" in page.evaluate(TRESC_BEZ_TABELI).lower()
    assert "do odbioru" not in page.locator("body").inner_text().lower(), (
        "run nieudany nie moze byc do odbioru"
    )


def test_tabela_krokow_odswieza_sie_ze_strumienia_bez_przeladowania_strony(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _otworz_run(page, adres_kokpitu, run_id)
    page.wait_for_timeout(OKNO_NA_POST_MS)    # strumien runu jest otwarty, zanim ruszą zdarzenia
    # znacznik na obiekcie window ginie przy przeladowaniu strony
    page.evaluate("window.__bez_przeladowania = true")

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped",
               tekst="brak_capability: brak narzedzia git")
    pominiety = page.locator(WIERSZE).filter(has_text="s2")
    expect(pominiety).to_contain_text("pominięty", timeout=LIMIT_WYNIKU_MS)
    expect(pominiety).to_contain_text("brak_capability: brak narzedzia git")

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s4", "step_started", agent_id="sub-z")
    nowy = page.locator(WIERSZE).filter(has_text="s4")
    expect(nowy).to_contain_text("w toku", timeout=LIMIT_WYNIKU_MS)
    expect(nowy).to_contain_text("sub-z")

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s4", "step_finished", agent_id="sub-z")
    expect(nowy).to_contain_text("zakończony", timeout=LIMIT_WYNIKU_MS)
    assert page.evaluate("window.__bez_przeladowania") is True, "strona zostala przeladowana"


KOLUMNA_AGENT = 1
KOLUMNA_STAN = 3
KOLUMNA_POWOD = 4


def _komorka(wiersz, numer):
    return wiersz.locator("td").nth(numer)


def _otworz_run_ze_strumieniem(page, adres, run_id):
    _otworz_run(page, adres, run_id)
    page.wait_for_timeout(OKNO_NA_POST_MS)    # strumien runu jest otwarty, zanim ruszy zdarzenie


def test_run_w_toku_pokazuje_stan_runu_w_toku(zalogowana_strona, adres_kokpitu):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Stan runu: w toku", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)


def test_run_bez_krokow_nie_pokazuje_tabeli_krokow(zalogowana_strona, adres_kokpitu):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.locator(TABELA_KROKOW)).to_have_count(0)


def test_rozpoczecie_kroku_po_otwarciu_runu_dodaje_wiersz_w_toku_bez_przeladowania(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _otworz_run_ze_strumieniem(page, adres_kokpitu, run_id)

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s6", "step_started", agent_id="sub-q")

    wiersz = page.locator(WIERSZE).filter(has_text="s6")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("w toku")


def test_pominiecie_kroku_po_otwarciu_runu_dodaje_wiersz_pominiety_z_powodem(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _otworz_run_ze_strumieniem(page, adres_kokpitu, run_id)

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s7", "step_skipped",
               tekst="brak_capability: brak narzedzia git")

    wiersz = page.locator(WIERSZE).filter(has_text="s7")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("pominięty")
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("brak_capability: brak narzedzia git")


def test_oczekiwanie_kroku_po_otwarciu_runu_dodaje_wiersz_czeka(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _otworz_run_ze_strumieniem(page, adres_kokpitu, run_id)

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s8", "step_waiting")

    wiersz = page.locator(WIERSZE).filter(has_text="s8")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("czeka")


def test_przerwanie_kroku_po_otwarciu_runu_dodaje_wiersz_nieudany_z_powodem_przerwania(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _otworz_run_ze_strumieniem(page, adres_kokpitu, run_id)

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s9", "step_aborted",
               tekst="brak miejsca na dysku")

    wiersz = page.locator(WIERSZE).filter(has_text="s9")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("nieudany")
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("brak miejsca na dysku")


def test_powod_pominiecia_nie_przechodzi_na_zakonczona_ponowiona_probe_kroku(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s5", "step_skipped",
               tekst="brak_capability: brak narzedzia git", proba=1)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s5", "step_started", proba=2)
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s5", "step_finished", proba=2)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.locator(WIERSZE)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    pierwsza = page.locator(WIERSZE).filter(has_text="próba 1")
    druga = page.locator(WIERSZE).filter(has_text="próba 2")
    expect(_komorka(pierwsza, KOLUMNA_POWOD)).to_have_text("brak_capability: brak narzedzia git")
    expect(_komorka(druga, KOLUMNA_STAN)).to_have_text("zakończony")
    expect(_komorka(druga, KOLUMNA_POWOD)).to_have_text("—")


def test_dwa_pominiete_kroki_pokazuja_kazdy_swoj_powod_a_nie_cudzy(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped", tekst="powod-alfa")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_skipped", tekst="powod-beta")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.locator(WIERSZE)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(page.locator(WIERSZE).filter(has_text="s2"), KOLUMNA_POWOD)).to_have_text(
        "powod-alfa")
    expect(_komorka(page.locator(WIERSZE).filter(has_text="s3"), KOLUMNA_POWOD)).to_have_text(
        "powod-beta")


def test_pominiety_krok_pokazuje_powod_z_pozniejszego_zdarzenia_pominiecia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped", tekst="powod-pierwszy")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped", tekst="powod-drugi")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s2")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("powod-drugi")


def test_powod_pominietego_kroku_pochodzi_ze_zdarzenia_pominiecia_a_nie_z_innego_zdarzenia_kroku(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped", tekst="powod-alfa")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "uwaga_agenta", tekst="uwaga-agenta")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s2")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("powod-alfa")


def test_pominiety_krok_bez_tekstu_zdarzenia_ma_w_kolumnie_powodu_kreske(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s2", "step_skipped", tekst="")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s2")
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("pominięty", timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("—")


def test_krok_nieudany_bez_tekstu_porazki_ma_w_kolumnie_powodu_kreske(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_failed", tekst="")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s3")
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("nieudany", timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("—")


def test_krok_zakonczony_ma_w_kolumnie_powodu_kreske(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s1", "step_finished")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s1")
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("zakończony", timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("—")


def test_krok_bez_subagenta_ma_w_kolumnie_agenta_kreske(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s6", "step_started")

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s6")
    expect(_komorka(wiersz, KOLUMNA_AGENT)).to_have_text("—", timeout=LIMIT_WYNIKU_MS)


@pytest.mark.parametrize("status,napis", [
    ("pending", "oczekuje"),
    ("waiting", "czeka"),
    ("skipped", "pominięty"),
    ("aborted", "przerwany"),
    ("rejected", "odrzucony"),
], ids=["pending", "waiting", "skipped", "aborted", "rejected"])
def test_stan_runu_w_bazie_ma_na_ekranie_polski_napis_a_nigdy_gotowe(
    zalogowana_strona, adres_kokpitu, status, napis
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku(status)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text(f"Stan runu: {napis}", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    assert "gotowe" not in page.locator("body").inner_text().lower()


def test_krok_nieudany_po_otwarciu_runu_zmienia_wiersz_z_w_toku_na_nieudany_z_powodem(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _otworz_run_ze_strumieniem(page, adres_kokpitu, run_id)
    page.evaluate("window.__bez_przeladowania = true")

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_started")
    wiersz = page.locator(WIERSZE).filter(has_text="s3")
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("w toku", timeout=LIMIT_WYNIKU_MS)

    _zdarzenie(adres_kokpitu, token_runnera, run_id, "s3", "step_failed", tekst="kod wyjscia 3")

    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("nieudany", timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_POWOD)).to_have_text("kod wyjscia 3")
    assert page.evaluate("window.__bez_przeladowania") is True, "strona zostala przeladowana"


def test_przejscie_na_inny_run_z_bledem_pobrania_nie_zostawia_krokow_poprzedniego_runu(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a = _utworz_run_w_toku()
    run_b = str(uuid.uuid4())
    _zdarzenie(adres_kokpitu, token_runnera, run_a, "s1", "step_started", agent_id="sub-a")
    _otworz_run(page, adres_kokpitu, run_a)
    expect(page.locator(WIERSZE)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    # run B powstaje przez "Uruchom" (odpowiedz podstawiona), a jego szczegoly nie daja sie pobrac
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": run_b})
               if r.request.method == "POST" else r.continue_())
    page.route(f"**/api/runs/{run_b}", lambda r: r.fulfill(status=500, body="{}"))
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_enabled(timeout=2 * LIMIT_WYNIKU_MS)

    przycisk.click()

    expect(page.get_by_text("Brak połączenia z kokpitem.")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text(f"run {run_b}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.locator(WIERSZE)).to_have_count(0)


def _podstaw_szczegoly_runu(page, run_id, *, status="running", kroki=()):
    """Odpowiedz GET /api/runs/{id} w kształcie RunSzczegoly (tylko ten adres, nie /events)."""
    szczegoly = {"id": run_id, "workflow": "szkielet", "status": status, "runner_kind": "local",
                 "reason": None, "events": [], "steps": list(kroki)}
    page.route(f"**/api/runs/{run_id}", lambda r: r.fulfill(json=szczegoly))


def test_nieznany_status_runu_z_nowszego_serwera_pokazuje_sie_surowy_a_nie_jako_sukces(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = str(uuid.uuid4())
    _podstaw_szczegoly_runu(page, run_id, status="nowy_status")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("Stan runu: nowy_status", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)


def test_nieznany_stan_kroku_z_nowszego_serwera_pokazuje_sie_surowy_a_nie_jako_sukces(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = str(uuid.uuid4())
    krok = {"step_id": "s1", "agent_id": "", "attempt": 1, "status": "nowy_stan", "reason": None}
    _podstaw_szczegoly_runu(page, run_id, kroki=[krok])

    _otworz_run(page, adres_kokpitu, run_id)

    wiersz = page.locator(WIERSZE).filter(has_text="s1")
    expect(wiersz).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_komorka(wiersz, KOLUMNA_STAN)).to_have_text("nowy_stan")

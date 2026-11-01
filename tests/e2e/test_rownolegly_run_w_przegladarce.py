"""Ostrzezenie o aktywnym runie na tym samym tickecie w prawdziwej przegladarce (HAR-71)."""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie."
KOMUNIKAT_SIEC = "Brak połączenia z kokpitem."
TICKET = "HAR-9001"
LIMIT_UI_MS = 5000
LIMIT_HTTP_S = 10
# Czas na ewentualne zadanie, ktore blednie poszloby po "Anuluj", zanim zajrzymy do bazy.
OKNO_NA_BLEDNE_ZADANIE_MS = 1000


def _id_runu(page) -> str:
    return parse_qs(urlparse(page.url).query)["run"][0]


def _ticket_runu_w_bazie(run_id: str) -> str | None:
    """ticket_id runu z prawdziwej bazy; conftest nie wystawia takiego zapytania."""
    async def _pytaj():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.connect() as conn:
                wynik = await conn.execute(
                    text("SELECT ticket_id FROM run WHERE id = :id"), {"id": run_id}
                )
                return wynik.scalar_one()
        finally:
            await engine.dispose()

    # sync API playwrighta trzyma petle zdarzen w watku testu: osobny watek i petla
    with ThreadPoolExecutor(max_workers=1) as pula:
        return pula.submit(lambda: asyncio.run(_pytaj())).result()


@pytest.fixture
def aktywny_run(runner_online, adres_kokpitu, dane_wlasciciela) -> str:
    """Run na TICKET, ktory trwa przez caly test.

    Domyslne polecenie konczy sie od razu stanem to_accept, a ten nie jest aktywny,
    wiec ostrzezenie nie mialoby czego wykryc.
    """
    email, haslo = dane_wlasciciela
    with httpx.Client(base_url=adres_kokpitu, timeout=LIMIT_HTTP_S) as klient:
        logowanie = klient.post("/api/konto/logowanie", json={"email": email, "haslo": haslo})
        assert logowanie.status_code == 200, f"logowanie nie powiodlo sie: {logowanie.text}"
        odpowiedz = klient.post(
            "/api/runs",
            json={"workflow": "szkielet", "ticket_id": TICKET, "command": ["sleep", "30"]},
            headers={"X-CSRF-Token": logowanie.json()["csrf"]},
        )
    assert odpowiedz.status_code in (200, 201), f"aktywny run nie powstal: {odpowiedz.text}"
    return odpowiedz.json()["run_id"]


def _uruchom_na_tickecie(page, ticket: str = TICKET) -> None:
    page.get_by_label("Ticket").fill(ticket)
    page.get_by_role("button", name="Uruchom", exact=True).click()


def test_drugi_run_na_tym_samym_tickecie_pokazuje_ostrzezenie_z_odnosnikiem_do_aktywnego(
    aktywny_run, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()

    _uruchom_na_tickecie(page)

    alert = page.get_by_role("alert")
    expect(alert).to_be_visible(timeout=LIMIT_UI_MS)
    # href rowny id AKTYWNEGO runu, nie dowolny: odroznia ostrzezenie od bledu ogolnego
    expect(alert.get_by_role("link")).to_have_attribute(
        "href", f"?run={aktywny_run}", timeout=LIMIT_UI_MS
    )
    expect(alert.get_by_role("button", name="Uruchom mimo to")).to_be_visible()
    expect(alert.get_by_role("button", name="Anuluj")).to_be_visible()
    # 409 z aktywnym runem to nie "runner offline"
    expect(page.get_by_text(KOMUNIKAT_OFFLINE)).to_have_count(0)
    assert liczba_runow() == przed, "samo ostrzezenie nie moze utworzyc drugiego runu"


def test_potwierdzenie_ostrzezenia_uruchamia_drugi_run(
    aktywny_run, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()
    _uruchom_na_tickecie(page)
    expect(page.get_by_role("alert")).to_be_visible(timeout=LIMIT_UI_MS)

    page.get_by_role("button", name="Uruchom mimo to").click()

    # Kroki runnera ida po kolei, wiec drugi run nie ma zdarzen w trakcie sleep pierwszego:
    # dowodem jest nowy adres i wiersz w bazie, nie ekran.
    page.wait_for_url(lambda url: "run=" in url and aktywny_run not in url, timeout=LIMIT_UI_MS)
    # Ostrzezenie dotyczylo decyzji, ktora juz zapadla; nie moze zostac na ekranie przy nowym runie
    # (chroni czyszczenie stanu na poczatku wyslijStart, nie handler Anuluj ani onChange).
    expect(page.get_by_role("alert")).to_have_count(0, timeout=LIMIT_UI_MS)
    nowy_run = _id_runu(page)
    assert nowy_run != aktywny_run
    assert liczba_runow() == przed + 1
    assert _ticket_runu_w_bazie(nowy_run) == TICKET, (
        "drugi run musi niesc ticket_id wpisany w pole"
    )


def test_anulowanie_ostrzezenia_nie_tworzy_runu(
    aktywny_run, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()
    _uruchom_na_tickecie(page)
    expect(page.get_by_role("alert")).to_be_visible(timeout=LIMIT_UI_MS)

    page.get_by_role("button", name="Anuluj").click()

    expect(page.get_by_role("alert")).to_have_count(0, timeout=LIMIT_UI_MS)
    page.wait_for_timeout(OKNO_NA_BLEDNE_ZADANIE_MS)
    assert liczba_runow() == przed, "anulowanie nie moze zostawic nowego runu w bazie"


def test_ticket_ze_spacjami_wskazuje_ten_sam_aktywny_run(
    aktywny_run, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()

    # Spacje z obu stron to typowy skutek wklejenia; bez przyciecia backend dostalby inny
    # ticket niz aktywny i utworzylby run bez ostrzezenia.
    _uruchom_na_tickecie(page, f"  {TICKET}  ")

    alert = page.get_by_role("alert")
    expect(alert).to_be_visible(timeout=LIMIT_UI_MS)
    expect(alert.get_by_role("link")).to_have_attribute(
        "href", f"?run={aktywny_run}", timeout=LIMIT_UI_MS
    )
    page.wait_for_timeout(OKNO_NA_BLEDNE_ZADANIE_MS)
    assert liczba_runow() == przed, "ticket ze spacjami nie moze utworzyc drugiego runu"


def test_pole_ticket_z_samymi_spacjami_uruchamia_run_bez_ticketu(
    runner_online, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()

    # Same spacje: tylko przyciecie w warunku decyduje, czy front wysle ticket_id "" (422)
    # czy pominie pole. Spacje wokol wartosci tego nie rozstrzygaja.
    _uruchom_na_tickecie(page, "   ")

    page.wait_for_url(lambda url: "run=" in url, timeout=LIMIT_UI_MS)
    assert liczba_runow() == przed + 1
    assert _ticket_runu_w_bazie(_id_runu(page)) is None, (
        "pole z samymi spacjami to brak ticketu, nie pusty ticket"
    )
    expect(page.get_by_text("Nie udało się uruchomić")).to_have_count(0)


def test_odpowiedz_409_bez_czytelnej_tresci_daje_komunikat_offline(
    zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()

    def _podstaw_409(route):
        # tylko start runu; front robi tez inne zadania
        if route.request.method == "POST":
            route.fulfill(status=409, content_type="text/plain", body="to nie json")
        else:
            route.continue_()

    page.route("**/api/runs", _podstaw_409)

    _uruchom_na_tickecie(page)

    expect(page.get_by_text(KOMUNIKAT_OFFLINE)).to_be_visible(timeout=LIMIT_UI_MS)
    # nieczytelne cialo to nie ostrzezenie i nie blad sieci
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.get_by_text(KOMUNIKAT_SIEC)).to_have_count(0)
    assert liczba_runow() == przed, "odpowiedz podstawiona w przegladarce nie tworzy runu"


def test_zmiana_pola_ticket_chowa_ostrzezenie_o_starej_wartosci(
    aktywny_run, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()
    _uruchom_na_tickecie(page)
    expect(page.get_by_role("alert")).to_be_visible(timeout=LIMIT_UI_MS)

    # Ostrzezenie dotyczy ticketu sprzed zmiany: po edycji pola nie moze zostac na ekranie.
    page.get_by_label("Ticket").fill(f"{TICKET}-inny")

    expect(page.get_by_role("alert")).to_have_count(0, timeout=LIMIT_UI_MS)
    page.wait_for_timeout(OKNO_NA_BLEDNE_ZADANIE_MS)
    assert liczba_runow() == przed, "zmiana pola nie moze utworzyc runu"


def test_start_bez_wpisanego_ticketu_tworzy_run_bez_ticketu(
    runner_online, zalogowana_strona, liczba_runow
):
    page = zalogowana_strona
    przed = liczba_runow()

    # Pola nie dotykamy: tylko wartosc domyslna pola decyduje, czy front wysle jakis ticket_id.
    expect(page.get_by_label("Ticket")).to_have_value("", timeout=LIMIT_UI_MS)
    page.get_by_role("button", name="Uruchom", exact=True).click()

    page.wait_for_url(lambda url: "run=" in url, timeout=LIMIT_UI_MS)
    assert liczba_runow() == przed + 1
    assert _ticket_runu_w_bazie(_id_runu(page)) is None, (
        "start bez wpisanego ticketu nie moze niesc zadnego ticket_id"
    )

"""Logowanie, sesja i zmiana hasla w prawdziwej przegladarce na PostgreSQL (HAR-50).

Sygnalem "uzytkownik jest zalogowany" jest widoczny przycisk "Uruchom" (dotychczasowy
widok runu), sygnalem "ekran logowania" - naglowek "Logowanie". Testy nie klikaja
"Uruchom", wiec nie potrzebuja runnera online.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

NOWE_HASLO = "nowe-haslo-dluzsze-niz-8"


@pytest.fixture
def otworz_kontekst(browser):
    """Fabryka kontekstow przegladarki (osobne ciasteczka); wszystkie zamykane po tescie."""
    otwarte = []

    def _otworz(**opcje):
        kontekst = browser.new_context(**opcje)
        otwarte.append(kontekst)
        return kontekst

    yield _otworz
    for kontekst in otwarte:
        kontekst.close()


def _zaloguj(strona: Page, adres: str, email: str, haslo: str) -> None:
    strona.goto(adres)
    strona.get_by_label("E-mail").fill(email)
    strona.get_by_label("Hasło", exact=True).fill(haslo)
    strona.get_by_role("button", name="Zaloguj", exact=True).click()
    expect(strona.get_by_role("button", name="Uruchom")).to_be_visible()


def _wyloguj(strona: Page) -> None:
    strona.get_by_role("button", name="Wyloguj", exact=True).click()


def _zmien_haslo(strona: Page, stare: str, nowe: str) -> None:
    strona.get_by_label("Obecne hasło").fill(stare)
    strona.get_by_label("Nowe hasło").fill(nowe)
    strona.get_by_role("button", name="Zmień hasło", exact=True).click()
    expect(strona.get_by_role("status")).to_have_text("Hasło zmienione.")


def _ekran_logowania_widoczny(strona: Page) -> None:
    expect(strona.get_by_role("heading", name="Logowanie")).to_be_visible()
    expect(strona.get_by_role("button", name="Uruchom")).to_have_count(0)


def _widok_zalogowanego_widoczny(strona: Page) -> None:
    expect(strona.get_by_role("button", name="Uruchom")).to_be_visible()
    expect(strona.get_by_role("heading", name="Logowanie")).to_have_count(0)


def test_zamkniecie_i_ponowne_otwarcie_przegladarki_zostawia_zalogowanego(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    pierwszy = otworz_kontekst()
    _zaloguj(pierwszy.new_page(), adres_kokpitu, email, haslo)
    zapisany_stan = pierwszy.storage_state()
    pierwszy.close()   # "zamkniecie przegladarki": po nim zostaje tylko zapisany profil

    drugi = otworz_kontekst(storage_state=zapisany_stan)
    strona = drugi.new_page()
    strona.goto(adres_kokpitu)

    _widok_zalogowanego_widoczny(strona)


def test_wylogowanie_pokazuje_ekran_logowania_takze_po_przeladowaniu(
    zalogowana_strona,
):
    strona = zalogowana_strona

    _wyloguj(strona)

    _ekran_logowania_widoczny(strona)
    strona.reload()
    _ekran_logowania_widoczny(strona)


def test_zmiana_hasla_w_jednej_przegladarce_wylogowuje_druga_ale_nie_biezaca(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    pierwsza = otworz_kontekst().new_page()
    druga = otworz_kontekst().new_page()
    _zaloguj(pierwsza, adres_kokpitu, email, haslo)
    _zaloguj(druga, adres_kokpitu, email, haslo)

    _zmien_haslo(pierwsza, haslo, NOWE_HASLO)

    druga.reload()
    _ekran_logowania_widoczny(druga)
    # "wszystkie poza biezaca", nie "wszystkie": sesja, w ktorej zmieniono haslo, zyje
    _widok_zalogowanego_widoczny(pierwsza)
    pierwsza.reload()
    _widok_zalogowanego_widoczny(pierwsza)


def test_ciasteczko_sesji_istnieje_ale_nie_jest_widoczne_z_javascriptu(zalogowana_strona):
    strona = zalogowana_strona

    ciasteczka_z_js = strona.evaluate("document.cookie")
    ciasteczka_przegladarki = {c["name"]: c for c in strona.context.cookies()}

    # bez drugiego warunku test przechodzilby tez wtedy, gdy ciasteczka w ogole nie ma
    assert "cockpit_sesja" in ciasteczka_przegladarki
    assert ciasteczka_przegladarki["cockpit_sesja"]["httpOnly"] is True
    assert "cockpit_sesja" not in ciasteczka_z_js


def test_wylogowanie_ze_wszystkich_urzadzen_konczy_obie_sesje(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    pierwsza = otworz_kontekst().new_page()
    druga = otworz_kontekst().new_page()
    _zaloguj(pierwsza, adres_kokpitu, email, haslo)
    _zaloguj(druga, adres_kokpitu, email, haslo)

    pierwsza.get_by_role(
        "button", name="Wyloguj ze wszystkich urządzeń", exact=True
    ).click()

    _ekran_logowania_widoczny(pierwsza)
    druga.reload()
    _ekran_logowania_widoczny(druga)
    pierwsza.reload()
    _ekran_logowania_widoczny(pierwsza)


def test_sesja_uniewazniona_w_trakcie_pracy_wraca_do_logowania_bez_przeladowania(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    pierwsza = otworz_kontekst().new_page()
    druga = otworz_kontekst().new_page()
    _zaloguj(pierwsza, adres_kokpitu, email, haslo)
    _zaloguj(druga, adres_kokpitu, email, haslo)
    _zmien_haslo(pierwsza, haslo, NOWE_HASLO)

    _wyloguj(druga)   # bez przeladowania: serwer odpowie 401, bo sesja juz nie istnieje

    _ekran_logowania_widoczny(druga)


def test_wylogowanie_udaje_sie_mimo_ze_druga_karta_uniewaznila_token_csrf(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    kontekst = otworz_kontekst()
    pierwsza = kontekst.new_page()
    _zaloguj(pierwsza, adres_kokpitu, email, haslo)
    druga = kontekst.new_page()
    druga.goto(adres_kokpitu)
    # druga karta pokazala widok zalogowanego, czyli jej GET /api/konto/ja juz wrocil
    # i uniewaznil token pierwszej karty
    _widok_zalogowanego_widoczny(druga)

    _wyloguj(pierwsza)   # bez przeladowania: pierwsze zadanie dostaje 403 CSRF

    _ekran_logowania_widoczny(pierwsza)
    pierwsza.reload()
    _ekran_logowania_widoczny(pierwsza)


def test_rejestracja_nowego_konta_formularzem_konczy_sie_widokiem_zalogowanego(
    adres_kokpitu, otworz_kontekst
):
    strona = otworz_kontekst().new_page()
    strona.goto(adres_kokpitu)

    strona.get_by_role("button", name="Załóż konto", exact=True).click()
    expect(strona.get_by_role("heading", name="Rejestracja")).to_be_visible()
    strona.get_by_label("E-mail").fill(f"nowy-{uuid.uuid4().hex[:10]}@example.com")
    strona.get_by_label("Hasło", exact=True).fill("haslo-nowego-konta-1")
    strona.get_by_role("button", name="Zarejestruj", exact=True).click()

    _widok_zalogowanego_widoczny(strona)
    assert "cockpit_sesja" in {c["name"] for c in strona.context.cookies()}


def test_bledne_haslo_przy_logowaniu_pokazuje_komunikat_i_zostawia_ekran_logowania(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    kontekst = otworz_kontekst()
    strona = kontekst.new_page()
    strona.goto(adres_kokpitu)

    strona.get_by_label("E-mail").fill(email)
    strona.get_by_label("Hasło", exact=True).fill(haslo + "-bledne")   # jedna zla proba
    strona.get_by_role("button", name="Zaloguj", exact=True).click()

    expect(strona.get_by_role("alert")).to_contain_text("nieprawidłowy adres lub hasło")
    _ekran_logowania_widoczny(strona)
    assert "cockpit_sesja" not in {c["name"] for c in kontekst.cookies()}


def test_zle_obecne_haslo_przy_zmianie_pokazuje_blad_ale_nie_konczy_sesji(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    strona = otworz_kontekst().new_page()
    _zaloguj(strona, adres_kokpitu, email, haslo)

    strona.get_by_label("Obecne hasło").fill(haslo + "-bledne")   # jedna zla proba
    strona.get_by_label("Nowe hasło").fill(NOWE_HASLO)
    strona.get_by_role("button", name="Zmień hasło", exact=True).click()

    expect(strona.get_by_role("alert")).to_contain_text("nieprawidłowe hasło")
    expect(strona.get_by_label("Obecne hasło")).to_be_visible()   # formularz zostaje na ekranie
    _widok_zalogowanego_widoczny(strona)
    strona.reload()
    _widok_zalogowanego_widoczny(strona)


def test_wygasla_sesja_w_formularzu_zmiany_hasla_wraca_do_logowania_zamiast_bledu_formularza(
    adres_kokpitu, dane_wlasciciela, otworz_kontekst
):
    email, haslo = dane_wlasciciela
    pierwsza = otworz_kontekst().new_page()
    druga = otworz_kontekst().new_page()
    _zaloguj(pierwsza, adres_kokpitu, email, haslo)
    _zaloguj(druga, adres_kokpitu, email, haslo)
    _zmien_haslo(pierwsza, haslo, NOWE_HASLO)   # unieważnia sesję drugiej karty

    # bez przeladowania: poprawne obecne hasło, ale serwer odpowie 401 (sesja nie istnieje)
    druga.get_by_label("Obecne hasło").fill(haslo)
    druga.get_by_label("Nowe hasło").fill(NOWE_HASLO + "-inne")
    druga.get_by_role("button", name="Zmień hasło", exact=True).click()

    _ekran_logowania_widoczny(druga)
    expect(druga.get_by_label("Obecne hasło")).to_have_count(0)

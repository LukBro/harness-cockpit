"""Trasy HTTP kont: rejestracja, logowanie, wylogowanie, ciasteczko sesji i CSRF (HAR-48).

Testy przed implementacja: `cockpit.api.konta` jeszcze nie istnieje. Produkcyjna aplikacja
`cockpit.api.app.app`, baza SQLite w tmp_path podpieta przez `dependency_overrides`.
Kazda asercja o uniewaznieniu sesji czyta wiersz `Sesja` w NOWEJ sesji bazy.
"""
from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db.models import Base, Sesja, Tenant, User
from cockpit.db.session import get_session
from cockpit.services import sesje
from cockpit.settings import Settings, settings

HASLO = "Sekret-Hasl0-Alfa"
ZLE_HASLO = "Zle-Haslo-Gamma"
NOWE_HASLO = "Nowe-Hasl0-Delta"
JAN = "jan@przyklad.pl"
EWA = "ewa@przyklad.pl"
NIKT = "nikt@przyklad.pl"
CIASTECZKO = "cockpit_sesja"
KOMUNIKAT_LOGOWANIA = "nieprawidłowy adres lub hasło"


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'konto_http.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)

    async def _sesja_testowa():
        async with m() as s:
            yield s

    app.dependency_overrides[get_session] = _sesja_testowa
    yield m
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


@pytest.fixture
def nowy_klient(maker):
    """Fabryka klientow; kazdy ma wlasny sloik ciasteczek (osobna przegladarka)."""

    def _fabryka() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://test")

    return _fabryka


@pytest.fixture
def klient(nowy_klient):
    return nowy_klient()


@pytest.fixture(autouse=True)
def _ustawienia(monkeypatch):
    monkeypatch.setattr(settings, "ciasteczko_secure", True)


# --- pomocnicy ---------------------------------------------------------------------------------


def _set_cookie(odp: httpx.Response) -> str:
    naglowki = [h for h in odp.headers.get_list("set-cookie") if h.startswith(f"{CIASTECZKO}=")]
    assert len(naglowki) == 1, odp.headers.get_list("set-cookie")
    return naglowki[0]


def _atrybuty(naglowek: str) -> dict[str, str]:
    """Atrybuty Set-Cookie z kluczami malymi literami; flagi bez wartosci maja ''."""
    czesci = [c.strip() for c in naglowek.split(";")]
    wynik: dict[str, str] = {}
    for c in czesci[1:]:
        k, _, v = c.partition("=")
        wynik[k.strip().lower()] = v.strip().strip('"')
    return wynik


def _id_sesji(odp: httpx.Response) -> str:
    wartosc = _set_cookie(odp).split(";")[0].split("=", 1)[1]
    assert wartosc
    return wartosc


async def _rejestruj(k, email=JAN, haslo=HASLO, zespol=None):
    cialo = {"email": email, "haslo": haslo}
    if zespol is not None:
        cialo["nazwa_zespolu"] = zespol
    return await k.post("/api/konto/rejestracja", json=cialo)


async def _zaloguj(k, email=JAN, haslo=HASLO):
    return await k.post("/api/konto/logowanie", json={"email": email, "haslo": haslo})


async def _zaloz_sesje(k, email=JAN, haslo=HASLO, zespol=None) -> tuple[str, str]:
    """Rejestruje konto i zwraca (identyfikator sesji, token csrf) tego klienta."""
    odp = await _rejestruj(k, email, haslo, zespol)
    assert odp.status_code == 201, odp.text
    return _id_sesji(odp), odp.json()["csrf"]


async def _sesje_uzytkownika(maker, email) -> list[Sesja]:
    async with maker() as s:
        return list(
            (
                await s.execute(
                    select(Sesja).join(User, User.id == Sesja.user_id).where(User.email == email)
                )
            ).scalars()
        )


async def _sesja_po_id(maker, identyfikator) -> Sesja:
    async with maker() as s:
        return (
            await s.execute(select(Sesja).where(Sesja.skrot == sesje.skrot(identyfikator)))
        ).scalar_one()


async def _wazna(maker, identyfikator) -> bool:
    return (await _sesja_po_id(maker, identyfikator)).uniewazniona_at is None


# --- ciasteczko sesji ----------------------------------------------------------------------------


async def test_ciasteczko_sesji_jest_httponly(klient):
    odp = await _rejestruj(klient)
    assert "httponly" in _atrybuty(_set_cookie(odp))


async def test_ciasteczko_sesji_ma_samesite_lax(klient):
    odp = await _rejestruj(klient)
    assert _atrybuty(_set_cookie(odp))["samesite"].lower() == "lax"


async def test_ciasteczko_sesji_dziala_na_calej_witrynie(klient):
    odp = await _rejestruj(klient)
    assert _atrybuty(_set_cookie(odp))["path"] == "/"


async def test_ciasteczko_sesji_trwa_tyle_dni_ile_wazna_jest_sesja(klient, monkeypatch):
    monkeypatch.setattr(settings, "sesja_wazna_dni", 7)
    odp = await _rejestruj(klient)
    assert _atrybuty(_set_cookie(odp))["max-age"] == str(7 * 86400)


async def test_ciasteczko_logowania_tez_ma_termin_wazny_z_ustawien(klient, monkeypatch):
    await _rejestruj(klient)
    monkeypatch.setattr(settings, "sesja_wazna_dni", 3)
    odp = await _zaloguj(klient)
    assert odp.status_code == 200
    assert _atrybuty(_set_cookie(odp))["max-age"] == str(3 * 86400)


async def test_ciasteczko_ma_secure_poza_lokalnym_developmentem(klient, monkeypatch):
    monkeypatch.setattr(settings, "ciasteczko_secure", True)
    odp = await _rejestruj(klient)
    assert "secure" in _atrybuty(_set_cookie(odp))


async def test_ciasteczko_nie_ma_secure_w_lokalnym_developmencie(klient, monkeypatch):
    monkeypatch.setattr(settings, "ciasteczko_secure", False)
    odp = await _rejestruj(klient)
    atrybuty = _atrybuty(_set_cookie(odp))
    assert "secure" not in atrybuty
    # pozostale flagi zostaja: wylaczone jest tylko Secure
    assert "httponly" in atrybuty
    assert atrybuty["samesite"].lower() == "lax"


def test_ciasteczko_secure_jest_domyslnie_wlaczone(monkeypatch):
    monkeypatch.delenv("COCKPIT_CIASTECZKO_SECURE", raising=False)
    assert Settings(_env_file=None).ciasteczko_secure is True


def test_zmienna_srodowiskowa_wylacza_secure_ciasteczka(monkeypatch):
    monkeypatch.setenv("COCKPIT_CIASTECZKO_SECURE", "false")
    assert Settings(_env_file=None).ciasteczko_secure is False


async def test_ciasteczko_secure_czytane_w_chwili_zadania(klient, monkeypatch):
    monkeypatch.setattr(settings, "ciasteczko_secure", False)
    pierwsze = await _rejestruj(klient, JAN)
    monkeypatch.setattr(settings, "ciasteczko_secure", True)
    drugie = await _rejestruj(klient, EWA)
    assert "secure" not in _atrybuty(_set_cookie(pierwsze))
    assert "secure" in _atrybuty(_set_cookie(drugie))


async def test_wartosc_ciasteczka_to_identyfikator_sesji_a_w_bazie_lezy_tylko_skrot(
    klient, maker
):
    identyfikator, csrf = await _zaloz_sesje(klient)
    (sesja,) = await _sesje_uzytkownika(maker, JAN)
    assert sesja.skrot == sesje.skrot(identyfikator)
    assert sesja.skrot != identyfikator
    assert sesja.csrf_skrot == sesje.skrot(csrf)


# --- rejestracja -------------------------------------------------------------------------------


async def test_rejestracja_zwraca_201_jawny_token_csrf_i_zaklada_sesje_w_bazie(klient, maker):
    odp = await _rejestruj(klient)
    assert odp.status_code == 201
    assert set(odp.json()) == {"csrf"}
    assert odp.json()["csrf"]
    (sesja,) = await _sesje_uzytkownika(maker, JAN)
    assert sesja.uniewazniona_at is None
    assert sesja.csrf_skrot == sesje.skrot(odp.json()["csrf"])


async def test_po_rejestracji_ciasteczko_daje_dzialajaca_sesje(klient):
    await _zaloz_sesje(klient)
    odp = await klient.get("/api/konto/ja")
    assert odp.status_code == 200
    assert odp.json()["email"] == JAN


async def test_rejestracja_zajetego_adresu_daje_409_i_nie_ustawia_ciasteczka(
    klient, nowy_klient, maker
):
    await _zaloz_sesje(klient)
    drugi = nowy_klient()
    odp = await _rejestruj(drugi, JAN, "Inne-Hasl0-Omega")
    assert odp.status_code == 409
    assert "set-cookie" not in odp.headers
    assert len(await _sesje_uzytkownika(maker, JAN)) == 1


async def test_rejestracja_z_za_krotkim_haslem_daje_409_i_nie_zaklada_konta(klient, maker):
    odp = await _rejestruj(klient, JAN, "krotkie1")  # 8 znakow: granica przechodzi
    assert odp.status_code == 201
    odp = await _rejestruj(klient, EWA, "krotki7")  # 7 znakow
    assert odp.status_code == 409
    assert "set-cookie" not in odp.headers
    async with maker() as s:
        adresy = set((await s.execute(select(User.email))).scalars())
    assert adresy == {JAN}


async def test_rejestracja_z_nazwa_zespolu_zaklada_zespol_o_tej_nazwie(klient, maker):
    await _rejestruj(klient, JAN, HASLO, "Zespół Jana")
    async with maker() as s:
        nazwy = set((await s.execute(select(Tenant.name))).scalars())
    assert nazwy == {"Zespół Jana"}


# --- logowanie ---------------------------------------------------------------------------------


async def test_logowanie_poprawnym_haslem_daje_200_csrf_i_nowa_sesje(klient, nowy_klient, maker):
    await _zaloz_sesje(klient)
    drugi = nowy_klient()
    odp = await _zaloguj(drugi)
    assert odp.status_code == 200
    assert odp.json()["csrf"]
    assert len(await _sesje_uzytkownika(maker, JAN)) == 2
    assert (await drugi.get("/api/konto/ja")).status_code == 200


async def test_zle_haslo_i_nieistniejacy_adres_daja_identyczna_odpowiedz(klient, nowy_klient):
    await _zaloz_sesje(klient)
    zle_haslo = await _zaloguj(nowy_klient(), JAN, ZLE_HASLO)
    brak_konta = await _zaloguj(nowy_klient(), NIKT, ZLE_HASLO)
    assert zle_haslo.status_code == brak_konta.status_code == 401
    assert zle_haslo.json() == brak_konta.json() == {"detail": KOMUNIKAT_LOGOWANIA}
    assert "set-cookie" not in zle_haslo.headers
    assert "set-cookie" not in brak_konta.headers


async def test_nieudane_logowanie_nie_zaklada_sesji_w_bazie(klient, nowy_klient, maker):
    await _zaloz_sesje(klient)
    await _zaloguj(nowy_klient(), JAN, ZLE_HASLO)
    assert len(await _sesje_uzytkownika(maker, JAN)) == 1


async def test_po_pieciu_nieudanych_probach_szosta_dostaje_429_takze_z_poprawnym_haslem(
    klient, nowy_klient, maker
):
    await _zaloz_sesje(klient)
    for _ in range(5):
        odp = await _zaloguj(nowy_klient(), JAN, ZLE_HASLO)
        assert odp.status_code == 401
    odp = await _zaloguj(nowy_klient(), JAN, HASLO)
    assert odp.status_code == 429
    assert "set-cookie" not in odp.headers
    assert len(await _sesje_uzytkownika(maker, JAN)) == 1


async def test_piata_nieudana_proba_to_jeszcze_401_a_nie_429(klient, nowy_klient):
    await _zaloz_sesje(klient)
    for _ in range(4):
        await _zaloguj(nowy_klient(), JAN, ZLE_HASLO)
    odp = await _zaloguj(nowy_klient(), JAN, ZLE_HASLO)
    assert odp.status_code == 401


async def test_blokada_dotyczy_tez_nieistniejacego_adresu_tak_samo(nowy_klient):
    for _ in range(5):
        odp = await _zaloguj(nowy_klient(), NIKT, ZLE_HASLO)
        assert odp.status_code == 401
    odp = await _zaloguj(nowy_klient(), NIKT, ZLE_HASLO)
    assert odp.status_code == 429


async def test_blokada_jednego_adresu_nie_blokuje_innego(klient, nowy_klient):
    await _zaloz_sesje(klient, JAN)
    await _zaloz_sesje(nowy_klient(), EWA)
    for _ in range(5):
        await _zaloguj(nowy_klient(), JAN, ZLE_HASLO)
    assert (await _zaloguj(nowy_klient(), JAN, HASLO)).status_code == 429
    assert (await _zaloguj(nowy_klient(), EWA, HASLO)).status_code == 200


# --- profil /ja ----------------------------------------------------------------------------------


async def test_profil_zwraca_adres_zespol_role_i_nowy_token_csrf(klient):
    _, csrf = await _zaloz_sesje(klient, JAN, HASLO, "Zespół Jana")
    odp = await klient.get("/api/konto/ja")
    assert odp.status_code == 200
    dane = odp.json()
    assert set(dane) == {"email", "zespol", "rola", "csrf"}
    assert dane["email"] == JAN
    assert dane["zespol"] == "Zespół Jana"
    assert dane["rola"] == "owner"
    assert dane["csrf"] and dane["csrf"] != csrf


async def test_profil_przy_dwoch_zespolach_zwraca_zespol_z_sesji_a_nie_cudzy(
    klient, nowy_klient
):
    # Ewa pierwsza: jej zespol ma najwczesniejszy wiersz w bazie
    kolega = nowy_klient()
    await _zaloz_sesje(kolega, EWA, HASLO, "Zespół Ewy")
    await _zaloz_sesje(klient, JAN, HASLO, "Zespół Jana")
    assert (await klient.get("/api/konto/ja")).json()["zespol"] == "Zespół Jana"
    assert (await kolega.get("/api/konto/ja")).json()["zespol"] == "Zespół Ewy"
    assert (await klient.get("/api/konto/ja")).json()["email"] == JAN


async def test_profil_nie_wymaga_tokenu_csrf(klient):
    await _zaloz_sesje(klient)
    odp = await klient.get("/api/konto/ja")  # GET bez naglowka X-CSRF-Token
    assert odp.status_code == 200


async def test_profil_odnawia_csrf_stary_token_przestaje_dzialac(klient, maker):
    identyfikator, stary = await _zaloz_sesje(klient)
    nowy = (await klient.get("/api/konto/ja")).json()["csrf"]
    sesja = await _sesja_po_id(maker, identyfikator)
    assert sesja.csrf_skrot == sesje.skrot(nowy)

    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": stary})
    assert odp.status_code == 403
    assert await _wazna(maker, identyfikator)

    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": nowy})
    assert odp.status_code == 204


async def test_profil_bez_ciasteczka_daje_401(klient):
    odp = await klient.get("/api/konto/ja")
    assert odp.status_code == 401
    assert odp.json() == {"detail": sesje.KOMUNIKAT_SESJI}


async def test_profil_z_wylogowana_sesja_daje_401(klient):
    _, csrf = await _zaloz_sesje(klient)
    cookies = dict(klient.cookies)
    assert (
        await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    ).status_code == 204
    klient.cookies.clear()
    klient.cookies.set(CIASTECZKO, cookies[CIASTECZKO])  # przegladarka nadal je trzyma
    assert (await klient.get("/api/konto/ja")).status_code == 401


async def test_profil_z_nieznanym_identyfikatorem_sesji_daje_401(klient):
    klient.cookies.set(CIASTECZKO, "wymyslony-identyfikator")
    assert (await klient.get("/api/konto/ja")).status_code == 401


# --- CSRF ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("sciezka", ["wyloguj", "wyloguj-wszedzie"])
async def test_operacja_zmieniajaca_stan_bez_tokenu_csrf_daje_403_a_sesja_zostaje_wazna(
    klient, maker, sciezka
):
    identyfikator, _ = await _zaloz_sesje(klient)
    odp = await klient.post(f"/api/konto/{sciezka}")
    assert odp.status_code == 403
    assert await _wazna(maker, identyfikator)
    assert (await klient.get("/api/konto/ja")).status_code == 200


@pytest.mark.parametrize("sciezka", ["wyloguj", "wyloguj-wszedzie"])
async def test_operacja_ze_zlym_tokenem_csrf_daje_403_a_sesja_zostaje_wazna(
    klient, maker, sciezka
):
    identyfikator, _ = await _zaloz_sesje(klient)
    odp = await klient.post(f"/api/konto/{sciezka}", headers={"X-CSRF-Token": "zly-token"})
    assert odp.status_code == 403
    assert await _wazna(maker, identyfikator)


async def test_zmiana_hasla_bez_tokenu_csrf_daje_403_i_nie_zmienia_hasla(
    klient, nowy_klient, maker
):
    identyfikator, _ = await _zaloz_sesje(klient)
    odp = await klient.post("/api/konto/haslo", json={"stare": HASLO, "nowe": NOWE_HASLO})
    assert odp.status_code == 403
    assert await _wazna(maker, identyfikator)
    assert (await _zaloguj(nowy_klient(), JAN, HASLO)).status_code == 200
    assert (await _zaloguj(nowy_klient(), JAN, NOWE_HASLO)).status_code == 401


async def test_token_csrf_innej_sesji_tego_samego_uzytkownika_jest_odrzucony(
    klient, nowy_klient, maker
):
    id_a, _ = await _zaloz_sesje(klient)
    drugi = nowy_klient()
    odp_b = await _zaloguj(drugi)
    csrf_b = odp_b.json()["csrf"]
    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf_b})
    assert odp.status_code == 403
    assert await _wazna(maker, id_a)


async def test_bez_sesji_operacja_zmieniajaca_stan_daje_401_przed_sprawdzeniem_csrf(klient):
    bez_naglowka = await klient.post("/api/konto/wyloguj")
    ze_zlym = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": "x"})
    assert bez_naglowka.status_code == 401
    assert ze_zlym.status_code == 401


async def test_operacja_z_wylogowana_sesja_i_poprawnym_tokenem_daje_401(klient):
    _, csrf = await _zaloz_sesje(klient)
    cookies = dict(klient.cookies)
    await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    klient.cookies.clear()
    klient.cookies.set(CIASTECZKO, cookies[CIASTECZKO])
    odp = await klient.post("/api/konto/wyloguj-wszedzie", headers={"X-CSRF-Token": csrf})
    assert odp.status_code == 401


# --- wylogowanie ---------------------------------------------------------------------------------


async def test_wylogowanie_uniewaznia_biezaca_sesje_w_bazie_i_kasuje_ciasteczko(klient, maker):
    identyfikator, csrf = await _zaloz_sesje(klient)
    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    assert odp.status_code == 204
    assert not odp.content
    assert (await _sesja_po_id(maker, identyfikator)).uniewazniona_at is not None
    atrybuty = _atrybuty(_set_cookie(odp))
    assert atrybuty["max-age"] == "0"
    assert _set_cookie(odp).split(";")[0].rstrip() in (f"{CIASTECZKO}=", f'{CIASTECZKO}=""')


async def test_skasowane_ciasteczko_ma_te_same_flagi_co_ustawione(klient):
    _, csrf = await _zaloz_sesje(klient)
    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    atrybuty = _atrybuty(_set_cookie(odp))
    assert atrybuty["path"] == "/"
    assert "httponly" in atrybuty
    assert "secure" in atrybuty
    assert atrybuty["samesite"].lower() == "lax"


async def test_skasowane_ciasteczko_w_developmencie_nie_ma_secure(klient, monkeypatch):
    _, csrf = await _zaloz_sesje(klient)
    monkeypatch.setattr(settings, "ciasteczko_secure", False)
    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    atrybuty = _atrybuty(_set_cookie(odp))
    assert "secure" not in atrybuty
    assert atrybuty["max-age"] == "0"


async def test_wylogowanie_nie_rusza_drugiej_sesji_tego_samego_uzytkownika(
    klient, nowy_klient, maker
):
    id_a, csrf_a = await _zaloz_sesje(klient)
    drugi = nowy_klient()
    odp_b = await _zaloguj(drugi)
    id_b = _id_sesji(odp_b)
    await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf_a})
    assert not await _wazna(maker, id_a)
    assert await _wazna(maker, id_b)
    assert (await drugi.get("/api/konto/ja")).status_code == 200


async def test_po_wylogowaniu_ta_sama_sesja_nie_daje_dostepu(klient):
    _, csrf = await _zaloz_sesje(klient)
    cookies = dict(klient.cookies)
    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    assert odp.status_code == 204
    klient.cookies.clear()
    klient.cookies.set(CIASTECZKO, cookies[CIASTECZKO])
    assert (await klient.get("/api/konto/ja")).status_code == 401


async def test_wyloguj_wszedzie_uniewaznia_wszystkie_sesje_uzytkownika_w_bazie(
    klient, nowy_klient, maker
):
    id_a, csrf_a = await _zaloz_sesje(klient)
    drugi = nowy_klient()
    id_b = _id_sesji(await _zaloguj(drugi))
    trzeci = nowy_klient()
    id_c = _id_sesji(await _zaloguj(trzeci))
    odp = await klient.post("/api/konto/wyloguj-wszedzie", headers={"X-CSRF-Token": csrf_a})
    assert odp.status_code == 204
    assert not odp.content
    for identyfikator in (id_a, id_b, id_c):
        assert not await _wazna(maker, identyfikator)
    assert (await drugi.get("/api/konto/ja")).status_code == 401
    assert _atrybuty(_set_cookie(odp))["max-age"] == "0"


async def test_wyloguj_wszedzie_nie_rusza_sesji_innego_uzytkownika(klient, nowy_klient, maker):
    id_jan, csrf_jan = await _zaloz_sesje(klient, JAN)
    ewa = nowy_klient()
    id_ewa, _ = await _zaloz_sesje(ewa, EWA)
    await klient.post("/api/konto/wyloguj-wszedzie", headers={"X-CSRF-Token": csrf_jan})
    assert not await _wazna(maker, id_jan)
    assert await _wazna(maker, id_ewa)
    assert (await ewa.get("/api/konto/ja")).status_code == 200


async def test_wyloguj_wszedzie_kasuje_ciasteczko_z_flagami(klient):
    _, csrf = await _zaloz_sesje(klient)
    odp = await klient.post("/api/konto/wyloguj-wszedzie", headers={"X-CSRF-Token": csrf})
    atrybuty = _atrybuty(_set_cookie(odp))
    assert atrybuty["path"] == "/"
    assert "httponly" in atrybuty and "secure" in atrybuty
    assert atrybuty["samesite"].lower() == "lax"


# --- zmiana hasla --------------------------------------------------------------------------------


async def test_zmiana_hasla_uniewaznia_pozostale_sesje_a_biezaca_zostaje_wazna(
    klient, nowy_klient, maker
):
    id_a, csrf_a = await _zaloz_sesje(klient)
    drugi = nowy_klient()
    id_b = _id_sesji(await _zaloguj(drugi))
    odp = await klient.post(
        "/api/konto/haslo",
        json={"stare": HASLO, "nowe": NOWE_HASLO},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert odp.status_code == 204
    assert await _wazna(maker, id_a)
    assert not await _wazna(maker, id_b)
    assert (await klient.get("/api/konto/ja")).status_code == 200
    assert (await drugi.get("/api/konto/ja")).status_code == 401


async def test_po_zmianie_hasla_logowanie_dziala_tylko_z_nowym_haslem(klient, nowy_klient):
    _, csrf = await _zaloz_sesje(klient)
    await klient.post(
        "/api/konto/haslo",
        json={"stare": HASLO, "nowe": NOWE_HASLO},
        headers={"X-CSRF-Token": csrf},
    )
    assert (await _zaloguj(nowy_klient(), JAN, NOWE_HASLO)).status_code == 200
    assert (await _zaloguj(nowy_klient(), JAN, HASLO)).status_code == 401


async def test_zmiana_hasla_ze_zlym_starym_haslem_daje_422_i_nic_nie_uniewaznia(
    klient, nowy_klient, maker
):
    id_a, csrf_a = await _zaloz_sesje(klient)
    drugi = nowy_klient()
    id_b = _id_sesji(await _zaloguj(drugi))
    odp = await klient.post(
        "/api/konto/haslo",
        json={"stare": ZLE_HASLO, "nowe": NOWE_HASLO},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert odp.status_code == 422
    assert odp.json() == {"detail": "nieprawidłowe hasło"}
    assert await _wazna(maker, id_a)
    assert await _wazna(maker, id_b)
    assert (await _zaloguj(nowy_klient(), JAN, HASLO)).status_code == 200


async def test_zmiana_hasla_z_wylogowana_sesja_i_poprawnym_tokenem_daje_401_i_nie_zmienia_hasla(
    klient, nowy_klient
):
    _, csrf = await _zaloz_sesje(klient)
    cookies = dict(klient.cookies)
    await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": csrf})
    klient.cookies.clear()
    klient.cookies.set(CIASTECZKO, cookies[CIASTECZKO])
    odp = await klient.post(
        "/api/konto/haslo",
        json={"stare": HASLO, "nowe": NOWE_HASLO},
        headers={"X-CSRF-Token": csrf},
    )
    assert odp.status_code == 401
    assert odp.json() == {"detail": "sesja wygasła, zaloguj się"}
    assert (await _zaloguj(nowy_klient(), JAN, HASLO)).status_code == 200
    assert (await _zaloguj(nowy_klient(), JAN, NOWE_HASLO)).status_code == 401


async def test_zmiana_hasla_bez_sesji_daje_401(klient):
    odp = await klient.post(
        "/api/konto/haslo",
        json={"stare": HASLO, "nowe": NOWE_HASLO},
        headers={"X-CSRF-Token": "x"},
    )
    assert odp.status_code == 401


async def test_zmiana_hasla_na_za_krotkie_daje_409_i_nie_uniewaznia_sesji(
    klient, nowy_klient, maker
):
    _, csrf = await _zaloz_sesje(klient)
    drugi = nowy_klient()
    id_b = _id_sesji(await _zaloguj(drugi))
    odp = await klient.post(
        "/api/konto/haslo",
        json={"stare": HASLO, "nowe": "krotkie"},
        headers={"X-CSRF-Token": csrf},
    )
    assert odp.status_code == 409
    assert await _wazna(maker, id_b)


# --- modele wejscia ------------------------------------------------------------------------------


def test_repr_modeli_wejscia_nie_zawiera_hasel():
    from pydantic import SecretStr

    from cockpit.api.konta import DaneLogowania, DaneRejestracji, ZmianaHasla

    modele = [
        DaneRejestracji(email=JAN, haslo=HASLO),
        DaneLogowania(email=JAN, haslo=HASLO),
        ZmianaHasla(stare=HASLO, nowe=NOWE_HASLO),
    ]
    for model in modele:
        tekst = repr(model) + str(model)
        assert HASLO not in tekst
        assert NOWE_HASLO not in tekst
    assert isinstance(modele[0].haslo, SecretStr)
    assert isinstance(modele[1].haslo, SecretStr)
    assert isinstance(modele[2].stare, SecretStr)
    assert isinstance(modele[2].nowe, SecretStr)


@pytest.mark.parametrize(
    ("sciezka", "cialo"),
    [
        ("rejestracja", {"haslo": HASLO}),
        ("logowanie", {"haslo": ZLE_HASLO}),
    ],
)
async def test_zbyt_dlugi_adres_email_daje_422(klient, sciezka, cialo):
    odp = await klient.post(
        f"/api/konto/{sciezka}", json={"email": "a" * 310 + "@przyklad.pl", **cialo}
    )
    assert odp.status_code == 422


async def test_adres_email_o_dlugosci_320_znakow_nie_jest_odrzucany_jako_za_dlugi(klient):
    email = "a" * (320 - len("@przyklad.pl")) + "@przyklad.pl"
    assert len(email) == 320
    odp = await _zaloguj(klient, email, ZLE_HASLO)
    assert odp.status_code == 401  # walidacja przeszla, konto nie istnieje


# --- komunikaty bledow i usluga profilu (mutanty HAR-48) -----------------------------------------


async def test_brak_naglowka_csrf_daje_403_z_komunikatem_o_braku_tokenu(klient):
    await _zaloz_sesje(klient)
    odp = await klient.post("/api/konto/wyloguj")
    assert odp.status_code == 403
    assert odp.json() == {"detail": "brak tokenu CSRF"}


async def test_zly_token_csrf_daje_403_z_komunikatem_o_nieprawidlowym_tokenie(klient):
    await _zaloz_sesje(klient)
    odp = await klient.post("/api/konto/wyloguj", headers={"X-CSRF-Token": "zly-token"})
    assert odp.status_code == 403
    assert odp.json() == {"detail": "nieprawidłowy token CSRF"}


async def test_puste_ciasteczko_sesji_daje_401_z_komunikatem_o_wygaslej_sesji(klient):
    await _zaloz_sesje(klient)
    klient.cookies.clear()
    klient.cookies.set(CIASTECZKO, "")
    odp = await klient.get("/api/konto/ja")
    assert odp.status_code == 401
    assert odp.json() == {"detail": sesje.KOMUNIKAT_SESJI}


async def _user_i_zespol(maker, email=JAN, nazwa="Zespół Jana") -> tuple[str, str]:
    async with maker() as s:
        user = User(email=email, haslo_skrot="skrot-nieistotny")
        tenant = Tenant(name=nazwa)
        s.add_all([user, tenant])
        await s.commit()
        return user.id, tenant.id


async def test_profil_uzytkownika_usunietego_z_bazy_to_brak_dostepu_z_komunikatem_sesji(maker):
    from cockpit.db.models import RolaWZespole
    from cockpit.services import konta
    from cockpit.services.dostep import Kontekst
    from cockpit.services.errors import BrakDostepu

    _, tenant_id = await _user_i_zespol(maker)
    kontekst = Kontekst(user_id="nie-ma-takiego", tenant_id=tenant_id, rola=RolaWZespole.owner)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as wyjatek:
            await konta.profil(s, kontekst)
    assert str(wyjatek.value) == sesje.KOMUNIKAT_SESJI


async def test_profil_zespolu_usunietego_z_bazy_to_brak_dostepu_z_komunikatem_sesji(maker):
    from cockpit.db.models import RolaWZespole
    from cockpit.services import konta
    from cockpit.services.dostep import Kontekst
    from cockpit.services.errors import BrakDostepu

    user_id, _ = await _user_i_zespol(maker)
    kontekst = Kontekst(user_id=user_id, tenant_id="nie-ma-takiego", rola=RolaWZespole.owner)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as wyjatek:
            await konta.profil(s, kontekst)
    assert str(wyjatek.value) == sesje.KOMUNIKAT_SESJI


async def test_zmiana_hasla_nieistniejacego_uzytkownika_to_brak_dostepu_z_komunikatem_sesji(maker):
    from cockpit.services import konta
    from cockpit.services.errors import BrakDostepu

    async with maker() as s:
        with pytest.raises(BrakDostepu) as wyjatek:
            await konta.zmien_haslo_uzytkownika(s, "nie-ma-takiego", HASLO, NOWE_HASLO, "sesja-x")
    assert str(wyjatek.value) == sesje.KOMUNIKAT_SESJI


# --- limity wejscia, metoda HEAD i brak adresu klienta (mutanty HAR-48, iteracja 2) ---------------

HASLO_256 = "H" * 256
HASLO_257 = "H" * 257


async def _liczba_zespolow_i_kont(maker) -> tuple[int, int]:
    async with maker() as s:
        zespoly = len(list((await s.execute(select(Tenant))).scalars()))
        konta = len(list((await s.execute(select(User))).scalars()))
    return zespoly, konta


async def test_rejestracja_z_haslem_dluzszym_niz_256_znakow_daje_422_i_nie_tworzy_konta(
    klient, maker
):
    odp = await _rejestruj(klient, haslo=HASLO_257)
    assert odp.status_code == 422
    assert odp.json()["detail"][0]["loc"] == ["body", "haslo"]
    assert await _liczba_zespolow_i_kont(maker) == (0, 0)


async def test_rejestracja_z_haslem_o_dokladnie_256_znakach_przechodzi(klient, maker):
    odp = await _rejestruj(klient, haslo=HASLO_256)
    assert odp.status_code == 201
    assert await _liczba_zespolow_i_kont(maker) == (1, 1)


async def test_logowanie_z_haslem_dluzszym_niz_256_znakow_daje_422_a_nie_401(klient):
    odp = await _zaloguj(klient, NIKT, HASLO_257)
    assert odp.status_code == 422
    assert odp.json()["detail"][0]["loc"] == ["body", "haslo"]


async def test_logowanie_z_haslem_o_dokladnie_256_znakach_nie_jest_odrzucane_jako_za_dlugie(
    klient,
):
    odp = await _zaloguj(klient, NIKT, HASLO_256)
    assert odp.status_code == 401  # walidacja przeszla, konto nie istnieje


@pytest.mark.parametrize(
    ("stare", "nowe", "pole"),
    [
        (HASLO_257, NOWE_HASLO, "stare"),
        (HASLO, HASLO_257, "nowe"),
    ],
)
async def test_zmiana_hasla_z_polem_dluzszym_niz_256_znakow_daje_422_z_wskazaniem_pola(
    klient, stare, nowe, pole
):
    _, csrf = await _zaloz_sesje(klient)
    odp = await klient.post(
        "/api/konto/haslo", json={"stare": stare, "nowe": nowe}, headers={"X-CSRF-Token": csrf}
    )
    assert odp.status_code == 422
    assert [b["loc"] for b in odp.json()["detail"]] == [["body", pole]]
    # haslo nie zostalo zmienione: stare nadal pozwala sie zalogowac
    assert (await _zaloguj(klient, JAN, HASLO)).status_code == 200


async def test_zmiana_hasla_na_nowe_o_dokladnie_256_znakach_przechodzi(klient):
    _, csrf = await _zaloz_sesje(klient)
    odp = await klient.post(
        "/api/konto/haslo",
        json={"stare": HASLO, "nowe": HASLO_256},
        headers={"X-CSRF-Token": csrf},
    )
    assert odp.status_code == 204


async def test_nazwa_zespolu_dluzsza_niz_200_znakow_daje_422_i_nie_tworzy_zespolu_ani_konta(
    klient, maker
):
    odp = await _rejestruj(klient, zespol="Z" * 201)
    assert odp.status_code == 422
    assert odp.json()["detail"][0]["loc"] == ["body", "nazwa_zespolu"]
    assert await _liczba_zespolow_i_kont(maker) == (0, 0)


async def test_nazwa_zespolu_o_dokladnie_200_znakach_przechodzi_w_calosci(klient, maker):
    odp = await _rejestruj(klient, zespol="Z" * 200)
    assert odp.status_code == 201
    async with maker() as s:
        nazwy = [t.name for t in (await s.execute(select(Tenant))).scalars()]
    assert nazwy == ["Z" * 200]


def _zadanie(metoda: str, *, z_klientem: bool = True):
    from starlette.requests import Request

    scope = {"type": "http", "method": metoda, "path": "/", "headers": []}
    if z_klientem:
        scope["client"] = ("203.0.113.7", 50000)
    return Request(scope)


async def test_odczyt_metoda_head_bez_tokenu_csrf_zwraca_kontekst_a_post_jest_zabroniony(
    klient, maker
):
    from cockpit.api.zaleznosci import kontekst_sesji
    from cockpit.services.errors import Zabronione

    identyfikator, _ = await _zaloz_sesje(klient)
    async with maker() as s:
        kontekst = await kontekst_sesji(_zadanie("HEAD"), identyfikator, s)
    assert kontekst.user_id
    assert kontekst.tenant_id
    async with maker() as s:
        with pytest.raises(Zabronione) as wyjatek:
            await kontekst_sesji(_zadanie("POST"), identyfikator, s)
    assert str(wyjatek.value) == "brak tokenu CSRF"


async def test_zle_haslo_przy_zadaniu_bez_adresu_klienta_to_brak_dostepu_i_licznik_dla_nieznany(
    maker,
):
    from fastapi import Response

    from cockpit.api import konta as trasy
    from cockpit.db.models import BlokadaLogowania
    from cockpit.services import konta as uslugi
    from cockpit.services.errors import BrakDostepu

    dane = trasy.DaneLogowania(email=NIKT, haslo=ZLE_HASLO)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as wyjatek:
            await trasy.logowanie(dane, _zadanie("POST", z_klientem=False), Response(), s)
    assert str(wyjatek.value) == KOMUNIKAT_LOGOWANIA
    async with maker() as s:
        wiersze = {w.klucz: w for w in (await s.execute(select(BlokadaLogowania))).scalars()}
    assert set(wiersze) == {
        uslugi.klucz_pary(NIKT, "nieznany"),
        uslugi.klucz_adresu(NIKT),
    }
    assert wiersze[uslugi.klucz_pary(NIKT, "nieznany")].nieudane == 1

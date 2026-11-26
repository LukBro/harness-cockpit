"""Tokeny runnera: nazwa, prefiks, ostatnie uzycie, termin waznosci, odwolanie (HAR-119).

Skutki sprawdzamy odczytem z bazy (nowa sesja), przez produkcyjne `services/tokeny.py`,
`runner_po_tokenie` i prawdziwa aplikacje. Czas wstrzykujemy parametrem `teraz`.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, RolaWZespole, Runner, Tenant
from cockpit.services import tokeny
from cockpit.services.dostep import Kontekst
from cockpit.services.errors import BrakDostepu, NieZnaleziono, Zabronione
from cockpit.services.runs import hash_token, runner_po_tokenie
from cockpit.services.sesje import na_utc
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_wlasciciela

TERAZ = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
KOMUNIKAT_ODWOLANY = "token odwołany - wygeneruj nowy w Ustawieniach"
KOMUNIKAT_WYGASL = "token wygasł - wygeneruj nowy w Ustawieniach"
POLA_LISTY = {
    "runner_id", "nazwa", "token_prefiks", "token_utworzony_at", "token_uzyty_at",
    "token_wazny_do", "odwolany_at",
}
# trasy, ktorymi runner przedstawia sie tokenem: (adres, poprawne cialo zadania)
TRASY_RUNNERA = [
    pytest.param("/api/runners/heartbeat", None, id="znak_zycia"),
    pytest.param("/api/runners/register",
                 {"name": "nazwa z rejestracji", "version": "9.9.9",
                  "backends": [{"nazwa": "claude", "auth": "api_key"}]},
                 id="rejestracja"),
]


class Swiat:
    def __init__(self, maker, klient, wlasciciel, tenant_id, obcy_tenant_id):
        self.maker = maker
        self.klient = klient
        self.wlasciciel = wlasciciel
        self.tenant_id = tenant_id
        self.obcy_tenant_id = obcy_tenant_id

    def kontekst(self, rola: RolaWZespole = RolaWZespole.owner) -> Kontekst:
        return Kontekst(user_id=self.wlasciciel.id, tenant_id=self.tenant_id, rola=rola)

    def kontekst_obcy(self) -> Kontekst:
        return Kontekst(user_id=self.wlasciciel.id, tenant_id=self.obcy_tenant_id,
                        rola=RolaWZespole.owner)

    async def wydaj(self, nazwa="macbook", wazny_do=None, kontekst=None):
        async with self.maker() as s:
            return await tokeny.wydaj_token_runnera(
                s, kontekst or self.kontekst(), nazwa, wazny_do)

    async def runner(self, runner_id: str) -> Runner:
        async with self.maker() as s:      # nowa sesja: czytamy to, co naprawde zapisano
            return (await s.execute(select(Runner).where(Runner.id == runner_id))).scalar_one()

    async def wszystkie_runnery(self) -> list[Runner]:
        async with self.maker() as s:
            return list((await s.execute(select(Runner))).scalars().all())

    async def uwierzytelnij(self, token: str | None, teraz=None):
        async with self.maker() as s:
            return await runner_po_tokenie(s, f"Bearer {token}", teraz)


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker)
    async with maker() as s:
        obcy = Tenant(name="Piekarnia")
        s.add(obcy)
        await s.commit()
        obcy_id = obcy.id
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield Swiat(maker, c, wlasciciel, tenant_id, obcy_id)
    finally:
        await engine.dispose()


# --- wydanie tokenu ------------------------------------------------------------

async def test_wydany_token_zaczyna_sie_od_ckr_a_w_bazie_jest_tylko_jego_skrot(swiat):
    runner, token = await swiat.wydaj()

    assert token.startswith("ckr_")
    zapisany = await swiat.runner(runner.id)
    assert zapisany.token_hash == hash_token(token)
    assert token not in {zapisany.token_hash, zapisany.token_prefiks, zapisany.name}


async def test_prefiks_tokenu_to_dokladnie_osiem_pierwszych_znakow_tokenu(swiat):
    runner, token = await swiat.wydaj()

    zapisany = await swiat.runner(runner.id)
    assert zapisany.token_prefiks == token[:8]
    assert len(zapisany.token_prefiks) == 8


async def test_kazdy_wydany_token_jest_inny(swiat):
    _, pierwszy = await swiat.wydaj("a")
    _, drugi = await swiat.wydaj("b")

    assert pierwszy != drugi


async def test_wydanie_zapisuje_nazwe_zespol_i_chwile_utworzenia_a_nie_uzycie_ani_odwolanie(swiat):
    przed = datetime.now(timezone.utc)
    runner, _ = await swiat.wydaj("laptop Ani")
    po = datetime.now(timezone.utc)

    zapisany = await swiat.runner(runner.id)
    assert zapisany.name == "laptop Ani"
    assert zapisany.tenant_id == swiat.tenant_id
    assert przed - timedelta(seconds=60) <= na_utc(zapisany.token_utworzony_at) <= po + timedelta(seconds=60)
    assert zapisany.token_uzyty_at is None      # brak pomiaru to brak, nie zero
    assert zapisany.odwolany_at is None
    assert zapisany.token_wazny_do is None


async def test_wydanie_z_terminem_waznosci_zapisuje_ten_termin(swiat):
    termin = TERAZ + timedelta(days=30)

    runner, _ = await swiat.wydaj(wazny_do=termin)

    assert na_utc((await swiat.runner(runner.id)).token_wazny_do) == termin


@pytest.mark.parametrize("rola", [RolaWZespole.owner, RolaWZespole.admin, RolaWZespole.member])
async def test_token_runnera_moze_wydac_wlasciciel_administrator_i_czlonek(swiat, rola):
    runner, token = await swiat.wydaj(kontekst=swiat.kontekst(rola))

    assert (await swiat.runner(runner.id)).token_hash == hash_token(token)


async def test_obserwator_nie_wydaje_tokenu_i_w_bazie_nie_powstaje_runner(swiat):
    with pytest.raises(Zabronione):
        await swiat.wydaj(kontekst=swiat.kontekst(RolaWZespole.viewer))

    assert await swiat.wszystkie_runnery() == []


# --- lista ---------------------------------------------------------------------

async def test_lista_tokenow_zawiera_tylko_runnery_wlasnego_zespolu(swiat):
    swiat_runner, _ = await swiat.wydaj("nasz")
    await swiat.wydaj("cudzy", kontekst=swiat.kontekst_obcy())

    async with swiat.maker() as s:
        lista = await tokeny.tokeny_runnerow(s, swiat.kontekst())

    assert len(lista) == 1
    odp = (await swiat.klient.get("/api/runnery")).json()
    assert [w["runner_id"] for w in odp] == [swiat_runner.id]
    assert [w["nazwa"] for w in odp] == ["nasz"]


async def test_lista_tokenow_pokazuje_dane_tokenu_ale_nigdy_sam_token_ani_skrot(swiat):
    runner, token = await swiat.wydaj("laptop", wazny_do=TERAZ + timedelta(days=1))

    r = await swiat.klient.get("/api/runnery")

    assert r.status_code == 200
    (wiersz,) = r.json()
    assert set(wiersz) == POLA_LISTY
    assert wiersz["token_prefiks"] == token[:8]
    assert wiersz["token_wazny_do"] is not None
    assert wiersz["token_uzyty_at"] is None
    assert wiersz["odwolany_at"] is None
    assert token not in r.text
    assert hash_token(token) not in r.text


# --- wydanie przez HTTP --------------------------------------------------------

async def test_wydanie_przez_api_zwraca_identyfikator_i_token_ktory_jest_w_bazie_jako_skrot(swiat):
    r = await swiat.klient.post("/api/runnery", json={"nazwa": "macbook"})

    assert r.status_code == 200
    assert set(r.json()) == {"runner_id", "token"}
    zapisany = await swiat.runner(r.json()["runner_id"])
    assert zapisany.token_hash == hash_token(r.json()["token"])
    assert zapisany.tenant_id == swiat.tenant_id
    assert zapisany.name == "macbook"


async def test_wydanie_przez_api_nie_pozwala_obserwatorowi_i_nie_tworzy_runnera(swiat):
    obserwator = await zaloz_czlonka(swiat.maker, swiat.tenant_id, RolaWZespole.viewer, "v@example.com")
    await zaloguj_klienta(swiat.klient, swiat.maker, obserwator)

    r = await swiat.klient.post("/api/runnery", json={"nazwa": "macbook"})

    assert r.status_code == 403
    assert await swiat.wszystkie_runnery() == []


async def test_wydanie_przez_api_bez_sesji_daje_401_i_nie_tworzy_runnera(swiat):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonim:
        r = await anonim.post("/api/runnery", json={"nazwa": "macbook"})

    assert r.status_code == 401
    assert await swiat.wszystkie_runnery() == []


@pytest.mark.parametrize("nazwa", ["", "x" * 201], ids=["pusta", "o_znak_za_dluga"])
async def test_nazwa_runnera_poza_limitem_daje_422_i_nie_tworzy_runnera(swiat, nazwa):
    r = await swiat.klient.post("/api/runnery", json={"nazwa": nazwa})

    assert r.status_code == 422
    assert await swiat.wszystkie_runnery() == []


async def test_nazwa_runnera_o_dokladnie_dopuszczalnej_dlugosci_przechodzi(swiat):
    r = await swiat.klient.post("/api/runnery", json={"nazwa": "x" * 200})

    assert r.status_code == 200
    assert (await swiat.runner(r.json()["runner_id"])).name == "x" * 200


# --- uzycie tokenu -------------------------------------------------------------

async def test_wydany_token_uwierzytelnia_runnera(swiat):
    runner, token = await swiat.wydaj()

    assert (await swiat.uwierzytelnij(token)).id == runner.id


async def test_udane_uzycie_tokenu_zapisuje_chwile_ostatniego_uzycia_w_bazie(swiat):
    runner, token = await swiat.wydaj()
    przed = datetime.now(timezone.utc)

    await swiat.uwierzytelnij(token)

    uzyty = (await swiat.runner(runner.id)).token_uzyty_at
    assert uzyty is not None
    assert na_utc(uzyty) >= przed - timedelta(seconds=60)


async def test_heartbeat_runnera_zapisuje_ostatnie_uzycie_tokenu_w_bazie(swiat):
    runner, token = await swiat.wydaj()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/runners/heartbeat", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    assert (await swiat.runner(runner.id)).token_uzyty_at is not None


async def test_token_bez_terminu_waznosci_nie_wygasa(swiat):
    runner, token = await swiat.wydaj(wazny_do=None)

    assert (await swiat.uwierzytelnij(token, TERAZ + timedelta(days=36500))).id == runner.id


async def test_token_przed_terminem_waznosci_dziala(swiat):
    runner, token = await swiat.wydaj(wazny_do=TERAZ)

    assert (await swiat.uwierzytelnij(token, TERAZ - timedelta(seconds=1))).id == runner.id


async def test_token_po_terminie_waznosci_jest_odrzucony_komunikatem_o_wygasnieciu(swiat):
    _, token = await swiat.wydaj(wazny_do=TERAZ)

    with pytest.raises(BrakDostepu) as e:
        await swiat.uwierzytelnij(token, TERAZ + timedelta(seconds=1))

    assert str(e.value) == KOMUNIKAT_WYGASL


async def test_token_w_chwili_terminu_waznosci_jest_juz_wygasly(swiat):
    _, token = await swiat.wydaj(wazny_do=TERAZ)

    with pytest.raises(BrakDostepu) as e:
        await swiat.uwierzytelnij(token, TERAZ)

    assert str(e.value) == KOMUNIKAT_WYGASL


async def test_odwolany_token_jest_odrzucony_komunikatem_o_odwolaniu(swiat):
    runner, token = await swiat.wydaj()
    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), runner.id)

    with pytest.raises(BrakDostepu) as e:
        await swiat.uwierzytelnij(token)

    assert str(e.value) == KOMUNIKAT_ODWOLANY


async def test_token_odwolany_i_wygasly_jest_zgloszony_jako_odwolany(swiat):
    runner, token = await swiat.wydaj(wazny_do=TERAZ)
    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), runner.id)

    with pytest.raises(BrakDostepu) as e:
        await swiat.uwierzytelnij(token, TERAZ + timedelta(days=1))

    assert str(e.value) == KOMUNIKAT_ODWOLANY


async def test_odrzucony_token_nie_zapisuje_ostatniego_uzycia(swiat):
    wygasly, token_wygasly = await swiat.wydaj("a", wazny_do=TERAZ)
    odwolany, token_odwolany = await swiat.wydaj("b")
    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), odwolany.id)

    with pytest.raises(BrakDostepu):
        await swiat.uwierzytelnij(token_wygasly, TERAZ + timedelta(days=1))
    with pytest.raises(BrakDostepu):
        await swiat.uwierzytelnij(token_odwolany)

    assert (await swiat.runner(wygasly.id)).token_uzyty_at is None
    assert (await swiat.runner(odwolany.id)).token_uzyty_at is None


@pytest.mark.parametrize("trasa, cialo", TRASY_RUNNERA)
async def test_odwolany_token_runnera_daje_401_z_komunikatem_i_nic_nie_zapisuje_o_runnerze(
        swiat, trasa, cialo):
    runner, token = await swiat.wydaj()
    await swiat.klient.delete(f"/api/runnery/{runner.id}")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(trasa, json=cialo, headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 401
    assert r.json()["detail"] == KOMUNIKAT_ODWOLANY
    zapisany = await swiat.runner(runner.id)
    assert zapisany.name == "macbook"
    assert zapisany.last_seen_at is None
    assert zapisany.token_uzyty_at is None


async def test_stary_token_bez_prefiksu_dziala_dalej(swiat):
    async with swiat.maker() as s:
        stary = Runner(tenant_id=swiat.tenant_id, name="sprzed-zmiany",
                       token_hash=hash_token("stary-token"))
        s.add(stary)
        await s.commit()
        stary_id = stary.id

    assert (await swiat.uwierzytelnij("stary-token", TERAZ)).id == stary_id
    assert (await swiat.runner(stary_id)).token_prefiks is None


async def test_nieznany_token_jest_odrzucony_jak_dotad(swiat):
    with pytest.raises(BrakDostepu) as e:
        await swiat.uwierzytelnij("ckr_nieznany")

    assert str(e.value) == "token nieznany - wygeneruj nowy w Ustawieniach"


# --- odwolanie -----------------------------------------------------------------

async def test_odwolanie_zapisuje_chwile_odwolania_w_bazie(swiat):
    runner, _ = await swiat.wydaj()
    przed = datetime.now(timezone.utc)

    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), runner.id)

    odwolany = (await swiat.runner(runner.id)).odwolany_at
    assert odwolany is not None
    assert na_utc(odwolany) >= przed - timedelta(seconds=60)


async def test_odwolanie_przez_api_zwraca_200_z_modelem_i_zapisuje_odwolanie(swiat):
    runner, _ = await swiat.wydaj("laptop")

    r = await swiat.klient.delete(f"/api/runnery/{runner.id}")

    assert r.status_code == 200
    assert r.json()["runner_id"] == runner.id
    assert r.json()["odwolany_at"] is not None
    assert (await swiat.runner(runner.id)).odwolany_at is not None


async def test_odwolany_token_nadal_widac_na_liscie_z_chwila_odwolania(swiat):
    runner, _ = await swiat.wydaj()
    await swiat.klient.delete(f"/api/runnery/{runner.id}")

    (wiersz,) = (await swiat.klient.get("/api/runnery")).json()

    assert wiersz["runner_id"] == runner.id
    assert wiersz["odwolany_at"] is not None


async def test_odwolanie_runnera_innego_zespolu_daje_404_i_nie_zmienia_go_w_bazie(swiat):
    cudzy, token = await swiat.wydaj("cudzy", kontekst=swiat.kontekst_obcy())

    r = await swiat.klient.delete(f"/api/runnery/{cudzy.id}")

    assert r.status_code == 404
    assert (await swiat.runner(cudzy.id)).odwolany_at is None
    assert (await swiat.uwierzytelnij(token)).id == cudzy.id      # nadal dziala


async def test_odwolanie_runnera_innego_zespolu_w_usludze_to_nie_znaleziono(swiat):
    cudzy, _ = await swiat.wydaj("cudzy", kontekst=swiat.kontekst_obcy())

    async with swiat.maker() as s:
        with pytest.raises(NieZnaleziono):
            await tokeny.odwolaj(s, swiat.kontekst(), cudzy.id)

    assert (await swiat.runner(cudzy.id)).odwolany_at is None


async def test_odwolanie_nieistniejacego_runnera_daje_404(swiat):
    r = await swiat.klient.delete("/api/runnery/nie-ma-takiego")

    assert r.status_code == 404


async def test_obserwator_nie_odwoluje_tokenu_i_w_bazie_nic_sie_nie_zmienia(swiat):
    runner, _ = await swiat.wydaj()
    obserwator = await zaloz_czlonka(swiat.maker, swiat.tenant_id, RolaWZespole.viewer, "v@example.com")
    await zaloguj_klienta(swiat.klient, swiat.maker, obserwator)

    r = await swiat.klient.delete(f"/api/runnery/{runner.id}")

    assert r.status_code == 403
    assert (await swiat.runner(runner.id)).odwolany_at is None


# --- poprawka po review (HAR-119): warunki zapisu, kolejnosc listy, czas, strefy ----------

async def _wstaw_runnera_z_czasami(swiat, runner_id, *, utworzony=None, uzyty=None,
                                   wazny_do=None, odwolany=None):
    """Runner z jawnie podanymi chwilami (rdzen SQLAlchemy: bez domyslnej chwili utworzenia)."""
    async with swiat.maker() as s:
        await s.execute(Runner.__table__.insert().values(
            id=runner_id, tenant_id=swiat.tenant_id, name=f"r-{runner_id}",
            token_hash=hash_token(f"token-{runner_id}"), token_prefiks="ckr_test",
            token_utworzony_at=utworzony, token_uzyty_at=uzyty,
            token_wazny_do=wazny_do, odwolany_at=odwolany))
        await s.commit()


async def test_odwolanie_jednego_runnera_zespolu_nie_odwoluje_drugiego_i_jego_token_dziala(swiat):
    pierwszy, _ = await swiat.wydaj("pierwszy")
    drugi, token_drugiego = await swiat.wydaj("drugi")

    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), pierwszy.id)

    assert (await swiat.runner(pierwszy.id)).odwolany_at is not None
    assert (await swiat.runner(drugi.id)).odwolany_at is None
    assert (await swiat.uwierzytelnij(token_drugiego)).id == drugi.id


async def test_uzycie_tokenu_jednego_runnera_zespolu_nie_zapisuje_uzycia_drugiemu(swiat):
    uzywany, token = await swiat.wydaj("uzywany")
    drugi, _ = await swiat.wydaj("drugi")

    await swiat.uwierzytelnij(token)

    assert (await swiat.runner(uzywany.id)).token_uzyty_at is not None
    assert (await swiat.runner(drugi.id)).token_uzyty_at is None


async def test_ponowne_odwolanie_zachowuje_pierwsza_chwile_odwolania(swiat):
    runner, _ = await swiat.wydaj()
    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), runner.id)
    pierwsza = (await swiat.runner(runner.id)).odwolany_at

    async with swiat.maker() as s:
        await tokeny.odwolaj(s, swiat.kontekst(), runner.id)

    assert (await swiat.runner(runner.id)).odwolany_at == pierwsza


async def test_ponowne_odwolanie_przez_api_zwraca_pierwsza_chwile_odwolania(swiat):
    runner, _ = await swiat.wydaj()
    pierwsze = (await swiat.klient.delete(f"/api/runnery/{runner.id}")).json()["odwolany_at"]

    drugie = await swiat.klient.delete(f"/api/runnery/{runner.id}")

    assert drugie.status_code == 200
    assert drugie.json()["odwolany_at"] == pierwsze


async def test_lista_tokenow_ma_najnowszy_token_pierwszy_a_runnery_bez_chwili_utworzenia_na_koncu(swiat):
    bazowa = datetime(2030, 1, 10, 8, 0, 0, tzinfo=timezone.utc)
    # kolejnosc wstawiania celowo rozna od wynikowej
    await _wstaw_runnera_z_czasami(swiat, "r-stary", utworzony=bazowa - timedelta(days=2))
    await _wstaw_runnera_z_czasami(swiat, "r-bez-daty", utworzony=None)
    await _wstaw_runnera_z_czasami(swiat, "r-najnowszy", utworzony=bazowa)
    await _wstaw_runnera_z_czasami(swiat, "r-srodkowy", utworzony=bazowa - timedelta(days=1))

    odp = (await swiat.klient.get("/api/runnery")).json()

    assert [w["runner_id"] for w in odp] == ["r-najnowszy", "r-srodkowy", "r-stary", "r-bez-daty"]


async def test_runnery_z_ta_sama_chwila_utworzenia_sa_na_liscie_w_kolejnosci_identyfikatorow(swiat):
    chwila = datetime(2030, 1, 10, 8, 0, 0, tzinfo=timezone.utc)
    await _wstaw_runnera_z_czasami(swiat, "r-z", utworzony=chwila)
    await _wstaw_runnera_z_czasami(swiat, "r-a", utworzony=chwila)
    await _wstaw_runnera_z_czasami(swiat, "r-n2", utworzony=None)
    await _wstaw_runnera_z_czasami(swiat, "r-n1", utworzony=None)

    odp = (await swiat.klient.get("/api/runnery")).json()

    assert [w["runner_id"] for w in odp] == ["r-a", "r-z", "r-n1", "r-n2"]


async def test_zapytanie_o_liste_tokenow_wymaga_od_bazy_runnerow_bez_daty_na_koncu(swiat):
    # SQLite sam kladzie NULL na koncu przy sortowaniu malejacym, PostgreSQL na poczatku;
    # kolejnosc w danych nie odroznia wiec reguly na SQLite, dlatego sprawdzamy zapytanie.
    zapytania: list[str] = []
    engine = db_session.engine.sync_engine

    def zapamietaj(conn, cursor, statement, parameters, context, executemany):
        zapytania.append(statement)

    event.listen(engine, "before_cursor_execute", zapamietaj)
    try:
        async with swiat.maker() as s:
            await tokeny.tokeny_runnerow(s, swiat.kontekst())
    finally:
        event.remove(engine, "before_cursor_execute", zapamietaj)

    (zapytanie,) = [z for z in zapytania if "ORDER BY" in z]
    assert "token_utworzony_at DESC NULLS LAST" in zapytanie


@pytest.mark.parametrize("trasa, cialo", TRASY_RUNNERA)
async def test_wygasly_token_runnera_daje_401_z_komunikatem_o_wygasnieciu_i_nic_nie_zapisuje(
        swiat, trasa, cialo):
    runner, token = await swiat.wydaj(wazny_do=datetime.now(timezone.utc) - timedelta(hours=1))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(trasa, json=cialo, headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 401
    assert r.json()["detail"] == KOMUNIKAT_WYGASL
    zapisany = await swiat.runner(runner.id)
    assert zapisany.name == "macbook"
    assert zapisany.token_uzyty_at is None
    assert zapisany.last_seen_at is None


async def test_token_z_terminem_w_przyszlosci_przez_http_dziala_i_zapisuje_uzycie(swiat):
    runner, token = await swiat.wydaj(wazny_do=datetime.now(timezone.utc) + timedelta(hours=1))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/runners/heartbeat", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200
    assert (await swiat.runner(runner.id)).token_uzyty_at is not None


@pytest.fixture
def strefa_maszyny_tokio():
    """Strefa lokalna procesu inna niz UTC; po tescie przywrocona tylko strefa (TZ)."""
    poprzednia = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        yield
    finally:
        if poprzednia is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = poprzednia
        time.tzset()


async def test_lista_tokenow_pokazuje_wartosci_czterech_chwil_zgodne_z_baza_w_utc(
        swiat, strefa_maszyny_tokio):
    utworzony = datetime(2030, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
    uzyty = datetime(2030, 2, 2, 11, 0, 2, tzinfo=timezone.utc)
    wazny_do = datetime(2030, 3, 3, 12, 0, 3, tzinfo=timezone.utc)
    odwolany = datetime(2030, 4, 4, 13, 0, 4, tzinfo=timezone.utc)
    await _wstaw_runnera_z_czasami(
        swiat, "r-czasy", utworzony=utworzony, uzyty=uzyty, wazny_do=wazny_do, odwolany=odwolany)

    (wiersz,) = (await swiat.klient.get("/api/runnery")).json()

    assert wiersz["token_utworzony_at"] == "2030-01-01T10:00:01+00:00"
    assert wiersz["token_uzyty_at"] == "2030-02-02T11:00:02+00:00"
    assert wiersz["token_wazny_do"] == "2030-03-03T12:00:03+00:00"
    assert wiersz["odwolany_at"] == "2030-04-04T13:00:04+00:00"


async def test_wydanie_przez_api_z_terminem_bez_strefy_zapisuje_go_jako_utc(
        swiat, strefa_maszyny_tokio):
    r = await swiat.klient.post(
        "/api/runnery", json={"nazwa": "m", "wazny_do": "2031-01-01T10:00:00"})

    assert r.status_code == 200
    zapisany = await swiat.runner(r.json()["runner_id"])
    assert na_utc(zapisany.token_wazny_do) == datetime(2031, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


async def test_wydanie_przez_api_z_terminem_w_innej_strefie_zapisuje_go_przeliczonego_na_utc(swiat):
    r = await swiat.klient.post(
        "/api/runnery", json={"nazwa": "m", "wazny_do": "2031-01-01T10:00:00+02:00"})

    assert r.status_code == 200
    zapisany = await swiat.runner(r.json()["runner_id"])
    assert na_utc(zapisany.token_wazny_do) == datetime(2031, 1, 1, 8, 0, 0, tzinfo=timezone.utc)


async def test_wydanie_przez_api_bez_terminu_zapisuje_brak_terminu(swiat):
    r = await swiat.klient.post("/api/runnery", json={"nazwa": "m"})

    assert r.status_code == 200
    assert (await swiat.runner(r.json()["runner_id"])).token_wazny_do is None


@pytest.mark.parametrize("termin", ["9999-12-31T23:59:59-00:01", "0001-01-01T00:00:00+00:01"],
                         ids=["za_daleko_w_przyszlosci", "za_daleko_w_przeszlosci"])
async def test_termin_waznosci_poza_zakresem_daje_422_i_nie_tworzy_runnera(swiat, termin):
    r = await swiat.klient.post("/api/runnery", json={"nazwa": "m", "wazny_do": termin})

    assert r.status_code == 422
    (wpis,) = r.json()["detail"]
    assert wpis["loc"] == ["body", "wazny_do"]
    assert wpis["msg"] == "Value error, wazny_do poza zakresem"
    assert await swiat.wszystkie_runnery() == []


async def test_wydanie_przez_api_z_jawnie_pustym_terminem_zapisuje_brak_terminu(swiat):
    r = await swiat.klient.post("/api/runnery", json={"nazwa": "m", "wazny_do": None})

    assert r.status_code == 200
    assert (await swiat.runner(r.json()["runner_id"])).token_wazny_do is None


@pytest.mark.parametrize("rola", [RolaWZespole.admin, RolaWZespole.member])
async def test_administrator_i_czlonek_odwoluja_token_a_odwolanie_jest_w_bazie(swiat, rola):
    runner, token = await swiat.wydaj()
    inny = await zaloz_czlonka(swiat.maker, swiat.tenant_id, rola, "inny@example.com")
    await zaloguj_klienta(swiat.klient, swiat.maker, inny)

    r = await swiat.klient.delete(f"/api/runnery/{runner.id}")

    assert r.status_code == 200
    assert (await swiat.runner(runner.id)).odwolany_at is not None
    with pytest.raises(BrakDostepu) as e:
        await swiat.uwierzytelnij(token)
    assert str(e.value) == KOMUNIKAT_ODWOLANY


async def test_obserwator_widzi_liste_tokenow_w_podgladzie(swiat):
    runner, _ = await swiat.wydaj("laptop")
    obserwator = await zaloz_czlonka(swiat.maker, swiat.tenant_id, RolaWZespole.viewer, "v@example.com")
    await zaloguj_klienta(swiat.klient, swiat.maker, obserwator)

    r = await swiat.klient.get("/api/runnery")

    assert r.status_code == 200
    assert [w["runner_id"] for w in r.json()] == [runner.id]


async def test_lista_i_odwolanie_tokenow_bez_sesji_daja_401_a_odwolanie_nic_nie_zmienia(swiat):
    runner, _ = await swiat.wydaj()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonim:
        lista = await anonim.get("/api/runnery")
        odwolanie = await anonim.delete(f"/api/runnery/{runner.id}")

    assert lista.status_code == 401
    assert odwolanie.status_code == 401
    assert (await swiat.runner(runner.id)).odwolany_at is None


class _SesjaOdwolujacaPrzedZapisem:
    """Sesja produkcyjnego `runner_po_tokenie`, w ktorej inna sesja zatwierdza odwolanie
    tuz przed UPDATE zapisujacym uzycie (przeplot: odczyt tokenu -> odwolanie -> zapis)."""

    def __init__(self, sesja, maker, kontekst, runner_id):
        self._sesja, self._maker, self._kontekst, self._runner_id = sesja, maker, kontekst, runner_id
        self.odwolano = False

    def __getattr__(self, nazwa):
        return getattr(self._sesja, nazwa)

    async def execute(self, instrukcja, *args, **kwargs):
        if isinstance(instrukcja, Update) and not self.odwolano:
            self.odwolano = True
            async with self._maker() as druga:
                await tokeny.odwolaj(druga, self._kontekst, self._runner_id)
        return await self._sesja.execute(instrukcja, *args, **kwargs)


async def test_odwolanie_zatwierdzone_miedzy_odczytem_a_zapisem_uzycia_odrzuca_token(swiat):
    runner, token = await swiat.wydaj()

    async with swiat.maker() as s:
        przeplot = _SesjaOdwolujacaPrzedZapisem(s, swiat.maker, swiat.kontekst(), runner.id)
        with pytest.raises(BrakDostepu) as e:
            await runner_po_tokenie(przeplot, f"Bearer {token}")

    assert przeplot.odwolano
    assert str(e.value) == KOMUNIKAT_ODWOLANY
    zapisany = await swiat.runner(runner.id)
    assert zapisany.odwolany_at is not None
    assert zapisany.token_uzyty_at is None


class _SesjaUsuwajacaRunneraPrzedZapisem(_SesjaOdwolujacaPrzedZapisem):
    """Jak wyzej, ale druga sesja USUWA wiersz runnera tuz przed UPDATE."""

    async def execute(self, instrukcja, *args, **kwargs):
        if isinstance(instrukcja, Update) and not self.odwolano:
            self.odwolano = True
            async with self._maker() as druga:
                await druga.execute(Runner.__table__.delete().where(Runner.id == self._runner_id))
                await druga.commit()
        return await self._sesja.execute(instrukcja, *args, **kwargs)


async def test_runner_usuniety_miedzy_odczytem_a_zapisem_uzycia_daje_odmowe_dostepu_nie_blad(swiat):
    runner, token = await swiat.wydaj()

    async with swiat.maker() as s:
        przeplot = _SesjaUsuwajacaRunneraPrzedZapisem(s, swiat.maker, swiat.kontekst(), runner.id)
        with pytest.raises(BrakDostepu) as e:
            await runner_po_tokenie(przeplot, f"Bearer {token}")

    assert przeplot.odwolano
    assert str(e.value) == KOMUNIKAT_WYGASL
    assert await swiat.wszystkie_runnery() == []


async def test_runner_zapisany_przez_model_bez_chwili_utworzenia_dostaje_chwile_zapisu(swiat):
    przed = datetime.now(timezone.utc)
    async with swiat.maker() as s:
        runner = Runner(tenant_id=swiat.tenant_id, name="bez-daty", token_hash=hash_token("x"))
        s.add(runner)
        await s.commit()
        runner_id = runner.id
    po = datetime.now(timezone.utc)

    zapisany = await swiat.runner(runner_id)

    assert zapisany.token_utworzony_at is not None
    assert przed - timedelta(seconds=60) <= na_utc(zapisany.token_utworzony_at) <= po + timedelta(seconds=60)
    assert zapisany.token_uzyty_at is None
    assert zapisany.token_wazny_do is None
    assert zapisany.odwolany_at is None

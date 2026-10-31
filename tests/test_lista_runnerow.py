"""Lista runnerow zespolu ze stanem online/offline, wersja i backendami (HAR-68).

Usluga `runnery_zespolu` i trasa `GET /api/runners`. SQLite w tmp_path, prawdziwa aplikacja.
Stan online wynika z reguly `runner_online`: strumien otwarty ORAZ swiezy heartbeat.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, RolaWZespole, Runner, Tenant
from cockpit.services.runs import runnery_zespolu
from cockpit.settings import settings
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

TERAZ = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
KLUCZE = {"id", "name", "version", "backends", "online", "last_seen_at"}
BACKENDY = [{"nazwa": "claude", "auth": "subscription"}]


@pytest.fixture
async def baza(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lista_runnerow.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        a, b = Tenant(name="Zespol A"), Tenant(name="Zespol B")
        s.add_all([a, b])
        await s.commit()
        tenant_a, tenant_b = a.id, b.id
    yield SimpleNamespace(maker=maker, tenant_a=tenant_a, tenant_b=tenant_b)
    await engine.dispose()


async def _runner(baza, tenant_id, name, *, id=None, version="", backends=None,
                  last_seen_at=TERAZ) -> str:
    async with baza.maker() as s:
        r = Runner(tenant_id=tenant_id, name=name, token_hash="x" * 64, version=version,
                   backends=backends if backends is not None else [], last_seen_at=last_seen_at)
        if id:
            r.id = id
        s.add(r)
        await s.commit()
        return r.id


async def _lista(baza, tenant_id, polaczone, teraz=TERAZ) -> list[dict]:
    async with baza.maker() as s:
        return await runnery_zespolu(s, tenant_id, polaczone, teraz)


# --- usluga: stan, wersja, backendy --------------------------------------------------------------


async def test_polaczony_runner_ze_swiezym_heartbeatem_jest_online_z_wersja_i_backendami(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop", version="1.4.2", backends=BACKENDY,
                        last_seen_at=TERAZ - timedelta(seconds=3))

    lista = await _lista(baza, baza.tenant_a, {rid})

    assert lista == [{
        "id": rid, "name": "laptop", "version": "1.4.2", "backends": BACKENDY,
        "online": True, "last_seen_at": (TERAZ - timedelta(seconds=3)).isoformat(),
    }]


async def test_runner_obcego_zespolu_nie_pojawia_sie_na_liscie_zespolu(baza):
    swoj = await _runner(baza, baza.tenant_a, "swoj")
    obcy = await _runner(baza, baza.tenant_b, "obcy")  # polaczony i swiezy: by sie zakwalifikowal

    lista = await _lista(baza, baza.tenant_a, {swoj, obcy})

    assert [r["id"] for r in lista] == [swoj]


async def test_runner_niepolaczony_ze_strumieniem_jest_offline_mimo_swiezego_heartbeatu(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop", last_seen_at=TERAZ)

    lista = await _lista(baza, baza.tenant_a, set())

    assert [(r["id"], r["online"]) for r in lista] == [(rid, False)]


async def test_polaczony_runner_bez_zadnego_heartbeatu_jest_offline_a_ostatni_kontakt_pusty(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop", last_seen_at=None)

    lista = await _lista(baza, baza.tenant_a, {rid})

    assert [(r["online"], r["last_seen_at"]) for r in lista] == [(False, None)]


async def test_runner_z_przeterminowanym_heartbeatem_jest_offline_mimo_otwartego_strumienia(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop",
                        last_seen_at=TERAZ - timedelta(seconds=settings.runner_offline_after_s + 30))

    lista = await _lista(baza, baza.tenant_a, {rid})

    assert [r["online"] for r in lista] == [False]


async def test_wiek_heartbeatu_rowny_progowi_to_offline_a_sekunde_mniej_to_online(baza, monkeypatch):
    monkeypatch.setattr(settings, "runner_offline_after_s", 37)  # prog z ustawienia, nie stala
    na_progu = await _runner(baza, baza.tenant_a, "na-progu",
                             last_seen_at=TERAZ - timedelta(seconds=37))
    przed_progiem = await _runner(baza, baza.tenant_a, "przed-progiem",
                                  last_seen_at=TERAZ - timedelta(seconds=36))

    lista = await _lista(baza, baza.tenant_a, {na_progu, przed_progiem})

    assert {r["name"]: r["online"] for r in lista} == {"na-progu": False, "przed-progiem": True}


async def test_zmiana_progu_w_ustawieniach_zmienia_stan_tego_samego_runnera(baza, monkeypatch):
    rid = await _runner(baza, baza.tenant_a, "laptop", last_seen_at=TERAZ - timedelta(seconds=20))

    monkeypatch.setattr(settings, "runner_offline_after_s", 10)
    krotki = await _lista(baza, baza.tenant_a, {rid})
    monkeypatch.setattr(settings, "runner_offline_after_s", 600)
    dlugi = await _lista(baza, baza.tenant_a, {rid})

    assert (krotki[0]["online"], dlugi[0]["online"]) == (False, True)


# --- usluga: ksztalt i kolejnosc -----------------------------------------------------------------


async def test_pozycja_listy_ma_dokladnie_pola_publiczne_bez_skrotu_tokenu_i_zespolu(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop")

    lista = await _lista(baza, baza.tenant_a, {rid})

    assert set(lista[0]) == KLUCZE


async def test_runnery_sa_posortowane_po_nazwie_a_nie_po_id(baza):
    # kolejnosc po id (aaa < bbb < ccc) jest odwrotna niz po nazwie
    await _runner(baza, baza.tenant_a, "alfa", id="ccc")
    await _runner(baza, baza.tenant_a, "gamma", id="aaa")
    await _runner(baza, baza.tenant_a, "beta", id="bbb")

    lista = await _lista(baza, baza.tenant_a, set())

    assert [r["name"] for r in lista] == ["alfa", "beta", "gamma"]


async def test_runnery_o_tej_samej_nazwie_sa_posortowane_po_id(baza):
    await _runner(baza, baza.tenant_a, "laptop", id="b-drugi")
    await _runner(baza, baza.tenant_a, "laptop", id="a-pierwszy")
    await _runner(baza, baza.tenant_a, "laptop", id="c-trzeci")

    lista = await _lista(baza, baza.tenant_a, set())

    assert [r["id"] for r in lista] == ["a-pierwszy", "b-drugi", "c-trzeci"]


# --- usluga: ostatni kontakt jako ISO w UTC ------------------------------------------------------


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


async def test_lista_bez_podanego_czasu_liczy_stan_wzgledem_biezacej_chwili(baza):
    teraz = datetime.now(timezone.utc)
    swiezy = await _runner(baza, baza.tenant_a, "swiezy", last_seen_at=teraz - timedelta(seconds=1))
    stary = await _runner(
        baza, baza.tenant_a, "stary",
        last_seen_at=teraz - timedelta(seconds=settings.runner_offline_after_s + 5))

    async with baza.maker() as s:
        lista = await runnery_zespolu(s, baza.tenant_a, {swiezy, stary})   # bez `teraz`

    assert {r["name"]: r["online"] for r in lista} == {"swiezy": True, "stary": False}


async def test_ostatni_kontakt_bez_strefy_jest_traktowany_jako_utc_niezaleznie_od_strefy_maszyny(
        baza, strefa_maszyny_tokio):
    # kontrola zalozenia: strefa lokalna faktycznie rozni sie od UTC
    assert datetime(2030, 1, 1, 12).astimezone(timezone.utc).hour != 12
    rid = await _runner(baza, baza.tenant_a, "laptop",
                        last_seen_at=datetime(2030, 1, 1, 11, 59, 30, tzinfo=timezone.utc))
    async with baza.maker() as s:   # kontrola zalozenia: SQLite oddaje date bez strefy
        assert (await s.get(Runner, rid)).last_seen_at.tzinfo is None

    lista = await _lista(baza, baza.tenant_a, {rid})

    assert lista[0]["last_seen_at"] == "2030-01-01T11:59:30+00:00"


async def test_ostatni_kontakt_podany_w_innej_strefie_jest_zwracany_w_utc(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop")
    plus_dwa = timezone(timedelta(hours=2))

    async with baza.maker() as s:
        runner = await s.get(Runner, rid)
        runner.last_seen_at = datetime(2030, 1, 1, 13, 59, 30, tzinfo=plus_dwa)  # 11:59:30 UTC
        lista = await runnery_zespolu(s, baza.tenant_a, {rid}, TERAZ)

    assert lista[0]["last_seen_at"] == "2030-01-01T11:59:30+00:00"


# --- usluga: kopia backendow, pusty zespol -------------------------------------------------------


async def test_zmiana_zwroconych_backendow_nie_zmienia_danych_runnera(baza):
    rid = await _runner(baza, baza.tenant_a, "laptop",
                        backends=[{"nazwa": "claude", "auth": "api_key"}])
    async with baza.maker() as s:
        lista = await runnery_zespolu(s, baza.tenant_a, {rid}, TERAZ)
        lista[0]["backends"].append({"nazwa": "obcy", "auth": "x"})
        lista[0]["backends"][0]["auth"] = "zepsute"
        runner = (await s.execute(select(Runner).where(Runner.id == rid))).scalar_one()
        assert runner.backends == [{"nazwa": "claude", "auth": "api_key"}]
        ponownie = await runnery_zespolu(s, baza.tenant_a, {rid}, TERAZ)

    assert ponownie[0]["backends"] == [{"nazwa": "claude", "auth": "api_key"}]


async def test_zespol_bez_runnerow_ma_pusta_liste_mimo_runnerow_innego_zespolu(baza):
    obcy = await _runner(baza, baza.tenant_b, "obcy")

    assert await _lista(baza, baza.tenant_a, {obcy}) == []


# --- trasa GET /api/runners ----------------------------------------------------------------------


@pytest.fixture
async def swiat(baza):
    wlasciciel_a, tenant_a = await zaloz_wlasciciela(baza.maker, "a@example.com", nazwa_zespolu="Browarek")
    obserwator_a = await zaloz_czlonka(baza.maker, tenant_a, RolaWZespole.viewer, "obs-a@example.com")
    _, tenant_b = await zaloz_wlasciciela(baza.maker, "b@example.com", nazwa_zespolu="Piekarnia")
    teraz = datetime.now(timezone.utc)
    r_a1 = await _runner(baza, tenant_a, "a-laptop", version="2.0.1", backends=BACKENDY, last_seen_at=teraz)
    r_a2 = await _runner(baza, tenant_a, "a-serwer", version="2.0.0", last_seen_at=teraz)
    r_b = await _runner(baza, tenant_b, "b-laptop", last_seen_at=teraz)
    for rid in (r_a1, r_b):
        bus.mark_runner_connected(rid)
    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
        klienci.append(k)
        return k

    yield SimpleNamespace(maker=baza.maker, nowy_klient=nowy_klient, wlasciciel_a=wlasciciel_a,
                          obserwator_a=obserwator_a, r_a1=r_a1, r_a2=r_a2, r_b=r_b)
    for k in klienci:
        await k.aclose()
    for rid in (r_a1, r_a2, r_b):
        while rid in bus.connected_runner_ids():
            bus.mark_runner_disconnected(rid)
        bus._rozlaczone.pop(rid, None)


async def test_bez_sesji_lista_runnerow_dostaje_401(swiat):
    odp = await swiat.nowy_klient().get("/api/runners")

    assert odp.status_code == 401


async def test_wlasciciel_widzi_tylko_runnery_swojego_zespolu_ze_stanem_z_polaczen(swiat):
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.wlasciciel_a)

    odp = await klient.get("/api/runners")

    assert odp.status_code == 200
    lista = odp.json()
    assert [(r["id"], r["name"], r["online"]) for r in lista] == [
        (swiat.r_a1, "a-laptop", True),    # polaczony
        (swiat.r_a2, "a-serwer", False),   # swiezy heartbeat, ale bez strumienia
    ]
    assert lista[0]["version"] == "2.0.1"
    assert lista[0]["backends"] == BACKENDY
    assert set(lista[0]) == KLUCZE


async def test_obserwator_zespolu_widzi_liste_runnerow(swiat):
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.obserwator_a)

    odp = await klient.get("/api/runners")

    assert odp.status_code == 200
    assert [r["id"] for r in odp.json()] == [swiat.r_a1, swiat.r_a2]


async def test_runner_po_rozlaczeniu_strumienia_jest_na_liscie_jako_offline(swiat):
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.wlasciciel_a)
    przed = {r["id"]: r["online"] for r in (await klient.get("/api/runners")).json()}

    bus.mark_runner_disconnected(swiat.r_a1)
    po = {r["id"]: r["online"] for r in (await klient.get("/api/runners")).json()}

    assert przed[swiat.r_a1] is True
    assert po[swiat.r_a1] is False

"""Lista runow zespolu ze statusem (HAR-72).

Usluga `runy_zespolu` i trasa `GET /api/runs`. SQLite w tmp_path, prawdziwa aplikacja.
Zespol B jest zakladany PIERWSZY, A drugi: odrozniamy "pierwszy w bazie" od "z sesji".
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, RolaWZespole, Run, RunStatus, Tenant
from cockpit.services.runs import LIMIT_LISTY_RUNOW, runy_zespolu
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
KLUCZE = {"id", "workflow", "status", "created_at"}
TOKEN_RUNNERA = "token-runnera-testowy"


@pytest.fixture
async def baza(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lista_runow.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield SimpleNamespace(maker=maker)
    await engine.dispose()


@pytest.fixture
async def swiat(baza):
    wlasciciel_b, tenant_b = await zaloz_wlasciciela(
        baza.maker, "b@example.com", nazwa_zespolu="Piekarnia")   # B pierwszy
    wlasciciel_a, tenant_a = await zaloz_wlasciciela(
        baza.maker, "a@example.com", nazwa_zespolu="Browarek")
    obserwator_a = await zaloz_czlonka(
        baza.maker, tenant_a, RolaWZespole.viewer, "obs-a@example.com")
    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
        klienci.append(k)
        return k

    yield SimpleNamespace(
        maker=baza.maker, nowy_klient=nowy_klient, tenant_a=tenant_a, tenant_b=tenant_b,
        wlasciciel_a=wlasciciel_a, wlasciciel_b=wlasciciel_b, obserwator_a=obserwator_a)
    for k in klienci:
        await k.aclose()


async def _run(swiat, tenant_id, *, id=None, workflow="szkielet", status=RunStatus.pending,
               created_at=T0) -> str:
    async with swiat.maker() as s:
        r = Run(tenant_id=tenant_id, workflow=workflow, status=status, created_at=created_at)
        if id:
            r.id = id
        s.add(r)
        await s.commit()
        return r.id


async def _lista(swiat, tenant_id, **kw) -> list[dict]:
    async with swiat.maker() as s:
        return await runy_zespolu(s, tenant_id, **kw)


async def _wstaw_runy(swiat, tenant_id, ile, prefiks="run", id_odwrotnie=False) -> list[str]:
    """`ile` runow, i-ty utworzony i minut po T0; zwraca id od najstarszego.

    Z `id_odwrotnie` numer w id MALEJE z czasem: najstarszy run ma najwiekszy id.
    """
    async with swiat.maker() as s:
        runy = [Run(tenant_id=tenant_id,
                    id=f"{prefiks}-{(ile - 1 - i if id_odwrotnie else i):03d}",
                    created_at=T0 + timedelta(minutes=i)) for i in range(ile)]
        s.add_all(runy)
        await s.commit()
    return [r.id for r in runy]


# --- usluga: zakres zespolu ----------------------------------------------------------------------


async def test_lista_runow_nie_pokazuje_runow_innego_zespolu(swiat):
    swoj = await _run(swiat, swiat.tenant_a, created_at=T0)
    # obce runy nowsze i starsze od swojego: zadna kolejnosc nie ukryje bledu filtra
    obcy_nowszy = await _run(swiat, swiat.tenant_b, created_at=T0 + timedelta(hours=1))
    obcy_starszy = await _run(swiat, swiat.tenant_b, created_at=T0 - timedelta(hours=1))

    lista_a = await _lista(swiat, swiat.tenant_a)
    lista_b = await _lista(swiat, swiat.tenant_b)

    assert [r["id"] for r in lista_a] == [swoj]
    assert {r["id"] for r in lista_b} == {obcy_nowszy, obcy_starszy}


async def test_zespol_bez_runow_ma_pusta_liste_mimo_runow_innego_zespolu(swiat):
    await _run(swiat, swiat.tenant_b)
    async with swiat.maker() as s:
        pusty = Tenant(name="Pusty")
        s.add(pusty)
        await s.commit()
        pusty_id = pusty.id

    assert await _lista(swiat, pusty_id) == []


async def test_limit_liczy_sie_w_obrebie_zespolu_a_nie_calej_bazy(swiat):
    swoj = await _run(swiat, swiat.tenant_a, created_at=T0 - timedelta(days=1))
    await _wstaw_runy(swiat, swiat.tenant_b, 3, prefiks="obcy")   # wszystkie nowsze

    lista = await _lista(swiat, swiat.tenant_a, limit=2)

    assert [r["id"] for r in lista] == [swoj]


# --- usluga: kolejnosc ---------------------------------------------------------------------------


async def test_runy_sa_od_najnowszego_niezaleznie_od_kolejnosci_zapisu(swiat):
    # zapis w kolejnosci czasu 3, 1, 4, 2 - ani rosnaco, ani malejaco; kolejnosc id
    # (d > c > b > a) jest inna niz kolejnosc czasu, wiec o wyniku decyduje tylko czas
    for nazwa, minuty in [("r-c", 3), ("r-d", 1), ("r-a", 4), ("r-b", 2)]:
        await _run(swiat, swiat.tenant_a, id=nazwa, created_at=T0 + timedelta(minutes=minuty))

    lista = await _lista(swiat, swiat.tenant_a)

    assert [r["id"] for r in lista] == ["r-a", "r-c", "r-b", "r-d"]


async def test_runy_z_ta_sama_chwila_utworzenia_sa_posortowane_po_id_malejaco(swiat):
    for rid in ("b-srodkowy", "a-pierwszy", "c-ostatni"):
        await _run(swiat, swiat.tenant_a, id=rid)

    lista = await _lista(swiat, swiat.tenant_a)

    assert [r["id"] for r in lista] == ["c-ostatni", "b-srodkowy", "a-pierwszy"]


# --- usluga: pola i data -------------------------------------------------------------------------


async def test_pozycja_listy_ma_dokladnie_id_workflow_status_i_date_utworzenia(swiat):
    rid = await _run(swiat, swiat.tenant_a, workflow="przeglad", status=RunStatus.to_accept)

    lista = await _lista(swiat, swiat.tenant_a)

    assert set(lista[0]) == KLUCZE
    assert lista == [{
        "id": rid, "workflow": "przeglad", "status": "to_accept",
        "created_at": "2030-01-01T12:00:00+00:00",
    }]


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


async def test_data_utworzenia_bez_strefy_jest_raportowana_jako_utc_niezaleznie_od_strefy_maszyny(
        swiat, strefa_maszyny_tokio):
    # kontrola zalozenia: strefa lokalna faktycznie rozni sie od UTC
    assert datetime(2030, 1, 1, 12).astimezone(timezone.utc).hour != 12
    rid = await _run(swiat, swiat.tenant_a, created_at=T0)
    async with swiat.maker() as s:   # kontrola zalozenia: SQLite oddaje date bez strefy
        assert (await s.get(Run, rid)).created_at.tzinfo is None

    lista = await _lista(swiat, swiat.tenant_a)

    assert lista[0]["created_at"] == "2030-01-01T12:00:00+00:00"


async def test_data_utworzenia_podana_w_innej_strefie_jest_zwracana_w_utc(swiat):
    rid = await _run(swiat, swiat.tenant_a)
    plus_dwa = timezone(timedelta(hours=2))

    async with swiat.maker() as s:
        run = await s.get(Run, rid)
        run.created_at = datetime(2030, 1, 1, 14, 0, 0, tzinfo=plus_dwa)   # 12:00 UTC
        lista = await runy_zespolu(s, swiat.tenant_a)

    assert lista[0]["created_at"] == "2030-01-01T12:00:00+00:00"


# --- usluga: limit -------------------------------------------------------------------------------


def test_limit_listy_runow_wynosi_sto():
    assert LIMIT_LISTY_RUNOW == 100


async def test_lista_bez_podanego_limitu_zwraca_sto_najnowszych_runow_a_najstarszy_odpada(swiat):
    # id malejace z czasem: najstarszy run ma najwiekszy id
    ids = await _wstaw_runy(swiat, swiat.tenant_a, 101, id_odwrotnie=True)

    lista = await _lista(swiat, swiat.tenant_a)   # bez `limit`

    assert len(lista) == 100
    assert [r["id"] for r in lista] == ids[:0:-1]   # od najnowszego, bez najstarszego
    assert ids[0] not in {r["id"] for r in lista}


async def test_jawny_limit_zwraca_tyle_najnowszych_runow(swiat):
    ids = await _wstaw_runy(swiat, swiat.tenant_a, 5)

    lista = await _lista(swiat, swiat.tenant_a, limit=2)

    assert [r["id"] for r in lista] == [ids[4], ids[3]]


# --- trasa GET /api/runs -------------------------------------------------------------------------


async def test_wlasciciel_widzi_przez_http_tylko_runy_swojego_zespolu_od_najnowszego(swiat):
    # jawne id: starszy run ma WIEKSZY id, wiec o kolejnosci decyduje tylko czas
    stary = await _run(swiat, swiat.tenant_a, id="z-stary", workflow="przeglad",
                       status=RunStatus.to_accept)
    nowy = await _run(swiat, swiat.tenant_a, id="a-nowy", workflow="szkielet",
                      status=RunStatus.running, created_at=T0 + timedelta(hours=1))
    obcy = await _run(swiat, swiat.tenant_b, created_at=T0 + timedelta(hours=2))
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.wlasciciel_a)

    odp = await klient.get("/api/runs")

    assert odp.status_code == 200
    tresc = odp.json()
    assert [r["id"] for r in tresc] == [nowy, stary]
    assert obcy not in {r["id"] for r in tresc}
    assert tresc[1] == {"id": stary, "workflow": "przeglad", "status": "to_accept",
                        "created_at": "2030-01-01T12:00:00+00:00"}
    assert set(tresc[0]) == KLUCZE


async def test_czlonek_pierwszego_w_bazie_zespolu_widzi_tylko_jego_runy(swiat):
    # B zalozony pierwszy: lista zalezna od zespolu z sesji, nie od "pierwszego w bazie"
    swoj_a = await _run(swiat, swiat.tenant_a)
    swoj_b = await _run(swiat, swiat.tenant_b)
    klient_a, klient_b = swiat.nowy_klient(), swiat.nowy_klient()
    await zaloguj_klienta(klient_a, swiat.maker, swiat.wlasciciel_a)
    await zaloguj_klienta(klient_b, swiat.maker, swiat.wlasciciel_b)

    ids_a = [r["id"] for r in (await klient_a.get("/api/runs")).json()]
    ids_b = [r["id"] for r in (await klient_b.get("/api/runs")).json()]

    assert (ids_a, ids_b) == ([swoj_a], [swoj_b])


async def test_obserwator_zespolu_widzi_liste_runow_swojego_zespolu(swiat):
    swoj = await _run(swiat, swiat.tenant_a)
    await _run(swiat, swiat.tenant_b)
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.obserwator_a)

    odp = await klient.get("/api/runs")

    assert odp.status_code == 200
    assert [r["id"] for r in odp.json()] == [swoj]


async def test_zespol_bez_runow_dostaje_przez_http_pusta_liste_a_nie_404(swiat):
    await _run(swiat, swiat.tenant_b)   # baza nie jest pusta - runy ma tylko B
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.wlasciciel_a)

    odp = await klient.get("/api/runs")

    assert (odp.status_code, odp.json()) == (200, [])


async def test_bez_sesji_lista_runow_dostaje_401_i_nie_ujawnia_runow(swiat):
    rid = await _run(swiat, swiat.tenant_a)

    odp = await swiat.nowy_klient().get("/api/runs")

    assert odp.status_code == 401
    assert rid not in odp.text


async def test_token_runnera_bez_sesji_nie_otwiera_listy_runow(swiat):
    await zaloz_runnera(swiat.maker, swiat.tenant_a, "laptop", TOKEN_RUNNERA)
    rid = await _run(swiat, swiat.tenant_a)

    odp = await swiat.nowy_klient().get(
        "/api/runs", headers={"Authorization": f"Bearer {TOKEN_RUNNERA}"})

    assert odp.status_code == 401
    assert rid not in odp.text


async def test_przez_http_przy_101_runach_zwracane_jest_sto_najnowszych(swiat):
    ids = await _wstaw_runy(swiat, swiat.tenant_a, 101, id_odwrotnie=True)
    klient = swiat.nowy_klient()
    await zaloguj_klienta(klient, swiat.maker, swiat.wlasciciel_a)

    odp = await klient.get("/api/runs")

    assert odp.status_code == 200
    assert [r["id"] for r in odp.json()] == ids[:0:-1]

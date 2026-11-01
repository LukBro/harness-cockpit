"""Dostep do API: kazda trasa za sesja lub tokenem runnera, zespol z sesji (HAR-57).

Swiat: dwa zespoly, B zalozony PIERWSZY, A drugi, kazdy z wlasnym runnerem online.
Dzieki temu wynik "pierwszy zespol w bazie" (stara atrapa `domyslny_tenant`) rozni sie
od wyniku "zespol z sesji". Skutki sprawdzamy w bazie, nie tylko w kodzie odpowiedzi.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.routing import Mount, Route

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.api.zaleznosci import kontekst_sesji, runner_z_tokenu, trasa_publiczna
from cockpit.db import session as db_session
from cockpit.db.models import Base, RolaWZespole, Run, RunStatus
from sesja_testowa import (
    HASLO_TESTOWE,
    zaloguj_klienta,
    zaloz_czlonka,
    zaloz_runnera,
    zaloz_wlasciciela,
)
from transport_asgi import TransportASGIStrumieniujacy

LOCALHOST = "http://127.0.0.1"
TOKEN_A, TOKEN_B = "token-zespolu-a", "token-zespolu-b"


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dostep_http.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    wlasciciel_b, tenant_b = await zaloz_wlasciciela(maker, "b@example.com", nazwa_zespolu="Piekarnia")
    wlasciciel_a, tenant_a = await zaloz_wlasciciela(maker, "a@example.com", nazwa_zespolu="Browarek")
    obserwator_a = await zaloz_czlonka(maker, tenant_a, RolaWZespole.viewer, "obserwator-a@example.com")
    czlonek_a = await zaloz_czlonka(maker, tenant_a, RolaWZespole.member, "czlonek-a@example.com")
    runner_a = await zaloz_runnera(maker, tenant_a, "runner-a", TOKEN_A)
    runner_b = await zaloz_runnera(maker, tenant_b, "runner-b", TOKEN_B)

    async with maker() as s:
        run_a = Run(tenant_id=tenant_a, runner_id=runner_a, status=RunStatus.running)
        run_b = Run(tenant_id=tenant_b, runner_id=runner_b, status=RunStatus.running)
        s.add_all([run_a, run_b])
        await s.commit()
        run_a_id, run_b_id = run_a.id, run_b.id

    polaczone = [runner_a, runner_b]
    for rid in polaczone:
        bus.mark_runner_connected(rid)
    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url=LOCALHOST)
        klienci.append(k)
        return k

    yield SimpleNamespace(
        maker=maker, nowy_klient=nowy_klient,
        tenant_a=tenant_a, tenant_b=tenant_b, run_a=run_a_id, run_b=run_b_id,
        runner_a=runner_a, runner_b=runner_b,
        wlasciciel_a=wlasciciel_a, wlasciciel_b=wlasciciel_b,
        obserwator_a=obserwator_a, czlonek_a=czlonek_a,
    )
    for k in klienci:
        await k.aclose()
    for rid in polaczone:
        while rid in bus.connected_runner_ids():
            bus.mark_runner_disconnected(rid)
        getattr(bus, "_rozlaczone", {}).pop(rid, None)
    for rid in (run_a_id, run_b_id):
        bus._run_subscribers.pop(rid, None)
    await engine.dispose()


async def _zalogowany(swiat, user) -> AsyncClient:
    k = swiat.nowy_klient()
    await zaloguj_klienta(k, swiat.maker, user)
    return k


async def _liczba_runow(swiat, tenant_id: str | None = None) -> int:
    async with swiat.maker() as s:
        q = select(func.count()).select_from(Run)
        if tenant_id is not None:
            q = q.where(Run.tenant_id == tenant_id)
        return (await s.execute(q)).scalar_one()


async def _odczyt(klient: AsyncClient, metoda: str, sciezka: str) -> int:
    """Kod odpowiedzi; strumien czytamy tylko do naglowkow i zamykamy (rozlaczenie klienta)."""
    if sciezka.endswith("/stream"):
        async with klient.stream(metoda, sciezka) as odp:
            return odp.status_code
    return (await klient.request(metoda, sciezka, json={} if metoda == "POST" else None)).status_code


TRASY_RUNU = {
    "uruchomienie_runu": ("POST", "/api/runs"),
    "odczyt_runu": ("GET", "/api/runs/{run}"),
    "strumien_runu": ("GET", "/api/runs/{run}/stream"),
}


# --- brak sesji: 401, takze na localhoscie ----------------------------------------------------


@pytest.mark.parametrize("trasa", TRASY_RUNU.values(), ids=TRASY_RUNU.keys())
async def test_bez_sesji_trasy_runow_dostaja_401_takze_na_localhoscie(swiat, trasa):
    metoda, wzor = trasa
    klient = swiat.nowy_klient()
    assert klient.base_url.host == "127.0.0.1"
    przed = await _liczba_runow(swiat)

    # run istnieje, wiec 401 nie da sie pomylic z 404
    assert await _odczyt(klient, metoda, wzor.format(run=swiat.run_a)) == 401
    assert await _liczba_runow(swiat) == przed


async def test_bez_sesji_lista_runnerow_dostaje_401_takze_na_localhoscie(swiat):
    klient = swiat.nowy_klient()
    assert klient.base_url.host == "127.0.0.1"
    assert (await klient.get("/api/runners")).status_code == 401


async def test_token_runnera_nie_zastepuje_sesji_przy_uruchamianiu_runu(swiat):
    przed = await _liczba_runow(swiat)
    odp = await swiat.nowy_klient().post(
        "/api/runs", json={}, headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert odp.status_code == 401
    assert await _liczba_runow(swiat) == przed


# --- izolacja zespolow: cudzy run to 404, wlasny to 200 -----------------------------------------


@pytest.mark.parametrize("wzor", ["/api/runs/{run}", "/api/runs/{run}/stream"],
                         ids=["odczyt_runu", "strumien_runu"])
async def test_czlonek_zespolu_nie_widzi_runu_cudzego_zespolu(swiat, wzor):
    klient = await _zalogowany(swiat, swiat.wlasciciel_a)
    assert await _odczyt(klient, "GET", wzor.format(run=swiat.run_b)) == 404


@pytest.mark.parametrize("wzor", ["/api/runs/{run}", "/api/runs/{run}/stream"],
                         ids=["odczyt_runu", "strumien_runu"])
async def test_czlonek_zespolu_widzi_wlasny_run(swiat, wzor):
    klient = await _zalogowany(swiat, swiat.wlasciciel_a)
    assert await _odczyt(klient, "GET", wzor.format(run=swiat.run_a)) == 200


async def test_odczyt_runu_zwraca_run_zespolu_z_sesji(swiat):
    klient = await _zalogowany(swiat, swiat.wlasciciel_b)
    odp = await klient.get(f"/api/runs/{swiat.run_b}")
    assert odp.status_code == 200
    assert odp.json()["id"] == swiat.run_b


async def test_strumien_cudzego_runu_nie_zostawia_subskrypcji(swiat):
    klient = await _zalogowany(swiat, swiat.wlasciciel_a)
    assert await _odczyt(klient, "GET", f"/api/runs/{swiat.run_b}/stream") == 404
    assert bus._run_subscribers.get(swiat.run_b, []) == []


async def test_lista_runnerow_zawiera_tylko_runnery_zespolu_z_sesji(swiat):
    klient = await _zalogowany(swiat, swiat.wlasciciel_a)

    odp = await klient.get("/api/runners")

    assert odp.status_code == 200
    assert [r["id"] for r in odp.json()] == [swiat.runner_a]


# --- zespol runu pochodzi z sesji -------------------------------------------------------------


async def test_run_uruchomiony_przez_czlonka_powstaje_w_jego_zespole_nie_w_pierwszym_w_bazie(swiat):
    klient = await _zalogowany(swiat, swiat.wlasciciel_a)
    przed_b = await _liczba_runow(swiat, swiat.tenant_b)

    odp = await klient.post("/api/runs", json={})

    assert odp.status_code == 200
    async with swiat.maker() as s:
        run = (await s.execute(select(Run).where(Run.id == odp.json()["run_id"]))).scalar_one()
    assert run.tenant_id == swiat.tenant_a
    assert run.runner_id == swiat.runner_a
    assert await _liczba_runow(swiat, swiat.tenant_b) == przed_b


async def test_czlonek_zespolu_uruchamia_run_i_run_powstaje(swiat):
    klient = await _zalogowany(swiat, swiat.czlonek_a)
    przed = await _liczba_runow(swiat, swiat.tenant_a)

    odp = await klient.post("/api/runs", json={})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat, swiat.tenant_a) == przed + 1


async def test_obserwator_nie_uruchamia_runu_i_run_nie_powstaje(swiat):
    klient = await _zalogowany(swiat, swiat.obserwator_a)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={})

    # runner zespolu A jest online, wiec 403 to odmowa roli, nie brak runnera (409)
    assert odp.status_code == 403
    assert await _liczba_runow(swiat) == przed


async def test_obserwator_widzi_run_swojego_zespolu(swiat):
    klient = await _zalogowany(swiat, swiat.obserwator_a)
    assert (await klient.get(f"/api/runs/{swiat.run_a}")).status_code == 200


# --- trasy publiczne dzialaja bez sesji -------------------------------------------------------


async def test_trasy_publiczne_dzialaja_bez_sesji(swiat):
    klient = swiat.nowy_klient()
    assert (await klient.get("/api/health")).status_code == 200
    odp = await klient.post("/api/konto/logowanie",
                            json={"email": "a@example.com", "haslo": HASLO_TESTOWE})
    assert odp.status_code == 200
    odp = await klient.post("/api/konto/rejestracja",
                            json={"email": "nowy@example.com", "haslo": HASLO_TESTOWE})
    assert odp.status_code == 201


# --- trasy runnera: token, nie sesja ---------------------------------------------------------

TRASY_RUNNERA = {
    "meldunek": ("POST", "/api/runners/register", {"name": "x"}),
    "heartbeat": ("POST", "/api/runners/heartbeat", None),
    "odpowiedz": ("POST", "/api/runners/odpowiedzi", {"req_id": "r", "ok": True}),
    "zdarzenie": ("POST", "/api/events",
                  {"run_id": "x", "event_id": "e", "step_id": "s", "kind": "log"}),
    "strumien_polecen": ("GET", "/api/runners/stream", None),
}


async def _wywolaj(klient, metoda, sciezka, cialo, naglowki=None) -> int:
    if sciezka.endswith("/stream"):
        async with klient.stream(metoda, sciezka, headers=naglowki) as odp:
            return odp.status_code
    return (await klient.request(metoda, sciezka, json=cialo, headers=naglowki)).status_code


@pytest.mark.parametrize("trasa", TRASY_RUNNERA.values(), ids=TRASY_RUNNERA.keys())
async def test_trasa_runnera_bez_tokenu_dostaje_401_nawet_z_sesja_uzytkownika(swiat, trasa):
    metoda, sciezka, cialo = trasa
    klient = await _zalogowany(swiat, swiat.wlasciciel_a)
    assert await _wywolaj(klient, metoda, sciezka, cialo) == 401


async def test_heartbeat_z_tokenem_runnera_dziala_a_z_nieznanym_tokenem_nie(swiat):
    klient = swiat.nowy_klient()
    zly = await _wywolaj(klient, "POST", "/api/runners/heartbeat", None,
                         {"Authorization": "Bearer nieznany"})
    dobry = await _wywolaj(klient, "POST", "/api/runners/heartbeat", None,
                           {"Authorization": f"Bearer {TOKEN_A}"})
    assert (zly, dobry) == (401, 200)


# --- test wyliczajacy: zadna trasa /api/ bez zabezpieczenia -------------------------------------

ZNACZNIKI = {
    "kontekst_sesji": kontekst_sesji,
    "runner_z_tokenu": runner_z_tokenu,
    "trasa_publiczna": trasa_publiczna,
}


def _wywolania(zaleznosc) -> set:
    wynik = set()
    for d in zaleznosc.dependencies:
        wynik.add(d.call)
        wynik |= _wywolania(d)
    return wynik


def _znaczniki_trasy(route: APIRoute) -> set[str]:
    wywolania = _wywolania(route.dependant)
    return {nazwa for nazwa, wywolanie in ZNACZNIKI.items() if wywolanie in wywolania}


def _trasy_aplikacji(trasy) -> list[APIRoute]:
    """Plaska lista `APIRoute`; schodzi do routerow dolaczonych (`original_router`).

    FastAPI nie splaszcza `include_router` do `app.routes`. Element nierozpoznany to blad,
    nie pominiecie: pominiecie dalo juz raz falszywie zielony wynik (evidence_required).
    """
    wynik: list[APIRoute] = []
    for route in trasy:
        if isinstance(route, APIRoute):
            wynik.append(route)
        elif hasattr(route, "original_router"):
            wynik.extend(_trasy_aplikacji(route.original_router.routes))
        elif isinstance(route, (Route, Mount)):
            continue  # docs/openapi i front statyczny, poza /api/
        else:
            pytest.fail(
                f"Wyliczenie tras nie rozpoznaje elementu {type(route).__name__} "
                f"({getattr(route, 'path', '?')}); nie wolno go pominac po cichu"
            )
    return wynik


def _trasy_api(aplikacja: FastAPI = app) -> dict[tuple[str, str], set[str]]:
    wynik = {}
    for route in _trasy_aplikacji(aplikacja.routes):
        if route.path.startswith("/api/"):
            for metoda in route.methods:
                wynik[(metoda, route.path)] = _znaczniki_trasy(route)
    return wynik


def test_kazda_trasa_api_ma_sesje_token_runnera_albo_jawna_publicznosc():
    bez_zabezpieczenia = sorted(t for t, z in _trasy_api().items() if not z)
    assert bez_zabezpieczenia == []


def test_wyliczenie_tras_obejmuje_znane_trasy_i_odroznia_zabezpieczenia():
    trasy = _trasy_api()
    oczekiwane = {
        ("GET", "/api/health"): {"trasa_publiczna"},
        ("POST", "/api/konto/rejestracja"): {"trasa_publiczna"},
        ("POST", "/api/konto/logowanie"): {"trasa_publiczna"},
        ("POST", "/api/events"): {"runner_z_tokenu"},
        ("POST", "/api/runners/register"): {"runner_z_tokenu"},
        ("POST", "/api/runners/heartbeat"): {"runner_z_tokenu"},
        ("POST", "/api/runners/odpowiedzi"): {"runner_z_tokenu"},
        ("GET", "/api/runners/stream"): {"runner_z_tokenu"},
        ("GET", "/api/runners"): {"kontekst_sesji"},
        ("GET", "/api/runs"): {"kontekst_sesji"},
        ("POST", "/api/runs"): {"kontekst_sesji"},
        ("GET", "/api/runs/{run_id}"): {"kontekst_sesji"},
        ("GET", "/api/runs/{run_id}/stream"): {"kontekst_sesji"},
        ("GET", "/api/konto/ja"): {"kontekst_sesji"},
        ("GET", "/api/pliki"): {"kontekst_sesji"},
        ("GET", "/api/pliki/lista"): {"kontekst_sesji"},
        ("PUT", "/api/pliki"): {"kontekst_sesji"},
        ("GET", "/api/katalog"): {"kontekst_sesji"},
        ("POST", "/api/katalog/odswiez"): {"kontekst_sesji"},
        ("POST", "/api/konto/wyloguj"): {"kontekst_sesji"},
        ("POST", "/api/konto/wyloguj-wszedzie"): {"kontekst_sesji"},
        ("POST", "/api/konto/haslo"): {"kontekst_sesji"},
    }
    assert {k: trasy.get(k) for k in oczekiwane} == oczekiwane


def test_wyliczone_trasy_api_to_dokladnie_znany_zbior_nowa_trasa_wymusza_aktualizacje():
    assert set(_trasy_api()) == {
        ("GET", "/api/health"),
        ("POST", "/api/runners/register"),
        ("POST", "/api/runners/heartbeat"),
        ("POST", "/api/runners/odpowiedzi"),
        ("GET", "/api/runners/stream"),
        ("GET", "/api/runners"),
        ("GET", "/api/runs"),
        ("POST", "/api/runs"),
        ("POST", "/api/events"),
        ("GET", "/api/runs/{run_id}"),
        ("GET", "/api/runs/{run_id}/stream"),
        ("GET", "/api/pliki"),
        ("GET", "/api/pliki/lista"),
        ("PUT", "/api/pliki"),
        ("GET", "/api/katalog"),
        ("POST", "/api/katalog/odswiez"),
        ("POST", "/api/konto/rejestracja"),
        ("POST", "/api/konto/logowanie"),
        ("GET", "/api/konto/ja"),
        ("POST", "/api/konto/wyloguj"),
        ("POST", "/api/konto/wyloguj-wszedzie"),
        ("POST", "/api/konto/haslo"),
    }


def test_detektor_widzi_trase_bez_zabezpieczenia_na_dolaczonym_routerze():
    probna = FastAPI()
    router = APIRouter(prefix="/api/x")

    @router.get("/otwarta")
    async def otwarta() -> dict:
        return {}

    probna.include_router(router)

    trasy = _trasy_api(probna)

    assert trasy == {("GET", "/api/x/otwarta"): set()}


def test_wyliczenie_tras_oblewa_na_nierozpoznanym_elemencie_zamiast_go_pominac():
    class Obcy:
        path = "/api/obcy"

    with pytest.raises(pytest.fail.Exception, match="Obcy"):
        _trasy_aplikacji([Obcy()])


def test_detektor_zabezpieczen_widzi_brak_zabezpieczenia_na_trasie_bez_zaleznosci():
    probna = FastAPI()

    @probna.get("/api/otwarta")
    async def otwarta() -> dict:
        return {}

    route = next(r for r in probna.routes if isinstance(r, APIRoute) and r.path == "/api/otwarta")
    assert _znaczniki_trasy(route) == set()

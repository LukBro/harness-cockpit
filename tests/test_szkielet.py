"""Nazwy testow w jezyku dziedziny, nie technologii (BRO-95)."""
from __future__ import annotations

import hashlib
import importlib.util
import stat
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Runner, Tenant
from cockpit.settings import settings
from runner import secrets


@asynccontextmanager
async def _kokpit_z_runnerem(tmp_path, monkeypatch, last_seen_at):
    """Zespol "Browarek" z jednym runnerem; last_seen_at decyduje, czy runner byl widziany."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        t = Tenant(name="Browarek")
        s.add(t)
        await s.flush()
        s.add(Runner(tenant_id=t.id, name="macbook",
                     token_hash=hashlib.sha256(b"tajne").hexdigest(),
                     last_seen_at=last_seen_at))
        await s.commit()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await engine.dispose()


@pytest.fixture
async def klient(tmp_path, monkeypatch):
    """Runner swiezo zameldowany, czyli online."""
    async with _kokpit_z_runnerem(tmp_path, monkeypatch,
                                  datetime.now(timezone.utc)) as c:
        yield c


@pytest.fixture
async def klient_runner_offline(tmp_path, monkeypatch):
    """Runner, ktory nigdy sie nie zameldowal."""
    async with _kokpit_z_runnerem(tmp_path, monkeypatch, None) as c:
        yield c


@pytest.fixture
async def klient_runner_dawno_nie_widziany(tmp_path, monkeypatch):
    prog = settings.runner_offline_after_s
    async with _kokpit_z_runnerem(
        tmp_path, monkeypatch,
        datetime.now(timezone.utc) - timedelta(seconds=prog * 10),
    ) as c:
        yield c


@pytest.fixture
def polaczenia_runnerow():
    """Ewidencja polaczen to stan globalny modulu - po ttescie sprzatamy to, co dopisal."""
    zanotowane: list[str] = []
    yield zanotowane
    for runner_id in zanotowane:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        # ewidencja rozlaczen (HAR-2, rozlaczenie wygrywa) to tez stan globalny
        getattr(bus, "_rozlaczone", {}).pop(runner_id, None)


async def _id_runnera() -> str:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Runner.id))).scalars().one()


async def test_kokpit_odpowiada_ze_zyje(klient):
    r = await klient.get("/api/health")
    assert r.json() == {"status": "ok"}


async def test_runner_bez_tokenu_dostaje_odmowe(klient):
    r = await klient.post("/api/runners/register", json={"name": "x"})
    assert r.status_code == 401


async def test_runner_z_niewaznym_tokenem_dostaje_odmowe_z_podpowiedzia(klient):
    r = await klient.post("/api/runners/register", json={"name": "x"},
                          headers={"Authorization": "Bearer nieznany"})
    assert r.status_code == 401
    assert "Ustawieniach" in r.json()["detail"]


async def test_runner_melduje_sie_tokenem_i_dostaje_identyfikator(klient):
    r = await klient.post("/api/runners/register", json={"name": "macbook"},
                          headers={"Authorization": "Bearer tajne"})
    assert r.status_code == 200
    assert r.json()["runner_id"]


async def test_uruchomienie_tworzy_run_w_stanie_w_toku(klient):
    r = await klient.post("/api/runs", json={"workflow": "szkielet"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    szczegoly = (await klient.get(f"/api/runs/{run_id}")).json()
    assert szczegoly["status"] == "running"
    assert szczegoly["events"] == []


async def test_zdarzenia_dostaja_kolejne_numery(klient):
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    h = {"Authorization": "Bearer tajne"}
    a = await klient.post("/api/events", json={"run_id": run_id, "kind": "output",
                                               "text": "pierwsze"}, headers=h)
    b = await klient.post("/api/events", json={"run_id": run_id, "kind": "output",
                                               "text": "drugie"}, headers=h)
    assert (a.json()["seq"], b.json()["seq"]) == (1, 2)


async def test_brak_pomiaru_kosztu_zapisuje_sie_jako_brak_nie_jako_zero(klient):
    """BRO-87: zero znaczy zmierzone zero. Brak pomiaru to None."""
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    await klient.post("/api/events",
                      json={"run_id": run_id, "kind": "step_finished"},
                      headers={"Authorization": "Bearer tajne"})
    async with db_session.SessionLocal() as s:
        from cockpit.db.models import Event
        ev = (await s.execute(select(Event))).scalars().one()
    assert ev.cost_usd is None
    assert ev.cost_source == "unavailable"


async def test_run_zakonczony_sukcesem_ma_stan_do_odbioru_nie_gotowe(klient):
    """BRO-108: odbior jest czynnoscia czlowieka."""
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    await klient.post("/api/events",
                      json={"run_id": run_id, "kind": "step_finished",
                            "status": "to_accept"},
                      headers={"Authorization": "Bearer tajne"})
    assert (await klient.get(f"/api/runs/{run_id}")).json()["status"] == "to_accept"


async def test_zapis_zera_przy_braku_pomiaru_jest_odrzucany(klient):
    """BRO-87, niezmiennik measured_or_null: zero znaczy ZMIERZONE zero."""
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    r = await klient.post(
        "/api/events",
        json={"run_id": run_id, "kind": "step_finished", "cost_usd": 0.0,
              "cost_source": "unavailable"},
        headers={"Authorization": "Bearer tajne"},
    )
    assert r.status_code == 422
    assert "measured_or_null" in r.json()["detail"]


# --- HAR-2: run powstaje tylko przy runnerze online ---

async def test_uruchomienie_przy_runnerze_offline_konczy_sie_odmowa_z_podpowiedzia(
    klient_runner_offline,
):
    r = await klient_runner_offline.post("/api/runs", json={"workflow": "szkielet"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail.startswith("runner offline")
    assert "uruchom runnera" in detail


async def test_uruchomienie_przy_runnerze_online_tworzy_run(klient):
    r = await klient.post("/api/runs", json={"workflow": "szkielet"})
    assert r.status_code == 200
    assert r.json()["run_id"]


async def _zespol_z_runnerami(runnery: list[tuple[str, bool]]) -> None:
    """Podmienia runnery zespolu na podane (id, online); wstawia w podanej kolejnosci."""
    from cockpit.db.models import Runner as R
    async with db_session.SessionLocal() as s:
        tenant_id = (await s.execute(select(Tenant.id))).scalars().one()
        await s.execute(delete(R))
        for rid, online in runnery:
            s.add(R(id=rid, tenant_id=tenant_id, name=rid,
                    token_hash=hashlib.sha256(rid.encode()).hexdigest(),
                    last_seen_at=datetime.now(timezone.utc) if online else None))
        await s.commit()


async def _runy():
    from cockpit.db.models import Run
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run))).scalars().all()


async def test_run_trafia_do_runnera_online_gdy_wczesniejszy_runner_zespolu_jest_offline(
    klient_runner_offline,
):
    await _zespol_z_runnerami([("a-offline", False), ("b-online", True)])
    r = await klient_runner_offline.post("/api/runs", json={"workflow": "przeglad-kodu"})
    assert r.status_code == 200
    run = (await _runy())[0]
    assert run.id == r.json()["run_id"]
    assert run.runner_id == "b-online"
    assert run.workflow == "przeglad-kodu"


async def test_run_trafia_do_runnera_o_najmniejszym_identyfikatorze_gdy_online_jest_kilku(
    klient_runner_offline,
):
    # wstawieni w odwrotnej kolejnosci niz id - kolejnosc wstawienia nie moze decydowac
    await _zespol_z_runnerami([("c-online", True), ("b-online", True), ("a-online", True)])
    r = await klient_runner_offline.post("/api/runs", json={})
    assert r.status_code == 200
    assert (await _runy())[0].runner_id == "a-online"


@pytest.fixture
def kolejki_runnerow():
    """Kolejki polecen runnerow to stan globalny modulu - sprzatamy te, ktore powstaly w tescie."""
    przed = set(bus._runner_queues)
    yield bus._runner_queues
    for runner_id in set(bus._runner_queues) - przed:
        bus._runner_queues.pop(runner_id, None)


async def _obcy_zespol_z_runnerem(runner_id: str, online: bool) -> str:
    """Doklada DRUGI zespol (po domyslnym "Browarek") z jednym runnerem; zwraca jego id."""
    async with db_session.SessionLocal() as s:
        obcy = Tenant(name="Obcy")
        s.add(obcy)
        await s.flush()
        s.add(Runner(id=runner_id, tenant_id=obcy.id, name=runner_id,
                     token_hash=hashlib.sha256(runner_id.encode()).hexdigest(),
                     last_seen_at=datetime.now(timezone.utc) if online else None))
        await s.commit()
        return obcy.id


async def test_zespol_bez_wlasnego_runnera_online_dostaje_odmowe_mimo_runnera_online_innego_zespolu(
    klient_runner_offline, kolejki_runnerow,
):
    # Browarek (domyslny, wstawiony pierwszy) ma tylko runnera offline;
    # obcy zespol ma runnera online o mniejszym id
    await _obcy_zespol_z_runnerem("0-obcy-online", online=True)
    r = await klient_runner_offline.post("/api/runs", json={"workflow": "szkielet"})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("runner offline")
    assert await _runy() == []
    assert "0-obcy-online" not in kolejki_runnerow


async def test_run_zespolu_trafia_do_jego_runnera_a_nie_do_runnera_obcego_zespolu_o_mniejszym_id(
    klient, kolejki_runnerow,
):
    wlasny = await _id_runnera()
    await _obcy_zespol_z_runnerem("0-obcy-online", online=True)
    assert "0-obcy-online" < wlasny
    r = await klient.post("/api/runs", json={"workflow": "szkielet"})
    assert r.status_code == 200
    run = (await _runy())[0]
    assert run.runner_id == wlasny
    assert kolejki_runnerow[wlasny].qsize() == 1
    assert "0-obcy-online" not in kolejki_runnerow


async def test_run_dla_jawnie_wskazanego_zespolu_wybiera_tylko_runnery_tego_zespolu(
    klient_runner_offline,
):
    from cockpit.services.errors import StanNiemozliwy
    from cockpit.services.runs import utworz_run
    async with db_session.SessionLocal() as s:
        browarek = (await s.execute(select(Tenant.id))).scalars().one()
    obcy = await _obcy_zespol_z_runnerem("0-obcy-online", online=True)
    async with db_session.SessionLocal() as s:
        with pytest.raises(StanNiemozliwy, match="runner offline"):
            await utworz_run(s, browarek, "szkielet")
        run = await utworz_run(s, obcy, "szkielet")
    assert run.runner_id == "0-obcy-online"
    assert run.tenant_id == obcy
    assert len(await _runy()) == 1


async def test_odmowa_przy_runnerze_offline_nie_zostawia_runu_w_bazie(
    klient_runner_offline,
):
    await klient_runner_offline.post("/api/runs", json={})
    from cockpit.db.models import Run
    async with db_session.SessionLocal() as s:
        liczba = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
    assert liczba == 0


async def test_runner_widziany_dawniej_niz_prog_jest_offline(
    klient_runner_dawno_nie_widziany,
):
    r = await klient_runner_dawno_nie_widziany.post("/api/runs", json={})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("runner offline")


async def test_runner_po_zameldowaniu_jest_online(klient_runner_offline):
    assert (await klient_runner_offline.post("/api/runs", json={})).status_code == 409
    zameldowanie = await klient_runner_offline.post(
        "/api/runners/register", json={"name": "macbook"},
        headers={"Authorization": "Bearer tajne"})
    assert zameldowanie.status_code == 200
    assert (await klient_runner_offline.post("/api/runs", json={})).status_code == 200


async def test_runner_z_otwartym_strumieniem_jest_online_mimo_braku_zameldowania(
    klient_runner_offline, polaczenia_runnerow,
):
    runner_id = await _id_runnera()
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    assert (await klient_runner_offline.post("/api/runs", json={})).status_code == 200


async def test_runner_po_rozlaczeniu_strumienia_znowu_jest_offline(
    klient_runner_offline, polaczenia_runnerow,
):
    runner_id = await _id_runnera()
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_disconnected(runner_id)
    r = await klient_runner_offline.post("/api/runs", json={})
    assert r.status_code == 409


async def test_runner_z_dwoma_strumieniami_zostaje_online_po_zamknieciu_jednego(
    klient_runner_offline, polaczenia_runnerow,
):
    runner_id = await _id_runnera()
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_disconnected(runner_id)
    assert (await klient_runner_offline.post("/api/runs", json={})).status_code == 200


async def test_runner_swiezo_zameldowany_ktory_zamknal_strumien_jest_od_razu_offline(
    klient, polaczenia_runnerow,
):
    """Rozlaczenie wygrywa ze swiezym zameldowaniem - bez czekania na prog."""
    zameldowanie = await klient.post(
        "/api/runners/register", json={"name": "macbook"},
        headers={"Authorization": "Bearer tajne"})
    assert zameldowanie.status_code == 200
    runner_id = zameldowanie.json()["runner_id"]
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    assert (await klient.post("/api/runs", json={})).status_code == 200
    bus.mark_runner_disconnected(runner_id)
    r = await klient.post("/api/runs", json={})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("runner offline")


async def test_odmowa_po_zamknieciu_strumienia_nie_zostawia_nowego_runu(
    klient, polaczenia_runnerow,
):
    from cockpit.db.models import Run
    runner_id = await _id_runnera()
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_disconnected(runner_id)
    assert (await klient.post("/api/runs", json={})).status_code == 409
    async with db_session.SessionLocal() as s:
        liczba = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
    assert liczba == 0


async def test_runner_ktory_po_rozlaczeniu_zameldowal_sie_ponownie_znowu_jest_online(
    klient, polaczenia_runnerow,
):
    import asyncio
    runner_id = await _id_runnera()
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_disconnected(runner_id)
    assert (await klient.post("/api/runs", json={})).status_code == 409
    await asyncio.sleep(0.01)
    zameldowanie = await klient.post(
        "/api/runners/register", json={"name": "macbook"},
        headers={"Authorization": "Bearer tajne"})
    assert zameldowanie.status_code == 200
    assert (await klient.post("/api/runs", json={})).status_code == 200


async def test_runner_z_dwoma_strumieniami_zamknietymi_oba_jest_offline(
    klient, polaczenia_runnerow,
):
    runner_id = await _id_runnera()
    polaczenia_runnerow.append(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_connected(runner_id)
    bus.mark_runner_disconnected(runner_id)
    assert (await klient.post("/api/runs", json={})).status_code == 200
    bus.mark_runner_disconnected(runner_id)
    assert (await klient.post("/api/runs", json={})).status_code == 409


def test_ewidencja_zapamietuje_chwile_rozlaczenia_dopiero_po_zamknieciu_ostatniego_strumienia(
    polaczenia_runnerow,
):
    polaczenia_runnerow.append("r-ewidencja")
    przed = datetime.now(timezone.utc)
    bus.mark_runner_connected("r-ewidencja")
    bus.mark_runner_connected("r-ewidencja")
    bus.mark_runner_disconnected("r-ewidencja")
    assert "r-ewidencja" not in bus.disconnected_runners()
    bus.mark_runner_disconnected("r-ewidencja")
    chwila = bus.disconnected_runners()["r-ewidencja"]
    assert przed <= chwila <= datetime.now(timezone.utc)


def test_rozlaczenie_pozniejsze_niz_zameldowanie_oznacza_offline():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    runner = Runner(id="r1", last_seen_at=teraz - timedelta(seconds=5))
    rozlaczone = {"r1": teraz - timedelta(seconds=2)}
    assert runner_online(runner, frozenset(), teraz, 60, rozlaczone) is False


def test_zameldowanie_pozniejsze_niz_rozlaczenie_oznacza_online():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    runner = Runner(id="r1", last_seen_at=teraz - timedelta(seconds=2))
    rozlaczone = {"r1": teraz - timedelta(seconds=5)}
    assert runner_online(runner, frozenset(), teraz, 60, rozlaczone) is True


def test_rozlaczony_runner_nigdy_niezameldowany_jest_offline():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    runner = Runner(id="r1", last_seen_at=None)
    assert runner_online(runner, frozenset(), teraz, 60, {"r1": teraz}) is False


def test_otwarty_strumien_wygrywa_z_wczesniejszym_rozlaczeniem():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    runner = Runner(id="r1", last_seen_at=teraz - timedelta(seconds=5))
    rozlaczone = {"r1": teraz - timedelta(seconds=1)}
    assert runner_online(runner, {"r1"}, teraz, 60, rozlaczone) is True


def test_rozlaczenie_innego_runnera_nie_zmienia_stanu_tego_runnera():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    runner = Runner(id="r1", last_seen_at=teraz - timedelta(seconds=5))
    assert runner_online(runner, frozenset(), teraz, 60, {"r2": teraz}) is True


def test_czas_rozlaczenia_bez_strefy_nie_wywraca_reguly_online():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    runner = Runner(id="r1", last_seen_at=(teraz - timedelta(seconds=5)).replace(tzinfo=None))
    pozniej = {"r1": (teraz - timedelta(seconds=2)).replace(tzinfo=None)}
    wczesniej = {"r1": (teraz - timedelta(seconds=9)).replace(tzinfo=None)}
    assert runner_online(runner, frozenset(), teraz, 60, pozniej) is False
    assert runner_online(runner, frozenset(), teraz, 60, wczesniej) is True


def test_rozlaczenie_nie_psuje_starej_reguly_gdy_nie_podano_ewidencji():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    swiezy = Runner(id="r1", last_seen_at=teraz - timedelta(seconds=1))
    assert runner_online(swiezy, frozenset(), teraz, 60) is True
    assert runner_online(swiezy, frozenset(), teraz, 60, None) is True


def test_czas_zameldowania_bez_strefy_nie_wywraca_reguly_online():
    """SQLite oddaje daty bez strefy - traktujemy je jako UTC."""
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    swiezy = Runner(id="r1", last_seen_at=(teraz - timedelta(seconds=1)).replace(tzinfo=None))
    stary = Runner(id="r2", last_seen_at=(teraz - timedelta(hours=1)).replace(tzinfo=None))
    assert runner_online(swiezy, frozenset(), teraz, 60) is True
    assert runner_online(stary, frozenset(), teraz, 60) is False


def test_runner_nigdy_niewidziany_jest_offline_chyba_ze_ma_strumien():
    from cockpit.services.runs import runner_online
    teraz = datetime.now(timezone.utc)
    assert runner_online(Runner(id="r1", last_seen_at=None), frozenset(), teraz, 60) is False
    assert runner_online(Runner(id="r1", last_seen_at=None), {"r1"}, teraz, 60) is True


# --- HAR-2: sekrety runnera ---

def test_zapis_tokenu_daje_plik_dostepny_tylko_dla_wlasciciela(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "kat" / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    secrets.set_token("runner", "sekret-1")
    assert stat.S_IMODE(secrets.CONFIG.stat().st_mode) == 0o600
    assert secrets.get_token("runner") == "sekret-1"


def test_zapis_tokenu_zaweza_prawa_pliku_ktory_byl_czytelny_dla_innych(
    tmp_path, monkeypatch,
):
    plik = tmp_path / "credentials.json"
    plik.write_text("{}")
    plik.chmod(0o644)
    monkeypatch.setattr(secrets, "CONFIG", plik)
    secrets.set_token("runner", "sekret-1")
    assert stat.S_IMODE(plik.stat().st_mode) == 0o600


def test_zapis_tokenu_zachowuje_pozostale_wpisy(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    monkeypatch.delenv("COCKPIT_INNY_TOKEN", raising=False)
    secrets.set_token("inny", "a")
    secrets.set_token("runner", "b")
    assert secrets.get_token("inny") == "a"
    assert secrets.get_token("runner") == "b"


# --- HAR-2: zasiew srodowiska developerskiego ---

def _zaladuj_seed_dev():
    sciezka = Path(__file__).resolve().parent.parent / "tools" / "seed_dev.py"
    spec = importlib.util.spec_from_file_location("seed_dev", sciezka)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture
async def baza_do_zasiewu(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "cfg" / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'seed.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_zasiew_zapisuje_token_w_pliku_0600_a_w_bazie_tylko_jego_skrot(
    baza_do_zasiewu,
):
    from cockpit.services.runs import hash_token
    sciezka = await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)
    assert sciezka == secrets.CONFIG
    assert stat.S_IMODE(sciezka.stat().st_mode) == 0o600
    token = secrets.get_token("runner")
    assert token
    async with baza_do_zasiewu() as s:
        runner = (await s.execute(select(Runner))).scalars().one()
    assert runner.token_hash == hash_token(token)
    assert runner.token_hash != token


def test_uruchomienie_zasiewu_z_wiersza_polecen_nie_wypisuje_tokenu_na_ekran(
    tmp_path, monkeypatch, capsys,
):
    import asyncio
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "cfg" / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    url = f"sqlite+aiosqlite:///{tmp_path/'main.db'}"

    async def schemat():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(schemat())
    monkeypatch.setattr(settings, "database_url", url)
    _zaladuj_seed_dev().main()
    token = secrets.get_token("runner")
    wyjscie = capsys.readouterr()
    assert token and len(token) >= 16
    assert str(secrets.CONFIG) in wyjscie.out        # main faktycznie cos wypisal
    assert token not in wyjscie.out
    assert token not in wyjscie.err


async def test_ponowny_zasiew_nie_zaklada_drugiego_zespolu_ani_runnera(
    baza_do_zasiewu,
):
    from cockpit.services.runs import hash_token
    seed = _zaladuj_seed_dev()
    await seed.zasiej(baza_do_zasiewu)
    await seed.zasiej(baza_do_zasiewu)
    async with baza_do_zasiewu() as s:
        zespoly = (await s.execute(select(func.count()).select_from(Tenant))).scalar_one()
        runnery = (await s.execute(select(Runner))).scalars().all()
    assert zespoly == 1
    assert len(runnery) == 1
    # token w pliku nadal pasuje do skrotu w bazie - runner moze sie zalogowac
    assert runnery[0].token_hash == hash_token(secrets.get_token("runner"))


# --- HAR-2: front serwowany z kokpitu ---

def _aplikacja_z_frontem(katalog: Path) -> tuple[FastAPI, bool]:
    from cockpit.api.app import zamontuj_front
    aplikacja = FastAPI()

    @aplikacja.get("/api/health")
    async def health():
        return {"status": "ok"}

    return aplikacja, zamontuj_front(aplikacja, katalog)


async def test_front_jest_serwowany_pod_adresem_glownym(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>Kokpit</body></html>")
    aplikacja, zamontowano = _aplikacja_z_frontem(tmp_path)
    assert zamontowano is True
    async with AsyncClient(transport=ASGITransport(app=aplikacja),
                           base_url="http://test") as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "Kokpit" in r.text


async def test_adresy_api_maja_pierwszenstwo_przed_frontem(tmp_path):
    (tmp_path / "index.html").write_text("<html>Kokpit</html>")
    aplikacja, _ = _aplikacja_z_frontem(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=aplikacja),
                           base_url="http://test") as c:
        r = await c.get("/api/health")
    assert r.json() == {"status": "ok"}


async def test_brak_zbudowanego_frontu_nie_psuje_kokpitu(tmp_path):
    aplikacja, zamontowano = _aplikacja_z_frontem(tmp_path / "nie-ma-takiego")
    assert zamontowano is False
    async with AsyncClient(transport=ASGITransport(app=aplikacja),
                           base_url="http://test") as c:
        assert (await c.get("/api/health")).json() == {"status": "ok"}


def _prawdziwa_aplikacja_z_frontem(tmp_path, monkeypatch):
    """Wykonuje PRAWDZIWY kod src/cockpit/api/app.py (kopia pliku), ktorego STATIC
    wskazuje na tmp_path/static. Modul nie trafia do globalnego stanu po tescie."""
    zrodlo = Path(__file__).resolve().parent.parent / "src" / "cockpit" / "api" / "app.py"
    kopia = tmp_path / "api" / "app.py"
    kopia.parent.mkdir()
    kopia.write_text(zrodlo.read_text())
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html>Kokpit z buildu</html>")
    nazwa = "cockpit.api._app_z_frontem"
    spec = importlib.util.spec_from_file_location(nazwa, kopia)
    modul = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, nazwa, modul)
    spec.loader.exec_module(modul)
    assert modul.STATIC == tmp_path / "static"
    return modul.app


async def test_prawdziwy_kokpit_oddaje_api_jako_json_a_front_pod_adresem_glownym(
    tmp_path, monkeypatch,
):
    aplikacja = _prawdziwa_aplikacja_z_frontem(tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=aplikacja),
                           base_url="http://test") as c:
        zdrowie = await c.get("/api/health")
        glowna = await c.get("/")
    assert zdrowie.json() == {"status": "ok"}
    assert "Kokpit z buildu" in glowna.text


def test_front_w_prawdziwym_kokpicie_jest_zamontowany_po_wszystkich_adresach_api(
    tmp_path, monkeypatch,
):
    from starlette.routing import Mount
    aplikacja = _prawdziwa_aplikacja_z_frontem(tmp_path, monkeypatch)
    trasy = aplikacja.routes
    assert isinstance(trasy[-1], Mount) and trasy[-1].path == ""
    assert not any(isinstance(t, Mount) for t in trasy[:-1])


# --- HAR-2: katalog sekretow runnera ---

def test_zapis_tokenu_tworzy_brakujacy_lancuch_katalogow(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "a" / "b" / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    secrets.set_token("runner", "sekret-1")
    assert secrets.CONFIG.is_file()
    assert secrets.get_token("runner") == "sekret-1"


def test_katalog_utworzony_przy_zapisie_tokenu_jest_dostepny_tylko_dla_wlasciciela(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "a" / "b" / "credentials.json")
    secrets.set_token("runner", "sekret-1")
    assert stat.S_IMODE(secrets.CONFIG.parent.stat().st_mode) == 0o700

"""Heartbeat runnera i idempotentny stop() (HAR-35, decyzja D12).

Online = otwarty strumien ORAZ heartbeat mlodszy niz `runner_offline_after_s`.
Skutki w bazie sprawdzamy odczytem z bazy, przez prawdziwa aplikacje. Runner idzie
przez produkcyjny kod (`Heartbeat`, `Runner.stop`, `Runner.listen`); transport HTTP
jest atrapa, a procesy krokow to prawdziwe procesy.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, Runner, Tenant
from cockpit.settings import Settings, settings
from runner import client as runner_client
from runner.client import KokpitNiezgodny, Runner as RunnerKlient, TokenOdrzucony

TOKEN_A = "tajne-a"          # runner zespolu Browarek (domyslny zespol kokpitu)
TOKEN_B = "tajne-b"          # runner zespolu Piekarnia
TOKEN_C = "tajne-c"          # runner Browarka, ktory nigdy sie nie zameldowal
ID_A = "a-wlasny"
ID_B = "0-obcy"              # mniejszy od ID_A: bez filtra zespolu to on by wygral
ID_C = "c-nowy"

URL_HEARTBEATU = "http://kokpit.test/api/runners/heartbeat"
TOKEN_RUNNERA = "zxqv7-HEARTBEAT-token-482913"
POLACZONO = b'data: {"type": "polaczono"}\n\n'


def _stary() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=settings.runner_offline_after_s * 10)


def _utc(chwila: datetime) -> datetime:
    """SQLite oddaje daty bez strefy; kokpit zapisuje UTC, wiec tak je czytamy."""
    return chwila if chwila.tzinfo else chwila.replace(tzinfo=timezone.utc)


# --- konfiguracja: czestotliwosc heartbeatu wynika z progu ---------------------

@pytest.fixture
def czyste_srodowisko(monkeypatch):
    monkeypatch.delenv("COCKPIT_HEARTBEAT_CO_S", raising=False)
    monkeypatch.delenv("COCKPIT_RUNNER_OFFLINE_AFTER_S", raising=False)


def test_heartbeat_domyslnie_przychodzi_cztery_razy_w_progu_offline(czyste_srodowisko):
    assert Settings(_env_file=None, runner_offline_after_s=60).heartbeat_co_s == 15
    assert Settings(_env_file=None, runner_offline_after_s=100).heartbeat_co_s == 25


def test_heartbeat_przy_bardzo_krotkim_progu_nie_spada_do_zera(czyste_srodowisko):
    """3 s / 4 = 0 - heartbeat co 0 s to petla bez przerwy, wiec dolna granica to 1 s."""
    assert Settings(_env_file=None, runner_offline_after_s=3).heartbeat_co_s == 1


def test_jawnie_podana_czestotliwosc_heartbeatu_nie_jest_nadpisywana_progiem(czyste_srodowisko):
    assert Settings(_env_file=None, runner_offline_after_s=60, heartbeat_co_s=7).heartbeat_co_s == 7


def test_czestotliwosc_heartbeatu_mozna_ustawic_zmienna_srodowiskowa(
    czyste_srodowisko, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_HEARTBEAT_CO_S", "9")
    assert Settings(_env_file=None, runner_offline_after_s=60).heartbeat_co_s == 9


# --- kokpit: endpoint heartbeatu i regula "online" -----------------------------

@pytest.fixture
def polaczenia():
    """Ewidencja polaczen to stan globalny szyny - po tescie sprzatamy to, co dopisal test."""
    zanotowane: list[str] = []
    yield zanotowane
    for runner_id in zanotowane:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        getattr(bus, "_rozlaczone", {}).pop(runner_id, None)


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    """Dwa zespoly. Browarek (pierwszy, wiec domyslny): runner A ze starym heartbeatem
    i runner C, ktory nigdy sie nie zameldowal. Piekarnia: runner B ze starym heartbeatem."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def runner(rid: str, tenant_id: str, token: str, widziany: datetime | None) -> Runner:
        return Runner(id=rid, tenant_id=tenant_id, name=rid,
                      token_hash=hashlib.sha256(token.encode()).hexdigest(),
                      last_seen_at=widziany)

    async with maker() as s:
        browarek, piekarnia = Tenant(name="Browarek"), Tenant(name="Piekarnia")
        s.add(browarek)
        await s.flush()
        s.add(piekarnia)
        await s.flush()
        s.add(runner(ID_A, browarek.id, TOKEN_A, _stary()))
        s.add(runner(ID_C, browarek.id, TOKEN_C, None))
        s.add(runner(ID_B, piekarnia.id, TOKEN_B, _stary()))
        await s.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        await engine.dispose()


async def _widziany(runner_id: str) -> datetime | None:
    async with db_session.SessionLocal() as s:
        chwila = (await s.execute(
            select(Runner.last_seen_at).where(Runner.id == runner_id))).scalar_one()
    return None if chwila is None else _utc(chwila)


async def _ustaw_widziany(runner_id: str, chwila: datetime) -> None:
    async with db_session.SessionLocal() as s:
        await s.execute(update(Runner).where(Runner.id == runner_id).values(last_seen_at=chwila))
        await s.commit()


async def _runy() -> list[Run]:
    async with db_session.SessionLocal() as s:
        return list((await s.execute(select(Run))).scalars().all())


def _naglowek(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_heartbeat_odswieza_w_bazie_chwile_ostatniego_zycia_runnera(swiat):
    assert await _widziany(ID_A) < datetime.now(timezone.utc) - timedelta(minutes=5)

    przed = datetime.now(timezone.utc)
    r = await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_A))
    po = datetime.now(timezone.utc)

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert przed <= await _widziany(ID_A) <= po


async def test_heartbeat_ustawia_chwile_zycia_runnerowi_ktory_nigdy_sie_nie_zameldowal(swiat):
    assert await _widziany(ID_C) is None

    przed = datetime.now(timezone.utc)
    r = await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_C))
    po = datetime.now(timezone.utc)

    assert r.status_code == 200
    assert przed <= await _widziany(ID_C) <= po


@pytest.mark.parametrize(
    "naglowek",
    [None, "tajne-a", "Bearer nieznany-token"],
    ids=["bez_tokenu", "token_bez_prefiksu_bearer", "token_nieznany"],
)
async def test_heartbeat_bez_waznego_tokenu_dostaje_odmowe_i_nikogo_nie_odswieza(
    swiat, naglowek,
):
    przed = {rid: await _widziany(rid) for rid in (ID_A, ID_B, ID_C)}
    naglowki = {} if naglowek is None else {"Authorization": naglowek}

    r = await swiat.post("/api/runners/heartbeat", headers=naglowki)

    assert r.status_code == 401
    assert {rid: await _widziany(rid) for rid in (ID_A, ID_B, ID_C)} == przed


async def test_heartbeat_runnera_jednego_zespolu_nie_odswieza_runnera_drugiego_zespolu(swiat):
    obcy_przed = await _widziany(ID_B)
    wlasny_przed = await _widziany(ID_A)

    assert (await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_A))).status_code == 200

    assert await _widziany(ID_B) == obcy_przed
    assert await _widziany(ID_A) > wlasny_przed

    assert (await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_B))).status_code == 200
    assert await _widziany(ID_B) > obcy_przed


async def test_runner_z_otwartym_strumieniem_bez_heartbeatu_dluzej_niz_prog_jest_offline_a_run_nie_powstaje(
    swiat, polaczenia,
):
    for rid in (ID_A, ID_B):
        polaczenia.append(rid)
        bus.mark_runner_connected(rid)
    # Piekarnia ma runnera ze strumieniem i swiezym heartbeatem - nie moze zastapic runnera Browarka
    assert (await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_B))).status_code == 200

    r = await swiat.post("/api/runs", json={"workflow": "szkielet"})

    assert r.status_code == 409
    assert r.json()["detail"].startswith("runner offline")
    assert await _runy() == []


async def test_ten_sam_runner_po_heartbeacie_dostaje_run_a_runner_obcego_zespolu_nie(
    swiat, polaczenia,
):
    for rid in (ID_A, ID_B):
        polaczenia.append(rid)
        bus.mark_runner_connected(rid)
    assert (await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_B))).status_code == 200
    assert (await swiat.post("/api/runs", json={})).status_code == 409

    assert (await swiat.post("/api/runners/heartbeat", headers=_naglowek(TOKEN_A))).status_code == 200
    r = await swiat.post("/api/runs", json={})

    assert r.status_code == 200
    runy = await _runy()
    assert [(x.id, x.runner_id) for x in runy] == [(r.json()["run_id"], ID_A)]


async def test_run_powstaje_gdy_heartbeat_jest_mlodszy_niz_prog_i_nie_powstaje_gdy_starszy(
    swiat, polaczenia,
):
    polaczenia.append(ID_A)
    bus.mark_runner_connected(ID_A)
    prog = settings.runner_offline_after_s

    await _ustaw_widziany(ID_A, datetime.now(timezone.utc) - timedelta(seconds=prog + 30))
    assert (await swiat.post("/api/runs", json={})).status_code == 409
    assert await _runy() == []

    await _ustaw_widziany(ID_A, datetime.now(timezone.utc) - timedelta(seconds=prog - 30))
    assert (await swiat.post("/api/runs", json={})).status_code == 200
    assert [x.runner_id for x in await _runy()] == [ID_A]


async def test_prog_offline_runnera_wynika_z_konfiguracji_kokpitu(swiat, polaczenia, monkeypatch):
    """Heartbeat sprzed 2 minut: przy progu 60 s runner jest offline, przy 600 s online."""
    polaczenia.append(ID_A)
    bus.mark_runner_connected(ID_A)
    await _ustaw_widziany(ID_A, datetime.now(timezone.utc) - timedelta(seconds=120))

    assert (await swiat.post("/api/runs", json={})).status_code == 409
    assert await _runy() == []

    monkeypatch.setattr(settings, "runner_offline_after_s", 600)
    assert (await swiat.post("/api/runs", json={})).status_code == 200
    assert [x.runner_id for x in await _runy()] == [ID_A]


# --- runner: petla heartbeatu --------------------------------------------------

async def _poczekaj(warunek, limit: float = 5.0) -> None:
    koniec = time.monotonic() + limit
    while not warunek():
        assert time.monotonic() < koniec, "warunek nie spelnil sie w limicie czasu"
        await asyncio.sleep(0.01)


class _AtrapaSieci(httpx.AsyncBaseTransport):
    """Transport nagrywajacy zadania heartbeatu: odpowiada, rzuca albo wisi bez konca."""

    def __init__(self, status: int = 200, blad: Exception | None = None, zawies: bool = False):
        self.status, self.blad, self.zawies = status, blad, zawies
        self.zadania: list[httpx.Request] = []
        self.chwile: list[float] = []
        self.odebrane = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.zadania.append(request)
        self.chwile.append(time.monotonic())
        self.odebrane.set()
        if self.zawies:
            await asyncio.Event().wait()
        if self.blad is not None:
            raise self.blad
        return httpx.Response(self.status)


@asynccontextmanager
async def _heartbeat(transport, co_s: float, token: str = TOKEN_RUNNERA):
    from runner.heartbeat import Heartbeat
    async with httpx.AsyncClient(transport=transport, timeout=None) as klient:
        hb = Heartbeat(klient, URL_HEARTBEATU, _naglowek(token), co_s)
        try:
            yield hb
        finally:
            await asyncio.wait_for(hb.stop(), 5)


async def test_heartbeat_wysyla_post_pod_adres_kokpitu_z_tokenem_runnera_w_naglowku():
    atrapa = _AtrapaSieci()
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await _poczekaj(lambda: len(atrapa.zadania) >= 2)

    for z in atrapa.zadania:
        assert z.method == "POST"
        assert str(z.url) == URL_HEARTBEATU
        assert z.headers["Authorization"] == f"Bearer {TOKEN_RUNNERA}"
        assert z.content == b""


async def test_heartbeat_czeka_odstep_przed_pierwszym_meldunkiem():
    atrapa = _AtrapaSieci()
    async with _heartbeat(atrapa, 0.3) as hb:
        start = time.monotonic()
        hb.start()
        await asyncio.wait_for(atrapa.odebrane.wait(), 3)

    assert atrapa.chwile[0] - start >= 0.25


async def test_limit_czasu_zadania_heartbeatu_to_mniejsza_z_wartosci_odstep_albo_10_sekund():
    from runner.heartbeat import Heartbeat
    async with httpx.AsyncClient(transport=_AtrapaSieci()) as klient:
        assert Heartbeat(klient, URL_HEARTBEATU, {}, 2).timeout_s == 2
        assert Heartbeat(klient, URL_HEARTBEATU, {}, 0.5).timeout_s == 0.5
        assert Heartbeat(klient, URL_HEARTBEATU, {}, 30).timeout_s == 10


async def test_zadanie_heartbeatu_dostaje_limit_czasu_rowny_jego_timeoutowi():
    atrapa = _AtrapaSieci()
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await _poczekaj(lambda: atrapa.zadania)
        assert hb.timeout_s == 0.05

    assert atrapa.zadania[0].extensions["timeout"]["read"] == 0.05


@pytest.mark.parametrize(
    "blad",
    [httpx.ConnectError(f"brak polaczenia z tokenem {TOKEN_RUNNERA}"),
     httpx.ReadTimeout(f"timeout {TOKEN_RUNNERA}")],
    ids=["ConnectError", "ReadTimeout"],
)
async def test_awaria_sieci_przy_heartbeacie_trafia_na_stderr_bez_tokenu_a_petla_trwa(
    blad, capsys,
):
    atrapa = _AtrapaSieci(blad=blad)
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await _poczekaj(lambda: len(atrapa.zadania) >= 3)

    wyjscie = capsys.readouterr()
    linie = wyjscie.err.splitlines()
    assert len(linie) >= 2
    assert set(linie) == {f"heartbeat nieudany: {type(blad).__name__}"}
    assert TOKEN_RUNNERA not in wyjscie.err
    assert TOKEN_RUNNERA not in wyjscie.out


async def test_zadanie_heartbeatu_na_ktore_kokpit_nie_odpowiada_konczy_sie_po_limicie_czasu(
    capsys,
):
    """Transport, ktory ignoruje limity httpx, i tak nie moze zawiesic petli."""
    atrapa = _AtrapaSieci(zawies=True)
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await _poczekaj(lambda: len(atrapa.zadania) >= 2)

    assert set(capsys.readouterr().err.splitlines()) == {"heartbeat nieudany: TimeoutError"}


@pytest.mark.parametrize("status", [401, 500])
async def test_heartbeat_odrzucony_przez_kokpit_trafia_na_stderr_z_kodem_a_petla_trwa(
    status, capsys,
):
    atrapa = _AtrapaSieci(status=status)
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await _poczekaj(lambda: len(atrapa.zadania) >= 3)

    wyjscie = capsys.readouterr()
    linie = wyjscie.err.splitlines()
    assert len(linie) >= 2
    assert set(linie) == {f"heartbeat odrzucony: http={status}"}
    assert TOKEN_RUNNERA not in wyjscie.err


async def test_heartbeat_przyjety_przez_kokpit_niczego_nie_wypisuje(capsys):
    atrapa = _AtrapaSieci(status=204)
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await _poczekaj(lambda: len(atrapa.zadania) >= 2)

    assert capsys.readouterr().err == ""


async def test_drugie_uruchomienie_heartbeatu_nie_tworzy_drugiej_petli():
    atrapa = _AtrapaSieci()
    async with _heartbeat(atrapa, 0.05) as hb:
        przed = asyncio.all_tasks()
        hb.start()
        po_pierwszym = asyncio.all_tasks() - przed
        hb.start()
        po_drugim = asyncio.all_tasks() - przed

    assert len(po_pierwszym) == 1
    assert po_drugim == po_pierwszym


async def test_zatrzymany_heartbeat_nie_wysyla_juz_zadnych_zadan_i_nie_zostawia_zadania():
    atrapa = _AtrapaSieci()
    async with _heartbeat(atrapa, 0.05) as hb:
        przed = asyncio.all_tasks()
        hb.start()
        await _poczekaj(lambda: atrapa.zadania)
        await asyncio.wait_for(hb.stop(), 5)
        wyslane = len(atrapa.zadania)
        await asyncio.sleep(0.3)

    assert len(atrapa.zadania) == wyslane
    assert asyncio.all_tasks() - przed == set()


async def test_zatrzymanie_heartbeatu_dwa_razy_i_bez_uruchomienia_nie_rzuca():
    atrapa = _AtrapaSieci()
    async with _heartbeat(atrapa, 0.05) as nieuruchomiony:
        await asyncio.wait_for(nieuruchomiony.stop(), 5)
        await asyncio.wait_for(nieuruchomiony.stop(), 5)
    async with _heartbeat(atrapa, 0.05) as hb:
        hb.start()
        await asyncio.wait_for(hb.stop(), 5)
        await asyncio.wait_for(hb.stop(), 5)


async def test_zawieszone_zadanie_heartbeatu_nie_blokuje_zatrzymania_dluzej_niz_timeout():
    atrapa = _AtrapaSieci(zawies=True)
    async with _heartbeat(atrapa, 0.6) as hb:
        hb.start()
        await asyncio.wait_for(atrapa.odebrane.wait(), 3)   # zadanie faktycznie wisi
        start = time.monotonic()
        await asyncio.wait_for(hb.stop(), 5)
        czas = time.monotonic() - start

    assert czas < hb.timeout_s / 2


# --- runner: stop() ------------------------------------------------------------

def _zyje(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# pid -> grupa procesow w chwili zapamietania; sprzatane po kazdym tescie
_PROCESY_TESTU: dict[int, int] = {}


def _zapamietaj_proces(pid: int) -> None:
    try:
        _PROCESY_TESTU[pid] = os.getpgid(pid)
    except ProcessLookupError:
        pass


def _zabij_proces_kroku(pid: int) -> None:
    """SIGKILL dla procesu kroku; cala grupa tylko wtedy, gdy krok ja prowadzi.

    Pod mutantem bez `start_new_session` krok siedzi w grupie procesu testow, a ta
    w CI jest tez grupa mutmuta i agenta runnera GitHuba: `killpg` tej grupy zrywal
    caly job `mutation` (PR #52, "runner lost communication").
    """
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.fixture(autouse=True)
def _sprzatnij_procesy_testu():
    """Proces kroku albo jego potomek zostawiony przez oblany test (albo zmutowany
    `stop()`) nie przezywa testu. Grupa porownywana z zapamietana, zeby nie zabic
    obcego procesu, ktory dostal ten sam, zwolniony juz pid."""
    yield
    while _PROCESY_TESTU:
        pid, grupa = _PROCESY_TESTU.popitem()
        try:
            if os.getpgid(pid) == grupa:
                _zabij_proces_kroku(pid)
        except ProcessLookupError:
            pass


@asynccontextmanager
async def _krok(runner: RunnerKlient, komenda: list[str]):
    """Uruchamia prawdziwy `Runner.run_command`; zwraca (zadanie, proces)."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as klient:
        zadanie = asyncio.create_task(runner.run_command(klient, "run-1", "s0", komenda))
        proc = None
        try:
            await _poczekaj(lambda: runner._procesy)
            proc = next(iter(runner._procesy))
            _zapamietaj_proces(proc.pid)
            yield zadanie, proc
        finally:
            if proc is not None:
                _zabij_proces_kroku(proc.pid)
            if not zadanie.done():
                zadanie.cancel()
            # wait, nie gather: zmutowany krok moze polknac anulowanie i wisiec
            await asyncio.wait({zadanie}, timeout=5)


async def _pid_z_pliku(plik) -> int:
    await _poczekaj(lambda: plik.exists() and plik.read_text().strip())
    pid = int(plik.read_text().strip())
    _zapamietaj_proces(pid)
    return pid


async def test_stop_runnera_konczy_proces_trwajacego_kroku_i_mozna_go_wywolac_dwa_razy():
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA)
    async with _krok(runner, ["sleep", "60"]) as (zadanie, proc):
        pid = proc.pid
        assert _zyje(pid)

        await asyncio.wait_for(runner.stop(), 10)
        start = time.monotonic()
        await asyncio.wait_for(runner.stop(), 10)      # drugi raz: bez wyjatku i bez czekania
        assert time.monotonic() - start < 0.5

        assert not _zyje(pid)                           # takze zombie: proces odebrany
        await asyncio.wait_for(zadanie, 10)
    assert runner._procesy == set()


async def test_stop_runnera_konczy_cala_grupe_procesow_kroku_takze_potomka(tmp_path):
    plik = tmp_path / "pid-potomka"
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA)
    async with _krok(runner, ["sh", "-c", f"sleep 60 & echo $! > {plik}; wait"]) as (_, proc):
        potomek = await _pid_z_pliku(plik)
        assert _zyje(potomek) and potomek != proc.pid

        await asyncio.wait_for(runner.stop(), 10)

        await _poczekaj(lambda: not _zyje(potomek), limit=3)


async def test_krok_ignorujacy_sygnal_zakonczenia_ginie_po_czasie_na_zakonczenie(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(runner_client, "CZAS_NA_ZAKONCZENIE_S", 0.2)
    plik = tmp_path / "pid-potomka"
    # pid trafia do pliku dopiero PO ustawieniu pułapki - wczesniej SIGTERM zabilby powloke
    komenda = f'trap "" TERM; sleep 60 & echo $! > {plik}; wait'
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA)
    async with _krok(runner, ["sh", "-c", komenda]) as (zadanie, proc):
        potomek = await _pid_z_pliku(plik)
        pid = proc.pid

        start = time.monotonic()
        await asyncio.wait_for(runner.stop(), 10)
        czas = time.monotonic() - start

        assert czas >= 0.15                  # najpierw uprzejma prosba, potem SIGKILL
        assert not _zyje(pid)
        await _poczekaj(lambda: not _zyje(potomek), limit=3)
        await asyncio.wait_for(zadanie, 10)


async def test_krok_dostaje_szanse_na_sprzatanie_po_sygnale_zakonczenia_zanim_padnie_sigkill(
    tmp_path,
):
    plik_pid, znacznik = tmp_path / "pid", tmp_path / "sprzatanie"
    # Krok to jeden proces Pythona, bez potomkow: sygnal dostaje tylko on, wiec nie ma
    # wyscigu miedzy obsluga sygnalu a smiercia dziecka (powloka bywala tu niedeterministyczna).
    # Pid trafia do pliku dopiero PO zainstalowaniu obslugi sygnalu. Sprzatanie trwa chwile
    # (0.3 s), wiec natychmiastowy SIGKILL albo brak okresu laski nie zostawi znacznika.
    program = (
        "import signal, sys, time\n"
        "def sprzatnij(*_):\n"
        "    time.sleep(0.3)\n"
        f"    open({str(znacznik)!r}, 'w').write('uprzatniete')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, sprzatnij)\n"
        f"open({str(plik_pid)!r}, 'w').write(str(__import__('os').getpid()))\n"
        "time.sleep(60)\n"
    )
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA)
    async with _krok(runner, [sys.executable, "-c", program]):
        pid = await _pid_z_pliku(plik_pid)

        await asyncio.wait_for(runner.stop(), 10)

        assert not _zyje(pid)
    assert znacznik.read_text().strip() == "uprzatniete"


async def test_stop_runnera_bez_trwajacych_krokow_i_bez_nasluchu_nie_rzuca():
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA)
    await asyncio.wait_for(runner.stop(), 5)
    await asyncio.wait_for(runner.stop(), 5)


async def test_anulowanie_zadania_kroku_przez_kogos_innego_niz_stop_zabija_grupe_procesow_kroku(
    tmp_path,
):
    plik = tmp_path / "pid-potomka"
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA)
    async with _krok(runner, ["sh", "-c", f"sleep 60 & echo $! > {plik}; wait"]) as (zadanie, proc):
        potomek = await _pid_z_pliku(plik)
        pid = proc.pid
        assert _zyje(pid) and _zyje(potomek)

        zadanie.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(zadanie, 5)

        # ta galaz nie czeka na proces, wiec ginie "po chwili"
        await _poczekaj(lambda: not _zyje(pid) and not _zyje(potomek), limit=3)
    assert runner._procesy == set()


# --- runner: nasluch z heartbeatem ---------------------------------------------

class _StrumienBezKonca(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield POLACZONO
        await asyncio.Event().wait()


class _AtrapaKokpitu(httpx.AsyncBaseTransport):
    """Kokpit: rejestracja (z `heartbeat_co_s`), strumien i heartbeat."""

    def __init__(self, heartbeat_co_s: int | None = 15, strumien_konczy: bool = False,
                 strumien_status: int = 200, heartbeat_wisi: bool = False):
        self.heartbeat_co_s = heartbeat_co_s
        self.strumien_konczy, self.strumien_status = strumien_konczy, strumien_status
        self.heartbeat_wisi = heartbeat_wisi
        self.zadania: list[httpx.Request] = []
        self.heartbeat_odebrany = asyncio.Event()

    @property
    def heartbeaty(self) -> list[httpx.Request]:
        return [z for z in self.zadania if z.url.path.endswith("/api/runners/heartbeat")]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.zadania.append(request)
        sciezka = request.url.path
        if sciezka.endswith("/api/runners/register"):
            tresc = {"runner_id": "r1", "tenant_id": "t1"}
            if self.heartbeat_co_s is not None:
                tresc["heartbeat_co_s"] = self.heartbeat_co_s
            return httpx.Response(200, json=tresc)
        if sciezka.endswith("/api/runners/stream"):
            naglowki = {"content-type": "text/event-stream"}
            if self.strumien_konczy:
                return httpx.Response(self.strumien_status, content=POLACZONO, headers=naglowki)
            return httpx.Response(self.strumien_status, stream=_StrumienBezKonca(), headers=naglowki)
        if sciezka.endswith("/api/runners/heartbeat"):
            self.heartbeat_odebrany.set()
            if self.heartbeat_wisi:
                await asyncio.Event().wait()
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)


def _podglad_heartbeatu(monkeypatch, co_s_testowe: float = 0.05):
    """Podmienia `Heartbeat` w runnerze na prawdziwa podklase, ktora zapisuje, jak ja
    zbudowano i czy ja zatrzymano, i przyspiesza odstep do `co_s_testowe`."""
    from runner.heartbeat import Heartbeat
    zapis = {"utworzone": [], "start": 0, "stop": 0}

    class PodgladHeartbeatu(Heartbeat):
        def __init__(self, client, url, naglowki, co_s):
            zapis["utworzone"].append(
                {"klient": client, "url": url, "naglowki": dict(naglowki), "co_s": co_s})
            super().__init__(client, url, naglowki, co_s_testowe)

        def start(self):
            zapis["start"] += 1
            super().start()

        async def stop(self):
            zapis["stop"] += 1
            await super().stop()

    monkeypatch.setattr(runner_client, "Heartbeat", PodgladHeartbeatu)
    return zapis


async def _koniec_nasluchu(nasluch: asyncio.Task, limit: float = 1.0):
    """Czeka na koniec zadania BEZ jego anulowania (`wait_for` anulowalby je po limicie,
    a `listen` po zatrzymaniu polyka anulowanie - test przechodzilby takze wtedy, gdy
    `stop()` nic nie anuluje). Zwraca wynik nasluchu."""
    start = time.monotonic()
    zakonczone, _ = await asyncio.wait({nasluch}, timeout=limit)
    if nasluch not in zakonczone:
        nasluch.cancel()
        await asyncio.wait({nasluch}, timeout=5)
        raise AssertionError(f"nasluch nie skonczyl sie w {limit} s po stop()")
    assert time.monotonic() - start < limit
    return nasluch.result()


async def test_runner_w_trakcie_nasluchu_melduje_kokpitowi_ze_zyje_z_czestoscia_od_kokpitu(
    monkeypatch,
):
    zapis = _podglad_heartbeatu(monkeypatch)
    atrapa = _AtrapaKokpitu(heartbeat_co_s=7)
    runner = RunnerKlient("http://kokpit.test/", TOKEN_RUNNERA, transport=atrapa)
    nasluch = asyncio.create_task(runner.listen())
    try:
        await asyncio.wait_for(atrapa.heartbeat_odebrany.wait(), 3)
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        await _koniec_nasluchu(nasluch)

    assert runner.heartbeat_co_s == 7
    assert len(zapis["utworzone"]) == 1
    zbudowany = zapis["utworzone"][0]
    assert zbudowany["url"] == URL_HEARTBEATU
    assert zbudowany["naglowki"] == _naglowek(TOKEN_RUNNERA)
    assert zbudowany["co_s"] == 7
    assert isinstance(zbudowany["klient"], httpx.AsyncClient)
    assert zapis["start"] == 1
    zadanie = atrapa.heartbeaty[0]
    assert zadanie.method == "POST"
    assert zadanie.headers["Authorization"] == f"Bearer {TOKEN_RUNNERA}"


async def test_stop_w_trakcie_nasluchu_konczy_nasluch_normalnie_bez_bledu_zamkniecia_strumienia(
    monkeypatch,
):
    _podglad_heartbeatu(monkeypatch)
    atrapa = _AtrapaKokpitu()
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA, transport=atrapa)
    przed = asyncio.all_tasks()
    nasluch = asyncio.create_task(runner.listen())
    await asyncio.wait_for(atrapa.heartbeat_odebrany.wait(), 3)

    await asyncio.wait_for(runner.stop(), 10)
    wynik = await _koniec_nasluchu(nasluch)

    assert wynik is None
    assert not nasluch.cancelled()
    wyslane = len(atrapa.heartbeaty)
    await asyncio.sleep(0.3)
    assert len(atrapa.heartbeaty) == wyslane            # heartbeat po stopie milczy
    assert asyncio.all_tasks() - przed == set()


async def test_zawieszony_heartbeat_nie_blokuje_zatrzymania_runnera_dluzej_niz_timeout(
    monkeypatch,
):
    _podglad_heartbeatu(monkeypatch, co_s_testowe=0.6)
    atrapa = _AtrapaKokpitu(heartbeat_wisi=True)
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA, transport=atrapa)
    nasluch = asyncio.create_task(runner.listen())
    await asyncio.wait_for(atrapa.heartbeat_odebrany.wait(), 5)   # zadanie faktycznie wisi

    start = time.monotonic()
    await asyncio.wait_for(runner.stop(), 10)
    czas = time.monotonic() - start
    await _koniec_nasluchu(nasluch)

    assert czas < 0.3        # timeout heartbeatu to 0.6 s


async def test_blad_nasluchu_zatrzymuje_heartbeat_i_nie_zostawia_zadania(monkeypatch):
    """Odrzucony token na strumieniu: błąd bez ponawiania, heartbeat zatrzymany."""
    zapis = _podglad_heartbeatu(monkeypatch)
    atrapa = _AtrapaKokpitu(strumien_konczy=True, strumien_status=401)
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA, transport=atrapa)
    przed = asyncio.all_tasks()

    with pytest.raises(TokenOdrzucony):
        await asyncio.wait_for(runner.listen(), 10)

    assert zapis["start"] == 1
    assert zapis["stop"] == 1
    assert asyncio.all_tasks() - przed == set()


async def test_zamkniecie_strumienia_zatrzymuje_heartbeat_a_po_ponownym_polaczeniu_dziala_nowy(
    monkeypatch,
):
    """HAR-45 (dawniej: zamknięcie strumienia kończyło `listen()` błędem). Teraz runner łączy
    się ponownie: stary heartbeat musi być zatrzymany, zanim ruszy nowy, i nic nie zostaje."""
    zapis = _podglad_heartbeatu(monkeypatch)
    atrapa = _AtrapaKokpitu(strumien_konczy=True)
    stan_przy_odczekaniu: list[tuple[int, int]] = []

    async def odczekaj(sekundy: float) -> None:
        stan_przy_odczekaniu.append((zapis["start"], zapis["stop"]))
        if len(stan_przy_odczekaniu) == 2:
            await runner.stop()

    runner = RunnerKlient(
        "http://kokpit.test", TOKEN_RUNNERA, transport=atrapa, odczekaj=odczekaj)
    przed = asyncio.all_tasks()

    assert await asyncio.wait_for(runner.listen(), 10) is None

    # przy pierwszym odczekaniu: jeden heartbeat ruszył i jest już zatrzymany;
    # przy drugim: ruszył nowy i też jest zatrzymany
    assert stan_przy_odczekaniu == [(1, 1), (2, 2)]
    assert len(zapis["utworzone"]) == 2
    assert zapis["utworzone"][0]["klient"] is not zapis["utworzone"][1]["klient"]
    assert len([z for z in atrapa.zadania if z.url.path.endswith("/api/runners/register")]) == 2
    assert asyncio.all_tasks() - przed == set()


async def test_heartbeat_dziala_po_ponownym_polaczeniu_i_milknie_po_stopie(monkeypatch):
    """Strumień zamyka się raz; drugie połączenie jest otwarte bez końca. Nowy heartbeat
    melduje kokpitowi życie, a `stop()` kończy wszystko w limicie czasu."""
    _podglad_heartbeatu(monkeypatch, co_s_testowe=0.1)   # pierwsze polaczenie zyje milisekundy

    class ZamknijPotemOtworz(_AtrapaKokpitu):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/api/runners/stream"):
                self.strumien_konczy = not any(
                    z.url.path.endswith("/api/runners/stream") for z in self.zadania)
            return await super().handle_async_request(request)

    async def bez_czekania(sekundy: float) -> None:
        return None

    atrapa = ZamknijPotemOtworz()
    runner = RunnerKlient(
        "http://kokpit.test", TOKEN_RUNNERA, transport=atrapa, odczekaj=bez_czekania)
    przed = asyncio.all_tasks()
    nasluch = asyncio.create_task(runner.listen())
    try:
        await asyncio.wait_for(atrapa.heartbeat_odebrany.wait(), 5)
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        await _koniec_nasluchu(nasluch)

    sciezki = [z.url.path for z in atrapa.zadania]
    assert sciezki.count("/api/runners/register") == 2
    assert atrapa.heartbeaty                     # zywe jest dopiero drugie polaczenie
    wyslane = len(atrapa.heartbeaty)
    await asyncio.sleep(0.3)
    assert len(atrapa.heartbeaty) == wyslane
    assert asyncio.all_tasks() - przed == set()


async def test_odpowiedz_rejestracji_bez_czestotliwosci_heartbeatu_konczy_nasluch_bledem_bez_heartbeatu(
    monkeypatch,
):
    zapis = _podglad_heartbeatu(monkeypatch)
    atrapa = _AtrapaKokpitu(heartbeat_co_s=None)
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA, transport=atrapa)

    with pytest.raises(KokpitNiezgodny):
        await asyncio.wait_for(runner.listen(), 10)

    assert zapis["utworzone"] == []
    assert not any(z.url.path.endswith("/api/runners/stream") for z in atrapa.zadania)

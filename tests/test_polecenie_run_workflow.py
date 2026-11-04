"""HAR-80: polecenie `run_workflow` - kokpit je wysyla, runner wykonuje w osobnym zadaniu.

Dwie strony protokolu:
- runner (`runner.client.Runner`): polecenie startuje run w osobnym zadaniu, strumien polecen
  jest czytany dalej; zdarzenia silnika ida do kokpitu przez `send_event` (z `event_id`);
  zerwanie strumienia nie przerywa runu (BRO-75), `stop()` go anuluje. Podstawiony jest
  tylko silnik (`runner.silnik.wykonaj_run`) - klient i transport sa produkcyjne;
  transport to wlasny kokpit-atrapa trzymajacy strumien SSE otwarty.
- kokpit: `POST /api/runs` z `project_id` wysyla runnerowi projektu `run_workflow` ze sciezka
  projektu (bez `command`), zapisuje `Run.project_id`; projekt innego zespolu to 404 i brak
  runu. Skutki czytane z bazy i z kolejki runnera, nie z samego kodu HTTP.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Project, Run, RunStatus
from cockpit.services import projekty
from cockpit.services import runs as uslugi
from cockpit.services.errors import NieZnaleziono
from procesy import czekaj_na_warunek, zadanie_tla
from runner.bufor import BrakMiejsca, Bufor
from runner.client import Runner
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela

TOKEN = "run-workflow-token-5512"
LIMIT_S = 10
POLACZONO = {"type": "polaczono"}
RUN_ID = "run-80"
POLECENIE = {"type": "run_workflow", "run_id": RUN_ID, "workflow": "w", "sciezka": "/repo"}


# =====================================================================================
# 1. Runner: osobne zadanie na run, strumien czytany dalej
# =====================================================================================


class Kokpit(httpx.AsyncBaseTransport):
    """Atrapa kokpitu: strumien SSE otwarty do `zakoncz_strumien()`, zdarzenia i odpowiedzi
    zapisane. `polecenia` to kolejka wysylana do strumienia po kolei."""

    def __init__(self, opoznienie_zdarzenia: float = 0.0) -> None:
        self.polecenia: asyncio.Queue = asyncio.Queue()
        self.zdarzenia: list[dict] = []
        self.naglowki_zdarzen: list[httpx.Headers] = []
        self.odpowiedzi: list[dict] = []
        self.strumienie = 0
        self.opoznienie_zdarzenia = opoznienie_zdarzenia

    def wyslij(self, polecenie: dict) -> None:
        self.polecenia.put_nowait(polecenie)

    def zakoncz_strumien(self) -> None:
        self.polecenia.put_nowait(None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sciezka = request.url.path
        if sciezka == "/api/runners/register":
            return httpx.Response(
                200, json={"runner_id": "r1", "tenant_id": "t1", "heartbeat_co_s": 15})
        if sciezka == "/api/runners/stream":
            self.strumienie += 1
            return httpx.Response(
                200, stream=_Strumien(self), headers={"content-type": "text/event-stream"})
        if sciezka == "/api/events":
            if self.opoznienie_zdarzenia:
                await asyncio.sleep(self.opoznienie_zdarzenia)
            self.zdarzenia.append(json.loads(request.content))
            self.naglowki_zdarzen.append(request.headers)
            return httpx.Response(200, json={"seq": len(self.zdarzenia)})
        if sciezka == "/api/runners/odpowiedzi":
            self.odpowiedzi.append(json.loads(request.content))
            return httpx.Response(200)
        return httpx.Response(200)


class _Strumien(httpx.AsyncByteStream):
    def __init__(self, kokpit: Kokpit) -> None:
        self.kokpit = kokpit

    async def __aiter__(self):
        yield _sse(POLACZONO)
        while True:
            polecenie = await self.kokpit.polecenia.get()
            if polecenie is None:
                return
            yield _sse(polecenie)


def _sse(polecenie: dict) -> bytes:
    return b"data: " + json.dumps(polecenie).encode() + b"\n\n"


class Odczekania:
    """Odczekanie przed ponownym polaczeniem: czeka na `brama` (test steruje wznowieniem)."""

    def __init__(self) -> None:
        self.brama = asyncio.Event()
        self.czeka = asyncio.Event()

    async def __call__(self, sekundy: float) -> None:
        self.czeka.set()
        await asyncio.wait_for(self.brama.wait(), LIMIT_S)


class Silnik:
    """Podstawiony `wykonaj_run`: zapisuje polecenie, zglasza start i czeka na zwolnienie."""

    def __init__(self) -> None:
        self.polecenia: list[dict] = []
        self.wystartowal = asyncio.Event()
        self.zwolnij = asyncio.Event()
        self.anulowany = asyncio.Event()
        self.zakonczony = asyncio.Event()
        self.zdarzenia_do_wyslania: list[dict] = []
        self.wyjatek_emit: BaseException | None = None

    async def __call__(self, polecenie, emit, *args, **kwargs):
        self.polecenia.append(polecenie)
        self.wystartowal.set()
        try:
            for zdarzenie in self.zdarzenia_do_wyslania:
                try:
                    await emit(dict(zdarzenie))
                except BrakMiejsca as e:
                    self.wyjatek_emit = e
                    raise
            await asyncio.wait_for(self.zwolnij.wait(), LIMIT_S)
            await emit({"kind": "run_finished", "run_id": polecenie["run_id"],
                        "step_id": "run", "agent_id": "", "text": "",
                        "cost_usd": None, "cost_source": "unavailable"})
        except asyncio.CancelledError:
            self.anulowany.set()
            raise
        finally:
            self.zakonczony.set()


@pytest.fixture
def silnik(monkeypatch) -> Silnik:
    atrapa = Silnik()
    monkeypatch.setattr("runner.silnik.wykonaj_run", atrapa)
    return atrapa


def _runner(kokpit: Kokpit, tmp_path, odczekania: Odczekania | None = None,
            bufor: Bufor | None = None) -> Runner:
    return Runner(
        "http://kokpit.test", TOKEN, transport=kokpit,
        bufor=bufor if bufor is not None else Bufor(tmp_path / "bufor"),
        odczekaj=odczekania if odczekania is not None else Odczekania(),
    )


async def _poczekaj(warunek, nazwa: str, limit: float = LIMIT_S) -> None:
    await czekaj_na_warunek(warunek, limit, nazwa)


async def test_trwajacy_run_nie_blokuje_odpowiedzi_na_kolejne_zapytanie_kokpitu(tmp_path, silnik):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(silnik.wystartowal.wait(), LIMIT_S)
        kokpit.wyslij({"type": "ping", "req_id": "zapytanie-1"})

        await _poczekaj(lambda: kokpit.odpowiedzi, "odpowiedz na zapytanie w trakcie runu")

        assert kokpit.odpowiedzi == [{"req_id": "zapytanie-1", "ok": True, "wynik": {"pong": True}}]
        assert not silnik.zwolnij.is_set()          # run nadal trwa
        assert not silnik.zakonczony.is_set()
        silnik.zwolnij.set()
        await asyncio.wait_for(silnik.zakonczony.wait(), LIMIT_S)


async def test_dwa_polecenia_run_workflow_pod_rzad_uruchamiaja_dwa_rownolegle_runy(tmp_path, silnik):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    drugie = {**POLECENIE, "run_id": "run-81"}
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        kokpit.wyslij(drugie)

        await _poczekaj(lambda: len(silnik.polecenia) == 2, "oba runy wystartowaly")

        assert [p["run_id"] for p in silnik.polecenia] == [RUN_ID, "run-81"]
        assert not silnik.zakonczony.is_set()         # pierwszy nadal trwa, a drugi ruszyl
        silnik.zwolnij.set()


async def test_silnik_dostaje_cale_polecenie_run_workflow_od_kokpitu(tmp_path, silnik):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(silnik.wystartowal.wait(), LIMIT_S)

        assert silnik.polecenia == [POLECENIE]
        silnik.zwolnij.set()


async def test_zdarzenia_silnika_trafiaja_do_kokpitu_z_tokenem_i_unikalnym_event_id(tmp_path, silnik):
    kokpit = Kokpit()
    silnik.zdarzenia_do_wyslania = [
        {"kind": "step_started", "run_id": RUN_ID, "step_id": "a", "agent_id": "a", "text": ""},
        {"kind": "output", "run_id": RUN_ID, "step_id": "a", "agent_id": "a", "text": "linia"},
    ]
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(silnik.wystartowal.wait(), LIMIT_S)
        silnik.zwolnij.set()
        await _poczekaj(lambda: len(kokpit.zdarzenia) == 3, "trzy zdarzenia u kokpitu")

    assert [z["kind"] for z in kokpit.zdarzenia] == ["step_started", "output", "run_finished"]
    assert [z["step_id"] for z in kokpit.zdarzenia] == ["a", "a", "run"]
    assert all(z["run_id"] == RUN_ID for z in kokpit.zdarzenia)
    ids = [z["event_id"] for z in kokpit.zdarzenia]
    assert all(isinstance(i, str) and i for i in ids) and len(set(ids)) == 3
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in kokpit.naglowki_zdarzen)


async def test_rownolegle_zdarzenia_runu_sa_wysylane_po_jednym_bez_duplikatow(tmp_path, monkeypatch):
    kokpit = Kokpit(opoznienie_zdarzenia=0.02)
    liczba = 12

    async def silnik(polecenie, emit, *args, **kwargs):
        await asyncio.gather(*(
            emit({"kind": "output", "run_id": RUN_ID, "step_id": f"k{i}",
                  "agent_id": f"k{i}", "text": str(i)})
            for i in range(liczba)))

    monkeypatch.setattr("runner.silnik.wykonaj_run", silnik)
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await _poczekaj(lambda: len(kokpit.zdarzenia) >= liczba, "wszystkie zdarzenia u kokpitu")
        await asyncio.sleep(0.3)                  # ewentualne duplikaty zdaza dojsc

    ids = [z["event_id"] for z in kokpit.zdarzenia]
    assert len(ids) == liczba
    assert len(set(ids)) == liczba                # kazde zdarzenie wyslane dokladnie raz


async def test_stop_anuluje_trwajacy_run(tmp_path, silnik):
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(silnik.wystartowal.wait(), LIMIT_S)

        await asyncio.wait_for(runner.stop(), LIMIT_S)

        await _poczekaj(silnik.anulowany.is_set, "run anulowany przez stop()")
        assert silnik.zakonczony.is_set()


async def test_zerwanie_strumienia_polecen_nie_przerywa_trwajacego_runu(tmp_path, silnik):
    kokpit = Kokpit()
    odczekania = Odczekania()
    runner = _runner(kokpit, tmp_path, odczekania)
    async with zadanie_tla(runner.listen(), "nasluch runnera"):
        kokpit.wyslij(POLECENIE)
        await asyncio.wait_for(silnik.wystartowal.wait(), LIMIT_S)

        kokpit.zakoncz_strumien()                          # kokpit zamyka strumien
        await asyncio.wait_for(odczekania.czeka.wait(), LIMIT_S)   # runner bez strumienia
        assert kokpit.strumienie == 1
        assert not silnik.anulowany.is_set()
        assert not silnik.zakonczony.is_set()

        # run konczy sie, gdy strumienia nie ma: zdarzenie musi dojsc osobnym polaczeniem
        silnik.zwolnij.set()
        await _poczekaj(
            lambda: [z["kind"] for z in kokpit.zdarzenia] == ["run_finished"],
            "run_finished u kokpitu mimo zerwanego strumienia")
        assert not silnik.anulowany.is_set()
        # brama zostaje zamknieta: wyjscie z `zadanie_tla` anuluje nasluch w trakcie odczekania.
        # WHY: zwolnienie bramy tuz przed anulowaniem to na Pythonie 3.11 wyscig, w ktorym
        # `asyncio.wait_for` polyka anulowanie (poprawione w 3.12) i runner laczy sie dalej.


async def test_brak_miejsca_na_zdarzenia_kontroluje_run_przerwaniem_a_nie_wyjatkiem_zadania(tmp_path, silnik):
    class PelnyBufor(Bufor):
        def dopisz(self, zdarzenie: dict) -> None:
            if zdarzenie.get("kind") != "step_aborted":
                raise BrakMiejsca("zapis bufora: brak miejsca")
            super().dopisz(zdarzenie)

    silnik.zdarzenia_do_wyslania = [
        {"kind": "output", "run_id": RUN_ID, "step_id": "a", "agent_id": "a", "text": "x"}]
    kokpit = Kokpit()
    runner = _runner(kokpit, tmp_path, bufor=PelnyBufor(tmp_path / "bufor"))
    bledy: list[dict] = []
    petla = asyncio.get_running_loop()
    stary = petla.get_exception_handler()
    petla.set_exception_handler(lambda p, kontekst: bledy.append(kontekst))
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            kokpit.wyslij(POLECENIE)
            await _poczekaj(lambda: kokpit.zdarzenia, "przerwanie runu u kokpitu")
            await _poczekaj(silnik.zakonczony.is_set, "koniec zadania runu")
            await asyncio.sleep(0.1)
    finally:
        petla.set_exception_handler(stary)

    assert silnik.wyjatek_emit is not None            # silnik naprawde dostal BrakMiejsca
    (przerwanie,) = kokpit.zdarzenia
    assert przerwanie["kind"] == "step_aborted"
    assert przerwanie["run_id"] == RUN_ID
    assert przerwanie["step_id"] == "run"            # przerwanie dotyczy calego runu
    assert przerwanie["text"] == "brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)"
    assert bledy == []                                # zadanie nie zostawia niepobranego wyjatku


# =====================================================================================
# 2. Kokpit: POST /api/runs z project_id
# =====================================================================================


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    """Zespol Browarek: dwa runnery online i projekt na kazdym; zespol Piekarnia: runner
    online i projekt. Zespol obcy jest zakladany PIERWSZY (nie wybierany przez przypadek)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _, piekarnia = await zaloz_wlasciciela(
        maker, email="piekarz@example.com", nazwa_zespolu="Piekarnia")
    wlasciciel, browarek = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    obcy_runner = await zaloz_runnera(maker, piekarnia, "obcy", "tok-obcy")
    runnery = sorted([
        await zaloz_runnera(maker, browarek, "r1", "tok-r1"),
        await zaloz_runnera(maker, browarek, "r2", "tok-r2"),
    ])
    mniejszy, wiekszy = runnery           # domyslny wybor runnera bierze najmniejsze id

    async def projekt(tenant_id: str, runner_id: str, sciezka: str) -> str:
        async with maker() as s:
            p = Project(tenant_id=tenant_id, runner_id=runner_id, path=sciezka,
                        name=sciezka.rsplit("/", 1)[-1], stack=[])
            s.add(p)
            await s.commit()
            return p.id

    projekt_na_wiekszym = await projekt(browarek, wiekszy, "/home/b/repo-wiekszy")
    projekt_obcy = await projekt(piekarnia, obcy_runner, "/home/p/ciasto")
    for rid in (*runnery, obcy_runner):
        bus.mark_runner_connected(rid)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield SimpleNamespace(
                klient=c, maker=maker, browarek=browarek, piekarnia=piekarnia,
                mniejszy=mniejszy, wiekszy=wiekszy, obcy_runner=obcy_runner,
                projekt=projekt_na_wiekszym, projekt_obcy=projekt_obcy,
                nowy_projekt=projekt)
    finally:
        for rid in (*runnery, obcy_runner):
            while rid in bus.connected_runner_ids():
                bus.mark_runner_disconnected(rid)
            getattr(bus, "_rozlaczone", {}).pop(rid, None)
            bus._runner_queues.pop(rid, None)
        await engine.dispose()


def _w_kolejce(runner_id: str) -> list[dict]:
    kolejka = bus.runner_queue(runner_id)
    wynik = []
    while not kolejka.empty():
        wynik.append(kolejka.get_nowait())
    return wynik


async def _runy(maker) -> list[Run]:
    async with maker() as s:
        return list((await s.execute(select(Run))).scalars().all())


async def test_run_z_projektem_zespolu_wysyla_runnerowi_projektu_polecenie_run_workflow(swiat):
    r = await swiat.klient.post(
        "/api/runs", json={"workflow": "budowa", "project_id": swiat.projekt})

    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert _w_kolejce(swiat.wiekszy) == [
        {"type": "run_workflow", "run_id": run_id, "workflow": "budowa",
         "sciezka": "/home/b/repo-wiekszy"}]
    assert _w_kolejce(swiat.mniejszy) == []     # nie runner wybrany domyslnie


async def test_run_z_projektem_zapisuje_projekt_i_runner_projektu_w_bazie(swiat):
    r = await swiat.klient.post(
        "/api/runs", json={"workflow": "budowa", "project_id": swiat.projekt})

    (run,) = await _runy(swiat.maker)
    assert run.id == r.json()["run_id"]
    assert run.project_id == swiat.projekt
    assert run.runner_id == swiat.wiekszy            # runner projektu, nie pierwszy online
    assert run.tenant_id == swiat.browarek
    assert run.workflow == "budowa"
    assert run.status == RunStatus.running


async def test_polecenie_run_workflow_nie_niesie_argv_ani_operacji(swiat):
    await swiat.klient.post("/api/runs", json={"project_id": swiat.projekt})

    (polecenie,) = _w_kolejce(swiat.wiekszy)
    assert set(polecenie) == {"type", "run_id", "workflow", "sciezka"}
    assert polecenie["workflow"] == "szkielet"       # domyslna nazwa z RunCreate


async def test_projekt_innego_zespolu_daje_404_i_run_nie_powstaje(swiat):
    r = await swiat.klient.post("/api/runs", json={"project_id": swiat.projekt_obcy})

    assert r.status_code == 404
    assert r.json()["detail"] == "projekt nieznany"
    assert await _runy(swiat.maker) == []
    assert _w_kolejce(swiat.obcy_runner) == []
    assert _w_kolejce(swiat.wiekszy) == [] and _w_kolejce(swiat.mniejszy) == []


async def test_nieistniejacy_projekt_daje_404_i_run_nie_powstaje(swiat):
    r = await swiat.klient.post("/api/runs", json={"project_id": str(uuid.uuid4())})

    assert r.status_code == 404
    assert r.json()["detail"] == "projekt nieznany"
    assert await _runy(swiat.maker) == []


async def test_projekt_na_runnerze_offline_daje_409_a_run_nie_idzie_na_inny_runner(swiat):
    bus.mark_runner_disconnected(swiat.wiekszy)          # drugi runner zespolu zostaje online

    r = await swiat.klient.post("/api/runs", json={"project_id": swiat.projekt})

    assert r.status_code == 409
    assert await _runy(swiat.maker) == []
    assert _w_kolejce(swiat.mniejszy) == []


async def test_projekt_zespolu_podpiety_do_runnera_innego_zespolu_nie_uruchamia_runu(swiat):
    projekt = await swiat.nowy_projekt(swiat.browarek, swiat.obcy_runner, "/home/b/dziwny")

    r = await swiat.klient.post("/api/runs", json={"project_id": projekt})

    assert r.status_code == 409
    assert await _runy(swiat.maker) == []
    assert _w_kolejce(swiat.obcy_runner) == []


async def test_run_bez_projektu_nadal_wysyla_krok_szkieletu_i_nie_zapisuje_projektu(swiat):
    r = await swiat.klient.post("/api/runs", json={"workflow": "szkielet"})

    assert r.status_code == 200
    run_id = r.json()["run_id"]
    (run,) = await _runy(swiat.maker)
    assert run.project_id is None
    polecenia = _w_kolejce(swiat.mniejszy) + _w_kolejce(swiat.wiekszy)
    assert polecenia == [
        {"type": "run_step", "run_id": run_id, "step_id": "s0", "operacja": "szkielet"}]


async def test_run_bez_projektu_przekazuje_runnerowi_wybrana_operacje(swiat):
    r = await swiat.klient.post("/api/runs", json={"operacja": "inna-operacja"})

    assert r.status_code == 200
    polecenia = _w_kolejce(swiat.mniejszy) + _w_kolejce(swiat.wiekszy)
    assert polecenia == [
        {"type": "run_step", "run_id": r.json()["run_id"], "step_id": "s0",
         "operacja": "inna-operacja"}]


def test_schemat_openapi_tworzenia_runu_opisuje_odpowiedz_404_dla_nieznanego_projektu():
    odpowiedzi = app.openapi()["paths"]["/api/runs"]["post"]["responses"]

    assert "404" in odpowiedzi


async def test_sciezka_projektu_zespolu_jest_wybrana_wsrod_wielu_projektow_zespolu(swiat):
    drugi = await swiat.nowy_projekt(swiat.browarek, swiat.mniejszy, "/home/b/repo-drugi")
    trzeci = await swiat.nowy_projekt(swiat.browarek, swiat.mniejszy, "/home/b/repo-trzeci")
    inny_obcy = await swiat.nowy_projekt(swiat.piekarnia, swiat.obcy_runner, "/home/p/chleb")

    async with swiat.maker() as s:
        sciezki = [
            await projekty.sciezka_projektu(s, swiat.browarek, id_projektu)
            for id_projektu in (swiat.projekt, drugi, trzeci)
        ]
        obca = await projekty.sciezka_projektu(s, swiat.piekarnia, inny_obcy)

    assert sciezki == ["/home/b/repo-wiekszy", "/home/b/repo-drugi", "/home/b/repo-trzeci"]
    assert obca == "/home/p/chleb"


@pytest.mark.parametrize("obcy", ["pierwszy", "drugi"])
async def test_sciezka_projektu_innego_zespolu_to_nie_znaleziono_z_komunikatem(swiat, obcy):
    drugi_obcy = await swiat.nowy_projekt(swiat.piekarnia, swiat.obcy_runner, "/home/p/chleb")
    id_obcego = swiat.projekt_obcy if obcy == "pierwszy" else drugi_obcy
    await swiat.nowy_projekt(swiat.browarek, swiat.mniejszy, "/home/b/repo-drugi")

    async with swiat.maker() as s:
        with pytest.raises(NieZnaleziono) as blad:
            await projekty.sciezka_projektu(s, swiat.browarek, id_obcego)

    assert str(blad.value) == "projekt nieznany"


async def test_sciezka_projektu_o_nieznanym_id_to_nie_znaleziono_z_komunikatem(swiat):
    async with swiat.maker() as s:
        with pytest.raises(NieZnaleziono) as blad:
            await projekty.sciezka_projektu(s, swiat.browarek, str(uuid.uuid4()))

    assert str(blad.value) == "projekt nieznany"


@pytest.mark.parametrize("project_id", ["a" * 37, ""])
async def test_project_id_dluzsze_niz_kolumna_albo_puste_daje_422_i_run_nie_powstaje(
    swiat, project_id,
):
    r = await swiat.klient.post("/api/runs", json={"project_id": project_id})

    assert r.status_code == 422
    assert await _runy(swiat.maker) == []


async def test_project_id_o_dlugosci_kolumny_przechodzi_walidacje_i_daje_404(swiat):
    r = await swiat.klient.post("/api/runs", json={"project_id": "a" * 36})

    assert r.status_code == 404                  # poprawny ksztalt, ale projektu nie ma


# --- polecenie_aktualne ----------------------------------------------------------------------


async def _run_w_stanie(swiat, status: RunStatus, runner_id: str | None = None) -> str:
    async with swiat.maker() as s:
        run = Run(tenant_id=swiat.browarek, runner_id=runner_id or swiat.mniejszy,
                  workflow="w", status=status)
        s.add(run)
        await s.commit()
        return run.id


async def _aktualne(swiat, polecenie: dict, runner_id: str | None = None,
                    tenant_id: str | None = None) -> bool:
    async with swiat.maker() as s:
        return await uslugi.polecenie_aktualne(
            s, polecenie, runner_id or swiat.mniejszy, tenant_id or swiat.browarek)


def _workflow(run_id: str | None) -> dict:
    polecenie = {"type": "run_workflow", "workflow": "w", "sciezka": "/repo"}
    if run_id is not None:
        polecenie["run_id"] = run_id
    return polecenie


async def test_polecenie_run_workflow_runu_w_toku_jest_aktualne(swiat):
    run_id = await _run_w_stanie(swiat, RunStatus.running)

    assert await _aktualne(swiat, _workflow(run_id)) is True


@pytest.mark.parametrize("stan", [RunStatus.aborted, RunStatus.to_accept, RunStatus.failed,
                                  RunStatus.waiting, RunStatus.pending, RunStatus.skipped])
async def test_polecenie_run_workflow_runu_ktory_nie_jest_w_toku_jest_odrzucane(swiat, stan):
    run_id = await _run_w_stanie(swiat, stan)

    assert await _aktualne(swiat, _workflow(run_id)) is False


async def test_polecenie_run_workflow_bez_run_id_jest_odrzucane(swiat):
    assert await _aktualne(swiat, _workflow(None)) is False


async def test_polecenie_inne_niz_krok_runu_jest_zawsze_aktualne_nawet_z_id_przerwanego_runu(swiat):
    run_id = await _run_w_stanie(swiat, RunStatus.aborted)

    assert await _aktualne(swiat, {"type": "ping", "run_id": run_id}) is True


async def test_polecenie_run_workflow_cudzego_runnera_jest_odrzucane(swiat):
    run_id = await _run_w_stanie(swiat, RunStatus.running, runner_id=swiat.wiekszy)

    assert await _aktualne(swiat, _workflow(run_id), runner_id=swiat.mniejszy) is False


async def test_polecenie_run_workflow_runu_innego_zespolu_jest_odrzucane(swiat):
    run_id = await _run_w_stanie(swiat, RunStatus.running)

    assert await _aktualne(swiat, _workflow(run_id), tenant_id=swiat.piekarnia) is False


async def test_przerwany_run_nie_wysyla_juz_polecenia_run_workflow_po_powrocie_runnera(swiat):
    """Krok szkieletu i run_workflow dziela regule: polecenie runu nieaktywnego nie wraca."""
    run_id = await _run_w_stanie(swiat, RunStatus.aborted)
    krok = {"type": "run_step", "run_id": run_id, "step_id": "s0", "operacja": "szkielet"}

    assert await _aktualne(swiat, krok) is False
    assert await _aktualne(swiat, _workflow(run_id)) is False
    async with swiat.maker() as s:
        liczba = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
    assert liczba == 1

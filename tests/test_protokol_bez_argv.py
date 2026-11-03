"""Kokpit nie przekazuje runnerowi poleceń; runner wykonuje tylko to, co sam ma na liście (HAR-159).

Dwie strony protokołu:
- kokpit: `POST /api/runs` nie przyjmuje pola `command`, a do kolejki runnera trafia
  nazwa operacji (sprawdzamy dane zdarzenia w kolejce, nie sam kod HTTP),
- runner: nazwę operacji rozwiązuje na argv z własnej, lokalnej listy; polecenie z `command`
  albo z nieznaną operacją kończy się `step_failed` i nie uruchamia żadnego procesu.

Nazwy testów w języku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, Runner
from procesy import czekaj_na_warunek, zadanie_tla, zapamietaj_proces
from runner import client as runner_client
from runner.bufor import BrakMiejsca, Bufor
from runner.client import POWOD_SPOZA_LISTY, Runner as RunnerKlient, argv_operacji
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

KORZEN = Path(__file__).resolve().parent.parent
TOKEN_RUNNERA = "protokol-bez-argv-token-7391"
ARGV_SZKIELET = ("echo", "szkielet dziala")
LIMIT_S = 10


# --- kokpit: zdarzenie w kolejce runnera ---------------------------------------------

@pytest.fixture
async def kokpit(tmp_path, monkeypatch):
    """Zespół z jednym runnerem online; zwraca (klient HTTP, id runnera)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        s.add(Runner(tenant_id=tenant_id, name="macbook",
                     token_hash=hashlib.sha256(b"tajne").hexdigest(),
                     last_seen_at=db_session_teraz()))
        await s.commit()
    async with db_session.SessionLocal() as s:
        runner_id = (await s.execute(select(Runner.id))).scalars().one()
    bus.mark_runner_connected(runner_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield c, runner_id
    finally:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        getattr(bus, "_rozlaczone", {}).pop(runner_id, None)
        bus._runner_queues.pop(runner_id, None)
        await engine.dispose()


def db_session_teraz():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _zdarzenia_w_kolejce(runner_id: str) -> list[dict]:
    kolejka = bus.runner_queue(runner_id)
    wynik = []
    while not kolejka.empty():
        wynik.append(kolejka.get_nowait())
    return wynik


async def _liczba_runow() -> int:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(Run))).scalar_one()


async def test_uruchomienie_runu_wysyla_runnerowi_nazwe_operacji_a_nie_polecenie(kokpit):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={"workflow": "szkielet", "operacja": "szkielet"})

    assert r.status_code == 200
    assert _zdarzenia_w_kolejce(runner_id) == [
        {"type": "run_step", "run_id": r.json()["run_id"], "step_id": "s0",
         "operacja": "szkielet"}]


async def test_uruchomienie_runu_bez_podanej_operacji_wysyla_runnerowi_operacje_szkielet(kokpit):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={})

    assert r.status_code == 200
    (polecenie,) = _zdarzenia_w_kolejce(runner_id)
    assert polecenie["operacja"] == "szkielet"
    assert "command" not in polecenie


async def test_uruchomienie_runu_wysyla_runnerowi_operacje_wskazana_przez_uzytkownika(kokpit):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={"operacja": "inna_nazwa"})

    assert r.status_code == 200
    (polecenie,) = _zdarzenia_w_kolejce(runner_id)
    assert polecenie["operacja"] == "inna_nazwa"
    assert polecenie["run_id"] == r.json()["run_id"]


async def test_kokpit_przyjmuje_operacje_poprawna_skladniowo_choc_runner_jej_nie_zna(kokpit):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={"operacja": "nieznana"})

    assert r.status_code == 200
    (polecenie,) = _zdarzenia_w_kolejce(runner_id)
    assert polecenie["operacja"] == "nieznana"
    assert await _liczba_runow() == 1


@pytest.mark.parametrize("command", [
    ["echo", "szkielet dziala"],       # wartość, którą stary model przyjmował
    ["rm", "-rf", "/"],
    [],
    "rm -rf /",
    None,
])
async def test_kokpit_nie_moze_kazac_runnerowi_wykonac_dowolnej_komendy(kokpit, command):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={"workflow": "szkielet", "command": command})

    assert r.status_code == 422
    assert await _liczba_runow() == 0
    assert _zdarzenia_w_kolejce(runner_id) == []


async def test_kokpit_odrzuca_command_nawet_przy_poprawnej_operacji(kokpit):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={"operacja": "szkielet", "command": ["echo"]})

    assert r.status_code == 422
    assert await _liczba_runow() == 0
    assert _zdarzenia_w_kolejce(runner_id) == []


@pytest.mark.parametrize("operacja", [
    "ze spacją",
    "a b",
    "a\x00b",
    "a\tb",
    "a\nb",
    "szkielet\n",
    "",
    "a" * 65,
    "ąę",
    "a/b",
    "a;b",
    "../szkielet",
    "a$b",
])
async def test_kokpit_odrzuca_nazwe_operacji_ze_znakami_spoza_dozwolonych_albo_za_dluga(
    kokpit, operacja,
):
    klient, runner_id = kokpit

    r = await klient.post("/api/runs", json={"operacja": operacja})

    assert r.status_code == 422
    assert await _liczba_runow() == 0
    assert _zdarzenia_w_kolejce(runner_id) == []


@pytest.mark.parametrize("operacja", [
    "a" * 64,
    "a.b",
    "a-b",
    "a_b",
    "A",
    "Z",
    "0",
    "9",
    "Aa09_.-" * 9 + "A",     # 64 znaki ze wszystkich klas
])
async def test_kokpit_przyjmuje_nazwe_operacji_ze_znakow_dozwolonych_do_64_znakow(kokpit, operacja):
    klient, runner_id = kokpit
    assert len(operacja) <= 64

    r = await klient.post("/api/runs", json={"operacja": operacja})

    assert r.status_code == 200
    (polecenie,) = _zdarzenia_w_kolejce(runner_id)
    assert polecenie["operacja"] == operacja


# --- runner: rozwiązywanie operacji na argv z lokalnej listy -----------------------------

def test_lista_operacji_runnera_jest_tylko_do_odczytu_i_zna_tylko_szkielet():
    assert isinstance(runner_client.OPERACJE_RUNU, MappingProxyType)
    assert dict(runner_client.OPERACJE_RUNU) == {"szkielet": ARGV_SZKIELET}
    with pytest.raises(TypeError):
        runner_client.OPERACJE_RUNU["czekaj"] = ("sleep", "30")     # type: ignore[index]


def test_powod_odmowy_to_operacja_spoza_listy_dozwolonych():
    assert POWOD_SPOZA_LISTY == "operacja spoza listy dozwolonych"


def test_znana_operacja_daje_argv_z_listy_runnera():
    wynik = argv_operacji({"type": "run_step", "run_id": "r", "step_id": "s0",
                           "operacja": "szkielet"})

    assert wynik == ARGV_SZKIELET
    assert type(wynik) is tuple


def test_argv_bierze_sie_z_biezacej_listy_runnera_a_nie_z_zaszytej_nazwy(monkeypatch):
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"inna": ("true", "x")})

    assert argv_operacji({"operacja": "inna"}) == ("true", "x")
    assert argv_operacji({"operacja": "szkielet"}) is None


@pytest.mark.parametrize("command", [["echo", "x"], [], None, "echo x"])
def test_polecenie_z_kluczem_command_jest_odrzucone_nawet_przy_znanej_operacji(command):
    assert argv_operacji({"operacja": "szkielet", "command": command}) is None


@pytest.mark.parametrize("command", [["echo", "x"], [], None])
def test_polecenie_tylko_z_kluczem_command_bez_operacji_jest_odrzucone(command):
    assert argv_operacji({"type": "run_step", "command": command}) is None


def test_operacja_spoza_listy_runnera_nie_ma_argv():
    assert argv_operacji({"operacja": "nieznana"}) is None


def test_polecenie_bez_nazwy_operacji_nie_ma_argv():
    assert argv_operacji({"type": "run_step", "run_id": "r", "step_id": "s0"}) is None


@pytest.mark.parametrize("operacja", [None, ["szkielet"], 1, True, {"szkielet": 1}])
def test_nazwa_operacji_inna_niz_napis_jest_odrzucona_bez_wyjatku(operacja):
    assert argv_operacji({"operacja": operacja}) is None


def test_nazwa_operacji_bedaca_liczba_nie_trafia_na_liste_nawet_gdy_lista_ma_taki_klucz(
    monkeypatch,
):
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {1: ("echo", "liczba")})

    assert argv_operacji({"operacja": 1}) is None


# --- runner: strumień poleceń z kokpitu ------------------------------------------------

POLACZONO = b'data: {"type": "polaczono"}\n\n'


class _StrumienPolecen(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield POLACZONO
        for polecenie in self.polecenia:
            yield b"data: " + json.dumps(polecenie).encode() + b"\n\n"
        await asyncio.Event().wait()

    def __init__(self, polecenia: list[dict]):
        self.polecenia = polecenia


class _AtrapaKokpitu(httpx.AsyncBaseTransport):
    """Kokpit: rejestracja, strumień z gotowymi poleceniami, heartbeat, przyjmowanie zdarzeń."""

    def __init__(self, polecenia: list[dict]):
        self.polecenia = polecenia
        self.zadania: list[httpx.Request] = []

    def zdarzenia(self) -> list[dict]:
        return [json.loads(z.content) for z in self.zadania
                if z.url.path.endswith("/api/events")]

    def kind_i_tekst(self, run_id: str, step_id: str) -> list[tuple[str, str]]:
        return [(z["kind"], z["text"]) for z in self.zdarzenia()
                if z["run_id"] == run_id and z["step_id"] == step_id]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.zadania.append(request)
        sciezka = request.url.path
        if sciezka.endswith("/api/runners/register"):
            return httpx.Response(200, json={"runner_id": "r1", "tenant_id": "t1",
                                             "heartbeat_co_s": 15})
        if sciezka.endswith("/api/runners/stream"):
            return httpx.Response(200, stream=_StrumienPolecen(self.polecenia),
                                  headers={"content-type": "text/event-stream"})
        if sciezka.endswith("/api/runners/heartbeat") or sciezka.endswith("/api/events"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)


def _krok(run_id: str, step_id: str, **pola) -> dict:
    return {"type": "run_step", "run_id": run_id, "step_id": step_id, **pola}


@asynccontextmanager
async def _runner_ze_strumieniem(polecenia: list[dict], monkeypatch):
    """Prawdziwy `Runner.listen` na atrapie kokpitu; zwraca (atrapa, argv wszystkich
    uruchomionych procesów). Procesy startują przez prawdziwe `create_subprocess_exec`."""
    uruchomione: list[tuple] = []
    prawdziwa = asyncio.create_subprocess_exec

    async def exec_z_rejestrem(*args, **kwargs):
        proc = await prawdziwa(*args, **kwargs)
        zapamietaj_proces(proc.pid)
        uruchomione.append(args)
        return proc

    monkeypatch.setattr(runner_client.asyncio, "create_subprocess_exec", exec_z_rejestrem)
    atrapa = _AtrapaKokpitu(polecenia)
    runner = RunnerKlient("http://kokpit.test", TOKEN_RUNNERA, transport=atrapa)
    async with zadanie_tla(runner.listen(), "nasluch runnera") as nasluch:
        try:
            yield atrapa, uruchomione, nasluch
        finally:
            await runner.stop()


async def _poczekaj_na_koniec_kroku(atrapa: _AtrapaKokpitu, run_id: str, step_id: str) -> None:
    def gotowe() -> bool:
        return any(kind in ("step_finished", "step_failed")
                   for kind, _ in atrapa.kind_i_tekst(run_id, step_id))

    await czekaj_na_warunek(gotowe, LIMIT_S, f"koniec kroku {run_id}/{step_id}")


async def test_runner_wykonuje_znana_operacje_z_lokalnej_listy_i_zglasza_jej_wynik(monkeypatch):
    polecenia = [_krok("run-1", "s0", operacja="szkielet")]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, _):
        await _poczekaj_na_koniec_kroku(atrapa, "run-1", "s0")

        assert uruchomione == [ARGV_SZKIELET]
        assert atrapa.kind_i_tekst("run-1", "s0") == [
            ("output", "szkielet dziala"), ("step_finished", "kod wyjscia 0")]


async def test_runner_uruchamia_argv_z_biezacej_listy_operacji_a_nie_zaszyte_polecenie(
    monkeypatch,
):
    argv = (sys.executable, "-c", "print('z listy lokalnej')")
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"drukuj": argv})
    polecenia = [_krok("run-1", "s0", operacja="drukuj")]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, _):
        await _poczekaj_na_koniec_kroku(atrapa, "run-1", "s0")

        assert uruchomione == [argv]
        assert atrapa.kind_i_tekst("run-1", "s0") == [
            ("output", "z listy lokalnej"), ("step_finished", "kod wyjscia 0")]


@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "print('z kokpitu')"], [], None])
async def test_runner_odrzuca_polecenie_z_argv_nawet_przy_znanej_operacji(monkeypatch, command):
    polecenia = [
        _krok("run-zly", "s7", operacja="szkielet", command=command),
        _krok("run-ok", "s1", operacja="szkielet"),
    ]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, _):
        await _poczekaj_na_koniec_kroku(atrapa, "run-ok", "s1")

        assert atrapa.kind_i_tekst("run-zly", "s7") == [
            ("step_failed", "operacja spoza listy dozwolonych")]
        assert uruchomione == [ARGV_SZKIELET]       # tylko drugie polecenie uruchomiło proces


async def test_runner_nie_uruchamia_procesu_dla_operacji_spoza_listy(monkeypatch):
    polecenia = [
        _krok("run-zly", "s7", operacja="nieznana"),
        _krok("run-ok", "s1", operacja="szkielet"),
    ]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, _):
        await _poczekaj_na_koniec_kroku(atrapa, "run-ok", "s1")

        assert atrapa.kind_i_tekst("run-zly", "s7") == [
            ("step_failed", "operacja spoza listy dozwolonych")]
        assert uruchomione == [ARGV_SZKIELET]


@pytest.mark.parametrize("pola", [
    {},                                         # brak nazwy operacji
    {"operacja": None},
    {"operacja": ["szkielet"]},
    {"operacja": 1},
    {"command": [sys.executable, "-c", "print('stary protokol')"]},   # stary protokół
])
async def test_runner_odrzuca_polecenie_bez_poprawnej_nazwy_operacji_i_dalej_obsluguje_strumien(
    monkeypatch, pola,
):
    polecenia = [
        _krok("run-zly", "s7", **pola),
        _krok("run-ok", "s1", operacja="szkielet"),
    ]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, nasluch):
        await _poczekaj_na_koniec_kroku(atrapa, "run-ok", "s1")

        assert atrapa.kind_i_tekst("run-zly", "s7") == [
            ("step_failed", "operacja spoza listy dozwolonych")]
        assert atrapa.kind_i_tekst("run-ok", "s1")[-1] == ("step_finished", "kod wyjscia 0")
        assert uruchomione == [ARGV_SZKIELET]
        assert not nasluch.done()                   # nasłuch żyje: odmowa nie zrywa strumienia


KOMUNIKAT_BEZ_ID = "polecenie bez run_id/step_id odrzucone: operacja spoza listy"
KOMUNIKAT_BRAK_MIEJSCA = "brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)"


@pytest.mark.parametrize("pola", [
    {"step_id": "s7"},                              # brak run_id
    {"run_id": 5, "step_id": "s7"},                 # run_id nie jest napisem
    {"run_id": "run-zly"},                          # brak step_id
    {"run_id": "run-zly", "step_id": 7},            # step_id nie jest napisem
    {"run_id": None, "step_id": None},
])
async def test_polecenie_spoza_listy_bez_identyfikatorow_kroku_nie_trafia_do_kokpitu_i_strumien_zyje(
    monkeypatch, capsys, pola,
):
    polecenia = [
        {"type": "run_step", "operacja": "nieznana", **pola},
        _krok("run-ok", "s1", operacja="szkielet"),
    ]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, nasluch):
        await _poczekaj_na_koniec_kroku(atrapa, "run-ok", "s1")

        # do kokpitu dociera wylacznie drugie polecenie: odmowa bez adresata nie ma dokad pojsc
        assert {z["run_id"] for z in atrapa.zdarzenia()} == {"run-ok"}
        assert atrapa.kind_i_tekst("run-ok", "s1")[-1] == ("step_finished", "kod wyjscia 0")
        assert uruchomione == [ARGV_SZKIELET]
        assert not nasluch.done()
        assert capsys.readouterr().err.splitlines().count(KOMUNIKAT_BEZ_ID) == 1  # cala linia


async def test_runner_otwiera_strumien_polecen_metoda_get(monkeypatch):
    metody: list[str] = []
    prawdziwy_stream = httpx.AsyncClient.stream

    def stream_z_rejestrem(self, method, url, **kw):
        metody.append(method)
        return prawdziwy_stream(self, method, url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "stream", stream_z_rejestrem)
    polecenia = [_krok("run-ok", "s1", operacja="szkielet")]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, _, _n):
        await _poczekaj_na_koniec_kroku(atrapa, "run-ok", "s1")

        assert metody == ["GET"]


async def test_brak_miejsca_przy_zapisie_odmowy_konczy_krok_przerwaniem_z_powodem_i_strumien_zyje(
    monkeypatch,
):
    prawdziwe_dopisz = Bufor.dopisz

    def dopisz_z_pelnym_dyskiem_dla_odmowy(self, zdarzenie):
        if zdarzenie.get("run_id") == "run-zly":
            raise BrakMiejsca("zapis run-zly.ndjson: OSError")
        return prawdziwe_dopisz(self, zdarzenie)

    monkeypatch.setattr(Bufor, "dopisz", dopisz_z_pelnym_dyskiem_dla_odmowy)
    polecenia = [
        _krok("run-zly", "s7", operacja="nieznana"),
        _krok("run-ok", "s1", operacja="szkielet"),
    ]
    async with _runner_ze_strumieniem(polecenia, monkeypatch) as (atrapa, uruchomione, nasluch):
        await _poczekaj_na_koniec_kroku(atrapa, "run-ok", "s1")

        # odmowa nie zmiescila sie w buforze, wiec kokpit dostaje bezposrednie przerwanie
        assert atrapa.kind_i_tekst("run-zly", "s7") == [("step_aborted", KOMUNIKAT_BRAK_MIEJSCA)]
        assert atrapa.kind_i_tekst("run-ok", "s1")[-1] == ("step_finished", "kod wyjscia 0")
        assert uruchomione == [ARGV_SZKIELET]
        assert not nasluch.done()


def test_zatwierdzony_schemat_kontraktu_ma_run_create_zgodny_z_kodem_kokpitu():
    plik = json.loads((KORZEN / "web" / "openapi.json").read_text(encoding="utf-8"))
    schemat = plik["components"]["schemas"]["RunCreate"]

    assert schemat == app.openapi()["components"]["schemas"]["RunCreate"]
    assert schemat["additionalProperties"] is False
    assert schemat["properties"]["operacja"]["maxLength"] == 64
    assert "command" not in schemat["properties"]

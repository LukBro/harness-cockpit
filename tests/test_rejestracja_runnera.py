"""Rejestracja runnera: wersja, backendy i sposob uwierzytelnienia (HAR-34).

Skutki w bazie sprawdzamy odczytem z bazy. Runner idzie przez produkcyjny
`Runner.register`; transport HTTP jest przechwycony (`httpx.MockTransport`).
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Runner, Tenant
from runner import __main__ as runner_main
from runner import client as runner_client
from runner.client import Runner as RunnerKlient

TOKEN = "tajne"
NAGLOWEK = {"Authorization": f"Bearer {TOKEN}"}
KLUCZ_MODELU = "sk-ant-ROZPOZNAWALNY-klucz-modelu-0451"


@pytest.fixture
async def klient(tmp_path, monkeypatch):
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
                     token_hash=hashlib.sha256(TOKEN.encode()).hexdigest()))
        await s.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        await engine.dispose()


async def _runner_z_bazy() -> Runner:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Runner))).scalars().one()


async def _zamelduj(klient, **body):
    return await klient.post("/api/runners/register", json=body, headers=NAGLOWEK)


# --- kokpit zapisuje deklaracje ------------------------------------------------

async def test_rejestracja_zapisuje_wersje_i_backendy_ze_sposobem_uwierzytelnienia(klient):
    r = await _zamelduj(klient, name="macbook", version="1.2.3", backends=[
        {"nazwa": "claude", "auth": "subscription"},
        {"nazwa": "codex", "auth": "api_key"},
    ])
    assert r.status_code == 200

    runner = await _runner_z_bazy()
    assert runner.version == "1.2.3"
    assert runner.backends == [
        {"nazwa": "claude", "auth": "subscription"},
        {"nazwa": "codex", "auth": "api_key"},
    ]


@pytest.fixture
def strefa_procesu_daleko_od_utc(monkeypatch):
    """Strefa procesu UTC+9: czas lokalny rozni sie od UTC o 9 h, wiec zapis
    czasu lokalnego zamiast uniwersalnego nie moze przejsc niezauwazony."""
    tz_przed = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Etc/GMT-9")
    time.tzset()
    yield
    # Przywroc TZ od razu (bez monkeypatch.undo(), ktore cofnaloby tez podmiane
    # bazy z fixture `klient`), a potem przeladuj strefe procesu.
    if tz_przed is None:
        monkeypatch.delenv("TZ")
    else:
        monkeypatch.setenv("TZ", tz_przed)
    time.tzset()


async def test_rejestracja_zapamietuje_chwile_zameldowania_w_czasie_uniwersalnym(
    klient, strefa_procesu_daleko_od_utc
):
    assert (await _runner_z_bazy()).last_seen_at is None

    przed = datetime.now(timezone.utc)
    r = await _zamelduj(klient, name="macbook")
    po = datetime.now(timezone.utc)
    assert r.status_code == 200

    zapisany = (await _runner_z_bazy()).last_seen_at
    assert zapisany is not None
    # SQLite oddaje czas bez strefy; kokpit zapisuje UTC, wiec tak go czytamy.
    if zapisany.tzinfo is None:
        zapisany = zapisany.replace(tzinfo=timezone.utc)
    zapas = timedelta(seconds=60)
    assert przed - zapas <= zapisany <= po + zapas


async def test_ponowna_rejestracja_zastepuje_deklaracje_backendow_a_nie_dopisuje(klient):
    await _zamelduj(klient, name="macbook", version="1.0.0", backends=[
        {"nazwa": "claude", "auth": "subscription"},
        {"nazwa": "codex", "auth": "api_key"},
    ])
    await _zamelduj(klient, name="macbook", version="1.1.0", backends=[
        {"nazwa": "claude", "auth": "api_key"},
    ])

    runner = await _runner_z_bazy()
    assert runner.backends == [{"nazwa": "claude", "auth": "api_key"}]
    assert runner.version == "1.1.0"


async def test_ponowna_rejestracja_z_pusta_lista_czysci_wczesniej_zadeklarowane_backendy(klient):
    await _zamelduj(klient, name="macbook", backends=[
        {"nazwa": "claude", "auth": "subscription"}])
    await _zamelduj(klient, name="macbook", backends=[])

    assert (await _runner_z_bazy()).backends == []


async def test_rejestracja_bez_pola_backendow_zostawia_pusta_liste(klient):
    r = await _zamelduj(klient, name="macbook")
    assert r.status_code == 200

    assert (await _runner_z_bazy()).backends == []


async def test_nieznany_sposob_uwierzytelnienia_daje_422_i_nie_zmienia_deklaracji(klient):
    await _zamelduj(klient, name="macbook", version="1.0.0", backends=[
        {"nazwa": "claude", "auth": "subscription"}])

    r = await _zamelduj(klient, name="przemianowany", version="2.0.0", backends=[
        {"nazwa": "claude", "auth": "haslo"}])
    assert r.status_code == 422

    runner = await _runner_z_bazy()
    assert runner.backends == [{"nazwa": "claude", "auth": "subscription"}]
    assert runner.version == "1.0.0"
    assert runner.name == "macbook"


async def test_backend_bez_sposobu_uwierzytelnienia_daje_422_i_nie_zmienia_deklaracji(klient):
    await _zamelduj(klient, name="macbook", backends=[
        {"nazwa": "claude", "auth": "api_key"}])

    r = await _zamelduj(klient, name="macbook", backends=[{"nazwa": "claude"}])
    assert r.status_code == 422

    assert (await _runner_z_bazy()).backends == [{"nazwa": "claude", "auth": "api_key"}]


# --- runner wykrywa backendy ---------------------------------------------------

def _claude_w_path(monkeypatch, jest: bool) -> None:
    monkeypatch.setattr(
        runner_client.shutil, "which",
        lambda nazwa, *a, **k: "/usr/bin/claude" if jest and nazwa == "claude" else None,
    )


def test_claude_w_path_z_kluczem_w_srodowisku_to_backend_z_kluczem_api(monkeypatch):
    _claude_w_path(monkeypatch, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KLUCZ_MODELU)

    assert runner_client.wykryj_backendy() == [{"nazwa": "claude", "auth": "api_key"}]


def test_claude_w_path_bez_klucza_w_srodowisku_to_backend_z_subskrypcja(monkeypatch):
    _claude_w_path(monkeypatch, True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert runner_client.wykryj_backendy() == [{"nazwa": "claude", "auth": "subscription"}]


def test_pusty_klucz_w_srodowisku_liczy_sie_jak_brak_klucza(monkeypatch):
    _claude_w_path(monkeypatch, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    assert runner_client.wykryj_backendy() == [{"nazwa": "claude", "auth": "subscription"}]


def test_brak_claude_w_path_nie_daje_zadnego_backendu_nawet_z_kluczem(monkeypatch):
    _claude_w_path(monkeypatch, False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KLUCZ_MODELU)

    assert runner_client.wykryj_backendy() == []


def test_wykryte_backendy_nie_zawieraja_wartosci_klucza_modelu(monkeypatch):
    _claude_w_path(monkeypatch, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KLUCZ_MODELU)

    assert KLUCZ_MODELU not in json.dumps(runner_client.wykryj_backendy())


# --- runner wysyla deklaracje --------------------------------------------------

async def _zarejestruj_i_przechwyc(base="http://kokpit.test") -> httpx.Request:
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200, json={"runner_id": "r-1", "tenant_id": "t-1"})

    runner = RunnerKlient(base, "tajny-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await runner.register(c)
    assert len(wyslane) == 1
    return wyslane[0]


async def test_rejestracja_runnera_wysyla_wykryte_backendy_ze_sposobem_uwierzytelnienia(monkeypatch):
    _claude_w_path(monkeypatch, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KLUCZ_MODELU)

    zadanie = await _zarejestruj_i_przechwyc()

    assert zadanie.url.path == "/api/runners/register"
    assert json.loads(zadanie.content)["backends"] == [{"nazwa": "claude", "auth": "api_key"}]


async def test_rejestracja_runnera_bez_claude_wysyla_pusta_liste_backendow(monkeypatch):
    _claude_w_path(monkeypatch, False)

    zadanie = await _zarejestruj_i_przechwyc()

    assert json.loads(zadanie.content)["backends"] == []


async def test_rejestracja_runnera_nie_wysyla_wartosci_klucza_modelu(monkeypatch):
    _claude_w_path(monkeypatch, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KLUCZ_MODELU)

    zadanie = await _zarejestruj_i_przechwyc()

    assert KLUCZ_MODELU.encode() not in zadanie.content
    assert "ROZPOZNAWALNY" not in zadanie.content.decode()
    assert KLUCZ_MODELU not in str(zadanie.headers)


async def test_rejestracja_runnera_nie_loguje_wartosci_klucza_modelu(monkeypatch, capfd):
    _claude_w_path(monkeypatch, True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KLUCZ_MODELU)

    await _zarejestruj_i_przechwyc()

    wyjscie = capfd.readouterr()
    assert "ROZPOZNAWALNY" not in wyjscie.out + wyjscie.err


async def test_rejestracja_runnera_wysyla_wersje_z_metadanych_pakietu(monkeypatch):
    _claude_w_path(monkeypatch, False)
    monkeypatch.setattr(importlib.metadata, "version", lambda nazwa: "9.9.9-test")

    zadanie = await _zarejestruj_i_przechwyc()

    assert json.loads(zadanie.content)["version"] == "9.9.9-test"


async def test_wersja_runnera_to_wersja_zainstalowanego_pakietu_harness_cockpit(monkeypatch):
    _claude_w_path(monkeypatch, False)
    oczekiwana = importlib.metadata.version("harness-cockpit")

    zadanie = await _zarejestruj_i_przechwyc()

    assert json.loads(zadanie.content)["version"] == oczekiwana
    assert runner_client.wersja_runnera() == oczekiwana


async def test_runner_bez_zainstalowanego_pakietu_melduje_jawnie_nieznana_wersje(monkeypatch):
    _claude_w_path(monkeypatch, False)

    def brak_pakietu(nazwa):
        raise importlib.metadata.PackageNotFoundError(nazwa)

    monkeypatch.setattr(importlib.metadata, "version", brak_pakietu)

    assert runner_client.wersja_runnera() == "0+nieznana"
    zadanie = await _zarejestruj_i_przechwyc()
    assert json.loads(zadanie.content)["version"] == "0+nieznana"


# --- czytelna odmowa przy niewaznym tokenie ------------------------------------

TOKEN_RUNNERA = "zxqv7-NIEWAZNY-tokenrunnera-482913"


def _main_z_kokpitem_odrzucajacym_token(monkeypatch) -> list[httpx.Request]:
    """Produkcyjny `Runner` (register i listen), tylko transport HTTP podmieniony:
    kokpit odpowiada 401 na kazde zadanie."""
    zadania: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        zadania.append(request)
        return httpx.Response(401, json={"detail": "token nieznany"})

    def runner_z_atrapa_sieci(base, token):
        return RunnerKlient(base, token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(runner_main, "Runner", runner_z_atrapa_sieci)
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN_RUNNERA)
    monkeypatch.setenv("COCKPIT_URL", "http://kokpit.test")
    return zadania


def test_runner_z_niewaznym_tokenem_konczy_sie_kodem_2(monkeypatch):
    zadania = _main_z_kokpitem_odrzucajacym_token(monkeypatch)

    with pytest.raises(SystemExit) as e:
        runner_main.main()

    assert zadania, "runner nie dotarl do kokpitu - test nie przechodzi przez rejestracje"
    assert e.value.code == 2


def test_odmowa_tokenu_mowi_co_zrobic_gdzie_wygenerowac_i_gdzie_zapisac(monkeypatch, capsys):
    _main_z_kokpitem_odrzucajacym_token(monkeypatch)

    with pytest.raises(SystemExit):
        runner_main.main()

    err = capsys.readouterr().err
    assert "odrzucon" in err
    assert "Ustawieniach" in err
    assert "COCKPIT_RUNNER_TOKEN" in err
    assert "credentials.json" in err


def test_odmowa_tokenu_nie_pokazuje_sladu_stosu(monkeypatch, capsys):
    _main_z_kokpitem_odrzucajacym_token(monkeypatch)

    with pytest.raises(SystemExit):
        runner_main.main()

    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "TokenOdrzucony" not in err


def test_odmowa_tokenu_nie_ujawnia_tokenu_ani_jego_fragmentow(monkeypatch, capsys):
    _main_z_kokpitem_odrzucajacym_token(monkeypatch)

    with pytest.raises(SystemExit):
        runner_main.main()

    wyjscie = capsys.readouterr()
    for strumien in (wyjscie.err, wyjscie.out):
        assert TOKEN_RUNNERA not in strumien
        assert TOKEN_RUNNERA[:8] not in strumien     # prefiks (tak maskuje secrets.mask)
        assert TOKEN_RUNNERA[-6:] not in strumien    # sufiks
        assert "NIEWAZNY" not in strumien


def test_odmowa_tokenu_przy_otwieraniu_strumienia_tez_konczy_sie_czytelnym_kodem_2(
    monkeypatch, capsys
):
    """Rejestracja przechodzi, 401 dopiero na strumieniu - ta sama czytelna odmowa."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runners/register":
            return httpx.Response(200, json={"runner_id": "r-1", "tenant_id": "t-1"})
        return httpx.Response(401)

    monkeypatch.setattr(
        runner_main, "Runner",
        lambda base, token: RunnerKlient(base, token, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN_RUNNERA)

    with pytest.raises(SystemExit) as e:
        runner_main.main()

    err = capsys.readouterr().err
    assert e.value.code == 2
    assert "COCKPIT_RUNNER_TOKEN" in err and "credentials.json" in err
    assert "Traceback" not in err and TOKEN_RUNNERA[:8] not in err

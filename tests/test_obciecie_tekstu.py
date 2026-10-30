"""HAR-56: tekst zdarzenia ponad limit nigdy nie udaje calosci.

Kokpit zapisuje znacznik obciecia i pelna dlugosc, a API je oddaje (historia runu
i strumien dla przegladarki). Skutek czytamy z bazy nowa sesja, przez prawdziwa
aplikacje i prawdziwy serwis. Limit obnizamy w `settings`, zeby nie budowac
64 000 znakow w kazdym tescie.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus
from cockpit.services import runs as uslugi_runow
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela
from cockpit.settings import Settings, settings

TOKEN = "token-wlasciciela"
LIMIT = 10  # obnizony limit na czas testu


@dataclass
class Swiat:
    klient: AsyncClient
    run_id: str


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # klient ma sesje wlasciciela zespolu: GET /api/runs/{id} czyta run zespolu z sesji
    wlasciciel_konta, browarek_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        wlasciciel = Runner(
            tenant_id=browarek_id, name="wlasciciel", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        s.add(wlasciciel)
        await s.flush()
        run = Run(tenant_id=browarek_id, runner_id=wlasciciel.id, status=RunStatus.running)
        s.add(run)
        await s.commit()
        run_id = run.id
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel_konta)
            yield Swiat(c, run_id)
    finally:
        await engine.dispose()


@pytest.fixture
def limit(monkeypatch):
    # raising=False: przed implementacja pole nie istnieje; test ma byc czerwony przez asercje
    monkeypatch.setattr(settings, "limit_tekstu_zdarzenia", LIMIT, raising=False)
    return LIMIT


def _tresc(run_id: str, **pola) -> dict:
    return {"run_id": run_id, "kind": "output", "step_id": "s0",
            "event_id": str(uuid.uuid4()), **pola}


def _post(swiat: Swiat, tresc: dict):
    return swiat.klient.post("/api/events", json=tresc,
                             headers={"Authorization": f"Bearer {TOKEN}"})


async def _zdarzenia_w_bazie(run_id: str) -> list[Event]:
    async with db_session.SessionLocal() as s:
        return list((await s.execute(
            select(Event).where(Event.run_id == run_id).order_by(Event.seq))).scalars().all())


async def _run_w_bazie(run_id: str) -> Run:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


async def _jedno_zdarzenie(swiat: Swiat, **pola) -> Event:
    r = await _post(swiat, _tresc(swiat.run_id, **pola))
    assert r.status_code == 200, r.text
    (ev,) = await _zdarzenia_w_bazie(swiat.run_id)
    return ev


# --- tekst ponad limit ---

async def test_tekst_dluzszy_niz_limit_jest_zapisany_jako_obciety_z_pelna_dlugoscia(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="a" * 25)

    assert ev.text == "a" * limit
    assert ev.text_truncated is True
    assert ev.text_full_len == 25


async def test_tekst_o_dlugosci_rownej_limitowi_nie_jest_obcinany(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="b" * limit)

    assert ev.text == "b" * limit
    assert ev.text_truncated is False
    assert ev.text_full_len == limit


async def test_tekst_o_jeden_znak_dluzszy_niz_limit_jest_obciety_do_limitu(swiat, limit):
    tekst = "0123456789X"
    ev = await _jedno_zdarzenie(swiat, text=tekst)

    assert ev.text == tekst[:limit]
    assert ev.text_truncated is True
    assert ev.text_full_len == limit + 1


async def test_limit_liczy_znaki_a_nie_bajty(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="ą" * (limit + 3))

    assert ev.text == "ą" * limit
    assert ev.text_truncated is True
    assert ev.text_full_len == limit + 3


# --- krotki tekst ---

async def test_krotki_tekst_nie_jest_oznaczony_jako_obciety(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="krotko")

    assert ev.text == "krotko"
    assert ev.text_truncated is False
    assert ev.text_full_len == len("krotko")


async def test_zdarzenie_bez_tekstu_ma_pelna_dlugosc_zero_a_nie_brak(swiat, limit):
    ev = await _jedno_zdarzenie(swiat)

    assert ev.text == ""
    assert ev.text_truncated is False
    assert ev.text_full_len == 0


# --- runner uciol tekst sam ---

async def test_tekst_uciety_przez_runnera_jest_oznaczony_jako_obciety(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="krotki", text_full_len=500)

    assert ev.text == "krotki"  # tekst nietkniety: krotszy od limitu
    assert ev.text_truncated is True
    assert ev.text_full_len == 500


async def test_pelna_dlugosc_rowna_dlugosci_tekstu_nie_oznacza_obciecia(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="abcde", text_full_len=5)

    assert ev.text_truncated is False
    assert ev.text_full_len == 5


async def test_tekst_ponad_limit_uciety_tez_przez_runnera_zachowuje_zgloszona_pelna_dlugosc(
        swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="c" * 25, text_full_len=900)

    assert ev.text == "c" * limit
    assert ev.text_truncated is True
    assert ev.text_full_len == 900


# --- niespojne dane: pelna dlugosc krotsza niz tekst ---

async def test_pelna_dlugosc_krotsza_niz_tekst_jest_odrzucona_i_nic_nie_zapisano(swiat, limit):
    r = await _post(swiat, _tresc(swiat.run_id, text="abcde", text_full_len=3))

    assert r.status_code == 422
    # blad dziedziny niesie komunikat tekstowy; walidacja Pydantic oddalaby liste
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "text_full_len" in detail and "3 znakow" in detail and "5 znakow" in detail
    assert await _zdarzenia_w_bazie(swiat.run_id) == []


async def test_serwis_odrzuca_pelna_dlugosc_krotsza_niz_tekst_bledem_niespojne_dane(swiat, limit):
    from cockpit.services.errors import NiespojneDane

    run = await _run_w_bazie(swiat.run_id)
    async with db_session.SessionLocal() as s:
        with pytest.raises(NiespojneDane) as blad:
            await uslugi_runow.zapisz_zdarzenie(
                s, run, event_id=str(uuid.uuid4()), kind="output", step_id="s0",
                text="abcde", text_full_len=3)

    komunikat = str(blad.value)
    assert "text_full_len" in komunikat and "3 znakow" in komunikat and "5 znakow" in komunikat
    assert await _zdarzenia_w_bazie(swiat.run_id) == []


def test_niespojne_dane_koncza_sie_kodem_422():
    from cockpit.api.bledy import KODY_BLEDOW
    from cockpit.services.errors import NiespojneDane

    assert KODY_BLEDOW[NiespojneDane] == 422


# --- zakres text_full_len ---

async def test_ujemna_pelna_dlugosc_jest_odrzucona_walidacja_i_nic_nie_zapisano(swiat, limit):
    r = await _post(swiat, _tresc(swiat.run_id, text="", text_full_len=-1))

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # walidacja wejscia, nie regula dziedziny
    assert await _zdarzenia_w_bazie(swiat.run_id) == []


async def test_pelna_dlugosc_zero_przy_pustym_tekscie_jest_zapisana(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="", text_full_len=0)

    assert ev.text_truncated is False
    assert ev.text_full_len == 0


async def test_pelna_dlugosc_ponad_zakres_liczby_calkowitej_bazy_jest_odrzucona(swiat, limit):
    r = await _post(swiat, _tresc(swiat.run_id, text="x", text_full_len=2_147_483_648))

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert await _zdarzenia_w_bazie(swiat.run_id) == []


async def test_pelna_dlugosc_rowna_najwiekszej_liczbie_calkowitej_bazy_jest_zapisana(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, text="x", text_full_len=2_147_483_647)

    assert ev.text_truncated is True
    assert ev.text_full_len == 2_147_483_647


# --- powod przerwania kroku ---

async def test_powod_przerwania_runu_to_tekst_obciety_a_nie_pelny(swiat, limit):
    ev = await _jedno_zdarzenie(swiat, kind="step_aborted", text="p" * 25)

    run = await _run_w_bazie(swiat.run_id)
    assert run.status == RunStatus.aborted
    assert ev.text == "p" * limit
    assert run.reason == "p" * limit


# --- widocznosc znacznika w API ---

async def test_historia_runu_oddaje_znacznik_obciecia(swiat, limit):
    await _post(swiat, _tresc(swiat.run_id, text="d" * 25))
    await _post(swiat, _tresc(swiat.run_id, text="krotko"))

    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")

    assert r.status_code == 200
    ev = r.json()["events"]
    assert [(e["text_truncated"], e["text_full_len"]) for e in ev] == [(True, 25), (False, 6)]


async def test_strumien_dla_przegladarki_niesie_znacznik_obciecia_i_pelna_dlugosc(swiat, limit):
    kolejka = bus.subscribe_run(swiat.run_id)
    try:
        await _post(swiat, _tresc(swiat.run_id, text="e" * 25))
        await _post(swiat, _tresc(swiat.run_id, text="krotko"))

        obciete = kolejka.get_nowait()
        krotkie = kolejka.get_nowait()
    finally:
        bus.unsubscribe_run(swiat.run_id, kolejka)

    assert (obciete["text_truncated"], obciete["text_full_len"]) == (True, 25)
    assert (krotkie["text_truncated"], krotkie["text_full_len"]) == (False, 6)


async def test_zdarzenie_przerwania_dla_szyny_niesie_znacznik_obciecia_i_pelna_dlugosc(swiat):
    tenant_id = (await _run_w_bazie(swiat.run_id)).tenant_id
    async with db_session.SessionLocal() as s:
        s.add(Event(tenant_id=tenant_id, run_id=swiat.run_id, seq=1, step_id="",
                    event_id=str(uuid.uuid4()), kind="run_aborted", text="przerwano",
                    text_truncated=True, text_full_len=777))
        await s.commit()

    async with db_session.SessionLocal() as s:
        wynik = dict(await uslugi_runow.zdarzenia_przerwania(s, [swiat.run_id]))

    assert wynik[swiat.run_id]["text_truncated"] is True
    assert wynik[swiat.run_id]["text_full_len"] == 777


# --- konfiguracja limitu ---

def test_domyslny_limit_tekstu_zdarzenia_to_64000_znakow(monkeypatch):
    monkeypatch.delenv("COCKPIT_LIMIT_TEKSTU_ZDARZENIA", raising=False)

    assert Settings().limit_tekstu_zdarzenia == 64_000


def test_limit_tekstu_zdarzenia_zero_jest_odrzucony(monkeypatch):
    monkeypatch.delenv("COCKPIT_LIMIT_TEKSTU_ZDARZENIA", raising=False)

    with pytest.raises(ValidationError):
        Settings(limit_tekstu_zdarzenia=0)


def test_limit_tekstu_zdarzenia_jeden_jest_dozwolony(monkeypatch):
    monkeypatch.delenv("COCKPIT_LIMIT_TEKSTU_ZDARZENIA", raising=False)

    assert Settings(limit_tekstu_zdarzenia=1).limit_tekstu_zdarzenia == 1

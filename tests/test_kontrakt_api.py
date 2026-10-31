"""Kontrakt API (HAR-69): każda odpowiedź ma model, schemat OpenAPI leży w repo i CI pilnuje zgodności.

Trzy warstwy:
  * schemat prawdziwej aplikacji `cockpit.api.app.app` (modele odpowiedzi, opisy błędów),
  * parytet kształtu JSON: `response_model` nie zmienia tego, co dostaje front i runner,
  * `tools/eksport_openapi.py` (importem, bez podprocesów): zapis, `--sprawdz`, krok w CI.

Pliki wspólne tylko z istniejących helperów (`sesja_testowa`); fixture bazy jak w
`test_status_runu.py`. Kryteria odhacza Team Manager.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tomllib
import typing
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field, create_model
from ruamel.yaml import YAML
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sse_starlette.sse import EventSourceResponse
from starlette.routing import Mount, Route

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.api.schematy import Blad, odpowiedzi_bledow
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Runner, RunnerKind, RunStatus
from cockpit.settings import settings
from sesja_testowa import HASLO_TESTOWE, zaloguj_klienta, zaloz_wlasciciela

KORZEN = Path(__file__).resolve().parent.parent
NAGLOWEK_RUNNERA = {"Authorization": "Bearer tajne"}
SCHEMAT_BLEDU = {"$ref": "#/components/schemas/Blad"}

# Trasy wyłączone z kryterium „2xx z polami”: jawna lista, porównywana z rzeczywistością.
BEZ_TRESCI_204 = {
    ("POST", "/api/runners/odpowiedzi"),
    ("POST", "/api/konto/wyloguj"),
    ("POST", "/api/konto/wyloguj-wszedzie"),
    ("POST", "/api/konto/haslo"),
}
STRUMIENIE_SSE = {
    ("GET", "/api/runners/stream"),
    ("GET", "/api/runs/{run_id}/stream"),
}
TRASY_PUBLICZNE = {
    ("GET", "/api/health"),
    ("POST", "/api/konto/rejestracja"),
    ("POST", "/api/konto/logowanie"),
}
KODY_BLEDOW = {"401", "403", "404", "409", "422", "429", "500"}


# --- wyliczenie tras (jak w test_dostep_http.py, z trasami routera `konta`) ---------------------

def _trasy_aplikacji(trasy) -> list[APIRoute]:
    wynik: list[APIRoute] = []
    for route in trasy:
        if isinstance(route, APIRoute):
            wynik.append(route)
        elif hasattr(route, "original_router"):
            wynik.extend(_trasy_aplikacji(route.original_router.routes))
        elif isinstance(route, (Route, Mount)):
            continue  # docs/openapi i front statyczny, poza /api/
        else:
            pytest.fail(f"Wyliczenie tras nie rozpoznaje elementu {type(route).__name__} "
                        f"({getattr(route, 'path', '?')}); nie wolno go pominąć po cichu")
    return wynik


def _trasy_api() -> dict[tuple[str, str], APIRoute]:
    wynik = {}
    for route in _trasy_aplikacji(app.routes):
        if route.path.startswith("/api/"):
            for metoda in route.methods:
                wynik[(metoda, route.path)] = route
    return wynik


TRASY = _trasy_api()
TRASY_ID = [f"{m} {p}" for m, p in TRASY]


def _jest_strumieniem_sse(route: APIRoute) -> bool:
    if isinstance(route.response_class, type) and issubclass(route.response_class, EventSourceResponse):
        return True
    zwracany = typing.get_type_hints(route.endpoint).get("return")
    return isinstance(zwracany, type) and issubclass(zwracany, EventSourceResponse)


# --- schemat prawdziwej aplikacji -------------------------------------------------------------

@pytest.fixture(scope="module")
def schemat() -> dict:
    """Świeży schemat aplikacji (bez cache), niezależny od `tools/eksport_openapi.py`."""
    poprzedni = app.openapi_schema
    app.openapi_schema = None
    try:
        return copy.deepcopy(app.openapi())
    finally:
        app.openapi_schema = poprzedni


def _operacja(schemat: dict, klucz: tuple[str, str]) -> dict:
    metoda, sciezka = klucz
    return schemat["paths"][sciezka][metoda.lower()]


def _rozwin(schemat: dict, wezel: dict) -> dict:
    """Idzie za `$ref` (także w pojedynczym `allOf`) do definicji w components."""
    if "allOf" in wezel and len(wezel["allOf"]) == 1:
        wezel = wezel["allOf"][0]
    if "$ref" in wezel:
        nazwa = wezel["$ref"].rsplit("/", 1)[-1]
        return schemat["components"]["schemas"][nazwa]
    return wezel


def _nazwa_ref(wezel: dict) -> str | None:
    if "allOf" in wezel and len(wezel["allOf"]) == 1:
        wezel = wezel["allOf"][0]
    return wezel["$ref"].rsplit("/", 1)[-1] if "$ref" in wezel else None


def _typy(schemat: dict, pole: dict) -> set[str]:
    """Zbiór typów pola, np. {"string", "null"}; wariant `$ref` do enuma liczy się jako jego typ."""
    warianty = pole["anyOf"] if "anyOf" in pole else [pole]
    typy: set[str] = set()
    for w in warianty:
        typy.add(_rozwin(schemat, w).get("type", "?"))
    return typy


def _tresc_json(odpowiedz: dict) -> dict:
    return odpowiedz["content"]["application/json"]["schema"]


def _model_odpowiedzi(schemat: dict, klucz: tuple[str, str]) -> tuple[str, bool]:
    """Nazwa modelu 2xx trasy i informacja, czy odpowiedź jest tablicą takich modeli."""
    op = _operacja(schemat, klucz)
    kody = [k for k in op["responses"] if k.startswith("2")]
    assert len(kody) == 1, f"{klucz}: oczekiwano jednej odpowiedzi 2xx, jest {kody}"
    tresc = _tresc_json(op["responses"][kody[0]])
    if tresc.get("type") == "array":
        return _nazwa_ref(tresc["items"]), True
    return _nazwa_ref(tresc), False


# =================================================================================================
# 1. Każda trasa /api/* ma model odpowiedzi 2xx z polami
# =================================================================================================

def test_wyliczenie_tras_obejmuje_trasy_aplikacji_i_routera_kont():
    znane = (BEZ_TRESCI_204 | STRUMIENIE_SSE | TRASY_PUBLICZNE | {
        ("POST", "/api/runners/register"), ("POST", "/api/runners/heartbeat"),
        ("POST", "/api/runs"), ("POST", "/api/events"), ("GET", "/api/runs/{run_id}"),
        ("GET", "/api/pliki"), ("GET", "/api/pliki/lista"), ("PUT", "/api/pliki"),
        ("GET", "/api/runners"), ("GET", "/api/katalog"), ("POST", "/api/katalog/odswiez"),
        ("GET", "/api/konto/ja"),
    })
    assert znane <= set(TRASY), f"brakuje tras: {sorted(znane - set(TRASY))}"


def test_lista_wylaczen_204_zgadza_sie_z_rzeczywistoscia():
    for klucz in BEZ_TRESCI_204:
        assert klucz in TRASY, f"nieaktualny wpis na liście wyłączeń: {klucz}"
        assert TRASY[klucz].status_code == 204, f"{klucz} nie jest trasą 204"
    # odwrotnie: żadna trasa 204 spoza listy nie schowa się przed kryterium „2xx z polami”
    ukryte = sorted(k for k, r in TRASY.items() if r.status_code == 204 and k not in BEZ_TRESCI_204)
    assert ukryte == []


def test_lista_wylaczen_strumieni_sse_zgadza_sie_z_rzeczywistoscia():
    for klucz in STRUMIENIE_SSE:
        assert klucz in TRASY, f"nieaktualny wpis na liście wyłączeń: {klucz}"
        assert _jest_strumieniem_sse(TRASY[klucz]), f"{klucz} nie jest strumieniem SSE"
    ukryte = sorted(k for k, r in TRASY.items()
                    if _jest_strumieniem_sse(r) and k not in STRUMIENIE_SSE)
    assert ukryte == []


@pytest.mark.parametrize("klucz", [k for k in TRASY if k not in BEZ_TRESCI_204 | STRUMIENIE_SSE],
                         ids=lambda k: f"{k[0]} {k[1]}")
def test_odpowiedz_sukcesu_trasy_to_model_z_polami_a_nie_pusty_slownik(schemat, klucz):
    op = _operacja(schemat, klucz)
    kody = [k for k in op["responses"] if k.startswith("2")]
    # trasa bez jawnego `status_code` ma w FastAPI domyślne 200
    assert kody == [str(TRASY[klucz].status_code or 200)]
    tresc = _tresc_json(op["responses"][kody[0]])
    element = tresc["items"] if tresc.get("type") == "array" else tresc
    nazwa = _nazwa_ref(element)
    assert nazwa is not None, f"{klucz}: odpowiedź bez modelu: {tresc}"
    assert _rozwin(schemat, element).get("properties"), f"{klucz}: model {nazwa} bez pól"


@pytest.mark.parametrize("klucz", [k for k in TRASY if k not in BEZ_TRESCI_204 | STRUMIENIE_SSE],
                         ids=lambda k: f"{k[0]} {k[1]}")
def test_trasa_api_ma_response_model_z_klasy_pydantic(klucz):
    model = TRASY[klucz].response_model
    if typing.get_origin(model) is list:
        (model,) = typing.get_args(model)
    assert isinstance(model, type) and issubclass(model, BaseModel), f"{klucz}: {model!r}"


def test_modele_odpowiedzi_tras_maja_uzgodnione_nazwy(schemat):
    oczekiwane = {
        ("GET", "/api/health"): ("Zdrowie", False),
        ("POST", "/api/runners/heartbeat"): ("Zdrowie", False),
        ("POST", "/api/runners/register"): ("RunnerZameldowany", False),
        ("POST", "/api/runs"): ("RunUtworzony", False),
        ("POST", "/api/events"): ("ZdarzeniePrzyjete", False),
        ("GET", "/api/runs/{run_id}"): ("RunSzczegoly", False),
        ("GET", "/api/pliki"): ("PlikOdczytany", False),
        ("GET", "/api/pliki/lista"): ("ListaPlikow", False),
        ("PUT", "/api/pliki"): ("PlikZapisany", False),
        ("GET", "/api/runners"): ("RunnerZespolu", True),
        ("GET", "/api/katalog"): ("Katalog", False),
        ("POST", "/api/katalog/odswiez"): ("KatalogOdswiezony", False),
        ("POST", "/api/konto/rejestracja"): ("TokenCsrf", False),
        ("POST", "/api/konto/logowanie"): ("TokenCsrf", False),
        ("GET", "/api/konto/ja"): ("Profil", False),
    }
    rzeczywiste = {k: _model_odpowiedzi(schemat, k) for k in oczekiwane}
    assert rzeczywiste == oczekiwane


# =================================================================================================
# 2. Błędy opisane modelem Blad
# =================================================================================================

def _kody_bledow(schemat: dict, klucz: tuple[str, str]) -> set[str]:
    return {k for k in _operacja(schemat, klucz)["responses"] if int(k) >= 400}


@pytest.mark.parametrize("klucz", list(TRASY), ids=lambda k: f"{k[0]} {k[1]}")
def test_kazdy_kod_bledu_trasy_jest_opisany_modelem_blad(schemat, klucz):
    odpowiedzi = _operacja(schemat, klucz)["responses"]
    bledy = {k: o for k, o in odpowiedzi.items() if int(k) >= 400}
    assert bledy, f"{klucz}: brak opisanych błędów"
    for kod, odpowiedz in bledy.items():
        assert _tresc_json(odpowiedz) == SCHEMAT_BLEDU, f"{klucz} {kod}"


def test_schemat_nie_zawiera_domyslnego_bledu_walidacji_fastapi(schemat):
    nazwy = set(schemat["components"]["schemas"])
    assert not nazwy & {"HTTPValidationError", "ValidationError"}
    assert "HTTPValidationError" not in json.dumps(schemat)


@pytest.mark.parametrize("klucz", list(TRASY), ids=lambda k: f"{k[0]} {k[1]}")
def test_kazda_trasa_opisuje_blad_wewnetrzny_500(schemat, klucz):
    assert "500" in _kody_bledow(schemat, klucz)


@pytest.mark.parametrize("klucz", sorted(set(TRASY) - TRASY_PUBLICZNE),
                         ids=lambda k: f"{k[0]} {k[1]}")
def test_trasa_chroniona_opisuje_brak_uwierzytelnienia_401(schemat, klucz):
    assert "401" in _kody_bledow(schemat, klucz)


def test_zdrowie_jako_trasa_publiczna_nie_obiecuje_bledow_dostepu(schemat):
    kody = _kody_bledow(schemat, ("GET", "/api/health"))
    assert not kody & {"401", "403", "404", "409", "429"}


def test_zbior_kodow_bledow_w_calym_schemacie_jest_zamkniety(schemat):
    uzyte = {k for klucz in TRASY for k in _kody_bledow(schemat, klucz)}
    assert uzyte == KODY_BLEDOW


@pytest.mark.parametrize(("klucz", "oczekiwane"), [
    (("POST", "/api/runs"), {"401", "403", "409", "422", "500"}),
    (("PUT", "/api/pliki"), {"401", "403", "409", "422", "500"}),
    (("GET", "/api/pliki"), {"401", "409", "422", "500"}),
    (("GET", "/api/pliki/lista"), {"401", "409", "422", "500"}),
    (("POST", "/api/katalog/odswiez"), {"401", "409", "500"}),
    (("GET", "/api/runs/{run_id}"), {"401", "404", "500"}),
    (("POST", "/api/events"), {"401", "404", "422", "500"}),
    (("POST", "/api/runners/odpowiedzi"), {"401", "404", "422"}),
    (("POST", "/api/konto/logowanie"), {"401", "422", "429", "500"}),
    (("POST", "/api/konto/rejestracja"), {"409", "422", "500"}),
], ids=lambda v: f"{v[0]} {v[1]}" if isinstance(v, tuple) else "kody")
def test_trasa_opisuje_kody_bledow_ktore_faktycznie_moze_zwrocic(schemat, klucz, oczekiwane):
    assert oczekiwane <= _kody_bledow(schemat, klucz)


def test_rejestracja_nie_obiecuje_limitu_prob_bo_ten_ma_tylko_logowanie(schemat):
    # `zarejestruj` rzuca tylko StanNiemozliwy (409); 429 pochodzi z limitu prób logowania
    assert "429" not in _kody_bledow(schemat, ("POST", "/api/konto/rejestracja"))


# =================================================================================================
# 3. Model Blad
# =================================================================================================

def test_blad_wymaga_pola_detail_a_aktywny_run_id_jest_opcjonalny(schemat):
    blad = schemat["components"]["schemas"]["Blad"]
    assert set(blad["required"]) == {"detail"}
    assert set(blad["properties"]) == {"detail", "aktywny_run_id"}


def test_detail_bledu_dopuszcza_napis_i_liste_bledow_walidacji(schemat):
    detail = schemat["components"]["schemas"]["Blad"]["properties"]["detail"]
    assert {w["type"] for w in detail["anyOf"]} == {"string", "array"}


def test_aktywny_run_id_bledu_to_napis_albo_brak(schemat):
    pole = schemat["components"]["schemas"]["Blad"]["properties"]["aktywny_run_id"]
    assert _typy(schemat, pole) == {"string", "null"}


# =================================================================================================
# 4. Pola modeli 1:1 z kodem
# =================================================================================================

S, INT, B, N = "string", "integer", "boolean", "null"

# model -> {pole: zbiór typów}; każde pole jest wymagane (klucz zawsze obecny w odpowiedzi,
# także gdy wartość to null: brak pomiaru zapisujemy jako brak, nie jako pominięty klucz).
POLA_MODELI = {
    "Zdrowie": {"status": {S}},
    "RunnerZameldowany": {"runner_id": {S}, "tenant_id": {S}, "heartbeat_co_s": {INT}},
    "RunUtworzony": {"run_id": {S}},
    "ZdarzeniePrzyjete": {"seq": {INT}},
    "ZdarzenieRunu": {"seq": {INT}, "kind": {S}, "text": {S}, "step_id": {S}, "agent_type": {S},
                      "text_truncated": {B}, "text_full_len": {INT, N}},
    "RunSzczegoly": {"id": {S}, "workflow": {S}, "status": {S}, "runner_kind": {S},
                     "reason": {S, N}, "events": {"array"}},
    "PlikOdczytany": {"sciezka": {S}, "tresc": {S}, "rozmiar": {INT}},
    "WpisPliku": {"sciezka": {S}, "katalog": {B}},
    "ListaPlikow": {"pliki": {"array"}, "obcieto": {B}},
    "PlikZapisany": {"sciezka": {S}, "rozmiar": {INT}},
    "BackendRunnera": {"nazwa": {S}, "auth": {S}},
    "RunnerZespolu": {"id": {S}, "name": {S}, "version": {S}, "backends": {"array"},
                      "online": {B}, "last_seen_at": {S, N}},
    "DefinicjaKatalogu": {"runner_id": {S}, "projekt": {S}, "rodzaj": {S}, "klucz": {S},
                          "nazwa": {S}, "rola": {S, N}, "sciezka": {S}, "odswiezono_at": {S}},
    "Katalog": {"odswiezono_at": {S, N}, "definicje": {"array"}},
    "KatalogOdswiezony": {"odswiezono_at": {S, N}, "definicje": {"array"}, "pominiete": {INT}},
    "TokenCsrf": {"csrf": {S}},
    "Profil": {"email": {S}, "zespol": {S}, "rola": {S}, "csrf": {S}},
}


@pytest.mark.parametrize("model", list(POLA_MODELI))
def test_model_odpowiedzi_ma_dokladnie_pola_z_kodu_i_wszystkie_sa_wymagane(schemat, model):
    definicja = schemat["components"]["schemas"][model]
    oczekiwane = POLA_MODELI[model]
    assert set(definicja["properties"]) == set(oczekiwane)
    assert set(definicja.get("required", [])) == set(oczekiwane)
    for pole, typy in oczekiwane.items():
        assert _typy(schemat, definicja["properties"][pole]) == typy, f"{model}.{pole}"


def test_status_runu_w_schemacie_to_zamknieta_lista_stanow_runu(schemat):
    pole = schemat["components"]["schemas"]["RunSzczegoly"]["properties"]["status"]
    assert set(_rozwin(schemat, pole)["enum"]) == {s.value for s in RunStatus}


def test_rodzaj_runnera_w_schemacie_to_zamknieta_lista_rodzajow(schemat):
    pole = schemat["components"]["schemas"]["RunSzczegoly"]["properties"]["runner_kind"]
    assert set(_rozwin(schemat, pole)["enum"]) == {k.value for k in RunnerKind}


@pytest.mark.parametrize(("model", "pole", "element"), [
    ("RunSzczegoly", "events", "ZdarzenieRunu"),
    ("ListaPlikow", "pliki", "WpisPliku"),
    ("RunnerZespolu", "backends", "BackendRunnera"),
    ("Katalog", "definicje", "DefinicjaKatalogu"),
    ("KatalogOdswiezony", "definicje", "DefinicjaKatalogu"),
])
def test_pole_listowe_modelu_wskazuje_model_elementu(schemat, model, pole, element):
    wlasciwosc = schemat["components"]["schemas"][model]["properties"][pole]
    assert wlasciwosc["type"] == "array"
    assert _nazwa_ref(wlasciwosc["items"]) == element


def test_model_katalogu_po_odswiezeniu_ma_pole_pominiete_a_sam_katalog_nie(schemat):
    schematy = schemat["components"]["schemas"]
    assert "pominiete" in schematy["KatalogOdswiezony"]["properties"]
    assert "pominiete" not in schematy["Katalog"]["properties"]


# =================================================================================================
# 5. Parytet kształtu JSON przez prawdziwą aplikację
# =================================================================================================

@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    """Zespół „Browarek”: zalogowany właściciel i runner (token `tajne`) z otwartym strumieniem."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kontrakt.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        s.add(Runner(tenant_id=tenant_id, name="macbook",
                     token_hash=hashlib.sha256(b"tajne").hexdigest(),
                     last_seen_at=datetime.now(timezone.utc)))
        await s.commit()
    async with maker() as s:
        runner_id = (await s.execute(select(Runner.id))).scalars().one()
    bus.mark_runner_connected(runner_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield SimpleNamespace(klient=c, maker=maker, tenant_id=tenant_id,
                                  runner_id=runner_id, email=wlasciciel.email)
    finally:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        getattr(bus, "_rozlaczone", {}).pop(runner_id, None)
        await engine.dispose()


async def _zdarzenie(klient, run_id: str, **pola):
    return await klient.post(
        "/api/events",
        json={"run_id": run_id, "kind": "output", "step_id": "s0",
              "event_id": str(uuid.uuid4()), **pola},
        headers=NAGLOWEK_RUNNERA)


async def test_zdrowie_i_heartbeat_zwracaja_dokladnie_status_ok(swiat):
    zdrowie = await swiat.klient.get("/api/health")
    heartbeat = await swiat.klient.post("/api/runners/heartbeat", headers=NAGLOWEK_RUNNERA)
    assert zdrowie.json() == {"status": "ok"}
    assert heartbeat.json() == {"status": "ok"}


async def test_zameldowanie_runnera_zwraca_trzy_pola_w_tym_interwal_heartbeatu(swiat):
    odp = await swiat.klient.post("/api/runners/register", json={"name": "macbook"},
                                  headers=NAGLOWEK_RUNNERA)
    assert odp.status_code == 200
    assert odp.json() == {"runner_id": swiat.runner_id, "tenant_id": swiat.tenant_id,
                          "heartbeat_co_s": settings.heartbeat_co_s}


async def test_uruchomienie_runu_zwraca_wylacznie_run_id(swiat):
    odp = await swiat.klient.post("/api/runs", json={})
    assert odp.status_code == 200
    assert set(odp.json()) == {"run_id"}


async def test_szczegoly_runu_niosa_klucze_reason_i_text_full_len_takze_gdy_sa_puste(swiat):
    run_id = (await swiat.klient.post("/api/runs", json={})).json()["run_id"]
    przyjete = await _zdarzenie(swiat.klient, run_id, text="abc", agent_type="szkielet")
    assert set(przyjete.json()) == {"seq"}
    drugie = await _zdarzenie(swiat.klient, run_id, text="bez-pomiaru")
    # brak pomiaru pełnej długości to null w bazie (measured_or_null); API oddaje go jako null
    async with swiat.maker() as s:
        zdarzenie = (await s.execute(
            select(Event).where(Event.run_id == run_id, Event.seq == drugie.json()["seq"])
        )).scalar_one()
        zdarzenie.text_full_len = None
        await s.commit()

    odp = await swiat.klient.get(f"/api/runs/{run_id}")

    assert odp.status_code == 200
    tresc = odp.json()
    assert set(tresc) == {"id", "workflow", "status", "runner_kind", "reason", "events"}
    assert tresc["id"] == run_id
    assert tresc["status"] == RunStatus.running.value
    assert tresc["runner_kind"] in {k.value for k in RunnerKind}
    assert "reason" in tresc and tresc["reason"] is None
    pola_zdarzenia = {"seq", "kind", "text", "step_id", "agent_type", "text_truncated",
                      "text_full_len"}
    zdarzenia = sorted(tresc["events"], key=lambda e: e["seq"])
    assert [set(e) for e in zdarzenia] == [pola_zdarzenia, pola_zdarzenia]
    assert (zdarzenia[0]["text"], zdarzenia[0]["agent_type"]) == ("abc", "szkielet")
    assert zdarzenia[0]["text_full_len"] == 3
    assert zdarzenia[0]["text_truncated"] is False
    assert "text_full_len" in zdarzenia[1] and zdarzenia[1]["text_full_len"] is None


async def test_konflikt_rownoleglego_runu_niesie_id_aktywnego_runu(swiat):
    pierwszy = (await swiat.klient.post("/api/runs", json={"ticket_id": "HAR-1"})).json()["run_id"]
    odp = await swiat.klient.post("/api/runs", json={"ticket_id": "HAR-1"})
    assert odp.status_code == 409
    assert set(odp.json()) == {"detail", "aktywny_run_id"}
    assert odp.json()["aktywny_run_id"] == pierwszy


async def test_inny_blad_nie_dostaje_pustego_pola_aktywny_run_id(swiat):
    odp = await swiat.klient.get("/api/runs/nieistniejacy")
    assert odp.status_code == 404
    assert set(odp.json()) == {"detail"}


async def test_profil_i_logowanie_zwracaja_ksztalt_z_tokenem_csrf(swiat):
    profil = await swiat.klient.get("/api/konto/ja")
    assert profil.status_code == 200
    assert set(profil.json()) == {"email", "zespol", "rola", "csrf"}
    assert profil.json()["email"] == swiat.email
    assert profil.json()["zespol"] == "Browarek"
    assert profil.json()["rola"] == "owner"

    logowanie = await swiat.klient.post(
        "/api/konto/logowanie", json={"email": swiat.email, "haslo": HASLO_TESTOWE})
    assert logowanie.status_code == 200
    assert set(logowanie.json()) == {"csrf"}


async def test_lista_runnerow_i_pusty_katalog_zachowuja_klucze_z_wartoscia_null(swiat):
    runnery = (await swiat.klient.get("/api/runners")).json()
    assert [set(r) for r in runnery] == [
        {"id", "name", "version", "backends", "online", "last_seen_at"}]
    assert runnery[0]["online"] is True
    assert runnery[0]["last_seen_at"] is not None

    katalog = await swiat.klient.get("/api/katalog")
    assert katalog.json() == {"odswiezono_at": None, "definicje": []}


# =================================================================================================
# 6. tools/eksport_openapi.py
# =================================================================================================

SKRYPT = KORZEN / "tools" / "eksport_openapi.py"
POLECENIE_NAPRAWY = "python tools/eksport_openapi.py"


@pytest.fixture(scope="module")
def eksport():
    sys.path.insert(0, str(KORZEN / "tools"))
    try:
        import eksport_openapi  # noqa: E402
    finally:
        sys.path.remove(str(KORZEN / "tools"))
    return eksport_openapi


class _Maly(BaseModel):
    # kolejność pól celowo nie alfabetyczna: tylko `sort_keys` ustawia je w innym porządku
    zeta: str
    alfa: str = Field(description="Zażółć gęślą jaźń")


def _mala_aplikacja() -> FastAPI:
    mala = FastAPI(title="Mała aplikacja kontraktu")

    @mala.get("/api/maly", response_model=_Maly)
    async def maly():
        return {"zeta": "z", "alfa": "a"}

    return mala


@pytest.fixture
def aplikacja_kokpitu(monkeypatch):
    """Prawdziwa aplikacja; cache schematu przywracany po teście (monkeypatch)."""
    monkeypatch.setattr(app, "openapi_schema", app.openapi_schema)
    return app


def test_generowanie_schematu_dwa_razy_daje_identyczny_napis_json_z_nowa_linia(eksport):
    pierwszy = eksport.generuj(_mala_aplikacja())
    drugi = eksport.generuj(_mala_aplikacja())
    assert pierwszy == drugi
    assert pierwszy.endswith("}\n")
    assert not pierwszy.endswith("\n\n")
    assert json.loads(pierwszy)["info"]["title"] == "Mała aplikacja kontraktu"


def test_schemat_prawdziwej_aplikacji_generuje_sie_bez_bazy_i_serwera_i_jest_deterministyczny(
        eksport, aplikacja_kokpitu):
    pierwszy = eksport.generuj()
    drugi = eksport.generuj(aplikacja_kokpitu)
    assert pierwszy == drugi
    sciezki = json.loads(pierwszy)["paths"]
    assert "/api/health" in sciezki
    assert "/api/konto/ja" in sciezki


def test_klucze_schematu_sa_posortowane_a_wciecie_to_dwie_spacje(eksport):
    napis = eksport.generuj(_mala_aplikacja())
    surowy = _mala_aplikacja().openapi()
    # wejście rozróżnia sort_keys: surowy schemat FastAPI ma pola modelu w kolejności z kodu
    assert list(surowy["components"]["schemas"]["_Maly"]["properties"]) == ["zeta", "alfa"]
    odczytany = json.loads(napis)
    assert list(odczytany["components"]["schemas"]["_Maly"]["properties"]) == ["alfa", "zeta"]
    assert list(odczytany) == sorted(odczytany)
    druga_linia = napis.splitlines()[1]
    assert druga_linia.startswith('  "') and not druga_linia.startswith('   ')


def test_polskie_znaki_w_schemacie_sa_zapisane_doslownie_a_nie_jako_sekwencje_unicode(eksport):
    napis = eksport.generuj(_mala_aplikacja())
    assert "Zażółć gęślą jaźń" in napis
    assert "\\u" not in napis


def test_generowanie_ignoruje_przestarzaly_schemat_zapamietany_w_aplikacji(eksport):
    mala = _mala_aplikacja()
    mala.openapi_schema = {"openapi": "3.1.0", "stary": True, "paths": {}}
    napis = eksport.generuj(mala)
    odczytany = json.loads(napis)
    assert "stary" not in odczytany
    assert "/api/maly" in odczytany["paths"]
    assert napis == eksport.generuj(_mala_aplikacja())


def test_zapis_bez_flag_tworzy_plik_rowny_wygenerowanemu_i_jest_powtarzalny(eksport, tmp_path):
    mala = _mala_aplikacja()
    plik = tmp_path / "openapi.json"
    assert eksport.main([], plik=plik, app=mala) == 0
    assert plik.read_bytes() == eksport.generuj(mala).encode("utf-8")
    assert "ż".encode("utf-8") in plik.read_bytes()
    pierwsze_bajty = plik.read_bytes()
    assert eksport.main([], plik=plik, app=mala) == 0
    assert plik.read_bytes() == pierwsze_bajty


def test_zapis_nadpisuje_stary_plik_schematu(eksport, tmp_path):
    plik = tmp_path / "openapi.json"
    plik.write_text("stara zawartość", encoding="utf-8")
    assert eksport.main([], plik=plik, app=_mala_aplikacja()) == 0
    assert plik.read_text(encoding="utf-8") == eksport.generuj(_mala_aplikacja())


def test_sprawdzenie_zgodnego_pliku_konczy_sie_kodem_zero_i_nie_zmienia_pliku(eksport, tmp_path):
    mala = _mala_aplikacja()
    plik = tmp_path / "openapi.json"
    eksport.main([], plik=plik, app=mala)
    przed = plik.read_bytes()
    assert eksport.main(["--sprawdz"], plik=plik, app=mala) == 0
    assert plik.read_bytes() == przed


def test_sprawdzenie_rozjechanego_pliku_daje_kod_jeden_z_komunikatem_i_nie_naprawia_pliku(
        eksport, tmp_path, capsys):
    plik = tmp_path / "openapi.json"
    plik.write_text('{"stary": true}\n', encoding="utf-8")
    kod = eksport.main(["--sprawdz"], plik=plik, app=_mala_aplikacja())
    komunikat = capsys.readouterr().err
    assert kod == 1
    assert "openapi.json" in komunikat
    assert POLECENIE_NAPRAWY in komunikat
    assert plik.read_text(encoding="utf-8") == '{"stary": true}\n'


def test_sprawdzenie_bez_pliku_schematu_to_blad_a_nie_zielone_przejscie(eksport, tmp_path, capsys):
    plik = tmp_path / "openapi.json"
    kod = eksport.main(["--sprawdz"], plik=plik, app=_mala_aplikacja())
    komunikat = capsys.readouterr().err
    assert kod == 1
    assert "openapi.json" in komunikat
    assert not plik.exists()


def test_sprawdzenie_pliku_w_innym_kodowaniu_niz_utf8_to_blad_z_komunikatem(
        eksport, tmp_path, capsys):
    plik = tmp_path / "openapi.json"
    plik.write_bytes(b'{"opis": "\xff\xfe\x80"}\n')
    kod = eksport.main(["--sprawdz"], plik=plik, app=_mala_aplikacja())
    komunikat = capsys.readouterr().err
    assert kod == 1
    assert "openapi.json" in komunikat


def test_sprawdzenie_gdy_pod_sciezka_jest_katalog_to_blad_z_komunikatem(
        eksport, tmp_path, capsys):
    plik = tmp_path / "openapi.json"
    plik.mkdir()
    kod = eksport.main(["--sprawdz"], plik=plik, app=_mala_aplikacja())
    assert kod == 1
    assert "openapi.json" in capsys.readouterr().err


def test_dodanie_pola_do_modelu_odpowiedzi_bez_regeneracji_pliku_jest_wykryte(
        eksport, aplikacja_kokpitu, tmp_path, monkeypatch, capsys):
    plik = tmp_path / "openapi.json"
    monkeypatch.setattr(aplikacja_kokpitu, "openapi_schema", None)
    assert eksport.main([], plik=plik, app=aplikacja_kokpitu) == 0
    # stan wyjściowy: plik zgodny z kodem
    assert eksport.main(["--sprawdz"], plik=plik, app=aplikacja_kokpitu) == 0

    stara = next(r for r in aplikacja_kokpitu.router.routes
                 if isinstance(r, APIRoute) and r.path == "/api/health")
    assert stara.response_model is not None
    rozszerzony = create_model(stara.response_model.__name__ + "Rozszerzone", __base__=stara.response_model,
                               nowe_pole=(int, 0))
    nowa = APIRoute(
        stara.path, stara.endpoint, response_model=rozszerzony, methods=stara.methods,
        name=stara.name, dependencies=stara.dependencies, responses=stara.responses,
        status_code=stara.status_code, tags=stara.tags,
    )
    trasy = [nowa if r is stara else r for r in aplikacja_kokpitu.router.routes]
    monkeypatch.setattr(aplikacja_kokpitu.router, "routes", trasy)

    kod = eksport.main(["--sprawdz"], plik=plik, app=aplikacja_kokpitu)
    komunikat = capsys.readouterr().err
    assert kod == 1
    assert POLECENIE_NAPRAWY in komunikat
    # po regeneracji rozjazd znika: wykrycie dotyczy pola, nie czegokolwiek innego
    assert eksport.main([], plik=plik, app=aplikacja_kokpitu) == 0
    assert "nowe_pole" in plik.read_text(encoding="utf-8")
    assert eksport.main(["--sprawdz"], plik=plik, app=aplikacja_kokpitu) == 0


def test_sciezka_pliku_schematu_wskazuje_web_openapi_json_niezaleznie_od_katalogu_roboczego(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("eksport_openapi_z_innego_cwd", SKRYPT)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    assert modul.PLIK_SCHEMATU == KORZEN / "web" / "openapi.json"
    assert modul.PLIK_SCHEMATU.is_absolute()


def test_plik_schematu_w_repozytorium_jest_zgodny_z_aplikacja(
        eksport, aplikacja_kokpitu, capsys, tmp_path, monkeypatch):
    assert eksport.PLIK_SCHEMATU == KORZEN / "web" / "openapi.json"
    monkeypatch.chdir(tmp_path)   # plik z repo, nie z katalogu roboczego
    kod = eksport.main(["--sprawdz"])
    assert kod == 0, capsys.readouterr().err


def test_krok_kontraktu_w_jobie_python_ci_wywoluje_sprawdzenie_schematu():
    ci = YAML(typ="safe").load((KORZEN / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    kroki = ci["jobs"]["python"]["steps"]
    kroki_kontraktu = [k for k in kroki if "eksport_openapi" in str(k.get("run", ""))]
    assert len(kroki_kontraktu) == 1
    krok = kroki_kontraktu[0]
    assert krok["run"].strip() == "python tools/eksport_openapi.py --sprawdz"
    # krok nie może być przepuszczany po cichu (falszywie-zielone)
    assert not krok.get("continue-on-error")


def test_kontrakt_sprawdzany_jest_w_jobie_python_a_nie_tylko_w_innym_jobie_ci():
    ci = YAML(typ="safe").load((KORZEN / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    inne = [nazwa for nazwa, job in ci["jobs"].items() if nazwa != "python"
            and any("eksport_openapi" in str(k.get("run", "")) for k in job.get("steps", []))]
    assert inne == []


def test_mutmut_kopiuje_web_bo_test_pliku_schematu_czyta_go_z_repozytorium():
    konfiguracja = tomllib.loads((KORZEN / "pyproject.toml").read_text("utf-8"))
    assert "web/" in konfiguracja["tool"]["mutmut"]["also_copy"]


# =================================================================================================
# 7. odpowiedzi_bledow wołane bezpośrednio w ciele testu
# =================================================================================================
# Pomocnik działa przy imporcie modułu (dekoratory tras), a mutmut aktywuje mutanta dopiero
# w czasie testu — testy czytające schemat aplikacji go nie widzą.

def test_opis_odpowiedzi_bledow_zawiera_podane_kody_i_zawsze_blad_wewnetrzny():
    assert set(odpowiedzi_bledow(401, 409)) == {401, 409, 500}


def test_kazdy_kod_bledu_w_opisie_odpowiedzi_ma_model_blad_i_niepusty_napis():
    for kod, wpis in odpowiedzi_bledow(401, 409).items():
        assert set(wpis) == {"model", "description"}, kod
        assert wpis["model"] is Blad, kod
        assert isinstance(wpis["description"], str) and wpis["description"].strip(), kod


def test_kody_bledu_maja_rozne_opisy_a_nie_jeden_wspolny_napis():
    odpowiedzi = odpowiedzi_bledow(401, 409)
    assert odpowiedzi[401]["description"].startswith("Brak uwierzytelnienia")
    assert odpowiedzi[409]["description"].startswith("Konflikt")
    assert odpowiedzi[500]["description"] == "Błąd wewnętrzny kokpitu"


def test_kod_bledu_bez_opisu_jest_odrzucany_z_numerem_kodu_w_komunikacie():
    with pytest.raises(ValueError, match="418"):
        odpowiedzi_bledow(418)


def test_kod_bez_opisu_obok_znanego_kodu_nie_jest_po_cichu_pominiety():
    with pytest.raises(ValueError, match=r"kod bez opisu błędu: \[418\]"):
        odpowiedzi_bledow(401, 418)


def test_wszystkie_znane_kody_bledow_sa_przyjmowane_bez_wyjatku():
    assert set(odpowiedzi_bledow(401, 403, 404, 409, 422, 429, 500)) == {
        401, 403, 404, 409, 422, 429, 500}

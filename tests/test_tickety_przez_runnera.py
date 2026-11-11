"""HAR-104: tickety pobierane i listowane wylacznie przez runnera; zapis wyniku wylaczony.

Warstwy testow:
 1. przelot HTTP: prawdziwa aplikacja + prawdziwy `runner.client.Runner` w procesie testu,
    dostawca ticketow to `httpx.MockTransport` podstawiony w `runner.tickety.operacje.TRANSPORT`;
 2. operacje runnera (`ticket_*`) wolane przez `runner.operacje.wykonaj` z prawdziwym zakresem;
 3. serwis kokpitu `cockpit.services.tickety` z wstrzykniegta wysylka i prawdziwa baza.
Nazwy testow w jezyku dziedziny (CLAUDE.md). Komunikaty sprawdzane pelna trescia.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from transport_asgi import TransportASGIStrumieniujacy

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.db import session as db_session
from cockpit.db.models import (
    Base,
    Membership,
    Project,
    RolaWZespole,
    Runner,
    Tenant,
    User,
)
from cockpit.services import zapytania_runnera
from cockpit.services.errors import (
    NieZnaleziono,
    PlikPozaZakresem,
    RunnerOffline,
    StanNiemozliwy,
)
from cockpit.settings import settings
from runner import operacje, secrets
from runner.client import Runner as RunnerKlient

HASLO = "Sekret-Hasl0-Alfa"
FIXTURY = Path(__file__).parent / "fixtures" / "tickety"
LINEAR_ZGLOSZENIE = json.loads((FIXTURY / "linear_issue.json").read_text(encoding="utf-8"))
GITHUB = json.loads((FIXTURY / "github_issue.json").read_text(encoding="utf-8"))
JIRA_DC = json.loads((FIXTURY / "jira_dc_issue.json").read_text(encoding="utf-8"))

ADRES_JIRY = "https://jira.firma.local"
REPO = "LukBro/harness-cockpit"
ID_GITHUB = "LukBro/harness-cockpit#65"
POLE_KRYTERIOW = "customfield_10050"

OFFLINE = "wybór ticketu niedostępny — runner offline; użyj wklejenia ręcznego"
BRAK_KONFIGURACJI = (
    "brak sekcji tickets w .cockpit/project.yaml — wybór ticketu wymaga konfiguracji dostawcy"
)
ZLE_ZRODLO = "tickets.source musi być jednym z: linear, jira, github"
BRAK_REPO = "tickets.repo jest wymagane dla dostawcy github"
ZAPIS_WYLACZONY = (
    "zapis wyniku do systemu ticketów wyłączony — włącz tickets.post_result: true "
    "w .cockpit/project.yaml"
)
ZAPIS_NIEDOSTEPNY = "zapis wyniku do systemu ticketów nie jest jeszcze dostępny"
NIECZYTELNA_LISTA = "runner zwrócił nieczytelną listę ticketów"
NIECZYTELNY_TICKET = "runner zwrócił nieczytelny ticket"

TOKEN_LINEAR = "lin_api_TOKEN_HAR104"
TOKEN_GITHUB = "ghp_TOKEN_HAR104"
TOKEN_JIRA = "jira_TOKEN_HAR104"

KLUCZE_TICKETU = [
    "source", "id", "url", "title", "description", "acceptance_criteria", "status",
    "priority", "labels", "comments", "fetched_at", "raw",
]

# zrodlo -> (konfiguracja sekcji tickets, identyfikator do pobrania)
KONFIGURACJE = {
    "linear": ({"source": "linear"}, "HAR-53"),
    "github": ({"source": "github", "repo": REPO}, ID_GITHUB),
    "jira": ({"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
              "jql": "project = AB"}, "AB-7"),
}

KOMUNIKAT_BRAKU_TOKENU = {
    "linear": ('Linear: brak tokenu. Ustaw go w ~/.config/cockpit/credentials.json '
               '(klucz "linear") albo w zmiennej COCKPIT_LINEAR_TOKEN.'),
    "github": ('GitHub: brak tokenu. Ustaw go w ~/.config/cockpit/credentials.json '
               '(klucz "github") albo w zmiennej COCKPIT_GITHUB_TOKEN.'),
    "jira": ('Jira: brak sekretu "jira". Ustaw go w ~/.config/cockpit/credentials.json '
             "albo w zmiennej COCKPIT_<NAZWA>_TOKEN."),
}
KOMUNIKAT_ODRZUCENIA = {
    "linear": ('Linear: token odrzucony. Popraw go w ~/.config/cockpit/credentials.json '
               '(klucz "linear") albo w zmiennej COCKPIT_LINEAR_TOKEN.'),
    "github": ('GitHub: token odrzucony. Popraw go w ~/.config/cockpit/credentials.json '
               '(klucz "github") albo w zmiennej COCKPIT_GITHUB_TOKEN.'),
    "jira": ("Jira: dane logowania odrzucone. Popraw je w ~/.config/cockpit/credentials.json "
             "albo w zmiennej COCKPIT_<NAZWA>_TOKEN."),
}


def _serwis():
    """Serwis importowany leniwie: brak modulu to czerwony test, nie blad zbierania."""
    return importlib.import_module("cockpit.services.tickety")


# =================================================================================
# Swiat testu: projekt w zakresie runnera, sekrety, szyna, aplikacja, dostawca
# =================================================================================

@pytest.fixture
def projekt(tmp_path, monkeypatch):
    """Projekt w zakresie runnera i obcy katalog poza nim; HOME w katalogu testu."""
    baza = tmp_path.resolve()
    korzen = baza / "projekt"
    obcy = baza / "obcy"
    dom = baza / "dom"
    for k in (korzen, obcy, dom):
        k.mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(korzen)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    return SimpleNamespace(korzen=korzen, obcy=obcy, dom=dom, konfig=konfig, baza=baza)


@pytest.fixture(autouse=True)
def _bez_sekretow(tmp_path, monkeypatch):
    """Domyslnie brak jakiegokolwiek tokenu: plik sekretow nie istnieje, zmienne nieustawione."""
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak-sekretow.json")
    for nazwa in ("LINEAR", "GITHUB", "JIRA", "JIRA_EMAIL"):
        monkeypatch.delenv(f"COCKPIT_{nazwa}_TOKEN", raising=False)


TOKENY = {
    "linear": ("COCKPIT_LINEAR_TOKEN", TOKEN_LINEAR),
    "github": ("COCKPIT_GITHUB_TOKEN", TOKEN_GITHUB),
    "jira": ("COCKPIT_JIRA_TOKEN", TOKEN_JIRA),
}


def _ustaw_token(monkeypatch, zrodlo: str) -> None:
    zmienna, token = TOKENY[zrodlo]
    monkeypatch.setenv(zmienna, token)


@pytest.fixture(autouse=True)
def _czysta_szyna():
    """Kolejki, oczekujace zapytania i ewidencja polaczen to stan procesu."""
    slowniki = ("_oczekujace", "_runner_queues", "_polaczone", "_rozlaczone")
    zapisane = {n: dict(getattr(bus, n)) for n in slowniki if hasattr(bus, n)}
    yield
    for nazwa, przed in zapisane.items():
        biezacy = getattr(bus, nazwa)
        biezacy.clear()
        biezacy.update(przed)


class Dostawca:
    """Zaslepka dostawcy ticketow: zapamietuje kazde zadanie i odpowiada `obsluga`."""

    def __init__(self, obsluga=None) -> None:
        self.zadania: list[httpx.Request] = []
        self.obsluga = obsluga or (lambda _z: httpx.Response(500, json={}))

    def __call__(self, zadanie: httpx.Request) -> httpx.Response:
        self.zadania.append(zadanie)
        return self.obsluga(zadanie)


def _podlacz(monkeypatch, obsluga=None) -> Dostawca:
    """Adaptery runnera pytaja ten transport zamiast sieci."""
    dostawca = Dostawca(obsluga)
    monkeypatch.setattr("runner.tickety.operacje.TRANSPORT", httpx.MockTransport(dostawca))
    return dostawca


def _linear(zadanie: httpx.Request) -> httpx.Response:
    cialo = json.loads(zadanie.content)
    if "issue(id" in cialo["query"]:
        return httpx.Response(200, json=LINEAR_ZGLOSZENIE)
    return httpx.Response(200, json={"data": {"issues": {
        "nodes": [
            {"identifier": "HAR-1", "title": "Pierwszy", "state": {"name": "Todo"}},
            {"identifier": "HAR-2", "title": "Drugi", "state": {"name": "W toku"}},
        ],
        "pageInfo": {"hasNextPage": False, "endCursor": None}}}})


def _github(zadanie: httpx.Request) -> httpx.Response:
    sciezka = zadanie.url.path
    if sciezka.endswith("/comments"):
        return httpx.Response(200, json=GITHUB["comments"])
    if sciezka.endswith("/issues/65"):
        return httpx.Response(200, json=GITHUB["issue"])
    return httpx.Response(200, json=[
        {"number": 12, "title": "Dwunaste", "state": "open",
         "html_url": f"https://github.com/{REPO}/issues/12"},
        {"number": 13, "title": "Trzynaste", "state": "open",
         "html_url": f"https://github.com/{REPO}/issues/13"},
    ])


def _jira(zadanie: httpx.Request) -> httpx.Response:
    if zadanie.url.path.startswith("/rest/api/2/issue/"):
        return httpx.Response(200, json=JIRA_DC)
    return httpx.Response(200, json={"total": 2, "issues": [
        {"key": "AB-1", "fields": {"summary": "Pierwsze", "status": {"name": "To Do"}}},
        {"key": "AB-2", "fields": {"summary": "Drugie", "status": {"name": "W toku"}}},
    ]})


OBSLUGA = {"linear": _linear, "github": _github, "jira": _jira}


def _pisz_konfig(projekt, tickets: dict | None, **reszta) -> None:
    """`.cockpit/project.yaml` jako JSON (poprawny YAML): sekcja `tickets` plus dodatki."""
    dane = dict(reszta)
    if tickets is not None:
        dane["tickets"] = tickets
    sciezka = projekt.korzen / ".cockpit" / "project.yaml"
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(json.dumps(dane), encoding="utf-8")


def _pisz_tekst(projekt, tekst: str) -> None:
    sciezka = projekt.korzen / ".cockpit" / "project.yaml"
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(tekst, encoding="utf-8")


class _Podsluch:
    """Owija `bus.zapytaj`: zapamietuje, KTORY runner dostal jakie zapytanie."""

    def __init__(self, oryginal) -> None:
        self.oryginal = oryginal
        self.wywolania: list[tuple[str, str, dict]] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dict(dane)))
        return await self.oryginal(runner_id, typ, dane, timeout_s)


@pytest.fixture
def podsluch(monkeypatch):
    p = _Podsluch(bus.zapytaj)
    monkeypatch.setattr(bus, "zapytaj", p)
    return p


class _Kokpit:
    """Aplikacja na bazie w katalogu testu: konta, runnery w procesie i klienci HTTP."""

    def __init__(self, maker) -> None:
        self.maker = maker
        self.klienci: list[AsyncClient] = []
        self.runnery: list[tuple[RunnerKlient, asyncio.Task]] = []

    def klient(self) -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app_mod.app), base_url="https://test")
        self.klienci.append(k)
        return k

    async def konto(self, email: str, zespol: str) -> SimpleNamespace:
        k = self.klient()
        odp = await k.post("/api/konto/rejestracja",
                           json={"email": email, "haslo": HASLO, "nazwa_zespolu": zespol})
        assert odp.status_code == 201, odp.text
        async with self.maker() as s:
            tenant_id = (await s.execute(select(Tenant.id).where(Tenant.name == zespol))).scalar_one()
        return SimpleNamespace(http=k, csrf={"X-CSRF-Token": odp.json()["csrf"]}, tenant_id=tenant_id)

    async def viewer(self, email: str, tenant_id: str) -> SimpleNamespace:
        """Konto z rola viewer w ISTNIEJACYM zespole, potem swieze logowanie."""
        wstepne = await self.konto(email, f"zespol-{email}")
        async with self.maker() as s:
            user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
            await s.execute(update(Membership).where(Membership.user_id == user_id).values(
                tenant_id=tenant_id, rola=RolaWZespole.viewer))
            await s.commit()
        k = self.klient()
        odp = await k.post("/api/konto/logowanie", json={"email": email, "haslo": HASLO})
        assert odp.status_code == 200, odp.text
        assert wstepne.tenant_id != tenant_id
        return SimpleNamespace(http=k, csrf={"X-CSRF-Token": odp.json()["csrf"]}, tenant_id=tenant_id)

    async def dodaj_runnera(self, runner_id: str, tenant_id: str) -> None:
        async with self.maker() as s:
            s.add(Runner(id=runner_id, tenant_id=tenant_id, name=runner_id,
                         token_hash=hashlib.sha256(f"tok-{runner_id}".encode()).hexdigest(),
                         last_seen_at=datetime.now(timezone.utc)))
            await s.commit()

    async def uruchom_runnera(self, runner_id: str) -> None:
        runner = RunnerKlient("http://kokpit.test", f"tok-{runner_id}",
                              transport=TransportASGIStrumieniujacy(app_mod.app))
        nasluch = asyncio.create_task(runner.listen())
        self.runnery.append((runner, nasluch))
        koniec = time.monotonic() + 10
        while runner_id not in bus.connected_runner_ids():
            assert time.monotonic() < koniec, "runner nie polaczyl sie z kokpitem"
            await asyncio.sleep(0.01)

    async def dodaj_projekt(self, tenant_id: str, runner_id: str, sciezka: Path,
                            nazwa: str = "projekt") -> str:
        async with self.maker() as s:
            p = Project(id=str(uuid.uuid4()), tenant_id=tenant_id, runner_id=runner_id,
                        path=str(sciezka), name=nazwa, stack=[])
            s.add(p)
            await s.commit()
            return p.id

    async def zamknij(self) -> None:
        for runner, nasluch in reversed(self.runnery):
            await asyncio.wait_for(runner.stop(), 10)
            gotowe, _ = await asyncio.wait({nasluch}, timeout=10)
            if nasluch not in gotowe:
                nasluch.cancel()
                await asyncio.wait({nasluch}, timeout=5)
        for k in self.klienci:
            await k.aclose()


@pytest.fixture
async def kokpit(tmp_path, monkeypatch, projekt):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kokpit.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    monkeypatch.setattr(settings, "ciasteczko_secure", True)
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 20.0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    k = _Kokpit(maker)
    try:
        yield k
    finally:
        await k.zamknij()
        await engine.dispose()


@pytest.fixture
async def zespol(kokpit, projekt):
    """Zespol Alfa z wlascicielem, zywym runnerem `runner-alfa` i projektem na tym runnerze."""
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-alfa", konto.tenant_id)
    await kokpit.uruchom_runnera("runner-alfa")
    konto.projekt_id = await kokpit.dodaj_projekt(konto.tenant_id, "runner-alfa", projekt.korzen)
    return konto


@pytest.fixture
async def zespol_offline(kokpit, projekt):
    """Zespol Alfa z runnerem w bazie, ktory sie nie polaczyl, i projektem na nim."""
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-alfa", konto.tenant_id)
    konto.projekt_id = await kokpit.dodaj_projekt(konto.tenant_id, "runner-alfa", projekt.korzen)
    return konto


def _adres(trasa: str, id_ticketu: str = "HAR-53") -> str:
    if trasa == "lista":
        return "/api/tickety"
    return "/api/tickety/" + id_ticketu.replace("#", "%23")


async def _odczytaj(konto, trasa: str, *, id_ticketu: str = "HAR-53", filtr: str | None = None,
                    projekt_id: str | None = None):
    params = {"projekt": projekt_id if projekt_id is not None else konto.projekt_id}
    if filtr is not None:
        params["filtr"] = filtr
    return await konto.http.get(_adres(trasa, id_ticketu), params=params)


def _zmienne(zadanie: httpx.Request) -> dict:
    return json.loads(zadanie.content)["variables"]


# =================================================================================
# 1. Pobranie ticketu przez prawdziwa aplikacje i runnera
# =================================================================================

async def test_pobranie_ticketu_z_lineara_oddaje_ticket_i_pusta_liste_ostrzezen(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="HAR-53")

    assert odp.status_code == 200, odp.text
    tresc = odp.json()
    assert sorted(tresc) == ["ostrzezenia", "ticket"]
    assert tresc["ostrzezenia"] == []
    ticket = tresc["ticket"]
    assert list(ticket) == KLUCZE_TICKETU
    assert ticket["source"] == "linear"
    assert ticket["id"] == "HAR-53"
    assert ticket["title"] == "Adapter Linear: pobranie zgłoszenia"
    assert ticket["url"] == "https://linear.app/harness/issue/HAR-53/adapter-linear"
    assert ticket["status"] == "W recenzji"
    assert ticket["priority"] == "High"
    assert ticket["labels"] == ["backend", "integracje"]
    assert ticket["acceptance_criteria"] == []
    assert [k["author"] for k in ticket["comments"]] == ["Łukasz Żółć", "nieznany"]
    assert ticket["raw"] == LINEAR_ZGLOSZENIE["data"]["issue"]
    datetime.fromisoformat(ticket["fetched_at"].replace("Z", "+00:00"))


async def test_pobranie_ticketu_wysyla_do_lineara_jedno_zadanie_z_identyfikatorem_i_tokenem(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="HAR-53")

    assert odp.status_code == 200, odp.text
    assert len(dostawca.zadania) == 1
    assert _zmienne(dostawca.zadania[0]) == {"id": "HAR-53"}
    assert dostawca.zadania[0].headers["authorization"] == TOKEN_LINEAR


async def test_pobranie_ticketu_z_jiry_bez_pola_kryteriow_niesie_ostrzezenie_z_konfiguracji(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY})
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="AB-7")

    assert odp.status_code == 200, odp.text
    assert odp.json()["ostrzezenia"] == ["pole kryteriów nieustawione w project.yaml"]
    ticket = odp.json()["ticket"]
    assert ticket["source"] == "jira"
    assert ticket["id"] == "AB-7"
    assert ticket["url"] == f"{ADRES_JIRY}/browse/AB-7"
    assert ticket["title"] == "Adapter Jira: pobranie zgłoszenia"
    assert ticket["status"] == "W recenzji"
    assert ticket["acceptance_criteria"] == []
    assert dostawca.zadania[0].url.path == "/rest/api/2/issue/AB-7"
    assert dostawca.zadania[0].headers["authorization"] == f"Bearer {TOKEN_JIRA}"


async def test_pobranie_ticketu_z_jiry_z_polem_kryteriow_nie_ma_ostrzezen_i_ma_kryteria(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
                           "pole_kryteriow": POLE_KRYTERIOW})
    _ustaw_token(monkeypatch, "jira")
    _podlacz(monkeypatch, _jira)

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="AB-7")

    assert odp.status_code == 200, odp.text
    assert odp.json()["ostrzezenia"] == []
    assert odp.json()["ticket"]["acceptance_criteria"] == [
        "Lista zwraca klucz, tytuł i stan",
        "Błędny token daje czytelny komunikat",
        "- zaczyna się od myślnika",
    ]


async def test_flaga_zapisu_w_sekcji_jiry_nie_psuje_pobrania_ticketu(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
                           "post_result": True})
    _ustaw_token(monkeypatch, "jira")
    _podlacz(monkeypatch, _jira)

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="AB-7")

    assert odp.status_code == 200, odp.text
    assert odp.json()["ticket"]["id"] == "AB-7"


async def test_identyfikator_githuba_z_krzyzykiem_przechodzi_przez_trase_jako_procent_23(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github"})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await zespol.http.get("/api/tickety/LukBro/harness-cockpit%2365",
                                params={"projekt": zespol.projekt_id})

    assert odp.status_code == 200, odp.text
    ticket = odp.json()["ticket"]
    assert ticket["source"] == "github"
    assert ticket["id"] == ID_GITHUB
    assert ticket["title"] == "Adapter GitHub Issues: pobranie zgłoszenia"
    assert ticket["status"] == "closed"
    assert ticket["url"] == "https://github.com/LukBro/harness-cockpit/issues/65"
    assert ticket["labels"] == ["backend", "integracje"]
    assert odp.json()["ostrzezenia"] == []
    assert [z.url.path for z in dostawca.zadania] == [
        "/repos/LukBro/harness-cockpit/issues/65",
        "/repos/LukBro/harness-cockpit/issues/65/comments",
    ]
    assert dostawca.zadania[0].headers["authorization"] == f"Bearer {TOKEN_GITHUB}"


# =================================================================================
# 2. Lista ticketow i filtr
# =================================================================================

async def test_lista_z_lineara_oddaje_skroty_id_tytul_stan(kokpit, zespol, projekt, monkeypatch):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "lista")

    assert odp.status_code == 200, odp.text
    assert odp.json() == [
        {"id": "HAR-1", "tytul": "Pierwszy", "stan": "Todo"},
        {"id": "HAR-2", "tytul": "Drugi", "stan": "W toku"},
    ]


async def test_lista_z_lineara_bez_filtra_i_konfiguracji_pyta_o_stany_unstarted_i_started(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    await _odczytaj(zespol, "lista")

    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["unstarted", "started"]]


async def test_lista_z_lineara_bez_filtra_uzywa_typow_stanu_z_konfiguracji(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear", "typy_stanu": ["backlog", "triage"]})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    await _odczytaj(zespol, "lista")

    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["backlog", "triage"]]


async def test_filtr_listy_z_lineara_zastepuje_typy_stanu_z_konfiguracji_i_jest_przycinany(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear", "typy_stanu": ["backlog"]})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "lista", filtr=" started , completed ,, ")

    assert odp.status_code == 200, odp.text
    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["started", "completed"]]


async def test_pusty_filtr_listy_z_lineara_oznacza_brak_filtra(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear", "typy_stanu": ["backlog"]})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    await _odczytaj(zespol, "lista", filtr="")

    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["backlog"]]


async def test_lista_z_githuba_oddaje_skroty_z_identyfikatorem_repo_i_numeru(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _odczytaj(zespol, "lista")

    assert odp.status_code == 200, odp.text
    assert odp.json() == [
        {"id": f"{REPO}#12", "tytul": "Dwunaste", "stan": "open"},
        {"id": f"{REPO}#13", "tytul": "Trzynaste", "stan": "open"},
    ]
    assert dostawca.zadania[0].url.path == f"/repos/{REPO}/issues"


async def test_lista_z_githuba_bez_filtra_i_etykiet_pyta_o_otwarte_bez_etykiet(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    await _odczytaj(zespol, "lista")

    parametry = dostawca.zadania[0].url.params
    assert parametry["state"] == "open"
    assert "labels" not in parametry


async def test_lista_z_githuba_bez_filtra_uzywa_etykiet_i_stanu_z_konfiguracji(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO,
                           "etykiety": ["bug", "pilne"], "stan": "closed"})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    await _odczytaj(zespol, "lista")

    parametry = dostawca.zadania[0].url.params
    assert parametry["labels"] == "bug,pilne"
    assert parametry["state"] == "closed"


async def test_filtr_listy_z_githuba_zastepuje_etykiety_z_konfiguracji_i_jest_przycinany(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO, "etykiety": ["bug"]})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _odczytaj(zespol, "lista", filtr=" ui , backend ,,")

    assert odp.status_code == 200, odp.text
    assert dostawca.zadania[0].url.params["labels"] == "ui,backend"
    assert dostawca.zadania[0].url.params["state"] == "open"


async def test_lista_z_jiry_bez_filtra_uzywa_jql_z_konfiguracji(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
                           "jql": "project = AB ORDER BY created"})
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    odp = await _odczytaj(zespol, "lista")

    assert odp.status_code == 200, odp.text
    assert odp.json() == [
        {"id": "AB-1", "tytul": "Pierwsze", "stan": "To Do"},
        {"id": "AB-2", "tytul": "Drugie", "stan": "W toku"},
    ]
    assert dostawca.zadania[0].url.path == "/rest/api/2/search"
    assert dostawca.zadania[0].url.params["jql"] == "project = AB ORDER BY created"


async def test_filtr_listy_z_jiry_zastepuje_jql_z_konfiguracji(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
                           "jql": "project = AB"})
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    await _odczytaj(zespol, "lista", filtr="assignee = currentUser()")

    assert dostawca.zadania[0].url.params["jql"] == "assignee = currentUser()"


async def test_filtr_listy_z_jiry_jest_przycinany_ale_nie_dzielony_po_przecinku(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
                           "jql": "project = AB"})
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    odp = await _op("ticket_lista", projekt, filtr="  status in (A, B)  ")

    assert odp["ok"] is True, odp
    assert [z.url.params["jql"] for z in dostawca.zadania] == ["status in (A, B)"]


async def test_filtr_listy_z_jiry_z_samych_spacji_oznacza_brak_filtra_i_uzywa_jql_z_konfiguracji(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY,
                           "jql": "project = AB"})
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    odp = await _op("ticket_lista", projekt, filtr="   ")

    assert odp["ok"] is True, odp
    assert [z.url.params["jql"] for z in dostawca.zadania] == ["project = AB"]


async def test_lista_z_jiry_bez_jql_w_konfiguracji_i_bez_filtra_to_409_z_komunikatem_adaptera(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center", "adres": ADRES_JIRY})
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    odp = await _odczytaj(zespol, "lista")

    assert odp.status_code == 409
    assert odp.json()["detail"] == "Jira: brak jql w konfiguracji, nie ma czego listować."
    assert dostawca.zadania == []


# =================================================================================
# 3. Runner offline i runner projektu
# =================================================================================

@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_gdy_runner_projektu_jest_offline_odczyt_ticketow_daje_409_z_odsylaczem_do_wklejenia(
    kokpit, zespol_offline, projekt, monkeypatch, trasa,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol_offline, trasa)

    assert odp.status_code == 409
    assert odp.json() == {"detail": OFFLINE}
    assert dostawca.zadania == []


@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_zapytanie_o_tickety_idzie_do_runnera_projektu_a_nie_do_pierwszego_online(
    kokpit, projekt, monkeypatch, podsluch, trasa,
):
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-a", konto.tenant_id)
    await kokpit.dodaj_runnera("runner-b", konto.tenant_id)
    await kokpit.uruchom_runnera("runner-a")
    await kokpit.uruchom_runnera("runner-b")
    konto.projekt_id = await kokpit.dodaj_projekt(konto.tenant_id, "runner-b", projekt.korzen)
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(konto, trasa)

    assert odp.status_code == 200, odp.text
    oczekiwane = (
        ("ticket_lista", {"projekt": str(projekt.korzen), "filtr": None}) if trasa == "lista"
        else ("ticket_pobierz", {"projekt": str(projekt.korzen), "id": "HAR-53"})
    )
    assert podsluch.wywolania == [("runner-b", *oczekiwane)]


async def test_filtr_z_zapytania_trafia_do_runnera_doslownie(
    kokpit, zespol, projekt, monkeypatch, podsluch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    await _odczytaj(zespol, "lista", filtr=" a , b ")

    assert podsluch.wywolania == [
        ("runner-alfa", "ticket_lista", {"projekt": str(projekt.korzen), "filtr": " a , b "})]


@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_gdy_runner_projektu_jest_offline_inny_runner_zespolu_online_go_nie_zastepuje(
    kokpit, projekt, monkeypatch, podsluch, trasa,
):
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-a", konto.tenant_id)
    await kokpit.dodaj_runnera("runner-b", konto.tenant_id)
    await kokpit.uruchom_runnera("runner-a")   # online, ale projekt nalezy do runner-b
    konto.projekt_id = await kokpit.dodaj_projekt(konto.tenant_id, "runner-b", projekt.korzen)
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(konto, trasa)

    assert odp.status_code == 409
    assert odp.json() == {"detail": OFFLINE}
    assert podsluch.wywolania == []
    assert dostawca.zadania == []


# =================================================================================
# 4. Projekt innego zespolu, sesja i uprawnienia
# =================================================================================

@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_projekt_innego_zespolu_daje_404_i_runner_nie_dostaje_zapytania(
    kokpit, zespol, projekt, monkeypatch, podsluch, trasa,
):
    beta = await kokpit.konto("bea@przyklad.pl", "Beta")
    await kokpit.dodaj_runnera("runner-beta", beta.tenant_id)
    obcy_projekt = await kokpit.dodaj_projekt(beta.tenant_id, "runner-beta", projekt.korzen)
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, trasa, projekt_id=obcy_projekt)

    assert odp.status_code == 404
    assert odp.json() == {"detail": "projekt nieznany"}
    assert podsluch.wywolania == []
    assert dostawca.zadania == []


@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_nieistniejacy_projekt_daje_404(kokpit, zespol, trasa):
    odp = await _odczytaj(zespol, trasa, projekt_id=str(uuid.uuid4()))

    assert odp.status_code == 404
    assert odp.json() == {"detail": "projekt nieznany"}


@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_odczyt_ticketow_bez_sesji_daje_401(kokpit, zespol, trasa):
    odp = await kokpit.klient().get(_adres(trasa), params={"projekt": zespol.projekt_id})

    assert odp.status_code == 401


@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_odczyt_ticketow_bez_numeru_projektu_daje_422(kokpit, zespol, trasa):
    odp = await zespol.http.get(_adres(trasa))

    assert odp.status_code == 422


@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_numer_projektu_dluzszy_niz_36_znakow_daje_422_a_36_znakow_przechodzi_walidacje(
    kokpit, zespol, trasa,
):
    za_dlugi = await _odczytaj(zespol, trasa, projekt_id="x" * 37)
    graniczny = await _odczytaj(zespol, trasa, projekt_id="x" * 36)

    assert za_dlugi.status_code == 422
    assert graniczny.status_code == 404


async def test_filtr_dluzszy_niz_500_znakow_daje_422_a_500_znakow_przechodzi_do_runnera(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    za_dlugi = await _odczytaj(zespol, "lista", filtr="a" * 501)
    graniczny = await _odczytaj(zespol, "lista", filtr="a" * 500)

    assert za_dlugi.status_code == 422
    assert graniczny.status_code == 200, graniczny.text
    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["a" * 500]]


async def test_czlonek_z_rola_odczytu_moze_listowac_tickety(
    kokpit, zespol, projekt, monkeypatch,
):
    obserwator = await kokpit.viewer("obserwator@przyklad.pl", zespol.tenant_id)
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    odp = await obserwator.http.get("/api/tickety", params={"projekt": zespol.projekt_id})

    assert odp.status_code == 200, odp.text
    assert [t["id"] for t in odp.json()] == ["HAR-1", "HAR-2"]


async def test_czlonek_z_rola_odczytu_moze_pobrac_ticket(kokpit, zespol, projekt, monkeypatch):
    obserwator = await kokpit.viewer("obserwator@przyklad.pl", zespol.tenant_id)
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    odp = await obserwator.http.get("/api/tickety/HAR-53", params={"projekt": zespol.projekt_id})

    assert odp.status_code == 200, odp.text
    assert odp.json()["ticket"]["id"] == "HAR-53"


# =================================================================================
# 5. Kody bledow dostawcy na prawdziwej sciezce
# =================================================================================

@pytest.mark.parametrize("zrodlo", ["linear", "github", "jira"])
@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_brak_tokenu_daje_409_z_nazwa_dostawcy_i_miejscem_tokenu_bez_zadania_do_dostawcy(
    kokpit, zespol, projekt, monkeypatch, zrodlo, trasa,
):
    konfiguracja, id_ticketu = KONFIGURACJE[zrodlo]
    _pisz_konfig(projekt, konfiguracja)
    dostawca = _podlacz(monkeypatch, OBSLUGA[zrodlo])

    odp = await _odczytaj(zespol, trasa, id_ticketu=id_ticketu)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_BRAKU_TOKENU[zrodlo]}
    assert dostawca.zadania == []


@pytest.mark.parametrize("zrodlo", ["linear", "github", "jira"])
@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_token_odrzucony_przez_dostawce_daje_409_z_komunikatem_adaptera(
    kokpit, zespol, projekt, monkeypatch, zrodlo, trasa,
):
    konfiguracja, id_ticketu = KONFIGURACJE[zrodlo]
    _pisz_konfig(projekt, konfiguracja)
    _ustaw_token(monkeypatch, zrodlo)
    dostawca = _podlacz(monkeypatch, lambda _z: httpx.Response(401, json={}))

    odp = await _odczytaj(zespol, trasa, id_ticketu=id_ticketu)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_ODRZUCENIA[zrodlo]}
    assert len(dostawca.zadania) == 1


def _brak_ticketu(zrodlo: str):
    if zrodlo == "linear":
        return lambda _z: httpx.Response(200, json={"data": {"issue": None}})
    return lambda _z: httpx.Response(404, json={})


@pytest.mark.parametrize(("zrodlo", "komunikat"), [
    ("linear", "Linear: nie znaleziono zgłoszenia HAR-53."),
    ("github", f"GitHub: nie znaleziono {ID_GITHUB}."),
    ("jira", "Jira: nie znaleziono zgłoszenia AB-7."),
])
async def test_ticket_nieznany_dostawcy_daje_404_z_komunikatem_adaptera(
    kokpit, zespol, projekt, monkeypatch, zrodlo, komunikat,
):
    konfiguracja, id_ticketu = KONFIGURACJE[zrodlo]
    _pisz_konfig(projekt, konfiguracja)
    _ustaw_token(monkeypatch, zrodlo)
    _podlacz(monkeypatch, _brak_ticketu(zrodlo))

    odp = await _odczytaj(zespol, "pobierz", id_ticketu=id_ticketu)

    assert odp.status_code == 404
    assert odp.json() == {"detail": komunikat}


async def test_blad_dostawcy_inny_niz_brak_ticketu_to_409_a_nie_404(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, lambda _z: httpx.Response(500, json={}))

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="HAR-53")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "Linear: błąd HTTP 500."}


# =================================================================================
# 6. Konfiguracja projektu: przelot HTTP
# =================================================================================

@pytest.mark.parametrize("trasa", ["lista", "pobierz"])
async def test_projekt_bez_pliku_konfiguracji_daje_409_z_komunikatem_o_braku_sekcji_tickets(
    kokpit, zespol, projekt, monkeypatch, trasa,
):
    dostawca = _podlacz(monkeypatch, _linear)
    assert not (projekt.korzen / ".cockpit").exists()

    odp = await _odczytaj(zespol, trasa)

    assert odp.status_code == 409
    assert odp.json() == {"detail": BRAK_KONFIGURACJI}
    assert dostawca.zadania == []


async def test_projekt_z_konfiguracja_bez_sekcji_tickets_daje_409_z_komunikatem_o_braku_sekcji(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, None, stack=["python"])
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "pobierz")

    assert odp.status_code == 409
    assert odp.json() == {"detail": BRAK_KONFIGURACJI}
    assert dostawca.zadania == []


async def test_nieznane_zrodlo_ticketow_w_konfiguracji_daje_409_z_lista_dozwolonych(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "gitlab"})
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "pobierz")

    assert odp.status_code == 409
    assert odp.json() == {"detail": ZLE_ZRODLO}
    assert dostawca.zadania == []


async def test_zepsuty_yaml_konfiguracji_daje_409_z_prefiksem_pliku_projektu(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_tekst(projekt, "tickets: [niedomkniete\n")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _odczytaj(zespol, "lista")

    assert odp.status_code == 409
    assert odp.json()["detail"].startswith(".cockpit/project.yaml: niepoprawny YAML")
    assert dostawca.zadania == []


async def test_lista_z_githuba_bez_repo_w_konfiguracji_daje_409_z_komunikatem_o_repo(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github"})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _odczytaj(zespol, "lista")

    assert odp.status_code == 409
    assert odp.json() == {"detail": BRAK_REPO}
    assert dostawca.zadania == []


async def test_jira_z_bledna_konfiguracja_daje_409_z_komunikatem_adaptera(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "jira", "tryb": "data_center"})   # brak adresu
    _ustaw_token(monkeypatch, "jira")
    dostawca = _podlacz(monkeypatch, _jira)

    odp = await _odczytaj(zespol, "pobierz", id_ticketu="AB-7")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "Jira: niepoprawna konfiguracja, błędne pola: adres."}
    assert dostawca.zadania == []


# =================================================================================
# 7. Operacje runnera na prawdziwym zakresie (konfiguracja i walidacja wejscia)
# =================================================================================

async def _op(typ: str, projekt, **dane) -> dict:
    polecenie = {"type": typ, "req_id": "r-1", "projekt": str(projekt.korzen), **dane}
    return await asyncio.wait_for(operacje.wykonaj(polecenie), 20)


@pytest.mark.parametrize(("typ", "dane"), [
    ("ticket_lista", {}),
    ("ticket_pobierz", {"id": "HAR-1"}),
    ("ticket_zapisz_wynik", {"id": "HAR-1"}),
], ids=["lista", "pobierz", "zapisz_wynik"])
async def test_operacje_ticketowe_bez_pliku_projektu_zglaszaja_brak_konfiguracji(
    projekt, monkeypatch, typ, dane,
):
    dostawca = _podlacz(monkeypatch)

    odp = await _op(typ, projekt, **dane)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "brak_konfiguracji",
                   "blad": BRAK_KONFIGURACJI}
    assert dostawca.zadania == []


@pytest.mark.parametrize("zrodlo", [
    {"repo": "x/y"},          # brak klucza source
    {"source": "gitlab"},     # spoza listy
    {"source": 5},            # nie napis
    {"source": ["linear"]},   # lista: niehaszowalna, nie moze wywrocic sprawdzenia
    {"source": True},
], ids=["brak_source", "spoza_listy", "liczba", "lista", "bool"])
async def test_zle_albo_brakujace_source_zglaszaja_zla_konfiguracje(projekt, monkeypatch, zrodlo):
    _pisz_konfig(projekt, zrodlo)
    dostawca = _podlacz(monkeypatch)

    odp = await _op("ticket_pobierz", projekt, id="HAR-1")

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja", "blad": ZLE_ZRODLO}
    assert dostawca.zadania == []


async def test_nieczytelny_plik_konfiguracji_zglasza_zla_konfiguracje_z_prefiksem_pliku(
    projekt, monkeypatch,
):
    _pisz_tekst(projekt, "stack: [a\n")
    _podlacz(monkeypatch)

    odp = await _op("ticket_lista", projekt)

    assert odp["ok"] is False
    assert odp["kod"] == "zla_konfiguracja"
    assert odp["blad"].startswith(".cockpit/project.yaml: niepoprawny YAML")


async def test_konfiguracja_bedaca_lista_zamiast_mapy_to_zla_konfiguracja_z_komunikatem_walidatora(
    projekt, monkeypatch,
):
    _pisz_tekst(projekt, "- a\n- b\n")
    _podlacz(monkeypatch)

    odp = await _op("ticket_lista", projekt)

    assert odp["kod"] == "zla_konfiguracja"
    assert odp["blad"] == ".cockpit/project.yaml: project.yaml musi byc mapa"


async def test_przy_kilku_bledach_konfiguracji_komunikat_niesie_pierwszy_z_nich(
    projekt, monkeypatch,
):
    # `stack` jest sprawdzany przed `capabilities`: dwa bledy, kolejnosc jest znana.
    _pisz_tekst(projekt, "stack: 5\ncapabilities: 5\ntickets:\n  source: linear\n")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_lista", projekt)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja",
                   "blad": ".cockpit/project.yaml: stack musi byc lista napisow"}
    assert dostawca.zadania == []


async def test_plik_konfiguracji_bedacy_katalogiem_to_zla_konfiguracja_z_prefiksem_pliku(
    projekt, monkeypatch,
):
    (projekt.korzen / ".cockpit" / "project.yaml").mkdir(parents=True)
    _podlacz(monkeypatch)

    odp = await _op("ticket_lista", projekt)

    assert odp["ok"] is False
    assert odp["kod"] == "zla_konfiguracja"
    assert odp["blad"].startswith(".cockpit/project.yaml: nie mozna odczytac pliku projektu")


async def test_lista_z_githuba_bez_repo_zglasza_zla_konfiguracje(projekt, monkeypatch):
    _pisz_konfig(projekt, {"source": "github"})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja", "blad": BRAK_REPO}
    assert dostawca.zadania == []


ZLE_TYPY_STANU = "tickets.typy_stanu musi być listą napisów"
ZLE_ETYKIETY = "tickets.etykiety musi być listą napisów"
ZLY_STAN = "tickets.stan musi być jednym z: open, closed, all"


@pytest.mark.parametrize("typy_stanu", [
    "x",                    # napis: sam iterowany dałby listę znaków
    [1],                    # lista z liczbą
    ["started", 1],         # tylko jeden element zły: `any` zamiast `all` by przepuścił
    {"started": "x"},       # mapa
], ids=["napis", "liczba_na_liscie", "jeden_zly_element", "mapa"])
async def test_typy_stanu_lineara_niebedace_lista_napisow_to_zla_konfiguracja_bez_zadania(
    projekt, monkeypatch, typy_stanu,
):
    _pisz_konfig(projekt, {"source": "linear", "typy_stanu": typy_stanu})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_lista", projekt)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja",
                   "blad": ZLE_TYPY_STANU}
    assert dostawca.zadania == []


async def test_niepusty_filtr_listy_z_lineara_pomija_zla_konfiguracje_typow_stanu(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear", "typy_stanu": "x"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_lista", projekt, filtr="canceled, started")

    assert odp["ok"] is True, odp
    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["canceled", "started"]]


async def test_poprawne_typy_stanu_z_konfiguracji_lineara_trafiaja_do_zadania(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear", "typy_stanu": ["completed", "canceled"]})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_lista", projekt)

    assert odp["ok"] is True, odp
    assert [_zmienne(z)["typy"] for z in dostawca.zadania] == [["completed", "canceled"]]


@pytest.mark.parametrize("etykiety", [
    "bug",
    [1],
    ["bug", 1],
    {"bug": "x"},
], ids=["napis", "liczba_na_liscie", "jeden_zly_element", "mapa"])
async def test_etykiety_githuba_niebedace_lista_napisow_to_zla_konfiguracja_bez_zadania(
    projekt, monkeypatch, etykiety,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO, "etykiety": etykiety})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja",
                   "blad": ZLE_ETYKIETY}
    assert dostawca.zadania == []


async def test_niepusty_filtr_listy_z_githuba_pomija_zla_konfiguracje_etykiet(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO, "etykiety": "bug"})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt, filtr="ui, backend")

    assert odp["ok"] is True, odp
    assert dostawca.zadania[0].url.params["labels"] == "ui,backend"


async def test_lista_z_githuba_bez_etykiet_w_konfiguracji_i_bez_filtra_nie_wysyla_parametru_etykiet(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt)

    assert odp["ok"] is True, odp
    assert "labels" not in dostawca.zadania[0].url.params


@pytest.mark.parametrize("stan", ["merged", 1, [], None],
                         ids=["spoza_listy", "liczba", "lista", "null"])
async def test_stan_githuba_spoza_open_closed_all_to_zla_konfiguracja_bez_zadania(
    projekt, monkeypatch, stan,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO, "stan": stan})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja", "blad": ZLY_STAN}
    assert dostawca.zadania == []


async def test_filtr_listy_z_githuba_nie_pomija_zlego_stanu_z_konfiguracji(projekt, monkeypatch):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO, "stan": "merged"})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt, filtr="bug")

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja", "blad": ZLY_STAN}
    assert dostawca.zadania == []


@pytest.mark.parametrize("stan", ["open", "closed", "all"])
async def test_kazdy_dozwolony_stan_githuba_z_konfiguracji_trafia_do_zadania(
    projekt, monkeypatch, stan,
):
    _pisz_konfig(projekt, {"source": "github", "repo": REPO, "stan": stan})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt)

    assert odp["ok"] is True, odp
    assert dostawca.zadania[0].url.params["state"] == stan


@pytest.mark.parametrize("repo", [5, ""], ids=["liczba", "pusty_napis"])
async def test_repo_githuba_niebedace_niepustym_napisem_to_jak_brak_repo(
    projekt, monkeypatch, repo,
):
    _pisz_konfig(projekt, {"source": "github", "repo": repo})
    _ustaw_token(monkeypatch, "github")
    dostawca = _podlacz(monkeypatch, _github)

    odp = await _op("ticket_lista", projekt)

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zla_konfiguracja", "blad": BRAK_REPO}
    assert dostawca.zadania == []


async def test_pobranie_z_githuba_nie_wymaga_repo_w_konfiguracji(projekt, monkeypatch):
    _pisz_konfig(projekt, {"source": "github"})
    _ustaw_token(monkeypatch, "github")
    _podlacz(monkeypatch, _github)

    odp = await _op("ticket_pobierz", projekt, id=ID_GITHUB)

    assert odp["ok"] is True, odp
    assert odp["wynik"]["ticket"]["id"] == ID_GITHUB


async def test_pobranie_wynik_ma_ticket_w_formie_json_i_ostrzezenia(projekt, monkeypatch):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_pobierz", projekt, id="HAR-53")

    assert odp["ok"] is True, odp
    assert sorted(odp["wynik"]) == ["ostrzezenia", "ticket"]
    assert odp["wynik"]["ostrzezenia"] == []
    assert list(odp["wynik"]["ticket"]) == KLUCZE_TICKETU
    json.dumps(odp)   # odpowiedz da sie wyslac do kokpitu


@pytest.mark.parametrize(("typ", "dane"), [
    ("ticket_lista", {"projekt": 5}),
    ("ticket_pobierz", {"projekt": ["x"], "id": "HAR-1"}),
    ("ticket_zapisz_wynik", {"projekt": None, "id": "HAR-1"}),
    ("ticket_pobierz", {"id": 7}),
    ("ticket_zapisz_wynik", {"id": ["HAR-1"]}),
    ("ticket_pobierz", {}),
    ("ticket_lista", {"filtr": 5}),
    ("ticket_lista", {"filtr": ["a"]}),
], ids=["lista_projekt", "pobierz_projekt", "zapis_projekt", "pobierz_id_liczba",
        "zapis_id_lista", "pobierz_bez_id", "lista_filtr_liczba", "lista_filtr_lista"])
async def test_operacje_ticketowe_ze_zlym_typem_pola_zglaszaja_zle_dane(
    projekt, monkeypatch, typ, dane,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)
    polecenie = {"type": typ, "req_id": "r-1", "projekt": str(projekt.korzen), **dane}

    odp = await asyncio.wait_for(operacje.wykonaj(polecenie), 20)

    assert odp["ok"] is False
    assert odp["kod"] == "zle_dane"
    assert dostawca.zadania == []


async def test_lista_przyjmuje_brak_pola_filtr_i_filtr_none(projekt, monkeypatch):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    _podlacz(monkeypatch, _linear)

    bez = await _op("ticket_lista", projekt)
    z_none = await _op("ticket_lista", projekt, filtr=None)

    assert bez["ok"] is True, bez
    assert z_none["ok"] is True, z_none


@pytest.mark.parametrize("typ", ["ticket_lista", "ticket_pobierz", "ticket_zapisz_wynik"])
async def test_operacje_ticketowe_na_katalogu_poza_zakresem_runnera_zglaszaja_poza_zakresem(
    projekt, monkeypatch, typ,
):
    _pisz_konfig(SimpleNamespace(korzen=projekt.obcy), {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await asyncio.wait_for(operacje.wykonaj(
        {"type": typ, "req_id": "r-1", "projekt": str(projekt.obcy), "id": "HAR-1"}), 20)

    assert odp["ok"] is False
    assert odp["kod"] == "poza_zakresem"
    assert dostawca.zadania == []


# =================================================================================
# 8. Zapis wyniku do systemu ticketow jest wylaczony, dopoki projekt go swiadomie nie wlaczy
# =================================================================================

ZRODLA = ["linear", "github", "jira"]


def _konfig_zapisu(zrodlo: str, **dodatki) -> dict:
    return {**KONFIGURACJE[zrodlo][0], **dodatki}


@pytest.mark.parametrize("zrodlo", ZRODLA)
async def test_zapis_wyniku_bez_flagi_post_result_jest_wylaczony_i_nie_wola_dostawcy(
    projekt, monkeypatch, zrodlo,
):
    _pisz_konfig(projekt, _konfig_zapisu(zrodlo))
    _ustaw_token(monkeypatch, zrodlo)
    dostawca = _podlacz(monkeypatch, OBSLUGA[zrodlo])

    odp = await _op("ticket_zapisz_wynik", projekt, id=KONFIGURACJE[zrodlo][1])

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zapis_wylaczony",
                   "blad": ZAPIS_WYLACZONY}
    assert dostawca.zadania == []


@pytest.mark.parametrize("wartosc", [False, "true", 1, None, "yes", 0],
                         ids=["false", "napis_true", "liczba_1", "null", "napis_yes", "zero"])
async def test_zapis_wyniku_z_flaga_inna_niz_prawda_logiczna_pozostaje_wylaczony(
    projekt, monkeypatch, wartosc,
):
    _pisz_konfig(projekt, _konfig_zapisu("linear", post_result=wartosc))
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_zapisz_wynik", projekt, id="HAR-53")

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zapis_wylaczony",
                   "blad": ZAPIS_WYLACZONY}
    assert dostawca.zadania == []


@pytest.mark.parametrize("zrodlo", ZRODLA)
async def test_zapis_wyniku_z_wlaczona_flaga_jest_jeszcze_niedostepny_i_nie_wola_dostawcy(
    projekt, monkeypatch, zrodlo,
):
    _pisz_konfig(projekt, _konfig_zapisu(zrodlo, post_result=True))
    _ustaw_token(monkeypatch, zrodlo)
    dostawca = _podlacz(monkeypatch, OBSLUGA[zrodlo])

    odp = await _op("ticket_zapisz_wynik", projekt, id=KONFIGURACJE[zrodlo][1])

    assert odp == {"req_id": "r-1", "ok": False, "kod": "zapis_niedostepny",
                   "blad": ZAPIS_NIEDOSTEPNY}
    assert dostawca.zadania == []


async def test_zapis_wyniku_przy_zlym_zrodle_zglasza_zla_konfiguracje_przed_sprawdzeniem_flagi(
    projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "gitlab", "post_result": True})
    _podlacz(monkeypatch)

    odp = await _op("ticket_zapisz_wynik", projekt, id="X-1")

    assert odp["kod"] == "zla_konfiguracja"
    assert odp["blad"] == ZLE_ZRODLO


async def test_zapis_wyniku_nie_wymaga_tokenu_dostawcy_do_odmowy(projekt, monkeypatch):
    _pisz_konfig(projekt, _konfig_zapisu("linear"))   # token nieustawiony (autouse)
    _podlacz(monkeypatch, _linear)

    odp = await _op("ticket_zapisz_wynik", projekt, id="HAR-53")

    assert odp["kod"] == "zapis_wylaczony"


async def test_zapis_wyniku_przez_kokpit_bez_flagi_to_409_z_komunikatem_i_zero_zadan_do_dostawcy(
    kokpit, zespol, projekt, monkeypatch,
):
    _pisz_konfig(projekt, {"source": "linear"})
    _ustaw_token(monkeypatch, "linear")
    dostawca = _podlacz(monkeypatch, _linear)

    async with kokpit.maker() as s:
        with pytest.raises(StanNiemozliwy) as exc:
            await asyncio.wait_for(zapytania_runnera.zapytaj_runnera_online(
                s, zespol.tenant_id, "ticket_zapisz_wynik",
                {"projekt": str(projekt.korzen), "id": "HAR-53"},
                bus.connected_runner_ids(), bus.zapytaj, runner_id="runner-alfa"), 20)

    assert type(exc.value) is StanNiemozliwy
    assert str(exc.value) == ZAPIS_WYLACZONY
    assert dostawca.zadania == []


# =================================================================================
# 9. Serwis kokpitu: ksztalt odpowiedzi runnera, wybor projektu, runner projektu
# =================================================================================

class _Wysylka:
    """Wstrzykiwana `wyslij`: zapamietuje wywolania i oddaje zadana odpowiedz."""

    def __init__(self, wynik=None, *, odpowiedz: dict | None = None) -> None:
        self.odpowiedz = odpowiedz if odpowiedz is not None else {"ok": True, "wynik": wynik}
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dict(dane)))
        return self.odpowiedz


SKROT = {"id": "HAR-1", "tytul": "Pierwszy", "stan": "Todo"}
TICKET = {"source": "linear", "id": "HAR-1"}


@pytest.fixture
async def alfa(kokpit):
    """Zespol Alfa z jednym runnerem i projektem; zwraca (tenant_id, project_id)."""
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-alfa", konto.tenant_id)
    projekt_id = await kokpit.dodaj_projekt(konto.tenant_id, "runner-alfa", Path("/p/alfa"))
    return SimpleNamespace(tenant_id=konto.tenant_id, projekt_id=projekt_id)


POLACZONE = frozenset({"runner-alfa"})


async def _lista(kokpit, alfa, wysylka, filtr=None, polaczone=POLACZONE, projekt_id=None,
                 tenant_id=None):
    async with kokpit.maker() as s:
        return await _serwis().lista(
            s, tenant_id or alfa.tenant_id, projekt_id or alfa.projekt_id, filtr, polaczone,
            wysylka)


async def _pobierz(kokpit, alfa, wysylka, id_ticketu="HAR-1", polaczone=POLACZONE,
                   projekt_id=None, tenant_id=None):
    async with kokpit.maker() as s:
        return await _serwis().pobierz(
            s, tenant_id or alfa.tenant_id, projekt_id or alfa.projekt_id, id_ticketu,
            polaczone, wysylka)


async def test_serwis_listy_pyta_runnera_projektu_o_ticket_lista_z_katalogiem_i_filtrem(
    kokpit, alfa,
):
    wysylka = _Wysylka([SKROT])

    wynik = await _lista(kokpit, alfa, wysylka, filtr="a,b")

    assert wynik == [SKROT]
    assert wysylka.wywolania == [
        ("runner-alfa", "ticket_lista", {"projekt": "/p/alfa", "filtr": "a,b"})]


async def test_serwis_pobrania_pyta_runnera_projektu_o_ticket_pobierz_z_katalogiem_i_id(
    kokpit, alfa,
):
    wynik_runnera = {"ticket": TICKET, "ostrzezenia": ["uwaga"]}
    wysylka = _Wysylka(wynik_runnera)

    wynik = await _pobierz(kokpit, alfa, wysylka, id_ticketu="HAR-1")

    assert wynik == wynik_runnera
    assert wysylka.wywolania == [
        ("runner-alfa", "ticket_pobierz", {"projekt": "/p/alfa", "id": "HAR-1"})]


async def test_serwis_listy_pusta_lista_od_runnera_to_pusta_lista_a_nie_blad(kokpit, alfa):
    assert await _lista(kokpit, alfa, _Wysylka([])) == []


async def test_serwis_pobrania_pusta_lista_ostrzezen_jest_poprawna(kokpit, alfa):
    wynik_runnera = {"ticket": TICKET, "ostrzezenia": []}

    assert await _pobierz(kokpit, alfa, _Wysylka(wynik_runnera)) == wynik_runnera


@pytest.mark.parametrize("wynik", [
    {"id": "HAR-1", "tytul": "T", "stan": "S"},
    "HAR-1",
    None,
    ["napis"],
    [5],
    [SKROT, "napis"],
    [SKROT, {"id": "HAR-2", "tytul": "T"}],
    [{"tytul": "T", "stan": "S"}],
    [{"id": "HAR-1", "stan": "S"}],
    [{"id": 1, "tytul": "T", "stan": "S"}],
    [{"id": "HAR-1", "tytul": 2, "stan": "S"}],
    [{"id": "HAR-1", "tytul": "T", "stan": 3}],
    [{"id": "HAR-1", "tytul": None, "stan": "S"}],
    [{"id": "HAR-1", "tytul": "T", "stan": None}],
], ids=["slownik_zamiast_listy", "napis", "none", "element_napis", "element_liczba",
        "drugi_element_zly", "drugi_bez_stanu", "bez_id", "bez_tytulu", "id_liczba",
        "tytul_liczba", "stan_liczba", "tytul_none", "stan_none"])
async def test_nieczytelna_lista_od_runnera_to_409_z_komunikatem_a_nie_wyjatek_biblioteki(
    kokpit, alfa, wynik,
):
    with pytest.raises(StanNiemozliwy) as exc:
        await _lista(kokpit, alfa, _Wysylka(wynik))

    assert type(exc.value) is StanNiemozliwy
    assert str(exc.value) == NIECZYTELNA_LISTA


@pytest.mark.parametrize("wynik", [
    [],
    "napis",
    None,
    {},
    {"ostrzezenia": []},
    {"ticket": TICKET},
    {"ticket": "napis", "ostrzezenia": []},
    {"ticket": None, "ostrzezenia": []},
    {"ticket": [TICKET], "ostrzezenia": []},
    {"ticket": TICKET, "ostrzezenia": "uwaga"},
    {"ticket": TICKET, "ostrzezenia": None},
    {"ticket": TICKET, "ostrzezenia": {"a": "b"}},
    {"ticket": TICKET, "ostrzezenia": [5]},
    {"ticket": TICKET, "ostrzezenia": ["ok", 5]},
], ids=["lista", "napis", "none", "pusty_slownik", "bez_ticketu", "bez_ostrzezen",
        "ticket_napis", "ticket_none", "ticket_lista", "ostrzezenia_napis", "ostrzezenia_none",
        "ostrzezenia_slownik", "ostrzezenie_liczba", "drugie_ostrzezenie_liczba"])
async def test_nieczytelny_ticket_od_runnera_to_409_z_komunikatem_a_nie_wyjatek_biblioteki(
    kokpit, alfa, wynik,
):
    with pytest.raises(StanNiemozliwy) as exc:
        await _pobierz(kokpit, alfa, _Wysylka(wynik))

    assert type(exc.value) is StanNiemozliwy
    assert str(exc.value) == NIECZYTELNY_TICKET


async def test_serwis_runner_offline_zglasza_runner_offline_z_odsylaczem_i_nie_wysyla_zapytania(
    kokpit, alfa,
):
    wysylka = _Wysylka([SKROT])

    with pytest.raises(RunnerOffline) as lista:
        await _lista(kokpit, alfa, wysylka, polaczone=frozenset())
    with pytest.raises(RunnerOffline) as pobranie:
        await _pobierz(kokpit, alfa, wysylka, polaczone=frozenset())

    assert str(lista.value) == OFFLINE
    assert str(pobranie.value) == OFFLINE
    assert wysylka.wywolania == []


async def test_serwis_nieznany_projekt_to_nie_znaleziono_a_runner_nie_dostaje_zapytania(
    kokpit, alfa,
):
    wysylka = _Wysylka([SKROT])

    with pytest.raises(NieZnaleziono) as lista:
        await _lista(kokpit, alfa, wysylka, projekt_id=str(uuid.uuid4()))
    with pytest.raises(NieZnaleziono) as pobranie:
        await _pobierz(kokpit, alfa, wysylka, projekt_id=str(uuid.uuid4()))

    assert str(lista.value) == "projekt nieznany"
    assert str(pobranie.value) == "projekt nieznany"
    assert wysylka.wywolania == []


async def test_serwis_projekt_innego_zespolu_to_nie_znaleziono_mimo_ze_istnieje_w_bazie(
    kokpit, alfa,
):
    beta = await kokpit.konto("bea@przyklad.pl", "Beta")
    await kokpit.dodaj_runnera("runner-beta", beta.tenant_id)
    obcy = await kokpit.dodaj_projekt(beta.tenant_id, "runner-beta", Path("/p/beta"))
    wysylka = _Wysylka([SKROT])

    with pytest.raises(NieZnaleziono) as lista:
        await _lista(kokpit, alfa, wysylka, projekt_id=obcy)
    with pytest.raises(NieZnaleziono) as pobranie:
        await _pobierz(kokpit, alfa, wysylka, projekt_id=obcy)

    assert str(lista.value) == "projekt nieznany"
    assert str(pobranie.value) == "projekt nieznany"
    assert wysylka.wywolania == []


async def test_serwis_zespol_wlasciciel_projektu_dostaje_odpowiedz_gdy_istnieje_projekt_obcego_zespolu(
    kokpit, alfa,
):
    beta = await kokpit.konto("bea@przyklad.pl", "Beta")
    await kokpit.dodaj_runnera("runner-beta", beta.tenant_id)
    await kokpit.dodaj_projekt(beta.tenant_id, "runner-beta", Path("/p/beta"))
    wysylka = _Wysylka([SKROT])

    wynik = await _lista(kokpit, alfa, wysylka)

    assert wynik == [SKROT]
    assert wysylka.wywolania[0][2]["projekt"] == "/p/alfa"


async def test_serwis_pyta_runnera_projektu_nawet_gdy_wczesniejszy_runner_zespolu_jest_online(
    kokpit, alfa,
):
    await kokpit.dodaj_runnera("runner-0-wczesniejszy", alfa.tenant_id)
    wysylka = _Wysylka([SKROT])

    await _lista(kokpit, alfa, wysylka,
                 polaczone=frozenset({"runner-0-wczesniejszy", "runner-alfa"}))

    assert [w[0] for w in wysylka.wywolania] == ["runner-alfa"]


@pytest.mark.parametrize("kod", ["brak_tokenu", "token_odrzucony", "zapis_wylaczony",
                                 "zla_konfiguracja", "brak_konfiguracji", "limit",
                                 "blad_dostawcy"])
async def test_serwis_inne_kody_bledu_runnera_niz_brak_ticketu_to_409_z_komunikatem_doslownie(
    kokpit, alfa, kod,
):
    wysylka = _Wysylka(odpowiedz={"ok": False, "kod": kod, "blad": f"komunikat-{kod}"})

    with pytest.raises(StanNiemozliwy) as exc:
        await _pobierz(kokpit, alfa, wysylka)

    assert type(exc.value) is StanNiemozliwy
    assert str(exc.value) == f"komunikat-{kod}"


async def test_serwis_kod_nie_znaleziono_od_runnera_to_nie_znaleziono_z_komunikatem_dostawcy(
    kokpit, alfa,
):
    wysylka = _Wysylka(odpowiedz={"ok": False, "kod": "nie_znaleziono",
                                  "blad": "Linear: nie znaleziono zgłoszenia HAR-9."})

    with pytest.raises(NieZnaleziono) as exc:
        await _pobierz(kokpit, alfa, wysylka, id_ticketu="HAR-9")

    assert str(exc.value) == "Linear: nie znaleziono zgłoszenia HAR-9."


async def test_serwis_poza_zakresem_od_runnera_pozostaje_odmowa_dostepu(kokpit, alfa):
    wysylka = _Wysylka(odpowiedz={"ok": False, "kod": "poza_zakresem",
                                  "blad": "sciezka poza zakresem runnera"})

    with pytest.raises(PlikPozaZakresem):
        await _lista(kokpit, alfa, wysylka)


@pytest.mark.parametrize("odpowiedz", [
    {"ok": False, "kod": "nie_znaleziono"},
    {"ok": False, "kod": "nie_znaleziono", "blad": ""},
], ids=["bez_pola_blad", "pusty_blad"])
async def test_serwis_kod_nie_znaleziono_bez_komunikatu_to_nie_znaleziono_z_komunikatem_zastepczym(
    kokpit, alfa, odpowiedz,
):
    with pytest.raises(NieZnaleziono) as exc:
        await _pobierz(kokpit, alfa, _Wysylka(odpowiedz=odpowiedz))

    assert str(exc.value) == "runner zglosil blad"


# =================================================================================
# 10. Kontrakt tras ticketowych w schemacie OpenAPI prawdziwej aplikacji
# =================================================================================

LISTA_TICKETOW = "/api/tickety"
POBRANIE_TICKETU = "/api/tickety/{id_ticketu}"
REF = "#/components/schemas/"
KODY_ODPOWIEDZI_TICKETOW = ["200", "401", "403", "404", "409", "422", "500"]
S, N = "string", "null"

# model -> {pole: zbior typow (albo $ref)}; kazde pole wymagane, takze gdy wartosc to null
POLA_TICKETOW = {
    "SkrotTicketu": {"id": {S}, "tytul": {S}, "stan": {S}},
    "KomentarzTicketu": {"author": {S}, "body": {S}, "at": {S, N}},
    "TicketJsonOdp": {
        "source": {S}, "id": {S}, "url": {S, N}, "title": {S}, "description": {S},
        "acceptance_criteria": {"array"}, "status": {S, N}, "priority": {S, N},
        "labels": {"array"}, "comments": {"array"}, "fetched_at": {S}, "raw": {"object"},
    },
    "TicketPobrany": {"ticket": {REF + "TicketJsonOdp"}, "ostrzezenia": {"array"}},
}


@pytest.fixture(scope="module")
def schemat_api() -> dict:
    """Swiezy schemat prawdziwej aplikacji (bez cache)."""
    poprzedni = app_mod.app.openapi_schema
    app_mod.app.openapi_schema = None
    try:
        return copy.deepcopy(app_mod.app.openapi())
    finally:
        app_mod.app.openapi_schema = poprzedni


def _typy_pola(pole: dict) -> set[str]:
    warianty = pole["anyOf"] if "anyOf" in pole else [pole]
    return {w["$ref"] if "$ref" in w else w["type"] for w in warianty}


def _tresc(odpowiedz: dict) -> dict:
    return odpowiedz["content"]["application/json"]["schema"]


@pytest.mark.parametrize("model", list(POLA_TICKETOW))
def test_model_odpowiedzi_ticketow_ma_dokladnie_pola_z_kontraktu_i_wszystkie_sa_wymagane(
    schemat_api, model,
):
    definicja = schemat_api["components"]["schemas"][model]
    oczekiwane = POLA_TICKETOW[model]

    assert set(definicja["properties"]) == set(oczekiwane)
    assert {p: _typy_pola(v) for p, v in definicja["properties"].items()} == oczekiwane
    assert set(definicja["required"]) == set(oczekiwane)


def test_komentarze_i_kryteria_w_modelu_ticketu_to_listy_wlasciwych_elementow(schemat_api):
    pola = schemat_api["components"]["schemas"]["TicketJsonOdp"]["properties"]

    assert pola["comments"]["items"] == {"$ref": REF + "KomentarzTicketu"}
    assert pola["acceptance_criteria"]["items"] == {"type": S}
    assert pola["labels"]["items"] == {"type": S}
    assert schemat_api["components"]["schemas"]["TicketPobrany"]["properties"][
        "ostrzezenia"]["items"] == {"type": S}


def test_lista_ticketow_w_schemacie_to_tablica_skrotow_z_identyfikatorem_operacji(schemat_api):
    op = schemat_api["paths"][LISTA_TICKETOW]["get"]

    assert op["operationId"] == "lista_ticketow_api_tickety_get"
    tresc = _tresc(op["responses"]["200"])
    assert tresc["type"] == "array"
    assert tresc["items"] == {"$ref": REF + "SkrotTicketu"}


def test_pobranie_ticketu_w_schemacie_to_model_ticket_pobrany_z_identyfikatorem_operacji(
    schemat_api,
):
    op = schemat_api["paths"][POBRANIE_TICKETU]["get"]

    assert op["operationId"] == "pobierz_ticket_api_tickety__id_ticketu__get"
    assert _tresc(op["responses"]["200"]) == {"$ref": REF + "TicketPobrany"}


@pytest.mark.parametrize("sciezka", [LISTA_TICKETOW, POBRANIE_TICKETU])
def test_trasa_ticketowa_opisuje_dokladnie_kody_sukcesu_i_bledow_z_modelem_blad(
    schemat_api, sciezka,
):
    odpowiedzi = schemat_api["paths"][sciezka]["get"]["responses"]

    assert sorted(odpowiedzi) == KODY_ODPOWIEDZI_TICKETOW
    for kod in KODY_ODPOWIEDZI_TICKETOW[1:]:
        assert _tresc(odpowiedzi[kod]) == {"$ref": REF + "Blad"}, kod


def test_lista_ticketow_ma_wymagany_projekt_do_36_znakow_i_opcjonalny_filtr_do_500(schemat_api):
    parametry = {p["name"]: p for p in schemat_api["paths"][LISTA_TICKETOW]["get"]["parameters"]}

    assert set(parametry) == {"projekt", "filtr"}
    assert parametry["projekt"]["in"] == "query"
    assert parametry["projekt"]["required"] is True
    assert parametry["projekt"]["schema"]["maxLength"] == 36
    assert parametry["filtr"]["in"] == "query"
    assert parametry["filtr"]["required"] is False
    assert parametry["filtr"]["schema"]["anyOf"] == [
        {"type": "string", "maxLength": 500}, {"type": "null"}]


def test_pobranie_ticketu_ma_identyfikator_w_sciezce_i_wymagany_projekt_do_36_znakow(
    schemat_api,
):
    parametry = {p["name"]: p for p in schemat_api["paths"][POBRANIE_TICKETU]["get"]["parameters"]}

    assert set(parametry) == {"id_ticketu", "projekt"}
    assert parametry["id_ticketu"]["in"] == "path"
    assert parametry["id_ticketu"]["required"] is True
    assert parametry["id_ticketu"]["schema"]["type"] == "string"
    assert parametry["projekt"]["in"] == "query"
    assert parametry["projekt"]["required"] is True
    assert parametry["projekt"]["schema"]["maxLength"] == 36

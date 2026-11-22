"""HAR-170: odbior runu - ekran dowodow (twierdzenia, dowody silnika, braki, ryzyko) i decyzja.

Testy pisane PRZED implementacja (TDD). Kontrakt: runner dosyla w zdarzeniu konca kroku
(`step_finished`/`step_failed`) pola `dowod`, `verdict`, `kotwice`, `artefakty` (zapis w
`Event.dane`, brak pol to SQL NULL); `GET /api/runs/{id}/dowody` sklada twierdzenia, dowody,
werdykty, braki, ryzyka i pomiary WYLACZNIE z bazy kokpitu; `POST /api/runs/{id}/decyzja-odbioru`
zapisuje decyzje czlowieka jako zdarzenie `acceptance` (kto, kiedy, uzasadnienie), nie zmieniajac
statusu runu.

Skutki czytamy z bazy (nowa sesja), nie z samej odpowiedzi HTTP. Testy wyscigu i skladni
specyficznej dla bazy: kompilacja zapytan dialektem PostgreSQL (SQLite ignoruje FOR UPDATE)
oraz przeplot dwoch decyzji na prawdziwym PostgreSQL (marker `baza`).
Silnik (`runner.silnik`) sprawdzamy na prawdziwym kodzie z `Szpiegiem` zamiast procesu agenta
(wzor `test_silnik_runu.py`), a jego zdarzenia przechodza przez prawdziwa trase `/api/events`.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Insert, Select, event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import (
    Base, Event, RolaWZespole, Run, Runner, RunStatus, StepRun, StepStatus, Tenant,
)
from cockpit.services import odbior_dowody
from cockpit.services.errors import StanNiemozliwy
from procesy import czekaj
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_wlasciciela
from test_krok_check import (
    DOWOD_TYPECHECK,
    KOD_CZERWONY,
    SzpiegZmieniajacyDrzewo,
    komenda,
    swiat_z_checkiem,
    swiat_z_dwoma_checkami,
)
from test_silnik_runu import K, Szpieg, jedno, swiat as swiat_silnika, uruchom
from runner.krok import WynikKroku

KORZEN_REPO = Path(__file__).resolve().parent.parent
TOKEN = "token-browarka"
NAGLOWEK = {"Authorization": f"Bearer {TOKEN}"}
URL_PG = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
tylko_postgres = pytest.mark.skipif(
    not URL_PG.startswith("postgresql"),
    reason="test przeplotu wymaga PostgreSQL: ustaw "
    "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test",
)
LIMIT_S = 15

SHA_A = "a" * 64
SHA_B = "b" * 64
CZAS_OD = "2026-03-05T14:07:09+00:00"
CZAS_DO = "2026-03-05T14:07:10+00:00"
UZASADNIENIE = "Ryzyko zaakceptowane — bramka lint pominięta, zgoda właściciela"
PUSTY_KONTEKST = "żadna bramka nie sprawdziła runu"


def dowod_wejscia(**zmiany) -> dict:
    """Dowod w dokladnym ksztalcie, jaki wysyla silnik (`runner.dowody`), z mozliwymi zmianami."""
    return {
        "capability": "typecheck", "exit_code": 0, "sha256": SHA_A,
        "started_at": CZAS_OD, "finished_at": CZAS_DO, "source": "engine",
        "plik": "dowod-typecheck.json", **zmiany,
    }


def dowod_w_odpowiedzi(step_id: str, dowod: dict) -> dict:
    return {
        "step_id": step_id, "capability": dowod["capability"], "source": dowod["source"],
        "exit_code": dowod["exit_code"], "sha256": dowod["sha256"], "plik": dowod["plik"],
    }


def brak(rodzaj: str, step_id: str | None, powod: str) -> dict:
    return {"rodzaj": rodzaj, "step_id": step_id, "powod": powod}


def utc(napis: str) -> datetime:
    """Czas z odpowiedzi JSON jako swiadomy strefy (SQLite oddaje daty bez strefy - to UTC)."""
    czas = datetime.fromisoformat(napis)
    return czas if czas.tzinfo else czas.replace(tzinfo=timezone.utc)


# =================================================================================================
# Swiat: dwa zespoly, runner Browarka, zalogowany wlasciciel
# =================================================================================================

@dataclass
class Swiat:
    klient: AsyncClient          # zalogowany wlasciciel zespolu "Browarek"
    engine: object
    maker: object
    stos: AsyncExitStack
    wlasciciel: object
    run_id: str                  # run Browarka w stanie `running` (wejscie runnera)
    tenant_id: str               # Browarek
    obcy_tenant_id: str          # Piekarnia
    runner_id: str
    obcy_runner_id: str

    async def klient_roli(self, rola: RolaWZespole, email: str) -> AsyncClient:
        """Klient zalogowanego czlonka Browarka w danej roli."""
        czlonek = await zaloz_czlonka(self.maker, self.tenant_id, rola, email)
        klient = await self.stos.enter_async_context(
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))
        await zaloguj_klienta(klient, self.maker, czlonek)
        return klient

    async def klient_bez_sesji(self) -> AsyncClient:
        return await self.stos.enter_async_context(
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))


async def _zbuduj_swiat(engine, monkeypatch) -> Swiat:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    teraz = datetime.now(timezone.utc)
    async with maker() as s:
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        runner = Runner(tenant_id=tenant_id, name="macbook", last_seen_at=teraz,
                        token_hash=sha256(TOKEN.encode()).hexdigest())
        obcy = Runner(tenant_id=piekarnia.id, name="obcy", last_seen_at=teraz,
                      token_hash=sha256(b"token-piekarni").hexdigest())
        s.add_all([runner, obcy])
        await s.flush()
        run = Run(tenant_id=tenant_id, runner_id=runner.id, status=RunStatus.running)
        s.add(run)
        await s.commit()
        ids = (run.id, tenant_id, piekarnia.id, runner.id, obcy.id)
    stos = AsyncExitStack()
    klient = await stos.enter_async_context(
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))
    await zaloguj_klienta(klient, maker, wlasciciel)
    return Swiat(klient, engine, maker, stos, wlasciciel, *ids)


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    s = await _zbuduj_swiat(engine, monkeypatch)
    try:
        yield s
    finally:
        await s.stos.aclose()
        await engine.dispose()


@pytest.fixture
async def swiat_pg(monkeypatch):
    engine = create_async_engine(URL_PG)
    s = await _zbuduj_swiat(engine, monkeypatch)
    try:
        yield s
    finally:
        await s.stos.aclose()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


# --- zasiew wprost w bazie ----------------------------------------------------------------------

async def zasiej_run(swiat: Swiat, *, status: RunStatus = RunStatus.to_accept,
                     tenant_id: str | None = None, bramki_powody: dict | None = None,
                     stan_pokrycia: str | None = None) -> str:
    tenant = tenant_id or swiat.tenant_id
    runner = swiat.runner_id if tenant == swiat.tenant_id else swiat.obcy_runner_id
    async with swiat.maker() as s:
        run = Run(tenant_id=tenant, runner_id=runner, status=status,
                  bramki_powody=bramki_powody, stan_pokrycia=stan_pokrycia)
        s.add(run)
        await s.commit()
        return run.id


async def zasiej_zdarzenie(swiat: Swiat, run_id: str, seq: int, kind: str, step_id: str, *,
                           tenant_id: str | None = None, text: str = "",
                           dane: dict | None = None) -> None:
    async with swiat.maker() as s:
        s.add(Event(tenant_id=tenant_id or swiat.tenant_id, run_id=run_id, seq=seq, kind=kind,
                    step_id=step_id, text=text, dane=dane))
        await s.commit()


async def zasiej_krok(swiat: Swiat, run_id: str, step_id: str, *, cost_usd: float | None = None,
                      tokens_in: int | None = None, tokens_out: int | None = None,
                      tenant_id: str | None = None,
                      status: StepStatus = StepStatus.finished) -> None:
    async with swiat.maker() as s:
        s.add(StepRun(tenant_id=tenant_id or swiat.tenant_id, run_id=run_id, step_id=step_id,
                      agent_id="", attempt=1, status=status, cost_usd=cost_usd,
                      tokens_in=tokens_in, tokens_out=tokens_out))
        await s.commit()


async def dowody(swiat: Swiat, run_id: str, klient: AsyncClient | None = None):
    return await (klient or swiat.klient).get(f"/api/runs/{run_id}/dowody")


async def dowody_ok(swiat: Swiat, run_id: str) -> dict:
    r = await dowody(swiat, run_id)
    assert r.status_code == 200, r.text
    return r.json()


# --- odczyt z bazy (nowa sesja) -------------------------------------------------------------------

async def liczba_zdarzen(swiat: Swiat) -> int:
    async with swiat.maker() as s:
        return (await s.execute(select(func.count()).select_from(Event))).scalar_one()


async def dane_zdarzen(swiat: Swiat, run_id: str) -> list[dict | None]:
    async with swiat.maker() as s:
        return list((await s.execute(
            select(Event.dane).where(Event.run_id == run_id).order_by(Event.seq))).scalars())


async def zdarzenia_odbioru(swiat: Swiat, run_id: str) -> list[Event]:
    async with swiat.maker() as s:
        return list((await s.execute(
            select(Event).where(Event.run_id == run_id, Event.kind == "acceptance")
            .order_by(Event.seq))).scalars())


async def status_runu(swiat: Swiat, run_id: str) -> RunStatus:
    async with swiat.maker() as s:
        return (await s.execute(select(Run.status).where(Run.id == run_id))).scalar_one()


# =================================================================================================
# A. Wejscie runnera: POST /api/events niesie dowod, werdykt, kotwice i artefakty
# =================================================================================================

async def wyslij(swiat: Swiat, kind: str = "step_finished", run_id: str | None = None,
                 step_id: str = "s0", **pola):
    return await swiat.klient.post(
        "/api/events",
        json={"run_id": run_id or swiat.run_id, "kind": kind, "step_id": step_id,
              "event_id": str(uuid.uuid4()), **pola},
        headers=NAGLOWEK)


async def wyslij_ok(swiat: Swiat, kind: str = "step_finished", **pola) -> None:
    r = await wyslij(swiat, kind, **pola)
    assert r.status_code == 200, r.text


async def odrzucone_bez_zapisu(swiat: Swiat, kind: str = "step_finished", **pola) -> None:
    przed = await liczba_zdarzen(swiat)
    r = await wyslij(swiat, kind, **pola)
    assert r.status_code == 422, r.text
    assert await liczba_zdarzen(swiat) == przed


POLA_KONCA_KROKU = {
    "dowod": dowod_wejscia(),
    "verdict": "pass",
    "kotwice": ["src/a.py:11", "web/b.tsx:7"],
    "artefakty": ["plan.md", "tdd.md"],
}


async def test_koniec_kroku_z_dowodem_werdyktem_kotwicami_i_artefaktami_zapisuje_je_w_danych_zdarzenia(
    swiat,
):
    await wyslij_ok(swiat, "step_finished", **POLA_KONCA_KROKU)

    assert await dane_zdarzen(swiat, swiat.run_id) == [{
        "dowod": dowod_wejscia(), "verdict": "pass",
        "kotwice": ["src/a.py:11", "web/b.tsx:7"], "artefakty": ["plan.md", "tdd.md"],
    }]


@pytest.mark.parametrize("pole", list(POLA_KONCA_KROKU))
async def test_zdarzenie_z_jednym_polem_dowodowym_zapisuje_w_danych_tylko_to_pole(swiat, pole):
    await wyslij_ok(swiat, "step_finished", **{pole: POLA_KONCA_KROKU[pole]})

    assert await dane_zdarzen(swiat, swiat.run_id) == [{pole: POLA_KONCA_KROKU[pole]}]


async def test_pusta_lista_kotwic_jest_zapisana_jako_pusta_lista_a_nie_jako_brak_danych(swiat):
    await wyslij_ok(swiat, "step_finished", kotwice=[])

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"kotwice": []}]


async def test_nieudany_krok_z_dowodem_i_werdyktem_tez_zapisuje_je_w_danych_zdarzenia(swiat):
    await wyslij_ok(swiat, "step_failed", text="kod wyjścia 3",
                    dowod=dowod_wejscia(exit_code=3), verdict="fail")

    assert await dane_zdarzen(swiat, swiat.run_id) == [
        {"dowod": dowod_wejscia(exit_code=3), "verdict": "fail"}]


async def test_zdarzenie_bez_pol_dowodowych_ma_w_bazie_sql_null_a_nie_json_null_ani_pusty_slownik(
    swiat,
):
    await wyslij_ok(swiat, "step_started")
    await wyslij_ok(swiat, "step_finished")

    async with swiat.maker() as s:
        z_null = (await s.execute(text("SELECT count(*) FROM event WHERE dane IS NULL"))).scalar_one()
        wszystkie = (await s.execute(text("SELECT count(*) FROM event"))).scalar_one()
    assert (z_null, wszystkie) == (2, 2)
    assert await dane_zdarzen(swiat, swiat.run_id) == [None, None]


async def test_pola_dowodowe_podane_jawnie_jako_null_nie_tworza_danych_zdarzenia(swiat):
    await wyslij_ok(swiat, "step_finished", dowod=None, verdict=None, kotwice=None, artefakty=None)

    async with swiat.maker() as s:
        z_null = (await s.execute(text("SELECT count(*) FROM event WHERE dane IS NULL"))).scalar_one()
    assert z_null == 1


async def test_zdarzenie_z_danymi_ma_w_bazie_wartosc_inna_niz_null(swiat):
    await wyslij_ok(swiat, "step_finished", verdict="pass")

    async with swiat.maker() as s:
        z_danymi = (await s.execute(
            text("SELECT count(*) FROM event WHERE dane IS NOT NULL"))).scalar_one()
    assert z_danymi == 1


async def test_dowod_z_kodem_wyjscia_none_przy_limicie_czasu_jest_przyjety(swiat):
    await wyslij_ok(swiat, "step_failed", text="przekroczono limit czasu",
                    dowod=dowod_wejscia(exit_code=None))

    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane["dowod"]["exit_code"] is None
    assert "exit_code" in dane["dowod"]


@pytest.mark.parametrize("kod", [-2_147_483_648, 2_147_483_647, 0, 3, -1])
async def test_kod_wyjscia_w_zakresie_int4_jest_przyjety(swiat, kod):
    await wyslij_ok(swiat, "step_failed", dowod=dowod_wejscia(exit_code=kod))

    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane["dowod"]["exit_code"] == kod


@pytest.mark.parametrize("kod", [-2_147_483_649, 2_147_483_648])
async def test_kod_wyjscia_poza_zakresem_int4_jest_odrzucony(swiat, kod):
    await odrzucone_bez_zapisu(swiat, "step_failed", dowod=dowod_wejscia(exit_code=kod))


@pytest.mark.parametrize("kod", [True, False, "0", "3", 1.0, 0.0])
async def test_kod_wyjscia_ktory_nie_jest_calkowita_liczba_jest_odrzucony(swiat, kod):
    # bool jest podklasa int, napis i liczba zmiennoprzecinkowa przechodza w trybie luznym
    await odrzucone_bez_zapisu(swiat, "step_failed", dowod=dowod_wejscia(exit_code=kod))


async def test_dowod_z_nieznanym_kluczem_jest_odrzucony_a_zdarzenie_nie_zapisane(swiat):
    # `argv` i `stdout_tail` zostaja w pliku dowodu u runnera (sekrety, HAR-66)
    await odrzucone_bez_zapisu(swiat, dowod={**dowod_wejscia(), "argv": ["tajne"]})


@pytest.mark.parametrize("klucz", list(dowod_wejscia()))
async def test_dowod_bez_jednego_z_kluczy_silnika_jest_odrzucony(swiat, klucz):
    dowod = dowod_wejscia()
    del dowod[klucz]

    await odrzucone_bez_zapisu(swiat, dowod=dowod)


@pytest.mark.parametrize("dowod", ["engine", ["capability"], 5])
async def test_dowod_ktory_nie_jest_obiektem_jest_odrzucony(swiat, dowod):
    await odrzucone_bez_zapisu(swiat, dowod=dowod)


ZLE_SHA = {
    "o_jeden_za_krotki": "a" * 63,
    "wielkie_litery": "A" * 64,
    "znak_spoza_szesnastkowych": "g" * 64,
    "smiec_na_poczatku": "z" + "a" * 64,
    "smiec_na_koncu": "a" * 64 + "z",
    "znak_nowej_linii_na_koncu": "a" * 64 + "\n",
    "pusty": "",
}


@pytest.mark.parametrize("sha", list(ZLE_SHA.values()), ids=list(ZLE_SHA))
async def test_suma_sha256_o_zlym_ksztalcie_jest_odrzucona(swiat, sha):
    await odrzucone_bez_zapisu(swiat, dowod=dowod_wejscia(sha256=sha))


async def test_suma_sha256_z_cyframi_i_malymi_literami_szesnastkowymi_jest_przyjeta(swiat):
    sha = "0123456789abcdef" * 4

    await wyslij_ok(swiat, dowod=dowod_wejscia(sha256=sha))

    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane["dowod"]["sha256"] == sha


@pytest.mark.parametrize("zrodlo", ["człowiek", "Engine", "ENGINE", "", "system"])
async def test_zrodlo_dowodu_spoza_engine_i_agent_jest_odrzucone(swiat, zrodlo):
    await odrzucone_bez_zapisu(swiat, dowod=dowod_wejscia(source=zrodlo))


@pytest.mark.parametrize("zrodlo", ["engine", "agent"])
async def test_zrodlo_dowodu_engine_i_agent_jest_przyjete(swiat, zrodlo):
    await wyslij_ok(swiat, dowod=dowod_wejscia(source=zrodlo))

    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane["dowod"]["source"] == zrodlo


# pole dowodu -> (najwieksza dozwolona wartosc, wartosc o jeden za dluga)
LIMITY_DOWODU = {
    "capability": ("c" * 200, "c" * 201),
    "plik": ("p" * 255, "p" * 256),
    "started_at": ("t" * 64, "t" * 65),
    "finished_at": ("t" * 64, "t" * 65),
}


@pytest.mark.parametrize("pole", list(LIMITY_DOWODU))
async def test_pole_dowodu_o_najwiekszej_dozwolonej_dlugosci_jest_przyjete(swiat, pole):
    wartosc = LIMITY_DOWODU[pole][0]

    await wyslij_ok(swiat, dowod=dowod_wejscia(**{pole: wartosc}))

    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane["dowod"][pole] == wartosc


@pytest.mark.parametrize("pole", list(LIMITY_DOWODU))
async def test_pole_dowodu_o_jeden_znak_za_dlugie_jest_odrzucone(swiat, pole):
    await odrzucone_bez_zapisu(swiat, dowod=dowod_wejscia(**{pole: LIMITY_DOWODU[pole][1]}))


@pytest.mark.parametrize("pole", list(LIMITY_DOWODU))
async def test_pole_dowodu_z_pustym_napisem_jest_odrzucone(swiat, pole):
    await odrzucone_bez_zapisu(swiat, dowod=dowod_wejscia(**{pole: ""}))


async def test_werdykt_o_200_znakach_jest_przyjety(swiat):
    await wyslij_ok(swiat, verdict="w" * 200)

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"verdict": "w" * 200}]


@pytest.mark.parametrize("werdykt", ["", "w" * 201, 5, ["pass"]], ids=["pusty", "201_znakow", "liczba", "lista"])
async def test_werdykt_pusty_za_dlugi_albo_nie_napis_jest_odrzucony(swiat, werdykt):
    await odrzucone_bez_zapisu(swiat, verdict=werdykt)


async def test_tysiac_kotwic_jest_przyjetych(swiat):
    kotwice = [f"src/a.py:{i}" for i in range(1000)]

    await wyslij_ok(swiat, kotwice=kotwice)

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"kotwice": kotwice}]


async def test_tysiac_i_jedna_kotwica_jest_odrzucone(swiat):
    await odrzucone_bez_zapisu(swiat, kotwice=[f"src/a.py:{i}" for i in range(1001)])


@pytest.mark.parametrize("kotwice", ["src/a.py:11", {}, 5], ids=["napis", "slownik", "liczba"])
async def test_kotwice_ktore_nie_sa_lista_sa_odrzucone(swiat, kotwice):
    await odrzucone_bez_zapisu(swiat, kotwice=kotwice)


async def test_kotwica_o_1000_znakach_jest_przyjeta(swiat):
    await wyslij_ok(swiat, kotwice=["k" * 1000])

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"kotwice": ["k" * 1000]}]


@pytest.mark.parametrize("kotwica", ["", "k" * 1001], ids=["pusta", "1001_znakow"])
async def test_kotwica_pusta_albo_dluzsza_niz_1000_znakow_jest_odrzucona(swiat, kotwica):
    await odrzucone_bez_zapisu(swiat, kotwice=["src/a.py:1", kotwica])


async def test_sto_artefaktow_jest_przyjetych(swiat):
    artefakty = [f"a{i}.md" for i in range(100)]

    await wyslij_ok(swiat, artefakty=artefakty)

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"artefakty": artefakty}]


async def test_sto_jeden_artefaktow_jest_odrzucone(swiat):
    await odrzucone_bez_zapisu(swiat, artefakty=[f"a{i}.md" for i in range(101)])


@pytest.mark.parametrize("artefakty", ["plan.md", {}], ids=["napis", "slownik"])
async def test_artefakty_ktore_nie_sa_lista_sa_odrzucone(swiat, artefakty):
    await odrzucone_bez_zapisu(swiat, artefakty=artefakty)


async def test_nazwa_artefaktu_o_255_znakach_jest_przyjeta(swiat):
    await wyslij_ok(swiat, artefakty=["a" * 255])

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"artefakty": ["a" * 255]}]


@pytest.mark.parametrize("nazwa", ["", "a" * 256], ids=["pusta", "256_znakow"])
async def test_nazwa_artefaktu_pusta_albo_dluzsza_niz_255_znakow_jest_odrzucona(swiat, nazwa):
    await odrzucone_bez_zapisu(swiat, artefakty=["plan.md", nazwa])


@pytest.mark.parametrize("kind", ["step_started", "output", "run_finished"])
@pytest.mark.parametrize("pole", list(POLA_KONCA_KROKU))
async def test_pole_dowodowe_w_zdarzeniu_innym_niz_koniec_kroku_jest_odrzucone(swiat, kind, pole):
    await odrzucone_bez_zapisu(swiat, kind, **{pole: POLA_KONCA_KROKU[pole]})
    assert await status_runu(swiat, swiat.run_id) == RunStatus.running


# =================================================================================================
# B. GET /api/runs/{id}/dowody
# =================================================================================================

KLUCZE_ODPOWIEDZI = {
    "run_id", "twierdzenia", "dowody", "werdykty", "braki", "ryzyka", "pomiary", "decyzja"}


async def test_run_bez_zdarzen_i_pomiarow_zwraca_wszystkie_klucze_z_pustymi_wartosciami(swiat):
    run_id = await zasiej_run(swiat)

    tresc = await dowody_ok(swiat, run_id)

    assert set(tresc) == KLUCZE_ODPOWIEDZI
    assert tresc == {
        "run_id": run_id, "twierdzenia": [], "dowody": [], "werdykty": [], "braki": [],
        "ryzyka": {"kotwice": None, "mutanty": None}, "pomiary": [], "decyzja": None,
    }


@pytest.mark.parametrize("status", [RunStatus.running, RunStatus.failed, RunStatus.to_accept])
async def test_dowody_sa_dostepne_dla_runu_w_kazdym_stanie(swiat, status):
    run_id = await zasiej_run(swiat, status=status)

    r = await dowody(swiat, run_id)

    assert r.status_code == 200
    assert r.json()["run_id"] == run_id


async def test_dowody_runu_innego_zespolu_daja_404(swiat):
    run_obcego = await zasiej_run(swiat, tenant_id=swiat.obcy_tenant_id)
    await zasiej_zdarzenie(swiat, run_obcego, 1, "step_finished", "k", tenant_id=swiat.obcy_tenant_id,
                           dane={"artefakty": ["plan.md"]})

    r = await dowody(swiat, run_obcego)

    assert r.status_code == 404
    assert "plan.md" not in r.text


async def test_dowody_nieistniejacego_runu_daja_404(swiat):
    assert (await dowody(swiat, "nie-ma-takiego-runu")).status_code == 404


async def test_dowody_bez_sesji_daja_401(swiat):
    run_id = await zasiej_run(swiat)
    klient = await swiat.klient_bez_sesji()

    r = await dowody(swiat, run_id, klient)

    assert r.status_code == 401


async def test_obserwator_zespolu_moze_czytac_dowody(swiat):
    run_id = await zasiej_run(swiat)
    klient = await swiat.klient_roli(RolaWZespole.viewer, "obserwator@example.com")

    assert (await dowody(swiat, run_id, klient)).status_code == 200


async def test_twierdzenia_to_nazwy_artefaktow_w_kolejnosci_numerow_zdarzen(swiat):
    run_id = await zasiej_run(swiat)
    # wstawiane w innej kolejnosci niz numery zdarzen: tylko ORDER BY seq daje oczekiwany wynik
    await zasiej_zdarzenie(swiat, run_id, 5, "step_finished", "b", dane={"artefakty": ["x.md"]})
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "c", dane={"artefakty": ["c.md"]})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_finished", "a",
                           dane={"artefakty": ["plan.md", "tdd.md"]})
    await zasiej_zdarzenie(swiat, run_id, 3, "step_finished", "bez-artefaktow")

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["twierdzenia"] == [
        {"step_id": "c", "artefakt": "c.md"},
        {"step_id": "a", "artefakt": "plan.md"},
        {"step_id": "a", "artefakt": "tdd.md"},
        {"step_id": "b", "artefakt": "x.md"},
    ]


async def test_artefakty_nieudanego_kroku_nie_sa_twierdzeniami(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "a", text="padl",
                           dane={"artefakty": ["x.md"]})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["twierdzenia"] == []


async def test_krok_powtorzony_i_przeplatany_innym_ma_pozycje_wedlug_ostatniego_zdarzenia(swiat):
    run_id = await zasiej_run(swiat)
    # kolejnosc pierwszego wystapienia (a, b) rozni sie od kolejnosci ostatnich zdarzen (b, a)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "a", text="pierwszy",
                           dane={"verdict": "fail"})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_failed", "b", text="drugi",
                           dane={"verdict": "fail"})
    await zasiej_zdarzenie(swiat, run_id, 3, "step_failed", "a", text="trzeci",
                           dane={"verdict": "fail"})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["werdykty"] == [
        {"step_id": "b", "verdict": "fail", "zaliczony": False},
        {"step_id": "a", "verdict": "fail", "zaliczony": False},
    ]
    assert tresc["braki"] == [
        brak("werdykt_fail", "b", "werdykt fail: drugi"),
        brak("werdykt_fail", "a", "werdykt fail: trzeci"),
    ]


async def test_dowody_i_werdykty_pochodza_ze_zdarzen_konca_kroku_w_kolejnosci_numerow(swiat):
    run_id = await zasiej_run(swiat)
    agent = dowod_wejscia(capability="przeglad", source="agent", exit_code=None, sha256=SHA_B,
                          plik="dowod-przeglad.json")
    await zasiej_zdarzenie(swiat, run_id, 3, "step_finished", "przeglad", dane={"dowod": agent})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_finished", "ocena", dane={"verdict": "pass"})
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "typecheck",
                           dane={"dowod": dowod_wejscia()})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["dowody"] == [
        dowod_w_odpowiedzi("typecheck", dowod_wejscia()),
        dowod_w_odpowiedzi("przeglad", agent),
    ]
    assert tresc["werdykty"] == [{"step_id": "ocena", "verdict": "pass", "zaliczony": True}]
    assert tresc["braki"] == []


async def test_dowod_agenta_jest_odroznialny_od_dowodu_silnika_polem_source(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "silnik",
                           dane={"dowod": dowod_wejscia(source="engine")})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_finished", "agent",
                           dane={"dowod": dowod_wejscia(source="agent")})

    tresc = await dowody_ok(swiat, run_id)

    assert [(d["step_id"], d["source"]) for d in tresc["dowody"]] == [
        ("silnik", "engine"), ("agent", "agent")]


async def test_krok_nieudany_a_potem_udany_ma_dowod_i_werdykt_z_ostatniego_zdarzenia_i_nie_jest_brakiem(
    swiat,
):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "k", text="kod wyjścia 1",
                           dane={"dowod": dowod_wejscia(exit_code=1), "verdict": "fail"})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_finished", "k",
                           dane={"dowod": dowod_wejscia(exit_code=0, sha256=SHA_B), "verdict": "pass"})
    await zasiej_zdarzenie(swiat, run_id, 3, "output", "k", text="dopisek po zakonczeniu")

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["dowody"] == [dowod_w_odpowiedzi("k", dowod_wejscia(exit_code=0, sha256=SHA_B))]
    assert tresc["werdykty"] == [{"step_id": "k", "verdict": "pass", "zaliczony": True}]
    assert tresc["braki"] == []


async def test_krok_udany_a_potem_nieudany_jest_brakiem_bo_liczy_sie_ostatnie_zdarzenie(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "k",
                           dane={"dowod": dowod_wejscia(), "verdict": "pass"})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_failed", "k", text="recenzja odrzuciła",
                           dane={"verdict": "fail"})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["dowody"] == []
    assert tresc["werdykty"] == [{"step_id": "k", "verdict": "fail", "zaliczony": False}]
    assert tresc["braki"] == [brak("werdykt_fail", "k", "werdykt fail: recenzja odrzuciła")]


async def test_werdykt_fail_to_czerwony_wiersz_z_werdyktem_i_tekstem_zdarzenia(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "przeglad",
                           text="2 uwagi blokujące", dane={"verdict": "fail"})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [brak("werdykt_fail", "przeglad", "werdykt fail: 2 uwagi blokujące")]
    assert tresc["werdykty"] == [{"step_id": "przeglad", "verdict": "fail", "zaliczony": False}]


async def test_powod_werdyktu_fail_niesie_wartosc_werdyktu_z_zdarzenia(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "przeglad",
                           text="do poprawy", dane={"verdict": "odrzucone"})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [brak("werdykt_fail", "przeglad", "werdykt odrzucone: do poprawy")]


async def test_nieudany_check_z_dowodem_to_brak_dowodu_z_nazwa_capability_i_tekstem_zdarzenia(swiat):
    run_id = await zasiej_run(swiat)
    dowod = dowod_wejscia(capability="lint", exit_code=3, plik="dowod-lint.json")
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "sprawdz-lint",
                           text="check lint: kod wyjścia 3", dane={"dowod": dowod})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [
        brak("brak_dowodu", "sprawdz-lint", "dowód lint: check lint: kod wyjścia 3")]
    assert tresc["dowody"] == [dowod_w_odpowiedzi("sprawdz-lint", dowod)]
    assert tresc["werdykty"] == []


@pytest.mark.parametrize("dane", [None, {"kotwice": []}], ids=["bez_danych", "dane_bez_dowodu_i_werdyktu"])
async def test_nieudany_krok_bez_dowodu_i_bez_werdyktu_to_brak_dowodu_z_samym_tekstem_zdarzenia(
    swiat, dane,
):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "autor", text="kod wyjścia 1", dane=dane)

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [brak("brak_dowodu", "autor", "kod wyjścia 1")]


async def test_krok_ukonczony_z_dowodem_i_werdyktem_pass_nie_jest_brakiem(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "k",
                           dane={"dowod": dowod_wejscia(), "verdict": "pass"})

    assert (await dowody_ok(swiat, run_id))["braki"] == []


async def test_kazda_pominieta_bramka_to_czerwony_wiersz_z_krokiem_i_powodem(swiat):
    run_id = await zasiej_run(
        swiat, bramki_powody={"lint": "brak_capability", "test": "brak_wejscia"},
        stan_pokrycia="czesciowo")

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [
        brak("pominieta_bramka", "lint", "bramka pominięta: brak_capability"),
        brak("pominieta_bramka", "test", "bramka pominięta: brak_wejscia"),
    ]


async def test_run_w_ktorym_zadna_bramka_nic_nie_sprawdzila_ma_brak_niesprawdzony_bez_kroku(swiat):
    run_id = await zasiej_run(swiat, stan_pokrycia="niesprawdzony")

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [brak("niesprawdzony", None, PUSTY_KONTEKST)]


@pytest.mark.parametrize("stan", ["sprawdzony", "czesciowo", None])
async def test_run_sprawdzony_czesciowo_albo_bez_pomiaru_pokrycia_nie_ma_braku_niesprawdzony(
    swiat, stan,
):
    run_id = await zasiej_run(swiat, stan_pokrycia=stan)

    assert (await dowody_ok(swiat, run_id))["braki"] == []


async def test_koszt_niezmierzony_to_jeden_zbiorczy_brak_z_lista_krokow_w_kolejnosci_krokow(swiat):
    run_id = await zasiej_run(swiat)
    # wstawiane nie alfabetycznie; zero i dodatni koszt sa zmierzone, tylko None jest brakiem pomiaru
    await zasiej_krok(swiat, run_id, "b-krok", cost_usd=None)
    await zasiej_krok(swiat, run_id, "a-krok", cost_usd=None)
    await zasiej_krok(swiat, run_id, "c-krok", cost_usd=0.0)
    await zasiej_krok(swiat, run_id, "d-krok", cost_usd=0.25)

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [brak("niezmierzone", None, "koszt niezmierzony: a-krok, b-krok")]


async def test_run_w_ktorym_kazdy_krok_ma_zmierzony_koszt_nie_ma_braku_niezmierzone(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_krok(swiat, run_id, "a-krok", cost_usd=0.0)
    await zasiej_krok(swiat, run_id, "b-krok", cost_usd=0.25)

    assert (await dowody_ok(swiat, run_id))["braki"] == []


async def test_pomiary_zachowuja_brak_pomiaru_jako_null_a_zmierzone_zero_jako_zero(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_krok(swiat, run_id, "b-krok", cost_usd=None)
    await zasiej_krok(swiat, run_id, "a-krok", cost_usd=None)
    await zasiej_krok(swiat, run_id, "c-krok", cost_usd=0.0, tokens_in=0, tokens_out=0)
    await zasiej_krok(swiat, run_id, "d-krok", cost_usd=0.25, tokens_in=10, tokens_out=20)

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["pomiary"] == [
        {"step_id": "a-krok", "cost_usd": None, "tokens_in": None, "tokens_out": None},
        {"step_id": "b-krok", "cost_usd": None, "tokens_in": None, "tokens_out": None},
        {"step_id": "c-krok", "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0},
        {"step_id": "d-krok", "cost_usd": 0.25, "tokens_in": 10, "tokens_out": 20},
    ]


async def test_braki_sa_podane_w_ustalonej_kolejnosci_rodzajow(swiat):
    run_id = await zasiej_run(swiat, bramki_powody={"lint": "brak_capability"},
                              stan_pokrycia="niesprawdzony")
    # brak dowodu ma NIZSZY numer zdarzenia niz werdykt, a mimo to idzie po nim
    await zasiej_zdarzenie(swiat, run_id, 1, "step_failed", "z-dowod", text="kod wyjścia 1")
    await zasiej_zdarzenie(swiat, run_id, 2, "step_failed", "m-werdykt", text="uwagi",
                           dane={"verdict": "fail"})
    await zasiej_krok(swiat, run_id, "q-krok", cost_usd=None)

    tresc = await dowody_ok(swiat, run_id)

    assert [(b["rodzaj"], b["step_id"]) for b in tresc["braki"]] == [
        ("pominieta_bramka", "lint"),
        ("niesprawdzony", None),
        ("werdykt_fail", "m-werdykt"),
        ("brak_dowodu", "z-dowod"),
        ("niezmierzone", None),
    ]


@pytest.mark.parametrize(("dane_zdarzen", "oczekiwane"), [
    pytest.param([], None, id="brak_zdarzen_to_brak_danych"),
    pytest.param([(1, "step_finished", None)], None, id="zdarzenie_bez_pola_kotwice_to_brak_danych"),
    pytest.param([(1, "step_finished", {"kotwice": []})], [], id="pusta_lista_to_brak_kotwic"),
    pytest.param(
        [(3, "step_finished", {"kotwice": ["a.py:1", "b.py:2"]}),
         (1, "step_finished", {"kotwice": ["c.py:3"]})],
        ["c.py:3", "a.py:1", "b.py:2"], id="zlaczone_w_kolejnosci_numerow_zdarzen"),
    pytest.param(
        [(1, "step_finished", {"kotwice": []}), (2, "step_finished", {"verdict": "pass"}),
         (3, "step_finished", {"kotwice": ["a.py:1"]})],
        ["a.py:1"], id="pusta_lista_obok_listy_i_zdarzenia_bez_pola"),
    pytest.param(
        [(1, "step_failed", {"kotwice": ["z.py:9"]})], None,
        id="kotwice_z_nieudanego_kroku_nie_licza_sie"),
])
async def test_ryzyko_kotwic_odroznia_brak_danych_od_pustej_listy_i_od_listy(
    swiat, dane_zdarzen, oczekiwane,
):
    run_id = await zasiej_run(swiat)
    for seq, kind, dane in dane_zdarzen:
        await zasiej_zdarzenie(swiat, run_id, seq, kind, f"k{seq}", dane=dane)

    ryzyka = (await dowody_ok(swiat, run_id))["ryzyka"]

    assert ryzyka["kotwice"] == oczekiwane
    assert ryzyka["mutanty"] is None


async def test_mutanty_sa_zawsze_brakiem_danych_nawet_gdy_run_ma_kotwice_i_dowody(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "k",
                           dane={"dowod": dowod_wejscia(), "kotwice": ["a.py:1"]})

    ryzyka = (await dowody_ok(swiat, run_id))["ryzyka"]

    assert ryzyka == {"kotwice": ["a.py:1"], "mutanty": None}


# --- izolacja: zespol i run -----------------------------------------------------------------------

OBCE_DANE = {
    "dowod": dowod_wejscia(capability="obcy"), "verdict": "pass",
    "artefakty": ["obcy.md"], "kotwice": ["obce.py:1"],
}


async def test_zdarzenia_koncowe_innego_zespolu_o_tym_samym_run_id_nie_trafiaja_do_odpowiedzi(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "wspolny", dane={"verdict": "pass"})
    # obcy wiersz: ten sam run_id, inny numer (uq_event_run_seq nie obejmuje zespolu)
    await zasiej_zdarzenie(swiat, run_id, 7, "step_finished", "obcy-krok",
                           tenant_id=swiat.obcy_tenant_id, dane=OBCE_DANE)
    # obcy nieudany koniec TEGO SAMEGO kroku z wyzszym numerem zmienilby stan koncowy kroku
    await zasiej_zdarzenie(swiat, run_id, 8, "step_failed", "wspolny", text="obce uwagi",
                           tenant_id=swiat.obcy_tenant_id, dane={"verdict": "fail"})

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["twierdzenia"] == []
    assert tresc["dowody"] == []
    assert tresc["werdykty"] == [{"step_id": "wspolny", "verdict": "pass", "zaliczony": True}]
    assert tresc["braki"] == []


async def test_kotwice_innego_zespolu_o_tym_samym_run_id_nie_trafiaja_do_ryzyka(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 7, "step_finished", "obcy-krok",
                           tenant_id=swiat.obcy_tenant_id, dane={"kotwice": ["obce.py:1"]})

    assert (await dowody_ok(swiat, run_id))["ryzyka"]["kotwice"] is None


async def test_kroki_innego_zespolu_o_tym_samym_run_id_nie_trafiaja_do_pomiarow_ani_do_brakow(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_krok(swiat, run_id, "obcy-krok", cost_usd=None, tenant_id=swiat.obcy_tenant_id)
    await zasiej_krok(swiat, run_id, "wlasny-krok", cost_usd=0.5)

    tresc = await dowody_ok(swiat, run_id)

    assert [p["step_id"] for p in tresc["pomiary"]] == ["wlasny-krok"]
    assert tresc["braki"] == []


async def test_zdarzenia_i_kroki_innego_runu_tego_samego_zespolu_nie_trafiaja_do_odpowiedzi(swiat):
    run_id = await zasiej_run(swiat)
    inny = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "wspolny", dane={"verdict": "pass"})
    await zasiej_zdarzenie(swiat, inny, 2, "step_finished", "inny-krok", dane=OBCE_DANE)
    await zasiej_zdarzenie(swiat, inny, 3, "step_failed", "wspolny", text="uwagi innego runu",
                           dane={"verdict": "fail"})
    await zasiej_krok(swiat, inny, "inny-krok", cost_usd=None)

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["twierdzenia"] == []
    assert tresc["dowody"] == []
    assert tresc["werdykty"] == [{"step_id": "wspolny", "verdict": "pass", "zaliczony": True}]
    assert tresc["braki"] == []
    assert tresc["pomiary"] == []
    assert tresc["ryzyka"]["kotwice"] is None


# =================================================================================================
# C. POST /api/runs/{id}/decyzja-odbioru
# =================================================================================================

async def run_z_brakami(swiat: Swiat) -> str:
    """Run do odbioru z jednym brakiem: pominieta bramka."""
    return await zasiej_run(swiat, bramki_powody={"lint": "brak_capability"},
                            stan_pokrycia="czesciowo")


async def run_bez_brakow(swiat: Swiat) -> str:
    return await zasiej_run(swiat, stan_pokrycia="sprawdzony")


async def decyduj(swiat: Swiat, run_id: str, klient: AsyncClient | None = None, **cialo):
    return await (klient or swiat.klient).post(
        f"/api/runs/{run_id}/decyzja-odbioru", json=cialo)


async def test_przyjecie_runu_z_brakami_bez_uzasadnienia_jest_odrzucone_a_zdarzenia_nie_ma_w_bazie(
    swiat,
):
    run_id = await run_z_brakami(swiat)

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 422
    assert await zdarzenia_odbioru(swiat, run_id) == []
    assert await status_runu(swiat, run_id) == RunStatus.to_accept


@pytest.mark.parametrize("uzasadnienie", [None, "", "   ", "\n\t "], ids=["null", "pusty", "spacje", "bialy_znak"])
async def test_przyjecie_z_brakami_i_uzasadnieniem_pustym_po_obcieciu_bialych_znakow_jest_odrzucone(
    swiat, uzasadnienie,
):
    run_id = await run_z_brakami(swiat)

    r = await decyduj(swiat, run_id, decyzja="accept", uzasadnienie=uzasadnienie)

    assert r.status_code == 422
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_przyjecie_z_brakami_i_uzasadnieniem_zapisuje_w_audycie_osobe_czas_i_tresc(swiat):
    run_id = await run_z_brakami(swiat)
    przed = datetime.now(timezone.utc)

    r = await decyduj(swiat, run_id, decyzja="accept", uzasadnienie=f"  {UZASADNIENIE}\n")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert zapisane.text == json.dumps({
        "user_id": swiat.wlasciciel.id, "email": swiat.wlasciciel.email,
        "decyzja": "accept", "uzasadnienie": UZASADNIENIE}, ensure_ascii=False)
    assert zapisane.tenant_id == swiat.tenant_id
    assert zapisane.step_id == ""
    assert utc(zapisane.at.isoformat()) >= przed.replace(microsecond=0)
    tresc = r.json()
    assert set(tresc) == {"decyzja", "user_id", "email", "uzasadnienie", "at"}
    assert (tresc["decyzja"], tresc["user_id"], tresc["email"], tresc["uzasadnienie"]) == (
        "accept", swiat.wlasciciel.id, swiat.wlasciciel.email, UZASADNIENIE)
    assert utc(tresc["at"]) == utc(zapisane.at.isoformat())


async def test_decyzja_odbioru_nie_zmienia_statusu_runu(swiat):
    run_id = await run_z_brakami(swiat)

    await decyduj(swiat, run_id, decyzja="accept", uzasadnienie=UZASADNIENIE)

    assert await status_runu(swiat, run_id) == RunStatus.to_accept


async def test_numer_zdarzenia_decyzji_to_kolejny_numer_w_runie_swojego_zespolu(swiat):
    run_id = await run_bez_brakow(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "output", "k")
    await zasiej_zdarzenie(swiat, run_id, 4, "step_finished", "k")
    # numer innego zespolu w tym samym run_id nie przesuwa numeracji
    await zasiej_zdarzenie(swiat, run_id, 9, "output", "k", tenant_id=swiat.obcy_tenant_id)

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert zapisane.seq == 5


async def test_przyjecie_runu_bez_brakow_nie_wymaga_uzasadnienia_i_zapisuje_je_jako_brak(swiat):
    run_id = await run_bez_brakow(swiat)

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert zapisane.text == json.dumps({
        "user_id": swiat.wlasciciel.id, "email": swiat.wlasciciel.email,
        "decyzja": "accept", "uzasadnienie": None}, ensure_ascii=False)
    assert r.json()["uzasadnienie"] is None


async def test_przyjecie_runu_bez_brakow_z_samymi_spacjami_zapisuje_uzasadnienie_jako_brak(swiat):
    run_id = await run_bez_brakow(swiat)

    r = await decyduj(swiat, run_id, decyzja="accept", uzasadnienie="   ")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert json.loads(zapisane.text)["uzasadnienie"] is None


async def test_odeslanie_runu_z_brakami_nie_wymaga_uzasadnienia(swiat):
    run_id = await run_z_brakami(swiat)

    r = await decyduj(swiat, run_id, decyzja="send_back")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert json.loads(zapisane.text) == {
        "user_id": swiat.wlasciciel.id, "email": swiat.wlasciciel.email,
        "decyzja": "send_back", "uzasadnienie": None}
    assert r.json()["decyzja"] == "send_back"


async def test_odeslanie_runu_zapisuje_uzasadnienie_po_obcieciu_bialych_znakow(swiat):
    run_id = await run_z_brakami(swiat)

    await decyduj(swiat, run_id, decyzja="send_back", uzasadnienie="  do poprawy \n")

    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert json.loads(zapisane.text)["uzasadnienie"] == "do poprawy"


@pytest.mark.parametrize(("pierwsza", "druga"), [
    ("accept", "accept"), ("send_back", "accept"), ("accept", "send_back"), ("send_back", "send_back"),
])
async def test_druga_decyzja_o_tym_samym_runie_daje_409_a_w_bazie_zostaje_jedna(swiat, pierwsza, druga):
    run_id = await run_bez_brakow(swiat)
    assert (await decyduj(swiat, run_id, decyzja=pierwsza)).status_code == 200

    r = await decyduj(swiat, run_id, decyzja=druga, uzasadnienie="druga próba")

    assert r.status_code == 409
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert json.loads(zapisane.text)["decyzja"] == pierwsza


@pytest.mark.parametrize("status", [
    RunStatus.running, RunStatus.waiting, RunStatus.failed, RunStatus.aborted, RunStatus.rejected,
])
async def test_decyzja_o_runie_ktory_nie_jest_do_odbioru_daje_409_bez_zapisu(swiat, status):
    run_id = await zasiej_run(swiat, status=status, stan_pokrycia="sprawdzony")

    r = await decyduj(swiat, run_id, decyzja="send_back", uzasadnienie=UZASADNIENIE)

    assert r.status_code == 409
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_decyzja_o_runie_innego_zespolu_daje_404_bez_zapisu(swiat):
    run_obcego = await zasiej_run(swiat, tenant_id=swiat.obcy_tenant_id, stan_pokrycia="sprawdzony")

    r = await decyduj(swiat, run_obcego, decyzja="accept")

    assert r.status_code == 404
    assert await zdarzenia_odbioru(swiat, run_obcego) == []


async def test_decyzja_o_nieistniejacym_runie_daje_404(swiat):
    assert (await decyduj(swiat, "nie-ma-takiego-runu", decyzja="accept")).status_code == 404


async def test_obserwator_zespolu_nie_podejmuje_decyzji_odbioru_403_bez_zapisu(swiat):
    run_id = await run_bez_brakow(swiat)
    klient = await swiat.klient_roli(RolaWZespole.viewer, "obserwator@example.com")

    r = await decyduj(swiat, run_id, klient, decyzja="accept")

    assert r.status_code == 403
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_czlonek_zespolu_podejmuje_decyzje_odbioru_pod_swoim_adresem(swiat):
    run_id = await run_bez_brakow(swiat)
    klient = await swiat.klient_roli(RolaWZespole.member, "czlonek@example.com")

    r = await decyduj(swiat, run_id, klient, decyzja="accept")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert json.loads(zapisane.text)["email"] == "czlonek@example.com"


async def test_decyzja_bez_sesji_daje_401_bez_zapisu(swiat):
    run_id = await run_bez_brakow(swiat)
    klient = await swiat.klient_bez_sesji()

    r = await decyduj(swiat, run_id, klient, decyzja="accept")

    assert r.status_code == 401
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_uzasadnienie_o_2000_znakach_jest_przyjete(swiat):
    run_id = await run_bez_brakow(swiat)

    r = await decyduj(swiat, run_id, decyzja="send_back", uzasadnienie="u" * 2000)

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert json.loads(zapisane.text)["uzasadnienie"] == "u" * 2000


async def test_uzasadnienie_o_2001_znakach_jest_odrzucone_bez_zapisu(swiat):
    run_id = await run_bez_brakow(swiat)

    r = await decyduj(swiat, run_id, decyzja="send_back", uzasadnienie="u" * 2001)

    assert r.status_code == 422
    assert await zdarzenia_odbioru(swiat, run_id) == []


@pytest.mark.parametrize("cialo", [
    {"decyzja": "accept", "uzasadnienie": "x", "user_id": "ktos-inny"},
    {"decyzja": "accept", "email": "podszywka@example.com"},
    {"decyzja": "reject"},
    {"decyzja": ""},
    {"decyzja": "Accept"},
    {"uzasadnienie": "x"},
    {},
], ids=["obce_user_id", "obcy_email", "nieznana_decyzja", "pusta_decyzja", "wielka_litera", "bez_decyzji", "puste_cialo"])
async def test_cialo_decyzji_z_nieznanym_polem_albo_zla_decyzja_jest_odrzucone_bez_zapisu(swiat, cialo):
    run_id = await run_bez_brakow(swiat)

    r = await decyduj(swiat, run_id, **cialo)

    assert r.status_code == 422
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_dowody_po_decyzji_zwracaja_decyzje_z_adresem_czasem_i_uzasadnieniem(swiat):
    run_id = await run_z_brakami(swiat)
    odpowiedz = (await decyduj(swiat, run_id, decyzja="accept", uzasadnienie=UZASADNIENIE)).json()

    decyzja = (await dowody_ok(swiat, run_id))["decyzja"]

    assert set(decyzja) == {"decyzja", "user_id", "email", "uzasadnienie", "at"}
    assert (decyzja["decyzja"], decyzja["user_id"], decyzja["email"], decyzja["uzasadnienie"]) == (
        "accept", swiat.wlasciciel.id, swiat.wlasciciel.email, UZASADNIENIE)
    assert utc(decyzja["at"]) == utc(odpowiedz["at"])


async def test_dowody_po_odeslaniu_zwracaja_decyzje_send_back(swiat):
    run_id = await run_z_brakami(swiat)
    await decyduj(swiat, run_id, decyzja="send_back")

    decyzja = (await dowody_ok(swiat, run_id))["decyzja"]

    assert (decyzja["decyzja"], decyzja["uzasadnienie"]) == ("send_back", None)


async def test_decyzja_innego_zespolu_o_tym_samym_run_id_nie_jest_decyzja_tego_runu(swiat):
    run_id = await run_bez_brakow(swiat)
    obca = json.dumps({"user_id": "u-obcy", "email": "obcy@piekarnia.example",
                       "decyzja": "accept", "uzasadnienie": "obce"})
    await zasiej_zdarzenie(swiat, run_id, 50, "acceptance", "", tenant_id=swiat.obcy_tenant_id,
                           text=obca)

    assert (await dowody_ok(swiat, run_id))["decyzja"] is None


async def test_decyzja_innego_zespolu_o_tym_samym_run_id_nie_blokuje_wlasnej_decyzji(swiat):
    run_id = await run_bez_brakow(swiat)
    await zasiej_zdarzenie(swiat, run_id, 50, "acceptance", "", tenant_id=swiat.obcy_tenant_id,
                           text="{}")

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 200, r.text


async def test_decyzja_o_innym_runie_tego_samego_zespolu_nie_blokuje_decyzji_o_tym_runie(swiat):
    run_id = await run_bez_brakow(swiat)
    inny = await run_bez_brakow(swiat)
    await zasiej_zdarzenie(swiat, inny, 1, "acceptance", "", text="{}")

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 200, r.text
    assert (await dowody_ok(swiat, run_id))["decyzja"]["decyzja"] == "accept"


async def test_decyzja_innego_runu_tego_samego_zespolu_nie_jest_decyzja_tego_runu(swiat):
    run_id = await run_bez_brakow(swiat)
    inny = await run_bez_brakow(swiat)
    await zasiej_zdarzenie(swiat, inny, 1, "acceptance", "", text=json.dumps({
        "user_id": "u", "email": "e@example.com", "decyzja": "accept", "uzasadnienie": None}))

    assert (await dowody_ok(swiat, run_id))["decyzja"] is None


async def test_runner_nie_moze_zapisac_zdarzenia_acceptance_przez_wejscie_zdarzen(swiat):
    przed = await liczba_zdarzen(swiat)

    r = await wyslij(swiat, "acceptance", text=json.dumps({"decyzja": "accept"}))

    assert r.status_code == 422
    assert await liczba_zdarzen(swiat) == przed
    assert await zdarzenia_odbioru(swiat, swiat.run_id) == []


async def test_decyzja_odbioru_trafia_na_szyne_zdarzen_runu_jako_acceptance(swiat, monkeypatch):
    run_id = await run_bez_brakow(swiat)
    opublikowane: list[tuple[str, dict]] = []

    async def zapisz(r_id, zdarzenie):
        opublikowane.append((r_id, zdarzenie))

    monkeypatch.setattr(bus, "publish_event", zapisz)

    r = await decyduj(swiat, run_id, decyzja="accept", uzasadnienie=UZASADNIENIE)

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert [(rid, z["kind"], z["seq"], z["text"]) for rid, z in opublikowane] == [
        (run_id, "acceptance", zapisane.seq, zapisane.text)]


async def test_odrzucona_decyzja_nic_nie_trafia_na_szyne_zdarzen(swiat, monkeypatch):
    run_id = await run_z_brakami(swiat)
    opublikowane: list = []

    async def zapisz(r_id, zdarzenie):
        opublikowane.append((r_id, zdarzenie))

    monkeypatch.setattr(bus, "publish_event", zapisz)

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 422
    assert opublikowane == []


# --- wyscig sprawdz-zapisz ---------------------------------------------------------------------

def _skompilowane_dla_postgresa(clauseelement) -> tuple[str, list]:
    skompilowane = clauseelement.compile(dialect=postgresql.dialect())
    return str(skompilowane), list(skompilowane.params.values())


def _blokada_wiersza_runu(q: str) -> bool:
    return bool(re.search(r"FROM run\b", q)) and q.rstrip().endswith("FOR UPDATE")


async def test_decyzja_blokuje_wiersz_runu_przez_for_update_przed_sprawdzeniem_i_zapisem_decyzji(swiat):
    # SQLite ignoruje FOR UPDATE, wiec blokade widac dopiero w zapytaniu skompilowanym dla PostgreSQL
    run_id = await run_bez_brakow(swiat)
    dziennik: list[tuple[str, str, list]] = []

    def zbierz(conn, clauseelement, multiparams, params, execution_options):
        if isinstance(clauseelement, Select):
            q, parametry = _skompilowane_dla_postgresa(clauseelement)
            dziennik.append(("select", q, parametry))
        elif isinstance(clauseelement, Insert) and clauseelement.table.name == "event":
            dziennik.append(("insert", "", []))

    event.listen(swiat.engine.sync_engine, "before_execute", zbierz)
    try:
        r = await decyduj(swiat, run_id, decyzja="accept")
    finally:
        event.remove(swiat.engine.sync_engine, "before_execute", zbierz)

    assert r.status_code == 200, r.text
    blokady = [i for i, (rodzaj, q, _) in enumerate(dziennik)
               if rodzaj == "select" and _blokada_wiersza_runu(q)]
    assert blokady, f"brak FOR UPDATE na run: {[q for _, q, _ in dziennik]}"
    zapisy = [i for i, (rodzaj, _, _) in enumerate(dziennik) if rodzaj == "insert"]
    assert zapisy, "brak zapisu zdarzenia decyzji"
    assert blokady[0] < zapisy[0]
    # sprawdzenie istniejacej decyzji zapytaniem o `acceptance` idzie dopiero pod blokada
    sprawdzenia = [i for i, (rodzaj, q, parametry) in enumerate(dziennik)
                   if rodzaj == "select" and ("acceptance" in parametry or "'acceptance'" in q)]
    for i in sprawdzenia:
        assert blokady[0] < i


class SesjaZBramka(AsyncSession):
    """Po pierwszym zapytaniu o zdarzenie `acceptance` zglasza to i czeka, az test zwolni sesje."""

    po_sprawdzeniu: asyncio.Event
    zwolnij: asyncio.Event
    zatrzymana = False

    async def execute(self, statement, *args, **kwargs):
        wynik = await super().execute(statement, *args, **kwargs)
        if not self.zatrzymana and isinstance(statement, Select):
            parametry = list(statement.compile().params.values())
            if "acceptance" in parametry:
                self.zatrzymana = True
                self.po_sprawdzeniu.set()
                await asyncio.wait_for(self.zwolnij.wait(), LIMIT_S)
        return wynik


async def _czeka_na_blokade_wiersza(maker) -> bool:
    async with maker() as s:
        return bool((await s.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND datname = current_database()"))).scalar_one())


@pytest.mark.baza
@tylko_postgres
async def test_dwie_przeplecione_decyzje_o_tym_samym_runie_zostawiaja_dokladnie_jedna_decyzje(
    swiat_pg, monkeypatch,
):
    swiat = swiat_pg
    run_id = await run_bez_brakow(swiat)
    sesje: list[SesjaZBramka] = []

    def fabryka() -> AsyncSession:
        if not sesje:        # pierwsze zadanie (A) dostaje sesje, ktora staje po sprawdzeniu
            a = SesjaZBramka(bind=swiat.engine, expire_on_commit=False)
            a.po_sprawdzeniu, a.zwolnij = asyncio.Event(), asyncio.Event()
            sesje.append(a)
            return a
        return AsyncSession(bind=swiat.engine, expire_on_commit=False)

    monkeypatch.setattr(db_session, "SessionLocal", fabryka)
    zadanie_a = asyncio.create_task(decyduj(swiat, run_id, decyzja="accept"))
    zadanie_b = None
    try:
        # A sprawdzil, ze decyzji nie ma, i stoi przed zapisem
        await asyncio.wait_for(_poczekaj_na_sesje(sesje), LIMIT_S)
        zadanie_b = asyncio.create_task(decyduj(swiat, run_id, decyzja="send_back"))
        # B albo konczy sie (brak blokady), albo staje na blokadzie wiersza trzymanej przez A
        koniec = asyncio.get_running_loop().time() + LIMIT_S
        while not zadanie_b.done() and not await _czeka_na_blokade_wiersza(swiat.maker):
            assert asyncio.get_running_loop().time() < koniec, "B ani nie skonczyl, ani nie czeka na blokade"
            await asyncio.sleep(0.05)
    finally:
        for a in sesje:
            a.zwolnij.set()
    wyniki = await asyncio.wait_for(asyncio.gather(zadanie_a, zadanie_b, return_exceptions=True), LIMIT_S)

    kody = [w.status_code if hasattr(w, "status_code") else repr(w) for w in wyniki]
    assert sorted(kody, key=str) == [200, 409]
    zapisane = await zdarzenia_odbioru(swiat, run_id)
    assert [json.loads(z.text)["decyzja"] for z in zapisane] == ["accept"]


async def _poczekaj_na_sesje(sesje: list) -> None:
    """Czeka (z limitem z `wait_for` wolajacego), az trasa A otworzy pierwsza sesje, potem na jej brame."""
    while not sesje:
        await asyncio.sleep(0.01)
    await czekaj(sesje[0].po_sprawdzeniu.wait(), LIMIT_S, "brama pierwszej sesji")


# --- runda 3: komunikaty odmowy na pelna tresc, kolejnosc i autor decyzji, dlugosc tekstu ---------

async def test_decyzja_o_runie_ktory_nie_jest_do_odbioru_wyjasnia_odmowe_pelnym_komunikatem(swiat):
    run_id = await zasiej_run(swiat, status=RunStatus.running, stan_pokrycia="sprawdzony")

    r = await decyduj(swiat, run_id, decyzja="send_back")

    assert r.status_code == 409
    assert r.json() == {"detail": "run nie jest do odbioru"}
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_druga_decyzja_o_runie_wyjasnia_odmowe_pelnym_komunikatem(swiat):
    run_id = await run_bez_brakow(swiat)
    assert (await decyduj(swiat, run_id, decyzja="accept")).status_code == 200

    r = await decyduj(swiat, run_id, decyzja="send_back")

    assert r.status_code == 409
    assert r.json() == {"detail": "decyzja odbioru zostala juz podjeta"}
    assert len(await zdarzenia_odbioru(swiat, run_id)) == 1


async def test_przyjecie_runu_z_brakami_bez_uzasadnienia_wyjasnia_odmowe_pelnym_komunikatem(swiat):
    run_id = await run_z_brakami(swiat)

    r = await decyduj(swiat, run_id, decyzja="accept")

    assert r.status_code == 422
    assert r.json() == {"detail": "przyjecie runu z brakami wymaga uzasadnienia"}
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_pole_dowodowe_w_zdarzeniu_output_wyjasnia_odmowe_pelnym_komunikatem(swiat):
    przed = await liczba_zdarzen(swiat)

    r = await wyslij(swiat, "output", verdict="pass")

    assert r.status_code == 422
    assert r.json() == {
        "detail": "dowod, verdict, kotwice i artefakty wolno przeslac tylko w zdarzeniu konca kroku"}
    assert await liczba_zdarzen(swiat) == przed


async def test_uzytkownik_z_innego_zespolu_nie_jest_autorem_decyzji_odbioru_runu(swiat):
    run_id = await run_bez_brakow(swiat)
    obcy = await zaloz_czlonka(
        swiat.maker, swiat.obcy_tenant_id, RolaWZespole.member, "obcy@piekarnia.example")

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await odbior_dowody.podejmij_decyzje_odbioru(
                s, run_id, swiat.tenant_id, obcy.id, "send_back", None)

    assert str(wyjatek.value) == "uzytkownik nie nalezy do zespolu runu"
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_nieistniejacy_uzytkownik_nie_jest_autorem_decyzji_odbioru_runu(swiat):
    run_id = await run_bez_brakow(swiat)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await odbior_dowody.podejmij_decyzje_odbioru(
                s, run_id, swiat.tenant_id, "nie-ma-takiego-uzytkownika", "send_back", None)

    assert str(wyjatek.value) == "uzytkownik nie nalezy do zespolu runu"
    assert await zdarzenia_odbioru(swiat, run_id) == []


async def test_krok_udany_przed_nieudanymi_krokami_nie_ukrywa_ich_brakow(swiat):
    run_id = await zasiej_run(swiat)
    await zasiej_zdarzenie(swiat, run_id, 1, "step_finished", "pierwszy", dane={"verdict": "pass"})
    await zasiej_zdarzenie(swiat, run_id, 2, "step_failed", "drugi", text="recenzja odrzuciła",
                           dane={"verdict": "fail"})
    await zasiej_zdarzenie(swiat, run_id, 3, "step_finished", "trzeci")
    await zasiej_zdarzenie(swiat, run_id, 4, "step_failed", "czwarty", text="brak pliku")

    tresc = await dowody_ok(swiat, run_id)

    assert tresc["braki"] == [
        brak("werdykt_fail", "drugi", "werdykt fail: recenzja odrzuciła"),
        brak("brak_dowodu", "czwarty", "brak pliku"),
    ]


async def test_gdy_w_bazie_sa_dwie_decyzje_runu_ekran_pokazuje_ta_o_mniejszym_numerze_zdarzenia(swiat):
    run_id = await run_bez_brakow(swiat)
    # wstawione w odwrotnej kolejnosci niz numery zdarzen
    await zasiej_zdarzenie(swiat, run_id, 2, "acceptance", "", text=json.dumps({
        "user_id": "u2", "email": "druga@example.com", "decyzja": "send_back", "uzasadnienie": None}))
    await zasiej_zdarzenie(swiat, run_id, 1, "acceptance", "", text=json.dumps({
        "user_id": "u1", "email": "pierwsza@example.com", "decyzja": "accept", "uzasadnienie": None}))

    decyzja = (await dowody_ok(swiat, run_id))["decyzja"]

    assert (decyzja["decyzja"], decyzja["email"]) == ("accept", "pierwsza@example.com")


async def test_zapytanie_o_decyzje_runu_sortuje_po_numerze_zdarzenia(swiat):
    # kolejnosc wierszy bez ORDER BY zalezy od planu zapytania, wiec sprawdzamy sam SQL (PostgreSQL)
    run_id = await run_bez_brakow(swiat)
    zapytania: list[tuple[str, list]] = []

    def zbierz(conn, clauseelement, multiparams, params, execution_options):
        if isinstance(clauseelement, Select):
            zapytania.append(_skompilowane_dla_postgresa(clauseelement))

    event.listen(swiat.engine.sync_engine, "before_execute", zbierz)
    try:
        await dowody_ok(swiat, run_id)
    finally:
        event.remove(swiat.engine.sync_engine, "before_execute", zbierz)

    decyzje = [q for q, parametry in zapytania if "acceptance" in parametry]
    assert len(decyzje) == 1, decyzje
    assert decyzje[0].rstrip().endswith("ORDER BY event.seq")


async def test_zapisana_decyzja_niesie_pelna_dlugosc_tekstu_w_znakach(swiat):
    run_id = await run_bez_brakow(swiat)

    r = await decyduj(swiat, run_id, decyzja="accept", uzasadnienie="zażółć gęślą jaźń")

    assert r.status_code == 200, r.text
    (zapisane,) = await zdarzenia_odbioru(swiat, run_id)
    assert "zażółć gęślą jaźń" in zapisane.text
    assert zapisane.text_full_len == len(zapisane.text)
    assert zapisane.text_truncated is False


# =================================================================================================
# D. Silnik: step_finished udanego kroku niesie artefakty = produces; dowod silnika przechodzi
#    przez prawdziwa trase wejscia kokpitu
# =================================================================================================

def _opcje_produkcji(**produces: list[str]) -> dict[str, dict]:
    return {krok: {"produces": lista} for krok, lista in produces.items()}


async def test_udany_krok_skilla_z_produces_niesie_w_step_finished_artefakty_rowne_produces(tmp_path):
    s = swiat_silnika(tmp_path, [K("a"), K("b", ("a",))],
                      opcje_skilli=_opcje_produkcji(a=["plan.md", "tdd.md"]))

    zdarzenia = await uruchom(s, Szpieg())

    assert jedno(zdarzenia, "step_finished", "a")["artefakty"] == ["plan.md", "tdd.md"]


async def test_udany_krok_skilla_bez_produces_nie_ma_pola_artefakty(tmp_path):
    s = swiat_silnika(tmp_path, [K("a"), K("b", ("a",))],
                      opcje_skilli=_opcje_produkcji(a=["plan.md"]))

    zdarzenia = await uruchom(s, Szpieg())

    assert "artefakty" not in jedno(zdarzenia, "step_finished", "b")


async def test_nieudany_krok_z_produces_nie_deklaruje_artefaktow(tmp_path):
    s = swiat_silnika(tmp_path, [K("a")], opcje_skilli=_opcje_produkcji(a=["plan.md"]))
    agent = Szpieg(wyniki={"a": WynikKroku(status="nieudany", powod="kod wyjścia 1", kod_wyjscia=1)})

    zdarzenia = await uruchom(s, agent)

    assert "artefakty" not in jedno(zdarzenia, "step_failed", "a")


async def test_kroki_niezalezne_niosa_kazdy_swoje_artefakty(tmp_path):
    s = swiat_silnika(tmp_path, [K("a"), K("b")],
                      opcje_skilli=_opcje_produkcji(a=["a.md"], b=["b1.md", "b2.md"]))

    zdarzenia = await uruchom(s, Szpieg())

    assert jedno(zdarzenia, "step_finished", "a")["artefakty"] == ["a.md"]
    assert jedno(zdarzenia, "step_finished", "b")["artefakty"] == ["b1.md", "b2.md"]


async def test_krok_systemowy_check_deklaruje_jako_artefakt_swoj_dowod(tmp_path):
    s = swiat_z_checkiem(tmp_path)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert jedno(zdarzenia, "step_finished", "typecheck")["artefakty"] == [DOWOD_TYPECHECK]
    assert jedno(zdarzenia, "step_finished", "rec")["artefakty"] == ["zastrzezenia.md"]
    assert "artefakty" not in jedno(zdarzenia, "step_finished", "autor")


async def test_krok_systemowy_diff_deklaruje_jako_artefakty_diff_i_zmienione_linie(tmp_path):
    s = swiat_z_dwoma_checkami(
        tmp_path, typecheck=komenda("print('typy-ok')"), contract=komenda("print('kontrakt-ok')"))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert jedno(zdarzenia, "step_finished", "diff")["artefakty"] == [
        "zmiana.diff", "zmienione-linie.json"]


async def test_udany_check_z_silnika_przechodzi_wejscie_kokpitu_i_zapisuje_dowod_oraz_artefakty(
    swiat, tmp_path,
):
    s = swiat_z_checkiem(tmp_path)
    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())
    koniec = jedno(zdarzenia, "step_finished", "typecheck")

    r = await swiat.klient.post(
        "/api/events", json={**koniec, "run_id": swiat.run_id, "event_id": str(uuid.uuid4())},
        headers=NAGLOWEK)

    assert r.status_code == 200, r.text
    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane == {"dowod": koniec["dowod"], "artefakty": [DOWOD_TYPECHECK]}
    assert dane["dowod"]["source"] == "engine"


async def test_nieudany_check_z_silnika_przechodzi_wejscie_kokpitu_z_dowodem_i_kodem_wyjscia(
    swiat, tmp_path,
):
    s = swiat_z_checkiem(tmp_path, typecheck=komenda(KOD_CZERWONY))
    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())
    koniec = jedno(zdarzenia, "step_failed", "typecheck")

    r = await swiat.klient.post(
        "/api/events", json={**koniec, "run_id": swiat.run_id, "event_id": str(uuid.uuid4())},
        headers=NAGLOWEK)

    assert r.status_code == 200, r.text
    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert dane == {"dowod": koniec["dowod"]}
    assert dane["dowod"]["exit_code"] == 3


# =================================================================================================
# G. Migracja 0014 i kolumna `event.dane`: brak danych to NULL, wiersze sprzed migracji zostaja
# =================================================================================================

def _sql_migracji(zakres: str, w_dol: bool) -> list[str]:
    """Polecenia ALTER i UPDATE migracji generowane przez Alembica bez bazy (tryb offline)."""
    bufor = io.StringIO()
    konfiguracja = Config(str(KORZEN_REPO / "alembic.ini"))
    konfiguracja.set_main_option("script_location", str(KORZEN_REPO / "alembic"))
    konfiguracja.set_main_option("sqlalchemy.url", "postgresql+asyncpg://u:p@localhost/offline")
    # env.py nie przekazuje output_buffer, wiec tryb offline pisze SQL na sys.stdout
    with contextlib.redirect_stdout(bufor):
        (command.downgrade if w_dol else command.upgrade)(konfiguracja, zakres, sql=True)
    return [w for w in bufor.getvalue().splitlines() if w.startswith(("ALTER", "UPDATE"))]


def test_migracja_danych_zdarzenia_dodaje_kolumne_dane_bez_not_null_i_przesuwa_wersje_na_0014():
    assert _sql_migracji("0013:0014", w_dol=False) == [
        "ALTER TABLE event ADD COLUMN dane JSON;",
        "UPDATE alembic_version SET version_num='0014' "
        "WHERE alembic_version.version_num = '0013';",
    ]


def test_cofniecie_migracji_danych_zdarzenia_usuwa_kolumne_dane_i_wraca_do_wersji_0013():
    assert _sql_migracji("0014:0013", w_dol=True) == [
        "ALTER TABLE event DROP COLUMN dane;",
        "UPDATE alembic_version SET version_num='0013' "
        "WHERE alembic_version.version_num = '0014';",
    ]

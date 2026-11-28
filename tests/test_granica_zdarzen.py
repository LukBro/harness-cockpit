"""HAR-208: granica zdarzenia od runnera (`EventIn`) i niezmiennik `measured_or_null`.

- `tokens` i `duration_ms` mieszcza sie w int4 PostgreSQL (wiekszy napis to 422, nie 500);
- `kind` to zamknieta lista rodzajow silnika i czlowieka albo `hook:<zdarzenie>` (hooki Claude Code
  maja otwarty zbior nazw, runner buduje rodzaj jako "hook:" + nazwa);
- `cost_source` to `measured` albo `unavailable`, a `unavailable` wystepuje wtedy i tylko wtedy,
  gdy kosztu brak (`cost_usd is None`).

Skutek odrzucenia czytamy z bazy (nowa sesja), przez prawdziwa aplikacje. Fixture `swiat` idzie
na SQLite i na PostgreSQL (wariant z markerem `baza`, w osobnym schemacie na czas testu).
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.api.schematy import EventIn
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus
from cockpit.services.invariants import Niezmiennik
from cockpit.services.runs import RODZAJE_CZLOWIEKA
from cockpit.services.walidacja import sprawdz_pomiar
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

KORZEN = Path(__file__).resolve().parent.parent
URL = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
TOKEN = "token-runnera-granicy"
INT4_MAX = 2_147_483_647

# Listy niezalezne od kodu: rodzaje, ktore wysyla silnik, i rodzaje zarezerwowane dla czlowieka.
RODZAJE_SILNIKA = [
    "output", "step_started", "step_finished", "step_failed", "step_aborted", "step_skipped",
    "step_waiting", "run_finished", "resume_ignored", "context_manifest", "ticket_ostrzezenie",
    "warning",
]
RODZAJE_Z_LISTY_CZLOWIEKA = [
    "decision", "integrated", "committed", "acceptance", "run_cancelled", "atrapa_pominieta"]

BAZA_ZDARZENIA = {"run_id": "run-1", "event_id": "e-1", "step_id": "krok-1", "kind": "output"}


def _zdarzenie(**pola) -> EventIn:
    return EventIn.model_validate({**BAZA_ZDARZENIA, **pola})


# --- czysta walidacja modelu wejscia ---------------------------------------------------------

@pytest.mark.parametrize("pole", ["tokens", "duration_ms"])
@pytest.mark.parametrize("wartosc", [0, 1, INT4_MAX])
def test_pomiar_zdarzenia_w_zakresie_int4_jest_przyjety(pole, wartosc):
    assert getattr(_zdarzenie(**{pole: wartosc}), pole) == wartosc


@pytest.mark.parametrize("pole", ["tokens", "duration_ms"])
@pytest.mark.parametrize("wartosc", [INT4_MAX + 1, -1, 10**12])
def test_pomiar_zdarzenia_poza_zakresem_int4_jest_odrzucony(pole, wartosc):
    with pytest.raises(ValidationError) as blad:
        _zdarzenie(**{pole: wartosc})

    assert [b["loc"] for b in blad.value.errors()] == [(pole,)]


@pytest.mark.parametrize("pole", ["tokens", "duration_ms"])
def test_brak_pomiaru_zdarzenia_to_brak_a_nie_zero(pole):
    assert getattr(_zdarzenie(), pole) is None


@pytest.mark.parametrize("kind", RODZAJE_SILNIKA + RODZAJE_Z_LISTY_CZLOWIEKA)
def test_kazdy_znany_rodzaj_zdarzenia_jest_przyjety(kind):
    assert _zdarzenie(kind=kind).kind == kind


def test_rodzaje_czlowieka_z_serwisu_to_dokladnie_te_z_listy_testu():
    # lista testu i RODZAJE_CZLOWIEKA nie rozjezdzaja sie po cichu
    assert set(RODZAJE_Z_LISTY_CZLOWIEKA) == set(RODZAJE_CZLOWIEKA)


@pytest.mark.parametrize("kind", sorted(RODZAJE_CZLOWIEKA))
def test_rodzaj_zarezerwowany_dla_czlowieka_przechodzi_model_by_serwis_odrzucil_go_niezmiennikiem(
    kind,
):
    assert _zdarzenie(kind=kind).kind == kind


@pytest.mark.parametrize("kind", [
    "hook:ToolDenied", "hook:ToolDeniedRepeat", "hook:SessionEnd", "hook:phase_failed",
    "hook:" + "k" * 45, "hook:A", "hook:a_1",
])
def test_rodzaj_hooka_z_nazwa_zdarzenia_jest_przyjety(kind):
    assert _zdarzenie(kind=kind).kind == kind


@pytest.mark.parametrize("kind", [
    "log", "cost", "x", "", "nieznany", "OUTPUT", "output ", "step_start",
    "hook:", "hook:" + "k" * 46, "hook:zla-nazwa", "hook:zla nazwa", "hook:zla.nazwa",
    "xhook:ToolDenied", "Hook:ToolDenied", "hook:ToolDenied\n", "hook:ąę", "hook",
])
def test_nieznany_rodzaj_zdarzenia_jest_odrzucony(kind):
    with pytest.raises(ValidationError) as blad:
        _zdarzenie(kind=kind)

    assert {b["loc"][0] for b in blad.value.errors()} == {"kind"}


@pytest.mark.parametrize("kind", [None, 7, ["output"]])
def test_rodzaj_zdarzenia_zlego_typu_jest_odrzucony(kind):
    with pytest.raises(ValidationError):
        _zdarzenie(kind=kind)


@pytest.mark.parametrize("zrodlo", ["measured", "unavailable"])
def test_znane_zrodlo_kosztu_jest_przyjete(zrodlo):
    assert _zdarzenie(cost_source=zrodlo).cost_source == zrodlo


def test_zdarzenie_bez_zrodla_kosztu_ma_zrodlo_unavailable():
    assert _zdarzenie().cost_source == "unavailable"


@pytest.mark.parametrize("zrodlo", ["", "x", "Measured", "UNAVAILABLE", "measured ", "estimated"])
def test_nieznane_zrodlo_kosztu_jest_odrzucone(zrodlo):
    with pytest.raises(ValidationError) as blad:
        _zdarzenie(cost_source=zrodlo)

    assert [b["loc"] for b in blad.value.errors()] == [("cost_source",)]


# --- kazdy rodzaj wysylany przez runnera nalezy do dozwolonych ---------------------------------

def _napisy(wezel: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(wezel) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


_WARIANT_MUTANTA = re.compile(r"__mutmut_\d+$")


def _wezly_bez_wariantow_mutanta(wezel: ast.AST):
    """Jak `ast.walk`, ale bez poddrzew funkcji `x_<nazwa>__mutmut_<n>`: pod mutmutem plik
    runnera zawiera zmutowane kopie funkcji (napisy `XX...XX`, wielkie litery), a to nie jest
    kod, ktory runner wysyla. `__mutmut_orig` i trampoliny zostaja skanowane."""
    if isinstance(wezel, (ast.FunctionDef, ast.AsyncFunctionDef)) and _WARIANT_MUTANTA.search(
        wezel.name
    ):
        return
    yield wezel
    for dziecko in ast.iter_child_nodes(wezel):
        yield from _wezly_bez_wariantow_mutanta(dziecko)


def _rodzaje_ze_zrodla(tekst: str) -> set[str]:
    """Napisy podane jako rodzaj: `kind=...`, `"kind": ...`, `kind_*=...` i drugi argument
    pozycyjny `_zdarzenie(run_id, <rodzaj>, ...)` w silniku. Prefiks `hook:` ma osobny test."""
    znalezione: set[str] = set()
    for w in _wezly_bez_wariantow_mutanta(ast.parse(tekst)):
        if isinstance(w, ast.keyword) and w.arg is not None and w.arg.startswith("kind"):
            znalezione |= _napisy(w.value)
        elif isinstance(w, ast.Dict):
            for klucz, wartosc in zip(w.keys, w.values):
                if isinstance(klucz, ast.Constant) and klucz.value == "kind":
                    znalezione |= _napisy(wartosc)
        elif isinstance(w, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith("kind") for t in w.targets
        ):
            znalezione |= _napisy(w.value)
        elif (isinstance(w, ast.Call) and isinstance(w.func, ast.Name)
              and w.func.id == "_zdarzenie" and len(w.args) >= 2):
            znalezione |= _napisy(w.args[1])
    return {k for k in znalezione if not k.startswith("hook:")}


def _rodzaje_wysylane_przez_runnera() -> set[str]:
    znalezione: set[str] = set()
    for plik in sorted((KORZEN / "src" / "runner").glob("*.py")):
        znalezione |= _rodzaje_ze_zrodla(plik.read_text(encoding="utf-8"))
    return znalezione


ZRODLO_Z_WARIANTEM_MUTANTA = '''
def x_f__mutmut_1():
    return {"kind": "XXoutputXX"}


def x_f__mutmut_orig():
    return {"kind": "output"}


def x_f__mutmut_12():
    return g(kind="OUTPUT")


async def x_h__mutmut_3():
    return {"kind": "STEP_STARTED"}
'''


def test_skaner_pomija_warianty_mutanta_a_zostawia_wersje_oryginalna():
    assert _rodzaje_ze_zrodla(ZRODLO_Z_WARIANTEM_MUTANTA) == {"output"}


def test_skaner_nadal_czyta_funkcje_o_zwyklej_nazwie_i_trampoline():
    zrodlo = (
        'def f():\n    return g(kind="step_started")\n\n'
        'def x_g():\n    return {"kind": "step_failed"}\n\n'
        'def g_mutmut_nie_wariant():\n    return {"kind": "run_finished"}\n\n'
        'def wyslij_v2():\n    return {"kind": "step_aborted"}\n\n'            # koncowka `\d+`, bez `__mutmut_`
        'def x_f_mutmut_1():\n    return {"kind": "step_skipped"}\n\n'         # jedno podkreslenie
        'def x_f__mutmut_1_pomocnik():\n    return {"kind": "step_waiting"}\n'  # cos po numerze
    )

    assert _rodzaje_ze_zrodla(zrodlo) == {
        "step_started", "step_failed", "run_finished", "step_aborted", "step_skipped",
        "step_waiting"}


def test_skaner_rodzajow_runnera_znajduje_znane_rodzaje_silnika():
    # skaner, ktory nic nie znalazl, nie moze dawac zielonego wyniku nastepnemu testowi
    assert set(RODZAJE_SILNIKA) <= _rodzaje_wysylane_przez_runnera()


def test_kazdy_rodzaj_wysylany_przez_runnera_jest_przyjety_przez_kokpit():
    odrzucone = []
    for kind in sorted(_rodzaje_wysylane_przez_runnera()):
        try:
            _zdarzenie(kind=kind)
        except ValidationError:
            odrzucone.append(kind)

    assert odrzucone == []


def test_rodzaje_zdarzenia_to_dokladnie_rodzaje_silnika_i_czlowieka_oraz_wzorzec_hooka():
    galezie = EventIn.model_json_schema()["properties"]["kind"]["anyOf"]

    lista = [set(g["enum"]) for g in galezie if "enum" in g]
    wzorce = [g["pattern"] for g in galezie if "pattern" in g]
    assert lista == [set(RODZAJE_SILNIKA) | set(RODZAJE_Z_LISTY_CZLOWIEKA)]
    assert [g.get("maxLength") for g in galezie if "pattern" in g] == [50]   # kolumna String(50)
    assert len(wzorce) == 1
    assert re.fullmatch(wzorce[0], "hook:" + "A" * 45)
    assert not re.fullmatch(wzorce[0], "hook:" + "A" * 46)


# --- sprawdz_pomiar: `unavailable` wtedy i tylko wtedy, gdy brak kosztu ----------------------

@pytest.mark.parametrize(("koszt", "zrodlo"), [
    (None, "unavailable"), (0.0, "measured"), (0.0123, "measured"), (1.5, "measured"),
])
def test_pomiar_spojny_ze_zrodlem_nie_jest_naruszeniem(koszt, zrodlo):
    assert sprawdz_pomiar(koszt, zrodlo) is None


@pytest.mark.parametrize(("koszt", "zrodlo"), [
    (1.5, "unavailable"),      # koszt jest, a zrodlo mowi, ze go nie ma
    (0.0, "unavailable"),      # zmyslone zero (BRO-87)
    (None, "measured"),        # zrodlo mowi o pomiarze, ktorego nie ma
    (0, ""),                   # puste zrodlo przy zerze
    (None, ""),                # puste zrodlo przy braku kosztu
    (1.5, ""),                 # puste zrodlo przy koszcie
    (0.5, "estimated"),        # zrodlo spoza listy
    (None, "estimated"),
])
def test_pomiar_sprzeczny_ze_zrodlem_to_naruszenie_measured_or_null(koszt, zrodlo):
    naruszenie = sprawdz_pomiar(koszt, zrodlo)

    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.measured_or_null


@pytest.mark.parametrize(("koszt", "zrodlo", "szczegol"), [
    (None, "estimated",
     "zrodlo kosztu 'estimated' spoza listy: measured, unavailable"),
    (0.0, "estimated",
     "zrodlo kosztu 'estimated' spoza listy: measured, unavailable"),
    (1.5, "unavailable",
     "koszt 1.5 przy zrodle 'unavailable' - zrodlo 'unavailable' oznacza brak kosztu"),
    (None, "measured",
     "zrodlo 'measured' bez kosztu - brak pomiaru to zrodlo 'unavailable'"),
])
def test_naruszenie_pomiaru_mowi_ktora_regula_zostala_zlamana(koszt, zrodlo, szczegol):
    naruszenie = sprawdz_pomiar(koszt, zrodlo)

    assert naruszenie is not None
    assert naruszenie.szczegol == szczegol


# --- przez prawdziwa aplikacje: skutek w bazie -----------------------------------------------

def _warianty_bazy():
    return [
        "sqlite",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.baza,
                pytest.mark.skipif(
                    not URL.startswith("postgresql"),
                    reason="test na PostgreSQL: ustaw "
                    "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test",
                ),
            ],
        ),
    ]


class Swiat:
    def __init__(self, klient: AsyncClient, run_id: str) -> None:
        self.klient = klient
        self.run_id = run_id


@pytest.fixture(params=_warianty_bazy())
async def swiat(request, tmp_path, monkeypatch):
    schemat = None
    admin = None
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    else:
        schemat = "t_" + uuid.uuid4().hex[:12]
        admin = create_async_engine(URL, isolation_level="AUTOCOMMIT")
        async with admin.connect() as c:
            await c.execute(text(f'CREATE SCHEMA "{schemat}"'))
        engine = create_async_engine(
            URL, connect_args={"server_settings": {"search_path": schemat}})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        konto, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
        async with maker() as s:
            runner = Runner(
                tenant_id=tenant_id, name="runner", last_seen_at=datetime.now(timezone.utc),
                token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
            s.add(runner)
            await s.flush()
            run = Run(tenant_id=tenant_id, runner_id=runner.id, status=RunStatus.running)
            s.add(run)
            await s.commit()
            run_id = run.id
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await zaloguj_klienta(c, maker, konto)
            yield Swiat(c, run_id)
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.connect() as c:
                await c.execute(text(f'DROP SCHEMA "{schemat}" CASCADE'))
            await admin.dispose()


def _tresc(swiat: Swiat, **pola) -> dict:
    return {"run_id": swiat.run_id, "kind": "output", "step_id": "s0",
            "event_id": str(uuid.uuid4()), **pola}


async def _wyslij(swiat: Swiat, **pola):
    return await swiat.klient.post(
        "/api/events", json=_tresc(swiat, **pola), headers={"Authorization": f"Bearer {TOKEN}"})


async def _zdarzenia(swiat: Swiat) -> list[Event]:
    async with db_session.SessionLocal() as s:  # nowa sesja: odczyt z bazy
        return list((await s.execute(
            select(Event).where(Event.run_id == swiat.run_id).order_by(Event.seq))).scalars())


async def test_step_id_o_jeden_ponad_limit_kolumny_to_422_i_nic_nie_powstaje(swiat):
    r = await _wyslij(swiat, step_id="s" * 101)

    assert r.status_code == 422
    assert await _zdarzenia(swiat) == []


async def test_step_id_dokladnie_na_limicie_kolumny_jest_zapisany(swiat):
    r = await _wyslij(swiat, step_id="s" * 100)

    assert r.status_code == 200, r.text
    assert [e.step_id for e in await _zdarzenia(swiat)] == ["s" * 100]


@pytest.mark.parametrize("pole", ["tokens", "duration_ms"])
@pytest.mark.parametrize("wartosc", [INT4_MAX + 1, -1])
async def test_pomiar_poza_int4_to_422_i_nic_nie_powstaje(swiat, pole, wartosc):
    r = await _wyslij(swiat, **{pole: wartosc})

    assert r.status_code == 422
    assert await _zdarzenia(swiat) == []


@pytest.mark.parametrize("pole", ["tokens", "duration_ms"])
@pytest.mark.parametrize("wartosc", [0, INT4_MAX])
async def test_pomiar_na_brzegu_int4_jest_zapisany_bez_zmiany(swiat, pole, wartosc):
    r = await _wyslij(swiat, **{pole: wartosc})

    assert r.status_code == 200, r.text
    assert [getattr(e, pole) for e in await _zdarzenia(swiat)] == [wartosc]


@pytest.mark.parametrize("kind", ["log", "cost", "x", "hook:", "hook:" + "k" * 46])
async def test_nieznany_rodzaj_zdarzenia_to_422_i_nic_nie_powstaje(swiat, kind):
    r = await _wyslij(swiat, kind=kind)

    assert r.status_code == 422
    assert await _zdarzenia(swiat) == []


@pytest.mark.parametrize("kind", ["hook:ToolDenied", "hook:ToolDeniedRepeat"])
async def test_zdarzenie_hooka_trafia_do_bazy_z_rodzajem_hooka(swiat, kind):
    r = await _wyslij(swiat, kind=kind, text="Bash: rm [role=reviewer]")

    assert r.status_code == 200, r.text
    assert [e.kind for e in await _zdarzenia(swiat)] == [kind]


@pytest.mark.parametrize("kind", RODZAJE_Z_LISTY_CZLOWIEKA)
async def test_rodzaj_czlowieka_od_runnera_jest_odrzucony_niezmiennikiem_approval_required(
    swiat, kind,
):
    r = await _wyslij(swiat, kind=kind)

    assert r.status_code == 422
    assert "approval_required" in r.text    # odrzucil serwis, nie sama lista rodzajow modelu
    assert await _zdarzenia(swiat) == []


@pytest.mark.parametrize("zrodlo", ["x", "", "estimated"])
async def test_nieznane_zrodlo_kosztu_to_422_i_nic_nie_powstaje(swiat, zrodlo):
    r = await _wyslij(swiat, cost_source=zrodlo)

    assert r.status_code == 422
    assert await _zdarzenia(swiat) == []


async def test_koszt_przy_zrodle_unavailable_to_422_i_nic_nie_powstaje(swiat):
    r = await _wyslij(swiat, cost_usd=1.5, cost_source="unavailable")

    assert r.status_code == 422
    assert "measured_or_null" in r.text
    assert await _zdarzenia(swiat) == []


async def test_koszt_zero_z_pustym_zrodlem_to_422_i_nic_nie_powstaje(swiat):
    r = await _wyslij(swiat, cost_usd=0, cost_source="")

    assert r.status_code == 422
    assert await _zdarzenia(swiat) == []


async def test_zmierzony_koszt_zero_jest_zapisany_jako_zero_ze_zrodlem_measured(swiat):
    r = await _wyslij(swiat, cost_usd=0.0, cost_source="measured")

    assert r.status_code == 200, r.text
    assert [(e.cost_usd, e.cost_source) for e in await _zdarzenia(swiat)] == [(0.0, "measured")]


async def test_zrodlo_measured_bez_kosztu_to_422_i_nic_nie_powstaje(swiat):
    r = await _wyslij(swiat, cost_source="measured")

    assert r.status_code == 422
    assert await _zdarzenia(swiat) == []


async def test_brak_kosztu_ze_zrodlem_unavailable_jest_zapisany_jako_brak(swiat):
    r = await _wyslij(swiat, cost_usd=None, cost_source="unavailable")

    assert r.status_code == 200, r.text
    assert [(e.cost_usd, e.cost_source) for e in await _zdarzenia(swiat)] == [
        (None, "unavailable")]


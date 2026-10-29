"""Migracje schematu: to, co buduje Alembic, ma byc tym, co opisuje model (HAR-1).

Testy sa synchroniczne celowo: env.py Alembica sam uruchamia petle zdarzen
(asyncio.run), co nie dziala wewnatrz petli pytest-asyncio.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Column, MetaData, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from cockpit.db.models import Base, Event, RolaWZespole, Run, RunnerKind, RunStatus

KORZEN = Path(__file__).resolve().parent.parent
URL = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.baza,
    pytest.mark.skipif(
        not URL.startswith("postgresql"),
        reason="test migracji wymaga PostgreSQL: ustaw "
        "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test",
    ),
]

TABELE_MODELU = {"tenant", "runner", "run", "event", "user", "membership", "sesja", "blokada_logowania"}
TYPY_ENUM = {"runstatus", "runnerkind", "rolawzespole"}


@pytest.fixture
def cfg():
    c = Config(str(KORZEN / "alembic.ini"))
    c.set_main_option("script_location", str(KORZEN / "alembic"))
    c.set_main_option("sqlalchemy.url", URL.replace("%", "%%"))
    command.downgrade(c, "base")  # czysty start, takze na pustej bazie
    yield c
    command.downgrade(c, "base")


def _z_polaczeniem(funkcja):
    async def _biegnij():
        engine = create_async_engine(URL)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(funkcja)
        finally:
            await engine.dispose()

    return asyncio.run(_biegnij())


def _roznice_schematu(metadata: MetaData = Base.metadata):
    return _z_polaczeniem(
        lambda c: compare_metadata(MigrationContext.configure(c), metadata)
    )


def _wartosci_enuma(nazwa_typu: str) -> set[str]:
    def zapytaj(c):
        wiersze = c.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typname = :nazwa AND n.nspname = 'public'"
            ),
            {"nazwa": nazwa_typu},
        )
        return {w[0] for w in wiersze}

    return _z_polaczeniem(zapytaj)


def _tabele_i_typy_w_public() -> tuple[set[str], set[str]]:
    def zapytaj(c):
        tabele = {
            w[0]
            for w in c.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        typy = {
            w[0]
            for w in c.execute(
                text(
                    "SELECT t.typname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public' AND t.typtype = 'e'"
                )
            )
        }
        return tabele, typy

    return _z_polaczeniem(zapytaj)


def test_migracja_tworzy_ten_sam_schemat_co_model(cfg):
    command.upgrade(cfg, "head")

    roznice = _roznice_schematu()
    assert roznice == [], f"schemat po migracji rozni sie od modelu: {roznice}"

    # compare_metadata nie porownuje wartosci enumow - sprawdzamy je osobno
    assert _wartosci_enuma("runstatus") == {m.name for m in RunStatus}
    assert _wartosci_enuma("runnerkind") == {m.name for m in RunnerKind}
    assert _wartosci_enuma("rolawzespole") == {m.name for m in RolaWZespole}


def test_cofniecie_migracji_zostawia_pusta_baze_bez_typow(cfg):
    command.upgrade(cfg, "head")
    tabele, typy = _tabele_i_typy_w_public()
    assert TABELE_MODELU <= tabele, "po upgrade brakuje tabel modelu"
    assert TYPY_ENUM <= typy, "po upgrade brakuje typow enum"

    command.downgrade(cfg, "base")

    tabele, typy = _tabele_i_typy_w_public()
    assert not (TABELE_MODELU & tabele), f"zostaly tabele: {TABELE_MODELU & tabele}"
    assert not (TYPY_ENUM & typy), f"zostaly typy enum: {TYPY_ENUM & typy}"


def test_zmiana_modelu_bez_migracji_daje_roznice_w_schemacie(cfg):
    kolumny_przed = {t.name: set(t.columns.keys()) for t in Base.metadata.sorted_tables}
    command.upgrade(cfg, "head")

    # kopia modelu: nie dotykamy Base.metadata wspolnego dla calego przebiegu testow
    kopia = MetaData()
    for tabela in Base.metadata.sorted_tables:
        tabela.to_metadata(kopia)
    kopia.tables["tenant"].append_column(Column("kolumna_spoza_migracji", String(50)))

    roznice = _roznice_schematu(kopia)

    # dokladnie ta jedna zmiana, nie jakakolwiek przypadkowa roznica
    assert len(roznice) == 1, f"oczekiwano jednej roznicy, jest: {roznice}"
    rodzaj, _schemat, tabela, kolumna = roznice[0]
    assert (rodzaj, tabela, kolumna.name) == (
        "add_column",
        "tenant",
        "kolumna_spoza_migracji",
    )

    kolumny_po = {t.name: set(t.columns.keys()) for t in Base.metadata.sorted_tables}
    assert kolumny_po == kolumny_przed, "test zostawil slad w Base.metadata"


# --- HAR-51: identyfikator ticketu w runie i znacznik obciecia tekstu zdarzenia ---


def _kolumny_tabeli(tabela: str) -> dict[str, str]:
    """Nazwa kolumny -> 'YES'/'NO' (czy dopuszcza NULL), schemat public."""

    def zapytaj(c):
        wiersze = c.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :tabela"
            ),
            {"tabela": tabela},
        )
        return {w[0]: w[1] for w in wiersze}

    return _z_polaczeniem(zapytaj)


def _definicje_indeksow(tabela: str) -> dict[str, str]:
    def zapytaj(c):
        wiersze = c.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :tabela"
            ),
            {"tabela": tabela},
        )
        return {w[0]: w[1] for w in wiersze}

    return _z_polaczeniem(zapytaj)


def _wstaw_run(c, *, ticket_id: str | None = None, z_ticketem: bool = False) -> None:
    c.execute(
        text(
            "INSERT INTO tenant (id, name, created_at) "
            "VALUES ('t1', 'Zespol', now())"
        )
    )
    kolumny = "id, tenant_id, runner_kind, workflow, status, created_at"
    wartosci = "'r1', 't1', 'local', 'wf', 'pending', now()"
    if z_ticketem:
        kolumny += ", ticket_id"
        wartosci += ", :ticket"
    c.execute(
        text(f"INSERT INTO run ({kolumny}) VALUES ({wartosci})"),
        {"ticket": ticket_id} if z_ticketem else {},
    )


_EVENT_BAZA = (
    "id, tenant_id, run_id, seq, step_id, agent_id, kind, text, cost_source, at, event_id"
)
_EVENT_WARTOSCI = "'e1', 't1', 'r1', 1, 's', 'a', 'k', 'tresc', 'none', now(), 'ev1'"


def test_pierwsza_migracja_ma_juz_tenant_runner_kind_i_runner_id_w_runie(cfg):
    command.upgrade(cfg, "0001")

    kolumny = set(_kolumny_tabeli("run"))

    assert {"tenant_id", "runner_kind", "runner_id"} <= kolumny
    # kontrast: to stan pierwszej migracji, a nie head - pola ticketu jeszcze nie ma
    assert "ticket_id" not in kolumny


def test_run_bez_podanego_ticketu_ma_ticket_nieznany_a_nie_pusty_napis(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run(c)
        return c.execute(text("SELECT ticket_id FROM run WHERE id = 'r1'")).one()[0]

    assert _z_polaczeniem(zapytaj) is None


def test_run_zapamietuje_identyfikator_ticketu(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run(c, ticket_id="HAR-51", z_ticketem=True)
        return c.execute(text("SELECT ticket_id FROM run WHERE id = 'r1'")).one()[0]

    assert _z_polaczeniem(zapytaj) == "HAR-51"


def test_ticket_runu_dopuszcza_brak_wartosci(cfg):
    command.upgrade(cfg, "head")

    assert _kolumny_tabeli("run")["ticket_id"] == "YES"


def test_zdarzenie_bez_podanego_znacznika_nie_jest_obciete(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run(c)
        c.execute(text(f"INSERT INTO event ({_EVENT_BAZA}) VALUES ({_EVENT_WARTOSCI})"))
        return c.execute(
            text("SELECT text_truncated FROM event WHERE id = 'e1'")
        ).one()[0]

    assert _z_polaczeniem(zapytaj) is False


def test_zdarzenie_nie_przyjmuje_nieznanego_znacznika_obciecia(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run(c)
        c.execute(
            text(
                f"INSERT INTO event ({_EVENT_BAZA}, text_truncated) "
                f"VALUES ({_EVENT_WARTOSCI}, NULL)"
            )
        )

    with pytest.raises(IntegrityError):
        _z_polaczeniem(zapytaj)


def test_znacznik_obciecia_jest_wymagany_w_schemacie(cfg):
    command.upgrade(cfg, "head")

    assert _kolumny_tabeli("event")["text_truncated"] == "NO"


def test_zdarzenie_bez_pomiaru_dlugosci_ma_dlugosc_nieznana_a_nie_zero(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run(c)
        c.execute(text(f"INSERT INTO event ({_EVENT_BAZA}) VALUES ({_EVENT_WARTOSCI})"))
        return c.execute(
            text("SELECT text_full_len FROM event WHERE id = 'e1'")
        ).one()[0]

    assert _z_polaczeniem(zapytaj) is None


def test_zdarzenie_zapamietuje_pelna_dlugosc_obcietego_tekstu(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run(c)
        c.execute(
            text(
                f"INSERT INTO event ({_EVENT_BAZA}, text_truncated, text_full_len) "
                f"VALUES ({_EVENT_WARTOSCI}, true, 123456)"
            )
        )
        return tuple(
            c.execute(
                text("SELECT text_truncated, text_full_len FROM event WHERE id = 'e1'")
            ).one()
        )

    assert _z_polaczeniem(zapytaj) == (True, 123456)


def test_pelna_dlugosc_tekstu_zdarzenia_dopuszcza_brak_wartosci(cfg):
    command.upgrade(cfg, "head")

    assert _kolumny_tabeli("event")["text_full_len"] == "YES"


def test_indeks_runow_wedlug_zespolu_i_ticketu_zaczyna_sie_od_zespolu(cfg):
    command.upgrade(cfg, "head")

    indeksy = _definicje_indeksow("run")

    assert "ix_run_tenant_ticket" in indeksy
    # kolejnosc ma znaczenie: zapytania zawsze niosa tenant_id
    assert "(tenant_id, ticket_id)" in indeksy["ix_run_tenant_ticket"]


def test_cofniecie_do_poprzedniej_migracji_usuwa_ticket_obciecie_i_indeks(cfg):
    command.upgrade(cfg, "head")
    assert "ticket_id" in _kolumny_tabeli("run")  # kontrast: przed cofnieciem jest

    command.downgrade(cfg, "0006")

    run = _kolumny_tabeli("run")
    event = _kolumny_tabeli("event")
    assert "ticket_id" not in run
    assert "text_truncated" not in event
    assert "text_full_len" not in event
    assert "ix_run_tenant_ticket" not in _definicje_indeksow("run")
    # to cofniecie tylko jednej migracji: reszta schematu zostaje
    assert {"tenant_id", "runner_kind", "runner_id"} <= set(run)
    assert "agent_type" in event


# --- HAR-51: wartosci domyslne zapisu przez model (nie surowy SQL) ---


def _wstaw_zespol(c) -> None:
    c.execute(text("INSERT INTO tenant (id, name, created_at) VALUES ('t1', 'Zespol', now())"))


def test_run_zapisany_przez_model_bez_ticketu_ma_ticket_nieznany_a_nie_pusty_napis(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_zespol(c)
        c.execute(Run.__table__.insert().values(id="r1", tenant_id="t1"))
        return c.execute(text("SELECT ticket_id FROM run WHERE id = 'r1'")).one()[0]

    assert _z_polaczeniem(zapytaj) is None


def test_zdarzenie_zapisane_przez_model_bez_znacznika_nie_jest_obciete(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_zespol(c)
        c.execute(Run.__table__.insert().values(id="r1", tenant_id="t1"))
        c.execute(
            Event.__table__.insert().values(
                id="e1", tenant_id="t1", run_id="r1", seq=1, kind="k"
            )
        )
        return c.execute(
            text("SELECT text_truncated FROM event WHERE id = 'e1'")
        ).one()[0]

    assert _z_polaczeniem(zapytaj) is False


def test_zdarzenie_zapisane_przez_model_bez_pomiaru_dlugosci_ma_dlugosc_nieznana_a_nie_zero(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_zespol(c)
        c.execute(Run.__table__.insert().values(id="r1", tenant_id="t1"))
        c.execute(
            Event.__table__.insert().values(
                id="e1", tenant_id="t1", run_id="r1", seq=1, kind="k"
            )
        )
        return c.execute(
            text("SELECT text_full_len FROM event WHERE id = 'e1'")
        ).one()[0]

    assert _z_polaczeniem(zapytaj) is None

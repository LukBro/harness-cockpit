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
from sqlalchemy.ext.asyncio import create_async_engine

from cockpit.db.models import Base, RunnerKind, RunStatus

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

TABELE_MODELU = {"tenant", "runner", "run", "event"}
TYPY_ENUM = {"runstatus", "runnerkind"}


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

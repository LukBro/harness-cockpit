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

from cockpit.db.models import (
    Base, Event, RolaWZespole, Run, RunnerKind, RunStatus, StepRun, StepStatus,
)

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

TABELE_MODELU = {"tenant", "runner", "run", "event", "user", "membership", "sesja", "blokada_logowania",
                 "definicja", "step_run", "project"}
TYPY_ENUM = {"runstatus", "runnerkind", "rolawzespole", "stepstatus"}


@pytest.fixture
def cfg():
    c = Config(str(KORZEN / "alembic.ini"))
    c.set_main_option("script_location", str(KORZEN / "alembic"))
    c.set_main_option("sqlalchemy.url", URL.replace("%", "%%"))
    # Czysty start przez reset schematu, nie przez downgrade: reczne mutanty migracji
    # (tests/mutanty_reczne) zostawiaja stan, ktorego niezmutowany downgrade nie cofnie
    # (DROP INDEX nieistniejacego indeksu, DROP TABLE blokowany przez FK pozostalej tabeli).
    # Baza z COCKPIT_TEST_DATABASE_URL jest wylacznie testowa (lokalnie tools/testy_pelne.sh,
    # w CI kontener postgres-mutanty / job e2e), wiec reset schematu public jest bezpieczny.
    _z_polaczeniem(_wyczysc_schemat)
    yield c
    command.downgrade(c, "base")


def _wyczysc_schemat(c) -> None:
    c.execute(text("DROP SCHEMA public CASCADE"))
    c.execute(text("CREATE SCHEMA public"))
    c.commit()


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
    assert _wartosci_enuma("stepstatus") == {m.name for m in StepStatus}


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


# --- HAR-67: katalog skilli i workflow w bazie (tabela `definicja`) ---


def _wstaw_runnera(c) -> None:
    _wstaw_zespol(c)
    c.execute(
        text(
            "INSERT INTO runner (id, tenant_id, name, version, token_hash) "
            "VALUES ('rn1', 't1', 'runner', '', 'h')"
        )
    )


def _wstaw_definicje(c, id_wiersza: str, *, klucz: str = "x") -> None:
    c.execute(
        text(
            "INSERT INTO definicja (id, tenant_id, runner_id, projekt, rodzaj, klucz, nazwa, "
            "sciezka, odswiezono_at) "
            "VALUES (:id, 't1', 'rn1', '/p', 'skill', :klucz, 'Nazwa', 'a/SKILL.md', now())"
        ),
        {"id": id_wiersza, "klucz": klucz},
    )


def test_cofniecie_do_migracji_0007_usuwa_tylko_tabele_katalogu_definicji(cfg):
    command.upgrade(cfg, "head")
    tabele, _ = _tabele_i_typy_w_public()
    assert {"definicja", "step_run"} <= tabele  # kontrast: przed cofnieciem tabele sa

    command.downgrade(cfg, "0007")

    tabele, _ = _tabele_i_typy_w_public()
    # znikaja tabele z migracji powyzej 0007 (katalog definicji, stan krokow) i nic wiecej
    assert not ({"definicja", "step_run", "project"} & tabele)
    assert (TABELE_MODELU - {"definicja", "step_run", "project"}) <= tabele


def test_ta_sama_definicja_tego_samego_runnera_i_projektu_nie_moze_wystapic_dwa_razy(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_runnera(c)
        _wstaw_definicje(c, "d1")
        _wstaw_definicje(c, "d2")  # ten sam klucz, rodzaj, projekt, runner, zespol

    with pytest.raises(IntegrityError, match="uq_definicja_klucz"):
        _z_polaczeniem(zapytaj)


def test_definicja_o_innym_kluczu_mieszka_obok_pierwszej(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_runnera(c)
        _wstaw_definicje(c, "d1", klucz="x")
        _wstaw_definicje(c, "d2", klucz="y")
        return c.execute(text("SELECT count(*) FROM definicja")).one()[0]

    assert _z_polaczeniem(zapytaj) == 2


# --- HAR-79: projekt zespolu (tabela `project`, kolumna `run.project_id`) ---


def _wstaw_projekt(c, id_projektu: str = "p1", sciezka: str = "/repo", *, stos: str | None = None):
    """Surowy INSERT: bez `stack`, gdy `stos` to None (sprawdza domyslna wartosc w bazie)."""
    _wstaw_runnera(c)
    _wstaw_projekt_obok(c, id_projektu, sciezka, stos=stos)


def _wstaw_projekt_obok(c, id_projektu: str, sciezka: str, *, stos: str | None = None) -> None:
    kolumna_stosu = ", stack" if stos is not None else ""
    wartosc_stosu = ", CAST(:stos AS json)" if stos is not None else ""
    c.execute(
        text(
            "INSERT INTO project (id, tenant_id, runner_id, path, name, created_at"
            + kolumna_stosu + ") VALUES (:id, 't1', 'rn1', :sciezka, 'repo', now()"
            + wartosc_stosu + ")"
        ),
        {"id": id_projektu, "sciezka": sciezka, **({"stos": stos} if stos is not None else {})},
    )


def test_projekt_zapisany_bez_stosu_ma_w_bazie_pusta_liste_stosu(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_projekt(c)
        return c.execute(text("SELECT CAST(stack AS text) FROM project WHERE id = 'p1'")).one()[0]

    assert _z_polaczeniem(zapytaj) == "[]"


def test_stos_projektu_zapisany_w_bazie_wraca_jako_ta_sama_lista(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_projekt(c, stos='["python"]')
        return c.execute(text("SELECT CAST(stack AS text) FROM project WHERE id = 'p1'")).one()[0]

    assert _z_polaczeniem(zapytaj) == '["python"]'


def test_ten_sam_katalog_tego_samego_runnera_nie_moze_byc_projektem_dwa_razy(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_projekt(c, "p1", "/repo")
        _wstaw_projekt_obok(c, "p2", "/repo")

    with pytest.raises(IntegrityError, match="uq_project_runner_path"):
        _z_polaczeniem(zapytaj)


def test_inny_katalog_tego_samego_runnera_mieszka_obok_pierwszego_projektu(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_projekt(c, "p1", "/repo")
        _wstaw_projekt_obok(c, "p2", "/inne")
        return c.execute(text("SELECT count(*) FROM project")).one()[0]

    assert _z_polaczeniem(zapytaj) == 2


def test_run_bez_projektu_jest_poprawny_a_kolumna_projektu_dopuszcza_brak(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_zespol(c)
        c.execute(Run.__table__.insert().values(id="r1", tenant_id="t1"))
        return c.execute(text("SELECT project_id FROM run WHERE id = 'r1'")).one()[0]

    assert _z_polaczeniem(zapytaj) is None


def test_run_nie_moze_wskazywac_nieistniejacego_projektu(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_zespol(c)
        c.execute(Run.__table__.insert().values(id="r1", tenant_id="t1", project_id="nie-ma"))

    with pytest.raises(IntegrityError):
        _z_polaczeniem(zapytaj)


def test_cofniecie_do_migracji_0009_usuwa_tabele_projektu_i_kolumne_runu(cfg):
    command.upgrade(cfg, "head")
    tabele, _ = _tabele_i_typy_w_public()
    assert "project" in tabele  # kontrast: przed cofnieciem tabela jest

    command.downgrade(cfg, "0009")

    tabele, _ = _tabele_i_typy_w_public()
    assert "project" not in tabele
    assert "run" in tabele

    def kolumny(c):
        return {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'run'"))}

    assert "project_id" not in _z_polaczeniem(kolumny)


# --- HAR-78: stan krokow (tabela `step_run`, kolumna `event.attempt`) ---


def _wstaw_run_do_krokow(c) -> None:
    _wstaw_zespol(c)
    c.execute(Run.__table__.insert().values(id="r1", tenant_id="t1"))


def _wstaw_krok_bez_agenta_i_proby(c) -> None:
    _wstaw_run_do_krokow(c)
    c.execute(
        StepRun.__table__.insert().values(
            id="k1", tenant_id="t1", run_id="r1", step_id="s", status=StepStatus.running,
        )
    )


def test_migracja_stanu_krokow_tworzy_tabele_kolumne_proby_i_unikalnosc_klucza(cfg):
    command.upgrade(cfg, "0008")
    tabele, _ = _tabele_i_typy_w_public()
    assert "step_run" not in tabele  # kontrast: przed migracja tabeli nie ma
    assert "attempt" not in _kolumny_tabeli("event")

    command.upgrade(cfg, "0009")

    tabele, typy = _tabele_i_typy_w_public()
    assert "step_run" in tabele
    assert "stepstatus" in typy
    assert "attempt" in _kolumny_tabeli("event")
    assert _wartosci_enuma("stepstatus") == {m.name for m in StepStatus}

    def zapytaj(c):
        _wstaw_run_do_krokow(c)
        for _ in range(2):
            c.execute(
                StepRun.__table__.insert().values(
                    tenant_id="t1", run_id="r1", step_id="s", agent_id="a", attempt=1,
                    status=StepStatus.running,
                )
            )

    with pytest.raises(IntegrityError, match="uq_step_run_klucz"):
        _z_polaczeniem(zapytaj)


def test_cofniecie_stanu_krokow_do_0008_usuwa_tabele_kolumne_i_typ(cfg):
    command.upgrade(cfg, "head")
    tabele, typy = _tabele_i_typy_w_public()
    assert "step_run" in tabele and "stepstatus" in typy  # kontrast
    assert "attempt" in _kolumny_tabeli("event")

    command.downgrade(cfg, "0008")

    tabele, typy = _tabele_i_typy_w_public()
    assert "step_run" not in tabele
    assert "stepstatus" not in typy
    assert "attempt" not in _kolumny_tabeli("event")
    # cofniecie tylko jednej migracji: reszta schematu zostaje
    assert (TABELE_MODELU - {"step_run", "project"}) <= tabele  # project powstaje w 0010
    assert (TYPY_ENUM - {"stepstatus"}) <= typy


def test_migracja_nadaje_krokowi_bez_agenta_agenta_pustego_napisem(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_krok_bez_agenta_i_proby(c)
        return c.execute(text("SELECT agent_id FROM step_run WHERE id = 'k1'")).one()[0]

    assert _z_polaczeniem(zapytaj) == ""


def test_migracja_nadaje_krokowi_bez_proby_probe_pierwsza(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_krok_bez_agenta_i_proby(c)
        return c.execute(text("SELECT attempt FROM step_run WHERE id = 'k1'")).one()[0]

    assert _z_polaczeniem(zapytaj) == 1


def test_migracja_zostawia_zdarzeniu_bez_proby_probe_nieznana_a_nie_pierwsza(cfg):
    command.upgrade(cfg, "head")

    def zapytaj(c):
        _wstaw_run_do_krokow(c)
        c.execute(
            Event.__table__.insert().values(
                id="e1", tenant_id="t1", run_id="r1", seq=1, kind="k"
            )
        )
        return c.execute(text("SELECT attempt FROM event WHERE id = 'e1'")).one()[0]

    assert _z_polaczeniem(zapytaj) is None


# --- HAR-172: status runu `rejected` (odrzucony na bramce) ---


def _wstaw_run_ze_statusem(c, id_runu: str, status: str, powod_sql: str = "NULL") -> None:
    """Surowy SQL, nie model: test nie zalezy od wartosci enuma w `RunStatus`."""
    c.execute(
        text(
            "INSERT INTO run (id, tenant_id, runner_kind, workflow, status, reason, created_at) "
            f"VALUES ('{id_runu}', 't1', 'local', 'wf', '{status}', {powod_sql}, now())"
        )
    )


def _statusy_i_powody_runow() -> dict[str, tuple[str, str | None]]:
    def zapytaj(c):
        wiersze = c.execute(text("SELECT id, status::text, reason FROM run ORDER BY id"))
        return {w[0]: (w[1], w[2]) for w in wiersze}

    return _z_polaczeniem(zapytaj)


def test_migracja_run_odrzucony_dodaje_wartosc_rejected_do_typu_statusu_runu(cfg):
    command.upgrade(cfg, "0010")
    assert "rejected" not in _wartosci_enuma("runstatus")  # kontrast: przed migracja wartosci nie ma

    command.upgrade(cfg, "0011")

    assert "rejected" in _wartosci_enuma("runstatus")
    assert _wartosci_enuma("runstatus") == {m.name for m in RunStatus}


def test_po_migracji_run_odrzucony_zapisuje_sie_z_powodem_i_wraca_z_bazy(cfg):
    command.upgrade(cfg, "head")

    def wstaw(c):
        _wstaw_zespol(c)
        _wstaw_run_ze_statusem(c, "r1", "rejected", "'nie ten plan'")
        c.commit()

    _z_polaczeniem(wstaw)

    assert _statusy_i_powody_runow() == {"r1": ("rejected", "nie ten plan")}


def test_migracja_run_odrzucony_przechodzi_gdy_wartosc_rejected_juz_istnieje_w_typie(cfg):
    command.upgrade(cfg, "0010")

    def dodaj_recznie(c):
        c.execute(text("ALTER TYPE runstatus ADD VALUE 'rejected'"))
        c.commit()

    _z_polaczeniem(dodaj_recznie)

    command.upgrade(cfg, "0011")                   # bez IF NOT EXISTS: duplicate_object

    assert "rejected" in _wartosci_enuma("runstatus")


def test_cofniecie_migracji_run_odrzucony_zamienia_odrzucone_runy_na_failed_i_usuwa_wartosc_z_typu(cfg):
    command.upgrade(cfg, "head")

    def wstaw(c):
        _wstaw_zespol(c)
        _wstaw_run_ze_statusem(c, "r1", "rejected", "'nie ten plan'")
        _wstaw_run_ze_statusem(c, "r2", "to_accept")
        _wstaw_run_ze_statusem(c, "r3", "failed", "'nieudane kroki: a'")
        c.commit()

    _z_polaczeniem(wstaw)

    command.downgrade(cfg, "0010")

    # tylko `rejected` zmienia status; powod zostaje; pozostale runy bez zmian
    assert _statusy_i_powody_runow() == {
        "r1": ("failed", "nie ten plan"),
        "r2": ("to_accept", None),
        "r3": ("failed", "nieudane kroki: a"),
    }
    assert _wartosci_enuma("runstatus") == {m.name for m in RunStatus} - {"rejected"}
    _, typy = _tabele_i_typy_w_public()
    assert "runstatus" in typy and "runstatus_old" not in typy   # typ przebudowany, bez smieci

    def typ_kolumny(c):
        return c.execute(text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'run' AND column_name = 'status'")).one()[0]

    assert _z_polaczeniem(typ_kolumny) == "runstatus"


# --- HAR-178: pokrycie bramkami w rekordzie runu ---

KOLUMNY_POKRYCIA = {"bramki_zaplanowane", "bramki_wykonane", "bramki_pominiete",
                    "stan_pokrycia", "bramki_powody"}


def test_migracja_pokrycia_bramek_dodaje_do_runu_piec_kolumn_ktore_dopuszczaja_brak(cfg):
    command.upgrade(cfg, "0011")
    assert not KOLUMNY_POKRYCIA & set(_kolumny_tabeli("run"))     # kontrast: przed migracja

    command.upgrade(cfg, "head")

    kolumny = _kolumny_tabeli("run")
    assert {k: kolumny.get(k) for k in KOLUMNY_POKRYCIA} == {k: "YES" for k in KOLUMNY_POKRYCIA}


def test_run_dodany_przed_migracja_pokrycia_ma_po_niej_brak_pomiaru_a_nie_zera(cfg):
    command.upgrade(cfg, "0011")

    def wstaw(c):
        _wstaw_zespol(c)
        _wstaw_run_ze_statusem(c, "r1", "to_accept")
        c.commit()

    _z_polaczeniem(wstaw)

    command.upgrade(cfg, "head")

    def zapytaj(c):
        return tuple(c.execute(text(
            "SELECT bramki_zaplanowane, bramki_wykonane, bramki_pominiete, stan_pokrycia, "
            "bramki_powody::text FROM run WHERE id = 'r1'")).one())

    assert _z_polaczeniem(zapytaj) == (None, None, None, None, None)


def test_cofniecie_migracji_pokrycia_usuwa_piec_kolumn_i_zostawia_run(cfg):
    command.upgrade(cfg, "head")

    def wstaw(c):
        _wstaw_zespol(c)
        _wstaw_run_ze_statusem(c, "r1", "to_accept")
        c.commit()

    _z_polaczeniem(wstaw)
    assert KOLUMNY_POKRYCIA <= set(_kolumny_tabeli("run"))        # kontrast: przed cofnieciem

    command.downgrade(cfg, "0011")

    assert not KOLUMNY_POKRYCIA & set(_kolumny_tabeli("run"))
    assert _statusy_i_powody_runow() == {"r1": ("to_accept", None)}

"""HAR-246: ostrzezenie o atrapie bramki w prawdziwym runie (TDD).

Prawdziwy silnik (`wykonaj_run`) i prawdziwe komendy capability; zaslepiony tylko agent. Testy
stanu runu wysylaja zdarzenia silnika do prawdziwej aplikacji (token runnera) i czytaja baze.
Ostrzezenie nic nie blokuje: komenda raz, repozytorium nietkniete, status kroku, runu i
pokrycie bez zmian.
"""
from __future__ import annotations

import shlex
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.api.schematy import EventIn
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, RunStatus, StepRun, StepStatus
from sesja_testowa import zaloz_runnera, zaloz_wlasciciela
from test_krok_check import (
    KLUCZE_PLIKU,
    KLUCZE_ZDARZENIA,
    SzpiegZmieniajacyDrzewo,
    projekt_z,
    swiat_z_checkiem,
    wczytaj_dowod,
)
from test_silnik_runu import RUN_ID, K, indeks, jedno, swiat, uruchom

PREFIKS = "atrapa bramki — "
TOKEN = "token-runnera-246"
# sciezka licznika idzie srodowiskiem: tmp_path zawiera "pytest", a tresc komendy ocenia detektor
ZMIENNA_LICZNIKA = "LICZNIK_HAR246"
LICZ = (f"import os, sys; open(os.environ['{ZMIENNA_LICZNIKA}'], 'a').write('wywolanie\\n'); "
        "sys.exit(int(sys.argv[1]))")


def w_powloce(skrypt: str) -> str:
    return f"/bin/sh -c {shlex.quote(skrypt)}"


CZYSTA = w_powloce("echo ok")
MASKUJACA = w_powloce("echo ok || true")


def ostrzezenia(zdarzenia: list[dict]) -> list[dict]:
    return [z for z in zdarzenia if z["kind"] == "warning" and z["text"].startswith(PREFIKS)]


def nazwy(zdarzenia: list[dict]) -> list[str]:
    return sorted(z["text"].removeprefix(PREFIKS).split(":", 1)[0] for z in ostrzezenia(zdarzenia))


# --- jedno wywolanie komendy -----------------------------------------------------------------


@pytest.mark.parametrize(("kod", "koniec"), [(0, "step_finished"), (5, "step_failed")])
async def test_komenda_bramki_z_ostrzezeniem_jest_wywolana_dokladnie_raz_na_krok(
    tmp_path, monkeypatch, kod, koniec,
):
    licznik = tmp_path / "licznik"
    monkeypatch.setenv(ZMIENNA_LICZNIKA, str(licznik))
    argumenty = [sys.executable, "-c", LICZ, str(kod)]
    # `exit $?` zachowuje kod, a `|| :` w pierwszej galezi daje ostrzezenie do zgloszenia
    skrypt = f"false || : ; {shlex.join(argumenty)}; k=$?; echo wynik; exit $k"
    s = swiat_z_checkiem(tmp_path, typecheck=w_powloce(skrypt))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert nazwy(zdarzenia) == ["kod wyjścia wymuszony na zero"]
    jedno(zdarzenia, koniec, "typecheck")
    assert licznik.read_text() == "wywolanie\n"


# --- repozytorium nietkniete -----------------------------------------------------------------

SKRYPT_ATRAPY = "echo 'Error: no test specified'\nexit 0\n"


def migawka(korzen: Path) -> dict[str, tuple]:
    return {
        str(p.relative_to(korzen)): (
            p.lstat().st_mode, p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in [korzen, *korzen.rglob("*")]
    }


@dataclass
class AutorZeSkryptem(SzpiegZmieniajacyDrzewo):
    """Autor zostawia skrypt bramki i zapamietuje drzewo tuz przed krokiem `check`."""

    po_autorze: dict | None = None

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, **dodatkowe):
        wynik = await super().__call__(
            spec, polecenie, katalog, emit, katalog_runu=katalog_runu, **dodatkowe)
        if spec.step_id == "autor":
            (katalog / "sprawdz.sh").write_text(SKRYPT_ATRAPY, encoding="utf-8")
            self.po_autorze = migawka(katalog)
        return wynik


async def test_ocena_atrapy_zostawia_drzewo_runu_i_repozytorium_uzytkownika_identyczne(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck="sh sprawdz.sh")
    repo_przed = migawka(s.repo / ".cockpit")
    pliki_przed = sorted(p.name for p in s.repo.iterdir())
    autor = AutorZeSkryptem()

    zdarzenia = await uruchom(s, autor)

    # oba sygnaly: z wyjscia komendy i z tresci pliku, wiec plik zostal przeczytany
    assert nazwy(zdarzenia) == ["atrapa npm init", "kod wyjścia wymuszony na zero"]
    assert autor.po_autorze is not None
    assert migawka(s.worktree) == autor.po_autorze
    assert migawka(s.repo / ".cockpit") == repo_przed
    assert sorted(p.name for p in s.repo.iterdir()) == pliki_przed


# --- zdarzenie ostrzezenia -------------------------------------------------------------------


async def test_ostrzezenie_o_maskowanym_wyniku_niesie_cytat_i_poprawke_przy_kroku_bramki(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=MASKUJACA)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    (ostrzezenie,) = ostrzezenia(zdarzenia)
    assert ostrzezenie["text"] == (
        "atrapa bramki — kod wyjścia wymuszony na zero: „echo ok || true”. "
        "Co poprawić: usuń wymuszenie kodu 0, żeby błąd komendy oblewał bramkę.")
    assert (ostrzezenie["run_id"], ostrzezenie["step_id"], ostrzezenie["agent_id"],
            ostrzezenie["attempt"]) == (RUN_ID, "typecheck", "typecheck", 1)


async def test_ostrzezenie_zielonego_kroku_jest_zgloszone_przed_zdarzeniem_konca_kroku(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=MASKUJACA)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    (ostrzezenie,) = ostrzezenia(zdarzenia)
    assert zdarzenia.index(ostrzezenie) < indeks(zdarzenia, "step_finished", "typecheck")


async def test_pytest_bez_zebranych_testow_ostrzega_przed_porazka_kroku(tmp_path):
    komenda = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
    s = swiat_z_checkiem(tmp_path, typecheck=komenda)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert wczytaj_dowod(s.katalog_runu / "typecheck")["exit_code"] == 5
    assert "brak zebranych testów" in nazwy(zdarzenia)
    pierwsze = ostrzezenia(zdarzenia)[0]
    assert zdarzenia.index(pierwsze) < indeks(zdarzenia, "step_failed", "typecheck")


async def test_komenda_ktora_nie_ruszyla_nadal_ostrzega_o_atrapie_z_jej_tresci(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck="nie-ma-programu-har246 --passWithNoTests")

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert nazwy(zdarzenia) == ["testy przechodzą bez testów"]
    (ostrzezenie,) = ostrzezenia(zdarzenia)
    assert zdarzenia.index(ostrzezenie) < indeks(zdarzenia, "step_failed", "typecheck")


async def test_kazda_usterka_bramki_to_osobne_ostrzezenie(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=w_powloce("echo --passWithNoTests || true"))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert nazwy(zdarzenia) == ["kod wyjścia wymuszony na zero", "testy przechodzą bez testów"]


async def test_bramka_bez_usterki_nie_zglasza_ostrzezenia_atrapy(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=CZYSTA)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    jedno(zdarzenia, "step_finished", "typecheck")
    assert ostrzezenia(zdarzenia) == []


async def test_sygnaly_nie_trafiaja_do_dowodu_a_kazde_zdarzenie_przyjmie_kokpit(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=MASKUJACA)
    przyjete: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        EventIn.model_validate({"event_id": "e-1", **zdarzenie})
        przyjete.append(zdarzenie)

    await uruchom(s, SzpiegZmieniajacyDrzewo(), emit=emit)

    assert len(ostrzezenia(przyjete)) == 1
    assert set(jedno(przyjete, "step_finished", "typecheck")["dowod"]) == KLUCZE_ZDARZENIA
    assert set(wczytaj_dowod(s.katalog_runu / "typecheck")) == KLUCZE_PLIKU


# --- co dokladnie trafia do oceny ------------------------------------------------------------


async def test_kod_wyjscia_piec_bez_slowa_o_testach_w_wyjsciu_nadal_ostrzega_o_braku_testow(
    tmp_path,
):
    narzedzie = tmp_path / "narzedzia" / "pytest"
    narzedzie.parent.mkdir()
    narzedzie.write_text("#!/bin/sh\necho cokolwiek\nexit 5\n", encoding="utf-8")
    narzedzie.chmod(0o755)
    s = swiat_z_checkiem(tmp_path, typecheck=shlex.quote(str(narzedzie)))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert nazwy(zdarzenia) == ["brak zebranych testów"]


async def test_wyjscie_bramki_spoza_utf8_nie_odbiera_krokowi_wyniku_ani_ostrzezenia(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=w_powloce("printf '\\377\\n' || true"))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    jedno(zdarzenia, "step_finished", "typecheck")
    assert nazwy(zdarzenia) == ["kod wyjścia wymuszony na zero"]


WORKFLOW_Z_PONOWIENIEM = """\
name: w
steps:
  autor: {skill: s_autor}
  typecheck: {system: check, capability: typecheck, needs: [autor], on_fail: {retry: autor, max: 1}}
"""


async def test_ostrzezenie_kazdej_proby_bramki_niesie_numer_tej_proby(tmp_path):
    komenda = w_powloce("false || true; echo zle; exit 1")
    s = swiat(
        tmp_path, [K("autor")], tresc=WORKFLOW_Z_PONOWIENIEM,
        project=projekt_z(typecheck=komenda))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert [(z["step_id"], z["attempt"]) for z in ostrzezenia(zdarzenia)] == [
        ("typecheck", 1), ("typecheck", 2)]


# --- ostrzezenie nic nie blokuje: stan w bazie kokpitu ---------------------------------------


@pytest.fixture
async def kokpit(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'atrapy.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _, tenant = await zaloz_wlasciciela(maker, "a@example.com", nazwa_zespolu="Browarek")
    runner = await zaloz_runnera(maker, tenant, "runner-a", TOKEN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as klient:

        async def przebieg(katalog: Path, komenda: str) -> tuple:
            """Run silnika ze zdarzeniami w prawdziwej aplikacji; (status, kroki, pokrycie,
            ostrzezenia) odczytane z bazy."""
            run_id = str(uuid.uuid4())
            async with maker() as s:
                s.add(Run(id=run_id, tenant_id=tenant, runner_id=runner, status=RunStatus.running))
                await s.commit()
            katalog.mkdir()
            sw = swiat_z_checkiem(katalog, typecheck=komenda)

            async def emit(zdarzenie: dict) -> None:
                odp = await klient.post(
                    "/api/events", json={"event_id": str(uuid.uuid4()), **zdarzenie},
                    headers={"Authorization": f"Bearer {TOKEN}"})
                assert odp.status_code == 200, odp.text

            await uruchom(
                sw, SzpiegZmieniajacyDrzewo(), emit=emit,
                polecenie={**sw.polecenie, "run_id": run_id})
            async with maker() as s:
                run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
                kroki = (await s.execute(
                    select(StepRun).where(StepRun.run_id == run_id)
                    .order_by(StepRun.step_id, StepRun.agent_id, StepRun.attempt))).scalars().all()
                teksty = (await s.execute(
                    select(Event.text).where(Event.run_id == run_id, Event.kind == "warning",
                                             Event.step_id == "typecheck"))).scalars().all()
            return (
                run.status,
                [(k.step_id, k.agent_id, k.attempt, k.status, k.reason) for k in kroki],
                (run.bramki_zaplanowane, run.bramki_wykonane, run.bramki_pominiete,
                 run.bramki_powody, run.stan_pokrycia),
                [t for t in teksty if t.startswith(PREFIKS)],
            )

        yield przebieg
    await engine.dispose()


async def test_run_z_ostrzezeniem_o_atrapie_jest_do_odbioru_z_tym_samym_stanem_krokow_i_pokryciem(
    kokpit, tmp_path,
):
    status_czysty, kroki_czyste, pokrycie_czyste, bez = await kokpit(tmp_path / "czysty", CZYSTA)
    status, kroki, pokrycie, teksty = await kokpit(tmp_path / "atrapa", MASKUJACA)

    assert bez == []
    assert len(teksty) == 1
    assert status == status_czysty == RunStatus.to_accept
    assert kroki == kroki_czyste
    assert ("typecheck", "typecheck", 1, StepStatus.finished) in [k[:4] for k in kroki]
    assert pokrycie == pokrycie_czyste
    assert pokrycie[:3] == (2, 2, 0)

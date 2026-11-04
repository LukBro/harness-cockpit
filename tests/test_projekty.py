"""HAR-79: projekt zespolu - katalog repozytorium na maszynie z runnerem, stos wykrywa runner.

Warstwy testow (jak w test_katalog_definicji.py):
 1. operacja runnera `detect_stack` na prawdziwych katalogach w katalogu testu;
 2. serwis kokpitu (`cockpit.services.projekty`) z wstrzykniegta wysylka; skutek czytany z bazy;
 3. przelot HTTP: prawdziwa aplikacja, odpowiedz runnera podstawiona w `bus.zapytaj`.
Zespol B jest zakladany PIERWSZY, A drugi: odrozniamy "pierwszy w bazie" od "z sesji".
Modul uslugi i model `Project` importujemy leniwie, zeby brak implementacji czerwienil
kazdy test osobno, a nie wylozyl zbierania calego pliku.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from transport_asgi import TransportASGIStrumieniujacy

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, RolaWZespole, Run, RunStatus, Runner, Tenant
from cockpit.services import zapytania_runnera as zapytania
from cockpit.services.errors import (
    NieZnaleziono,
    PlikPozaZakresem,
    RunnerOffline,
    StanNiemozliwy,
)
from cockpit.services.runs import runy_zespolu
from runner import operacje
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_runnera, zaloz_wlasciciela

KOMUNIKAT_OFFLINE = "runner offline - uruchom runnera na swojej maszynie (cockpit-runner)"
KOMUNIKAT_POZA_ZAKRESEM = "sciezka poza zakresem runnera"
KOMUNIKAT_DUPLIKATU = "ten katalog jest juz projektem zespolu na tym runnerze"
PREFIKS_ZLEGO_KSZTALTU = "odpowiedz runnera ma zly ksztalt"
SEKRET = "zxqv7-SEKRET-token-482913"
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
POLACZONE_A = frozenset({"runner-a"})

ZNACZNIKI = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "package.json": "node",
    "Gemfile": "ruby",
    "go.mod": "go",
    "Cargo.toml": "rust",
}


def _projekty_uslugi():
    from cockpit.services import projekty
    return projekty


def _model_projektu():
    from cockpit.db import models
    return models.Project


# =================================================================================
# 1. Runner: operacja `detect_stack`
# =================================================================================

@pytest.fixture
def projekt(tmp_path, monkeypatch):
    """Projekt w zakresie runnera i obcy katalog poza nim. Sciezki rozwiazane
    (macOS: /tmp to dowiazanie do /private/tmp)."""
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


async def _wyk(**dane) -> dict:
    return await operacje.wykonaj({"type": "detect_stack", "req_id": "r-1", **dane})


def _blad(odp: dict, kod: str) -> None:
    assert odp["ok"] is False, odp
    assert odp["kod"] == kod, odp
    assert odp["req_id"] == "r-1"
    assert isinstance(odp["blad"], str) and odp["blad"]


def _katalog_z_plikami(rodzic: Path, nazwa: str, *pliki: str) -> Path:
    katalog = rodzic / nazwa
    katalog.mkdir(parents=True, exist_ok=True)
    for p in pliki:
        (katalog / p).write_text("", encoding="utf-8")
    return katalog


async def _stos(katalog: Path) -> list[str]:
    odp = await _wyk(sciezka=str(katalog))
    assert odp["ok"] is True, odp
    return odp["wynik"]["stack"]


def test_runner_zna_operacje_wykrywania_stosu():
    assert "detect_stack" in operacje.OPERACJE


def test_tabela_znacznikow_stosu_ma_dokladnie_siedem_znanych_plikow():
    assert dict(operacje.ZNACZNIKI_STOSU) == ZNACZNIKI


@pytest.mark.parametrize(("plik", "stos"), list(ZNACZNIKI.items()), ids=list(ZNACZNIKI))
async def test_sam_znacznik_w_katalogu_wystarcza_do_wykrycia_swojego_stosu(projekt, plik, stos):
    katalog = _katalog_z_plikami(projekt.korzen, "app", plik)

    assert await _stos(katalog) == [stos]


async def test_katalog_bez_znacznikow_ma_pusty_stos_i_to_nie_jest_blad(projekt):
    katalog = _katalog_z_plikami(projekt.korzen, "app", "README.md", "main.py", "Makefile")

    odp = await _wyk(sciezka=str(katalog))

    assert odp["ok"] is True
    assert odp["wynik"]["stack"] == []


async def test_pusty_katalog_ma_pusty_stos(projekt):
    katalog = _katalog_z_plikami(projekt.korzen, "pusty")

    assert await _stos(katalog) == []


async def test_znacznik_w_podkatalogu_nie_liczy_sie_do_stosu_projektu(projekt):
    katalog = _katalog_z_plikami(projekt.korzen, "app", "README.md")
    _katalog_z_plikami(katalog, "frontend", "package.json")
    _katalog_z_plikami(katalog / "frontend", "glebiej", "go.mod")

    assert await _stos(katalog) == []


async def test_stos_jest_posortowany_i_bez_powtorzen_gdy_wiele_znacznikow_daje_ten_sam_jezyk(
    projekt,
):
    katalog = _katalog_z_plikami(
        projekt.korzen, "app", "pyproject.toml", "requirements.txt", "setup.py")

    assert await _stos(katalog) == ["python"]


async def test_stos_z_wielu_jezykow_jest_posortowany_alfabetycznie(projekt):
    # kolejnosc tworzenia plikow odwrotna do alfabetycznej wyniku
    katalog = _katalog_z_plikami(
        projekt.korzen, "app", "Cargo.toml", "Gemfile", "package.json", "go.mod", "setup.py")

    assert await _stos(katalog) == ["go", "node", "python", "ruby", "rust"]


async def test_wynik_niesie_nazwe_katalogu_i_jego_sciezke(projekt):
    katalog = _katalog_z_plikami(projekt.korzen, "moj-projekt", "go.mod")

    odp = await _wyk(sciezka=str(katalog))

    assert odp["wynik"] == {"sciezka": str(katalog), "nazwa": "moj-projekt", "stack": ["go"]}


async def test_wynik_niesie_sciezke_rozwiazana_przez_dowiazanie_i_kropki(projekt):
    prawdziwy = _katalog_z_plikami(projekt.korzen, "prawdziwy", "Gemfile")
    (projekt.korzen / "alias").symlink_to(prawdziwy, target_is_directory=True)
    kreta = str(projekt.korzen / "prawdziwy" / ".." / "alias")

    po_dowiazaniu = await _wyk(sciezka=str(projekt.korzen / "alias"))
    po_kropkach = await _wyk(sciezka=kreta)

    for odp in (po_dowiazaniu, po_kropkach):
        assert odp["wynik"]["sciezka"] == str(prawdziwy)
        assert odp["wynik"]["nazwa"] == "prawdziwy"
        assert odp["wynik"]["stack"] == ["ruby"]


async def test_katalog_poza_zakresem_runnera_jest_odrzucony_choc_ma_znaczniki(projekt):
    obcy = _katalog_z_plikami(projekt.obcy, "cudze", "package.json")

    _blad(await _wyk(sciezka=str(obcy)), "poza_zakresem")


async def test_odpowiedz_poza_zakresem_ma_staly_komunikat_bez_sciezki(projekt):
    obcy = _katalog_z_plikami(projekt.obcy, "cudze", "package.json")

    odp = await _wyk(sciezka=str(obcy))

    assert odp["blad"] == KOMUNIKAT_POZA_ZAKRESEM
    assert str(obcy) not in json.dumps(odp)


async def test_dowiazanie_z_zakresu_do_katalogu_poza_zakresem_jest_odrzucone(projekt):
    obcy = _katalog_z_plikami(projekt.obcy, "cudze", "package.json")
    (projekt.korzen / "furtka").symlink_to(obcy, target_is_directory=True)

    _blad(await _wyk(sciezka=str(projekt.korzen / "furtka")), "poza_zakresem")


async def test_sciezka_wzgledna_jest_odrzucona_jak_wszystkie_operacje_runnera(projekt):
    _katalog_z_plikami(projekt.korzen, "app", "package.json")

    _blad(await _wyk(sciezka="app"), "poza_zakresem")


async def test_plik_zamiast_katalogu_to_blad_nie_katalog(projekt):
    plik = projekt.korzen / "plik.txt"
    plik.write_text("x", encoding="utf-8")

    odp = await _wyk(sciezka=str(plik))

    _blad(odp, "nie_katalog")
    assert odp["blad"] == "sciezka nie jest katalogiem"


async def test_nieistniejacy_katalog_to_blad_nie_istnieje(projekt):
    _blad(await _wyk(sciezka=str(projekt.korzen / "nie-ma")), "nie_istnieje")


@pytest.mark.parametrize("dane", [{}, {"sciezka": 5}, {"sciezka": None}],
                         ids=["brak_pola", "liczba", "null"])
async def test_wykrywanie_stosu_bez_sciezki_tekstowej_to_blad_zle_dane(projekt, dane):
    _blad(await _wyk(**dane), "zle_dane")


async def test_blad_systemu_przy_przegladaniu_katalogu_to_brak_uprawnien_bez_tresci_wyjatku(
    projekt, monkeypatch,
):
    katalog = _katalog_z_plikami(projekt.korzen, "zamkniety", "package.json")
    prawdziwy_scandir = os.scandir

    def scandir_z_odmowa(sciezka="."):
        if str(sciezka) == str(katalog):
            raise PermissionError(errno.EACCES, f"odmowa {SEKRET}")
        return prawdziwy_scandir(sciezka)

    monkeypatch.setattr(os, "scandir", scandir_z_odmowa)

    odp = await _wyk(sciezka=str(katalog))

    _blad(odp, "brak_uprawnien")
    assert SEKRET not in json.dumps(odp)


async def test_katalog_czytany_jednym_przegladem_korzenia_bez_zagladania_w_podkatalogi(
    projekt, monkeypatch,
):
    katalog = _katalog_z_plikami(projekt.korzen, "app", "package.json")
    _katalog_z_plikami(katalog, "sub", "go.mod")
    prawdziwy_scandir = os.scandir
    przejrzane: list[str] = []

    def liczacy_scandir(sciezka="."):
        przejrzane.append(str(sciezka))
        return prawdziwy_scandir(sciezka)

    monkeypatch.setattr(os, "scandir", liczacy_scandir)

    assert await _stos(katalog) == ["node"]
    assert przejrzane == [str(katalog)]


# =================================================================================
# 2. Serwis kokpitu: `cockpit.services.projekty`
# =================================================================================

@pytest.fixture
async def zespoly(tmp_path):
    """Dwa zespoly (B zalozony pierwszy); A ma runnery runner-a i runner-z, B ma runner-b."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'serwis.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        b, a = Tenant(name="Beta"), Tenant(name="Alfa")
        s.add(b)
        await s.flush()
        s.add(a)
        await s.flush()
        teraz = datetime.now(timezone.utc)
        s.add(Runner(id="runner-b", tenant_id=b.id, name="b", token_hash="y", last_seen_at=teraz))
        s.add(Runner(id="runner-a", tenant_id=a.id, name="a", token_hash="x", last_seen_at=teraz))
        s.add(Runner(id="runner-z", tenant_id=a.id, name="z", token_hash="w", last_seen_at=teraz))
        await s.commit()
        ids = SimpleNamespace(a=a.id, b=b.id)
    yield SimpleNamespace(maker=maker, ids=ids)
    await engine.dispose()


class _Wysylka:
    """Wstrzykiwana `wyslij`: zapamietuje wywolania i oddaje zadana odpowiedz (albo rzuca)."""

    def __init__(self, odpowiedz: dict | None = None, *, wyjatek: Exception | None = None) -> None:
        self.odpowiedz = odpowiedz
        self.wyjatek = wyjatek
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane))
        if self.wyjatek is not None:
            raise self.wyjatek
        return self.odpowiedz


def _ok(sciezka: str = "/repo/app", nazwa: str = "app", stack=("python",)) -> dict:
    return {"ok": True, "wynik": {"sciezka": sciezka, "nazwa": nazwa, "stack": list(stack)}}


async def _podlacz(zespoly, tenant_id, runner_id, path, odpowiedz=None, *, polaczone=POLACZONE_A,
                   wysylka=None):
    """(wynik albo wyjatek, wysylka); wyjatek zwracany, zeby test sprawdzal jego dokladny typ."""
    wysylka = wysylka or _Wysylka(odpowiedz)
    async with zespoly.maker() as s:
        try:
            wynik = await _projekty_uslugi().podlacz_projekt(
                s, tenant_id, runner_id, path, polaczone, wysylka)
        except Exception as e:
            return e, wysylka
    return wynik, wysylka


async def _w_bazie(maker) -> list[dict]:
    Project = _model_projektu()
    async with maker() as s:
        wiersze = (await s.execute(select(Project))).scalars().all()
    return sorted(({c: getattr(w, c) for c in (
        "id", "tenant_id", "runner_id", "path", "name", "stack")} for w in wiersze),
        key=lambda w: (w["tenant_id"], w["runner_id"], w["path"]))


async def _dodaj_projekt(maker, tenant_id, runner_id, path, *, id=None, name="app", stack=(),
                         created_at=T0) -> str:
    Project = _model_projektu()
    id_projektu = id or str(uuid.uuid4())
    async with maker() as s:
        s.add(Project(id=id_projektu, tenant_id=tenant_id, runner_id=runner_id, path=path,
                      name=name, stack=list(stack), created_at=created_at))
        await s.commit()
    return id_projektu


# ---- zapis ---------------------------------------------------------------------------------

async def test_podlaczony_projekt_trafia_do_bazy_z_wykrytym_stosem_nazwa_i_sciezka(zespoly):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app",
                              _ok("/repo/app", "app", ("node", "python")))

    assert not isinstance(wynik, Exception), wynik
    wiersze = await _w_bazie(zespoly.maker)
    assert wiersze == [{
        "id": wynik["id"], "tenant_id": zespoly.ids.a, "runner_id": "runner-a",
        "path": "/repo/app", "name": "app", "stack": ["node", "python"]}]


async def test_runner_dostaje_zapytanie_detect_stack_z_podana_sciezka(zespoly):
    _, wysylka = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", _ok())

    assert wysylka.wywolania == [("runner-a", "detect_stack", {"sciezka": "/repo/app"})]


async def test_podlaczenie_zwraca_projekt_z_tymi_samymi_polami_co_w_bazie(zespoly):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", _ok())

    [wiersz] = await _w_bazie(zespoly.maker)
    assert set(wynik) == {"id", "runner_id", "path", "name", "stack", "created_at"}
    assert (wynik["id"], wynik["runner_id"], wynik["path"], wynik["name"], wynik["stack"]) == (
        wiersz["id"], "runner-a", "/repo/app", "app", ["python"])
    utworzony = datetime.fromisoformat(wynik["created_at"])
    assert utworzony.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - utworzony) < timedelta(minutes=5)
    # data pochodzi z modelu i jest zapisana w bazie (SQLite oddaje ja bez strefy - to UTC)
    async with zespoly.maker() as s:
        w_bazie = (await s.execute(select(_model_projektu().created_at))).scalar_one()
    assert abs(datetime.now(timezone.utc) - w_bazie.replace(tzinfo=timezone.utc)) < timedelta(
        minutes=5)


async def test_projekt_zapisuje_sciezke_i_nazwe_z_odpowiedzi_runnera_a_nie_te_wyslane(zespoly):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/alias",
                              _ok("/prywatne/repo/prawdziwy", "prawdziwy"))

    [wiersz] = await _w_bazie(zespoly.maker)
    assert (wiersz["path"], wiersz["name"]) == ("/prywatne/repo/prawdziwy", "prawdziwy")
    assert (wynik["path"], wynik["name"]) == ("/prywatne/repo/prawdziwy", "prawdziwy")


async def test_katalog_bez_znacznikow_jest_projektem_z_pustym_stosem(zespoly):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app",
                              _ok(stack=()))

    assert wynik["stack"] == []
    [wiersz] = await _w_bazie(zespoly.maker)
    assert wiersz["stack"] == []


async def test_nazwa_dlugosci_kolumny_i_sciezka_dlugosci_kolumny_sa_zapisane(zespoly):
    nazwa, sciezka = "n" * 200, "/" + "p" * 4095

    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/x", _ok(sciezka, nazwa))

    assert not isinstance(wynik, Exception), wynik
    [wiersz] = await _w_bazie(zespoly.maker)
    assert (wiersz["name"], wiersz["path"]) == (nazwa, sciezka)


# ---- zespol i runner -------------------------------------------------------------------------

async def test_runner_innego_zespolu_nie_podlacza_projektu_a_nikt_nie_jest_pytany(zespoly):
    wynik, wysylka = await _podlacz(
        zespoly, zespoly.ids.a, "runner-b", "/repo/app", _ok(),
        polaczone=frozenset({"runner-a", "runner-b"}))

    assert type(wynik) is NieZnaleziono
    assert str(wynik) == "runner nie istnieje"
    assert wysylka.wywolania == []
    assert await _w_bazie(zespoly.maker) == []


async def test_nieistniejacy_runner_to_nie_znaleziono_bez_zapytania_i_zapisu(zespoly):
    wynik, wysylka = await _podlacz(zespoly, zespoly.ids.a, "nie-ma-takiego", "/repo/app", _ok())

    assert type(wynik) is NieZnaleziono
    assert str(wynik) == "runner nie istnieje"
    assert wysylka.wywolania == []
    assert await _w_bazie(zespoly.maker) == []


async def test_runner_offline_daje_blad_z_podpowiedzia_a_projekt_nie_powstaje(zespoly):
    wynik, wysylka = await _podlacz(
        zespoly, zespoly.ids.a, "runner-a", "/repo/app", _ok(), polaczone=frozenset())

    assert type(wynik) is RunnerOffline
    assert str(wynik) == KOMUNIKAT_OFFLINE
    assert wysylka.wywolania == []
    assert await _w_bazie(zespoly.maker) == []


async def test_runner_z_przeterminowanym_heartbeatem_jest_offline_mimo_otwartego_strumienia(
    zespoly,
):
    async with zespoly.maker() as s:
        r = await s.get(Runner, "runner-a")
        r.last_seen_at = datetime.now(timezone.utc) - timedelta(days=1)
        await s.commit()

    wynik, wysylka = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", _ok())

    assert type(wynik) is RunnerOffline
    assert wysylka.wywolania == []


async def test_wskazany_runner_offline_nie_jest_zastepowany_innym_runnerem_zespolu(zespoly):
    wynik, wysylka = await _podlacz(
        zespoly, zespoly.ids.a, "runner-a", "/repo/app", _ok(),
        polaczone=frozenset({"runner-z"}))   # online jest tylko drugi runner zespolu

    assert type(wynik) is RunnerOffline
    assert wysylka.wywolania == []
    assert await _w_bazie(zespoly.maker) == []


async def test_projekt_powstaje_na_wskazanym_runnerze_a_nie_na_pierwszym_po_id(zespoly):
    wynik, wysylka = await _podlacz(
        zespoly, zespoly.ids.a, "runner-z", "/repo/app", _ok(),
        polaczone=frozenset({"runner-a", "runner-z"}))

    assert wynik["runner_id"] == "runner-z"
    assert [w[0] for w in wysylka.wywolania] == ["runner-z"]
    [wiersz] = await _w_bazie(zespoly.maker)
    assert wiersz["runner_id"] == "runner-z"


# ---- odpowiedzi runnera: porazki -------------------------------------------------------------

async def test_odmowa_poza_zakresem_to_blad_pliku_poza_zakresem_a_projekt_nie_powstaje(zespoly):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/obce", {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"})

    assert type(wynik) is PlikPozaZakresem
    assert str(wynik) == KOMUNIKAT_POZA_ZAKRESEM
    assert await _w_bazie(zespoly.maker) == []


@pytest.mark.parametrize("kod", ["nie_istnieje", "nie_katalog", "brak_uprawnien", "blad_operacji"])
async def test_inna_porazka_runnera_to_stan_niemozliwy_z_komunikatem_runnera_bez_zapisu(
    zespoly, kod,
):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", {
        "ok": False, "blad": f"komunikat-{kod}", "kod": kod})

    assert type(wynik) is StanNiemozliwy
    assert str(wynik) == f"komunikat-{kod}"
    assert await _w_bazie(zespoly.maker) == []


async def test_porazka_runnera_bez_kodu_to_stan_niemozliwy_a_nie_wyjatek_z_kluczem(zespoly):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", {"ok": False})

    assert type(wynik) is StanNiemozliwy
    assert await _w_bazie(zespoly.maker) == []


async def test_brak_odpowiedzi_runnera_w_limicie_czasu_to_stan_niemozliwy_bez_zapisu(zespoly):
    wynik, wysylka = await _podlacz(
        zespoly, zespoly.ids.a, "runner-a", "/repo/app", wysylka=_Wysylka(wyjatek=TimeoutError()))

    assert type(wynik) is StanNiemozliwy
    assert len(wysylka.wywolania) == 1
    assert await _w_bazie(zespoly.maker) == []


ZLE_KSZTALTY = {
    "wynik_none": {"ok": True, "wynik": None},
    "wynik_lista": {"ok": True, "wynik": []},
    "wynik_napis": {"ok": True, "wynik": "python"},
    "brak_stosu": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a"}},
    "stos_napis": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a", "stack": "python"}},
    "stos_slownik": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a", "stack": {}}},
    "stos_none": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a", "stack": None}},
    "stos_z_liczba": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a", "stack": ["go", 5]}},
    "stos_z_none": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a", "stack": [None]}},
    "brak_sciezki": {"ok": True, "wynik": {"nazwa": "a", "stack": []}},
    "sciezka_pusta": {"ok": True, "wynik": {"sciezka": "", "nazwa": "a", "stack": []}},
    "sciezka_liczba": {"ok": True, "wynik": {"sciezka": 5, "nazwa": "a", "stack": []}},
    "sciezka_none": {"ok": True, "wynik": {"sciezka": None, "nazwa": "a", "stack": []}},
    "sciezka_4097_znakow": {"ok": True, "wynik": {
        "sciezka": "/" + "p" * 4096, "nazwa": "a", "stack": []}},
    "brak_nazwy": {"ok": True, "wynik": {"sciezka": "/r/a", "stack": []}},
    "nazwa_pusta": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "", "stack": []}},
    "nazwa_liczba": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": 5, "stack": []}},
    "nazwa_none": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": None, "stack": []}},
    "nazwa_201_znakow": {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "n" * 201, "stack": []}},
}


@pytest.mark.parametrize("odpowiedz", ZLE_KSZTALTY.values(), ids=ZLE_KSZTALTY.keys())
async def test_zly_ksztalt_odpowiedzi_runnera_to_stan_niemozliwy_z_opisem_i_bez_zapisu(
    zespoly, odpowiedz,
):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", odpowiedz)

    assert type(wynik) is StanNiemozliwy
    assert str(wynik).startswith(PREFIKS_ZLEGO_KSZTALTU)
    assert await _w_bazie(zespoly.maker) == []


KOMUNIKATY_ZLEGO_KSZTALTU = {
    "wynik_none": "wynik nie jest slownikiem",
    "wynik_lista": "wynik nie jest slownikiem",
    "stos_napis": "pole stack nie jest lista napisow",
    "stos_z_liczba": "pole stack nie jest lista napisow",
    "sciezka_pusta": "pole sciezka nie jest niepustym napisem do 4096 znakow",
    "sciezka_4097_znakow": "pole sciezka nie jest niepustym napisem do 4096 znakow",
    "nazwa_pusta": "pole nazwa nie jest niepustym napisem do 200 znakow",
    "nazwa_201_znakow": "pole nazwa nie jest niepustym napisem do 200 znakow",
}


@pytest.mark.parametrize("klucz", KOMUNIKATY_ZLEGO_KSZTALTU, ids=KOMUNIKATY_ZLEGO_KSZTALTU)
async def test_zly_ksztalt_odpowiedzi_runnera_wskazuje_w_komunikacie_dokladnie_co_jest_zle(
    zespoly, klucz,
):
    wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app",
                              ZLE_KSZTALTY[klucz])

    assert type(wynik) is StanNiemozliwy
    assert str(wynik) == f"{PREFIKS_ZLEGO_KSZTALTU} ({KOMUNIKATY_ZLEGO_KSZTALTU[klucz]})"


# ---- duplikaty -------------------------------------------------------------------------------

async def test_ponowne_podlaczenie_tego_samego_katalogu_to_stan_niemozliwy_i_jeden_rekord(zespoly):
    pierwszy, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app",
                                 _ok("/repo/app", "app", ("python",)))

    # inna sciezka wejsciowa (alias), ta sama rozwiazana - liczy sie rozwiazana
    drugi, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/alias",
                              _ok("/repo/app", "app", ("node",)))

    assert type(drugi) is StanNiemozliwy
    assert str(drugi) == KOMUNIKAT_DUPLIKATU
    assert await _w_bazie(zespoly.maker) == [{
        "id": pierwszy["id"], "tenant_id": zespoly.ids.a, "runner_id": "runner-a",
        "path": "/repo/app", "name": "app", "stack": ["python"]}]


async def test_ten_sam_katalog_na_innym_runnerze_zespolu_to_osobny_projekt(zespoly):
    polaczone = frozenset({"runner-a", "runner-z"})
    await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app", _ok(), polaczone=polaczone)

    drugi, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-z", "/repo/app", _ok(),
                              polaczone=polaczone)

    assert not isinstance(drugi, Exception), drugi
    assert [w["runner_id"] for w in await _w_bazie(zespoly.maker)] == ["runner-a", "runner-z"]


async def test_dwa_rozne_katalogi_tego_samego_runnera_to_dwa_projekty(zespoly):
    pierwszy, _ = await _podlacz(
        zespoly, zespoly.ids.a, "runner-a", "/repo/a", _ok("/repo/a", "a"))

    drugi, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/b", _ok("/repo/b", "b"))

    assert not isinstance(pierwszy, Exception), pierwszy
    assert not isinstance(drugi, Exception), drugi
    assert [w["path"] for w in await _w_bazie(zespoly.maker)] == ["/repo/a", "/repo/b"]


async def test_projekt_zapisany_przez_model_bez_stosu_i_daty_dostaje_pusty_stos_date_i_id(zespoly):
    Project = _model_projektu()
    async with zespoly.maker() as s:
        s.add(Project(tenant_id=zespoly.ids.a, runner_id="runner-a", path="/repo/a", name="a"))
        await s.commit()

    async with zespoly.maker() as s:
        [wiersz] = (await s.execute(select(Project))).scalars().all()
    assert wiersz.stack == []
    assert isinstance(wiersz.created_at, datetime)
    assert isinstance(wiersz.id, str) and len(wiersz.id) == 36


async def test_projekt_wstawiony_surowym_sql_bez_stosu_ma_w_bazie_pusty_stos(zespoly):
    async with zespoly.maker() as s:
        await s.execute(text(
            "INSERT INTO project (id, tenant_id, runner_id, path, name, created_at) "
            "VALUES ('p-raw', :tenant, 'runner-a', '/repo/raw', 'raw', '2030-01-01 12:00:00')"),
            {"tenant": zespoly.ids.a})
        await s.commit()

    [wiersz] = await _w_bazie(zespoly.maker)
    assert wiersz["stack"] == []


async def test_dwa_rownolegle_podlaczenia_tego_samego_katalogu_zostawiaja_jeden_projekt(zespoly):
    bariera = asyncio.Barrier(2)

    class _ZBariera(_Wysylka):
        async def __call__(self, runner_id, typ, dane, timeout_s):
            self.wywolania.append((runner_id, typ, dane))
            await asyncio.wait_for(bariera.wait(), 10)   # oba zapytania wracaja przed zapisem
            return self.odpowiedz

    async def jedno():
        wynik, _ = await _podlacz(zespoly, zespoly.ids.a, "runner-a", "/repo/app",
                                  wysylka=_ZBariera(_ok()))
        return wynik

    wyniki = await asyncio.wait_for(asyncio.gather(jedno(), jedno()), 30)

    bledy = [w for w in wyniki if isinstance(w, Exception)]
    assert [type(b) for b in bledy] == [StanNiemozliwy]
    assert len(await _w_bazie(zespoly.maker)) == 1


def _wymus_konflikt_zapisu(monkeypatch) -> None:
    """Pierwszy zapis (`flush`) kazdej sesji konczy sie `IntegrityError`, jakby baza odrzucila
    wiersz ograniczeniem; kolejne dzialaja naprawde. Kod `podlacz_projekt` jest prawdziwy."""
    prawdziwy = AsyncSession.flush
    stan = {"rzucono": False}

    async def flush(self, *args, **kwargs):
        if not stan["rzucono"]:
            stan["rzucono"] = True
            raise IntegrityError("INSERT INTO project", {}, Exception("wymuszony konflikt"))
        return await prawdziwy(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flush)


async def _podlacz_prawdziwym_kodem(zespoly, path="/repo/app"):
    async with zespoly.maker() as s:
        return await _projekty_uslugi().podlacz_projekt(
            s, zespoly.ids.a, "runner-a", path, POLACZONE_A, _Wysylka(_ok(path)))


async def test_blad_bazy_niebedacy_duplikatem_katalogu_nie_jest_udawany_za_duplikat(
    zespoly, monkeypatch,
):
    _wymus_konflikt_zapisu(monkeypatch)   # w bazie nie ma zadnego projektu

    with pytest.raises(IntegrityError):
        await _podlacz_prawdziwym_kodem(zespoly)

    assert await _w_bazie(zespoly.maker) == []


@pytest.mark.parametrize("obcy", [
    ("tenant_b", "runner-a", "/repo/app"),
    ("tenant_a", "runner-z", "/repo/app"),
    ("tenant_a", "runner-a", "/repo/inny"),
], ids=["inny_zespol", "inny_runner", "inna_sciezka"])
async def test_konflikt_zapisu_nie_jest_duplikatem_gdy_istniejacy_projekt_rozni_sie_jednym_polem(
    zespoly, monkeypatch, obcy,
):
    # projekt rozni sie od podlaczanego (zespol A, runner-a, /repo/app) dokladnie jednym polem
    tenant = zespoly.ids.a if obcy[0] == "tenant_a" else zespoly.ids.b
    await _dodaj_projekt(zespoly.maker, tenant, obcy[1], obcy[2])
    _wymus_konflikt_zapisu(monkeypatch)

    with pytest.raises(IntegrityError):
        await _podlacz_prawdziwym_kodem(zespoly)

    assert len(await _w_bazie(zespoly.maker)) == 1


async def test_konflikt_zapisu_przy_istniejacym_projekcie_zespolu_na_tym_runnerze_to_duplikat(
    zespoly, monkeypatch,
):
    await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/repo/app")
    _wymus_konflikt_zapisu(monkeypatch)

    with pytest.raises(StanNiemozliwy) as e:
        await _podlacz_prawdziwym_kodem(zespoly)

    assert str(e.value) == KOMUNIKAT_DUPLIKATU
    assert len(await _w_bazie(zespoly.maker)) == 1


# ---- lista projektow zespolu -----------------------------------------------------------------

async def test_lista_projektow_zawiera_tylko_projekty_zespolu(zespoly):
    swoj = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/app")
    await _dodaj_projekt(zespoly.maker, zespoly.ids.b, "runner-b", "/b/app",
                         created_at=T0 + timedelta(hours=1))
    await _dodaj_projekt(zespoly.maker, zespoly.ids.b, "runner-b", "/b/inny",
                         created_at=T0 - timedelta(hours=1))

    async with zespoly.maker() as s:
        lista_a = await _projekty_uslugi().projekty_zespolu(s, zespoly.ids.a)
        lista_b = await _projekty_uslugi().projekty_zespolu(s, zespoly.ids.b)

    assert [p["id"] for p in lista_a] == [swoj]
    assert {p["path"] for p in lista_b} == {"/b/app", "/b/inny"}


async def test_zespol_bez_projektow_ma_pusta_liste_mimo_projektow_innego_zespolu(zespoly):
    await _dodaj_projekt(zespoly.maker, zespoly.ids.b, "runner-b", "/b/app")
    async with zespoly.maker() as s:
        pusty = Tenant(name="Pusty")
        s.add(pusty)
        await s.commit()
        pusty_id = pusty.id

    async with zespoly.maker() as s:
        assert await _projekty_uslugi().projekty_zespolu(s, pusty_id) == []


async def test_lista_projektow_jest_od_najnowszego_a_remis_rozstrzyga_id_malejaco(zespoly):
    # id "z-stary" jest najwieksze, ale najstarszy w czasie: o kolejnosci decyduje czas
    await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/1", id="z-stary")
    await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/2", id="a-nowy",
                         created_at=T0 + timedelta(hours=1))
    await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/3", id="b-remis-a",
                         created_at=T0 + timedelta(hours=2))
    await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/4", id="b-remis-b",
                         created_at=T0 + timedelta(hours=2))

    async with zespoly.maker() as s:
        lista = await _projekty_uslugi().projekty_zespolu(s, zespoly.ids.a)

    assert [p["id"] for p in lista] == ["b-remis-b", "b-remis-a", "a-nowy", "z-stary"]


async def test_pozycja_listy_projektow_ma_dokladnie_te_pola_a_data_jest_iso_w_utc(zespoly):
    pid = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/app", name="app",
                               stack=("go", "node"))

    async with zespoly.maker() as s:
        lista = await _projekty_uslugi().projekty_zespolu(s, zespoly.ids.a)

    assert lista == [{
        "id": pid, "runner_id": "runner-a", "path": "/a/app", "name": "app",
        "stack": ["go", "node"], "created_at": "2030-01-01T12:00:00+00:00"}]


@pytest.fixture
def strefa_maszyny_tokio():
    """Strefa lokalna procesu inna niz UTC; po tescie przywrocona tylko strefa (TZ)."""
    poprzednia = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        yield
    finally:
        if poprzednia is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = poprzednia
        time.tzset()


async def test_data_utworzenia_projektu_bez_strefy_jest_raportowana_jako_utc_niezaleznie_od_strefy_maszyny(
        zespoly, strefa_maszyny_tokio):
    # kontrola zalozenia: strefa lokalna faktycznie rozni sie od UTC
    assert datetime(2030, 1, 1, 12).astimezone(timezone.utc).hour != 12
    pid = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/app")
    async with zespoly.maker() as s:   # kontrola zalozenia: SQLite oddaje date bez strefy
        assert (await s.get(_model_projektu(), pid)).created_at.tzinfo is None

    async with zespoly.maker() as s:
        lista = await _projekty_uslugi().projekty_zespolu(s, zespoly.ids.a)

    assert lista[0]["created_at"] == "2030-01-01T12:00:00+00:00"


async def test_data_utworzenia_projektu_w_innej_strefie_jest_zwracana_w_utc_niezaleznie_od_strefy_maszyny(
        zespoly, strefa_maszyny_tokio):
    assert datetime(2030, 1, 1, 12).astimezone(timezone.utc).hour != 12
    pid = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/app")
    plus_dwa = timezone(timedelta(hours=2))

    async with zespoly.maker() as s:
        projekt = await s.get(_model_projektu(), pid)
        projekt.created_at = datetime(2030, 1, 1, 14, 0, 0, tzinfo=plus_dwa)   # 12:00 UTC
        lista = await _projekty_uslugi().projekty_zespolu(s, zespoly.ids.a)

    assert lista[0]["created_at"] == "2030-01-01T12:00:00+00:00"


# ---- wybor runnera po id ---------------------------------------------------------------------

async def test_zapytanie_z_runner_id_trafia_do_wskazanego_runnera_a_nie_do_pierwszego_po_id(
    zespoly,
):
    wysylka = _Wysylka({"ok": True, "wynik": {"x": 1}})

    async with zespoly.maker() as s:
        runner_id, wynik = await zapytania.zapytaj_runnera_online(
            s, zespoly.ids.a, "detect_stack", {}, frozenset({"runner-a", "runner-z"}), wysylka,
            runner_id="runner-z")

    assert (runner_id, wynik) == ("runner-z", {"x": 1})
    assert [w[0] for w in wysylka.wywolania] == ["runner-z"]


async def test_zapytanie_bez_runner_id_wybiera_pierwszego_online_po_id_jak_dotad(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {}})

    async with zespoly.maker() as s:
        runner_id, _ = await zapytania.zapytaj_runnera_online(
            s, zespoly.ids.a, "detect_stack", {}, frozenset({"runner-a", "runner-z"}), wysylka)

    assert runner_id == "runner-a"


async def test_wskazany_runner_offline_to_runner_offline_choc_inny_runner_zespolu_jest_online(
    zespoly,
):
    wysylka = _Wysylka({"ok": True, "wynik": {}})

    async with zespoly.maker() as s:
        with pytest.raises(RunnerOffline):
            await zapytania.zapytaj_runnera_online(
                s, zespoly.ids.a, "detect_stack", {}, frozenset({"runner-a"}), wysylka,
                runner_id="runner-z")
    assert wysylka.wywolania == []


async def test_wskazany_runner_innego_zespolu_nie_jest_pytany_choc_jest_online(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {}})

    async with zespoly.maker() as s:
        with pytest.raises(RunnerOffline):
            await zapytania.zapytaj_runnera_online(
                s, zespoly.ids.a, "detect_stack", {}, frozenset({"runner-a", "runner-b"}),
                wysylka, runner_id="runner-b")
    assert wysylka.wywolania == []


# ---- lista runow: filtr po projekcie (usluga) ------------------------------------------------

async def _run(maker, tenant_id, *, project_id=None, id=None, created_at=T0) -> str:
    async with maker() as s:
        r = Run(tenant_id=tenant_id, workflow="szkielet", status=RunStatus.pending,
                created_at=created_at, project_id=project_id)
        if id:
            r.id = id
        s.add(r)
        await s.commit()
        return r.id


async def test_filtr_projektu_zwraca_tylko_runy_tego_projektu(zespoly):
    p1 = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/1")
    p2 = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/2")
    r1 = await _run(zespoly.maker, zespoly.ids.a, project_id=p1)
    await _run(zespoly.maker, zespoly.ids.a, project_id=p2)
    await _run(zespoly.maker, zespoly.ids.a)

    async with zespoly.maker() as s:
        lista = await runy_zespolu(s, zespoly.ids.a, project_id=p1)

    assert [r["id"] for r in lista] == [r1]
    assert lista[0]["project_id"] == p1


async def test_bez_filtra_projektu_lista_ma_wszystkie_runy_a_run_bez_projektu_ma_null(zespoly):
    p1 = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/1")
    z_projektem = await _run(zespoly.maker, zespoly.ids.a, project_id=p1)
    bez = await _run(zespoly.maker, zespoly.ids.a, created_at=T0 + timedelta(hours=1))

    async with zespoly.maker() as s:
        lista = await runy_zespolu(s, zespoly.ids.a)

    assert {r["id"]: r["project_id"] for r in lista} == {z_projektem: p1, bez: None}


async def test_filtr_projektu_innego_zespolu_nie_pokazuje_jego_runow(zespoly):
    pb = await _dodaj_projekt(zespoly.maker, zespoly.ids.b, "runner-b", "/b/1")
    rb = await _run(zespoly.maker, zespoly.ids.b, project_id=pb)
    await _run(zespoly.maker, zespoly.ids.a)

    async with zespoly.maker() as s:
        z_a = await runy_zespolu(s, zespoly.ids.a, project_id=pb)
        z_b = await runy_zespolu(s, zespoly.ids.b, project_id=pb)

    assert z_a == []
    assert [r["id"] for r in z_b] == [rb]


async def test_filtr_projektu_dziala_przed_limitem_a_nie_po_nim(zespoly):
    p1 = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/1")
    p2 = await _dodaj_projekt(zespoly.maker, zespoly.ids.a, "runner-a", "/a/2")
    szukany = await _run(zespoly.maker, zespoly.ids.a, project_id=p1)
    for i in range(3):   # nowsze runy innego projektu zajelyby caly limit
        await _run(zespoly.maker, zespoly.ids.a, project_id=p2,
                   created_at=T0 + timedelta(hours=i + 1))

    async with zespoly.maker() as s:
        lista = await runy_zespolu(s, zespoly.ids.a, limit=1, project_id=p1)

    assert [r["id"] for r in lista] == [szukany]


# =================================================================================
# 3. Przelot HTTP: prawdziwa aplikacja, odpowiedz runnera podstawiona w `bus.zapytaj`
# =================================================================================

class _FalszywyRunner:
    """Podstawka `bus.zapytaj`: zapisuje zapytania; domyslnie odpowiada jak zdrowy runner."""

    def __init__(self) -> None:
        self.wywolania: list[tuple] = []
        self.odpowiedz: dict | None = None
        self.wyjatek: Exception | None = None

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane))
        if self.wyjatek is not None:
            raise self.wyjatek
        if self.odpowiedz is not None:
            return self.odpowiedz
        return {"ok": True, "wynik": {"sciezka": dane["sciezka"], "nazwa": "app",
                                      "stack": ["python"]}}


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'projekty.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel_b, tenant_b = await zaloz_wlasciciela(
        maker, "b@example.com", nazwa_zespolu="Piekarnia")   # B pierwszy
    wlasciciel_a, tenant_a = await zaloz_wlasciciela(
        maker, "a@example.com", nazwa_zespolu="Browarek")
    obserwator_a = await zaloz_czlonka(maker, tenant_a, RolaWZespole.viewer, "obs-a@example.com")
    czlonek_a = await zaloz_czlonka(maker, tenant_a, RolaWZespole.member, "czl-a@example.com")
    runner_a = await zaloz_runnera(maker, tenant_a, "laptop-a", "token-a")
    runner_b = await zaloz_runnera(maker, tenant_b, "laptop-b", "token-b")
    falszywy = _FalszywyRunner()
    monkeypatch.setattr(bus, "zapytaj", falszywy)
    polaczone = [runner_a, runner_b]
    for rid in polaczone:
        bus.mark_runner_connected(rid)
    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
        klienci.append(k)
        return k

    async def zalogowany(user) -> AsyncClient:
        k = nowy_klient()
        await zaloguj_klienta(k, maker, user)
        return k

    yield SimpleNamespace(
        maker=maker, nowy_klient=nowy_klient, zalogowany=zalogowany, falszywy=falszywy,
        tenant_a=tenant_a, tenant_b=tenant_b, runner_a=runner_a, runner_b=runner_b,
        wlasciciel_a=wlasciciel_a, wlasciciel_b=wlasciciel_b,
        obserwator_a=obserwator_a, czlonek_a=czlonek_a)
    for k in klienci:
        await k.aclose()
    for rid in polaczone:
        while rid in bus.connected_runner_ids():
            bus.mark_runner_disconnected(rid)
        getattr(bus, "_rozlaczone", {}).pop(rid, None)
    await engine.dispose()


async def _liczba_projektow(swiat) -> int:
    async with swiat.maker() as s:
        return (await s.execute(
            select(func.count()).select_from(_model_projektu()))).scalar_one()


async def _post(klient, runner_id, path):
    return await klient.post("/api/projects", json={"runner_id": runner_id, "path": path})


# ---- POST /api/projects ----------------------------------------------------------------------

async def test_podlaczenie_projektu_przez_http_daje_201_i_rekord_w_bazie_z_wykrytym_stosem(swiat):
    swiat.falszywy.odpowiedz = _ok("/prywatne/repo/app", "app", ("node", "python"))
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_a, "/repo/app")

    assert odp.status_code == 201, odp.text
    tresc = odp.json()
    async with swiat.maker() as s:
        [wiersz] = (await s.execute(select(_model_projektu()))).scalars().all()
    assert (wiersz.id, wiersz.tenant_id, wiersz.runner_id, wiersz.path, wiersz.name,
            wiersz.stack) == (tresc["id"], swiat.tenant_a, swiat.runner_a,
                              "/prywatne/repo/app", "app", ["node", "python"])
    assert tresc == {"id": wiersz.id, "runner_id": swiat.runner_a, "path": "/prywatne/repo/app",
                     "name": "app", "stack": ["node", "python"],
                     "created_at": tresc["created_at"]}
    assert swiat.falszywy.wywolania == [
        (swiat.runner_a, "detect_stack", {"sciezka": "/repo/app"})]


async def test_czlonek_zespolu_moze_podlaczyc_projekt(swiat):
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _post(klient, swiat.runner_a, "/repo/app")

    assert odp.status_code == 201, odp.text
    assert await _liczba_projektow(swiat) == 1


async def test_runner_offline_daje_409_z_podpowiedzia_a_projekt_nie_powstaje(swiat):
    bus.mark_runner_disconnected(swiat.runner_a)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_a, "/repo/app")

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.falszywy.wywolania == []
    assert await _liczba_projektow(swiat) == 0


async def test_sciezka_poza_zakresem_runnera_daje_403_z_czytelnym_komunikatem_a_nie_500(swiat):
    swiat.falszywy.odpowiedz = {"ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM,
                                "kod": "poza_zakresem"}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_a, "/etc")

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert await _liczba_projektow(swiat) == 0


async def test_inna_porazka_runnera_daje_409_z_jego_komunikatem(swiat):
    swiat.falszywy.odpowiedz = {"ok": False, "blad": "sciezka nie istnieje", "kod": "nie_istnieje"}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_a, "/repo/nie-ma")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "sciezka nie istnieje"}
    assert await _liczba_projektow(swiat) == 0


async def test_brak_odpowiedzi_runnera_w_limicie_czasu_daje_409_a_nie_500(swiat):
    swiat.falszywy.wyjatek = TimeoutError()
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_a, "/repo/app")

    assert odp.status_code == 409
    assert await _liczba_projektow(swiat) == 0


async def test_zly_ksztalt_odpowiedzi_runnera_daje_409_i_nie_zapisuje_projektu(swiat):
    swiat.falszywy.odpowiedz = {"ok": True, "wynik": {"sciezka": "/r/a", "nazwa": "a",
                                                      "stack": "python"}}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_a, "/repo/app")

    assert odp.status_code == 409
    assert odp.json()["detail"].startswith(PREFIKS_ZLEGO_KSZTALTU)
    assert await _liczba_projektow(swiat) == 0


async def test_runner_innego_zespolu_daje_404_nikt_nie_jest_pytany_i_projekt_nie_powstaje(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _post(klient, swiat.runner_b, "/repo/app")   # runner B jest online

    assert odp.status_code == 404
    assert swiat.falszywy.wywolania == []
    assert await _liczba_projektow(swiat) == 0


async def test_ponowne_podlaczenie_tego_samego_katalogu_daje_409_i_jeden_rekord(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    pierwsze = await _post(klient, swiat.runner_a, "/repo/app")

    drugie = await _post(klient, swiat.runner_a, "/repo/app")

    assert pierwsze.status_code == 201
    assert drugie.status_code == 409
    assert await _liczba_projektow(swiat) == 1


async def test_obserwator_nie_podlaczy_projektu_a_runner_nie_jest_nawet_pytany(swiat):
    klient = await swiat.zalogowany(swiat.obserwator_a)

    odp = await _post(klient, swiat.runner_a, "/repo/app")

    assert odp.status_code == 403
    assert swiat.falszywy.wywolania == []
    assert await _liczba_projektow(swiat) == 0


async def test_bez_sesji_podlaczenie_i_lista_projektow_daja_401_a_runner_nie_jest_pytany(swiat):
    klient = swiat.nowy_klient()

    post = await _post(klient, swiat.runner_a, "/repo/app")
    get = await klient.get("/api/projects")

    assert (post.status_code, get.status_code) == (401, 401)
    assert swiat.falszywy.wywolania == []
    assert await _liczba_projektow(swiat) == 0


# ---- granice pol wejscia ---------------------------------------------------------------------

async def test_runner_id_dluzszy_niz_36_znakow_to_422_a_dokladnie_36_przechodzi_walidacje(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    za_dlugi = await _post(klient, "r" * 37, "/repo/app")
    graniczny = await _post(klient, "r" * 36, "/repo/app")

    assert za_dlugi.status_code == 422
    assert graniczny.status_code == 404   # walidacja przeszla; takiego runnera nie ma
    assert swiat.falszywy.wywolania == []


async def test_sciezka_dluzsza_niz_4096_znakow_to_422_a_dokladnie_4096_jest_przyjeta(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    graniczna = "/" + "p" * 4095

    za_dluga = await _post(klient, swiat.runner_a, graniczna + "p")
    odp = await _post(klient, swiat.runner_a, graniczna)

    assert za_dluga.status_code == 422
    assert odp.status_code == 201, odp.text
    assert await _liczba_projektow(swiat) == 1


@pytest.mark.parametrize("cialo", [
    {"runner_id": "", "path": "/repo/app"},
    {"runner_id": "runner", "path": ""},
    {"path": "/repo/app"},
    {"runner_id": "runner"},
], ids=["pusty_runner_id", "pusta_sciezka", "brak_runner_id", "brak_sciezki"])
async def test_nowy_projekt_bez_runnera_albo_sciezki_to_422_bez_pytania_runnera(swiat, cialo):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.post("/api/projects", json=cialo)

    assert odp.status_code == 422
    assert swiat.falszywy.wywolania == []


# ---- GET /api/projects -----------------------------------------------------------------------

async def test_lista_projektow_przez_http_pokazuje_tylko_projekty_zespolu_z_sesji(swiat):
    # B zalozony pierwszy: lista zalezy od zespolu z sesji, nie od "pierwszego w bazie"
    projekt_b = await _dodaj_projekt(swiat.maker, swiat.tenant_b, swiat.runner_b, "/b/app")
    projekt_a = await _dodaj_projekt(swiat.maker, swiat.tenant_a, swiat.runner_a, "/a/app",
                                     stack=("python",))
    klient_a = await swiat.zalogowany(swiat.wlasciciel_a)
    klient_b = await swiat.zalogowany(swiat.wlasciciel_b)

    odp_a = await klient_a.get("/api/projects")
    odp_b = await klient_b.get("/api/projects")

    assert odp_a.status_code == 200
    assert odp_a.json() == [{
        "id": projekt_a, "runner_id": swiat.runner_a, "path": "/a/app", "name": "app",
        "stack": ["python"], "created_at": "2030-01-01T12:00:00+00:00"}]
    assert [p["id"] for p in odp_b.json()] == [projekt_b]


async def test_projekt_podlaczony_przez_post_jest_na_liscie_zespolu_a_nie_na_liscie_obcego(swiat):
    klient_a = await swiat.zalogowany(swiat.wlasciciel_a)
    klient_b = await swiat.zalogowany(swiat.wlasciciel_b)
    nowy = (await _post(klient_a, swiat.runner_a, "/repo/app")).json()

    lista_a = (await klient_a.get("/api/projects")).json()
    lista_b = (await klient_b.get("/api/projects")).json()

    assert lista_a == [nowy]
    assert lista_b == []


async def test_obserwator_widzi_liste_projektow_zespolu(swiat):
    projekt_a = await _dodaj_projekt(swiat.maker, swiat.tenant_a, swiat.runner_a, "/a/app")
    klient = await swiat.zalogowany(swiat.obserwator_a)

    odp = await klient.get("/api/projects")

    assert odp.status_code == 200
    assert [p["id"] for p in odp.json()] == [projekt_a]


async def test_zespol_bez_projektow_dostaje_pusta_liste_a_nie_404(swiat):
    await _dodaj_projekt(swiat.maker, swiat.tenant_b, swiat.runner_b, "/b/app")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.get("/api/projects")

    assert (odp.status_code, odp.json()) == (200, [])


# ---- GET /api/runs?project=<id> --------------------------------------------------------------

async def test_lista_runow_z_filtrem_projektu_pokazuje_tylko_runy_tego_projektu(swiat):
    p1 = await _dodaj_projekt(swiat.maker, swiat.tenant_a, swiat.runner_a, "/a/1")
    p2 = await _dodaj_projekt(swiat.maker, swiat.tenant_a, swiat.runner_a, "/a/2")
    r1 = await _run(swiat.maker, swiat.tenant_a, project_id=p1)
    r2 = await _run(swiat.maker, swiat.tenant_a, project_id=p1, created_at=T0 + timedelta(hours=1))
    await _run(swiat.maker, swiat.tenant_a, project_id=p2)
    await _run(swiat.maker, swiat.tenant_a)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.get("/api/runs", params={"project": p1})

    assert odp.status_code == 200
    assert [r["id"] for r in odp.json()] == [r2, r1]
    assert {r["project_id"] for r in odp.json()} == {p1}


async def test_lista_runow_bez_filtra_ma_wszystkie_runy_a_run_bez_projektu_niesie_null(swiat):
    p1 = await _dodaj_projekt(swiat.maker, swiat.tenant_a, swiat.runner_a, "/a/1")
    z_projektem = await _run(swiat.maker, swiat.tenant_a, project_id=p1)
    bez = await _run(swiat.maker, swiat.tenant_a, created_at=T0 + timedelta(hours=1))
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.get("/api/runs")

    assert odp.status_code == 200
    assert odp.json() == [
        {"id": bez, "workflow": "szkielet", "status": "pending",
         "created_at": "2030-01-01T13:00:00+00:00", "project_id": None},
        {"id": z_projektem, "workflow": "szkielet", "status": "pending",
         "created_at": "2030-01-01T12:00:00+00:00", "project_id": p1},
    ]


async def test_filtr_projektu_innego_zespolu_daje_pusta_liste_mimo_jego_runow(swiat):
    projekt_b = await _dodaj_projekt(swiat.maker, swiat.tenant_b, swiat.runner_b, "/b/1")
    await _run(swiat.maker, swiat.tenant_b, project_id=projekt_b)
    await _run(swiat.maker, swiat.tenant_a)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.get("/api/runs", params={"project": projekt_b})

    assert (odp.status_code, odp.json()) == (200, [])


async def test_filtr_nieistniejacego_projektu_daje_pusta_liste(swiat):
    await _run(swiat.maker, swiat.tenant_a)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.get("/api/runs", params={"project": "nie-ma-takiego"})

    assert (odp.status_code, odp.json()) == (200, [])


async def test_filtr_projektu_dluzszy_niz_36_znakow_to_422_a_dokladnie_36_jest_przyjety(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    za_dlugi = await klient.get("/api/runs", params={"project": "p" * 37})
    graniczny = await klient.get("/api/runs", params={"project": "p" * 36})

    assert za_dlugi.status_code == 422
    assert (graniczny.status_code, graniczny.json()) == (200, [])


async def test_bez_sesji_lista_runow_z_filtrem_projektu_daje_401(swiat):
    odp = await swiat.nowy_klient().get("/api/runs", params={"project": "p1"})

    assert odp.status_code == 401

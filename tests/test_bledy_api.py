"""Jeden punkt tlumaczenia bledow dziedziny na HTTP, bez sladu stosu (HAR-30).

Testy przed implementacja: `cockpit.api.bledy` jeszcze nie istnieje.
"""
from __future__ import annotations

import ast
import gc
import importlib
import logging
import pkgutil
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cockpit.api.bledy import KODY_BLEDOW, zarejestruj_obsluge_bledow
from cockpit.services.errors import (
    BladDziedziny,
    BlednySkill,
    BrakDostepu,
    NaruszonyNiezmiennik,
    NieZnaleziono,
    PlikPozaZakresem,
    RunnerOffline,
    StanNiemozliwy,
)

KOMUNIKAT_WEWNETRZNY = "błąd wewnętrzny kokpitu"
KATALOG_API = Path(__file__).resolve().parent.parent / "src" / "cockpit" / "api"


# --- pomocnicy: kompletnosc tabeli (D1) ---------------------------------

def _zaimportuj_wszystkie_moduly() -> None:
    """Importuje cale pakiety cockpit i runner, zeby __subclasses__ bylo pelne.

    Pomijamy wylacznie moduly, ktorych brakuje zewnetrznej zaleznosci
    (ModuleNotFoundError spoza naszych pakietow); kazdy inny blad wywala test.
    """
    for nazwa_pakietu in ("cockpit", "runner"):
        pakiet = importlib.import_module(nazwa_pakietu)
        for info in pkgutil.walk_packages(pakiet.__path__, prefix=f"{nazwa_pakietu}."):
            try:
                importlib.import_module(info.name)
            except ModuleNotFoundError as e:
                if (e.name or "").split(".")[0] in ("cockpit", "runner"):
                    raise
                # brak zewnetrznej zaleznosci: jawne pominiecie tego modulu


def _wszystkie_podklasy(baza: type) -> set[type]:
    wynik: set[type] = set()
    for p in baza.__subclasses__():
        wynik.add(p)
        wynik |= _wszystkie_podklasy(p)
    return wynik


def klasy_kokpitu_bez_kodu(tabela) -> list[type]:
    _zaimportuj_wszystkie_moduly()
    return sorted(
        (k for k in _wszystkie_podklasy(BladDziedziny)
         if k.__module__.startswith("cockpit.") and k not in tabela),
        key=lambda k: k.__qualname__,
    )


@pytest.fixture
def nowy_rodzaj_bledu():
    """Syntetyczna podklasa 'z kokpitu'; po tescie znika z __subclasses__."""
    klasa = type("NowyBladBezKodu", (BladDziedziny,), {"__module__": "cockpit.services.errors"})
    yield klasa
    del klasa
    gc.collect()


# --- pomocnicy: aplikacja testowa --------------------------------------

def _klient(wyjatek_fabryka) -> httpx.AsyncClient:
    app = FastAPI()
    zarejestruj_obsluge_bledow(app)

    @app.get("/rzuc")
    async def rzuc():
        raise wyjatek_fabryka()

    return httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


# --- 1-2. kompletnosc tabeli ------------------------------------------

def test_kazdy_blad_dziedziny_kokpitu_ma_kod_http_w_tabeli():
    assert klasy_kokpitu_bez_kodu(KODY_BLEDOW) == []


def test_podklasy_bledow_runnera_sa_zaladowane_ale_poza_tabela():
    """Filtr po module jest potrzebny: bez niego klasy runnera oblewalyby kontrole."""
    _zaimportuj_wszystkie_moduly()
    runnerowe = {k.__name__ for k in _wszystkie_podklasy(BladDziedziny)
                 if k.__module__.startswith("runner.")}
    assert {"BrakModelu", "PusteWklejenie", "PozaZakresem"} <= runnerowe
    assert not any(k.__module__.startswith("runner.") for k in KODY_BLEDOW)


def test_nowy_rodzaj_bledu_bez_kodu_http_oblewa_kontrole(nowy_rodzaj_bledu):
    assert nowy_rodzaj_bledu in klasy_kokpitu_bez_kodu(KODY_BLEDOW)


# --- 3. kazdy wpis tabeli przez prawdziwa trase -------------------------

@pytest.mark.parametrize(
    ("fabryka", "oczekiwany_kod", "komunikat"),
    [
        (lambda: BrakDostepu("brak sesji"), 401, "brak sesji"),
        (lambda: NieZnaleziono("nie ma takiego runu"), 404, "nie ma takiego runu"),
        (lambda: StanNiemozliwy("runner offline"), 409, "runner offline"),
        (lambda: NaruszonyNiezmiennik("no_git_write", "push"), 422, "niezmiennik no_git_write: push"),
        (lambda: BlednySkill("skill bez pola role"), 422, "skill bez pola role"),
        (lambda: PlikPozaZakresem("sciezka poza zakresem runnera"), 403,
         "sciezka poza zakresem runnera"),
        (lambda: RunnerOffline("runner offline — tryb tylko do odczytu"), 409,
         "runner offline — tryb tylko do odczytu"),
    ],
    ids=["brak_dostepu", "nie_znaleziono", "stan_niemozliwy", "naruszony_niezmiennik", "bledny_skill",
         "plik_poza_zakresem", "runner_offline"],
)
async def test_kazdy_rodzaj_bledu_dziedziny_daje_swoj_kod_http_i_komunikat(
    fabryka, oczekiwany_kod, komunikat
):
    async with _klient(fabryka) as c:
        r = await c.get("/rzuc")
    assert r.status_code == oczekiwany_kod
    assert r.json() == {"detail": komunikat}


def test_plik_poza_zakresem_i_runner_offline_maja_wlasne_wpisy_w_tabeli_z_wartoscia_403_i_409():
    """Same podklasy dziedziczylyby 403/409 po rodzicach przez MRO; wpis ma byc jawny (HAR-55)."""
    assert KODY_BLEDOW[PlikPozaZakresem] == 403
    assert KODY_BLEDOW[RunnerOffline] == 409


def test_plik_poza_zakresem_jest_zabronionym_a_runner_offline_stanem_niemozliwym():
    from cockpit.services.errors import Zabronione

    assert issubclass(PlikPozaZakresem, Zabronione)
    assert issubclass(RunnerOffline, StanNiemozliwy)


async def test_blednie_opisany_skill_zwraca_422_z_komunikatem_dla_czlowieka():
    komunikat = "SKILL.md: nieznana rola 'szef'"
    async with _klient(lambda: BlednySkill(komunikat)) as c:
        r = await c.get("/rzuc")
    assert r.status_code == 422
    assert r.json() == {"detail": komunikat}


# --- 4. MRO: najblizsza klasa z tabeli decyduje ------------------------

async def test_podklasa_bledu_bez_wlasnego_wpisu_dostaje_kod_rodzica():
    class SzczegolowyStan(StanNiemozliwy):
        pass

    async with _klient(lambda: SzczegolowyStan("runner zajety")) as c:
        r = await c.get("/rzuc")
    assert r.status_code == 409
    assert r.json() == {"detail": "runner zajety"}


async def test_podklasa_bledu_z_wlasnym_wpisem_dostaje_swoj_kod_nie_rodzica(monkeypatch):
    class InnyBrak(NieZnaleziono):
        pass

    monkeypatch.setitem(KODY_BLEDOW, InnyBrak, 410)
    async with _klient(lambda: InnyBrak("run usuniety")) as c:
        r = await c.get("/rzuc")
    assert r.status_code == 410
    assert r.json() == {"detail": "run usuniety"}


# --- 5. D2: blad dziedziny bez wpisu ------------------------------------

async def test_blad_dziedziny_bez_kodu_w_tabeli_to_500_bez_komunikatu_wyjatku(caplog):
    class NieznanyBlad(BladDziedziny):
        pass

    with caplog.at_level(logging.ERROR, logger="cockpit.api.bledy"):
        async with _klient(lambda: NieznanyBlad("wewnetrzny szczegol xyz")) as c:
            r = await c.get("/rzuc")
    assert r.status_code == 500
    assert r.json() == {"detail": KOMUNIKAT_WEWNETRZNY}
    assert "wewnetrzny szczegol xyz" not in r.text
    assert any(rec.name == "cockpit.api.bledy" for rec in caplog.records)


# --- 6. nieoczekiwany blad ----------------------------------------------

async def test_nieoczekiwany_blad_nie_pokazuje_sladu_stosu(caplog):
    with caplog.at_level(logging.ERROR, logger="cockpit.api.bledy"):
        async with _klient(lambda: RuntimeError("sekret")) as c:
            r = await c.get("/rzuc")
    assert r.status_code == 500
    assert r.json() == {"detail": KOMUNIKAT_WEWNETRZNY}
    assert "sekret" not in r.text
    assert "Traceback" not in r.text
    zalogowane = [
        rec.exc_info[1] for rec in caplog.records
        if rec.name == "cockpit.api.bledy" and rec.exc_info
    ]
    assert any(isinstance(e, RuntimeError) and str(e) == "sekret" for e in zalogowane)


# --- produkcyjny punkt wejscia ------------------------------------------

def test_produkcyjna_aplikacja_ma_zarejestrowana_obsluge_bledow():
    from cockpit.api import bledy
    from cockpit.api.app import app

    assert app.exception_handlers.get(BladDziedziny) is not None
    assert app.exception_handlers.get(Exception) is not None
    assert app.exception_handlers[BladDziedziny].__module__ == bledy.__name__
    assert app.exception_handlers[Exception].__module__ == bledy.__name__


# --- 7. skan AST: zadnego HTTPException w api/ --------------------------

def uzycia_httpexception(zrodlo: str) -> list[int]:
    """Numery linii z `raise HTTPException`, wywolaniem `HTTPException(` lub importem."""
    wynik: list[int] = []
    for wezel in ast.walk(ast.parse(zrodlo)):
        if isinstance(wezel, ast.Raise) and isinstance(wezel.exc, ast.Name) \
                and wezel.exc.id == "HTTPException":
            wynik.append(wezel.lineno)
        elif isinstance(wezel, ast.Call):
            f = wezel.func
            if (isinstance(f, ast.Name) and f.id == "HTTPException") or \
                    (isinstance(f, ast.Attribute) and f.attr == "HTTPException"):
                wynik.append(wezel.lineno)
        elif isinstance(wezel, ast.ImportFrom):
            if any(a.name == "HTTPException" for a in wezel.names):
                wynik.append(wezel.lineno)
    return wynik


def test_trasy_api_nie_rzucaja_httpexception():
    pliki = sorted(KATALOG_API.rglob("*.py"))
    assert pliki, "skan nic nie przeszukal"
    znalezione = {
        str(p.relative_to(KATALOG_API)): uzycia_httpexception(p.read_text(encoding="utf-8"))
        for p in pliki
    }
    assert {k: v for k, v in znalezione.items() if v} == {}


@pytest.mark.parametrize(
    "zrodlo",
    [
        "def f():\n    raise HTTPException\n",
        "def f():\n    raise HTTPException(404, 'x')\n",
        "def f():\n    raise fastapi.HTTPException(404, 'x')\n",
        "e = HTTPException(404)\n",
        "e = fastapi.HTTPException(404)\n",
        "from fastapi import HTTPException\n",
        "from starlette.exceptions import HTTPException\n",
    ],
    ids=["raise_nazwa", "raise_wywolanie", "raise_atrybut_wywolanie",
         "samo_wywolanie", "samo_wywolanie_atrybut", "import_fastapi", "import_starlette"],
)
def test_skan_zglasza_kazdy_wariant_uzycia_httpexception(zrodlo):
    assert uzycia_httpexception(zrodlo) != []


@pytest.mark.parametrize(
    "zrodlo",
    [
        "from cockpit.services.errors import NieZnaleziono\n\ndef f():\n    raise NieZnaleziono('x')\n",
        "# HTTPException jest zakazany\nx = 1\n",
        "x = 'raise HTTPException(404)'\n",
    ],
    ids=["czyste_zrodlo", "slowo_w_komentarzu", "slowo_w_napisie"],
)
def test_skan_nie_zglasza_czystego_zrodla_ani_samego_slowa(zrodlo):
    assert uzycia_httpexception(zrodlo) == []


# --- HAR-58: ostrzezenie o aktywnym runie na tickecie -----------------------------------------

def test_ostrzezenie_o_rownoleglym_runie_ma_wlasny_wpis_w_tabeli_z_kodem_409():
    from cockpit.services.errors import WymagaPotwierdzenia

    assert KODY_BLEDOW[WymagaPotwierdzenia] == 409
    assert issubclass(WymagaPotwierdzenia, StanNiemozliwy)


async def test_wymagane_potwierdzenie_zwraca_409_z_id_aktywnego_runu_w_odpowiedzi():
    from cockpit.services.errors import WymagaPotwierdzenia

    wyjatek = WymagaPotwierdzenia("r-1")
    async with _klient(lambda: wyjatek) as c:
        r = await c.get("/rzuc")
    assert r.status_code == 409
    assert r.json() == {"detail": str(wyjatek), "aktywny_run_id": "r-1"}
    assert "r-1" in str(wyjatek)


async def test_inny_stan_niemozliwy_nie_niesie_id_aktywnego_runu():
    async with _klient(lambda: StanNiemozliwy("runner offline")) as c:
        r = await c.get("/rzuc")
    assert r.status_code == 409
    assert "aktywny_run_id" not in r.json()


async def test_blad_dziedziny_bez_wpisu_w_tabeli_trafia_do_logu_z_wlasnym_wyjatkiem(caplog):
    class BladBezWpisu(BladDziedziny):
        pass

    rzucony = BladBezWpisu("szczegol tylko do logu")
    with caplog.at_level(logging.ERROR, logger="cockpit.api.bledy"):
        async with _klient(lambda: rzucony) as c:
            r = await c.get("/rzuc")
    assert r.status_code == 500
    assert r.json() == {"detail": KOMUNIKAT_WEWNETRZNY}
    zalogowane = [
        rec.exc_info[1] for rec in caplog.records
        if rec.name == "cockpit.api.bledy" and rec.levelno == logging.ERROR and rec.exc_info
    ]
    assert any(e is rzucony for e in zalogowane)

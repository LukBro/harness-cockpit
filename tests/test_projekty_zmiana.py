"""HAR-180: zmiana capabilities projektu z UI zapisuje `.cockpit/project.yaml` w repozytorium
uzytkownika wylacznie przez runnera tego projektu (`PATCH /api/projects/{project_id}`).

Przelot HTTP przez prawdziwa aplikacje. Runner jest podstawiony w `bus.zapytaj`, ale domyslnie
wykonuje PRAWDZIWE operacje runnera (`read_file`, `write_file`) na plikach w katalogu testu, wiec
asercje czytaja skutek z dysku, a nie tylko kod odpowiedzi. Zespol B jest zakladany PIERWSZY.
Fixture `swiat` i podstawka `RunnerZPlikami` uzywa tez `test_skille_api.py`.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from ruamel.yaml import YAML
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from transport_asgi import TransportASGIStrumieniujacy

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Project, RolaWZespole
from runner import operacje
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_runnera, zaloz_wlasciciela

KOMUNIKAT_OFFLINE = "runner offline — tryb tylko do odczytu"
KOMUNIKAT_PROJEKT_NIEZNANY = "projekt nieznany"
KOMUNIKAT_CAPABILITIES_NIE_MAPA = "capabilities musi byc mapa nazwa: komenda"
KOMUNIKAT_PLIK_NIE_MAPA = "project.yaml musi byc mapa"
KOMUNIKAT_POZA_ZAKRESEM = "sciezka poza zakresem runnera"
MAX_NAZWA_CAPABILITY = 100
MAX_KOMENDA_CAPABILITY = 1000

PROJECT_YAML = """\
# Konfiguracja projektu Browarek (ten komentarz musi przetrwac zapis z UI)
stack:
  - python   # glowny jezyk
  - node
tickets:
  # zrodlo ticketow, nie ruszamy
  source: linear
  team: "HAR"
capabilities:
  # komendy projektu
  test: pytest -q   # szybkie testy
  lint: ruff check .
  build: make build   # budowanie
"""

PROJECT_YAML_BEZ_CAPABILITIES = """\
# Projekt bez komend
stack:
  - go   # jedyny jezyk
tickets:
  source: jira
"""


class RunnerZPlikami:
    """Podstawka `bus.zapytaj`: zapisuje kazde zapytanie, a odpowiada prawdziwa operacja runnera
    na plikach z katalogu testu. `wymuszone[typ]` podmienia odpowiedz (slownik) albo rzuca (wyjatek)."""

    def __init__(self) -> None:
        self.wywolania: list[tuple] = []
        self.wymuszone: dict[str, dict | Exception] = {}

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane))
        wymuszona = self.wymuszone.get(typ)
        if isinstance(wymuszona, Exception):
            raise wymuszona
        if wymuszona is not None:
            return wymuszona
        return await operacje.wykonaj({"type": typ, "req_id": "r-1", **dane})

    def typy(self) -> list[str]:
        return [w[1] for w in self.wywolania]


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    baza = tmp_path.resolve()   # macOS: /tmp to dowiazanie do /private/tmp
    korzen, korzen_b, dom = baza / "projekt", baza / "projekt-b", baza / "dom"
    for katalog in (korzen, korzen_b, dom):
        katalog.mkdir()
    (korzen / ".cockpit").mkdir()
    (korzen_b / ".cockpit").mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(korzen), str(korzen_b)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    monkeypatch.delenv("COCKPIT_MAX_READ_KB", raising=False)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'zmiana.db'}")
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
    runner_a2 = await zaloz_runnera(maker, tenant_a, "laptop-a2", "token-a2")
    runner_b = await zaloz_runnera(maker, tenant_b, "laptop-b", "token-b")
    async with maker() as s:
        projekt_a = Project(tenant_id=tenant_a, runner_id=runner_a, path=str(korzen), name="app")
        projekt_a2 = Project(tenant_id=tenant_a, runner_id=runner_a2, path=str(korzen), name="app")
        projekt_b = Project(tenant_id=tenant_b, runner_id=runner_b, path=str(korzen_b), name="app")
        s.add_all([projekt_a, projekt_a2, projekt_b])
        await s.commit()
    falszywy = RunnerZPlikami()
    monkeypatch.setattr(bus, "zapytaj", falszywy)
    polaczone = [runner_a, runner_a2, runner_b]
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
        maker=maker, nowy_klient=nowy_klient, zalogowany=zalogowany, runner=falszywy,
        korzen=korzen, korzen_b=korzen_b, dom=dom, plik=korzen / ".cockpit" / "project.yaml",
        plik_b=korzen_b / ".cockpit" / "project.yaml",
        tenant_a=tenant_a, tenant_b=tenant_b, runner_a=runner_a, runner_a2=runner_a2,
        runner_b=runner_b, projekt_a=projekt_a.id, projekt_a2=projekt_a2.id,
        projekt_b=projekt_b.id, wlasciciel_a=wlasciciel_a, wlasciciel_b=wlasciciel_b,
        obserwator_a=obserwator_a, czlonek_a=czlonek_a)
    for k in klienci:
        await k.aclose()
    for rid in polaczone:
        while rid in bus.connected_runner_ids():
            bus.mark_runner_disconnected(rid)
        getattr(bus, "_rozlaczone", {}).pop(rid, None)
    await engine.dispose()


def _zapisz(plik: Path, tekst: str) -> None:
    plik.write_bytes(tekst.encode("utf-8"))


def _tekst(plik: Path) -> str:
    return plik.read_bytes().decode("utf-8")


def _dane(tekst: str) -> dict:
    """Zwykle dane YAML (kolejnosc kluczy zachowana), bez komentarzy."""
    return YAML(typ="safe").load(tekst)


async def _patch(swiat, klient, capabilities, projekt_id=None):
    return await klient.patch(
        f"/api/projects/{projekt_id or swiat.projekt_a}", json={"capabilities": capabilities})


@pytest.fixture
async def plik_projektu(swiat):
    _zapisz(swiat.plik, PROJECT_YAML)
    return swiat.plik


# =================================================================================
# 1. Zapis przez runnera projektu
# =================================================================================

async def test_zmiana_capability_z_ui_zapisuje_project_yaml_w_repo_przez_runnera_projektu(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4", "deploy": "make deploy"})

    assert odp.status_code == 200, odp.text
    sciezka = str(swiat.plik)
    assert swiat.runner.typy() == ["read_file", "write_file"]
    odczyt, zapis = swiat.runner.wywolania
    assert odczyt == (swiat.runner_a, "read_file", {"sciezka": sciezka})
    assert zapis[:2] == (swiat.runner_a, "write_file")
    assert set(zapis[2]) == {"sciezka", "tresc"}
    assert zapis[2]["sciezka"] == sciezka
    assert _tekst(swiat.plik) == zapis[2]["tresc"]   # skutek w pliku, nie tylko w odpowiedzi
    assert _dane(_tekst(swiat.plik))["capabilities"] == {
        "test": "pytest -q -n 4", "lint": "ruff check .", "build": "make build",
        "deploy": "make deploy"}


async def test_odpowiedz_na_zmiane_niesie_projekt_i_pelna_mape_capabilities_z_pliku(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4", "deploy": "make deploy"})

    assert odp.json() == {
        "project_id": swiat.projekt_a,
        "capabilities": {"test": "pytest -q -n 4", "lint": "ruff check .",
                         "build": "make build", "deploy": "make deploy"}}
    assert list(odp.json()["capabilities"]) == ["test", "lint", "build", "deploy"]


async def test_zmiana_capability_z_ui_zachowuje_komentarze_w_project_yaml(swiat, plik_projektu):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4", "deploy": "make deploy"})

    assert odp.status_code == 200, odp.text
    tekst = _tekst(swiat.plik)
    assert tekst.splitlines()[0] == (
        "# Konfiguracja projektu Browarek (ten komentarz musi przetrwac zapis z UI)")
    assert "# glowny jezyk" in tekst
    assert "# zrodlo ticketow, nie ruszamy" in tekst
    assert "# komendy projektu" in tekst
    assert "# budowanie" in tekst
    linia_testu = next(w for w in tekst.splitlines() if w.strip().startswith("test:"))
    assert "pytest -q -n 4" in linia_testu     # komentarz przy zmienionej capability zostaje
    assert "# szybkie testy" in linia_testu


async def test_zmiana_capability_zachowuje_kolejnosc_kluczy_pliku_a_nowa_trafia_na_koniec(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    await _patch(swiat, klient, {"build": "make all", "deploy": "make deploy", "test": "pytest"})

    dane = _dane(_tekst(swiat.plik))
    assert list(dane) == ["stack", "tickets", "capabilities"]
    assert list(dane["capabilities"]) == ["test", "lint", "build", "deploy"]


async def test_zmiana_capability_nie_zmienia_pozostalych_sekcji_pliku_ani_ich_formatowania(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    przed = PROJECT_YAML.split("capabilities:")[0]
    po = _tekst(swiat.plik).split("capabilities:")[0]
    assert po == przed     # w tym cudzyslow przy `team: "HAR"` i wciecia listy
    assert _dane(_tekst(swiat.plik))["tickets"] == {"source": "linear", "team": "HAR"}
    assert _dane(_tekst(swiat.plik))["stack"] == ["python", "node"]


async def test_zmiana_jednej_capability_zostawia_linie_pozostalych_bez_zmian(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    tekst = _tekst(swiat.plik)
    assert "\n  lint: ruff check .\n" in tekst
    assert "\n  build: make build   # budowanie\n" in tekst


async def test_dluga_komenda_capability_zostaje_w_jednej_linii_pliku(swiat, plik_projektu):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    dluga = "pytest -q -n 4 --cov=src --cov-report=term-missing --maxfail=1 -p no:cacheprovider tests/ -k smoke"
    assert len(dluga) > 80

    await _patch(swiat, klient, {"test": dluga})

    wiersze = _tekst(swiat.plik).splitlines()
    linia = next(w for w in wiersze if w.lstrip().startswith("test:"))
    assert dluga in linia                                   # bez zawijania na kilka linii
    assert "# szybkie testy" in linia
    assert wiersze[wiersze.index(linia) + 1] == "  lint: ruff check ."


@pytest.mark.parametrize("komenda", [
    "pytest -k 'a: b' # nie komentarz",
    'echo "x" && ls',
    "- zaczyna sie od myslnika",
    "{a: b}",
    "123",
    "yes",
    "null",
    "true",
    "ze znakiem # w srodku",
    "zażółć gęślą jaźń && echo ✓",
], ids=["dwukropek_i_hash", "cudzyslow", "myslnik", "klamry", "liczba", "yes", "null", "true",
        "hash_w_srodku", "znaki_nie_ascii"])
async def test_komenda_ze_znakami_specjalnymi_yaml_wraca_z_pliku_bez_zmian_jako_ten_sam_napis(
    swiat, plik_projektu, komenda,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"deploy": komenda})

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik))["capabilities"]["deploy"] == komenda
    assert odp.json()["capabilities"]["deploy"] == komenda


async def test_dodanie_capability_do_pliku_bez_sekcji_capabilities_tworzy_sekcje_i_zostawia_reszte(
    swiat,
):
    _zapisz(swiat.plik, PROJECT_YAML_BEZ_CAPABILITIES)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q"})

    assert odp.status_code == 200, odp.text
    tekst = _tekst(swiat.plik)
    assert tekst.startswith(PROJECT_YAML_BEZ_CAPABILITIES)   # komentarze i sekcje bez zmian
    assert _dane(tekst) == {**_dane(PROJECT_YAML_BEZ_CAPABILITIES),
                            "capabilities": {"test": "pytest -q"}}
    assert odp.json()["capabilities"] == {"test": "pytest -q"}


async def test_zmiana_nie_ruszy_sekcji_tickets_nawet_gdy_plik_ma_tam_dane_pasujace_do_nazwy_capability(
    swiat,
):
    _zapisz(swiat.plik, "tickets:\n  test: zrodlo\ncapabilities:\n  lint: ruff\n")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    await _patch(swiat, klient, {"test": "pytest"})

    assert _dane(_tekst(swiat.plik)) == {
        "tickets": {"test": "zrodlo"}, "capabilities": {"lint": "ruff", "test": "pytest"}}


# =================================================================================
# 2. Pusta komenda usuwa capability
# =================================================================================

@pytest.mark.parametrize("pusta", ["", None], ids=["pusty_napis", "null"])
async def test_pusta_komenda_usuwa_capability_z_pliku_a_pozostale_zostaja_z_komentarzami(
    swiat, plik_projektu, pusta,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"lint": pusta})

    assert odp.status_code == 200, odp.text
    tekst = _tekst(swiat.plik)
    assert _dane(tekst)["capabilities"] == {"test": "pytest -q", "build": "make build"}
    assert "lint" not in tekst
    assert "# szybkie testy" in tekst
    assert "# budowanie" in tekst
    assert "# komendy projektu" in tekst
    assert odp.json()["capabilities"] == {"test": "pytest -q", "build": "make build"}
    assert tekst.split("capabilities:")[0] == PROJECT_YAML.split("capabilities:")[0]


@pytest.mark.parametrize("pusta", ["", None], ids=["pusty_napis", "null"])
async def test_usuniecie_nieistniejacej_capability_to_200_a_plik_zostaje_bez_zmian(
    swiat, plik_projektu, pusta,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"nie-ma-takiej": pusta})

    assert odp.status_code == 200, odp.text
    assert _tekst(swiat.plik) == PROJECT_YAML
    assert odp.json()["capabilities"] == {
        "test": "pytest -q", "lint": "ruff check .", "build": "make build"}


@pytest.mark.parametrize("pusta", ["", None], ids=["pusty_napis", "null"])
async def test_usuniecie_capability_z_pliku_bez_sekcji_capabilities_to_200_bez_bledu(
    swiat, pusta,
):
    _zapisz(swiat.plik, PROJECT_YAML_BEZ_CAPABILITIES)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": pusta})

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik)) == _dane(PROJECT_YAML_BEZ_CAPABILITIES)
    assert odp.json()["capabilities"] == {}


async def test_w_jednym_zadaniu_capability_moze_byc_dodana_zmieniona_i_usunieta(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest", "lint": "", "deploy": "make deploy"})

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik))["capabilities"] == {
        "test": "pytest", "build": "make build", "deploy": "make deploy"}


async def test_usuniecie_ostatniej_capability_zostawia_plik_poprawny_a_mape_pusta(swiat):
    _zapisz(swiat.plik, "stack: [go]\ncapabilities:\n  test: go test ./...\n")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": ""})

    assert odp.status_code == 200, odp.text
    dane = _dane(_tekst(swiat.plik))
    assert dane["stack"] == ["go"]
    assert not dane.get("capabilities")
    assert odp.json()["capabilities"] == {}


# =================================================================================
# 3. Zespol i runner projektu
# =================================================================================

async def test_projekt_innego_zespolu_daje_404_a_runner_nie_dostaje_zadnego_zapisu(swiat):
    _zapisz(swiat.plik_b, PROJECT_YAML)
    klient_a = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient_a, {"test": "obce"}, projekt_id=swiat.projekt_b)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []
    assert _tekst(swiat.plik_b) == PROJECT_YAML


async def test_projekt_swojego_zespolu_dziala_mimo_projektu_innego_zespolu_zalozonego_pierwszego(
    swiat, plik_projektu,
):
    _zapisz(swiat.plik_b, PROJECT_YAML)
    klient_b = await swiat.zalogowany(swiat.wlasciciel_b)

    odp = await _patch(swiat, klient_b, {"test": "zmienione-b"}, projekt_id=swiat.projekt_b)

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik_b))["capabilities"]["test"] == "zmienione-b"
    assert _tekst(swiat.plik) == PROJECT_YAML    # plik zespolu A nietkniety
    assert [w[0] for w in swiat.runner.wywolania] == [swiat.runner_b, swiat.runner_b]


async def test_nieistniejacy_projekt_daje_404_bez_zapytania_runnera(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "x"}, projekt_id="nie-ma-takiego")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_identyfikator_projektu_dluzszy_niz_36_znakow_to_422_a_dokladnie_36_to_404(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    za_dlugi = await _patch(swiat, klient, {"test": "x"}, projekt_id="p" * 37)
    graniczny = await _patch(swiat, klient, {"test": "x"}, projekt_id="p" * 36)

    assert za_dlugi.status_code == 422
    assert graniczny.status_code == 404   # walidacja przeszla; takiego projektu nie ma
    assert swiat.runner.wywolania == []


async def test_runner_projektu_offline_daje_409_z_pelnym_komunikatem_i_brak_zapisu(
    swiat, plik_projektu,
):
    for rid in (swiat.runner_a, swiat.runner_a2):
        bus.mark_runner_disconnected(rid)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_runner_projektu_offline_to_409_choc_inny_runner_zespolu_jest_online(
    swiat, plik_projektu,
):
    bus.mark_runner_disconnected(swiat.runner_a)   # runner_a2 tego samego zespolu zyje
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_zapis_idzie_do_runnera_projektu_a_nie_do_pierwszego_runnera_zespolu_po_id(swiat):
    # projekt na runnerze o WYZSZYM id: "pierwszy online po id" wybralby drugi runner
    wyzszy_runner, wyzszy_projekt = max(
        (swiat.runner_a, swiat.projekt_a), (swiat.runner_a2, swiat.projekt_a2))
    _zapisz(swiat.plik, PROJECT_YAML)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "inne"}, projekt_id=wyzszy_projekt)

    assert odp.status_code == 200, odp.text
    assert [w[0] for w in swiat.runner.wywolania] == [wyzszy_runner, wyzszy_runner]


# =================================================================================
# 4. Zly plik projektu: blad dziedziny, nigdy zapis ani 500
# =================================================================================

@pytest.mark.parametrize("tresc", [
    "capabilities:\n  - test\n  - lint\n",
    "capabilities: pytest\n",
], ids=["lista", "napis"])
async def test_capabilities_w_pliku_niebedace_mapa_to_422_i_plik_zostaje_bez_zmian(swiat, tresc):
    _zapisz(swiat.plik, tresc)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_CAPABILITIES_NIE_MAPA}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == tresc


async def test_plik_projektu_niebedacy_mapa_to_422_i_plik_zostaje_bez_zmian(swiat):
    _zapisz(swiat.plik, "- stack\n- capabilities\n")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_PLIK_NIE_MAPA}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == "- stack\n- capabilities\n"


async def test_niepoprawny_yaml_w_pliku_to_422_z_komunikatem_dziedziny_i_bez_zapisu(swiat):
    _zapisz(swiat.plik, "capabilities: [niezamkniete\n")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json()["detail"] == "niepoprawny YAML (linia 2)"
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == "capabilities: [niezamkniete\n"


async def test_zduplikowany_klucz_w_pliku_to_422_z_komunikatem_dziedziny_i_bez_zapisu(swiat):
    tresc = "stack: [go]\nstack: [python]\n"
    _zapisz(swiat.plik, tresc)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json()["detail"] == "zduplikowany klucz (linia 2)"
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == tresc


async def test_brak_pliku_projektu_to_409_z_komunikatem_runnera_i_plik_nie_powstaje(swiat):
    assert not swiat.plik.exists()
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert odp.json()["detail"]
    assert swiat.runner.typy() == ["read_file"]
    assert not swiat.plik.exists()


async def test_odmowa_odczytu_przez_runnera_to_403_a_zapis_nie_jest_probowany(swiat, plik_projektu):
    swiat.runner.wymuszone["read_file"] = {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert swiat.runner.typy() == ["read_file"]


async def test_zly_ksztalt_odpowiedzi_odczytu_to_409_a_zapis_nie_jest_probowany(
    swiat, plik_projektu,
):
    swiat.runner.wymuszone["read_file"] = {"ok": True, "wynik": {"sciezka": str(swiat.plik)}}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert odp.json()["detail"].startswith("odpowiedz runnera ma zly ksztalt")
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_odpowiedz_odczytu_z_trescia_niebedaca_napisem_to_409_a_zapis_nie_jest_probowany(
    swiat, plik_projektu,
):
    swiat.runner.wymuszone["read_file"] = {
        "ok": True, "wynik": {"sciezka": str(swiat.plik), "tresc": 5, "rozmiar": 1}}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert odp.json()["detail"].startswith("odpowiedz runnera ma zly ksztalt")
    assert swiat.runner.typy() == ["read_file"]


async def test_odmowa_zapisu_przez_runnera_poza_zakresem_to_403_a_plik_zostaje_bez_zmian(
    swiat, plik_projektu,
):
    swiat.runner.wymuszone["write_file"] = {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_inna_porazka_zapisu_u_runnera_to_409_z_jego_komunikatem(swiat, plik_projektu):
    swiat.runner.wymuszone["write_file"] = {
        "ok": False, "blad": "brak miejsca na dysku", "kod": "blad_dysku"}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert odp.json() == {"detail": "brak miejsca na dysku"}
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_brak_odpowiedzi_runnera_w_limicie_czasu_to_409_a_nie_500(swiat, plik_projektu):
    swiat.runner.wymuszone["read_file"] = TimeoutError()
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_zapis_opiera_sie_na_tresci_odczytanej_od_runnera_a_nie_na_innej(swiat, plik_projektu):
    # runner oddaje inna tresc niz ta na dysku testu: zapis musi ja wziac z odpowiedzi runnera
    swiat.runner.wymuszone["read_file"] = {"ok": True, "wynik": {
        "sciezka": str(swiat.plik), "rozmiar": 40,
        "tresc": "# tylko od runnera\ncapabilities:\n  lint: ruff\n"}}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 200, odp.text
    tekst = _tekst(swiat.plik)
    assert tekst.startswith("# tylko od runnera\n")
    assert _dane(tekst) == {"capabilities": {"lint": "ruff", "test": "pytest"}}
    assert odp.json()["capabilities"] == {"lint": "ruff", "test": "pytest"}


# =================================================================================
# 5. Walidacja wejscia: zadne zapytanie do runnera
# =================================================================================

ZLE_CIALA = {
    "brak_capabilities": {},
    "capabilities_null": {"capabilities": None},
    "capabilities_lista": {"capabilities": ["test"]},
    "capabilities_napis": {"capabilities": "test"},
    "pusta_nazwa": {"capabilities": {"": "make"}},
    "komenda_liczba": {"capabilities": {"test": 5}},
    "komenda_lista": {"capabilities": {"test": ["make"]}},
    "komenda_bool": {"capabilities": {"test": True}},
    "komenda_same_biale_znaki": {"capabilities": {"test": "   "}},
    "pole_dodatkowe": {"capabilities": {"test": "make"}, "stack": ["go"]},
    "zmiana_tickets_poza_zakresem": {
        "capabilities": {"test": "make"}, "tickets": {"source": "jira"}},
    "nazwa_za_dluga": {"capabilities": {"n" * (MAX_NAZWA_CAPABILITY + 1): "make"}},
    "komenda_za_dluga": {"capabilities": {"test": "k" * (MAX_KOMENDA_CAPABILITY + 1)}},
}


@pytest.mark.parametrize("cialo", ZLE_CIALA.values(), ids=ZLE_CIALA.keys())
async def test_zle_cialo_zmiany_to_422_bez_pytania_runnera_i_bez_zmiany_pliku(
    swiat, plik_projektu, cialo,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await klient.patch(f"/api/projects/{swiat.projekt_a}", json=cialo)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_komenda_z_samych_bialych_znakow_jest_odrzucona_z_komunikatem_o_tej_capability(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"deploy": "   \t"})

    assert odp.status_code == 422
    assert "komenda capability deploy sklada sie z samych bialych znakow" in odp.text


async def test_nazwa_capability_dlugosci_limitu_jest_przyjeta(swiat, plik_projektu):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    nazwa = "n" * MAX_NAZWA_CAPABILITY

    odp = await _patch(swiat, klient, {nazwa: "make"})

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik))["capabilities"][nazwa] == "make"


async def test_komenda_capability_dlugosci_limitu_jest_przyjeta(swiat, plik_projektu):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    komenda = "k" * MAX_KOMENDA_CAPABILITY

    odp = await _patch(swiat, klient, {"deploy": komenda})

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik))["capabilities"]["deploy"] == komenda


async def test_cialo_z_jednym_blednym_polem_nie_dopuszcza_czesciowego_zapisu(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4", "": "zle"})

    assert odp.status_code == 422
    assert _tekst(swiat.plik) == PROJECT_YAML


# =================================================================================
# 6. Uprawnienia i uwierzytelnianie
# =================================================================================

async def test_obserwator_nie_zmieni_capabilities_a_runner_nie_jest_nawet_pytany(
    swiat, plik_projektu,
):
    klient = await swiat.zalogowany(swiat.obserwator_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    assert odp.status_code == 403
    assert swiat.runner.wywolania == []
    assert _tekst(swiat.plik) == PROJECT_YAML


async def test_czlonek_zespolu_moze_zmienic_capabilities_projektu(swiat, plik_projektu):
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    assert odp.status_code == 200, odp.text
    assert _dane(_tekst(swiat.plik))["capabilities"]["test"] == "pytest -q -n 4"


async def test_bez_sesji_zmiana_capabilities_daje_401_a_runner_nie_jest_pytany(
    swiat, plik_projektu,
):
    klient = swiat.nowy_klient()

    odp = await _patch(swiat, klient, {"test": "pytest -q -n 4"})

    assert odp.status_code == 401
    assert swiat.runner.wywolania == []
    assert _tekst(swiat.plik) == PROJECT_YAML


# =================================================================================
# 7. Runda poprawek: kazdy typ bledu parsera, null i pusty plik, ksztalt odpowiedzi runnera
# =================================================================================

KOMUNIKAT_NIEPOPRAWNA_WARTOSC = "niepoprawny YAML: niepoprawna wartosc"
KOMUNIKAT_ZA_GLEBOKO = "niepoprawny YAML: zbyt gleboko zagniezdzona struktura"
KOMUNIKAT_ZLY_KSZTALT_TRESCI = "odpowiedz runnera ma zly ksztalt (pole tresc nie jest napisem)"


@pytest.mark.parametrize("tresc", [
    "utworzono: 2024-02-30\n",                              # parser rzuca ValueError
    "utworzono: 9999-12-31T23:59:59.999999999-23:59\n",     # parser rzuca OverflowError
    "? [a, {b: c}]\n: 1\n",                                 # parser rzuca TypeError (klucz zlozony)
], ids=["niepoprawna_data", "data_poza_zakresem", "klucz_zlozony"])
async def test_plik_z_wartoscia_ktorej_parser_nie_zbuduje_to_422_z_pelnym_komunikatem_i_bez_zapisu(
    swiat, tresc,
):
    _zapisz(swiat.plik, tresc)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_NIEPOPRAWNA_WARTOSC}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == tresc


async def test_plik_zagniezdzony_ponad_stos_parsera_to_422_z_pelnym_komunikatem_i_bez_zapisu(swiat):
    tresc = "x: " + "[" * 5000 + "]" * 5000 + "\n"
    _zapisz(swiat.plik, tresc)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_ZA_GLEBOKO}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == tresc


@pytest.mark.parametrize("tresc", [
    "capabilities:\n",
    "capabilities: null\n",
], ids=["klucz_bez_wartosci", "jawny_null"])
async def test_capabilities_bedace_nullem_w_pliku_to_422_z_pelnym_komunikatem_i_bez_zapisu(
    swiat, tresc,
):
    _zapisz(swiat.plik, tresc)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_CAPABILITIES_NIE_MAPA}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == tresc


@pytest.mark.parametrize("tresc", ["", "# sam komentarz bez zadnych kluczy\n"],
                         ids=["pusty_plik", "sam_komentarz"])
async def test_pusty_plik_projektu_albo_sam_komentarz_to_422_z_pelnym_komunikatem_i_bez_zapisu(
    swiat, tresc,
):
    _zapisz(swiat.plik, tresc)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_PLIK_NIE_MAPA}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == tresc


@pytest.mark.parametrize("wynik", [None, [], ["tresc"], "tresc"],
                         ids=["brak_wyniku", "pusta_lista", "lista_napisow", "napis"])
async def test_odpowiedz_odczytu_niebedaca_slownikiem_to_409_z_pelnym_komunikatem_a_nie_500(
    swiat, plik_projektu, wynik,
):
    swiat.runner.wymuszone["read_file"] = {"ok": True, "wynik": wynik}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_ZLY_KSZTALT_TRESCI}
    assert swiat.runner.typy() == ["read_file"]
    assert _tekst(swiat.plik) == PROJECT_YAML


@pytest.mark.parametrize("wynik", [
    {"sciezka": "x"},
    {"sciezka": "x", "tresc": 5, "rozmiar": 1},
    {"sciezka": "x", "tresc": None, "rozmiar": 1},
], ids=["brak_tresci", "tresc_liczba", "tresc_null"])
async def test_slownik_z_trescia_niebedaca_napisem_to_409_z_pelnym_komunikatem(
    swiat, plik_projektu, wynik,
):
    swiat.runner.wymuszone["read_file"] = {"ok": True, "wynik": wynik}
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _patch(swiat, klient, {"test": "pytest"})

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_ZLY_KSZTALT_TRESCI}
    assert swiat.runner.typy() == ["read_file"]


# --- HAR-180: ksztalt zapisu YAML (wciecia sekwencji, szerokosc linii) -----------------------

PROJECT_YAML_Z_LISTA_MAP = """\
stack:
  - python
tickets:
  zrodla:
    - nazwa: linear
      ustawienia:
        team: HAR
    - nazwa: jira
capabilities:
  test: pytest -q
"""


async def test_zmiana_capability_zachowuje_wciecia_listy_map_w_innej_sekcji_pliku(swiat):
    _zapisz(swiat.plik, PROJECT_YAML_Z_LISTA_MAP)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    await _patch(swiat, klient, {"lint": "ruff check ."})

    assert _tekst(swiat.plik) == PROJECT_YAML_Z_LISTA_MAP + "  lint: ruff check .\n"
    assert _dane(_tekst(swiat.plik))["tickets"] == {
        "zrodla": [{"nazwa": "linear", "ustawienia": {"team": "HAR"}}, {"nazwa": "jira"}]}


def _tekst_z_komenda(komenda: str) -> str:
    return f"capabilities:\n  test: {komenda}\n"


def test_komenda_dajaca_linie_dokladnie_na_szerokosci_4096_znakow_zostaje_w_jednej_linii():
    from cockpit.services.projekt import zmien_capabilities

    komenda = ("ab " * 2000)[:4087] + "z"
    assert len(f"  test: {komenda}") == 4096
    tekst = _tekst_z_komenda(komenda)

    nowy, _ = zmien_capabilities(tekst, {"lint": "ruff"})

    assert nowy == tekst + "  lint: ruff\n"


def test_komenda_dluzsza_o_znak_niz_szerokosc_4096_jest_zawijana_a_wartosc_zostaje_ta_sama():
    from cockpit.services.projekt import zmien_capabilities

    komenda = ("ab " * 2000)[:4088] + "z"
    assert len(f"  test: {komenda}") == 4097
    tekst = _tekst_z_komenda(komenda)

    nowy, capabilities = zmien_capabilities(tekst, {"lint": "ruff"})

    assert nowy != tekst + "  lint: ruff\n"
    assert capabilities == {"test": komenda, "lint": "ruff"}
    assert _dane(nowy)["capabilities"] == {"test": komenda, "lint": "ruff"}

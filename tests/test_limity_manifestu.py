"""HAR-169: limity wpisow manifestu (runner i API), niezmiennosc dataclass, zapis manifestu w silniku.

Limity runnera (`runner.kontekst.sprawdz_limity`) i kokpitu (`WpisManifestuIn` w `EventIn`) musza byc
zgodne: runner odrzuca przed wyslaniem, bo 422 z kokpitu oznaczalby utracone zdarzenie (granica-bledow).
Kazde ograniczenie ma wejscie na granicy (jeden znak ponizej i ponad limit), na ktorym tylko ono decyduje.
"""
# ruff: noqa: F811  (fixture `swiat` importowana z test_odbior_dowody i uzywana jako argument testu)
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace

import pytest

from cockpit.services.errors import BladDziedziny
from runner.agent import zbuduj_prompt
from runner.kontekst import (
    BladManifestu,
    Manifest,
    WpisManifestu,
    sprawdz_limity,
    zloz_manifest,
)
from runner.silnik import PorazkaKroku, _zloz_i_zapisz_manifest
from test_ingest_context_manifest import INT4_MAX, SHA_A, wpis
from test_manifest_kontekstu import LEKCJE, SKILL, swiat_z_kontekstem, zapisz
from test_odbior_dowody import (  # noqa: F401  (swiat to fixture)
    dane_zdarzen,
    odrzucone_bez_zapisu,
    swiat,
    wyslij_ok,
)
from test_polecenie_agenta import spec
from test_silnik_runu import Szpieg, jedno, uruchom

WPIS_POPRAWNY = WpisManifestu(
    warstwa="claude_md_korzenia", sciezka="CLAUDE.md", sha256=SHA_A, rozmiar=10, id="L-001")


def komunikat_wpisu(w: WpisManifestu) -> str:
    return f"wpis manifestu ponad limit pól: {w.warstwa} {w.sciezka}"


# --- limity po stronie runnera ---------------------------------------------------------------

GRANICE_PRZYJETE = [
    pytest.param({"warstwa": "w" * 64}, id="warstwa-64"),
    pytest.param({"sciezka": "s" * 255}, id="sciezka-255"),
    pytest.param({"id": "i" * 64}, id="id-64"),
    pytest.param({"rozmiar": 0}, id="rozmiar-0"),
    pytest.param({"rozmiar": INT4_MAX}, id="rozmiar-max"),
    pytest.param({"sha256": "0" * 64}, id="sha-zera"),
    pytest.param({"sha256": "9" * 64}, id="sha-dziewiatki"),
    pytest.param({"sha256": "a" * 64}, id="sha-a"),
    pytest.param({"sha256": "f" * 64}, id="sha-f"),
    pytest.param({"sha256": None, "rozmiar": None, "id": None, "sciezka": None}, id="brak-pomiaru"),
]


@pytest.mark.parametrize("zmiany", GRANICE_PRZYJETE)
def test_wpis_manifestu_na_granicy_limitu_jest_przyjety_przez_runnera(zmiany):
    sprawdz_limity(Manifest(wpisy=(replace(WPIS_POPRAWNY, **zmiany),)))


GRANICE_ODRZUCONE = [
    pytest.param({"warstwa": "w" * 65}, id="warstwa-65"),
    pytest.param({"sciezka": "s" * 256}, id="sciezka-256"),
    pytest.param({"id": "i" * 65}, id="id-65"),
    pytest.param({"rozmiar": -1}, id="rozmiar-ujemny"),
    pytest.param({"rozmiar": INT4_MAX + 1}, id="rozmiar-ponad-max"),
    pytest.param({"sha256": "A" * 64}, id="sha-wielkie-litery"),
    pytest.param({"sha256": "g" * 64}, id="sha-nie-hex"),
    pytest.param({"sha256": "a" * 63}, id="sha-63"),
    pytest.param({"sha256": "a" * 65}, id="sha-65"),
    pytest.param({"sha256": "a" * 63 + "\n"}, id="sha-nowa-linia-na-koncu"),
]


@pytest.mark.parametrize("zmiany", GRANICE_ODRZUCONE)
def test_wpis_ponad_limit_pola_jest_odrzucony_przez_runner_z_pelnym_komunikatem(zmiany):
    zly = replace(WPIS_POPRAWNY, **zmiany)

    with pytest.raises(BladManifestu) as wyjatek:
        sprawdz_limity(Manifest(wpisy=(WPIS_POPRAWNY, zly)))

    assert type(wyjatek.value) is BladManifestu
    assert str(wyjatek.value) == komunikat_wpisu(zly)


def test_manifest_z_tysiacem_wpisow_jest_przyjety_przez_runnera():
    sprawdz_limity(Manifest(wpisy=(WpisManifestu(warstwa="lekcje"),) * 1000))


def test_manifest_z_tysiac_jednym_wpisem_jest_odrzucony_przez_runner_z_pelnym_komunikatem():
    with pytest.raises(BladManifestu) as wyjatek:
        sprawdz_limity(Manifest(wpisy=(WpisManifestu(warstwa="lekcje"),) * 1001))

    assert str(wyjatek.value) == "manifest ma 1001 wpisów, limit 1000"


def test_blad_manifestu_jest_bledem_dziedziny():
    assert issubclass(BladManifestu, BladDziedziny)


def test_plik_lekcji_nie_w_utf8_to_blad_manifestu_z_pelnym_komunikatem(tmp_path):
    zapisz(tmp_path, LEKCJE, b"## L-001 \xff\xfe nie utf-8")

    with pytest.raises(BladManifestu) as wyjatek:
        zloz_manifest(tmp_path, "budowa", ["a/plan.md"], skill_id=SKILL)

    assert str(wyjatek.value) == "plik lekcji .ai/lessons.md nie jest w UTF-8"
    assert isinstance(wyjatek.value.__cause__, UnicodeDecodeError)


# --- dataclassy manifestu -------------------------------------------------------------------


def test_wpis_manifestu_bez_pomiarow_ma_brak_a_nie_zero_ani_pusty_napis():
    w = WpisManifestu(warstwa="niezmienniki_silnika")

    assert w.sciezka is None
    assert w.sha256 is None
    assert w.rozmiar is None
    assert w.id is None


def test_wpis_manifestu_wymaga_warstwy():
    with pytest.raises(TypeError):
        WpisManifestu()  # type: ignore[call-arg]


def test_wpis_manifestu_ma_pola_w_kolejnosci_warstwa_sciezka_sha256_rozmiar_id():
    assert [f.name for f in fields(WpisManifestu)] == [
        "warstwa", "sciezka", "sha256", "rozmiar", "id"]
    w = WpisManifestu("w", "s", SHA_A, 7, "L-001")
    assert (w.warstwa, w.sciezka, w.sha256, w.rozmiar, w.id) == ("w", "s", SHA_A, 7, "L-001")


def test_wpis_manifestu_jest_niezmienny():
    with pytest.raises(FrozenInstanceError):
        WPIS_POPRAWNY.sha256 = SHA_A.upper()  # type: ignore[misc]


def test_pusty_manifest_nie_ma_wpisow():
    assert Manifest().wpisy == ()


def test_manifest_jest_niezmienny():
    with pytest.raises(FrozenInstanceError):
        Manifest().wpisy = (WPIS_POPRAWNY,)  # type: ignore[misc]


# --- zdanie reguly w prompcie ----------------------------------------------------------------


def test_zdanie_reguly_w_prompcie_mowi_ze_przy_sprzecznosci_wygrywa_plik_wyzej_na_liscie():
    prompt = zbuduj_prompt(spec(kontekst=("CLAUDE.md", ".ai/lessons.md")))

    assert prompt.splitlines()[-1] == (
        "Przeczytaj te pliki; przy sprzeczności instrukcji wygrywa plik wyżej na liście.")


# --- silnik: zapis manifestu i zdarzenie ------------------------------------------------------


async def test_zdarzenie_context_manifest_ma_pusty_tekst(tmp_path):
    s = swiat_z_kontekstem(tmp_path)

    zdarzenia = await uruchom(s, Szpieg())

    assert jedno(zdarzenia, "context_manifest", "a")["text"] == ""


def p_silnika(tmp_path):
    repo, katalog_runu = tmp_path / "wt", tmp_path / "run"
    repo.mkdir()
    katalog_runu.mkdir()
    return SimpleNamespace(worktree=SimpleNamespace(sciezka=repo, katalog_runu=katalog_runu))


async def test_manifest_w_pliku_runu_zachowuje_polskie_znaki_w_sciezkach_bez_ucieczek(tmp_path):
    p = p_silnika(tmp_path)
    zapisz(p.worktree.sciezka, "zażółć/CLAUDE.md", "# katalog\n")

    manifest = await _zloz_i_zapisz_manifest(
        p, SimpleNamespace(id="a", skill=SKILL), ["zażółć/plan.md"])

    surowe = (p.worktree.katalog_runu / "manifest-a.json").read_bytes()
    assert "zażółć/CLAUDE.md".encode("utf-8") in surowe
    assert b"\\u" not in surowe
    assert json.loads(surowe.decode("utf-8")) == manifest.jako_listy()


@pytest.mark.parametrize("zly_id", ["../a", "a/b", "a\\b", "..", ".", ""])
async def test_krok_z_niedozwolonym_identyfikatorem_nie_dostaje_manifestu_i_konczy_sie_porazka(
    tmp_path, zly_id,
):
    p = p_silnika(tmp_path)

    with pytest.raises(PorazkaKroku) as wyjatek:
        await _zloz_i_zapisz_manifest(p, SimpleNamespace(id=zly_id, skill=SKILL), [])

    assert str(wyjatek.value) == f"niedozwolony identyfikator kroku: {zly_id!r}"
    assert list(p.worktree.katalog_runu.iterdir()) == []


async def test_blad_zapisu_pliku_manifestu_to_porazka_kroku_z_nazwa_kroku_i_przyczyna_dysku(
    tmp_path,
):
    p = p_silnika(tmp_path)
    (p.worktree.katalog_runu / "manifest-a.json").mkdir()      # zapis pliku trafia w katalog

    with pytest.raises(PorazkaKroku) as wyjatek:
        await _zloz_i_zapisz_manifest(p, SimpleNamespace(id="a", skill=SKILL), [])

    tekst = str(wyjatek.value)
    assert tekst.startswith("nie można zapisać manifestu kroku a: ")
    assert "manifest-a.json" in tekst
    assert isinstance(wyjatek.value.__cause__, OSError)


# --- API: wpis manifestu w EventIn ------------------------------------------------------------


async def wyslano_i_zapisano(swiat, **zmiany) -> list[dict | None]:
    await wyslij_ok(swiat, "context_manifest", manifest=[wpis(**zmiany)])
    return await dane_zdarzen(swiat, swiat.run_id)


PRZYJETE_PRZEZ_API = [
    pytest.param({"warstwa": "w" * 64}, id="warstwa-64"),
    pytest.param({"sciezka": "s" * 255}, id="sciezka-255"),
    pytest.param({"id": "i" * 64}, id="id-64"),
    pytest.param({"sha256": "0" * 64}, id="sha-zera"),
    pytest.param({"sha256": "9" * 64}, id="sha-dziewiatki"),
    pytest.param({"sha256": "a" * 64}, id="sha-a"),
    pytest.param({"sha256": "f" * 64}, id="sha-f"),
]


@pytest.mark.parametrize("zmiany", PRZYJETE_PRZEZ_API)
async def test_wpis_manifestu_na_granicy_limitu_jest_przyjety_przez_api_i_zapisany(swiat, zmiany):
    zapisane = await wyslano_i_zapisano(swiat, **zmiany)

    assert zapisane == [{"manifest": [wpis(**zmiany)]}]


ODRZUCONE_PRZEZ_API = [
    pytest.param({"warstwa": ""}, id="warstwa-pusta"),
    pytest.param({"warstwa": "w" * 65}, id="warstwa-65"),
    pytest.param({"sciezka": ""}, id="sciezka-pusta"),
    pytest.param({"sciezka": "s" * 256}, id="sciezka-256"),
    pytest.param({"id": ""}, id="id-pusty"),
    pytest.param({"id": "i" * 65}, id="id-65"),
    pytest.param({"sha256": "x" + "a" * 64}, id="sha-przed-64-znakami-cos-jeszcze"),
    pytest.param({"sha256": "a" * 64 + "x"}, id="sha-po-64-znakach-cos-jeszcze"),
    pytest.param({"sha256": "a" * 63}, id="sha-63"),
    pytest.param({"sha256": "G" * 64}, id="sha-wielkie-litery-poza-hex"),
    pytest.param({"sha256": "A" * 64}, id="sha-wielkie-litery-hex"),
    pytest.param({"rozmiar": "12"}, id="rozmiar-napis"),
    pytest.param({"rozmiar": 12.0}, id="rozmiar-zmiennoprzecinkowy"),
    pytest.param({"rozmiar": True}, id="rozmiar-bool"),
    pytest.param({"tresc": "zawartosc pliku"}, id="nieznany-klucz"),
]


@pytest.mark.parametrize("zmiany", ODRZUCONE_PRZEZ_API)
async def test_wpis_manifestu_ponad_limit_albo_zlego_typu_jest_odrzucony_przez_api_bez_zapisu(
    swiat, zmiany,
):
    await odrzucone_bez_zapisu(swiat, "context_manifest", manifest=[wpis(**zmiany)])


async def test_wpis_manifestu_tylko_z_warstwa_jest_przyjety_a_brak_pomiaru_zostaje_brakiem(swiat):
    await wyslij_ok(swiat, "context_manifest", manifest=[{"warstwa": "niezmienniki_silnika"}])

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"manifest": [{
        "warstwa": "niezmienniki_silnika", "sciezka": None, "sha256": None, "rozmiar": None,
        "id": None}]}]


async def test_manifest_z_tysiacem_wpisow_jest_przyjety_przez_api(swiat):
    await wyslij_ok(swiat, "context_manifest", manifest=[{"warstwa": "lekcje"}] * 1000)

    (dane,) = await dane_zdarzen(swiat, swiat.run_id)
    assert len(dane["manifest"]) == 1000


async def test_manifest_z_tysiac_jednym_wpisem_jest_odrzucony_przez_api_bez_zapisu(swiat):
    await odrzucone_bez_zapisu(
        swiat, "context_manifest", manifest=[{"warstwa": "lekcje"}] * 1001)


async def test_pusta_lista_manifestu_jest_zapisana_jako_pusta_lista_a_nie_brak_danych(swiat):
    await wyslij_ok(swiat, "context_manifest", manifest=[])

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"manifest": []}]

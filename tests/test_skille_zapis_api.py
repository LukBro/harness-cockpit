"""HAR-181: zapis skilli z UI przez runnera projektu - `PUT /api/skills/{skill_id}`,
`POST /api/skills`, `DELETE /api/skills/{skill_id}` (parametr `project`; DELETE ma jeszcze `scope`).

Kontrakt (opis w raporcie test-writera):
 * ciala: `ZapisSkilla {scope, frontmatter, brief}` (PUT) i `NowySkill {id, scope, frontmatter, brief}`
   (POST); `scope` to `project` albo `global`; oba bez pol dodatkowych;
 * kokpit sklada plik `---\\n` + frontmatter + `---\\n` + brief (bajt w bajt jak w `SkillSzczegoly`),
   waliduje go przez `skill_z_tekstu` (scisle) PRZED jakimkolwiek zapisem: zly plik to 422;
 * PUT i POST odpowiadaja `SkillSzczegoly` (200 i 201), DELETE odpowiada 204 bez tresci;
 * scope `project` -> `<projekt>/.cockpit/skills/<id>/SKILL.md`, scope `global` ->
   `~/.cockpit/skills/<id>/SKILL.md`; zapis i usuniecie ZAWSZE przez runnera projektu;
 * po zapisie i usunieciu kokpit odswieza katalog, wiec `GET /api/skills` od razu pokazuje skutek;
 * POST na zajete id w zakresie to 409, PUT na skill bez pliku w zakresie to 404 "skill nieznany".
Runner robi PRAWDZIWE operacje na plikach w katalogu testu (`RunnerZPlikami`), wiec asercje czytaja
bajty pliku na dysku. Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from transport_asgi import TransportASGIStrumieniujacy

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Definicja, Project
from cockpit.services import skille_api, zapytania_runnera
from cockpit.services.errors import BlednySkill, StanNiemozliwy
from cockpit.settings import settings
from runner import operacje
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela
from test_dostep import LIMIT_S, _czeka_na_blokade_wiersza, maker_pg, tylko_postgres
from test_projekty_zmiana import (
    KOMUNIKAT_OFFLINE,
    KOMUNIKAT_POZA_ZAKRESEM,
    KOMUNIKAT_PROJEKT_NIEZNANY,
    swiat,
)
from test_skille_api import (
    DOZWOLONE_ROLE,
    KOMUNIKAT_SKILL_NIEZNANY,
    POLA_SZCZEGOLOW,
    _bajty,
    _klient_z_katalogiem,
    _lista,
    _skill,
    _szczegoly,
    _wpisy,
    _wstaw_definicje,
)

FIXTURY_Z_INNYCH_TESTOW = (swiat, maker_pg)   # fixture pytest zaimportowana z innego pliku testow

PODKATALOG = ".cockpit/skills"
FM_X = "id: x\nname: Skill X\nrole: author\n"
FM_BOGATY = (
    '# komentarz zostaje\nid: x\nname: "Skill X"   # nazwa w cudzyslowie\nrole: reviewer\n'
    "consumes: [ plan.md ]\nproduces: [raport.md]\n")
BRIEF_TRUDNY = (
    "Pierwsza linia ze spacjami   \r\n\r\n---\r\nDruga ✓ po linii granicznej\r\n"
    "Bez koncowej nowej linii  \t")
TOZSAMOSC_GIT = [
    "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
]


def _plik(baza: Path, skill_id: str) -> Path:
    return baza / PODKATALOG / skill_id / "SKILL.md"


def _tekst_pliku(frontmatter: str, brief: str) -> str:
    return "---\n" + frontmatter + "---\n" + brief


def _bajty_pliku(plik: Path) -> bytes:
    return plik.read_bytes()


def _drzewo(swiat) -> list[str]:
    """Nazwy wszystkich plikow i katalogow w katalogu testu (bez pliku bazy)."""
    baza = swiat.korzen.parent
    return sorted(str(p.relative_to(baza)) for p in baza.rglob("*")
                  if not p.name.startswith("zmiana.db"))


def _dane_zapisu(runner, typ: str) -> list[tuple]:
    return [w for w in runner.wywolania if w[1] == typ]


async def _put(klient, skill_id, projekt, scope="project", frontmatter=FM_X, brief="Brief.\n"):
    return await klient.put(
        f"/api/skills/{skill_id}", params={"project": projekt},
        json={"scope": scope, "frontmatter": frontmatter, "brief": brief})


async def _post(klient, projekt, skill_id, scope="project", frontmatter=None, brief="Brief.\n"):
    if frontmatter is None:
        frontmatter = f"id: {skill_id}\nname: Skill {skill_id}\nrole: author\n"
    return await klient.post(
        "/api/skills", params={"project": projekt},
        json={"id": skill_id, "scope": scope, "frontmatter": frontmatter, "brief": brief})


async def _delete(klient, skill_id, projekt, scope="project"):
    params = {"project": projekt} if scope is None else {"project": projekt, "scope": scope}
    return await klient.delete(f"/api/skills/{skill_id}", params=params)


def _czytelny_blad(odp, swiat) -> None:
    """Blad pokazywany czlowiekowi: napis dziedziny, bez sladu stosu i bez sciezek z dysku."""
    assert isinstance(odp.json()["detail"], str) and odp.json()["detail"]
    assert "Traceback" not in odp.text
    assert 'File "' not in odp.text
    assert str(swiat.korzen.parent) not in odp.text


# =================================================================================
# 1. PUT scope project: nadpisanie SKILL.md w repo
# =================================================================================

async def test_zapis_skilla_projektu_sklada_plik_z_frontmattera_i_briefu_bajt_w_bajt(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=FM_BOGATY, brief=BRIEF_TRUDNY)

    assert odp.status_code == 200, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == _tekst_pliku(FM_BOGATY, BRIEF_TRUDNY).encode()
    assert not (swiat.dom / PODKATALOG / "x").exists()


async def test_odpowiedz_na_zapis_skilla_to_szczegoly_zapisanego_skilla_z_frontmatterem_i_briefem(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=FM_BOGATY, brief=BRIEF_TRUDNY)

    assert set(odp.json()) == POLA_SZCZEGOLOW
    assert odp.json() == {
        "id": "x", "name": "Skill X", "role": "reviewer", "consumes": ["plan.md"],
        "produces": ["raport.md"], "zakres": "lokalny", "sciezka": ".cockpit/skills/x/SKILL.md",
        "frontmatter": FM_BOGATY, "brief": BRIEF_TRUDNY}
    szczegoly = await _szczegoly(klient, "x", swiat.projekt_a)
    assert szczegoly.json() == odp.json()


async def test_zapis_idzie_do_runnera_projektu_z_bezwzgledna_sciezka_pliku_i_gotowa_trescia(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    await _put(klient, "x", swiat.projekt_a, frontmatter=FM_BOGATY, brief=BRIEF_TRUDNY)

    (zapis,) = _dane_zapisu(swiat.runner, "write_file")
    assert zapis[0] == swiat.runner_a
    assert zapis[2]["sciezka"] == f"{swiat.korzen}/.cockpit/skills/x/SKILL.md"
    assert zapis[2]["tresc"] == _tekst_pliku(FM_BOGATY, BRIEF_TRUDNY)


async def test_ponowny_zapis_tego_co_oddaly_szczegoly_nie_zmienia_bajtow_pliku(swiat):
    _bajty(swiat.korzen, PODKATALOG, "x",
           "---\n# komentarz\nid: x\nname:   'Skill X'\nrole: author\n---\n" + BRIEF_TRUDNY)
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)
    szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=szczegoly["frontmatter"],
                     brief=szczegoly["brief"])

    assert odp.status_code == 200, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


def _repo_git(korzen: Path) -> None:
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "start"]):
        subprocess.run(["git", *TOZSAMOSC_GIT, "-C", str(korzen), *argv],
                       check=True, capture_output=True, text=True, timeout=60)


def _git(korzen: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *TOZSAMOSC_GIT, "-C", str(korzen), *argv],
        check=True, capture_output=True, text=True, timeout=60).stdout


async def test_zapis_tego_samego_briefu_nie_zostawia_zmian_w_git_diff_prawdziwego_repozytorium(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", brief="Linia 1  \nLinia 2\r\n\nLinia 3")
    _repo_git(swiat.korzen)
    klient = await _klient_z_katalogiem(swiat)
    szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=szczegoly["frontmatter"],
                     brief=szczegoly["brief"])

    assert odp.status_code == 200, odp.text
    assert _git(swiat.korzen, "diff", "--numstat") == ""
    assert _git(swiat.korzen, "status", "--porcelain") == ""


async def test_zmiana_jednej_linii_briefu_to_w_git_diff_jedna_linia_dodana_i_jedna_usunieta(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", brief="Linia 1\nLinia 2\nLinia 3\n")
    _repo_git(swiat.korzen)
    klient = await _klient_z_katalogiem(swiat)
    szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=szczegoly["frontmatter"],
                     brief="Linia 1\nLinia ZMIENIONA\nLinia 3\n")

    assert odp.status_code == 200, odp.text
    assert _git(swiat.korzen, "diff", "--numstat") == "1\t1\t.cockpit/skills/x/SKILL.md\n"
    assert _git(swiat.korzen, "status", "--porcelain") == " M .cockpit/skills/x/SKILL.md\n"


async def test_zapis_skilla_z_pustym_briefem_zostawia_plik_sam_z_frontmatterem(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, brief="")

    assert odp.status_code == 200, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == b"---\n" + FM_X.encode() + b"---\n"
    assert odp.json()["brief"] == ""


async def test_po_zapisie_lista_skilli_od_razu_pokazuje_nowe_dane_bez_recznego_odswiezania(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", produces=("stary.md",))
    klient = await _klient_z_katalogiem(swiat)

    await _put(klient, "x", swiat.projekt_a, frontmatter=FM_BOGATY)

    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    assert (wpis["name"], wpis["role"], wpis["consumes"], wpis["produces"]) == (
        "Skill X", "reviewer", ["plan.md"], ["raport.md"])


async def test_czlonek_zespolu_moze_zapisac_skill(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat, swiat.czlonek_a)

    odp = await _put(klient, "x", swiat.projekt_a, brief="Od czlonka.\n")

    assert odp.status_code == 200, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "x")).endswith(b"---\nOd czlonka.\n")


async def test_zapis_skilla_ktorego_nie_ma_w_zakresie_to_404_a_plik_nie_powstaje(swiat):
    _skill(swiat.dom, PODKATALOG, "tylko-globalny")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "tylko-globalny", swiat.projekt_a, scope="project",
                     frontmatter="id: tylko-globalny\nname: T\nrole: author\n")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_SKILL_NIEZNANY}
    assert not _plik(swiat.korzen, "tylko-globalny").exists()
    assert _dane_zapisu(swiat.runner, "write_file") == []


# ---- walidacja pliku przed zapisem -----------------------------------------------------------

ZLE_PLIKI = {
    "nieznana_rola": (
        "id: x\nname: X\nrole: czarodziej\n", "Brief.\n",
        f"nieznana rola 'czarodziej' w 'x' (dozwolone: {DOZWOLONE_ROLE})"),
    "brak_roli": ("id: x\nname: X\n", "Brief.\n", "brak pola 'role' w 'x'"),
    "brak_id": ("name: X\nrole: author\n", "Brief.\n", "brak pola 'id' w 'x'"),
    "id_inne_niz_w_adresie": (
        "id: inny\nname: X\nrole: author\n", "Brief.\n",
        "id 'inny' rozni sie od nazwy katalogu 'x' w 'x'"),
    "frontmatter_bez_koncowej_nowej_linii": (
        "id: x\nname: X\nrole: author", "Brief.\n", "niezamkniety frontmatter w 'x'"),
    "rola_bez_zapisu_z_dwoma_artefaktami": (
        "id: x\nname: X\nrole: reviewer\nproduces: [a.md, b.md]\n", "Brief.\n",
        "skill x: rola reviewer oddaje jeden artefakt, produces ma 2: a.md, b.md"),
    "recenzent_z_prawem_zapisu": ("id: x\nname: X\nrole: reviewer\nwrite: true\n", "Brief.\n", None),
}


@pytest.mark.parametrize(("frontmatter", "brief", "komunikat"), ZLE_PLIKI.values(),
                         ids=ZLE_PLIKI.keys())
async def test_zapis_pliku_ktory_nie_jest_poprawnym_skillem_to_422_a_stary_plik_zostaje(
    swiat, frontmatter, brief, komunikat,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=frontmatter, brief=brief)

    assert odp.status_code == 422
    if komunikat is not None:
        assert odp.json() == {"detail": komunikat}
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


@pytest.mark.parametrize(("frontmatter", "brief", "komunikat"), [
    z for k, z in ZLE_PLIKI.items() if k not in ("id_inne_niz_w_adresie",)
], ids=[k for k in ZLE_PLIKI if k != "id_inne_niz_w_adresie"])
async def test_dodanie_pliku_ktory_nie_jest_poprawnym_skillem_to_422_a_nic_nie_powstaje(
    swiat, frontmatter, brief, komunikat,
):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    odp = await _post(klient, swiat.projekt_a, "x", frontmatter=frontmatter, brief=brief)

    assert odp.status_code == 422
    if komunikat is not None:
        assert odp.json() == {"detail": komunikat}
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert _drzewo(swiat) == przed


async def test_dodanie_skilla_z_id_w_frontmatterze_innym_niz_podane_id_to_422_a_nic_nie_powstaje(
    swiat,
):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    odp = await _post(klient, swiat.projekt_a, "nowy",
                      frontmatter="id: inny\nname: N\nrole: author\n")

    assert odp.status_code == 422
    assert odp.json() == {"detail": "id 'inny' rozni sie od nazwy katalogu 'nowy' w 'nowy'"}
    assert _drzewo(swiat) == przed


# ---- ksztalt wejscia: zadne zapytanie do runnera ----------------------------------------------

ZLE_CIALA_ZAPISU = {
    "brak_scope": {"frontmatter": FM_X, "brief": "B\n"},
    "scope_nieznany": {"scope": "team", "frontmatter": FM_X, "brief": "B\n"},
    "scope_null": {"scope": None, "frontmatter": FM_X, "brief": "B\n"},
    "scope_wielkimi": {"scope": "PROJECT", "frontmatter": FM_X, "brief": "B\n"},
    "brak_frontmattera": {"scope": "project", "brief": "B\n"},
    "frontmatter_liczba": {"scope": "project", "frontmatter": 5, "brief": "B\n"},
    "brak_briefu": {"scope": "project", "frontmatter": FM_X},
    "brief_null": {"scope": "project", "frontmatter": FM_X, "brief": None},
    "pole_dodatkowe": {"scope": "project", "frontmatter": FM_X, "brief": "B\n", "sciezka": "/etc/x"},
}


@pytest.mark.parametrize("cialo", ZLE_CIALA_ZAPISU.values(), ids=ZLE_CIALA_ZAPISU.keys())
async def test_zle_cialo_zapisu_skilla_to_422_bez_pytania_runnera_i_bez_zmiany_pliku(swiat, cialo):
    _skill(swiat.korzen, PODKATALOG, "x")
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await klient.put("/api/skills/x", params={"project": swiat.projekt_a}, json=cialo)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


@pytest.mark.parametrize("cialo", [{**c, "id": "nowy"} for c in ZLE_CIALA_ZAPISU.values()],
                         ids=ZLE_CIALA_ZAPISU.keys())
async def test_zle_cialo_dodania_skilla_to_422_bez_pytania_runnera_i_bez_nowych_plikow(swiat, cialo):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    odp = await klient.post("/api/skills", params={"project": swiat.projekt_a}, json=cialo)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


async def test_dodanie_skilla_bez_id_w_ciele_to_422_bez_pytania_runnera(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await klient.post("/api/skills", params={"project": swiat.projekt_a},
                            json={"scope": "project", "frontmatter": FM_X, "brief": "B\n"})

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []


NIEBEZPIECZNE_ID = ["../x", "a/b", "..", ".", "", "a\\b", "a b", "x" * 300, "a:b", "x\u0000y",
                    "/etc/passwd", "~/x"]


@pytest.mark.parametrize("skill_id", NIEBEZPIECZNE_ID,
                         ids=[f"niebezpieczne_id_{n}" for n in range(len(NIEBEZPIECZNE_ID))])
async def test_dodanie_skilla_o_id_niebedacym_bezpieczna_nazwa_katalogu_to_422_i_nic_nie_powstaje(
    swiat, skill_id,
):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)
    # id we frontmatterze rowne podanemu id: gdyby wzorzec id nie istnial, plik przeszedlby walidacje
    frontmatter = f"id: {json.dumps(skill_id)}\nname: X\nrole: author\n"

    odp = await _post(klient, swiat.projekt_a, skill_id, frontmatter=frontmatter)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("skill_id", ["a b", "a:b", "x" * 300], ids=["spacja", "dwukropek", "dlugie"])
async def test_zapis_i_usuniecie_skilla_o_id_niebedacym_bezpieczna_nazwa_to_422_a_nie_404(
    swiat, skill_id,
):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)
    frontmatter = f"id: {json.dumps(skill_id)}\nname: X\nrole: author\n"

    zapis = await _put(klient, skill_id, swiat.projekt_a, frontmatter=frontmatter)
    usuniecie = await _delete(klient, skill_id, swiat.projekt_a)

    assert (zapis.status_code, usuniecie.status_code) == (422, 422)
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("skill_id", ["nowy-skill", "skill_2", "a"])
async def test_dodanie_skilla_o_bezpiecznym_id_tworzy_plik_w_katalogu_o_tej_nazwie(swiat, skill_id):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, skill_id)

    assert odp.status_code == 201, odp.text
    assert _plik(swiat.korzen, skill_id).is_file()


# =================================================================================
# 2. PUT scope global: rejestr domowy runnera
# =================================================================================

async def test_zapis_skilla_globalnego_trafia_do_rejestru_domowego_runnera_a_repo_zostaje(swiat):
    _skill(swiat.dom, PODKATALOG, "glob", name="Stary globalny")
    klient = await _klient_z_katalogiem(swiat)
    przed_repo = _drzewo(swiat)

    odp = await _put(klient, "glob", swiat.projekt_a, scope="global",
                     frontmatter="id: glob\nname: Nowy globalny\nrole: gatherer\n",
                     brief=BRIEF_TRUDNY)

    assert odp.status_code == 200, odp.text
    assert _bajty_pliku(_plik(swiat.dom, "glob")) == _tekst_pliku(
        "id: glob\nname: Nowy globalny\nrole: gatherer\n", BRIEF_TRUDNY).encode()
    assert _drzewo(swiat) == przed_repo
    (zapis,) = _dane_zapisu(swiat.runner, "write_file")
    assert zapis[0] == swiat.runner_a
    assert zapis[2]["sciezka"] == "~/.cockpit/skills/glob/SKILL.md"
    assert (odp.json()["zakres"], odp.json()["sciezka"]) == (
        "globalny", "~/.cockpit/skills/glob/SKILL.md")


async def test_po_zapisie_skilla_globalnego_lista_pokazuje_go_jako_globalny_z_nowymi_danymi(swiat):
    _skill(swiat.dom, PODKATALOG, "glob", name="Stary globalny")
    klient = await _klient_z_katalogiem(swiat)

    await _put(klient, "glob", swiat.projekt_a, scope="global",
               frontmatter="id: glob\nname: Nowy globalny\nrole: gatherer\n")

    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["glob"]
    assert (wpis["name"], wpis["role"], wpis["zakres"]) == ("Nowy globalny", "gatherer", "globalny")


async def test_zapis_skilla_globalnego_nie_rusza_lokalnego_o_tym_samym_id_a_lista_zostaje_nadpisany(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    lokalny = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, scope="global",
                     frontmatter="id: x\nname: Nowy z domu\nrole: author\n")

    assert odp.status_code == 200, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == lokalny
    assert b"Nowy z domu" in _bajty_pliku(_plik(swiat.dom, "x"))
    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    assert (wpis["name"], wpis["zakres"]) == ("Z repo", "nadpisany")


async def test_zapis_skilla_projektu_nie_rusza_globalnego_o_tym_samym_id(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    globalny = _bajty_pliku(_plik(swiat.dom, "x"))
    klient = await _klient_z_katalogiem(swiat)

    await _put(klient, "x", swiat.projekt_a, scope="project",
               frontmatter="id: x\nname: Nowy z repo\nrole: author\n")

    assert _bajty_pliku(_plik(swiat.dom, "x")) == globalny
    assert b"Nowy z repo" in _bajty_pliku(_plik(swiat.korzen, "x"))


# =================================================================================
# 3. POST: nowy skill i zajete id
# =================================================================================

async def test_nowy_skill_projektu_powstaje_w_nieistniejacym_katalogu_i_odpowiedz_to_201_ze_szczegolami(
    swiat,
):
    assert not (swiat.korzen / ".cockpit" / "skills").exists()
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "nowy", frontmatter=FM_BOGATY.replace("id: x", "id: nowy"),
                      brief=BRIEF_TRUDNY)

    assert odp.status_code == 201, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "nowy")) == _tekst_pliku(
        FM_BOGATY.replace("id: x", "id: nowy"), BRIEF_TRUDNY).encode()
    assert set(odp.json()) == POLA_SZCZEGOLOW
    assert (odp.json()["id"], odp.json()["zakres"], odp.json()["sciezka"]) == (
        "nowy", "lokalny", ".cockpit/skills/nowy/SKILL.md")
    assert odp.json()["brief"] == BRIEF_TRUDNY


async def test_nowy_skill_globalny_powstaje_w_rejestrze_domowym_runnera(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "glob", scope="global")

    assert odp.status_code == 201, odp.text
    assert _plik(swiat.dom, "glob").is_file()
    assert not (swiat.korzen / ".cockpit" / "skills").exists()
    (zapis,) = _dane_zapisu(swiat.runner, "write_file")
    assert zapis[2]["sciezka"] == "~/.cockpit/skills/glob/SKILL.md"
    assert odp.json()["zakres"] == "globalny"


async def test_po_dodaniu_skilla_lista_od_razu_go_pokazuje_bez_recznego_odswiezania(swiat):
    klient = await _klient_z_katalogiem(swiat)

    await _post(klient, swiat.projekt_a, "nowy")

    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["nowy"]
    assert (wpis["name"], wpis["role"], wpis["zakres"]) == ("Skill nowy", "author", "lokalny")


async def test_dodanie_skilla_o_zajetym_id_w_projekcie_to_409_a_stary_plik_zostaje_bajt_w_bajt(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", brief=BRIEF_TRUDNY)
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "x", brief="Nowy brief.\n")

    assert odp.status_code == 409
    _czytelny_blad(odp, swiat)
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


async def test_dodanie_skilla_o_zajetym_id_globalnym_to_409_a_plik_w_domu_zostaje(swiat):
    _skill(swiat.dom, PODKATALOG, "glob", brief=BRIEF_TRUDNY)
    oryginal = _bajty_pliku(_plik(swiat.dom, "glob"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "glob", scope="global")

    assert odp.status_code == 409
    assert _bajty_pliku(_plik(swiat.dom, "glob")) == oryginal


async def test_zajete_id_sprawdza_dysk_a_nie_tylko_cache_wiec_plik_dodany_po_odswiezeniu_tez_daje_409(
    swiat,
):
    klient = await _klient_z_katalogiem(swiat)
    _skill(swiat.korzen, PODKATALOG, "swiezy", brief=BRIEF_TRUDNY)   # po odswiezeniu katalogu
    oryginal = _bajty_pliku(_plik(swiat.korzen, "swiezy"))

    odp = await _post(klient, swiat.projekt_a, "swiezy")

    assert odp.status_code == 409
    assert _bajty_pliku(_plik(swiat.korzen, "swiezy")) == oryginal


async def test_id_zajete_tylko_globalnie_mozna_uzyc_w_projekcie_a_lista_pokazuje_nadpisany(swiat):
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    globalny = _bajty_pliku(_plik(swiat.dom, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "x", scope="project",
                      frontmatter="id: x\nname: Z repo\nrole: author\n")

    assert odp.status_code == 201, odp.text
    assert _bajty_pliku(_plik(swiat.dom, "x")) == globalny
    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    assert (wpis["name"], wpis["zakres"]) == ("Z repo", "nadpisany")


async def test_id_zajete_tylko_w_projekcie_mozna_uzyc_jako_globalne(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    lokalny = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "x", scope="global",
                      frontmatter="id: x\nname: Z domu\nrole: author\n")

    assert odp.status_code == 201, odp.text
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == lokalny
    assert _plik(swiat.dom, "x").is_file()


async def test_dodanie_skilla_o_id_zajetym_w_zrodle_konwencyjnym_nie_jest_konfliktem_w_zakresie_projektu(
    swiat,
):
    _skill(swiat.korzen, ".claude/skills", "x", name="Z claude")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "x")

    assert odp.status_code == 201, odp.text
    assert _plik(swiat.korzen, "x").is_file()
    assert (swiat.korzen / ".claude" / "skills" / "x" / "SKILL.md").is_file()


async def test_dodanie_skilla_przez_runner_drugiego_projektu_idzie_do_runnera_tego_projektu(swiat):
    # oba projekty zespolu maja te sama sciezke; runner z wyzszym id nie moze byc zastapiony pierwszym
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)
    swiat.runner.wywolania.clear()

    odp = await _post(klient, swiat.projekt_a2, "nowy")

    assert odp.status_code == 201, odp.text
    assert [w[0] for w in _dane_zapisu(swiat.runner, "write_file")] == [swiat.runner_a2]
    assert _plik(swiat.korzen, "nowy").is_file()


async def test_zapis_skilla_pierwszego_projektu_idzie_do_jego_runnera_nawet_gdy_drugi_runner_jest_online(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)
    swiat.runner.wywolania.clear()

    odp = await _put(klient, "x", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert [w[0] for w in _dane_zapisu(swiat.runner, "write_file")] == [swiat.runner_a]


# =================================================================================
# 4. DELETE: usuniecie lokalnego, globalny znow widoczny
# =================================================================================

async def test_usuniecie_skilla_projektu_kasuje_tylko_jego_plik_skilla_i_odpowiada_204(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    notatki = _plik(swiat.korzen, "x").parent / "notatki.md"
    notatki.write_text("zostaja", encoding="utf-8")
    inny = _skill(swiat.korzen, PODKATALOG, "inny")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _delete(klient, "x", swiat.projekt_a)

    assert odp.status_code == 204
    assert odp.content == b""
    assert not _plik(swiat.korzen, "x").exists()
    assert notatki.read_text(encoding="utf-8") == "zostaja"
    assert inny.is_file()


async def test_usuniecie_idzie_do_runnera_projektu_jako_delete_file_z_bezwzgledna_sciezka(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    await _delete(klient, "x", swiat.projekt_a)

    assert _dane_zapisu(swiat.runner, "delete_file") == [(
        swiat.runner_a, "delete_file", {"sciezka": f"{swiat.korzen}/.cockpit/skills/x/SKILL.md"})]


async def test_po_usunieciu_lokalnego_skilla_globalny_o_tym_samym_id_jest_znow_widoczny_jako_globalny(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu", role="reviewer", produces=("g.md",))
    klient = await _klient_z_katalogiem(swiat)
    przed = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    assert (przed["name"], przed["zakres"]) == ("Z repo", "nadpisany")

    odp = await _delete(klient, "x", swiat.projekt_a, scope="project")

    assert odp.status_code == 204
    po = _wpisy(await _lista(klient, swiat.projekt_a))["x"]   # bez recznego odswiezenia katalogu
    assert (po["name"], po["role"], po["produces"], po["zakres"]) == (
        "Z domu", "reviewer", ["g.md"], "globalny")
    assert _plik(swiat.dom, "x").is_file()


async def test_po_usunieciu_skilla_ktory_nie_mial_odpowiednika_znika_z_listy(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    _skill(swiat.korzen, PODKATALOG, "zostaje")
    klient = await _klient_z_katalogiem(swiat)

    await _delete(klient, "x", swiat.projekt_a)

    assert list(_wpisy(await _lista(klient, swiat.projekt_a))) == ["zostaje"]
    assert (await _szczegoly(klient, "x", swiat.projekt_a)).status_code == 404


async def test_usuniecie_skilla_globalnego_kasuje_plik_w_domu_a_lokalny_zostaje_lokalnym(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    lokalny = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _delete(klient, "x", swiat.projekt_a, scope="global")

    assert odp.status_code == 204
    assert not _plik(swiat.dom, "x").exists()
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == lokalny
    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    assert (wpis["name"], wpis["zakres"]) == ("Z repo", "lokalny")
    assert _dane_zapisu(swiat.runner, "delete_file")[0][2] == {
        "sciezka": "~/.cockpit/skills/x/SKILL.md"}


async def test_usuniecie_lokalnego_nie_rusza_globalnego_pliku_na_dysku(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    _skill(swiat.dom, PODKATALOG, "x", brief=BRIEF_TRUDNY)
    globalny = _bajty_pliku(_plik(swiat.dom, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _delete(klient, "x", swiat.projekt_a, scope="project")

    assert odp.status_code == 204
    assert not _plik(swiat.korzen, "x").exists()
    assert _bajty_pliku(_plik(swiat.dom, "x")) == globalny


@pytest.mark.parametrize("scope", [None, "team", "PROJECT", ""], ids=["brak", "nieznany", "wielkie", "pusty"])
async def test_usuniecie_bez_poprawnego_zakresu_to_422_a_pliki_zostaja(swiat, scope):
    _skill(swiat.korzen, PODKATALOG, "x")
    _skill(swiat.dom, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    odp = await _delete(klient, "x", swiat.projekt_a, scope=scope)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


async def test_usuniecie_skilla_ktorego_plik_jest_tylko_w_innym_zakresie_to_409_a_ten_plik_zostaje(
    swiat,
):
    _skill(swiat.dom, PODKATALOG, "tylko-globalny")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _delete(klient, "tylko-globalny", swiat.projekt_a, scope="project")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "sciezka nie istnieje"}
    assert _plik(swiat.dom, "tylko-globalny").is_file()


async def test_usuniecie_dotyczy_tylko_zrodla_kokpitu_a_skill_ze_zrodla_konwencyjnego_zostaje(swiat):
    konwencyjny = _skill(swiat.korzen, ".claude/skills", "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _delete(klient, "x", swiat.projekt_a, scope="project")

    assert odp.status_code == 409
    assert konwencyjny.is_file()


# =================================================================================
# 5. Bledy zapisu: czytelny komunikat, bez sladu stosu
# =================================================================================

async def _wywolaj(klient, rodzaj, swiat):
    if rodzaj == "put":
        return await _put(klient, "x", swiat.projekt_a)
    if rodzaj == "post":
        return await _post(klient, swiat.projekt_a, "nowy")
    return await _delete(klient, "x", swiat.projekt_a)


RODZAJE = ["put", "post", "delete"]
TYP_RUNNERA = {"put": "write_file", "post": "write_file", "delete": "delete_file"}


@pytest.mark.parametrize("rodzaj", RODZAJE)
async def test_runner_offline_daje_409_z_pelnym_komunikatem_a_pliki_zostaja(swiat, rodzaj):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    for rid in (swiat.runner_a, swiat.runner_a2):
        bus.mark_runner_disconnected(rid)
    przed = _drzewo(swiat)

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("rodzaj", RODZAJE)
async def test_runner_projektu_offline_to_409_choc_inny_runner_zespolu_jest_online(swiat, rodzaj):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_disconnected(swiat.runner_a)
    bus.mark_runner_connected(swiat.runner_a2)
    przed = _drzewo(swiat)

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("rodzaj", RODZAJE)
async def test_odmowa_runnera_poza_zakresem_to_403_z_komunikatem_dziedziny(swiat, rodzaj):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone[TYP_RUNNERA[rodzaj]] = {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"}
    przed = _drzewo(swiat)

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("rodzaj", RODZAJE)
async def test_inna_porazka_runnera_to_409_z_jego_komunikatem_bez_sladu_stosu(swiat, rodzaj):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone[TYP_RUNNERA[rodzaj]] = {
        "ok": False, "blad": "blad dysku przy operacji na pliku", "kod": "blad_dysku"}

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 409
    assert odp.json() == {"detail": "blad dysku przy operacji na pliku"}
    _czytelny_blad(odp, swiat)


@pytest.mark.parametrize("rodzaj", RODZAJE)
async def test_brak_odpowiedzi_runnera_w_limicie_czasu_to_409_a_nie_500(swiat, rodzaj):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone[TYP_RUNNERA[rodzaj]] = TimeoutError()
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 409
    _czytelny_blad(odp, swiat)
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


async def test_projekt_poza_zakresem_runnera_daje_403_a_nic_nie_powstaje_w_cudzym_katalogu(swiat):
    obcy = swiat.korzen.parent / "obcy"
    obcy.mkdir()
    async with swiat.maker() as s:
        projekt = Project(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, path=str(obcy),
                          name="obcy")
        s.add(projekt)
        await s.commit()
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, projekt.id, "nowy")

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert list(obcy.iterdir()) == []


async def test_katalog_skilla_bedacy_dowiazaniem_poza_zakres_daje_403_a_cel_zostaje_nietkniety(swiat):
    obcy = swiat.korzen.parent / "obcy"
    obcy.mkdir()
    cel = obcy / "SKILL.md"
    cel.write_bytes(_tekst_pliku(FM_X, "Cudzy.\n").encode())
    (swiat.korzen / PODKATALOG).mkdir(parents=True)
    (swiat.korzen / PODKATALOG / "x").symlink_to(obcy, target_is_directory=True)
    klient = await _klient_z_katalogiem(swiat)
    cudzy = cel.read_bytes()

    # dowiazany skill jest poza zakresem, wiec nie ma go w katalogu: zapis idzie przez POST
    zapis = await _post(klient, swiat.projekt_a, "x")
    usuniecie = await _delete(klient, "x", swiat.projekt_a)

    assert (zapis.status_code, usuniecie.status_code) == (403, 403)
    assert zapis.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert cel.read_bytes() == cudzy
    assert [p.name for p in cel.parent.iterdir()] == ["SKILL.md"]


async def test_plik_skilla_bedacy_dowiazaniem_daje_403_a_dowiazanie_i_cel_zostaja(swiat):
    cel = swiat.korzen / "cel.md"
    cel.write_bytes(_tekst_pliku(FM_X, "Cel.\n").encode())
    (swiat.korzen / PODKATALOG / "x").mkdir(parents=True)
    _plik(swiat.korzen, "x").symlink_to(cel)
    klient = await _klient_z_katalogiem(swiat)
    tresc_celu = cel.read_bytes()

    zapis = await _put(klient, "x", swiat.projekt_a)
    usuniecie = await _delete(klient, "x", swiat.projekt_a)

    assert (zapis.status_code, usuniecie.status_code) == (403, 403)
    assert _plik(swiat.korzen, "x").is_symlink()
    assert cel.read_bytes() == tresc_celu


@pytest.mark.skipif(os.geteuid() == 0, reason="root pisze do katalogow bez wzgledu na prawa")
@pytest.mark.parametrize("rodzaj", ["put", "delete"])
async def test_katalog_skilla_bez_prawa_zapisu_daje_409_z_komunikatem_o_uprawnieniach_bez_sladu_stosu(
    swiat, rodzaj,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    katalog = _plik(swiat.korzen, "x").parent
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)
    katalog.chmod(0o500)
    try:
        odp = await _wywolaj(klient, rodzaj, swiat)
    finally:
        katalog.chmod(0o700)

    assert odp.status_code == 409
    assert odp.json() == {"detail": "brak uprawnien do pliku lub katalogu"}
    _czytelny_blad(odp, swiat)
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


async def test_nieudany_zapis_nie_zmienia_listy_skilli(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", name="Przed")
    klient = await _klient_z_katalogiem(swiat)
    przed = (await _lista(klient, swiat.projekt_a)).json()
    swiat.runner.wymuszone["write_file"] = {
        "ok": False, "blad": "blad dysku przy operacji na pliku", "kod": "blad_dysku"}

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")

    assert odp.status_code == 409
    assert (await _lista(klient, swiat.projekt_a)).json() == przed


# =================================================================================
# 6. Zespol i projekt
# =================================================================================

TRASY_ZAPISU = ["put", "post", "delete"]


@pytest.mark.parametrize("rodzaj", TRASY_ZAPISU)
async def test_projekt_innego_zespolu_daje_404_a_runner_i_pliki_zespolu_b_zostaja_nietkniete(
    swiat, rodzaj,
):
    _skill(swiat.korzen_b, PODKATALOG, "x")
    obcy_plik = _plik(swiat.korzen_b, "x")
    oryginal = _bajty_pliku(obcy_plik)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    przed = _drzewo(swiat)

    if rodzaj == "put":
        odp = await _put(klient, "x", swiat.projekt_b)
    elif rodzaj == "post":
        odp = await _post(klient, swiat.projekt_b, "nowy")
    else:
        odp = await _delete(klient, "x", swiat.projekt_b)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []
    assert _bajty_pliku(obcy_plik) == oryginal
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("rodzaj", TRASY_ZAPISU)
async def test_nieistniejacy_projekt_daje_404_na_trasach_zapisu_skilli(swiat, rodzaj):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    if rodzaj == "put":
        odp = await _put(klient, "x", "nie-ma-takiego")
    elif rodzaj == "post":
        odp = await _post(klient, "nie-ma-takiego", "nowy")
    else:
        odp = await _delete(klient, "x", "nie-ma-takiego")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []


@pytest.mark.parametrize("rodzaj", TRASY_ZAPISU)
async def test_parametr_projektu_trasy_zapisu_jest_wymagany_i_ma_limit_36_znakow(swiat, rodzaj):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    cialo = {"scope": "project", "frontmatter": FM_X, "brief": "B\n"}

    if rodzaj == "put":
        bez = await klient.put("/api/skills/x", json=cialo)
        za_dlugi = await _put(klient, "x", "p" * 37)
        graniczny = await _put(klient, "x", "p" * 36)
    elif rodzaj == "post":
        bez = await klient.post("/api/skills", json={**cialo, "id": "x"})
        za_dlugi = await _post(klient, "p" * 37, "x")
        graniczny = await _post(klient, "p" * 36, "x")
    else:
        bez = await klient.delete("/api/skills/x", params={"scope": "project"})
        za_dlugi = await _delete(klient, "x", "p" * 37)
        graniczny = await _delete(klient, "x", "p" * 36)

    assert (bez.status_code, za_dlugi.status_code) == (422, 422)
    assert graniczny.status_code == 404   # walidacja przeszla; takiego projektu nie ma
    assert swiat.runner.wywolania == []


# =================================================================================
# 7. Rownolegle dodanie tego samego id (PostgreSQL)
# =================================================================================

class RunnerPamieciowy:
    """Podstawka runnera z plikami w pamieci. Pierwsze zapytanie jakiegokolwiek typu zglasza sie
    i stoi, az test je zwolni; potem pliki zachowuja sie jak na dysku (zapis tworzy, odczyt brakujacego
    to `nie_istnieje`)."""

    def __init__(self) -> None:
        self.pliki: dict[str, str] = {}
        self.zapisy = 0
        self.wywolania = 0
        self.pierwsze_wywolanie = asyncio.Event()
        self.kolejne_wywolanie = asyncio.Event()
        self.zwolnij = asyncio.Event()

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania += 1
        # odczyt widzi stan z CHWILI WYWOLANIA (przed wstrzymaniem): bez blokady oba rownolegle
        # zadania zobacza "nie istnieje", bo drugie czyta, zanim pierwsze zapisze
        istnieje_przy_wywolaniu = dane.get("sciezka") in self.pliki
        if self.wywolania == 1:
            self.pierwsze_wywolanie.set()
            await asyncio.wait_for(self.zwolnij.wait(), LIMIT_S)
        else:
            self.kolejne_wywolanie.set()
        if typ == "katalog_definicji":
            return {"ok": True, "wynik": {"projekty": []}}
        sciezka = dane["sciezka"]
        if typ == "read_file":
            if not istnieje_przy_wywolaniu:
                return {"ok": False, "blad": "sciezka nie istnieje", "kod": "nie_istnieje"}
            tresc = self.pliki[sciezka]
            return {"ok": True, "wynik": {"sciezka": sciezka, "tresc": tresc, "rozmiar": len(tresc)}}
        if typ == "write_file":
            self.zapisy += 1
            self.pliki[sciezka] = dane["tresc"]
            return {"ok": True, "wynik": {"sciezka": sciezka, "rozmiar": len(dane["tresc"])}}
        return {"ok": False, "blad": "nieznana operacja", "kod": "nieznana_operacja"}


@pytest.mark.baza
@tylko_postgres
async def test_dwa_rownolegle_dodania_tego_samego_id_daja_jedno_201_i_jedno_409_a_plik_ma_tresc_pierwszego(
    maker_pg, monkeypatch,
):
    monkeypatch.setattr(db_session, "SessionLocal", maker_pg)
    runner_atrapa = RunnerPamieciowy()
    monkeypatch.setattr(bus, "zapytaj", runner_atrapa)
    wlasciciel, tenant = await zaloz_wlasciciela(maker_pg, "pg-skill@example.com")
    runner = await zaloz_runnera(maker_pg, tenant, "laptop-pg", "token-pg")
    async with maker_pg() as s:
        projekt = Project(tenant_id=tenant, runner_id=runner, path="/pg/projekt", name="app")
        s.add(projekt)
        await s.commit()
    bus.mark_runner_connected(runner)
    klient_a = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
    klient_b = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
    zadania = []
    try:
        await zaloguj_klienta(klient_a, maker_pg, wlasciciel)
        await zaloguj_klienta(klient_b, maker_pg, wlasciciel)
        zadania.append(asyncio.create_task(
            _post(klient_a, projekt.id, "wspolny", brief="Brief A.\n")))
        await asyncio.wait_for(runner_atrapa.pierwsze_wywolanie.wait(), LIMIT_S)
        zadania.append(asyncio.create_task(
            _post(klient_b, projekt.id, "wspolny", brief="Brief B.\n")))
        # drugie dodanie albo dochodzi do runnera (brak blokady), albo staje na blokadzie wiersza
        koniec = asyncio.get_running_loop().time() + LIMIT_S
        while (not runner_atrapa.kolejne_wywolanie.is_set()
               and not await _czeka_na_blokade_wiersza(maker_pg)):
            assert asyncio.get_running_loop().time() < koniec, "drugie dodanie ani nie dziala, ani nie czeka"
            await asyncio.sleep(0.05)
        runner_atrapa.zwolnij.set()
        odpowiedzi = await asyncio.wait_for(asyncio.gather(*zadania), LIMIT_S)
    finally:
        runner_atrapa.zwolnij.set()
        for zadanie in zadania:
            zadanie.cancel()
        await asyncio.gather(*zadania, return_exceptions=True)
        await klient_a.aclose()
        await klient_b.aclose()
        while runner in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner)
        getattr(bus, "_rozlaczone", {}).pop(runner, None)

    assert sorted(o.status_code for o in odpowiedzi) == [201, 409], [o.text for o in odpowiedzi]
    assert runner_atrapa.zapisy == 1
    (tresc,) = runner_atrapa.pliki.values()   # plik u runnera, nie tylko kody odpowiedzi
    zwyciezca = "Brief A.\n" if odpowiedzi[0].status_code == 201 else "Brief B.\n"
    assert tresc.endswith("---\n" + zwyciezca)


# =================================================================================
# 8. Poprawka 1: odswiezenie katalogu po zapisie, zakres w odpowiedzi, blokady, surogaty
# =================================================================================

KOMUNIKAT_BLAD_DYSKU = "blad dysku przy operacji na pliku"
BLAD_DYSKU_RUNNERA = {"ok": False, "blad": KOMUNIKAT_BLAD_DYSKU, "kod": "blad_dysku"}
KOMUNIKAT_NIEUDANEGO_ODSWIEZENIA = "katalog projektu nie odswiezony po zmianie skilla"
KATALOG_Z_PUSTYM_PROJEKTEM = {"projekty": [{"projekt": "/p", "skille": [], "workflow": []}]}


class _KatalogRunnera:
    """Podstawka `zapytaj_runnera_online`: zapamietuje, kogo kokpit pytal o katalog."""

    def __init__(self) -> None:
        self.pytania: list[tuple] = []

    async def __call__(self, s, tenant_id, typ, dane, polaczone, wyslij, runner_id=None):
        self.pytania.append((tenant_id, typ, runner_id))
        return "runner", KATALOG_Z_PUSTYM_PROJEKTEM


async def _odswiez_bezposrednio(swiat, monkeypatch, tenant_id, runner_id) -> _KatalogRunnera:
    runner = _KatalogRunnera()
    monkeypatch.setattr(zapytania_runnera, "zapytaj_runnera_online", runner)
    async with swiat.maker() as s:
        await skille_api._odswiez_katalog_projektu(
            s, tenant_id, SimpleNamespace(runner_id=runner_id), frozenset(), None)
    return runner


async def test_zapis_skilla_przy_nieudanym_odswiezeniu_katalogu_zostaje_na_dysku_a_przyczyna_trafia_do_logu(
    swiat, caplog,
):
    _skill(swiat.korzen, PODKATALOG, "x", name="Przed")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await _put(klient, "x", swiat.projekt_a,
                         frontmatter="id: x\nname: Po\nrole: author\n")

    assert odp.status_code == 200, odp.text
    assert b"name: Po" in _bajty_pliku(_plik(swiat.korzen, "x"))
    wpisy = [r for r in caplog.records if r.name == "cockpit.services.skille_api"]
    assert len(wpisy) == 1
    assert wpisy[0].levelno == logging.WARNING
    assert wpisy[0].getMessage() == KOMUNIKAT_NIEUDANEGO_ODSWIEZENIA
    assert wpisy[0].exc_info is not None
    assert wpisy[0].exc_info[0] is StanNiemozliwy
    assert _wpisy(await _lista(klient, swiat.projekt_a))["x"]["name"] == "Przed"   # cache czeka


@pytest.mark.parametrize(("scope", "zakres"), [("project", "lokalny"), ("global", "globalny")])
async def test_nowy_skill_przy_nieudanym_odswiezeniu_katalogu_ma_zakres_zapisu_a_nie_blad(
    swiat, scope, zakres,
):
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    odp = await _post(klient, swiat.projekt_a, "nowy", scope=scope)

    assert odp.status_code == 201, odp.text
    assert odp.json()["zakres"] == zakres
    assert _plik(swiat.korzen if scope == "project" else swiat.dom, "nowy").is_file()


async def test_usuniecie_skilla_przy_nieudanym_odswiezeniu_katalogu_to_204_a_plik_znika(
    swiat, caplog,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await _delete(klient, "x", swiat.projekt_a)

    assert odp.status_code == 204
    assert not _plik(swiat.korzen, "x").exists()
    assert [r.getMessage() for r in caplog.records if r.name == "cockpit.services.skille_api"] == [
        KOMUNIKAT_NIEUDANEGO_ODSWIEZENIA]


async def test_odpowiedz_na_zapis_lokalnego_skilla_przykrywajacego_globalny_ma_zakres_nadpisany(swiat):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    _skill(swiat.korzen, PODKATALOG, "inny")
    _skill(swiat.dom, PODKATALOG, "obcy")
    klient = await _klient_z_katalogiem(swiat)

    nadpisany = await _put(klient, "x", swiat.projekt_a, scope="project",
                           frontmatter="id: x\nname: Nowy z repo\nrole: author\n")
    lokalny = await _put(klient, "inny", swiat.projekt_a, scope="project",
                         frontmatter="id: inny\nname: Nowy inny\nrole: author\n")

    assert (nadpisany.status_code, nadpisany.json()["zakres"]) == (200, "nadpisany")
    assert (lokalny.status_code, lokalny.json()["zakres"]) == (200, "lokalny")


async def test_odpowiedz_na_dodanie_lokalnego_skilla_o_id_globalnego_ma_zakres_nadpisany(swiat):
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    _skill(swiat.dom, PODKATALOG, "obcy")
    _skill(swiat.korzen, PODKATALOG, "inny")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "x", scope="project",
                      frontmatter="id: x\nname: Z repo\nrole: author\n")

    assert odp.status_code == 201, odp.text
    assert odp.json()["zakres"] == "nadpisany"


async def test_zapisany_globalny_skill_przykryty_kopia_z_repo_jest_w_katalogu_nadpisany_tak_jak_w_liscie_i_get(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, scope="global",
                     frontmatter="id: x\nname: Nowy z domu\nrole: author\n")

    assert odp.status_code == 200, odp.text
    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()
    assert wpis["zakres"] == szczegoly["zakres"] == odp.json()["zakres"] == "nadpisany"


async def test_odswiezenie_po_zapisie_pyta_runnera_projektu_a_nie_pierwszego_runnera_online(swiat):
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)
    # pierwszy online to runner o mniejszym id; zapis idzie przez projekt tego drugiego
    if swiat.runner_a < swiat.runner_a2:
        drugi_runner, jego_projekt = swiat.runner_a2, swiat.projekt_a2
    else:
        drugi_runner, jego_projekt = swiat.runner_a, swiat.projekt_a
    swiat.runner.wywolania.clear()

    odp = await _post(klient, jego_projekt, "nowy")

    assert odp.status_code == 201, odp.text
    assert [w[0] for w in _dane_zapisu(swiat.runner, "katalog_definicji")] == [drugi_runner]
    async with swiat.maker() as s:
        wiersze = (await s.execute(
            select(Definicja).where(Definicja.klucz == "nowy"))).scalars().all()
    assert [w.runner_id for w in wiersze] == [drugi_runner]


async def test_odswiezenie_katalogu_projektu_pyta_wylacznie_runnera_tego_projektu(swiat, monkeypatch):
    runner = await _odswiez_bezposrednio(swiat, monkeypatch, swiat.tenant_a, swiat.runner_a2)

    assert runner.pytania == [(swiat.tenant_a, "katalog_definicji", swiat.runner_a2)]


async def test_odswiezenie_katalogu_projektu_kasuje_tylko_wiersze_jego_runnera_w_jego_zespole(
    swiat, monkeypatch,
):
    projekt = str(swiat.korzen)
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, projekt, "wlasny")
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_a, projekt, "cudzy-zespol")
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a2, projekt, "inny-runner")

    await _odswiez_bezposrednio(swiat, monkeypatch, swiat.tenant_a, swiat.runner_a)

    async with swiat.maker() as s:
        klucze = sorted((await s.execute(select(Definicja.klucz))).scalars().all())
    assert klucze == ["cudzy-zespol", "inny-runner"]


async def test_odswiezenie_katalogu_runnera_ktorego_juz_nie_ma_to_stan_niemozliwy_z_opisem(
    swiat, monkeypatch,
):
    with pytest.raises(StanNiemozliwy) as wyjatek:
        await _odswiez_bezposrednio(swiat, monkeypatch, swiat.tenant_a, "runner-ktorego-nie-ma")

    assert str(wyjatek.value) == "runner zniknal w trakcie odswiezania katalogu"


async def test_odswiezenie_katalogu_runnera_innego_zespolu_to_stan_niemozliwy_a_jego_wiersze_zostaja(
    swiat, monkeypatch,
):
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_b, str(swiat.korzen), "zostaje")

    with pytest.raises(StanNiemozliwy) as wyjatek:
        await _odswiez_bezposrednio(swiat, monkeypatch, swiat.tenant_a, swiat.runner_b)

    assert str(wyjatek.value) == "runner zniknal w trakcie odswiezania katalogu"
    async with swiat.maker() as s:
        assert (await s.execute(select(Definicja.klucz))).scalars().all() == ["zostaje"]


async def test_odswiezenie_katalogu_jedynego_runnera_zespolu_konczy_sie_bez_bledu(swiat, monkeypatch):
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_b, str(swiat.korzen_b), "stary")

    runner = await _odswiez_bezposrednio(swiat, monkeypatch, swiat.tenant_b, swiat.runner_b)

    assert runner.pytania == [(swiat.tenant_b, "katalog_definicji", swiat.runner_b)]
    async with swiat.maker() as s:
        assert (await s.execute(select(Definicja.klucz))).scalars().all() == []   # wymieniony


@pytest.fixture
def strefa_lokalna_plus_piec_godzin():
    stara = os.environ.get("TZ")
    os.environ["TZ"] = "UTC-5"   # POSIX: lokalny czas = UTC + 5h
    time.tzset()
    yield
    if stara is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = stara
    time.tzset()


async def test_znacznik_odswiezenia_po_zapisie_jest_w_utc_niezaleznie_od_strefy_serwera(
    swiat, strefa_lokalna_plus_piec_godzin,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    await _put(klient, "x", swiat.projekt_a)

    znacznik = datetime.fromisoformat((await _lista(klient, swiat.projekt_a)).json()["odswiezono_at"])
    roznica = abs((datetime.now(timezone.utc) - znacznik).total_seconds())
    assert roznica < 120, znacznik


class _SesjaZeSzpiegiem(AsyncSession):
    """Prawdziwa sesja; zapamietuje kazde zapytanie przekazane do `execute`."""

    zapytania: list = []

    async def execute(self, statement, *args, **kwargs):
        self.zapytania.append(statement)
        return await super().execute(statement, *args, **kwargs)


@pytest.mark.parametrize("rodzaj", ["put", "delete"])
async def test_zapis_i_usuniecie_skilla_blokuja_wiersz_projektu_i_wiersz_runnera_w_postgresql(
    swiat, monkeypatch, rodzaj,
):
    # SQLite ignoruje FOR UPDATE, wiec sprawdzamy zapytania skompilowane dla PostgreSQL
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    _SesjaZeSzpiegiem.zapytania = []
    szpieg = async_sessionmaker(
        swiat.maker.kw["bind"], class_=_SesjaZeSzpiegiem, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", szpieg)

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code in (200, 204), odp.text
    sql = [str(z.compile(dialect=postgresql.dialect())) for z in _SesjaZeSzpiegiem.zapytania]
    projekt = [q for q in sql if q.rstrip().endswith("FOR NO KEY UPDATE")]
    assert len(projekt) == 1, sql
    assert "FROM project" in projekt[0]
    assert "project.tenant_id =" in projekt[0].split("WHERE", 1)[1]
    runner = [q for q in sql if q.rstrip().endswith("FOR UPDATE")]
    assert len(runner) == 1, sql
    assert "FROM runner" in runner[0]
    warunki = runner[0].split("WHERE", 1)[1]
    assert "runner.id =" in warunki
    assert "runner.tenant_id =" in warunki


class _SesjaZLicznikiemZatwierdzen(AsyncSession):
    """Prawdziwa sesja; zapamietuje kazde zatwierdzenie transakcji."""

    zatwierdzenia = 0

    async def commit(self):
        type(self).zatwierdzenia += 1
        await super().commit()


async def test_zapis_skilla_konczy_transakcje_tak_samo_gdy_katalog_nie_dal_sie_odswiezyc(
    swiat, monkeypatch,
):
    # blokada wiersza projektu nie moze wisiec po zapisie, wiec nieudane odswiezenie tez zatwierdza
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    monkeypatch.setattr(db_session, "SessionLocal", async_sessionmaker(
        swiat.maker.kw["bind"], class_=_SesjaZLicznikiemZatwierdzen, expire_on_commit=False))
    _SesjaZLicznikiemZatwierdzen.zatwierdzenia = 0
    udane = await _put(klient, "x", swiat.projekt_a)
    po_udanym = _SesjaZLicznikiemZatwierdzen.zatwierdzenia
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA
    _SesjaZLicznikiemZatwierdzen.zatwierdzenia = 0

    nieudane = await _put(klient, "x", swiat.projekt_a)

    assert (udane.status_code, nieudane.status_code) == (200, 200)
    assert po_udanym >= 1   # odswiezenie katalogu zatwierdza wymiane wierszy
    assert _SesjaZLicznikiemZatwierdzen.zatwierdzenia == po_udanym


@pytest.mark.parametrize("rodzaj", ["put", "post"])
async def test_nieudany_odczyt_pliku_skilla_przed_zapisem_to_409_z_komunikatem_runnera_bez_zapisu(
    swiat, rodzaj,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["read_file"] = BLAD_DYSKU_RUNNERA
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))

    odp = await _put(klient, "x", swiat.projekt_a) if rodzaj == "put" else await _post(
        klient, swiat.projekt_a, "nowy")

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_BLAD_DYSKU}
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal
    assert not _plik(swiat.korzen, "nowy").exists()


# ---- surogaty: tekst, ktorego nie da sie zapisac jako UTF-8 -----------------------------------

TEKSTY_Z_SUROGATEM = {
    "w_frontmatterze": ("id: x\nname: \ud800\nrole: author\n", "Brief.\n"),
    "w_briefie": (FM_X, "Brief \udfff.\n"),
}


@pytest.mark.parametrize(("frontmatter", "brief"), TEKSTY_Z_SUROGATEM.values(),
                         ids=TEKSTY_Z_SUROGATEM.keys())
@pytest.mark.parametrize("operacja", ["zapisz_skill", "dodaj_skill"])
async def test_skill_z_samotnym_surogatem_to_blad_skilla_zanim_serwis_dotknie_bazy_albo_runnera(
    operacja, frontmatter, brief,
):
    # s=None i wyslij=None: kazde dotkniecie bazy albo runnera byloby innym wyjatkiem
    with pytest.raises(BlednySkill) as wyjatek:
        await getattr(skille_api, operacja)(
            None, "zespol", "projekt", "x", "project", frontmatter, brief, frozenset(), None)

    assert str(wyjatek.value) == "tresc skilla nie jest poprawnym tekstem UTF-8"


# =================================================================================
# 9. Runda 4: zle tagi YAML, blokada runnera przy zapisie globalnym, pelne komunikaty
# =================================================================================

KOMUNIKAT_NIEPOPRAWNY_FRONTMATTER = "niepoprawny frontmatter: nieprawidlowa wartosc"
FRONTMATTERY_ZE_ZLYM_TAGIEM = {
    "tag_bool_z_napisem": "id: x\nname: !!bool x\nrole: author\n",        # KeyError
    "tag_zbioru_z_napisem": "id: x\nname: !!set x\nrole: author\n",       # AttributeError
    "liczba_bez_cyfr": "id: x\nname: !!int 0b\nrole: author\n",           # IndexError
}


@pytest.mark.parametrize("frontmatter", FRONTMATTERY_ZE_ZLYM_TAGIEM.values(),
                         ids=FRONTMATTERY_ZE_ZLYM_TAGIEM.keys())
async def test_dodanie_skilla_ze_zlym_tagiem_we_frontmatterze_to_422_a_plik_nie_powstaje(
    swiat, frontmatter,
):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    odp = await _post(klient, swiat.projekt_a, "x", frontmatter=frontmatter)

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_NIEPOPRAWNY_FRONTMATTER}
    assert "Traceback" not in odp.text
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert not _plik(swiat.korzen, "x").exists()
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("frontmatter", FRONTMATTERY_ZE_ZLYM_TAGIEM.values(),
                         ids=FRONTMATTERY_ZE_ZLYM_TAGIEM.keys())
async def test_zapis_skilla_ze_zlym_tagiem_we_frontmatterze_to_422_a_stary_plik_zostaje(
    swiat, frontmatter,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=frontmatter)

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_NIEPOPRAWNY_FRONTMATTER}
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize("zly_frontmatter", FRONTMATTERY_ZE_ZLYM_TAGIEM.values(),
                         ids=FRONTMATTERY_ZE_ZLYM_TAGIEM.keys())
async def test_udany_zapis_skilla_przy_innym_skillu_ze_zlym_tagiem_w_katalogu_runnera_zostaje_na_dysku_a_cache_czeka(
    swiat, caplog, rodzaj, zly_frontmatter,
):
    _skill(swiat.korzen, PODKATALOG, "x", name="Przed")
    klient = await _klient_z_katalogiem(swiat)
    _bajty(swiat.korzen, PODKATALOG, "zly", _tekst_pliku(zly_frontmatter.replace("id: x", "id: zly"), "B\n"))
    przed = (await _lista(klient, swiat.projekt_a)).json()

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        if rodzaj == "put":
            odp = await _put(klient, "x", swiat.projekt_a,
                             frontmatter="id: x\nname: Po\nrole: author\n")
        else:
            odp = await _post(klient, swiat.projekt_a, "nowy")

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    zapisany = "x" if rodzaj == "put" else "nowy"
    assert _plik(swiat.korzen, zapisany).is_file()
    if rodzaj == "put":
        assert b"name: Po" in _bajty_pliku(_plik(swiat.korzen, "x"))
    assert (await _lista(klient, swiat.projekt_a)).json() == przed   # cache nieodswiezony
    wpisy = [r for r in caplog.records if r.name == "cockpit.services.skille_api"]
    assert [r.getMessage() for r in wpisy] == [KOMUNIKAT_NIEUDANEGO_ODSWIEZENIA]
    # przyczyna w logu: katalog runnera ma plik z zlym tagiem, a nie np. brak runnera
    assert str(wpisy[0].exc_info[1]) == "katalog runnera zawiera plik z niepoprawnym YAML"


@pytest.mark.parametrize("rodzaj", ["put", "post"])
async def test_frontmatter_z_linia_zamykajaca_ze_spacja_jest_w_odpowiedzi_podzielony_tak_jak_w_pliku(
    swiat, rodzaj,
):
    # linia `--- ` zamyka frontmatter, wiec reszta idzie do briefu: odpowiedz mowi, co jest w pliku
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    wyslany = FM_X + "opis: a\n--- \n"

    odp = await (_put(klient, "x", swiat.projekt_a, frontmatter=wyslany, brief="B\n")
                 if rodzaj == "put" else _post(klient, swiat.projekt_a, "nowy",
                                               frontmatter=wyslany.replace("id: x", "id: nowy"),
                                               brief="B\n"))

    assert odp.status_code in (200, 201), odp.text
    assert odp.json()["frontmatter"] == wyslany.split("--- ")[0].replace(
        "id: x", "id: x" if rodzaj == "put" else "id: nowy")
    assert odp.json()["brief"] == "---\nB\n"
    plik = _plik(swiat.korzen, "x" if rodzaj == "put" else "nowy")
    assert plik.read_bytes().decode().endswith("--- \n---\nB\n")


async def test_zle_id_skilla_ma_pelny_komunikat_o_wzorcu_nazwy(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "a b", frontmatter="id: a b\nname: X\nrole: author\n")

    assert odp.status_code == 422
    assert odp.json() == {"detail": (
        "id skilla musi byc nazwa katalogu: litery, cyfry, _ - . (do 64 znakow, "
        "pierwszy znak litera lub cyfra)")}


async def test_dodanie_skilla_o_zajetym_id_ma_pelny_komunikat_z_id_i_zakresem(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _post(klient, swiat.projekt_a, "x")

    assert odp.status_code == 409
    assert odp.json() == {"detail": "skill 'x' juz istnieje w tym zakresie"}


async def test_zapis_nieistniejacego_skilla_ma_komunikat_skill_nieznany(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "nie-ma", swiat.projekt_a,
                     frontmatter="id: nie-ma\nname: X\nrole: author\n")

    assert odp.status_code == 404
    assert odp.json() == {"detail": "skill nieznany"}


# ---- zapis do katalogu domowego runnera szereguje wiersz Runner -------------------------------

def _kolejnosc_runnerow(swiat):
    """(runner, jego projekt) o WIEKSZYM id, czyli taki, ktory NIE jest pierwszy w `ORDER BY id`."""
    if swiat.runner_a < swiat.runner_a2:
        return swiat.runner_a2, swiat.projekt_a2
    return swiat.runner_a, swiat.projekt_a


async def test_zapis_pliku_skilla_idzie_do_runnera_projektu_ktory_nie_jest_pierwszy_w_kolejnosci_id(swiat):
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)
    drugi_runner, jego_projekt = _kolejnosc_runnerow(swiat)
    swiat.runner.wywolania.clear()

    odp = await _post(klient, jego_projekt, "nowy")

    assert odp.status_code == 201, odp.text
    assert [w[0] for w in _dane_zapisu(swiat.runner, "write_file")] == [drugi_runner]


async def test_usuniecie_pliku_skilla_idzie_do_runnera_projektu_ktory_nie_jest_pierwszy_w_kolejnosci_id(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)
    drugi_runner, jego_projekt = _kolejnosc_runnerow(swiat)
    swiat.runner.wywolania.clear()

    odp = await _delete(klient, "x", jego_projekt)

    assert odp.status_code == 204
    assert [w[0] for w in _dane_zapisu(swiat.runner, "delete_file")] == [drugi_runner]


async def test_odczyt_pliku_przed_zapisem_skilla_idzie_do_runnera_projektu_a_nie_pierwszego_online(swiat):
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)
    drugi_runner, jego_projekt = _kolejnosc_runnerow(swiat)
    swiat.runner.wywolania.clear()

    await _post(klient, jego_projekt, "nowy")

    assert {w[0] for w in _dane_zapisu(swiat.runner, "read_file")} == {drugi_runner}


@pytest.mark.parametrize(("rodzaj", "zakres", "blokad_runnera"), [
    ("post", "project", 1), ("post", "global", 2),
    ("delete", "project", 1), ("delete", "global", 2),
], ids=["post_project", "post_global", "delete_project", "delete_global"])
async def test_zapis_globalny_blokuje_wiersz_runnera_dodatkowo_przed_zapisem_a_lokalny_tylko_przy_odswiezeniu(
    swiat, monkeypatch, rodzaj, zakres, blokad_runnera,
):
    _skill(swiat.dom if zakres == "global" else swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    _SesjaZeSzpiegiem.zapytania = []
    szpieg = async_sessionmaker(
        swiat.maker.kw["bind"], class_=_SesjaZeSzpiegiem, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", szpieg)

    if rodzaj == "post":
        odp = await _post(klient, swiat.projekt_a, "nowy", scope=zakres)
    else:
        odp = await _delete(klient, "x", swiat.projekt_a, scope=zakres)

    assert odp.status_code in (201, 204), odp.text
    sql = [str(z.compile(dialect=postgresql.dialect())) for z in _SesjaZeSzpiegiem.zapytania]
    runner = [q for q in sql if q.rstrip().endswith("FOR UPDATE") and "FROM runner" in q]
    assert len(runner) == blokad_runnera, sql
    for q in runner:
        assert "runner.id =" in q and "runner.tenant_id =" in q
    projekt = [q for q in sql if q.rstrip().endswith("FOR NO KEY UPDATE")]
    assert len(projekt) == 1, sql


@pytest.mark.parametrize("rodzaj", ["post", "delete"])
async def test_zapis_globalny_dla_projektu_bez_wiersza_runnera_to_409_z_opisem_zanim_runner_dostanie_zapytanie(
    swiat, rodzaj,
):
    async with swiat.maker() as s:
        projekt = Project(tenant_id=swiat.tenant_a, runner_id="runner-ktorego-nie-ma",
                          path=str(swiat.korzen), name="sierota")
        s.add(projekt)
        await s.commit()
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wywolania.clear()

    odp = await (_post(klient, projekt.id, "nowy", scope="global") if rodzaj == "post"
                 else _delete(klient, "x", projekt.id, scope="global"))

    assert odp.status_code == 409
    assert odp.json() == {"detail": "runner zniknal w trakcie odswiezania katalogu"}
    assert swiat.runner.wywolania == []
    assert not _plik(swiat.dom, "nowy").exists()


@pytest.mark.baza
@tylko_postgres
async def test_dwa_rownolegle_dodania_globalnego_id_z_dwoch_projektow_jednego_runnera_daja_jedno_201_i_jedno_409(
    maker_pg, monkeypatch,
):
    monkeypatch.setattr(db_session, "SessionLocal", maker_pg)
    runner_atrapa = RunnerPamieciowy()
    monkeypatch.setattr(bus, "zapytaj", runner_atrapa)
    wlasciciel, tenant = await zaloz_wlasciciela(maker_pg, "pg-global@example.com")
    runner = await zaloz_runnera(maker_pg, tenant, "laptop-pg-g", "token-pg-g")
    async with maker_pg() as s:
        projekt_a = Project(tenant_id=tenant, runner_id=runner, path="/pg/a", name="a")
        projekt_b = Project(tenant_id=tenant, runner_id=runner, path="/pg/b", name="b")
        s.add_all([projekt_a, projekt_b])
        await s.commit()
    bus.mark_runner_connected(runner)
    klient_a = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
    klient_b = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
    zadania = []
    try:
        await zaloguj_klienta(klient_a, maker_pg, wlasciciel)
        await zaloguj_klienta(klient_b, maker_pg, wlasciciel)
        zadania.append(asyncio.create_task(
            _post(klient_a, projekt_a.id, "wspolny", scope="global", brief="Brief A.\n")))
        await asyncio.wait_for(runner_atrapa.pierwsze_wywolanie.wait(), LIMIT_S)
        zadania.append(asyncio.create_task(
            _post(klient_b, projekt_b.id, "wspolny", scope="global", brief="Brief B.\n")))
        koniec = asyncio.get_running_loop().time() + LIMIT_S
        while (not runner_atrapa.kolejne_wywolanie.is_set()
               and not await _czeka_na_blokade_wiersza(maker_pg)):
            assert asyncio.get_running_loop().time() < koniec, "drugie dodanie ani nie dziala, ani nie czeka"
            await asyncio.sleep(0.05)
        runner_atrapa.zwolnij.set()
        odpowiedzi = await asyncio.wait_for(asyncio.gather(*zadania), LIMIT_S)
    finally:
        runner_atrapa.zwolnij.set()
        for zadanie in zadania:
            zadanie.cancel()
        await asyncio.gather(*zadania, return_exceptions=True)
        await klient_a.aclose()
        await klient_b.aclose()
        while runner in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner)
        getattr(bus, "_rozlaczone", {}).pop(runner, None)

    assert sorted(o.status_code for o in odpowiedzi) == [201, 409], [o.text for o in odpowiedzi]
    assert runner_atrapa.zapisy == 1
    assert list(runner_atrapa.pliki) == ["~/.cockpit/skills/wspolny/SKILL.md"]
    (tresc,) = runner_atrapa.pliki.values()
    zwyciezca = "Brief A.\n" if odpowiedzi[0].status_code == 201 else "Brief B.\n"
    assert tresc.endswith("---\n" + zwyciezca)


# =================================================================================
# 10. Runda domykajaca: kolejnosc blokady, straznik ksztaltu, zakres z dysku, limity pol
# =================================================================================

def _indeks(sql: list[str], warunek) -> int:
    return next(i for i, q in enumerate(sql) if warunek(q))


@pytest.mark.parametrize(("zakres", "lock_przed_pytaniem"), [("global", True), ("project", False)])
async def test_zapis_globalny_blokuje_wiersz_runnera_przed_pytaniem_runnera_o_istnienie_id(
    swiat, monkeypatch, zakres, lock_przed_pytaniem,
):
    klient = await _klient_z_katalogiem(swiat)
    _SesjaZeSzpiegiem.zapytania = []
    szpieg = async_sessionmaker(
        swiat.maker.kw["bind"], class_=_SesjaZeSzpiegiem, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", szpieg)

    odp = await _post(klient, swiat.projekt_a, "nowy", scope=zakres)

    assert odp.status_code == 201, odp.text
    sql = [str(z.compile(dialect=postgresql.dialect())) for z in _SesjaZeSzpiegiem.zapytania]
    blokada = _indeks(sql, lambda q: "FROM runner" in q and q.rstrip().endswith("FOR UPDATE"))
    pytanie = _indeks(sql, lambda q: "FROM runner" in q and "ORDER BY runner.id" in q)
    assert (blokada < pytanie) is lock_przed_pytaniem, sql


ZLE_KSZTALTY_KATALOGU = {
    "pusta_lista_projektow": ({"projekty": []}, "lista projektow jest pusta"),
    "wynik_jest_lista": ([], "wynik nie jest slownikiem"),
}


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize(("wynik", "powod"), ZLE_KSZTALTY_KATALOGU.values(),
                         ids=ZLE_KSZTALTY_KATALOGU.keys())
async def test_zly_ksztalt_katalogu_po_zapisie_skilla_zostawia_cache_innych_projektow_i_zapis_zostaje(
    swiat, caplog, rodzaj, wynik, powod,
):
    _skill(swiat.korzen, PODKATALOG, "x", name="Przed")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, "/inny/projekt", "cudzy-skill")
    async with swiat.maker() as s:
        przed = sorted((await s.execute(select(Definicja.klucz))).scalars().all())
    swiat.runner.wymuszone["katalog_definicji"] = {"ok": True, "wynik": wynik}

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                     if rodzaj == "put" else _post(klient, swiat.projekt_a, "nowy"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert "Traceback" not in odp.text
    assert _plik(swiat.korzen, "x" if rodzaj == "put" else "nowy").is_file()
    async with swiat.maker() as s:
        po = sorted((await s.execute(select(Definicja.klucz))).scalars().all())
    assert "cudzy-skill" in po
    assert po == przed   # cache nietkniety: ani wymiany wierszy, ani skasowania
    wpisy = [r for r in caplog.records if r.name == "cockpit.services.skille_api"]
    assert [r.getMessage() for r in wpisy] == [KOMUNIKAT_NIEUDANEGO_ODSWIEZENIA]
    assert str(wpisy[0].exc_info[1]) == f"odpowiedz runnera ma zly ksztalt ({powod})"


@pytest.mark.parametrize("rodzaj", ["put", "post"])
async def test_zakres_nadpisany_wynika_z_dysku_runnera_takze_gdy_katalog_sie_nie_odswiezyl(swiat, rodzaj):
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    odp = await (_put(klient, "x", swiat.projekt_a) if rodzaj == "put"
                 else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert odp.json()["zakres"] == "nadpisany"


@pytest.mark.parametrize(("zakres", "odczytow"), [("project", 2), ("global", 1)])
async def test_zapis_czyta_plik_globalny_tylko_przy_zakresie_lokalnym_bo_globalny_juz_sprawdzono(
    swiat, zakres, odczytow,
):
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wywolania.clear()

    odp = await _post(klient, swiat.projekt_a, "nowy", scope=zakres)

    assert odp.status_code == 201, odp.text
    assert len(_dane_zapisu(swiat.runner, "read_file")) == odczytow


async def test_zakres_lokalny_gdy_na_dysku_runnera_nie_ma_globalnego_skilla_o_tym_id(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    odp = await _put(klient, "x", swiat.projekt_a)

    assert (odp.status_code, odp.json()["zakres"]) == (200, "lokalny")


USZKODZONE_DOMOWE_PLIKI = {
    "ponad_limit_odczytu": "ponad_limit_odczytu",
    "katalog_zamiast_pliku": "katalog_zamiast_pliku",
    "bajt_nul": "bajt_nul",
    "bajty_nie_utf8": "bajty_nie_utf8",
    "plik_nie_jest_zwyklym_plikiem": "plik_nie_jest_zwyklym_plikiem",
    "element_sciezki_nie_jest_katalogiem": "element_sciezki_nie_jest_katalogiem",
}
KATALOG_ODSWIEZONY_ALBO_NIE = pytest.mark.parametrize(
    "katalog_sie_odswiezy", [True, False], ids=["katalog_odswiezony", "katalog_nieodswiezony"])


def _uszkodz_domowy_skill(swiat, skill_id: str, uszkodzenie: str) -> None:
    if uszkodzenie == "ponad_limit_odczytu":
        _bajty(swiat.dom, PODKATALOG, skill_id, "a" * 600_000)
    elif uszkodzenie == "katalog_zamiast_pliku":
        _plik(swiat.dom, skill_id).mkdir(parents=True)
    elif uszkodzenie == "bajty_nie_utf8":
        plik = _plik(swiat.dom, skill_id)
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_bytes(b"---\nid: x\nname: \xff\xfe\nrole: author\n---\n")
    elif uszkodzenie == "plik_nie_jest_zwyklym_plikiem":
        plik = _plik(swiat.dom, skill_id)
        plik.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(plik)   # runner nie otwiera potoku (open moglby zawisnac), tylko odmawia
    elif uszkodzenie == "element_sciezki_nie_jest_katalogiem":
        (swiat.dom / PODKATALOG).mkdir(parents=True, exist_ok=True)
        (swiat.dom / PODKATALOG / skill_id).write_bytes(b"to jest plik, nie katalog skilla")
    else:
        _bajty(swiat.dom, PODKATALOG, skill_id, f"---\nid: {skill_id}\nname: X\nrole: author\n---\n\0")


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize("uszkodzenie", USZKODZONE_DOMOWE_PLIKI.values(),
                         ids=USZKODZONE_DOMOWE_PLIKI.keys())
@KATALOG_ODSWIEZONY_ALBO_NIE
async def test_nieczytelny_globalny_skill_o_tym_id_nie_blokuje_zapisu_lokalnego_ktory_jest_lokalny(
    swiat, caplog, rodzaj, uszkodzenie, katalog_sie_odswiezy,
):
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    _uszkodz_domowy_skill(swiat, "x", uszkodzenie)
    if not katalog_sie_odswiezy:   # zakres w odpowiedzi rozstrzyga wtedy odczyt pliku domowego
        swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                     if rodzaj == "put" else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert "Traceback" not in odp.text
    assert odp.json()["zakres"] == "lokalny"
    assert _plik(swiat.korzen, "x").is_file()
    if katalog_sie_odswiezy:
        wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
        assert wpis["zakres"] == "lokalny"
    if rodzaj == "put":
        assert b"name: Po" in _bajty_pliku(_plik(swiat.korzen, "x"))
    nieczytelne = [r.getMessage() for r in caplog.records
                   if r.name == "cockpit.services.skille_api" and "nieczytelny" in r.getMessage()]
    assert len(nieczytelne) == 1, nieczytelne   # powod odmowy odczytu trafia do logu
    przedrostek = "globalny skill 'x' nieczytelny, traktuje jak brak: "
    assert nieczytelne[0].startswith(przedrostek)
    powod = nieczytelne[0][len(przedrostek):]
    assert powod and powod != "None"


@KATALOG_ODSWIEZONY_ALBO_NIE
async def test_brak_globalnego_pliku_skilla_nie_zostawia_ostrzezenia_o_nieczytelnym_pliku(
    swiat, caplog, katalog_sie_odswiezy,
):
    klient = await _klient_z_katalogiem(swiat)
    if not katalog_sie_odswiezy:   # zakres w odpowiedzi rozstrzyga wtedy odczyt pliku domowego
        swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await _post(klient, swiat.projekt_a, "nowy")

    assert odp.status_code == 201, odp.text
    assert odp.json()["zakres"] == "lokalny"
    assert [r.getMessage() for r in caplog.records if r.name == "cockpit.services.skille_api"] == (
        [] if katalog_sie_odswiezy else [KOMUNIKAT_NIEUDANEGO_ODSWIEZENIA])


NIEPOPRAWNE_DOMOWE_SKILLE = {
    "brak_frontmattera": ("Sam brief bez frontmattera\n", True),
    "brak_pola_role": ("---\nid: x\nname: X\n---\nB\n", True),
    "zly_tag_yaml": ("---\nid: x\nname: !!bool x\nrole: author\n---\nB\n", False),
}


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize(("tresc", "katalog_sie_odswiezy"), NIEPOPRAWNE_DOMOWE_SKILLE.values(),
                         ids=NIEPOPRAWNE_DOMOWE_SKILLE.keys())
@pytest.mark.parametrize("wymuszony_blad_odswiezenia", [False, True],
                         ids=["katalog_wg_pliku", "katalog_nieodswiezony"])
async def test_niepoprawny_plik_domowy_o_tym_id_nie_czyni_zapisu_lokalnego_nadpisanym_zgodnie_z_lista_i_get(
    swiat, caplog, rodzaj, tresc, katalog_sie_odswiezy, wymuszony_blad_odswiezenia,
):
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    klient = await _klient_z_katalogiem(swiat)
    _bajty(swiat.dom, PODKATALOG, "x", tresc)
    if wymuszony_blad_odswiezenia:   # zakres w odpowiedzi rozstrzyga wtedy odczyt pliku domowego
        swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA
        katalog_sie_odswiezy = False

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                     if rodzaj == "put" else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert odp.json()["zakres"] == "lokalny"
    niepoprawne = [r.getMessage() for r in caplog.records
                   if r.name == "cockpit.services.skille_api" and "niepoprawny," in r.getMessage()]
    assert len(niepoprawne) == 1, niepoprawne
    przedrostek = "globalny skill 'x' niepoprawny, traktuje jak brak: "
    assert niepoprawne[0].startswith(przedrostek)
    powod = niepoprawne[0][len(przedrostek):]
    assert powod and powod != "None"
    if katalog_sie_odswiezy or rodzaj == "put":
        wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
        szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()
        assert wpis["zakres"] == szczegoly["zakres"] == odp.json()["zakres"] == "lokalny"


@pytest.mark.parametrize("rodzaj", ["put", "post"])
async def test_poprawny_plik_domowy_o_tym_id_czyni_zapis_lokalny_nadpisanym_zgodnie_z_lista_i_get(
    swiat, rodzaj,
):
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x", name="Z repo")
    klient = await _klient_z_katalogiem(swiat)

    odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                 if rodzaj == "put" else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()
    assert wpis["zakres"] == szczegoly["zakres"] == odp.json()["zakres"] == "nadpisany"


@pytest.mark.parametrize("rodzaj", ["put", "post"])
async def test_bajt_nul_w_briefie_to_422_z_opisem_a_plik_na_dysku_sie_nie_zmienia(swiat, rodzaj):
    _skill(swiat.korzen, PODKATALOG, "x")
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wywolania.clear()

    odp = await (_put(klient, "x", swiat.projekt_a, brief="a\0b")
                 if rodzaj == "put" else _post(klient, swiat.projekt_a, "nowy", brief="a\0b"))

    assert odp.status_code == 422
    assert odp.json() == {"detail": "plik skilla zawiera bajt NUL; runner nie odczyta takiego pliku"}
    assert _dane_zapisu(swiat.runner, "write_file") == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal
    assert not _plik(swiat.korzen, "nowy").exists()


def test_plik_skilla_z_wielobajtowych_znakow_ponad_512_kb_w_bajtach_to_niespojne_dane_choc_znakow_jest_mniej():
    from cockpit.services.errors import NiespojneDane

    brief = "\U0001F600" * 131070   # znakow wielokrotnie mniej niz 524288, bajtow wiecej
    oczekiwane_bajty = len("---\n---\n") + len(FM_X) + 4 * 131070
    assert oczekiwane_bajty > 512 * 1024

    with pytest.raises(NiespojneDane) as wyjatek:
        skille_api._plik_skilla("x", FM_X, brief)

    assert str(wyjatek.value) == (
        f"plik skilla ma {oczekiwane_bajty} bajtow; limit odczytu runnera to 512 KB")


async def test_brief_ponad_limit_pola_to_422_a_plik_na_dysku_sie_nie_zmienia(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _put(klient, "x", swiat.projekt_a, brief="b" * 600_000)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal


LIMIT_POLA = 65535   # niezalezna od kodu kopia: (512 KB - 8 bajtow linii granicznych) / 8


@pytest.mark.parametrize("pole", ["frontmatter", "brief"])
@pytest.mark.parametrize(("dlugosc", "status"), [(LIMIT_POLA, 200), (LIMIT_POLA + 1, 422)],
                         ids=["rowno_limit", "limit_plus_1"])
async def test_pole_skilla_do_65535_znakow_jest_zapisane_a_dluzsze_to_422_bez_zapisu(
    swiat, pole, dlugosc, status,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)
    if pole == "brief":
        wartosc = "b" * dlugosc
        odp = await _put(klient, "x", swiat.projekt_a, brief=wartosc)
    else:
        wartosc = FM_X + "#" + "z" * (dlugosc - len(FM_X) - 2) + "\n"
        assert len(wartosc) == dlugosc
        odp = await _put(klient, "x", swiat.projekt_a, frontmatter=wartosc)

    assert odp.status_code == status, odp.text[:200]
    if status == 200:
        assert wartosc.encode() in _bajty_pliku(_plik(swiat.korzen, "x"))
    else:
        assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal
        assert _dane_zapisu(swiat.runner, "write_file") == []


async def test_pola_z_wielobajtowych_znakow_w_limicie_znakow_daja_plik_miesci_sie_w_limicie_odczytu(swiat):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    frontmatter = FM_X + "#" + "😀" * (LIMIT_POLA - len(FM_X) - 2) + "\n"

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter=frontmatter, brief="😀" * LIMIT_POLA)

    assert odp.status_code == 200, odp.text[:200]
    rozmiar = _plik(swiat.korzen, "x").stat().st_size
    assert 4 * LIMIT_POLA < rozmiar <= 512 * 1024
    assert (await _szczegoly(klient, "x", swiat.projekt_a)).status_code == 200   # runner go odczyta


@pytest.mark.parametrize(("nadmiar", "przechodzi"), [(0, True), (1, False)], ids=["rowno_limit", "bajt_ponad"])
def test_plik_skilla_ponad_512_kb_w_bajtach_to_niespojne_dane_z_opisem(nadmiar, przechodzi):
    from cockpit.services.errors import NiespojneDane

    brief = "b" * (512 * 1024 - len("---\n---\n") - len(FM_X) + nadmiar)

    if przechodzi:
        tekst, skill = skille_api._plik_skilla("x", FM_X, brief)
        assert len(tekst.encode()) == 512 * 1024
        assert skill.id == "x"
    else:
        with pytest.raises(NiespojneDane) as wyjatek:
            skille_api._plik_skilla("x", FM_X, brief)
        assert str(wyjatek.value) == "plik skilla ma 524289 bajtow; limit odczytu runnera to 512 KB"


# =================================================================================
# 11. Runda 5: odmowa dotyczaca samego pliku domowego to "nie globalny"; awaria runnera to blad
# =================================================================================

PREFIKS_OSTRZEZENIA_POZA_ZAKRESEM = "globalny skill 'x' poza zakresem, traktuje jak brak: "
PREFIKS_OSTRZEZENIA_NIECZYTELNY = "globalny skill 'x' nieczytelny, traktuje jak brak: "
# komunikaty odmow runnera o samym pliku domowym, wziete z kodu runnera (nie z kopii napisow)
ODMOWY_RUNNERA_O_PLIKU = {
    "brak_uprawnien": operacje._blad_dysku(PermissionError()),
    "blad_dysku": operacje._blad_dysku(OSError()),
}


def _runner_z_awaria_odczytu_domu(swiat, monkeypatch, *, odpowiedz=None, offline=False) -> None:
    """Podstawia wysylke do runnera: odczyt pliku z katalogu domowego (`~/...`) dostaje
    `odpowiedz()` (slownik albo wyjatek do rzucenia), a wszystko inne robi prawdziwy runner z plikami.
    `offline=True`: po pierwszym prawdziwym odczycie (istnienie pliku projektu) runner przestaje byc
    online, wiec odczyt pliku domowego trafia w `RunnerOffline`."""
    prawdziwy = swiat.runner
    stan = {"offline": False}
    oryginal = zapytania_runnera.runner_online

    async def wyslij(runner_id, typ, dane, timeout_s):
        dom = typ == "read_file" and dane["sciezka"].startswith("~/")
        if dom and odpowiedz is not None:
            prawdziwy.wywolania.append((runner_id, typ, dane))
            wymuszona = odpowiedz()
            if isinstance(wymuszona, Exception):
                raise wymuszona
            return wymuszona
        wynik = await prawdziwy(runner_id, typ, dane, timeout_s)
        if offline and typ == "read_file":
            stan["offline"] = True
        return wynik

    def runner_online(runner, polaczone, teraz=None):
        return not stan["offline"] and oryginal(runner, polaczone, teraz)

    monkeypatch.setattr(bus, "zapytaj", wyslij)
    monkeypatch.setattr(zapytania_runnera, "runner_online", runner_online)


def _ostrzezenia_serwisu(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "cockpit.services.skille_api"]


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@KATALOG_ODSWIEZONY_ALBO_NIE
async def test_domowy_skill_bedacy_dowiazaniem_poza_zakres_runnera_nie_blokuje_zapisu_projektu_ktory_jest_lokalny(
    swiat, caplog, rodzaj, katalog_sie_odswiezy,
):
    obcy = swiat.korzen.parent / "obcy"
    obcy.mkdir()
    cel = obcy / "SKILL.md"
    cel.write_bytes(_tekst_pliku("id: x\nname: Cudzy\nrole: author\n", "Cudzy.\n").encode())
    (swiat.dom / PODKATALOG).mkdir(parents=True)
    (swiat.dom / PODKATALOG / "x").symlink_to(obcy, target_is_directory=True)
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    cudzy = cel.read_bytes()
    if not katalog_sie_odswiezy:   # zakres w odpowiedzi rozstrzyga wtedy odczyt pliku domowego
        swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA
    frontmatter = "id: x\nname: Po\nrole: author\n"

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await (_put(klient, "x", swiat.projekt_a, frontmatter=frontmatter)
                     if rodzaj == "put" else _post(klient, swiat.projekt_a, "x", frontmatter=frontmatter))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert odp.json()["zakres"] == "lokalny"
    assert b"name: Po" in _bajty_pliku(_plik(swiat.korzen, "x"))
    assert cel.read_bytes() == cudzy
    assert [p.name for p in obcy.iterdir()] == ["SKILL.md"]
    poza_zakresem = [m for m in _ostrzezenia_serwisu(caplog) if "poza zakresem" in m]
    assert len(poza_zakresem) == 1, poza_zakresem
    # powod odmowy runnera trafia do logu dosłownie (nie "None")
    assert poza_zakresem == [PREFIKS_OSTRZEZENIA_POZA_ZAKRESEM + KOMUNIKAT_POZA_ZAKRESEM]
    if katalog_sie_odswiezy:
        wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
        szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()
        assert wpis["zakres"] == szczegoly["zakres"] == odp.json()["zakres"] == "lokalny"


AWARIE_RUNNERA_PRZY_ODCZYCIE_PLIKU_DOMOWEGO = {
    "runner_offline": {"offline": True, "komunikat": KOMUNIKAT_OFFLINE},
    "limit_czasu": {
        "odpowiedz": TimeoutError,
        "komunikat": "runner nie odpowiedzial w limicie czasu"},
    "zly_ksztalt_odpowiedzi": {
        "odpowiedz": lambda: {"ok": True, "wynik": {"sciezka": "~/x", "tresc": 5}},
        "komunikat": "odpowiedz runnera ma zly ksztalt (pole tresc nie jest napisem)"},
    "za_duza_odpowiedz": {
        "odpowiedz": lambda: {"ok": True, "wynik": {"sciezka": "~/x", "tresc": "a" * 3000}},
        "limit_kb": 1, "komunikat": "odpowiedz runnera jest za duza (limit 1 KB)"},
    "inna_awaria_runnera": {
        "odpowiedz": lambda: {"ok": False, "blad": "runner zglosil dziwny blad", "kod": "dziwny"},
        "komunikat": "runner zglosil dziwny blad"},
}


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize("awaria", AWARIE_RUNNERA_PRZY_ODCZYCIE_PLIKU_DOMOWEGO.values(),
                         ids=AWARIE_RUNNERA_PRZY_ODCZYCIE_PLIKU_DOMOWEGO.keys())
async def test_awaria_runnera_przy_odczycie_pliku_domowego_to_blad_a_nie_zapis_lokalnego_skilla(
    swiat, monkeypatch, caplog, rodzaj, awaria,
):
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x")
        oryginal = _bajty_pliku(_plik(swiat.korzen, "x"))
    klient = await _klient_z_katalogiem(swiat)
    _runner_z_awaria_odczytu_domu(
        swiat, monkeypatch, odpowiedz=awaria.get("odpowiedz"), offline=awaria.get("offline", False))
    if "limit_kb" in awaria:   # po odswiezeniu katalogu: jego odpowiedz bywa wieksza niz 1 KB
        monkeypatch.setattr(settings, "runner_odpowiedz_max_kb", awaria["limit_kb"])
    swiat.runner.wywolania.clear()

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                     if rodzaj == "put" else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == 409, odp.text
    assert odp.json() == {"detail": awaria["komunikat"]}
    _czytelny_blad(odp, swiat)
    assert _dane_zapisu(swiat.runner, "write_file") == []
    if rodzaj == "put":
        assert _bajty_pliku(_plik(swiat.korzen, "x")) == oryginal
    else:
        assert not _plik(swiat.korzen, "x").exists()
    assert _ostrzezenia_serwisu(caplog) == []   # ani "nieczytelny", ani "poza zakresem"


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize("odmowa", ODMOWY_RUNNERA_O_PLIKU.values(), ids=ODMOWY_RUNNERA_O_PLIKU.keys())
@KATALOG_ODSWIEZONY_ALBO_NIE
async def test_odmowa_runnera_o_pliku_domowym_to_nie_globalny_a_powod_trafia_do_logu(
    swiat, monkeypatch, caplog, rodzaj, odmowa, katalog_sie_odswiezy,
):
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    _runner_z_awaria_odczytu_domu(
        swiat, monkeypatch,
        odpowiedz=lambda: {"ok": False, "blad": odmowa.komunikat, "kod": odmowa.kod})
    if not katalog_sie_odswiezy:
        swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    with caplog.at_level(logging.WARNING, logger="cockpit.services.skille_api"):
        odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                     if rodzaj == "put" else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert odp.json()["zakres"] == "lokalny"
    assert _plik(swiat.korzen, "x").is_file()
    nieczytelne = [m for m in _ostrzezenia_serwisu(caplog) if "nieczytelny" in m]
    assert nieczytelne == [PREFIKS_OSTRZEZENIA_NIECZYTELNY + odmowa.komunikat]


@pytest.mark.parametrize("rodzaj", ["put", "post"])
@pytest.mark.parametrize(("dlugosc_nazwy", "zakres"), [(200, "nadpisany"), (201, "lokalny")],
                         ids=["nazwa_200_znakow_katalog_ja_przyjmuje", "nazwa_201_znakow_katalog_ja_pomija"])
async def test_domowy_skill_z_nazwa_dluzsza_niz_200_znakow_nie_czyni_zapisu_nadpisanym_zgodnie_z_lista_i_get(
    swiat, rodzaj, dlugosc_nazwy, zakres,
):
    _skill(swiat.dom, PODKATALOG, "x", name="N" * dlugosc_nazwy)
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await (_put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")
                 if rodzaj == "put" else _post(klient, swiat.projekt_a, "x"))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    wpis = _wpisy(await _lista(klient, swiat.projekt_a))["x"]
    szczegoly = (await _szczegoly(klient, "x", swiat.projekt_a)).json()
    assert wpis["zakres"] == szczegoly["zakres"] == odp.json()["zakres"] == zakres


@pytest.mark.parametrize("rodzaj", ["put", "post"])
async def test_zapisany_skill_z_nazwa_dluzsza_niz_200_znakow_ktorej_katalog_nie_przyjmuje_ma_zakres_z_dysku(
    swiat, rodzaj,
):
    if rodzaj == "put":
        _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)
    frontmatter = "id: x\nname: " + "N" * 201 + "\nrole: author\n"

    odp = await (_put(klient, "x", swiat.projekt_a, frontmatter=frontmatter)
                 if rodzaj == "put" else _post(klient, swiat.projekt_a, "x", frontmatter=frontmatter))

    assert odp.status_code == (200 if rodzaj == "put" else 201), odp.text
    assert odp.json()["zakres"] == "lokalny"
    assert frontmatter.encode() in _bajty_pliku(_plik(swiat.korzen, "x"))
    assert "x" not in _wpisy(await _lista(klient, swiat.projekt_a))   # katalog skilla nie ma


async def test_przy_nieodswiezonym_katalogu_zakres_w_odpowiedzi_wynika_z_dysku_a_nie_ze_starego_wiersza(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    klient = await _klient_z_katalogiem(swiat)   # katalog zna x jako lokalny
    _skill(swiat.dom, PODKATALOG, "x", name="Z domu")   # po odswiezeniu katalogu
    swiat.runner.wymuszone["katalog_definicji"] = BLAD_DYSKU_RUNNERA

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")

    assert odp.status_code == 200, odp.text
    assert odp.json()["zakres"] == "nadpisany"
    assert _wpisy(await _lista(klient, swiat.projekt_a))["x"]["zakres"] == "lokalny"   # stary cache


async def test_zakres_zapisanego_skilla_nie_miesza_wierszy_innych_runnerow_zespolow_projektow_i_workflow(
    swiat,
):
    _skill(swiat.korzen, PODKATALOG, "x")
    _skill(swiat.korzen, PODKATALOG, "inny")
    _skill(swiat.korzen_b, PODKATALOG, "x")   # ten sam runner, inny projekt, to samo id
    workflowy = swiat.korzen / ".cockpit" / "workflows"
    workflowy.mkdir(parents=True)
    (workflowy / "x.yaml").write_text("steps: []\n", encoding="utf-8")   # workflow o nazwie x
    klient = await _klient_z_katalogiem(swiat)
    # wiersze, ktore odswiezenie runnera projektu zostawia: inny runner i inny zespol, to samo id
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a2, str(swiat.korzen), "x",
                           zakres="globalny")
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_a, str(swiat.korzen), "x",
                           zakres="globalny")

    odp = await _put(klient, "x", swiat.projekt_a, frontmatter="id: x\nname: Po\nrole: author\n")

    assert odp.status_code == 200, odp.text
    assert odp.json()["zakres"] == "lokalny"


async def test_zapisany_skill_dostaje_zakres_swojego_id_a_nie_innego_skilla_projektu(swiat):
    _skill(swiat.korzen, PODKATALOG, "a")
    _skill(swiat.korzen, PODKATALOG, "b")
    _skill(swiat.dom, PODKATALOG, "b")   # tylko b jest w katalogu nadpisany
    klient = await _klient_z_katalogiem(swiat)

    a = await _put(klient, "a", swiat.projekt_a, frontmatter="id: a\nname: Po\nrole: author\n")
    b = await _put(klient, "b", swiat.projekt_a, frontmatter="id: b\nname: Po\nrole: author\n")

    assert (a.status_code, a.json()["zakres"]) == (200, "lokalny")
    assert (b.status_code, b.json()["zakres"]) == (200, "nadpisany")


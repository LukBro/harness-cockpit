"""HAR-180: odczyt skilli projektu ze znacznikiem zakresu (`GET /api/skills`, `GET /api/skills/{id}`).

Lista pochodzi z cache `definicja` (BRO-93, HAR-67): najpierw odswiezenie katalogu przez runnera
(`POST /api/katalog/odswiez`), potem odczyt - takze przy runnerze offline. Szczegoly skilla czytaja
plik przez runnera projektu (`read_file`), a tresc oddaja bajt w bajt. Runner robi PRAWDZIWE
operacje na plikach w katalogu testu (`RunnerZPlikami` z `test_projekty_zmiana.py`).
Zespol B jest zakladany PIERWSZY; jego wiersze maja ten sam `projekt` co projekt zespolu A.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from cockpit.api import bus
from cockpit.db.models import Definicja
from test_projekty_zmiana import (
    KOMUNIKAT_OFFLINE,
    KOMUNIKAT_POZA_ZAKRESEM,
    KOMUNIKAT_PROJEKT_NIEZNANY,
    swiat,
)

FIXTURY_Z_TEST_PROJEKTY_ZMIANA = (swiat,)   # fixture pytest zaimportowana z innego pliku testow

KOMUNIKAT_SKILL_NIEZNANY = "skill nieznany"
DOZWOLONE_ROLE = "author, reviewer, executor, gatherer"
POLA_NA_LISCIE = {"id", "name", "role", "consumes", "produces", "zakres", "problem"}
POLA_SZCZEGOLOW = {"id", "name", "role", "consumes", "produces", "zakres", "sciezka",
                   "frontmatter", "brief"}
STARA_DATA = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _skill(baza: Path, podkatalog: str, katalog: str, *, role: str = "author",
           name: str | None = None, consumes=(), produces=(), brief: str = "Brief skilla.\n") -> Path:
    wiersze = [f"id: {katalog}", f"name: {name or 'Skill ' + katalog}", f"role: {role}"]
    if consumes:
        wiersze.append("consumes: [" + ", ".join(consumes) + "]")
    if produces:
        wiersze.append("produces: [" + ", ".join(produces) + "]")
    plik = baza / podkatalog / katalog / "SKILL.md"
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_bytes(("---\n" + "\n".join(wiersze) + "\n---\n" + brief).encode("utf-8"))
    return plik


def _bajty(baza: Path, podkatalog: str, katalog: str, tresc: str) -> Path:
    plik = baza / podkatalog / katalog / "SKILL.md"
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_bytes(tresc.encode("utf-8"))
    return plik


def _tylko_runner_projektu(swiat) -> None:
    """Odswiezenie katalogu pyta pierwszego runnera online zespolu: zostaje runner projektu."""
    bus.mark_runner_disconnected(swiat.runner_a2)


async def _odswiez(klient) -> None:
    odp = await klient.post("/api/katalog/odswiez")
    assert odp.status_code == 200, odp.text


async def _lista(klient, projekt_id):
    return await klient.get("/api/skills", params={"project": projekt_id})


async def _szczegoly(klient, skill_id, projekt_id):
    return await klient.get(f"/api/skills/{skill_id}", params={"project": projekt_id})


async def _klient_z_katalogiem(swiat, user=None):
    """Zalogowany klient zespolu A po odswiezeniu katalogu z plikow w katalogu testu."""
    _tylko_runner_projektu(swiat)
    klient = await swiat.zalogowany(user or swiat.wlasciciel_a)
    await _odswiez(klient)
    swiat.runner.wywolania.clear()
    return klient


def _wpisy(odp) -> dict[str, dict]:
    assert odp.status_code == 200, odp.text
    return {w["id"]: w for w in odp.json()["skille"]}


async def _wstaw_definicje(swiat, tenant_id, runner_id, projekt, klucz, *, zakres="lokalny",
                           problem=None, rola="author", rodzaj="skill", odswiezono_at=STARA_DATA):
    async with swiat.maker() as s:
        s.add(Definicja(
            id=str(uuid.uuid4()), tenant_id=tenant_id, runner_id=runner_id, projekt=projekt,
            rodzaj=rodzaj, klucz=klucz, nazwa=f"Nazwa {klucz}", rola=rola,
            sciezka=f".cockpit/skills/{klucz}/SKILL.md", odswiezono_at=odswiezono_at,
            consumes=[], produces=[], zakres=zakres, problem=problem))
        await s.commit()


# =================================================================================
# 1. GET /api/skills?project=<id>: rozstrzygniety rejestr ze znacznikiem zakresu
# =================================================================================

async def test_lista_skilli_projektu_ma_dla_kazdego_id_nazwe_role_consumes_produces_i_zakres(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "pisanie", name="Pisanie tekstu",
           consumes=("plan.md",), produces=("tekst.md",))
    _skill(swiat.dom, ".cockpit/skills", "recenzja", role="reviewer", name="Recenzja",
           produces=("raport.md",))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert set(odp.json()) == {"odswiezono_at", "skille"}
    assert all(set(w) == POLA_NA_LISCIE for w in odp.json()["skille"])
    assert odp.json()["skille"] == [
        {"id": "pisanie", "name": "Pisanie tekstu", "role": "author", "consumes": ["plan.md"],
         "produces": ["tekst.md"], "zakres": "lokalny", "problem": None},
        {"id": "recenzja", "name": "Recenzja", "role": "reviewer", "consumes": [],
         "produces": ["raport.md"], "zakres": "globalny", "problem": None},
    ]


async def test_lista_skilli_jest_posortowana_po_id(swiat):
    for katalog in ("zeta", "alfa", "mu"):
        _skill(swiat.korzen, ".cockpit/skills", katalog)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["alfa", "mu", "zeta"]


async def test_lokalny_skill_o_id_globalnego_jest_oznaczony_jako_nadpisany(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", role="reviewer", name="Z repo",
           consumes=("lokalny.md",), produces=("l.md",))
    _skill(swiat.dom, ".cockpit/skills", "x", role="author", name="Z domu",
           consumes=("globalny.md",), produces=("g.md",))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert odp.json()["skille"] == [
        {"id": "x", "name": "Z repo", "role": "reviewer", "consumes": ["lokalny.md"],
         "produces": ["l.md"], "zakres": "nadpisany", "problem": None}]   # jeden wpis, dane lokalnego


async def test_skill_ze_zrodla_konwencyjnego_o_id_globalnego_tez_jest_nadpisany(swiat):
    _skill(swiat.korzen, ".claude/skills", "x", name="Z claude")
    _skill(swiat.dom, ".cockpit/skills", "x", name="Z domu")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [(w["id"], w["name"], w["zakres"]) for w in odp.json()["skille"]] == [
        ("x", "Z claude", "nadpisany")]


async def test_ten_sam_id_w_dwoch_zrodlach_repo_bez_globalnego_jest_lokalny_a_nie_nadpisany(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", name="Z kokpitu")
    _skill(swiat.korzen, ".claude/skills", "x", name="Z claude")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [(w["id"], w["name"], w["zakres"]) for w in odp.json()["skille"]] == [
        ("x", "Z kokpitu", "lokalny")]


async def test_skill_globalny_bez_lokalnego_odpowiednika_w_tym_projekcie_jest_globalny(swiat):
    _skill(swiat.dom, ".cockpit/skills", "x")
    _skill(swiat.korzen, ".cockpit/skills", "inny")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert {w["id"]: w["zakres"] for w in odp.json()["skille"]} == {
        "x": "globalny", "inny": "lokalny"}


async def test_skill_z_bledem_jest_na_liscie_jako_problem_a_pozostale_skille_dalej_sa(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "dobry", name="Dobry")
    _skill(swiat.korzen, ".cockpit/skills", "zla-rola", role="czarodziej")
    _skill(swiat.korzen, ".cockpit/skills", "dwa", role="reviewer", produces=("a.md", "b.md"))
    _skill(swiat.dom, ".cockpit/skills", "inne-id").write_text(
        "---\nid: cos-innego\nname: X\nrole: author\n---\nB\n", encoding="utf-8")
    _skill(swiat.korzen, ".cockpit/skills", "pozniejszy", name="Pozniejszy")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    wpisy = _wpisy(odp)
    assert set(wpisy) == {"dobry", "zla-rola", "dwa", "inne-id", "pozniejszy"}
    assert wpisy["dobry"]["problem"] is None
    assert wpisy["pozniejszy"]["problem"] is None
    assert wpisy["zla-rola"] == {
        "id": "zla-rola", "name": "zla-rola", "role": None, "consumes": None, "produces": None,
        "zakres": "lokalny",
        "problem": f"nieznana rola 'czarodziej' w 'zla-rola' (dozwolone: {DOZWOLONE_ROLE})"}
    assert wpisy["dwa"]["problem"] == (
        "skill dwa: rola reviewer oddaje jeden artefakt, produces ma 2: a.md, b.md")
    assert wpisy["dwa"]["role"] is None
    assert wpisy["inne-id"]["problem"] == (
        "id 'cos-innego' rozni sie od nazwy katalogu 'inne-id' w 'inne-id'")
    assert wpisy["inne-id"]["zakres"] == "globalny"


async def test_skill_poprawny_bez_consumes_i_produces_ma_na_liscie_pusta_liste_a_zepsuty_brak_pomiaru(
        swiat):
    _skill(swiat.korzen, ".cockpit/skills", "pusty", name="Pusty")
    _skill(swiat.korzen, ".cockpit/skills", "zepsuty", role="czarodziej")
    klient = await _klient_z_katalogiem(swiat)

    wpisy = _wpisy(await _lista(klient, swiat.projekt_a))

    assert (wpisy["pusty"]["consumes"], wpisy["pusty"]["produces"]) == ([], [])
    assert (wpisy["zepsuty"]["consumes"], wpisy["zepsuty"]["produces"]) == (None, None)


async def test_skill_bez_frontmattera_jest_na_liscie_jako_problem_z_niepustym_komunikatem(swiat):
    _bajty(swiat.korzen, ".cockpit/skills", "goly", "Sam brief bez frontmattera\n")
    _skill(swiat.korzen, ".cockpit/skills", "dobry")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    wpisy = _wpisy(odp)
    assert set(wpisy) == {"goly", "dobry"}
    assert isinstance(wpisy["goly"]["problem"], str) and wpisy["goly"]["problem"]
    assert wpisy["dobry"]["problem"] is None


async def test_skill_cudzego_narzedzia_bez_roli_nie_jest_skillem_kokpitu_ani_problemem(swiat):
    _bajty(swiat.korzen, ".claude/skills", "cudzy", "---\nname: Cudzy\n---\nB\n")
    _skill(swiat.korzen, ".cockpit/skills", "nasz")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["nasz"]


async def test_lista_nie_pyta_runnera_bo_czyta_tylko_cache(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)

    await _lista(klient, swiat.projekt_a)

    assert swiat.runner.wywolania == []


async def test_gdy_runner_jest_offline_lista_nadal_pochodzi_z_cache_z_data_odswiezenia(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", name="Z repo")
    _skill(swiat.dom, ".cockpit/skills", "x", name="Z domu")
    _skill(swiat.dom, ".cockpit/skills", "g")
    klient = await _klient_z_katalogiem(swiat)
    online = await _lista(klient, swiat.projekt_a)
    bus.mark_runner_disconnected(swiat.runner_a)

    offline = await _lista(klient, swiat.projekt_a)

    assert offline.status_code == 200, offline.text
    assert offline.json() == online.json()
    async with swiat.maker() as s:
        daty = (await s.execute(select(Definicja.odswiezono_at))).scalars().all()
    assert offline.json()["odswiezono_at"] == min(daty).replace(tzinfo=timezone.utc).isoformat()
    assert len(offline.json()["skille"]) == 2


async def test_projekt_bez_odswiezonego_katalogu_ma_pusta_liste_bez_daty_a_nie_blad(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _lista(klient, swiat.projekt_a)

    assert (odp.status_code, odp.json()) == (200, {"odswiezono_at": None, "skille": []})


async def test_lista_nie_zawiera_workflow_ze_wspolnego_katalogu_definicji(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    (swiat.korzen / ".cockpit" / "workflows").mkdir(parents=True)
    (swiat.korzen / ".cockpit" / "workflows" / "wydanie.yaml").write_text("name: wydanie\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["x"]


# ---- zespol, runner, projekt ----------------------------------------------------------------

async def test_projekt_innego_zespolu_daje_404_na_liscie_skilli(swiat):
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_b, str(swiat.korzen_b), "obcy")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _lista(klient, swiat.projekt_b)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}


async def test_nieznany_projekt_daje_404_na_liscie_skilli(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _lista(klient, "nie-ma-takiego")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}


async def test_lista_skilli_wymaga_parametru_projektu_o_dlugosci_do_36_znakow(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    bez = await klient.get("/api/skills")
    za_dlugi = await _lista(klient, "p" * 37)
    graniczny = await _lista(klient, "p" * 36)

    assert (bez.status_code, za_dlugi.status_code) == (422, 422)
    assert graniczny.status_code == 404   # walidacja przeszla; takiego projektu nie ma


async def test_lista_skilli_zespolu_a_nie_zawiera_wpisow_zespolu_b_o_tej_samej_sciezce_projektu(
    swiat,
):
    _skill(swiat.korzen, ".cockpit/skills", "alfa-skill")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_b, str(swiat.korzen), "beta-skill")

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["alfa-skill"]


async def test_lista_skilli_projektu_nie_zawiera_wpisow_innego_runnera_tego_samego_zespolu(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "ten-runner")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a2, str(swiat.korzen), "inny-runner")

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["ten-runner"]


async def test_lista_skilli_projektu_nie_zawiera_wpisow_innego_projektu_tego_samego_runnera(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "ten-projekt")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, "/inny/projekt", "inny-projekt")

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["ten-projekt"]


async def test_data_odswiezenia_listy_pochodzi_z_wierszy_projektu_a_nie_z_wierszy_innych_zespolow(
    swiat,
):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_b, str(swiat.korzen), "stary")

    odp = await _lista(klient, swiat.projekt_a)

    assert datetime.fromisoformat(odp.json()["odswiezono_at"]) > STARA_DATA


async def test_obserwator_zespolu_widzi_liste_skilli(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat, swiat.obserwator_a)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["x"]


async def test_bez_sesji_lista_i_szczegoly_skilli_daja_401(swiat):
    klient = swiat.nowy_klient()

    lista = await _lista(klient, swiat.projekt_a)
    szczegoly = await _szczegoly(klient, "x", swiat.projekt_a)

    assert (lista.status_code, szczegoly.status_code) == (401, 401)
    assert swiat.runner.wywolania == []


# =================================================================================
# 2. GET /api/skills/{id}?project=<id>: frontmatter i brief bajt w bajt
# =================================================================================

TRESC_TRUDNA = (
    "---\r\nid: trudny\r\nname: Zażółć gęślą jaźń\r\nrole: author\r\n---\r\n"
    "Pierwsza linia z końcowymi spacjami   \r\n\r\nDruga ✓ po pustej\r\n---\r\n"
    "Po dalszej granicy  \t"
)


async def test_szczegoly_skilla_oddaja_frontmatter_i_brief_bajt_w_bajt(swiat):
    _bajty(swiat.korzen, ".cockpit/skills", "trudny", TRESC_TRUDNA)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _szczegoly(klient, "trudny", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert odp.json()["frontmatter"] == (
        "id: trudny\r\nname: Zażółć gęślą jaźń\r\nrole: author\r\n")
    assert odp.json()["brief"] == (
        "Pierwsza linia z końcowymi spacjami   \r\n\r\nDruga ✓ po pustej\r\n---\r\n"
        "Po dalszej granicy  \t")


async def test_brief_bez_koncowej_nowej_linii_i_pusty_brief_nie_sa_normalizowane(swiat):
    _bajty(swiat.korzen, ".cockpit/skills", "bez-nl", "---\nid: bez-nl\nname: B\nrole: author\n---\nx")
    _bajty(swiat.korzen, ".cockpit/skills", "pusty", "---\nid: pusty\nname: P\nrole: author\n---\n")
    klient = await _klient_z_katalogiem(swiat)

    bez_nl = await _szczegoly(klient, "bez-nl", swiat.projekt_a)
    pusty = await _szczegoly(klient, "pusty", swiat.projekt_a)

    assert bez_nl.json()["brief"] == "x"
    assert pusty.json()["brief"] == ""
    assert pusty.json()["frontmatter"] == "id: pusty\nname: P\nrole: author\n"


async def test_szczegoly_niosa_pola_skilla_zakres_i_sciezke_z_cache(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", role="reviewer", name="Recenzja X",
           consumes=("diff.md",), produces=("raport.md",))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert set(odp.json()) == POLA_SZCZEGOLOW
    assert {k: odp.json()[k] for k in POLA_SZCZEGOLOW - {"frontmatter", "brief"}} == {
        "id": "x", "name": "Recenzja X", "role": "reviewer", "consumes": ["diff.md"],
        "produces": ["raport.md"], "zakres": "lokalny", "sciezka": ".cockpit/skills/x/SKILL.md"}


async def test_skill_lokalny_jest_czytany_przez_runnera_projektu_z_bezwzgledna_sciezka_pliku(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)

    await _szczegoly(klient, "x", swiat.projekt_a)

    assert swiat.runner.wywolania == [(
        swiat.runner_a, "read_file",
        {"sciezka": f"{swiat.korzen}/.cockpit/skills/x/SKILL.md"})]


async def test_skill_ze_zrodla_konwencyjnego_jest_czytany_z_jego_podkatalogu_w_repo(swiat):
    _skill(swiat.korzen, ".claude/skills", "x", brief="Brief z claude.\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.json()["brief"] == "Brief z claude.\n"
    assert swiat.runner.wywolania[0][2] == {
        "sciezka": f"{swiat.korzen}/.claude/skills/x/SKILL.md"}


async def test_skill_domowy_jest_czytany_ze_sciezka_z_tylda_taka_jak_w_cache(swiat):
    _skill(swiat.dom, ".cockpit/skills", "glob", name="Globalny", brief="Brief globalny.\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _szczegoly(klient, "glob", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert swiat.runner.wywolania == [(
        swiat.runner_a, "read_file", {"sciezka": "~/.cockpit/skills/glob/SKILL.md"})]
    assert odp.json()["brief"] == "Brief globalny.\n"
    assert odp.json()["zakres"] == "globalny"
    assert odp.json()["sciezka"] == "~/.cockpit/skills/glob/SKILL.md"


async def test_szczegoly_nadpisanego_skilla_pochodza_z_pliku_lokalnego_z_zakresem_nadpisany(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", name="Z repo", brief="Brief lokalny.\n")
    _skill(swiat.dom, ".cockpit/skills", "x", name="Z domu", brief="Brief globalny.\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert (odp.json()["name"], odp.json()["brief"], odp.json()["zakres"]) == (
        "Z repo", "Brief lokalny.\n", "nadpisany")
    assert [w[2]["sciezka"] for w in swiat.runner.wywolania] == [
        f"{swiat.korzen}/.cockpit/skills/x/SKILL.md"]


async def test_szczegoly_czytaja_biezacy_plik_a_lista_zostaje_przy_cache_do_odswiezenia(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", produces=("stary.md",))
    klient = await _klient_z_katalogiem(swiat)
    _skill(swiat.korzen, ".cockpit/skills", "x", produces=("nowy.md",), brief="Nowy brief.\n")

    lista = await _lista(klient, swiat.projekt_a)
    szczegoly = await _szczegoly(klient, "x", swiat.projekt_a)

    assert lista.json()["skille"][0]["produces"] == ["stary.md"]
    assert (szczegoly.json()["produces"], szczegoly.json()["brief"]) == (
        ["nowy.md"], "Nowy brief.\n")


async def test_skill_zepsuty_po_odswiezeniu_to_422_z_komunikatem_a_nie_500(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    _skill(swiat.korzen, ".cockpit/skills", "x", role="czarodziej")   # psuje sie po odswiezeniu

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 422
    assert odp.json() == {
        "detail": f"nieznana rola 'czarodziej' w 'x' (dozwolone: {DOZWOLONE_ROLE})"}


async def test_skill_usuniety_z_dysku_po_odswiezeniu_to_409_z_komunikatem_runnera(swiat):
    plik = _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    plik.unlink()

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json()["detail"]


async def test_odmowa_odczytu_przez_runnera_poza_zakresem_to_403(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["read_file"] = {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"}

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}


@pytest.mark.parametrize("wynik", [
    {"sciezka": "x"},
    {"sciezka": "x", "tresc": 5, "rozmiar": 1},
    {"sciezka": "x", "tresc": None, "rozmiar": 1},
], ids=["brak_tresci", "tresc_liczba", "tresc_null"])
async def test_zly_ksztalt_odpowiedzi_odczytu_to_409_z_opisem_a_nie_500(swiat, wynik):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["read_file"] = {"ok": True, "wynik": wynik}

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json()["detail"].startswith("odpowiedz runnera ma zly ksztalt")


async def test_runner_offline_daje_na_szczegolach_409_z_pelnym_komunikatem(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    for rid in (swiat.runner_a, swiat.runner_a2):
        bus.mark_runner_disconnected(rid)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []


async def test_runner_projektu_offline_to_409_na_szczegolach_choc_inny_runner_zespolu_jest_online(
    swiat,
):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_disconnected(swiat.runner_a)
    bus.mark_runner_connected(swiat.runner_a2)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []


async def test_nieznany_skill_daje_404_a_runner_nie_jest_pytany(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _szczegoly(klient, "nie-ma-takiego", swiat.projekt_a)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_SKILL_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_szczegoly_skilla_projektu_innego_zespolu_daja_404_na_projekcie(swiat):
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_b, str(swiat.korzen_b), "obcy")
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _szczegoly(klient, "obcy", swiat.projekt_b)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_nieznany_projekt_daje_404_na_szczegolach(swiat):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _szczegoly(klient, "x", "nie-ma-takiego")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}


async def test_skill_istniejacy_tylko_u_zespolu_b_o_tej_samej_sciezce_projektu_to_404_u_zespolu_a(
    swiat,
):
    _skill(swiat.korzen, ".cockpit/skills", "alfa")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_b, str(swiat.korzen), "tylko-beta")

    odp = await _szczegoly(klient, "tylko-beta", swiat.projekt_a)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_SKILL_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_skill_istniejacy_tylko_u_innego_runnera_zespolu_to_404_w_projekcie_tego_runnera(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "alfa")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a2, str(swiat.korzen), "tylko-a2")

    odp = await _szczegoly(klient, "tylko-a2", swiat.projekt_a)

    assert odp.status_code == 404
    assert swiat.runner.wywolania == []


async def test_skill_istniejacy_tylko_w_innym_projekcie_tego_samego_runnera_to_404(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "alfa")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, "/inny/projekt", "tylko-tam")

    odp = await _szczegoly(klient, "tylko-tam", swiat.projekt_a)

    assert odp.status_code == 404
    assert swiat.runner.wywolania == []


async def test_obserwator_zespolu_widzi_szczegoly_skilla(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "x", brief="Brief.\n")
    klient = await _klient_z_katalogiem(swiat, swiat.obserwator_a)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert odp.json()["brief"] == "Brief.\n"


# =================================================================================
# 3. Runda poprawek: warunki zapytania, zrodlo scisle i niescisle, sciezka pliku
# =================================================================================

SAMA_ROLA = "---\nrole: author\n---\nBrief po utracie pol.\n"
KOMUNIKAT_NIE_SKILL = "plik skilla 'x' nie jest skillem kokpitu: brak frontmattera albo pola role"


async def test_wiersz_sprzed_migracji_bez_zakresu_nie_jest_na_liscie_skilli_projektu(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "nowy")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(
        swiat, swiat.tenant_a, swiat.runner_a, str(swiat.korzen), "stary", zakres=None)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["nowy"]


async def test_wiersz_sprzed_migracji_bez_zakresu_to_w_szczegolach_404_bez_pytania_runnera(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "stary")
    klient = await _klient_z_katalogiem(swiat)
    async with swiat.maker() as s:
        wiersz = (await s.execute(select(Definicja).where(Definicja.klucz == "stary"))).scalar_one()
        wiersz.zakres = None   # tak wyglada wiersz zapisany przed migracja 0015
        await s.commit()

    odp = await _szczegoly(klient, "stary", swiat.projekt_a)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_SKILL_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_wiersz_workflow_z_zakresem_nie_jest_skillem_ani_na_liscie_ani_w_szczegolach(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "prawdziwy")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(
        swiat, swiat.tenant_a, swiat.runner_a, str(swiat.korzen), "wydanie",
        rodzaj="workflow", rola=None, zakres="lokalny")

    lista = await _lista(klient, swiat.projekt_a)
    szczegoly = await _szczegoly(klient, "wydanie", swiat.projekt_a)

    assert [w["id"] for w in lista.json()["skille"]] == ["prawdziwy"]
    assert szczegoly.status_code == 404
    assert szczegoly.json() == {"detail": KOMUNIKAT_SKILL_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_wiersz_innego_zespolu_o_tym_samym_runnerze_i_projekcie_nie_jest_na_liscie_skilli(
    swiat,
):
    # tylko zespol odroznia ten wiersz: runner i sciezka projektu sa takie same jak u zespolu A
    _skill(swiat.korzen, ".cockpit/skills", "nasz")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_a, str(swiat.korzen), "cudzy")

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["id"] for w in odp.json()["skille"]] == ["nasz"]


async def test_wiersz_innego_zespolu_o_tym_samym_runnerze_i_projekcie_to_w_szczegolach_404(swiat):
    _skill(swiat.korzen, ".cockpit/skills", "nasz")
    klient = await _klient_z_katalogiem(swiat)
    await _wstaw_definicje(swiat, swiat.tenant_b, swiat.runner_a, str(swiat.korzen), "cudzy")

    odp = await _szczegoly(klient, "cudzy", swiat.projekt_a)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_SKILL_NIEZNANY}
    assert swiat.runner.wywolania == []


async def test_data_odswiezenia_listy_to_najstarsza_z_dat_wierszy_projektu(swiat):
    nowsza = datetime(2025, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
    for klucz, data in (("a-nowszy", nowsza), ("b-starszy", STARA_DATA), ("c-nowszy", nowsza)):
        await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, str(swiat.korzen), klucz,
                               odswiezono_at=data)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.json()["odswiezono_at"] == STARA_DATA.isoformat()


async def test_skille_domowy_i_repo_jednego_projektu_sa_czytane_kazdy_ze_swoja_sciezka(swiat):
    _skill(swiat.dom, ".cockpit/skills", "globalny-x", brief="Z domu.\n")
    _skill(swiat.korzen, ".cockpit/skills", "lokalny-x", brief="Z repo.\n")
    klient = await _klient_z_katalogiem(swiat)

    dom = await _szczegoly(klient, "globalny-x", swiat.projekt_a)
    repo = await _szczegoly(klient, "lokalny-x", swiat.projekt_a)

    assert (dom.json()["brief"], repo.json()["brief"]) == ("Z domu.\n", "Z repo.\n")
    assert [w[2]["sciezka"] for w in swiat.runner.wywolania] == [
        "~/.cockpit/skills/globalny-x/SKILL.md",
        f"{swiat.korzen}/.cockpit/skills/lokalny-x/SKILL.md"]


@pytest.mark.parametrize("zrodlo", ["korzen", "dom"])
async def test_skill_zrodla_kokpitu_ktory_po_odswiezeniu_stracil_id_i_nazwe_to_422_a_nie_skill(
    swiat, zrodlo,
):
    baza = getattr(swiat, zrodlo)
    _skill(baza, ".cockpit/skills", "x")
    klient = await _klient_z_katalogiem(swiat)
    _bajty(baza, ".cockpit/skills", "x", SAMA_ROLA)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 422
    assert odp.json() == {"detail": "brak pola 'id' w 'x'"}


@pytest.mark.parametrize("podkatalog", [".claude/skills", ".agents/skills"])
async def test_skill_zrodla_konwencyjnego_z_sama_rola_dostaje_id_i_nazwe_z_katalogu(
    swiat, podkatalog,
):
    _skill(swiat.korzen, podkatalog, "x")
    klient = await _klient_z_katalogiem(swiat)
    _bajty(swiat.korzen, podkatalog, "x", SAMA_ROLA)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert (odp.json()["id"], odp.json()["name"], odp.json()["role"]) == ("x", "x", "author")
    assert odp.json()["brief"] == "Brief po utracie pol.\n"


@pytest.mark.parametrize("podkatalog", [".claude/skills", ".agents/skills"])
@pytest.mark.parametrize("tresc", ["---\nname: Cudzy\n---\nBrief bez roli.\n", "Sam brief.\n"],
                         ids=["bez_roli", "bez_frontmattera"])
async def test_plik_zrodla_konwencyjnego_ktory_po_odswiezeniu_przestal_byc_skillem_to_422(
    swiat, podkatalog, tresc,
):
    _skill(swiat.korzen, podkatalog, "x")
    klient = await _klient_z_katalogiem(swiat)
    _bajty(swiat.korzen, podkatalog, "x", tresc)

    odp = await _szczegoly(klient, "x", swiat.projekt_a)

    assert odp.status_code == 422
    assert odp.json() == {"detail": KOMUNIKAT_NIE_SKILL}

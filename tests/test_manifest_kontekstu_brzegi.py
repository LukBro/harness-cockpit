"""HAR-169 (tura 2c): brzegi skladania manifestu - brak dowodu to blad, nie pominiecie.

Kazdy test ma wejscie, na ktorym decyduje jeden element: skladnik sciezki bedacy plikiem,
katalog bez praw dostepu, sciezka planu poza worktree, limit wpisow, deduplikacja plikow
w prompcie, zakres lekcji w bloku kodu. Funkcje sa prawdziwe (bez zaslepek).
"""
from __future__ import annotations

import errno
import os
from dataclasses import dataclass

import pytest

from cockpit.services.lekcje import BRAK_ZAKRESU, waliduj_lekcje, wpisy_lekcji
from cockpit.services.warstwy_instrukcji import WarstwaInstrukcji
from runner.kontekst import BladManifestu, zloz_manifest
from test_manifest_kontekstu import (
    LEKCJE,
    KROK,
    dopisz_do_repo,
    id_lekcji,
    lekcja,
    manifest_z_lekcjami,
    plik_lekcji,
    swiat_z_kontekstem,
    wczytaj_manifest,
    wpisy_warstwy,
    zapisz,
    zloz,
)
from test_silnik_runu import K, Szpieg, jedno, rodzaje, swiat, uruchom

KATALOGU = WarstwaInstrukcji.claude_md_katalogu


def komunikat_odmowy(wt, sciezka: str) -> str:
    blad = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(wt / sciezka))
    return f"nie można odczytać pliku warstwy {sciezka}: {blad}"


# --- skladnik sciezki bedacy plikiem, katalog w miejscu pliku -----------------------------------


def test_katalog_planu_bedacy_zwyklym_plikiem_nie_daje_claude_md_katalogu_ani_bledu(tmp_path):
    zapisz(tmp_path, "a", "to jest plik, nie katalog")
    zapisz(tmp_path, "CLAUDE.md", "# korzen\n")

    manifest = zloz(tmp_path, ("a/b/plan.md",))

    assert wpisy_warstwy(manifest, KATALOGU) == []
    assert [w.sciezka for w in wpisy_warstwy(manifest, WarstwaInstrukcji.claude_md_korzenia)] == [
        "CLAUDE.md"]


def test_cockpit_bedacy_zwyklym_plikiem_pomija_project_yaml_i_skill_bez_bledu(tmp_path):
    zapisz(tmp_path, ".cockpit", "to jest plik, nie katalog")

    manifest = zloz(tmp_path, ())

    assert [WarstwaInstrukcji(w.warstwa) for w in manifest.wpisy] == [
        WarstwaInstrukcji.niezmienniki_silnika]


def test_katalog_o_nazwie_claude_md_nie_jest_plikiem_warstwy_i_szukanie_idzie_wyzej(tmp_path):
    (tmp_path / "a/b/CLAUDE.md").mkdir(parents=True)
    zapisz(tmp_path, "a/CLAUDE.md", "# a\n")

    manifest = zloz(tmp_path, ("a/b/x.md",))

    assert [w.sciezka for w in wpisy_warstwy(manifest, KATALOGU)] == ["a/CLAUDE.md"]


def test_brak_claude_md_w_katalogu_planu_daje_najblizszy_plik_z_katalogu_wyzej(tmp_path):
    zapisz(tmp_path, "a/CLAUDE.md", "# a\n")    # a/b/CLAUDE.md nie istnieje

    manifest = zloz(tmp_path, ("a/b/x.md",))

    assert [w.sciezka for w in wpisy_warstwy(manifest, KATALOGU)] == ["a/CLAUDE.md"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root przechodzi przez katalog bez uprawnien")
def test_katalog_planu_bez_praw_dostepu_to_blad_manifestu_z_pelnym_komunikatem(tmp_path):
    zapisz(tmp_path, "a/b/CLAUDE.md", "# b\n")
    zakazany = tmp_path / "a"
    zakazany.chmod(0)
    try:
        with pytest.raises(BladManifestu) as wyjatek:
            zloz(tmp_path, ("a/b/plan.md",))
    finally:
        zakazany.chmod(0o700)

    assert type(wyjatek.value) is BladManifestu
    assert str(wyjatek.value) == komunikat_odmowy(tmp_path, "a/b/CLAUDE.md")


# --- sciezki planu poza worktree ----------------------------------------------------------------


def test_bezwzgledna_sciezka_planu_to_blad_manifestu_mimo_claude_md_w_jej_katalogu(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    zapisz(tmp_path, "obce/CLAUDE.md", "# poza worktree\n")
    plan = str(tmp_path / "obce" / "plan.md")

    with pytest.raises(BladManifestu) as wyjatek:
        zloz(wt, (plan,))

    assert str(wyjatek.value) == f"ścieżka planu {plan} wskazuje poza worktree"


def test_sciezka_planu_wychodzaca_w_gore_to_blad_manifestu_mimo_claude_md_w_tym_katalogu(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    zapisz(tmp_path, "obce/CLAUDE.md", "# poza worktree\n")

    with pytest.raises(BladManifestu) as wyjatek:
        zloz(wt, ("../obce/plan.md",))

    assert str(wyjatek.value) == "ścieżka planu ../obce/plan.md wskazuje poza worktree"


def test_sciezka_planu_z_kropkami_w_srodku_jest_odrzucona_takze_gdy_zostaje_w_worktree(tmp_path):
    zapisz(tmp_path, "b/CLAUDE.md", "# b\n")

    with pytest.raises(BladManifestu) as wyjatek:
        zloz(tmp_path, ("a/../b/plan.md",))

    assert str(wyjatek.value) == "ścieżka planu a/../b/plan.md wskazuje poza worktree"


def test_zla_sciezka_planu_na_drugim_miejscu_tez_jest_odrzucona(tmp_path):
    with pytest.raises(BladManifestu) as wyjatek:
        zloz(tmp_path, ("a/plan.md", "../obce/plan.md"))

    assert str(wyjatek.value) == "ścieżka planu ../obce/plan.md wskazuje poza worktree"


def test_nazwa_katalogu_zaczynajaca_sie_od_dwoch_kropek_nie_jest_wyjsciem_w_gore(tmp_path):
    zapisz(tmp_path, "a/..b/CLAUDE.md", "# kropki w nazwie\n")

    manifest = zloz(tmp_path, ("a/..b/plan.md",))

    assert [w.sciezka for w in wpisy_warstwy(manifest, KATALOGU)] == ["a/..b/CLAUDE.md"]


# --- znaki sterujace w skill_id i sciezkach planu: ta sama regula co prompt agenta ------------------

ZNAKI_STERUJACE = {
    "nul": "\x00", "nowa-linia": "\n", "separator-linii": " ", "separator-akapitu": " "}
ZNAK_NA_POZYCJI = [
    pytest.param(znak, pozycja, id=f"{nazwa}-{pozycja}")
    for nazwa, znak in ZNAKI_STERUJACE.items() for pozycja in (0, 1)
]


@pytest.mark.parametrize(("znak", "pozycja"), ZNAK_NA_POZYCJI)
def test_znak_sterujacy_w_sciezce_planu_to_blad_manifestu_z_pelnym_komunikatem(
    tmp_path, znak, pozycja,
):
    zla = f"a/b{znak}plan.md"

    with pytest.raises(BladManifestu) as wyjatek:
        zloz(tmp_path, ["a/dobra.md"] * pozycja + [zla])

    assert type(wyjatek.value) is BladManifestu
    assert str(wyjatek.value) == f"pole sciezki_planu[{pozycja}] zawiera znak sterujący: {zla!r}"


def test_znak_sterujacy_w_skill_id_to_blad_manifestu_z_pelnym_komunikatem(tmp_path):
    with pytest.raises(BladManifestu) as wyjatek:
        zloz_manifest(tmp_path, KROK, [], skill_id="pi\x00sanie")

    assert type(wyjatek.value) is BladManifestu
    assert str(wyjatek.value) == "pole skill_id zawiera znak sterujący: 'pi\\x00sanie'"


def test_nielamliwa_spacja_w_sciezce_planu_nie_jest_znakiem_sterujacym(tmp_path):
    zapisz(tmp_path, "a b/CLAUDE.md", "# katalog ze spacja niełamliwą\n")

    manifest = zloz(tmp_path, ("a b/plan.md",))

    assert [w.sciezka for w in wpisy_warstwy(manifest, KATALOGU)] == ["a b/CLAUDE.md"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez uprawnien")
def test_znak_sterujacy_w_sciezce_planu_jest_odrzucony_przed_odczytem_plikow_warstw(tmp_path):
    zapisz(tmp_path, ".cockpit/project.yaml", "stack: [python]\n")
    plik = tmp_path / ".cockpit/project.yaml"
    plik.chmod(0)    # odczyt dysku dalby inny komunikat niz odrzucenie sciezki
    try:
        with pytest.raises(BladManifestu) as wyjatek:
            zloz(tmp_path, ("a/\x00plan.md",))
    finally:
        plik.chmod(0o600)

    assert str(wyjatek.value) == "pole sciezki_planu[0] zawiera znak sterujący: 'a/\\x00plan.md'"


# --- limity sprawdzane w zloz_manifest -----------------------------------------------------------


def test_wpis_ze_sciezka_ponad_limit_to_blad_manifestu_z_pelnym_komunikatem(tmp_path):
    katalog = "/".join(["d" * 100] * 3)
    zapisz(tmp_path, f"{katalog}/CLAUDE.md", "# gleboko\n")

    with pytest.raises(BladManifestu) as wyjatek:
        zloz(tmp_path, (f"{katalog}/plan.md",))

    assert str(wyjatek.value) == (
        f"wpis manifestu ponad limit pól: {KATALOGU.value} {katalog}/CLAUDE.md")


def test_tysiac_lekcji_z_wpisem_niezmiennikow_to_blad_manifestu_z_pelnym_komunikatem(tmp_path):
    zapisz(tmp_path, LEKCJE, plik_lekcji(*[lekcja(i, KROK) for i in range(1000)]))

    with pytest.raises(BladManifestu) as wyjatek:
        zloz(tmp_path, ())

    assert str(wyjatek.value) == "manifest ma 1001 wpisów, limit 1000"


# --- silnik: porazki skladania manifestu -----------------------------------------------------------


async def test_manifest_ponad_limit_konczy_krok_porazka_bez_agenta_zdarzenia_i_pliku(tmp_path):
    s = swiat_z_kontekstem(tmp_path)
    dopisz_do_repo(s, {LEKCJE: plik_lekcji(*[lekcja(i, "a") for i in range(1000)])})
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert agent.kroki == []
    assert jedno(zdarzenia, "step_failed", "a")["text"] == "manifest ma 1005 wpisów, limit 1000"
    assert not any(z["kind"] == "context_manifest" for z in zdarzenia)
    assert not (s.katalog_runu / "manifest-a.json").exists()


async def test_sciezka_ze_znakiem_nul_w_produces_konczy_krok_porazka_bez_agenta_zdarzenia_i_pliku(
    tmp_path,
):
    # `\0` w cudzyslowie YAML to NUL; consumes nie da sie tu odizolowac: walidacja workflow wymaga
    # przodka, ktory ten artefakt produkuje, a on konczy sie porazka pierwszy
    s = swiat(tmp_path, [K("a")], opcje_skilli={"a": {"produces": ['"docs/\\0plan.md"']}})
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert agent.kroki == []
    assert jedno(zdarzenia, "step_failed", "a")["text"] == (
        "pole sciezki_planu[0] zawiera znak sterujący: 'docs/\\x00plan.md'")
    assert not any(z["kind"] == "context_manifest" for z in zdarzenia)
    assert not (s.katalog_runu / "manifest-a.json").exists()
    assert rodzaje(zdarzenia)[-1] == "run_finished"


@dataclass
class SzpiegZamykajacyKatalogDocs(Szpieg):
    """Po kroku `a` odbiera prawa do `docs/`: krok `b` nie ocenia, czy jest tam CLAUDE.md."""

    katalog_kroku: object = None

    async def __call__(self, spec, polecenie, katalog, emit, **dodatkowe):
        wynik = await super().__call__(spec, polecenie, katalog, emit, **dodatkowe)
        if spec.step_id == "a":
            self.katalog_kroku = katalog
            (katalog / "docs").chmod(0)
        return wynik


@pytest.mark.skipif(os.geteuid() == 0, reason="root przechodzi przez katalog bez uprawnien")
async def test_katalog_planu_bez_praw_dostepu_konczy_krok_porazka_z_komunikatem_o_odczycie(
    tmp_path,
):
    s = swiat_z_kontekstem(tmp_path)
    agent = SzpiegZamykajacyKatalogDocs()
    try:
        zdarzenia = await uruchom(s, agent)
    finally:
        (s.worktree / "docs").chmod(0o755)

    assert agent.kroki == ["a"]
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"] == komunikat_odmowy(agent.katalog_kroku, "docs/CLAUDE.md")
    assert not any(z["kind"] == "context_manifest" and z["step_id"] == "b" for z in zdarzenia)
    assert not (s.katalog_runu / "manifest-b.json").exists()


async def test_plik_lekcji_wystepuje_w_prompcie_kroku_raz_mimo_kilku_pasujacych_lekcji(tmp_path):
    s = swiat_z_kontekstem(tmp_path)
    agent = Szpieg()

    await uruchom(s, agent)

    lekcje = [w["id"] for w in wczytaj_manifest(s, "b") if w["warstwa"] == "lekcje"]
    assert lekcje == ["L-003", "L-005"]
    (wywolanie,) = [w for w in agent.wywolania if w.spec.step_id == "b"]
    assert wywolanie.spec.kontekst.count(LEKCJE) == 1
    assert wywolanie.prompt.count(f"- {LEKCJE}") == 1


# --- zakres lekcji: blok kodu, pierwszenstwo, brak dziedziczenia ----------------------------------

FALSZYWY_ZAKRES = "zakres: krok=falszywy · ścieżki=falsz/*.md · od: 2026-01-01\n"
PRAWDZIWY_ZAKRES = "zakres: krok=prawdziwy · ścieżki=real/*.md · od: 2026-01-01\n"
PRZYKLAD_ZAKRESU = f"```markdown\n{FALSZYWY_ZAKRES}```\n"


def test_zakres_w_bloku_kodu_lekcji_nie_jest_brany_a_wygrywa_prawdziwy_pod_nim():
    blok = f"## L-001 — tytuł\n{PRZYKLAD_ZAKRESU}{PRAWDZIWY_ZAKRES}objaw: o\n"

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert (wpis.krok, wpis.sciezki) == ("prawdziwy", ("real/*.md",))
    assert waliduj_lekcje(plik_lekcji(blok)) == []


def test_lekcja_z_zakresem_tylko_w_bloku_kodu_nie_ma_zakresu_ani_w_wpisach_ani_w_walidatorze():
    blok = f"## L-001 — tytuł\n{PRZYKLAD_ZAKRESU}objaw: o\n"

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert (wpis.krok, wpis.sciezki) == ("", ())
    assert [(p.wpis, p.kod) for p in waliduj_lekcje(plik_lekcji(blok))] == [("L-001", BRAK_ZAKRESU)]


def test_przy_dwoch_zakresach_poza_blokiem_wygrywa_pierwszy():
    blok = (
        "## L-001 — tytuł\n"
        "zakres: krok=pierwszy · ścieżki=a.md · od: 2026-01-01\n"
        "zakres: krok=drugi · ścieżki=b.md · od: 2026-01-01\n"
    )

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert (wpis.krok, wpis.sciezki) == ("pierwszy", ("a.md",))


def test_zakres_poprzedniej_lekcji_nie_przechodzi_na_kolejna_lekcje_bez_zakresu():
    (pierwsza, druga) = wpisy_lekcji(
        plik_lekcji(lekcja(1, "budowa", "a.md"), "## L-002 — bez zakresu\nobjaw: o\n"))

    assert (pierwsza.krok, pierwsza.sciezki) == ("budowa", ("a.md",))
    assert (druga.id, druga.krok, druga.sciezki) == ("L-002", "", ())


@pytest.mark.parametrize("koniec_linii", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_zakres_konczacy_sie_litera_x_zachowuje_ja_w_sciezce_lekcji_przy_obu_koncach_linii(
    koniec_linii,
):
    blok = koniec_linii.join([
        "## L-001 — tytuł",
        "zakres: krok=budowa · od: 2026-01-01 · ścieżki=src/aX",
        "objaw: o",
        "",
    ])

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert (wpis.krok, wpis.sciezki) == ("budowa", ("src/aX",))


# --- glob lekcji: cala sciezka planu, zakres z samymi separatorami -----------------------------------


def test_glob_lekcji_pasuje_do_calej_sciezki_planu_a_nie_do_jej_prefiksu(tmp_path):
    blok = lekcja(1, KROK, "docs/*.md")

    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok, plan=("docs/a.md",))) == ["L-001"]
    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok, plan=("docs/a.md.bak",))) == []


def test_lekcja_z_zakresem_sciezek_z_samych_przecinkow_trafia_do_manifestu_planu_dowolnej_sciezki(
    tmp_path,
):
    blok = lekcja(1, KROK, ", ,")      # waliduje sie: `ścieżki=` ma niepusty tekst

    assert waliduj_lekcje(plik_lekcji(blok)) == []
    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok, plan=("src/x.py",))) == ["L-001"]
    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok, plan=())) == ["L-001"]


async def test_manifest_ponowionego_kroku_niesie_numer_proby_a_nie_zawsze_pierwszy(tmp_path):
    from test_petla_naprawcza import NIEUDANY, AgentNaprawczy, swiat_naprawy, uruchom_swiat

    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})

    zdarzenia = await uruchom_swiat(s, agent)

    assert [(z["kind"], z["attempt"]) for z in zdarzenia
            if z["kind"] == "context_manifest" and z["step_id"] == NIEUDANY] == [
        ("context_manifest", 1), ("context_manifest", 2)]

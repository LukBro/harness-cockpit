"""HAR-169: manifest kroku - warstwy instrukcji z sha256 i lekcje filtrowane po zakresie (TDD).

Kontrakt (brief HAR-169, decyzje Team Managera):
- `runner.kontekst`: `WpisManifestu`, `Manifest(wpisy)`, `BladManifestu(BladDziedziny)` i
  `zloz_manifest(worktree, krok, sciezki_planu, *, skill_id)`; wpisy w kolejnosci `OD_NAJWYZSZEJ`;
- `cockpit.services.lekcje.wpisy_lekcji(tekst) -> list[WpisLekcji]` (id, krok, sciezki, tekst);
- silnik zapisuje `<katalog_runu>/manifest-<step_id>.json` (lista wpisow) przed startem agenta
  i wysyla zdarzenie `context_manifest` z samymi hashami.
Testy silnika ida przez prawdziwe `runner.silnik.wykonaj_run` z `Szpiegiem` zamiast agenta
(wzor `test_silnik_runu.py`); pliki warstw leza w prawdziwym repozytorium git w tmp_path.
"""
from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from cockpit.services.errors import BladDziedziny
from cockpit.services.lekcje import wpisy_lekcji
from cockpit.services.warstwy_instrukcji import OD_NAJWYZSZEJ, WarstwaInstrukcji, pierwszenstwo
from runner.agent import SKILLE_RUNU
from runner.kontekst import BladManifestu, Manifest, WpisManifestu, zloz_manifest
from test_krok_diff import SzpiegZmieniajacyDrzewo, swiat_z_diffem, zmien_drzewo
from test_silnik_runu import K, Szpieg, _git, indeks, jedno, rodzaje, swiat, uruchom

KROK = "budowa"
SKILL = "pisanie"
LEKCJE = ".ai/lessons.md"
SKILL_SCIEZKA = f"{SKILLE_RUNU}/{SKILL}/SKILL.md"
PROJECT = ".cockpit/project.yaml"

# Tresci z wielobajtowymi znakami: rozmiar w bajtach rozni sie od liczby znakow.
TRESCI = {
    PROJECT: "stack: [python]\nopis: zażółć gęślą jaźń\n",
    "CLAUDE.md": "# korzeń\nzażółć\n",
    "a/CLAUDE.md": "# katalog a\ngęślą\n",
    "a/b/CLAUDE.md": "# katalog a/b\njaźń\n",
    SKILL_SCIEZKA: "---\nid: pisanie\n---\nbrief ąę\n",
}


def zapisz(wt: Path, sciezka: str, tresc: str | bytes) -> bytes:
    plik = wt / sciezka
    plik.parent.mkdir(parents=True, exist_ok=True)
    dane = tresc if isinstance(tresc, bytes) else tresc.encode("utf-8")
    plik.write_bytes(dane)
    return dane


def lekcja(numer: int, krok: str, sciezki: str | None = None, *, klucz: str = "ścieżki") -> str:
    zakres = f"zakres: krok={krok}"
    if sciezki is not None:
        zakres += f" · {klucz}={sciezki}"
    zakres += " · od: 2026-01-01"
    return f"## L-{numer:03d} — tytuł {numer}\n{zakres}\nobjaw: o{numer}\nprzyczyna: p{numer}\nzamiast: z{numer}\n"


def plik_lekcji(*bloki: str) -> str:
    return "# Pułapki\n\nwstęp\n\n" + "\n".join(bloki)


PRZYKLAD_L000 = (
    "## Przykład (nie jest wpisem)\n\n```markdown\n"
    f"{lekcja(0, KROK, 'a/plan.md')}```\n"
)


def pelny_worktree(wt: Path) -> dict[str, bytes]:
    """Wszystkie warstwy z plikiem, dwie lekcje pasujace do kroku `budowa` i planu `a/b/plan.md`."""
    zapisane = {sciezka: zapisz(wt, sciezka, tresc) for sciezka, tresc in TRESCI.items()}
    zapisz(wt, LEKCJE, plik_lekcji(lekcja(1, KROK, "a/b/*.md"), lekcja(2, KROK, "a/b/plan.md")))
    return zapisane


def zloz(wt: Path, sciezki=("a/b/plan.md",), krok: str = KROK) -> Manifest:
    return zloz_manifest(wt, krok, list(sciezki), skill_id=SKILL)


def wpisy_warstwy(manifest: Manifest, warstwa: WarstwaInstrukcji) -> list[WpisManifestu]:
    return [w for w in manifest.wpisy if w.warstwa == warstwa]


def id_lekcji(manifest: Manifest) -> list[str | None]:
    return [w.id for w in wpisy_warstwy(manifest, WarstwaInstrukcji.lekcje)]


# --- kolejnosc warstw -----------------------------------------------------------------


def test_warstwy_manifestu_ida_w_kolejnosci_pierwszenstwa_z_kodu_a_lekcje_sa_ostatnie(tmp_path):
    pelny_worktree(tmp_path)

    manifest = zloz(tmp_path)

    warstwy = [WarstwaInstrukcji(w.warstwa) for w in manifest.wpisy]
    assert list(dict.fromkeys(warstwy)) == list(OD_NAJWYZSZEJ)
    assert warstwy == sorted(warstwy, key=pierwszenstwo)
    assert len(manifest.wpisy) == 7      # 5 plikow warstw + 2 lekcje


def test_manifest_bez_zadnego_pliku_ma_tylko_jawny_wpis_niezmiennikow_bez_sciezki_hasha_i_rozmiaru(
    tmp_path,
):
    manifest = zloz(tmp_path)

    (wpis,) = manifest.wpisy
    assert wpis.warstwa == WarstwaInstrukcji.niezmienniki_silnika
    assert (wpis.sciezka, wpis.sha256, wpis.rozmiar, wpis.id) == (None, None, None, None)


def test_wpis_niezmiennikow_jest_pierwszy_i_jedyny_takiego_rodzaju(tmp_path):
    pelny_worktree(tmp_path)

    manifest = zloz(tmp_path)

    assert manifest.wpisy[0].warstwa == WarstwaInstrukcji.niezmienniki_silnika
    assert len(wpisy_warstwy(manifest, WarstwaInstrukcji.niezmienniki_silnika)) == 1


# --- sciezka, sha256 i rozmiar z faktycznej tresci ------------------------------------------


PLIKI_WARSTW = [
    pytest.param(WarstwaInstrukcji.project_yaml, PROJECT, id="project-yaml"),
    pytest.param(WarstwaInstrukcji.claude_md_katalogu, "a/b/CLAUDE.md", id="claude-md-katalogu"),
    pytest.param(WarstwaInstrukcji.claude_md_korzenia, "CLAUDE.md", id="claude-md-korzenia"),
    pytest.param(WarstwaInstrukcji.skill_md, SKILL_SCIEZKA, id="skill-md"),
]


@pytest.mark.parametrize(("warstwa", "sciezka"), PLIKI_WARSTW)
def test_wpis_pliku_warstwy_ma_sciezke_wzgledna_sha256_i_rozmiar_w_bajtach_faktycznej_tresci(
    tmp_path, warstwa, sciezka
):
    zapisane = pelny_worktree(tmp_path)

    (wpis,) = wpisy_warstwy(zloz(tmp_path), warstwa)

    assert wpis.sciezka == sciezka
    assert wpis.sha256 == sha256(zapisane[sciezka]).hexdigest()
    assert wpis.rozmiar == len(zapisane[sciezka])
    assert wpis.rozmiar != len(zapisane[sciezka].decode("utf-8"))     # bajty, nie znaki
    assert wpis.id is None


BAJTY_NIETEKSTOWE = [
    pytest.param(b"a\r\nb\r\n", id="konce-linii-crlf"),
    pytest.param(b"\xff\xfe\x00\x80 nie utf-8", id="bajty-spoza-utf-8"),
]


@pytest.mark.parametrize("bajty", BAJTY_NIETEKSTOWE)
def test_hash_i_rozmiar_liczone_sa_z_surowych_bajtow_pliku(tmp_path, bajty):
    zapisz(tmp_path, "CLAUDE.md", bajty)

    (wpis,) = wpisy_warstwy(zloz(tmp_path), WarstwaInstrukcji.claude_md_korzenia)

    assert wpis.sha256 == sha256(bajty).hexdigest()
    assert wpis.rozmiar == len(bajty)


def test_edycja_pliku_zmienia_hash_wpisu_a_te_same_bajty_daja_ten_sam_hash(tmp_path):
    zapisz(tmp_path, "CLAUDE.md", "wersja 1")
    (pierwszy,) = wpisy_warstwy(zloz(tmp_path), WarstwaInstrukcji.claude_md_korzenia)
    (drugi,) = wpisy_warstwy(zloz(tmp_path), WarstwaInstrukcji.claude_md_korzenia)
    zapisz(tmp_path, "CLAUDE.md", "wersja 2")
    (po_edycji,) = wpisy_warstwy(zloz(tmp_path), WarstwaInstrukcji.claude_md_korzenia)

    assert pierwszy.sha256 == drugi.sha256
    assert po_edycji.sha256 == sha256(b"wersja 2").hexdigest() != pierwszy.sha256


# --- CLAUDE.md katalogu a korzen ----------------------------------------------------------


NAJBLIZSZY_CLAUDE = [
    pytest.param(["a/b/CLAUDE.md", "a/CLAUDE.md"], ["a/b/x.md", "a/b/y.md"], "a/b/CLAUDE.md",
                 id="najblizszy-w-katalogu-planu"),
    pytest.param(["a/b/CLAUDE.md", "a/CLAUDE.md"], ["a/b/x.md", "a/c/y.md"], "a/CLAUDE.md",
                 id="wspolny-katalog-rozbieznych-sciezek"),
    pytest.param(["a/CLAUDE.md"], ["a/b/c/x.md"], "a/CLAUDE.md", id="w-gore-od-glebokiej-sciezki"),
    pytest.param(["a/CLAUDE.md"], ["a/y.md", "x.md"], None, id="wspolnym-katalogiem-jest-korzen"),
    pytest.param(["a/c/CLAUDE.md"], ["a/b/x.md"], None, id="claude-md-poza-droga-w-gore"),
    pytest.param(["a/CLAUDE.md"], [], None, id="pusty-plan"),
    pytest.param([], ["a/b/x.md"], None, id="brak-claude-md-katalogu"),
]


@pytest.mark.parametrize(("pliki", "plan", "oczekiwany"), NAJBLIZSZY_CLAUDE)
def test_claude_md_katalogu_to_najblizszy_plik_w_gore_od_wspolnego_katalogu_sciezek_planu(
    tmp_path, pliki, plan, oczekiwany
):
    zapisz(tmp_path, "CLAUDE.md", TRESCI["CLAUDE.md"])
    for sciezka in pliki:
        zapisz(tmp_path, sciezka, TRESCI.get(sciezka, f"# {sciezka}\n"))

    manifest = zloz(tmp_path, plan)

    katalogu = wpisy_warstwy(manifest, WarstwaInstrukcji.claude_md_katalogu)
    assert [w.sciezka for w in katalogu] == ([oczekiwany] if oczekiwany else [])
    # korzen zostaje osobnym wpisem, nigdy nie jest podany drugi raz jako katalog
    assert [w.sciezka for w in wpisy_warstwy(manifest, WarstwaInstrukcji.claude_md_korzenia)] == [
        "CLAUDE.md"]


# --- brak i nieczytelnosc pliku warstwy ---------------------------------------------------


PLIKI_Z_LEKCJAMI = [
    *PLIKI_WARSTW,
    pytest.param(WarstwaInstrukcji.lekcje, LEKCJE, id="lekcje"),
]


@pytest.mark.parametrize(("warstwa", "sciezka"), PLIKI_Z_LEKCJAMI)
def test_brak_pliku_warstwy_pomija_jej_wpis_a_reszta_warstw_zostaje(tmp_path, warstwa, sciezka):
    pelny_worktree(tmp_path)
    (tmp_path / sciezka).unlink()
    if warstwa == WarstwaInstrukcji.claude_md_katalogu:
        (tmp_path / "a/CLAUDE.md").unlink()     # bez tego najblizszym bylby plik wyzej

    manifest = zloz(tmp_path)

    obecne = {WarstwaInstrukcji(w.warstwa) for w in manifest.wpisy}
    assert obecne == set(OD_NAJWYZSZEJ) - {warstwa}


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez uprawnien")
@pytest.mark.parametrize(("warstwa", "sciezka"), PLIKI_Z_LEKCJAMI)
def test_nieczytelny_plik_warstwy_to_blad_dziedziny_z_jego_sciezka_a_nie_pusty_wpis(
    tmp_path, warstwa, sciezka
):
    pelny_worktree(tmp_path)
    plik = tmp_path / sciezka
    plik.chmod(0)
    try:
        with pytest.raises(BladManifestu) as wyjatek:
            zloz(tmp_path)
    finally:
        plik.chmod(0o600)

    assert type(wyjatek.value) is BladManifestu
    assert isinstance(wyjatek.value, BladDziedziny)
    assert sciezka in str(wyjatek.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez uprawnien")
def test_nieczytelny_plik_lekcji_nie_jest_pomijany_gdy_zadna_lekcja_nie_pasuje(tmp_path):
    zapisz(tmp_path, LEKCJE, plik_lekcji(lekcja(1, "inny-krok", "a/b/plan.md")))
    plik = tmp_path / LEKCJE
    plik.chmod(0)
    try:
        with pytest.raises(BladManifestu):
            zloz(tmp_path)
    finally:
        plik.chmod(0o600)


# --- lekcje filtrowane po zakresie ------------------------------------------------------------


def manifest_z_lekcjami(tmp_path: Path, *bloki: str, plan=("a/b/plan.md",), krok: str = KROK):
    zapisz(tmp_path, LEKCJE, plik_lekcji(*bloki))
    return zloz(tmp_path, plan, krok)


def test_lekcja_pasujaca_do_kroku_i_sciezek_planu_trafia_do_manifestu_z_id_sciezka_i_hashem_bloku(
    tmp_path,
):
    manifest = manifest_z_lekcjami(tmp_path, lekcja(7, KROK, "a/b/plan.md"))

    (wpis,) = wpisy_warstwy(manifest, WarstwaInstrukcji.lekcje)
    (blok,) = wpisy_lekcji(plik_lekcji(lekcja(7, KROK, "a/b/plan.md")))
    assert (wpis.id, wpis.sciezka) == ("L-007", LEKCJE)
    assert wpis.sha256 == sha256(blok.tekst.encode("utf-8")).hexdigest()
    assert wpis.rozmiar == len(blok.tekst.encode("utf-8"))


def test_lekcja_z_innym_krokiem_nie_trafia_do_manifestu_mimo_pasujacych_sciezek(tmp_path):
    manifest = manifest_z_lekcjami(tmp_path, lekcja(1, "inny-krok", "a/b/plan.md"))

    assert id_lekcji(manifest) == []


def test_krok_lekcji_jest_porownywany_z_id_kroku_workflow_a_nie_z_id_skilla(tmp_path):
    manifest = manifest_z_lekcjami(tmp_path, lekcja(1, SKILL, "a/b/plan.md"))

    assert id_lekcji(manifest) == []


def test_lekcja_ze_sciezkami_niepasujacymi_do_planu_nie_trafia_do_manifestu_mimo_pasujacego_kroku(
    tmp_path,
):
    manifest = manifest_z_lekcjami(tmp_path, lekcja(1, KROK, "web/*.ts"))

    assert id_lekcji(manifest) == []


def test_lekcja_bez_sciezek_trafia_do_manifestu_kroku_zawsze_takze_przy_pustym_planie(tmp_path):
    blok = lekcja(3, KROK)    # zakres bez `ścieżki=`

    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok)) == ["L-003"]
    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok, plan=())) == ["L-003"]


def test_lekcja_ze_sciezkami_nie_trafia_do_manifestu_kroku_bez_planu(tmp_path):
    manifest = manifest_z_lekcjami(tmp_path, lekcja(1, KROK, "a/b/plan.md"), plan=())

    assert id_lekcji(manifest) == []


def test_wystarczy_jedna_sciezka_planu_pasujaca_do_globu_lekcji(tmp_path):
    manifest = manifest_z_lekcjami(
        tmp_path, lekcja(1, KROK, "src/*.py"), plan=("plan.md", "src/a.py", "docs/b.md"))

    assert id_lekcji(manifest) == ["L-001"]


def test_glob_z_gwiazdkami_dopasowuje_zagniezdzone_sciezki_planu(tmp_path):
    pasuje = manifest_z_lekcjami(
        tmp_path, lekcja(1, KROK, "tests/**/*.py"), plan=("tests/unit/test_x.py",))
    nie_pasuje = manifest_z_lekcjami(
        tmp_path, lekcja(1, KROK, "tests/**/*.py"), plan=("src/test_x.py",))

    assert id_lekcji(pasuje) == ["L-001"]
    assert id_lekcji(nie_pasuje) == []


def test_z_czterech_lekcji_roznych_jednym_warunkiem_trafia_tylko_ta_spelniajaca_oba(tmp_path):
    manifest = manifest_z_lekcjami(
        tmp_path,
        lekcja(1, KROK, "a/b/plan.md"),            # krok i sciezka pasuja
        lekcja(2, KROK, "web/*.ts"),               # tylko krok
        lekcja(3, "inny-krok", "a/b/plan.md"),     # tylko sciezka
        lekcja(4, "inny-krok", "web/*.ts"),        # zaden
    )

    assert id_lekcji(manifest) == ["L-001"]


def test_lekcje_w_manifescie_zachowuja_kolejnosc_z_pliku(tmp_path):
    manifest = manifest_z_lekcjami(
        tmp_path, lekcja(5, KROK, "a/b/plan.md"), lekcja(2, KROK), lekcja(9, KROK, "a/b/*.md"))

    assert id_lekcji(manifest) == ["L-005", "L-002", "L-009"]


def test_klucz_sciezki_w_innej_normalizacji_unicode_nadal_zaweza_lekcje(tmp_path):
    klucz_nfd = unicodedata.normalize("NFD", "ścieżki")
    assert klucz_nfd != "ścieżki"
    blok = lekcja(1, KROK, "web/*.ts", klucz=klucz_nfd)

    assert id_lekcji(manifest_z_lekcjami(tmp_path, blok)) == []


def test_przyklad_lekcji_w_bloku_kodu_nie_jest_wpisem_a_lekcja_po_nim_tak(tmp_path):
    manifest = manifest_z_lekcjami(tmp_path, PRZYKLAD_L000, lekcja(1, KROK, "a/b/plan.md"))

    assert id_lekcji(manifest) == ["L-001"]


def test_hash_bloku_lekcji_zalezy_od_jej_tresci_a_nie_od_sasiednich_lekcji(tmp_path):
    pierwsza = lekcja(1, KROK, "a/b/plan.md")
    druga = lekcja(2, KROK, "a/b/plan.md")
    manifest = manifest_z_lekcjami(tmp_path, pierwsza, druga)

    w1, w2 = wpisy_warstwy(manifest, WarstwaInstrukcji.lekcje)
    assert w1.sha256 != w2.sha256
    (blok1, blok2) = wpisy_lekcji(plik_lekcji(pierwsza, druga))
    assert w1.sha256 == sha256(blok1.tekst.encode("utf-8")).hexdigest()
    assert w2.sha256 == sha256(blok2.tekst.encode("utf-8")).hexdigest()


# --- publiczna funkcja lekcji ---------------------------------------------------------------


def test_wpisy_lekcji_zwracaja_id_krok_sciezki_i_tekst_bloku_kazdej_lekcji_w_kolejnosci_pliku():
    pierwsza, druga = lekcja(1, "mutacje", "src/*.py"), lekcja(2, "budowa", "tests/**/*.py")

    wpisy = wpisy_lekcji(plik_lekcji(pierwsza, druga))

    assert [(w.id, w.krok, tuple(w.sciezki)) for w in wpisy] == [
        ("L-001", "mutacje", ("src/*.py",)),
        ("L-002", "budowa", ("tests/**/*.py",)),
    ]
    assert wpisy[0].tekst.strip() == pierwsza.strip()
    assert wpisy[1].tekst.strip() == druga.strip()


def test_wpis_lekcji_bez_sciezek_ma_pusty_zbior_sciezek():
    (wpis,) = wpisy_lekcji(plik_lekcji(lekcja(1, "budowa")))

    assert (wpis.id, wpis.krok, tuple(wpis.sciezki)) == ("L-001", "budowa", ())


@pytest.mark.parametrize("plotek", ["```", "~~~"])
def test_przyklad_w_bloku_kodu_nie_jest_wpisem_lekcji(plotek):
    przyklad = f"{plotek}markdown\n{lekcja(0, 'budowa', 'a.md')}{plotek}\n"

    wpisy = wpisy_lekcji(plik_lekcji(przyklad, lekcja(1, "budowa", "a.md")))

    assert [w.id for w in wpisy] == ["L-001"]


def test_klucz_sciezki_w_rozlozonej_normalizacji_unicode_jest_rozpoznany():
    blok = lekcja(1, "budowa", "src/*.py", klucz=unicodedata.normalize("NFD", "ścieżki"))

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert tuple(wpis.sciezki) == ("src/*.py",)


def test_tekst_bloku_lekcji_konczy_sie_przed_naglowkiem_nastepnej_sekcji():
    tekst = plik_lekcji(lekcja(1, "budowa", "a.md"), "## Inna sekcja\nnie ta lekcja\n")

    (wpis,) = wpisy_lekcji(tekst)

    assert "objaw: o1" in wpis.tekst
    assert "nie ta lekcja" not in wpis.tekst


def test_plik_bez_lekcji_nie_ma_wpisow():
    assert wpisy_lekcji("# Pułapki\n\nPusty na starcie.\n") == []


# --- silnik: manifest przed krokiem, w katalogu runu, w zdarzeniu --------------------------------

MARKERY = {
    "CLAUDE.md": "ZNACZNIK-KORZEN-4417",
    "docs/CLAUDE.md": "ZNACZNIK-KATALOG-5528",
    LEKCJE: "ZNACZNIK-LEKCJA-6639",
}


def dopisz_do_repo(s, pliki: dict[str, str]) -> None:
    for sciezka, tresc in pliki.items():
        plik = s.repo / sciezka
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    _git(s.repo, "add", "-A")
    _git(s.repo, "commit", "-q", "-m", "kontekst")


LEKCJE_SILNIKA = plik_lekcji(
    lekcja(1, "a", "docs/plan.md").replace("objaw: o1", f"objaw: {MARKERY[LEKCJE]}"),
    lekcja(2, "a", "obce/*.md"),
    lekcja(3, "b", "docs/plan.md"),
    lekcja(4, "b", "obce/*.md"),
    lekcja(5, "b", "docs/wynik.md"),
    lekcja(6, "inny", "docs/plan.md"),
)


def swiat_z_kontekstem(tmp_path: Path):
    s = swiat(
        tmp_path, [K("a"), K("b", ("a",))],
        opcje_skilli={
            "a": {"produces": ["docs/plan.md"]},
            "b": {"consumes": ["docs/plan.md"], "produces": ["docs/wynik.md"]},
        })
    dopisz_do_repo(s, {
        "CLAUDE.md": f"# korzeń {MARKERY['CLAUDE.md']}\n",
        "docs/CLAUDE.md": f"# docs {MARKERY['docs/CLAUDE.md']}\n",
        LEKCJE: LEKCJE_SILNIKA,
    })
    return s


def wczytaj_manifest(s, krok: str) -> list[dict]:
    return json.loads((s.katalog_runu / f"manifest-{krok}.json").read_text(encoding="utf-8"))


async def test_kazdy_krok_ze_skillem_ma_manifest_w_katalogu_runu_poza_repozytorium(tmp_path):
    s = swiat_z_kontekstem(tmp_path)

    await uruchom(s, Szpieg())

    for krok in ("a", "b"):
        plik = s.katalog_runu / f"manifest-{krok}.json"
        assert plik.is_file()
        assert not plik.is_relative_to(s.repo)
        assert not plik.is_relative_to(s.worktree)
        assert not (s.worktree / f"manifest-{krok}.json").exists()


async def test_manifest_kroku_ma_warstwy_w_kolejnosci_pierwszenstwa_i_sha256_kazdego_pliku(tmp_path):
    s = swiat_z_kontekstem(tmp_path)

    await uruchom(s, Szpieg())

    wpisy = wczytaj_manifest(s, "a")
    warstwy = [WarstwaInstrukcji(w["warstwa"]) for w in wpisy]
    assert list(dict.fromkeys(warstwy)) == list(OD_NAJWYZSZEJ)
    assert warstwy == sorted(warstwy, key=pierwszenstwo)
    for wpis in wpisy:
        assert set(wpis) == {"warstwa", "sciezka", "sha256", "rozmiar", "id"}
    pliki = [w for w in wpisy if w["warstwa"] != WarstwaInstrukcji.lekcje
             and w["sciezka"] is not None]
    assert len(pliki) == 4
    for wpis in pliki:
        bajty = (s.worktree / wpis["sciezka"]).read_bytes()
        assert wpis["sha256"] == sha256(bajty).hexdigest()
        assert wpis["rozmiar"] == len(bajty)
    niezmienniki = wpisy[0]
    assert niezmienniki["warstwa"] == WarstwaInstrukcji.niezmienniki_silnika
    assert (niezmienniki["sciezka"], niezmienniki["sha256"], niezmienniki["rozmiar"]) == (
        None, None, None)


async def test_lekcje_w_manifescie_kroku_zaleza_od_kroku_oraz_od_produces_i_consumes_skilla(tmp_path):
    s = swiat_z_kontekstem(tmp_path)

    await uruchom(s, Szpieg())

    def lekcje(krok: str) -> list[str | None]:
        return [w["id"] for w in wczytaj_manifest(s, krok) if w["warstwa"] == "lekcje"]

    assert lekcje("a") == ["L-001"]                  # produces kroku a
    assert lekcje("b") == ["L-003", "L-005"]         # consumes i produces kroku b


async def test_zdarzenie_context_manifest_niesie_te_same_wpisy_co_plik_i_idzie_po_starcie_kroku(
    tmp_path,
):
    s = swiat_z_kontekstem(tmp_path)

    zdarzenia = await uruchom(s, Szpieg())

    zdarzenie = jedno(zdarzenia, "context_manifest", "a")
    assert zdarzenie["manifest"] == wczytaj_manifest(s, "a")
    assert (zdarzenie["run_id"], zdarzenie["agent_id"], zdarzenie["attempt"]) == ("run-80", "a", 1)
    assert indeks(zdarzenia, "step_started", "a") < indeks(zdarzenia, "context_manifest", "a")
    assert indeks(zdarzenia, "context_manifest", "a") < indeks(zdarzenia, "step_finished", "a")


async def test_zdarzenie_context_manifest_nie_zawiera_tresci_plikow(tmp_path):
    s = swiat_z_kontekstem(tmp_path)

    zdarzenia = await uruchom(s, Szpieg())

    for krok in ("a", "b"):
        surowe = json.dumps(jedno(zdarzenia, "context_manifest", krok), ensure_ascii=False)
        for znacznik in MARKERY.values():
            assert znacznik not in surowe
    skopiowany = (s.worktree / SKILLE_RUNU / "s_a" / "SKILL.md").read_text(encoding="utf-8")
    assert skopiowany.strip() not in json.dumps(
        jedno(zdarzenia, "context_manifest", "a"), ensure_ascii=False)


async def test_prompt_kroku_wymienia_sciezki_manifestu_w_jego_kolejnosci_bez_tresci_plikow(tmp_path):
    s = swiat_z_kontekstem(tmp_path)
    agent = Szpieg()

    await uruchom(s, agent)

    sciezki = [w["sciezka"] for w in wczytaj_manifest(s, "a") if w["sciezka"] is not None]
    assert sciezki[0] == PROJECT and sciezki[-1] == LEKCJE
    (wywolanie,) = [w for w in agent.wywolania if w.spec.step_id == "a"]
    assert wywolanie.spec.kontekst == tuple(sciezki)
    sekcja = "kontekst (w kolejności pierwszeństwa):\n" + "\n".join(f"- {p}" for p in sciezki)
    assert sekcja in wywolanie.prompt
    for znacznik in MARKERY.values():
        assert znacznik not in wywolanie.prompt


@dataclass
class SzpiegEdytujacySkill(Szpieg):
    """Przy pierwszym kroku zapisuje hash kopii SKILL.md i ja nadpisuje (edycja w UI w trakcie runu)."""

    odczytane: dict[str, str] | None = None

    async def __call__(self, spec, polecenie, katalog, emit, **dodatkowe):
        if spec.step_id == "a":
            kopia = katalog / SKILLE_RUNU / spec.skill_id / "SKILL.md"
            self.odczytane = {"sha256": sha256(kopia.read_bytes()).hexdigest()}
            kopia.write_text("EDYCJA PO ZLOZENIU MANIFESTU\n", encoding="utf-8")
        return await super().__call__(spec, polecenie, katalog, emit, **dodatkowe)


async def test_edycja_skilla_po_zlozeniu_manifestu_nie_zmienia_hasha_zapisanego_w_pliku(tmp_path):
    s = swiat_z_kontekstem(tmp_path)
    agent = SzpiegEdytujacySkill()

    await uruchom(s, agent)

    (skill,) = [w for w in wczytaj_manifest(s, "a") if w["warstwa"] == "skill_md"]
    po_edycji = sha256(b"EDYCJA PO ZLOZENIU MANIFESTU\n").hexdigest()
    assert skill["sha256"] == agent.odczytane["sha256"]
    assert skill["sha256"] != po_edycji


async def test_krok_systemowy_nie_ma_manifestu_a_kroki_ze_skillem_wokol_niego_maja(tmp_path):
    s = swiat_z_diffem(tmp_path)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo}))

    assert (s.katalog_runu / "manifest-autor.json").is_file()
    assert (s.katalog_runu / "manifest-rec.json").is_file()
    assert not (s.katalog_runu / "manifest-diff.json").exists()
    assert sorted(z["step_id"] for z in zdarzenia if z["kind"] == "context_manifest") == [
        "autor", "rec"]


@dataclass
class SzpiegPsujacyClaudeMd(Szpieg):
    """Po kroku `a` odbiera prawo odczytu CLAUDE.md w worktree: krok `b` nie zlozy manifestu."""

    async def __call__(self, spec, polecenie, katalog, emit, **dodatkowe):
        wynik = await super().__call__(spec, polecenie, katalog, emit, **dodatkowe)
        if spec.step_id == "a":
            (katalog / "CLAUDE.md").chmod(0)
        return wynik


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez uprawnien")
async def test_nieczytelny_plik_warstwy_konczy_krok_porazka_bez_startu_agenta_i_bez_manifestu(
    tmp_path,
):
    s = swiat_z_kontekstem(tmp_path)
    agent = SzpiegPsujacyClaudeMd()

    zdarzenia = await uruchom(s, agent)

    assert agent.kroki == ["a"]
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert "CLAUDE.md" in porazka["text"]
    assert not any(z["kind"] == "context_manifest" and z["step_id"] == "b" for z in zdarzenia)
    assert not (s.katalog_runu / "manifest-b.json").exists()
    assert rodzaje(zdarzenia)[-1] == "run_finished"

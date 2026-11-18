"""Nazwy testow w jezyku dziedziny, nie technologii (BRO-95).

Skill jako byt w kodzie: odczyt i zapis SKILL.md, rejestr (lokalny przeslania
globalny) i materializacja wybranego zestawu do katalogu runu (HAR-5).
Wszystko na tmp_path, bez PostgreSQL. Tier i prawo zapisu to HAR-6, tu ich nie ma.
"""
import ast
import shutil
import subprocess
from pathlib import Path

import pytest

from cockpit.services.errors import BladDziedziny, BlednySkill, NieZnaleziono
from cockpit.services.invariants import Rola
from cockpit.services.skills import (
    Rejestr,
    Skill,
    materializuj,
    podziel_skill,
    rozstrzygnij_rejestr,
    wczytaj_skill,
    zapisz_skill,
)
from cockpit.services.walidacja import waliduj_definicje

KORZEN_PROJEKTU = Path(__file__).resolve().parents[1]


def korzen_repozytorium() -> Path:
    """Korzeń repozytorium git, także gdy test leży w kopii `mutants/` (mutmut
    nie kopiuje `.gitignore`, więc `parents[1]` wskazywałoby katalog bez niego)."""
    wynik = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(wynik.stdout.strip())


# --- pomocnicze ---------------------------------------------------------


def napisz_skill_kokpitu(
    zrodlo: Path,
    id: str,
    *,
    role: str | None = "author",
    name: str | None = None,
    brief: str = "Tresc briefu.\n",
    frontmatter_id: str | None = "__domyslnie__",
    pomocnicze: dict[str, str] | None = None,
) -> Path:
    """Tworzy <zrodlo>/<id>/SKILL.md w formacie kokpitu; zwraca plik SKILL.md."""
    katalog = zrodlo / id
    katalog.mkdir(parents=True, exist_ok=True)
    linie = []
    if frontmatter_id == "__domyslnie__":
        frontmatter_id = id
    if frontmatter_id is not None:
        linie.append(f"id: {frontmatter_id}")
    linie.append(f"name: {name if name is not None else id}")
    if role is not None:
        linie.append(f"role: {role}")
    plik = katalog / "SKILL.md"
    plik.write_text("---\n" + "\n".join(linie) + "\n---\n" + brief, encoding="utf-8")
    for nazwa, tresc in (pomocnicze or {}).items():
        cel = katalog / nazwa
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")
    return plik


def napisz_skill_konwencyjny(
    zrodlo: Path, id: str, *, extra: str = "", brief: str = "Opis dla Claude Code.\n"
) -> Path:
    """Skill w formacie innych narzedzi: tylko name i description (+ ewentualnie extra)."""
    katalog = zrodlo / id
    katalog.mkdir(parents=True, exist_ok=True)
    plik = katalog / "SKILL.md"
    plik.write_text(
        f"---\nname: {id}\ndescription: Zwykly skill\n{extra}---\n{brief}",
        encoding="utf-8",
    )
    return plik


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


@pytest.fixture
def dom(tmp_path: Path) -> Path:
    d = tmp_path / "dom"
    d.mkdir()
    return d


def drzewo(korzen: Path) -> dict[str, bytes]:
    """Zawartosc katalogu jako {sciezka wzgledna: bajty} - do porownan skutku w danych."""
    return {
        str(p.relative_to(korzen)): p.read_bytes()
        for p in sorted(korzen.rglob("*"))
        if p.is_file()
    }


# --- Rola ----------------------------------------------------------------


def test_lista_rol_ma_dokladnie_cztery_pozycje():
    assert {r.value for r in Rola} == {"author", "reviewer", "executor", "gatherer"}
    assert len(list(Rola)) == 4


# --- odczyt i zapis -------------------------------------------------------


def test_odczyt_i_zapis_skilla_nie_zmienia_formatowania_briefu(tmp_path: Path):
    katalog = tmp_path / "skills" / "recenzja"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    oryginal = (
        "---\n"
        "# komentarz otwierajacy frontmatter\n"
        'id:   "recenzja"   # komentarz na koncu linii\n'
        "name: 'Recenzja kodu'\n"
        "role: reviewer\n"
        "consumes:\n"
        "- diff\n"
        "  \n"
        "produces: [raport]\n"
        "---\n"
        "\n"
        "Pierwsza linia briefu ze spacjami na koncu   \n"
        "\n"
        "\n"
        "\n"
        "    Wciety blok kodu\n"
        "Ostatnia linia\n"
        "\n"
    )
    plik.write_bytes(oryginal.encode("utf-8"))
    przed = plik.read_bytes()

    skill = wczytaj_skill(plik)
    zapisz_skill(skill, plik)

    assert plik.read_bytes() == przed


def test_wczytany_skill_niesie_pola_brief_i_katalog(tmp_path: Path):
    plik = napisz_skill_kokpitu(
        tmp_path / "skills",
        "analiza",
        role="gatherer",
        name="Analiza",
        brief="\nBrief z pusta linia na poczatku.\n",
    )
    plik.write_text(
        plik.read_text(encoding="utf-8").replace(
            "role: gatherer\n", "role: gatherer\nconsumes: [a]\nproduces: [b]\nneeds: [d]\n"
        ),
        encoding="utf-8",
    )

    skill = wczytaj_skill(plik)

    assert isinstance(skill, Skill)
    assert skill.id == "analiza"
    assert skill.name == "Analiza"
    assert skill.role == Rola.gatherer
    assert skill.consumes == ["a"]
    assert skill.produces == ["b"]
    assert skill.needs == ["d"]
    assert skill.brief == "\nBrief z pusta linia na poczatku.\n"
    assert skill.katalog == plik.parent


def test_zmiana_pola_zachowuje_komentarze_i_kolejnosc_kluczy_frontmatteru(tmp_path: Path):
    katalog = tmp_path / "skills" / "recenzja"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    brief = (
        "\n"
        "Brief ze spacjami na koncu   \n"
        "\n"
        "\n"
        "Ostatnia linia\n"
        "\n"
    )
    plik.write_bytes(
        (
            "---\n"
            "# naglowek frontmatteru\n"
            "id: recenzja\n"
            "description: 'Opis nieznany modelowi'   # komentarz przy opisie\n"
            'name: "Stara nazwa"\n'
            "# komentarz przed rola\n"
            "role: reviewer\n"
            "consumes:\n"
            "  - diff\n"
            "---\n" + brief
        ).encode("utf-8")
    )

    skill = wczytaj_skill(plik)
    skill.name = "nowa"
    zapisz_skill(skill, plik)

    tekst = plik.read_text(encoding="utf-8")
    # komentarze przetrwaly
    assert "# naglowek frontmatteru" in tekst
    assert "# komentarz przy opisie" in tekst
    assert "# komentarz przed rola" in tekst
    # kolejnosc kluczy, wraz z kluczem nieznanym modelowi, zachowana
    pozycje = [tekst.index(k) for k in ("id:", "description:", "name:", "role:", "consumes:")]
    assert pozycje == sorted(pozycje)
    # cudzyslow wokol niezmienionego pola przetrwal
    assert "'Opis nieznany modelowi'" in tekst
    # brief bajt w bajt
    assert tekst.endswith("---\n" + brief)
    # nowa wartosc, stara znikla
    ponownie = wczytaj_skill(plik)
    assert ponownie.name == "nowa"
    assert "Stara nazwa" not in tekst
    assert ponownie.role == Rola.reviewer
    assert ponownie.brief == brief


def test_zmiana_jednego_pola_nie_rusza_zapisu_pozostalych_pol(tmp_path: Path):
    katalog = tmp_path / "skills" / "abc"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text(
        '---\nid: "abc"\nname: stara\nrole: author\n'
        "consumes:\n  - a   # komentarz przy a\n  - b\n---\nBrief\n",
        encoding="utf-8",
    )

    skill = wczytaj_skill(plik)
    skill.name = "nowa"
    zapisz_skill(skill, plik)

    tekst = plik.read_text(encoding="utf-8")
    assert 'id: "abc"' in tekst
    assert "# komentarz przy a" in tekst
    assert "name: nowa" in tekst
    assert tekst.endswith("---\nBrief\n")


def test_lista_we_frontmatterze_po_zmianie_pola_ma_kanoniczne_wciecie(tmp_path: Path):
    katalog = tmp_path / "skills" / "abc"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text(
        "---\nid: abc\nname: stara\nrole: author\nconsumes:\n- a\n- b\n---\nBrief\n",
        encoding="utf-8",
    )

    skill = wczytaj_skill(plik)
    skill.name = "nowa"
    zapisz_skill(skill, plik)

    assert "consumes:\n  - a\n  - b\n" in plik.read_text(encoding="utf-8")


# --- bledy pojedynczego skilla -------------------------------------------------


def test_niezamkniety_frontmatter_jest_bledem_ktory_nazywa_plik(tmp_path: Path):
    katalog = tmp_path / "skills" / "x"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("---\nid: x\nname: x\nrole: author\nBrief bez zamkniecia\n", encoding="utf-8")

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    assert str(plik) in str(wyjatek.value)


def test_niepoprawny_yaml_we_frontmatterze_jest_bledem_ktory_nazywa_plik(tmp_path: Path):
    katalog = tmp_path / "skills" / "x"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("---\nid: [x\nname: x\n---\nBrief\n", encoding="utf-8")

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    assert str(plik) in str(wyjatek.value)


def test_frontmatter_bedacy_lista_zamiast_mapy_jest_bledem_ktory_nazywa_plik(
    tmp_path: Path,
):
    katalog = tmp_path / "skills" / "x"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("---\n- a\n- b\n---\nBrief\n", encoding="utf-8")

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    assert str(plik) in str(wyjatek.value)


def test_blad_id_innego_niz_katalog_nazywa_oba_identyfikatory_i_plik(tmp_path: Path):
    plik = napisz_skill_kokpitu(tmp_path / "skills", "katalog", frontmatter_id="inny")

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    komunikat = str(wyjatek.value)
    assert "inny" in komunikat
    assert "katalog" in komunikat
    assert str(plik) in komunikat


def test_skill_z_nieznana_rola_jest_bledem_ktory_nazywa_role_i_plik(tmp_path: Path):
    plik = napisz_skill_kokpitu(tmp_path / "skills", "x", role="planner")

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    komunikat = str(wyjatek.value)
    assert "planner" in komunikat
    assert str(plik) in komunikat
    assert isinstance(wyjatek.value, BladDziedziny)
    for dozwolona in ("author", "reviewer", "executor", "gatherer"):
        assert dozwolona in komunikat


@pytest.mark.parametrize("brakujace", ["id", "name", "role"])
def test_skill_kokpitu_bez_wymaganego_pola_jest_bledem_ktory_nazywa_pole_i_plik(
    tmp_path: Path, brakujace: str
):
    plik = napisz_skill_kokpitu(
        tmp_path / "skills",
        "x",
        role=None if brakujace == "role" else "author",
        frontmatter_id=None if brakujace == "id" else "x",
    )
    if brakujace == "name":
        plik.write_text(
            plik.read_text(encoding="utf-8").replace("name: x\n", ""), encoding="utf-8"
        )

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    assert brakujace in str(wyjatek.value)
    assert str(plik) in str(wyjatek.value)


@pytest.mark.parametrize(
    ("klucz", "wartosc"),
    [("consumes", "abc"), ("produces", "abc"), ("needs", "abc"), ("name", "[a]")],
)
def test_pole_skilla_kokpitu_o_zlym_ksztalcie_jest_bledem_ktory_nazywa_plik(
    tmp_path: Path, klucz: str, wartosc: str
):
    katalog = tmp_path / "skills" / "x"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    pola = {"id": "x", "name": "x", "role": "author"}
    pola[klucz] = wartosc
    tresc = "".join(f"{k}: {v}\n" for k, v in pola.items())
    plik.write_text(f"---\n{tresc}---\nBrief\n", encoding="utf-8")

    with pytest.raises(BlednySkill) as wyjatek:
        wczytaj_skill(plik)

    assert str(plik) in str(wyjatek.value)


# --- rejestr ----------------------------------------------------------------


def test_lokalny_skill_o_tym_samym_id_przeslania_globalny(repo: Path, dom: Path):
    napisz_skill_kokpitu(
        dom / ".cockpit" / "skills", "recenzja", name="globalna", role="reviewer"
    )
    napisz_skill_kokpitu(
        repo / ".cockpit" / "skills", "recenzja", name="lokalna", role="reviewer"
    )

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert isinstance(rejestr, Rejestr)
    assert list(rejestr.skille) == ["recenzja"]
    wygrany = rejestr.skille["recenzja"]
    assert wygrany.name == "lokalna"
    assert wygrany.katalog == repo / ".cockpit" / "skills" / "recenzja"


def test_skille_z_roznych_zrodel_trafiaja_do_jednego_rejestru(repo: Path, dom: Path):
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "a", role="author")
    napisz_skill_kokpitu(dom / ".cockpit" / "skills", "b", role="executor")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"a", "b"}
    assert rejestr.pominiete == []


def test_kolejnosc_przeslaniania_to_kokpit_potem_claude_potem_agents_potem_dom(
    repo: Path, dom: Path
):
    """Priorytet: <repo>/.cockpit > <repo>/.claude > <repo>/.agents > <dom>/.cockpit."""
    def konwencyjny(zrodlo: Path, opis: str):
        napisz_skill_konwencyjny(
            zrodlo, "s", extra="role: author\n", brief=f"{opis}\n"
        )

    napisz_skill_kokpitu(dom / ".cockpit" / "skills", "s", brief="dom\n")
    konwencyjny(repo / ".agents" / "skills", "agents")
    assert rozstrzygnij_rejestr(repo, dom).skille["s"].brief == "agents\n"

    konwencyjny(repo / ".claude" / "skills", "claude")
    assert rozstrzygnij_rejestr(repo, dom).skille["s"].brief == "claude\n"

    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "s", brief="lokalny\n")
    assert rozstrzygnij_rejestr(repo, dom).skille["s"].brief == "lokalny\n"


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_zwykly_skill_z_katalogu_claude_bez_roli_jest_pominiety_i_zgloszony(
    repo: Path, dom: Path, zrodlo: str
):
    plik = napisz_skill_konwencyjny(repo / zrodlo / "skills", "zwykly")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert "zwykly" not in rejestr.skille
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert rejestr.pominiete[0].powod.strip() != ""


def test_pominiecie_zwyklego_skilla_nie_blokuje_skilli_kokpitu(repo: Path, dom: Path):
    napisz_skill_konwencyjny(repo / ".claude" / "skills", "zwykly")
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "nasz", role="executor")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"nasz"}
    assert len(rejestr.pominiete) == 1


def test_skill_konwencyjny_z_rola_trafia_do_rejestru_z_id_z_nazwy_katalogu(
    repo: Path, dom: Path
):
    napisz_skill_konwencyjny(repo / ".claude" / "skills", "wlasny", extra="role: gatherer\n")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille["wlasny"].id == "wlasny"
    assert rejestr.skille["wlasny"].role == Rola.gatherer
    assert rejestr.pominiete == []


def test_skill_konwencyjny_z_nieznana_rola_jest_bledem(repo: Path, dom: Path):
    plik = napisz_skill_konwencyjny(
        repo / ".claude" / "skills", "x", extra="role: planner\n"
    )

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert "planner" in str(wyjatek.value)
    assert str(plik) in str(wyjatek.value)


def test_skill_konwencyjny_z_id_innym_niz_katalog_jest_bledem(repo: Path, dom: Path):
    napisz_skill_konwencyjny(
        repo / ".agents" / "skills", "katalog", extra="id: inny\nrole: author\n"
    )

    with pytest.raises(BlednySkill):
        rozstrzygnij_rejestr(repo, dom)


def test_przeslaniety_skill_kokpitu_z_bledna_rola_nadal_jest_bledem(repo: Path, dom: Path):
    napisz_skill_kokpitu(dom / ".cockpit" / "skills", "x", role="planner")
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "x", role="author")

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert "planner" in str(wyjatek.value)


def test_globalny_skill_kokpitu_bez_roli_jest_bledem_a_nie_pominieciem(repo: Path, dom: Path):
    plik = napisz_skill_kokpitu(dom / ".cockpit" / "skills", "x", role=None)

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert "role" in str(wyjatek.value)
    assert str(plik) in str(wyjatek.value)


def test_przeslaniety_globalny_skill_kokpitu_bez_roli_nadal_jest_bledem(repo: Path, dom: Path):
    napisz_skill_kokpitu(dom / ".cockpit" / "skills", "x", role=None)
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "x", role="author")

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert "role" in str(wyjatek.value)


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_skill_konwencyjny_z_frontmatterem_ktory_nie_jest_yaml_jest_pominiety_a_reszta_dziala(
    repo: Path, dom: Path, zrodlo: str
):
    katalog = repo / zrodlo / "skills" / "zepsuty"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text(
        "---\nname: zepsuty\ndescription: Use when: something\n---\nBrief\n",
        encoding="utf-8",
    )
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "nasz", role="executor")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"nasz"}
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert rejestr.pominiete[0].powod.strip() != ""


def test_frontmatter_ktory_nie_jest_yaml_w_katalogu_kokpitu_jest_bledem_ktory_nazywa_plik(
    repo: Path, dom: Path
):
    katalog = repo / ".cockpit" / "skills" / "zepsuty"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text(
        "---\nname: zepsuty\ndescription: Use when: something\n---\nBrief\n",
        encoding="utf-8",
    )

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert str(plik) in str(wyjatek.value)


def zapisz_plik_w_latin1(katalog: Path) -> Path:
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_bytes(
        b"---\nname: x\nrole: author\n---\nBrief z literka "
        + "ó".encode("latin-1")
        + b"\n"
    )
    return plik


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_skill_konwencyjny_zapisany_nie_w_utf8_jest_pominiety_z_nazwa_pliku(
    repo: Path, dom: Path, zrodlo: str
):
    plik = zapisz_plik_w_latin1(repo / zrodlo / "skills" / "x")
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "nasz", role="executor")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"nasz"}
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert rejestr.pominiete[0].powod.strip() != ""


def test_skill_kokpitu_zapisany_nie_w_utf8_jest_bledem_ktory_nazywa_plik(
    repo: Path, dom: Path
):
    plik = zapisz_plik_w_latin1(repo / ".cockpit" / "skills" / "x")

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert str(plik) in str(wyjatek.value)


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_skill_konwencyjny_z_frontmatterem_bedacym_lista_jest_pominiety(
    repo: Path, dom: Path, zrodlo: str
):
    katalog = repo / zrodlo / "skills" / "lista"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("---\n- a\n- b\n---\nBrief\n", encoding="utf-8")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille == {}
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert rejestr.pominiete[0].powod.strip() != ""


def test_skill_kokpitu_bez_pola_w_rejestrze_jest_bledem_z_nazwa_pola(repo: Path, dom: Path):
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "x", role=None)

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert "role" in str(wyjatek.value)


def test_brak_katalogow_zrodel_daje_pusty_rejestr(repo: Path, dom: Path):
    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille == {}
    assert rejestr.pominiete == []


def test_katalog_bez_pliku_skill_md_jest_pominiety_bez_bledu(repo: Path, dom: Path):
    (repo / ".cockpit" / "skills" / "pusty").mkdir(parents=True)
    (repo / ".claude" / "skills" / "tylko-notatki").mkdir(parents=True)
    (repo / ".claude" / "skills" / "tylko-notatki" / "README.md").write_text("x")
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "ok", role="author")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"ok"}


def test_katalog_bez_skill_md_nie_przerywa_wczytywania_kolejnych_skilli(
    repo: Path, dom: Path
):
    zrodlo = repo / ".cockpit" / "skills"
    (zrodlo / "a-pusty").mkdir(parents=True)
    napisz_skill_kokpitu(zrodlo, "z-poprawny", role="author")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"z-poprawny"}


def test_plik_bez_frontmattera_w_katalogu_claude_jest_pominiety_z_powodem(
    repo: Path, dom: Path
):
    katalog = repo / ".claude" / "skills" / "goly"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("Sam tekst, bez frontmattera.\n", encoding="utf-8")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille == {}
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert rejestr.pominiete[0].powod.strip() != ""


def test_plik_bez_frontmattera_w_katalogu_kokpitu_jest_bledem_ktory_nazywa_plik(
    repo: Path, dom: Path
):
    katalog = repo / ".cockpit" / "skills" / "goly"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("Sam tekst, bez frontmattera.\n", encoding="utf-8")

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert str(plik) in str(wyjatek.value)


def test_skill_konwencyjny_z_rola_bez_pola_name_dostaje_nazwe_katalogu(
    repo: Path, dom: Path
):
    katalog = repo / ".claude" / "skills" / "bez-nazwy"
    katalog.mkdir(parents=True)
    (katalog / "SKILL.md").write_text(
        "---\ndescription: Opis\nrole: executor\n---\nBrief\n", encoding="utf-8"
    )

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille["bez-nazwy"].name == "bez-nazwy"
    assert rejestr.skille["bez-nazwy"].role == Rola.executor


# --- materializacja ---------------------------------------------------------


def rejestr_z_trzema_skillami(repo: Path, dom: Path) -> Rejestr:
    zrodlo = repo / ".cockpit" / "skills"
    napisz_skill_kokpitu(
        zrodlo,
        "a",
        brief="Brief A\n",
        pomocnicze={"szablon.md": "szablon A\n", "dane/tabela.csv": "x,y\n1,2\n"},
    )
    napisz_skill_kokpitu(zrodlo, "b", role="reviewer", pomocnicze={"lista.txt": "B\n"})
    napisz_skill_kokpitu(zrodlo, "c", role="executor", pomocnicze={"c.txt": "C\n"})
    return rozstrzygnij_rejestr(repo, dom)


def test_materializacja_kopiuje_tylko_skille_uzywane_przez_workflow(
    repo: Path, dom: Path, tmp_path: Path
):
    rejestr = rejestr_z_trzema_skillami(repo, dom)
    run = tmp_path / "run"
    run.mkdir()

    wynik = materializuj(["a", "b"], rejestr, run)

    assert wynik == run / ".cockpit" / "_run" / "skills"
    assert sorted(p.name for p in wynik.iterdir()) == ["a", "b"]
    assert drzewo(wynik / "a") == drzewo(rejestr.skille["a"].katalog)
    assert drzewo(wynik / "b") == drzewo(rejestr.skille["b"].katalog)
    assert (wynik / "a" / "dane" / "tabela.csv").read_text() == "x,y\n1,2\n"
    assert not (wynik / "c").exists()


def test_zmiana_skilla_w_rejestrze_po_materializacji_nie_zmienia_kopii(
    repo: Path, dom: Path, tmp_path: Path
):
    rejestr = rejestr_z_trzema_skillami(repo, dom)
    run = tmp_path / "run"
    run.mkdir()
    wynik = materializuj(["a"], rejestr, run)
    kopia_przed = drzewo(wynik / "a")

    zrodlo = rejestr.skille["a"].katalog
    (zrodlo / "SKILL.md").write_text("---\nid: a\nname: zmieniony\nrole: author\n---\nNOWY\n")
    (zrodlo / "szablon.md").write_text("zmieniony szablon\n")
    (zrodlo / "nowy.txt").write_text("dodany po fakcie\n")

    assert drzewo(wynik / "a") == kopia_przed
    assert not (wynik / "a").is_symlink()
    assert not (wynik / "a" / "SKILL.md").is_symlink()
    assert not (wynik / "a" / "szablon.md").is_symlink()
    assert not (wynik / "a" / "nowy.txt").exists()


def test_materializacja_nieznanego_skilla_jest_bledem_ktory_nazywa_id(
    repo: Path, dom: Path, tmp_path: Path
):
    rejestr = rejestr_z_trzema_skillami(repo, dom)
    run = tmp_path / "run"
    run.mkdir()

    with pytest.raises(NieZnaleziono) as wyjatek:
        materializuj(["a", "nie-ma-takiego"], rejestr, run)

    assert "nie-ma-takiego" in str(wyjatek.value)


def test_materializacja_z_nieznanym_skillem_nie_rusza_istniejacego_zestawu_runu(
    repo: Path, dom: Path, tmp_path: Path
):
    rejestr = rejestr_z_trzema_skillami(repo, dom)
    run = tmp_path / "run"
    run.mkdir()
    wynik = materializuj(["a", "b"], rejestr, run)
    przed = drzewo(run)

    with pytest.raises(NieZnaleziono):
        materializuj(["c", "nie-ma-takiego"], rejestr, run)

    assert drzewo(run) == przed
    assert sorted(p.name for p in wynik.iterdir()) == ["a", "b"]


def test_powtorna_materializacja_zostawia_wylacznie_nowy_zestaw(
    repo: Path, dom: Path, tmp_path: Path
):
    rejestr = rejestr_z_trzema_skillami(repo, dom)
    run = tmp_path / "run"
    run.mkdir()
    wynik = materializuj(["a", "b"], rejestr, run)
    (wynik / "recznie-dopisany.txt").write_text("smiec z poprzedniego runu\n")

    drugi = materializuj(["c"], rejestr, run)

    assert drugi == wynik
    assert sorted(p.name for p in wynik.iterdir()) == ["c"]
    assert drzewo(wynik / "c") == drzewo(rejestr.skille["c"].katalog)


def test_dowiazanie_symboliczne_w_skillu_jest_w_kopii_zwyklym_plikiem_z_trescia_celu(
    repo: Path, dom: Path, tmp_path: Path
):
    poza = tmp_path / "poza" / "wspolna.md"
    poza.parent.mkdir()
    poza.write_text("tresc wspolna\n", encoding="utf-8")
    plik = napisz_skill_kokpitu(repo / ".cockpit" / "skills", "a")
    (plik.parent / "pomoc.md").symlink_to(poza)
    run = tmp_path / "run"
    run.mkdir()
    rejestr = rozstrzygnij_rejestr(repo, dom)

    wynik = materializuj(["a"], rejestr, run)

    kopia = wynik / "a" / "pomoc.md"
    assert not kopia.is_symlink()
    assert kopia.read_bytes() == b"tresc wspolna\n"


def test_zmaterializowane_skille_nie_sa_zmiana_do_odbioru_w_git_status(
    repo: Path, dom: Path
):
    def git(*argumenty: str) -> str:
        return subprocess.run(
            ["git", *argumenty], cwd=repo, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q")
    shutil.copy(korzen_repozytorium() / ".gitignore", repo / ".gitignore")
    (repo / "notatki.txt").write_text("nieslediony plik kontrolny\n")
    rejestr = rejestr_z_trzema_skillami(repo, dom)

    materializuj(["a", "b"], rejestr, repo)

    assert (repo / ".cockpit" / "_run" / "skills" / "a" / "SKILL.md").exists()
    status = git("status", "--porcelain", "--untracked-files=all")
    # kontrola: status w ogole pokazuje nieslediowane pliki
    assert "notatki.txt" in status
    assert ".cockpit/_run" not in status


# --- zakaz pyyaml -------------------------------------------------------------


def test_odczyt_skilla_idzie_wylacznie_przez_parser_zachowujacy_komentarze():
    """Dzieki temu zapis skilla z kanwy nie gubi komentarzy i kolejnosci kluczy."""
    zrodlo = KORZEN_PROJEKTU / "src" / "cockpit" / "services" / "skills.py"
    drzewo_ast = ast.parse(zrodlo.read_text(encoding="utf-8"))

    importowane: list[str] = []
    zakazane_uzycia: list[str] = []
    for wezel in ast.walk(drzewo_ast):
        if isinstance(wezel, ast.Import):
            importowane += [a.name for a in wezel.names]
        elif isinstance(wezel, ast.ImportFrom):
            modul = wezel.module or ""
            importowane.append(modul)
            if modul == "frontmatter":
                zakazane_uzycia += [
                    a.name for a in wezel.names if a.name in {"loads", "dumps", "load", "dump", "Post"}
                ]
        elif (
            isinstance(wezel, ast.Attribute)
            and isinstance(wezel.value, ast.Name)
            and wezel.value.id == "frontmatter"
            and wezel.attr in {"loads", "dumps", "load", "dump", "Post"}
        ):
            zakazane_uzycia.append(wezel.attr)

    assert not [m for m in importowane if m == "yaml" or m.startswith("yaml.")]
    assert zakazane_uzycia == []


# --- HAR-10: plik, którego nie da się odczytać, i pusty frontmatter --------


def _odczyt_zawodzi_dla(monkeypatch, cel: Path, *, usun_plik: bool) -> None:
    """Tylko `cel` zawodzi przy odczycie; inne pliki czytane normalnie.

    Wyjątek bez nazwy pliku, żeby ścieżka w komunikacie musiała pochodzić z kodu rejestru.
    """
    oryginal = Path.read_bytes

    def czytaj(self: Path) -> bytes:
        if self != cel:
            return oryginal(self)
        if usun_plik:
            # plik znika między sprawdzeniem istnienia a odczytem
            self.unlink()
            return oryginal(self)
        raise PermissionError("brak praw do odczytu")

    monkeypatch.setattr(Path, "read_bytes", czytaj)


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_skill_konwencyjny_bez_praw_do_odczytu_jest_pominiety_z_nazwa_pliku(
    repo: Path, dom: Path, zrodlo: str, monkeypatch
):
    plik = napisz_skill_konwencyjny(repo / zrodlo / "skills", "zamkniety")
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "nasz", role="executor")
    _odczyt_zawodzi_dla(monkeypatch, plik, usun_plik=False)

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"nasz"}
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert rejestr.pominiete[0].powod.strip() != ""


def test_skill_kokpitu_bez_praw_do_odczytu_jest_bledem_ktory_nazywa_plik(
    repo: Path, dom: Path, monkeypatch
):
    plik = napisz_skill_kokpitu(repo / ".cockpit" / "skills", "zamkniety")
    _odczyt_zawodzi_dla(monkeypatch, plik, usun_plik=False)

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert str(plik) in str(wyjatek.value)


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_skill_konwencyjny_usuniety_w_trakcie_skanu_jest_pominiety_z_nazwa_pliku(
    repo: Path, dom: Path, zrodlo: str, monkeypatch
):
    plik = napisz_skill_konwencyjny(repo / zrodlo / "skills", "znikajacy")
    napisz_skill_kokpitu(repo / ".cockpit" / "skills", "nasz", role="executor")
    _odczyt_zawodzi_dla(monkeypatch, plik, usun_plik=True)

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"nasz"}
    assert [p.plik for p in rejestr.pominiete] == [plik]


def test_skill_kokpitu_usuniety_w_trakcie_skanu_jest_bledem_ktory_nazywa_plik(
    repo: Path, dom: Path, monkeypatch
):
    plik = napisz_skill_kokpitu(repo / ".cockpit" / "skills", "znikajacy")
    _odczyt_zawodzi_dla(monkeypatch, plik, usun_plik=True)

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert str(plik) in str(wyjatek.value)


@pytest.mark.parametrize("zrodlo", [".claude", ".agents"])
def test_skill_konwencyjny_z_pustym_frontmatterem_jest_pominiety_bo_nie_ma_roli(
    repo: Path, dom: Path, zrodlo: str
):
    katalog = repo / zrodlo / "skills" / "pusty"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("---\n---\nBrief\n", encoding="utf-8")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille == {}
    assert [p.plik for p in rejestr.pominiete] == [plik]
    assert "role" in rejestr.pominiete[0].powod


def test_skill_kokpitu_z_pustym_frontmatterem_jest_bledem_z_nazwa_brakujacego_pola(
    repo: Path, dom: Path
):
    katalog = repo / ".cockpit" / "skills" / "pusty"
    katalog.mkdir(parents=True)
    plik = katalog / "SKILL.md"
    plik.write_text("---\n---\nBrief\n", encoding="utf-8")

    with pytest.raises(BlednySkill) as wyjatek:
        rozstrzygnij_rejestr(repo, dom)

    assert "'id'" in str(wyjatek.value)
    assert str(plik) in str(wyjatek.value)


# --- HAR-67: metadane z samego frontmattera i wspolna walidacja pol -------------------


def _meta(frontmatter: str, katalog: str = "x", *, scisle: bool = True):
    from cockpit.services.skills import metadane_skilla

    return metadane_skilla(frontmatter, katalog, scisle=scisle)


def _komunikat_bledu(frontmatter: str, katalog: str = "zly", *, scisle: bool = True) -> str:
    with pytest.raises(BlednySkill) as wyjatek:
        _meta(frontmatter, katalog, scisle=scisle)
    return str(wyjatek.value)


def test_metadane_skilla_oddaja_id_nazwe_i_role_jako_napisy():
    assert _meta("id: x\nname: Nazwa X\nrole: reviewer\n") == ("x", "Nazwa X", "reviewer")


def test_metadane_skilla_konwencyjnego_bez_roli_to_none_a_z_rola_bierze_id_i_nazwe_z_katalogu():
    assert _meta("name: Cudzy\n", "k", scisle=False) is None
    assert _meta("role: executor\n", "k", scisle=False) == ("k", "k", "executor")


def test_skill_kokpitu_bez_roli_ma_w_komunikacie_nazwe_pola_i_katalog():
    assert _komunikat_bledu("id: zly\nname: Zly\n") == "brak pola 'role' w 'zly'"


def test_skill_kokpitu_z_nieznana_rola_ma_w_komunikacie_liste_dozwolonych_rol_po_przecinku():
    dozwolone = ", ".join(r.value for r in Rola)

    assert _komunikat_bledu("id: zly\nname: Zly\nrole: czarodziej\n") == (
        f"nieznana rola 'czarodziej' w 'zly' (dozwolone: {dozwolone})")


def test_skill_o_id_roznym_od_katalogu_ma_w_komunikacie_oba_identyfikatory():
    assert _komunikat_bledu("id: inny\nname: Zly\nrole: author\n") == (
        "id 'inny' rozni sie od nazwy katalogu 'zly' w 'zly'")


def test_frontmatter_z_blednym_yaml_ma_w_komunikacie_nazwe_katalogu():
    assert _komunikat_bledu("id: zly\nname: [niezamkniete\n").startswith(
        "niepoprawny frontmatter w 'zly':")


def test_frontmatter_bedacy_lista_ma_w_komunikacie_nazwe_katalogu():
    assert _komunikat_bledu("- id\n- name\n") == "frontmatter w 'zly' nie jest mapa"


def test_zbyt_gleboko_zagniezdzony_frontmatter_to_blad_skilla_a_nie_wyjatek_biblioteki():
    zagniezdzony = "id: x\nname: " + "[" * 3000 + "]" * 3000 + "\nrole: author\n"

    assert _komunikat_bledu(zagniezdzony, "x").startswith("niepoprawny frontmatter w 'x':")


@pytest.mark.parametrize("frontmatter", [
    "id: x\nname: 5\nrole: author\n",      # tylko nazwa nie jest napisem
    "id: 5\nname: Nazwa\nrole: author\n",  # tylko id nie jest napisem (id == nazwa katalogu "5")
], ids=["nazwa_liczba", "id_liczba"])
def test_id_i_nazwa_skilla_musza_byc_napisami_kazde_z_osobna(frontmatter):
    katalog = "5" if "id: 5" in frontmatter else "x"

    assert _komunikat_bledu(frontmatter, katalog) == (
        f"id i name skilla '{katalog}' musza byc napisami")


def test_lista_w_polu_listowym_skilla_jest_zapisana_jako_lista_napisow(tmp_path: Path):
    plik = tmp_path / "x" / "SKILL.md"
    plik.parent.mkdir()
    plik.write_text(
        "---\nid: x\nname: X\nrole: author\nconsumes: [1, dwa]\nproduces: [3]\n---\nBrief\n",
        encoding="utf-8")

    skill = wczytaj_skill(plik)

    assert skill.consumes == ["1", "dwa"]
    assert skill.produces == ["3"]


def test_konwencyjny_skill_bez_roli_ma_w_pominietych_stale_uzasadnienie(repo: Path, dom: Path):
    katalog = repo / ".claude" / "skills" / "cudzy"
    katalog.mkdir(parents=True)
    (katalog / "SKILL.md").write_text("---\nname: Cudzy\n---\nBrief\n", encoding="utf-8")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert [p.powod for p in rejestr.pominiete] == ["brak pola role - to nie skill kokpitu"]


@pytest.mark.parametrize("dodatek", [
    "utworzono: 2024-02-30\n",                              # ValueError
    "utworzono: 9999-12-31T23:59:59.999999999-23:59\n",     # OverflowError
    "? [a, {b: c}]\n: 1\n",                                 # TypeError (klucz zlozony)
], ids=["niepoprawna_data", "data_poza_zakresem", "klucz_zlozony"])
def test_frontmatter_ktorego_parser_nie_zbuduje_to_dokladnie_blad_skilla(dodatek):
    with pytest.raises(BlednySkill) as wyjatek:
        _meta("id: x\nname: X\nrole: author\n" + dodatek)

    assert type(wyjatek.value) is BlednySkill


# --- rola bez zapisu oddaje jeden artefakt (HAR-160) ----------------------------


def napisz_skill_z_produces(zrodlo: Path, id: str, role: str, produces: str) -> Path:
    plik = napisz_skill_kokpitu(zrodlo, id, role=role)
    plik.write_text(
        plik.read_text(encoding="utf-8").replace(
            f"role: {role}\n", f"role: {role}\nproduces: {produces}\n"
        ),
        encoding="utf-8",
    )
    return plik


@pytest.mark.parametrize("rola", ["reviewer", "gatherer"])
def test_skill_roli_bez_zapisu_z_dwoma_artefaktami_nie_przechodzi_wczytania(
    tmp_path: Path, rola: str
):
    plik = napisz_skill_z_produces(tmp_path / "skills", "dwa", rola, "[a.md, b.md]")

    with pytest.raises(BlednySkill) as blad:
        wczytaj_skill(plik)

    assert type(blad.value) is BlednySkill
    assert "skill dwa" in str(blad.value)
    assert f"rola {rola}" in str(blad.value)
    assert "a.md" in str(blad.value)
    assert "b.md" in str(blad.value)
    assert "produces ma 2: a.md, b.md" in str(blad.value)


def test_skill_autora_z_dwoma_artefaktami_jest_wczytany(tmp_path: Path):
    plik = napisz_skill_z_produces(tmp_path / "skills", "dwa", "author", "[a.md, b.md]")

    assert wczytaj_skill(plik).produces == ["a.md", "b.md"]


def test_skill_wykonawcy_z_dwoma_artefaktami_jest_wczytany(tmp_path: Path):
    plik = napisz_skill_z_produces(tmp_path / "skills", "dwa", "executor", "[a.md, b.md]")

    assert wczytaj_skill(plik).produces == ["a.md", "b.md"]


@pytest.mark.parametrize("rola", ["reviewer", "gatherer"])
def test_skill_roli_bez_zapisu_z_jednym_artefaktem_jest_wczytany(tmp_path: Path, rola: str):
    plik = napisz_skill_z_produces(tmp_path / "skills", "jeden", rola, "[a.md]")

    assert wczytaj_skill(plik).produces == ["a.md"]


# --- HAR-175: frontmatter SKILL.md przechodzi przez waliduj_definicje -------------------


def skill_z_polami(zrodlo: Path, id: str, role: str, dodatek: str = "") -> Path:
    """SKILL.md z id, name i role oraz doklejonymi wierszami frontmattera (kazdy konczy sie \\n)."""
    katalog = zrodlo / id
    katalog.mkdir(parents=True, exist_ok=True)
    plik = katalog / "SKILL.md"
    plik.write_text(
        f"---\nid: {id}\nname: {id}\nrole: {role}\n{dodatek}---\nBrief.\n", encoding="utf-8"
    )
    return plik


ZRODLA_SKILLI = [
    pytest.param("repo", ".cockpit/skills", id="kokpit-w-repozytorium"),
    pytest.param("repo", ".claude/skills", id="konwencyjne-claude"),
    pytest.param("repo", ".agents/skills", id="konwencyjne-agents"),
    pytest.param("dom", ".cockpit/skills", id="kokpit-w-katalogu-domowym"),
]


def zrodlo_skilli(repo: Path, dom: Path, baza: str, podkatalog: str) -> Path:
    return (repo if baza == "repo" else dom) / podkatalog


def test_recenzent_z_dopisanym_prawem_zapisu_w_skill_md_nie_wchodzi_do_rejestru(
    repo: Path, dom: Path
):
    plik = skill_z_polami(repo / ".cockpit" / "skills", "recenzja", "reviewer", "write: true\n")

    with pytest.raises(BlednySkill) as przez_rejestr:
        rozstrzygnij_rejestr(repo, dom)
    with pytest.raises(BlednySkill) as przez_wczytanie:
        wczytaj_skill(plik)

    for blad in (przez_rejestr.value, przez_wczytanie.value):
        assert type(blad) is BlednySkill
        assert str(blad).startswith(f"{plik}: ")
        assert "niezmiennik reviewer_readonly" in str(blad)


@pytest.mark.parametrize("klucz", ["write", "zapis", "can_write"])
@pytest.mark.parametrize("wartosc", ["true", "yes", "tak", "1"])
def test_recenzent_z_kazdym_kluczem_prawa_zapisu_lamie_reviewer_readonly(
    repo: Path, dom: Path, klucz: str, wartosc: str
):
    skill_z_polami(repo / ".cockpit" / "skills", "recenzja", "reviewer", f"{klucz}: {wartosc}\n")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert "niezmiennik reviewer_readonly" in str(blad.value)


def test_recenzent_z_kluczem_zapisu_wylaczonym_lamie_tylko_role_binding_a_nie_regule_recenzenta(
    repo: Path, dom: Path
):
    skill_z_polami(repo / ".cockpit" / "skills", "recenzja", "reviewer", "write: false\n")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert "niezmiennik role_binding" in str(blad.value)
    assert "reviewer_readonly" not in str(blad.value)


def test_skill_z_nazwa_modelu_wskazuje_profil_backendu_u_runnera(repo: Path, dom: Path):
    plik = skill_z_polami(repo / ".cockpit" / "skills", "pisanie", "author", "model: opus\n")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert type(blad.value) is BlednySkill
    assert str(blad.value).startswith(f"{plik}: ")
    assert "niezmiennik role_binding" in str(blad.value)
    assert "'model'" in str(blad.value)
    assert "profilu backendu u runnera" in str(blad.value)


def test_skill_z_tierem_spoza_listy_tierow_wskazuje_profil_backendu_u_runnera(
    repo: Path, dom: Path
):
    plik = skill_z_polami(repo / ".cockpit" / "skills", "pisanie", "author", "tier: gold\n")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert str(blad.value).startswith(f"{plik}: ")
    assert "niezmiennik role_binding" in str(blad.value)
    assert "tier 'gold' nie jest tierem" in str(blad.value)
    assert "profilu backendu u runnera" in str(blad.value)


def test_skill_z_tierem_niepasujacym_do_roli_lamie_role_binding(repo: Path, dom: Path):
    skill_z_polami(repo / ".cockpit" / "skills", "pisanie", "author", "tier: execution\n")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert "niezmiennik role_binding" in str(blad.value)
    assert "nie pasuje do roli author" in str(blad.value)


def test_skill_z_tierem_zgodnym_z_rola_jest_wczytany(repo: Path, dom: Path):
    skill_z_polami(repo / ".cockpit" / "skills", "pisanie", "author", "tier: judgment\n")

    assert set(rozstrzygnij_rejestr(repo, dom).skille) == {"pisanie"}


@pytest.mark.parametrize("rola", ["author", "executor"])
@pytest.mark.parametrize("klucz", ["write", "zapis", "can_write"])
@pytest.mark.parametrize("wartosc", ["true", "false"])
def test_autor_z_jawnym_prawem_zapisu_lamie_role_binding(
    repo: Path, dom: Path, rola: str, klucz: str, wartosc: str
):
    plik = skill_z_polami(
        repo / ".cockpit" / "skills", "pisanie", rola, f"{klucz}: {wartosc}\n"
    )

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert str(blad.value).startswith(f"{plik}: ")
    assert "niezmiennik role_binding" in str(blad.value)
    assert f"'{klucz}'" in str(blad.value)
    assert "reviewer_readonly" not in str(blad.value)


def test_skill_konwencyjny_z_rola_i_prawem_zapisu_jest_bledem_a_bez_roli_jest_pominiety(
    repo: Path, dom: Path
):
    zrodlo = repo / ".claude" / "skills"
    bez_roli = napisz_skill_konwencyjny(zrodlo, "bez-roli", extra="write: true\nmodel: opus\n")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert rejestr.skille == {}
    assert [p.plik for p in rejestr.pominiete] == [bez_roli]

    z_rola = napisz_skill_konwencyjny(
        zrodlo, "z-rola", extra="role: reviewer\nwrite: true\n"
    )

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert type(blad.value) is BlednySkill
    assert str(blad.value).startswith(f"{z_rola}: ")
    assert "niezmiennik reviewer_readonly" in str(blad.value)


@pytest.mark.parametrize("baza,podkatalog", ZRODLA_SKILLI)
def test_naruszenie_niezmiennika_w_skill_md_jest_bledem_w_kazdym_zrodle_skilli(
    repo: Path, dom: Path, baza: str, podkatalog: str
):
    plik = skill_z_polami(
        zrodlo_skilli(repo, dom, baza, podkatalog), "pisanie", "author", "write: true\n"
    )

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert str(blad.value).startswith(f"{plik}: ")
    assert "niezmiennik role_binding" in str(blad.value)


@pytest.mark.parametrize("baza,podkatalog", ZRODLA_SKILLI)
def test_skill_bez_kluczy_zapisu_modelu_i_tieru_wchodzi_do_rejestru_w_kazdym_zrodle(
    repo: Path, dom: Path, baza: str, podkatalog: str
):
    skill_z_polami(
        zrodlo_skilli(repo, dom, baza, podkatalog), "pisanie", "reviewer",
        "description: Opis\nconsumes: [a.md]\n",
    )

    assert set(rozstrzygnij_rejestr(repo, dom).skille) == {"pisanie"}


def test_przeslaniety_skill_kokpitu_z_naruszeniem_niezmiennika_nadal_jest_bledem(
    repo: Path, dom: Path
):
    skill_z_polami(dom / ".cockpit" / "skills", "wspolny", "reviewer", "write: true\n")
    skill_z_polami(repo / ".cockpit" / "skills", "wspolny", "reviewer")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert "niezmiennik reviewer_readonly" in str(blad.value)


def test_dwa_naruszenia_w_jednym_skillu_sa_oba_w_komunikacie(repo: Path, dom: Path):
    plik = skill_z_polami(
        repo / ".cockpit" / "skills", "recenzja", "reviewer", "write: true\nmodel: opus\n"
    )
    oczekiwane = waliduj_definicje({"role": "reviewer", "write": True, "model": "opus"})
    assert {n.niezmiennik.value for n in oczekiwane} == {"reviewer_readonly", "role_binding"}

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert str(blad.value) == f"{plik}: " + "; ".join(str(n) for n in oczekiwane)
    assert "niezmiennik reviewer_readonly" in str(blad.value)
    assert "niezmiennik role_binding" in str(blad.value)
    assert "'model'" in str(blad.value)


def test_trzy_naruszenia_roznych_regul_w_jednym_skillu_sa_wszystkie_w_komunikacie(
    repo: Path, dom: Path
):
    skill_z_polami(
        repo / ".cockpit" / "skills", "recenzja", "reviewer",
        "write: true\nmodel: opus\ntier: gold\ncommand: git push\n",
    )

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    for nazwa in ("reviewer_readonly", "role_binding", "no_git_write"):
        assert f"niezmiennik {nazwa}" in str(blad.value)
    assert "tier 'gold' nie jest tierem" in str(blad.value)
    assert "'model'" in str(blad.value)


@pytest.mark.parametrize("klucz", ["command", "run"])
@pytest.mark.parametrize("komenda", ["git push", "git commit -m x", "cd r && git push"])
def test_skill_z_komenda_zapisujaca_do_gita_lamie_no_git_write(
    repo: Path, dom: Path, klucz: str, komenda: str
):
    plik = skill_z_polami(
        repo / ".cockpit" / "skills", "wydanie", "executor", f"{klucz}: {komenda}\n"
    )

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert str(blad.value).startswith(f"{plik}: ")
    assert "niezmiennik no_git_write" in str(blad.value)


def test_skill_z_komenda_gita_podana_jako_lista_lamie_no_git_write(repo: Path, dom: Path):
    skill_z_polami(
        repo / ".cockpit" / "skills", "wydanie", "executor", "command: [git, push]\n"
    )

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert "niezmiennik no_git_write" in str(blad.value)


@pytest.mark.parametrize("klucz", ["command", "run"])
def test_skill_z_bezpieczna_komenda_wchodzi_do_rejestru(repo: Path, dom: Path, klucz: str):
    skill_z_polami(repo / ".cockpit" / "skills", "testy", "executor", f"{klucz}: pytest -q\n")

    assert set(rozstrzygnij_rejestr(repo, dom).skille) == {"testy"}


@pytest.mark.parametrize("klucz", ["command", "run"])
def test_komenda_niebedaca_napisem_ani_lista_nie_daje_surowego_typeerror(
    repo: Path, dom: Path, klucz: str
):
    skill_z_polami(repo / ".cockpit" / "skills", "testy", "executor", f"{klucz}: 5\n")

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"testy"}


@pytest.mark.parametrize("klucz", ["1: x\n", "2.5: x\n", "true: x\n", "~: x\n"])
def test_klucz_frontmattera_niebedacy_napisem_nie_daje_surowego_typeerror_i_jest_pomijany(
    repo: Path, dom: Path, klucz: str
):
    skill_z_polami(repo / ".cockpit" / "skills", "pisanie", "author", klucz)

    rejestr = rozstrzygnij_rejestr(repo, dom)

    assert set(rejestr.skille) == {"pisanie"}


@pytest.mark.parametrize("klucz", ["1: x\n", "true: x\n", "~: x\n"])
def test_klucz_niebedacy_napisem_obok_naruszenia_nie_ukrywa_naruszenia(
    repo: Path, dom: Path, klucz: str
):
    skill_z_polami(repo / ".cockpit" / "skills", "pisanie", "author", klucz + "write: true\n")

    with pytest.raises(BlednySkill) as blad:
        rozstrzygnij_rejestr(repo, dom)

    assert type(blad.value) is BlednySkill
    assert "niezmiennik role_binding" in str(blad.value)


def test_klucz_niebedacy_napisem_w_skillu_konwencyjnym_z_rola_nie_daje_surowego_typeerror(
    repo: Path, dom: Path
):
    napisz_skill_konwencyjny(
        repo / ".claude" / "skills", "wlasny", extra="role: author\n1: x\n"
    )

    assert set(rozstrzygnij_rejestr(repo, dom).skille) == {"wlasny"}


# --- HAR-180: parsowanie z tekstu stosuje te same reguly co parsowanie z pliku ----------------


def _skill_z_tekstu(tekst: str, katalog: str, *, scisle: bool = True):
    from cockpit.services.skills import skill_z_tekstu

    return skill_z_tekstu(tekst, katalog, scisle=scisle)


def _plik_z_tekstem(zrodlo: Path, katalog: str, tekst: str) -> Path:
    plik = zrodlo / katalog / "SKILL.md"
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_bytes(tekst.encode("utf-8"))
    return plik


def _pola(skill: Skill) -> dict:
    return skill.model_dump(exclude={"katalog"})


TEKST_PELNY = (
    "---\n"
    "id: pelny\n"
    "name: Pelny skill\n"
    "role: reviewer\n"
    "consumes: [diff.md, plan.md]\n"
    "produces: [raport.md]\n"
    "needs: [lint]\n"
    "verdict:\n"
    "  pass: [ZGODNY]\n"
    "  fail: [BLAD]\n"
    "---\n"
    "Brief z CRLF\r\n  i spacjami na koncu   \r\n\r\nbez konca linii"
)


def test_skill_z_tekstu_ma_te_same_pola_co_ten_sam_skill_wczytany_z_pliku(tmp_path: Path):
    plik = _plik_z_tekstem(tmp_path / "skills", "pelny", TEKST_PELNY)

    z_tekstu = _skill_z_tekstu(TEKST_PELNY, "pelny")

    assert _pola(z_tekstu) == _pola(wczytaj_skill(plik))
    assert z_tekstu.role is Rola.reviewer
    assert z_tekstu.consumes == ["diff.md", "plan.md"]
    assert z_tekstu.produces == ["raport.md"]
    assert z_tekstu.needs == ["lint"]
    assert z_tekstu.verdict == wczytaj_skill(plik).verdict
    assert z_tekstu.verdict is not None


def test_skill_z_tekstu_oddaje_brief_bajt_w_bajt_wlacznie_z_dalsza_granica_i_bez_konca_linii():
    tekst = "---\nid: x\nname: X\nrole: author\n---\r\nPierwsza  \r\n---\r\nPo granicy ✓   "

    skill = _skill_z_tekstu(tekst, "x")

    assert skill.brief == "Pierwsza  \r\n---\r\nPo granicy ✓   "


def test_skill_z_tekstu_niesie_nazwe_katalogu_jako_katalog_skilla():
    skill = _skill_z_tekstu("---\nid: x\nname: X\nrole: author\n---\nB\n", "x")

    assert skill.katalog.name == "x"


# Przypadki niezalezne od kodu: kazdy musi byc odrzucony tak samo z pliku i z tekstu.
TEKSTY_ODRZUCANE = {
    "rola_bez_zapisu_z_dwoma_artefaktami_recenzent": (
        "---\nid: x\nname: X\nrole: reviewer\nproduces: [a.md, b.md]\n---\nB\n"),
    "rola_bez_zapisu_z_dwoma_artefaktami_zbierajacy": (
        "---\nid: x\nname: X\nrole: gatherer\nproduces: [a.md, b.md]\n---\nB\n"),
    "nieznana_rola": "---\nid: x\nname: X\nrole: czarodziej\n---\nB\n",
    "brak_nazwy": "---\nid: x\nrole: author\n---\nB\n",
    "brak_roli": "---\nid: x\nname: X\n---\nB\n",
    "id_rozne_od_katalogu": "---\nid: inny\nname: X\nrole: author\n---\nB\n",
    "verdict_bez_produces": (
        "---\nid: x\nname: X\nrole: author\nverdict:\n  pass: [OK]\n---\nB\n"),
    "verdict_nie_jest_mapa": (
        "---\nid: x\nname: X\nrole: author\nproduces: [a.md]\nverdict: tak\n---\nB\n"),
    "naruszenie_niezmiennika_role_binding": (
        "---\nid: x\nname: X\nrole: reviewer\nwrite: true\n---\nB\n"),
    "brak_frontmattera": "Sam brief bez frontmattera\n",
    "niezamkniety_frontmatter": "---\nid: x\nname: X\nrole: author\nBrief\n",
    "zly_yaml": "---\nid: x\nname: [niezamkniete\nrole: author\n---\nB\n",
    "frontmatter_nie_jest_mapa": "---\n- id\n- name\n---\nB\n",
}


@pytest.mark.parametrize("tekst", TEKSTY_ODRZUCANE.values(), ids=TEKSTY_ODRZUCANE.keys())
def test_tekst_odrzucany_przy_wczytaniu_z_pliku_jest_odrzucany_tak_samo_przy_parsowaniu_z_tekstu(
    tmp_path: Path, tekst: str
):
    plik = _plik_z_tekstem(tmp_path / "skills", "x", tekst)
    with pytest.raises(BlednySkill) as z_pliku:
        wczytaj_skill(plik)

    with pytest.raises(BlednySkill) as z_tekstu:
        _skill_z_tekstu(tekst, "x")

    assert type(z_pliku.value) is BlednySkill
    assert type(z_tekstu.value) is BlednySkill


TEKSTY_PRZYJMOWANE = {
    "autor_z_dwoma_artefaktami": (
        "---\nid: x\nname: X\nrole: author\nproduces: [a.md, b.md]\n---\nB\n"),
    "wykonawca_z_dwoma_artefaktami": (
        "---\nid: x\nname: X\nrole: executor\nproduces: [a.md, b.md]\n---\nB\n"),
    "recenzent_z_jednym_artefaktem": (
        "---\nid: x\nname: X\nrole: reviewer\nproduces: [a.md]\n---\nB\n"),
    "zbierajacy_bez_artefaktow": "---\nid: x\nname: X\nrole: gatherer\n---\nB\n",
    "pusty_brief": "---\nid: x\nname: X\nrole: author\n---\n",
}


@pytest.mark.parametrize("tekst", TEKSTY_PRZYJMOWANE.values(), ids=TEKSTY_PRZYJMOWANE.keys())
def test_tekst_przyjmowany_przy_wczytaniu_z_pliku_daje_te_same_pola_przy_parsowaniu_z_tekstu(
    tmp_path: Path, tekst: str
):
    plik = _plik_z_tekstem(tmp_path / "skills", "x", tekst)

    assert _pola(_skill_z_tekstu(tekst, "x")) == _pola(wczytaj_skill(plik))


@pytest.mark.parametrize("rola", ["reviewer", "gatherer"])
def test_komunikat_roli_bez_zapisu_z_dwoma_artefaktami_jest_ten_sam_z_pliku_i_z_tekstu(
    tmp_path: Path, rola: str
):
    tekst = f"---\nid: dwa\nname: Dwa\nrole: {rola}\nproduces: [a.md, b.md]\n---\nB\n"
    plik = _plik_z_tekstem(tmp_path / "skills", "dwa", tekst)
    with pytest.raises(BlednySkill) as z_pliku:
        wczytaj_skill(plik)

    with pytest.raises(BlednySkill) as z_tekstu:
        _skill_z_tekstu(tekst, "dwa")

    oczekiwany = f"skill dwa: rola {rola} oddaje jeden artefakt, produces ma 2: a.md, b.md"
    assert str(z_pliku.value) == oczekiwany
    assert str(z_tekstu.value) == oczekiwany


def test_komunikat_nieznanej_roli_z_tekstu_wskazuje_katalog_a_nie_sciezke_pliku():
    tekst = "---\nid: zly\nname: Zly\nrole: czarodziej\n---\nB\n"

    with pytest.raises(BlednySkill) as blad:
        _skill_z_tekstu(tekst, "zly")

    assert str(blad.value) == (
        "nieznana rola 'czarodziej' w 'zly' (dozwolone: author, reviewer, executor, gatherer)")


def test_komunikat_id_roznego_od_katalogu_z_tekstu_wskazuje_katalog_a_nie_sciezke_pliku():
    tekst = "---\nid: inny\nname: X\nrole: author\n---\nB\n"

    with pytest.raises(BlednySkill) as blad:
        _skill_z_tekstu(tekst, "zly")

    assert str(blad.value) == "id 'inny' rozni sie od nazwy katalogu 'zly' w 'zly'"


def test_zrodlo_niescisle_bez_roli_to_nie_skill_kokpitu_a_nie_blad():
    assert _skill_z_tekstu("---\nname: Cudzy\n---\nB\n", "cudzy", scisle=False) is None


def test_zrodlo_niescisle_z_rola_dostaje_id_i_nazwe_z_nazwy_katalogu():
    skill = _skill_z_tekstu("---\nrole: executor\n---\nB\n", "konwencja", scisle=False)

    assert (skill.id, skill.name, skill.role) == ("konwencja", "konwencja", Rola.executor)


def test_zrodlo_niescisle_z_nieparsowalnym_tekstem_to_nie_skill_kokpitu_a_nie_blad():
    assert _skill_z_tekstu("Sam brief bez frontmattera\n", "cudzy", scisle=False) is None


def test_zrodlo_niescisle_z_rola_ale_naruszeniem_reguly_to_nadal_blad_skilla():
    tekst = "---\nrole: reviewer\nproduces: [a.md, b.md]\n---\nB\n"

    with pytest.raises(BlednySkill):
        _skill_z_tekstu(tekst, "konwencja", scisle=False)


def test_zrodlo_scisle_z_nieparsowalnym_tekstem_to_blad_skilla_a_nie_brak_skilla():
    with pytest.raises(BlednySkill):
        _skill_z_tekstu("Sam brief bez frontmattera\n", "x", scisle=True)


def test_komunikat_niepoprawnego_yaml_z_tekstu_wskazuje_katalog_a_nie_sciezke_pliku():
    with pytest.raises(BlednySkill) as blad:
        _skill_z_tekstu("---\nid: x\nname: [niezamkniete\nrole: author\n---\nB\n", "x")

    assert str(blad.value).startswith("niepoprawny frontmatter w 'x': ")


def test_komunikat_frontmattera_ktory_nie_jest_mapa_z_tekstu_wskazuje_katalog():
    with pytest.raises(BlednySkill) as blad:
        _skill_z_tekstu("---\n- id\n- name\n---\nB\n", "x")

    assert str(blad.value) == "frontmatter w 'x' nie jest mapa"


def test_komunikat_braku_frontmattera_w_podziale_skilla_wskazuje_katalog():
    with pytest.raises(BlednySkill) as blad:
        podziel_skill("Sam brief bez frontmattera\n", "goly")

    assert str(blad.value) == "brak frontmattera w 'goly'"


def test_komunikat_niezamknietego_frontmattera_w_podziale_skilla_wskazuje_katalog():
    with pytest.raises(BlednySkill) as blad:
        podziel_skill("---\nid: x\nBrief\n", "otwarty")

    assert str(blad.value) == "niezamkniety frontmatter w 'otwarty'"


def test_linia_z_myslnikami_i_dopiskiem_nie_zamyka_frontmattera():
    # `---X` to nie granica: obciecie dopisku przed dopasowaniem zamknęloby blok
    with pytest.raises(BlednySkill) as blad:
        podziel_skill("---\nid: x\n---X\nBrief\n", "dopisek")

    assert str(blad.value) == "niezamkniety frontmatter w 'dopisek'"


def test_podzial_skilla_oddaje_frontmatter_i_brief_bajt_w_bajt_z_granica_z_koncowymi_spacjami():
    frontmatter, brief = podziel_skill("---\nid: x\n---  \r\nBrief\n---\n", "x")

    assert (frontmatter, brief) == ("id: x\n", "Brief\n---\n")


@pytest.mark.parametrize(
    ("tekst", "komunikat"),
    [
        ("Sam brief bez frontmattera\n", "brak frontmattera w 'moj-skill'"),
        ("---\nid: x\nname: X\nrole: author\nBrief\n", "niezamkniety frontmatter w 'moj-skill'"),
    ],
    ids=["brak_frontmattera", "niezamkniety_frontmatter"],
)
def test_skill_z_tekstu_scisle_bez_poprawnego_frontmattera_wskazuje_katalog_w_komunikacie(
    tekst: str, komunikat: str
):
    with pytest.raises(BlednySkill) as e:
        _skill_z_tekstu(tekst, "moj-skill", scisle=True)

    assert str(e.value) == komunikat

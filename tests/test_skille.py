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
    rozstrzygnij_rejestr,
    wczytaj_skill,
    zapisz_skill,
)

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
            "role: gatherer\n", "role: gatherer\nconsumes: [a]\nproduces: [b, c]\nneeds: [d]\n"
        ),
        encoding="utf-8",
    )

    skill = wczytaj_skill(plik)

    assert isinstance(skill, Skill)
    assert skill.id == "analiza"
    assert skill.name == "Analiza"
    assert skill.role == Rola.gatherer
    assert skill.consumes == ["a"]
    assert skill.produces == ["b", "c"]
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

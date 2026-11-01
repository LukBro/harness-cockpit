"""Pierwszenstwo warstw instrukcji jako byt w kodzie (HAR-87), zgodny z CLAUDE.md."""
from itertools import product
from pathlib import Path

import pytest

from cockpit.services.warstwy_instrukcji import (
    OD_NAJWYZSZEJ,
    OPIS_W_CLAUDE_MD,
    WarstwaInstrukcji,
    lekcja_moze_nadpisac,
    pierwszenstwo,
    rozstrzygnij,
    warstwy_z_claude_md,
)

W = WarstwaInstrukcji

# Kolejnosc jawnie, niezaleznie od kodu: numer = pozycja na liscie w CLAUDE.md.
OCZEKIWANE = [
    (1, "niezmienniki_silnika"),
    (2, "project_yaml"),
    (3, "claude_md_katalogu"),
    (4, "claude_md_korzenia"),
    (5, "skill_md"),
    (6, "lekcje"),
]
NUMER = {W(nazwa): numer for numer, nazwa in OCZEKIWANE}

CLAUDE_MD = Path(__file__).resolve().parents[1] / "CLAUDE.md"

ZLE_TYPY = ["lekcje", "niezmienniki_silnika", None, 1, 6, 1.0, ("lekcje",)]


def _pozycje(*warstwy: WarstwaInstrukcji) -> list[str]:
    return [f"{i}. {OPIS_W_CLAUDE_MD[w]}" for i, w in enumerate(warstwy, start=1)]


def _claude_md(pozycje: list[str], przed: str = "", po: str = "") -> str:
    return (
        "# Tytul\n\n"
        + przed
        + "## Pierwszeństwo warstw — MUST / NEVER\n\nWstep.\n\n"
        + "\n".join(pozycje)
        + "\n\n- MUST: cos.\n\n"
        + po
    )


# --- kolejnosc w kodzie ---------------------------------------------------------


def test_warstw_instrukcji_jest_dokladnie_szesc_w_kolejnosci_pierwszenstwa():
    assert [w.value for w in WarstwaInstrukcji] == [n for _, n in OCZEKIWANE]


def test_lista_od_najwyzszej_to_wszystkie_warstwy_w_kolejnosci_definicji():
    assert OD_NAJWYZSZEJ == tuple(WarstwaInstrukcji)
    assert isinstance(OD_NAJWYZSZEJ, tuple)


@pytest.mark.parametrize(("numer", "nazwa"), OCZEKIWANE)
def test_warstwa_ma_pozycje_z_listy_w_claude_md(numer, nazwa):
    assert pierwszenstwo(W(nazwa)) == numer


@pytest.mark.parametrize(("a", "b"), list(product(W, W)))
def test_przy_sprzecznosci_wygrywa_warstwa_o_mniejszym_numerze(a, b):
    oczekiwana = a if NUMER[a] <= NUMER[b] else b
    assert rozstrzygnij(a, b) is oczekiwana


@pytest.mark.parametrize(("a", "b"), list(product(W, W)))
def test_rozstrzygniecie_nie_zalezy_od_kolejnosci_argumentow(a, b):
    assert rozstrzygnij(a, b) == rozstrzygnij(b, a)


@pytest.mark.parametrize(
    "wyzsza", [W.niezmienniki_silnika, W.project_yaml, W.claude_md_katalogu, W.claude_md_korzenia]
)
def test_lekcja_nie_nadpisuje_warstw_od_pierwszej_do_czwartej(wyzsza):
    assert rozstrzygnij(W.lekcje, wyzsza) is wyzsza
    assert rozstrzygnij(wyzsza, W.lekcje) is wyzsza


def test_lekcja_nie_nadpisuje_niezmiennika_silnika():
    assert rozstrzygnij(W.lekcje, W.niezmienniki_silnika) is W.niezmienniki_silnika
    assert rozstrzygnij(W.niezmienniki_silnika, W.lekcje) is W.niezmienniki_silnika


def test_skill_md_wygrywa_z_lekcja():
    assert rozstrzygnij(W.skill_md, W.lekcje) is W.skill_md
    assert rozstrzygnij(W.lekcje, W.skill_md) is W.skill_md


@pytest.mark.parametrize(("numer", "nazwa"), OCZEKIWANE)
def test_tylko_lekcja_moze_nadpisac_lekcje(numer, nazwa):
    assert lekcja_moze_nadpisac(W(nazwa)) is (nazwa == "lekcje")


@pytest.mark.parametrize("zly", ZLE_TYPY)
def test_pierwszenstwo_zlego_typu_to_blad_typu(zly):
    with pytest.raises(TypeError):
        pierwszenstwo(zly)


@pytest.mark.parametrize("zly", ZLE_TYPY)
def test_rozstrzygniecie_ze_zlym_typem_pierwszego_argumentu_to_blad_typu(zly):
    with pytest.raises(TypeError):
        rozstrzygnij(zly, W.lekcje)


@pytest.mark.parametrize("zly", ZLE_TYPY)
def test_rozstrzygniecie_ze_zlym_typem_drugiego_argumentu_to_blad_typu(zly):
    with pytest.raises(TypeError):
        rozstrzygnij(W.skill_md, zly)


@pytest.mark.parametrize("zly", ZLE_TYPY)
def test_moc_nadpisania_zlego_typu_to_blad_typu(zly):
    with pytest.raises(TypeError):
        lekcja_moze_nadpisac(zly)


def test_opis_w_claude_md_ma_wpis_dla_kazdej_warstwy_z_dokladna_wartoscia():
    assert dict(OPIS_W_CLAUDE_MD) == {
        W.niezmienniki_silnika: "niezmienniki silnika",
        W.project_yaml: "`.cockpit/project.yaml`",
        W.claude_md_katalogu: "`CLAUDE.md` katalogu",
        W.claude_md_korzenia: "`CLAUDE.md` korzenia",
        W.skill_md: "`SKILL.md` kroku",
        W.lekcje: "`.ai/lessons.md`",
    }


# --- zgodnosc z CLAUDE.md -------------------------------------------------------


def test_kolejnosc_warstw_w_kodzie_zgodna_z_claude_md():
    tekst = CLAUDE_MD.read_text(encoding="utf-8")
    assert warstwy_z_claude_md(tekst) == list(WarstwaInstrukcji)


def test_claude_md_o_kolejnosci_z_kodu_jest_odczytany_w_tej_kolejnosci():
    tekst = _claude_md(_pozycje(*OD_NAJWYZSZEJ))
    assert warstwy_z_claude_md(tekst) == list(OD_NAJWYZSZEJ)


def test_zamiana_dwoch_pozycji_w_claude_md_rozjezdza_sie_z_kodem():
    zamienione = [W.niezmienniki_silnika, W.project_yaml, W.claude_md_korzenia,
                  W.claude_md_katalogu, W.skill_md, W.lekcje]
    odczytane = warstwy_z_claude_md(_claude_md(_pozycje(*zamienione)))
    assert odczytane == zamienione
    assert odczytane != list(WarstwaInstrukcji)


def test_brakujaca_pozycja_w_claude_md_rozjezdza_sie_z_kodem():
    bez_lekcji = list(OD_NAJWYZSZEJ[:-1])
    odczytane = warstwy_z_claude_md(_claude_md(_pozycje(*bez_lekcji)))
    assert odczytane == bez_lekcji
    assert odczytane != list(WarstwaInstrukcji)


def test_zdublowana_pozycja_w_claude_md_zostaje_na_liscie():
    z_duplikatem = [*OD_NAJWYZSZEJ, W.lekcje]
    assert warstwy_z_claude_md(_claude_md(_pozycje(*z_duplikatem))) == z_duplikatem


def test_nieznana_pozycja_w_claude_md_to_blad_wartosci():
    pozycje = [*_pozycje(*OD_NAJWYZSZEJ), "7. pamiec modelu"]
    with pytest.raises(ValueError, match="pamiec modelu"):
        warstwy_z_claude_md(_claude_md(pozycje))


def test_pozycja_zaczynajaca_sie_inaczej_niz_opis_warstwy_to_blad_wartosci():
    pozycje = _pozycje(*OD_NAJWYZSZEJ)
    pozycje[5] = "6. moje `.ai/lessons.md`"
    with pytest.raises(ValueError):
        warstwy_z_claude_md(_claude_md(pozycje))


def test_claude_md_bez_sekcji_pierwszenstwa_to_blad_wartosci():
    with pytest.raises(ValueError):
        warstwy_z_claude_md("# Tytul\n\n## Inna sekcja\n\n1. niezmienniki silnika\n")


def test_numeracja_z_dziura_w_claude_md_to_blad_wartosci():
    pozycje = _pozycje(*OD_NAJWYZSZEJ)
    pozycje[3] = "5. " + OPIS_W_CLAUDE_MD[W.claude_md_korzenia]
    pozycje[4] = "6. " + OPIS_W_CLAUDE_MD[W.skill_md]
    pozycje[5] = "7. " + OPIS_W_CLAUDE_MD[W.lekcje]
    with pytest.raises(ValueError):
        warstwy_z_claude_md(_claude_md(pozycje))


def test_numeracja_niezaczynajaca_sie_od_jedynki_w_claude_md_to_blad_wartosci():
    pozycje = [f"{i}. {OPIS_W_CLAUDE_MD[w]}" for i, w in enumerate(OD_NAJWYZSZEJ, start=2)]
    with pytest.raises(ValueError):
        warstwy_z_claude_md(_claude_md(pozycje))


def test_lista_numerowana_przed_sekcja_pierwszenstwa_nie_jest_brana_pod_uwage():
    przed = "## Inna sekcja\n\n1. niezmienniki silnika\n2. cos zupelnie innego\n\n"
    tekst = _claude_md(_pozycje(*OD_NAJWYZSZEJ), przed=przed)
    assert warstwy_z_claude_md(tekst) == list(OD_NAJWYZSZEJ)


def test_lista_numerowana_po_sekcji_pierwszenstwa_nie_jest_brana_pod_uwage():
    po = "## Nastepna sekcja\n\n1. cos zupelnie innego\n2. `.ai/lessons.md`\n"
    tekst = _claude_md(_pozycje(*OD_NAJWYZSZEJ), po=po)
    assert warstwy_z_claude_md(tekst) == list(OD_NAJWYZSZEJ)


@pytest.mark.parametrize(("zly", "nazwa_typu"), [("lekcje", "str"), (1, "int"), (None, "NoneType")])
def test_komunikat_zlego_typu_wskazuje_oczekiwany_i_podany_typ(zly, nazwa_typu):
    with pytest.raises(TypeError) as exc:
        pierwszenstwo(zly)
    assert str(exc.value) == f"oczekiwano WarstwaInstrukcji, jest {nazwa_typu}"


def test_komunikat_braku_sekcji_pierwszenstwa_jest_dokladny():
    with pytest.raises(ValueError) as exc:
        warstwy_z_claude_md("# Tytul\n\n## Inna sekcja\n\n1. niezmienniki silnika\n")
    assert str(exc.value) == "CLAUDE.md nie ma sekcji „Pierwszeństwo warstw”"


def test_komunikat_numeracji_z_dziura_cytuje_pozycje():
    pozycje = _pozycje(*OD_NAJWYZSZEJ)
    pozycje[3] = "5. " + OPIS_W_CLAUDE_MD[W.claude_md_korzenia]
    with pytest.raises(ValueError) as exc:
        warstwy_z_claude_md(_claude_md(pozycje))
    assert str(exc.value) == (
        "numeracja listy warstw nie jest kolejna od 1: '5. `CLAUDE.md` korzenia'"
    )


def test_komunikat_nieznanej_pozycji_cytuje_pozycje():
    pozycje = [*_pozycje(*OD_NAJWYZSZEJ), "7. pamiec modelu"]
    with pytest.raises(ValueError) as exc:
        warstwy_z_claude_md(_claude_md(pozycje))
    assert str(exc.value) == "pozycja listy nie pasuje do zadnej warstwy: '7. pamiec modelu'"


def test_dziesiata_pozycja_listy_z_zla_numeracja_jest_odrzucona():
    pozycje = [*_pozycje(*OD_NAJWYZSZEJ), "10. niezmienniki silnika"]
    with pytest.raises(ValueError) as exc:
        warstwy_z_claude_md(_claude_md(pozycje))
    assert str(exc.value) == (
        "numeracja listy warstw nie jest kolejna od 1: '10. niezmienniki silnika'"
    )


def test_wiersz_bez_spacji_po_numerze_nie_jest_pozycja_listy():
    pozycje = [*_pozycje(*OD_NAJWYZSZEJ), "7.zupelnie cos innego"]
    assert warstwy_z_claude_md(_claude_md(pozycje)) == list(OD_NAJWYZSZEJ)


def test_wiersz_z_wcieciem_nie_jest_pozycja_listy():
    pozycje = [*_pozycje(*OD_NAJWYZSZEJ), " 7. zupelnie cos innego"]
    assert warstwy_z_claude_md(_claude_md(pozycje)) == list(OD_NAJWYZSZEJ)


@pytest.mark.parametrize(("warstwa", "wartosc"), [
    (W.niezmienniki_silnika, "niezmienniki_silnika"),
    (W.project_yaml, "project_yaml"),
    (W.claude_md_katalogu, "claude_md_katalogu"),
    (W.claude_md_korzenia, "claude_md_korzenia"),
    (W.skill_md, "skill_md"),
    (W.lekcje, "lekcje"),
])
def test_warstwa_ma_stala_wartosc_tekstowa(warstwa, wartosc):
    assert warstwa.value == wartosc
    assert str(warstwa) == wartosc


def test_pierwsza_pozycja_tuz_pod_naglowkiem_sekcji_jest_odczytana():
    tekst = "## Pierwszeństwo warstw\n" + "\n".join(_pozycje(*OD_NAJWYZSZEJ)) + "\n"
    assert warstwy_z_claude_md(tekst) == list(OD_NAJWYZSZEJ)


def test_sekcja_pierwszenstwa_na_koncu_pliku_bez_nastepnego_naglowka_jest_odczytana():
    tekst = "## Pierwszeństwo warstw\n\n" + "\n".join(_pozycje(*OD_NAJWYZSZEJ)) + "\n"
    assert warstwy_z_claude_md(tekst) == list(OD_NAJWYZSZEJ)

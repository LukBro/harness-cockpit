"""HAR-176: kotwice `plik:linia` w `notatka-zmiany.md` (explain-change) i ich sprawdzenie (TDD).

Kazde ryzyko w notatce ma kotwice `plik:linia` z diffu runu, a sekcja `## Jak to sprawdzić`
jest obowiazkowa. Silnik odrzuca kotwice spoza diffu z tolerancja +-3 linie (`evidence_required`).

Prawdziwy kod: `sprawdz_kotwice` / `wyciagnij_kotwice` (czyste funkcje, pelne komunikaty),
`wykonaj_krok` z prawdziwym procesem (atrapa agenta z `test_wykonanie_kroku`, rola recenzenta
oddaje notatke na stdout) i `wykonaj_run` z wstrzyknietym agentem. Nazwy testow w jezyku
dziedziny (CLAUDE.md).
"""
import asyncio
import json
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola
from cockpit.services.walidacja import Naruszenie, Niezmiennik
from runner.agent import SpecKroku
from runner.kotwice import sprawdz_kotwice, wczytaj_zmienione_linie, wyciagnij_kotwice
from runner.krok import WynikKroku, wykonaj_krok
from test_krok_diff import SzpiegZmieniajacyDrzewo, zmien_drzewo
from test_silnik_runu import K, jedno, swiat, uruchom
from test_wykonanie_kroku import atrapa

EV = "niezmiennik evidence_required: "

ZMIENIONE = [
    {"plik": "src/a.py", "linie": [[10, 12]]},
    {"plik": "src/b.py", "linie": [[40, 41], [60, 60]]},
    {"plik": "src/usuniety.py", "linie": []},
]

RYZYKO_A = "- [src/a.py:11] scenariusz: Run przerwany w zapisie zostawi plik bez werdyktu."
RYZYKO_B = "- [src/b.py:40] scenariusz: Zly stan runu pokaze sie jako gotowy."


def _notatka(ryzyka: list[str] | None, jak: str | None = "Otwórz ekran odbioru i sprawdź stan.") -> str:
    """Notatka w ksztalcie explain-change; `None` pomija cala sekcje."""
    czesci = ["## Co zmieniono\n\nRun ma nowy stan.\n"]
    if ryzyka is not None:
        czesci.append("## Jakie ryzyko\n\n" + "\n".join(ryzyka) + "\n")
    if jak is not None:
        czesci.append("## Jak to sprawdzić\n\n" + jak + "\n")
    return "\n".join(czesci)


def _teksty(naruszenia: list[Naruszenie]) -> list[str]:
    return [str(n) for n in naruszenia]


# --- sprawdz_kotwice: komunikaty z kontraktu ---------------------------------------------------


def test_notatka_z_kotwicami_w_diffie_i_sekcja_jak_to_sprawdzic_nie_ma_naruszen():
    assert sprawdz_kotwice(_notatka([RYZYKO_A, RYZYKO_B]), ZMIENIONE) == []


def test_naruszenia_kotwic_dotycza_niezmiennika_evidence_required():
    wynik = sprawdz_kotwice(_notatka(None), ZMIENIONE)

    assert wynik == [Naruszenie(Niezmiennik.evidence_required, "brak sekcji ## Jakie ryzyko")]


def test_notatka_bez_sekcji_jakie_ryzyko_ma_naruszenie_braku_sekcji():
    wynik = sprawdz_kotwice(_notatka(None), ZMIENIONE)

    assert _teksty(wynik) == [EV + "brak sekcji ## Jakie ryzyko"]


def test_sekcja_ryzyka_z_samym_opisem_bez_pozycji_listy_ma_naruszenie_braku_ryzyka():
    wynik = sprawdz_kotwice(_notatka(["Nie znalazlem ryzyka."]), ZMIENIONE)

    assert _teksty(wynik) == [EV + "brak ryzyka w sekcji ## Jakie ryzyko"]


def test_pusta_sekcja_ryzyka_ma_naruszenie_braku_ryzyka():
    wynik = sprawdz_kotwice(_notatka([""]), ZMIENIONE)

    assert _teksty(wynik) == [EV + "brak ryzyka w sekcji ## Jakie ryzyko"]


def test_ryzyko_bez_kotwicy_na_poczatku_pozycji_cytuje_tekst_pozycji_bez_myslnika():
    wynik = sprawdz_kotwice(
        _notatka(["- scenariusz: Run bez kotwicy zepsuje odbior."]), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "ryzyko bez kotwicy plik:linia: scenariusz: Run bez kotwicy zepsuje odbior."]


def test_kotwica_w_srodku_pozycji_nie_jest_kotwica_ryzyka():
    pozycja = "- scenariusz: Zmiana w [src/a.py:11] zepsuje odbior."

    wynik = sprawdz_kotwice(_notatka([pozycja]), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "ryzyko bez kotwicy plik:linia: scenariusz: Zmiana w [src/a.py:11] zepsuje odbior."]


def test_kotwica_z_nienumeryczna_linia_to_ryzyko_bez_kotwicy():
    wynik = sprawdz_kotwice(
        _notatka(["- [src/a.py:abc] scenariusz: Cos sie zepsuje."]), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "ryzyko bez kotwicy plik:linia: [src/a.py:abc] scenariusz: Cos sie zepsuje."]


def test_kotwica_pliku_spoza_diffu_ma_naruszenie_pliku_bez_zmienionych_linii():
    wynik = sprawdz_kotwice(_notatka(["- [src/c.py:5] scenariusz: Cos sie zepsuje."]), ZMIENIONE)

    assert _teksty(wynik) == [EV + "kotwica src/c.py:5: plik bez zmienionych linii w diffie"]


def test_kotwica_pliku_z_pustymi_liniami_w_diffie_ma_naruszenie_pliku_bez_zmienionych_linii():
    # usuniety albo binarny plik jest w diffie, ale nie ma w nim dodanych linii
    wynik = sprawdz_kotwice(
        _notatka(["- [src/usuniety.py:1] scenariusz: Cos sie zepsuje."]), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "kotwica src/usuniety.py:1: plik bez zmienionych linii w diffie"]


def test_nazwa_pliku_kotwicy_musi_byc_cala_sciezka_z_diffu_a_nie_jej_koncowka():
    wynik = sprawdz_kotwice(_notatka(["- [a.py:11] scenariusz: Cos sie zepsuje."]), ZMIENIONE)

    assert _teksty(wynik) == [EV + "kotwica a.py:11: plik bez zmienionych linii w diffie"]


def test_kotwica_z_dwukropkiem_w_sciezce_to_ryzyko_bez_kotwicy():
    zmienione = [{"plik": "a:b.py", "linie": [[1, 2]]}]
    pozycja = "[a:b.py:1] scenariusz: Cos sie zepsuje."

    wynik = sprawdz_kotwice(_notatka(["- " + pozycja]), zmienione)

    assert _teksty(wynik) == [EV + "ryzyko bez kotwicy plik:linia: " + pozycja]


def test_kotwica_z_otwierajacym_nawiasem_w_sciezce_to_ryzyko_bez_kotwicy():
    pozycja = "[[src/a.py:11] scenariusz: Cos sie zepsuje."

    wynik = sprawdz_kotwice(_notatka(["- " + pozycja]), ZMIENIONE)

    assert _teksty(wynik) == [EV + "ryzyko bez kotwicy plik:linia: " + pozycja]


def test_kotwica_z_zamykajacym_nawiasem_w_sciezce_to_ryzyko_bez_kotwicy():
    zmienione = [{"plik": "src/a]b.py", "linie": [[10, 12]]}]
    pozycja = "[src/a]b.py:11] scenariusz: Cos sie zepsuje."

    wynik = sprawdz_kotwice(_notatka(["- " + pozycja]), zmienione)

    assert _teksty(wynik) == [EV + "ryzyko bez kotwicy plik:linia: " + pozycja]


def test_kotwica_bez_zamykajacego_nawiasu_po_linii_to_ryzyko_bez_kotwicy():
    pozycja = "[src/a.py:11x] scenariusz: Cos sie zepsuje."

    wynik = sprawdz_kotwice(_notatka(["- " + pozycja]), ZMIENIONE)

    assert _teksty(wynik) == [EV + "ryzyko bez kotwicy plik:linia: " + pozycja]


def test_kotwica_daleko_od_zmienionych_linii_ma_naruszenie_z_tolerancja_trzy():
    wynik = sprawdz_kotwice(_notatka(["- [src/a.py:90] scenariusz: Cos sie zepsuje."]), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "kotwica src/a.py:90: linia poza zmienionymi liniami (tolerancja 3)"]


# Lista przypadkow stala i niezalezna od kodu: zakres [10, 12] daje okno [7, 15].
@pytest.mark.parametrize(
    "linia, przechodzi",
    [(7, True), (15, True), (6, False), (16, False), (10, True), (12, True)],
)
def test_granice_tolerancji_wokol_zakresu_to_trzy_linie_w_obie_strony(linia, przechodzi):
    ryzyko = f"- [src/a.py:{linia}] scenariusz: Cos sie zepsuje."

    wynik = sprawdz_kotwice(_notatka([ryzyko]), ZMIENIONE)

    if przechodzi:
        assert wynik == []
    else:
        assert _teksty(wynik) == [
            EV + f"kotwica src/a.py:{linia}: linia poza zmienionymi liniami (tolerancja 3)"]


# Zakresy src/b.py: [40, 41] -> okno [37, 44]; [60, 60] -> okno [57, 63]; miedzy oknami luka.
@pytest.mark.parametrize(
    "linia, przechodzi",
    [(37, True), (44, True), (57, True), (63, True), (36, False), (45, False),
     (50, False), (56, False), (64, False)],
)
def test_kotwica_przechodzi_gdy_miesci_sie_w_oknie_dowolnego_zakresu_pliku(linia, przechodzi):
    ryzyko = f"- [src/b.py:{linia}] scenariusz: Cos sie zepsuje."

    wynik = sprawdz_kotwice(_notatka([ryzyko]), ZMIENIONE)

    if przechodzi:
        assert wynik == []
    else:
        assert _teksty(wynik) == [
            EV + f"kotwica src/b.py:{linia}: linia poza zmienionymi liniami (tolerancja 3)"]


def test_tolerancja_kotwicy_liczy_sie_tylko_z_zakresow_jej_pliku_a_nie_innych_plikow():
    # linia 11 lezy w zakresie src/a.py, ale src/b.py ma zakresy dopiero od 40
    wynik = sprawdz_kotwice(_notatka(["- [src/b.py:11] scenariusz: Cos sie zepsuje."]), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "kotwica src/b.py:11: linia poza zmienionymi liniami (tolerancja 3)"]


def test_przy_kilku_ryzykach_tylko_zle_ma_naruszenie_z_nazwa_swojej_kotwicy():
    ryzyka = [RYZYKO_A, "- [src/b.py:50] scenariusz: Cos sie zepsuje.", RYZYKO_B]

    wynik = sprawdz_kotwice(_notatka(ryzyka), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "kotwica src/b.py:50: linia poza zmienionymi liniami (tolerancja 3)"]


def test_naruszenia_ryzyk_sa_w_kolejnosci_pozycji_a_naruszenie_jak_to_sprawdzic_na_koncu():
    ryzyka = [
        "- [src/a.py:90] scenariusz: Pierwsze.",
        "- scenariusz: Drugie bez kotwicy.",
        "- [src/c.py:1] scenariusz: Trzecie.",
    ]

    wynik = sprawdz_kotwice(_notatka(ryzyka, jak=None), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "kotwica src/a.py:90: linia poza zmienionymi liniami (tolerancja 3)",
        EV + "ryzyko bez kotwicy plik:linia: scenariusz: Drugie bez kotwicy.",
        EV + "kotwica src/c.py:1: plik bez zmienionych linii w diffie",
        EV + "brak sekcji ## Jak to sprawdzić",
    ]


def test_notatka_bez_obu_sekcji_ma_dwa_naruszenia_najpierw_ryzyko_potem_jak_to_sprawdzic():
    wynik = sprawdz_kotwice(_notatka(None, jak=None), ZMIENIONE)

    assert _teksty(wynik) == [
        EV + "brak sekcji ## Jakie ryzyko",
        EV + "brak sekcji ## Jak to sprawdzić",
    ]


def test_kotwica_pliku_z_myslnikiem_podkresleniem_i_wielka_litera_w_sciezce_jest_rozpoznana():
    zmienione = [{"plik": "web/src/Widok-Runu_v2.tsx", "linie": [[105, 107]]}]
    ryzyko = "- [web/src/Widok-Runu_v2.tsx:105] scenariusz: Cos sie zepsuje."

    assert sprawdz_kotwice(_notatka([ryzyko]), zmienione) == []
    assert wyciagnij_kotwice(_notatka([ryzyko])) == ["web/src/Widok-Runu_v2.tsx:105"]


# --- sekcja "Jak to sprawdzić" i granice sekcji ------------------------------------------------


def test_notatka_bez_sekcji_jak_to_sprawdzic_ma_naruszenie_braku_sekcji():
    wynik = sprawdz_kotwice(_notatka([RYZYKO_A], jak=None), ZMIENIONE)

    assert _teksty(wynik) == [EV + "brak sekcji ## Jak to sprawdzić"]


def test_sekcja_jak_to_sprawdzic_z_samymi_pustymi_liniami_przed_nastepnym_naglowkiem_jest_pusta():
    notatka = _notatka([RYZYKO_A], jak="\n   \n") + "\n## Uwagi\n\nTekst.\n"

    wynik = sprawdz_kotwice(notatka, ZMIENIONE)

    assert _teksty(wynik) == [EV + "pusta sekcja ## Jak to sprawdzić"]


def test_sekcja_jak_to_sprawdzic_bez_zadnej_tresci_na_koncu_notatki_jest_pusta():
    notatka = _notatka([RYZYKO_A], jak=None) + "\n## Jak to sprawdzić"

    wynik = sprawdz_kotwice(notatka, ZMIENIONE)

    assert _teksty(wynik) == [EV + "pusta sekcja ## Jak to sprawdzić"]


def test_tresc_sekcji_jak_to_sprawdzic_po_pustej_linii_liczy_sie_jako_niepusta():
    notatka = _notatka([RYZYKO_A], jak="\n\nOtwórz ekran odbioru.")

    assert sprawdz_kotwice(notatka, ZMIENIONE) == []


def test_pozycja_listy_w_sekcji_jak_to_sprawdzic_nie_jest_ryzykiem_bez_kotwicy():
    notatka = _notatka([RYZYKO_A], jak="- Otwórz ekran odbioru.\n- Sprawdź stan runu.")

    assert sprawdz_kotwice(notatka, ZMIENIONE) == []


def test_naglowek_kolejnej_sekcji_konczy_sekcje_ryzyka_i_jej_pozycje_nie_sa_ryzykami():
    notatka = _notatka([RYZYKO_A]) + "\n## Uwagi\n\n- uwaga bez kotwicy\n- [src/zle.py:1] cos\n"

    assert sprawdz_kotwice(notatka, ZMIENIONE) == []


def test_kotwica_z_innej_sekcji_niz_ryzyko_nie_jest_sprawdzana():
    notatka = (
        "## Co zmieniono\n\n- [src/zle.py:1] scenariusz: To nie jest ryzyko.\n\n"
        "## Jakie ryzyko\n\n" + RYZYKO_A + "\n\n## Jak to sprawdzić\n\nKrok.\n"
    )

    assert sprawdz_kotwice(notatka, ZMIENIONE) == []


def test_tresc_w_pierwszej_linii_pod_naglowkiem_bez_pustej_linii_nalezy_do_sekcji():
    notatka = (
        "## Co zmieniono\n\nRun ma nowy stan.\n"
        "## Jakie ryzyko\n" + RYZYKO_A + "\n"
        "## Jak to sprawdzić\nOtwórz ekran odbioru.\n"
    )

    assert sprawdz_kotwice(notatka, ZMIENIONE) == []
    assert wyciagnij_kotwice(notatka) == ["src/a.py:11"]


def test_naglowki_sekcji_ze_spacjami_na_koncu_linii_sa_rozpoznawane():
    notatka = (
        "## Co zmieniono\n\nRun ma nowy stan.\n\n"
        "## Jakie ryzyko  \n\n" + RYZYKO_A + "\n\n"
        "## Jak to sprawdzić \t\n\nOtwórz ekran odbioru.\n"
    )

    assert sprawdz_kotwice(notatka, ZMIENIONE) == []
    assert wyciagnij_kotwice(notatka) == ["src/a.py:11"]


# --- wyciagnij_kotwice -------------------------------------------------------------------------


def test_kotwice_ryzyk_sa_w_kolejnosci_wystapienia_w_notatce_a_nie_w_kolejnosci_plikow():
    notatka = _notatka([RYZYKO_B, "- scenariusz: Bez kotwicy.", RYZYKO_A])

    assert wyciagnij_kotwice(notatka) == ["src/b.py:40", "src/a.py:11"]


def test_kotwice_spoza_sekcji_ryzyka_nie_trafiaja_na_liste():
    notatka = (
        "## Co zmieniono\n\n- [src/zle.py:1] cos\n\n"
        "## Jakie ryzyko\n\n" + RYZYKO_A + "\n\n"
        "## Jak to sprawdzić\n\n- [src/zle.py:2] cos\n"
    )

    assert wyciagnij_kotwice(notatka) == ["src/a.py:11"]


def test_notatka_bez_sekcji_ryzyka_nie_ma_kotwic():
    assert wyciagnij_kotwice(_notatka(None)) == []


# --- wykonaj_krok z prawdziwym procesem --------------------------------------------------------


KROK = "krok-23"
BRAK_LINII = EV + "brak czytelnego zmienione-linie.json dla notatka-zmiany.md"


def spec_notatki(rola: Rola = Rola.reviewer, wyjscia: list[str] | None = None) -> SpecKroku:
    return SpecKroku(
        run_id="run-17", step_id=KROK, agent_id="agent-31", skill_id="explain-change",
        rola=rola, wejscia=[],
        wyjscia=list(wyjscia if wyjscia is not None else ["notatka-zmiany.md"]),
        komendy={},
    )


def wypisz(tresc: str) -> list[str]:
    # bajty, nie tekst: atrapa nie zalezy od kodowania stdout srodowiska
    return atrapa(bajty=tresc.encode("utf-8").hex())


@pytest.fixture
def katalog(tmp_path: Path) -> Path:
    k = tmp_path / "repo"
    k.mkdir()
    return k


@pytest.fixture
def katalog_runu(tmp_path: Path) -> Path:
    k = tmp_path / "run"
    k.mkdir()
    return k


@pytest.fixture
def plik_linii(tmp_path: Path) -> Path:
    plik = tmp_path / "zmienione-linie.json"
    plik.write_text(json.dumps(ZMIENIONE), encoding="utf-8")
    return plik


async def krok(spec, polecenie, katalog, katalog_runu, **reszta) -> WynikKroku:
    async def emit(zdarzenie: dict) -> None:
        pass

    return await asyncio.wait_for(
        wykonaj_krok(spec, polecenie, katalog, emit, katalog_runu=katalog_runu, **reszta), 30)


async def test_notatka_z_kotwicami_w_diffie_konczy_krok_sukcesem_i_niesie_kotwice(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_B, RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=plik_linii)

    assert wynik == WynikKroku(
        "zakonczony", None, 0, kotwice=("src/b.py:40", "src/a.py:11"))
    assert (katalog_runu / KROK / "notatka-zmiany.md").is_file()


async def test_kotwica_spoza_diffu_konczy_krok_porazka_z_pelnym_powodem_i_nazwa_kotwicy(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec_notatki(),
        wypisz(_notatka(["- [src/a.py:90] scenariusz: Cos sie zepsuje."])),
        katalog, katalog_runu, zmienione_linie=plik_linii)

    assert wynik.status == "nieudany"
    assert wynik.powod == EV + "kotwica src/a.py:90: linia poza zmienionymi liniami (tolerancja 3)"
    assert wynik.kod_wyjscia == 0       # agent skonczyl sie kodem 0; porazka pochodzi z notatki
    assert wynik.kotwice == ()


async def test_kilka_naruszen_notatki_jest_w_powodzie_porazki_polaczonych_srednikiem(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec_notatki(),
        wypisz(_notatka(["- [src/a.py:90] scenariusz: Cos sie zepsuje."], jak=None)),
        katalog, katalog_runu, zmienione_linie=plik_linii)

    assert wynik.status == "nieudany"
    assert wynik.powod == (
        EV + "kotwica src/a.py:90: linia poza zmienionymi liniami (tolerancja 3); "
        + EV + "brak sekcji ## Jak to sprawdzić")


async def test_notatka_bez_sekcji_jak_to_sprawdzic_konczy_krok_porazka(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A], jak=None)), katalog, katalog_runu,
        zmienione_linie=plik_linii)

    assert (wynik.status, wynik.powod) == ("nieudany", EV + "brak sekcji ## Jak to sprawdzić")


async def test_brak_sciezki_do_zmienionych_linii_konczy_krok_notatki_porazka_z_brakiem_dowodu(
    katalog, katalog_runu
):
    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=None)

    assert (wynik.status, wynik.powod) == ("nieudany", BRAK_LINII)


async def test_wywolanie_bez_kwargu_zmienione_linie_tez_konczy_krok_notatki_porazka(
    katalog, katalog_runu
):
    wynik = await krok(spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu)

    assert (wynik.status, wynik.powod) == ("nieudany", BRAK_LINII)


async def test_nieistniejacy_plik_zmienionych_linii_konczy_krok_notatki_porazka(
    tmp_path, katalog, katalog_runu
):
    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=tmp_path / "nie-ma.json")

    assert (wynik.status, wynik.powod) == ("nieudany", BRAK_LINII)


async def test_katalog_zamiast_pliku_zmienionych_linii_konczy_krok_notatki_porazka(
    tmp_path, katalog, katalog_runu
):
    katalog_linii = tmp_path / "zmienione-linie.json"
    katalog_linii.mkdir()

    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=katalog_linii)

    assert (wynik.status, wynik.powod) == ("nieudany", BRAK_LINII)


# Lista zlych zawartosci pliku zmienionych linii, stala i niezalezna od kodu; kazda wartosc jest
# odrzucana tylko przez swoja regule ksztaltu (lista slownikow z `plik: str`, `linie: [[int, int]]`).
ZLE_ZAWARTOSCI = [
    pytest.param(b"to nie jest json", id="nie-json"),
    pytest.param(b"\xff\xfe\x00zle bajty", id="nie-utf8"),
    pytest.param(b"[" * 200_000, id="zbyt-gleboko-zagniezdzony"),
    pytest.param(b"", id="pusty-plik"),
    pytest.param(b'{"plik": "src/a.py", "linie": [[10, 12]]}', id="slownik-zamiast-listy"),
    pytest.param(b"{}", id="pusty-slownik-zamiast-listy"),
    pytest.param(b"5", id="liczba-zamiast-listy"),
    pytest.param(b"null", id="null-zamiast-listy"),
    pytest.param(b'"tekst"', id="napis-zamiast-listy"),
    pytest.param(b'[["src/a.py", [[10, 12]]]]', id="element-nie-slownik"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[10, 12]]}, 5]', id="drugi-element-nie-slownik"),
    pytest.param(b'[{"linie": [[10, 12]]}]', id="bez-pola-plik"),
    pytest.param(b'[{"plik": 5, "linie": [[10, 12]]}]', id="plik-liczba"),
    pytest.param(b'[{"plik": "src/a.py"}]', id="bez-pola-linie"),
    pytest.param(b'[{"plik": "src/a.py", "linie": "10-12"}]', id="linie-napis"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [10, 12]}]', id="linie-plaska-lista"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[10]]}]', id="zakres-z-jedna-liczba"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[10, 12, 14]]}]', id="zakres-z-trzema-liczbami"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[true, 12]]}]', id="bool-na-poczatku-zakresu"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[10, true]]}]', id="bool-na-koncu-zakresu"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [["10", 12]]}]', id="napis-na-poczatku-zakresu"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[10, "12"]]}]', id="napis-na-koncu-zakresu"),
    pytest.param(b'[{"plik": "src/a.py", "linie": [[10.5, 12]]}]', id="ulamek-w-zakresie"),
    pytest.param(
        b'[{"plik": "src/a.py", "linie": [[10, 12]]}, {"plik": "src/b.py", "linie": [[1, true]]}]',
        id="zly-ksztalt-dopiero-w-drugim-pliku"),
]


@pytest.mark.parametrize("zawartosc", ZLE_ZAWARTOSCI)
async def test_nieczytelny_albo_zle_uksztaltowany_plik_zmienionych_linii_konczy_krok_notatki_porazka(
    tmp_path, katalog, katalog_runu, zawartosc
):
    plik = tmp_path / "zle.json"
    plik.write_bytes(zawartosc)

    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=plik)

    assert (wynik.status, wynik.powod) == ("nieudany", BRAK_LINII)


async def test_poprawna_lista_zmienionych_linii_bez_plikow_jest_czytelna_a_kotwica_nie_ma_pliku(
    tmp_path, katalog, katalog_runu
):
    plik = tmp_path / "puste.json"
    plik.write_text("[]", encoding="utf-8")

    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=plik)

    assert (wynik.status, wynik.powod) == (
        "nieudany", EV + "kotwica src/a.py:11: plik bez zmienionych linii w diffie")


async def test_plik_z_pustymi_liniami_w_zmienionych_liniach_jest_czytelny(
    tmp_path, katalog, katalog_runu
):
    plik = tmp_path / "usuniety.json"
    plik.write_text(
        json.dumps([{"plik": "src/usuniety.py", "linie": []}, *ZMIENIONE[:1]]), encoding="utf-8")

    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=plik)

    assert (wynik.status, wynik.powod, wynik.kotwice) == ("zakonczony", None, ("src/a.py:11",))


async def test_pusty_wynik_recenzenta_to_brak_dowodu_a_nie_blad_zmienionych_linii(
    katalog, katalog_runu
):
    wynik = await krok(
        spec_notatki(), wypisz("   \n"), katalog, katalog_runu, zmienione_linie=None)

    assert (wynik.status, wynik.powod) == (
        "nieudany", EV + "pusty wynik agenta, brak artefaktu notatka-zmiany.md")


async def test_kod_wyjscia_agenta_rozny_od_zera_ma_pierwszenstwo_przed_kotwicami(
    katalog, katalog_runu
):
    wynik = await krok(
        spec_notatki(), atrapa(bajty=_notatka([RYZYKO_A]).encode().hex(), kod=3), katalog,
        katalog_runu, zmienione_linie=None)

    assert (wynik.status, wynik.powod) == ("nieudany", "kod wyjścia 3")


async def test_skill_ktory_nie_produkuje_notatki_zmiany_ignoruje_zmienione_linie(
    tmp_path, katalog, katalog_runu
):
    # ani brak sciezki, ani zepsuty plik, ani tresc bez kotwic nie psuja innego artefaktu
    zepsuty = tmp_path / "zepsuty.json"
    zepsuty.write_bytes(b"to nie jest json")

    for zmienione in (None, zepsuty):
        wynik = await krok(
            spec_notatki(wyjscia=["zastrzezenia.md"]), wypisz("WERDYKT: brak zastrzeżeń\n"),
            katalog, katalog_runu, zmienione_linie=zmienione)

        assert (wynik.status, wynik.powod, wynik.kotwice) == ("zakonczony", None, ())


async def test_notatka_zapisana_przez_autora_w_kopii_roboczej_tez_podlega_sprawdzeniu_kotwic(
    katalog, katalog_runu, plik_linii
):
    # rola z prawem zapisu zapisuje artefakt sama; kotwica spoza diffu nadal jest brakiem dowodu
    zle = _notatka(["- [src/a.py:90] scenariusz: Cos sie zepsuje."])
    polecenie = atrapa(zapisz={str(katalog / "notatka-zmiany.md"): zle})

    wynik = await krok(
        spec_notatki(Rola.author), polecenie, katalog, katalog_runu, zmienione_linie=plik_linii)

    assert wynik.status == "nieudany"
    assert wynik.powod == EV + "kotwica src/a.py:90: linia poza zmienionymi liniami (tolerancja 3)"


async def test_wynik_kroku_bez_kotwic_ma_domyslnie_pusta_krotke_kotwic():
    assert WynikKroku("zakonczony", None, 0).kotwice == ()


@pytest.fixture
def kodowanie_srodowiska_ascii(monkeypatch):
    """Symuluje srodowisko z kodowaniem innym niz UTF-8: `read_text` bez jawnego kodowania czyta ASCII."""
    oryginal = Path.read_text

    def read_text(self, encoding=None, errors=None, newline=None):
        return oryginal(self, encoding="ascii" if encoding is None else encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_zmienione_linie_z_polskimi_znakami_w_nazwie_pliku_sa_czytane_jako_utf8_niezaleznie_od_srodowiska(
    tmp_path, kodowanie_srodowiska_ascii
):
    plik = tmp_path / "zmienione-linie.json"
    plik.write_text(
        json.dumps([{"plik": "zażółć.py", "linie": [[1, 2]]}], ensure_ascii=False),
        encoding="utf-8")

    assert wczytaj_zmienione_linie(plik) == [{"plik": "zażółć.py", "linie": [[1, 2]]}]


async def test_blad_odczytu_notatki_konczy_krok_porazka_z_przyczyna(
    katalog, katalog_runu, plik_linii, monkeypatch
):
    oryginal = Path.read_text

    def read_text(self, encoding=None, errors=None, newline=None):
        # tylko odczyt notatki do oceny kotwic (jawne UTF-8); dowod z pliku idzie bez kodowania
        if self.name == "notatka-zmiany.md" and encoding == "utf-8":
            raise OSError("odmowa dostepu")
        return oryginal(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)

    wynik = await krok(
        spec_notatki(), wypisz(_notatka([RYZYKO_A])), katalog, katalog_runu,
        zmienione_linie=plik_linii)

    assert wynik.status == "nieudany"
    assert wynik.powod == "nie można odczytać artefaktu notatka-zmiany.md: odmowa dostepu"
    assert wynik.kod_wyjscia == 0
    assert wynik.kotwice == ()


async def test_notatka_z_polskimi_znakami_jest_czytana_jako_utf8_niezaleznie_od_srodowiska(
    tmp_path, katalog, katalog_runu, kodowanie_srodowiska_ascii
):
    plik = tmp_path / "zmienione-linie.json"
    plik.write_text(
        json.dumps([{"plik": "zażółć.py", "linie": [[1, 2]]}], ensure_ascii=False),
        encoding="utf-8")
    notatka = _notatka(["- [zażółć.py:1] scenariusz: Zażółcenie zepsuje odbiór."])

    wynik = await krok(
        spec_notatki(), wypisz(notatka), katalog, katalog_runu, zmienione_linie=plik)

    assert wynik == WynikKroku("zakonczony", None, 0, kotwice=("zażółć.py:1",))


async def test_notatka_zapisana_przez_autora_z_niepoprawnym_utf8_jest_czytana_z_zastapieniem_znaku(
    katalog, katalog_runu, plik_linii
):
    # bajt 0xFF nie jest poprawnym UTF-8; notatka i tak jest sprawdzana, a nie wywala kroku
    tresc = _notatka([RYZYKO_A]).replace("Run ma nowy stan.", "Run ma nowy stan \x00")
    (katalog / "notatka-zmiany.md").write_bytes(tresc.encode("utf-8").replace(b"\x00", b"\xff"))

    wynik = await krok(
        spec_notatki(Rola.author), atrapa(), katalog, katalog_runu, zmienione_linie=plik_linii)

    assert wynik == WynikKroku("zakonczony", None, 0, kotwice=("src/a.py:11",))


# --- silnik: przekazanie zmienionych linii i kotwic -------------------------------------------


WORKFLOW_NOTATKI = """\
name: w
steps:
  autor: {skill: s_autor}
  roznica: {system: diff, needs: [autor]}
  opis: {skill: s_opis, needs: [roznica]}
  rec: {skill: s_rec, needs: [roznica]}
"""

SKILLE_NOTATKI = {
    "autor": {},
    "opis": {
        "role": "reviewer",
        "consumes": ["zmiana.diff", "zmienione-linie.json"],
        "produces": ["notatka-zmiany.md"],
    },
    "rec": {"role": "reviewer", "consumes": ["zmiana.diff"], "produces": ["zastrzezenia.md"]},
}


def swiat_notatki(tmp_path: Path, tresc: str = WORKFLOW_NOTATKI):
    return swiat(tmp_path, [K("autor"), K("opis"), K("rec")], tresc=tresc, opcje_skilli=SKILLE_NOTATKI)


async def test_silnik_podaje_zmienione_linie_z_kroku_diff_tylko_krokowi_produkujacemu_notatke(
    tmp_path,
):
    s = swiat_notatki(tmp_path)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    await uruchom(s, agent)

    dodatkowe = {w.spec.step_id: w.dodatkowe for w in agent.wywolania}
    assert set(dodatkowe) == {"autor", "opis", "rec"}
    assert Path(dodatkowe["opis"]["zmienione_linie"]) == (
        s.katalog_runu / "roznica" / "zmienione-linie.json")
    assert set(dodatkowe["opis"]) == {"zmienione_linie"}
    assert dodatkowe["autor"] == {}
    assert dodatkowe["rec"] == {}


async def test_zdarzenie_konca_kroku_notatki_niesie_kotwice_z_wyniku_kroku(tmp_path):
    s = swiat_notatki(tmp_path)
    wyniki = {"opis": WynikKroku("zakonczony", None, 0, kotwice=("README.md:2", "nowy.txt:1"))}
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo}, wyniki=wyniki)

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_finished", "opis")["kotwice"] == ["README.md:2", "nowy.txt:1"]


async def test_zdarzenie_konca_kroku_notatki_bez_kotwic_ma_pusta_liste_kotwic(tmp_path):
    s = swiat_notatki(tmp_path)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    zdarzenia = await uruchom(s, agent)

    koniec = jedno(zdarzenia, "step_finished", "opis")
    assert "kotwice" in koniec
    assert koniec["kotwice"] == []


async def test_zdarzenia_konca_krokow_bez_notatki_zmiany_nie_maja_klucza_kotwice(tmp_path):
    s = swiat_notatki(tmp_path)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    zdarzenia = await uruchom(s, agent)

    for krok_bez_notatki in ("autor", "roznica", "rec"):
        assert "kotwice" not in jedno(zdarzenia, "step_finished", krok_bez_notatki)


async def test_zdarzenie_porazki_kroku_notatki_nie_ma_klucza_kotwice(tmp_path):
    s = swiat_notatki(tmp_path)
    wyniki = {"opis": WynikKroku("nieudany", "kod wyjścia 1", 1, kotwice=("README.md:2",))}
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo}, wyniki=wyniki)

    zdarzenia = await uruchom(s, agent)

    porazka = jedno(zdarzenia, "step_failed", "opis")
    assert porazka["text"] == "kod wyjścia 1"
    assert "kotwice" not in porazka


class AgentZPrawdziwymKrokiem:
    """Autor zmienia drzewo; krok notatki idzie przez prawdziwe `wykonaj_krok` z atrapa-procesem."""

    def __init__(self, notatka: str) -> None:
        self.notatka = notatka

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, **dodatkowe):
        if spec.step_id == "autor":
            zmien_drzewo(katalog)
            return WynikKroku("zakonczony", None, 0)
        if spec.step_id == "opis":
            # silnik podaje prompt, wiec krok idzie w trybie agenta (HAR-174): wynik recenzenta
            # przychodzi jako zdarzenie `result` strumienia stream-json, nie surowy tekst
            wynik = json.dumps({"type": "result", "subtype": "success", "result": self.notatka})
            return await wykonaj_krok(
                spec, wypisz(wynik + "\n"), katalog, emit,
                katalog_runu=katalog_runu, **dodatkowe)
        return WynikKroku("zakonczony", None, 0)


async def test_run_z_kotwicami_z_diffu_konczy_krok_notatki_a_kotwice_trafiaja_do_zdarzenia(
    tmp_path,
):
    s = swiat_notatki(tmp_path)
    # README.md ma dodane linie 2-3, nowy.txt 1-2 (zmien_drzewo)
    notatka = _notatka([
        "- [README.md:2] scenariusz: Nowy opis zmyli czytelnika.",
        "- [nowy.txt:1] scenariusz: Nowy plik trafi do odbioru.",
    ])

    zdarzenia = await uruchom(s, AgentZPrawdziwymKrokiem(notatka))

    koniec = jedno(zdarzenia, "step_finished", "opis")
    assert koniec["kotwice"] == ["README.md:2", "nowy.txt:1"]
    assert (s.katalog_runu / "opis" / "notatka-zmiany.md").read_text(encoding="utf-8") == notatka


async def test_run_z_kotwica_spoza_diffu_konczy_krok_notatki_porazka_a_run_idzie_dalej(tmp_path):
    s = swiat_notatki(tmp_path)
    notatka = _notatka(["- [README.md:40] scenariusz: Nowy opis zmyli czytelnika."])

    zdarzenia = await uruchom(s, AgentZPrawdziwymKrokiem(notatka))

    assert jedno(zdarzenia, "step_failed", "opis")["text"] == (
        EV + "kotwica README.md:40: linia poza zmienionymi liniami (tolerancja 3)")
    jedno(zdarzenia, "step_finished", "rec")
    assert zdarzenia[-1]["kind"] == "run_finished"


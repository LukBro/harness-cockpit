"""HAR-168: capability `mutate_diff` - przezyle mutanty na zmienionych liniach jako dowod kroku (TDD).

Krok `system: check` z `capability: mutate_diff` uruchamia komende z project.yaml
(`python tools/raport_mutantow.py`), ktora wypisuje na stdout raport JSON
`{"zbadane": int, "przezyle": [{"plik","linia","opis_mutanta"}], "kod_narzedzia": int,
"poza_zasiegiem": [{"plik","linia"}]}` (klucz `poza_zasiegiem` opcjonalny na wejsciu, niepusty
zawsze przy kodzie narzedzia 3, dozwolony przy 0 i 1, w `mutanty.json` zapisywany zawsze).
Silnik parsuje go z kontrola typow i kluczy, zapisuje `mutanty.json` w katalogu kroku i:
- `zbadane == 0` przy niepustym diffie (jakikolwiek wpis w `zmienione-linie.json` z kroku `diff`,
  takze z pusta lista `linie`) to blad kroku `evidence_required`; tylko pusta lista `[]` dopuszcza
  zero zbadanych,
- przezyle mutanty oblewaja krok tylko przy `fail_on_survivors: true` na kroku.

Prawdziwy kod: `runner.dowody` na prawdziwych procesach (komendy `python -c` zamiast raportu;
prawdziwego mutmuta ani `tools/mutanty_zmian.py` testy NIE uruchamiaja) oraz `wykonaj_run`
z wstrzyknietym agentem. Importy nowych nazw sa leniwe: brak implementacji czerwieni kazdy test
osobno, nie wywraca kolekcji pliku.

Oczekiwany interfejs `runner.dowody` (do implementacji przez backend):
- `wykonaj_mutate_diff(worktree: Path, komenda: str, katalog_kroku: Path, plik_linii: Path,
  *, fail_on_survivors: bool = False, limit_s: float = LIMIT_MUTATE_DIFF_S) -> WynikKroku`
- `parsuj_raport_mutantow(tekst: str) -> dict` (rzuca `BladRaportuMutantow`)
- `BladRaportuMutantow(BladDziedziny)`
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from procesy import czekaj
from test_krok_check import (
    SzpiegZmieniajacyDrzewo,
    komenda,
    pliki_pod,
    projekt_z,
)
from test_silnik_runu import K, Szpieg, jedno, rodzaje, sprawdz_blad_przed_startem, swiat, uruchom

LIMIT_TESTU_S = 60.0
CAPABILITY = "mutate_diff"
PLIK_DOWODU = "dowod-mutate_diff.json"
PLIK_MUTANTOW = "mutanty.json"
KLUCZE_RAPORTU = {"zbadane", "przezyle", "kod_narzedzia", "poza_zasiegiem"}
KLUCZE_PRZEZYLEGO = {"plik", "linia", "opis_mutanta"}

PRZEZYL_A = {"plik": "src/cockpit/a.py", "linia": 12, "opis_mutanta": "-    return x\n+    return None"}
PRZEZYL_B = {"plik": "src/runner/b.py", "linia": 7, "opis_mutanta": "-    if a:\n+    if not a:"}
POZA_A = {"plik": "src/cockpit/c.py", "linia": 3}
POZA_B = {"plik": "src/runner/d.py", "linia": 40}
LINIE_NIEPUSTE = [{"plik": "src/cockpit/a.py", "linie": [[10, 14]]}]

KSZTALT_RAPORTU = (
    "raport mutantow: oczekiwano obiektu z kluczami zbadane, przezyle, kod_narzedzia "
    "i opcjonalnie poza_zasiegiem")
BRAK_OBIEKTU = "raport mutantow: w wyjsciu komendy nie ma obiektu JSON"
ZLY_KOD = "raport mutantow: kod_narzedzia musi byc jednym z [0, 1, 3]"
EVIDENCE = "niezmiennik evidence_required: zero zbadanych mutantów przy zmienionych liniach"


def raport(zbadane: int = 3, przezyle=(), kod_narzedzia: int = 0, poza=None) -> dict:
    """Poprawny raport; przy kodzie 3 bez `poza` dostaje jeden wpis (kod 3 wymaga listy)."""
    if poza is None:
        poza = [POZA_A] if kod_narzedzia == 3 else []
    return {
        "zbadane": zbadane, "przezyle": [dict(p) for p in przezyle], "kod_narzedzia": kod_narzedzia,
        "poza_zasiegiem": [dict(p) for p in poza]}


def komenda_raportu(tekst: str, *, stderr: str = "") -> str:
    """Komenda capability: najpierw `stderr`, potem raport na stdout (ktorys program moze pisac
    ostrzezenia na stderr, a stderr komendy checku laczy sie ze stdout)."""
    kod = (
        "import sys; sys.stderr.write(sys.argv[2]); sys.stderr.flush(); "
        "sys.stdout.write(sys.argv[1]); sys.stdout.flush()"
    )
    return komenda(kod, tekst, stderr)


def komenda_raportu_json(r: dict, *, stderr: str = "") -> str:
    return komenda_raportu(json.dumps(r, ensure_ascii=False), stderr=stderr)


class Katalogi:
    def __init__(self, tmp_path: Path):
        self.worktree = tmp_path / "repo"
        self.worktree.mkdir()
        self.diff = tmp_path / "run" / "diff"
        self.diff.mkdir(parents=True)
        self.krok = tmp_path / "run" / "mutanty"
        self.plik_linii = self.diff / "zmienione-linie.json"

    def linie(self, zawartosc) -> None:
        self.plik_linii.write_text(json.dumps(zawartosc), encoding="utf-8")

    def mutanty(self) -> dict:
        return json.loads((self.krok / PLIK_MUTANTOW).read_text(encoding="utf-8"))


@pytest.fixture
def k(tmp_path: Path) -> Katalogi:
    return Katalogi(tmp_path)


async def mutuj(
    k: Katalogi, polecenie: str, *, fail_on_survivors: bool | None = None,
    limit_s: float | None = None,
):
    from runner.dowody import wykonaj_mutate_diff

    opcje = {} if fail_on_survivors is None else {"fail_on_survivors": fail_on_survivors}
    if limit_s is not None:
        opcje["limit_s"] = limit_s
    return await czekaj(
        wykonaj_mutate_diff(k.worktree, polecenie, k.krok, k.plik_linii, **opcje),
        LIMIT_TESTU_S, "wykonaj_mutate_diff")


# --- parser raportu: kontrola typow i kluczy ---------------------------------------------------


def test_poprawny_raport_jest_zwrocony_bez_zmian():
    from runner.dowody import parsuj_raport_mutantow

    r = raport(4, [PRZEZYL_A, PRZEZYL_B], 1)

    assert parsuj_raport_mutantow(json.dumps(r)) == r


@pytest.mark.parametrize("kod", [0, 1, 3])
def test_raport_przyjmuje_kody_narzedzia_zero_jeden_i_trzy(kod):
    from runner.dowody import parsuj_raport_mutantow

    assert parsuj_raport_mutantow(json.dumps(raport(2, kod_narzedzia=kod)))["kod_narzedzia"] == kod


def test_raport_z_kodem_trzy_zwraca_liste_poza_zasiegiem_w_kolejnosci_z_wejscia():
    from runner.dowody import parsuj_raport_mutantow

    r = raport(2, kod_narzedzia=3, poza=[POZA_B, POZA_A])

    assert parsuj_raport_mutantow(json.dumps(r))["poza_zasiegiem"] == [POZA_B, POZA_A]


def test_raport_bez_klucza_poza_zasiegiem_ma_pusta_liste_a_nie_blad():
    from runner.dowody import parsuj_raport_mutantow

    bez_klucza = {k: v for k, v in raport(2, [PRZEZYL_A], 1).items() if k != "poza_zasiegiem"}

    assert parsuj_raport_mutantow(json.dumps(bez_klucza)) == raport(2, [PRZEZYL_A], 1)


def test_raport_z_zerem_zbadanych_i_liniami_numerowanymi_od_jedynki_jest_poprawny():
    from runner.dowody import parsuj_raport_mutantow

    r = raport(0, [{**PRZEZYL_A, "linia": 1}], 1)
    z_poza = raport(0, kod_narzedzia=3, poza=[{**POZA_A, "linia": 1}])

    assert parsuj_raport_mutantow(json.dumps(r)) == r
    assert parsuj_raport_mutantow(json.dumps(z_poza)) == z_poza


@pytest.mark.parametrize("kod", [0, 1])
def test_raport_z_kodem_zero_i_jeden_moze_miec_niepusta_liste_poza_zasiegiem(kod):
    # kod 1 ma pierwszenstwo przed 3, wiec zmiany poza zasiegiem ida do raportu takze przy nim
    from runner.dowody import parsuj_raport_mutantow

    r = raport(2, kod_narzedzia=kod, poza=[POZA_B, POZA_A])

    assert parsuj_raport_mutantow(json.dumps(r)) == r


def test_raport_z_kodem_zero_i_pusta_lista_poza_zasiegiem_jest_poprawny():
    # bliźniak: kod 3 z ta sama pusta lista jest bledem (kod_trzy_z_pusta_lista)
    from runner.dowody import parsuj_raport_mutantow

    r = raport(2, kod_narzedzia=0, poza=[])

    assert parsuj_raport_mutantow(json.dumps(r)) == r


def test_wartosc_nie_bedaca_obiektem_o_tych_samych_nazwach_kluczy_nie_jest_raportem():
    # lista nazw kluczy ma ten sam zbior co obiekt raportu: odrzuca ja tylko kontrola typu.
    # Przez `parsuj_raport_mutantow` nie da sie jej podac (szuka obiektow od `{`), wiec sprawdzamy
    # funkcje, ktora decyduje o ksztalcie jednego obiektu.
    from runner.dowody import BladRaportuMutantow, _sprawdz_raport

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        _sprawdz_raport(["zbadane", "przezyle", "kod_narzedzia"])

    assert str(wyjatek.value) == KSZTALT_RAPORTU


def test_blad_raportu_mutantow_jest_bledem_dziedziny():
    from cockpit.services.errors import BladDziedziny
    from runner.dowody import BladRaportuMutantow

    assert issubclass(BladRaportuMutantow, BladDziedziny)


def bez_klucza(raport_: dict, klucz: str) -> dict:
    return {k: v for k, v in raport_.items() if k != klucz}


def z_poza(poza, kod: int = 3) -> str:
    return json.dumps({**raport(1, kod_narzedzia=kod, poza=[POZA_A]), "poza_zasiegiem": poza})


PRZEZYLY_ZLY_KSZTALT = "przezyle[0]: oczekiwano obiektu z kluczami plik, linia, opis_mutanta"
PRZEZYLY_ZLY_PLIK = "przezyle[0].plik: oczekiwano niepustego napisu"
PRZEZYLY_ZLA_LINIA = "przezyle[0].linia: oczekiwano liczby calkowitej >= 1"
POZA_ZLY_KSZTALT = "poza_zasiegiem[0]: oczekiwano obiektu z kluczami plik, linia"
POZA_ZLY_PLIK = "poza_zasiegiem[0].plik: oczekiwano niepustego napisu"
POZA_ZLA_LINIA = "poza_zasiegiem[0].linia: oczekiwano liczby calkowitej >= 1"

# nazwa -> (wyjscie komendy, dokladny komunikat bledu dziedziny)
ZLE_RAPORTY = {
    "nie_json": ("to nie jest json", BRAK_OBIEKTU),
    "pusty_tekst": ("", BRAK_OBIEKTU),
    "lista_zamiast_obiektu": ("[]", BRAK_OBIEKTU),
    "brak_zbadane": (json.dumps({"przezyle": [], "kod_narzedzia": 0}), KSZTALT_RAPORTU),
    "brak_przezyle": (json.dumps({"zbadane": 1, "kod_narzedzia": 0}), KSZTALT_RAPORTU),
    "brak_kodu_narzedzia": (json.dumps({"zbadane": 1, "przezyle": []}), KSZTALT_RAPORTU),
    "nieznany_klucz": (json.dumps({**raport(1), "dodatkowy": 1}), KSZTALT_RAPORTU),
    "zbadane_napis": (
        json.dumps({**raport(1), "zbadane": "1"}),
        "raport mutantow: zbadane musi byc liczba calkowita >= 0"),
    "zbadane_bool": (
        json.dumps({**raport(1), "zbadane": True}),
        "raport mutantow: zbadane musi byc liczba calkowita >= 0"),
    "zbadane_ulamek": (
        json.dumps({**raport(1), "zbadane": 1.5}),
        "raport mutantow: zbadane musi byc liczba calkowita >= 0"),
    "zbadane_ujemne": (
        json.dumps({**raport(1), "zbadane": -1}),
        "raport mutantow: zbadane musi byc liczba calkowita >= 0"),
    "przezyle_obiekt": (
        json.dumps({**raport(1), "przezyle": {}}), "raport mutantow: przezyle musi byc lista"),
    "przezyly_nie_obiekt": (
        json.dumps({**raport(1), "przezyle": ["src/a.py:3"]}), PRZEZYLY_ZLY_KSZTALT),
    "przezyly_jako_lista_wartosci": (
        json.dumps({**raport(1), "przezyle": [["plik", "linia", "opis_mutanta"]]}),
        PRZEZYLY_ZLY_KSZTALT),
    "przezyly_bez_opisu": (
        json.dumps(raport(1, [{"plik": "src/a.py", "linia": 3}])), PRZEZYLY_ZLY_KSZTALT),
    "przezyly_bez_pliku": (
        json.dumps(raport(1, [{"linia": 3, "opis_mutanta": "x"}])), PRZEZYLY_ZLY_KSZTALT),
    "przezyly_bez_linii": (
        json.dumps(raport(1, [{"plik": "src/a.py", "opis_mutanta": "x"}])), PRZEZYLY_ZLY_KSZTALT),
    "przezyly_z_obcym_kluczem": (
        json.dumps(raport(1, [{**PRZEZYL_A, "status": "survived"}])), PRZEZYLY_ZLY_KSZTALT),
    "plik_liczba": (json.dumps(raport(1, [{**PRZEZYL_A, "plik": 5}])), PRZEZYLY_ZLY_PLIK),
    "plik_pusty": (json.dumps(raport(1, [{**PRZEZYL_A, "plik": ""}])), PRZEZYLY_ZLY_PLIK),
    "linia_napis": (json.dumps(raport(1, [{**PRZEZYL_A, "linia": "12"}])), PRZEZYLY_ZLA_LINIA),
    "linia_bool": (json.dumps(raport(1, [{**PRZEZYL_A, "linia": True}])), PRZEZYLY_ZLA_LINIA),
    "linia_zero": (json.dumps(raport(1, [{**PRZEZYL_A, "linia": 0}])), PRZEZYLY_ZLA_LINIA),
    "opis_liczba": (
        json.dumps(raport(1, [{**PRZEZYL_A, "opis_mutanta": 1}])),
        "przezyle[0].opis_mutanta: oczekiwano napisu"),
    "drugi_przezyly_zly": (
        json.dumps(raport(1, [PRZEZYL_A, {**PRZEZYL_B, "linia": 0}])),
        "przezyle[1].linia: oczekiwano liczby calkowitej >= 1"),
    "kod_napis": (json.dumps({**raport(1), "kod_narzedzia": "0"}), ZLY_KOD),
    "kod_bool": (json.dumps({**raport(1), "kod_narzedzia": False}), ZLY_KOD),
    "kod_dwa": (json.dumps({**raport(1), "kod_narzedzia": 2}), ZLY_KOD),
    "kod_nieznany": (json.dumps({**raport(1), "kod_narzedzia": 7}), ZLY_KOD),
    "poza_obiekt": (
        json.dumps({**raport(1, kod_narzedzia=3), "poza_zasiegiem": {}}),
        "raport mutantow: poza_zasiegiem musi byc lista"),
    "poza_wpis_nie_obiekt": (z_poza(["src/a.py:3"]), POZA_ZLY_KSZTALT),
    # lista samych nazw kluczy ma ten sam zbior co obiekt: odrzuca ja tylko kontrola typu wpisu
    "poza_wpis_jako_lista_nazw_kluczy": (z_poza([["plik", "linia"]]), POZA_ZLY_KSZTALT),
    "poza_wpis_bez_linii": (z_poza([{"plik": "src/a.py"}]), POZA_ZLY_KSZTALT),
    "poza_wpis_bez_pliku": (z_poza([{"linia": 3}]), POZA_ZLY_KSZTALT),
    "poza_wpis_z_obcym_kluczem": (z_poza([{**POZA_A, "opis_mutanta": "x"}]), POZA_ZLY_KSZTALT),
    "poza_plik_pusty": (z_poza([{**POZA_A, "plik": ""}]), POZA_ZLY_PLIK),
    "poza_plik_liczba": (z_poza([{**POZA_A, "plik": 5}]), POZA_ZLY_PLIK),
    "poza_linia_zero": (z_poza([{**POZA_A, "linia": 0}]), POZA_ZLA_LINIA),
    "poza_linia_bool": (z_poza([{**POZA_A, "linia": True}]), POZA_ZLA_LINIA),
    "poza_linia_napis": (z_poza([{**POZA_A, "linia": "3"}]), POZA_ZLA_LINIA),
    "drugi_wpis_poza_zly": (
        z_poza([POZA_A, {**POZA_B, "linia": 0}]),
        "poza_zasiegiem[1].linia: oczekiwano liczby calkowitej >= 1"),
    "kod_trzy_z_pusta_lista": (
        json.dumps(raport(1, kod_narzedzia=3, poza=[])),
        "raport mutantow: kod_narzedzia 3 wymaga niepustej listy poza_zasiegiem"),
    "kod_trzy_bez_klucza_poza_zasiegiem": (
        json.dumps(bez_klucza(raport(1, kod_narzedzia=3), "poza_zasiegiem")),
        "raport mutantow: kod_narzedzia 3 wymaga niepustej listy poza_zasiegiem"),
    "przezyle_napis": (
        json.dumps({**raport(1), "przezyle": "plik"}), "raport mutantow: przezyle musi byc lista"),
    "poza_napis": (
        json.dumps({**raport(1, kod_narzedzia=3), "poza_zasiegiem": "plik"}),
        "raport mutantow: poza_zasiegiem musi byc lista"),
}


@pytest.mark.parametrize(
    ("tekst", "komunikat"), list(ZLE_RAPORTY.values()), ids=list(ZLE_RAPORTY))
def test_raport_o_nieznanym_ksztalcie_to_blad_dziedziny_z_dokladnym_komunikatem(tekst, komunikat):
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow(tekst)

    assert str(wyjatek.value) == komunikat


def test_raport_zagniezdzony_bez_konca_to_blad_raportu_o_zbyt_glebokim_zagniezdzeniu():
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    tekst = '{"zbadane": ' + "[" * 100_000

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow(tekst)

    assert str(wyjatek.value) == "raport mutantow: zbyt gleboko zagniezdzony JSON"


def test_raport_zagniezdzony_bez_konca_po_ostrzezeniu_tez_to_blad_o_zbyt_glebokim_zagniezdzeniu():
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    tekst = "UWAGA {oops\n" + '{"zbadane": ' + "[" * 100_000

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow(tekst)

    assert str(wyjatek.value) == "raport mutantow: zbyt gleboko zagniezdzony JSON"


def test_raport_poprzedzony_ostrzezeniem_z_nawiasem_klamrowym_jest_odczytany():
    from runner.dowody import parsuj_raport_mutantow

    r = raport(2, [PRZEZYL_A], 1)

    assert parsuj_raport_mutantow("UWAGA {nie json}\n" + json.dumps(r)) == r


def test_raport_poprzedzony_obiektem_json_o_innym_ksztalcie_jest_odczytany():
    from runner.dowody import parsuj_raport_mutantow

    r = raport(2)

    assert parsuj_raport_mutantow('{"ostrzezenie": "x"}\n' + json.dumps(r)) == r


def test_tekst_po_raporcie_jest_pomijany():
    from runner.dowody import parsuj_raport_mutantow

    r = raport(2)

    assert parsuj_raport_mutantow(json.dumps(r) + '\n{"zbadane": "x"}\nkoniec') == r


def test_przy_kilku_obiektach_o_zlym_ksztalcie_powodem_jest_pierwszy_z_nich():
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    tekst = json.dumps({**raport(1), "kod_narzedzia": 9}) + "\n" + json.dumps({**raport(1), "zbadane": "x"})

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow(tekst)

    assert str(wyjatek.value) == ZLY_KOD


def test_poprawny_raport_zagniezdzony_w_napisie_obiektu_o_zlym_ksztalcie_nie_jest_raportem():
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    tekst = json.dumps({"ostrzezenie": json.dumps(raport(2))})

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow(tekst)

    assert str(wyjatek.value) == KSZTALT_RAPORTU


def test_poprawny_raport_zagniezdzony_jako_wartosc_obiektu_o_zlym_ksztalcie_nie_jest_raportem():
    # szukanie kolejnego obiektu zaczyna sie ZA koncem odrzuconego, nie w jego srodku
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    tekst = json.dumps({"meta": raport(2)})

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow(tekst)

    assert str(wyjatek.value) == KSZTALT_RAPORTU


def test_wyjscie_bez_zadnego_obiektu_json_ma_komunikat_o_braku_obiektu():
    from runner.dowody import BladRaportuMutantow, parsuj_raport_mutantow

    with pytest.raises(BladRaportuMutantow) as wyjatek:
        parsuj_raport_mutantow("same ostrzezenia bez nawiasow")

    assert str(wyjatek.value) == BRAK_OBIEKTU


# --- krok: mutanty.json i dowod ----------------------------------------------------------------


async def test_przezyly_mutant_ma_wpis_plik_linia_w_pliku_mutanty_json_a_krok_przechodzi(k):
    k.linie(LINIE_NIEPUSTE)
    r = raport(5, [PRZEZYL_A, PRZEZYL_B], kod_narzedzia=1)

    wynik = await mutuj(k, komenda_raportu_json(r))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)
    assert k.mutanty() == r
    assert set(k.mutanty()) == KLUCZE_RAPORTU
    assert all(set(p) == KLUCZE_PRZEZYLEGO for p in k.mutanty()["przezyle"])
    assert pliki_pod(k.krok) == [PLIK_DOWODU, PLIK_MUTANTOW]
    assert wynik.dowod is not None and wynik.dowod["plik"] == PLIK_DOWODU
    assert wynik.dowod["capability"] == CAPABILITY


async def test_raport_bez_przezylych_zapisuje_pusta_liste_i_licznik_zbadanych(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_json(raport(7)))

    assert wynik.status == "zakonczony"
    assert k.mutanty()["przezyle"] == []
    assert k.mutanty()["zbadane"] == 7


async def test_kod_trzy_narzedzia_zostaje_w_mutanty_json_jako_ostrzezenie_a_nie_znika(k):
    # zmiany poza zasiegiem mutmuta: krok nie jest zielony "bez sladu" - ostrzezenie jest w dowodzie
    k.linie(LINIE_NIEPUSTE)

    r = raport(2, kod_narzedzia=3, poza=[POZA_A, POZA_B])

    wynik = await mutuj(k, komenda_raportu_json(r))

    assert wynik.status == "zakonczony"
    assert k.mutanty()["kod_narzedzia"] == 3
    assert k.mutanty()["poza_zasiegiem"] == [POZA_A, POZA_B]


async def test_kod_jeden_z_lista_poza_zasiegiem_zostaje_w_mutanty_json_razem_z_przezylymi(k):
    k.linie(LINIE_NIEPUSTE)
    r = raport(4, [PRZEZYL_A], kod_narzedzia=1, poza=[POZA_B])

    wynik = await mutuj(k, komenda_raportu_json(r))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)
    assert k.mutanty() == r


async def test_raport_bez_klucza_poza_zasiegiem_trafia_do_mutanty_json_z_pusta_lista(k):
    k.linie(LINIE_NIEPUSTE)
    bez = {k_: v for k_, v in raport(2, [PRZEZYL_A], 1).items() if k_ != "poza_zasiegiem"}

    wynik = await mutuj(k, komenda_raportu(json.dumps(bez)))

    assert wynik.status == "zakonczony"
    assert k.mutanty() == raport(2, [PRZEZYL_A], 1)
    assert k.mutanty()["poza_zasiegiem"] == []


async def test_pelny_raport_wiekszy_niz_ogon_dowodu_trafia_do_mutanty_json_w_calosci(k):
    from runner.dowody import LIMIT_OGONA

    k.linie(LINIE_NIEPUSTE)
    przezyle = [
        {"plik": f"src/modul_{i}.py", "linia": i + 1, "opis_mutanta": "-" + "x" * 80 + f"{i}\n+" + "y" * 80}
        for i in range(120)
    ]
    r = raport(500, przezyle, kod_narzedzia=1)
    assert len(json.dumps(r).encode("utf-8")) > 3 * LIMIT_OGONA

    wynik = await mutuj(k, komenda_raportu_json(r))

    assert wynik.status == "zakonczony"
    assert k.mutanty() == r


async def test_ostrzezenie_komendy_na_stderr_nie_psuje_raportu_json_na_stdout(k):
    k.linie(LINIE_NIEPUSTE)
    r = raport(3, [PRZEZYL_A], kod_narzedzia=1)

    wynik = await mutuj(k, komenda_raportu_json(r, stderr="UWAGA: zmiany poza zasiegiem mutmuta\n"))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)
    assert k.mutanty() == r


# --- brak dowodu: kazda galaz osobno -----------------------------------------------------------


async def test_zero_zbadanych_mutantow_przy_niepustym_diffie_to_evidence_required(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert wynik.status == "nieudany"
    assert wynik.powod == f"check mutate_diff: {EVIDENCE}"


async def test_raport_z_zbadanymi_mutantami_nie_czyta_pliku_zmienionych_linii(k):
    # plik jest potrzebny tylko do oceny zera zbadanych; brakujacy nie moze oblac raportu z dowodem
    assert not k.plik_linii.exists()

    wynik = await mutuj(k, komenda_raportu_json(raport(1)))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)


async def test_jeden_zbadany_mutant_przy_niepustym_diffie_wystarcza_za_dowod(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_json(raport(1)))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)


async def test_zero_zbadanych_przy_diffie_z_jednym_plikiem_z_liniami_mimo_drugiego_pustego_to_blad(k):
    k.linie([{"plik": "README.md", "linie": []}, {"plik": "src/b.py", "linie": [[5, 5]]}])

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert wynik.status == "nieudany"
    assert wynik.powod == f"check mutate_diff: {EVIDENCE}"


@pytest.mark.parametrize(
    "linie",
    [
        [{"plik": "README.md", "linie": []}],
        [{"plik": "README.md", "linie": []}, {"plik": "stary.txt", "linie": []}],
    ],
    ids=["jeden_wpis_bez_linii", "wpisy_samych_usuniec_bez_linii"],
)
async def test_zero_zbadanych_przy_wpisach_diffu_bez_dodanych_linii_to_evidence_required(k, linie):
    # usuniecie pliku albo sama zmiana trybu to zmiana, wiec zero zbadanych mutantow to brak dowodu
    k.linie(linie)

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert wynik.status == "nieudany"
    assert wynik.powod == f"check mutate_diff: {EVIDENCE}"


async def test_zero_zbadanych_gdy_diff_nie_ma_zadnego_wpisu_nie_jest_bledem(k):
    # bliźniak: tylko pusta lista `[]` oznacza, ze nie bylo czego mutowac
    k.linie([])

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)
    assert k.mutanty()["zbadane"] == 0


async def test_komenda_raportu_z_kodem_dwa_to_porazka_kroku_bez_mutanty_json(k):
    k.linie(LINIE_NIEPUSTE)
    kod = "import sys; print('blad narzedzia: brak mutmuta'); sys.exit(2)"

    wynik = await mutuj(k, komenda(kod))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check mutate_diff: kod wyjścia 2", 2)
    assert not (k.krok / PLIK_MUTANTOW).exists()
    assert (k.krok / PLIK_DOWODU).is_file()


async def test_komenda_raportu_bez_wyjscia_to_evidence_required_bez_mutanty_json(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda("pass"))

    assert wynik.status == "nieudany"
    assert wynik.powod == (
        "check mutate_diff: niezmiennik evidence_required: komenda nie zwrocila zadnego wyniku")
    assert not (k.krok / PLIK_MUTANTOW).exists()


@pytest.mark.parametrize(
    "nazwa",
    ["nie_json", "zbadane_napis", "przezyly_bez_opisu", "nieznany_klucz", "kod_dwa",
     "linia_zero", "poza_linia_zero", "kod_trzy_z_pusta_lista", "poza_wpis_jako_lista_nazw_kluczy"],
)
async def test_raport_o_nieznanym_ksztalcie_konczy_krok_porazka_i_nie_zapisuje_mutanty_json(k, nazwa):
    tekst, komunikat = ZLE_RAPORTY[nazwa]
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu(tekst))

    assert wynik.status == "nieudany"
    assert wynik.powod == f"check mutate_diff: {komunikat}"
    assert not (k.krok / PLIK_MUTANTOW).exists()


# --- zmienione-linie.json: wejscie z dysku ------------------------------------------------------

KSZTALT_LINII = "zmienione linie: oczekiwano listy obiektow z lista linie"

# nazwa -> (tresc pliku, dokladny komunikat bledu)
ZLE_PLIKI_LINII = {
    "nie_json": (b"to nie jest json", "zmienione linie nie sa JSON-em (JSONDecodeError)"),
    "pusty_plik": (b"", "zmienione linie nie sa JSON-em (JSONDecodeError)"),
    "nie_utf8": (b"\xff\xfe\x00", "nie odczytano zmienionych linii (UnicodeDecodeError)"),
    "obiekt_zamiast_listy": (b'{"plik": "a.py"}', KSZTALT_LINII),
    "pusty_obiekt": (b"{}", KSZTALT_LINII),
    "pusty_napis": (b'""', KSZTALT_LINII),
    "null": (b"null", KSZTALT_LINII),
    "wpis_bez_linii": (b'[{"plik": "a.py"}]', KSZTALT_LINII),
    "linie_napis": (b'[{"plik": "a.py", "linie": "1-3"}]', KSZTALT_LINII),
    "wpis_nie_obiekt": (b'["a.py"]', KSZTALT_LINII),
    "drugi_wpis_nie_obiekt": (b'[{"plik": "a.py", "linie": []}, "b.py"]', KSZTALT_LINII),
    "linie_null": (b'[{"plik": "a.py", "linie": null}]', KSZTALT_LINII),
    "zbyt_gleboko_zagniezdzony": (
        b"[" * 100_000, "zmienione linie nie sa JSON-em (RecursionError)"),
}


@pytest.mark.parametrize(
    ("tresc", "komunikat"), list(ZLE_PLIKI_LINII.values()), ids=list(ZLE_PLIKI_LINII))
async def test_nieczytelny_plik_zmienionych_linii_konczy_krok_porazka_a_nie_cichym_sukcesem(
    k, tresc, komunikat,
):
    # zero zbadanych: o wyniku decyduje tylko to, czy da sie ocenic, czy diff byl pusty
    k.plik_linii.write_bytes(tresc)

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert wynik.status == "nieudany"
    assert wynik.powod == f"check mutate_diff: {komunikat}"


async def test_brak_pliku_zmienionych_linii_konczy_krok_porazka_a_nie_cichym_sukcesem(k):
    assert not k.plik_linii.exists()

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert wynik.status == "nieudany"
    assert wynik.powod == "check mutate_diff: nie odczytano zmienionych linii (FileNotFoundError)"


async def test_katalog_w_miejscu_pliku_zmienionych_linii_konczy_krok_porazka(k):
    k.plik_linii.mkdir()

    wynik = await mutuj(k, komenda_raportu_json(raport(0)))

    assert wynik.status == "nieudany"
    assert wynik.powod == "check mutate_diff: nie odczytano zmienionych linii (IsADirectoryError)"


# --- wyjscie komendy: ostre dekodowanie, limit rozmiaru, zapis ----------------------------------

LIMIT_RAPORTU_BAJTOW = 8 * 1024 * 1024


def komenda_bajtow(dane: bytes) -> str:
    """Komenda capability, ktora wypisuje na stdout dokladnie `dane` (bajty, bez kodowania)."""
    kod = "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); sys.stdout.flush()"
    return komenda(kod, dane.hex())


async def test_raport_z_bajtami_spoza_utf8_to_porazka_kroku_a_nie_raport_z_znakiem_zastepczym(k):
    k.linie(LINIE_NIEPUSTE)
    dane = b"\xff" + json.dumps(raport(3)).encode("utf-8")

    wynik = await mutuj(k, komenda_bajtow(dane))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check mutate_diff: raport mutantów nie jest poprawnym UTF-8", 0)
    assert not (k.krok / PLIK_MUTANTOW).exists()
    assert wynik.dowod is not None and wynik.dowod["plik"] == PLIK_DOWODU


async def test_ten_sam_raport_bez_bajtow_spoza_utf8_jest_przyjety(k):
    k.linie(LINIE_NIEPUSTE)
    dane = " ".join(["zażółć", json.dumps(raport(3))]).encode("utf-8")

    wynik = await mutuj(k, komenda_bajtow(dane))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)
    assert k.mutanty()["zbadane"] == 3


def komenda_raportu_dopelnionego_do(bajtow: int) -> str:
    """Raport (3 zbadane) dopelniony spacjami do dokladnie `bajtow` bajtow stdout."""
    kod = (
        "import sys; surowy = sys.argv[1].encode(); "
        "sys.stdout.buffer.write(surowy + b' ' * (int(sys.argv[2]) - len(surowy))); "
        "sys.stdout.flush()"
    )
    return komenda(kod, json.dumps(raport(3)), str(bajtow))


async def test_raport_o_rozmiarze_rownym_limitowi_jest_przyjety(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_dopelnionego_do(LIMIT_RAPORTU_BAJTOW))

    assert (wynik.status, wynik.powod) == ("zakonczony", None)
    assert k.mutanty()["zbadane"] == 3


async def test_raport_o_jeden_bajt_wiekszy_niz_limit_to_porazka_kroku_bez_mutanty_json(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_dopelnionego_do(LIMIT_RAPORTU_BAJTOW + 1))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", f"check mutate_diff: raport mutantów większy niż {LIMIT_RAPORTU_BAJTOW} bajtów", 0)
    assert not (k.krok / PLIK_MUTANTOW).exists()
    assert wynik.dowod is not None and wynik.dowod["plik"] == PLIK_DOWODU


async def test_raport_wiekszy_niz_limit_w_wielu_kawalkach_to_porazka_kroku(k):
    k.linie(LINIE_NIEPUSTE)
    kod = (
        "import sys\n"
        "for _ in range(3):\n"
        "    sys.stdout.write('x' * (3 * 1024 * 1024))\n"
        "    sys.stdout.flush()\n"
    )

    wynik = await mutuj(k, komenda(kod))

    assert wynik.status == "nieudany"
    assert "większy niż" in wynik.powod
    assert not (k.krok / PLIK_MUTANTOW).exists()


async def test_blad_zapisu_mutanty_json_to_porazka_kroku_z_dowodem_komendy(k):
    k.linie(LINIE_NIEPUSTE)
    (k.krok / PLIK_MUTANTOW).mkdir(parents=True)     # w miejscu pliku stoi katalog

    wynik = await mutuj(k, komenda_raportu_json(raport(3)))

    assert wynik.status == "nieudany"
    assert wynik.powod == "check mutate_diff: nie zapisano raportu mutantów (IsADirectoryError)"
    assert wynik.kod_wyjscia == 0
    assert wynik.dowod is not None and wynik.dowod["plik"] == PLIK_DOWODU
    assert (k.krok / PLIK_DOWODU).is_file()


async def test_komenda_przekraczajaca_limit_czasu_to_porazka_kroku_bez_mutanty_json(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda("import time; time.sleep(60)"), limit_s=0.5)

    assert wynik.status == "nieudany"
    assert wynik.powod == "check mutate_diff: przekroczony limit czasu 0.5 s"
    assert not (k.krok / PLIK_MUTANTOW).exists()


async def test_komenda_w_katalogu_runu_wewnatrz_repozytorium_to_porazka_bez_uruchomienia(k):
    k.linie(LINIE_NIEPUSTE)
    k.krok = k.worktree / "run" / "mutanty"

    wynik = await mutuj(k, komenda_raportu_json(raport(3)))

    assert wynik.status == "nieudany"
    assert not k.krok.exists()


# --- fail_on_survivors -------------------------------------------------------------------------


async def test_przezyly_mutant_bez_fail_on_survivors_nie_oblewa_kroku(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_json(raport(3, [PRZEZYL_A], 1)))

    assert wynik.status == "zakonczony"


async def test_przezyly_mutant_z_jawnym_fail_on_survivors_false_nie_oblewa_kroku(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_json(raport(3, [PRZEZYL_A], 1)), fail_on_survivors=False)

    assert wynik.status == "zakonczony"


async def test_przezyly_mutant_z_fail_on_survivors_true_oblewa_krok_a_lista_zostaje_w_dowodzie(k):
    k.linie(LINIE_NIEPUSTE)
    r = raport(3, [PRZEZYL_A, PRZEZYL_B], 1)

    wynik = await mutuj(k, komenda_raportu_json(r), fail_on_survivors=True)

    assert wynik.status == "nieudany"
    assert wynik.powod == "check mutate_diff: przeżyłe mutanty: 2"
    assert k.mutanty() == r
    assert wynik.dowod is not None and wynik.dowod["plik"] == PLIK_DOWODU


async def test_fail_on_survivors_true_bez_przezylych_mutantow_nie_oblewa_kroku(k):
    k.linie(LINIE_NIEPUSTE)

    wynik = await mutuj(k, komenda_raportu_json(raport(3)), fail_on_survivors=True)

    assert (wynik.status, wynik.powod) == ("zakonczony", None)


# --- silnik: krok `system: check` z capability mutate_diff w runie ------------------------------

WORKFLOW_MUTANTOW = """\
name: w
steps:
  autor: {{skill: s_autor}}
  diff: {{system: diff, needs: [autor]}}
  mutanty: {{system: check, capability: mutate_diff, needs: [diff]{dopisek}}}
"""


def swiat_mutantow(tmp_path: Path, *, raport_json: str | None, dopisek: str = ""):
    komendy = {} if raport_json is None else {CAPABILITY: komenda_raportu(raport_json)}
    return swiat(
        tmp_path, [K("autor")], tresc=WORKFLOW_MUTANTOW.format(dopisek=dopisek),
        project=projekt_z(**komendy))


def mutanty_runu(s) -> dict:
    return json.loads((s.katalog_runu / "mutanty" / PLIK_MUTANTOW).read_text(encoding="utf-8"))


async def test_run_z_przezylym_mutantem_zapisuje_plik_linia_w_katalogu_kroku_w_katalogu_runu(tmp_path):
    r = raport(4, [PRZEZYL_A], kod_narzedzia=1)
    s = swiat_mutantow(tmp_path, raport_json=json.dumps(r))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert mutanty_runu(s) == r
    assert pliki_pod(s.katalog_runu / "mutanty") == [PLIK_DOWODU, PLIK_MUTANTOW]
    koniec = jedno(zdarzenia, "step_finished", "mutanty")
    assert koniec["dowod"]["capability"] == CAPABILITY
    assert koniec["dowod"]["plik"] == PLIK_DOWODU
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_run_z_fail_on_survivors_i_przezylym_mutantem_konczy_krok_porazka(tmp_path):
    r = raport(4, [PRZEZYL_A], kod_narzedzia=1)
    s = swiat_mutantow(tmp_path, raport_json=json.dumps(r), dopisek=", fail_on_survivors: true")

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert jedno(zdarzenia, "step_failed", "mutanty")["text"] == (
        "check mutate_diff: przeżyłe mutanty: 1")
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "mutanty"]
    assert mutanty_runu(s) == r
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_run_z_fail_on_survivors_i_bez_przezylych_mutantow_konczy_krok_sukcesem(tmp_path):
    s = swiat_mutantow(
        tmp_path, raport_json=json.dumps(raport(4)), dopisek=", fail_on_survivors: true")

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    jedno(zdarzenia, "step_finished", "mutanty")
    assert mutanty_runu(s)["przezyle"] == []


async def test_run_z_zerem_zbadanych_mutantow_przy_niepustym_diffie_konczy_krok_evidence_required(tmp_path):
    s = swiat_mutantow(tmp_path, raport_json=json.dumps(raport(0)))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert jedno(zdarzenia, "step_failed", "mutanty")["text"] == f"check mutate_diff: {EVIDENCE}"
    assert zdarzenia[-1]["kind"] == "run_finished"


class SzpiegUsuwajacyPlik(Szpieg):
    """Autor tylko usuwa sledzony plik: diff nie jest pusty, ale nie ma ani jednej dodanej linii."""

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, **dodatkowe):
        if spec.step_id == "autor":
            (katalog / "README.md").unlink()
        return await super().__call__(
            spec, polecenie, katalog, emit, katalog_runu=katalog_runu, **dodatkowe)


async def test_run_z_diffem_samych_usuniec_i_zerem_zbadanych_konczy_krok_evidence_required(tmp_path):
    s = swiat_mutantow(tmp_path, raport_json=json.dumps(raport(0)))

    zdarzenia = await uruchom(s, SzpiegUsuwajacyPlik())

    zmienione = json.loads(
        (s.katalog_runu / "diff" / "zmienione-linie.json").read_text(encoding="utf-8"))
    assert zmienione and all(wpis["linie"] == [] for wpis in zmienione)
    assert jedno(zdarzenia, "step_failed", "mutanty")["text"] == f"check mutate_diff: {EVIDENCE}"
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "mutanty"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_krok_mutantow_w_runie_dostaje_limit_czasu_dluzszy_niz_zwykly_check(
    tmp_path, monkeypatch,
):
    from runner import dowody

    limity: dict[str, float] = {}
    prawdziwa = dowody._wykonaj_komende

    async def szpieg(worktree, capability, polecenie, katalog_kroku, limit_s, *, pelne):
        limity[capability] = limit_s
        return await prawdziwa(
            worktree, capability, polecenie, katalog_kroku, limit_s, pelne=pelne)

    monkeypatch.setattr(dowody, "_wykonaj_komende", szpieg)
    s = swiat_mutantow(tmp_path, raport_json=json.dumps(raport(3)))

    await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert limity == {CAPABILITY: dowody.LIMIT_MUTATE_DIFF_S}
    assert dowody.LIMIT_MUTATE_DIFF_S > dowody.LIMIT_CHECKU_S


async def test_projekt_bez_mutate_diff_pomija_krok_z_powodem_brak_capability(tmp_path):
    s = swiat_mutantow(tmp_path, raport_json=None)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert jedno(zdarzenia, "step_skipped", "mutanty")["text"] == (
        "brak_capability: brak capability mutate_diff")
    assert "step_started" not in [z["kind"] for z in zdarzenia if z["step_id"] == "mutanty"]
    assert pliki_pod(s.katalog_runu / "mutanty") == []
    assert zdarzenia[-1]["kind"] == "run_finished"


WORKFLOW_Z_INNYM_ID_DIFFU = """\
name: w
steps:
  autor: {skill: s_autor}
  zmiany: {system: diff, needs: [autor]}
  mutanty: {system: check, capability: mutate_diff, needs: [zmiany]}
"""


async def test_zmienione_linie_krok_mutantow_czyta_z_kroku_diff_o_dowolnym_id(tmp_path):
    s = swiat(
        tmp_path, [K("autor")], tresc=WORKFLOW_Z_INNYM_ID_DIFFU,
        project=projekt_z(mutate_diff=komenda_raportu_json(raport(0))))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert "evidence_required" in jedno(zdarzenia, "step_failed", "mutanty")["text"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_krok_mutantow_bez_przodka_diff_konczy_sie_porazka_kroku_bez_uruchomienia_komendy(
    tmp_path,
):
    # walidator workflow zatrzymuje taki workflow przed startem; silnik broni sie sam, bo wejscie
    # (zmienione linie) czyta z dysku, a brak dowodu to blad
    from types import SimpleNamespace

    from cockpit.services.workflow import wczytaj_workflow
    from runner.silnik import PorazkaKroku, _wykonaj_check

    plik = tmp_path / "w.yaml"
    plik.write_text(
        "name: w\nsteps:\n"
        "  autor: {skill: s_autor}\n"
        "  mutanty: {system: check, capability: mutate_diff, needs: [autor]}\n",
        encoding="utf-8")
    workflow, problemy = wczytaj_workflow(plik)
    assert workflow is not None and not problemy
    znacznik = tmp_path / "uruchomiono"
    polecenie = komenda("import sys; open(sys.argv[1], 'w').close()", str(znacznik))
    przygotowany = SimpleNamespace(
        workflow=workflow,
        projekt=SimpleNamespace(capabilities={CAPABILITY: polecenie}),
        worktree=SimpleNamespace(sciezka=tmp_path / "repo", katalog_runu=tmp_path / "run"))
    krok = next(krok for krok in workflow.kroki if krok.id == "mutanty")

    with pytest.raises(PorazkaKroku) as wyjatek:
        await czekaj(_wykonaj_check(krok, przygotowany), LIMIT_TESTU_S, "_wykonaj_check")

    assert str(wyjatek.value) == "brak kroku diff dla capability mutate_diff"
    assert not znacznik.exists()


# --- walidator: pole fail_on_survivors tylko przy check: mutate_diff ---------------------------

KOD_POLA = "fail_on_survivors_bez_mutate_diff"
KOMUNIKAT_POLA = (
    f"{KOD_POLA}: pole fail_on_survivors dotyczy tylko kroku system: check z capability mutate_diff")


def workflow_z_polem(krok: str) -> str:
    return (
        "name: w\nsteps:\n"
        "  autor: {skill: s_autor}\n"
        "  diff: {system: diff, needs: [autor]}\n"
        f"  x: {krok}\n"
    )


async def blad_startu(tmp_path: Path, tresc: str) -> list[str]:
    s = swiat(
        tmp_path, [K("autor")], tresc=tresc,
        project=projekt_z(typecheck=komenda("print('ok')"), mutate_diff=komenda("print('ok')")))
    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())
    return sprawdz_blad_przed_startem(zdarzenia).splitlines()


@pytest.mark.parametrize(
    "krok",
    [
        "{system: check, capability: typecheck, fail_on_survivors: true, needs: [diff]}",
        "{system: diff, fail_on_survivors: true, needs: [autor]}",
        "{skill: s_autor, fail_on_survivors: true, needs: [diff]}",
    ],
    ids=["check_innej_capability", "krok_diff", "krok_ze_skillem"],
)
async def test_fail_on_survivors_poza_check_mutate_diff_to_blad_workflow_przed_startem(tmp_path, krok):
    linie = await blad_startu(tmp_path, workflow_z_polem(krok))

    assert linie == [f"x: {KOMUNIKAT_POLA}"]


CAPABILITY_BEZ_CHECK = (
    "x: capability_bez_check: pole capability dotyczy tylko kroku system: check")


@pytest.mark.parametrize(
    ("krok", "oczekiwane"),
    [
        (
            "{system: diff, capability: mutate_diff, fail_on_survivors: true, needs: [autor]}",
            [CAPABILITY_BEZ_CHECK, f"x: {KOMUNIKAT_POLA}"],
        ),
        (
            "{skill: s_autor, system: check, capability: mutate_diff, fail_on_survivors: true, "
            "needs: [diff]}",
            ["x: skill_i_system: krok ma naraz pole skill i system", CAPABILITY_BEZ_CHECK,
             f"x: {KOMUNIKAT_POLA}"],
        ),
    ],
    ids=["krok_diff_z_capability_mutate_diff", "krok_ze_skillem_i_system_check"],
)
async def test_fail_on_survivors_na_kroku_ze_skillem_albo_bez_check_jest_zglaszane_obok_innych_bledow(
    tmp_path, krok, oczekiwane,
):
    # capability mutate_diff nie wystarcza: krok z `skill` albo `system` innym niz check tez dostaje blad pola
    linie = await blad_startu(tmp_path, workflow_z_polem(krok))

    assert linie == oczekiwane


@pytest.mark.parametrize("artefakt", [PLIK_MUTANTOW, PLIK_DOWODU])
async def test_krok_po_kroku_mutantow_moze_konsumowac_mutanty_json_i_dowod_silnika(tmp_path, artefakt):
    tresc = WORKFLOW_MUTANTOW.format(dopisek="") + "  rec: {skill: s_rec, needs: [mutanty]}\n"
    s = swiat(
        tmp_path, [K("autor")], tresc=tresc,
        skille_dodatkowe={"s_rec": {"consumes": [artefakt]}},
        project=projekt_z(mutate_diff=komenda_raportu_json(raport(3))))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    jedno(zdarzenia, "step_finished", "mutanty")
    jedno(zdarzenia, "step_finished", "rec")
    assert "walidacja" not in [z["step_id"] for z in zdarzenia]


async def test_artefakt_ktorego_krok_mutantow_nie_produkuje_jest_bledem_workflow_przed_startem(tmp_path):
    # bliźniak konsumentow `mutanty.json` i dowodu: ten sam workflow z innym artefaktem nie startuje
    tresc = WORKFLOW_MUTANTOW.format(dopisek="") + "  rec: {skill: s_rec, needs: [mutanty]}\n"
    s = swiat(
        tmp_path, [K("autor")], tresc=tresc,
        skille_dodatkowe={"s_rec": {"consumes": ["inny.json"]}},
        project=projekt_z(mutate_diff=komenda_raportu_json(raport(3))))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert sprawdz_blad_przed_startem(zdarzenia) == (
        "rec: brak_artefaktu: artefakt inny.json nie jest produkowany przez zadnego przodka kroku")


async def test_fail_on_survivors_przy_check_mutate_diff_przechodzi_walidacje_i_run_rusza(tmp_path):
    s = swiat(
        tmp_path, [K("autor")],
        tresc=workflow_z_polem(
            "{system: check, capability: mutate_diff, fail_on_survivors: true, needs: [diff]}"),
        project=projekt_z(mutate_diff=komenda_raportu_json(raport(2))))

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert "step_started" in rodzaje(zdarzenia)
    jedno(zdarzenia, "step_finished", "x")


def test_krok_workflow_ma_fail_on_survivors_domyslnie_falsz_a_po_ustawieniu_prawde(tmp_path):
    from cockpit.services.workflow import wczytaj_workflow

    plik = tmp_path / "w.yaml"
    plik.write_text(
        "name: w\nsteps:\n"
        "  autor: {skill: s_autor}\n"
        "  diff: {system: diff, needs: [autor]}\n"
        "  a: {system: check, capability: mutate_diff, needs: [diff]}\n"
        "  b: {system: check, capability: mutate_diff, fail_on_survivors: true, needs: [diff]}\n",
        encoding="utf-8")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None and not problemy
    pola = {krok.id: krok.fail_on_survivors for krok in workflow.kroki}
    assert pola["a"] is False
    assert pola["b"] is True
    assert pola["autor"] is False

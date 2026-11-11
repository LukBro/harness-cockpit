"""HAR-177, czesc 2: werdykt jako kontrakt skilla (TDD, przed implementacja).

Skill deklaruje we frontmatterze `verdict: {line, prefix, pass, fail}`. Po zapisie artefaktu krok
czyta linie werdyktu: wartosc z listy `fail` to porazka kroku, brak linii albo wartosc spoza list
to `evidence_required`, a werdykt trafia do `step_finished`/`step_failed` jako pole `verdict`.

Prawdziwy kod: `sprawdz_werdykt` na plikach w tmp_path, `wczytaj_skill`/`zapisz_skill` na prawdziwym
SKILL.md, `wykonaj_krok` na prawdziwych procesach (atrapa agenta z `test_wykonanie_kroku`) oraz
`wykonaj_run` z wstrzyknietym agentem. Status runu wylicza kokpit ze zdarzen
(`nastepny_status`), wiec test sprawdza go na zdarzeniach silnika. Nazwy testow w jezyku
dziedziny (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path

import pytest

from cockpit.db.models import RunStatus
from cockpit.services.errors import BlednySkill
from cockpit.services.invariants import Rola
from cockpit.services.runs import nastepny_status
from cockpit.services.skills import wczytaj_skill, zapisz_skill
from cockpit.services.walidacja import (
    PREFIKS_WERDYKTU,
    SPRAWDZENIA,
    KontraktWerdyktu,
    Naruszenie,
    Niezmiennik,
    Werdykt,
    dowod_z_komendy,
    dowod_z_pliku,
    sprawdz_werdykt,
)
from procesy import czekaj, uruchom_proces, zakoncz_proces
from runner.agent import SpecKroku
from runner.krok import WynikKroku, wykonaj_krok
from test_silnik_runu import K, Szpieg, _git, jedno, swiat, uruchom
from test_wykonanie_kroku import atrapa

EV = Niezmiennik.evidence_required
KONTRAKT = KontraktWerdyktu(pass_=("zgodny",), fail=("rozjazd", "brak dowodu"))
NAZWA = "rozjazdy.md"
KORZEN_REPO = Path(__file__).resolve().parents[1]


def naruszenie(szczegol: str) -> Naruszenie:
    return Naruszenie(EV, szczegol)


def artefakt(tmp_path: Path, tresc: str | bytes, nazwa: str = NAZWA) -> Path:
    plik = tmp_path / nazwa
    if isinstance(tresc, bytes):
        plik.write_bytes(tresc)
    else:
        plik.write_text(tresc, encoding="utf-8")
    return plik


# --- kontrakt i niezmiennik --------------------------------------------------------------------


def test_prefiks_werdyktu_to_werdykt_z_dwukropkiem():
    assert PREFIKS_WERDYKTU == "WERDYKT:"


def test_kontrakt_werdyktu_ma_domyslnie_pierwsza_linie_prefiks_werdykt_i_brak_wartosci_porazki():
    kontrakt = KontraktWerdyktu(pass_=("a",))

    assert kontrakt.line == 1
    assert kontrakt.prefix == "WERDYKT:"
    assert kontrakt.fail == ()


def test_kontrakt_werdyktu_wymaga_wartosci_pass_a_kontrakt_i_wynik_werdyktu_sa_niemutowalne():
    with pytest.raises(TypeError):
        KontraktWerdyktu()
    with pytest.raises(FrozenInstanceError):
        KONTRAKT.line = 2
    with pytest.raises(FrozenInstanceError):
        Werdykt("zgodny", True).zdany = False
    assert hash(KONTRAKT) == hash(KontraktWerdyktu(pass_=("zgodny",), fail=("rozjazd", "brak dowodu")))
    assert hash(Werdykt("a", True)) == hash(Werdykt("a", True))


def test_sprawdzenie_werdyktu_jest_wsrod_sprawdzen_niezmiennika_evidence_required():
    sprawdzenia = SPRAWDZENIA[Niezmiennik.evidence_required]

    assert sprawdz_werdykt in sprawdzenia
    assert dowod_z_komendy in sprawdzenia
    assert dowod_z_pliku in sprawdzenia


# --- sprawdz_werdykt: tabela ---------------------------------------------------------------------


def test_werdykt_z_listy_pass_jest_zdany_a_wartosc_pochodzi_z_kontraktu(tmp_path):
    plik = artefakt(tmp_path, "WERDYKT: zgodny\nreszta\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == Werdykt("zgodny", True)


@pytest.mark.parametrize("wartosc", ["rozjazd", "brak dowodu"])
def test_werdykt_z_listy_fail_nie_jest_zdany_takze_gdy_wartosc_ma_spacje(tmp_path, wartosc):
    plik = artefakt(tmp_path, f"WERDYKT: {wartosc}\nopis\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == Werdykt(wartosc, False)


def test_wartosc_werdyktu_jest_w_brzmieniu_kontraktu_a_nie_pliku(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("TAK",), fail=("NIE",))

    assert sprawdz_werdykt(artefakt(tmp_path, "WERDYKT: tak\n"), kontrakt) == Werdykt("TAK", True)
    assert sprawdz_werdykt(artefakt(tmp_path, "werdykt: Nie\n"), kontrakt) == Werdykt("NIE", False)


def test_prefiks_i_wartosc_porownuje_sie_bez_wzgledu_na_wielkosc_liter(tmp_path):
    plik = artefakt(tmp_path, "werdykt: ZGODNY\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == Werdykt("zgodny", True)


def test_wielkosc_liter_jest_zwijana_jak_casefold_a_nie_jak_lower(tmp_path):
    # casefold: "Straße" == "strasse", lower: nie
    kontrakt = KontraktWerdyktu(pass_=("strasse",))

    plik = artefakt(tmp_path, "WERDYKT: Straße\n")

    assert sprawdz_werdykt(plik, kontrakt) == Werdykt("strasse", True)


def test_biale_znaki_wokol_linii_prefiksu_i_wartosci_sa_pomijane(tmp_path):
    plik = artefakt(tmp_path, "   WERDYKT:   \t zgodny \t \nreszta\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == Werdykt("zgodny", True)


def test_wartosc_spoza_obu_list_to_evidence_required_bez_echa_wartosci(tmp_path):
    plik = artefakt(tmp_path, "WERDYKT: wymyslony\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie("werdykt spoza kontraktu w rozjazdy.md")


@pytest.mark.parametrize("linia", ["WERDYKT: zgodny, ale", "WERDYKT: zgodnyX", "WERDYKT: zgod", "WERDYKT:"])
def test_wartosc_musi_rownac_sie_elementowi_listy_a_nie_tylko_zaczynac_sie_od_niego(tmp_path, linia):
    plik = artefakt(tmp_path, linia + "\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie("werdykt spoza kontraktu w rozjazdy.md")


def test_licznik_przy_wartosci_jest_wartoscia_spoza_kontraktu(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("brak zastrzeżeń",), fail=("zastrzeżenia",))
    plik = artefakt(tmp_path, "WERDYKT: zastrzeżenia: 3\n")

    assert sprawdz_werdykt(plik, kontrakt) == naruszenie("werdykt spoza kontraktu w rozjazdy.md")


def test_pusty_plik_to_brak_linii_werdyktu(tmp_path):
    plik = artefakt(tmp_path, "")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie("brak linii werdyktu 1 w rozjazdy.md")


def test_pierwsza_linia_pusta_to_zly_prefiks_a_nie_brak_linii(tmp_path):
    plik = artefakt(tmp_path, "\nWERDYKT: zgodny\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie(
        "linia 1 w rozjazdy.md nie zaczyna się od WERDYKT:")


def test_tekst_zamiast_linii_werdyktu_to_zly_prefiks_z_nazwa_pliku_i_prefiksu(tmp_path):
    plik = artefakt(tmp_path, "Wszystko jest w porzadku.\nWERDYKT: zgodny\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie(
        "linia 1 w rozjazdy.md nie zaczyna się od WERDYKT:")


def test_prefiks_nie_w_poczatku_linii_to_zly_prefiks(tmp_path):
    plik = artefakt(tmp_path, "x WERDYKT: zgodny\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie(
        "linia 1 w rozjazdy.md nie zaczyna się od WERDYKT:")


def test_werdykt_w_drugiej_linii_jest_czytany_gdy_kontrakt_wskazuje_linie_dwa(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("zgodny",), line=2)
    plik = artefakt(tmp_path, "# Naglowek\nWERDYKT: zgodny\n")

    assert sprawdz_werdykt(plik, kontrakt) == Werdykt("zgodny", True)


def test_werdykt_w_pierwszej_linii_nie_wystarcza_gdy_kontrakt_wskazuje_linie_dwa(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("zgodny",), line=2)
    plik = artefakt(tmp_path, "WERDYKT: zgodny\ninna tresc\n")

    assert sprawdz_werdykt(plik, kontrakt) == naruszenie(
        "linia 2 w rozjazdy.md nie zaczyna się od WERDYKT:")


def test_plik_krotszy_niz_wskazana_linia_to_brak_linii_werdyktu_z_numerem_linii(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("zgodny",), line=2)
    plik = artefakt(tmp_path, "WERDYKT: zgodny\n")

    assert sprawdz_werdykt(plik, kontrakt) == naruszenie("brak linii werdyktu 2 w rozjazdy.md")


def test_plik_z_dokladnie_tyloma_liniami_ile_wskazano_ma_linie_werdyktu(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("zgodny",), line=3)
    plik = artefakt(tmp_path, "a\nb\nWERDYKT: zgodny")

    assert sprawdz_werdykt(plik, kontrakt) == Werdykt("zgodny", True)


def test_numeracja_linii_dzieli_tekst_jak_splitlines_takze_na_separatorze_unicode(tmp_path):
    # kontrakt: `read_text(...).splitlines()`;   konczy linie, split("\n") by jej nie rozdzielil
    kontrakt = KontraktWerdyktu(pass_=("zgodny",), line=2)
    plik = artefakt(tmp_path, "naglowek WERDYKT: zgodny\n")

    assert sprawdz_werdykt(plik, kontrakt) == Werdykt("zgodny", True)


def test_zakonczenia_linii_windows_nie_psuja_wartosci_werdyktu(tmp_path):
    plik = artefakt(tmp_path, b"WERDYKT: rozjazd\r\nopis\r\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == Werdykt("rozjazd", False)


def test_wlasny_prefiks_kontraktu_zastepuje_domyslny(tmp_path):
    kontrakt = KontraktWerdyktu(pass_=("ok",), prefix="VERDICT:")

    assert sprawdz_werdykt(artefakt(tmp_path, "VERDICT: ok\n"), kontrakt) == Werdykt("ok", True)
    assert sprawdz_werdykt(artefakt(tmp_path, "WERDYKT: ok\n"), kontrakt) == naruszenie(
        "linia 1 w rozjazdy.md nie zaczyna się od VERDICT:")


def test_wartosc_z_obu_list_jest_najpierw_szukana_w_pass(tmp_path):
    # kontrakt z wspolna wartoscia odrzuca wczytanie skilla; sam sprawdzacz ma ustalona kolejnosc
    kontrakt = KontraktWerdyktu(pass_=("a",), fail=("A",))

    assert sprawdz_werdykt(artefakt(tmp_path, "WERDYKT: a\n"), kontrakt) == Werdykt("a", True)


def test_plik_niezakodowany_w_utf8_to_evidence_required_z_nazwa_pliku(tmp_path):
    plik = artefakt(tmp_path, b"WERDYKT: zgodny \xff\xfe\n")

    assert sprawdz_werdykt(plik, KONTRAKT) == naruszenie(
        "nie da się odczytać werdyktu z rozjazdy.md")


KOD_ODCZYT_W_LOCALE_ASCII = """\
import json, sys
sys.path[:0] = json.loads(sys.argv[1])
from pathlib import Path
from cockpit.services.walidacja import KontraktWerdyktu, sprawdz_werdykt

print(repr(sprawdz_werdykt(Path(sys.argv[2]), KontraktWerdyktu(pass_=("zgodny",)))))
"""


async def test_artefakt_utf8_z_polskimi_znakami_jest_czytany_jako_utf8_takze_w_srodowisku_z_locale_ascii(
    tmp_path,
):
    plik = artefakt(tmp_path, "WERDYKT: zgodny\nrozjazd w polu zażółć\n")
    srodowisko = {
        **os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
    }
    proc = await uruchom_proces(
        sys.executable, "-c", KOD_ODCZYT_W_LOCALE_ASCII, json.dumps(sys.path), str(plik),
        cwd=KORZEN_REPO, env=srodowisko, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE)
    try:
        wyjscie = await czekaj(proc.stdout.read(), 40, "wynik odczytu werdyktu")
        await czekaj(proc.wait(), 20, "koniec procesu odczytu werdyktu")
    finally:
        await zakoncz_proces(proc, nazwa="odczyt werdyktu w locale ascii")

    assert wyjscie.decode("utf-8").strip() == repr(Werdykt("zgodny", True))


def test_brak_pliku_to_evidence_required_z_nazwa_pliku(tmp_path):
    assert sprawdz_werdykt(tmp_path / NAZWA, KONTRAKT) == naruszenie(
        "nie da się odczytać werdyktu z rozjazdy.md")


def test_katalog_zamiast_pliku_to_evidence_required_z_nazwa(tmp_path):
    (tmp_path / NAZWA).mkdir()

    assert sprawdz_werdykt(tmp_path / NAZWA, KONTRAKT) == naruszenie(
        "nie da się odczytać werdyktu z rozjazdy.md")


# --- wczytanie skilla z `verdict` ----------------------------------------------------------------


def napisz_skill(
    tmp_path: Path, verdict: str | None, *, produces: str | None = "[rozjazdy.md]",
    id: str = "wer",
) -> Path:
    katalog = tmp_path / id
    katalog.mkdir(parents=True, exist_ok=True)
    linie = [f"id: {id}", f"name: {id}", "role: reviewer"]
    if produces is not None:
        linie.append(f"produces: {produces}")
    if verdict is not None:
        linie.append(verdict)
    plik = katalog / "SKILL.md"
    plik.write_text("---\n" + "\n".join(linie) + "\n---\nBrief.\n", encoding="utf-8")
    return plik


def test_skill_bez_verdict_nie_ma_kontraktu_werdyktu(tmp_path):
    assert wczytaj_skill(napisz_skill(tmp_path, None)).verdict is None


def test_skill_z_pelnym_verdict_ma_kontrakt_z_wartosciami_z_frontmattera(tmp_path):
    plik = napisz_skill(
        tmp_path,
        "verdict: {pass: [zgodny], fail: [rozjazd, brak dowodu], line: 2, prefix: 'VERDICT:'}")

    assert wczytaj_skill(plik).verdict == KontraktWerdyktu(
        pass_=("zgodny",), fail=("rozjazd", "brak dowodu"), line=2, prefix="VERDICT:")


def test_verdict_tylko_z_pass_ma_domyslna_linie_pierwsza_prefiks_werdykt_i_pusta_liste_fail(tmp_path):
    kontrakt = wczytaj_skill(napisz_skill(tmp_path, "verdict: {pass: [zgodny]}")).verdict

    assert kontrakt is not None
    assert (kontrakt.line, kontrakt.prefix, kontrakt.fail) == (1, "WERDYKT:", ())
    assert kontrakt.pass_ == ("zgodny",)


def test_verdict_w_stylu_blokowym_jest_czytany_tak_samo_jak_przepływowy(tmp_path):
    plik = napisz_skill(
        tmp_path,
        "verdict:\n  pass: [TAK]\n  fail:\n    - NIE\n    - nie wiem\n  line: 3",
    )

    assert wczytaj_skill(plik).verdict == KontraktWerdyktu(
        pass_=("TAK",), fail=("NIE", "nie wiem"), line=3)


def test_listy_kontraktu_sa_krotkami_a_nie_listami(tmp_path):
    kontrakt = wczytaj_skill(napisz_skill(tmp_path, "verdict: {pass: [a], fail: [b]}")).verdict

    assert kontrakt is not None
    assert isinstance(kontrakt.pass_, tuple) and isinstance(kontrakt.fail, tuple)


PASS_ZLY = "pass musi być niepustą listą napisów"
FAIL_ZLY = "fail musi być listą napisów"
LINE_ZLY = "line musi być liczbą całkowitą >= 1"
PREFIX_ZLY = "prefix musi być niepustym napisem"

BLEDNE_VERDICT = [
    pytest.param("verdict: zgodny", "verdict musi być mapą", id="napis-zamiast-mapy"),
    pytest.param("verdict: [zgodny]", "verdict musi być mapą", id="lista-zamiast-mapy"),
    pytest.param("verdict: 5", "verdict musi być mapą", id="liczba-zamiast-mapy"),
    pytest.param("verdict: {pass: [a], extra: 1}", "nieznany klucz extra", id="nieznany-klucz"),
    pytest.param("verdict: {pass_: [a]}", "nieznany klucz pass_", id="klucz-pass-z-podkresleniem"),
    pytest.param("verdict: {}", PASS_ZLY, id="pusta-mapa-bez-pass"),
    pytest.param("verdict: {fail: [b]}", PASS_ZLY, id="brak-pass"),
    pytest.param("verdict: {pass: []}", PASS_ZLY, id="pass-pusta-lista"),
    pytest.param("verdict: {pass: zgodny}", PASS_ZLY, id="pass-napis-zamiast-listy"),
    pytest.param("verdict: {pass: [5]}", PASS_ZLY, id="pass-liczba-w-liscie"),
    pytest.param("verdict: {pass: ['']}", PASS_ZLY, id="pass-pusty-napis-w-liscie"),
    pytest.param("verdict: {pass: [zgodny, 5]}", PASS_ZLY, id="pass-liczba-obok-napisu"),
    pytest.param("verdict: {pass: [a], fail: b}", FAIL_ZLY, id="fail-napis-zamiast-listy"),
    pytest.param("verdict: {pass: [a], fail: [5]}", FAIL_ZLY, id="fail-liczba-w-liscie"),
    pytest.param("verdict: {pass: [a], fail: ['']}", FAIL_ZLY, id="fail-pusty-napis-w-liscie"),
    pytest.param("verdict: {pass: [a], fail: [b, 5]}", FAIL_ZLY, id="fail-liczba-obok-napisu"),
    pytest.param("verdict: {pass: [a], line: true}", LINE_ZLY, id="line-bool"),
    pytest.param("verdict: {pass: [a], line: 0}", LINE_ZLY, id="line-zero"),
    pytest.param("verdict: {pass: [a], line: -1}", LINE_ZLY, id="line-ujemna"),
    pytest.param("verdict: {pass: [a], line: '1'}", LINE_ZLY, id="line-napis"),
    pytest.param("verdict: {pass: [a], line: 1.5}", LINE_ZLY, id="line-ulamek"),
    pytest.param("verdict: {pass: [a], prefix: ''}", PREFIX_ZLY, id="prefix-pusty"),
    pytest.param("verdict: {pass: [a], prefix: 5}", PREFIX_ZLY, id="prefix-liczba"),
    pytest.param("verdict: {pass: [a], prefix: [x]}", PREFIX_ZLY, id="prefix-lista"),
    pytest.param(
        "verdict: {pass: [zgodny, ok], fail: [ok]}",
        "pass i fail mają wspólną wartość: ok", id="wspolna-wartosc"),
    pytest.param(
        "verdict: {pass: [Zgodny], fail: [ZGODNY]}",
        "pass i fail mają wspólną wartość: Zgodny", id="wspolna-wartosc-rozna-tylko-wielkoscia-liter"),
    pytest.param(
        "verdict: {pass: [a, b, c], fail: [c, b]}",
        "pass i fail mają wspólną wartość: b", id="wspolna-wartosc-pierwsza-w-kolejnosci-pass"),
]


@pytest.mark.parametrize(("verdict", "powod"), BLEDNE_VERDICT)
def test_niepoprawny_verdict_nie_przechodzi_wczytania_z_pelnym_komunikatem(tmp_path, verdict, powod):
    plik = napisz_skill(tmp_path, verdict)

    with pytest.raises(BlednySkill) as blad:
        wczytaj_skill(plik)

    assert str(blad.value) == f"niepoprawny verdict w {plik}: {powod}"


@pytest.mark.parametrize("produces", [None, "[]"])
def test_verdict_bez_produces_nie_przechodzi_wczytania(tmp_path, produces):
    plik = napisz_skill(tmp_path, "verdict: {pass: [a]}", produces=produces)

    with pytest.raises(BlednySkill) as blad:
        wczytaj_skill(plik)

    assert str(blad.value) == f"niepoprawny verdict w {plik}: verdict wymaga produces"


def test_skill_bez_verdict_i_bez_produces_nadal_sie_wczytuje(tmp_path):
    assert wczytaj_skill(napisz_skill(tmp_path, None, produces=None)).verdict is None


def test_zapis_skilla_po_zmianie_innego_pola_zachowuje_verdict(tmp_path):
    plik = napisz_skill(
        tmp_path, "verdict: {pass: [zgodny], fail: [rozjazd], line: 2, prefix: 'VERDICT:'}")
    skill = wczytaj_skill(plik)
    skill.name = "inna nazwa"

    zapisz_skill(skill, plik)

    tekst = plik.read_text(encoding="utf-8")
    assert "name: inna nazwa" in tekst
    assert "pass_" not in tekst
    ponownie = wczytaj_skill(plik)
    assert ponownie.verdict == KontraktWerdyktu(
        pass_=("zgodny",), fail=("rozjazd",), line=2, prefix="VERDICT:")


# --- wykonaj_krok z kontraktem werdyktu ------------------------------------------------------


KROK = "krok-23"


def spec(rola: Rola = Rola.reviewer, wyjscia: list[str] | None = None) -> SpecKroku:
    return SpecKroku(
        run_id="run-17", step_id=KROK, agent_id="agent-31", skill_id="kontrakt",
        rola=rola, wejscia=[],
        wyjscia=list(wyjscia if wyjscia is not None else [NAZWA]), komendy={},
    )


def wypisz(tresc: str, **reszta) -> list[str]:
    # bajty, nie tekst: atrapa nie zalezy od kodowania stdout srodowiska
    return atrapa(bajty=tresc.encode("utf-8").hex(), **reszta)


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


async def krok(spec_kroku, polecenie, katalog, katalog_runu, **reszta) -> WynikKroku:
    async def emit(zdarzenie: dict) -> None:
        pass

    return await asyncio.wait_for(
        wykonaj_krok(spec_kroku, polecenie, katalog, emit, katalog_runu=katalog_runu, **reszta), 30)


async def test_artefakt_z_werdyktem_rozjazd_konczy_krok_porazka_z_wartoscia_werdyktu(
    katalog, katalog_runu
):
    tresc = "WERDYKT: rozjazd\n- pole x ma inny typ\n"

    wynik = await krok(spec(), wypisz(tresc), katalog, katalog_runu, werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="nieudany", powod="werdykt: rozjazd", kod_wyjscia=0, werdykt="rozjazd")
    assert (katalog_runu / KROK / NAZWA).read_text(encoding="utf-8") == tresc


async def test_werdykt_z_listy_fail_o_wielu_slowach_jest_podany_w_calosci(katalog, katalog_runu):
    wynik = await krok(
        spec(), wypisz("WERDYKT: brak dowodu\n"), katalog, katalog_runu, werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="nieudany", powod="werdykt: brak dowodu", kod_wyjscia=0, werdykt="brak dowodu")


async def test_artefakt_z_werdyktem_z_listy_pass_konczy_krok_sukcesem_i_niesie_werdykt(
    katalog, katalog_runu
):
    wynik = await krok(
        spec(), wypisz("WERDYKT: zgodny\nprzejrzano modele\n"), katalog, katalog_runu,
        werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0, werdykt="zgodny")


async def test_werdykt_w_wyniku_kroku_ma_wartosc_z_kontraktu_a_nie_z_pliku(katalog, katalog_runu):
    kontrakt = KontraktWerdyktu(pass_=("TAK",), fail=("NIE",))

    wynik = await krok(spec(), wypisz("werdykt: tak\n"), katalog, katalog_runu, werdykt=kontrakt)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0, werdykt="TAK")


async def test_artefakt_bez_linii_werdyktu_konczy_krok_bledem_evidence_required_bez_werdyktu(
    katalog, katalog_runu
):
    wynik = await krok(
        spec(), wypisz("Wszystko zgodne.\n"), katalog, katalog_runu, werdykt=KONTRAKT)

    assert wynik == WynikKroku(
        status="nieudany",
        powod="niezmiennik evidence_required: linia 1 w rozjazdy.md nie zaczyna się od WERDYKT:", kod_wyjscia=0)
    assert wynik.werdykt is None


async def test_artefakt_krotszy_niz_wskazana_linia_werdyktu_konczy_krok_bledem_evidence_required(
    katalog, katalog_runu
):
    kontrakt = KontraktWerdyktu(pass_=("zgodny",), line=2)

    wynik = await krok(
        spec(), wypisz("WERDYKT: zgodny\n"), katalog, katalog_runu, werdykt=kontrakt)

    assert wynik == WynikKroku(
        status="nieudany", powod="niezmiennik evidence_required: brak linii werdyktu 2 w rozjazdy.md", kod_wyjscia=0)


async def test_werdykt_spoza_list_konczy_krok_bledem_evidence_required_bez_echa_wartosci(
    katalog, katalog_runu
):
    wynik = await krok(
        spec(), wypisz("WERDYKT: wymyslony\n"), katalog, katalog_runu, werdykt=KONTRAKT)

    assert wynik == WynikKroku(
        status="nieudany", powod="niezmiennik evidence_required: werdykt spoza kontraktu w rozjazdy.md", kod_wyjscia=0)


async def test_bez_kontraktu_werdyktu_artefakt_bez_linii_werdyktu_nadal_konczy_krok_sukcesem(
    katalog, katalog_runu
):
    wynik = await krok(spec(), wypisz("Wszystko zgodne.\n"), katalog, katalog_runu)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert wynik.werdykt is None


async def test_bez_kontraktu_werdyktu_jawne_none_nie_zmienia_zachowania(katalog, katalog_runu):
    wynik = await krok(
        spec(), wypisz("WERDYKT: rozjazd\n"), katalog, katalog_runu, werdykt=None)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)


async def test_kod_wyjscia_agenta_rozny_od_zera_wygrywa_z_werdyktem(katalog, katalog_runu):
    wynik = await krok(
        spec(), wypisz("WERDYKT: zgodny\n", kod=3), katalog, katalog_runu, werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="nieudany", powod="kod wyjścia 3", kod_wyjscia=3)


async def test_recenzent_ktory_zmienil_repozytorium_ma_porazke_reviewer_readonly_a_nie_werdykt(
    katalog, katalog_runu
):
    polecenie = wypisz("WERDYKT: rozjazd\n", zapisz={str(katalog / "nowy.txt"): "samowola"})

    wynik = await krok(spec(), polecenie, katalog, katalog_runu, werdykt=KONTRAKT)

    assert wynik.status == "nieudany"
    assert wynik.powod is not None
    assert wynik.powod.startswith("niezmiennik reviewer_readonly: ")
    assert wynik.werdykt is None


async def test_autor_zapisujacy_artefakt_w_kopii_roboczej_tez_podlega_werdyktowi(
    katalog, katalog_runu
):
    kontrakt = KontraktWerdyktu(pass_=("TAK",), fail=("NIE",))
    polecenie = atrapa(zapisz={str(katalog / "wynik.md"): "WERDYKT: NIE\nnie dziala\n"})

    wynik = await krok(
        spec(Rola.author, ["wynik.md"]), polecenie, katalog, katalog_runu, werdykt=kontrakt)

    assert wynik == WynikKroku(status="nieudany", powod="werdykt: NIE", kod_wyjscia=0, werdykt="NIE")


ZMIENIONE = [{"plik": "src/a.py", "linie": [[10, 12]]}]
NOTATKA_PO_WERDYKCIE = (
    "\n## Co zmieniono\n\n- cos\n\n"
    "## Jakie ryzyko\n\n- [src/{kotwica}] scenariusz: Cos sie zepsuje.\n\n"
    "## Jak to sprawdzić\n\n- uruchom test\n"
)


@pytest.fixture
def plik_linii(tmp_path: Path) -> Path:
    plik = tmp_path / "zmienione-linie.json"
    plik.write_text(json.dumps(ZMIENIONE), encoding="utf-8")
    return plik


def notatka(werdykt: str, kotwica: str) -> str:
    return f"WERDYKT: {werdykt}" + NOTATKA_PO_WERDYKCIE.format(kotwica=kotwica)


async def test_notatka_zmiany_z_werdyktem_pass_niesie_obok_kotwic_werdykt(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec(wyjscia=["notatka-zmiany.md"]), wypisz(notatka("zgodny", "a.py:11")),
        katalog, katalog_runu, zmienione_linie=plik_linii, werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0, kotwice=("src/a.py:11",), werdykt="zgodny")


async def test_werdykt_fail_notatki_wygrywa_z_oceniana_pozniej_kotwica_spoza_diffu(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec(wyjscia=["notatka-zmiany.md"]), wypisz(notatka("rozjazd", "a.py:90")),
        katalog, katalog_runu, zmienione_linie=plik_linii, werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="nieudany", powod="werdykt: rozjazd", kod_wyjscia=0, werdykt="rozjazd")


async def test_werdykt_fail_notatki_z_poprawna_kotwica_nadal_konczy_krok_porazka(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec(wyjscia=["notatka-zmiany.md"]), wypisz(notatka("rozjazd", "a.py:11")),
        katalog, katalog_runu, zmienione_linie=plik_linii, werdykt=KONTRAKT)

    assert wynik == WynikKroku(status="nieudany", powod="werdykt: rozjazd", kod_wyjscia=0, werdykt="rozjazd")


async def test_werdykt_pass_notatki_nie_chroni_kotwicy_spoza_diffu(
    katalog, katalog_runu, plik_linii
):
    wynik = await krok(
        spec(wyjscia=["notatka-zmiany.md"]), wypisz(notatka("zgodny", "a.py:90")),
        katalog, katalog_runu, zmienione_linie=plik_linii, werdykt=KONTRAKT)

    assert wynik.status == "nieudany"
    assert wynik.powod is not None
    assert wynik.powod.startswith("niezmiennik evidence_required: kotwica src/a.py:90")
    assert wynik.werdykt is None


# --- silnik: werdykt w zdarzeniach i w statusie runu ---------------------------------------------


@dataclass
class AgentZWerdyktem:
    """Agent z prawdziwym procesem przez prawdziwe `wykonaj_krok`: wypisuje zadany tekst."""

    wyjscia: dict[str, str]
    wywolania: list[str] = field(default_factory=list)

    async def __call__(self, spec_kroku, polecenie, katalog, emit, *, katalog_runu=None, **dodatkowe):
        self.wywolania.append(spec_kroku.step_id)
        tresc = self.wyjscia.get(spec_kroku.step_id, "gotowe\n")
        # silnik podaje prompt, wiec krok idzie w trybie agenta (HAR-174): wynik agenta
        # przychodzi jako zdarzenie `result` strumienia stream-json, nie surowy tekst
        wynik = json.dumps({"type": "result", "subtype": "success", "result": tresc}) + "\n"
        return await wykonaj_krok(
            spec_kroku, atrapa(bajty=wynik.encode("utf-8").hex()), katalog, emit,
            katalog_runu=katalog_runu, **dodatkowe)


def swiat_z_werdyktem(tmp_path: Path, verdict: str | None = "verdict: {pass: [zgodny], fail: [rozjazd]}"):
    s = swiat(tmp_path, [K("autor"), K("rec", ("autor",)), K("po", ("rec",))])
    linie = ["id: s_rec", "name: s_rec", "role: reviewer", "produces: [rozjazdy.md]"]
    if verdict is not None:
        linie.append(verdict)
    plik = s.repo / ".cockpit" / "skills" / "s_rec" / "SKILL.md"
    plik.write_text("---\n" + "\n".join(linie) + "\n---\nBrief.\n", encoding="utf-8")
    _git(s.repo, "add", "-A")
    _git(s.repo, "commit", "-q", "-m", "skill z werdyktem")
    return s


def status_runu(zdarzenia: list[dict]) -> RunStatus:
    """Jak kokpit: status runu ze zdarzen kroku i `run_finished`."""
    assert zdarzenia[-1]["kind"] == "run_finished"
    nieudane = [z["step_id"] for z in zdarzenia if z["kind"] == "step_failed"]
    return nastepny_status(RunStatus.running, "run_finished", nieudane)


async def test_werdykt_rozjazd_konczy_krok_porazka_a_run_nie_przechodzi_do_odbioru(tmp_path):
    s = swiat_z_werdyktem(tmp_path)
    agent = AgentZWerdyktem({"rec": "WERDYKT: rozjazd\n- pole x\n"})

    zdarzenia = await uruchom(s, agent)

    nieudany = jedno(zdarzenia, "step_failed", "rec")
    assert nieudany["text"] == "werdykt: rozjazd"
    assert nieudany["verdict"] == "rozjazd"
    assert (s.katalog_runu / "rec" / NAZWA).read_text(encoding="utf-8") == "WERDYKT: rozjazd\n- pole x\n"
    assert status_runu(zdarzenia) == RunStatus.failed
    assert status_runu(zdarzenia) != RunStatus.to_accept
    assert jedno(zdarzenia, "step_skipped", "po")["text"] == (
        "zaleznosc_nieudana: zależność nieudana: rec")
    assert agent.wywolania == ["autor", "rec"]


async def test_werdykt_z_listy_pass_konczy_krok_sukcesem_a_step_finished_niesie_werdykt(tmp_path):
    s = swiat_z_werdyktem(tmp_path)
    agent = AgentZWerdyktem({"rec": "WERDYKT: zgodny\nprzejrzano\n"})

    zdarzenia = await uruchom(s, agent)

    koniec = jedno(zdarzenia, "step_finished", "rec")
    assert koniec["verdict"] == "zgodny"
    assert koniec["text"] == ""
    assert "step_failed" not in [z["kind"] for z in zdarzenia]
    assert status_runu(zdarzenia) == RunStatus.to_accept


async def test_zdarzenie_kroku_bez_kontraktu_werdyktu_nie_ma_pola_verdict(tmp_path):
    s = swiat_z_werdyktem(tmp_path)

    zdarzenia = await uruchom(s, AgentZWerdyktem({"rec": "WERDYKT: zgodny\n"}))

    assert "verdict" not in jedno(zdarzenia, "step_finished", "autor")
    assert "verdict" not in jedno(zdarzenia, "step_finished", "po")


async def test_artefakt_bez_linii_werdyktu_konczy_krok_bledem_evidence_required_a_run_nie_jest_do_odbioru(
    tmp_path,
):
    s = swiat_z_werdyktem(tmp_path)

    zdarzenia = await uruchom(s, AgentZWerdyktem({"rec": "Wszystko jest zgodne.\n"}))

    nieudany = jedno(zdarzenia, "step_failed", "rec")
    assert nieudany["text"] == (
        "niezmiennik evidence_required: linia 1 w rozjazdy.md nie zaczyna się od WERDYKT:")
    assert "verdict" not in nieudany
    assert status_runu(zdarzenia) == RunStatus.failed


async def test_werdykt_spoza_list_konczy_krok_bledem_evidence_required_bez_pola_verdict(tmp_path):
    s = swiat_z_werdyktem(tmp_path)

    zdarzenia = await uruchom(s, AgentZWerdyktem({"rec": "WERDYKT: moze\n"}))

    nieudany = jedno(zdarzenia, "step_failed", "rec")
    assert nieudany["text"] == (
        "niezmiennik evidence_required: werdykt spoza kontraktu w rozjazdy.md")
    assert "verdict" not in nieudany


async def test_skill_bez_kontraktu_werdyktu_nie_dostaje_go_i_artefakt_bez_werdyktu_przechodzi(
    tmp_path,
):
    s = swiat_z_werdyktem(tmp_path, verdict=None)
    agent = AgentZWerdyktem({"rec": "Tresc bez linii werdyktu.\n"})

    zdarzenia = await uruchom(s, agent)

    koniec = jedno(zdarzenia, "step_finished", "rec")
    assert "verdict" not in koniec
    assert status_runu(zdarzenia) == RunStatus.to_accept


async def test_silnik_przekazuje_agentowi_kontrakt_werdyktu_tylko_gdy_skill_go_deklaruje(tmp_path):
    s = swiat_z_werdyktem(tmp_path, verdict="verdict: {pass: [zgodny], fail: [rozjazd], line: 2}")
    agent = Szpieg()

    await uruchom(s, agent)

    dodatkowe = {w.spec.step_id: w.dodatkowe for w in agent.wywolania}
    assert dodatkowe["rec"]["werdykt"] == KontraktWerdyktu(
        pass_=("zgodny",), fail=("rozjazd",), line=2)
    assert "werdykt" not in dodatkowe["autor"]
    assert "werdykt" not in dodatkowe["po"]


@pytest.mark.parametrize(
    ("wynik", "rodzaj"),
    [
        (WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0, werdykt="zgodny"), "step_finished"),
        (WynikKroku(status="nieudany", powod="werdykt: rozjazd", kod_wyjscia=0, werdykt="rozjazd"), "step_failed"),
    ],
)
async def test_werdykt_z_wyniku_kroku_trafia_do_zdarzenia_konca_kroku(tmp_path, wynik, rodzaj):
    s = swiat_z_werdyktem(tmp_path)

    zdarzenia = await uruchom(s, Szpieg(wyniki={"rec": wynik}))

    assert jedno(zdarzenia, rodzaj, "rec")["verdict"] == wynik.werdykt


async def test_wynik_kroku_bez_werdyktu_nie_dokleja_pola_verdict_do_zdarzenia(tmp_path):
    s = swiat_z_werdyktem(tmp_path)

    zdarzenia = await uruchom(s, Szpieg())

    assert "verdict" not in jedno(zdarzenia, "step_finished", "rec")

"""Model pliku projektu (.cockpit/project.yaml): stack, tickets i capabilities.

Testy przed implementacją (HAR-31). Bez bazy danych, wszystko na tmp_path.
Kody problemów porównujemy ze stałymi modułu, nie z treścią komunikatów.
"""

import json
from pathlib import Path

import pytest

from cockpit.services.projekt import (
    BLAD_ODCZYTU,
    BLAD_POLA,
    BLEDNA_CAPABILITY,
    BRAK_PROJEKTU,
    NIE_MAPA,
    NIEPOPRAWNY_YAML,
    PLIK_PROJEKTU,
    ZDUPLIKOWANY_KLUCZ,
    Pominiecie,
    Projekt,
    pominiete_kroki,
    wczytaj_projekt,
)
from cockpit.services.skills import rozstrzygnij_rejestr
from cockpit.services.workflow import Poziom, Problem, Workflow, ma_bledy, wczytaj_workflow

TRZY_CAPABILITIES = """\
stack: [python, postgres]
tickets:
  source: linear
  team: HAR
  filtry: {stan: [todo, doing]}
capabilities:
  test: pytest -q
  lint: ruff check .
  build: make build
"""


def _zapisz(repo: Path, tresc: str | bytes) -> Path:
    plik = repo / PLIK_PROJEKTU
    plik.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(tresc, bytes):
        plik.write_bytes(tresc)
    else:
        plik.write_text(tresc, encoding="utf-8")
    return plik


def _kody(problemy: list[Problem]) -> list[tuple[str | None, str]]:
    return [(p.krok, p.kod) for p in problemy]


def _odczyt_zawodzi(monkeypatch, cel: Path, wyjatek: Exception) -> None:
    """Tylko `cel` zawodzi przy odczycie; wyjątek bez nazwy pliku."""
    oryginal = Path.read_bytes

    def czytaj(self: Path) -> bytes:
        if self != cel:
            return oryginal(self)
        raise wyjatek

    monkeypatch.setattr(Path, "read_bytes", czytaj)


# --- poprawny plik -----------------------------------------------------------


def test_projekt_z_trzema_capabilities_wczytuje_nazwy_i_komendy(tmp_path: Path):
    _zapisz(tmp_path, TRZY_CAPABILITIES)

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert problemy == []
    assert projekt is not None
    assert projekt.stack == ["python", "postgres"]
    assert projekt.tickets == {
        "source": "linear",
        "team": "HAR",
        "filtry": {"stan": ["todo", "doing"]},
    }
    assert projekt.capabilities == {
        "test": "pytest -q",
        "lint": "ruff check .",
        "build": "make build",
    }
    # kolejność jak w pliku
    assert list(projekt.capabilities) == ["test", "lint", "build"]


def test_projekt_bez_tickets_i_capabilities_ma_wartosci_domyslne(tmp_path: Path):
    _zapisz(tmp_path, "stack: [python]\n")

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert problemy == []
    assert projekt == Projekt(stack=["python"])
    assert projekt.tickets is None
    assert projekt.capabilities == {}


def test_komenda_capability_zostaje_zapisana_bez_przycinania_spacji(tmp_path: Path):
    _zapisz(tmp_path, 'capabilities:\n  test: " pytest -q "\n')

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert problemy == []
    assert projekt is not None
    assert projekt.capabilities == {"test": " pytest -q "}


# --- brak pliku i błędy odczytu ----------------------------------------------


def test_brak_pliku_projektu_to_ostrzezenie_a_nie_blad(tmp_path: Path):
    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt == Projekt()
    assert projekt is not None
    assert projekt.capabilities == {}
    assert _kody(problemy) == [(None, BRAK_PROJEKTU)]
    assert problemy[0].poziom == Poziom.ostrzezenie
    assert not ma_bledy(problemy)


def test_brak_katalogu_cockpit_to_tez_brak_projektu(tmp_path: Path):
    assert not (tmp_path / ".cockpit").exists()

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt == Projekt()
    assert _kody(problemy) == [(None, BRAK_PROJEKTU)]


def test_katalog_zamiast_pliku_projektu_to_blad_odczytu_a_nie_brak_projektu(
    tmp_path: Path,
):
    (tmp_path / PLIK_PROJEKTU).mkdir(parents=True)

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, BLAD_ODCZYTU)]
    assert problemy[0].poziom == Poziom.blad


def test_plik_projektu_bez_praw_do_odczytu_to_blad_odczytu(tmp_path: Path, monkeypatch):
    plik = _zapisz(tmp_path, TRZY_CAPABILITIES)
    _odczyt_zawodzi(monkeypatch, plik, PermissionError("brak praw do odczytu"))

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, BLAD_ODCZYTU)]
    assert problemy[0].poziom == Poziom.blad


def test_plik_projektu_spoza_utf8_to_problem(tmp_path: Path):
    _zapisz(tmp_path, b"stack: [\xff\xfe]\n")

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, BLAD_ODCZYTU)]
    assert problemy[0].poziom == Poziom.blad


# --- niepoprawna składnia ----------------------------------------------------


def test_niepoprawny_yaml_projektu_to_problem_a_nie_wyjatek(tmp_path: Path):
    _zapisz(tmp_path, "stack: [python\n")

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, NIEPOPRAWNY_YAML)]
    assert problemy[0].poziom == Poziom.blad


def test_zduplikowana_capability_to_problem(tmp_path: Path):
    _zapisz(
        tmp_path,
        "capabilities:\n  test: pytest -q\n  lint: ruff check .\n  test: pytest -x\n",
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, ZDUPLIKOWANY_KLUCZ)]
    assert problemy[0].poziom == Poziom.blad


@pytest.mark.parametrize(
    "tresc",
    [
        "x: " + "[" * 5000 + "]" * 5000 + "\n",
        "tickets:\n  x: " + "[" * 5000 + "]" * 5000 + "\n",
    ],
    ids=["w_korzeniu", "w_polu_tickets"],
)
def test_zbyt_gleboko_zagniezdzony_projekt_to_problem_a_nie_wyjatek(
    tmp_path: Path, tresc: str
):
    _zapisz(tmp_path, tresc)

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, NIEPOPRAWNY_YAML)]
    assert problemy[0].poziom == Poziom.blad


@pytest.mark.parametrize(
    "tresc",
    ["- python\n- postgres\n", "python\n", "", "# tylko komentarz\n"],
    ids=["lista", "skalar", "pusty_plik", "sam_komentarz"],
)
def test_projekt_ktorego_korzen_nie_jest_mapa_to_problem(tmp_path: Path, tresc: str):
    _zapisz(tmp_path, tresc)

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert projekt is None
    assert _kody(problemy) == [(None, NIE_MAPA)]
    assert problemy[0].poziom == Poziom.blad


# --- zły typ pól -------------------------------------------------------------


@pytest.mark.parametrize(
    "stack",
    ["python", "[python, 3]", "{a: b}"],
    ids=["napis_zamiast_listy", "element_nie_napis", "mapa"],
)
def test_zly_typ_stack_pomija_pole_a_reszta_projektu_sie_wczytuje(
    tmp_path: Path, stack: str
):
    _zapisz(
        tmp_path,
        f"stack: {stack}\ntickets: {{source: linear}}\ncapabilities:\n  test: pytest -q\n",
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("stack", BLAD_POLA)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.stack == []
    assert projekt.tickets == {"source": "linear"}
    assert projekt.capabilities == {"test": "pytest -q"}


def test_tickets_z_kluczem_liczbowym_wczytuje_sie_z_kluczem_jako_napisem(
    tmp_path: Path,
):
    _zapisz(tmp_path, "tickets:\n  1: a\n  zrodlo:\n    2: b\n")

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert problemy == []
    assert projekt is not None
    assert projekt.tickets == {"1": "a", "zrodlo": {"2": "b"}}


@pytest.mark.parametrize(
    "tickets",
    ["[linear, jira]", "linear"],
    ids=["lista", "napis"],
)
def test_zly_typ_tickets_pomija_pole_a_reszta_projektu_sie_wczytuje(
    tmp_path: Path, tickets: str
):
    _zapisz(
        tmp_path,
        f"stack: [python]\ntickets: {tickets}\ncapabilities:\n  test: pytest -q\n",
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("tickets", BLAD_POLA)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.tickets is None
    assert projekt.stack == ["python"]
    assert projekt.capabilities == {"test": "pytest -q"}


def test_capabilities_bedace_lista_zamiast_mapy_pomijaja_pole(tmp_path: Path):
    _zapisz(tmp_path, "stack: [python]\ncapabilities:\n  - pytest -q\n  - ruff check .\n")

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("capabilities", BLAD_POLA)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.capabilities == {}
    assert projekt.stack == ["python"]


# --- błędne pozycje capabilities ---------------------------------------------


def test_capability_z_pusta_komenda_jest_bledem_a_reszta_zostaje(tmp_path: Path):
    _zapisz(
        tmp_path,
        'capabilities:\n  test: pytest -q\n  lint: ""\n  build: make build\n',
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("lint", BLEDNA_CAPABILITY)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.capabilities == {"test": "pytest -q", "build": "make build"}
    assert list(projekt.capabilities) == ["test", "build"]


def test_capability_z_samymi_spacjami_jest_bledem_a_reszta_zostaje(tmp_path: Path):
    _zapisz(
        tmp_path,
        'capabilities:\n  test: pytest -q\n  lint: "   "\n  build: make build\n',
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("lint", BLEDNA_CAPABILITY)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.capabilities == {"test": "pytest -q", "build": "make build"}


@pytest.mark.parametrize(
    "wartosc",
    ["123", "true", "null", "[pytest, -q]", "{cmd: pytest}"],
    ids=["liczba", "bool", "null", "lista", "mapa"],
)
def test_capability_z_komenda_niebedaca_napisem_jest_bledem_a_reszta_zostaje(
    tmp_path: Path, wartosc: str
):
    _zapisz(
        tmp_path,
        f"capabilities:\n  test: pytest -q\n  lint: {wartosc}\n  build: make build\n",
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("lint", BLEDNA_CAPABILITY)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.capabilities == {"test": "pytest -q", "build": "make build"}


def test_capability_z_kluczem_niebedacym_napisem_jest_bledem_z_nazwa_jako_tekstem(
    tmp_path: Path,
):
    _zapisz(tmp_path, "capabilities:\n  1: make\n  test: pytest -q\n")

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("1", BLEDNA_CAPABILITY)]
    assert problemy[0].poziom == Poziom.blad
    assert projekt is not None
    assert projekt.capabilities == {"test": "pytest -q"}


def test_kilka_blednych_capabilities_daje_osobny_problem_dla_kazdej(tmp_path: Path):
    _zapisz(
        tmp_path,
        'capabilities:\n  a: ""\n  test: pytest -q\n  b: 5\n',
    )

    projekt, problemy = wczytaj_projekt(tmp_path)

    assert _kody(problemy) == [("a", BLEDNA_CAPABILITY), ("b", BLEDNA_CAPABILITY)]
    assert projekt is not None
    assert projekt.capabilities == {"test": "pytest -q"}
    assert ma_bledy(problemy)


# --- pominiete kroki: brak capability to pominiecie z powodem, nie blad (HAR-32) ---


def _skill(repo: Path, id: str, needs: list[str] | None = None) -> None:
    katalog = repo / ".cockpit" / "skills" / id
    katalog.mkdir(parents=True, exist_ok=True)
    linie = [f"id: {id}", f"name: {id}", "role: author"]
    if needs:
        linie.append(f"needs: [{', '.join(needs)}]")
    (katalog / "SKILL.md").write_text("---\n" + "\n".join(linie) + "\n---\nBrief.\n", encoding="utf-8")


def _workflow(tmp_path: Path, kroki: str, *, bez_problemow: bool = True) -> Workflow:
    plik = tmp_path / "workflow.yaml"
    plik.write_text("name: w\nsteps:\n" + kroki, encoding="utf-8")
    workflow, problemy = wczytaj_workflow(plik)
    assert workflow is not None
    if bez_problemow:
        assert problemy == []
    return workflow


def _pominiete(tmp_path: Path, workflow: Workflow, capabilities: dict[str, str]) -> list[Pominiecie]:
    repo = tmp_path / "repo"
    dom = tmp_path / "dom"
    dom.mkdir(exist_ok=True)
    rejestr = rozstrzygnij_rejestr(repo, dom)
    if capabilities:
        # json.dumps daje poprawny cytowany napis YAML (znaki specjalne w komendach)
        wpisy = "".join(f"  {nazwa}: {json.dumps(komenda)}\n" for nazwa, komenda in capabilities.items())
        _zapisz(repo, "capabilities:\n" + wpisy)
    else:
        _zapisz(repo, "capabilities: {}\n")
    projekt, problemy = wczytaj_projekt(repo)
    assert projekt is not None and problemy == []
    return pominiete_kroki(workflow, rejestr, projekt)


def test_krok_wymagajacy_e2e_w_projekcie_bez_e2e_jest_pominiety_z_powodem(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e"])
    workflow = _workflow(tmp_path, "  sprawdz: {skill: tester}\n")

    wynik = _pominiete(tmp_path, workflow, {"test": "pytest -q"})

    assert wynik == [Pominiecie(krok="sprawdz", powod="brak capability e2e", brakujace=("e2e",))]


def test_krok_z_kompletem_capabilities_nie_jest_pominiety(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e", "lint"])
    workflow = _workflow(tmp_path, "  sprawdz: {skill: tester}\n")

    assert _pominiete(tmp_path, workflow, {"e2e": "playwright test", "lint": "ruff check ."}) == []


def test_skill_bez_wymaganych_capabilities_nigdy_nie_jest_pominiety(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "pisarz")
    workflow = _workflow(tmp_path, "  pisz: {skill: pisarz}\n")

    assert _pominiete(tmp_path, workflow, {}) == []


def test_dwie_brakujace_capabilities_daja_jedno_pominiecie_z_obiema_nazwami(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e", "lint"])
    workflow = _workflow(tmp_path, "  sprawdz: {skill: tester}\n")

    wynik = _pominiete(tmp_path, workflow, {})

    assert wynik == [
        Pominiecie(krok="sprawdz", powod="brak capability e2e, lint", brakujace=("e2e", "lint"))
    ]


def test_kolejnosc_nazw_w_powodzie_wynika_z_needs_skilla_nie_z_alfabetu(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["lint", "e2e"])
    workflow = _workflow(tmp_path, "  sprawdz: {skill: tester}\n")

    wynik = _pominiete(tmp_path, workflow, {})

    assert wynik == [
        Pominiecie(krok="sprawdz", powod="brak capability lint, e2e", brakujace=("lint", "e2e"))
    ]


def test_czesciowo_spelnione_wymagania_wymieniaja_tylko_brakujaca_capability(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e", "lint"])
    workflow = _workflow(tmp_path, "  sprawdz: {skill: tester}\n")

    wynik = _pominiete(tmp_path, workflow, {"lint": "ruff check ."})

    assert wynik == [Pominiecie(krok="sprawdz", powod="brak capability e2e", brakujace=("e2e",))]


def test_pominiecia_sa_w_kolejnosci_krokow_workflow_nie_alfabetycznie(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "zeta", needs=["e2e"])
    _skill(repo, "alfa", needs=["lint"])
    workflow = _workflow(
        tmp_path,
        "  zzz_pierwszy: {skill: zeta}\n  aaa_drugi: {skill: alfa}\n",
    )

    wynik = _pominiete(tmp_path, workflow, {})

    assert wynik == [
        Pominiecie(krok="zzz_pierwszy", powod="brak capability e2e", brakujace=("e2e",)),
        Pominiecie(krok="aaa_drugi", powod="brak capability lint", brakujace=("lint",)),
    ]


def test_krok_z_nieznanym_skillem_nie_jest_pomijany_z_powodu_capability(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e"])
    workflow = _workflow(
        tmp_path,
        "  duch: {skill: nie-ma-takiego}\n  sprawdz: {skill: tester}\n",
    )

    wynik = _pominiete(tmp_path, workflow, {})

    assert wynik == [Pominiecie(krok="sprawdz", powod="brak capability e2e", brakujace=("e2e",))]


def test_krok_z_blednym_polem_nie_trafia_do_pominiec_mimo_braku_capability(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e"])
    workflow = _workflow(
        tmp_path,
        '  zepsuty: {skill: tester, fanout: "3"}\n  sprawdz: {skill: tester}\n',
        bez_problemow=False,
    )
    assert workflow.niepoprawne == ["zepsuty"]

    wynik = _pominiete(tmp_path, workflow, {})

    assert [p.krok for p in wynik] == ["sprawdz"]


def test_krok_zalezny_od_pominietego_nie_jest_pomijany_przez_funkcje_planujaca(tmp_path: Path):
    repo = tmp_path / "repo"
    _skill(repo, "tester", needs=["e2e"])
    _skill(repo, "pisarz")
    workflow = _workflow(
        tmp_path,
        "  sprawdz: {skill: tester}\n  podsumuj: {skill: pisarz, needs: [sprawdz]}\n",
    )

    wynik = _pominiete(tmp_path, workflow, {})

    assert wynik == [Pominiecie(krok="sprawdz", powod="brak capability e2e", brakujace=("e2e",))]

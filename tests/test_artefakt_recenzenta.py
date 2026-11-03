"""Artefakt roli bez prawa zapisu zapisuje runner z wyniku agenta (HAR-160).

Recenzent i zbieracz nie maja `Write`: artefakt oddaja jako koncowy wynik agenta
(stdout), a runner zapisuje go atomowo w katalogu runu POZA repozytorium.
Prawdziwe procesy: atrapa agenta z `test_wykonanie_kroku` (maly skrypt Pythona
sterowany jednym argumentem JSON), uruchamiana przez `wykonaj_krok`, ktory sam
sprzata grupe procesow. Kazde czekanie ma limit czasu.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola
from runner.agent import SpecKroku
from runner import krok as modul_kroku
from runner.krok import WynikKroku, wykonaj_krok
from test_wykonanie_kroku import atrapa

KROK = "krok-23"
EVIDENCE_PUSTY = "niezmiennik evidence_required: pusty wynik agenta, brak artefaktu plan.md"
BEZ_PRAW = pytest.mark.skipif(os.geteuid() == 0, reason="root pisze bez wzgledu na prawa")


def spec(
    rola: Rola = Rola.reviewer,
    wyjscia: list[str] | None = None,
    step_id: str = KROK,
) -> SpecKroku:
    return SpecKroku(
        run_id="run-17",
        step_id=step_id,
        agent_id="agent-31",
        skill_id="planowanie",
        rola=rola,
        wejscia=[],
        wyjscia=list(wyjscia if wyjscia is not None else ["plan.md"]),
        komendy={},
    )


@pytest.fixture
def katalog(tmp_path: Path) -> Path:
    """Kopia robocza repozytorium uzytkownika (cwd agenta)."""
    k = tmp_path / "repo"
    k.mkdir()
    return k


@pytest.fixture
def katalog_runu(tmp_path: Path) -> Path:
    """Katalog runu poza repozytorium; artefakt ma lezec w <katalog_runu>/<step_id>/."""
    k = tmp_path / "run"
    k.mkdir()
    return k


@pytest.fixture
def znacznik(tmp_path: Path) -> Path:
    """Plik, ktory tworzy atrapa po starcie: jego brak dowodzi, ze agent nie ruszyl."""
    return tmp_path / "znacznik-startu.txt"


async def uruchom(
    krok: SpecKroku,
    polecenie: list[str],
    katalog: Path,
    katalog_runu: Path | None,
) -> tuple[WynikKroku, list[dict]]:
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await asyncio.wait_for(
        wykonaj_krok(krok, polecenie, katalog, emit, katalog_runu=katalog_runu), 30
    )
    return wynik, zdarzenia


def wypisz(tresc: str, **reszta) -> list[str]:
    return atrapa(linie=[["out", tresc]], **reszta)


def pliki_pod(korzen: Path) -> list[str]:
    """Wszystkie wpisy pod katalogiem (pliki, katalogi, pliki tymczasowe); brak katalogu = pusto."""
    if not korzen.exists():
        return []
    return sorted(str(p.relative_to(korzen)) for p in korzen.rglob("*"))


# --- artefakt z wyniku agenta ---------------------------------------------------


async def test_recenzent_wypisujacy_plan_ma_go_w_katalogu_runu_i_krok_zakonczony(
    katalog, katalog_runu
):
    tresc = "# Plan\n\n1. zmien A\n2. zmien B\n"

    wynik, _ = await uruchom(spec(), wypisz(tresc), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == tresc.encode()


async def test_zbieracz_wypisujacy_notatke_ma_ja_w_katalogu_runu_i_krok_zakonczony(
    katalog, katalog_runu
):
    tresc = "znalezione: trzy pliki\n"

    wynik, _ = await uruchom(
        spec(Rola.gatherer, ["plan.md"]), wypisz(tresc), katalog, katalog_runu
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == tresc.encode()


async def test_artefakt_recenzenta_jest_zapisany_dokladnie_bez_przycinania_bialych_znakow(
    katalog, katalog_runu
):
    tresc = "\n\n  wynik z wcieciem  \n\n"

    await uruchom(spec(), wypisz(tresc), katalog, katalog_runu)

    assert (katalog_runu / KROK / "plan.md").read_bytes() == tresc.encode()


async def test_artefakt_recenzenta_w_podkatalogu_powstaje_pod_katalogiem_kroku(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(wyjscia=["raporty/plan.md"]), wypisz("tresc\n"), katalog, katalog_runu
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "raporty" / "plan.md").read_text() == "tresc\n"


async def test_ponowny_krok_zastepuje_artefakt_z_poprzedniego_podejscia(katalog, katalog_runu):
    stary = katalog_runu / KROK / "plan.md"
    stary.parent.mkdir()
    stary.write_text("stary plan")

    wynik, _ = await uruchom(spec(), wypisz("nowy plan\n"), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert stary.read_text() == "nowy plan\n"
    assert pliki_pod(katalog_runu / KROK) == ["plan.md"]


async def test_bajty_spoza_utf8_w_wyniku_sa_zastapione_w_artefakcie(katalog, katalog_runu):
    wynik, _ = await uruchom(spec(), atrapa(bajty="ff0a"), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == "�\n".encode()


# --- brak wyniku to brak dowodu ---------------------------------------------------


@pytest.mark.parametrize("stdout", ["", " \n\t \n"], ids=["pusty", "same-biale-znaki"])
async def test_pusty_wynik_recenzenta_to_porazka_evidence_required_bez_pliku(
    katalog, katalog_runu, stdout
):
    wynik, _ = await uruchom(spec(), atrapa(linie=[["out", stdout]]), katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", EVIDENCE_PUSTY, 0)
    assert pliki_pod(katalog_runu) == []


async def test_samo_stderr_agenta_nie_jest_wynikiem_wiec_artefakt_nie_powstaje(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(linie=[["err", "ostrzezenie: cos poszlo nie tak\n"]]), katalog, katalog_runu
    )

    assert wynik == WynikKroku("nieudany", EVIDENCE_PUSTY, 0)
    assert pliki_pod(katalog_runu) == []


async def test_pusty_wynik_zbieracza_to_porazka_evidence_required_bez_pliku(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(spec(Rola.gatherer), atrapa(), katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", EVIDENCE_PUSTY, 0)
    assert pliki_pod(katalog_runu) == []


async def test_brak_wyniku_wygrywa_z_samowolnym_zapisem_recenzenta_poza_katalogiem_runu(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(), atrapa(zapisz={"nowy.txt": "samowola"}), katalog, katalog_runu
    )

    assert wynik == WynikKroku("nieudany", EVIDENCE_PUSTY, 0)


async def test_samowolny_zapis_recenzenta_obok_artefaktu_nadal_jest_naruszeniem_tylko_do_odczytu(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(),
        atrapa(linie=[["out", "plan\n"]], zapisz={"nowy.txt": "samowola"}),
        katalog,
        katalog_runu,
    )

    assert wynik == WynikKroku(
        "nieudany", "niezmiennik reviewer_readonly: nowe: nowy.txt", 0
    )


async def test_kod_wyjscia_niezerowy_przy_wypisanej_tresci_to_porazka_bez_pliku(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(spec(), wypisz("plan\n", kod=3), katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", "kod wyjścia 3", 3)
    assert pliki_pod(katalog_runu) == []


# --- stderr osobno od artefaktu ---------------------------------------------------


async def test_stderr_agenta_nie_trafia_do_artefaktu_ale_jest_w_zdarzeniach(
    katalog, katalog_runu
):
    wynik, zdarzenia = await uruchom(
        spec(),
        atrapa(linie=[["err", "ostrzezenie agenta\n"], ["out", "tresc planu\n"]]),
        katalog,
        katalog_runu,
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_text() == "tresc planu\n"
    assert all(z["kind"] == "output" and z["step_id"] == KROK for z in zdarzenia)
    assert sorted(z["text"] for z in zdarzenia) == ["ostrzezenie agenta", "tresc planu"]


# --- jeden artefakt, katalog runu i nazwy: sprawdzenia przed startem agenta --------


def agent_wystartowal(znacznik: Path) -> bool:
    return znacznik.exists()


def atrapa_ze_znacznikiem(znacznik: Path) -> list[str]:
    return atrapa(linie=[["out", "tresc\n"]], zapisz={str(znacznik): "wystartowal"})


async def test_recenzent_z_dwoma_artefaktami_nie_startuje_i_porazka_wymienia_oba(
    katalog, katalog_runu, znacznik
):
    wynik, zdarzenia = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa_ze_znacznikiem(znacznik), katalog, katalog_runu
    )

    assert wynik == WynikKroku(
        "nieudany", "rola reviewer oddaje jeden artefakt, zadeklarowano 2: a.md, b.md", None
    )
    assert zdarzenia == []
    assert not agent_wystartowal(znacznik)
    assert pliki_pod(katalog_runu) == []


async def test_zbieracz_z_dwoma_artefaktami_nie_startuje_i_porazka_nazywa_role(
    katalog, katalog_runu, znacznik
):
    wynik, _ = await uruchom(
        spec(Rola.gatherer, ["a.md", "b.md", "c.md"]),
        atrapa_ze_znacznikiem(znacznik),
        katalog,
        katalog_runu,
    )

    assert wynik == WynikKroku(
        "nieudany",
        "rola gatherer oddaje jeden artefakt, zadeklarowano 3: a.md, b.md, c.md",
        None,
    )
    assert not agent_wystartowal(znacznik)


async def test_recenzent_bez_katalogu_runu_nie_startuje_i_porazka_nazywa_artefakt(
    katalog, znacznik
):
    wynik, zdarzenia = await uruchom(
        spec(), atrapa_ze_znacznikiem(znacznik), katalog, None
    )

    assert wynik == WynikKroku(
        "nieudany", "brak katalogu runu dla artefaktu plan.md roli reviewer", None
    )
    assert zdarzenia == []
    assert not agent_wystartowal(znacznik)
    assert pliki_pod(katalog) == []


async def test_zbieracz_bez_katalogu_runu_nie_startuje_i_porazka_nazywa_role(
    katalog, znacznik
):
    wynik, _ = await uruchom(
        spec(Rola.gatherer), atrapa_ze_znacznikiem(znacznik), katalog, None
    )

    assert wynik == WynikKroku(
        "nieudany", "brak katalogu runu dla artefaktu plan.md roli gatherer", None
    )
    assert not agent_wystartowal(znacznik)


async def test_dwa_artefakty_wygrywaja_z_brakiem_katalogu_runu(katalog, znacznik):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa_ze_znacznikiem(znacznik), katalog, None
    )

    assert wynik == WynikKroku(
        "nieudany", "rola reviewer oddaje jeden artefakt, zadeklarowano 2: a.md, b.md", None
    )


async def test_brak_katalogu_runu_wygrywa_z_niedozwolona_nazwa_artefaktu(katalog, znacznik):
    wynik, _ = await uruchom(
        spec(wyjscia=["../plan.md"]), atrapa_ze_znacznikiem(znacznik), katalog, None
    )

    assert wynik == WynikKroku(
        "nieudany", "brak katalogu runu dla artefaktu ../plan.md roli reviewer", None
    )


@pytest.mark.parametrize(
    "nazwa",
    ["../plan.md", "a/../../plan.md", "a/../plan.md", ""],
    ids=["nadrzedny", "wyjscie-poza-krok", "srodkowy-segment", "pusta"],
)
async def test_niedozwolona_nazwa_artefaktu_nie_startuje_agenta_i_nic_nie_powstaje(
    tmp_path, katalog, katalog_runu, znacznik, nazwa
):
    wynik, zdarzenia = await uruchom(
        spec(wyjscia=[nazwa]), atrapa_ze_znacznikiem(znacznik), katalog, katalog_runu
    )

    assert wynik == WynikKroku(
        "nieudany", f"niedozwolona ścieżka artefaktu: {nazwa!r}", None
    )
    assert zdarzenia == []
    assert not agent_wystartowal(znacznik)
    assert pliki_pod(katalog_runu) == []
    assert not (tmp_path / "plan.md").exists()


async def test_bezwzgledna_sciezka_artefaktu_nie_startuje_agenta_i_nic_pod_nia_nie_powstaje(
    tmp_path, katalog, katalog_runu, znacznik
):
    poza = tmp_path / "poza" / "plan.md"

    wynik, _ = await uruchom(
        spec(wyjscia=[str(poza)]), atrapa_ze_znacznikiem(znacznik), katalog, katalog_runu
    )

    assert wynik == WynikKroku(
        "nieudany", f"niedozwolona ścieżka artefaktu: {str(poza)!r}", None
    )
    assert not agent_wystartowal(znacznik)
    assert not poza.parent.exists()
    assert pliki_pod(katalog_runu) == []


@pytest.mark.parametrize(
    "step_id",
    ["a/b", "..", ".", "", "a\\b", "/abs"],
    ids=["ukosnik", "nadrzedny", "kropka", "pusty", "odwrotny-ukosnik", "bezwzgledny"],
)
async def test_niedozwolony_identyfikator_kroku_nie_startuje_agenta_i_nic_nie_powstaje(
    tmp_path, katalog, katalog_runu, znacznik, step_id
):
    wynik, zdarzenia = await uruchom(
        spec(step_id=step_id), atrapa_ze_znacznikiem(znacznik), katalog, katalog_runu
    )

    assert wynik == WynikKroku(
        "nieudany", f"niedozwolony identyfikator kroku: {step_id!r}", None
    )
    assert zdarzenia == []
    assert not agent_wystartowal(znacznik)
    assert pliki_pod(katalog_runu) == []
    assert not (tmp_path / "plan.md").exists()


async def test_niedozwolona_nazwa_artefaktu_wygrywa_z_niedozwolonym_identyfikatorem_kroku(
    katalog, katalog_runu, znacznik
):
    wynik, _ = await uruchom(
        spec(wyjscia=["../plan.md"], step_id=".."),
        atrapa_ze_znacznikiem(znacznik),
        katalog,
        katalog_runu,
    )

    assert wynik == WynikKroku(
        "nieudany", "niedozwolona ścieżka artefaktu: '../plan.md'", None
    )


# --- run_dir_outside_repo -----------------------------------------------------------


async def test_katalog_runu_w_repozytorium_to_porazka_run_dir_outside_repo_i_nic_nie_powstaje(
    katalog, znacznik
):
    wynik, zdarzenia = await uruchom(
        spec(), atrapa_ze_znacznikiem(znacznik), katalog, katalog
    )

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("niezmiennik run_dir_outside_repo: ")
    assert zdarzenia == []
    assert not agent_wystartowal(znacznik)
    assert pliki_pod(katalog) == []


async def test_katalog_kroku_rowny_repozytorium_to_porazka_run_dir_outside_repo(
    tmp_path, znacznik
):
    # step_id "worktree": katalog_runu/worktree to dokladnie kopia robocza agenta.
    katalog_runu = tmp_path / "run"
    katalog = katalog_runu / "worktree"
    katalog.mkdir(parents=True)

    wynik, _ = await uruchom(
        spec(step_id="worktree"), atrapa_ze_znacznikiem(znacznik), katalog, katalog_runu
    )

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("niezmiennik run_dir_outside_repo: ")
    assert not agent_wystartowal(znacznik)
    assert pliki_pod(katalog) == []


async def test_katalog_runu_obok_repozytorium_nie_jest_naruszeniem(tmp_path, katalog):
    katalog_runu = tmp_path / "run-obok"
    katalog_runu.mkdir()

    wynik, _ = await uruchom(spec(), wypisz("plan\n"), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0)


# --- limit rozmiaru -------------------------------------------------------------------


async def test_wynik_dluzszy_niz_limit_to_porazka_bez_pliku(
    monkeypatch, katalog, katalog_runu
):
    monkeypatch.setattr("runner.krok.LIMIT_ARTEFAKTU", 100)

    wynik, _ = await uruchom(
        spec(), atrapa(bajty=(b"x" * 101).hex()), katalog, katalog_runu
    )

    assert wynik == WynikKroku("nieudany", "wynik agenta przekracza 100 bajtów", 0)
    assert pliki_pod(katalog_runu) == []


async def test_wynik_rowny_limitowi_jest_zapisany(monkeypatch, katalog, katalog_runu):
    monkeypatch.setattr("runner.krok.LIMIT_ARTEFAKTU", 100)

    wynik, _ = await uruchom(
        spec(), atrapa(bajty=(b"x" * 100).hex()), katalog, katalog_runu
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == b"x" * 100


async def test_limit_liczy_bajty_a_nie_znaki(monkeypatch, katalog, katalog_runu):
    monkeypatch.setattr("runner.krok.LIMIT_ARTEFAKTU", 100)
    litera = "ą".encode()  # 2 bajty

    wynik, _ = await uruchom(
        spec(), atrapa(bajty=(litera * 51).hex()), katalog, katalog_runu
    )

    assert wynik.status == "nieudany"
    assert wynik.powod == "wynik agenta przekracza 100 bajtów"
    assert pliki_pod(katalog_runu) == []


async def test_wynik_rowny_limitowi_w_bajtach_wielobajtowych_znakow_jest_zapisany(
    monkeypatch, katalog, katalog_runu
):
    monkeypatch.setattr("runner.krok.LIMIT_ARTEFAKTU", 100)
    litera = "ą".encode()

    wynik, _ = await uruchom(
        spec(), atrapa(bajty=(litera * 50).hex()), katalog, katalog_runu
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_bytes() == litera * 50


async def test_stderr_nie_liczy_sie_do_limitu_rozmiaru_artefaktu(
    monkeypatch, katalog, katalog_runu
):
    monkeypatch.setattr("runner.krok.LIMIT_ARTEFAKTU", 100)

    wynik, _ = await uruchom(
        spec(),
        atrapa(linie=[["err", "e" * 500 + "\n"], ["out", "krotki plan\n"]]),
        katalog,
        katalog_runu,
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_text() == "krotki plan\n"


# --- zapis atomowy i bledy zapisu ---------------------------------------------------


async def test_katalog_kroku_zajety_przez_plik_to_porazka_zapisu_artefaktu(
    katalog, katalog_runu
):
    (katalog_runu / KROK).write_text("to jest plik, nie katalog")

    wynik, _ = await uruchom(spec(), wypisz("plan\n"), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == 0
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można zapisać artefaktu plan.md: ")
    assert (katalog_runu / KROK).read_text() == "to jest plik, nie katalog"


@BEZ_PRAW
async def test_katalog_kroku_bez_prawa_zapisu_to_porazka_zapisu_bez_pliku_tymczasowego(
    katalog, katalog_runu
):
    krok = katalog_runu / KROK
    krok.mkdir()
    krok.chmod(0o500)
    try:
        wynik, _ = await uruchom(spec(), wypisz("plan\n"), katalog, katalog_runu)
        zawartosc = pliki_pod(krok)
    finally:
        krok.chmod(0o755)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == 0
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można zapisać artefaktu plan.md: ")
    assert zawartosc == []


async def test_nieudane_podmienienie_artefaktu_nie_zostawia_pliku_tymczasowego(
    katalog, katalog_runu
):
    # Cel jest katalogiem: zapis pliku tymczasowego sie udaje, podmiana (os.replace) nie.
    cel = katalog_runu / KROK / "plan.md"
    cel.mkdir(parents=True)

    wynik, _ = await uruchom(spec(), wypisz("plan\n"), katalog, katalog_runu)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == 0
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można zapisać artefaktu plan.md: ")
    assert cel.is_dir()
    assert pliki_pod(katalog_runu / KROK) == ["plan.md"]


# --- kryterium: w repozytorium uzytkownika nie ma artefaktu -----------------------------


def git(katalog: Path, *argumenty: str) -> str:
    wynik = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *argumenty],
        cwd=katalog,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return wynik.stdout


async def test_artefakt_recenzenta_nie_pojawia_sie_w_git_status_repozytorium(
    katalog, katalog_runu
):
    git(katalog, "init", "-q")
    (katalog / "kod.py").write_text("print('x')\n")
    git(katalog, "add", "kod.py")
    git(katalog, "commit", "-q", "-m", "stan poczatkowy")

    wynik, _ = await uruchom(spec(), wypisz("# Plan\n"), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert git(katalog, "status", "--porcelain") == ""
    assert (katalog_runu / KROK / "plan.md").read_text() == "# Plan\n"


# --- tryb tylko dla roli bez zapisu --------------------------------------------------------


@pytest.mark.parametrize("rola", [Rola.author, Rola.executor])
async def test_rola_z_zapisem_pisze_artefakt_sama_a_jej_wynik_nie_trafia_do_katalogu_runu(
    katalog, katalog_runu, rola
):
    wynik, _ = await uruchom(
        spec(rola, ["a.md"]),
        atrapa(linie=[["out", "gadanie agenta\n"]], zapisz={"a.md": "praca autora"}),
        katalog,
        katalog_runu,
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog / "a.md").read_text() == "praca autora"
    assert pliki_pod(katalog_runu) == []


async def test_recenzent_bez_deklarowanych_artefaktow_konczy_krok_i_nic_nie_zapisuje(
    katalog, katalog_runu
):
    wynik, _ = await uruchom(
        spec(wyjscia=[]), wypisz("uwagi bez artefaktu\n"), katalog, katalog_runu
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert pliki_pod(katalog_runu) == []


# --- sprzatanie drugiego czytnika i pelny stderr ---------------------------------------


async def test_wyjatek_z_odbiorcy_zdarzen_zatrzymuje_tez_czytnik_stderr_recenzenta(
    katalog, katalog_runu
):
    # Agent: trzy linie stderr naraz, po chwili linia stdout, potem czeka na SIGTERM
    # od straznika. Odbiorca zdarzen przy kazdej linii stderr "przetwarza" ja 2 s,
    # a na linii stdout rzuca. Czytnik stderr stoi wtedy w srodku przetwarzania z dwiema
    # liniami w zapasie: bez anulowania wznowi sie po wyjsciu wyjatku i wyemituje je.
    kod_agenta = (
        "import sys, time\n"
        "sys.stderr.write('e1\\ne2\\ne3\\n'); sys.stderr.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stdout.write('pierwsza\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    wywolania: list[str] = []

    class BladOdbiorcy(Exception):
        pass

    async def emit(zdarzenie: dict) -> None:
        wywolania.append(zdarzenie["text"])
        if zdarzenie["text"] == "pierwsza":
            raise BladOdbiorcy("odbiorca zdarzen padl")
        await asyncio.sleep(2)

    with pytest.raises(BladOdbiorcy) as blad:
        await asyncio.wait_for(
            wykonaj_krok(
                spec(), [sys.executable, "-I", "-c", kod_agenta], katalog, emit,
                katalog_runu=katalog_runu,
            ),
            30,
        )
    po_wyjatku = list(wywolania)
    await asyncio.sleep(3)

    assert type(blad.value) is BladOdbiorcy
    assert po_wyjatku[0] == "e1"
    assert "pierwsza" in po_wyjatku
    assert wywolania == po_wyjatku
    assert pliki_pod(katalog_runu) == []


async def test_pelny_stderr_agenta_nie_blokuje_kroku_recenzenta_a_artefakt_ma_tylko_stdout(
    katalog, katalog_runu
):
    kod_agenta = (
        "import sys\n"
        "sys.stderr.write('e' * 300000 + '\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('tresc planu\\n'); sys.stdout.flush()\n"
    )
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await asyncio.wait_for(
        wykonaj_krok(
            spec(), [sys.executable, "-I", "-c", kod_agenta], katalog, emit,
            katalog_runu=katalog_runu,
        ),
        30,
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").read_text() == "tresc planu\n"
    assert any(z["text"] == "e" * 300000 for z in zdarzenia)


# --- domyslny limit 8 MiB (bez podmiany stalej) -------------------------------------


MIB8 = 8 * 1024 * 1024


async def test_wynik_dokladnie_8_mib_jest_zapisany_przy_domyslnym_limicie(katalog, katalog_runu):
    # "dluga" dopisuje "\n": n znakow + newline = MIB8 bajtow
    wynik, _ = await uruchom(spec(), atrapa(dluga=MIB8 - 1), katalog, katalog_runu)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (katalog_runu / KROK / "plan.md").stat().st_size == MIB8


async def test_wynik_8_mib_i_bajt_to_porazka_przy_domyslnym_limicie(katalog, katalog_runu):
    wynik, _ = await uruchom(spec(), atrapa(dluga=MIB8), katalog, katalog_runu)

    assert wynik == WynikKroku("nieudany", f"wynik agenta przekracza {MIB8} bajtów", 0)
    assert pliki_pod(katalog_runu) == []


# --- bufor wyniku jest ograniczony (ochrona pamieci) ---------------------------------


async def test_bufor_wyniku_zatrzymuje_sie_na_limicie_plus_jeden_bajt(monkeypatch):
    monkeypatch.setattr("runner.krok.ROZMIAR_KAWALKA", 10)
    strumien = asyncio.StreamReader()
    for kawalek in (b"A" * 10, b"B" * 10, b"C" * 10):
        strumien.feed_data(kawalek)
    strumien.feed_eof()
    bufor = bytearray()
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(
        modul_kroku._emituj_strumien(strumien, spec(), emit, bufor, 5), 10
    )

    assert bytes(bufor) == b"AAAAAA"
    assert "".join(z["text"] for z in zdarzenia) == "A" * 10 + "B" * 10 + "C" * 10


# --- plik tymczasowy w katalogu docelowym --------------------------------------------


def test_plik_tymczasowy_artefaktu_lezy_w_katalogu_docelowym(monkeypatch, tmp_path):
    wywolania: list[tuple[str, str]] = []
    prawdziwe = os.replace

    def podmiana(zrodlo, cel):
        wywolania.append((str(zrodlo), str(cel)))
        prawdziwe(zrodlo, cel)

    monkeypatch.setattr(modul_kroku.os, "replace", podmiana)
    cel = tmp_path / "docelowy" / "plan.md"

    modul_kroku._zapisz_atomowo(cel, b"tresc")

    assert len(wywolania) == 1
    zrodlo, miejsce = wywolania[0]
    assert Path(zrodlo).parent == cel.parent
    assert Path(zrodlo) != cel
    assert Path(miejsce) == cel
    assert cel.read_bytes() == b"tresc"

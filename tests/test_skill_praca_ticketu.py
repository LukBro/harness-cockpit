"""Bramki skilla `praca-ticketu` (.claude/skills/praca-ticketu/scripts) na tymczasowym repozytorium git.

Przeniesienie skryptu audytu bramek (39 przypadków) do repozytorium, PR #176. Każdy test uruchamia
prawdziwy skrypt jako osobny proces: przez `tests/procesy.py`, z `cwd` = korzeń repozytorium
liczony od pliku testu, z limitem czasu. Repozytorium, na którym działa skrypt, wskazują
`GIT_DIR` i `GIT_WORK_TREE`; konfiguracja gita użytkownika jest odcięta (`GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_NOSYSTEM`, własny `HOME`), a tożsamość autora ustawia test w konfiguracji lokalnej.

Skrypty są poza `source_paths` mutmuta - gałęzie kodów wyjścia chroni tabela
`tests/mutanty_reczne/praca-ticketu.toml`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from procesy import LimitCzasuTestu, uruchom_proces_sync, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parents[1]
SKRYPTY = KORZEN / ".claude" / "skills" / "praca-ticketu" / "scripts"
RUBRYKA = KORZEN / ".claude" / "skills" / "praca-ticketu" / "review-rubric.md"
LIMIT = 120.0
KEY = "HAR-12"
BRANCH = "worktree-HAR-12"
WLASCICIEL = "wlasciciel@example.invalid"
PY = sys.executable
OBCY_SKROT = "0000000000000000"

WERDYKT_APPROVED = "**Code Review**\n\nbackend: 100 -5 (a.py:2 konwencja) = 95/100\ntesty: 100 = 100/100\n"
WERDYKT_NEEDS_WORK = (
    "**Code Review**\n\n"
    "backend: 100 -30 (a.py:1 zapis) -20 (brak testu) = 50/100\n"
    "  -> NEEDS WORK: x\n"
    "  REGULA: Każdy zapis ma test.\n\n"
    "testy: 100 = 100/100\n"
    "  -> APPROVED\n"
)
WERDYKT_BLOKADY = (
    "**Code Review**\n\n"
    "backend: 100 -15 (ci.yml:4 brak joba; BLOKADA-ZEWNĘTRZNA: token ustawia człowiek) = 85/100\n"
    "testy: 100 -25 (x; BLOKADA-ZEWNĘTRZNA: brak bazy na maszynie) = 75/100\n"
)
KOMUNIKAT_OK = f"{KEY}: Dodaj X\n"
KOMUNIKAT_ZAMKNIECIA_INNY = "bramka_zamkniecia kod {kod} (5 = zamknięcie na decyzję człowieka, bez auto-commita)"


@dataclass(frozen=True)
class Wynik:
    kod: int
    stdout: str
    stderr: str

    @property
    def linie(self) -> list[str]:
        return self.stdout.splitlines()

    @property
    def ostatnia(self) -> str:
        return self.linie[-1] if self.linie else ""


def _uruchom(argv: list[str], env: dict[str, str], nazwa: str) -> Wynik:
    proc = uruchom_proces_sync(
        argv, cwd=KORZEN, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        try:
            out, err = proc.communicate(timeout=LIMIT)
        except subprocess.TimeoutExpired:
            raise LimitCzasuTestu(f"{nazwa}: przekroczono limit {LIMIT} s") from None
    finally:
        zakoncz_proces_sync(proc, nazwa=nazwa)
    return Wynik(proc.returncode, out, err)


def _srodowisko(dom: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "MONOLYNX_"))}
    env.update(
        HOME=str(dom), XDG_CONFIG_HOME=str(dom / ".config"), GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
        MONOLYNX_BASE_BRANCH="main", MONOLYNX_BRANCH_MODE="ticket",
    )
    return env


class Epizod:
    """Repozytorium robocze `repo` (branch worktree-HAR-12) z bazowym `origin.git`."""

    def __init__(self, tmp: Path) -> None:
        tmp.mkdir()
        # Ścieżka bez dowiązań: git i skrypty wypisują katalog stanu w tej samej postaci co test.
        tmp = tmp.resolve()
        self.tmp = tmp
        self.pliki = tmp / "pliki"
        self.pliki.mkdir()
        (tmp / "dom").mkdir()
        self.env = _srodowisko(tmp / "dom")
        self.origin = tmp / "origin.git"
        self.repo = tmp / "repo"
        self.git_w(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git_w(tmp, "init", "-q", "-b", "main", str(self.repo))
        self.ustaw_tozsamosc(self.repo)
        self.git("remote", "add", "origin", str(self.origin))
        self.zapisz("a.py", "x = 1\n")
        self.git("add", "a.py")
        self.git("commit", "-q", "-m", "init")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "-b", BRANCH)
        self.sha = self.git("rev-parse", "HEAD").strip()
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        self.kat = Path(common) / "monolynx" / KEY

    # --- git i pliki -------------------------------------------------------------

    def git_w(self, katalog: Path, *a: str) -> str:
        w = _uruchom(["git", "-C", str(katalog), *a], self.env, f"git {a[0]}")
        assert w.kod == 0, f"git {' '.join(a)}: {w.stderr}"
        return w.stdout

    def git(self, *a: str) -> str:
        return self.git_w(self.repo, *a)

    def ustaw_tozsamosc(self, katalog: Path, email: str = WLASCICIEL) -> None:
        self.git_w(katalog, "config", "user.name", "Autor Testowy")
        self.git_w(katalog, "config", "user.email", email)
        self.git_w(katalog, "config", "commit.gpgsign", "false")

    def zapisz(self, nazwa: str, tresc: str, dopisz: bool = False) -> None:
        with open(self.repo / nazwa, "a" if dopisz else "w", encoding="utf-8") as f:
            f.write(tresc)

    def plik(self, nazwa: str, tresc: str) -> str:
        """Plik wejściowy skryptu poza drzewem roboczym (nie zmienia skrótu diffu)."""
        p = self.pliki / nazwa
        p.write_text(tresc, encoding="utf-8")
        return str(p)

    # --- skrypty -----------------------------------------------------------------

    def skrypt(self, nazwa: str, *a: str) -> Wynik:
        env = dict(self.env, GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(self.repo))
        return _uruchom([PY, "-I", str(SKRYPTY / nazwa), *a], env, nazwa)

    # --- stan epizodu ------------------------------------------------------------

    def stan(self) -> dict:
        return json.loads((self.kat / "stan.json").read_text(encoding="utf-8"))

    def zapisz_stan(self, d: dict) -> None:
        self.kat.mkdir(parents=True, exist_ok=True)
        (self.kat / "stan.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def zmien_stan(self, **pola: object) -> None:
        d = self.stan()
        d.update(pola)
        self.zapisz_stan(d)

    def stan_bazowy(self, **pola: object) -> None:
        d = {
            "key": KEY, "baza": "main",
            "baseline": {"sha": self.sha, "pliki_sprzed_startu": ["stary.py"]},
            "przydzial": {"backend": ["a.py", "b.py"], "testy": ["test_a.py"]},
            "pliki_tm": [],
            "kryteria": [{"id": "k1", "zrobione": True, "dowod": "a.py:2"}],
        }
        d.update(pola)
        self.zapisz_stan(d)

    def bramki(self, lista: list) -> None:
        self.kat.mkdir(parents=True, exist_ok=True)
        (self.kat / "bramki.json").write_text(json.dumps(lista), encoding="utf-8")

    def log(self, nazwa: str) -> str:
        return str(self.kat / f"bramka-{nazwa}.log")


@pytest.fixture
def ep(tmp_path: Path) -> Epizod:
    return Epizod(tmp_path / "epizod")


BRAMKA_OK = {"nazwa": "lint", "argv": [PY, "-c", "print('ok')"]}


@pytest.fixture
def gotowy(ep: Epizod) -> Epizod:
    """Epizod gotowy do commita: zielone bramki i APPROVED dla bieżącego diffu, kryteria z dowodem,
    a.py i b.py w indeksie. Pliki c.py (spoza przydziału) i stary.py (sprzed startu) leżą w drzewie
    nieśledzone - test, który doda je do indeksu, zmienia tylko zawartość indeksu, nie skrót diffu."""
    ep.stan_bazowy()
    ep.zapisz("a.py", "y = 2\n", dopisz=True)
    ep.zapisz("b.py", "b = 1\n")
    ep.zapisz("c.py", "c = 1\n")
    ep.zapisz("stary.py", "s = 1\n")
    ep.bramki([BRAMKA_OK])
    assert ep.skrypt("bramka_testow.py", KEY).kod == 0
    assert ep.skrypt("werdykt.py", KEY, "--plik", ep.plik("w.md", WERDYKT_APPROVED)).kod == 0
    ep.git("add", "a.py", "b.py")
    assert ep.skrypt("bramka_zamkniecia.py", KEY).kod == 0
    return ep


def _skrot_biezacy(ep: Epizod) -> str:
    """Skrót bieżącego diffu z ostatniej linii bramki zamknięcia (`ZAMKNIECIE: kod=n skrot=<s>`)."""
    return ep.skrypt("bramka_zamkniecia.py", KEY).ostatnia.split("skrot=", 1)[1]


def _commit(ep: Epizod, komunikat: str = KOMUNIKAT_OK, *opcje: str) -> Wynik:
    return ep.skrypt("bramka_commita.py", KEY, "--komunikat", ep.plik("msg.txt", komunikat), *opcje)


def _commit_z_regulami(ep: Epizod, komunikat: str = KOMUNIKAT_OK) -> Wynik:
    return _commit(ep, komunikat, "--bez-wspolautora", "--autor-email", WLASCICIEL)


# =============================================================================
# bramka_commita.py
# =============================================================================


def test_commit_zgodny_z_regulami_przechodzi_bramke(gotowy):
    w = _commit_z_regulami(gotowy)
    assert w.kod == 0, w.stdout + w.stderr
    assert w.linie == ["COMMIT-GATE: kod=0"]


@pytest.mark.parametrize("linia", [
    "Co-Authored-By: Ktoś <k@example.invalid>",
    "co-authored-by: ktos <k@example.invalid>",
])
def test_commit_z_linia_wspolautora_nie_przechodzi_bramki(gotowy, linia):
    w = _commit_z_regulami(gotowy, f"{KEY}: Dodaj X\n\n{linia}\n")
    assert w.kod == 1
    assert w.linie == ["BRAK: komunikat zawiera Co-Authored-By (zakazane przez projekt)", "COMMIT-GATE: kod=1"]


def test_linia_wspolautora_bez_opcji_zakazu_nie_blokuje_commita(gotowy):
    w = _commit(gotowy, f"{KEY}: Dodaj X\n\nCo-Authored-By: Ktoś <k@example.invalid>\n",
                "--autor-email", WLASCICIEL)
    assert w.kod == 0, w.stdout


def test_commit_przy_innym_user_email_niz_wymagany_nie_przechodzi_bramki(gotowy):
    gotowy.git("config", "user.email", "ktos@example.invalid")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == [
        f"BRAK: git config user.email = 'ktos@example.invalid', oczekiwano '{WLASCICIEL}'",
        "COMMIT-GATE: kod=1",
    ]


def test_commit_bez_ustawionego_user_email_nie_przechodzi_bramki(gotowy):
    gotowy.git("config", "--unset", "user.email")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == [f"BRAK: git config user.email = '', oczekiwano '{WLASCICIEL}'", "COMMIT-GATE: kod=1"]


def test_commit_z_plikiem_spoza_przydzialu_nie_przechodzi_bramki(gotowy):
    gotowy.git("add", "c.py")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == ["BRAK: w indeksie plik spoza przydziału: c.py", "COMMIT-GATE: kod=1"]


def test_plik_team_managera_w_indeksie_przechodzi_bramke(gotowy):
    gotowy.zmien_stan(pliki_tm=["c.py"])
    gotowy.git("add", "c.py")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 0, w.stdout


def test_commit_z_plikiem_sprzed_startu_nie_przechodzi_bramki_nawet_gdy_jest_w_przydziale(gotowy):
    gotowy.zmien_stan(pliki_tm=["stary.py"])
    gotowy.git("add", "stary.py")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == ["BRAK: w indeksie plik sprzed startu: stary.py", "COMMIT-GATE: kod=1"]


def test_commit_na_branchu_bazowym_nie_przechodzi_bramki(gotowy):
    gotowy.git("checkout", "-q", "main")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == ["BRAK: commit na branchu bazowym main", "COMMIT-GATE: kod=1"]


def test_commit_z_pustym_indeksem_nie_przechodzi_bramki(gotowy):
    gotowy.git("reset", "-q")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == ["BRAK: indeks pusty", "COMMIT-GATE: kod=1"]


@pytest.mark.parametrize("komunikat", ["Dodaj X\n", "\n"])
def test_komunikat_bez_klucza_ticketu_w_pierwszej_linii_nie_przechodzi_bramki(gotowy, komunikat):
    w = _commit_z_regulami(gotowy, komunikat)
    assert w.kod == 1
    assert w.linie == [f"BRAK: pierwsza linia komunikatu nie zaczyna się od {KEY}", "COMMIT-GATE: kod=1"]


def test_commit_po_zmianie_kodu_bez_ponownej_oceny_nie_przechodzi_bramki(gotowy):
    gotowy.zapisz("a.py", "z = 3\n", dopisz=True)
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == ["BRAK: " + KOMUNIKAT_ZAMKNIECIA_INNY.format(kod=1), "COMMIT-GATE: kod=1"]


def test_commit_przy_zamknieciu_na_decyzje_czlowieka_nie_przechodzi_bramki(gotowy):
    gotowy.zapisz("a.py", "z = 3\n", dopisz=True)
    gotowy.zmien_stan(zamkniecie_wyjatek="decyzja człowieka 2026-11-10")
    w = _commit_z_regulami(gotowy)
    assert w.kod == 1
    assert w.linie == ["BRAK: " + KOMUNIKAT_ZAMKNIECIA_INNY.format(kod=5), "COMMIT-GATE: kod=1"]


def test_bramka_commita_bez_pliku_komunikatu_to_brak_dowodu(gotowy):
    w = gotowy.skrypt("bramka_commita.py", KEY, "--komunikat", str(gotowy.pliki / "nie-ma.txt"))
    assert w.kod == 2
    assert w.linie == ["COMMIT-GATE: kod=2"]


def test_bramka_commita_bez_stanu_epizodu_to_brak_dowodu(gotowy):
    w = gotowy.skrypt("bramka_commita.py", "HAR-77", "--komunikat", gotowy.plik("m.txt", "HAR-77: x\n"))
    assert w.kod == 2
    assert w.linie == ["COMMIT-GATE: kod=2"]


@pytest.mark.parametrize("argumenty", [
    # Pierwszy argument-opcja: bez tego warunku "--bez-wspolautora" byłby kluczem ticketu (kod 2, nie 64).
    [], ["--bez-wspolautora", "--komunikat", "m.txt"], [KEY], [KEY, "--nieznana"], [KEY, "--komunikat"],
])
def test_bramka_commita_przy_zlym_wywolaniu_konczy_sie_bledem_uzycia(gotowy, argumenty):
    w = gotowy.skrypt("bramka_commita.py", *argumenty)
    assert w.kod == 64
    assert w.stdout == ""


# =============================================================================
# bramka_zamkniecia.py
# =============================================================================


def test_komplet_bramek_werdyktu_i_kryteriow_pozwala_zamknac(gotowy):
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    skrot = gotowy.stan()["bramki"]["lint"]["skrot_diffu"]
    assert w.kod == 0
    assert w.linie == [f"ZAMKNIECIE: kod=0 skrot={skrot}"]


def test_zamkniecie_gdy_skrot_bramki_rozni_sie_od_biezacego_diffu_nie_przechodzi(gotowy):
    teraz = _skrot_biezacy(gotowy)
    st = gotowy.stan()
    st["bramki"]["lint"]["skrot_diffu"] = OBCY_SKROT
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie == [f"BRAK: bramka lint: wynik dotyczy innego diffu ({OBCY_SKROT} != {teraz})",
                       f"ZAMKNIECIE: kod=1 skrot={teraz}"]


def test_zamkniecie_gdy_skrot_werdyktu_rozni_sie_od_biezacego_diffu_nie_przechodzi(gotowy):
    teraz = _skrot_biezacy(gotowy)
    st = gotowy.stan()
    st["werdykty"][-1]["skrot_diffu"] = OBCY_SKROT
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie == ["BRAK: krytyk: werdykt dotyczy innego diffu - kod zmieniono po ocenie",
                       f"ZAMKNIECIE: kod=1 skrot={teraz}"]


def test_zmiana_kodu_po_bramkach_i_werdykcie_blokuje_zamkniecie(gotowy):
    przed = gotowy.stan()["bramki"]["lint"]["skrot_diffu"]
    gotowy.zapisz("a.py", "z = 3\n", dopisz=True)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    teraz = w.ostatnia.split("skrot=", 1)[1]
    assert w.kod == 1
    assert teraz != przed
    assert w.linie == [f"BRAK: bramka lint: wynik dotyczy innego diffu ({przed} != {teraz})",
                       "BRAK: krytyk: werdykt dotyczy innego diffu - kod zmieniono po ocenie",
                       f"ZAMKNIECIE: kod=1 skrot={teraz}"]


def test_nieuruchomiona_bramka_blokuje_zamkniecie(gotowy):
    gotowy.bramki([BRAMKA_OK, {"nazwa": "testy", "argv": [PY, "-c", "pass"]}])
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: bramka testy: nie uruchomiona"]


@pytest.mark.parametrize("wynik", ["czerwony", "brak-dowodu", "reczne"])
def test_bramka_niezielona_bez_potwierdzenia_blokuje_zamkniecie(gotowy, wynik):
    st = gotowy.stan()
    st["bramki"]["lint"]["wynik"] = wynik
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == [f"BRAK: bramka lint: wynik {wynik}"]


def test_bramka_do_recznego_sprawdzenia_potwierdzona_dla_biezacego_diffu_pozwala_zamknac(gotowy):
    teraz = _skrot_biezacy(gotowy)
    st = gotowy.stan()
    st["bramki"]["lint"]["wynik"] = "reczne"
    st["reczne_potwierdzone"] = teraz
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 0, w.stdout


def test_potwierdzenie_reczne_dla_innego_diffu_nie_otwiera_zamkniecia(gotowy):
    st = gotowy.stan()
    st["bramki"]["lint"]["wynik"] = "reczne"
    st["reczne_potwierdzone"] = OBCY_SKROT
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: bramka lint: wynik reczne"]


def test_brak_werdyktu_krytyka_blokuje_zamkniecie(gotowy):
    gotowy.zmien_stan(werdykty=[])
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: krytyk: brak werdyktu"]


def test_ostatni_werdykt_needs_work_blokuje_zamkniecie(gotowy):
    st = gotowy.stan()
    st["werdykty"][-1].update(kod=1, status="NEEDS-WORK")
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: krytyk: ostatni werdykt NEEDS-WORK"]


def test_zamkniecie_po_werdykcie_bez_ocenianych_agentow_nie_przechodzi(gotowy):
    gotowy.zmien_stan(przydzial={}, pliki_tm=["a.py", "b.py"])
    w = gotowy.skrypt("werdykt.py", KEY, "--plik", gotowy.plik("w0.md", "**Code Review**\n\nStatus: APPROVED\n"))
    assert w.kod == 2
    z = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert z.kod == 1
    assert z.linie[:-1] == ["BRAK: krytyk: ostatni werdykt BRAK"]


def test_brak_listy_kryteriow_blokuje_zamkniecie(gotowy):
    st = gotowy.stan()
    del st["kryteria"]
    gotowy.zapisz_stan(st)
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: kryteria: brak listy w stanie (pusta lista [] = ticket bez kryteriów)"]


def test_ticket_bez_kryteriow_z_pusta_lista_pozwala_zamknac(gotowy):
    gotowy.zmien_stan(kryteria=[])
    assert gotowy.skrypt("bramka_zamkniecia.py", KEY).kod == 0


@pytest.mark.parametrize("kryterium", [{"id": "k1"}, {"id": "k1", "zrobione": "true", "dowod": "a.py:2"}, "k1"])
def test_kryterium_bez_decyzji_blokuje_zamkniecie(gotowy, kryterium):
    gotowy.zmien_stan(kryteria=[kryterium])
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: kryterium k1: brak decyzji"]


@pytest.mark.parametrize("dowod", ["", "   ", None])
def test_kryterium_zrobione_bez_dowodu_blokuje_zamkniecie(gotowy, dowod):
    gotowy.zmien_stan(kryteria=[{"id": "k1", "zrobione": True, "dowod": dowod}])
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 1
    assert w.linie[:-1] == ["BRAK: kryterium k1: zrobione bez dowodu"]


def test_kryterium_niezrealizowane_jest_wypisane_i_nie_blokuje_zamkniecia(gotowy):
    gotowy.zmien_stan(kryteria=[{"id": "k1", "zrobione": False}])
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 0
    assert w.linie[:-1] == ["NIEZREALIZOWANE: k1"]


def test_wyjatek_czlowieka_przy_niespelnionych_warunkach_daje_zamkniecie_bez_auto_commita(gotowy):
    gotowy.zapisz("a.py", "z = 3\n", dopisz=True)
    gotowy.zmien_stan(zamkniecie_wyjatek="decyzja człowieka 2026-11-10")
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 5
    assert w.ostatnia.startswith("ZAMKNIECIE: kod=5 skrot=")


def test_wyjatek_czlowieka_przy_spelnionych_warunkach_nie_zmienia_wyniku(gotowy):
    gotowy.zmien_stan(zamkniecie_wyjatek="decyzja człowieka 2026-11-10")
    assert gotowy.skrypt("bramka_zamkniecia.py", KEY).kod == 0


def test_pusty_wyjatek_czlowieka_nie_otwiera_zamkniecia(gotowy):
    gotowy.zapisz("a.py", "z = 3\n", dopisz=True)
    gotowy.zmien_stan(zamkniecie_wyjatek="   ")
    assert gotowy.skrypt("bramka_zamkniecia.py", KEY).kod == 1


def test_zamkniecie_bez_zadnej_bramki_to_brak_dowodu(gotowy):
    teraz = _skrot_biezacy(gotowy)
    gotowy.bramki([])
    w = gotowy.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 2
    assert w.linie == [f"ZAMKNIECIE: kod=2 skrot={teraz}"]


def test_zamkniecie_bez_stanu_epizodu_to_brak_dowodu(ep):
    w = ep.skrypt("bramka_zamkniecia.py", KEY)
    assert w.kod == 2
    assert w.linie == ["ZAMKNIECIE: kod=2 skrot=-"]


@pytest.mark.parametrize("argumenty", [[], [KEY, "nadmiar"]])
def test_bramka_zamkniecia_przy_zlym_wywolaniu_konczy_sie_bledem_uzycia(ep, argumenty):
    w = ep.skrypt("bramka_zamkniecia.py", *argumenty)
    assert w.kod == 64
    assert w.stdout == ""


# =============================================================================
# bramka_testow.py
# =============================================================================


def _wyjscie(kod: int) -> list[str]:
    return [PY, "-c", f"import sys; sys.exit({kod})"]


def test_zielone_bramki_zapisuja_wynik_ze_skrotem_diffu(ep):
    ep.stan_bazowy()
    ep.bramki([BRAMKA_OK])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 0
    assert w.linie == [f"BRAMKA lint: wynik=zielony kod=0 log={ep.log('lint')}", "BRAMKI: kod=0"]
    zapis = ep.stan()["bramki"]["lint"]
    assert (zapis["wynik"], zapis["kod"], len(zapis["skrot_diffu"])) == ("zielony", 0, 16)


def test_czerwona_bramka_daje_kod_poprawki(ep):
    ep.stan_bazowy()
    ep.bramki([BRAMKA_OK, {"nazwa": "testy", "argv": _wyjscie(1)}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 1
    assert f"BRAMKA testy: wynik=czerwony kod=1 log={ep.log('testy')}" in w.linie
    assert w.ostatnia == "BRAMKI: kod=1"


def test_kod_wyjscia_spoza_mapy_to_brak_dowodu(ep):
    ep.stan_bazowy()
    ep.bramki([{"nazwa": "lint", "argv": _wyjscie(7)}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie[0] == f"BRAMKA lint: wynik=brak-dowodu kod=7 log={ep.log('lint')}"
    assert ep.stan()["bramki"]["lint"]["wynik"] == "brak-dowodu"
    assert w.ostatnia == "BRAMKI: kod=2"


def test_kod_zmapowany_na_reczne_daje_kod_recznego_sprawdzenia(ep):
    ep.stan_bazowy()
    ep.bramki([{"nazwa": "mut", "argv": _wyjscie(3), "kody": {"3": "reczne"}}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 3
    assert w.linie[0] == f"BRAMKA mut: wynik=reczne kod=3 log={ep.log('mut')}"


def test_kod_zmapowany_na_nieznany_wynik_to_brak_dowodu(ep):
    ep.stan_bazowy()
    ep.bramki([{"nazwa": "lint", "argv": _wyjscie(0), "kody": {"0": "super"}}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie[0] == f"BRAMKA lint: wynik=brak-dowodu kod=0 log={ep.log('lint')}"


def test_przekroczony_limit_czasu_bramki_to_brak_dowodu(ep):
    ep.stan_bazowy()
    ep.bramki([{"nazwa": "lint", "argv": [PY, "-c", "import time; time.sleep(60)"], "timeout": 1}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie[0] == f"BRAMKA lint: wynik=brak-dowodu kod=timeout log={ep.log('lint')}"


def test_niewystartowana_komenda_bramki_to_brak_dowodu(ep):
    ep.stan_bazowy()
    brak = str(ep.tmp / "nie-ma-takiego-programu")
    ep.bramki([{"nazwa": "lint", "argv": [brak]}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie[0] == f"BRAMKA lint: wynik=brak-dowodu kod=start log={ep.log('lint')}"
    assert "nie wystartowała:" in Path(ep.log("lint")).read_text(encoding="utf-8")


def test_zmiana_diffu_w_trakcie_przebiegu_to_brak_dowodu(ep):
    ep.stan_bazowy()
    cel = str(ep.repo / "b.py")
    ep.bramki([{"nazwa": "lint", "argv": [PY, "-c", f"open({cel!r}, 'a').write('#\\n')"]}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie == [
        f"BRAMKA lint: wynik=zielony kod=0 log={ep.log('lint')}",
        "bramka_testow: diff zmienił się w trakcie przebiegu - wynik nie dotyczy bieżącego kodu",
        "BRAMKI: kod=2",
    ]
    assert ep.stan()["bramki"]["lint"]["wynik"] == "brak-dowodu"


def test_git_niesprawny_po_przebiegu_to_brak_dowodu(ep):
    ep.stan_bazowy()
    konfig = str(ep.repo / ".git" / "config")
    ep.bramki([{"nazwa": "lint", "argv": [PY, "-c", f"open({konfig!r}, 'a').write('[zepsuty\\n')"]}])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie[-2:] == [
        "bramka_testow: diff zmienił się w trakcie przebiegu - wynik nie dotyczy bieżącego kodu",
        "BRAMKI: kod=2",
    ]


@pytest.mark.parametrize(("kody", "oczekiwany"), [
    ([1, 3], 1),
    ([7, 1], 2),
    ([3, 0], 3),
])
def test_kod_wyjscia_bramek_ma_priorytet_brak_dowodu_czerwony_reczne(ep, kody, oczekiwany):
    ep.stan_bazowy()
    ep.bramki([{"nazwa": f"b{i}", "argv": _wyjscie(k), "kody": {"3": "reczne"}} for i, k in enumerate(kody)])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == oczekiwany
    assert w.ostatnia == f"BRAMKI: kod={oczekiwany}"


def test_opcja_tylko_uruchamia_wylacznie_wskazane_bramki(ep):
    ep.stan_bazowy()
    ep.bramki([BRAMKA_OK, {"nazwa": "testy", "argv": _wyjscie(1)}])
    w = ep.skrypt("bramka_testow.py", KEY, "--tylko", "lint")
    assert w.kod == 0
    assert set(ep.stan()["bramki"]) == {"lint"}


def test_opcja_tylko_bez_istniejacej_bramki_to_brak_dowodu(ep):
    ep.stan_bazowy()
    ep.bramki([BRAMKA_OK])
    w = ep.skrypt("bramka_testow.py", KEY, "--tylko", "nie-ma")
    assert w.kod == 2
    assert w.linie == ["BRAMKI: kod=2"]


@pytest.mark.parametrize("lista", [[], {"nazwa": "lint"}, [{"nazwa": "lint"}], [{"nazwa": "lint", "argv": []}],
                                   [{"argv": ["true"]}]])
def test_niepoprawna_lista_bramek_to_blad_konfiguracji(ep, lista):
    ep.stan_bazowy()
    ep.bramki(lista)
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 64
    assert w.stdout == ""


def test_bramka_testow_bez_stanu_epizodu_to_brak_dowodu(ep):
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie == ["BRAMKI: kod=2"]


def test_bramka_testow_z_nieznanym_baseline_to_brak_dowodu(ep):
    ep.stan_bazowy(baseline={"sha": "0" * 40, "pliki_sprzed_startu": []})
    ep.bramki([BRAMKA_OK])
    w = ep.skrypt("bramka_testow.py", KEY)
    assert w.kod == 2
    assert w.linie == ["BRAMKI: kod=2"]
    assert "bramki" not in ep.stan()


@pytest.mark.parametrize("argumenty", [[], [KEY, "--tylko"], [KEY, "nadmiar"]])
def test_bramka_testow_przy_zlym_wywolaniu_konczy_sie_bledem_uzycia(ep, argumenty):
    w = ep.skrypt("bramka_testow.py", *argumenty)
    assert w.kod == 64


# =============================================================================
# werdykt.py
# =============================================================================


def _werdykt(ep: Epizod, tresc: str, *opcje: str) -> Wynik:
    return ep.skrypt("werdykt.py", KEY, "--plik", ep.plik("w.md", tresc), *opcje)


def test_werdykt_z_ocenami_powyzej_progu_jest_approved_i_wiazany_ze_skrotem_diffu(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, WERDYKT_APPROVED)
    assert w.kod == 0
    assert w.linie == ["OCENA: backend=95", "OCENA: testy=100", "WERDYKT: kod=0 status=APPROVED minimum=95"]
    zapis = ep.stan()["werdykty"][-1]
    assert (zapis["kod"], zapis["status"], zapis["oceny"]) == (0, "APPROVED", {"backend": 95, "testy": 100})
    assert len(zapis["skrot_diffu"]) == 16


def test_werdykt_bez_bloku_code_review_to_brak_werdyktu(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, "Wszystko dobrze, APPROVED\nbackend: 100 = 100/100\ntesty: 100 = 100/100\n")
    assert w.kod == 2
    assert w.linie == ["werdykt: brak bloku **Code Review**", "WERDYKT: kod=2 status=BRAK minimum=-"]
    assert ep.stan()["werdykty"][-1]["kod"] == 2


@pytest.mark.parametrize(("blok", "minimum"), [
    ("**Code Review**\n\nStatus: APPROVED\n", "-"),
    ("**Code Review**\n\nbackend: 100 = 100/100\n", "100"),
])
def test_werdykt_przy_zerze_ocenianych_agentow_to_brak_werdyktu(ep, blok, minimum):
    ep.stan_bazowy(przydzial={}, pliki_tm=["a.py"])
    w = _werdykt(ep, blok)
    assert w.kod == 2
    assert "werdykt: brak ocenianych agentów (pusty przydzial w stan.json)" in w.linie
    assert w.ostatnia == f"WERDYKT: kod=2 status=BRAK minimum={minimum}"
    zapis = ep.stan()["werdykty"][-1]
    assert (zapis["kod"], zapis["status"]) == (2, "BRAK")


def test_werdykt_bez_oceny_ktoregos_agenta_to_brak_werdyktu(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, "**Code Review**\n\nbackend: 100 = 100/100\n")
    assert w.kod == 2
    assert w.linie == ["OCENA: backend=100", "werdykt: brak oceny agentów: testy",
                       "WERDYKT: kod=2 status=BRAK minimum=100"]


def test_werdykt_ponizej_progu_to_needs_work_z_regula(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, WERDYKT_NEEDS_WORK)
    assert w.kod == 1
    assert w.linie == ["OCENA: backend=50", "OCENA: testy=100", "REGULA: backend: Każdy zapis ma test.",
                       "WERDYKT: kod=1 status=NEEDS-WORK minimum=50"]


def test_werdykt_ponizej_progu_tylko_przez_blokady_zewnetrzne_to_blokada(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, WERDYKT_BLOKADY)
    assert w.kod == 4
    assert w.ostatnia == "WERDYKT: kod=4 status=BLOKADA minimum=75"


def test_blokada_zewnetrzna_obok_zwyklego_odjecia_to_needs_work(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, "**Code Review**\n\nbackend: 100 -15 (ci.yml:4; BLOKADA-ZEWNĘTRZNA: token) "
                     "-10 (a.py:1 brak testu) = 75/100\ntesty: 100 = 100/100\n")
    assert w.kod == 1
    assert w.ostatnia == "WERDYKT: kod=1 status=NEEDS-WORK minimum=75"


def test_blokada_zewnetrzna_jednego_agenta_nie_zwalnia_drugiego_z_poprawki(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, "**Code Review**\n\nbackend: 100 -25 (ci.yml:4; BLOKADA-ZEWNĘTRZNA: token) = 75/100\n"
                     "testy: 100 -25 (test_a.py:1 brak asercji) = 75/100\n")
    assert w.kod == 1
    assert w.ostatnia == "WERDYKT: kod=1 status=NEEDS-WORK minimum=75"


def test_ocena_ponizej_progu_bez_wskazanego_odjecia_to_needs_work(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, "**Code Review**\n\nbackend: 100 = 80/100\ntesty: 100 = 100/100\n")
    assert w.kod == 1
    assert w.ostatnia == "WERDYKT: kod=1 status=NEEDS-WORK minimum=80"


def test_ponowna_ocena_przenosi_brakujaca_ocene_z_poprzedniej_rundy(ep):
    ep.stan_bazowy()
    assert _werdykt(ep, WERDYKT_BLOKADY).kod == 4
    w = _werdykt(ep, "**Code Review HAR-12**\n\n  backend: 100 = 100/100\n")
    assert w.kod == 1
    assert w.linie[0] == "PRZENIESIONA: testy=75 (z poprzedniej rundy)"
    assert w.ostatnia == "WERDYKT: kod=1 status=NEEDS-WORK minimum=75"


def test_prog_z_opcji_zmienia_rozstrzygniecie(ep):
    ep.stan_bazowy()
    w = _werdykt(ep, WERDYKT_APPROVED, "--prog", "96")
    assert w.kod == 1
    assert w.ostatnia == "WERDYKT: kod=1 status=NEEDS-WORK minimum=95"


def test_nieczytelny_plik_werdyktu_to_brak_werdyktu(ep):
    ep.stan_bazowy()
    w = ep.skrypt("werdykt.py", KEY, "--plik", str(ep.pliki / "nie-ma.md"))
    assert w.kod == 2
    assert w.linie == ["werdykt: brak bloku **Code Review**", "WERDYKT: kod=2 status=BRAK minimum=-"]


@pytest.mark.parametrize("stan", [
    None,                                                     # brak pliku: OSError
    "{",                                                      # ValueError
    {"baseline": {"sha": "x"}},                               # KeyError
    {"przydzial": ["backend"], "baseline": {"sha": "x"}},     # AttributeError
    {"przydzial": {"backend": []}, "baseline": None},         # TypeError
])
def test_werdykt_przy_nieczytelnym_stanie_to_brak_werdyktu(ep, stan):
    if stan is not None:
        ep.kat.mkdir(parents=True)
        (ep.kat / "stan.json").write_text(stan if isinstance(stan, str) else json.dumps(stan), encoding="utf-8")
    w = _werdykt(ep, WERDYKT_APPROVED)
    assert w.kod == 2
    assert w.linie == ["WERDYKT: kod=2 status=BRAK minimum=-"]


@pytest.mark.parametrize("argumenty", [
    [KEY, "--plik"], [KEY, "--inne", "x"], [KEY, "--plik", "x", "--prog"], [KEY, "--plik", "x", "--prog", "abc"],
])
def test_werdykt_przy_zlym_wywolaniu_konczy_sie_bledem_uzycia(ep, argumenty):
    ep.stan_bazowy()
    w = ep.skrypt("werdykt.py", *argumenty)
    assert w.kod == 64
    assert "werdykty" not in ep.stan()


# =============================================================================
# start.py, stan.py, rekoncyliacja.py, prompt_krytyka.py (pozostałe przypadki audytu)
# =============================================================================


def _ticket(ep: Epizod, **pola: object) -> str:
    d = {"key": KEY, "title": "Dodaj X", "status": "todo", "blocked_by": [], "labels": []}
    d.update(pola)
    return ep.plik("ticket.json", json.dumps(d))


def test_start_z_niezamknietym_blokerem_czeka_na_merge(ep):
    w = ep.skrypt("start.py", KEY, "--ticket-json", _ticket(ep, blocked_by=[{"key": "HAR-9", "status": "in_review"}]))
    assert w.kod == 10
    assert w.ostatnia == "START: kod=10 powod=czeka na merge HAR-9"


def test_start_ticketu_zakonczonego_wymaga_decyzji_czlowieka(ep):
    w = ep.skrypt("start.py", KEY, "--ticket-json", _ticket(ep, status="done"))
    assert w.kod == 13
    assert w.ostatnia == "START: kod=13 powod=ticket ma status done"


def test_start_na_branchu_bez_numeru_ticketu_wymaga_decyzji(ep):
    w = ep.skrypt("start.py", "HAR-99", "--ticket-json", _ticket(ep, key="HAR-99"))
    assert w.kod == 11
    assert w.ostatnia == f"START: kod=11 powod=branch {BRANCH} bez numeru ticketu 99"


def test_start_z_nieczytelnym_ticketem_to_brak_dowodu(ep):
    w = ep.skrypt("start.py", KEY, "--ticket-json", ep.plik("zly.json", "{"))
    assert w.kod == 2
    assert w.ostatnia.startswith("START: kod=2 powod=ticket-json nieczytelny:")


def test_start_na_branchu_bazowym_wymaga_decyzji(ep):
    ep.git("checkout", "-q", "main")
    w = ep.skrypt("start.py", KEY, "--ticket-json", _ticket(ep))
    assert w.kod == 11
    assert w.ostatnia == "START: kod=11 powod=praca na branchu bazowym albo detached HEAD (main)"


def test_start_przewija_czysty_branch_do_bazy_i_rozpoznaje_tryb_defektu(ep):
    inny = ep.tmp / "inny"
    ep.git_w(ep.tmp, "clone", "-q", str(ep.origin), str(inny))
    ep.ustaw_tozsamosc(inny)
    (inny / "z.py").write_text("z = 1\n", encoding="utf-8")
    ep.git_w(inny, "add", "z.py")
    ep.git_w(inny, "commit", "-q", "-m", "baza+1")
    ep.git_w(inny, "push", "-q", "origin", "main")
    w = ep.skrypt("start.py", KEY, "--ticket-json",
                  _ticket(ep, title="Napraw Y", blocked_by=[{"key": "HAR-9", "status": "done"}]))
    assert w.kod == 0, w.stdout + w.stderr
    wynik = json.loads(w.linie[0])
    assert (wynik["rozjazd"]["przewinieto"], wynik["tryb"]) == (1, "defekt")
    assert (ep.repo / "z.py").is_file()


def test_ponowny_start_jest_wznowieniem_i_zachowuje_przydzial(ep):
    assert ep.skrypt("start.py", KEY, "--ticket-json", _ticket(ep)).kod == 0
    assert ep.skrypt("stan.py", "ustaw", KEY, 'przydzial={"backend": ["a.py"]}').kod == 0
    w = ep.skrypt("start.py", KEY, "--ticket-json", _ticket(ep))
    assert w.kod == 0
    assert w.ostatnia == "START: kod=0 powod=wznowienie - czytaj references/wznowienie.md"
    assert ep.stan()["przydzial"] == {"backend": ["a.py"]}


def test_stan_odrzuca_json_bez_cudzyslowow(ep):
    w = ep.skrypt("stan.py", "ustaw", KEY, "przydzial={backend:[a.py]}")
    assert w.kod == 64
    assert w.stderr.strip() == "stan: wartość przydzial wygląda na JSON, ale nie parsuje się: {backend:[a.py]}"


def test_czwarta_poprawka_agenta_przekracza_limit(ep):
    for _ in range(3):
        assert ep.skrypt("stan.py", "poprawka", KEY, "backend").kod == 0
    w = ep.skrypt("stan.py", "poprawka", KEY, "backend")
    assert w.kod == 1
    assert w.linie == ["POPRAWKA: agent=backend n=4 limit=3 -> decyzja człowieka"]


def test_czas_team_managera_liczony_od_znacznika_startu(ep):
    assert ep.skrypt("stan.py", "znacznik", KEY, "start_tm").kod == 0
    w = ep.skrypt("stan.py", "czas", KEY)
    assert w.kod == 0
    assert w.linie == ["CZAS-TM: minuty=1 metoda=wall-clock"]


def test_rekoncyliacja_bez_zmian_to_brak_dowodu(ep):
    ep.stan_bazowy()
    w = ep.skrypt("rekoncyliacja.py", KEY)
    assert w.kod == 2
    assert w.linie == ["REKONCYLIACJA: kod=2 zmienione=0 poza_przydzialem=0 niezgodne_raporty=0"]


def test_rekoncyliacja_wykrywa_plik_poza_przydzialem(ep):
    ep.stan_bazowy()
    ep.zapisz("a.py", "y = 2\n", dopisz=True)
    ep.zapisz("c.py", "c = 1\n")
    w = ep.skrypt("rekoncyliacja.py", KEY)
    assert w.kod == 1
    assert w.linie == ["POZA: c.py", "REKONCYLIACJA: kod=1 zmienione=2 poza_przydzialem=1 niezgodne_raporty=0"]


def test_rekoncyliacja_zgodna_z_raportem_agenta(ep):
    ep.stan_bazowy()
    ep.zapisz("a.py", "y = 2\n", dopisz=True)
    ep.zapisz("b.py", "b = 1\n")
    w = ep.skrypt("rekoncyliacja.py", KEY, "--raport", "backend=a.py,b.py")
    assert w.kod == 0
    assert w.linie == ["REKONCYLIACJA: kod=0 zmienione=2 poza_przydzialem=0 niezgodne_raporty=0"]


def test_rekoncyliacja_wykrywa_raport_pomijajacy_plik(ep):
    ep.stan_bazowy()
    ep.zapisz("a.py", "y = 2\n", dopisz=True)
    ep.zapisz("b.py", "b = 1\n")
    w = ep.skrypt("rekoncyliacja.py", KEY, "--raport", "backend=a.py")
    assert w.kod == 1
    assert w.linie == ["RAPORT: backend nie zgłosił b.py",
                       "REKONCYLIACJA: kod=1 zmienione=2 poza_przydzialem=0 niezgodne_raporty=1"]


def test_prompt_krytyka_nie_powstaje_przy_niezielonych_bramkach(ep):
    ep.stan_bazowy()
    ep.bramki([BRAMKA_OK, {"nazwa": "testy", "argv": _wyjscie(1)}])
    assert ep.skrypt("bramka_testow.py", KEY).kod == 1
    w = ep.skrypt("prompt_krytyka.py", KEY, "--rubryka", str(RUBRYKA))
    assert w.kod == 1
    assert w.linie == ["prompt_krytyka: bramki niezielone dla bieżącego diffu: testy", "PROMPT-KRYTYKA: kod=1 plik=-"]


def test_prompt_krytyka_powstaje_po_zielonych_bramkach(ep):
    ep.stan_bazowy()
    ep.bramki([BRAMKA_OK])
    assert ep.skrypt("bramka_testow.py", KEY).kod == 0
    w = ep.skrypt("prompt_krytyka.py", KEY, "--rubryka", str(RUBRYKA))
    plik = ep.kat / "krytyk-prompt-1.md"
    assert w.kod == 0
    assert w.linie == [str(plik), f"PROMPT-KRYTYKA: kod=0 plik={plik}"]
    assert str(RUBRYKA) in plik.read_text(encoding="utf-8")

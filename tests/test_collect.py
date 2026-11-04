"""HAR-89: odbior runu - `collect` zwraca diff runu wzgledem `base_commit`, tylko do odczytu.

Warstwy: runner (`runner.odbior.zbierz_diff`, prawdziwy git w tmp_path), operacja runnera
`collect`, serwis kokpitu (`cockpit.services.odbior`) i trasa `GET /api/runs/{id}/diff`
z przelotem end-to-end przez prawdziwego runnera. Testy pisane PRZED implementacja (TDD).

Niezmienniki: `no_git_write` (zbieranie diffu nie zapisuje w drzewie ani w indeksie),
`run_dir_outside_repo` (pliki `.cockpit/_run/` to artefakty runu, nie zmiany do odbioru),
`evidence_required` (blad gita to blad, nie pusty diff), `tenant_id` (run cudzego zespolu to 404).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).

Zalozenie o gicie (sprawdzone lokalnie na git 2.45.2): `git diff --no-index -- /dev/null pusty`
daje kod 1 i naglowek "new file mode" (pusty nowy plik JEST w diffie), a nazwa z polskimi
znakami jest w naglowku diffu cytowana ("za\\305\\274...") dopoki nie ma `core.quotepath=false`.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, Runner, RunStatus, Tenant
from cockpit.services import odbior
from cockpit.services.errors import NieZnaleziono, RunnerOffline, StanNiemozliwy
from cockpit.services.walidacja import migawka
from cockpit.settings import settings
from runner import operacje
from runner.client import Runner as RunnerKlient
from runner.odbior import BladOdbioru, zbierz_diff
from runner.worktree import utworz_worktree
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

TOZSAMOSC = [
    "-c", "user.name=Test",
    "-c", "user.email=test@example.com",
    "-c", "commit.gpgsign=false",
]

RUN_ID = "run-odbior-1"
SHA_X = "a" * 40            # poprawny kształt SHA, do testow serwisu (runner go nie widzi)
SHA_NIEISTNIEJACY = "0" * 40


def git(repo: Path, *argumenty: str) -> str:
    return subprocess.run(
        ["git", *TOZSAMOSC, "-C", str(repo), *argumenty],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout


def zapisz(korzen: Path, rel: str, tresc: str) -> None:
    cel = korzen / rel
    cel.parent.mkdir(parents=True, exist_ok=True)
    cel.write_text(tresc, encoding="utf-8")


# =================================================================================
# Fixture'y repozytorium uzytkownika i worktree runu
# =================================================================================

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Repozytorium uzytkownika BEZ .gitignore. Sledzone sa tez pliki z `.cockpit/`:
    `_run/sledzony.md` (artefakt sledzony przez pomylke), `_runner/x.txt` i `inne.txt`
    (katalogi o nazwie podobnej do `_run`)."""
    r = tmp_path / "repo_uzytkownika"
    r.mkdir()
    git(r, "init", "-q")
    zapisz(r, "a.txt", "a\n")
    zapisz(r, "b.txt", "b\n")
    zapisz(r, ".cockpit/_run/sledzony.md", "stary\n")
    zapisz(r, ".cockpit/_runner/x.txt", "x\n")
    zapisz(r, ".cockpit/inne.txt", "inne\n")
    git(r, "update-index", "--add", "a.txt", "b.txt", ".cockpit/_run/sledzony.md",
        ".cockpit/_runner/x.txt", ".cockpit/inne.txt")
    # fixture testowy (nie silnik): commit w repo testowym w tmp_path
    git(r, "commit", "-q", "-a", "--allow-empty", "-m", "init")
    return r


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "runy"


@pytest.fixture
def runy_w_srodowisku(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(root))
    return root


@pytest.fixture
def wt(repo: Path, root: Path):
    return utworz_worktree(repo, RUN_ID, root)


def zmiany_runu(wt) -> None:
    """Zmiana pliku sledzonego, nowy plik i artefakty runu (nieslediowne) w worktree."""
    zapisz(wt.sciezka, "a.txt", "a\nzmieniona linia\n")
    zapisz(wt.sciezka, "nowy.txt", "nowy plik\n")
    zapisz(wt.sciezka, ".cockpit/_run/skills/x/SKILL.md", "zmaterializowany skill\n")
    zapisz(wt.sciezka, ".cockpit/_run/artefakt.md", "artefakt runu\n")


def status(katalog: Path) -> str:
    return git(katalog, "status", "--porcelain", "--untracked-files=all")


@pytest.fixture
def git_pod_obserwacja(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Lista argv kazdego wywolania `subprocess.run` od momentu uzycia fixture'a."""
    prawdziwy = subprocess.run
    wywolania: list[list[str]] = []

    def run(argv, *a, **kw):
        wywolania.append([str(x) for x in argv])
        return prawdziwy(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    return wywolania


def podmien_kod_gita(monkeypatch: pytest.MonkeyPatch, wybor, kod: int, stdout: str = "") -> None:
    """Prawdziwy git, ale dla wybranego wywolania zwracamy podany kod wyjscia (zaslepka
    TYLKO kodu: sama funkcja zbierajaca diff jest prawdziwa)."""
    prawdziwy = subprocess.run

    def run(argv, *a, **kw):
        wynik = prawdziwy(argv, *a, **kw)
        if not wybor([str(x) for x in argv]):
            return wynik
        bajty = isinstance(wynik.stdout, bytes)
        return subprocess.CompletedProcess(
            argv, kod,
            stdout=stdout.encode() if bajty else stdout,
            stderr=b"fatal: zaslepka" if bajty else "fatal: zaslepka")

    monkeypatch.setattr(subprocess, "run", run)


# =================================================================================
# Runner: zbierz_diff - zawartosc diffu
# =================================================================================

def test_diff_runu_zawiera_zmieniony_plik_sledzony_z_jego_zmiana(wt):
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "diff --git a/a.txt b/a.txt" in diff
    assert "--- a/a.txt" in diff
    assert "+++ b/a.txt" in diff
    assert "+zmieniona linia" in diff


def test_diff_runu_zawiera_nowy_plik_niesledzony_jako_dodany_od_zera(wt):
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "diff --git a/nowy.txt b/nowy.txt" in diff
    assert "--- /dev/null" in diff
    assert "+++ b/nowy.txt" in diff
    assert "+nowy plik" in diff


def test_diff_runu_nie_zawiera_pliku_niezmienionego(wt):
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "b.txt" not in diff


def test_nowy_plik_w_glebokim_katalogu_jest_w_diffie_jako_plik_a_nie_katalog(wt):
    zapisz(wt.sciezka, "nowy_katalog/glebiej/plik.txt", "gleboko\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "diff --git a/nowy_katalog/glebiej/plik.txt b/nowy_katalog/glebiej/plik.txt" in diff
    assert "+gleboko" in diff


def test_nowy_plik_z_polskimi_znakami_w_nazwie_jest_w_diffie_doslownie(wt):
    zapisz(wt.sciezka, "zażółć.txt", "gęślą jaźń\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "diff --git a/zażółć.txt b/zażółć.txt" in diff
    assert "+++ b/zażółć.txt" in diff
    assert "+gęślą jaźń" in diff
    assert "\\305" not in diff     # nazwa nie jest zapisana ósemkowo


def test_nowy_plik_z_bajtem_spoza_utf8_nie_przerywa_zbierania_diffu(wt):
    (wt.sciezka / "binarny.dat").write_bytes(b"tekst \xff koniec\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "binarny.dat" in diff


def test_nowy_plik_ignorowany_przez_gitignore_nie_jest_zmiana_do_odbioru(wt):
    zapisz(wt.sciezka, ".gitignore", "ignorowany.log\n")
    zapisz(wt.sciezka, "ignorowany.log", "szum\n")
    zapisz(wt.sciezka, "widoczny.txt", "widoczny\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+++ b/widoczny.txt" in diff
    assert "diff --git a/ignorowany.log" not in diff
    assert "+szum" not in diff


def test_pusty_nowy_plik_nie_jest_bledem_a_reszta_diffu_zostaje(wt):
    zapisz(wt.sciezka, "pusty.txt", "")
    zapisz(wt.sciezka, "nowy.txt", "nowy plik\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+nowy plik" in diff
    # git 2.45.2: pusty plik daje kod 1 i naglowek "new file mode" - jest w diffie
    assert "diff --git a/pusty.txt b/pusty.txt" in diff


def test_kod_zero_z_porownania_nowego_pliku_nie_jest_bledem(wt, monkeypatch):
    """Starsze wersje gita zwracaja 0 dla pustego pliku: brak roznic to nie blad."""
    zapisz(wt.sciezka, "pusty.txt", "")
    zapisz(wt.sciezka, "a.txt", "a\nzmieniona linia\n")
    podmien_kod_gita(monkeypatch, lambda argv: "--no-index" in argv, kod=0)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff
    assert "pusty.txt" not in diff


def test_wynik_to_diff_sledzonych_i_nowych_plikow_w_jednym_tekscie(wt):
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert isinstance(diff, str)
    assert diff.count("diff --git a/a.txt") == 1
    assert diff.count("diff --git a/nowy.txt") == 1


# =================================================================================
# Runner: nic spod .cockpit/_run/ nie wyglada na zmiane do odbioru (BRO-91, BRO-100)
# =================================================================================

def test_niesledzone_skille_i_artefakty_runu_nie_sa_w_diffie_mimo_braku_gitignore(repo, wt):
    zmiany_runu(wt)
    assert not (repo / ".gitignore").exists()
    assert not (wt.sciezka / ".gitignore").exists()
    # dowod, ze bez wykluczenia git widzialby te pliki jako zmiany
    zmienione = status(wt.sciezka)
    assert ".cockpit/_run/skills/x/SKILL.md" in zmienione
    assert ".cockpit/_run/artefakt.md" in zmienione

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff          # diff niepusty, wiec brak _run nie jest przypadkiem
    assert "+nowy plik" in diff
    assert ".cockpit/_run/" not in diff
    assert "SKILL.md" not in diff
    assert "artefakt.md" not in diff
    assert "zmaterializowany skill" not in diff
    assert "artefakt runu" not in diff


def test_artefakt_runu_sledzony_w_commicie_bazowym_i_zmieniony_nie_jest_w_diffie(wt):
    zapisz(wt.sciezka, ".cockpit/_run/sledzony.md", "zmieniony przez run\n")
    zapisz(wt.sciezka, "a.txt", "a\nzmieniona linia\n")
    assert ".cockpit/_run/sledzony.md" in git(wt.sciezka, "diff", "--name-only", wt.base_commit)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff
    assert "sledzony.md" not in diff
    assert "zmieniony przez run" not in diff


def test_artefakt_runu_sledzony_w_commicie_bazowym_i_usuniety_nie_jest_w_diffie(wt):
    (wt.sciezka / ".cockpit/_run/sledzony.md").unlink()
    zapisz(wt.sciezka, "a.txt", "a\nzmieniona linia\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff
    assert "sledzony.md" not in diff


def test_niesledzone_pliki_runu_odfiltrowuje_takze_sama_lista_plikow_gdy_git_ich_poda(
    wt, monkeypatch,
):
    """Drugie zabezpieczenie: z `ls-files` zdejmujemy wykluczenie, wiec o pominieciu
    `.cockpit/_run/` decyduje wylacznie filtr po stronie Pythona."""
    zmiany_runu(wt)
    zapisz(wt.sciezka, ".cockpit/_runner/nowy.txt", "obok\n")
    zapisz(wt.sciezka, ".cockpit/_run.txt", "plik o podobnej nazwie\n")
    prawdziwy = subprocess.run
    ls_files: list[list[str]] = []

    def run(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if "ls-files" in argv:
            argv = [x for x in argv if not x.startswith(":(exclude)")]
            ls_files.append(argv)
        return prawdziwy(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert ls_files, "implementacja nie uzywa `git ls-files` do nowych plikow"
    assert not any(x.startswith(":(exclude)") for x in ls_files[0])
    assert ".cockpit/_run/" not in diff
    assert "SKILL.md" not in diff
    assert "artefakt.md" not in diff
    assert "+++ b/nowy.txt" in diff
    assert "+++ b/.cockpit/_runner/nowy.txt" in diff      # filtr nie jest zbyt szeroki
    assert "+++ b/.cockpit/_run.txt" in diff


def test_pliki_w_katalogach_o_nazwie_podobnej_do_run_sa_w_diffie_sledzone_i_nowe(wt):
    zapisz(wt.sciezka, ".cockpit/_runner/x.txt", "x\nzmiana runnera\n")
    zapisz(wt.sciezka, ".cockpit/inne.txt", "inne\nzmiana inna\n")
    zapisz(wt.sciezka, ".cockpit/_runner/nowy.txt", "obok\n")
    zapisz(wt.sciezka, ".cockpit/_run.txt", "plik o podobnej nazwie\n")
    zapisz(wt.sciezka, ".cockpit/_run2/y.txt", "inny katalog\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "diff --git a/.cockpit/_runner/x.txt b/.cockpit/_runner/x.txt" in diff
    assert "+zmiana runnera" in diff
    assert "diff --git a/.cockpit/inne.txt b/.cockpit/inne.txt" in diff
    assert "+zmiana inna" in diff
    assert "+++ b/.cockpit/_runner/nowy.txt" in diff
    assert "+++ b/.cockpit/_run.txt" in diff
    assert "+++ b/.cockpit/_run2/y.txt" in diff


# =================================================================================
# Runner: tylko do odczytu (BRO-108) - katalog uzytkownika i worktree bez zmian
# =================================================================================

def indeks_worktree(wt) -> Path:
    wyjscie = git(wt.sciezka, "rev-parse", "--git-path", "index").strip()
    sciezka = Path(wyjscie)
    return sciezka if sciezka.is_absolute() else wt.sciezka / sciezka


def test_suma_kontrolna_katalogu_uzytkownika_po_zebraniu_diffu_jest_ta_sama_co_przed(repo, wt):
    zmiany_runu(wt)
    przed = migawka(repo)
    assert przed        # migawka nie jest pusta
    status_przed = status(repo)
    indeks_przed = (repo / ".git" / "index").read_bytes()

    zbierz_diff(wt.sciezka, wt.base_commit)

    assert migawka(repo) == przed
    assert status(repo) == status_przed
    assert (repo / ".git" / "index").read_bytes() == indeks_przed


def test_drzewo_robocze_runu_po_zebraniu_diffu_jest_nietkniete(wt):
    zmiany_runu(wt)
    przed = migawka(wt.sciezka)
    assert przed["a.txt"] and ".cockpit/_run/artefakt.md" in przed

    zbierz_diff(wt.sciezka, wt.base_commit)

    assert migawka(wt.sciezka) == przed


def test_zebranie_diffu_nie_dodaje_plikow_do_indeksu_i_nie_odswieza_go(wt):
    zmiany_runu(wt)
    status_przed = status(wt.sciezka)            # odswieza indeks do stanu spoczynku
    # pliki o tej samej tresci, ale nowym czasie modyfikacji: porcelanowy `git diff <commit>`
    # odswieza ich wpisy w indeksie MIMO `GIT_OPTIONAL_LOCKS=0` (zmierzone), dlatego
    # implementacja uzywa plumbingu `git diff-index -p`, ktory indeksu nie zmienia
    teraz = time.time() + 100
    for rel in ("b.txt", ".cockpit/inne.txt"):
        os.utime(wt.sciezka / rel, (teraz, teraz))
    indeks = indeks_worktree(wt)
    bajty_przed = indeks.read_bytes()

    zbierz_diff(wt.sciezka, wt.base_commit)

    assert indeks.read_bytes() == bajty_przed
    status_po = status(wt.sciezka)
    assert status_po == status_przed
    assert "?? nowy.txt" in status_po            # nowy plik dalej nieśledzony, nie "A "


def test_kazdy_proces_gita_zbierajacy_diff_dostaje_wylaczone_opcjonalne_blokady(wt, monkeypatch):
    """Zmienna srodowiskowa jest przekazana procesowi gita (nie tylko ustawiona w testach)."""
    prawdziwy = subprocess.run
    srodowiska: list[dict | None] = []

    def run(argv, *a, **kw):
        srodowiska.append(kw.get("env"))
        return prawdziwy(argv, *a, **kw)

    zapisz(wt.sciezka, "a.txt", "a\nzmieniona linia\n")
    zapisz(wt.sciezka, "nowy.txt", "nowy plik\n")
    monkeypatch.setattr(subprocess, "run", run)

    zbierz_diff(wt.sciezka, wt.base_commit)

    assert len(srodowiska) >= 3          # diff sledzonych, lista nowych, diff nowego pliku
    assert all(env is not None and env.get("GIT_OPTIONAL_LOCKS") == "0" for env in srodowiska)


def test_zebranie_diffu_nie_wola_zadnej_komendy_zapisujacej_w_gicie(wt, git_pod_obserwacja):
    zmiany_runu(wt)

    zbierz_diff(wt.sciezka, wt.base_commit)

    podkomendy = set()
    for argv in git_pod_obserwacja:
        if argv and argv[0] == "git":
            # pierwsza pozycja po `-C <katalog>` (i ewentualnych `-c k=v`) to podkomenda
            reszta = argv[1:]
            i = 0
            while i < len(reszta) and reszta[i] in ("-C", "-c"):
                i += 2
            podkomendy.add(reszta[i])
    assert podkomendy
    assert podkomendy <= {"diff", "diff-index", "ls-files"}


# =================================================================================
# Runner: format diffu nie zalezy od konfiguracji uzytkownika
# =================================================================================

@pytest.mark.parametrize("opcja", ["diff.noprefix", "diff.mnemonicPrefix"])
def test_konfiguracja_prefiksow_gita_uzytkownika_nie_zmienia_prefiksow_a_i_b_w_diffie(
    repo, wt, opcja,
):
    git(repo, "config", opcja, "true")
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "diff --git a/a.txt b/a.txt" in diff
    assert "--- a/a.txt" in diff
    assert "+++ b/a.txt" in diff
    assert "diff --git a/nowy.txt b/nowy.txt" in diff
    assert "--- /dev/null" in diff
    assert "+++ b/nowy.txt" in diff


def test_kolorowanie_diffu_wlaczone_w_konfiguracji_uzytkownika_nie_trafia_do_wyniku(repo, wt):
    git(repo, "config", "color.ui", "always")
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff
    assert "+nowy plik" in diff
    assert "\x1b[" not in diff


def test_zewnetrzny_program_diffu_z_konfiguracji_uzytkownika_nie_jest_uruchamiany(repo, wt):
    git(repo, "config", "diff.external", "false")      # program, ktory zawsze konczy sie bledem
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff
    assert "+nowy plik" in diff


def test_filtr_tekstowy_textconv_z_konfiguracji_uzytkownika_nie_zmienia_tresci_diffu(repo, wt):
    git(repo, "config", "diff.wielkie.textconv", "tr a-z A-Z")
    zapisz(wt.sciezka, ".gitattributes", "*.txt diff=wielkie\n")
    zapisz(wt.sciezka, "a.txt", "a\nzmieniona linia\n")

    diff = zbierz_diff(wt.sciezka, wt.base_commit)

    assert "+zmieniona linia" in diff
    assert "ZMIENIONA" not in diff


# =================================================================================
# Runner: bledy dziedziny (BladOdbioru), nie wyjatki biblioteki ani pusty sukces
# =================================================================================

@pytest.mark.parametrize("base_commit", [
    "HEAD",
    "--output=x",
    "",
    "a" * 39,
    "a" * 41,
    "A" * 40,
    "g" * 40,
    "main",
], ids=["HEAD", "opcja_gita", "pusty", "39_znakow", "41_znakow", "wielkie_litery",
        "nie_heksadecymalny", "nazwa_galezi"])
def test_base_commit_niebedacy_pelnym_sha_jest_bledem_odbioru_i_git_nie_rusza(
    wt, base_commit, git_pod_obserwacja,
):
    zmiany_runu(wt)

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, base_commit)

    assert str(blad.value) == "base_commit nie jest SHA"
    assert git_pod_obserwacja == []              # odrzucony przed jakimkolwiek wywolaniem gita
    assert not (wt.sciezka / "x").exists()        # `--output=x` nie zapisal pliku


def test_prawdziwy_sha_ze_smieciem_na_koncu_jest_bledem_odbioru(wt):
    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit + "\n")
    assert str(blad.value) == "base_commit nie jest SHA"
    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit + "0")
    assert str(blad.value) == "base_commit nie jest SHA"


def test_blad_odbioru_jest_bledem_dziedziny_modulu():
    from cockpit.services.errors import BladDziedziny
    assert issubclass(BladOdbioru, BladDziedziny)


def test_nieistniejacy_commit_bazowy_to_blad_odbioru_a_nie_pusty_diff(wt):
    zmiany_runu(wt)

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, SHA_NIEISTNIEJACY)

    assert str(blad.value) == "git nie zebral diffu runu"


def test_brak_binarki_gita_to_blad_odbioru(wt, tmp_path, monkeypatch):
    pusty_path = tmp_path / "pusty_path"
    pusty_path.mkdir()
    monkeypatch.setenv("PATH", str(pusty_path))

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit)

    assert str(blad.value) == "git nie jest dostepny"


@pytest.mark.parametrize("wyjatek", [FileNotFoundError, PermissionError, OSError])
def test_kazdy_blad_systemowy_przy_uruchomieniu_gita_to_blad_odbioru(wt, monkeypatch, wyjatek):
    def run(argv, *a, **kw):
        raise wyjatek("git niedostepny")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit)

    assert str(blad.value) == "git nie jest dostepny"


def test_katalog_niebedacy_repozytorium_gita_to_blad_odbioru(tmp_path):
    zwykly = tmp_path / "zwykly_katalog"
    zwykly.mkdir()

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(zwykly, SHA_NIEISTNIEJACY)

    assert str(blad.value) == "git nie zebral diffu runu"


def test_nieistniejacy_katalog_worktree_to_blad_odbioru(tmp_path):
    with pytest.raises(BladOdbioru):
        zbierz_diff(tmp_path / "nie_ma_takiego", SHA_NIEISTNIEJACY)


def _diff_sledzonych(argv: list[str]) -> bool:
    return "diff-index" in argv


@pytest.mark.parametrize(("wybor", "kod"), [
    pytest.param(_diff_sledzonych, 1, id="diff_sledzonych_kod_1"),
    pytest.param(_diff_sledzonych, 128, id="diff_sledzonych_kod_128"),
    pytest.param(lambda a: "ls-files" in a, 1, id="lista_nowych_kod_1"),
    pytest.param(lambda a: "ls-files" in a, 128, id="lista_nowych_kod_128"),
    pytest.param(lambda a: "--no-index" in a, 2, id="diff_nowego_pliku_kod_2"),
    pytest.param(lambda a: "--no-index" in a, 128, id="diff_nowego_pliku_kod_128"),
])
def test_niezerowy_kod_gita_w_dowolnym_kroku_zbierania_diffu_to_blad_odbioru(
    wt, monkeypatch, wybor, kod,
):
    zmiany_runu(wt)
    podmien_kod_gita(monkeypatch, wybor, kod)

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit)

    assert str(blad.value) == "git nie zebral diffu runu"


def _zagniezdzone_repozytorium(wt) -> None:
    """Nieśledzony podkatalog z własnym repozytorium: `ls-files` zgłasza go jako `katalog/`."""
    podkatalog = wt.sciezka / "zagniezdzone"
    podkatalog.mkdir()
    git(podkatalog, "init", "-q")
    zapisz(podkatalog, "w_srodku.txt", "tresc zagniezdzona\n")


def test_worktree_z_zagniezdzonym_repozytorium_jest_bledem_a_nie_diffem_bez_jego_zawartosci(wt):
    zmiany_runu(wt)
    _zagniezdzone_repozytorium(wt)

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit)

    assert str(blad.value) == "worktree zawiera zagniezdzone repozytorium"


def test_porownanie_nowego_pliku_z_kodem_jeden_i_pustym_wynikiem_to_blad_odbioru(
    wt, monkeypatch,
):
    """Kod 1 oznacza roznice, wiec pusty wynik przy kodzie 1 to brak dowodu, nie pusty diff."""
    zmiany_runu(wt)
    podmien_kod_gita(monkeypatch, lambda argv: "--no-index" in argv, kod=1, stdout="")

    with pytest.raises(BladOdbioru) as blad:
        zbierz_diff(wt.sciezka, wt.base_commit)

    assert str(blad.value) == "git nie zebral diffu runu"


# =================================================================================
# Operacja runnera `collect`
# =================================================================================

def test_operacja_collect_jest_zarejestrowana_w_runnerze_jako_korutyna():
    assert "collect" in operacje.OPERACJE
    assert inspect.iscoroutinefunction(operacje.OPERACJE["collect"])


async def _collect(run_id, base_commit, **dodatkowe) -> dict:
    return await asyncio.wait_for(operacje.wykonaj(
        {"type": "collect", "req_id": "r1", "run_id": run_id, "base_commit": base_commit,
         **dodatkowe}), 60)


async def test_operacja_collect_zwraca_diff_runu_z_plikiem_zmienionym_i_nowym(
    wt, runy_w_srodowisku,
):
    zmiany_runu(wt)

    odp = await _collect(RUN_ID, wt.base_commit)

    assert odp["req_id"] == "r1"
    assert odp["ok"] is True
    assert set(odp["wynik"]) == {"diff"}
    assert "diff --git a/a.txt b/a.txt" in odp["wynik"]["diff"]
    assert "+++ b/nowy.txt" in odp["wynik"]["diff"]
    assert ".cockpit/_run/" not in odp["wynik"]["diff"]


async def test_operacja_collect_sama_wylicza_worktree_z_identyfikatora_runu_a_nie_przyjmuje_sciezki(
    wt, runy_w_srodowisku, tmp_path,
):
    zmiany_runu(wt)
    obca = tmp_path / "obce"
    obca.mkdir()

    odp = await _collect(RUN_ID, wt.base_commit, worktree=str(obca), sciezka=str(obca))

    assert odp["ok"] is True
    assert "+++ b/nowy.txt" in odp["wynik"]["diff"]


async def test_operacja_collect_dla_runu_bez_worktree_daje_kod_brak_worktree(
    runy_w_srodowisku,
):
    odp = await _collect("run-bez-worktree", SHA_X)

    assert odp == {"req_id": "r1", "ok": False, "blad": "worktree runu nie istnieje",
                   "kod": "brak_worktree"}


async def test_operacja_collect_gdy_katalog_runu_jest_a_worktree_brak_daje_brak_worktree(
    runy_w_srodowisku,
):
    (runy_w_srodowisku / "run-pusty").mkdir(parents=True)

    odp = await _collect("run-pusty", SHA_X)

    assert odp["kod"] == "brak_worktree"


@pytest.mark.parametrize("run_id", ["..", ".", "", "a/b", "../x", "a\\b"])
async def test_operacja_collect_odrzuca_niebezpieczny_identyfikator_runu_bez_ujawniania_sciezek(
    runy_w_srodowisku, git_pod_obserwacja, run_id,
):
    odp = await _collect(run_id, SHA_X)

    assert odp == {"req_id": "r1", "ok": False, "blad": "niedozwolony identyfikator runu",
                   "kod": "zle_dane"}
    assert str(runy_w_srodowisku) not in odp["blad"]
    assert git_pod_obserwacja == []


@pytest.mark.parametrize("base_commit", ["HEAD", "--output=x", "a" * 39, "A" * 40])
async def test_operacja_collect_odrzuca_base_commit_niebedacy_sha_i_nie_uruchamia_gita(
    wt, runy_w_srodowisku, git_pod_obserwacja, base_commit,
):
    odp = await _collect(RUN_ID, base_commit)

    assert odp == {"req_id": "r1", "ok": False, "blad": "base_commit nie jest SHA",
                   "kod": "zle_dane"}
    assert git_pod_obserwacja == []


@pytest.mark.parametrize("dane", [
    {"base_commit": SHA_X},
    {"run_id": "r"},
    {"run_id": 5, "base_commit": SHA_X},
    {"run_id": "r", "base_commit": 5},
    {"run_id": None, "base_commit": SHA_X},
], ids=["bez_run_id", "bez_base_commit", "run_id_liczba", "base_commit_liczba", "run_id_null"])
async def test_operacja_collect_odrzuca_brakujace_i_zle_typem_pola(runy_w_srodowisku, dane):
    odp = await operacje.wykonaj({"type": "collect", "req_id": "r1", **dane})

    assert odp["ok"] is False
    assert odp["kod"] == "zle_dane"


async def test_operacja_collect_przy_bledzie_gita_daje_staly_komunikat_bez_stderr_i_sciezek(
    wt, runy_w_srodowisku,
):
    odp = await _collect(RUN_ID, SHA_NIEISTNIEJACY)

    assert odp == {"req_id": "r1", "ok": False, "blad": "git nie zebral diffu runu",
                   "kod": "blad_gita"}


async def test_operacja_collect_dla_worktree_z_zagniezdzonym_repozytorium_daje_blad_gita(
    wt, runy_w_srodowisku,
):
    _zagniezdzone_repozytorium(wt)

    odp = await _collect(RUN_ID, wt.base_commit)

    assert odp == {"req_id": "r1", "ok": False, "blad": "git nie zebral diffu runu",
                   "kod": "blad_gita"}


async def test_operacja_collect_gdy_worktree_nie_jest_repozytorium_daje_blad_gita(
    runy_w_srodowisku,
):
    (runy_w_srodowisku / "run-bez-gita" / "worktree").mkdir(parents=True)

    odp = await _collect("run-bez-gita", SHA_X)

    assert odp["kod"] == "blad_gita"
    assert odp["blad"] == "git nie zebral diffu runu"
    assert str(runy_w_srodowisku) not in odp["blad"]


async def test_operacja_collect_zbiera_diff_z_katalogu_worktree_w_katalogu_runu(
    wt, runy_w_srodowisku, monkeypatch,
):
    from runner.worktree import katalog_runu
    widziane = []

    def podglad(sciezka, base_commit):
        widziane.append(sciezka)
        return ""

    monkeypatch.setattr(operacje, "zbierz_diff", podglad)

    odp = await _collect(RUN_ID, wt.base_commit)

    assert odp["ok"] is True
    assert len(widziane) == 1
    assert widziane[0].name == "worktree"              # z rozroznieniem wielkosci liter
    assert widziane[0].parent == katalog_runu(RUN_ID)


async def test_operacja_collect_nie_zmienia_katalogu_uzytkownika(
    repo, wt, runy_w_srodowisku,
):
    zmiany_runu(wt)
    przed = migawka(repo)

    odp = await _collect(RUN_ID, wt.base_commit)

    assert odp["ok"] is True
    assert migawka(repo) == przed


# =================================================================================
# Serwis kokpitu: cockpit.services.odbior
# =================================================================================

B1 = "b-1"
B2 = "b-2"          # w zespole dwoch runnerow online; id B2 sortuje sie PO B1
B_STARY = "b-3-stary"
OBCY = "0-obcy"     # runner Piekarni; id mniejsze niz kazdy runner Browarka


class _Wyslij:
    """Atrapa `wyslij`: liczy wywolania i zwraca ustalone cialo albo rzuca ustalony wyjatek."""

    def __init__(self, cialo=None, blad: BaseException | None = None) -> None:
        self.cialo = {"ok": True, "wynik": {"diff": "diff --git a/a b/a\n"}} if cialo is None else cialo
        self.blad = blad
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane, timeout_s))
        if self.blad is not None:
            raise self.blad
        return self.cialo


@pytest.fixture
async def serwis(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'serwis.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def runner(rid: str, tenant_id: str, widziany: datetime) -> Runner:
        return Runner(id=rid, tenant_id=tenant_id, name=rid, token_hash=f"h-{rid}",
                      last_seen_at=widziany)

    async with maker() as s:
        browarek, piekarnia = Tenant(name="Browarek"), Tenant(name="Piekarnia")
        s.add(browarek)
        await s.flush()
        s.add(piekarnia)
        await s.flush()
        teraz = datetime.now(timezone.utc)
        stary = teraz - timedelta(seconds=settings.runner_offline_after_s * 10)
        s.add_all([runner(B1, browarek.id, teraz), runner(B2, browarek.id, teraz),
                   runner(B_STARY, browarek.id, stary), runner(OBCY, piekarnia.id, teraz)])
        await s.commit()
        ids = SimpleNamespace(browarek=browarek.id, piekarnia=piekarnia.id, maker=maker)
    try:
        yield ids
    finally:
        await engine.dispose()


async def _zasiej_run(maker, tenant_id: str, runner_id: str | None,
                      status: RunStatus = RunStatus.to_accept,
                      base_commit: str | None = SHA_X, run_id: str | None = None) -> str:
    async with maker() as s:
        run = Run(tenant_id=tenant_id, runner_id=runner_id, status=status,
                  base_commit=base_commit)
        if run_id:
            run.id = run_id
        s.add(run)
        await s.commit()
        return run.id


async def _do_odbioru(serwis, tenant_id: str, run_id: str) -> Run:
    async with serwis.maker() as s:
        return await odbior.run_do_odbioru(s, run_id, tenant_id)


async def _diff(serwis, tenant_id: str, run_id: str, wyslij, polaczone=(B1, B2)) -> str:
    async with serwis.maker() as s:
        return await asyncio.wait_for(
            odbior.diff_runu(s, run_id, tenant_id, frozenset(polaczone), wyslij), 10)


# --- run_do_odbioru: czy run mozna odebrac ------------------------------------------

@pytest.mark.parametrize("status", [RunStatus.to_accept, RunStatus.failed, RunStatus.skipped,
                                    RunStatus.aborted], ids=lambda s: s.value)
async def test_run_w_stanie_koncowym_mozna_odebrac(serwis, status):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1, status)

    run = await _do_odbioru(serwis, serwis.browarek, run_id)

    assert run.id == run_id
    assert run.status == status


@pytest.mark.parametrize("status", [RunStatus.pending, RunStatus.running, RunStatus.waiting],
                         ids=lambda s: s.value)
async def test_run_aktywny_nie_jest_do_odbioru_bo_jeszcze_trwa(serwis, status):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1, status)

    with pytest.raises(StanNiemozliwy) as blad:
        await _do_odbioru(serwis, serwis.browarek, run_id)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "run jeszcze trwa"


async def test_run_aktywny_bez_base_commit_zglasza_najpierw_ze_trwa(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1, RunStatus.running,
                               base_commit=None)

    with pytest.raises(StanNiemozliwy, match="^run jeszcze trwa$"):
        await _do_odbioru(serwis, serwis.browarek, run_id)


async def test_run_bez_base_commit_nie_ma_wzgledem_czego_liczyc_diffu(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1, base_commit=None)

    with pytest.raises(StanNiemozliwy) as blad:
        await _do_odbioru(serwis, serwis.browarek, run_id)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "run nie ma base_commit"


async def test_run_bez_przypisanego_runnera_nie_ma_gdzie_zebrac_diffu(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, None)

    with pytest.raises(StanNiemozliwy) as blad:
        await _do_odbioru(serwis, serwis.browarek, run_id)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "run nie ma runnera"


async def test_run_innego_zespolu_dla_odbioru_nie_istnieje(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.piekarnia, OBCY)

    with pytest.raises(NieZnaleziono):
        await _do_odbioru(serwis, serwis.browarek, run_id)


async def test_nieznany_run_dla_odbioru_nie_istnieje(serwis):
    with pytest.raises(NieZnaleziono):
        await _do_odbioru(serwis, serwis.browarek, "nie-ma-takiego-runu")


# --- diff_runu: zapytanie do runnera ------------------------------------------------

async def test_diff_runu_innego_zespolu_nie_wysyla_zapytania_do_zadnego_runnera(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.piekarnia, OBCY)
    wyslij = _Wyslij()

    with pytest.raises(NieZnaleziono):
        await _diff(serwis, serwis.browarek, run_id, wyslij, polaczone=(B1, B2, OBCY))

    assert wyslij.wywolania == []


async def test_diff_runu_drugiego_zespolu_idzie_do_runnera_tego_zespolu(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.piekarnia, OBCY)
    wyslij = _Wyslij()

    diff = await _diff(serwis, serwis.piekarnia, run_id, wyslij, polaczone=(B1, OBCY))

    assert [rid for rid, *_ in wyslij.wywolania] == [OBCY]
    assert diff == "diff --git a/a b/a\n"


@pytest.mark.parametrize("runner_runu", [B1, B2])
async def test_diff_runu_pyta_runner_tego_runu_a_nie_pierwszego_runnera_zespolu(
    serwis, runner_runu,
):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, runner_runu)
    wyslij = _Wyslij()

    await _diff(serwis, serwis.browarek, run_id, wyslij, polaczone=(B1, B2))

    assert [rid for rid, *_ in wyslij.wywolania] == [runner_runu]


async def test_diff_runu_wysyla_operacje_collect_z_identyfikatorem_runu_i_base_commit(
    serwis, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 7.5)
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1, base_commit="c" * 40)
    wyslij = _Wyslij()

    await _diff(serwis, serwis.browarek, run_id, wyslij)

    assert wyslij.wywolania == [(B1, "collect", {"run_id": run_id, "base_commit": "c" * 40}, 7.5)]


async def test_diff_runu_zwraca_tresc_diffu_z_odpowiedzi_runnera(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1)
    wyslij = _Wyslij({"ok": True, "wynik": {"diff": "+nowa linia\n", "inne": 1}})

    assert await _diff(serwis, serwis.browarek, run_id, wyslij) == "+nowa linia\n"


async def test_pusty_diff_z_odpowiedzi_runnera_jest_poprawnym_wynikiem_a_nie_bledem(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1)
    wyslij = _Wyslij({"ok": True, "wynik": {"diff": ""}})

    assert await _diff(serwis, serwis.browarek, run_id, wyslij) == ""


async def test_runner_runu_offline_mimo_ze_inny_runner_zespolu_jest_online_to_runner_offline(
    serwis,
):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B2)
    wyslij = _Wyslij()

    with pytest.raises(RunnerOffline, match="^runner runu jest offline$"):
        await _diff(serwis, serwis.browarek, run_id, wyslij, polaczone=(B1,))

    assert wyslij.wywolania == []


async def test_runner_runu_z_nieswiezym_heartbeatem_jest_offline_mimo_otwartego_strumienia(
    serwis,
):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B_STARY)
    wyslij = _Wyslij()

    with pytest.raises(RunnerOffline, match="^runner runu jest offline$"):
        await _diff(serwis, serwis.browarek, run_id, wyslij, polaczone=(B1, B_STARY))

    assert wyslij.wywolania == []


async def test_runner_runu_nalezacy_do_innego_zespolu_jest_traktowany_jak_offline(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, OBCY)
    wyslij = _Wyslij()

    with pytest.raises(RunnerOffline, match="^runner runu jest offline$"):
        await _diff(serwis, serwis.browarek, run_id, wyslij, polaczone=(B1, OBCY))

    assert wyslij.wywolania == []


async def test_runner_runu_ktorego_nie_ma_w_bazie_jest_traktowany_jak_offline(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, "nie-ma-takiego-runnera")
    wyslij = _Wyslij()

    with pytest.raises(RunnerOffline, match="^runner runu jest offline$"):
        await _diff(serwis, serwis.browarek, run_id, wyslij,
                    polaczone=(B1, "nie-ma-takiego-runnera"))

    assert wyslij.wywolania == []


async def test_diff_runu_aktywnego_nie_wysyla_zapytania(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1, RunStatus.running)
    wyslij = _Wyslij()

    with pytest.raises(StanNiemozliwy, match="^run jeszcze trwa$"):
        await _diff(serwis, serwis.browarek, run_id, wyslij)

    assert wyslij.wywolania == []


async def test_blad_zgloszony_przez_runner_przy_zbieraniu_diffu_trafia_do_wolajacego(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1)
    wyslij = _Wyslij({"ok": False, "blad": "worktree runu nie istnieje", "kod": "brak_worktree"})

    with pytest.raises(StanNiemozliwy) as blad:
        await _diff(serwis, serwis.browarek, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "worktree runu nie istnieje"


async def test_runner_ktory_nie_odpowiada_na_collect_w_limicie_czasu_daje_blad_dziedziny(serwis):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1)

    with pytest.raises(StanNiemozliwy) as blad:
        await _diff(serwis, serwis.browarek, run_id, _Wyslij(blad=TimeoutError()))

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "runner nie odpowiedzial w limicie czasu"


@pytest.mark.parametrize("cialo", [
    {"ok": True},
    {"ok": True, "wynik": None},
    {"ok": True, "wynik": {}},
    {"ok": True, "wynik": {"inne": "x"}},
    {"ok": True, "wynik": {"diff": 5}},
    {"ok": True, "wynik": {"diff": None}},
    {"ok": True, "wynik": {"diff": ["a"]}},
    {"ok": True, "wynik": "tekst"},
    {"ok": True, "wynik": ["diff"]},
], ids=["bez_wyniku", "wynik_null", "wynik_pusty", "bez_klucza_diff", "diff_liczba", "diff_null",
        "diff_lista", "wynik_napis", "wynik_lista"])
async def test_niepoprawny_wynik_collect_od_runnera_to_blad_stanu_a_nie_awaria(serwis, cialo):
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1)

    with pytest.raises(StanNiemozliwy) as blad:
        await _diff(serwis, serwis.browarek, run_id, _Wyslij(cialo))

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "runner zwrocil zly wynik collect"


async def test_zbyt_duzy_diff_od_runnera_jest_odrzucony_przez_limit_odpowiedzi(
    serwis, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_odpowiedz_max_kb", 1)
    run_id = await _zasiej_run(serwis.maker, serwis.browarek, B1)
    wyslij = _Wyslij({"ok": True, "wynik": {"diff": "x" * 2048}})

    with pytest.raises(StanNiemozliwy, match="za duza"):
        await _diff(serwis, serwis.browarek, run_id, wyslij)


# =================================================================================
# Trasa HTTP GET /api/runs/{id}/diff
# =================================================================================

TOKEN_B = "token-runnera-browarka"
TOKEN_P = "token-runnera-piekarni"


@pytest.fixture(autouse=True)
def _czysta_szyna():
    """Stan szyny (polaczenia, zapytania) to stan procesu: test nie zostawia po sobie nic."""
    slowniki = ("_oczekujace", "_runner_queues", "_polaczone", "_rozlaczone")
    zapisane = {n: dict(getattr(bus, n)) for n in slowniki if hasattr(bus, n)}
    yield
    for nazwa, przed in zapisane.items():
        biezacy = getattr(bus, nazwa)
        biezacy.clear()
        biezacy.update(przed)


@pytest.fixture
async def http(tmp_path, monkeypatch):
    """Dwa zespoly z wlascicielami i runnerami; klient zalogowany jako wlasciciel Browarka."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'http.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel_b, tenant_b = await zaloz_wlasciciela(
        maker, "browarek@example.com", nazwa_zespolu="Browarek")
    _, tenant_p = await zaloz_wlasciciela(maker, "piekarnia@example.com", nazwa_zespolu="Piekarnia")
    runner_b = await zaloz_runnera(maker, tenant_b, "laptop-b", TOKEN_B)
    runner_p = await zaloz_runnera(maker, tenant_p, "laptop-p", TOKEN_P)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c, \
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anonim:
            await zaloguj_klienta(c, maker, wlasciciel_b)
            yield SimpleNamespace(klient=c, anonim=anonim, maker=maker, tenant_b=tenant_b,
                                  tenant_p=tenant_p, runner_b=runner_b, runner_p=runner_p)
    finally:
        await engine.dispose()


@pytest.fixture
def wysylka_do_prawdziwych_operacji(monkeypatch):
    """`bus.zapytaj` podmienione na wykonanie prawdziwej operacji runnera w tym procesie."""
    wywolania: list[tuple] = []

    async def zapytaj(runner_id, typ, dane, timeout_s):
        wywolania.append((runner_id, typ, dict(dane)))
        odp = await operacje.wykonaj({"type": typ, "req_id": "r-http", **dane})
        return {"ok": odp["ok"], "wynik": odp.get("wynik"), "blad": odp.get("blad"),
                "kod": odp.get("kod")}

    monkeypatch.setattr(bus, "zapytaj", zapytaj)
    return wywolania


async def _pobierz_diff(klient: AsyncClient, run_id: str):
    return await asyncio.wait_for(klient.get(f"/api/runs/{run_id}/diff"), 60)


async def test_diff_runu_innego_zespolu_dostaje_404_i_nic_nie_trafia_do_runnera(
    http, wysylka_do_prawdziwych_operacji,
):
    run_id = await _zasiej_run(http.maker, http.tenant_p, http.runner_p)
    bus.mark_runner_connected(http.runner_p)

    r = await _pobierz_diff(http.klient, run_id)

    assert r.status_code == 404
    assert wysylka_do_prawdziwych_operacji == []


async def test_diff_nieznanego_runu_dostaje_404(http):
    assert (await _pobierz_diff(http.klient, "nie-ma-takiego-runu")).status_code == 404


async def test_diff_runu_bez_sesji_dostaje_401_i_nic_nie_trafia_do_runnera(
    http, wysylka_do_prawdziwych_operacji,
):
    run_id = await _zasiej_run(http.maker, http.tenant_b, http.runner_b)
    bus.mark_runner_connected(http.runner_b)

    r = await _pobierz_diff(http.anonim, run_id)

    assert r.status_code == 401
    assert wysylka_do_prawdziwych_operacji == []


@pytest.mark.parametrize("status", [RunStatus.pending, RunStatus.running, RunStatus.waiting],
                         ids=lambda s: s.value)
async def test_diff_runu_ktory_jeszcze_trwa_dostaje_409(
    http, wysylka_do_prawdziwych_operacji, status,
):
    run_id = await _zasiej_run(http.maker, http.tenant_b, http.runner_b, status)
    bus.mark_runner_connected(http.runner_b)

    r = await _pobierz_diff(http.klient, run_id)

    assert r.status_code == 409
    assert r.json()["detail"] == "run jeszcze trwa"
    assert wysylka_do_prawdziwych_operacji == []


async def test_diff_runu_bez_base_commit_dostaje_409(http):
    run_id = await _zasiej_run(http.maker, http.tenant_b, http.runner_b, base_commit=None)
    bus.mark_runner_connected(http.runner_b)

    r = await _pobierz_diff(http.klient, run_id)

    assert r.status_code == 409
    assert r.json()["detail"] == "run nie ma base_commit"


async def test_diff_runu_ktorego_runner_jest_offline_dostaje_409(
    http, wysylka_do_prawdziwych_operacji,
):
    run_id = await _zasiej_run(http.maker, http.tenant_b, http.runner_b)      # strumienia brak

    r = await _pobierz_diff(http.klient, run_id)

    assert r.status_code == 409
    assert r.json()["detail"] == "runner runu jest offline"
    assert wysylka_do_prawdziwych_operacji == []


async def test_diff_runu_zwraca_200_z_plikiem_zmienionym_i_nowym_bez_plikow_runu(
    http, repo, wt, runy_w_srodowisku, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, base_commit=wt.base_commit,
                      run_id=RUN_ID)
    bus.mark_runner_connected(http.runner_b)

    r = await _pobierz_diff(http.klient, RUN_ID)

    assert r.status_code == 200
    assert set(r.json()) == {"diff"}
    diff = r.json()["diff"]
    assert "diff --git a/a.txt b/a.txt" in diff and "+zmieniona linia" in diff
    assert "diff --git a/nowy.txt b/nowy.txt" in diff and "--- /dev/null" in diff
    assert ".cockpit/_run/" not in diff
    assert wysylka_do_prawdziwych_operacji == [
        (http.runner_b, "collect", {"run_id": RUN_ID, "base_commit": wt.base_commit})]


async def test_diff_runu_po_http_nie_zmienia_katalogu_uzytkownika_ani_worktree(
    http, repo, wt, runy_w_srodowisku, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, base_commit=wt.base_commit,
                      run_id=RUN_ID)
    bus.mark_runner_connected(http.runner_b)
    repo_przed, worktree_przed = migawka(repo), migawka(wt.sciezka)
    status_przed = status(wt.sciezka)
    assert repo_przed and worktree_przed

    r = await _pobierz_diff(http.klient, RUN_ID)

    assert r.status_code == 200
    assert migawka(repo) == repo_przed
    assert migawka(wt.sciezka) == worktree_przed
    assert status(wt.sciezka) == status_przed


async def test_diff_runu_gdy_worktree_zniknal_dostaje_409_z_komunikatem_runnera(
    http, runy_w_srodowisku, wysylka_do_prawdziwych_operacji,
):
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, run_id="run-bez-worktree")
    bus.mark_runner_connected(http.runner_b)

    r = await _pobierz_diff(http.klient, "run-bez-worktree")

    assert r.status_code == 409
    assert r.json()["detail"] == "worktree runu nie istnieje"


async def test_run_zakonczony_bledem_tez_mozna_odebrac_po_http(
    http, wt, runy_w_srodowisku, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, RunStatus.failed,
                      base_commit=wt.base_commit, run_id=RUN_ID)
    bus.mark_runner_connected(http.runner_b)

    r = await _pobierz_diff(http.klient, RUN_ID)

    assert r.status_code == 200
    assert "+zmieniona linia" in r.json()["diff"]


# --- przelot end-to-end: prawdziwa aplikacja + prawdziwy runner ----------------------

@pytest.fixture
async def runner_zywy(http, monkeypatch):
    """Prawdziwy runner Browarka polaczony z prawdziwa aplikacja; zatrzymany po tescie."""
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 20.0)
    runner = RunnerKlient("http://kokpit.test", TOKEN_B,
                          transport=TransportASGIStrumieniujacy(app_mod.app))
    nasluch = asyncio.create_task(runner.listen())
    try:
        koniec = time.monotonic() + 10
        while http.runner_b not in bus.connected_runner_ids():
            assert time.monotonic() < koniec, "runner nie polaczyl sie w limicie czasu"
            await asyncio.sleep(0.02)
        yield runner
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        zakonczone, _ = await asyncio.wait({nasluch}, timeout=10)
        if nasluch not in zakonczone:
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=5)


async def test_odbior_runu_przez_prawdziwego_runnera_daje_diff_i_nie_rusza_katalogu_uzytkownika(
    http, repo, wt, runy_w_srodowisku, runner_zywy,
):
    zmiany_runu(wt)
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, RunStatus.to_accept,
                      base_commit=wt.base_commit, run_id=RUN_ID)
    repo_przed = migawka(repo)
    assert repo_przed

    r = await _pobierz_diff(http.klient, RUN_ID)

    assert r.status_code == 200
    diff = r.json()["diff"]
    assert "diff --git a/a.txt b/a.txt" in diff and "+zmieniona linia" in diff
    assert "diff --git a/nowy.txt b/nowy.txt" in diff and "+nowy plik" in diff
    assert ".cockpit/_run/" not in diff
    assert "SKILL.md" not in diff
    assert migawka(repo) == repo_przed
    assert bus._oczekujace == {}


async def test_odbior_runu_przez_prawdziwego_runnera_gdy_worktree_zniknal_daje_409(
    http, runy_w_srodowisku, runner_zywy,
):
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, run_id="run-bez-worktree")

    r = await _pobierz_diff(http.klient, "run-bez-worktree")

    assert r.status_code == 409
    assert r.json()["detail"] == "worktree runu nie istnieje"


async def test_odbior_runu_nie_zmienia_statusu_ani_base_commit_runu_w_bazie(
    http, wt, runy_w_srodowisku, runner_zywy,
):
    """Odbior jest tylko do odczytu takze po stronie kokpitu: status i base_commit runu bez zmian."""
    zmiany_runu(wt)
    await _zasiej_run(http.maker, http.tenant_b, http.runner_b, RunStatus.to_accept,
                      base_commit=wt.base_commit, run_id=RUN_ID)

    r = await _pobierz_diff(http.klient, RUN_ID)

    assert r.status_code == 200
    async with http.maker() as s:
        run = (await s.execute(select(Run).where(Run.id == RUN_ID))).scalar_one()
    assert run.status == RunStatus.to_accept
    assert run.base_commit == wt.base_commit
    assert run.runner_id == http.runner_b


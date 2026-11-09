"""HAR-179 (HAR-103): `integrate` - zmiany runu trafiaja do drzewa uzytkownika wylacznie na jawne
zadanie czlowieka, bez commita i bez zmiany indeksu, z wpisem w historii runu (kto i kiedy).

Warstwy: runner (`runner.odbior.integruj`, prawdziwy git w tmp_path), operacje runnera
`integrate`/`commit` (`operacje.wykonaj`), serwis kokpitu (`cockpit.services.odbior`), trasa
`POST /api/runs/{id}/integrate` oraz zakaz zglaszania zdarzen `integrated`/`committed` przez
runnera. Testy pisane PRZED implementacja (TDD).

Niezmienniki: `no_git_write` (integrate to `git apply` bez `--index`; bez commita; push nie
istnieje), `evidence_required` (konflikt i blad gita to odmowa, nigdy zielone przejscie),
`tenant_id` (run i zdarzenia innego zespolu nie wplywaja na wynik), `approval`: o przyjeciu
wyniku decyduje czlowiek (status runu zostaje `to_accept`).

Pomocniki z tego pliku uzywa `test_commit_po_odbiorze.py` (importuje funkcje, nie fixture'y).
Repozytorium testowe ma wlasna tozsamosc gita w lokalnym configu, a globalny config, HOME i
zmienne GIT_* sa odizolowane: test nie zalezy od maszyny.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Select, event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.api.app import app
from cockpit.api.zaleznosci import NAGLOWEK_CSRF
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, RolaWZespole, Run, Runner, RunStatus
from cockpit.services import odbior, runs
from cockpit.services.errors import (
    NaruszonyNiezmiennik, NieZnaleziono, RunnerOffline, StanNiemozliwy,
)
from cockpit.services.runs import hash_token
from cockpit.services.walidacja import migawka
from cockpit.settings import settings
from runner import operacje
from runner.client import Runner as RunnerKlient
from runner.odbior import (
    BladIntegracji,
    BladOdbioru,
    BrakZmianRunu,
    integruj,
    pliki_runu,
    repozytorium_docelowe,
    zbierz_diff,
)
from runner import odbior as modul_odbioru
from runner.worktree import utworz_worktree
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

RUN_ID = "run-integrate-1"
SHA_X = "a" * 40
SHA_NIEISTNIEJACY = "0" * 40
TOZSAMOSC_REPO = ("Wlasciciel Repo", "wlasciciel@repo.example")

B1 = "b-1"
B2 = "b-2"            # w zespole dwoch runnerow; id B2 sortuje sie PO B1
OBCY = "0-obcy"       # runner Piekarni; id mniejsze niz kazdy runner Browarka
TOKEN_B1 = "token-runnera-b1"
TOKEN_B2 = "token-runnera-b2"
TOKEN_OBCY = "token-runnera-piekarni"

OCZEKIWANE_PLIKI = ["a.txt", "b.txt", "nowy.txt", "podkatalog/nowy2.txt"]
A_PO_RUNIE = "".join(
    "a.txt linia 5 zmieniona w runie\n" if i == 5 else f"a.txt linia {i}\n" for i in range(1, 11))
STATUS_PO_INTEGRACJI = " M a.txt\n D b.txt\n?? nowy.txt\n?? podkatalog/nowy2.txt\n"

# Pomiar prawdziwego gita: lista przeslania `subprocess.run` w fixture'ach obserwujacych
# nie moze wciagac wywolan pomocnika testowego.
_RUN = subprocess.run


def git(repo: Path, *argumenty: str) -> str:
    return _RUN(
        ["git", "-C", str(repo), *argumenty],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout


def zapisz(korzen: Path, rel: str, tresc: str) -> None:
    cel = korzen / rel
    cel.parent.mkdir(parents=True, exist_ok=True)
    cel.write_text(tresc, encoding="utf-8")


def tresc_pliku(nazwa: str) -> str:
    return "".join(f"{nazwa} linia {i}\n" for i in range(1, 11))


def zmien_linie(korzen: Path, rel: str, numer: int, nowa: str) -> None:
    linie = (korzen / rel).read_text(encoding="utf-8").splitlines(keepends=True)
    linie[numer - 1] = nowa + "\n"
    (korzen / rel).write_text("".join(linie), encoding="utf-8")


def status(katalog: Path) -> str:
    return git(katalog, "status", "--porcelain", "--untracked-files=all")


def reflog_head(repo: Path) -> list[str]:
    return git(repo, "reflog", "show", "HEAD").splitlines()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def izoluj_konfiguracje_gita(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Globalny i systemowy config gita oraz tozsamosc ze srodowiska nie wplywaja na test."""
    pusty_home = tmp_path / "pusty_home"
    pusty_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(pusty_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(pusty_home / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for zmienna in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(zmienna, raising=False)


def zaloz_repo(tmp_path: Path) -> Path:
    """Repozytorium uzytkownika: sledzone pliki, nazwy specjalne, `.cockpit/_run/sledzony.md`."""
    r = tmp_path / "repo_uzytkownika"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.name", TOZSAMOSC_REPO[0])
    git(r, "config", "user.email", TOZSAMOSC_REPO[1])
    git(r, "config", "commit.gpgsign", "false")
    for nazwa in ("a.txt", "b.txt", "c.txt", "zażółć.txt", "plik ze spacja.txt"):
        zapisz(r, nazwa, tresc_pliku(nazwa))
    zapisz(r, ".cockpit/_run/sledzony.md", "stary\n")
    zapisz(r, ".cockpit/inne.txt", "inne\n")
    git(r, "add", "-A")
    # fixture testowy (nie silnik): commit w repozytorium testowym w tmp_path
    git(r, "commit", "-q", "-m", "init")
    return r


def zmiany_runu(wt) -> None:
    """Zmieniony, usuniety i dwa nowe pliki plus artefakty runu, ktorych integrate nie rusza."""
    zmien_linie(wt.sciezka, "a.txt", 5, "a.txt linia 5 zmieniona w runie")
    (wt.sciezka / "b.txt").unlink()
    zapisz(wt.sciezka, "nowy.txt", "nowy plik\n")
    zapisz(wt.sciezka, "podkatalog/nowy2.txt", "drugi nowy plik\n")
    zapisz(wt.sciezka, ".cockpit/_run/skills/x/SKILL.md", "zmaterializowany skill\n")
    zapisz(wt.sciezka, ".cockpit/_run/artefakt.md", "artefakt runu\n")
    zapisz(wt.sciezka, ".cockpit/_run/sledzony.md", "zmieniony artefakt\n")


def komunikat_dla_statusu(status_runu: RunStatus) -> str:
    """Run aktywny odrzuca `run_do_odbioru`; pozostale statusy poza `to_accept` odrzuca odbior."""
    if status_runu in (RunStatus.pending, RunStatus.running, RunStatus.waiting):
        return "run jeszcze trwa"
    return "run nie jest do odbioru"


def podkomenda(argv: list[str]) -> str | None:
    """Podkomenda gita z argv (pomija `-c x` i `-C x` oraz inne opcje globalne)."""
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in ("-c", "-C"):
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return token
    return None


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# =================================================================================
# Fixture'y repozytorium uzytkownika i worktree runu
# =================================================================================

@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    izoluj_konfiguracje_gita(tmp_path, monkeypatch)
    return zaloz_repo(tmp_path)


@pytest.fixture
def runy_w_srodowisku(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runy"
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(root))
    return root


@pytest.fixture
def wt(repo: Path, runy_w_srodowisku: Path):
    return utworz_worktree(repo, RUN_ID, runy_w_srodowisku)


@pytest.fixture
def git_pod_obserwacja(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Lista argv kazdego wywolania `subprocess.run` od momentu uzycia fixture'a."""
    wywolania: list[list[str]] = []

    def run(argv, *a, **kw):
        wywolania.append([str(x) for x in argv])
        return _RUN(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    return wywolania


# =================================================================================
# Runner: repozytorium_docelowe i pliki_runu
# =================================================================================

def test_repozytorium_docelowe_to_korzen_repozytorium_uzytkownika_a_nie_worktree_runu(repo, wt):
    cel = repozytorium_docelowe(wt.sciezka)

    assert cel.resolve() == repo.resolve()
    assert cel.resolve() != wt.sciezka.resolve()


def test_repozytorium_docelowe_nie_zmienia_ani_drzewa_ani_historii(repo, wt):
    zmiany_runu(wt)
    przed = (migawka(repo), migawka(wt.sciezka), reflog_head(repo), status(repo))

    repozytorium_docelowe(wt.sciezka)

    assert (migawka(repo), migawka(wt.sciezka), reflog_head(repo), status(repo)) == przed


def test_katalog_niebedacy_repozytorium_nie_ma_repozytorium_docelowego(tmp_path):
    zwykly = tmp_path / "zwykly_katalog"
    zwykly.mkdir()

    with pytest.raises(BladOdbioru) as blad:
        repozytorium_docelowe(zwykly)

    assert type(blad.value) is BladOdbioru
    assert str(zwykly) not in str(blad.value)


def test_worktree_repozytorium_bez_katalogu_dot_git_nie_ma_repozytorium_docelowego(
    repo, tmp_path,
):
    """Repozytorium typu bare (wspolny katalog gita to nie `.git`): nie ma drzewa roboczego
    uzytkownika, do ktorego mozna by przeniesc zmiany."""
    bare = tmp_path / "baza_bare"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    wt_bare = tmp_path / "wt_z_bare"
    git(bare, "worktree", "add", "--detach", str(wt_bare), "HEAD")

    with pytest.raises(BladOdbioru) as blad:
        repozytorium_docelowe(wt_bare)

    assert type(blad.value) is BladOdbioru
    assert str(bare) not in str(blad.value)


@pytest.mark.parametrize("nazwa_katalogu_gita", [".git ", ".gitX"])
def test_wspolny_katalog_gita_o_nazwie_innej_niz_dot_git_nie_wskazuje_repozytorium_docelowego(
    repo, tmp_path, nazwa_katalogu_gita,
):
    """Nazwa katalogu gita z bialym znakiem albo literalem na koncu to nie `.git`: wynik gita
    przycinamy tylko o znak nowej linii, wiec taka nazwa nie jest brana za repozytorium."""
    magazyn = tmp_path / "magazyn_gita"
    magazyn.mkdir()
    r = tmp_path / "repo_z_osobnym_katalogiem_gita"
    r.mkdir()
    git(r, "init", "-q", f"--separate-git-dir={magazyn / nazwa_katalogu_gita}")
    git(r, "config", "user.name", TOZSAMOSC_REPO[0])
    git(r, "config", "user.email", TOZSAMOSC_REPO[1])
    git(r, "config", "commit.gpgsign", "false")
    # fixture testowy (nie silnik): commit w repozytorium testowym w tmp_path
    git(r, "commit", "-q", "--allow-empty", "-m", "init")
    wt_obce = tmp_path / "wt_z_osobnego_katalogu"
    git(r, "worktree", "add", "--detach", str(wt_obce), "HEAD")

    with pytest.raises(BladOdbioru) as blad:
        repozytorium_docelowe(wt_obce)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wskazal repozytorium uzytkownika"


def test_pliki_runu_to_posortowana_lista_zmienionych_usunietych_i_nowych_bez_artefaktow(wt):
    zmiany_runu(wt)

    assert pliki_runu(wt.sciezka, wt.base_commit) == OCZEKIWANE_PLIKI


def test_pliki_runu_sa_posortowane_takze_gdy_nowy_plik_sortuje_sie_przed_zmienionym(wt):
    """Nowe pliki sa w diffie za sledzonymi: `0-nowy.txt` jest w diffie PO `a.txt`."""
    zmien_linie(wt.sciezka, "a.txt", 5, "zmiana")
    zapisz(wt.sciezka, "0-nowy.txt", "nowy\n")

    assert pliki_runu(wt.sciezka, wt.base_commit) == ["0-nowy.txt", "a.txt"]


def test_pliki_runu_podaja_nazwy_z_polskimi_znakami_i_spacjami_doslownie(wt):
    zmien_linie(wt.sciezka, "zażółć.txt", 5, "zmiana")
    zmien_linie(wt.sciezka, "plik ze spacja.txt", 5, "zmiana")
    zapisz(wt.sciezka, "gęślą jaźń.txt", "nowy\n")

    assert pliki_runu(wt.sciezka, wt.base_commit) == [
        "gęślą jaźń.txt", "plik ze spacja.txt", "zażółć.txt"]


def test_pliki_runu_bez_zmian_to_pusta_lista_a_artefakty_runu_nie_licza_sie(wt):
    zapisz(wt.sciezka, ".cockpit/_run/artefakt.md", "artefakt runu\n")
    zapisz(wt.sciezka, ".cockpit/_run/sledzony.md", "zmieniony artefakt\n")

    assert pliki_runu(wt.sciezka, wt.base_commit) == []


def test_pliki_runu_to_te_same_pliki_co_w_diffie_do_odbioru(wt):
    zmiany_runu(wt)

    diff = zbierz_diff(wt.sciezka, wt.base_commit)
    z_diffu = sorted(w[len("diff --git a/"):].split(" b/")[0]
                     for w in diff.splitlines() if w.startswith("diff --git a/"))

    assert pliki_runu(wt.sciezka, wt.base_commit) == z_diffu


def test_pliki_runu_z_blednym_commitem_bazowym_to_blad_odbioru(wt):
    with pytest.raises(BladOdbioru) as blad:
        pliki_runu(wt.sciezka, SHA_NIEISTNIEJACY)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie zebral diffu runu"


# =================================================================================
# Runner: integruj - skutek w drzewie uzytkownika
# =================================================================================

def test_integracja_przenosi_zmieniony_usuniety_i_nowe_pliki_do_drzewa_uzytkownika(repo, wt):
    zmiany_runu(wt)

    zintegrowane = integruj(wt.sciezka, wt.base_commit)

    assert zintegrowane == OCZEKIWANE_PLIKI
    assert (repo / "a.txt").read_text(encoding="utf-8") == A_PO_RUNIE
    assert not (repo / "b.txt").exists()
    assert (repo / "nowy.txt").read_text(encoding="utf-8") == "nowy plik\n"
    assert (repo / "podkatalog" / "nowy2.txt").read_text(encoding="utf-8") == "drugi nowy plik\n"
    assert status(repo) == STATUS_PO_INTEGRACJI


def test_integracja_nie_przenosi_artefaktow_runu_do_drzewa_uzytkownika(repo, wt):
    zmiany_runu(wt)

    integruj(wt.sciezka, wt.base_commit)

    assert not (repo / ".cockpit" / "_run" / "skills").exists()
    assert not (repo / ".cockpit" / "_run" / "artefakt.md").exists()
    assert (repo / ".cockpit" / "_run" / "sledzony.md").read_text(encoding="utf-8") == "stary\n"


def test_integracja_nie_dopisuje_commita_ani_wpisu_w_reflogu(repo, wt):
    zmiany_runu(wt)
    head_przed, reflog_przed = head(repo), reflog_head(repo)
    liczba_commitow = git(repo, "rev-list", "--all", "--count")

    integruj(wt.sciezka, wt.base_commit)

    assert head(repo) == head_przed
    assert reflog_head(repo) == reflog_przed
    assert git(repo, "rev-list", "--all", "--count") == liczba_commitow


def test_integracja_zostawia_indeks_repozytorium_bez_zmian(repo, wt):
    zmiany_runu(wt)
    indeks_przed = git(repo, "ls-files", "-s")

    integruj(wt.sciezka, wt.base_commit)

    assert git(repo, "ls-files", "-s") == indeks_przed
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_integracja_nie_zmienia_worktree_runu(wt, repo):
    zmiany_runu(wt)
    przed = (migawka(wt.sciezka), status(wt.sciezka))

    integruj(wt.sciezka, wt.base_commit)

    assert (migawka(wt.sciezka), status(wt.sciezka)) == przed


def test_integracja_zachowuje_lokalne_zmiany_uzytkownika_w_innych_plikach_i_miejscach(repo, wt):
    zmiany_runu(wt)
    zmien_linie(repo, "c.txt", 2, "c.txt lokalna zmiana")          # plik, ktorego run nie rusza
    zmien_linie(repo, "a.txt", 10, "a.txt lokalna linia 10")       # inne miejsce tego samego pliku

    integruj(wt.sciezka, wt.base_commit)

    assert "c.txt linia 2\n" not in (repo / "c.txt").read_text(encoding="utf-8")
    assert (repo / "c.txt").read_text(encoding="utf-8").splitlines()[1] == "c.txt lokalna zmiana"
    a = (repo / "a.txt").read_text(encoding="utf-8").splitlines()
    assert a[4] == "a.txt linia 5 zmieniona w runie"
    assert a[9] == "a.txt lokalna linia 10"


@pytest.mark.parametrize("nazwa", ["zażółć.txt", "plik ze spacja.txt"])
def test_integracja_przenosi_plik_o_nazwie_ze_znakami_specjalnymi(repo, wt, nazwa):
    zmien_linie(wt.sciezka, nazwa, 5, "zmiana w runie")

    zintegrowane = integruj(wt.sciezka, wt.base_commit)

    assert zintegrowane == [nazwa]
    assert (repo / nazwa).read_text(encoding="utf-8").splitlines()[4] == "zmiana w runie"


def test_integracja_przenosi_plik_z_bajtem_spoza_utf8_bajt_w_bajt(repo, wt):
    """Znak zastepczy zamiast bajtu 0xff to utrata danych uzytkownika."""
    bajty = b"tekst \xff koniec\n"
    (wt.sciezka / "binarny.dat").write_bytes(bajty)
    zmien_linie(wt.sciezka, "a.txt", 5, "zmiana w runie")

    zintegrowane = integruj(wt.sciezka, wt.base_commit)

    assert zintegrowane == ["a.txt", "binarny.dat"]
    assert (repo / "binarny.dat").read_bytes() == bajty


STARY_OBRAZ = b"\x00\x01\x02stary\xff\n"
NOWY_OBRAZ = b"\x00\x09\x08nowy\xfe\x00\n"
NOWY_PLIK_BINARNY = bytes(range(256))


def run_z_plikami_binarnymi(repo: Path, katalog_runow: Path):
    """Repozytorium ze sledzonym plikiem binarnym; run zmienia go i dodaje drugi plik binarny."""
    (repo / "obraz.bin").write_bytes(STARY_OBRAZ)
    git(repo, "add", "obraz.bin")
    # fixture testowy (nie silnik): commit w repozytorium testowym w tmp_path
    git(repo, "commit", "-q", "-m", "obraz")
    wt2 = utworz_worktree(repo, "run-binarny", katalog_runow)
    (wt2.sciezka / "obraz.bin").write_bytes(NOWY_OBRAZ)
    (wt2.sciezka / "nowy.bin").write_bytes(NOWY_PLIK_BINARNY)
    return wt2


def test_integracja_przenosi_zmieniony_i_nowy_plik_binarny_bajt_w_bajt(
    repo, runy_w_srodowisku,
):
    wt2 = run_z_plikami_binarnymi(repo, runy_w_srodowisku)

    zintegrowane = integruj(wt2.sciezka, wt2.base_commit)

    assert zintegrowane == ["nowy.bin", "obraz.bin"]
    assert (repo / "obraz.bin").read_bytes() == NOWY_OBRAZ
    assert (repo / "nowy.bin").read_bytes() == NOWY_PLIK_BINARNY
    assert status(repo) == " M obraz.bin\n?? nowy.bin\n"
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_odbior_diffu_dla_pliku_binarnego_pokazuje_tylko_ze_pliki_sie_roznia(
    repo, runy_w_srodowisku,
):
    """Diff do odbioru (collect) nie niesie latki binarnej: `--binary` dotyczy tylko integracji."""
    wt2 = run_z_plikami_binarnymi(repo, runy_w_srodowisku)

    diff = zbierz_diff(wt2.sciezka, wt2.base_commit)

    assert "Binary files a/obraz.bin and b/obraz.bin differ" in diff
    assert "Binary files /dev/null and b/nowy.bin differ" in diff
    assert "GIT binary patch" not in diff


# --- konflikty: odmowa z lista plikow, drzewo nietkniete -----------------------------------

def test_konflikt_z_lokalna_zmiana_to_blad_integracji_z_lista_plikow_i_pelnym_komunikatem(
    repo, wt,
):
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert type(blad.value) is BladIntegracji
    assert isinstance(blad.value, BladOdbioru)
    assert blad.value.pliki == ["a.txt"]
    assert str(blad.value) == "zmiany runu koliduja z lokalnymi zmianami: a.txt"


def test_konflikt_jednego_pliku_z_kilku_zostawia_drzewo_uzytkownika_bajtowo_nietkniete(
    repo, wt,
):
    """Atomowosc: pozostale pliki runu (usuniecie b.txt, nowe pliki) tez nie sa zastosowane."""
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")
    przed, status_przed, indeks_przed = migawka(repo), status(repo), git(repo, "ls-files", "-s")

    with pytest.raises(BladIntegracji):
        integruj(wt.sciezka, wt.base_commit)

    assert migawka(repo) == przed
    assert status(repo) == status_przed
    assert git(repo, "ls-files", "-s") == indeks_przed
    assert (repo / "b.txt").exists()
    assert not (repo / "nowy.txt").exists()
    assert not (repo / "podkatalog").exists()


def test_konflikt_nie_zostawia_plikow_odrzuconych_ani_kopii_zapasowych(repo, wt):
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")

    with pytest.raises(BladIntegracji):
        integruj(wt.sciezka, wt.base_commit)

    nazwy = sorted(p.name for p in repo.iterdir() if p.name != ".git")
    assert nazwy == [".cockpit", "a.txt", "b.txt", "c.txt", "plik ze spacja.txt", "zażółć.txt"]


def test_konflikt_na_dwoch_plikach_podaje_oba_posortowane_nawet_gdy_nowy_plik_sortuje_sie_pierwszy(
    repo, wt,
):
    """`0-nowy.txt` jest w diffie za `a.txt`, ale w liscie plikow ma byc przed nim."""
    zmiany_runu(wt)
    zapisz(wt.sciezka, "0-nowy.txt", "nowy w runie\n")
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")
    zapisz(repo, "0-nowy.txt", "lokalny plik o tej samej nazwie\n")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert blad.value.pliki == ["0-nowy.txt", "a.txt"]
    assert str(blad.value) == "zmiany runu koliduja z lokalnymi zmianami: 0-nowy.txt, a.txt"


def test_plik_usuniety_w_runie_a_zmieniony_lokalnie_to_konflikt(repo, wt):
    zmiany_runu(wt)
    zmien_linie(repo, "b.txt", 3, "b.txt lokalna zmiana")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert blad.value.pliki == ["b.txt"]
    assert (repo / "b.txt").exists()


def test_plik_zmieniony_w_runie_a_usuniety_lokalnie_to_konflikt(repo, wt):
    zmiany_runu(wt)
    (repo / "a.txt").unlink()

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert blad.value.pliki == ["a.txt"]
    assert not (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()


def test_nowy_plik_runu_istniejacy_juz_lokalnie_to_konflikt_i_lokalna_tresc_zostaje(repo, wt):
    zmiany_runu(wt)
    zapisz(repo, "nowy.txt", "moj wlasny plik o tej samej nazwie\n")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert blad.value.pliki == ["nowy.txt"]
    assert (repo / "nowy.txt").read_text(encoding="utf-8") == "moj wlasny plik o tej samej nazwie\n"
    assert (repo / "a.txt").read_text(encoding="utf-8") == tresc_pliku("a.txt")


@pytest.mark.parametrize("nazwa", ["zażółć.txt", "plik ze spacja.txt"])
def test_konflikt_w_pliku_o_nazwie_ze_znakami_specjalnymi_podaje_nazwe_doslownie(
    repo, wt, nazwa,
):
    zmien_linie(wt.sciezka, nazwa, 5, "zmiana w runie")
    zmien_linie(repo, nazwa, 5, "zmiana lokalna")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert blad.value.pliki == [nazwa]
    assert str(blad.value) == f"zmiany runu koliduja z lokalnymi zmianami: {nazwa}"


def test_komunikat_konfliktu_nie_zawiera_stderr_gita_ani_sciezek_bezwzglednych(repo, wt):
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    komunikat = str(blad.value)
    assert str(repo) not in komunikat
    assert str(wt.sciezka) not in komunikat
    for slowo in ("error:", "patch failed", "does not apply", "fatal"):
        assert slowo not in komunikat


# --- brak zmian i bledy gita ---------------------------------------------------------------

def test_run_bez_zmian_nie_ma_czego_integrowac(repo, wt):
    przed = (migawka(repo), reflog_head(repo))

    with pytest.raises(BrakZmianRunu) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert type(blad.value) is BrakZmianRunu
    assert isinstance(blad.value, BladOdbioru)
    assert str(blad.value) == "run nie ma zmian do przeniesienia"
    assert (migawka(repo), reflog_head(repo)) == przed


def test_run_z_samymi_artefaktami_runu_nie_ma_czego_integrowac(repo, wt):
    zapisz(wt.sciezka, ".cockpit/_run/artefakt.md", "artefakt runu\n")
    zapisz(wt.sciezka, ".cockpit/_run/sledzony.md", "zmieniony artefakt\n")

    with pytest.raises(BrakZmianRunu) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert str(blad.value) == "run nie ma zmian do przeniesienia"
    assert (repo / ".cockpit" / "_run" / "sledzony.md").read_text(encoding="utf-8") == "stary\n"


@pytest.mark.parametrize("base_commit", ["HEAD", "--output=x", "a" * 39, "A" * 40])
def test_integracja_odrzuca_commit_bazowy_niebedacy_sha(repo, wt, base_commit):
    zmiany_runu(wt)
    przed = migawka(repo)

    with pytest.raises(BladOdbioru) as blad:
        integruj(wt.sciezka, base_commit)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "base_commit nie jest SHA"
    assert migawka(repo) == przed


def test_integracja_z_nieistniejacym_commitem_bazowym_to_blad_odbioru_a_nie_konflikt(repo, wt):
    zmiany_runu(wt)
    przed = migawka(repo)

    with pytest.raises(BladOdbioru) as blad:
        integruj(wt.sciezka, SHA_NIEISTNIEJACY)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie zebral diffu runu"
    assert migawka(repo) == przed


def test_integracja_katalogu_niebedacego_repozytorium_to_blad_odbioru(tmp_path):
    zwykly = tmp_path / "zwykly_katalog"
    zwykly.mkdir()

    with pytest.raises(BladOdbioru) as blad:
        integruj(zwykly, SHA_X)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie zebral diffu runu"


def test_integracja_uzywa_tylko_gita_do_odczytu_i_apply_najpierw_z_check_potem_bez(
    repo, wt, git_pod_obserwacja,
):
    zmiany_runu(wt)
    git_pod_obserwacja.clear()

    integruj(wt.sciezka, wt.base_commit)

    wywolania = [a for a in git_pod_obserwacja if a and a[0] == "git"]
    podkomendy = {podkomenda(a) for a in wywolania}
    zapisujace = {"commit", "push", "add", "reset", "checkout", "switch", "stash", "merge",
                  "rebase", "cherry-pick", "restore", "rm", "mv", "update-index", "fetch",
                  "pull", "clean", "tag", "branch", "am", "worktree"}
    assert podkomendy & zapisujace == set()
    zastosowania = [a for a in wywolania if podkomenda(a) == "apply"]
    sprawdzajace = [i for i, a in enumerate(zastosowania) if "--check" in a]
    wlasciwe = [i for i, a in enumerate(zastosowania)
                if not {"--check", "--numstat", "--stat", "--summary"} & set(a)]
    assert len(wlasciwe) == 1
    assert sprawdzajace and sprawdzajace[0] < wlasciwe[0]
    for a in zastosowania:
        assert not {"--index", "--cached", "--3way", "-3", "--reject", "--intent-to-add"} & set(a)


# =================================================================================
# Operacje runnera `integrate` i `commit`
# =================================================================================

@pytest.mark.parametrize("typ", ["integrate", "commit"])
def test_operacje_integrate_i_commit_sa_zarejestrowane_w_runnerze_jako_korutyny(typ):
    assert typ in operacje.OPERACJE
    assert inspect.iscoroutinefunction(operacje.OPERACJE[typ])


async def _op(typ: str, run_id: str, base_commit: str, **dodatkowe) -> dict:
    return await asyncio.wait_for(operacje.wykonaj(
        {"type": typ, "req_id": "r1", "run_id": run_id, "base_commit": base_commit,
         **dodatkowe}), 60)


async def test_operacja_integrate_zwraca_liste_zintegrowanych_plikow_i_zmienia_drzewo(
    repo, wt, runy_w_srodowisku,
):
    zmiany_runu(wt)

    odp = await _op("integrate", RUN_ID, wt.base_commit)

    assert odp == {"req_id": "r1", "ok": True, "wynik": {"pliki": OCZEKIWANE_PLIKI}}
    assert status(repo) == STATUS_PO_INTEGRACJI


async def test_operacja_integrate_sama_wylicza_worktree_i_repozytorium_a_nie_przyjmuje_sciezek(
    repo, wt, runy_w_srodowisku, tmp_path,
):
    zmiany_runu(wt)
    cudze = tmp_path / "cudze_repo"
    cudze.mkdir()
    git(cudze, "init", "-q")
    zapisz(cudze, "a.txt", tresc_pliku("a.txt"))
    przed_cudzego = migawka(cudze)

    odp = await _op("integrate", RUN_ID, wt.base_commit, repo=str(cudze),
                    repozytorium=str(cudze), sciezka=str(cudze), worktree=str(cudze))

    assert odp["ok"] is True
    assert migawka(cudze) == przed_cudzego
    assert (repo / "a.txt").read_text(encoding="utf-8") == A_PO_RUNIE


async def test_operacja_integrate_przy_konflikcie_daje_kod_konflikt_z_lista_plikow(
    repo, wt, runy_w_srodowisku,
):
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")
    przed = migawka(repo)

    odp = await _op("integrate", RUN_ID, wt.base_commit)

    assert odp["ok"] is False
    assert odp["kod"] == "konflikt"
    assert odp["blad"] == "zmiany runu koliduja z lokalnymi zmianami: a.txt"
    assert migawka(repo) == przed


async def test_operacja_integrate_dla_runu_bez_zmian_daje_kod_brak_zmian(
    repo, wt, runy_w_srodowisku,
):
    odp = await _op("integrate", RUN_ID, wt.base_commit)

    assert odp == {"req_id": "r1", "ok": False, "kod": "brak_zmian",
                   "blad": "run nie ma zmian do przeniesienia"}


@pytest.mark.parametrize("typ", ["integrate", "commit"])
async def test_operacja_dla_runu_bez_worktree_daje_kod_brak_worktree(runy_w_srodowisku, typ):
    odp = await _op(typ, "run-bez-worktree", SHA_X, opis="cockpit: run x")

    assert odp == {"req_id": "r1", "ok": False, "kod": "brak_worktree",
                   "blad": "worktree runu nie istnieje"}


@pytest.mark.parametrize(("typ", "komunikat"), [
    ("integrate", "git nie wykonal integracji runu"),
    ("commit", "git nie wykonal commita"),
])
async def test_operacja_gdy_worktree_nie_jest_repozytorium_daje_kod_blad_gita(
    runy_w_srodowisku, typ, komunikat,
):
    (runy_w_srodowisku / "run-bez-gita" / "worktree").mkdir(parents=True)

    odp = await _op(typ, "run-bez-gita", SHA_X, opis="cockpit: run x")

    assert odp == {"req_id": "r1", "ok": False, "kod": "blad_gita", "blad": komunikat}


@pytest.mark.parametrize("typ", ["integrate", "commit"])
@pytest.mark.parametrize("run_id", ["..", ".", "", "a/b", "../x", "a\\b"])
async def test_operacja_odrzuca_niebezpieczny_identyfikator_runu_i_nie_uruchamia_gita(
    runy_w_srodowisku, git_pod_obserwacja, typ, run_id,
):
    odp = await _op(typ, run_id, SHA_X, opis="cockpit: run x")

    assert odp == {"req_id": "r1", "ok": False, "kod": "zle_dane",
                   "blad": "niedozwolony identyfikator runu"}
    assert git_pod_obserwacja == []


@pytest.mark.parametrize("typ", ["integrate", "commit"])
@pytest.mark.parametrize("base_commit", ["HEAD", "--output=x", "a" * 39, "A" * 40])
async def test_operacja_odrzuca_base_commit_niebedacy_sha_i_nie_uruchamia_gita(
    wt, runy_w_srodowisku, git_pod_obserwacja, typ, base_commit,
):
    git_pod_obserwacja.clear()

    odp = await _op(typ, RUN_ID, base_commit, opis="cockpit: run x")

    assert odp == {"req_id": "r1", "ok": False, "kod": "zle_dane",
                   "blad": "base_commit nie jest SHA"}
    assert git_pod_obserwacja == []


@pytest.mark.parametrize(("dane", "pole"), [
    ({"base_commit": SHA_X}, "run_id"),
    ({"run_id": "r"}, "base_commit"),
    ({"run_id": 5, "base_commit": SHA_X}, "run_id"),
    ({"run_id": "r", "base_commit": 5}, "base_commit"),
    ({"run_id": None, "base_commit": SHA_X}, "run_id"),
], ids=["bez_run_id", "bez_base_commit", "run_id_liczba", "base_commit_liczba", "run_id_null"])
@pytest.mark.parametrize("typ", ["integrate", "commit"])
async def test_operacja_odrzuca_brakujace_i_zle_typem_pola(runy_w_srodowisku, typ, dane, pole):
    odp = await operacje.wykonaj({"type": typ, "req_id": "r1", "opis": "cockpit: run x", **dane})

    assert odp == {"req_id": "r1", "ok": False, "kod": "zle_dane",
                   "blad": f"pole {pole} musi byc napisem"}


# =================================================================================
# Swiat kokpitu: dwa zespoly, runnery, uzytkownicy w roli wlasciciela, czlonka i obserwatora
# =================================================================================

@asynccontextmanager
async def zbuduj_swiat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'odbior.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_b = await zaloz_wlasciciela(
        maker, "browarek@example.com", nazwa_zespolu="Browarek")
    wlasciciel_p, tenant_p = await zaloz_wlasciciela(
        maker, "piekarnia@example.com", nazwa_zespolu="Piekarnia")
    czlonek = await zaloz_czlonka(maker, tenant_b, RolaWZespole.member, "czlonek@browarek.example")
    obserwator = await zaloz_czlonka(
        maker, tenant_b, RolaWZespole.viewer, "obserwator@browarek.example")
    teraz = datetime.now(timezone.utc)
    async with maker() as s:
        s.add_all([
            Runner(id=B1, tenant_id=tenant_b, name=B1, token_hash=hash_token(TOKEN_B1),
                   last_seen_at=teraz),
            Runner(id=B2, tenant_id=tenant_b, name=B2, token_hash=hash_token(TOKEN_B2),
                   last_seen_at=teraz),
            Runner(id=OBCY, tenant_id=tenant_p, name=OBCY, token_hash=hash_token(TOKEN_OBCY),
                   last_seen_at=teraz),
        ])
        await s.commit()
    try:
        async with AsyncExitStack() as stos:
            async def nowy_klient(user=None) -> AsyncClient:
                k = await stos.enter_async_context(
                    AsyncClient(transport=ASGITransport(app=app), base_url="http://test"))
                if user is not None:
                    await zaloguj_klienta(k, maker, user)
                return k

            yield SimpleNamespace(
                maker=maker, engine=engine, tenant_b=tenant_b, tenant_p=tenant_p,
                wlasciciel=wlasciciel, wlasciciel_p=wlasciciel_p, czlonek=czlonek,
                obserwator=obserwator, klient=await nowy_klient(wlasciciel),
                anonim=await nowy_klient(), nowy_klient=nowy_klient)
    finally:
        await engine.dispose()


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
async def swiat(tmp_path, monkeypatch):
    async with zbuduj_swiat(tmp_path, monkeypatch) as s:
        yield s


def podmien_wysylke_na_prawdziwe_operacje(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """`bus.zapytaj` podmienione na wykonanie prawdziwej operacji runnera w tym procesie."""
    wywolania: list[tuple] = []

    async def zapytaj(runner_id, typ, dane, timeout_s):
        wywolania.append((runner_id, typ, dict(dane)))
        odp = await operacje.wykonaj({"type": typ, "req_id": "r-http", **dane})
        return {"ok": odp["ok"], "wynik": odp.get("wynik"), "blad": odp.get("blad"),
                "kod": odp.get("kod")}

    monkeypatch.setattr(bus, "zapytaj", zapytaj)
    return wywolania


@pytest.fixture
def wysylka_do_prawdziwych_operacji(monkeypatch):
    return podmien_wysylke_na_prawdziwe_operacje(monkeypatch)


def podmien_publikacje_na_szyne(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    publikacje: list[tuple] = []

    async def publish_event(run_id, zdarzenie):
        publikacje.append((run_id, dict(zdarzenie)))

    monkeypatch.setattr(bus, "publish_event", publish_event)
    return publikacje


@pytest.fixture
def publikacje(monkeypatch):
    return podmien_publikacje_na_szyne(monkeypatch)


class Wyslij:
    """Atrapa `wyslij` serwisu: odpowiedz zalezy od typu zapytania, kazde wywolanie jest liczone."""

    def __init__(self, integrate=None, commit=None, blad: BaseException | None = None,
                 dziennik: list | None = None) -> None:
        self.odpowiedzi = {
            "integrate": integrate if integrate is not None
            else {"ok": True, "wynik": {"pliki": ["a.txt"]}},
            "commit": commit if commit is not None
            else {"ok": True, "wynik": {"sha": "d" * 40, "pliki": ["a.txt"]}},
        }
        self.blad = blad
        self.dziennik = dziennik if dziennik is not None else []
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane, timeout_s))
        self.dziennik.append(("runner", typ))
        if self.blad is not None:
            raise self.blad
        return self.odpowiedzi[typ]


async def zasiej_run(maker, tenant_id: str, runner_id: str | None,
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


async def zasiej_zdarzenie(maker, tenant_id: str, run_id: str, kind: str, seq: int,
                           text: str = "") -> None:
    async with maker() as s:
        s.add(Event(tenant_id=tenant_id, run_id=run_id, seq=seq, kind=kind, text=text,
                    event_id=str(uuid.uuid4())))
        await s.commit()


async def zdarzenia(maker, run_id: str, kind: str | None = None) -> list[Event]:
    async with maker() as s:
        q = select(Event).where(Event.run_id == run_id).order_by(Event.seq)
        if kind is not None:
            q = q.where(Event.kind == kind)
        return list((await s.execute(q)).scalars().all())


async def run_w_bazie(maker, run_id: str) -> Run:
    async with maker() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


def tekst_zdarzenia(ev: Event) -> dict:
    return json.loads(ev.text)


async def wywolaj_integruj(swiat, run_id: str, wyslij, tenant_id=None, user_id=None,
                           polaczone=(B1, B2)):
    async with swiat.maker() as s:
        return await asyncio.wait_for(
            odbior.integruj_run(
                s, run_id, tenant_id or swiat.tenant_b, user_id or swiat.wlasciciel.id,
                frozenset(polaczone), wyslij), 10)


# =================================================================================
# Serwis kokpitu: integruj_run
# =================================================================================

def test_rodzaje_zdarzen_odbioru_maja_uzgodnione_nazwy():
    zrodlo = odbior if hasattr(odbior, "KIND_ZINTEGROWANO") else runs

    assert zrodlo.KIND_ZINTEGROWANO == "integrated"
    assert getattr(odbior, "KIND_ZACOMMITOWANO", None) == "committed" or \
        runs.KIND_ZACOMMITOWANO == "committed"


async def test_integracja_runu_pyta_runner_tego_runu_o_integrate_z_identyfikatorem_i_base_commit(
    swiat, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 7.5)
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B2, base_commit="c" * 40)
    wyslij = Wyslij()

    await wywolaj_integruj(swiat, run_id, wyslij)

    assert wyslij.wywolania == [
        (B2, "integrate", {"run_id": run_id, "base_commit": "c" * 40}, 7.5)]


async def test_integracja_zapisuje_jedno_zdarzenie_integrated_z_uzytkownikiem_sesji(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, run_id, "output", 1)
    await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, run_id, "run_finished", 2)

    zwrocone = await wywolaj_integruj(swiat, run_id, Wyslij(), user_id=swiat.czlonek.id)

    zapisane = await zdarzenia(swiat.maker, run_id, "integrated")
    assert len(zapisane) == 1
    ev = zapisane[0]
    assert (ev.tenant_id, ev.run_id, ev.seq) == (swiat.tenant_b, run_id, 3)
    assert tekst_zdarzenia(ev) == {"user_id": swiat.czlonek.id, "email": swiat.czlonek.email}
    assert ev.at is not None
    assert zwrocone.zdarzenie.kind == "integrated"
    assert zwrocone.zdarzenie.seq == 3
    assert zwrocone.pliki == ["a.txt"]
    assert zwrocone.sha is None
    assert tekst_zdarzenia(zwrocone.zdarzenie) == {"user_id": swiat.czlonek.id, "email": swiat.czlonek.email}


async def test_integracja_nie_zmienia_statusu_runu_ani_jego_pol(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit="c" * 40)

    await wywolaj_integruj(swiat, run_id, Wyslij())

    run = await run_w_bazie(swiat.maker, run_id)
    assert run.status == RunStatus.to_accept
    assert run.base_commit == "c" * 40
    assert run.runner_id == B1


async def test_drugie_integrate_tego_samego_runu_daje_stan_niemozliwy_i_jedno_zdarzenie(swiat):
    """Runner odpowiada sukcesem takze za drugim razem: tylko serwis moze odmowic."""
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    wyslij = Wyslij()
    await wywolaj_integruj(swiat, run_id, wyslij)

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "zmiany runu zostaly juz zintegrowane"
    assert len(wyslij.wywolania) == 1
    assert len(await zdarzenia(swiat.maker, run_id, "integrated")) == 1


@pytest.mark.parametrize("status", [s for s in RunStatus if s != RunStatus.to_accept],
                         ids=lambda s: s.value)
async def test_integracja_runu_ktory_nie_jest_do_odbioru_daje_stan_niemozliwy_bez_zdarzenia(
    swiat, status,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, status)
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == komunikat_dla_statusu(status)
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id) == []


async def test_integracja_runu_innego_zespolu_to_nie_znaleziono_i_nic_nie_trafia_do_runnera(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_p, OBCY)
    wyslij = Wyslij()

    with pytest.raises(NieZnaleziono) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij, polaczone=(B1, B2, OBCY))

    assert str(blad.value) == "run nieznany"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id) == []


async def test_integracja_runu_bez_base_commit_daje_stan_niemozliwy_bez_pytania_runnera(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=None)
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij)

    assert str(blad.value) == "run nie ma base_commit"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id) == []


async def test_integracja_gdy_runner_runu_jest_offline_to_runner_offline_bez_zdarzenia(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B2)
    wyslij = Wyslij()

    with pytest.raises(RunnerOffline) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij, polaczone=(B1,))

    assert str(blad.value) == "runner runu jest offline"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id) == []


@pytest.mark.parametrize("przeszkadzajace", ["inny_run", "inny_zespol", "inny_rodzaj"])
async def test_zdarzenie_innego_runu_zespolu_albo_rodzaju_nie_blokuje_integracji(
    swiat, przeszkadzajace,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    if przeszkadzajace == "inny_run":
        inny = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, inny, "integrated", 1)
    elif przeszkadzajace == "inny_zespol":
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_p, run_id, "integrated", 50)
    else:
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, run_id, "committed", 1)
    wyslij = Wyslij()

    zwrocone = await wywolaj_integruj(swiat, run_id, wyslij)

    assert zwrocone.zdarzenie.kind == "integrated"
    assert len(wyslij.wywolania) == 1


@pytest.mark.parametrize("cialo", [
    {"ok": True},
    {"ok": True, "wynik": None},
    {"ok": True, "wynik": {}},
    {"ok": True, "wynik": {"inne": ["a.txt"]}},
    {"ok": True, "wynik": {"pliki": "a.txt"}},
    {"ok": True, "wynik": {"pliki": None}},
    {"ok": True, "wynik": {"pliki": [5]}},
    {"ok": True, "wynik": {"pliki": ["a.txt", None]}},
    {"ok": True, "wynik": "a.txt"},
    {"ok": True, "wynik": ["a.txt"]},
], ids=["bez_wyniku", "wynik_null", "wynik_pusty", "bez_klucza_pliki", "pliki_napis",
        "pliki_null", "pliki_liczby", "pliki_z_null", "wynik_napis", "wynik_lista"])
async def test_niepoprawny_wynik_integrate_od_runnera_to_stan_niemozliwy_bez_zdarzenia(
    swiat, cialo,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, Wyslij(integrate=cialo))

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "runner zwrocil zly wynik integrate"
    assert await zdarzenia(swiat.maker, run_id) == []


async def test_blad_zgloszony_przez_runner_przy_integracji_trafia_do_wolajacego_bez_zdarzenia(
    swiat,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    komunikat = "zmiany runu koliduja z lokalnymi zmianami: a.txt"
    wyslij = Wyslij(integrate={"ok": False, "blad": komunikat, "kod": "konflikt"})

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == komunikat
    assert await zdarzenia(swiat.maker, run_id) == []


async def test_runner_ktory_nie_odpowiada_na_integrate_w_limicie_czasu_nie_zostawia_zdarzenia(
    swiat,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, Wyslij(blad=TimeoutError()))

    assert str(blad.value) == "runner nie odpowiedzial w limicie czasu"
    assert await zdarzenia(swiat.maker, run_id) == []


async def _zapytania_select_dla_postgresa(swiat, wywolanie, dziennik: list) -> list[str]:
    """Wykonuje `wywolanie` i zwraca SELECT-y skompilowane dialektem PostgreSQL. SQLite
    ignoruje FOR UPDATE, wiec blokade widac dopiero w zapytaniu dla PostgreSQL. Kazdy SELECT
    trafia tez do `dziennik`, zeby sprawdzic jego kolejnosc wzgledem zapytania do runnera."""
    sql: list[str] = []

    def zbierz(conn, clauseelement, multiparams, params, execution_options):
        if isinstance(clauseelement, Select):
            q = str(clauseelement.compile(dialect=postgresql.dialect()))
            sql.append(q)
            dziennik.append(("sql", q))

    event.listen(swiat.engine.sync_engine, "before_execute", zbierz)
    try:
        await wywolanie()
    finally:
        event.remove(swiat.engine.sync_engine, "before_execute", zbierz)
    return sql


def _blokada_wiersza_runu(q: str) -> bool:
    return bool(re.search(r"FROM run\b", q)) and q.rstrip().endswith("FOR UPDATE")


async def test_integracja_blokuje_wiersz_runu_przez_for_update_przed_pytaniem_runnera(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    dziennik: list = []
    wyslij = Wyslij(dziennik=dziennik)

    sql = await _zapytania_select_dla_postgresa(
        swiat, lambda: wywolaj_integruj(swiat, run_id, wyslij), dziennik)

    assert any(_blokada_wiersza_runu(q) for q in sql), f"brak FOR UPDATE na run: {sql}"
    pierwsza_blokada = next(i for i, (rodzaj, tresc) in enumerate(dziennik)
                            if rodzaj == "sql" and _blokada_wiersza_runu(tresc))
    pytanie_do_runnera = dziennik.index(("runner", "integrate"))
    assert pierwsza_blokada < pytanie_do_runnera


# =================================================================================
# Zdarzenia odbioru zapisuje wylacznie kokpit: runner nie moze ich zglosic
# =================================================================================

def _odmowa_runnera(kind: str) -> str:
    # Pelna tresc odmowy strażnika `RODZAJE_CZLOWIEKA` (HAR-234), ktory obejmuje zdarzenia odbioru.
    return (f"niezmiennik approval_required: rodzaj zdarzenia {kind!r} jest zarezerwowany "
            "dla decyzji czlowieka; runner nie moze go zapisac")


@pytest.mark.parametrize("kind", ["integrated", "committed"])
async def test_serwis_zdarzen_odrzuca_zdarzenie_odbioru_zgloszone_przez_runnera(swiat, kind):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, RunStatus.running)
    async with swiat.maker() as s:
        run = await runs.run_dla_najemcy(s, run_id, swiat.tenant_b)
        with pytest.raises(NaruszonyNiezmiennik) as blad:
            await runs.zapisz_zdarzenie(s, run, event_id=str(uuid.uuid4()), kind=kind)

    assert type(blad.value) is NaruszonyNiezmiennik
    assert str(blad.value) == _odmowa_runnera(kind)
    assert await zdarzenia(swiat.maker, run_id) == []


@pytest.mark.parametrize("kind", ["integrated", "committed"])
async def test_runner_zglaszajacy_zdarzenie_odbioru_dostaje_422_a_zdarzenia_nie_ma_w_bazie(
    swiat, kind,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, RunStatus.running)

    r = await asyncio.wait_for(swiat.anonim.post(
        "/api/events",
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": "s0", "kind": kind,
              "text": json.dumps({"user_id": "x", "email": "falszywy@example.com"})},
        headers={"Authorization": f"Bearer {TOKEN_B1}"}), 60)

    assert r.status_code == 422
    assert r.json()["detail"] == _odmowa_runnera(kind)
    assert await zdarzenia(swiat.maker, run_id, kind) == []


async def test_zwykle_zdarzenie_od_runnera_jest_nadal_przyjmowane_obok_zakazu(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, RunStatus.running)

    r = await asyncio.wait_for(swiat.anonim.post(
        "/api/events",
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": "s0",
              "kind": "output", "text": "linia"},
        headers={"Authorization": f"Bearer {TOKEN_B1}"}), 60)

    assert r.status_code == 200
    assert len(await zdarzenia(swiat.maker, run_id, "output")) == 1


async def test_sfalszowane_integrated_od_runnera_nie_odblokowuje_commita(
    swiat, monkeypatch,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, RunStatus.running)
    bus.mark_runner_connected(B1)
    wysylka = Wyslij()

    async def zapytaj(runner_id, typ, dane, timeout_s):
        return await wysylka(runner_id, typ, dane, timeout_s)

    monkeypatch.setattr(bus, "zapytaj", zapytaj)
    forged = await swiat.anonim.post(
        "/api/events",
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": "s0",
              "kind": "integrated"},
        headers={"Authorization": f"Bearer {TOKEN_B1}"})
    async with swiat.maker() as s:
        await s.execute(update(Run).where(Run.id == run_id).values(status=RunStatus.to_accept))
        await s.commit()

    r = await asyncio.wait_for(swiat.klient.post(f"/api/runs/{run_id}/commit"), 60)

    assert forged.status_code == 422
    assert forged.json()["detail"] == _odmowa_runnera("integrated")
    assert r.status_code == 409
    assert r.json()["detail"] == "zmiany runu nie zostaly jeszcze zintegrowane"
    assert wysylka.wywolania == []
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


# =================================================================================
# Trasa POST /api/runs/{id}/integrate
# =================================================================================

async def _integrate(klient: AsyncClient, run_id: str):
    return await asyncio.wait_for(klient.post(f"/api/runs/{run_id}/integrate"), 60)


async def zasiej_run_z_worktree(swiat, wt, status=RunStatus.to_accept, runner=B1):
    await zasiej_run(swiat.maker, swiat.tenant_b, runner, status,
                     base_commit=wt.base_commit, run_id=RUN_ID)
    bus.mark_runner_connected(runner)


async def test_integrate_daje_200_z_lista_plikow_a_zmiany_trafiaja_do_drzewa_uzytkownika(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 200, r.text
    cialo = r.json()
    assert set(cialo) == {"run_id", "at", "pliki"}
    assert cialo["run_id"] == RUN_ID
    assert cialo["pliki"] == OCZEKIWANE_PLIKI
    assert wysylka_do_prawdziwych_operacji == [
        (B1, "integrate", {"run_id": RUN_ID, "base_commit": wt.base_commit})]
    assert status(repo) == STATUS_PO_INTEGRACJI
    assert (repo / "a.txt").read_text(encoding="utf-8") == A_PO_RUNIE


async def test_integrate_zapisuje_w_historii_runu_kto_i_kiedy(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    przed = datetime.now(timezone.utc)

    r = await _integrate(swiat.klient, RUN_ID)

    zapisane = await zdarzenia(swiat.maker, RUN_ID, "integrated")
    assert len(zapisane) == 1
    ev = zapisane[0]
    assert ev.tenant_id == swiat.tenant_b
    assert tekst_zdarzenia(ev) == {"user_id": swiat.wlasciciel.id,
                                   "email": "browarek@example.com"}
    assert przed - timedelta(seconds=1) <= _utc(ev.at) <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert abs(_utc(datetime.fromisoformat(r.json()["at"])) - _utc(ev.at)) < timedelta(seconds=1)


async def test_integrate_przez_czlonka_zespolu_zapisuje_jego_adres_a_nie_wlasciciela(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    klient = await swiat.nowy_klient(swiat.czlonek)

    r = await _integrate(klient, RUN_ID)

    assert r.status_code == 200, r.text
    ev = (await zdarzenia(swiat.maker, RUN_ID, "integrated"))[0]
    assert tekst_zdarzenia(ev) == {"user_id": swiat.czlonek.id,
                                   "email": "czlonek@browarek.example"}


async def test_integrate_publikuje_zdarzenie_na_szynie_po_sukcesie(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji, publikacje,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 200
    ev = (await zdarzenia(swiat.maker, RUN_ID, "integrated"))[0]
    assert len(publikacje) == 1
    run_id, zdarzenie = publikacje[0]
    assert run_id == RUN_ID
    assert zdarzenie == {"seq": ev.seq, "kind": "integrated", "text": ev.text, "step_id": "",
                         "text_truncated": False, "text_full_len": len(ev.text)}


async def test_integrate_nie_zmienia_statusu_runu(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)

    await _integrate(swiat.klient, RUN_ID)

    run = await run_w_bazie(swiat.maker, RUN_ID)
    assert run.status == RunStatus.to_accept
    assert run.base_commit == wt.base_commit


async def test_drugie_integrate_tego_samego_runu_daje_409_i_jedno_zdarzenie(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji, publikacje,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    await _integrate(swiat.klient, RUN_ID)
    po_pierwszym = (migawka(repo), status(repo))

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "zmiany runu zostaly juz zintegrowane"
    assert len(await zdarzenia(swiat.maker, RUN_ID, "integrated")) == 1
    assert [typ for _, typ, _ in wysylka_do_prawdziwych_operacji] == ["integrate"]
    assert (migawka(repo), status(repo)) == po_pierwszym
    assert len(publikacje) == 1


@pytest.mark.parametrize("status_runu", [s for s in RunStatus if s != RunStatus.to_accept],
                         ids=lambda s: s.value)
async def test_integrate_runu_ktory_nie_jest_do_odbioru_daje_409_i_nie_rusza_drzewa(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji, publikacje, status_runu,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt, status_runu)
    przed = migawka(repo)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == komunikat_dla_statusu(status_runu)
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert migawka(repo) == przed
    assert publikacje == []


async def test_integrate_runu_innego_zespolu_daje_404_a_baza_i_drzewo_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_p, OBCY, base_commit=wt.base_commit,
                     run_id=RUN_ID)
    bus.mark_runner_connected(OBCY)
    przed = migawka(repo)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 404
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert migawka(repo) == przed


async def test_integrate_nieznanego_runu_daje_404(swiat):
    assert (await _integrate(swiat.klient, "nie-ma-takiego-runu")).status_code == 404


async def test_obserwator_nie_integruje_runu_a_drzewo_i_baza_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    klient = await swiat.nowy_klient(swiat.obserwator)
    przed = migawka(repo)

    r = await _integrate(klient, RUN_ID)

    assert r.status_code == 403
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert migawka(repo) == przed


async def test_integrate_bez_sesji_daje_401_a_drzewo_i_baza_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    przed = migawka(repo)

    r = await _integrate(swiat.anonim, RUN_ID)

    assert r.status_code == 401
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert migawka(repo) == przed


async def test_integrate_bez_tokenu_csrf_daje_403_a_drzewo_i_baza_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    klient = await swiat.nowy_klient(swiat.wlasciciel)
    del klient.headers[NAGLOWEK_CSRF]
    przed = migawka(repo)

    r = await _integrate(klient, RUN_ID)

    assert r.status_code == 403
    assert r.json() == {"detail": "brak tokenu CSRF"}
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert migawka(repo) == przed


async def test_integrate_gdy_runner_runu_jest_offline_daje_409_bez_zdarzenia(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    przed = migawka(repo)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "runner runu jest offline"
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert migawka(repo) == przed


async def test_integrate_przy_konflikcie_daje_409_z_lista_plikow_bez_zdarzenia_i_bez_zmian_w_drzewie(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji, publikacje,
):
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")
    await zasiej_run_z_worktree(swiat, wt)
    przed, status_przed = migawka(repo), status(repo)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "zmiany runu koliduja z lokalnymi zmianami: a.txt"
    assert await zdarzenia(swiat.maker, RUN_ID) == []
    assert (migawka(repo), status(repo)) == (przed, status_przed)
    assert publikacje == []


async def test_integrate_nie_dopisuje_commita_ani_wpisu_w_reflogu_repozytorium(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    reflog_przed, head_przed = reflog_head(repo), head(repo)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 200
    assert reflog_head(repo) == reflog_przed
    assert head(repo) == head_przed
    assert git(repo, "diff", "--cached", "--name-only") == ""


async def test_run_zakonczony_bez_integrate_zostawia_drzewo_i_historie_uzytkownika_nietkniete(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    """Sam koniec runu i odbior diffu (collect) nie przenosza niczego do drzewa uzytkownika."""
    zmiany_runu(wt)
    await zasiej_run_z_worktree(swiat, wt)
    przed = (migawka(repo), status(repo), reflog_head(repo), git(repo, "ls-files", "-s"))

    odp = await asyncio.wait_for(swiat.klient.get(f"/api/runs/{RUN_ID}/diff"), 60)

    assert odp.status_code == 200
    assert "+++ b/nowy.txt" in odp.json()["diff"]
    assert (migawka(repo), status(repo), reflog_head(repo), git(repo, "ls-files", "-s")) == przed
    assert status(repo) == ""
    assert await zdarzenia(swiat.maker, RUN_ID, "integrated") == []


# --- przelot end-to-end: prawdziwa aplikacja + prawdziwy runner ----------------------------

@pytest.fixture
async def runner_zywy(swiat, monkeypatch):
    """Prawdziwy runner B1 polaczony z prawdziwa aplikacja; zatrzymany po tescie."""
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 20.0)
    runner = RunnerKlient("http://kokpit.test", TOKEN_B1,
                          transport=TransportASGIStrumieniujacy(app_mod.app))
    nasluch = asyncio.create_task(runner.listen())
    try:
        koniec = time.monotonic() + 10
        while B1 not in bus.connected_runner_ids():
            assert time.monotonic() < koniec, "runner nie polaczyl sie w limicie czasu"
            await asyncio.sleep(0.02)
        yield runner
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        zakonczone, _ = await asyncio.wait({nasluch}, timeout=10)
        if nasluch not in zakonczone:
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=5)


async def test_integrate_przez_prawdziwego_runnera_przenosi_zmiany_i_zapisuje_zdarzenie(
    swiat, repo, wt, runner_zywy,
):
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    reflog_przed = reflog_head(repo)

    r = await _integrate(swiat.klient, RUN_ID)

    assert r.status_code == 200, r.text
    assert r.json()["pliki"] == OCZEKIWANE_PLIKI
    assert status(repo) == STATUS_PO_INTEGRACJI
    assert reflog_head(repo) == reflog_przed
    assert len(await zdarzenia(swiat.maker, RUN_ID, "integrated")) == 1
    assert bus._oczekujace == {}


# =================================================================================
# HAR-179 faza 2: gałęzie tłumaczące błędy, nazwy plików, użytkownik spoza zespołu
# =================================================================================

KOMUNIKAT_NAZWY = "zmiany runu zawieraja nazwe pliku, ktorej nie da sie przeniesc"
KOMUNIKAT_REPOZYTORIUM = "git nie wskazal repozytorium uzytkownika"


@pytest.mark.parametrize("naglowek", [
    b"a/ b/",
    b"a/x.txt b/y.txt",
    b"a/zly\xff.txt b/zly\xff.txt",
], ids=["pusta_nazwa", "inna_nazwa_przed_i_po", "nazwa_spoza_utf8"])
def test_nazwa_pliku_ktorej_nie_da_sie_przeniesc_to_blad_odbioru_z_pelnym_komunikatem(
    repo, wt, monkeypatch, naglowek,
):
    """Diff podstawiony na wejsciu funkcji dzielacej (reszta kodu prawdziwa): na prawdziwych
    plikach nie da sie utworzyc nazwy spoza UTF-8 (APFS), a pusta i rozna nazwa nie powstaja."""
    diff = b"diff --git " + naglowek + b"\n--- /dev/null\n+++ b/x\n"
    monkeypatch.setattr(modul_odbioru, "_zbierz_diff_bajty",
                        lambda w, b, binarny=False: diff)
    przed = migawka(repo)

    for wywolanie in (pliki_runu, integruj):
        with pytest.raises(BladOdbioru) as blad:
            wywolanie(wt.sciezka, wt.base_commit)

        assert type(blad.value) is BladOdbioru
        assert str(blad.value) == KOMUNIKAT_NAZWY
    assert migawka(repo) == przed


def test_nazwa_pliku_ze_znakiem_tabulacji_jest_cytowana_przez_git_wiec_run_nie_da_sie_przeniesc(
    repo, wt,
):
    zapisz(wt.sciezka, "tab\tw_nazwie.txt", "x\n")
    zmien_linie(wt.sciezka, "a.txt", 5, "zmiana w runie")
    przed = migawka(repo)

    for wywolanie in (pliki_runu, integruj):
        with pytest.raises(BladOdbioru) as blad:
            wywolanie(wt.sciezka, wt.base_commit)

        assert type(blad.value) is BladOdbioru
        assert str(blad.value) == KOMUNIKAT_NAZWY
    assert migawka(repo) == przed


def test_nazwa_pliku_z_fragmentem_spacja_b_ukosnik_jest_podana_i_przeniesiona_doslownie(repo, wt):
    zapisz(wt.sciezka, "x b/plik.txt", "nowy\n")

    assert pliki_runu(wt.sciezka, wt.base_commit) == ["x b/plik.txt"]
    assert integruj(wt.sciezka, wt.base_commit) == ["x b/plik.txt"]
    assert (repo / "x b" / "plik.txt").read_text(encoding="utf-8") == "nowy\n"


def test_odmowa_calosci_bez_kolidujacego_kawalka_to_blad_odbioru_a_nie_pusta_lista_plikow(
    repo, wt, monkeypatch,
):
    """Calosciowy `apply --check` odmawia, a zaden kawalek osobno nie koliduje (kod 0 per plik):
    to blad gita, nie `BladIntegracji` z pusta lista. Podmieniony jest tylko wynik calosciowego
    sprawdzenia; sprawdzenia per plik i reszta idzie prawdziwym kodem."""
    zmiany_runu(wt)
    pelny = modul_odbioru._zbierz_diff_bajty(wt.sciezka, wt.base_commit, True)
    prawdziwa = modul_odbioru._git_z_kodem

    def podmiana(*a, **kw):
        wynik = prawdziwa(*a, **kw)
        wejscie = a[3] if len(a) > 3 else kw.get("wejscie")
        if a[1] == ["apply", "--check"] and wejscie == pelny:
            return b"", 1
        return wynik

    monkeypatch.setattr(modul_odbioru, "_git_z_kodem", podmiana)
    przed = migawka(repo)

    with pytest.raises(BladOdbioru) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wykonal integracji runu"
    assert migawka(repo) == przed


def test_komunikat_braku_repozytorium_docelowego_dla_katalogu_bez_gita_jest_staly(tmp_path):
    zwykly = tmp_path / "zwykly_katalog"
    zwykly.mkdir()

    with pytest.raises(BladOdbioru) as blad:
        repozytorium_docelowe(zwykly)

    assert str(blad.value) == KOMUNIKAT_REPOZYTORIUM


def test_komunikat_braku_repozytorium_docelowego_dla_worktree_z_bare_jest_staly(repo, tmp_path):
    bare = tmp_path / "baza_bare"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    wt_bare = tmp_path / "wt_z_bare"
    git(bare, "worktree", "add", "--detach", str(wt_bare), "HEAD")

    with pytest.raises(BladOdbioru) as blad:
        repozytorium_docelowe(wt_bare)

    assert str(blad.value) == KOMUNIKAT_REPOZYTORIUM


async def test_pusta_lista_plikow_w_odpowiedzi_integrate_to_stan_niemozliwy_bez_zdarzenia(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    wyslij = Wyslij(integrate={"ok": True, "wynik": {"pliki": []}})

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "runner zwrocil zly wynik integrate"
    assert await zdarzenia(swiat.maker, run_id) == []


@pytest.mark.parametrize("kto", ["uzytkownik_innego_zespolu", "nieistniejacy_uzytkownik"])
async def test_integracja_przez_uzytkownika_spoza_zespolu_runu_nie_pyta_runnera_i_nie_zapisuje(
    swiat, kto,
):
    """Uzytkownik z kontem w innym zespole (adres istnieje w bazie) nie jest autorem akcji."""
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    user_id = swiat.wlasciciel_p.id if kto == "uzytkownik_innego_zespolu" else "nie-ma-takiego"
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_integruj(swiat, run_id, wyslij, user_id=user_id)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "uzytkownik nie nalezy do zespolu runu"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id) == []


# =================================================================================
# HAR-179 poprawka 3: kod wyjscia gita spoza dozwolonych to blad z komunikatem dziedziny
# =================================================================================

KOMUNIKAT_INTEGRACJI = "git nie wykonal integracji runu"


def wymus_kod_gita(monkeypatch: pytest.MonkeyPatch, wybor, kod: int) -> None:
    """Prawdziwy git dla wszystkich wywolan, a dla wybranych (`wybor(argv, wejscie)`) zwracamy
    podany kod wyjscia. Zaslepiony jest tylko kod procesu: `_git_z_kodem` jest prawdziwe, wiec
    komunikat bledu wybiera jego wlasny kod."""
    def run(argv, *a, **kw):
        if wybor([str(x) for x in argv], kw.get("input")):
            return subprocess.CompletedProcess(argv, kod, stdout=b"", stderr=b"fatal: zaslepka")
        return _RUN(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


def _apply_check(argv: list[str]) -> bool:
    return podkomenda(argv) == "apply" and "--check" in argv


def test_calosciowe_sprawdzenie_apply_z_kodem_spoza_zakresu_to_blad_odbioru_z_komunikatem_integracji(
    repo, wt, monkeypatch,
):
    zmiany_runu(wt)
    przed = migawka(repo)
    wymus_kod_gita(monkeypatch, lambda argv, wejscie: _apply_check(argv), 128)

    with pytest.raises(BladOdbioru) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == KOMUNIKAT_INTEGRACJI
    assert migawka(repo) == przed


def test_wlasciwe_apply_z_kodem_spoza_zakresu_to_blad_odbioru_z_komunikatem_integracji(
    repo, wt, monkeypatch,
):
    zmiany_runu(wt)
    przed = migawka(repo)
    wymus_kod_gita(
        monkeypatch, lambda argv, wejscie: podkomenda(argv) == "apply" and not _apply_check(argv),
        128)

    with pytest.raises(BladOdbioru) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == KOMUNIKAT_INTEGRACJI
    assert migawka(repo) == przed


def test_sprawdzenie_apply_pojedynczego_pliku_z_kodem_spoza_zakresu_to_blad_odbioru(
    repo, wt, monkeypatch,
):
    """Calosciowe sprawdzenie odmawia (kod 1), a sprawdzenie kawalka pliku konczy sie bledem gita."""
    zmiany_runu(wt)
    pelny = modul_odbioru._zbierz_diff_bajty(wt.sciezka, wt.base_commit, True)
    przed = migawka(repo)

    prawdziwy = subprocess.run

    def run(argv, *a, **kw):
        argv = [str(x) for x in argv]
        if _apply_check(argv):
            kod = 1 if kw.get("input") == pelny else 128
            return subprocess.CompletedProcess(argv, kod, stdout=b"", stderr=b"fatal: zaslepka")
        return prawdziwy(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(BladOdbioru) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == KOMUNIKAT_INTEGRACJI
    assert migawka(repo) == przed


def test_jednoznakowa_nazwa_pliku_runu_jest_podana_i_przeniesiona(repo, wt):
    zapisz(wt.sciezka, "x", "plik o jednoznakowej nazwie\n")

    assert pliki_runu(wt.sciezka, wt.base_commit) == ["x"]
    assert integruj(wt.sciezka, wt.base_commit) == ["x"]
    assert (repo / "x").read_text(encoding="utf-8") == "plik o jednoznakowej nazwie\n"


def test_konflikt_tylko_drugiego_pliku_w_diffie_nie_obejmuje_pierwszego(repo, wt):
    """Kawalek pierwszego pliku konczy sie przed naglowkiem drugiego: gdyby obejmowal tez drugi,
    pierwszy plik bylby zglaszany jako kolidujacy."""
    zmien_linie(wt.sciezka, "a.txt", 5, "a.txt linia 5 zmieniona w runie")
    zmien_linie(wt.sciezka, "b.txt", 5, "b.txt linia 5 zmieniona w runie")
    zmien_linie(repo, "b.txt", 5, "b.txt linia 5 lokalna")

    with pytest.raises(BladIntegracji) as blad:
        integruj(wt.sciezka, wt.base_commit)

    assert blad.value.pliki == ["b.txt"]
    assert str(blad.value) == "zmiany runu koliduja z lokalnymi zmianami: b.txt"
    assert (repo / "a.txt").read_text(encoding="utf-8") == tresc_pliku("a.txt")

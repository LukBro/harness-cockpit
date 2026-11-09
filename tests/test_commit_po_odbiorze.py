"""HAR-179 (HAR-116): `commit` - osobna akcja czlowieka po `integrate`, tylko pliki runu,
tozsamosc gita uzytkownika, wpis w historii runu (kto, kiedy, SHA), push nie istnieje.

Warstwy: runner (`runner.odbior.commituj`, prawdziwy git w tmp_path), operacja runnera `commit`,
serwis kokpitu (`cockpit.services.odbior.commituj_run`) i trasa `POST /api/runs/{id}/commit`.
Testy pisane PRZED implementacja (TDD). Pomocniki i swiat kokpitu pochodza z
`test_integrate.py` (importowane sa funkcje i klasy, fixture'y sa zdefiniowane tu).

Niezmienniki: `no_git_write` (commit tylko jako osobna, jawna akcja czlowieka po `integrate`;
push nigdy: zdalne repozytorium `origin` bez zmian w kazdym scenariuszu), `evidence_required`
(brak tozsamosci albo odrzucenie przez hook to blad, nie zielone przejscie), `tenant_id`.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from sqlalchemy import update

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.api.zaleznosci import NAGLOWEK_CSRF
from cockpit.db.models import Run, RunStatus
from cockpit.services import odbior
from cockpit.services.errors import NieZnaleziono, RunnerOffline, StanNiemozliwy
from cockpit.services.walidacja import migawka
from cockpit.settings import settings
from runner import operacje
from runner.client import Runner as RunnerKlient
from runner import odbior as modul_odbioru
from runner.odbior import SHA_PELNY, BladOdbioru, BrakZmianRunu, commituj, integruj
from runner.worktree import utworz_worktree
from test_integrate import (
    A_PO_RUNIE,
    NOWY_OBRAZ,
    NOWY_PLIK_BINARNY,
    B1,
    B2,
    OBCY,
    OCZEKIWANE_PLIKI,
    RUN_ID,
    TOKEN_B1,
    TOZSAMOSC_REPO,
    Wyslij,
    _blokada_wiersza_runu,
    komunikat_dla_statusu,
    _zapytania_select_dla_postgresa,
    git,
    head,
    izoluj_konfiguracje_gita,
    podkomenda,
    podmien_publikacje_na_szyne,
    podmien_wysylke_na_prawdziwe_operacje,
    run_w_bazie,
    run_z_plikami_binarnymi,
    status,
    wymus_kod_gita,
    wywolaj_integruj,
    zaloz_repo,
    zapisz,
    zasiej_run,
    zasiej_zdarzenie,
    zbuduj_swiat,
    zdarzenia,
    zmiany_runu,
    zmien_linie,
)
from transport_asgi import TransportASGIStrumieniujacy

OPIS = f"cockpit: run {RUN_ID}"


def tematy_reflogu(repo: Path) -> list[str]:
    """Tematy wpisow reflogu HEAD (bez numerow `HEAD@{n}`, ktore przesuwaja sie z kazdym wpisem)."""
    return git(repo, "reflog", "show", "--format=%gs", "HEAD").splitlines()


def zmienione_w_head(repo: Path) -> str:
    return git(repo, "show", "--name-status", "--format=", "HEAD")


def stan_zdalny(repo: Path, bare: Path) -> tuple[str, str, str]:
    """Wszystko, co zmieniloby sie po push: refy zdalnego, sledzace refy lokalne i konfiguracja."""
    return (git(bare, "for-each-ref"), git(repo, "for-each-ref", "refs/remotes"),
            git(repo, "config", "--get-regexp", r"^remote\."))


# =================================================================================
# Fixture'y
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
def origin(repo: Path, tmp_path: Path) -> Path:
    """Zdalne repozytorium `origin` (bare) z kopia historii; push by je zmienil."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "fetch", "-q", "origin")
    return bare


@pytest.fixture
def bez_tozsamosci(repo: Path) -> None:
    """Repozytorium bez `user.name`/`user.email`; config globalny jest pusty (izolacja w `repo`),
    a `useConfigOnly` wylacza zgadywanie tozsamosci z nazwy uzytkownika i hosta."""
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "user.useConfigOnly", "true")


@pytest.fixture
def git_pod_obserwacja(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    prawdziwy = subprocess.run
    wywolania: list[list[str]] = []

    def run(argv, *a, **kw):
        wywolania.append([str(x) for x in argv])
        return prawdziwy(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    return wywolania


@pytest.fixture(autouse=True)
def _czysta_szyna():
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


@pytest.fixture
def wysylka_do_prawdziwych_operacji(monkeypatch):
    return podmien_wysylke_na_prawdziwe_operacje(monkeypatch)


@pytest.fixture
def publikacje(monkeypatch):
    return podmien_publikacje_na_szyne(monkeypatch)


def zintegruj(wt) -> list[str]:
    zmiany_runu(wt)
    return integruj(wt.sciezka, wt.base_commit)


def dodaj_hook_odrzucajacy_commit(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'hook odrzucil commit' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


# =================================================================================
# Runner: commituj - skutek w historii repozytorium uzytkownika
# =================================================================================

def test_commit_po_integracji_dodaje_dokladnie_jeden_wpis_w_reflogu_i_zwraca_sha_head(repo, wt):
    pliki = zintegruj(wt)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    sha = commituj(wt.sciezka, pliki, OPIS)

    tematy_po = tematy_reflogu(repo)
    assert len(tematy_po) == len(tematy_przed) + 1
    assert tematy_po[1:] == tematy_przed
    assert tematy_po[0].startswith("commit")
    assert SHA_PELNY.fullmatch(sha)
    assert sha == head(repo)
    assert sha != head_przed
    assert git(repo, "rev-parse", "HEAD^").strip() == head_przed


def test_commit_zawiera_dokladnie_pliki_runu_a_drzewo_robocze_jest_po_nim_czyste(repo, wt):
    pliki = zintegruj(wt)

    commituj(wt.sciezka, pliki, OPIS)

    assert zmienione_w_head(repo) == (
        "M\ta.txt\nD\tb.txt\nA\tnowy.txt\nA\tpodkatalog/nowy2.txt\n")
    assert status(repo) == ""
    assert (repo / "a.txt").read_text(encoding="utf-8") == A_PO_RUNIE


def test_commit_ma_opis_z_zadania_jako_temat_i_pusta_tresc(repo, wt):
    pliki = zintegruj(wt)

    commituj(wt.sciezka, pliki, OPIS)

    assert git(repo, "log", "-1", "--format=%s").strip() == OPIS
    assert git(repo, "log", "-1", "--format=%b").strip() == ""


def test_autorem_commita_jest_tozsamosc_z_konfiguracji_gita_repozytorium_uzytkownika(repo, wt):
    pliki = zintegruj(wt)

    commituj(wt.sciezka, pliki, OPIS)

    nazwa, adres = TOZSAMOSC_REPO
    assert git(repo, "log", "-1", "--format=%an%n%ae%n%cn%n%ce").splitlines() == [
        nazwa, adres, nazwa, adres]


def test_commit_nie_zabiera_zmian_ktore_uzytkownik_juz_dodal_do_indeksu(repo, wt):
    pliki = zintegruj(wt)
    zapisz(repo, "zlecone.txt", "staged przez uzytkownika\n")
    zmien_linie(repo, "c.txt", 3, "c.txt staged zmiana")
    git(repo, "add", "zlecone.txt", "c.txt")

    commituj(wt.sciezka, pliki, OPIS)

    assert "zlecone.txt" not in zmienione_w_head(repo)
    assert "c.txt" not in zmienione_w_head(repo)
    assert git(repo, "diff", "--cached", "--name-only") == "c.txt\nzlecone.txt\n"


def test_commit_nie_zabiera_niezaindeksowanych_zmian_w_plikach_spoza_runu(repo, wt):
    pliki = zintegruj(wt)
    zmien_linie(repo, "c.txt", 3, "c.txt lokalna zmiana")

    commituj(wt.sciezka, pliki, OPIS)

    assert "c.txt" not in zmienione_w_head(repo)
    assert status(repo) == " M c.txt\n"


def test_nazwa_pliku_runu_ze_znakami_wzorca_nie_zabiera_cudzych_plikow(repo, wt):
    """`[ab].txt` jako wzorzec pasowalby do `a.txt`: commit ma zawierac tylko plik o tej nazwie."""
    zapisz(wt.sciezka, "[ab].txt", "plik o nazwie ze znakami wzorca\n")
    pliki = integruj(wt.sciezka, wt.base_commit)
    zmien_linie(repo, "a.txt", 2, "a.txt lokalna zmiana uzytkownika")

    commituj(wt.sciezka, pliki, OPIS)

    assert pliki == ["[ab].txt"]
    assert zmienione_w_head(repo) == "A\t[ab].txt\n"
    assert status(repo) == " M a.txt\n"


def test_commit_nie_zmienia_plikow_roboczych_uzytkownika_ani_worktree_runu(repo, wt):
    pliki = zintegruj(wt)
    zmien_linie(repo, "c.txt", 3, "c.txt lokalna zmiana")
    przed = (migawka(repo), migawka(wt.sciezka))

    commituj(wt.sciezka, pliki, OPIS)

    assert (migawka(repo), migawka(wt.sciezka)) == przed


def test_commit_bez_wczesniejszej_integracji_to_blad_odbioru_bez_nowego_commita(repo, wt):
    zmiany_runu(wt)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    with pytest.raises(BladOdbioru) as blad:
        commituj(wt.sciezka, OCZEKIWANE_PLIKI, OPIS)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wykonal commita"
    assert head(repo) == head_przed
    assert tematy_reflogu(repo) == tematy_przed


def test_brak_tozsamosci_w_konfiguracji_gita_to_blad_odbioru_i_brak_nowego_commita(
    repo, wt, bez_tozsamosci,
):
    pliki = zintegruj(wt)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    with pytest.raises(BladOdbioru) as blad:
        commituj(wt.sciezka, pliki, OPIS)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wykonal commita"
    assert head(repo) == head_przed
    assert tematy_reflogu(repo) == tematy_przed


def test_hook_gita_uzytkownika_odrzucajacy_commit_to_blad_odbioru_bez_nowego_commita(repo, wt):
    """Commit nie omija hookow uzytkownika (bez `--no-verify`)."""
    pliki = zintegruj(wt)
    dodaj_hook_odrzucajacy_commit(repo)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    with pytest.raises(BladOdbioru) as blad:
        commituj(wt.sciezka, pliki, OPIS)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wykonal commita"
    assert head(repo) == head_przed
    assert tematy_reflogu(repo) == tematy_przed


def test_zdalne_repozytorium_zostaje_bez_zmian_po_integracji_i_commicie(repo, wt, origin):
    przed = stan_zdalny(repo, origin)
    assert przed[0] and przed[1]

    pliki = zintegruj(wt)
    commituj(wt.sciezka, pliki, OPIS)

    assert stan_zdalny(repo, origin) == przed


def test_commit_uzywa_tylko_add_i_commit_w_repozytorium_uzytkownika_bez_push_i_obejsc(
    repo, wt, git_pod_obserwacja,
):
    pliki = zintegruj(wt)
    git_pod_obserwacja.clear()

    commituj(wt.sciezka, pliki, OPIS)

    wywolania = [a for a in git_pod_obserwacja if a and a[0] == "git"]
    podkomendy = {podkomenda(a) for a in wywolania}
    niedozwolone = {"push", "fetch", "pull", "reset", "checkout", "switch", "stash", "merge",
                    "rebase", "cherry-pick", "restore", "rm", "clean", "tag", "branch", "am",
                    "remote", "worktree", "apply"}
    assert podkomendy & niedozwolone == set()
    assert "commit" in podkomendy
    komendy_commit = [a for a in wywolania if podkomenda(a) == "commit"]
    assert len(komendy_commit) == 1
    argv = komendy_commit[0]
    assert argv[argv.index("-m") + 1] == OPIS
    for zabronione in ("--no-verify", "-n", "--amend", "--allow-empty", "-a", "--all",
                       "--no-gpg-sign"):
        assert zabronione not in argv
    assert not [t for t in argv if t.startswith(("user.", "--author", "--date"))]
    polecenia_add = [a for a in wywolania if podkomenda(a) == "add"]
    assert polecenia_add
    assert wywolania.index(polecenia_add[0]) < wywolania.index(argv)


# =================================================================================
# Operacja runnera `commit`
# =================================================================================

async def _op(run_id: str, base_commit: str, **dodatkowe) -> dict:
    return await asyncio.wait_for(operacje.wykonaj(
        {"type": "commit", "req_id": "r1", "run_id": run_id, "base_commit": base_commit,
         **dodatkowe}), 60)


async def test_operacja_commit_zwraca_sha_nowego_commita_i_liczy_pliki_runu_sama(
    repo, wt, runy_w_srodowisku,
):
    zintegruj(wt)

    odp = await _op(RUN_ID, wt.base_commit, opis=OPIS)

    assert odp == {"req_id": "r1", "ok": True,
                   "wynik": {"sha": head(repo), "pliki": OCZEKIWANE_PLIKI}}
    assert zmienione_w_head(repo) == (
        "M\ta.txt\nD\tb.txt\nA\tnowy.txt\nA\tpodkatalog/nowy2.txt\n")


async def test_operacja_commit_ignoruje_liste_plikow_i_sciezki_przyslane_przez_kokpit(
    repo, wt, runy_w_srodowisku, tmp_path,
):
    zintegruj(wt)
    zmien_linie(repo, "c.txt", 3, "c.txt lokalna zmiana")

    odp = await _op(RUN_ID, wt.base_commit, opis=OPIS, pliki=["c.txt"],
                    repo=str(tmp_path), repozytorium=str(tmp_path), sciezka=str(tmp_path))

    assert odp["ok"] is True
    assert odp["wynik"]["pliki"] == OCZEKIWANE_PLIKI
    assert "c.txt" not in zmienione_w_head(repo)
    assert status(repo) == " M c.txt\n"


@pytest.mark.parametrize(("dodatkowe", "komunikat"), [
    ({}, "pole opis musi byc napisem"),
    ({"opis": ""}, "opis commita jest pusty albo zawiera znak NUL"),
    ({"opis": None}, "pole opis musi byc napisem"),
    ({"opis": 5}, "pole opis musi byc napisem"),
    ({"opis": ["cockpit: run x"]}, "pole opis musi byc napisem"),
    ({"opis": {"temat": "x"}}, "pole opis musi byc napisem"),
], ids=["bez_opisu", "pusty", "null", "liczba", "lista", "slownik"])
async def test_operacja_commit_odrzuca_brakujacy_pusty_i_zle_typem_opis_bez_commita(
    repo, wt, runy_w_srodowisku, dodatkowe, komunikat,
):
    zintegruj(wt)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    odp = await _op(RUN_ID, wt.base_commit, **dodatkowe)

    assert odp == {"req_id": "r1", "ok": False, "kod": "zle_dane", "blad": komunikat}
    assert head(repo) == head_przed
    assert tematy_reflogu(repo) == tematy_przed


async def test_operacja_commit_dla_runu_bez_zmian_daje_kod_brak_zmian(
    repo, wt, runy_w_srodowisku,
):
    tematy_przed = tematy_reflogu(repo)

    odp = await _op(RUN_ID, wt.base_commit, opis=OPIS)

    assert odp == {"req_id": "r1", "ok": False, "kod": "brak_zmian",
                   "blad": "run nie ma zmian do zapisania"}
    assert tematy_reflogu(repo) == tematy_przed


async def test_operacja_commit_bez_tozsamosci_gita_daje_kod_blad_gita_bez_stderr(
    repo, wt, runy_w_srodowisku, bez_tozsamosci,
):
    zintegruj(wt)
    head_przed = head(repo)

    odp = await _op(RUN_ID, wt.base_commit, opis=OPIS)

    assert odp == {"req_id": "r1", "ok": False, "kod": "blad_gita",
                   "blad": "git nie wykonal commita"}
    assert head(repo) == head_przed


async def test_operacja_commit_bez_wczesniejszej_integracji_daje_kod_blad_gita(
    repo, wt, runy_w_srodowisku,
):
    zmiany_runu(wt)
    head_przed = head(repo)

    odp = await _op(RUN_ID, wt.base_commit, opis=OPIS)

    assert odp == {"req_id": "r1", "ok": False, "kod": "blad_gita",
                   "blad": "git nie wykonal commita"}
    assert head(repo) == head_przed


async def test_operacja_commit_odrzucona_przez_hook_daje_kod_blad_gita(
    repo, wt, runy_w_srodowisku,
):
    zintegruj(wt)
    dodaj_hook_odrzucajacy_commit(repo)
    head_przed = head(repo)

    odp = await _op(RUN_ID, wt.base_commit, opis=OPIS)

    assert odp == {"req_id": "r1", "ok": False, "kod": "blad_gita",
                   "blad": "git nie wykonal commita"}
    assert head(repo) == head_przed


# =================================================================================
# Serwis kokpitu: commituj_run
# =================================================================================

async def zasiej_integracje(swiat, run_id: str, tenant_id: str | None = None, seq: int = 1,
                            user=None) -> None:
    user = user or swiat.wlasciciel
    await zasiej_zdarzenie(
        swiat.maker, tenant_id or swiat.tenant_b, run_id, "integrated", seq,
        json.dumps({"user_id": user.id, "email": user.email}))


async def wywolaj_commit(swiat, run_id: str, wyslij, tenant_id=None, user_id=None,
                         polaczone=(B1, B2)):
    async with swiat.maker() as s:
        return await asyncio.wait_for(
            odbior.commituj_run(
                s, run_id, tenant_id or swiat.tenant_b, user_id or swiat.wlasciciel.id,
                frozenset(polaczone), wyslij), 10)


async def test_commit_runu_bez_wczesniejszej_integracji_daje_stan_niemozliwy_bez_pytania_runnera(
    swiat,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "zmiany runu nie zostaly jeszcze zintegrowane"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id) == []


async def test_commit_po_integracji_pyta_runner_tego_runu_o_commit_z_opisem_zbudowanym_przez_kokpit(
    swiat, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 7.5)
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B2, base_commit="c" * 40)
    await zasiej_integracje(swiat, run_id)
    wyslij = Wyslij()

    await wywolaj_commit(swiat, run_id, wyslij)

    assert wyslij.wywolania == [
        (B2, "commit",
         {"run_id": run_id, "base_commit": "c" * 40, "opis": f"cockpit: run {run_id}"}, 7.5)]


async def test_commit_zapisuje_jedno_zdarzenie_committed_z_uzytkownikiem_i_sha(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id, seq=4)
    wyslij = Wyslij(commit={"ok": True, "wynik": {"sha": "e" * 40, "pliki": ["a.txt"]}})

    zwrocone = await wywolaj_commit(swiat, run_id, wyslij, user_id=swiat.czlonek.id)

    zapisane = await zdarzenia(swiat.maker, run_id, "committed")
    assert len(zapisane) == 1
    ev = zapisane[0]
    assert (ev.tenant_id, ev.run_id, ev.seq) == (swiat.tenant_b, run_id, 5)
    assert json.loads(ev.text) == {"user_id": swiat.czlonek.id, "email": swiat.czlonek.email,
                                   "sha": "e" * 40}
    assert ev.at is not None
    assert zwrocone.zdarzenie.kind == "committed"
    assert zwrocone.zdarzenie.seq == 5
    assert json.loads(zwrocone.zdarzenie.text)["sha"] == "e" * 40
    assert zwrocone.sha == "e" * 40


async def test_commit_nie_zmienia_statusu_runu_ani_nie_usuwa_zdarzenia_integracji(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)

    await wywolaj_commit(swiat, run_id, Wyslij())

    assert (await run_w_bazie(swiat.maker, run_id)).status == RunStatus.to_accept
    assert len(await zdarzenia(swiat.maker, run_id, "integrated")) == 1


async def test_drugi_commit_tego_samego_runu_daje_stan_niemozliwy_i_jedno_zdarzenie(swiat):
    """Runner odpowiada sukcesem takze za drugim razem: tylko serwis moze odmowic."""
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)
    wyslij = Wyslij()
    await wywolaj_commit(swiat, run_id, wyslij)

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "zmiany runu zostaly juz zapisane w historii repozytorium"
    assert len(wyslij.wywolania) == 1
    assert len(await zdarzenia(swiat.maker, run_id, "committed")) == 1


@pytest.mark.parametrize("przeszkadzajace", ["inny_run", "inny_zespol", "inny_rodzaj"])
async def test_integracja_innego_runu_zespolu_albo_rodzaju_nie_odblokowuje_commita(
    swiat, przeszkadzajace,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    if przeszkadzajace == "inny_run":
        inny = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
        await zasiej_integracje(swiat, inny)
    elif przeszkadzajace == "inny_zespol":
        await zasiej_integracje(swiat, run_id, tenant_id=swiat.tenant_p, seq=50)
    else:
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, run_id, "decision", 1)
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, run_id, "output", 2)
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "zmiany runu nie zostaly jeszcze zintegrowane"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


@pytest.mark.parametrize("przeszkadzajace", ["inny_run", "inny_zespol"])
async def test_commit_innego_runu_albo_zespolu_nie_blokuje_commita_tego_runu(
    swiat, przeszkadzajace,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)
    if przeszkadzajace == "inny_run":
        inny = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_b, inny, "committed", 1)
    else:
        await zasiej_zdarzenie(swiat.maker, swiat.tenant_p, run_id, "committed", 50)
    wyslij = Wyslij()

    zwrocone = await wywolaj_commit(swiat, run_id, wyslij)

    assert zwrocone.zdarzenie.kind == "committed"
    assert len(wyslij.wywolania) == 1


@pytest.mark.parametrize("status_runu", [s for s in RunStatus if s != RunStatus.to_accept],
                         ids=lambda s: s.value)
async def test_commit_runu_ktory_nie_jest_do_odbioru_daje_stan_niemozliwy_bez_zdarzenia(
    swiat, status_runu,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1, status_runu)
    await zasiej_integracje(swiat, run_id)
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == komunikat_dla_statusu(status_runu)
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


async def test_commit_runu_innego_zespolu_to_nie_znaleziono_mimo_zdarzenia_integracji(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_p, OBCY)
    await zasiej_integracje(swiat, run_id, tenant_id=swiat.tenant_p)
    wyslij = Wyslij()

    with pytest.raises(NieZnaleziono) as blad:
        await wywolaj_commit(swiat, run_id, wyslij, polaczone=(B1, B2, OBCY))

    assert str(blad.value) == "run nieznany"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


async def test_commit_gdy_runner_runu_jest_offline_to_runner_offline_bez_zdarzenia(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B2)
    await zasiej_integracje(swiat, run_id)
    wyslij = Wyslij()

    with pytest.raises(RunnerOffline) as blad:
        await wywolaj_commit(swiat, run_id, wyslij, polaczone=(B1,))

    assert str(blad.value) == "runner runu jest offline"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


@pytest.mark.parametrize("cialo", [
    {"ok": True},
    {"ok": True, "wynik": None},
    {"ok": True, "wynik": {}},
    {"ok": True, "wynik": {"pliki": ["a.txt"]}},
    {"ok": True, "wynik": {"sha": None}},
    {"ok": True, "wynik": {"sha": 5}},
    {"ok": True, "wynik": {"sha": "abc123"}},
    {"ok": True, "wynik": {"sha": "A" * 40}},
    {"ok": True, "wynik": {"sha": "a" * 39}},
    {"ok": True, "wynik": {"sha": "a" * 41}},
    {"ok": True, "wynik": {"sha": "a" * 40 + "zz"}},
    {"ok": True, "wynik": {"sha": "zz" + "a" * 40}},
    {"ok": True, "wynik": {"sha": "a" * 40 + "\n"}},
    {"ok": True, "wynik": "a" * 40},
    {"ok": True, "wynik": ["a" * 40]},
    {"ok": True, "wynik": {"sha": "g" * 40}},
    {"ok": True, "wynik": {"sha": "z" * 40}},
], ids=["bez_wyniku", "wynik_null", "wynik_pusty", "bez_klucza_sha", "sha_null", "sha_liczba",
        "sha_krotki", "sha_wielkie_litery", "sha_39", "sha_41", "sha_sufiks", "sha_prefiks",
        "sha_nowa_linia", "wynik_napis", "wynik_lista", "sha_litera_g", "sha_litera_z"])
async def test_niepoprawny_wynik_commit_od_runnera_to_stan_niemozliwy_bez_zdarzenia(swiat, cialo):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, Wyslij(commit=cialo))

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "runner zwrocil zly wynik commit"
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


async def test_blad_zgloszony_przez_runner_przy_commicie_trafia_do_wolajacego_bez_zdarzenia(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)
    komunikat = "git nie wykonal commita"
    wyslij = Wyslij(commit={"ok": False, "blad": komunikat, "kod": "blad_gita"})

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, wyslij)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == komunikat
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


async def test_commit_blokuje_wiersz_runu_przez_for_update_przed_pytaniem_runnera(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)
    dziennik: list = []
    wyslij = Wyslij(dziennik=dziennik)

    sql = await _zapytania_select_dla_postgresa(
        swiat, lambda: wywolaj_commit(swiat, run_id, wyslij), dziennik)

    assert any(_blokada_wiersza_runu(q) for q in sql), f"brak FOR UPDATE na run: {sql}"
    pierwsza_blokada = next(i for i, (rodzaj, tresc) in enumerate(dziennik)
                            if rodzaj == "sql" and _blokada_wiersza_runu(tresc))
    assert pierwsza_blokada < dziennik.index(("runner", "commit"))


# =================================================================================
# Trasa POST /api/runs/{id}/commit
# =================================================================================

async def _commit(klient, run_id: str):
    return await asyncio.wait_for(klient.post(f"/api/runs/{run_id}/commit"), 60)


async def _integrate(klient, run_id: str):
    return await asyncio.wait_for(klient.post(f"/api/runs/{run_id}/integrate"), 60)


async def run_po_integracji(swiat, wt) -> None:
    """Run do odbioru, runner online i zintegrowane zmiany (przez trase integrate)."""
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    bus.mark_runner_connected(B1)
    r = await _integrate(swiat.klient, RUN_ID)
    assert r.status_code == 200, r.text


async def test_commit_przed_integrate_daje_409_i_nie_tworzy_commita_w_repozytorium(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    bus.mark_runner_connected(B1)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "zmiany runu nie zostaly jeszcze zintegrowane"
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed
    assert tematy_reflogu(repo) == tematy_przed


async def test_commit_po_integrate_daje_200_i_jeden_nowy_commit_o_sha_zapisanym_w_historii_runu(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 200, r.text
    cialo = r.json()
    assert set(cialo) == {"run_id", "at", "sha"}
    assert cialo["run_id"] == RUN_ID
    assert cialo["sha"] == head(repo)
    assert SHA_PELNY.fullmatch(cialo["sha"])
    tematy_po = tematy_reflogu(repo)
    assert len(tematy_po) == len(tematy_przed) + 1
    assert tematy_po[1:] == tematy_przed
    assert git(repo, "rev-parse", "HEAD^").strip() == head_przed
    assert zmienione_w_head(repo) == (
        "M\ta.txt\nD\tb.txt\nA\tnowy.txt\nA\tpodkatalog/nowy2.txt\n")
    assert git(repo, "log", "-1", "--format=%s").strip() == f"cockpit: run {RUN_ID}"
    assert status(repo) == ""
    assert [typ for _, typ, _ in wysylka_do_prawdziwych_operacji] == ["integrate", "commit"]


async def test_commit_zapisuje_w_historii_runu_kto_kiedy_i_sha(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)

    r = await _commit(swiat.klient, RUN_ID)

    zapisane = await zdarzenia(swiat.maker, RUN_ID, "committed")
    assert len(zapisane) == 1
    ev = zapisane[0]
    assert ev.tenant_id == swiat.tenant_b
    assert json.loads(ev.text) == {"user_id": swiat.wlasciciel.id,
                                   "email": "browarek@example.com", "sha": head(repo)}
    assert ev.at is not None
    assert (await zdarzenia(swiat.maker, RUN_ID))[-1].id == ev.id
    assert r.json()["sha"] == head(repo)


async def test_commit_przez_czlonka_zapisuje_jego_adres_a_autorem_commita_jest_tozsamosc_gita(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)
    klient = await swiat.nowy_klient(swiat.czlonek)

    r = await _commit(klient, RUN_ID)

    assert r.status_code == 200, r.text
    ev = (await zdarzenia(swiat.maker, RUN_ID, "committed"))[0]
    assert json.loads(ev.text)["email"] == "czlonek@browarek.example"
    assert json.loads(ev.text)["user_id"] == swiat.czlonek.id
    assert git(repo, "log", "-1", "--format=%ae").strip() == TOZSAMOSC_REPO[1]


async def test_commit_publikuje_zdarzenie_committed_na_szynie_po_sukcesie(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji, publikacje,
):
    await run_po_integracji(swiat, wt)
    publikacje.clear()

    await _commit(swiat.klient, RUN_ID)

    ev = (await zdarzenia(swiat.maker, RUN_ID, "committed"))[0]
    assert len(publikacje) == 1
    run_id, zdarzenie = publikacje[0]
    assert run_id == RUN_ID
    assert zdarzenie == {"seq": ev.seq, "kind": "committed", "text": ev.text, "step_id": "",
                         "text_truncated": False, "text_full_len": len(ev.text)}


async def test_commit_nie_zmienia_statusu_runu(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)

    await _commit(swiat.klient, RUN_ID)

    assert (await run_w_bazie(swiat.maker, RUN_ID)).status == RunStatus.to_accept


async def test_drugi_commit_tego_samego_runu_daje_409_jedno_zdarzenie_i_jeden_commit(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji, publikacje,
):
    await run_po_integracji(swiat, wt)
    await _commit(swiat.klient, RUN_ID)
    tematy_po_pierwszym, head_po_pierwszym = tematy_reflogu(repo), head(repo)
    publikacje.clear()

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "zmiany runu zostaly juz zapisane w historii repozytorium"
    assert len(await zdarzenia(swiat.maker, RUN_ID, "committed")) == 1
    assert head(repo) == head_po_pierwszym
    assert tematy_reflogu(repo) == tematy_po_pierwszym
    assert [typ for _, typ, _ in wysylka_do_prawdziwych_operacji] == ["integrate", "commit"]
    assert publikacje == []


async def test_commit_po_nieudanym_integrate_z_konfliktem_daje_409_bez_commita(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    zmien_linie(repo, "a.txt", 5, "a.txt linia 5 lokalna")
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    bus.mark_runner_connected(B1)
    head_przed = head(repo)

    integracja = await _integrate(swiat.klient, RUN_ID)
    r = await _commit(swiat.klient, RUN_ID)

    assert integracja.status_code == 409
    assert integracja.json()["detail"] == "zmiany runu koliduja z lokalnymi zmianami: a.txt"
    assert r.status_code == 409
    assert r.json()["detail"] == "zmiany runu nie zostaly jeszcze zintegrowane"
    assert head(repo) == head_przed
    assert [typ for _, typ, _ in wysylka_do_prawdziwych_operacji] == ["integrate"]


async def test_commit_bez_tozsamosci_gita_daje_409_bez_zdarzenia_a_po_ustawieniu_tozsamosci_dziala(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)
    head_przed = head(repo)
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "user.useConfigOnly", "true")

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "git nie wykonal commita"
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed

    git(repo, "config", "user.name", TOZSAMOSC_REPO[0])
    git(repo, "config", "user.email", TOZSAMOSC_REPO[1])
    ponowiony = await _commit(swiat.klient, RUN_ID)

    assert ponowiony.status_code == 200, ponowiony.text
    assert len(await zdarzenia(swiat.maker, RUN_ID, "committed")) == 1


async def test_commit_odrzucony_przez_hook_uzytkownika_daje_409_bez_zdarzenia(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)
    dodaj_hook_odrzucajacy_commit(repo)
    head_przed = head(repo)

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "git nie wykonal commita"
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed


async def _run_po_integracji_w_bazie(swiat, wt, status_runu=RunStatus.to_accept, runner=B1,
                                     tenant=None) -> None:
    """Run z zapisanym w historii zdarzeniem `integrated` (bez prawdziwej integracji)."""
    tenant_id = tenant or swiat.tenant_b
    await zasiej_run(swiat.maker, tenant_id, runner, status_runu,
                     base_commit=wt.base_commit, run_id=RUN_ID)
    await zasiej_integracje(swiat, RUN_ID, tenant_id=tenant_id)


async def test_obserwator_nie_commituje_runu_a_repozytorium_i_baza_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _run_po_integracji_w_bazie(swiat, wt)
    bus.mark_runner_connected(B1)
    klient = await swiat.nowy_klient(swiat.obserwator)
    head_przed = head(repo)

    r = await _commit(klient, RUN_ID)

    assert r.status_code == 403
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed


async def test_commit_bez_sesji_daje_401_a_repozytorium_i_baza_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _run_po_integracji_w_bazie(swiat, wt)
    bus.mark_runner_connected(B1)
    head_przed = head(repo)

    r = await _commit(swiat.anonim, RUN_ID)

    assert r.status_code == 401
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed


async def test_commit_bez_tokenu_csrf_daje_403_a_repozytorium_i_baza_zostaja_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _run_po_integracji_w_bazie(swiat, wt)
    bus.mark_runner_connected(B1)
    klient = await swiat.nowy_klient(swiat.wlasciciel)
    del klient.headers[NAGLOWEK_CSRF]
    head_przed = head(repo)

    r = await _commit(klient, RUN_ID)

    assert r.status_code == 403
    assert r.json() == {"detail": "brak tokenu CSRF"}
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed


async def test_commit_runu_innego_zespolu_daje_404_a_repozytorium_zostaje_bez_zmian(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _run_po_integracji_w_bazie(swiat, wt, runner=OBCY, tenant=swiat.tenant_p)
    bus.mark_runner_connected(OBCY)
    head_przed = head(repo)

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 404
    assert wysylka_do_prawdziwych_operacji == []
    assert await zdarzenia(swiat.maker, RUN_ID, "committed") == []
    assert head(repo) == head_przed


async def test_commit_gdy_runner_runu_jest_offline_daje_409(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _run_po_integracji_w_bazie(swiat, wt)       # strumienia runnera brak

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "runner runu jest offline"
    assert wysylka_do_prawdziwych_operacji == []


async def test_commit_runu_zakonczonego_bledem_daje_409_mimo_zdarzenia_integracji(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    zmiany_runu(wt)
    await _run_po_integracji_w_bazie(swiat, wt, RunStatus.failed)
    bus.mark_runner_connected(B1)
    head_przed = head(repo)

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "run nie jest do odbioru"
    assert wysylka_do_prawdziwych_operacji == []
    assert head(repo) == head_przed


async def test_zmiana_statusu_runu_po_integracji_blokuje_commit(
    swiat, repo, wt, wysylka_do_prawdziwych_operacji,
):
    await run_po_integracji(swiat, wt)
    async with swiat.maker() as s:
        await s.execute(update(Run).where(Run.id == RUN_ID).values(status=RunStatus.rejected))
        await s.commit()
    head_przed = head(repo)

    r = await _commit(swiat.klient, RUN_ID)

    assert r.status_code == 409
    assert r.json()["detail"] == "run nie jest do odbioru"
    assert head(repo) == head_przed


async def test_zdalne_repozytorium_zostaje_bez_zmian_po_calym_odbiorze_runu(
    swiat, repo, wt, origin, wysylka_do_prawdziwych_operacji,
):
    """Odbior diffu, integrate, commit i ponowne proby: push nie istnieje w zadnej sciezce."""
    przed = stan_zdalny(repo, origin)
    assert przed[0] and przed[1]
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    bus.mark_runner_connected(B1)

    diff = await asyncio.wait_for(swiat.klient.get(f"/api/runs/{RUN_ID}/diff"), 60)
    integracja = await _integrate(swiat.klient, RUN_ID)
    commit = await _commit(swiat.klient, RUN_ID)
    powtorny = await _commit(swiat.klient, RUN_ID)

    assert (diff.status_code, integracja.status_code, commit.status_code,
            powtorny.status_code) == (200, 200, 200, 409)
    assert stan_zdalny(repo, origin) == przed
    assert git(repo, "rev-parse", "HEAD").strip() != git(origin, "rev-parse", "HEAD").strip()


# --- przelot end-to-end: prawdziwa aplikacja + prawdziwy runner ----------------------------

@pytest.fixture
async def runner_zywy(swiat, monkeypatch):
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


async def test_integrate_i_commit_przez_prawdziwego_runnera_daja_jeden_commit_z_plikami_runu(
    swiat, repo, wt, origin, runner_zywy,
):
    zmiany_runu(wt)
    await zasiej_run(swiat.maker, swiat.tenant_b, B1, base_commit=wt.base_commit, run_id=RUN_ID)
    zdalny_przed = stan_zdalny(repo, origin)
    tematy_przed = tematy_reflogu(repo)

    integracja = await _integrate(swiat.klient, RUN_ID)
    po_integracji = tematy_reflogu(repo)
    commit = await _commit(swiat.klient, RUN_ID)

    assert integracja.status_code == 200, integracja.text
    assert po_integracji == tematy_przed
    assert commit.status_code == 200, commit.text
    assert commit.json()["sha"] == head(repo)
    assert len(tematy_reflogu(repo)) == len(tematy_przed) + 1
    assert zmienione_w_head(repo) == (
        "M\ta.txt\nD\tb.txt\nA\tnowy.txt\nA\tpodkatalog/nowy2.txt\n")
    assert stan_zdalny(repo, origin) == zdalny_przed
    assert bus._oczekujace == {}


# =================================================================================
# HAR-179 faza 2: gałęzie runnera i serwisu, które chroni tylko jeden element
# =================================================================================

def test_commit_pustej_listy_plikow_to_brak_zmian_a_cudzy_staged_plik_nie_trafia_do_commita(
    repo, wt,
):
    """Bez strażnika `commit --` bez ścieżek zapisałby wszystko, co użytkownik ma w indeksie."""
    zapisz(repo, "zlecone.txt", "staged przez uzytkownika\n")
    git(repo, "add", "zlecone.txt")
    tematy_przed, head_przed = tematy_reflogu(repo), head(repo)

    with pytest.raises(BrakZmianRunu) as blad:
        commituj(wt.sciezka, [], OPIS)

    assert type(blad.value) is BrakZmianRunu
    assert str(blad.value) == "brak plikow runu do zapisania"
    assert head(repo) == head_przed
    assert tematy_reflogu(repo) == tematy_przed
    assert git(repo, "diff", "--cached", "--name-only") == "zlecone.txt\n"


@pytest.mark.parametrize("opis", ["   ", "\n\t ", "cockpit\0run", "\0"],
                         ids=["spacje", "biale_znaki", "nul_w_srodku", "sam_nul"])
async def test_operacja_commit_odrzuca_opis_z_samych_bialych_znakow_i_z_znakiem_nul(
    repo, wt, runy_w_srodowisku, opis,
):
    zintegruj(wt)
    head_przed = head(repo)

    odp = await _op(RUN_ID, wt.base_commit, opis=opis)

    assert odp == {"req_id": "r1", "ok": False, "kod": "zle_dane",
                   "blad": "opis commita jest pusty albo zawiera znak NUL"}
    assert head(repo) == head_przed


@pytest.mark.parametrize("wynik", [b"\xff\xfe\n", b"abc\n", b"", b"a" * 41 + b"\n",
                                   b"A" * 40 + b"\n", b"z" * 40 + b"\n"],
                         ids=["spoza_ascii", "krotki", "pusty", "za_dlugi", "wielkie_litery",
                              "nie_szesnastkowy"])
def test_nieczytelny_wynik_odczytu_sha_po_commicie_to_blad_odbioru_z_pelnym_komunikatem(
    repo, wt, monkeypatch, wynik,
):
    pliki = zintegruj(wt)
    prawdziwa = modul_odbioru._git_z_kodem

    def podmiana(*a, **kw):
        rzeczywisty = prawdziwa(*a, **kw)
        if a[1] == ["rev-parse", "HEAD"]:
            return wynik, 0
        return rzeczywisty

    monkeypatch.setattr(modul_odbioru, "_git_z_kodem", podmiana)

    with pytest.raises(BladOdbioru) as blad:
        commituj(wt.sciezka, pliki, OPIS)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wykonal commita"


@pytest.mark.parametrize(("polecenie", "wybor"), [
    ("add", lambda argv: podkomenda(argv) == "add"),
    ("odczyt_sha", lambda argv: podkomenda(argv) == "rev-parse" and argv[-1] == "HEAD"),
], ids=["add", "odczyt_sha"])
def test_blad_gita_przy_add_i_przy_odczycie_sha_to_blad_odbioru_z_komunikatem_commita(
    repo, wt, monkeypatch, polecenie, wybor,
):
    """Zaslepiony jest tylko kod wyjscia wybranego polecenia; `_git_z_kodem` jest prawdziwe."""
    pliki = zintegruj(wt)
    wymus_kod_gita(monkeypatch, lambda argv, wejscie: wybor(argv), 128)

    with pytest.raises(BladOdbioru) as blad:
        commituj(wt.sciezka, pliki, OPIS)

    assert type(blad.value) is BladOdbioru
    assert str(blad.value) == "git nie wykonal commita"


def test_ponowienie_commita_po_odrzuceniu_przez_hook_dziala_gdy_run_usuwa_plik(repo, wt):
    """Wersja dodająca `git add` także dla usuniętych plików zostawiłaby po pierwszej próbie w
    indeksie usunięcie `b.txt`; ponowienie próbowałoby `git add` na ścieżce, której nie ma ani w
    drzewie, ani w indeksie."""
    pliki = zintegruj(wt)
    assert "b.txt" in pliki and not (repo / "b.txt").exists()
    dodaj_hook_odrzucajacy_commit(repo)
    with pytest.raises(BladOdbioru) as pierwsza:
        commituj(wt.sciezka, pliki, OPIS)
    assert str(pierwsza.value) == "git nie wykonal commita"
    (repo / ".git" / "hooks" / "pre-commit").unlink()

    sha = commituj(wt.sciezka, pliki, OPIS)

    assert sha == head(repo)
    assert zmienione_w_head(repo) == (
        "M\ta.txt\nD\tb.txt\nA\tnowy.txt\nA\tpodkatalog/nowy2.txt\n")


def test_commit_zawiera_zmieniony_i_nowy_plik_binarny_bajt_w_bajt(repo, runy_w_srodowisku):
    wt2 = run_z_plikami_binarnymi(repo, runy_w_srodowisku)
    pliki = integruj(wt2.sciezka, wt2.base_commit)

    commituj(wt2.sciezka, pliki, OPIS)

    assert zmienione_w_head(repo) == "A\tnowy.bin\nM\tobraz.bin\n"
    for nazwa, oczekiwane in (("obraz.bin", NOWY_OBRAZ), ("nowy.bin", NOWY_PLIK_BINARNY)):
        zapisane = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{nazwa}"],
            check=True, capture_output=True, timeout=60).stdout
        assert zapisane == oczekiwane
    assert status(repo) == ""


def test_commit_obejmuje_wiszace_dowiazanie_symboliczne_dodane_przez_run(repo, wt):
    """Dowiązanie bez celu nie istnieje dla `os.path.exists`, ale jest plikiem runu."""
    os.symlink("nie-ma-takiego-celu", wt.sciezka / "wiszacy")
    pliki = integruj(wt.sciezka, wt.base_commit)
    assert pliki == ["wiszacy"]
    assert os.path.islink(repo / "wiszacy")

    commituj(wt.sciezka, pliki, OPIS)

    assert zmienione_w_head(repo) == "A\twiszacy\n"
    assert status(repo) == ""


@pytest.mark.parametrize("kto", ["uzytkownik_innego_zespolu", "nieistniejacy_uzytkownik"])
async def test_commit_przez_uzytkownika_spoza_zespolu_runu_nie_pyta_runnera_i_nie_zapisuje(
    swiat, kto,
):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)
    user_id = swiat.wlasciciel_p.id if kto == "uzytkownik_innego_zespolu" else "nie-ma-takiego"
    wyslij = Wyslij()

    with pytest.raises(StanNiemozliwy) as blad:
        await wywolaj_commit(swiat, run_id, wyslij, user_id=user_id)

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "uzytkownik nie nalezy do zespolu runu"
    assert wyslij.wywolania == []
    assert await zdarzenia(swiat.maker, run_id, "committed") == []


async def test_wynik_commitu_niesie_sha_i_pusta_liste_plikow_a_wynik_integracji_nie_ma_sha(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)
    inny = await zasiej_run(swiat.maker, swiat.tenant_b, B1)

    po_commicie = await wywolaj_commit(swiat, run_id, Wyslij())
    po_integracji = await wywolaj_integruj(swiat, inny, Wyslij())

    assert po_commicie.sha == "d" * 40
    assert po_commicie.pliki == []
    assert po_integracji.sha is None
    assert po_integracji.pliki == ["a.txt"]


async def test_wynik_odbioru_jest_niezmienny(swiat):
    run_id = await zasiej_run(swiat.maker, swiat.tenant_b, B1)
    await zasiej_integracje(swiat, run_id)

    wynik = await wywolaj_commit(swiat, run_id, Wyslij())

    with pytest.raises(FrozenInstanceError):
        wynik.sha = "e" * 40

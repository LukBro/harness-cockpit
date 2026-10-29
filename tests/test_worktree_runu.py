"""HAR-52: worktree per run poza repozytorium uzytkownika (TDD, przed implementacja)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import runner.worktree as worktree_modul
from cockpit.services.errors import NaruszonyNiezmiennik
from runner.worktree import (
    BladWorktree,
    WorktreeRunu,
    katalog_runu,
    liczba_zmienionych_plikow,
    usun_worktree,
    utworz_worktree,
)

TOZSAMOSC = [
    "-c", "user.name=Test",
    "-c", "user.email=test@example.com",
    "-c", "commit.gpgsign=false",
]


def git(repo: Path, *argumenty: str) -> str:
    return subprocess.run(
        ["git", *TOZSAMOSC, "-C", str(repo), *argumenty],
        check=True, capture_output=True, text=True,
    ).stdout


def zacommituj(repo: Path, komunikat: str) -> None:
    # fixture testowy (nie silnik): commit w repo testowym w tmp_path
    git(repo, "commit", "-q", "-a", "--allow-empty", "-m", komunikat)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo_uzytkownika"
    r.mkdir()
    git(r, "init", "-q")
    (r / "a.txt").write_text("a\n")
    (r / "b.txt").write_text("b\n")
    git(r, "update-index", "--add", "a.txt", "b.txt")
    zacommituj(r, "init")
    return r


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "runy"


def wpisy_worktree(repo: Path) -> list[str]:
    wyjscie = git(repo, "worktree", "list", "--porcelain")
    return [w.removeprefix("worktree ") for w in wyjscie.splitlines() if w.startswith("worktree ")]


def sciezki_worktree(repo: Path) -> set[Path]:
    return {Path(w).resolve() for w in wpisy_worktree(repo)}


def reflog(repo: Path) -> str:
    return git(repo, "reflog", "show", "--all")


def reflog_head(repo: Path) -> str:
    return git(repo, "reflog")


def wpisy_reflogu_runow(repo: Path) -> list[str]:
    # Reflog worktree runu zyje w .git/worktrees/<id>/logs/HEAD, a `--all` pokazuje go
    # jako ref `worktrees/<id>/HEAD`. To metadane runu, kasowane przez usun_worktree.
    return [w for w in reflog(repo).splitlines() if " worktrees/" in w or w.startswith("worktrees/")]


def reflog_uzytkownika(repo: Path) -> str:
    """Reflog repozytorium uzytkownika: `--all` bez wpisow worktree runow."""
    obce = set(wpisy_reflogu_runow(repo))
    return "\n".join(w for w in reflog(repo).splitlines() if w not in obce)


def status(repo: Path) -> str:
    return git(repo, "status", "--porcelain", "--untracked-files=all")


# --- katalog_runu -------------------------------------------------------------


def test_katalog_runu_z_jawnego_roota_to_root_i_identyfikator_runu(tmp_path: Path):
    assert katalog_runu("r1", root=tmp_path / "x") == tmp_path / "x" / "r1"


def test_jawny_root_ma_pierwszenstwo_przed_zmienna_srodowiskowa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "z_env"))

    assert katalog_runu("r1", root=tmp_path / "jawny") == tmp_path / "jawny" / "r1"


def test_zmienna_srodowiskowa_ma_pierwszenstwo_przed_katalogiem_domowym(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "dom"))
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "z_env"))

    assert katalog_runu("r1") == tmp_path / "z_env" / "r1"


def test_zmienna_srodowiskowa_jest_czytana_przy_wywolaniu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "pierwszy"))
    assert katalog_runu("r1") == tmp_path / "pierwszy" / "r1"

    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "drugi"))

    assert katalog_runu("r1") == tmp_path / "drugi" / "r1"


def test_bez_roota_i_zmiennej_katalog_runu_lezy_w_cockpit_runs_w_domu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "dom"))
    monkeypatch.delenv("COCKPIT_RUNS_DIR", raising=False)

    assert katalog_runu("r1") == tmp_path / "dom" / ".cockpit" / "runs" / "r1"


@pytest.mark.parametrize("run_id", ["", ".", "..", "a/b", "a\\b", "../x"])
def test_identyfikator_runu_wychodzacy_poza_baze_jest_odrzucany(
    run_id: str, tmp_path: Path
):
    with pytest.raises(BladWorktree):
        katalog_runu(run_id, root=tmp_path / "runy")


def test_identyfikator_runu_z_kropka_w_srodku_jest_dozwolony(tmp_path: Path):
    assert katalog_runu("run.1", root=tmp_path) == tmp_path / "run.1"


# --- kryterium dfabcc28: zapis w worktree nie brudzi drzewa uzytkownika --------


def test_zapis_w_worktree_runu_nie_zmienia_drzewa_uzytkownika(repo: Path, root: Path):
    wt = utworz_worktree(repo, "r1", root=root)

    (wt.sciezka / "wynik.txt").write_text("z runu\n")
    (wt.sciezka / "a.txt").write_text("zmieniony w runie\n")

    assert status(repo) == ""
    assert (repo / "a.txt").read_text() == "a\n"
    assert not (repo / "wynik.txt").exists()


def test_kontrola_ten_sam_zapis_w_repo_uzytkownika_jest_widoczny_w_status(
    repo: Path, root: Path
):
    utworz_worktree(repo, "r1", root=root)

    (repo / "wynik.txt").write_text("z runu\n")
    (repo / "a.txt").write_text("zmieniony\n")

    zmiany = status(repo)
    assert "wynik.txt" in zmiany
    assert "a.txt" in zmiany


def test_worktree_runu_lezy_w_katalogu_runu_poza_repozytorium(repo: Path, root: Path):
    wt = utworz_worktree(repo, "r1", root=root)

    assert isinstance(wt, WorktreeRunu)
    assert wt.katalog_runu == root / "r1"
    assert wt.sciezka == root / "r1" / "worktree"
    assert wt.sciezka.is_dir()
    assert (wt.sciezka / "a.txt").read_text() == "a\n"
    assert repo.resolve() not in wt.sciezka.resolve().parents
    assert wt.sciezka.resolve() in sciezki_worktree(repo)


# --- kryterium 5b1a9634: dwa runy sa odizolowane --------------------------------


def test_dwa_runy_na_tym_samym_repozytorium_maja_osobne_worktree(repo: Path, root: Path):
    wt1 = utworz_worktree(repo, "r1", root=root)
    wt2 = utworz_worktree(repo, "r2", root=root)

    assert wt1.sciezka != wt2.sciezka
    assert {wt1.sciezka.resolve(), wt2.sciezka.resolve()} <= sciezki_worktree(repo)


def test_runy_nie_widza_nawzajem_swoich_plikow(repo: Path, root: Path):
    wt1 = utworz_worktree(repo, "r1", root=root)
    wt2 = utworz_worktree(repo, "r2", root=root)

    (wt1.sciezka / "tylko_run1.txt").write_text("1\n")
    (wt2.sciezka / "tylko_run2.txt").write_text("2\n")
    (wt1.sciezka / "a.txt").write_text("zmiana run1\n")

    assert not (wt2.sciezka / "tylko_run1.txt").exists()
    assert not (wt1.sciezka / "tylko_run2.txt").exists()
    assert (wt2.sciezka / "a.txt").read_text() == "a\n"
    zmiany_run2 = status(wt2.sciezka)
    assert "tylko_run2.txt" in zmiany_run2
    assert "run1" not in zmiany_run2
    assert "a.txt" not in zmiany_run2


# --- kryterium bc5edf2c: katalog runu w repozytorium -----------------------------


def test_katalog_runu_wewnatrz_repozytorium_to_naruszenie_i_nie_tworzy_worktree(
    repo: Path,
):
    przed = wpisy_worktree(repo)
    root_w_repo = repo / "runy"

    with pytest.raises(NaruszonyNiezmiennik) as blad:
        utworz_worktree(repo, "r1", root=root_w_repo)

    assert blad.value.nazwa == "run_dir_outside_repo"
    assert not (root_w_repo / "r1").exists()
    assert not root_w_repo.exists()
    assert wpisy_worktree(repo) == przed
    assert status(repo) == ""


def test_root_rowny_repozytorium_to_naruszenie_run_dir_outside_repo(repo: Path):
    przed = wpisy_worktree(repo)

    with pytest.raises(NaruszonyNiezmiennik) as blad:
        utworz_worktree(repo, "r1", root=repo)

    assert blad.value.nazwa == "run_dir_outside_repo"
    assert not (repo / "r1").exists()
    assert wpisy_worktree(repo) == przed
    assert status(repo) == ""


def test_root_w_glebokim_podkatalogu_repozytorium_to_naruszenie(repo: Path):
    przed = wpisy_worktree(repo)
    glebokie = repo / "src" / "wewnatrz"
    glebokie.mkdir(parents=True)

    with pytest.raises(NaruszonyNiezmiennik) as blad:
        utworz_worktree(repo, "r1", root=glebokie)

    assert blad.value.nazwa == "run_dir_outside_repo"
    assert not (glebokie / "r1").exists()
    assert wpisy_worktree(repo) == przed
    assert status(repo) == ""


def test_root_bedacy_symlinkiem_do_katalogu_w_repozytorium_to_naruszenie(
    repo: Path, tmp_path: Path
):
    przed = wpisy_worktree(repo)
    cel = repo / "podkatalog"
    cel.mkdir()
    link = tmp_path / "link_do_repo"
    link.symlink_to(cel, target_is_directory=True)

    with pytest.raises(NaruszonyNiezmiennik) as blad:
        utworz_worktree(repo, "r1", root=link)

    assert blad.value.nazwa == "run_dir_outside_repo"
    assert not (cel / "r1").exists()
    assert wpisy_worktree(repo) == przed
    assert status(repo) == ""


def test_katalog_rodzenstwa_o_wspolnym_prefiksie_nazwy_nie_jest_wnetrzem_repozytorium(
    repo: Path,
):
    # "repo_uzytkownika_runy" nie lezy w "repo_uzytkownika" mimo wspolnego prefiksu
    root_obok = repo.parent / (repo.name + "_runy")

    wt = utworz_worktree(repo, "r1", root=root_obok)

    assert wt.sciezka.is_dir()


# --- kryterium 754ac484: base_commit i brak zapisu gita ---------------------------


def test_base_commit_to_czterdziestoznakowy_sha_head_z_chwili_startu(
    repo: Path, root: Path
):
    head = git(repo, "rev-parse", "HEAD").strip()

    wt = utworz_worktree(repo, "r1", root=root)

    assert wt.base_commit == head
    assert re.fullmatch(r"[0-9a-f]{40}", wt.base_commit)
    assert git(wt.sciezka, "rev-parse", "HEAD").strip() == head


def test_base_commit_to_sha_a_nie_nazwa_galezi(repo: Path, root: Path):
    git(repo, "checkout", "-q", "-b", "funkcja-x")

    wt = utworz_worktree(repo, "r1", root=root)

    assert wt.base_commit != "funkcja-x"
    assert re.fullmatch(r"[0-9a-f]{40}", wt.base_commit)
    # worktree jest odlaczony (detached), nie zajmuje galezi uzytkownika
    assert git(wt.sciezka, "branch", "--show-current").strip() == ""
    assert git(repo, "branch", "--show-current").strip() == "funkcja-x"


def test_commit_w_repo_po_utworzeniu_worktree_nie_zmienia_base_commit_ani_head_worktree(
    repo: Path, root: Path
):
    head_przed = git(repo, "rev-parse", "HEAD").strip()
    wt = utworz_worktree(repo, "r1", root=root)

    (repo / "nowy.txt").write_text("n\n")
    git(repo, "update-index", "--add", "nowy.txt")
    zacommituj(repo, "po starcie runu")

    assert git(repo, "rev-parse", "HEAD").strip() != head_przed
    assert wt.base_commit == head_przed
    assert git(wt.sciezka, "rev-parse", "HEAD").strip() == head_przed
    assert not (wt.sciezka / "nowy.txt").exists()


def test_silnik_nie_dopisuje_wpisow_do_reflogu_repozytorium_przy_tworzeniu_i_usuwaniu(
    repo: Path, root: Path
):
    przed = reflog(repo)
    assert przed.strip() != ""  # kontrola: fixture ma jakis reflog
    przed_uzytkownika = reflog_uzytkownika(repo)
    przed_head = reflog_head(repo)

    wt = utworz_worktree(repo, "r1", root=root)
    (wt.sciezka / "x.txt").write_text("x\n")

    assert reflog_uzytkownika(repo) == przed_uzytkownika
    assert reflog_head(repo) == przed_head
    # jedyna nowa pozycja w `--all` to reflog worktree runu
    nowe = [w for w in reflog(repo).splitlines() if w not in przed.splitlines()]
    assert nowe != []
    assert nowe == wpisy_reflogu_runow(repo)

    usun_worktree(repo, wt)

    # metadane runu zniknely: pelny reflog bez filtrowania jak przed
    assert reflog(repo) == przed
    assert reflog_head(repo) == przed_head


def test_silnik_nie_tworzy_commitow_ani_galezi_w_repozytorium(repo: Path, root: Path):
    commity_przed = git(repo, "rev-list", "--all", "--count")
    galezie_przed = git(repo, "branch", "--all")

    wt = utworz_worktree(repo, "r1", root=root)
    (wt.sciezka / "x.txt").write_text("x\n")
    usun_worktree(repo, wt)

    assert git(repo, "rev-list", "--all", "--count") == commity_przed
    assert git(repo, "branch", "--all") == galezie_przed


# --- brudne drzewo uzytkownika ----------------------------------------------------


def test_niezacommitowana_zmiana_uzytkownika_nie_trafia_do_worktree_i_zostaje_nietknieta(
    repo: Path, root: Path
):
    (repo / "a.txt").write_text("brudna zmiana\n")
    (repo / "nowy.txt").write_text("nieslediony\n")
    status_przed = status(repo)

    wt = utworz_worktree(repo, "r1", root=root)

    assert (wt.sciezka / "a.txt").read_text() == "a\n"
    assert not (wt.sciezka / "nowy.txt").exists()
    assert (repo / "a.txt").read_text() == "brudna zmiana\n"
    assert (repo / "nowy.txt").read_text() == "nieslediony\n"
    assert status(repo) == status_przed


def test_liczba_zmienionych_plikow_czystego_drzewa_to_zero(repo: Path):
    assert liczba_zmienionych_plikow(repo) == 0


def test_liczba_zmienionych_plikow_liczy_zmienione_i_nieslediowne(repo: Path):
    (repo / "a.txt").write_text("zmiana\n")
    (repo / "nowy.txt").write_text("n\n")

    assert liczba_zmienionych_plikow(repo) == 2


def test_liczba_zmienionych_plikow_liczy_sama_zmiane_sledzonego_pliku(repo: Path):
    (repo / "a.txt").write_text("zmiana\n")

    assert liczba_zmienionych_plikow(repo) == 1


def test_liczba_zmienionych_plikow_liczy_sam_nieslediowny_plik(repo: Path):
    (repo / "nowy.txt").write_text("n\n")

    assert liczba_zmienionych_plikow(repo) == 1


def test_liczba_zmienionych_plikow_nie_liczy_zapisow_w_worktree_runu(
    repo: Path, root: Path
):
    wt = utworz_worktree(repo, "r1", root=root)
    (wt.sciezka / "wynik.txt").write_text("w\n")

    assert liczba_zmienionych_plikow(repo) == 0


def test_liczba_zmienionych_plikow_poza_repozytorium_to_blad_dziedziny(tmp_path: Path):
    nie_repo = tmp_path / "pusty"
    nie_repo.mkdir()

    with pytest.raises(BladWorktree) as blad:
        liczba_zmienionych_plikow(nie_repo)

    assert "fatal" in str(blad.value)


# --- usuwanie ---------------------------------------------------------------------


def test_usuniety_worktree_znika_z_listy_i_z_dysku_a_katalog_runu_zostaje(
    repo: Path, root: Path
):
    wt = utworz_worktree(repo, "r1", root=root)
    (wt.katalog_runu / "log.txt").write_text("artefakt runu\n")
    assert wt.sciezka.resolve() in sciezki_worktree(repo)

    usun_worktree(repo, wt)

    assert wt.sciezka.resolve() not in sciezki_worktree(repo)
    assert not wt.sciezka.exists()
    assert wt.katalog_runu.is_dir()
    assert (wt.katalog_runu / "log.txt").read_text() == "artefakt runu\n"


def test_usuniecie_worktree_z_niezacommitowanymi_zmianami_dziala_i_nie_dotyka_repo(
    repo: Path, root: Path
):
    wt = utworz_worktree(repo, "r1", root=root)
    (wt.sciezka / "wynik.txt").write_text("w\n")
    (wt.sciezka / "a.txt").write_text("zmiana w runie\n")

    usun_worktree(repo, wt)

    assert not wt.sciezka.exists()
    assert not (repo / "wynik.txt").exists()
    assert (repo / "a.txt").read_text() == "a\n"
    assert status(repo) == ""


def test_usuniecie_jednego_runu_nie_usuwa_worktree_drugiego(repo: Path, root: Path):
    wt1 = utworz_worktree(repo, "r1", root=root)
    wt2 = utworz_worktree(repo, "r2", root=root)
    (wt2.sciezka / "wynik.txt").write_text("w\n")

    usun_worktree(repo, wt1)

    assert wt2.sciezka.resolve() in sciezki_worktree(repo)
    assert (wt2.sciezka / "wynik.txt").read_text() == "w\n"


def test_po_usunieciu_worktree_git_nie_trzyma_metadanych_o_nim(repo: Path, root: Path):
    wt = utworz_worktree(repo, "r1", root=root)
    assert any((repo / ".git" / "worktrees").iterdir())  # kontrola: wpis byl

    usun_worktree(repo, wt)

    metadane = repo / ".git" / "worktrees"
    assert not metadane.exists() or not any(metadane.iterdir())


def test_usuniecie_worktree_ktorego_nie_ma_to_blad_dziedziny(repo: Path, root: Path):
    wt = WorktreeRunu(
        sciezka=root / "nieznany" / "worktree",
        base_commit="0" * 40,
        katalog_runu=root / "nieznany",
    )

    with pytest.raises(BladWorktree) as blad:
        usun_worktree(repo, wt)

    assert "fatal" in str(blad.value)


# --- bledy na granicy -------------------------------------------------------------


def test_katalog_ktory_nie_jest_repozytorium_daje_blad_dziedziny_z_komunikatem_gita(
    tmp_path: Path, root: Path
):
    nie_repo = tmp_path / "zwykly_katalog"
    nie_repo.mkdir()

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(nie_repo, "r1", root=root)

    assert "fatal" in str(blad.value)
    assert not (root / "r1").exists()


def test_repozytorium_bez_zadnego_commita_daje_blad_dziedziny_i_nic_nie_powstaje(
    tmp_path: Path, root: Path
):
    puste = tmp_path / "puste_repo"
    puste.mkdir()
    git(puste, "init", "-q")

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(puste, "r1", root=root)

    assert "fatal" in str(blad.value)
    assert not (root / "r1").exists()


def test_powtorzony_identyfikator_runu_daje_blad_z_komunikatem_gita_i_nie_psuje_pierwszego(
    repo: Path, root: Path
):
    pierwszy = utworz_worktree(repo, "r1", root=root)
    (pierwszy.sciezka / "wynik.txt").write_text("w\n")

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(repo, "r1", root=root)

    assert "fatal" in str(blad.value)
    assert (pierwszy.sciezka / "wynik.txt").read_text() == "w\n"
    assert pierwszy.sciezka.resolve() in sciezki_worktree(repo)


def test_brak_binarki_git_daje_blad_dziedziny_a_nie_wyjatek_biblioteki(
    repo: Path, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pusty_path = tmp_path / "pusty_path"
    pusty_path.mkdir()
    monkeypatch.setenv("PATH", str(pusty_path))

    with pytest.raises(BladWorktree):
        utworz_worktree(repo, "r1", root=root)

    monkeypatch.undo()
    assert not (root / "r1").exists()


def test_brak_binarki_git_przy_liczeniu_zmian_i_usuwaniu_to_blad_dziedziny(
    repo: Path, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    wt = utworz_worktree(repo, "r1", root=root)
    pusty_path = tmp_path / "pusty_path"
    pusty_path.mkdir()
    monkeypatch.setenv("PATH", str(pusty_path))

    with pytest.raises(BladWorktree):
        liczba_zmienionych_plikow(repo)
    with pytest.raises(BladWorktree):
        usun_worktree(repo, wt)


def test_nie_mozna_utworzyc_katalogu_runu_bo_root_jest_zwyklym_plikiem(
    repo: Path, tmp_path: Path
):
    root_plik = tmp_path / "root_jest_plikiem"
    root_plik.write_text("to nie katalog\n")
    przed = wpisy_worktree(repo)

    with pytest.raises(BladWorktree):
        utworz_worktree(repo, "r1", root=root_plik)

    assert wpisy_worktree(repo) == przed
    assert root_plik.read_text() == "to nie katalog\n"
    assert status(repo) == ""


def test_niedozwolony_identyfikator_runu_w_utworz_worktree_nic_nie_tworzy(
    repo: Path, root: Path
):
    przed = wpisy_worktree(repo)

    with pytest.raises(BladWorktree):
        utworz_worktree(repo, "../ucieczka", root=root)

    assert not (root.parent / "ucieczka").exists()
    assert wpisy_worktree(repo) == przed


# --- komunikaty bledow niosa przyczyne (zabijanie mutantow komunikatow) -----------


@pytest.mark.parametrize("run_id", ["", ".", "..", "a/b", "a\\b"])
def test_komunikat_o_niedozwolonym_identyfikatorze_runu_podaje_ten_identyfikator(
    run_id: str, tmp_path: Path
):
    with pytest.raises(BladWorktree) as blad:
        katalog_runu(run_id, root=tmp_path / "runy")

    assert repr(run_id) in str(blad.value)


def test_komunikat_o_powtorzonym_runie_podaje_polecenie_gita_ktore_zawiodlo(
    repo: Path, root: Path
):
    utworz_worktree(repo, "r1", root=root)

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(repo, "r1", root=root)

    assert "worktree add --detach" in str(blad.value)


def test_komunikat_o_katalogu_bez_repozytorium_podaje_polecenie_gita_ktore_zawiodlo(
    tmp_path: Path, root: Path
):
    nie_repo = tmp_path / "zwykly_katalog"
    nie_repo.mkdir()

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(nie_repo, "r1", root=root)

    assert "rev-parse HEAD" in str(blad.value)


def test_komunikat_o_braku_binarki_git_podaje_polecenie_i_przyczyne_systemu(
    repo: Path, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pusty_path = tmp_path / "pusty_path"
    pusty_path.mkdir()
    monkeypatch.setenv("PATH", str(pusty_path))

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(repo, "r1", root=root)

    komunikat = str(blad.value)
    assert "rev-parse HEAD" in komunikat
    assert "Errno" in komunikat


def test_komunikat_o_niemozliwym_katalogu_runu_podaje_sciezke_i_przyczyne_systemu(
    repo: Path, tmp_path: Path
):
    root_plik = tmp_path / "root_jest_plikiem"
    root_plik.write_text("to nie katalog\n")

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(repo, "r1", root=root_plik)

    komunikat = str(blad.value)
    assert str(root_plik / "r1") in komunikat
    assert "Errno" in komunikat


def test_naruszenie_run_dir_outside_repo_niesie_szczegol_z_walidatora(repo: Path):
    with pytest.raises(NaruszonyNiezmiennik) as blad:
        utworz_worktree(repo, "r1", root=repo / "runy")

    assert "lezy w repozytorium" in str(blad.value)
    assert str(repo) in str(blad.value)


# --- SHA-256: Run.base_commit to String(40) ---------------------------------------


def test_repozytorium_z_haszami_sha256_jest_odrzucone_bo_base_commit_ma_40_znakow(
    tmp_path: Path, root: Path
):
    r = tmp_path / "repo_sha256"
    r.mkdir()
    git(r, "init", "-q", "--object-format=sha256")
    zacommituj(r, "init")
    hash_head = git(r, "rev-parse", "HEAD").strip()
    assert len(hash_head) == 64  # kontrola: to naprawde repo SHA-256
    przed = wpisy_worktree(r)

    with pytest.raises(BladWorktree) as blad:
        utworz_worktree(r, "r1", root=root)

    assert hash_head in str(blad.value)
    assert not (root / "r1").exists()
    assert wpisy_worktree(r) == przed


# --- liczenie zmian nie zalezy od configu uzytkownika -----------------------------


def test_nieslediowny_katalog_z_dwoma_plikami_liczy_sie_jako_dwa_zmienione_pliki(
    repo: Path,
):
    (repo / "nowy").mkdir()
    (repo / "nowy" / "a.txt").write_text("a\n")
    (repo / "nowy" / "b.txt").write_text("b\n")

    assert liczba_zmienionych_plikow(repo) == 2


def test_config_ukrywajacy_nieslediowne_pliki_nie_ukrywa_ich_przed_ostrzezeniem(
    repo: Path,
):
    git(repo, "config", "status.showUntrackedFiles", "no")
    (repo / "nowy.txt").write_text("n\n")
    assert "nowy.txt" not in git(repo, "status", "--porcelain")  # kontrola: config ukrywa

    assert liczba_zmienionych_plikow(repo) == 1


# --- usuwanie sprzata nieaktualne metadane worktree -------------------------------


def test_usuniecie_runu_sprzata_metadane_worktree_ktorego_katalog_znikl_z_dysku(
    repo: Path, root: Path
):
    wt1 = utworz_worktree(repo, "r1", root=root)
    wt2 = utworz_worktree(repo, "r2", root=root)
    shutil.rmtree(wt2.sciezka)  # tmp_path testu: katalog r2 znika poza gitem
    assert wt2.sciezka.resolve() in sciezki_worktree(repo)  # kontrola: wpis r2 jeszcze jest

    usun_worktree(repo, wt1)

    sciezki = sciezki_worktree(repo)
    assert wt1.sciezka.resolve() not in sciezki
    assert wt2.sciezka.resolve() not in sciezki
    metadane = repo / ".git" / "worktrees"
    assert not metadane.exists() or not any(metadane.iterdir())


# --- base_commit przy ruchu HEAD w trakcie startu (D8) -------------------------------


def test_commit_uzytkownika_miedzy_odczytem_head_a_utworzeniem_worktree_nie_rozjezdza_base_commit(
    repo: Path, root: Path, monkeypatch: pytest.MonkeyPatch
):
    prawdziwy_git = worktree_modul._git
    head_przed = git(repo, "rev-parse", "HEAD").strip()

    def nakladka(repo_arg, *argumenty):
        wynik = prawdziwy_git(repo_arg, *argumenty)
        if argumenty == ("rev-parse", "HEAD"):
            # Uzytkownik commituje, zanim worktree powstanie.
            (repo / "a.txt").write_text("zmiana uzytkownika\n")
            zacommituj(repo, "w trakcie startu runu")
        return wynik

    monkeypatch.setattr(worktree_modul, "_git", nakladka)

    wt = utworz_worktree(repo, "r1", root=root)

    head_po = git(repo, "rev-parse", "HEAD").strip()
    assert head_po != head_przed  # kontrola: nakladka faktycznie commitowala
    assert wt.base_commit == head_przed
    assert git(wt.sciezka, "rev-parse", "HEAD").strip() == wt.base_commit
    assert (wt.sciezka / "a.txt").read_text() == "a\n"


# --- repo jako podkatalog repozytorium (monorepo) ----------------------------------


@pytest.fixture
def podkatalog(repo: Path) -> Path:
    sub = repo / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("c\n")
    git(repo, "update-index", "--add", "sub/c.txt")
    zacommituj(repo, "podkatalog")
    return sub


def test_uruchomienie_z_podkatalogu_repozytorium_nie_pozwala_na_katalog_runu_w_innym_miejscu_repo(
    repo: Path, podkatalog: Path
):
    wpisy_przed = wpisy_worktree(repo)
    root_w_repo = repo / "runy"

    with pytest.raises(NaruszonyNiezmiennik) as e:
        utworz_worktree(podkatalog, "r1", root=root_w_repo)

    assert e.value.nazwa == "run_dir_outside_repo"
    assert not root_w_repo.exists()
    assert wpisy_worktree(repo) == wpisy_przed
    assert status(repo) == ""


def test_kontrola_uruchomienie_z_podkatalogu_z_rootem_poza_repozytorium_tworzy_worktree(
    repo: Path, podkatalog: Path, root: Path
):
    wt = utworz_worktree(podkatalog, "r1", root=root)

    assert wt.base_commit == git(repo, "rev-parse", "HEAD").strip()
    assert wt.sciezka.resolve() in sciezki_worktree(repo)
    assert status(repo) == ""

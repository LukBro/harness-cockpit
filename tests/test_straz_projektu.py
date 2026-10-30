"""Straz projektu (.claude/hooks/straz.py): co sesja agenta moze wypchnac i czego nie.

Kazda regula ma przypadek, w ktorym tylko ona decyduje o wyniku
(`.claude/rules/test-odroznia-regule.md`). Git jest podmieniony na slownik odpowiedzi,
wiec testy nie dotykaja prawdziwego repozytorium.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCIEZKA = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "straz.py"
_spec = importlib.util.spec_from_file_location("straz", SCIEZKA)
straz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(straz)

DOBRY_URL = "https://github.com/LukBro/harness-cockpit.git"
AUTOR = "lukaszbrowar@gmail.com"
BRAK_ORIGIN_MAIN = object()


def _git(odpowiedzi: dict | None = None, *, url=DOBRY_URL, push_url=None, galaz="worktree-HAR-7",
         commity=None):
    mapa = {
        ("remote", "get-url", "origin"): url,
        ("remote", "get-url", "--push", "origin"): push_url or url,
        ("rev-parse", "--abbrev-ref", "HEAD"): galaz,
    }
    mapa.update(odpowiedzi or {})
    log = [("a" * 40, AUTOR, "HAR-7: zmiana")] if commity is None else commity

    def git(katalog, *argv):
        if argv and argv[0] == "log":
            if log is BRAK_ORIGIN_MAIN:
                raise straz.Odmowa("brak origin/main")
            return "".join(f"{s}\x00{a}\x00{t}\x1e" for s, a, t in log)
        if argv not in mapa:
            raise straz.Odmowa(f"nieznane: {argv}")
        return mapa[argv]
    return git


def _bash(komenda: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": komenda}, "cwd": "/repo"}


def _edycja(sciezka: str, narzedzie: str = "Edit") -> dict:
    return {"tool_name": narzedzie, "tool_input": {"file_path": sciezka}, "cwd": "/repo"}


def _odmowa(zdarzenie, git=None, srodowisko=None) -> str:
    with pytest.raises(straz.Odmowa) as e:
        straz.ocen(zdarzenie, git=git or _git(), srodowisko=srodowisko or {})
    return str(e.value)


def _zgoda(zdarzenie, git=None, srodowisko=None):
    return straz.ocen(zdarzenie, git=git or _git(), srodowisko=srodowisko or {})


# --- push: galaz i repozytorium --------------------------------------------------------------

def test_push_galezi_ticketu_do_tego_repo_przechodzi_i_trafia_do_dziennika():
    assert _zgoda(_bash("git push -u origin worktree-HAR-7")).startswith("git push")


@pytest.mark.parametrize("galaz", ["retro-M1.4", "fix-main-kill-helper", "worktree-HAR-142"])
def test_push_galezi_retro_i_poprawek_przechodzi(galaz):
    assert _zgoda(_bash(f"git push -u origin {galaz}"))


def test_push_na_main_jest_odrzucony():
    assert "main" in _odmowa(_bash("git push origin main"))


def test_push_bieżącej_galezi_main_bez_nazwy_jest_odrzucony():
    assert "main" in _odmowa(_bash("git push"), git=_git(galaz="main"))


def test_push_refspecem_na_main_jest_odrzucony():
    assert "main" in _odmowa(_bash("git push origin worktree-HAR-7:main"))


def test_push_refspecem_refs_heads_main_jest_odrzucony():
    assert "main" in _odmowa(_bash("git push origin HEAD:refs/heads/main"))


@pytest.mark.parametrize("galaz", ["feature-x", "worktree-HARX-1", "worktree-HAR-", "master"])
def test_push_galezi_spoza_wzorca_jest_odrzucony(galaz):
    assert "zabroniony" in _odmowa(_bash(f"git push origin {galaz}"))


def test_push_do_innego_zdalnego_jest_odrzucony():
    assert "origin" in _odmowa(_bash("git push upstream worktree-HAR-7"))


def test_push_gdy_origin_wskazuje_na_inne_repo_jest_odrzucony():
    git = _git(url="https://github.com/ktos/inne.git")
    assert "LukBro/harness-cockpit" in _odmowa(_bash("git push origin worktree-HAR-7"), git)


def test_push_gdy_adres_push_origin_wskazuje_na_inne_repo_jest_odrzucony():
    git = _git(push_url="git@github.com:ktos/inne.git")
    assert "LukBro/harness-cockpit" in _odmowa(_bash("git push origin worktree-HAR-7"), git)


@pytest.mark.parametrize("url", [
    "https://github.com/LukBro/harness-cockpit",
    "git@github.com:LukBro/harness-cockpit.git",
    "ssh://git@github.com/LukBro/harness-cockpit.git",
])
def test_kazdy_zapis_adresu_tego_repo_jest_rozpoznany(url):
    assert _zgoda(_bash("git push origin worktree-HAR-7"), git=_git(url=url))


def test_adres_z_podobna_nazwa_repo_nie_jest_tym_repo():
    git = _git(url="https://github.com/LukBro/harness-cockpit-fork.git")
    assert _odmowa(_bash("git push origin worktree-HAR-7"), git)


# --- push: wymuszenie, kasowanie, opcje --------------------------------------------------------

@pytest.mark.parametrize("flaga", ["--force", "-f", "--force-with-lease", "--force-with-lease=x",
                                   "--delete", "-d", "--mirror", "--all", "--prune", "-uf"])
def test_push_z_wymuszeniem_albo_kasowaniem_jest_odrzucony(flaga):
    assert "zabroniony" in _odmowa(_bash(f"git push {flaga} origin worktree-HAR-7"))


def test_push_refspecem_z_plusem_jest_odrzucony():
    assert "+" in _odmowa(_bash("git push origin +worktree-HAR-7"))


def test_push_z_opcja_globalna_c_jest_odrzucony():
    assert "-c" in _odmowa(_bash("git -c remote.origin.url=x push origin worktree-HAR-7"))


def test_push_z_innego_katalogu_przez_C_jest_sprawdzany_w_tym_katalogu():
    widziane = []
    bazowy = _git()

    def git(katalog, *argv):
        widziane.append(katalog)
        return bazowy(katalog, *argv)
    _zgoda(_bash("git -C /inne/repo push origin worktree-HAR-7"), git=git)
    assert set(widziane) == {"/inne/repo"}


def test_slowo_push_w_tresci_commita_nie_jest_pushem():
    assert _zgoda(_bash('git commit -m "push poprawki"')) is None


def test_push_ukryty_w_lancuchu_komend_jest_sprawdzany():
    assert "main" in _odmowa(_bash("ruff check . && git push origin main"))


def test_push_po_przypisaniu_zmiennej_jest_sprawdzany():
    assert "main" in _odmowa(_bash("GIT_TRACE=1 git push origin main"))


# --- push: autor commitow ---------------------------------------------------------------------

def test_commit_innego_autora_blokuje_push():
    git = _git(commity=[("b" * 40, "praca@firma.pl", "HAR-7")])
    assert "praca@firma.pl" in _odmowa(_bash("git push origin worktree-HAR-7"), git)


def test_linia_wspolautora_blokuje_push():
    git = _git(commity=[("c" * 40, AUTOR, "HAR-7\n\nCo-Authored-By: Claude <x@y>")])
    assert "Co-Authored-By" in _odmowa(_bash("git push origin worktree-HAR-7"), git)


def test_nie_da_sie_sprawdzic_commitow_to_odmowa():
    git = _git(commity=BRAK_ORIGIN_MAIN)
    assert "origin/main" in _odmowa(_bash("git push origin worktree-HAR-7"), git)


def test_wiele_poprawnych_commitow_przechodzi():
    git = _git(commity=[("d" * 40, AUTOR, "a"), ("e" * 40, AUTOR, "b")])
    assert _zgoda(_bash("git push origin worktree-HAR-7"), git)


# --- zdalne repozytoria i gh ------------------------------------------------------------------

@pytest.mark.parametrize("akcja", ["add", "set-url", "rename", "remove", "rm"])
def test_zmiana_zdalnych_repozytoriow_jest_odrzucona(akcja):
    assert "zdalnych" in _odmowa(_bash(f"git remote {akcja} origin x"))


def test_lista_zdalnych_repozytoriow_przechodzi():
    assert _zgoda(_bash("git remote -v")) is None


def test_zalozenie_pr_w_tym_repo_przechodzi_i_trafia_do_dziennika():
    assert _zgoda(_bash("gh pr create --repo LukBro/harness-cockpit --base main"))


@pytest.mark.parametrize("opcja", ["--repo ktos/inne", "-R ktos/inne", "--repo=ktos/inne"])
def test_gh_w_innym_repo_jest_odrzucone(opcja):
    assert "LukBro/harness-cockpit" in _odmowa(_bash(f"gh pr create {opcja}"))


def test_scalenie_z_pominieciem_ochrony_jest_odrzucone():
    assert "--admin" in _odmowa(_bash("gh pr merge 5 --squash --admin"))


def test_automatyczne_scalenie_po_zielonym_ci_przechodzi():
    assert _zgoda(_bash("gh pr merge 5 --auto --squash"))


@pytest.mark.parametrize("polecenie", ["repo delete x", "secret set X", "workflow run ci",
                                       "auth token", "release create v1", "variable set A"])
def test_gh_zmieniajace_repozytorium_sekrety_i_workflow_jest_odrzucone(polecenie):
    assert "zabronione" in _odmowa(_bash(f"gh {polecenie}"))


def test_gh_api_do_odczytu_przechodzi():
    assert _zgoda(_bash("gh api repos/LukBro/harness-cockpit/pulls")) is None


@pytest.mark.parametrize("zapis", ["-X POST", "--method PATCH", "-f title=x", "--input plik"])
def test_gh_api_zapisujace_jest_odrzucone(zapis):
    assert "odczytu" in _odmowa(_bash(f"gh api repos/LukBro/harness-cockpit/pulls {zapis}"))


# --- ustawienia i hooki Claude Code -----------------------------------------------------------

@pytest.mark.parametrize("sciezka", ["/repo/.claude/settings.json",
                                     "/repo/.claude/settings.local.json",
                                     "/repo/.claude/hooks/straz.py"])
@pytest.mark.parametrize("narzedzie", ["Edit", "Write", "MultiEdit"])
def test_sesja_nie_zmienia_sobie_ustawien_ani_hookow(sciezka, narzedzie):
    assert "czlowieka" in _odmowa(_edycja(sciezka, narzedzie))


def test_czlowiek_odblokowuje_zmiane_ustawien_zmienna_srodowiska():
    assert _zgoda(_edycja("/repo/.claude/settings.json"),
                  srodowisko={"COCKPIT_STRAZ_ODBLOKUJ": "1"}) is None


def test_inna_wartosc_zmiennej_nie_odblokowuje():
    assert _odmowa(_edycja("/repo/.claude/settings.json"),
                   srodowisko={"COCKPIT_STRAZ_ODBLOKUJ": "tak"})


@pytest.mark.parametrize("sciezka", ["/repo/.claude/agent-memory/test-writer/MEMORY.md",
                                     "/repo/.claude/rules/nowa.md", "/repo/src/cockpit/x.py"])
def test_pamiec_agentow_reguly_i_kod_mozna_edytowac(sciezka):
    assert _zgoda(_edycja(sciezka)) is None


@pytest.mark.parametrize("komenda", ["cat .claude/settings.json",
                                     "git diff .claude/hooks/straz.py",
                                     "grep push .claude/settings.json"])
def test_odczyt_ustawien_przechodzi(komenda):
    assert _zgoda(_bash(komenda)) is None


@pytest.mark.parametrize("komenda", ["sed -i s/a/b/ .claude/settings.json",
                                     "cat x > .claude/settings.json",
                                     "echo x | tee .claude/hooks/straz.py",
                                     "cp inny .claude/settings.local.json"])
def test_zapis_ustawien_z_powloki_jest_odrzucony(komenda):
    assert "czlowiek" in _odmowa(_bash(komenda))


def test_inne_narzedzia_nie_sa_oceniane():
    assert _zgoda({"tool_name": "Read", "tool_input": {"file_path": ".claude/settings.json"}}) \
        is None


def test_nieczytelna_komenda_to_odmowa():
    assert "nieczytelna" in _odmowa(_bash('git push "origin'))


# --- punkt wejscia hooka ----------------------------------------------------------------------

def _uruchom(monkeypatch, tmp_path, wejscie: str):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(wejscie))
    return straz.main()


def test_odmowa_konczy_hook_kodem_2_z_powodem_i_wpisem_w_dzienniku(monkeypatch, tmp_path, capsys):
    kod = _uruchom(monkeypatch, tmp_path, json.dumps(_bash("git remote add x y")))
    assert kod == 2
    assert "straz projektu" in capsys.readouterr().err
    wpis = json.loads((tmp_path / "harness-cockpit" / "audyt-git.ndjson").read_text())
    assert wpis["decyzja"] == "odmowa"


def test_zwykla_komenda_przechodzi_kodem_0_bez_wpisu(monkeypatch, tmp_path):
    assert _uruchom(monkeypatch, tmp_path, json.dumps(_bash("pytest -q"))) == 0
    assert not (tmp_path / "harness-cockpit" / "audyt-git.ndjson").exists()


def test_zgoda_na_gh_trafia_do_dziennika(monkeypatch, tmp_path):
    kod = _uruchom(monkeypatch, tmp_path, json.dumps(_bash("gh pr merge 5 --auto --squash")))
    assert kod == 0
    wpis = json.loads((tmp_path / "harness-cockpit" / "audyt-git.ndjson").read_text())
    assert wpis["decyzja"] == "zgoda"


def test_nieczytelne_wejscie_hooka_to_odmowa(monkeypatch, tmp_path, capsys):
    assert _uruchom(monkeypatch, tmp_path, "{nie json") == 2
    assert "odmowa" in capsys.readouterr().err


# --- gh pr merge: tylko z gwarancja zielonego CI (poprawka po wlaczeniu ochrony main) ----------

SPRINT = {"MONOLYNX_SPRINT_RUN": "1"}


def test_w_sesji_sprintu_zwykly_merge_jest_odrzucony():
    assert "--auto" in _odmowa(_bash("gh pr merge 5 --squash"), srodowisko=SPRINT)


def test_w_sesji_sprintu_merge_z_match_head_commit_tez_jest_odrzucony():
    assert "--auto" in _odmowa(_bash("gh pr merge 5 --merge --match-head-commit abc"),
                               srodowisko=SPRINT)


def test_w_sesji_sprintu_merge_po_zielonym_ci_przechodzi():
    assert _zgoda(_bash("gh pr merge 5 --auto --squash"), srodowisko=SPRINT)


def test_wylaczenie_automatycznego_merge_przechodzi():
    assert _zgoda(_bash("gh pr merge 5 --disable-auto"), srodowisko=SPRINT)


def test_poza_sprintem_goly_merge_jest_odrzucony():
    assert "--match-head-commit" in _odmowa(_bash("gh pr merge 5 --squash"))


@pytest.mark.parametrize("forma", ["--match-head-commit abc123", "--match-head-commit=abc123"])
def test_poza_sprintem_merge_sprawdzonego_commita_jak_w_mr_queue_przechodzi(forma):
    assert _zgoda(_bash(f"gh pr merge 5 --merge {forma}"))


def test_admin_jest_odrzucony_nawet_z_auto():
    assert "--admin" in _odmowa(_bash("gh pr merge 5 --auto --admin"))


def test_inna_wartosc_zmiennej_sprintu_nie_wlacza_trybu_sprintu():
    assert _zgoda(_bash("gh pr merge 5 --merge --match-head-commit abc"),
                  srodowisko={"MONOLYNX_SPRINT_RUN": "0"})

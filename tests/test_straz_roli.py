"""HAR-175: straz roli - hook PreToolUse odmawia gita, zapisu poza worktree i zapisu roli bez prawa zapisu.

`decyzja` jest czysta (zdarzenie, rola, worktree -> powod odmowy albo None), `main` to skrypt
hooka (stdin JSON, stdout JSON deny). Hook jest uruchamiany naprawde: komenda pochodzi z
`ustawienia_hookow(plik, rola=..., worktree=...)`, a proces startuje przez `tests/procesy.py`
z `cwd` = korzen repozytorium i twardymi limitami czasu. Kryteria "executor + git commit"
i "reviewer nie zapisze pliku" sprawdzamy na danych: atrapa `claude` uruchomiona przez
`wykonaj_krok` wola komende hooka z pliku `--settings` i wykonuje narzedzie tylko wtedy,
gdy hook nie odmowil; skutek czytamy z repozytorium i z dysku.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import shlex
import sys
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from pathlib import Path

import pytest
from procesy import (
    czekaj,
    uruchom_proces,
    uruchom_proces_sync,
    zadanie_tla,
    zakoncz_proces,
    zakoncz_proces_sync,
)

from cockpit.services.invariants import Rola, profil_roli
from runner import hooks, straz_roli
from runner.agent import ProfilBackendu, SpecKroku, polecenie_agenta
from runner.hooks import (
    ZMIENNA_PLIKU,
    BladZdarzeniaHooka,
    CzytnikHookow,
    do_wysylki,
    plik_hookow,
    plik_ustawien,
    przekazuj,
    ustawienia_hookow,
)
from runner.krok import wykonaj_krok
from runner.straz_roli import decyzja, main

KORZEN = Path(__file__).resolve().parents[1]
STRAZ = Path(straz_roli.__file__)
MATCHER_STRAZY = "Bash|Write|Edit|MultiEdit|NotebookEdit"
RUN = "run-175"
KROK = "krok-175"
LIMIT = 10.0
LIMIT_HOOKA = 60.0
LIMIT_KROKU = 120.0

ROLE_Z_ZAPISEM = [Rola.author, Rola.executor]
ROLE_BEZ_ZAPISU = [Rola.reviewer, Rola.gatherer]
WSZYSTKIE_ROLE = [*ROLE_Z_ZAPISEM, *ROLE_BEZ_ZAPISU]

BRAK = object()  # klucz nieobecny w zdarzeniu (inaczej niz wartosc None)

NARZEDZIA_ZAPISU = [
    pytest.param("Write", "file_path", id="Write"),
    pytest.param("Edit", "file_path", id="Edit"),
    pytest.param("MultiEdit", "file_path", id="MultiEdit"),
    pytest.param("NotebookEdit", "notebook_path", id="NotebookEdit"),
]


def _rola(r: Rola) -> str:
    return r.value


def _zd(narzedzie, wejscie=BRAK, *, cwd=None, sesja="sesja-1") -> dict:
    zdarzenie: dict = {
        "session_id": sesja,
        "hook_event_name": "PreToolUse",
        "tool_name": narzedzie,
    }
    if wejscie is not BRAK:
        zdarzenie["tool_input"] = wejscie
    if cwd is not None:
        zdarzenie["cwd"] = str(cwd)
    return zdarzenie


def _bash(komenda, *, cwd=None) -> dict:
    return _zd("Bash", {"command": komenda}, cwd=cwd)


def _zapis(narzedzie: str, klucz: str, sciezka, *, cwd=None) -> dict:
    return _zd(narzedzie, {klucz: sciezka}, cwd=cwd)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    katalog = tmp_path / "wt"
    katalog.mkdir()
    return katalog


@pytest.fixture
def obcy(tmp_path: Path) -> Path:
    katalog = tmp_path / "obcy"
    katalog.mkdir()
    return katalog


def test_podzial_rol_w_testach_zgadza_sie_z_profilem_roli():
    assert all(profil_roli(r).zapis for r in ROLE_Z_ZAPISEM)
    assert not any(profil_roli(r).zapis for r in ROLE_BEZ_ZAPISU)
    assert set(WSZYSTKIE_ROLE) == set(Rola)


# --- git: odmowa w kazdej roli ---------------------------------------------------------

GIT_ZAKAZANY = [
    "git commit -m x",
    "git commit",
    "git push",
    "git push origin main",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git checkout main",
    "git checkout -- plik.py",
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("komenda", GIT_ZAKAZANY)
def test_git_zmieniajacy_historie_albo_drzewo_jest_odmowiony_w_kazdej_roli_z_powodem_no_git_write(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "no_git_write" in powod


JADRA_GITA = [
    "git commit -m x",
    "git push origin main",
    "git reset --hard",
    "git checkout main",
]
# Obudowy, w ktorych kazdy znak powloki z listy rozdzielajacej decyduje sam (bez spacji wokol).
OBUDOWY = [
    pytest.param("{k}", id="goly"),
    pytest.param("ls; {k}", id="srednik-spacja"),
    pytest.param("ls && {k}", id="i-spacja"),
    pytest.param("ls | {k}", id="rura-spacja"),
    pytest.param("ls || {k}", id="lub-spacja"),
    pytest.param("ls & {k}", id="tlo-spacja"),
    pytest.param("ls;{k}", id="srednik-sklejony"),
    pytest.param("ls&&{k}", id="i-sklejone"),
    pytest.param("ls|{k}", id="rura-sklejona"),
    pytest.param("ls||{k}", id="lub-sklejone"),
    pytest.param("ls&{k}", id="tlo-sklejone"),
    pytest.param("({k}", id="nawias-otwierajacy"),
    pytest.param("`{k}`", id="odwrotny-apostrof"),
    pytest.param("echo $({k}", id="podstawienie-polecenia"),
    pytest.param('bash -c "{k}"', id="bash-c-cudzyslow"),
    pytest.param("bash -c '{k}'", id="bash-c-apostrof"),
    pytest.param("ls\n{k}", id="nowa-linia"),
]


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("obudowa", OBUDOWY)
@pytest.mark.parametrize("jadro", JADRA_GITA)
def test_zakazany_git_jest_wykryty_takze_za_separatorem_powloki_i_w_bash_c(
    rola, obudowa, jadro, worktree
):
    komenda = obudowa.format(k=jadro)

    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "no_git_write" in powod


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "git commit)",
        "git push)",
        "git checkout)",
        "git reset --hard)",
    ],
)
def test_nawias_zamykajacy_przyklejony_do_podpolecenia_gita_nie_ukrywa_zakazu(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "no_git_write" in powod


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "git -C x commit -m y",
        "git -c user.name=a commit -m y",
        "git --git-dir d push",
        "git -C x checkout main",
        "git -c a=b checkout main",
        "git -C x reset --hard",
        "git --no-pager checkout main",
        "/usr/bin/git push",
        "/usr/bin/git commit -m x",
        "/usr/bin/git checkout main",
        "/usr/bin/git reset --hard",
        "cd r&&git push",
    ],
)
def test_git_z_opcjami_globalnymi_albo_pelna_sciezka_nie_omija_zakazu(rola, komenda, worktree):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "no_git_write" in powod


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "git status",
        "git diff",
        "git log --oneline -5",
        "git log --grep=commit",
        "git log --grep=checkout",
        "git diff --stat | head",
        "git status; git diff",
        "git reset HEAD plik.py",
        "git add plik.py",
        "pytest -q",
        "ruff check .",
        "ruff format --check .",
        "grep -rn commit src",
        "echo git is great",
        "python -c 'print(1)'",
        "echo x > plik",
        "sed -i s/a/b/ plik.py",
        "mkdir -p out && touch out/x",
        "cat a | tee b",
    ],
)
def test_rola_z_prawem_zapisu_moze_uruchamiac_polecenia_ktore_nie_zmieniaja_historii_gita(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


# --- rola bez zapisu: Bash tylko z bialej listy odczytu --------------------------------

ODCZYT_DOZWOLONY = [
    "ls",
    "ls -la src",
    "cat README.md",
    "head -n 5 README.md",
    "tail -n 5 README.md",
    "grep -rn foo src",
    "rg foo src",
    "wc -l README.md",
    "pwd",
    "git status",
    "git status --short",
    "git diff",
    "git diff HEAD~1",
    "git log --oneline -5",
    "git show HEAD",
]


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda", ODCZYT_DOZWOLONY)
def test_rola_bez_zapisu_moze_uruchomic_polecenie_do_odczytu_z_bialej_listy(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


POZA_BIALA_LISTA = [
    "rm -rf x",
    "touch x",
    "mkdir d",
    "cp a b",
    "mv a b",
    "python script.py",
    "pytest -q",
    "ruff check .",
    "echo hi",
    "tee x",
    "sed -i s/a/b/ plik",
    "sed -n 1p plik",
    "find . -delete",
    "git add .",
    "git branch x",
    "git fetch",
    "git",
    "lsof -i",
    "catalog x",
    "pwdx",
    "headx",
    "tailx",
    "greper x",
    "rgx x",
    "wcx x",
    "git statusx",
    "git diffx",
    "git logx",
    "git showx",
    "python -c \"open('x', 'w').write('y')\"",
]


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda", POZA_BIALA_LISTA)
def test_rola_bez_zapisu_nie_uruchomi_polecenia_spoza_bialej_listy_z_powodem_reviewer_readonly(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "reviewer_readonly" in powod


METAZNAKI = [
    pytest.param("ls > out", id="przekierowanie"),
    pytest.param("ls >out", id="przekierowanie-sklejone"),
    pytest.param("cat a>b", id="przekierowanie-w-argumencie"),
    pytest.param("cat a >> b", id="dopisanie"),
    pytest.param("ls | wc -l", id="rura"),
    pytest.param("cat a|wc -l", id="rura-w-argumencie"),
    pytest.param("ls ; pwd", id="srednik"),
    pytest.param("ls a;rm x", id="srednik-w-argumencie"),
    pytest.param("ls && pwd", id="i"),
    pytest.param("ls a&&rm x", id="i-w-argumencie"),
    pytest.param("ls || pwd", id="lub"),
    pytest.param("ls a||rm x", id="lub-w-argumencie"),
    pytest.param("ls &", id="tlo"),
    pytest.param("ls a&rm x", id="tlo-w-argumencie"),
    pytest.param("ls `pwd`", id="odwrotny-apostrof"),
    pytest.param("ls a`rm x`", id="odwrotny-apostrof-w-argumencie"),
    pytest.param("ls $(pwd)", id="podstawienie-polecenia"),
    pytest.param("ls a$(rm x)", id="podstawienie-w-argumencie"),
    pytest.param("cat <<EOF", id="here-doc"),
    pytest.param("cat a<<EOF", id="here-doc-w-argumencie"),
    pytest.param("ls\nrm x", id="nowa-linia"),
    pytest.param("git status > out", id="git-przekierowanie"),
    pytest.param("git diff | tee x", id="git-rura"),
    pytest.param("echo x > plik", id="echo-do-pliku"),
]


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda", METAZNAKI)
def test_rola_bez_zapisu_nie_przepuszcza_polecenia_z_bialej_listy_z_metaznakiem_zapisu(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "reviewer_readonly" in powod


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("narzedzie,klucz", NARZEDZIA_ZAPISU)
def test_rola_bez_zapisu_nie_uzyje_narzedzia_zapisu_nawet_w_swoim_worktree(
    rola, narzedzie, klucz, worktree
):
    zdarzenie = _zapis(narzedzie, klucz, str(worktree / "plik.md"), cwd=worktree)

    powod = decyzja(zdarzenie, rola, worktree)

    assert powod is not None
    assert "reviewer_readonly" in powod


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("narzedzie,klucz", NARZEDZIA_ZAPISU)
def test_rola_z_prawem_zapisu_zapisuje_w_swoim_worktree(rola, narzedzie, klucz, worktree):
    zdarzenie = _zapis(narzedzie, klucz, str(worktree / "src" / "plik.py"), cwd=worktree)

    assert decyzja(zdarzenie, rola, worktree) is None


# --- zapis poza worktree: kazda rola ---------------------------------------------------


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("narzedzie,klucz", NARZEDZIA_ZAPISU)
def test_zapis_pod_obca_sciezka_bezwzgledna_jest_odmowiony_z_powodem_poza_worktree(
    rola, narzedzie, klucz, worktree, obcy
):
    zdarzenie = _zapis(narzedzie, klucz, str(obcy / "x.py"), cwd=worktree)

    powod = decyzja(zdarzenie, rola, worktree)

    assert powod is not None
    assert "poza worktree" in powod


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("narzedzie,klucz", NARZEDZIA_ZAPISU)
def test_zapis_poza_worktree_jest_odmowiony_w_kazdej_roli(
    rola, narzedzie, klucz, worktree, obcy
):
    zdarzenie = _zapis(narzedzie, klucz, str(obcy / "x.py"), cwd=worktree)

    assert decyzja(zdarzenie, rola, worktree) is not None


def test_zapis_do_katalogu_o_wspolnym_prefiksie_nazwy_z_worktree_jest_poza_worktree(
    tmp_path, worktree
):
    sasiad = tmp_path / "wt-sasiad"
    sasiad.mkdir()

    powod = decyzja(_zapis("Write", "file_path", str(sasiad / "x.py"), cwd=worktree),
                    Rola.executor, worktree)

    assert powod is not None
    assert "poza worktree" in powod


def test_sciezka_wzgledna_z_kropkami_wychodzaca_z_worktree_jest_odmowiona(worktree):
    powod = decyzja(_zapis("Write", "file_path", "../poza.txt", cwd=worktree),
                    Rola.executor, worktree)

    assert powod is not None
    assert "poza worktree" in powod


def test_sciezka_bezwzgledna_z_kropkami_wychodzaca_z_worktree_jest_odmowiona(worktree):
    sciezka = f"{worktree}/podkatalog/../../poza.txt"

    powod = decyzja(_zapis("Edit", "file_path", sciezka, cwd=worktree), Rola.author, worktree)

    assert powod is not None
    assert "poza worktree" in powod


def test_sciezka_z_kropkami_ktora_zostaje_w_worktree_jest_dozwolona(worktree):
    sciezka = f"{worktree}/a/../b.txt"

    assert decyzja(_zapis("Write", "file_path", sciezka, cwd=worktree),
                   Rola.executor, worktree) is None


def test_dowiazanie_symboliczne_katalogu_wychodzace_z_worktree_jest_odmowione(
    worktree, obcy
):
    (worktree / "wyjscie").symlink_to(obcy, target_is_directory=True)

    powod = decyzja(_zapis("Write", "file_path", str(worktree / "wyjscie" / "x.py"), cwd=worktree),
                    Rola.executor, worktree)

    assert powod is not None
    assert "poza worktree" in powod


def test_dowiazanie_symboliczne_pliku_wychodzace_z_worktree_jest_odmowione(worktree, obcy):
    cel = obcy / "cel.txt"
    cel.write_text("x")
    (worktree / "link.txt").symlink_to(cel)

    powod = decyzja(_zapis("Edit", "file_path", str(worktree / "link.txt"), cwd=worktree),
                    Rola.author, worktree)

    assert powod is not None
    assert "poza worktree" in powod


def test_worktree_podany_jako_dowiazanie_rozpoznaje_zapis_przez_prawdziwa_sciezke(
    tmp_path, worktree
):
    alias = tmp_path / "alias-worktree"
    alias.symlink_to(worktree, target_is_directory=True)
    prawdziwa = Path(os.path.realpath(worktree)) / "plik.py"

    assert decyzja(_zapis("Write", "file_path", str(prawdziwa), cwd=worktree),
                   Rola.executor, alias) is None


def test_zapis_w_jeszcze_nieistniejacym_podkatalogu_worktree_jest_dozwolony(worktree):
    sciezka = worktree / "nowy" / "gleboki" / "plik.py"

    assert decyzja(_zapis("Write", "file_path", str(sciezka), cwd=worktree),
                   Rola.executor, worktree) is None


def test_sciezka_wzgledna_jest_rozwijana_wzgledem_cwd_zdarzenia_a_nie_worktree(worktree, obcy):
    podkatalog = worktree / "sub"
    podkatalog.mkdir()

    # "../x.py" z podkatalogu worktree zostaje w worktree; wzgledem samego worktree wyszloby poza
    assert decyzja(_zapis("Write", "file_path", "../x.py", cwd=podkatalog),
                   Rola.executor, worktree) is None
    # ta sama sciezka z cwd poza worktree laduje poza worktree
    powod = decyzja(_zapis("Write", "file_path", "x.py", cwd=obcy), Rola.executor, worktree)
    assert powod is not None
    assert "poza worktree" in powod


def test_sciezka_wzgledna_bez_cwd_w_zdarzeniu_jest_rozwijana_wzgledem_worktree(worktree):
    assert decyzja(_zapis("Write", "file_path", "x.py"), Rola.executor, worktree) is None

    powod = decyzja(_zapis("Write", "file_path", "../x.py"), Rola.executor, worktree)
    assert powod is not None
    assert "poza worktree" in powod


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize(
    "wejscie",
    [
        pytest.param({}, id="brak-sciezki"),
        pytest.param({"file_path": 5}, id="sciezka-liczba"),
        pytest.param({"file_path": None}, id="sciezka-null"),
        pytest.param({"file_path": ["a.py"]}, id="sciezka-lista"),
        pytest.param({"file_path": "a\x00b.py"}, id="sciezka-z-bajtem-zerowym"),
        pytest.param({"notebook_path": "a.ipynb"}, id="zly-klucz-sciezki"),
        pytest.param("a.py", id="tool-input-napis"),
        pytest.param(None, id="tool-input-null"),
        pytest.param(BRAK, id="brak-tool-input"),
    ],
)
def test_zapis_bez_poprawnej_sciezki_jest_odmowiony_bez_wyjatku(rola, wejscie, worktree):
    assert decyzja(_zd("Write", wejscie, cwd=worktree), rola, worktree) is not None


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_notebook_edit_z_kluczem_file_path_zamiast_notebook_path_jest_odmowiony(
    rola, worktree
):
    zdarzenie = _zd("NotebookEdit", {"file_path": str(worktree / "a.ipynb")}, cwd=worktree)

    assert decyzja(zdarzenie, rola, worktree) is not None


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize(
    "wejscie",
    [
        pytest.param({"command": 5}, id="komenda-liczba"),
        pytest.param({"command": None}, id="komenda-null"),
        pytest.param({"command": ["git", "commit"]}, id="komenda-lista-gita"),
        pytest.param({"command": ["ls"]}, id="komenda-lista-z-bialej-listy"),
        pytest.param({"command": {"a": 1}}, id="komenda-slownik"),
        pytest.param({}, id="brak-komendy"),
        pytest.param("git commit", id="tool-input-napis"),
        pytest.param(None, id="tool-input-null"),
        pytest.param([], id="tool-input-lista"),
        pytest.param(BRAK, id="brak-tool-input"),
    ],
)
def test_bash_bez_poprawnej_komendy_jest_odmowiony_bez_wyjatku(rola, wejscie, worktree):
    assert decyzja(_zd("Bash", wejscie, cwd=worktree), rola, worktree) is not None


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("narzedzie", ["Read", "Grep", "Glob", "WebFetch"])
def test_narzedzia_odczytu_nie_sa_blokowane_w_zadnej_roli_takze_poza_worktree(
    rola, narzedzie, worktree, obcy
):
    zdarzenie = _zd(narzedzie, {"file_path": str(obcy / "x.py"), "pattern": "x"}, cwd=worktree)

    assert decyzja(zdarzenie, rola, worktree) is None


# --- main: protokol hooka ----------------------------------------------------------------


def _stdin(monkeypatch, dane: str | bytes) -> None:
    surowe = dane.encode() if isinstance(dane, str) else dane
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(surowe), encoding="utf-8"))


def _wywolaj_main(monkeypatch, capfd, dane, rola="executor", worktree="/nie-wazne"):
    _stdin(monkeypatch, dane)
    kod = main(["--rola", rola, "--worktree", str(worktree)])
    return kod, capfd.readouterr().out


def test_odmowa_to_json_deny_hooka_pretooluse_z_kodem_zero(monkeypatch, capfd, worktree):
    dane = json.dumps(_bash("git commit -m x", cwd=worktree))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, dane, worktree=worktree)

    assert kod == 0
    odpowiedz = json.loads(wyjscie)
    assert set(odpowiedz) == {"hookSpecificOutput"}
    wynik = odpowiedz["hookSpecificOutput"]
    assert set(wynik) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    assert wynik["hookEventName"] == "PreToolUse"
    assert wynik["permissionDecision"] == "deny"
    assert "no_git_write" in wynik["permissionDecisionReason"]


def test_dozwolone_polecenie_nie_daje_zadnego_wyjscia_i_konczy_sie_kodem_zero(
    monkeypatch, capfd, worktree
):
    dane = json.dumps(_bash("git status", cwd=worktree))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, dane, worktree=worktree)

    assert kod == 0
    assert wyjscie == ""


def test_main_stosuje_role_z_argumentow_reviewer_nie_zapisze_a_executor_zapisze(
    monkeypatch, capfd, worktree
):
    dane = json.dumps(_zapis("Write", "file_path", str(worktree / "a.md"), cwd=worktree))

    _, dla_recenzenta = _wywolaj_main(monkeypatch, capfd, dane, "reviewer", worktree)
    _, dla_wykonawcy = _wywolaj_main(monkeypatch, capfd, dane, "executor", worktree)

    wynik = json.loads(dla_recenzenta)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    assert "reviewer_readonly" in wynik["permissionDecisionReason"]
    assert dla_wykonawcy == ""


def test_main_bez_argumentu_czyta_argumenty_wiersza_polecenia_procesu(
    monkeypatch, capfd, worktree
):
    monkeypatch.setattr(
        sys, "argv", ["straz_roli.py", "--rola", "reviewer", "--worktree", str(worktree)]
    )
    _stdin(monkeypatch, json.dumps(_bash("rm -rf x", cwd=worktree)))

    kod = main()

    assert kod == 0
    wynik = json.loads(capfd.readouterr().out)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    assert "reviewer_readonly" in wynik["permissionDecisionReason"]


@pytest.mark.parametrize(
    "dane",
    [
        pytest.param("to nie jest json", id="smieci"),
        pytest.param("", id="pusty-stdin"),
        pytest.param('{"tool_name": "Bash", "tool_input": {"command": ', id="ucity-json"),
        pytest.param(b"\xff\xfe\x00", id="zly-utf8"),
    ],
)
def test_nieczytelne_wejscie_hooka_daje_odmowe_z_powodem_straz_roli_a_nie_wyjatek(
    monkeypatch, capfd, dane
):
    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, dane)

    assert kod == 0
    wynik = json.loads(wyjscie)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    assert wynik["permissionDecisionReason"].startswith("straż roli:")


@pytest.mark.parametrize("dane", ["[]", "5", '"git status"', "null"])
def test_wejscie_hooka_niebedace_obiektem_daje_odmowe_a_nie_wyjatek(monkeypatch, capfd, dane):
    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, dane)

    assert kod == 0
    assert json.loads(wyjscie)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("rola", ["czarodziej", "", "REVIEWER"])
def test_nieznana_rola_w_argumentach_daje_odmowe_nawet_dla_bezpiecznego_narzedzia(
    monkeypatch, capfd, worktree, rola
):
    dane = json.dumps(_zd("Read", {"file_path": "a.py"}, cwd=worktree))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, dane, rola, worktree)

    assert kod == 0
    wynik = json.loads(wyjscie)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    assert wynik["permissionDecisionReason"].startswith("straż roli:")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="bez-argumentow"),
        pytest.param(["--rola", "executor"], id="bez-worktree"),
        pytest.param(["--worktree", "/tmp"], id="bez-roli"),
    ],
)
def test_brak_wymaganego_argumentu_daje_odmowe_z_powodem_straz_roli(monkeypatch, capfd, argv):
    _stdin(monkeypatch, json.dumps(_zd("Read", {"file_path": "a.py"})))

    kod = main(argv)

    assert kod == 0
    wynik = json.loads(capfd.readouterr().out)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    assert wynik["permissionDecisionReason"].startswith("straż roli:")


def test_wyjatek_w_decyzji_daje_odmowe_z_powodem_straz_roli(monkeypatch, capfd, worktree):
    def zepsuta(*args, **kwargs):
        raise RuntimeError("awaria wewnetrzna")

    monkeypatch.setattr(straz_roli, "decyzja", zepsuta)
    dane = json.dumps(_bash("git status", cwd=worktree))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, dane, worktree=worktree)

    assert kod == 0
    wynik = json.loads(wyjscie)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    assert wynik["permissionDecisionReason"].startswith("straż roli:")


# --- odmowa jako zdarzenie: plik z COCKPIT_HOOK_FILE ------------------------------------


def _linie(plik: Path) -> list[dict]:
    return [json.loads(w) for w in plik.read_text().splitlines() if w.strip()]


def test_odmowa_dopisuje_do_pliku_hookow_jedna_linie_tool_denied_z_narzedziem_komenda_i_rola(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    dane = json.dumps(_bash("git push origin main", cwd=worktree))

    _, wyjscie = _wywolaj_main(monkeypatch, capfd, dane, "executor", worktree)

    powod = json.loads(wyjscie)["hookSpecificOutput"]["permissionDecisionReason"]
    assert plik.read_bytes().count(b"\n") == 1
    assert _linie(plik) == [{
        "hook_event_name": "ToolDenied",
        "tool_name": "Bash",
        "command": "git push origin main",
        "role": "executor",
        "reason": powod,
        "session_id": "sesja-1",
    }]


def test_dozwolone_polecenie_nie_dopisuje_nic_do_pliku_hookow(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))

    _wywolaj_main(monkeypatch, capfd, json.dumps(_bash("git status", cwd=worktree)),
                  "executor", worktree)

    assert plik.read_bytes() == b""


def test_kolejne_odmowy_dopisuja_kolejne_linie_zamiast_nadpisywac(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))

    for komenda in ("git push", "git commit -m x"):
        _wywolaj_main(monkeypatch, capfd, json.dumps(_bash(komenda, cwd=worktree)),
                      "executor", worktree)

    assert [linia["command"] for linia in _linie(plik)] == ["git push", "git commit -m x"]


def test_komenda_w_zdarzeniu_odmowy_jest_ucieta_do_dwustu_znakow(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    komenda = "git commit -m " + "a" * 500

    _wywolaj_main(monkeypatch, capfd, json.dumps(_bash(komenda, cwd=worktree)),
                  "executor", worktree)

    [linia] = _linie(plik)
    assert linia["command"] == komenda[:200]
    assert len(linia["command"]) == 200


@pytest.mark.parametrize("narzedzie,klucz", NARZEDZIA_ZAPISU)
def test_przy_odmowie_zapisu_zdarzenie_niesie_sciezke_ucieta_do_dwustu_znakow(
    monkeypatch, capfd, tmp_path, worktree, obcy, narzedzie, klucz
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    sciezka = str(obcy / ("d" * 300 + ".py"))
    assert len(sciezka) > 200

    _wywolaj_main(monkeypatch, capfd, json.dumps(_zapis(narzedzie, klucz, sciezka, cwd=worktree)),
                  "executor", worktree)

    [linia] = _linie(plik)
    assert linia["tool_name"] == narzedzie
    assert linia["command"] == sciezka[:200]
    assert linia["role"] == "executor"


def test_zdarzenie_odmowy_nie_wynosi_tresci_zapisywanego_pliku(
    monkeypatch, capfd, tmp_path, worktree, obcy
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    zdarzenie = _zd("Write", {"file_path": str(obcy / "x.py"), "content": "TAJNA-TRESC-PLIKU-77"},
                    cwd=worktree)

    _wywolaj_main(monkeypatch, capfd, json.dumps(zdarzenie), "executor", worktree)

    assert "TAJNA-TRESC-PLIKU-77" not in plik.read_text()


def test_brak_pliku_hookow_nie_jest_tworzony_a_odmowa_zostaje(
    monkeypatch, capfd, tmp_path, worktree
):
    brakujacy = tmp_path / "stan" / "brak.ndjson"
    monkeypatch.setenv(ZMIENNA_PLIKU, str(brakujacy))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, json.dumps(_bash("git push", cwd=worktree)),
                                 "executor", worktree)

    assert kod == 0
    assert json.loads(wyjscie)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not brakujacy.exists()
    assert not brakujacy.parent.exists()


def test_blad_zapisu_pliku_hookow_nie_zmienia_decyzji_odmowy(
    monkeypatch, capfd, tmp_path, worktree
):
    katalog = tmp_path / "to-katalog"
    katalog.mkdir()
    monkeypatch.setenv(ZMIENNA_PLIKU, str(katalog))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, json.dumps(_bash("git push", cwd=worktree)),
                                 "executor", worktree)

    assert kod == 0
    assert json.loads(wyjscie)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_odmowa_bez_zmiennej_pliku_hookow_dziala_bez_zapisu(monkeypatch, capfd, worktree):
    monkeypatch.delenv(ZMIENNA_PLIKU, raising=False)

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, json.dumps(_bash("git push", cwd=worktree)),
                                 "executor", worktree)

    assert kod == 0
    assert json.loads(wyjscie)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- hook jako prawdziwy proces: komenda z ustawien ---------------------------------------


@dataclass
class _Wynik:
    returncode: int
    stdout: str
    stderr: str


def _polecenie_strazy(tmp_path: Path, rola: Rola, worktree: Path) -> str:
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    ustawienia = ustawienia_hookow(plik, rola=rola, worktree=worktree)
    grupy = [g for g in ustawienia["hooks"]["PreToolUse"] if g.get("matcher") == MATCHER_STRAZY]
    assert len(grupy) == 1
    return grupy[0]["hooks"][0]["command"]


async def _uruchom_async(argv: list[str], stdin: str, env: dict[str, str]) -> _Wynik:
    proc = await uruchom_proces(
        *argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, cwd=str(KORZEN)
    )
    try:
        wyjscie, bledy = await czekaj(
            proc.communicate(stdin.encode()), LIMIT_HOOKA, "straz roli jako proces"
        )
    finally:
        await zakoncz_proces(proc, nazwa="straz roli")
    return _Wynik(proc.returncode, wyjscie.decode(), bledy.decode())


def _uruchom(argv: list[str], stdin: str, plik: Path | None = None) -> _Wynik:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if plik is not None:
        env[ZMIENNA_PLIKU] = str(plik)
    return asyncio.run(_uruchom_async(argv, stdin, env))


def _uruchom_polecenie(polecenie: str, stdin: str, plik: Path | None = None) -> _Wynik:
    return _uruchom(["sh", "-c", polecenie], stdin, plik)


def _odmowa(wynik: _Wynik) -> dict:
    assert wynik.returncode == 0, wynik.stderr
    odpowiedz = json.loads(wynik.stdout)["hookSpecificOutput"]
    assert odpowiedz["hookEventName"] == "PreToolUse"
    assert odpowiedz["permissionDecision"] == "deny"
    return odpowiedz


def test_hook_uruchomiony_jako_proces_odmawia_commita_wykonawcy_jsonem_deny_i_kodem_zero(
    tmp_path, worktree
):
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("git commit -m x", cwd=worktree)))

    assert "no_git_write" in _odmowa(wynik)["permissionDecisionReason"]


def test_hook_uruchomiony_jako_proces_przepuszcza_dozwolone_polecenie_pustym_wyjsciem(
    tmp_path, worktree
):
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("git status", cwd=worktree)))

    assert wynik.returncode == 0
    assert wynik.stdout == ""


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_hook_uruchomiony_jako_proces_odmawia_zapisu_poza_worktree_w_kazdej_roli(
    tmp_path, worktree, obcy, rola
):
    polecenie = _polecenie_strazy(tmp_path, rola, worktree)
    zdarzenie = _zapis("Write", "file_path", str(obcy / "x.py"), cwd=worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(zdarzenie))

    _odmowa(wynik)


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
def test_hook_uruchomiony_jako_proces_odmawia_roli_bez_zapisu_przekierowania_w_bashu(
    tmp_path, worktree, rola
):
    polecenie = _polecenie_strazy(tmp_path, rola, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("echo x > plik.txt", cwd=worktree)))

    assert "reviewer_readonly" in _odmowa(wynik)["permissionDecisionReason"]


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
def test_hook_uruchomiony_jako_proces_przepuszcza_roli_bez_zapisu_odczyt_z_bialej_listy(
    tmp_path, worktree, rola
):
    polecenie = _polecenie_strazy(tmp_path, rola, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("git diff", cwd=worktree)))

    assert wynik.returncode == 0
    assert wynik.stdout == ""


def test_hook_uruchomiony_jako_proces_dopisuje_odmowe_do_pliku_z_zmiennej(tmp_path, worktree):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("git push", cwd=worktree)), plik)

    powod = _odmowa(wynik)["permissionDecisionReason"]
    assert _linie(plik) == [{
        "hook_event_name": "ToolDenied",
        "tool_name": "Bash",
        "command": "git push",
        "role": "executor",
        "reason": powod,
        "session_id": "sesja-1",
    }]


def test_hook_uruchomiony_jako_proces_nie_dopisuje_nic_gdy_polecenie_jest_dozwolone(
    tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    _uruchom_polecenie(polecenie, json.dumps(_bash("git diff", cwd=worktree)), plik)

    assert plik.read_bytes() == b""


@pytest.mark.parametrize(
    "stdin",
    [
        pytest.param("to nie jest json", id="smieci"),
        pytest.param("", id="pusty-stdin"),
        pytest.param('{"tool_name": "Bash", "tool_inp', id="ucity-json"),
    ],
)
def test_hook_uruchomiony_jako_proces_na_smieciowym_wejsciu_odmawia_z_powodem_straz_roli(
    tmp_path, worktree, stdin
):
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    wynik = _uruchom_polecenie(polecenie, stdin)

    assert _odmowa(wynik)["permissionDecisionReason"].startswith("straż roli:")


def test_hook_uruchomiony_jako_proces_z_nieznana_rola_odmawia_nawet_odczytu(tmp_path, worktree):
    argv = [sys.executable, "-I", str(STRAZ), "--rola", "czarodziej", "--worktree", str(worktree)]

    wynik = _uruchom(argv, json.dumps(_zd("Read", {"file_path": "a.py"}, cwd=worktree)))

    assert _odmowa(wynik)["permissionDecisionReason"].startswith("straż roli:")


def test_hook_uruchomiony_jako_proces_bez_argumentow_odmawia(tmp_path):
    argv = [sys.executable, "-I", str(STRAZ)]

    wynik = _uruchom(argv, json.dumps(_zd("Read", {"file_path": "a.py"})))

    assert _odmowa(wynik)["permissionDecisionReason"].startswith("straż roli:")


def test_hook_uruchomiony_jako_proces_z_nieistniejacym_plikiem_hookow_nie_tworzy_go(
    tmp_path, worktree
):
    brakujacy = tmp_path / "stan" / "brak.ndjson"
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("git push", cwd=worktree)), brakujacy)

    _odmowa(wynik)
    assert not brakujacy.exists()


# --- zdarzenia w gore: odmowa narzedzia i powtorzona odmowa -------------------------------

KLUCZE_WYSYLKI = {"kind", "run_id", "step_id", "agent_id", "agent_type", "duration_ms", "text"}
PREFIKS_POWTORZENIA = "agent powtarza zablokowaną komendę: "
ZNACZNIK_POWODU = "ZNACZNIK-POWODU-9314"


def _odmowa_payload(narzedzie="Bash", komenda="git push", rola="executor") -> dict:
    return {
        "session_id": "sesja-1",
        "hook_event_name": "ToolDenied",
        "tool_name": narzedzie,
        "command": komenda,
        "role": rola,
        "reason": f"niezmiennik no_git_write: {ZNACZNIK_POWODU}",
    }


async def _przeslij(tmp_path: Path, payloady: list[dict], krok: str = KROK) -> list[dict]:
    plik = plik_hookow(tmp_path / "stan", RUN, krok)
    czytnik = CzytnikHookow(plik, RUN, krok, interwal=0.01)
    with plik.open("ab") as f:
        for p in payloady:
            f.write(json.dumps(p).encode() + b"\n")
    czytnik.odczytaj_nowe()
    wyslane: list[dict] = []

    async def wyslij(**kw) -> None:
        wyslane.append(kw)

    async with zadanie_tla(przekazuj(czytnik, wyslij), "przekazywanie odmow"):
        await czekaj(czytnik.kolejka.join(), LIMIT, "kolejka odmow")
    return wyslane


def _rodzaje(wyslane: list[dict]) -> list[str]:
    return sorted(w["kind"] for w in wyslane)


async def test_odmowa_narzedzia_idzie_w_gore_jako_zdarzenie_z_narzedziem_komenda_i_rola(tmp_path):
    [zdarzenie] = await _przeslij(tmp_path, [_odmowa_payload("Bash", "git push", "executor")])

    assert zdarzenie["kind"] == "hook:ToolDenied"
    assert zdarzenie["text"] == "Bash: git push [role=executor]"
    assert zdarzenie["run_id"] == RUN
    assert zdarzenie["step_id"] == KROK


async def test_odmowa_zapisu_niesie_w_zdarzeniu_narzedzie_sciezke_i_role(tmp_path):
    [zdarzenie] = await _przeslij(
        tmp_path, [_odmowa_payload("NotebookEdit", "/obce/a.ipynb", "reviewer")]
    )

    assert zdarzenie["text"] == "NotebookEdit: /obce/a.ipynb [role=reviewer]"


async def test_zdarzenie_odmowy_ma_tylko_metadane_bez_powodu_z_hooka(tmp_path):
    [zdarzenie] = await _przeslij(tmp_path, [_odmowa_payload()])

    assert set(zdarzenie) == KLUCZE_WYSYLKI
    assert ZNACZNIK_POWODU not in json.dumps(zdarzenie)


async def test_pierwsza_odmowa_komendy_w_kroku_nie_daje_ostrzezenia_o_powtorzeniu(tmp_path):
    wyslane = await _przeslij(tmp_path, [_odmowa_payload()])

    assert _rodzaje(wyslane) == ["hook:ToolDenied"]


async def test_druga_identyczna_odmowa_w_kroku_daje_dodatkowe_ostrzezenie_o_powtorzeniu(tmp_path):
    wyslane = await _przeslij(tmp_path, [_odmowa_payload(), _odmowa_payload()])

    assert _rodzaje(wyslane) == ["hook:ToolDenied", "hook:ToolDenied", "hook:ToolDeniedRepeat"]
    [powtorzenie] = [w for w in wyslane if w["kind"] == "hook:ToolDeniedRepeat"]
    assert powtorzenie["text"].startswith(PREFIKS_POWTORZENIA)
    assert "git push" in powtorzenie["text"]
    assert powtorzenie["run_id"] == RUN
    assert powtorzenie["step_id"] == KROK
    assert set(powtorzenie) == KLUCZE_WYSYLKI


async def test_kazda_kolejna_identyczna_odmowa_dodaje_ostrzezenie(tmp_path):
    wyslane = await _przeslij(tmp_path, [_odmowa_payload() for _ in range(4)])

    assert _rodzaje(wyslane).count("hook:ToolDenied") == 4
    assert _rodzaje(wyslane).count("hook:ToolDeniedRepeat") == 3


async def test_rozne_komendy_tego_samego_narzedzia_nie_licza_sie_jako_powtorzenie(tmp_path):
    wyslane = await _przeslij(
        tmp_path, [_odmowa_payload(komenda="git push"), _odmowa_payload(komenda="git commit")]
    )

    assert _rodzaje(wyslane) == ["hook:ToolDenied", "hook:ToolDenied"]


async def test_ta_sama_komenda_w_roznych_narzedziach_nie_liczy_sie_jako_powtorzenie(tmp_path):
    wyslane = await _przeslij(
        tmp_path,
        [_odmowa_payload("Write", "/obce/x.py"), _odmowa_payload("Edit", "/obce/x.py")],
    )

    assert _rodzaje(wyslane) == ["hook:ToolDenied", "hook:ToolDenied"]


async def test_licznik_powtorzen_jest_osobny_dla_kazdego_kroku(tmp_path):
    w_kroku_a = await _przeslij(tmp_path, [_odmowa_payload()], krok="krok-a")
    w_kroku_b = await _przeslij(tmp_path, [_odmowa_payload()], krok="krok-b")

    assert _rodzaje(w_kroku_a) == ["hook:ToolDenied"]
    assert _rodzaje(w_kroku_b) == ["hook:ToolDenied"]


async def test_powtarzajace_sie_zdarzenia_innego_rodzaju_nie_daja_ostrzezenia_o_odmowie(tmp_path):
    zwykle = {
        "session_id": "sesja-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git push"},
    }

    wyslane = await _przeslij(tmp_path, [zwykle, zwykle])

    assert _rodzaje(wyslane) == ["hook:PreToolUse", "hook:PreToolUse"]


# --- atrapa claude: hook z pliku --settings, skutek w danych -------------------------------

KOD_ATRAPY_CLAUDE = r"""
import json, subprocess, sys

ster = json.loads(sys.argv[-1])
argv = sys.argv[1:-1]
with open(argv[argv.index("--settings") + 1]) as f:
    ustawienia = json.load(f)
grupy = [g for g in ustawienia["hooks"]["PreToolUse"] if g.get("matcher") == ster["matcher"]]
polecenie = grupy[0]["hooks"][0]["command"]
wyniki = []
for proba in ster["proby"]:
    narzedzie, wejscie = proba["narzedzie"], proba["wejscie"]
    odmowa = False
    if not proba.get("bez_strazy"):
        zdarzenie = {
            "session_id": "sesja-atrapy",
            "hook_event_name": "PreToolUse",
            "tool_name": narzedzie,
            "tool_input": wejscie,
            "cwd": ster["worktree"],
        }
        h = subprocess.run(
            ["sh", "-c", polecenie], input=json.dumps(zdarzenie), capture_output=True,
            text=True, cwd=ster["korzen"], timeout=60,
        )
        if h.stdout.strip():
            odmowa = json.loads(h.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    if not odmowa:
        if narzedzie == "Bash":
            subprocess.run(["sh", "-c", wejscie["command"]], cwd=ster["worktree"], timeout=60)
        else:
            with open(wejscie["file_path"], "w") as f:
                f.write(wejscie["content"])
    wyniki.append({"odmowa": odmowa})
with open(ster["wynik"], "w") as f:
    json.dump({"argv": argv, "proby": wyniki}, f)
"""


def _bash_proba(komenda: str, **dodatki) -> dict:
    return {"narzedzie": "Bash", "wejscie": {"command": komenda}, **dodatki}


def _write_proba(sciezka: Path, **dodatki) -> dict:
    return {
        "narzedzie": "Write",
        "wejscie": {"file_path": str(sciezka), "content": "tresc"},
        **dodatki,
    }


def _git(repo: Path, *args: str) -> str:
    proc = uruchom_proces_sync(
        ["git", *args], cwd=repo, stdout=PIPE, stderr=PIPE, text=True, env=dict(os.environ)
    )
    try:
        wyjscie, bledy = proc.communicate(timeout=LIMIT_HOOKA)
    finally:
        zakoncz_proces_sync(proc, nazwa="git")
    assert proc.returncode == 0, bledy
    return wyjscie.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "dom"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    katalog = tmp_path / "repo"
    katalog.mkdir()
    _git(katalog, "init", "-q")
    _git(katalog, "config", "user.email", "atrapa@example.invalid")
    _git(katalog, "config", "user.name", "Atrapa")
    _git(katalog, "config", "commit.gpgsign", "false")
    _git(katalog, "commit", "--allow-empty", "-q", "-m", "poczatek")
    return katalog


def _stan_repo(repo: Path) -> tuple[str, str]:
    return _git(repo, "rev-list", "--count", "HEAD"), _git(repo, "rev-parse", "HEAD")


def _spec(rola: Rola) -> SpecKroku:
    return SpecKroku(
        run_id=RUN, step_id=KROK, agent_id="agent-1", skill_id="pisanie", rola=rola,
        wejscia=[], wyjscia=[], komendy={},
    )


async def _uruchom_krok(tmp_path: Path, repo: Path, rola: Rola, proby: list[dict]):
    """Krok z atrapa `claude` przez wykonaj_krok; zwraca (WynikKroku, lista odmow atrapy, plik hookow)."""
    stan = tmp_path / "stan"
    plik = plik_hookow(stan, RUN, KROK)
    ustawienia = plik_ustawien(
        stan, RUN, KROK, ustawienia_hookow(plik, rola=rola, worktree=repo)
    )
    wynik_atrapy = tmp_path / "wynik-atrapy.json"
    sterowanie = {
        "matcher": MATCHER_STRAZY,
        "worktree": str(repo),
        "korzen": str(KORZEN),
        "wynik": str(wynik_atrapy),
        "proby": proby,
    }
    argv = polecenie_agenta(
        _spec(rola), ProfilBackendu.z_srodowiska({}), ustawienia=ustawienia
    )
    polecenie = [sys.executable, "-I", "-c", KOD_ATRAPY_CLAUDE, *argv[1:], json.dumps(sterowanie)]

    async def emit(zdarzenie: dict) -> None:
        pass

    wynik = await asyncio.wait_for(wykonaj_krok(_spec(rola), polecenie, repo, emit), LIMIT_KROKU)
    odpowiedz = json.loads(wynik_atrapy.read_text())
    return wynik, [p["odmowa"] for p in odpowiedz["proby"]], plik


async def test_wykonawca_probujacy_commitowac_dostaje_odmowe_i_repozytorium_nie_dostaje_commita(
    tmp_path, repo
):
    przed = _stan_repo(repo)

    wynik, odmowy, _ = await _uruchom_krok(
        tmp_path, repo, Rola.executor,
        [_bash_proba("git commit --allow-empty -m proba-agenta")],
    )

    assert wynik.status == "zakonczony"
    assert odmowy == [True]
    assert _stan_repo(repo) == przed


async def test_ten_sam_commit_bez_strazy_powstaje_wiec_odmowa_jest_zasluga_hooka(tmp_path, repo):
    przed_liczba, przed_glowa = _stan_repo(repo)

    _, odmowy, _ = await _uruchom_krok(
        tmp_path, repo, Rola.executor,
        [_bash_proba("git commit --allow-empty -m proba-agenta", bez_strazy=True)],
    )

    po_liczba, po_glowa = _stan_repo(repo)
    assert odmowy == [False]
    assert int(po_liczba) == int(przed_liczba) + 1
    assert po_glowa != przed_glowa


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
async def test_rola_z_zapisem_wykonuje_dozwolone_polecenie_i_zapis_w_worktree_mimo_strazy(
    tmp_path, repo, rola
):
    wynik, odmowy, _ = await _uruchom_krok(
        tmp_path, repo, rola,
        [
            _bash_proba("touch dozwolone.txt"),
            _bash_proba("echo x > przekierowane.txt"),
            _write_proba(repo / "zapisane.md"),
        ],
    )

    assert wynik.status == "zakonczony"
    assert odmowy == [False, False, False]
    assert (repo / "dozwolone.txt").is_file()
    assert (repo / "przekierowane.txt").is_file()
    assert (repo / "zapisane.md").read_text() == "tresc"


async def test_odmowa_commita_trafia_do_pliku_hookow_jako_zdarzenie_runu(
    tmp_path, repo, monkeypatch
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    komenda = "git commit --allow-empty -m proba-agenta"

    await _uruchom_krok(tmp_path, repo, Rola.executor, [_bash_proba(komenda)])

    [zdarzenie] = CzytnikHookow(plik, RUN, KROK).odczytaj_nowe()
    assert zdarzenie["event"] == "ToolDenied"
    assert zdarzenie["tool"] == "Bash"
    assert do_wysylki(zdarzenie, RUN, KROK)["text"] == f"Bash: {komenda} [role=executor]"


async def test_recenzent_nie_zapisze_pliku_ani_przez_write_ani_przez_bash(tmp_path, repo):
    wynik, odmowy, _ = await _uruchom_krok(
        tmp_path, repo, Rola.reviewer,
        [
            _write_proba(repo / "przez-write.md"),
            _bash_proba("echo x > przez-bash.txt"),
            _bash_proba("ls"),
        ],
    )

    assert odmowy == [True, True, False]
    assert not (repo / "przez-write.md").exists()
    assert not (repo / "przez-bash.txt").exists()
    assert wynik.status == "zakonczony"
    assert wynik.powod is None


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
async def test_zapis_poza_worktree_przez_agenta_nie_tworzy_pliku_w_zadnej_roli(
    tmp_path, repo, rola
):
    obcy = tmp_path / "obcy"
    obcy.mkdir()

    _, odmowy, _ = await _uruchom_krok(
        tmp_path, repo, rola, [_write_proba(obcy / "x.txt")]
    )

    assert odmowy == [True]
    assert list(obcy.iterdir()) == []


# --- mutanty zmian HAR-175: dokladne powody odmowy, zdarzenie odmowy i uruchomienie jako skrypt ---

KOMUNIKAT_ARGUMENTOW = "straż roli: wymagane argumenty: --rola <rola> --worktree <ścieżka>"
POWOD_RECENZENTA = (
    "straż roli: niezmiennik reviewer_readonly: rola {rola} "
    "uruchamia tylko polecenia odczytu bez metaznaków powłoki"
)
POWOD_GITA = (
    "straż roli: niezmiennik no_git_write: silnik nie zmienia historii ani drzewa gita ({git})"
)


def _powod_main(monkeypatch, capfd, dane, argv) -> str:
    _stdin(monkeypatch, dane)
    assert main(argv) == 0
    wynik = json.loads(capfd.readouterr().out)["hookSpecificOutput"]
    assert wynik["permissionDecision"] == "deny"
    return wynik["permissionDecisionReason"]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="bez-argumentow"),
        pytest.param(["--rola", "executor", "--worktree"], id="trzy-argumenty"),
        pytest.param(["--foo", "x", "--bar", "y"], id="obce-flagi"),
        pytest.param(["--rola", "a", "--rola", "b"], id="dwa-razy-rola"),
        pytest.param(["--rola", "executor", "--worktree", "/tmp", "nadmiar"], id="pieciu"),
    ],
)
def test_zle_argumenty_hooka_daja_dokladny_komunikat_o_wymaganych_argumentach(
    monkeypatch, capfd, argv
):
    powod = _powod_main(monkeypatch, capfd, json.dumps(_zd("Read", {})), argv)

    assert powod == KOMUNIKAT_ARGUMENTOW


def test_nieznana_rola_w_argumentach_daje_komunikat_z_nazwa_roli(monkeypatch, capfd, worktree):
    powod = _powod_main(
        monkeypatch, capfd, json.dumps(_zd("Read", {})),
        ["--rola", "czarodziej", "--worktree", str(worktree)],
    )

    assert powod == "straż roli: nieznana rola 'czarodziej'"


def test_stdin_bez_json_w_utf8_daje_dokladny_komunikat_o_nieczytelnym_wejsciu(
    monkeypatch, capfd, worktree
):
    powod = _powod_main(
        monkeypatch, capfd, "to nie jest json",
        ["--rola", "executor", "--worktree", str(worktree)],
    )

    assert powod == "straż roli: nieczytelne wejście hooka (stdin nie jest JSON w UTF-8)"


def test_stdin_z_json_niebedacym_obiektem_daje_dokladny_komunikat(monkeypatch, capfd, worktree):
    powod = _powod_main(
        monkeypatch, capfd, "[]", ["--rola", "executor", "--worktree", str(worktree)]
    )

    assert powod == "straż roli: wejście hooka nie jest obiektem JSON"


def test_wyjatek_w_decyzji_daje_komunikat_z_nazwa_klasy_wyjatku(monkeypatch, capfd, worktree):
    def zepsuta(*args, **kwargs):
        raise RuntimeError("awaria wewnetrzna")

    monkeypatch.setattr(straz_roli, "decyzja", zepsuta)

    powod = _powod_main(
        monkeypatch, capfd, json.dumps(_bash("ls", cwd=worktree)),
        ["--rola", "executor", "--worktree", str(worktree)],
    )

    assert powod == "straż roli: nieoczekiwany błąd (RuntimeError)"


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
def test_odmowa_roli_bez_zapisu_ma_dokladny_powod_reviewer_readonly(rola, worktree):
    powod = decyzja(_bash("rm x", cwd=worktree), rola, worktree)

    assert powod == POWOD_RECENZENTA.format(rola=rola.value)


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_odmowa_checkout_ma_dokladny_powod_z_nazwa_zakazanej_komendy(rola, worktree):
    powod = decyzja(_bash("git checkout main", cwd=worktree), rola, worktree)

    assert powod == POWOD_GITA.format(git="git checkout")


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_odmowa_reset_hard_ma_dokladny_powod_z_nazwa_zakazanej_komendy(rola, worktree):
    powod = decyzja(_bash("git reset --hard HEAD~1", cwd=worktree), rola, worktree)

    assert powod == POWOD_GITA.format(git="git reset --hard")


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_flaga_hard_przed_podpoleceniem_reset_nie_jest_resetem_hard(rola, worktree):
    # "--hard" stoi PRZED "reset": tylko znaki po podpolecenii decyduja o zakazie
    assert decyzja(_bash("git --hard reset HEAD", cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param('ls "abc', id="niezamkniety-cudzyslow"),
        pytest.param("cat 'abc", id="niezamkniety-apostrof"),
        pytest.param("", id="pusta-komenda"),
        pytest.param("   ", id="same-biale-znaki"),
    ],
)
def test_rola_bez_zapisu_nie_uruchomi_polecenia_ktorego_nie_umie_ocenic(rola, komenda, worktree):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_RECENZENTA.format(rola=rola.value)


# zdarzenie odmowy zapisywane przez main

def _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie, rola="executor") -> dict:
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    _wywolaj_main(monkeypatch, capfd, json.dumps(zdarzenie), rola, worktree)
    [linia] = _linie(plik)
    return linia


@pytest.mark.parametrize("sesja", [5, None, ["s"]])
def test_sesja_niebedaca_napisem_trafia_do_zdarzenia_odmowy_jako_pusty_napis(
    monkeypatch, capfd, tmp_path, worktree, sesja
):
    zdarzenie = _bash("git push", cwd=worktree) | {"session_id": sesja}

    linia = _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie)

    assert linia["session_id"] == ""


def test_brak_sesji_w_zdarzeniu_daje_w_zdarzeniu_odmowy_pusty_napis(
    monkeypatch, capfd, tmp_path, worktree
):
    zdarzenie = _bash("git push", cwd=worktree)
    del zdarzenie["session_id"]

    linia = _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie)

    assert linia["session_id"] == ""


@pytest.mark.parametrize(
    "wejscie",
    [
        pytest.param({"command": 5}, id="komenda-liczba"),
        pytest.param("git push", id="tool-input-napis"),
        pytest.param(None, id="tool-input-null"),
        pytest.param([], id="tool-input-lista"),
    ],
)
def test_odmowa_bashu_bez_komendy_w_postaci_napisu_ma_w_zdarzeniu_pusta_komende(
    monkeypatch, capfd, tmp_path, worktree, wejscie
):
    zdarzenie = _zd("Bash", wejscie, cwd=worktree)

    linia = _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie)

    assert linia["tool_name"] == "Bash"
    assert linia["command"] == ""


def test_odmowa_awaryjna_dla_nieznanego_narzedzia_nie_wynosi_zadnego_pola_wejscia(
    monkeypatch, capfd, tmp_path, worktree
):
    def zepsuta(*args, **kwargs):
        raise RuntimeError("awaria")

    monkeypatch.setattr(straz_roli, "decyzja", zepsuta)
    zdarzenie = _zd("Read", {"XXXX": "x", "file_path": "y", "command": "z"}, cwd=worktree)

    linia = _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie)

    assert linia["tool_name"] == "Read"
    assert linia["command"] == ""


def test_blad_zapisu_pliku_hookow_jest_zgloszony_na_stderr_z_nazwa_klasy_bledu(
    monkeypatch, capfd, tmp_path, worktree
):
    katalog = tmp_path / "to-katalog"
    katalog.mkdir()
    monkeypatch.setenv(ZMIENNA_PLIKU, str(katalog))

    _stdin(monkeypatch, json.dumps(_bash("git push", cwd=worktree)))

    assert main(["--rola", "executor", "--worktree", str(worktree)]) == 0

    # jedno readouterr() po main: helper _wywolaj_main zjadlby stderr
    assert capfd.readouterr().err == "straż roli: nie zapisano zdarzenia odmowy: IsADirectoryError\n"


def test_nieczytelne_wejscie_nie_dopisuje_zdarzenia_odmowy_ani_nie_przerywa_hooka(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))

    kod, wyjscie = _wywolaj_main(monkeypatch, capfd, "to nie jest json", "executor", worktree)

    assert kod == 0
    assert json.loads(wyjscie)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert plik.read_bytes() == b""


def test_zle_argumenty_nie_dopisuja_zdarzenia_odmowy(monkeypatch, capfd, tmp_path):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    _stdin(monkeypatch, json.dumps(_bash("git push")))

    assert main(["--rola", "czarodziej", "--worktree", "/tmp"]) == 0

    assert json.loads(capfd.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert plik.read_bytes() == b""


# uruchomienie pliku jako skrypt i jako modul pakietu

def _zaladuj_straz_jako(nazwa: str) -> None:
    spec = importlib.util.spec_from_file_location(nazwa, STRAZ)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)


def test_straz_uruchomiona_jako_skrypt_dodaje_katalog_src_na_poczatek_sciezek_importu(
    monkeypatch,
):
    monkeypatch.setattr(sys, "path", ["/nic"])

    _zaladuj_straz_jako("straz_jako_skrypt")

    assert sys.path == [str(STRAZ.resolve().parent.parent), "/nic"]


def test_straz_zaladowana_jako_modul_pakietu_nie_zmienia_sciezek_importu(monkeypatch):
    monkeypatch.setattr(sys, "path", ["/nic"])

    _zaladuj_straz_jako("runner.straz_roli_kopia")

    assert sys.path == ["/nic"]


# tekst zdarzenia odmowy z niepelnych albo zepsutych danych

def _ev_odmowy(**pola) -> dict:
    return {"event": "ToolDenied", "ts": 1.0, "session_id": "s", **pola}


@pytest.mark.parametrize(
    "ev",
    [
        pytest.param(_ev_odmowy(tool="Bash"), id="bez-extra"),
        pytest.param(_ev_odmowy(tool="Bash", extra="nie-slownik"), id="extra-napis"),
        pytest.param(_ev_odmowy(tool="Bash", extra={}), id="extra-pusty"),
        pytest.param(
            _ev_odmowy(tool="Bash", extra={"command": None, "role": None}), id="pola-null"
        ),
    ],
)
def test_odmowa_bez_komendy_i_roli_ma_tekst_z_pustymi_polami_a_nie_wyjatek(ev):
    assert do_wysylki(ev, RUN, KROK)["text"] == "Bash:  [role=]"


def test_odmowa_bez_narzedzia_komendy_i_roli_ma_tekst_z_samymi_pustymi_polami():
    assert do_wysylki(_ev_odmowy(), RUN, KROK)["text"] == ":  [role=]"


@pytest.mark.parametrize("pole", ["command", "role"])
@pytest.mark.parametrize("wartosc", [5, ["a"], {"a": 1}, True])
def test_odmowa_z_komenda_albo_rola_niebedaca_napisem_jest_odrzucona_z_nazwa_pola(pole, wartosc):
    ev = _ev_odmowy(tool="Bash", extra={"command": "git push", "role": "executor", pole: wartosc})

    with pytest.raises(BladZdarzeniaHooka) as wyjatek:
        do_wysylki(ev, RUN, KROK)

    assert wyjatek.value.pole == pole


# --- HAR-219: awaria startu straży to odmowa (fail-closed) ----------------------------------

POWOD_BLEDU_STARTU = "straż roli: błąd startu straży ({klasa})"
KOMUNIKAT_AWARII = "straż roli: awaria straży (kod {kod})"


def test_straz_bez_bledu_importu_ma_pusty_blad_startu():
    assert straz_roli._BLAD_STARTU is None


@pytest.mark.parametrize("blad", [ModuleNotFoundError("x"), ImportError("x"), RuntimeError("x")])
@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_blad_startu_strazy_daje_odmowe_z_nazwa_klasy_wyjatku_nawet_dla_dozwolonego_polecenia(
    monkeypatch, capfd, worktree, rola, blad
):
    monkeypatch.setattr(straz_roli, "_BLAD_STARTU", blad)

    powod = _powod_main(
        monkeypatch, capfd, json.dumps(_bash("ls", cwd=worktree)),
        ["--rola", rola.value, "--worktree", str(worktree)],
    )

    assert powod == POWOD_BLEDU_STARTU.format(klasa=type(blad).__name__)


def test_blad_startu_strazy_nie_dopisuje_zdarzenia_odmowy_do_pliku_hookow(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    monkeypatch.setattr(straz_roli, "_BLAD_STARTU", ModuleNotFoundError("x"))

    _wywolaj_main(monkeypatch, capfd, json.dumps(_bash("ls", cwd=worktree)), "executor", worktree)

    assert plik.read_bytes() == b""


def test_blad_startu_strazy_to_json_deny_hooka_pretooluse_z_kodem_zero(
    monkeypatch, capfd, worktree
):
    monkeypatch.setattr(straz_roli, "_BLAD_STARTU", ModuleNotFoundError("x"))

    kod, wyjscie = _wywolaj_main(
        monkeypatch, capfd, json.dumps(_bash("ls", cwd=worktree)), "executor", worktree
    )

    assert kod == 0
    assert json.loads(wyjscie) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "straż roli: błąd startu straży (ModuleNotFoundError)",
    }}


def _kopia_strazy_bez_pakietu_cockpit(tmp_path: Path) -> Path:
    """Kopia skryptu straży obok pakietu `cockpit` bez `invariants` (import się nie uda).

    `-I` ignoruje PYTHONPATH, a skrypt dokłada do `sys.path` katalog nad `runner/`, więc
    zepsuty import robimy kopią, nie zmienną środowiskową.
    """
    katalog = tmp_path / "kopia"
    (katalog / "runner").mkdir(parents=True)
    (katalog / "cockpit" / "services").mkdir(parents=True)
    (katalog / "cockpit" / "__init__.py").write_text("")
    (katalog / "cockpit" / "services" / "__init__.py").write_text("")
    skrypt = katalog / "runner" / "straz_roli.py"
    skrypt.write_bytes(STRAZ.read_bytes())
    return skrypt


def _polecenie_z_innym_skryptem(polecenie: str, skrypt: Path) -> str:
    stary = shlex.quote(str(hooks._STRAZ_ROLI))
    assert polecenie.count(stary) == 1
    return polecenie.replace(stary, shlex.quote(str(skrypt)))


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_zepsuty_import_strazy_uruchomionej_bezposrednio_daje_json_deny_z_nazwa_wyjatku(
    tmp_path, worktree, rola
):
    skrypt = _kopia_strazy_bez_pakietu_cockpit(tmp_path)
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)

    wynik = _uruchom(
        [sys.executable, "-I", str(skrypt), "--rola", rola.value, "--worktree", str(worktree)],
        json.dumps(_bash("ls", cwd=worktree)),
        plik,
    )

    powod = _odmowa(wynik)["permissionDecisionReason"]
    assert powod == "straż roli: błąd startu straży (ModuleNotFoundError)"
    assert plik.read_bytes() == b""


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_zepsuty_import_strazy_daje_odmowe_narzedzia_takze_przez_polecenie_hooka_z_ustawien(
    tmp_path, worktree, rola
):
    skrypt = _kopia_strazy_bez_pakietu_cockpit(tmp_path)
    polecenie = _polecenie_z_innym_skryptem(_polecenie_strazy(tmp_path, rola, worktree), skrypt)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("ls", cwd=worktree)))

    powod = _odmowa(wynik)["permissionDecisionReason"]
    assert powod == "straż roli: błąd startu straży (ModuleNotFoundError)"


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_brak_interpretera_strazy_daje_kod_2_i_komunikat_awarii_kod_127_bez_json_deny(
    tmp_path, worktree, rola
):
    polecenie = hooks._polecenie_strazy(rola, worktree, wykonywalny="/nieistniejacy/python")

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("ls", cwd=worktree)))

    assert wynik.returncode == 2
    assert wynik.stdout == ""
    assert wynik.stderr.splitlines()[-1] == KOMUNIKAT_AWARII.format(kod=127)


def test_straz_konczaca_sie_kodem_1_daje_kod_2_i_dokladnie_komunikat_awarii_na_stderr(
    tmp_path, worktree
):
    # `false` ignoruje argumenty i wychodzi kodem 1: tylko galaz powloki moze zablokowac narzedzie
    polecenie = hooks._polecenie_strazy(Rola.executor, worktree, wykonywalny="false")

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("ls", cwd=worktree)))

    assert wynik.returncode == 2
    assert wynik.stdout == ""
    assert wynik.stderr == KOMUNIKAT_AWARII.format(kod=1) + "\n"


def test_straz_z_bledem_skladni_w_skrypcie_daje_kod_2_i_komunikat_awarii_kod_1(
    tmp_path, worktree
):
    # blad skladni nie ma jak zostac obsluzony w module: zostaje galaz powloki
    skrypt = tmp_path / "zepsuty" / "straz_roli.py"
    skrypt.parent.mkdir()
    skrypt.write_text("def (:\n")
    polecenie = _polecenie_z_innym_skryptem(
        _polecenie_strazy(tmp_path, Rola.executor, worktree), skrypt
    )

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("ls", cwd=worktree)))

    assert wynik.returncode == 2
    assert wynik.stdout == ""
    assert wynik.stderr.splitlines()[-1] == KOMUNIKAT_AWARII.format(kod=1)


def test_sprawna_straz_przepuszczajaca_polecenie_nie_uruchamia_galezi_awarii(tmp_path, worktree):
    polecenie = _polecenie_strazy(tmp_path, Rola.executor, worktree)

    wynik = _uruchom_polecenie(polecenie, json.dumps(_bash("ls", cwd=worktree)))

    assert (wynik.returncode, wynik.stdout, wynik.stderr) == (0, "", "")


# --- HAR-219: git tworzący commity i aliasy gita -----------------------------------------------

POWOD_COMMIT = "straż roli: no_git_write — git {podpolecenie} tworzy commit"
POWOD_ALIASU = "straż roli: no_git_write — alias gita przez -c"

# Lista niezalezna od `_GIT_TWORZY_COMMIT`: kazdy element zbioru ma wlasny przypadek.
GIT_TWORZY_COMMIT = [
    pytest.param("git merge feature", "merge", id="merge"),
    pytest.param("git cherry-pick abc123", "cherry-pick", id="cherry-pick"),
    pytest.param("git revert abc123", "revert", id="revert"),
    pytest.param("git rebase main", "rebase", id="rebase"),
    pytest.param("git am poprawka.patch", "am", id="am"),
    pytest.param("git stash", "stash", id="stash"),
    pytest.param("git stash pop", "stash", id="stash-pop"),
    pytest.param("git -C katalog merge feature", "merge", id="merge-po-opcji-C"),
    pytest.param("git --no-pager rebase main", "rebase", id="rebase-po-opcji-bez-argumentu"),
    pytest.param("git -c user.name=a am p.patch", "am", id="am-po-opcji-c-bez-aliasu"),
    pytest.param("/usr/bin/git cherry-pick abc", "cherry-pick", id="cherry-pick-pelna-sciezka"),
    pytest.param("ls && git merge x", "merge", id="merge-za-separatorem"),
    pytest.param("ls;git revert abc", "revert", id="revert-za-srednikiem"),
    pytest.param('bash -c "git rebase main"', "rebase", id="rebase-w-bash-c"),
    pytest.param('"g"it merge x', "merge", id="merge-cudzyslow-w-nazwie-gita"),
    pytest.param("g'it' rebase main", "rebase", id="rebase-apostrof-w-nazwie-gita"),
    pytest.param("'git' cherry-pick abc", "cherry-pick", id="cherry-pick-nazwa-w-apostrofach"),
    pytest.param('"git" revert abc', "revert", id="revert-nazwa-w-cudzyslowach"),
    pytest.param('git "merge" x', "merge", id="merge-podpolecenie-w-cudzyslowach"),
    pytest.param("git 'am' p.patch", "am", id="am-podpolecenie-w-apostrofach"),
    pytest.param('git "stash"', "stash", id="stash-podpolecenie-w-cudzyslowach"),
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("komenda,podpolecenie", GIT_TWORZY_COMMIT)
def test_git_tworzacy_commit_jest_odmowiony_w_kazdej_roli_z_dokladnym_powodem(
    rola, komenda, podpolecenie, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_COMMIT.format(podpolecenie=podpolecenie)


# commit i push lapie tez `waliduj_definicje` (inny komunikat), wiec tu tylko niezmiennik
GIT_COMMIT_I_PUSH_W_CUDZYSLOWACH = [
    pytest.param('"g"it commit -m x', id="commit-cudzyslow-w-nazwie-gita"),
    pytest.param("g'it' push", id="push-apostrof-w-nazwie-gita"),
    pytest.param("'git' push origin main", id="push-nazwa-w-apostrofach"),
    pytest.param('"git" commit -m x', id="commit-nazwa-w-cudzyslowach"),
    pytest.param('git "commit" -m x', id="commit-podpolecenie-w-cudzyslowach"),
    pytest.param("git 'push'", id="push-podpolecenie-w-apostrofach"),
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("komenda", GIT_COMMIT_I_PUSH_W_CUDZYSLOWACH)
def test_commit_i_push_z_cudzyslowami_w_nazwie_gita_sa_odmowione_w_kazdej_roli(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert "no_git_write" in powod


GIT_ALIAS_PRZEZ_C = [
    pytest.param("git -c alias.c=commit c -m x", id="alias-na-commit"),
    pytest.param("git -calias.c=commit c", id="alias-sklejony-z-opcja"),
    pytest.param("git -c alias.st=status st", id="alias-na-bezpieczne-polecenie"),
    pytest.param('git -c "alias.c=commit" c', id="alias-w-cudzyslowach"),
    pytest.param("git -c user.name=a -c alias.c=commit c", id="alias-po-innej-opcji-c"),
    pytest.param("git -C d -c alias.c=commit c", id="alias-po-opcji-C"),
    pytest.param("ls && git -c alias.x=log x", id="alias-za-separatorem"),
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("komenda", GIT_ALIAS_PRZEZ_C)
def test_alias_gita_ustawiony_przez_opcje_c_jest_odmowiony_w_kazdej_roli(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_ALIASU


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "git status",
        "git diff",
        "git log",
        "git add plik.py",
        "echo git is great",
        "git -c user.name=a status",
        "git -c core.alias.x=1 status",
        "git -c notalias.c=x status",
        "git log --grep=merge",
        "git log merge",
        "git branch merge-fix",
        "grep -rn merge src",
    ],
)
def test_polecenia_niebedace_commitem_ani_aliasem_nie_sa_odmawiane_przez_nowe_reguly_gita(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda", ["git status", "git diff", "git log", "git show HEAD"])
def test_odczyt_gita_w_roli_bez_zapisu_nie_jest_odmawiany_przez_nowe_reguly_gita(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


# --- HAR-219: przekierowania Basha poza worktree w roli z zapisem (decyzja: odmowa) ----------

POWOD_POZA_WORKTREE = "straż roli: przekierowanie poza worktree: "
POWOD_CEL_NIEOCENIANY = "straż roli: przekierowanie do celu, którego nie da się ocenić: "

FORMY_PRZEKIEROWANIA = [
    pytest.param("echo x > {c}", id="wyjscie"),
    pytest.param("echo x >> {c}", id="dopisanie"),
    pytest.param("echo x 2> {c}", id="stderr"),
    pytest.param("echo x 2>> {c}", id="stderr-dopisanie"),
    pytest.param("echo x 1> {c}", id="stdout-jawnie"),
    pytest.param("echo x &> {c}", id="oba-strumienie"),
    pytest.param("echo x &>> {c}", id="oba-strumienie-dopisanie"),
    pytest.param("echo x >| {c}", id="wymuszenie-nadpisania"),
    pytest.param("echo x >{c}", id="wyjscie-sklejone"),
    pytest.param("echo x >>{c}", id="dopisanie-sklejone"),
    pytest.param("echo x 2>{c}", id="stderr-sklejone"),
    pytest.param("echo x 2>>{c}", id="stderr-dopisanie-sklejone"),
    pytest.param("echo x &>{c}", id="oba-strumienie-sklejone"),
    pytest.param("echo x &>>{c}", id="oba-strumienie-dopisanie-sklejone"),
    pytest.param("echo x >|{c}", id="wymuszenie-nadpisania-sklejone"),
    pytest.param("echo x 2>&1 > {c}", id="po-przekierowaniu-deskryptora"),
    pytest.param("ls | tee a > {c}", id="za-rura"),
    pytest.param("ls && echo x > {c}", id="za-separatorem"),
    pytest.param("echo x > plik >> {c}", id="drugie-przekierowanie-poza"),
    pytest.param("echo x > plik 2> {c}", id="stderr-poza-gdy-stdout-w-worktree"),
]


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("forma", FORMY_PRZEKIEROWANIA)
def test_przekierowanie_basha_poza_worktree_jest_odmowione_z_dokladnym_powodem(
    rola, forma, worktree, obcy
):
    cel = str(obcy / "plik")

    powod = decyzja(_bash(forma.format(c=cel), cwd=worktree), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + cel


CELE_NIEOCENIANE = [
    pytest.param("$HOME/plik", id="zmienna"),
    pytest.param("`pwd`/plik", id="podstawienie-polecenia"),
    pytest.param("~/plik", id="tylda"),
    pytest.param("*.txt", id="gwiazdka"),
    pytest.param("a?.txt", id="znak-zapytania"),
]


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("cel", CELE_NIEOCENIANE)
def test_przekierowanie_do_celu_ktorego_nie_da_sie_ocenic_jest_odmowione(rola, cel, worktree):
    powod = decyzja(_bash(f"echo x > {cel}", cwd=worktree), rola, worktree)

    assert powod == POWOD_CEL_NIEOCENIANY + cel


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "echo x > plik",
        "echo x >plik",
        "echo x > sub/plik",
        "echo x > ./plik",
        "echo x > sub/../plik",
        "echo x >> plik",
        "echo x 2> blad.log",
        "echo x &> wynik.log",
        "echo x >| plik",
        "ls > /dev/null",
        "ls 2> /dev/null",
        "ls &> /dev/null",
        "ls > /dev/null 2>&1",
        "ls 2>&1",
        "ls >&2",
        "mkdir -p out && echo x > out/plik",
        "cat a | tee b",
    ],
)
def test_przekierowania_w_worktree_do_dev_null_i_deskryptorow_sa_dozwolone(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_przekierowanie_do_bezwzglednej_sciezki_w_worktree_jest_dozwolone(rola, worktree):
    komenda = f"echo x > {worktree}/plik"

    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_wzgledny_cel_z_kropkami_wychodzacy_z_worktree_jest_liczony_od_cwd_i_odmowiony(
    rola, worktree
):
    powod = decyzja(_bash("echo x > ../poza", cwd=worktree), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + "../poza"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_cwd_ze_zdarzenia_decyduje_o_celu_wzglednym_ten_sam_tekst_w_podkatalogu_jest_dozwolony(
    rola, worktree
):
    podkatalog = worktree / "sub"
    podkatalog.mkdir()
    komenda = "echo x > ../plik"

    assert decyzja(_bash(komenda, cwd=podkatalog), rola, worktree) is None
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) == (
        POWOD_POZA_WORKTREE + "../plik"
    )


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_cwd_poza_worktree_robi_z_celu_wzglednego_przekierowanie_poza_worktree(
    rola, worktree, obcy
):
    powod = decyzja(_bash("echo x > plik", cwd=obcy), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + "plik"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_bez_cwd_w_zdarzeniu_cel_wzgledny_jest_liczony_od_worktree(rola, worktree):
    assert decyzja(_bash("echo x > plik"), rola, worktree) is None
    assert decyzja(_bash("echo x > ../poza"), rola, worktree) == POWOD_POZA_WORKTREE + "../poza"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_przekierowanie_przez_dowiazanie_katalogu_wychodzace_z_worktree_jest_odmowione(
    rola, worktree, obcy
):
    (worktree / "link").symlink_to(obcy, target_is_directory=True)

    powod = decyzja(_bash("echo x > link/plik", cwd=worktree), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + "link/plik"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_przekierowanie_przez_dowiazanie_pliku_wychodzace_z_worktree_jest_odmowione(
    rola, worktree, obcy
):
    (worktree / "ln").symlink_to(obcy / "plik")

    powod = decyzja(_bash("echo x > ln", cwd=worktree), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + "ln"


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    ["echo x > {obcy}/plik", "ls > /dev/null", "ls 2>&1", "ls >&2"],
)
def test_rola_bez_zapisu_nadal_dostaje_powod_reviewer_readonly_dla_kazdego_przekierowania(
    rola, komenda, worktree, obcy
):
    polecenie = komenda.format(obcy=obcy)

    powod = decyzja(_bash(polecenie, cwd=worktree), rola, worktree)

    assert powod == POWOD_RECENZENTA.format(rola=rola.value)


# --- HAR-219: opcje zapisujące albo uruchamiające polecenia z białej listy --------------------

POWOD_OPCJI = "straż roli: opcja zapisująca albo uruchamiająca polecenie: "

# Opcje podane dokladnie (bez `=`): powod niesie dokladnie ten token.
OPCJE_ZAPISU_DOKLADNE = [
    pytest.param("git diff --output plik", "--output", id="diff-output-osobny-argument"),
    pytest.param("git diff --ou", "--ou", id="diff-sam-prefiks-ou"),
    pytest.param("git log --output plik", "--output", id="log-output"),
    pytest.param("git show --output plik HEAD", "--output", id="show-output"),
    pytest.param("git diff --ext-diff", "--ext-diff", id="diff-ext-diff"),
    pytest.param("git log --ext-diff", "--ext-diff", id="log-ext-diff"),
    pytest.param("git show --ext-diff", "--ext-diff", id="show-ext-diff"),
    pytest.param("git diff --textconv", "--textconv", id="diff-textconv"),
    pytest.param("git log --textconv", "--textconv", id="log-textconv"),
    pytest.param("git show --textconv HEAD", "--textconv", id="show-textconv"),
    pytest.param("git diff HEAD~1 --ext-diff", "--ext-diff", id="opcja-po-argumencie"),
    pytest.param("git log --oneline --textconv", "--textconv", id="opcja-po-innej-opcji"),
    pytest.param("rg --pre cmd foo", "--pre", id="rg-pre-osobny-argument"),
    pytest.param("rg --pre-glob '*.x' foo", "--pre-glob", id="rg-pre-glob-osobny-argument"),
    pytest.param("rg -z foo", "-z", id="rg-z"),
    pytest.param("rg --search-zip foo", "--search-zip", id="rg-search-zip"),
    pytest.param("rg foo -z src", "-z", id="rg-z-po-wzorcu"),
    pytest.param("rg foo src --search-zip", "--search-zip", id="rg-search-zip-na-koncu"),
]


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda,opcja", OPCJE_ZAPISU_DOKLADNE)
def test_opcja_zapisujaca_albo_uruchamiajaca_polecenie_jest_odmowiona_w_roli_bez_zapisu(
    rola, komenda, opcja, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_OPCJI + opcja


# Formy z `=` albo skrotem: powod ma ten sam poczatek; reszta zalezy od zapisu tokenu.
OPCJE_ZAPISU_Z_ROWNA_SIE = [
    pytest.param("git diff --output=plik", id="diff-output-rowna-sie"),
    pytest.param("git log --outp=x", id="log-skrot-outp"),
    pytest.param("git show --out=x HEAD", id="show-skrot-out"),
    pytest.param("git diff --output-indicator-new=x", id="diff-dluzsza-opcja-na-ou"),
    pytest.param("rg --pre=cmd foo", id="rg-pre-rowna-sie"),
    pytest.param("rg --pre-glob=*.x foo", id="rg-pre-glob-rowna-sie"),
]


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda", OPCJE_ZAPISU_Z_ROWNA_SIE)
def test_opcja_zapisujaca_z_wartoscia_po_rowna_sie_albo_skrocona_jest_odmowiona(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod is not None
    assert powod.startswith(POWOD_OPCJI)


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "git diff HEAD~1",
        "git diff --stat",
        "git diff --name-only HEAD~1",
        "git log --oneline -5",
        "git log --pretty=oneline",
        "git log --format=%h",
        "git show HEAD",
        "git show --stat HEAD",
        "rg foo src",
        "rg -n foo src",
        "rg -i --hidden foo",
        "grep -rn foo src",
        "grep -z foo plik",
    ],
)
def test_odczyt_z_bialej_listy_bez_opcji_zapisujacych_pozostaje_dozwolony(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    ["git diff --output=plik", "git diff --ext-diff", "rg --pre cmd foo", "rg -z foo"],
)
def test_opcje_z_bialej_listy_odmawiane_sa_tylko_w_roli_bez_zapisu(rola, komenda, worktree):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


# --- HAR-219: sekret w komendzie zdarzenia ToolDenied zapisywanym przez straż ---------------


def test_zdarzenie_odmowy_w_pliku_hookow_ma_zamaskowany_sekret_w_komendzie(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    komenda = "GH_TOKEN=sekret12345 git push"

    _wywolaj_main(monkeypatch, capfd, json.dumps(_bash(komenda, cwd=worktree)),
                  "executor", worktree)

    [linia] = _linie(plik)
    assert linia["command"] == "GH_TOKEN=*** git push"
    assert "sekret12345" not in json.dumps(linia)


def test_maskowanie_komendy_w_zdarzeniu_odmowy_nie_zmienia_komendy_bez_sekretu(
    monkeypatch, capfd, tmp_path, worktree
):
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))

    _wywolaj_main(monkeypatch, capfd, json.dumps(_bash("git push origin main", cwd=worktree)),
                  "executor", worktree)

    [linia] = _linie(plik)
    assert linia["command"] == "git push origin main"


def test_sekret_przecinany_ucieciem_do_dwustu_znakow_jest_maskowany_przed_ucieciem(
    monkeypatch, capfd, tmp_path, worktree
):
    sekret = "ZXQ-sekret-bardzo-dlugi-12345"
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", sekret)
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)
    monkeypatch.setenv(ZMIENNA_PLIKU, str(plik))
    komenda = "git push " + "x" * 176 + " " + sekret
    assert komenda.index(sekret) == 186

    _wywolaj_main(monkeypatch, capfd, json.dumps(_bash(komenda, cwd=worktree)),
                  "executor", worktree)

    [linia] = _linie(plik)
    zamaskowana = komenda.replace(sekret, sekret[:8] + "*" * 8)
    assert linia["command"] == zamaskowana[:200]
    assert sekret[:10] not in linia["command"]


# --- HAR-219: sekret w komendzie idzie w górę zamaskowany, a powtórzenia liczą po masce -------


async def test_odmowa_z_sekretem_w_komendzie_idzie_w_gore_z_zamaskowana_komenda(tmp_path):
    [zdarzenie] = await _przeslij(
        tmp_path, [_odmowa_payload(komenda="GH_TOKEN=sekret12345 git push")]
    )

    assert zdarzenie["text"] == "Bash: GH_TOKEN=*** git push [role=executor]"
    assert "sekret12345" not in json.dumps(zdarzenie)


async def test_ostrzezenie_o_powtorzeniu_odmowy_niesie_zamaskowana_komende(tmp_path):
    wyslane = await _przeslij(tmp_path, [
        _odmowa_payload(komenda="GH_TOKEN=sekret12345 git push"),
        _odmowa_payload(komenda="GH_TOKEN=sekret12345 git push"),
    ])

    [powtorzenie] = [w for w in wyslane if w["kind"] == "hook:ToolDeniedRepeat"]
    assert powtorzenie["text"] == PREFIKS_POWTORZENIA + "Bash: GH_TOKEN=*** git push"
    assert "sekret12345" not in json.dumps(wyslane)


async def test_dwie_odmowy_tej_samej_komendy_z_roznymi_sekretami_licza_sie_jako_powtorzenie(
    tmp_path,
):
    wyslane = await _przeslij(tmp_path, [
        _odmowa_payload(komenda="GH_TOKEN=sekretA9981 git push"),
        _odmowa_payload(komenda="GH_TOKEN=sekretB7742 git push"),
    ])

    assert _rodzaje(wyslane) == ["hook:ToolDenied", "hook:ToolDenied", "hook:ToolDeniedRepeat"]
    assert "sekretA9981" not in json.dumps(wyslane)
    assert "sekretB7742" not in json.dumps(wyslane)


async def test_odmowy_roznych_komend_z_tym_samym_sekretem_nie_licza_sie_jako_powtorzenie(
    tmp_path,
):
    wyslane = await _przeslij(tmp_path, [
        _odmowa_payload(komenda="GH_TOKEN=sekret12345 git push"),
        _odmowa_payload(komenda="GH_TOKEN=sekret12345 git commit"),
    ])

    assert _rodzaje(wyslane) == ["hook:ToolDenied", "hook:ToolDenied"]


# --- HAR-219 (faza 3): awaria startu - kolejnosc i wyjatki spoza ImportError -------------------


@pytest.mark.parametrize(
    "tresc,klasa",
    [
        pytest.param('raise RuntimeError("x")\n', "RuntimeError", id="wyjatek-w-czasie-importu"),
        pytest.param("def (:\n", "SyntaxError", id="blad-skladni-w-zaleznosci"),
    ],
)
@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
def test_zaleznosc_strazy_zepsuta_innym_wyjatkiem_niz_blad_importu_daje_json_deny_z_nazwa_klasy(
    tmp_path, worktree, rola, tresc, klasa
):
    skrypt = _kopia_strazy_bez_pakietu_cockpit(tmp_path)
    (skrypt.parent.parent / "cockpit" / "services" / "invariants.py").write_text(tresc)
    plik = plik_hookow(tmp_path / "stan", RUN, KROK)

    wynik = _uruchom(
        [sys.executable, "-I", str(skrypt), "--rola", rola.value, "--worktree", str(worktree)],
        json.dumps(_bash("ls", cwd=worktree)),
        plik,
    )

    assert _odmowa(wynik)["permissionDecisionReason"] == POWOD_BLEDU_STARTU.format(klasa=klasa)
    assert plik.read_bytes() == b""


@pytest.mark.parametrize(
    "argv,dane",
    [
        pytest.param([], "to nie jest json", id="bez-argumentow-i-smieciowe-wejscie"),
        pytest.param(["--rola", "czarodziej", "--worktree", "/x"], "[]", id="nieznana-rola"),
    ],
)
def test_blad_startu_strazy_ma_pierwszenstwo_przed_bledami_argumentow_i_wejscia(
    monkeypatch, capfd, argv, dane
):
    monkeypatch.setattr(straz_roli, "_BLAD_STARTU", ModuleNotFoundError("x"))

    powod = _powod_main(monkeypatch, capfd, dane, argv)

    assert powod == POWOD_BLEDU_STARTU.format(klasa="ModuleNotFoundError")


# --- HAR-219 (faza 3): git - powody, wielkosc liter, -c i -C ------------------------------------

POWOD_WALIDACJI_GITA = (
    "straż roli: niezmiennik no_git_write: silnik nie commituje ani nie pushuje: "
    "git w komendzie '{komenda}'"
)

GIT_POWODY_DOKLADNE = [
    pytest.param(
        '"g"it commit -m x',
        POWOD_WALIDACJI_GITA.format(komenda="git commit -m x"),
        id="commit-w-cudzyslowie-powod-cytuje-komende-bez-cudzyslowow",
    ),
    pytest.param(
        "g'it' push",
        POWOD_WALIDACJI_GITA.format(komenda="git push"),
        id="push-w-apostrofach-powod-cytuje-komende-bez-apostrofow",
    ),
    pytest.param(
        "git -C katalog commit -m x",
        POWOD_WALIDACJI_GITA.format(komenda="git -C katalog commit -m x"),
        id="commit-po-opcji-C-to-commit-a-nie-alias",
    ),
    pytest.param(
        "git -C alias.d merge x",
        POWOD_COMMIT.format(podpolecenie="merge"),
        id="opcja-C-z-katalogiem-alias-kropka-to-nie-alias",
    ),
    pytest.param("GIT merge x", POWOD_COMMIT.format(podpolecenie="merge"), id="GIT-wielkimi"),
    pytest.param("git MERGE x", POWOD_COMMIT.format(podpolecenie="merge"), id="MERGE-wielkimi"),
    pytest.param("Git Rebase main", POWOD_COMMIT.format(podpolecenie="rebase"), id="Git-Rebase"),
    pytest.param(
        "git CHECKOUT main", POWOD_GITA.format(git="git checkout"), id="CHECKOUT-wielkimi"
    ),
    pytest.param(
        "git Reset --Hard", POWOD_GITA.format(git="git reset --hard"), id="Reset-Hard-wielkimi"
    ),
    pytest.param("git -c ALIAS.c=commit c", POWOD_ALIASU, id="alias-wielkimi-literami"),
    pytest.param("git -cALIAS.c=commit c", POWOD_ALIASU, id="alias-sklejony-wielkimi-literami"),
    pytest.param("git -c Alias.c=x c", POWOD_ALIASU, id="alias-mieszane-litery"),
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("komenda,oczekiwany", GIT_POWODY_DOKLADNE)
def test_git_zakazany_w_nietypowym_zapisie_ma_w_kazdej_roli_dokladny_powod(
    rola, komenda, oczekiwany, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) == oczekiwany


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param("git -c", id="opcja-c-na-koncu-komendy"),
        pytest.param("git -C", id="opcja-C-na-koncu-komendy"),
        pytest.param("git --git-dir", id="opcja-z-argumentem-na-koncu-komendy"),
        pytest.param("git -cuser.name=x status", id="sklejone-c-z-kluczem-niebedacym-aliasem"),
        pytest.param("git -Calias.d status", id="sklejone-duze-C-to-nie-opcja-c"),
        pytest.param("git -C alias.d status", id="duze-C-z-katalogiem-alias-kropka"),
        pytest.param("git -c user.name=alias.x status", id="alias-dopiero-w-wartosci-klucza"),
    ],
)
def test_opcja_gita_ktora_nie_ustawia_aliasu_albo_nie_ma_wartosci_nie_jest_odmawiana(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize("komenda", ["git -c", "git -C", "git --git-dir"])
def test_opcja_gita_bez_podpolecenia_w_roli_bez_zapisu_to_reviewer_readonly(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_RECENZENTA.format(rola=rola.value)


# --- HAR-219 (faza 3): przekierowania - brak celu, skladnia, cudzyslowy, deskryptory -----------


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    ["echo x >", "echo x >>", "echo x 2>", "echo x &>", "echo x >&"],
)
def test_przekierowanie_bez_celu_na_koncu_komendy_jest_odmowione_z_pustym_celem(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == "straż roli: przekierowanie do celu, którego nie da się ocenić: "


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("znak", list("();<>|&"))
def test_znak_skladni_powloki_jako_cel_przekierowania_jest_odmowiony_z_tym_znakiem_w_powodzie(
    rola, znak, worktree
):
    powod = decyzja(_bash(f"echo x > {znak} y", cwd=worktree), rola, worktree)

    assert powod == POWOD_CEL_NIEOCENIANY + znak


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_podstawienie_procesu_jako_cel_przekierowania_jest_odmowione_z_celem_w_powodzie(
    rola, worktree
):
    powod = decyzja(_bash("echo x > >(cat)", cwd=worktree), rola, worktree)

    assert powod == POWOD_CEL_NIEOCENIANY + ">("


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param('echo "x > /poza/plik', id="niezamkniety-cudzyslow-z-przekierowaniem"),
        pytest.param("echo 'x > /poza/plik", id="niezamkniety-apostrof-z-przekierowaniem"),
        pytest.param("echo x > \\", id="ucieczka-na-koncu-po-przekierowaniu"),
    ],
)
def test_komenda_z_przekierowaniem_ktorej_nie_da_sie_rozebrac_jest_odmowiona_jako_nie_do_rozbioru(
    rola, komenda, worktree
):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_CEL_NIEOCENIANY + "(komenda nie do rozbioru)"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param('echo "x', id="niezamkniety-cudzyslow"),
        pytest.param("ls 'abc", id="niezamkniety-apostrof"),
        pytest.param("echo x\\", id="ucieczka-na-koncu"),
    ],
)
def test_komenda_bez_przekierowania_ktorej_nie_da_sie_rozebrac_nie_jest_odmawiana_przez_straz(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


FORMY_PRZEKIEROWANIA_DODATKOWE = [
    pytest.param("echo x >& {c}", id="stdout-i-stderr-po-ampersandzie"),
    pytest.param("echo x <> {c}", id="otwarcie-do-odczytu-i-zapisu"),
    pytest.param("echo x 3<> {c}", id="otwarcie-do-odczytu-i-zapisu-z-deskryptorem"),
    pytest.param("(echo x)> {c}", id="za-nawiasem-zamykajacym"),
    pytest.param("(echo x)>{c}", id="za-nawiasem-zamykajacym-sklejone"),
    pytest.param('echo x > "{c}"', id="cel-w-cudzyslowie"),
    pytest.param("echo x > '{c}'", id="cel-w-apostrofach"),
    pytest.param('echo x >"{c}"', id="cel-w-cudzyslowie-sklejony"),
    pytest.param("echo X#a > {c}", id="znak-hash-w-slowie-nie-zaczyna-komentarza"),
    pytest.param("echo x > /dev/null 2> {c}", id="po-dev-null-w-tej-samej-komendzie"),
    pytest.param("echo x 2> /dev/null > {c}", id="dev-null-na-stderr-potem-cel-poza"),
]


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize("forma", FORMY_PRZEKIEROWANIA_DODATKOWE)
def test_dalsze_formy_przekierowania_poza_worktree_sa_odmowione_z_celem_bez_cudzyslowow(
    rola, forma, worktree, obcy
):
    cel = str(obcy / "plik")

    powod = decyzja(_bash(forma.format(c=cel), cwd=worktree), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + cel


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param('echo "a>b" {obcy}/plik', id="znak-wiekszosci-w-cudzyslowie-nie-jest-przekierowaniem"),
        pytest.param('echo x > "a;b"', id="cel-z-srednikiem-w-cudzyslowie"),
        pytest.param('echo x > "my file"', id="cel-ze-spacja-w-cudzyslowie"),
        pytest.param("ls | {obcy}/prog", id="rura-przed-sciezka-poza-worktree"),
        pytest.param("ls ; {obcy}/prog", id="srednik-przed-sciezka-poza-worktree"),
        pytest.param("ls && {obcy}/prog", id="i-przed-sciezka-poza-worktree"),
    ],
)
def test_znaki_skladni_bez_przekierowania_wyjscia_i_cele_w_cudzyslowach_w_worktree_sa_dozwolone(
    rola, komenda, worktree, obcy
):
    assert decyzja(_bash(komenda.format(obcy=obcy), cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "echo x >&2",
        "echo x >&12",
        "echo x >&2-",
        "echo x >&-",
        "echo x 2>&1",
        "echo x 1>&2",
        "echo x 2>&1 >&2",
    ],
)
def test_kopia_deskryptora_nie_jest_zapisem_do_pliku_nawet_gdy_cwd_jest_poza_worktree(
    rola, komenda, worktree, obcy
):
    assert decyzja(_bash(komenda, cwd=obcy), rola, worktree) is None


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda,cel",
    [
        pytest.param("echo x > 2", "2", id="liczba-po-zwyklym-przekierowaniu-to-plik"),
        pytest.param("echo x &> 2", "2", id="liczba-po-ampersandzie-przed-wiekszoscia-to-plik"),
        pytest.param("echo x >& 2x", "2x", id="cyfra-z-dopiskiem-to-plik"),
        pytest.param("echo x >&2x", "2x", id="cyfra-z-dopiskiem-sklejona-to-plik"),
        pytest.param("echo x >& plik", "plik", id="nazwa-pliku-po-ampersandzie-to-plik"),
        pytest.param("echo x >& 2 > 3", "3", id="kopia-deskryptora-nie-zaslania-kolejnego-celu"),
    ],
)
def test_cel_niebedacy_deskryptorem_jest_plikiem_liczonym_od_cwd_poza_worktree(
    rola, komenda, cel, worktree, obcy
):
    powod = decyzja(_bash(komenda, cwd=obcy), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + cel


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_cel_przekierowania_z_bajtem_zerowym_jest_odmowiony_jako_nie_do_oceny(rola, worktree):
    powod = decyzja(_bash("echo x > a\x00b", cwd=worktree), rola, worktree)

    assert powod == POWOD_CEL_NIEOCENIANY + "a\x00b"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_cel_wzgledny_gdy_nie_da_sie_rozwiazac_biezacego_katalogu_jest_odmowiony_jako_nie_do_oceny(
    rola, worktree, tmp_path, monkeypatch
):
    # wzgledny `cwd` ze zdarzenia + usuniety katalog procesu: prawdziwy realpath rzuca OSError
    usuniety = tmp_path / "usuniety"
    usuniety.mkdir()
    monkeypatch.chdir(usuniety)
    usuniety.rmdir()
    zdarzenie = _bash("echo x > plik")
    zdarzenie["cwd"] = "wzgledny"

    powod = decyzja(zdarzenie, rola, worktree)

    assert powod == POWOD_CEL_NIEOCENIANY + "plik"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "cwd", [5, None, ["x"], {"a": 1}], ids=["liczba", "brak-wartosci", "lista", "slownik"]
)
def test_cwd_niebedace_napisem_jest_ignorowane_a_cel_wzgledny_liczy_sie_od_worktree(
    rola, cwd, worktree
):
    w_worktree = _bash("echo x > plik")
    w_worktree["cwd"] = cwd
    poza = _bash("echo x > ../poza")
    poza["cwd"] = cwd

    assert decyzja(w_worktree, rola, worktree) is None
    assert decyzja(poza, rola, worktree) == POWOD_POZA_WORKTREE + "../poza"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_worktree_podany_jako_dowiazanie_nie_odmawia_przekierowania_do_pliku_w_nim(
    rola, worktree, tmp_path
):
    dowiazanie = tmp_path / "wt-dowiazanie"
    dowiazanie.symlink_to(worktree, target_is_directory=True)

    assert decyzja(_bash("echo x > plik", cwd=dowiazanie), rola, dowiazanie) is None
    assert decyzja(_bash(f"echo x > {dowiazanie}/plik", cwd=dowiazanie), rola, dowiazanie) is None


# --- HAR-219 (faza 3): opcje z bialej listy - pelny token w powodzie i granice zakresu ---------


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize(
    "komenda,token",
    [
        pytest.param("git diff --output=plik", "--output=plik", id="diff-output-rowna-sie"),
        pytest.param("git log --outp=x", "--outp=x", id="log-skrot-outp"),
        pytest.param("git show --out=x HEAD", "--out=x", id="show-skrot-out"),
        pytest.param(
            "git diff --output-indicator-new=x",
            "--output-indicator-new=x",
            id="diff-dluzsza-opcja-na-ou",
        ),
        pytest.param("rg --pre=cmd foo", "--pre=cmd", id="rg-pre-rowna-sie"),
        pytest.param("rg --pre-glob=*.x foo", "--pre-glob=*.x", id="rg-pre-glob-rowna-sie"),
    ],
)
def test_opcja_z_wartoscia_albo_skrocona_ma_w_powodzie_caly_token(rola, komenda, token, worktree):
    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_OPCJI + token


@pytest.mark.parametrize("rola", ROLE_BEZ_ZAPISU, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        "grep foo --textconv",
        "cat plik --ext-diff",
        "ls --ou",
        "git log -z",
        "git status",
    ],
)
def test_opcje_zapisujace_gita_i_rg_nie_obejmuja_innych_polecen_z_bialej_listy(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


# --- HAR-219 (faza 3): powod odmowy cytujacy komende jest maskowany na stdout i w pliku -----------

POWOD_PUSH_Z_ZMIENNA = (
    "straż roli: niezmiennik no_git_write: silnik nie commituje ani nie pushuje: "
    "git w komendzie 'GH_TOKEN=*** git push'"
)
POWOD_PUSH_Z_OPCJA = (
    "straż roli: niezmiennik no_git_write: silnik nie commituje ani nie pushuje: "
    "git w komendzie 'git --token=*** push'"
)

POWODY_CYTUJACE_KOMENDE = [
    pytest.param(
        Rola.executor, "GH_TOKEN=sekret12345 git push", POWOD_PUSH_Z_ZMIENNA,
        id="walidacja-no-git-write-z-przypisaniem",
    ),
    pytest.param(
        Rola.executor, "git --token=sekret12345 push", POWOD_PUSH_Z_OPCJA,
        id="walidacja-no-git-write-z-opcja",
    ),
    pytest.param(
        Rola.reviewer, "rg --pre=GH_TOKEN=sekret12345 foo",
        POWOD_OPCJI + "--pre=GH_TOKEN=***",
        id="opcja-zapisujaca-recenzenta",
    ),
    pytest.param(
        Rola.executor, "echo x > {obcy}/GH_TOKEN=sekret12345",
        POWOD_POZA_WORKTREE + "{obcy}/GH_TOKEN=***",
        id="przekierowanie-poza-worktree",
    ),
]


@pytest.mark.parametrize("rola,komenda,oczekiwany", POWODY_CYTUJACE_KOMENDE)
def test_powod_odmowy_na_stdout_ma_zamaskowany_sekret_z_komendy(
    monkeypatch, capfd, worktree, obcy, rola, komenda, oczekiwany
):
    monkeypatch.delenv(ZMIENNA_PLIKU, raising=False)
    dane = json.dumps(_bash(komenda.format(obcy=obcy), cwd=worktree))

    powod = _powod_main(
        monkeypatch, capfd, dane, ["--rola", rola.value, "--worktree", str(worktree)]
    )

    assert powod == oczekiwany.format(obcy=obcy)


@pytest.mark.parametrize("rola,komenda,oczekiwany", POWODY_CYTUJACE_KOMENDE)
def test_powod_odmowy_w_pliku_hookow_ma_zamaskowany_sekret_z_komendy(
    monkeypatch, capfd, tmp_path, worktree, obcy, rola, komenda, oczekiwany
):
    zdarzenie = _bash(komenda.format(obcy=obcy), cwd=worktree)

    linia = _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie, rola.value)

    assert linia["reason"] == oczekiwany.format(obcy=obcy)
    assert "sekret12345" not in json.dumps(linia)


def test_sciezka_zapisu_w_zdarzeniu_odmowy_nie_jest_maskowana_bo_maskowanie_dotyczy_komend_basha(
    monkeypatch, capfd, tmp_path, worktree, obcy
):
    sciezka = str(obcy / "MOJ_KEY=1.txt")
    zdarzenie = _zapis("Write", "file_path", sciezka, cwd=worktree)

    linia = _linia_odmowy(monkeypatch, capfd, tmp_path, worktree, zdarzenie)

    assert linia["command"] == sciezka


# --- HAR-219 (poprawka 1): przekierowanie na poczatku komendy, alias jako ostatnia para --------

POWOD_ALIASU_CONFIG_ENV = "straż roli: no_git_write — alias gita przez --config-env"
POWOD_ALIASU_GIT_CONFIG = "straż roli: no_git_write — alias gita przez git config"


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "forma",
    [
        pytest.param(">{c} echo hi", id="przekierowanie-sklejone-przed-poleceniem"),
        pytest.param("> {c} echo hi", id="przekierowanie-ze-spacja-przed-poleceniem"),
    ],
)
def test_przekierowanie_na_poczatku_komendy_poza_worktree_jest_odmowione_z_dokladnym_powodem(
    rola, forma, worktree, obcy
):
    cel = str(obcy / "x")

    powod = decyzja(_bash(forma.format(c=cel), cwd=worktree), rola, worktree)

    assert powod == POWOD_POZA_WORKTREE + cel


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_przekierowanie_na_poczatku_komendy_do_pliku_w_worktree_jest_dozwolone(rola, worktree):
    assert decyzja(_bash("> plik echo hi", cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize(
    "komenda,oczekiwany",
    [
        pytest.param("git -c alias.c=commit", POWOD_ALIASU, id="opcja-c-jako-ostatnia-para"),
        pytest.param(
            "git --config-env alias.c=V", POWOD_ALIASU_CONFIG_ENV,
            id="opcja-config-env-jako-ostatnia-para",
        ),
        pytest.param(
            "git --config-env=alias.c=V", POWOD_ALIASU_CONFIG_ENV,
            id="opcja-config-env-z-rownasie-bez-podpolecenia",
        ),
    ],
)
def test_alias_gita_podany_jako_ostatnia_para_tokenow_bez_podpolecenia_jest_odmowiony(
    rola, komenda, oczekiwany, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) == oczekiwany


# --- HAR-219 (poprawka 1): cudzyslow ANSI-C, ucieczka, --config-env i git config alias ---------

POWOD_COMMIT_Z_WALIDACJI = POWOD_WALIDACJI_GITA.format(komenda="git commit -m x")

GIT_ZLOZONY_W_POWLOCE = [
    pytest.param(r"\git commit -m x", POWOD_COMMIT_Z_WALIDACJI, id="ucieczka-przed-nazwa-gita"),
    pytest.param(r"gi\t commit -m x", POWOD_COMMIT_Z_WALIDACJI, id="ucieczka-w-nazwie-gita"),
    pytest.param("$'g'it commit -m x", POWOD_COMMIT_Z_WALIDACJI, id="ansi-c-w-nazwie-gita"),
    pytest.param('$"g"it commit -m x', POWOD_COMMIT_Z_WALIDACJI, id="tlumaczenie-w-nazwie-gita"),
    pytest.param(
        r"g\it merge x", POWOD_COMMIT.format(podpolecenie="merge"),
        id="ucieczka-w-nazwie-gita-merge",
    ),
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("komenda,oczekiwany", GIT_ZLOZONY_W_POWLOCE)
def test_git_zlozony_w_powloce_z_ucieczki_albo_cudzyslowu_ansi_c_jest_odmowiony_w_kazdej_roli(
    rola, komenda, oczekiwany, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) == oczekiwany


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
def test_dolar_bez_cudzyslowu_nie_skleja_nazwy_gita_i_polecenie_nie_jest_odmawiane(
    rola, worktree
):
    assert decyzja(_bash("echo $git commit", cwd=worktree), rola, worktree) is None


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param("git --config-env=alias.c=V c -m x", id="rowna-sie-w-jednym-tokenie"),
        pytest.param("git --config-env alias.c=V c", id="wartosc-w-osobnym-tokenie"),
        pytest.param("git --config-env ALIAS.c=V c", id="wielkie-litery-aliasu"),
        pytest.param("git -C d --config-env alias.c=V c", id="po-innej-opcji-globalnej"),
    ],
)
def test_alias_gita_przez_config_env_jest_odmowiony_w_kazdej_roli_z_dokladnym_powodem(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) == POWOD_ALIASU_CONFIG_ENV


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param("git --config-env k=v status", id="config-env-z-osobna-wartoscia"),
        pytest.param("git --config-env=k=v status", id="config-env-z-wartoscia-po-rownasie"),
        pytest.param("git --config-env=alias status", id="config-env-z-kluczem-bez-kropki"),
    ],
)
def test_config_env_ktory_nie_ustawia_aliasu_nie_jest_odmawiany(rola, komenda, worktree):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None


# Stala lista, niezalezna od `_GIT_TWORZY_COMMIT`. `waliduj_definicje` nie zna `--config-env`, wiec
# po tej opcji o odmowie decyduje wylacznie element zbioru.
PODPOLECENIA_TWORZACE_COMMIT = [
    "commit",
    "push",
    "merge",
    "cherry-pick",
    "revert",
    "rebase",
    "am",
    "stash",
]


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize("podpolecenie", PODPOLECENIA_TWORZACE_COMMIT)
def test_podpolecenie_tworzace_commit_po_opcji_config_env_jest_odmowione_w_kazdej_roli(
    rola, podpolecenie, worktree
):
    komenda = f"git --config-env k=v {podpolecenie} origin main"

    powod = decyzja(_bash(komenda, cwd=worktree), rola, worktree)

    assert powod == POWOD_COMMIT.format(podpolecenie=podpolecenie)


@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param("git --config-env k=v commit", id="config-env-z-osobna-wartoscia"),
        pytest.param("git --config-env=k=v commit", id="config-env-z-rownasie"),
        pytest.param("git --git-dir d commit", id="opcja-z-argumentem-spoza-konfiguracji"),
        pytest.param("git --no-pager commit", id="opcja-bez-argumentu"),
        pytest.param("git -c user.name=a commit", id="opcja-c-z-kluczem-niebedacym-aliasem"),
    ],
)
def test_zakazany_git_pomija_wartosc_opcji_globalnej_i_znajduje_commit_za_nia(komenda):
    # bezposrednio: `waliduj_definicje` w `decyzja` wyprzedza te formy innym powodem albo ich nie zna
    assert straz_roli._zakazany_git(komenda) == POWOD_COMMIT.format(podpolecenie="commit")


@pytest.mark.parametrize("rola", WSZYSTKIE_ROLE, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param("git config alias.c commit", id="alias-bez-zakresu"),
        pytest.param("git config --global alias.c commit", id="alias-z-opcja-zakresu"),
        pytest.param("git config ALIAS.c commit", id="alias-wielkimi-literami"),
    ],
)
def test_alias_gita_przez_git_config_jest_odmowiony_w_kazdej_roli_z_dokladnym_powodem(
    rola, komenda, worktree
):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) == POWOD_ALIASU_GIT_CONFIG


@pytest.mark.parametrize("rola", ROLE_Z_ZAPISEM, ids=_rola)
@pytest.mark.parametrize(
    "komenda",
    [
        pytest.param("git config user.name x", id="zwykly-klucz"),
        pytest.param("git -C alias.d config user.name x", id="alias-tylko-przed-podpoleceniem"),
        pytest.param("git config aliasy x", id="klucz-zaczynajacy-sie-od-alias-bez-kropki"),
        pytest.param("git -c aliasy=1 status", id="opcja-c-z-kluczem-bez-kropki-po-alias"),
    ],
)
def test_git_config_bez_aliasu_nie_jest_odmawiany(rola, komenda, worktree):
    assert decyzja(_bash(komenda, cwd=worktree), rola, worktree) is None

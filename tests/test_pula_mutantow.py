"""Pula maszyn dla bramki mutantow (audyt procesu 2026-11-20): shardy statystyk mutmuta naraz."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from procesy import uruchom_i_zbierz

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / ".claude" / "skills" / "praca-ticketu" / "scripts" / "pula_mutantow.py"
SZABLON = KORZEN / ".claude" / "skills" / "praca-ticketu" / "bramki-szablon.json"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("pula_mutantow_test", SKRYPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _maszyna(nazwa: str, shardy: int, sloty: int = 4) -> dict:
    return {"ssh": nazwa, "katalog": "~/h", "obraz": "obraz:1", "shardy": shardy, "sloty": sloty}


def _git(repo: Path, *a: str) -> str:
    return uruchom_i_zbierz(["git", *a], cwd=repo, check=True, capture_output=True, text=True, timeout=60).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "src" / "a").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["src/a/", "src/b.py"]\n')
    (r / "src" / "a" / "x.py").write_text("x = 1\n")
    (r / "docs" / "o.md").write_text("o\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "start")
    _git(r, "branch", "baza")
    return r


# --- konfiguracja i przydzial shardow -------------------------------------------------------------

def test_bez_pliku_puli_wszystkie_shardy_liczy_sie_lokalnie(mod, tmp_path):
    zdalne = mod.wczytaj_pule(tmp_path / "pula.json")
    assert zdalne == []
    assert mod.plan(4, zdalne) == [None, None, None, None]


def test_maszyna_zdalna_bierze_shardy_od_konca(mod):
    b = _maszyna("browar", 2)
    assert mod.plan(4, [b]) == [None, None, b, b]


def test_druga_maszyna_bierze_kolejne_shardy_od_konca(mod):
    a, b = _maszyna("a", 1), _maszyna("b", 2)
    assert mod.plan(4, [a, b]) == [None, b, b, a]


def test_shard_zero_zostaje_lokalnie_nawet_gdy_maszyna_chce_wiecej(mod):
    b = _maszyna("browar", 5)
    assert mod.plan(3, [b]) == [None, b, b]


def test_poprawna_pula_wczytana_z_pliku(mod, tmp_path):
    p = tmp_path / "pula.json"
    p.write_text(json.dumps({"zdalne": [_maszyna("browar", 2)]}))
    assert mod.wczytaj_pule(p) == [_maszyna("browar", 2)]


@pytest.mark.parametrize("tresc", [
    "{zly json",
    json.dumps({"inne": []}),
    json.dumps([]),
    json.dumps({"zdalne": [{"ssh": "b", "katalog": "~/h", "shardy": 1, "sloty": 1}]}),
    json.dumps({"zdalne": [{"ssh": "", "katalog": "~/h", "obraz": "o", "shardy": 1, "sloty": 1}]}),
    json.dumps({"zdalne": [{"ssh": "b", "katalog": "~/h", "obraz": "o", "shardy": 0, "sloty": 1}]}),
    json.dumps({"zdalne": [{"ssh": "b", "katalog": "~/h", "obraz": "o", "shardy": True, "sloty": 1}]}),
    json.dumps({"zdalne": [{"ssh": "b", "katalog": "~/h", "obraz": "o", "shardy": 1, "sloty": "4"}]}),
    json.dumps({"zdalne": ["browar"]}),
], ids=["zly-json", "bez-zdalne", "lista-zamiast-obiektu", "bez-obrazu", "pusty-ssh", "zero-shardow",
        "shardy-bool", "sloty-napis", "maszyna-napis"])
def test_zla_pula_to_blad_konfiguracji(mod, tmp_path, tresc):
    p = tmp_path / "pula.json"
    p.write_text(tresc)
    with pytest.raises(mod.BladPuli):
        mod.wczytaj_pule(p)


def test_nieczytelna_pula_to_blad_konfiguracji(mod, tmp_path):
    p = tmp_path / "pula.json"
    p.mkdir()
    with pytest.raises(mod.BladPuli):
        mod.wczytaj_pule(p)


# --- czy statystyki sa potrzebne ----------------------------------------------------------------

def test_zmiana_poza_zrodlami_mutmuta_nie_potrzebuje_statystyk(mod, repo):
    (repo / "docs" / "o.md").write_text("zmiana\n")
    assert mod.zmienia_zrodla_mutmuta(repo, "baza") is False


def test_zmiana_w_katalogu_zrodel_mutmuta_potrzebuje_statystyk(mod, repo):
    (repo / "src" / "a" / "x.py").write_text("x = 2\n")
    assert mod.zmienia_zrodla_mutmuta(repo, "baza") is True


def test_nowy_niesledzony_plik_w_zrodlach_potrzebuje_statystyk(mod, repo):
    (repo / "src" / "a" / "nowy.py").write_text("y = 1\n")
    assert mod.zmienia_zrodla_mutmuta(repo, "baza") is True


def test_zrodlo_mutmuta_bedace_plikiem_potrzebuje_statystyk(mod, repo):
    (repo / "src" / "b.py").write_text("z = 1\n")
    assert mod.zmienia_zrodla_mutmuta(repo, "baza") is True


def test_plik_o_nazwie_zaczynajacej_sie_jak_katalog_zrodel_nie_potrzebuje_statystyk(mod, repo):
    (repo / "src" / "ab.py").write_text("z = 1\n")
    assert mod.zmienia_zrodla_mutmuta(repo, "baza") is False


def test_zmiana_zacommitowana_po_bazie_potrzebuje_statystyk(mod, repo):
    (repo / "src" / "a" / "x.py").write_text("x = 3\n")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "zmiana")
    assert mod.zmienia_zrodla_mutmuta(repo, "baza") is True


# --- kopia kodu dla shardu ----------------------------------------------------------------------

def test_kopia_dla_shardu_ma_ten_sam_commit_zmiany_i_nowe_pliki(mod, repo, tmp_path):
    (repo / "src" / "a" / "x.py").write_text("x = 2\n")
    (repo / "src" / "a" / "nowy.py").write_text("y = 1\n")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    cel = tmp_path / "kopia"
    mod.odtworz(repo, cel, sha, mod.paczka(repo))
    assert _git(cel, "rev-parse", "HEAD").strip() == sha
    assert (cel / "src" / "a" / "x.py").read_text() == "x = 2\n"
    assert (cel / "src" / "a" / "nowy.py").read_text() == "y = 1\n"


def test_kopia_dla_shardu_staje_na_wskazanym_commicie_nie_na_glowie_repozytorium(mod, repo, tmp_path):
    sha = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "docs" / "o.md").write_text("dalej\n")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "dalej")
    cel = tmp_path / "kopia"
    mod.odtworz(repo, cel, sha, mod.paczka(repo))
    assert _git(cel, "rev-parse", "HEAD").strip() == sha


def test_kopia_bez_zmian_to_czysty_commit(mod, repo, tmp_path):
    sha = _git(repo, "rev-parse", "HEAD").strip()
    cel = tmp_path / "kopia"
    mod.odtworz(repo, cel, sha, mod.paczka(repo))
    assert _git(cel, "status", "--porcelain") == ""


# --- skrypt maszyny zdalnej ---------------------------------------------------------------------

def test_skrypt_zdalny_czeka_na_wolny_slot_maszyny(mod):
    s = mod.skrypt_zdalny(_maszyna("browar", 2, sloty=3), "praca1", "abc123", 2, 4)
    assert "for s in $(seq 0 2); do" in s
    assert "flock -n -E 200 /tmp/harness-pula-$s.lock docker run" in s
    assert "[ $k != 200 ] && break 2" in s


def test_skrypt_zdalny_liczy_wskazany_shard_na_commicie_kopii(mod):
    s = mod.skrypt_zdalny(_maszyna("browar", 2), "praca1", "abc123", 2, 4)
    assert "git clone -q --no-checkout ~/h/repo $D/repo && git -C $D/repo checkout -q --detach abc123 || exit 3" in s
    assert "shard --indeks 2 --liczba 4 --katalog-dowodow .pula-dowody" in s
    assert "[ $k = 0 ] && cat $D/repo/.pula-dowody/shard-2.json" in s


def test_skrypt_zdalny_odtwarza_zmiany_i_nowe_pliki(mod):
    s = mod.skrypt_zdalny(_maszyna("browar", 2), "praca1", "abc123", 2, 4)
    assert "flock ~/h/repo.lock git -C ~/h/repo fetch -q origin || exit 3" in s
    assert "[ -s $D/zmiany.bundle ] && { flock ~/h/repo.lock git -C ~/h/repo fetch -q $D/zmiany.bundle || exit 3; }" in s
    assert "[ -s $D/lata.diff ] && { git -C $D/repo apply --binary $D/lata.diff || exit 3; }" in s
    assert "[ -d $D/nowe ] && cp -a $D/nowe/. $D/repo/" in s


def test_skrypt_zdalny_cytuje_obraz_kontenera(mod):
    m = _maszyna("browar", 2)
    m["obraz"] = "obraz;rm"
    assert "'obraz;rm'" in mod.skrypt_zdalny(m, "praca1", "abc123", 1, 4)


# --- przebieg bramki ----------------------------------------------------------------------------

@pytest.fixture
def przebieg(mod, monkeypatch, repo):
    """main() na repo testowym z podmienionymi shardami i uruchomieniem narzedzi."""
    (repo / "src" / "a" / "x.py").write_text("x = 2\n")
    monkeypatch.chdir(repo)
    stan = {"zdalne": [], "lokalne": [], "uruchom": [], "wyniki_zdalne": {}, "wyniki_lokalne": {}, "kody": []}

    def zdalny(korzen, m, sha, dane, indeks, liczba, dowody, log):
        stan["zdalne"].append(indeks)
        return stan["wyniki_zdalne"].get(indeks, True)

    def lokalny(korzen, sha, dane, indeks, liczba, dowody, log):
        stan["lokalne"].append(indeks)
        return stan["wyniki_lokalne"].get(indeks, True)

    def uruchom(argv, cwd):
        stan["uruchom"].append(argv)
        return stan["kody"].pop(0) if stan["kody"] else 0

    monkeypatch.setattr(mod, "shard_zdalny", zdalny)
    monkeypatch.setattr(mod, "shard_lokalny", lokalny)
    monkeypatch.setattr(mod, "uruchom", uruchom)
    return stan


def _pula(repo: Path, *maszyny: dict) -> None:
    (repo / ".git" / "monolynx").mkdir(exist_ok=True)
    (repo / ".git" / "monolynx" / "pula.json").write_text(json.dumps({"zdalne": list(maszyny)}))


def test_wszystkie_shardy_i_mutanty_z_dowodami_po_scaleniu(mod, przebieg, repo):
    _pula(repo, _maszyna("browar", 2))
    assert mod.main(["--liczba", "4", "--", "--baza", "baza", "--max-children", "4"]) == 0
    assert sorted(przebieg["lokalne"]) == [0, 1]
    assert sorted(przebieg["zdalne"]) == [2, 3]
    scal, mutanty = przebieg["uruchom"]
    assert scal[1:4] == ["tools/statystyki_mutmuta.py", "scal", "--katalog-dowodow"]
    assert mutanty[1:] == ["tools/mutanty_zmian.py", "--baza", "baza", "--max-children", "4",
                           "--katalog-dowodow", scal[-1]]


def test_shard_zdalny_ktory_zawiodl_liczy_sie_lokalnie_i_tak_jest_raportowany(mod, przebieg, repo, capsys):
    _pula(repo, _maszyna("browar", 2))
    przebieg["wyniki_zdalne"][3] = False
    assert mod.main(["--liczba", "4", "--", "--baza", "baza"]) == 0
    assert sorted(przebieg["lokalne"]) == [0, 1, 3]
    wyjscie = capsys.readouterr().out
    assert "PULA shard 2: ok (browar)" in wyjscie
    assert "PULA shard 3: ok (lokalnie)" in wyjscie


def test_shard_bez_dowodu_to_brak_dowodu_bez_mutantow(mod, przebieg, repo):
    przebieg["wyniki_lokalne"][1] = False
    assert mod.main(["--liczba", "2", "--", "--baza", "baza"]) == 2
    assert przebieg["uruchom"] == []


def test_nieudane_scalenie_to_brak_dowodu_bez_mutantow(mod, przebieg, repo):
    przebieg["kody"] = [2]
    assert mod.main(["--liczba", "2", "--", "--baza", "baza"]) == 2
    assert len(przebieg["uruchom"]) == 1


def test_kod_mutantow_jest_kodem_bramki(mod, przebieg, repo):
    przebieg["kody"] = [0, 3]
    assert mod.main(["--liczba", "2", "--", "--baza", "baza"]) == 3


def test_zmiana_poza_zrodlami_idzie_wprost_do_mutantow_bez_shardow(mod, przebieg, repo):
    _git(repo, "checkout", "-q", "--", "src")
    (repo / "docs" / "o.md").write_text("zmiana\n")
    przebieg["kody"] = [3]
    assert mod.main(["--", "--baza", "baza"]) == 3
    assert przebieg["lokalne"] == [] and przebieg["zdalne"] == []
    assert przebieg["uruchom"] == [[sys.executable, "tools/mutanty_zmian.py", "--baza", "baza"]]


def test_domyslnie_cztery_shardy_wzgledem_origin_main(mod, przebieg, repo, monkeypatch):
    bazy = []
    monkeypatch.setattr(mod, "zmienia_zrodla_mutmuta", lambda korzen, baza: bazy.append(baza) or True)
    assert mod.main(["--"]) == 0
    assert bazy == ["origin/main"]
    assert sorted(przebieg["lokalne"]) == [0, 1, 2, 3]


@pytest.mark.parametrize("argv", [[], ["--baza", "x"], ["--liczba", "0", "--"], ["--liczba", "x", "--"]],
                         ids=["puste", "bez-separatora", "zero-shardow", "liczba-napis"])
def test_zle_wywolanie_to_blad_uzycia(mod, przebieg, argv):
    assert mod.main(argv) == 64


def test_zla_pula_to_blad_uzycia(mod, przebieg, repo):
    (repo / ".git" / "monolynx").mkdir(exist_ok=True)
    (repo / ".git" / "monolynx" / "pula.json").write_text("{zly")
    assert mod.main(["--", "--baza", "baza"]) == 64


def test_nieznana_baza_to_brak_dowodu(mod, przebieg):
    assert mod.main(["--", "--baza", "nie-ma-takiej"]) == 2


def test_szablon_liczy_mutanty_przez_pule_czterech_shardow():
    b = next(x for x in json.loads(SZABLON.read_text()) if x["nazwa"] == "mutanty")
    assert b["argv"][:5] == ["python", ".claude/skills/praca-ticketu/scripts/pula_mutantow.py", "--liczba", "4", "--"]

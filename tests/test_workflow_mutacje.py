"""Mutacje w CI (.github/workflows/mutation.yml): co swieci na czerwono, a co jest tylko trendem (HAR-132, HAR-149).

Workflow ma dwa zadania:
- `mutation` (wymagany check na pull_request): mutuje tylko funkcje zmienione w PR przez
  `tools/mutanty_zmian.py`; przezywajacy mutant albo blad narzedzia to czerwony krok,
- `mutation-main` (push na main, niewymagany): pelny `mutmut run`, raport i zapadka baseline.

Czytamy prawdziwy workflow, wyciagamy skrypt kroku decydujacego i uruchamiamy go powloka tak jak GitHub
Actions, na kopii baseline w tmp_path. Prawdziwe `mutmut` i `git` zastepuja atrapy na PATH; skutek czytamy
z kodu wyjscia kroku, podsumowania zadania, pliku baseline i dziennika wywolan atrapy mutmut.
Zmienne kroku (BASE_REF, COMMIT_SHA) test czyta z jego `env` w YAML i podstawia za wyrazenia GitHub Actions.
"""
import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from procesy import uruchom_proces_sync, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOW = KORZEN / ".github" / "workflows" / "mutation.yml"

ZADANIE_PR = "mutation"
ZADANIE_MAIN = "mutation-main"
KROK_PR = "mutanty zmienionych funkcji"
KROK_MAIN = "pelny przebieg mutacyjny i zapadka"

# Prawdziwy baseline repozytorium: testy wyprowadzaja z niego "lepszy" i "gorszy" wynik, wiec nowy pomiar
# nie lamie testow. Lepszy = 100% zabitych (dziala dla kazdego baseline < 100), gorszy = 1 zabity na 10.
BASELINE_SCORE = json.loads((KORZEN / "cicd" / "mutation-baseline.json").read_text(encoding="utf-8"))["score"]
WYNIK_LEPSZY = 100.0
GALAZ_DOCELOWA = "main"
SHA_SCALENIA = "merge9999"
# Wartosci, ktore GitHub Actions wstawia za wyrazenia w `env` kroku.
WYRAZENIA = {
    "${{ github.base_ref }}": GALAZ_DOCELOWA,
    "${{ github.sha }}": SHA_SCALENIA,
}
# Wyjatek od zakazu ukrywania bledu: skladany, zeby ten plik nie zawieral literalnego wzorca.
UKRYCIE_BLEDU = re.compile(r"\|\|\s*" + "tr" + "ue")

# Atrapa zapisuje kazde wywolanie (argumenty) do dziennika; kod wyjscia i raport bierze ze srodowiska.
ATRAPA_MUTMUT = """#!/bin/sh
echo "$*" >> "$ATRAPA_LOG_MUTMUT"
case "$1" in
  run)
    mkdir -p mutants
    echo "$ATRAPA_WYJSCIE_RUN"
    exit "$ATRAPA_KOD_RUN"
    ;;
  results)
    cat "$ATRAPA_RAPORT"
    ;;
esac
"""
ATRAPA_GIT = "#!/bin/sh\nexit 0\n"
# Atrapa tools/mutanty_zmian.py (HAR-139): zapisuje argv, wypisuje to, co kaze srodowisko, i konczy zadanym kodem.
ATRAPA_MUTANTY_ZMIAN = """import os
import sys

with open(os.environ["ATRAPA_ARGV_ZMIAN"], "w", encoding="utf-8") as plik:
    plik.write(" ".join(sys.argv[1:]))
sys.stdout.write(os.environ["ATRAPA_STDOUT_ZMIAN"])
sys.stderr.write(os.environ["ATRAPA_STDERR_ZMIAN"])
sys.exit(int(os.environ["ATRAPA_KOD_ZMIAN"]))
"""
NAGLOWEK_ZMIAN = "## Mutanty zmienionych funkcji"
PRZEZYWAJACE = [
    "cockpit.services.runs.x_hash_token__mutmut_3",
    "cockpit.services.invariants.x_profil_roli__mutmut_7",
]


# --- workflow -------------------------------------------------------------------


def _workflow() -> dict:
    return YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))


def _wyzwalacze() -> dict:
    # YAML 1.1 czyta `on` jako True; ruamel (1.2) jako napis. Obslugujemy oba.
    workflow = _workflow()
    return workflow["on"] if "on" in workflow else workflow[True]


def _zadanie(nazwa: str) -> dict:
    return _workflow()["jobs"][nazwa]


def _krok(zadanie: str, nazwa: str) -> dict:
    return next(k for k in _zadanie(zadanie)["steps"] if k.get("name") == nazwa)


def _skrypty(zadanie: str) -> list[str]:
    return [k["run"] for k in _zadanie(zadanie)["steps"] if "run" in k]


def _linie_skryptow(zadanie: str) -> list[str]:
    return [linia for skrypt in _skrypty(zadanie) for linia in skrypt.splitlines()]


def _kroki_uses(zadanie: str, prefiks: str) -> list[dict]:
    return [k for k in _zadanie(zadanie)["steps"] if str(k.get("uses", "")).startswith(prefiks)]


def _artefakt(zadanie: str, nazwa: str) -> dict:
    return next(k for k in _kroki_uses(zadanie, "actions/upload-artifact") if k["with"]["name"] == nazwa)


# --- uruchamianie kroku ---------------------------------------------------------


def _polecenie_powloki(krok: dict, skrypt: Path) -> list[str]:
    # GitHub Actions: bez `shell` jest `bash -e`, z `shell: bash` dochodzi pipefail.
    powloka = krok.get("shell")
    if powloka is None:
        return ["bash", "-e", str(skrypt)]
    assert powloka == "bash", f"test nie zna powloki {powloka!r}"
    return ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(skrypt)]


def _atrapa(katalog: Path, nazwa: str, tresc: str) -> None:
    plik = katalog / nazwa
    plik.write_text(tresc, encoding="utf-8")
    plik.chmod(plik.stat().st_mode | stat.S_IXUSR)


def _linia_mutanta(numer: int, status: str) -> str:
    return f"    cockpit.services.runs.x_hash_token__mutmut_{numer}: {status}"


def _srodowisko_kroku(krok: dict) -> dict[str, str]:
    """`env` kroku z YAML z podstawionymi wyrazeniami GitHub Actions; nieznane wyrazenie to blad testu."""
    wynik = {}
    for nazwa, wartosc in (krok.get("env") or {}).items():
        assert wartosc in WYRAZENIA, f"env {nazwa}: test nie podstawia {wartosc!r}"
        wynik[nazwa] = WYRAZENIA[wartosc]
    return wynik


class Przebieg:
    def __init__(self, kod: int, wyjscie: str, baseline: Path, oryginal: bytes, praca: Path, tmp_path: Path):
        self.kod = kod
        self.wyjscie = wyjscie
        self.baseline = baseline
        self.oryginal = oryginal
        self.praca = praca
        self.argv_zmian = tmp_path / "argv-zmian.txt"
        self.log_mutmut = tmp_path / "log-mutmut.txt"
        self.podsumowanie_plik = tmp_path / "podsumowanie.md"

    @property
    def linie(self) -> list[str]:
        return self.wyjscie.splitlines()

    @property
    def podsumowanie(self) -> str:
        if not self.podsumowanie_plik.exists():
            return ""
        return self.podsumowanie_plik.read_text(encoding="utf-8")

    @property
    def wywolania_mutmut(self) -> list[str]:
        if not self.log_mutmut.exists():
            return []
        return self.log_mutmut.read_text(encoding="utf-8").splitlines()

    @property
    def baseline_niezmieniony(self) -> bool:
        return self.baseline.read_bytes() == self.oryginal

    @property
    def jest_blad_ci(self) -> bool:
        return any(linia.startswith("::error::") for linia in self.linie)

    @property
    def score_w_baseline(self) -> float:
        return self._baseline_json()["score"]

    @property
    def commit_w_baseline(self) -> str:
        return self._baseline_json()["commit"]

    def _baseline_json(self) -> dict:
        return json.loads(self.baseline.read_text(encoding="utf-8"))


def _uruchom_powloke(polecenie: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    proces = uruchom_proces_sync(
        polecenie, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        wyjscie, _ = proces.communicate(timeout=60)
    finally:
        zakoncz_proces_sync(proces, nazwa="krok workflow mutacji")
    return proces.returncode, wyjscie


def uruchom_krok(
    tmp_path: Path,
    zadanie: str,
    nazwa_kroku: str,
    *,
    statusy: list[str] | None = None,
    kod_run: int = 0,
    wyjscie_run: str = "",
    mutmut_na_path: bool = True,
    kod_zmian: int = 0,
    stdout_zmian: str = "",
    stderr_zmian: str = "",
    tresc_baseline: str | None = None,
) -> Przebieg:
    praca = tmp_path / "praca"
    (praca / "cicd").mkdir(parents=True)
    (praca / "tools").mkdir()
    _atrapa(praca / "tools", "mutanty_zmian.py", ATRAPA_MUTANTY_ZMIAN)
    for nazwa in ("mutation_report.py", "mutation_baseline.py", "mutation-baseline.json"):
        shutil.copy(KORZEN / "cicd" / nazwa, praca / "cicd" / nazwa)
    baseline = praca / "cicd" / "mutation-baseline.json"
    if tresc_baseline is not None:
        # Uszkadzamy wylacznie kopie w tmp; prawdziwy baseline repozytorium zostaje nietkniety.
        baseline.write_text(tresc_baseline, encoding="utf-8")

    narzedzia = tmp_path / "narzedzia"
    narzedzia.mkdir()
    # Bez katalogu venv na PATH: tam siedzi prawdziwy mutmut.
    (narzedzia / "python3").symlink_to(sys.executable)
    atrapy = tmp_path / "atrapy"
    atrapy.mkdir()
    _atrapa(atrapy, "git", ATRAPA_GIT)
    if mutmut_na_path:
        _atrapa(atrapy, "mutmut", ATRAPA_MUTMUT)

    przebieg = Przebieg(0, "", baseline, baseline.read_bytes(), praca, tmp_path)
    raport = tmp_path / "raport-atrapy.txt"
    raport.write_text(
        "\n".join(_linia_mutanta(i, s) for i, s in enumerate(statusy or [], 1)) + "\n", encoding="utf-8"
    )

    krok = _krok(zadanie, nazwa_kroku)
    skrypt_krok = krok["run"]
    assert "${{" not in skrypt_krok, "skrypt kroku uzywa wyrazenia GitHub Actions zamiast zmiennej srodowiska"
    skrypt = tmp_path / "krok.sh"
    skrypt.write_text(skrypt_krok, encoding="utf-8")

    srodowisko = {
        "PATH": f"{atrapy}:{narzedzia}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ATRAPA_KOD_RUN": str(kod_run),
        "ATRAPA_WYJSCIE_RUN": wyjscie_run,
        "ATRAPA_RAPORT": str(raport),
        "ATRAPA_LOG_MUTMUT": str(przebieg.log_mutmut),
        "ATRAPA_KOD_ZMIAN": str(kod_zmian),
        "ATRAPA_STDOUT_ZMIAN": stdout_zmian,
        "ATRAPA_STDERR_ZMIAN": stderr_zmian,
        "ATRAPA_ARGV_ZMIAN": str(przebieg.argv_zmian),
        "GITHUB_STEP_SUMMARY": str(przebieg.podsumowanie_plik),
        **_srodowisko_kroku(krok),
    }
    przebieg.kod, przebieg.wyjscie = _uruchom_powloke(_polecenie_powloki(krok, skrypt), praca, srodowisko)
    return przebieg


def _krok_pr(tmp_path: Path, **kw) -> Przebieg:
    return uruchom_krok(tmp_path, ZADANIE_PR, KROK_PR, **kw)


def _krok_main(tmp_path: Path, **kw) -> Przebieg:
    return uruchom_krok(tmp_path, ZADANIE_MAIN, KROK_MAIN, **kw)


def _lepszy_od_baseline() -> list[str]:
    # 100% > baseline: bez wczesnego wyjscia zapadka podniosalaby baseline.
    assert BASELINE_SCORE < WYNIK_LEPSZY, "baseline 100: lepszy wynik niemozliwy, test wymaga innego kryterium"
    return ["killed"] * 8


# =============================================================================
# Zadanie `mutation` na pull_request: tylko zmienione funkcje, wymagana bramka
# =============================================================================


def test_zadanie_mutation_odpala_sie_na_pull_request_a_nie_na_push():
    assert "pull_request" in _wyzwalacze()
    assert _zadanie(ZADANIE_PR)["if"] == "github.event_name == 'pull_request'"


def test_krok_zmienionych_funkcji_dostaje_galaz_docelowa_pull_requestu_ze_zmiennej_srodowiska():
    krok = _krok(ZADANIE_PR, KROK_PR)

    assert krok["env"]["BASE_REF"] == "${{ github.base_ref }}"
    assert "${{" not in krok["run"]
    assert '"$BASE_REF"' in krok["run"]


def test_mutanty_zmian_bez_przezywajacych_daja_zielony_krok_i_czysto_w_podsumowaniu(tmp_path):
    przebieg = _krok_pr(tmp_path, kod_zmian=0)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not przebieg.jest_blad_ci
    assert NAGLOWEK_ZMIAN in przebieg.podsumowanie
    assert "czysto" in przebieg.podsumowanie.split(NAGLOWEK_ZMIAN, 1)[1]


def test_przezywajace_mutanty_zmienionych_funkcji_oblewaja_krok_i_sa_wypisane_w_podsumowaniu(tmp_path):
    przebieg = _krok_pr(tmp_path, kod_zmian=1, stdout_zmian="\n".join(PRZEZYWAJACE) + "\n")

    assert przebieg.kod != 0, przebieg.wyjscie
    assert NAGLOWEK_ZMIAN in przebieg.podsumowanie
    pod_naglowkiem = przebieg.podsumowanie.split(NAGLOWEK_ZMIAN, 1)[1]
    for klucz in PRZEZYWAJACE:
        assert klucz in pod_naglowkiem
    assert "czysto" not in przebieg.podsumowanie


def test_blad_narzedzia_mutantow_zmian_oblewa_krok_i_stderr_trafia_do_podsumowania(tmp_path):
    przebieg = _krok_pr(tmp_path, kod_zmian=2, stderr_zmian="nieznana baza origin/main")

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "nieznana baza origin/main" in przebieg.podsumowanie
    assert "czysto" not in przebieg.podsumowanie


def test_nieoczekiwany_kod_narzedzia_mutantow_zmian_tez_oblewa_krok(tmp_path):
    # Zielone sa tylko kody 0 i 3 (HAR-144); kod 4 nie jest "tylko 1 i 2 sa bledem".
    przebieg = _krok_pr(tmp_path, kod_zmian=4, stderr_zmian="nieoczekiwany stan narzedzia")

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "nieoczekiwany stan narzedzia" in przebieg.podsumowanie
    assert "czysto" not in przebieg.podsumowanie


def test_zmiany_poza_zasiegiem_mutmuta_daja_zielony_krok_z_lista_do_recznego_sprawdzenia_nie_czysto(tmp_path):
    wpisy = ["src/cockpit/db/models.py:12", "src/cockpit/services/stale.py:3"]
    # Stdout bez nagłówka: tekst "bez mutantów — sprawdź ręcznie" w podsumowaniu musi pochodzić z kroku.
    stdout = "\n".join(wpisy) + "\n"

    przebieg = _krok_pr(
        tmp_path, kod_zmian=3, stdout_zmian=stdout, stderr_zmian="wskazowka narzedzia"
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not przebieg.jest_blad_ci
    assert NAGLOWEK_ZMIAN in przebieg.podsumowanie
    pod_naglowkiem = przebieg.podsumowanie.split(NAGLOWEK_ZMIAN, 1)[1]
    assert "### bez mutantów — sprawdź ręcznie" in pod_naglowkiem.splitlines()
    assert "::warning::" in przebieg.wyjscie
    for wpis in wpisy:
        assert wpis in pod_naglowkiem
    assert "czysto" not in przebieg.podsumowanie


def test_narzedzie_mutantow_zmian_dostaje_baze_z_galezi_docelowej_pull_requestu(tmp_path):
    przebieg = _krok_pr(tmp_path)

    assert przebieg.argv_zmian.exists(), przebieg.wyjscie
    argv = przebieg.argv_zmian.read_text(encoding="utf-8").split()
    assert argv[argv.index("--baza") + 1] == f"origin/{GALAZ_DOCELOWA}"


def test_brak_narzedzia_mutacyjnego_na_runnerze_oblewa_krok_pull_requestu_bez_wolania_mutantow_zmian(tmp_path):
    # Brak dowodu to nie zielone: wymagany check nie moze przejsc, gdy nic nie zmierzono.
    przebieg = _krok_pr(tmp_path, mutmut_na_path=False, kod_zmian=0)

    assert przebieg.kod != 0, przebieg.wyjscie
    assert not przebieg.argv_zmian.exists()
    assert "czysto" not in przebieg.podsumowanie


def test_zadanie_pull_requestu_nie_wola_pelnego_przebiegu_mutmut(tmp_path):
    przebieg = _krok_pr(tmp_path, statusy=_lepszy_od_baseline(), kod_zmian=0)

    assert przebieg.kod == 0, przebieg.wyjscie
    # Dziennik atrapy: `mutmut run` bez argumentow to pelny przebieg; nie ma prawa sie tu pojawic.
    assert "run" not in [wywolanie.strip() for wywolanie in przebieg.wywolania_mutmut]


def test_zadanie_pull_requestu_nie_liczy_raportu_ani_nie_rusza_zapadki(tmp_path):
    przebieg = _krok_pr(tmp_path, statusy=_lepszy_od_baseline(), kod_zmian=0)

    assert przebieg.kod == 0, przebieg.wyjscie
    # Gdyby zapadka ruszyla, 100% zabitych podniosloby baseline, a raport zostawilby podsumowanie.
    assert przebieg.baseline_niezmieniony
    assert not (przebieg.praca / "mutation-summary.json").exists()


def test_zadanie_pull_requestu_nie_zawiera_pelnego_mutmut_run_raportu_ani_zapadki():
    for skrypt in _skrypty(ZADANIE_PR):
        assert "cicd/mutation_baseline.py" not in skrypt
        assert "cicd/mutation_report.py" not in skrypt
        for linia in skrypt.splitlines():
            assert not re.search(r"\bmutmut\s+run\s*($|[;&|)])", linia), linia


def test_cache_mutantow_w_zadaniu_pull_requestu_jest_tylko_do_odczytu():
    cache = _kroki_uses(ZADANIE_PR, "actions/cache")

    assert [krok["uses"] for krok in cache] == ["actions/cache/restore@v4"]
    assert "mutants" in cache[0]["with"]["path"]


def test_zadanie_pull_requestu_publikuje_liste_przezywajacych_mutantow_zmian_nawet_po_czerwonym_kroku():
    artefakt = _artefakt(ZADANIE_PR, "mutation-report")

    assert artefakt["if"] == "always()"
    assert "mutants/mutanty-zmian.txt" in artefakt["with"]["path"].splitlines()


def test_zadanie_pull_requestu_pisze_podsumowanie_do_pliku_zadania():
    assert "$GITHUB_STEP_SUMMARY" in _krok(ZADANIE_PR, KROK_PR)["run"]


# =============================================================================
# Zadanie `mutation-main` po scaleniu na main: pelny przebieg i zapadka
# =============================================================================


def test_pelny_przebieg_mutacyjny_odpala_sie_tylko_na_push_do_main():
    wyzwalacze = _wyzwalacze()

    assert wyzwalacze["push"]["branches"] == ["main"]
    assert _zadanie(ZADANIE_MAIN)["if"] == "github.event_name == 'push'"


def test_krok_pelnego_przebiegu_dostaje_commit_scalenia_ze_zmiennej_srodowiska():
    krok = _krok(ZADANIE_MAIN, KROK_MAIN)

    assert krok["env"]["COMMIT_SHA"] == "${{ github.sha }}"
    assert "${{" not in krok["run"]
    assert '"$COMMIT_SHA"' in krok["run"]


def test_pelny_przebieg_mutacyjny_wola_mutmut_run_i_zbiera_raport(tmp_path):
    przebieg = _krok_main(tmp_path, statusy=_lepszy_od_baseline())

    assert przebieg.kod == 0, przebieg.wyjscie
    assert "run" in [wywolanie.strip() for wywolanie in przebieg.wywolania_mutmut]
    assert any(w.startswith("results") for w in przebieg.wywolania_mutmut)
    assert (przebieg.praca / "mutants" / "mutmut-results.txt").read_text(encoding="utf-8").count("killed") == 8


def test_pelny_przebieg_bez_startu_testow_oblewa_zadanie_mimo_starego_raportu(tmp_path):
    # Stary cache ma zabite mutanty, wiec tylko kod wyjscia `mutmut run` zdradza, ze testy nie ruszyly.
    przebieg = _krok_main(
        tmp_path,
        kod_run=1,
        wyjscie_run="failed to collect stats. runner returned 1",
        statusy=["killed"] * 9 + ["survived"],
    )

    assert przebieg.kod != 0
    assert przebieg.jest_blad_ci, przebieg.wyjscie
    assert przebieg.baseline_niezmieniony


def test_pelny_przebieg_bez_zabitego_mutanta_oblewa_zadanie_i_nie_zapisuje_baseline(tmp_path):
    przebieg = _krok_main(tmp_path, statusy=["survived"] * 4 + ["no tests"] * 2)

    assert przebieg.kod != 0
    assert przebieg.jest_blad_ci, przebieg.wyjscie
    assert przebieg.baseline_niezmieniony


def test_pelny_przebieg_z_wynikiem_lepszym_od_baseline_podnosi_baseline(tmp_path):
    # 100% zabitych jest powyzej kazdego baseline < 100; sprawdzamy wartosc w pliku, nie sam kod wyjscia.
    przebieg = _krok_main(tmp_path, statusy=_lepszy_od_baseline())

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not przebieg.jest_blad_ci
    assert przebieg.score_w_baseline == WYNIK_LEPSZY


def test_baseline_zapisany_po_scaleniu_niesie_sha_commitu_z_main(tmp_path):
    przebieg = _krok_main(tmp_path, statusy=_lepszy_od_baseline())

    assert przebieg.kod == 0, przebieg.wyjscie
    assert przebieg.commit_w_baseline == SHA_SCALENIA


def test_pelny_przebieg_z_wynikiem_gorszym_od_baseline_nie_oblewa_zadania_i_zostawia_baseline(tmp_path):
    # Spadek score nie jest bramka, ale nie wolno tez obnizyc zapadki.
    przebieg = _krok_main(tmp_path, statusy=["killed"] + ["survived"] * 9)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not przebieg.jest_blad_ci
    assert przebieg.baseline_niezmieniony
    assert przebieg.score_w_baseline == BASELINE_SCORE


def test_pelny_przebieg_bez_narzedzia_mutacyjnego_na_runnerze_oblewa_zadanie_bez_zmiany_baseline(tmp_path):
    przebieg = _krok_main(tmp_path, statusy=_lepszy_od_baseline(), mutmut_na_path=False)

    assert przebieg.kod != 0, przebieg.wyjscie
    assert przebieg.baseline_niezmieniony
    assert not (przebieg.praca / "mutants").exists()


def test_uszkodzony_baseline_po_scaleniu_oblewa_krok_z_linia_bledu_ci_zamiast_zielonego(tmp_path):
    # Zapadka nie moze ukryc bledu wejscia (zly JSON baseline): brak dowodu to nie zielone.
    przebieg = _krok_main(tmp_path, statusy=_lepszy_od_baseline(), tresc_baseline="{to nie jest json")

    assert przebieg.kod != 0, przebieg.wyjscie
    assert przebieg.jest_blad_ci, przebieg.wyjscie
    assert przebieg.baseline.read_text(encoding="utf-8") == "{to nie jest json"


def test_cache_mutantow_w_zadaniu_po_scaleniu_jest_do_odczytu_i_zapisu():
    cache = _kroki_uses(ZADANIE_MAIN, "actions/cache")

    assert [krok["uses"] for krok in cache] == ["actions/cache@v4"]
    assert "mutants" in cache[0]["with"]["path"]


def test_zadanie_po_scaleniu_publikuje_raport_mutacji_nawet_po_czerwonym_kroku():
    artefakt = _artefakt(ZADANIE_MAIN, "mutation-report")

    assert artefakt["if"] == "always()"


# =============================================================================
# Wspolne: klucz cache, brak ukrywania bledow
# =============================================================================


def _cache(zadanie: str) -> dict:
    return _kroki_uses(zadanie, "actions/cache")[0]["with"]


HASH_TESTOW = "hashFiles('tests/**', 'pyproject.toml')"
PREFIKS_OGOLNY = "mutmut-v1-"


def _klucze_przywracania(zadanie: str) -> list[str]:
    surowe = _cache(zadanie)["restore-keys"]
    wpisy = surowe.splitlines() if isinstance(surowe, str) else list(surowe)
    return [wpis.strip() for wpis in wpisy if wpis.strip()]


def test_oba_zadania_dziela_klucz_cache_mutantow_i_prefiks_zgodny_z_testami():
    pr, main = _cache(ZADANIE_PR), _cache(ZADANIE_MAIN)

    assert pr["key"] == main["key"]
    assert _klucze_przywracania(ZADANIE_PR) == _klucze_przywracania(ZADANIE_MAIN)


def test_klucz_cache_mutantow_zmienia_sie_z_kodem_testami_i_konfiguracja():
    klucz = _cache(ZADANIE_MAIN)["key"]
    granica = klucz.rindex("-${{")
    testy_i_konfiguracja, kod = klucz[:granica], klucz[granica:]

    assert "tests/**" in testy_i_konfiguracja
    assert "pyproject.toml" in testy_i_konfiguracja
    assert "src/**" in kod
    assert "src/**" not in testy_i_konfiguracja


def test_prefiks_przywracania_cache_pasuje_do_poczatku_klucza():
    for zadanie in (ZADANIE_PR, ZADANIE_MAIN):
        klucz = _cache(zadanie)["key"]
        prefiksy = _klucze_przywracania(zadanie)

        assert prefiksy
        for prefiks in prefiksy:
            assert klucz.startswith(prefiks), (zadanie, prefiks)


def test_plik_testu_usuniety_w_zmianie_nie_zostaje_w_kopii_mutmuta_bo_zadne_zadanie_nie_przywraca_cache_z_innymi_testami():
    # mutmut tylko dokladuje pliki do mutants/tests, wiec cache z innych testow zachowalby usuniety test.
    for zadanie in (ZADANIE_PR, ZADANIE_MAIN):
        prefiksy = _klucze_przywracania(zadanie)

        assert prefiksy, zadanie
        assert PREFIKS_OGOLNY not in prefiksy, zadanie
        for prefiks in prefiksy:
            assert HASH_TESTOW in prefiks, (zadanie, prefiks)


def test_zadania_mutacyjne_nie_swieca_na_zielono_mimo_czerwonego_kroku():
    for nazwa in (ZADANIE_PR, ZADANIE_MAIN):
        zadanie = _zadanie(nazwa)
        assert zadanie.get("continue-on-error") not in (True, "true")
        for krok in zadanie["steps"]:
            assert krok.get("continue-on-error") not in (True, "true"), krok


def test_zadanie_pull_requestu_nigdzie_nie_ukrywa_bledu_komendy():
    for linia in _linie_skryptow(ZADANIE_PR):
        assert not UKRYCIE_BLEDU.search(linia), linia


def test_zadanie_po_scaleniu_nigdzie_nie_ukrywa_bledu_komendy_takze_w_zapadce():
    for linia in _linie_skryptow(ZADANIE_MAIN):
        assert not UKRYCIE_BLEDU.search(linia), linia


# =============================================================================
# HAR-150: kod mutantow zmian trafia do pliku, krok `mutanty ręczne` wymaga tabeli
# =============================================================================

KROK_RECZNE = "mutanty ręczne"
KOD_ZMIAN_PLIK = "mutants/mutanty-zmian.kod"
TABELA_A = "tests/mutanty_reczne/HAR-1.toml"
TABELA_B = "tests/mutanty_reczne/HAR-2.toml"
# Atrapa git: zapisuje argumenty; `diff` z `--diff-filter=d` zwraca tylko tabele, drugi `diff`
# wszystkie zmienione pliki (tabele tez). Reszta polecen (np. fetch) konczy sie sukcesem.
ATRAPA_GIT_ZMIANY = """#!/bin/sh
echo "$*" >> "$ATRAPA_LOG_GIT"
case "$1" in
  diff)
    case "$*" in
      *--diff-filter=d*)
        [ -n "$ATRAPA_TABELE" ] && printf '%s\\n' "$ATRAPA_TABELE"
        exit 0
        ;;
      *)
        [ -n "$ATRAPA_ZMIANY" ] && printf '%s\\n' "$ATRAPA_ZMIANY"
        exit 0
        ;;
    esac
    ;;
esac
exit 0
"""
# Atrapa tools/mutanty_reczne.py: dopisuje argv wywolania do pliku, wypisuje i konczy sie zadanym kodem.
ATRAPA_MUTANTY_RECZNE = """import os
import sys

with open(os.environ["ATRAPA_ARGV_RECZNE"], "a", encoding="utf-8") as plik:
    plik.write(" ".join(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ["ATRAPA_STDOUT_RECZNE"])
sys.stderr.write(os.environ["ATRAPA_STDERR_RECZNE"])
sys.exit(int(os.environ["ATRAPA_KOD_RECZNE"]))
"""


class PrzebiegRecznych:
    def __init__(self, tmp_path: Path, praca: Path):
        self.praca = praca
        self.kod = -1
        self.wyjscie = ""
        self.argv_plik = tmp_path / "argv-reczne.txt"
        self.log_git_plik = tmp_path / "log-git.txt"
        self.podsumowanie_plik = tmp_path / "podsumowanie.md"

    @property
    def wywolania_skryptu(self) -> list[str]:
        if not self.argv_plik.exists():
            return []
        return self.argv_plik.read_text(encoding="utf-8").splitlines()

    @property
    def wywolania_git(self) -> list[str]:
        if not self.log_git_plik.exists():
            return []
        return self.log_git_plik.read_text(encoding="utf-8").splitlines()

    @property
    def podsumowanie(self) -> str:
        if not self.podsumowanie_plik.exists():
            return ""
        return self.podsumowanie_plik.read_text(encoding="utf-8")


def uruchom_krok_recznych(
    tmp_path: Path,
    *,
    zmiany: tuple[str, ...] = (),
    tabele: tuple[str, ...] = (),
    usuniete_tabele: tuple[str, ...] = (),
    kod_zmian: int | None = 0,
    kod_recznych: int = 0,
    stdout_recznych: str = "",
    stderr_recznych: str = "",
    base_ref: str | None = None,
) -> PrzebiegRecznych:
    """Uruchamia `run` kroku `mutanty ręczne` z atrapami git i tools/mutanty_reczne.py.

    `kod_zmian=None` to brak pliku `mutants/mutanty-zmian.kod`. Lista zmian z gita to `zmiany`,
    `tabele` i `usuniete_tabele`; wywolanie z `--diff-filter=d` zwraca wylacznie `tabele`.
    """
    praca = tmp_path / "praca"
    (praca / "tools").mkdir(parents=True)
    (praca / "mutants").mkdir()
    _atrapa(praca / "tools", "mutanty_reczne.py", ATRAPA_MUTANTY_RECZNE)
    if kod_zmian is not None:
        (praca / KOD_ZMIAN_PLIK).write_text(f"{kod_zmian}\n", encoding="utf-8")

    narzedzia = tmp_path / "narzedzia"
    narzedzia.mkdir()
    (narzedzia / "python3").symlink_to(sys.executable)
    atrapy = tmp_path / "atrapy"
    atrapy.mkdir()
    _atrapa(atrapy, "git", ATRAPA_GIT_ZMIANY)

    przebieg = PrzebiegRecznych(tmp_path, praca)
    krok = _krok(ZADANIE_PR, KROK_RECZNE)
    skrypt_krok = krok["run"]
    assert "${{" not in skrypt_krok, "skrypt kroku uzywa wyrazenia GitHub Actions zamiast zmiennej srodowiska"
    skrypt = tmp_path / "krok.sh"
    skrypt.write_text(skrypt_krok, encoding="utf-8")

    srodowisko = {
        "PATH": f"{atrapy}:{narzedzia}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ATRAPA_LOG_GIT": str(przebieg.log_git_plik),
        "ATRAPA_ZMIANY": "\n".join([*zmiany, *tabele, *usuniete_tabele]),
        "ATRAPA_TABELE": "\n".join(tabele),
        "ATRAPA_ARGV_RECZNE": str(przebieg.argv_plik),
        "ATRAPA_KOD_RECZNE": str(kod_recznych),
        "ATRAPA_STDOUT_RECZNE": stdout_recznych,
        "ATRAPA_STDERR_RECZNE": stderr_recznych,
        "GITHUB_STEP_SUMMARY": str(przebieg.podsumowanie_plik),
        **_srodowisko_kroku(krok),
    }
    if base_ref is not None:
        srodowisko["BASE_REF"] = base_ref
    przebieg.kod, przebieg.wyjscie = _uruchom_powloke(_polecenie_powloki(krok, skrypt), praca, srodowisko)
    return przebieg


# --- struktura kroku ----------------------------------------------------------


def test_krok_mutanty_reczne_stoi_w_zadaniu_pull_requestu_zaraz_po_kroku_zmienionych_funkcji():
    nazwy = [k.get("name") for k in _zadanie(ZADANIE_PR)["steps"]]

    assert nazwy.index(KROK_RECZNE) == nazwy.index(KROK_PR) + 1


def test_krok_mutanty_reczne_dostaje_galaz_docelowa_ze_zmiennej_srodowiska():
    krok = _krok(ZADANIE_PR, KROK_RECZNE)

    assert krok["env"]["BASE_REF"] == "${{ github.base_ref }}"


def test_zaden_krok_run_zadania_pull_requestu_nie_uzywa_wyrazen_github_actions_w_skrypcie():
    for skrypt in _skrypty(ZADANIE_PR):
        assert "${{" not in skrypt


def test_zadanie_pull_requestu_publikuje_wynik_recznych_mutantow_nawet_po_czerwonym_kroku():
    artefakt = _artefakt(ZADANIE_PR, "mutation-report")
    sciezki = artefakt["with"]["path"].splitlines()

    assert artefakt["if"] == "always()"
    assert "mutants/mutanty-reczne.txt" in sciezki
    assert "mutants/mutanty-reczne.err" in sciezki


def test_krok_mutanty_reczne_pisze_podsumowanie_do_pliku_zadania():
    assert "$GITHUB_STEP_SUMMARY" in _krok(ZADANIE_PR, KROK_RECZNE)["run"]


# --- zapis kodu narzedzia mutantow zmian ---------------------------------------


@pytest.mark.parametrize(
    "kod",
    [
        pytest.param(0, id="czysto"),
        pytest.param(1, id="przezywajace_czerwony_krok"),
        pytest.param(2, id="blad_narzedzia_czerwony_krok"),
        pytest.param(3, id="poza_zasiegiem_mutmuta"),
        pytest.param(4, id="nieoczekiwany_kod"),
    ],
)
def test_krok_zmienionych_funkcji_zapisuje_kod_narzedzia_do_pliku_takze_gdy_krok_jest_czerwony(
    tmp_path, kod
):
    przebieg = _krok_pr(tmp_path, kod_zmian=kod, stderr_zmian="komunikat")

    plik = przebieg.praca / KOD_ZMIAN_PLIK
    assert plik.exists(), przebieg.wyjscie
    assert plik.read_text(encoding="utf-8").strip() == str(kod)


# --- krok `mutanty ręczne`: wymaga tabeli poza zasiegiem mutmuta ---------------


@pytest.mark.parametrize(
    "zmieniony",
    [
        pytest.param("web/src/App.tsx", id="web"),
        pytest.param("tools/x.py", id="tools"),
        pytest.param("cicd/x.py", id="cicd"),
    ],
)
def test_zmiana_w_katalogu_poza_zasiegiem_mutmuta_bez_tabeli_oblewa_krok_z_brakiem_dowodu(
    tmp_path, zmieniony
):
    przebieg = uruchom_krok_recznych(tmp_path, zmiany=(zmieniony,), kod_zmian=0)

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "brak dowodu" in przebieg.wyjscie
    assert "::error::" in przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


def test_zmiana_frontu_wsrod_zmian_w_src_bez_tabeli_tez_oblewa_krok(tmp_path):
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("src/cockpit/services/runs.py", "web/src/App.tsx"), kod_zmian=0
    )

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "brak dowodu" in przebieg.wyjscie


def test_kod_3_narzedzia_mutantow_zmian_bez_tabeli_oblewa_krok_z_brakiem_dowodu(tmp_path):
    przebieg = uruchom_krok_recznych(tmp_path, zmiany=("src/cockpit/db/models.py",), kod_zmian=3)

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "brak dowodu" in przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


def test_zmiana_w_zasiegu_mutmuta_bez_tabeli_jest_zielona_i_nie_wola_skryptu_recznych_mutantow(
    tmp_path,
):
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("src/cockpit/services/runs.py",), kod_zmian=0
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert "brak dowodu" not in przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


def test_brak_zmian_w_diffie_i_kod_0_jest_zielony_bez_wolania_skryptu(tmp_path):
    przebieg = uruchom_krok_recznych(tmp_path, zmiany=(), kod_zmian=0)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


@pytest.mark.parametrize(
    "zmieniony",
    [
        pytest.param("docs/web/x.md", id="web_nie_na_poczatku"),
        pytest.param("docs/tools/x.md", id="tools_nie_na_poczatku"),
        pytest.param("docs/cicd/x.md", id="cicd_nie_na_poczatku"),
        pytest.param("webapp/x.ts", id="web_bez_ukosnika"),
        pytest.param("tools_stare/x.py", id="tools_bez_ukosnika"),
        pytest.param("cicd-stare/x.yml", id="cicd_bez_ukosnika"),
    ],
)
def test_katalog_web_tools_cicd_liczy_sie_tylko_na_poczatku_sciezki_i_jako_caly_katalog(
    tmp_path, zmieniony
):
    przebieg = uruchom_krok_recznych(tmp_path, zmiany=(zmieniony,), kod_zmian=0)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert "brak dowodu" not in przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


@pytest.mark.parametrize(
    ("zmiany", "kod_zmian"),
    [
        pytest.param(("web/src/App.tsx",), 0, id="front_poza_zasiegiem"),
        pytest.param(("src/cockpit/services/runs.py",), 0, id="src_w_zasiegu"),
        pytest.param(("src/cockpit/db/models.py",), 3, id="kod_3_narzedzia"),
    ],
)
def test_zmieniona_tabela_i_zabite_mutanty_daja_zielony_krok_i_skrypt_dostaje_sciezke_tabeli(
    tmp_path, zmiany, kod_zmian
):
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=zmiany, tabele=(TABELA_A,), kod_zmian=kod_zmian, kod_recznych=0
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert "brak dowodu" not in przebieg.wyjscie
    assert [linia.split() for linia in przebieg.wywolania_skryptu] == [[TABELA_A]]


def test_skrypt_recznych_mutantow_dostaje_wszystkie_zmienione_tabele(tmp_path):
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("web/src/App.tsx",), tabele=(TABELA_A, TABELA_B)
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert len(przebieg.wywolania_skryptu) == 1
    assert set(przebieg.wywolania_skryptu[0].split()) == {TABELA_A, TABELA_B}


@pytest.mark.parametrize(
    "kod_recznych",
    [pytest.param(1, id="przezyli"), pytest.param(2, id="blad_narzedzia")],
)
def test_niezerowy_kod_skryptu_recznych_mutantow_oblewa_krok(tmp_path, kod_recznych):
    przebieg = uruchom_krok_recznych(
        tmp_path,
        zmiany=("web/src/App.tsx",),
        tabele=(TABELA_A,),
        kod_recznych=kod_recznych,
        stderr_recznych="blad: cos poszlo nie tak",
    )

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "::error::" in przebieg.wyjscie
    assert len(przebieg.wywolania_skryptu) == 1


def test_niezerowy_kod_skryptu_oblewa_krok_takze_gdy_zmiana_jest_w_zasiegu_mutmuta(tmp_path):
    # Tabela jest dowodem niezaleznie od zasiegu: jej czerwony wynik nie moze byc ignorowany.
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("src/cockpit/services/runs.py",), tabele=(TABELA_A,), kod_recznych=1
    )

    assert przebieg.kod != 0, przebieg.wyjscie


def test_przezywajacy_reczny_mutant_jest_wypisany_w_podsumowaniu_zadania(tmp_path):
    wiersz = "src/modul.py: return a + b → return b + a"

    przebieg = uruchom_krok_recznych(
        tmp_path,
        zmiany=("web/src/App.tsx",),
        tabele=(TABELA_A,),
        kod_recznych=1,
        stdout_recznych=wiersz + "\n",
    )

    assert przebieg.kod != 0, przebieg.wyjscie
    assert wiersz in przebieg.podsumowanie


def test_blad_skryptu_recznych_mutantow_pokazuje_komunikat_z_stderr(tmp_path):
    komunikat = "blad: szukaj wystepuje 2 razy"

    przebieg = uruchom_krok_recznych(
        tmp_path,
        zmiany=("web/src/App.tsx",),
        tabele=(TABELA_A,),
        kod_recznych=2,
        stderr_recznych=komunikat,
    )

    assert przebieg.kod != 0, przebieg.wyjscie
    assert komunikat in przebieg.wyjscie + przebieg.podsumowanie


@pytest.mark.parametrize(
    ("zmiany", "tabele"),
    [
        pytest.param(("web/src/App.tsx",), (TABELA_A,), id="tabela_i_front"),
        pytest.param(("src/cockpit/services/runs.py",), (), id="sam_src"),
        pytest.param(("src/cockpit/services/runs.py",), (TABELA_A,), id="tabela_i_src"),
    ],
)
def test_brak_pliku_z_kodem_mutantow_zmian_oblewa_krok_z_brakiem_dowodu(
    tmp_path, zmiany, tabele
):
    # Bez pliku nie wiadomo, czy mutmut pokryl zmiane; brak sygnalu to nie zielone.
    przebieg = uruchom_krok_recznych(tmp_path, zmiany=zmiany, tabele=tabele, kod_zmian=None)

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "brak dowodu" in przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


def test_tabela_usunieta_w_pr_nie_jest_dowodem_dla_zmiany_frontu(tmp_path):
    # Diff z --diff-filter=d nie zwraca usunietej tabeli, wiec front zostaje bez dowodu.
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("web/src/App.tsx",), usuniete_tabele=(TABELA_A,), kod_zmian=0
    )

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "brak dowodu" in przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


def test_usunieta_tabela_nie_trafia_do_skryptu_gdy_zmiana_jest_w_zasiegu_mutmuta(tmp_path):
    przebieg = uruchom_krok_recznych(
        tmp_path,
        zmiany=("src/cockpit/services/runs.py",),
        usuniete_tabele=(TABELA_A,),
        kod_zmian=0,
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert przebieg.wywolania_skryptu == []


def test_lista_zmian_pochodzi_z_roznicy_wzgledem_galezi_docelowej_bez_wykrywania_przemianowan(
    tmp_path,
):
    # Galaz inna niz domyslna `main`: tylko krok czytajacy BASE_REF poda ja gitowi.
    inna_galaz = "release-7"
    assert inna_galaz != GALAZ_DOCELOWA
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("web/src/App.tsx",), tabele=(TABELA_A,), base_ref=inna_galaz
    )

    diffy = [w for w in przebieg.wywolania_git if w.startswith("diff")]
    assert len(diffy) >= 2, przebieg.wywolania_git
    for wywolanie in diffy:
        assert f"origin/{inna_galaz}...HEAD" in wywolanie
        assert "--name-only" in wywolanie
        assert "--no-renames" in wywolanie


def test_tabele_sa_wybierane_zapytaniem_gita_ograniczonym_do_katalogu_tabel_bez_usunietych(
    tmp_path,
):
    przebieg = uruchom_krok_recznych(
        tmp_path, zmiany=("web/src/App.tsx",), tabele=(TABELA_A,)
    )

    zapytania_o_tabele = [w for w in przebieg.wywolania_git if "--diff-filter=d" in w]
    assert len(zapytania_o_tabele) == 1, przebieg.wywolania_git
    assert "tests/mutanty_reczne/*.toml" in zapytania_o_tabele[0]

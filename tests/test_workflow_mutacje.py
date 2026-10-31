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
    # Zielony jest tylko kod 0; kod 3 nie jest "tylko 1 i 2 sa bledem".
    przebieg = _krok_pr(tmp_path, kod_zmian=3, stderr_zmian="nieoczekiwany stan narzedzia")

    assert przebieg.kod != 0, przebieg.wyjscie
    assert "nieoczekiwany stan narzedzia" in przebieg.podsumowanie
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

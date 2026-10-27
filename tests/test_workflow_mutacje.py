"""Krok mutacyjny w CI (.github/workflows/mutation.yml): kiedy zadanie ma swiecic na czerwono (HAR-132).

Czytamy prawdziwy workflow, wyciagamy skrypt kroku `przebieg mutacyjny i zapadka` i uruchamiamy
go powloka tak jak GitHub Actions, na kopii baseline w tmp_path. Prawdziwe `mutmut` i `git`
zastepuja atrapy na PATH; skutek czytamy z kodu kroku, jego wyjscia i pliku baseline.
"""
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOW = KORZEN / ".github" / "workflows" / "mutation.yml"
NAZWA_KROKU = "przebieg mutacyjny i zapadka"
BASELINE_SCORE = 71.64

# Atrapa pisze katalog mutants/ jak prawdziwy `mutmut run`, a kod wyjscia i raport bierze ze srodowiska.
ATRAPA_MUTMUT = """#!/bin/sh
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


def _workflow() -> dict:
    return YAML(typ="safe").load(WORKFLOW.read_text(encoding="utf-8"))


def _krok() -> dict:
    kroki = _workflow()["jobs"]["mutation"]["steps"]
    return next(k for k in kroki if k.get("name") == NAZWA_KROKU)


def _polecenie_powloki(skrypt: Path) -> list[str]:
    # GitHub Actions: bez `shell` jest `bash -e`, z `shell: bash` dochodzi pipefail.
    powloka = _krok().get("shell")
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


class Przebieg:
    def __init__(
        self,
        kod: int,
        wyjscie: str,
        baseline: Path,
        oryginal: bytes,
        podsumowanie: str = "",
        argv_zmian: Path | None = None,
    ):
        self.kod = kod
        self.wyjscie = wyjscie
        self.baseline = baseline
        self.oryginal = oryginal
        self.podsumowanie = podsumowanie
        self.argv_zmian = argv_zmian

    @property
    def linie(self) -> list[str]:
        return self.wyjscie.splitlines()

    @property
    def baseline_niezmieniony(self) -> bool:
        return self.baseline.read_bytes() == self.oryginal


def uruchom_krok(
    tmp_path: Path,
    *,
    kod_run: int = 0,
    wyjscie_run: str = "",
    statusy: list[str],
    mutmut_na_path: bool = True,
    kod_zmian: int = 0,
    stdout_zmian: str = "",
    stderr_zmian: str = "",
) -> Przebieg:
    praca = tmp_path / "praca"
    (praca / "cicd").mkdir(parents=True)
    (praca / "tools").mkdir()
    _atrapa(praca / "tools", "mutanty_zmian.py", ATRAPA_MUTANTY_ZMIAN)
    for nazwa in ("mutation_report.py", "mutation_baseline.py", "mutation-baseline.json"):
        shutil.copy(KORZEN / "cicd" / nazwa, praca / "cicd" / nazwa)
    baseline = praca / "cicd" / "mutation-baseline.json"

    narzedzia = tmp_path / "narzedzia"
    narzedzia.mkdir()
    # Bez katalogu venv na PATH: tam siedzi prawdziwy mutmut.
    (narzedzia / "python3").symlink_to(sys.executable)
    atrapy = tmp_path / "atrapy"
    atrapy.mkdir()
    _atrapa(atrapy, "git", ATRAPA_GIT)
    if mutmut_na_path:
        _atrapa(atrapy, "mutmut", ATRAPA_MUTMUT)

    argv_zmian = tmp_path / "argv-zmian.txt"
    podsumowanie = tmp_path / "podsumowanie.md"
    raport = tmp_path / "raport-atrapy.txt"
    raport.write_text("\n".join(_linia_mutanta(i, s) for i, s in enumerate(statusy, 1)) + "\n", encoding="utf-8")

    skrypt_krok = _krok()["run"]
    skrypt_krok = skrypt_krok.replace("${{ github.base_ref }}", "main").replace("${{ github.sha }}", "abc1234")
    assert "${{" not in skrypt_krok, "krok uzywa wyrazenia, ktorego test nie podstawia"
    skrypt = tmp_path / "krok.sh"
    skrypt.write_text(skrypt_krok, encoding="utf-8")

    srodowisko = {
        "PATH": f"{atrapy}:{narzedzia}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ATRAPA_KOD_RUN": str(kod_run),
        "ATRAPA_WYJSCIE_RUN": wyjscie_run,
        "ATRAPA_RAPORT": str(raport),
        "ATRAPA_KOD_ZMIAN": str(kod_zmian),
        "ATRAPA_STDOUT_ZMIAN": stdout_zmian,
        "ATRAPA_STDERR_ZMIAN": stderr_zmian,
        "ATRAPA_ARGV_ZMIAN": str(argv_zmian),
        "GITHUB_STEP_SUMMARY": str(podsumowanie),
    }
    wynik = subprocess.run(
        _polecenie_powloki(skrypt),
        cwd=praca,
        env=srodowisko,
        capture_output=True,
        text=True,
        timeout=60,
    )
    tresc = podsumowanie.read_text(encoding="utf-8") if podsumowanie.exists() else ""
    return Przebieg(wynik.returncode, wynik.stdout + wynik.stderr, baseline, baseline.read_bytes(), tresc, argv_zmian)


def _score_w_baseline(przebieg: Przebieg) -> float:
    return json.loads(przebieg.baseline.read_text(encoding="utf-8"))["score"]


def _jest_blad_ci(przebieg: Przebieg) -> bool:
    return any(linia.startswith("::error::") for linia in przebieg.linie)


def test_przebieg_mutacyjny_bez_startu_testow_oblewa_zadanie_mimo_starego_raportu(tmp_path):
    # Stary cache ma zabite mutanty, wiec tylko kod wyjscia `mutmut run` zdradza, ze testy nie ruszyly.
    przebieg = uruchom_krok(
        tmp_path,
        kod_run=1,
        wyjscie_run="failed to collect stats. runner returned 1",
        statusy=["killed"] * 9 + ["survived"],
    )

    assert przebieg.kod != 0
    assert _jest_blad_ci(przebieg), przebieg.wyjscie
    assert przebieg.baseline_niezmieniony


def test_przebieg_mutacyjny_bez_zabitego_mutanta_oblewa_zadanie_i_nie_zapisuje_baseline(tmp_path):
    przebieg = uruchom_krok(tmp_path, statusy=["survived"] * 4 + ["no tests"] * 2)

    assert przebieg.kod != 0
    assert _jest_blad_ci(przebieg), przebieg.wyjscie
    assert przebieg.baseline_niezmieniony


def test_przebieg_mutacyjny_z_wynikiem_lepszym_od_baseline_podnosi_baseline(tmp_path):
    # 7 z 8 = 87.5, wyraznie powyzej 71.64; sprawdzamy wartosc w pliku, nie sam kod wyjscia.
    przebieg = uruchom_krok(tmp_path, statusy=["killed"] * 7 + ["survived"])

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not _jest_blad_ci(przebieg)
    assert _score_w_baseline(przebieg) == 87.5


def test_przebieg_mutacyjny_z_wynikiem_gorszym_od_baseline_nie_oblewa_zadania_i_zostawia_baseline(tmp_path):
    # Spadek score nie jest bramka, ale nie wolno tez obnizyc zapadki.
    przebieg = uruchom_krok(tmp_path, statusy=["killed"] + ["survived"] * 9)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not _jest_blad_ci(przebieg)
    assert przebieg.baseline_niezmieniony
    assert _score_w_baseline(przebieg) == BASELINE_SCORE


def test_przebieg_mutacyjny_bez_narzedzia_na_runnerze_konczy_sie_sukcesem_bez_zmiany_baseline(tmp_path):
    przebieg = uruchom_krok(tmp_path, statusy=["killed"] * 9 + ["survived"], mutmut_na_path=False)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert przebieg.baseline_niezmieniony
    assert not (przebieg.baseline.parents[1] / "mutants").exists()


def test_zadanie_mutacyjne_nie_swieci_na_zielono_mimo_czerwonego_kroku():
    zadanie = _workflow()["jobs"]["mutation"]

    assert zadanie.get("continue-on-error") not in (True, "true")
    assert _krok().get("continue-on-error") not in (True, "true")


def _lepszy_od_baseline() -> list[str]:
    # 7 z 8 = 87.5 > 71.64: bez wczesnego wyjscia zapadka podnioslaby baseline.
    return ["killed"] * 7 + ["survived"]


def _linie_kroku() -> list[str]:
    return _krok()["run"].splitlines()


def test_przebieg_mutacyjny_z_przezywajacymi_mutantami_zmian_nie_oblewa_zadania_i_wypisuje_je_w_podsumowaniu(tmp_path):
    przebieg = uruchom_krok(
        tmp_path,
        statusy=_lepszy_od_baseline(),
        kod_zmian=1,
        stdout_zmian="\n".join(PRZEZYWAJACE) + "\n",
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not _jest_blad_ci(przebieg)
    assert NAGLOWEK_ZMIAN in przebieg.podsumowanie
    pod_naglowkiem = przebieg.podsumowanie.split(NAGLOWEK_ZMIAN, 1)[1]
    for klucz in PRZEZYWAJACE:
        assert klucz in pod_naglowkiem
    assert "czysto" not in przebieg.podsumowanie
    # Zapadka dziala dalej: wynik lepszy od baseline podnosi baseline mimo survivorow zmian.
    assert _score_w_baseline(przebieg) == 87.5


def test_przebieg_mutacyjny_bez_przezywajacych_mutantow_zmian_pisze_w_podsumowaniu_ze_jest_czysto(tmp_path):
    przebieg = uruchom_krok(tmp_path, statusy=_lepszy_od_baseline(), kod_zmian=0)

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not _jest_blad_ci(przebieg)
    assert NAGLOWEK_ZMIAN in przebieg.podsumowanie
    assert "czysto" in przebieg.podsumowanie.split(NAGLOWEK_ZMIAN, 1)[1]
    assert _score_w_baseline(przebieg) == 87.5


def test_przebieg_mutacyjny_z_bledem_narzedzia_zmian_oblewa_zadanie_i_nie_podnosi_baseline(tmp_path):
    przebieg = uruchom_krok(
        tmp_path,
        statusy=_lepszy_od_baseline(),
        kod_zmian=2,
        stderr_zmian="nieznana baza origin/main",
    )

    assert przebieg.kod != 0
    blad = [linia for linia in przebieg.linie if linia.startswith("::error::")]
    assert any("nieznana baza origin/main" in linia for linia in blad), przebieg.wyjscie
    assert "blad narzedzia (kod 2)" in przebieg.podsumowanie
    assert "nieznana baza origin/main" in przebieg.podsumowanie
    assert przebieg.baseline_niezmieniony


def test_przebieg_mutacyjny_z_nieoczekiwanym_kodem_narzedzia_zmian_tez_oblewa_zadanie(tmp_path):
    # Zielone sa tylko kody 0 i 1; kod 3 nie jest "tylko 2 jest bledem".
    przebieg = uruchom_krok(
        tmp_path,
        statusy=_lepszy_od_baseline(),
        kod_zmian=3,
        stderr_zmian="nieoczekiwany stan narzedzia",
    )

    assert przebieg.kod != 0
    assert any(
        linia.startswith("::error::") and "nieoczekiwany stan narzedzia" in linia for linia in przebieg.linie
    ), przebieg.wyjscie
    assert "blad narzedzia (kod 3)" in przebieg.podsumowanie
    assert przebieg.baseline_niezmieniony


def test_narzedzie_mutantow_zmian_dostaje_baze_z_galezi_docelowej_pull_requestu(tmp_path):
    przebieg = uruchom_krok(tmp_path, statusy=_lepszy_od_baseline())

    assert przebieg.argv_zmian.exists(), przebieg.wyjscie
    argv = przebieg.argv_zmian.read_text(encoding="utf-8").split()
    assert argv[argv.index("--baza") + 1] == "origin/main"


def test_przebieg_mutacyjny_bez_narzedzia_na_runnerze_nie_wola_mutantow_zmian_i_nie_pisze_podsumowania(tmp_path):
    przebieg = uruchom_krok(
        tmp_path,
        statusy=_lepszy_od_baseline(),
        mutmut_na_path=False,
        kod_zmian=2,
        stderr_zmian="nie powinno byc widoczne",
    )

    assert przebieg.kod == 0, przebieg.wyjscie
    assert not przebieg.argv_zmian.exists()
    assert przebieg.podsumowanie == ""
    assert not _jest_blad_ci(przebieg)


def test_wywolanie_mutantow_zmian_w_kroku_uzywa_galezi_docelowej_i_nie_ukrywa_bledu():
    wywolania = [linia for linia in _linie_kroku() if "tools/mutanty_zmian.py" in linia]

    assert wywolania, "krok nie wola tools/mutanty_zmian.py"
    for linia in wywolania:
        assert "github.base_ref" in linia
        assert "|| true" not in linia


def test_w_kroku_mutacyjnym_tylko_zapadka_ma_wyjatek_or_true():
    linie_or_true = [linia for linia in _linie_kroku() if "|| true" in linia]

    assert linie_or_true, "zapadka stracila swoj jawny wyjatek"
    for linia in linie_or_true:
        assert "cicd/mutation_baseline.py" in linia


def test_krok_mutacyjny_pisze_wynik_mutantow_zmian_do_podsumowania_zadania():
    assert "$GITHUB_STEP_SUMMARY" in _krok()["run"]


def test_artefakt_raportu_mutacji_zawiera_liste_przezywajacych_mutantow_zmian():
    kroki = _workflow()["jobs"]["mutation"]["steps"]
    artefakt = next(
        k
        for k in kroki
        if str(k.get("uses", "")).startswith("actions/upload-artifact") and k["with"]["name"] == "mutation-report"
    )

    assert "mutants/mutanty-zmian.txt" in artefakt["with"]["path"].splitlines()

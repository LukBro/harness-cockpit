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
    def __init__(self, kod: int, wyjscie: str, baseline: Path, oryginal: bytes):
        self.kod = kod
        self.wyjscie = wyjscie
        self.baseline = baseline
        self.oryginal = oryginal

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
) -> Przebieg:
    praca = tmp_path / "praca"
    (praca / "cicd").mkdir(parents=True)
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
    }
    wynik = subprocess.run(
        _polecenie_powloki(skrypt),
        cwd=praca,
        env=srodowisko,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return Przebieg(wynik.returncode, wynik.stdout + wynik.stderr, baseline, baseline.read_bytes())


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

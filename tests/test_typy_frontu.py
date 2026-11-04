"""Typy frontu pochodza z kontraktu API (HAR-84): statyczne straznice, bez npm i bez przegladarki.

Dynamiczny dowod (build prawdziwie oblewa po zmianie kontraktu) jest w
`tests/e2e/test_typy_frontu_z_kontraktu.py`; tu sprawdzamy to, co da sie sprawdzic w jobie
`python`: plik generowany nie trafia do repozytorium, a typy odpowiedzi API ma jedno miejsce.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from procesy import uruchom_proces_sync, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parents[1]
WEB = KORZEN / "web"
SRC = WEB / "src"
IMPORT_TYPOW_API = re.compile(r"""["'][^"'\n]*api-types(?:\.ts)?["']""")
LIMIT_GITA_S = 30


def _pliki_zrodlowe_frontu() -> list[Path]:
    """Wszystkie *.ts i *.tsx pod web/src; blad odczytu katalogu to blad testu, nie pusta lista."""

    def blad(e: OSError) -> None:
        raise e

    pliki = [
        Path(katalog) / nazwa
        for katalog, _, nazwy in os.walk(SRC, onerror=blad)
        for nazwa in nazwy
        if nazwa.endswith((".ts", ".tsx"))
    ]
    assert len(pliki) >= 3, f"zbadano {len(pliki)} plikow w {SRC}: brak dowodu, to nie jest czysto"
    return pliki


def _git(argv: list[str], cwd: Path) -> tuple[int, str]:
    """Git w `cwd` bez konfiguracji uzytkownika (globalny `core.excludesFile` zmienilby wynik)."""
    srodowisko = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    proc = uruchom_proces_sync(
        ["git", *argv], cwd=cwd, env=srodowisko,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _, stderr = proc.communicate(timeout=LIMIT_GITA_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"git {argv[0]} przekroczyl limit {LIMIT_GITA_S} s")
    finally:
        zakoncz_proces_sync(proc, nazwa=f"git {argv[0]}")
    return proc.returncode, stderr


def _kod_ignorowania(repo: Path, sciezka: str) -> int:
    kod, stderr = _git(["check-ignore", "-q", sciezka], repo)
    if kod not in (0, 1):
        pytest.fail(f"git check-ignore zakonczyl sie bledem {kod}: {stderr}")
    return kod


def test_wygenerowane_typy_frontu_nie_trafiaja_do_repozytorium(tmp_path: Path) -> None:
    # Swieze repozytorium z samym .gitignore projektu: wynik nie zalezy od .git w korzeniu.
    kod, stderr = _git(["init", "-q"], tmp_path)
    assert kod == 0, stderr
    shutil.copy(KORZEN / ".gitignore", tmp_path / ".gitignore")
    (tmp_path / "web" / "src").mkdir(parents=True)
    (tmp_path / "web" / "src" / "api-types.ts").write_text("", encoding="utf-8")
    (tmp_path / "web" / "src" / "api.ts").write_text("", encoding="utf-8")
    assert _kod_ignorowania(tmp_path, "web/src/api-types.ts") == 0, (
        "web/src/api-types.ts nie jest ignorowany przez git"
    )
    # Kontrola zawezenia: wpis `web/` albo `*.ts` ukrylby tez recznie pisany modul.
    assert _kod_ignorowania(tmp_path, "web/src/api.ts") == 1, (
        "web/src/api.ts jest ignorowany - .gitignore jest za szeroki"
    )


def test_typy_odpowiedzi_api_importuje_tylko_modul_api() -> None:
    api = SRC / "api.ts"
    assert api.is_file(), "brak web/src/api.ts - jedynego miejsca importu wygenerowanych typow"
    assert IMPORT_TYPOW_API.search(api.read_text(encoding="utf-8")), (
        "web/src/api.ts nie importuje z ./api-types"
    )
    inni = [
        str(p.relative_to(WEB))
        for p in _pliki_zrodlowe_frontu()
        if p not in (api, SRC / "api-types.ts")
        and IMPORT_TYPOW_API.search(p.read_text(encoding="utf-8"))
    ]
    assert inni == [], f"pliki importujace api-types z pominieciem api.ts: {inni}"


def test_budowanie_frontu_samo_generuje_typy_z_pliku_kontraktu_przed_kompilacja() -> None:
    skrypty = json.loads((WEB / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert "prebuild" not in skrypty, "typy maja byc generowane wprost w build, nie w prebuild"
    build = skrypty["build"]
    assert build.startswith("npm run types && "), build
    assert build.index("npm run types") < build.index("tsc -b"), build
    assert "./openapi.json" in skrypty["types"], "typy maja pochodzic z pliku, nie z serwera"
    assert "http" not in skrypty["types"], skrypty["types"]

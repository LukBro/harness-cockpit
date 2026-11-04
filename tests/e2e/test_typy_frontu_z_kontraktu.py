"""Build frontu zalezy od kontraktu API (HAR-84): zmiana odpowiedzi backendu, ktorej front
nie obsluzy, oblewa `npm run build`.

Testy nie potrzebuja PostgreSQL ani przegladarki (bez markera `baza`), ale wymagaja `npm`
i `web/node_modules` - brak ktoregos z nich to blad testu, nie pominiecie. Marker `e2e`,
bo job e2e i `tools/testy_pelne.sh` maja npm.

Proces to `npm` w kopii `web/` w tmp_path (nie kod projektu w Pythonie), wiec `cwd` = kopia
web/, nie korzen repozytorium. Kopia nie ma `src/api-types.ts`, wiec jedyne zrodlo typow to
`openapi.json` skopiowany obok.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from procesy import uruchom_proces_sync, zakoncz_proces_sync

pytestmark = pytest.mark.e2e

KORZEN = Path(__file__).resolve().parents[2]
WEB = KORZEN / "web"
LIMIT_BUILDU_S = 300


def _kopia_frontu(tmp_path: Path) -> Path:
    """Kopia web/ bez node_modules, wygenerowanych typow i artefaktow; node_modules dowiazane."""
    if shutil.which("npm") is None:
        pytest.fail("brak npm w PATH - test buildu frontu nie moze nic dowiesc")
    if not (WEB / "node_modules").is_dir():
        pytest.fail("brak web/node_modules - uruchom `npm ci` w web/ (test nie jest pominiety)")
    kopia = tmp_path / "web"
    shutil.copytree(
        WEB, kopia,
        ignore=shutil.ignore_patterns("node_modules", "api-types.ts", "*.tsbuildinfo", "dist"),
    )
    (kopia / "node_modules").symlink_to(WEB / "node_modules", target_is_directory=True)
    assert not (kopia / "src" / "api-types.ts").exists()
    return kopia


def _zbuduj(kopia: Path, wyjscie: Path) -> tuple[int, str]:
    proc = uruchom_proces_sync(
        ["npm", "run", "build", "--", "--outDir", str(wyjscie), "--emptyOutDir"],
        cwd=kopia, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        tekst, _ = proc.communicate(timeout=LIMIT_BUILDU_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"npm run build przekroczyl limit {LIMIT_BUILDU_S} s")
    finally:
        zakoncz_proces_sync(proc, nazwa="npm run build")
    return proc.returncode, tekst


def _usun_wartosc_enum(kopia: Path, schemat: str, wartosc: str) -> None:
    sciezka = kopia / "openapi.json"
    kontrakt = json.loads(sciezka.read_text(encoding="utf-8"))
    kontrakt["components"]["schemas"][schemat]["enum"].remove(wartosc)
    sciezka.write_text(json.dumps(kontrakt), encoding="utf-8")


def _usun_pole(kopia: Path, schemat: str, pole: str) -> None:
    sciezka = kopia / "openapi.json"
    kontrakt = json.loads(sciezka.read_text(encoding="utf-8"))
    s = kontrakt["components"]["schemas"][schemat]
    del s["properties"][pole]
    s["required"].remove(pole)
    sciezka.write_text(json.dumps(kontrakt), encoding="utf-8")


def test_front_buduje_sie_z_samego_pliku_kontraktu_bez_serwera(tmp_path: Path) -> None:
    kopia = _kopia_frontu(tmp_path)
    kod, tekst = _zbuduj(kopia, tmp_path / "dist")
    assert kod == 0, tekst
    assert (kopia / "src" / "api-types.ts").is_file(), "build sam nie wygenerowal typow"
    assert (tmp_path / "dist" / "index.html").is_file(), tekst


@pytest.mark.parametrize(
    ("schemat", "pole"),
    [
        ("RunSzczegoly", "status"),       # uzywa WidokRunu.tsx
        ("DefinicjaKatalogu", "nazwa"),   # uzywa App.tsx (tabela katalogu)
        ("RunnerZespolu", "online"),      # uzywa App.tsx (lista runnerow)
        ("ZdarzenieRunu", "seq"),         # WidokRunu.tsx: z.seq przy scalaniu zdarzen
        ("KrokRunu", "status"),           # WidokRunu.tsx: krok.status w powodKroku
        ("Profil", "csrf"),               # Konto.tsx: ja.csrf po odswiezeniu sesji
    ],
)
def test_pole_usuniete_z_kontraktu_api_oblewa_kompilacje_frontu(
    tmp_path: Path, schemat: str, pole: str
) -> None:
    kopia = _kopia_frontu(tmp_path)
    _usun_pole(kopia, schemat, pole)
    kod, tekst = _zbuduj(kopia, tmp_path / "dist")
    assert kod != 0, f"build przeszedl mimo braku pola {schemat}.{pole}"
    assert "error TS" in tekst, tekst
    assert not (tmp_path / "dist" / "index.html").exists(), "vite zbudowal front mimo bledu typow"


def test_identyfikator_runu_zmieniony_w_kontrakcie_api_na_liczbe_oblewa_kompilacje_frontu(
    tmp_path: Path,
) -> None:
    # RunUtworzony ma jedno pole: jego usuniecie daje Record<string, never> i tsc milczy,
    # wiec zmieniamy typ. WidokRunu.tsx:209 `setRunId(id)` przyjmuje string | null.
    kopia = _kopia_frontu(tmp_path)
    sciezka = kopia / "openapi.json"
    kontrakt = json.loads(sciezka.read_text(encoding="utf-8"))
    kontrakt["components"]["schemas"]["RunUtworzony"]["properties"]["run_id"] = {"type": "integer"}
    sciezka.write_text(json.dumps(kontrakt), encoding="utf-8")
    kod, tekst = _zbuduj(kopia, tmp_path / "dist")
    assert kod != 0, "build przeszedl mimo zmiany typu RunUtworzony.run_id na liczbe"
    assert "error TS" in tekst, tekst
    assert not (tmp_path / "dist" / "index.html").exists(), "vite zbudowal front mimo bledu typow"


def test_wartosc_stanu_runu_usunieta_z_kontraktu_api_oblewa_kompilacje_frontu(tmp_path: Path) -> None:
    # WidokRunu.tsx: `Record<RunStatus, string>` ma klucz to_accept, ktorego kontrakt juz nie zna.
    kopia = _kopia_frontu(tmp_path)
    _usun_wartosc_enum(kopia, "RunStatus", "to_accept")
    kod, tekst = _zbuduj(kopia, tmp_path / "dist")
    assert kod != 0, "build przeszedl mimo braku stanu to_accept w RunStatus"
    assert "error TS" in tekst, tekst
    assert not (tmp_path / "dist" / "index.html").exists(), "vite zbudowal front mimo bledu typow"

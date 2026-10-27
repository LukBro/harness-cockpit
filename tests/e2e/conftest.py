"""Fixture'y e2e (HAR-3): prawdziwy PostgreSQL, prawdziwy kokpit i runner jako procesy.

Bez importu playwrighta na poziomie modulu: zwykle uruchomienie testow laduje ten
plik nawet gdy testy e2e sa odfiltrowane, a ma dzialac bez przegladarki i bez PG.
Fixture'y sa synchroniczne (plugin playwrighta uzywa sync API).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

KORZEN = Path(__file__).resolve().parents[2]
URL_BAZY = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
INDEX_FRONTU = KORZEN / "src" / "cockpit" / "static" / "index.html"
LIMIT_STARTU_S = 20


def _srodowisko(tmp_path: Path, **dodatkowe: str) -> dict[str, str]:
    """Czyste srodowisko procesu: bez cudzych zmiennych COCKPIT_*, z pythonpath na src."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(KORZEN / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    env["COCKPIT_CONFIG"] = str(tmp_path / "credentials.json")
    env.update(dodatkowe)
    return env


def _uruchom_w_grupie(argv: list[str], env: dict[str, str], cwd: Path, log: Path):
    with log.open("wb") as wyjscie:
        return subprocess.Popen(
            argv, env=env, cwd=cwd, stdout=wyjscie, stderr=subprocess.STDOUT,
            start_new_session=True,   # wlasna grupa: zabijamy ja cala, zero sierot
        )


def _koniec_logu(log: Path) -> str:
    return log.read_text(errors="replace")[-2000:] if log.exists() else ""


def _grupa_istnieje(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _zabij_grupe(proc: subprocess.Popen) -> None:
    """Zabija cala grupe procesu i sprawdza, ze nic po niej nie zostalo."""
    pgid = proc.pid   # start_new_session=True: lider sesji ma pgid == pid
    if _grupa_istnieje(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=10)
    koniec = time.monotonic() + 5
    while _grupa_istnieje(pgid) and time.monotonic() < koniec:
        time.sleep(0.05)
    assert not _grupa_istnieje(pgid), f"po tescie zostaly procesy grupy {pgid}"


def _wolny_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _skalar_z_bazy(zapytanie: str):
    """Jedna wartosc z prawdziwej bazy; zapytanie z osobnego watku, w osobnej petli."""
    async def _pytaj():
        engine = create_async_engine(URL_BAZY)
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text(zapytanie))).scalar_one()
        finally:
            await engine.dispose()

    # sync API playwrighta trzyma petle zdarzen w watku testu; asyncio.run osobno
    with ThreadPoolExecutor(max_workers=1) as pula:
        return pula.submit(lambda: asyncio.run(_pytaj())).result()


def _liczba_runow() -> int:
    return _skalar_z_bazy("SELECT count(*) FROM run")


@pytest.fixture
def liczba_runow():
    """Funkcja zliczajaca runy w bazie (to samo zapytanie, ktorego uzywa zatrzymaj_runnera)."""
    return _liczba_runow


@pytest.fixture
def czysta_baza():
    if not URL_BAZY.startswith("postgresql"):
        pytest.skip(
            "test e2e wymaga PostgreSQL: ustaw "
            "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test"
        )
    if not INDEX_FRONTU.is_file():
        pytest.fail("brak zbudowanego frontu: zbuduj front: npm ci && npm run build w web/")
    cfg = Config(str(KORZEN / "alembic.ini"))
    cfg.set_main_option("script_location", str(KORZEN / "alembic"))
    cfg.set_main_option("sqlalchemy.url", URL_BAZY.replace("%", "%%"))
    # env.py Alembica robi asyncio.run, a plugin playwrighta moze juz trzymac petle
    # w watku testu: migracje ida w osobnym watku.
    def _migruj(akcja, rewizja: str) -> None:
        with ThreadPoolExecutor(max_workers=1) as pula:
            pula.submit(akcja, cfg, rewizja).result()

    _migruj(command.downgrade, "base")   # domyslny_tenant bierze pierwszy zespol: ma byc jeden
    _migruj(command.upgrade, "head")
    yield URL_BAZY
    _migruj(command.downgrade, "base")


@pytest.fixture
def token_runnera(czysta_baza, tmp_path) -> str:
    """Zespol z runnerem zalozony przez tools/seed_dev.py; plik z tokenem w tmp_path."""
    wynik = subprocess.run(
        [sys.executable, str(KORZEN / "tools" / "seed_dev.py")],
        env=_srodowisko(tmp_path, COCKPIT_DATABASE_URL=czysta_baza),
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert wynik.returncode == 0, f"zasiew nie powiodl sie: {wynik.stderr[-1500:]}"
    return json.loads((tmp_path / "credentials.json").read_text())["runner"]


@pytest.fixture
def adres_kokpitu(czysta_baza, token_runnera, tmp_path):
    port = _wolny_port()
    adres = f"http://127.0.0.1:{port}"
    log = tmp_path / "kokpit.log"
    proc = _uruchom_w_grupie(
        [sys.executable, "-m", "uvicorn", "cockpit.api.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        _srodowisko(tmp_path, COCKPIT_DATABASE_URL=czysta_baza), tmp_path, log,
    )
    try:
        koniec = time.monotonic() + LIMIT_STARTU_S
        while True:
            assert proc.poll() is None, f"kokpit nie wystartowal: {_koniec_logu(log)}"
            try:
                if httpx.get(f"{adres}/api/health", timeout=1).json() == {"status": "ok"}:
                    break
            except (httpx.HTTPError, ValueError):
                pass
            assert time.monotonic() < koniec, f"kokpit nie odpowiada: {_koniec_logu(log)}"
            time.sleep(0.1)
        yield adres
    finally:
        _zabij_grupe(proc)


@pytest.fixture
def runner_online(adres_kokpitu, token_runnera, tmp_path):
    """Runner polaczony z kokpitem; token tylko w zmiennej srodowiskowej."""
    log = tmp_path / "runner.log"
    proc = _uruchom_w_grupie(
        [sys.executable, "-m", "runner"],
        _srodowisko(tmp_path, COCKPIT_RUNNER_TOKEN=token_runnera,
                    COCKPIT_URL=adres_kokpitu),
        tmp_path, log,
    )
    try:
        koniec = time.monotonic() + LIMIT_STARTU_S
        # Runner jest dostepny dopiero z otwartym strumieniem (HAR-26): sam last_seen_at
        # po zameldowaniu nie wystarcza, klik przed otwarciem strumienia dalby 409.
        # Sygnal gotowosci to wpis runnera w logu - sondowanie POST /api/runs tworzyloby runy.
        while "strumien otwarty" not in log.read_text(errors="replace") if log.exists() else True:
            assert proc.poll() is None, f"runner zakonczyl prace: {_koniec_logu(log)}"
            assert time.monotonic() < koniec, f"strumien runnera sie nie otworzyl: {_koniec_logu(log)}"
            time.sleep(0.1)
        yield proc
    finally:
        _zabij_grupe(proc)


@pytest.fixture
def zatrzymaj_runnera(runner_online, adres_kokpitu):
    """Zatrzymuje runnera jak uzytkownik (SIGTERM na grupe) i czeka, az kokpit to zauwazy.

    Zwraca liczbe runow w bazie po zakonczeniu sondowania: sonda w oknie miedzy SIGTERM
    a wykryciem rozlaczenia moze zalozyc prawdziwe runy, wiec punkt odniesienia
    odczytujemy dopiero, gdy kokpit zaczal odpowiadac 409.
    """

    def _zatrzymaj() -> int:
        os.killpg(os.getpgid(runner_online.pid), signal.SIGTERM)
        runner_online.wait(timeout=10)
        # Rozlaczenie strumienia widac tylko w pamieci kokpitu: pytamy o nowy run az odpowie 409.
        koniec = time.monotonic() + 10
        while httpx.post(f"{adres_kokpitu}/api/runs", json={"workflow": "szkielet"}
                         ).status_code != 409:
            assert time.monotonic() < koniec, "kokpit nie wykryl rozlaczenia runnera"
            time.sleep(0.2)
        return _liczba_runow()

    return _zatrzymaj

"""Fixture'y e2e (HAR-3): prawdziwy PostgreSQL, prawdziwy kokpit i runner jako procesy.

Bez importu playwrighta na poziomie modulu: zwykle uruchomienie testow laduje ten
plik nawet gdy testy e2e sa odfiltrowane, a ma dzialac bez przegladarki i bez PG.
Fixture'y sa synchroniczne (plugin playwrighta uzywa sync API).
"""
from __future__ import annotations

import asyncio
import faulthandler
import gc
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from procesy import (
    SIGTERM,
    grupa_istnieje,
    uruchom_proces_sync,
    zabij_grupe,
    zakoncz_proces_sync,
)

KORZEN = Path(__file__).resolve().parents[2]
URL_BAZY = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
INDEX_FRONTU = KORZEN / "src" / "cockpit" / "static" / "index.html"
LIMIT_STARTU_S = 20
# Runner e2e = produkcyjny `python -m runner`, ale lista operacji w jego procesie dostaje
# dodatkowo operacje testowa "czekaj" (HAR-159). Produkcyjna lista jej nie ma: kokpit
# nie moze kazac runnerowi wykonac niczego spoza lokalnej listy.
# `runner` stoi na poczatku skryptu: `ps -o args=` na CI obcina argumenty do 80 znakow,
# a test_token_runnera_nie_jest_widoczny_w_argumentach_procesu szuka w nich "runner".
START_RUNNERA_Z_OPERACJA_TESTOWA = [
    sys.executable, "-c",
    "import runner.client as c, runpy, types; "
    "c.OPERACJE_RUNU = types.MappingProxyType("
    "{**c.OPERACJE_RUNU, 'czekaj': ('sleep', '30')}); "
    "runpy.run_module('runner', run_name='__main__', alter_sys=True)",
]


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
    # wlasna sesja i grupa (robi to helper): zabijamy ja cala, zero sierot
    with log.open("wb") as wyjscie:
        return uruchom_proces_sync(
            argv, env=env, cwd=cwd, stdout=wyjscie, stderr=subprocess.STDOUT,
        )


def _koniec_logu(log: Path) -> str:
    return log.read_text(errors="replace")[-2000:] if log.exists() else ""


def _zabij_grupe(proc: subprocess.Popen) -> None:
    """Zabija cala grupe procesu i sprawdza, ze nic po niej nie zostalo."""
    pgid = proc.pid   # start_new_session=True: lider sesji ma pgid == pid
    zakoncz_proces_sync(proc, nazwa=f"proces e2e {proc.pid}", limit_term=10, limit_wait=10)
    # Lider mogl zakonczyc sie wczesniej niz jego potomkowie - helper wtedy nic nie robi.
    zabij_grupe(pgid)
    koniec = time.monotonic() + 5
    while grupa_istnieje(pgid) and time.monotonic() < koniec:
        time.sleep(0.05)
    assert not grupa_istnieje(pgid), f"po tescie zostaly procesy grupy {pgid}"


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


LIMIT_TESTU_S = 300    # zawieszony test e2e konczy przebieg ze stosem zamiast wisiec do limitu joba


def _zrzuc_zadania_asyncio() -> None:
    """Niezakonczone zadania kazdej zywej petli asyncio w procesie (diagnostyka zawieszenia)."""
    try:
        for petla in [o for o in gc.get_objects() if isinstance(o, asyncio.AbstractEventLoop)]:
            if petla.is_closed():
                continue
            zadania = asyncio.all_tasks(petla)
            sys.stderr.write(f"\n+++ petla {petla!r}: {len(zadania)} zadan\n")
            for zadanie in zadania:
                zadanie.print_stack(file=sys.stderr)
    except Exception as blad:  # diagnostyka nie moze przykryc zrzutu stosu
        sys.stderr.write(f"\n+++ zrzut zadan asyncio nieudany: {blad!r}\n")


def _zrzuc_aktywnosc_bazy() -> None:
    """Sesje bazy i na co czekaja (pg_stat_activity), z limitem czasu."""
    async def _pytaj():
        engine = create_async_engine(URL_BAZY)
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text(
                    "SELECT pid, state, wait_event_type, wait_event, now() - query_start, "
                    "left(query, 200) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid()"))).all()
        finally:
            await engine.dispose()

    try:
        wiersze = asyncio.run(asyncio.wait_for(_pytaj(), timeout=20))
        sys.stderr.write(f"\n+++ pg_stat_activity ({len(wiersze)} sesji):\n")
        for wiersz in wiersze:
            sys.stderr.write(f"  {tuple(wiersz)!r}\n")
    except Exception as blad:  # diagnostyka nie moze przykryc zrzutu stosu
        sys.stderr.write(f"\n+++ pg_stat_activity nieudane: {blad!r}\n")


@pytest.fixture(autouse=True)
def _limit_czasu_testu(request):
    """Zawieszony test nie blokuje CI bez konca: po limicie stos wszystkich watkow w logu i koniec procesu.

    Przechwytywanie wyjscia pytesta jest zdejmowane przed zrzutem (jak w pytest-timeout),
    inaczej stos trafilby do przechwyconego stderr i zniknal razem z procesem."""
    koniec = threading.Event()

    def straz() -> None:
        if koniec.wait(LIMIT_TESTU_S):
            return
        capman = request.config.pluginmanager.getplugin("capturemanager")
        if capman is not None:
            capman.suspend_global_capture(in_=True)
        sys.stderr.write(f"\n+++ ZAWIESZONY TEST po {LIMIT_TESTU_S} s: {request.node.nodeid}\n")
        sys.stderr.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        _zrzuc_zadania_asyncio()
        _zrzuc_aktywnosc_bazy()
        sys.stderr.flush()
        os._exit(1)

    watek = threading.Thread(target=straz, name="limit-czasu-testu", daemon=True)
    watek.start()
    try:
        yield
    finally:
        koniec.set()
        watek.join(timeout=5)


@pytest.fixture
def czysta_baza():
    if not URL_BAZY.startswith("postgresql"):
        pytest.skip(
            "test e2e wymaga PostgreSQL: uruchom tools/testy_pelne.sh (baza "
            "cockpit_test_<skrot> na worktree) albo ustaw jawnie "
            "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../<baza>"
        )
    if not INDEX_FRONTU.is_file():
        pytest.fail("brak zbudowanego frontu: zbuduj front: npm ci && npm run build w web/")
    # Migracje w osobnym procesie, nie w watku procesu testu. Watek dzielil proces z
    # Playwrightem: do petli asyncio.run z env.py Alembica trafialo zadanie Playwrighta
    # (`Connection.wrap_api_call`), ktore czekalo na glowny watek, a ten na migracje -
    # zakleszczenie w CI (HAR-121, zrzut stosu i zadan asyncio z przebiegu e2e PR #167).
    def _migruj(akcja: str, rewizja: str) -> None:
        wynik = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(KORZEN / "alembic.ini"), akcja, rewizja],
            env={**os.environ, "COCKPIT_DATABASE_URL": URL_BAZY},
            cwd=KORZEN, capture_output=True, text=True, timeout=120,
        )
        assert wynik.returncode == 0, f"alembic {akcja} {rewizja} nie powiodl sie: {wynik.stderr[-1500:]}"

    _migruj("downgrade", "base")   # czysta baza: zasiew zaklada jeden zespol i jednego wlasciciela
    _migruj("upgrade", "head")
    yield URL_BAZY
    _migruj("downgrade", "base")


@pytest.fixture
def dane_wlasciciela() -> tuple[str, str]:
    """Losowy e-mail i haslo wlasciciela zespolu zakladanego przez zasiew (HAR-50)."""
    return f"wlasciciel-{uuid.uuid4().hex[:10]}@example.com", f"haslo-{uuid.uuid4().hex}"


@pytest.fixture
def token_runnera(czysta_baza, tmp_path, dane_wlasciciela) -> str:
    """Zespol z runnerem i wlascicielem zalozony przez tools/seed_dev.py; plik z tokenem w tmp_path.

    Dane wlasciciela ida zmiennymi srodowiska procesu, nigdy argumentami (widocznymi w ps).
    """
    email, haslo = dane_wlasciciela
    wynik = subprocess.run(
        [sys.executable, str(KORZEN / "tools" / "seed_dev.py")],
        env=_srodowisko(tmp_path, COCKPIT_DATABASE_URL=czysta_baza,
                        COCKPIT_DEV_EMAIL=email, COCKPIT_DEV_PASSWORD=haslo),
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert wynik.returncode == 0, f"zasiew nie powiodl sie: {wynik.stderr[-1500:]}"
    return json.loads((tmp_path / "credentials.json").read_text())["runner"]


@pytest.fixture
def dodatkowe_env_kokpitu() -> dict[str, str]:
    """Dodatkowe zmienne srodowiska procesu kokpitu; test nadpisuje je przez parametryzacje (HAR-70)."""
    return {}


@pytest.fixture
def adres_kokpitu(czysta_baza, token_runnera, tmp_path, dodatkowe_env_kokpitu):
    port = _wolny_port()
    adres = f"http://127.0.0.1:{port}"
    log = tmp_path / "kokpit.log"
    proc = _uruchom_w_grupie(
        [sys.executable, "-m", "uvicorn", "cockpit.api.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        # kokpit w e2e stoi na http: ciasteczko z flaga Secure nie wrociloby do serwera
        _srodowisko(tmp_path, COCKPIT_DATABASE_URL=czysta_baza,
                    COCKPIT_CIASTECZKO_SECURE="0", **dodatkowe_env_kokpitu), tmp_path, log,
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


def zdejmij_podstawione_trasy_z_kontekstu(kontekst) -> None:
    """Zdejmuje trasy testu (`page.route`, `context.route`) z kazdej strony kontekstu i z kontekstu.

    HAR-185: zamkniecie kontekstu przy handlerze trasy w locie (`route.fetch()`) konczylo sie
    `TargetClosedError` zglaszanym dopiero przy nastepnym wywolaniu sync API, czyli w
    nastepnym tescie. `behavior="ignoreErrors"` nie czeka na handlery w locie i ignoruje
    ich bledy, wiec handlery tras nie potrzebuja wlasnej obslugi zamkniecia kontekstu.
    Kontekst zamkniety juz przez test (np. zapis profilu w tescie logowania) rzuca przy
    zdejmowaniu `TargetClosedError` - sprzatanie go pomija, kazdy inny wyjatek (takze inny
    blad Playwrighta, np. protokolu albo TimeoutError) leci dalej.
    WHY import z `playwright._impl._errors`: `TargetClosedError` nie jest eksportowany z
    `playwright.sync_api` (tam tylko Error, TimeoutError, WebError). Typ nie jest publiczny:
    zmiana w Playwright da glosny ImportError, nie ciche przepuszczenie bledow.
    Import jest leniwy: ten plik laduje sie takze bez przegladarki.
    """
    from playwright._impl._errors import TargetClosedError

    for cel in (*kontekst.pages, kontekst):
        try:
            cel.unroute_all(behavior="ignoreErrors")
        except TargetClosedError:
            pass


@pytest.fixture
def context(context):
    """Kontekst z pluginu playwrighta; trasy testu zdejmowane tuz przed jego zamknieciem.

    Teardown tego nadpisania biegnie przed teardownem fixture'a z pluginu, ktory zamyka kontekst.
    """
    yield context
    zdejmij_podstawione_trasy_z_kontekstu(context)


@pytest.fixture
def zdejmij_podstawione_trasy():
    """Funkcja sprzatajaca trasy kontekstu, dla fixture'ow tworzacych wlasne konteksty."""
    return zdejmij_podstawione_trasy_z_kontekstu


@pytest.fixture
def zalogowana_strona(page, adres_kokpitu, dane_wlasciciela):
    """Strona zalogowana formularzem jak uzytkownik; zwraca ja po pojawieniu sie widoku runu."""
    from playwright.sync_api import expect

    email, haslo = dane_wlasciciela
    page.goto(adres_kokpitu)
    page.get_by_label("E-mail").fill(email)
    page.get_by_label("Hasło", exact=True).fill(haslo)
    page.get_by_role("button", name="Zaloguj", exact=True).click()
    expect(page.get_by_role("button", name="Uruchom")).to_be_visible()
    return page


@pytest.fixture
def runner_online(adres_kokpitu, token_runnera, tmp_path):
    """Runner polaczony z kokpitem; token tylko w zmiennej srodowiskowej."""
    log = tmp_path / "runner.log"
    proc = _uruchom_w_grupie(
        START_RUNNERA_Z_OPERACJA_TESTOWA,
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
def zatrzymaj_runnera(runner_online, adres_kokpitu, dane_wlasciciela):
    """Zatrzymuje runnera jak uzytkownik (SIGTERM na grupe) i czeka, az kokpit to zauwazy.

    Zwraca liczbe runow w bazie po zakonczeniu sondowania: sonda w oknie miedzy SIGTERM
    a wykryciem rozlaczenia moze zalozyc prawdziwe runy, wiec punkt odniesienia
    odczytujemy dopiero, gdy kokpit zaczal odpowiadac 409.
    """

    def _zatrzymaj() -> int:
        zabij_grupe(runner_online.pid, SIGTERM)   # start w nowej sesji: pgid == pid
        runner_online.wait(timeout=10)
        # Rozlaczenie strumienia widac tylko w pamieci kokpitu: pytamy o nowy run az odpowie 409.
        # POST /api/runs wymaga sesji (HAR-57): sonda loguje sie jak uzytkownik.
        email, haslo = dane_wlasciciela
        with httpx.Client(base_url=adres_kokpitu) as klient:
            logowanie = klient.post("/api/konto/logowanie", json={"email": email, "haslo": haslo})
            assert logowanie.status_code == 200, f"sonda nie zalogowala sie: {logowanie.text}"
            naglowki = {"X-CSRF-Token": logowanie.json()["csrf"]}
            koniec = time.monotonic() + 10
            while klient.post("/api/runs", json={"workflow": "szkielet"},
                              headers=naglowki).status_code != 409:
                assert time.monotonic() < koniec, "kokpit nie wykryl rozlaczenia runnera"
                time.sleep(0.2)
        return _liczba_runow()

    return _zatrzymaj

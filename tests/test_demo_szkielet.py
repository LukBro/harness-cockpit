"""Przelot na wylot idzie sciezka produkcyjna: PostgreSQL i alembic (HAR-13).

`tools/demo_szkielet.py` to skrypt, nie modul: import uruchomilby przelot, wiec
testujemy go jako podproces. Testy sa synchroniczne celowo (jak test_migracje).
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

KORZEN = Path(__file__).resolve().parent.parent
SKRYPT = KORZEN / "tools" / "demo_szkielet.py"
ZMIENNA = "COCKPIT_DATABASE_URL"
STARA_BAZA = Path("/tmp/demo.db")
TEST_URL = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
TABELE_MODELU = {"tenant", "runner", "run", "event"}
PORT_ZMIENNA = "COCKPIT_DEMO_PORT"
PORT_DOMYSLNY = 8799

wymaga_postgresa = pytest.mark.skipif(
    not TEST_URL.startswith("postgresql"),
    reason="przelot na PostgreSQL wymaga bazy: ustaw "
    "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test",
)


def _zajmij_port(port: int) -> socket.socket:
    """Gniazdo nasluchujace na 127.0.0.1:port - port zajety dla procesu przelotu."""
    zajety = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    zajety.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    zajety.bind(("127.0.0.1", port))
    zajety.listen(1)
    return zajety


def _uruchom(cwd: Path, **env_dodatkowe: str):
    env = {k: v for k, v in os.environ.items() if k not in (ZMIENNA, PORT_ZMIENNA)}
    env.update(env_dodatkowe)
    return subprocess.run(
        [sys.executable, str(SKRYPT)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )


def _stan_pliku(sciezka: Path):
    if not sciezka.exists():
        return None
    st = sciezka.stat()
    return (st.st_mtime_ns, st.st_size)


def _sprawdz_blad_o_zmiennej(wynik):
    assert wynik.returncode != 0
    assert ZMIENNA in wynik.stderr
    # blad ma byc czytelnym komunikatem, nie wyjatkiem importu ani sciezki
    assert "ModuleNotFoundError" not in wynik.stderr
    assert "Traceback" not in wynik.stderr
    assert "szkielet dziala na wylot" not in wynik.stdout


def test_przelot_bez_adresu_bazy_konczy_sie_bledem_z_nazwa_zmiennej(tmp_path):
    _sprawdz_blad_o_zmiennej(_uruchom(tmp_path))


def test_przelot_z_pusta_wartoscia_adresu_bazy_konczy_sie_bledem(tmp_path):
    _sprawdz_blad_o_zmiennej(_uruchom(tmp_path, **{ZMIENNA: ""}))


def test_przelot_z_adresem_bazy_z_samych_spacji_traktuje_go_jak_brak_zmiennej(tmp_path):
    wynik = _uruchom(tmp_path, **{ZMIENNA: "   "})

    _sprawdz_blad_o_zmiennej(wynik)
    # same spacje to brak adresu, nie adres do sparsowania
    assert "Brak COCKPIT_DATABASE_URL" in wynik.stderr
    assert "nie da sie go sparsowac" not in wynik.stderr


def test_plik_env_nie_zastepuje_zmiennej_srodowiskowej_adresu_bazy(tmp_path):
    baza = tmp_path / "z_env.db"
    (tmp_path / ".env").write_text(f"{ZMIENNA}=sqlite+aiosqlite:///{baza}\n")

    wynik = _uruchom(tmp_path)

    _sprawdz_blad_o_zmiennej(wynik)
    assert not baza.exists(), "skrypt siegnal po baze wskazana tylko w .env"


def test_przelot_bez_adresu_bazy_nie_tworzy_starej_bazy_sqlite_w_tmp(tmp_path):
    przed = _stan_pliku(STARA_BAZA)

    _uruchom(tmp_path)

    # jesli plik istnial przed testem, nie wolno go ani zmienic, ani skasowac
    assert _stan_pliku(STARA_BAZA) == przed


def test_przelot_nie_buduje_schematu_przez_create_all():
    drzewo = ast.parse(SKRYPT.read_text(encoding="utf-8"))
    uzycia = (
        {n.id for n in ast.walk(drzewo) if isinstance(n, ast.Name)}
        | {n.attr for n in ast.walk(drzewo) if isinstance(n, ast.Attribute)}
        | {
            a.name
            for n in ast.walk(drzewo)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in n.names
        }
    )
    assert "create_all" not in uzycia


def _lapie_blad_bazy(proba: ast.Try) -> bool:
    nazwy = {"OSError", "SQLAlchemyError"}
    for h in proba.handlers:
        typy = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        for t in typy:
            if isinstance(t, ast.Name) and t.id in nazwy:
                return True
            if isinstance(t, ast.Attribute) and t.attr in nazwy:
                return True
    return False


def _jest_wywolaniem_przelotu(wezel: ast.AST) -> bool:
    if not isinstance(wezel, ast.Call):
        return False
    f = wezel.func
    return (isinstance(f, ast.Name) and f.id == "przelot") or (
        isinstance(f, ast.Attribute) and f.attr == "przelot")


def test_blad_w_przelocie_nie_jest_tlumaczony_na_blad_polaczenia_z_baza():
    """Wezszy except: `except (OSError, SQLAlchemyError)` wolno obejmowac tylko
    migracje i zasiew, a nie caly przelot (serwer, runner, proces potomny), bo
    inaczej OSError z przelotu wyglada jak awaria bazy."""
    drzewo = ast.parse(SKRYPT.read_text(encoding="utf-8"))
    przelot_pod_lapaniem = [
        wezel.lineno
        for proba in ast.walk(drzewo)
        if isinstance(proba, ast.Try) and _lapie_blad_bazy(proba)
        for instrukcja in proba.body  # tylko body: orelse i finalbody nie sa objete
        for wezel in ast.walk(instrukcja)
        if _jest_wywolaniem_przelotu(wezel)
    ]
    assert przelot_pod_lapaniem == [], (
        f"przelot() wywolany w try lapiacym OSError/SQLAlchemyError (linie {przelot_pod_lapaniem})"
    )


# --- prawdziwa baza -------------------------------------------------------

def _cfg() -> Config:
    c = Config(str(KORZEN / "alembic.ini"))
    c.set_main_option("script_location", str(KORZEN / "alembic"))
    c.set_main_option("sqlalchemy.url", TEST_URL.replace("%", "%%"))
    return c


def _tabele_w_public() -> set[str]:
    async def biegnij():
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.connect() as conn:
                wiersze = await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
                return {w[0] for w in wiersze}
        finally:
            await engine.dispose()

    return asyncio.run(biegnij())


def _wersje_alembica() -> set[str]:
    """Wersje zapisane w alembic_version; brak tabeli to brak wersji.

    `alembic downgrade base` zostawia pusta tabele alembic_version (zachowanie
    Alembica), wiec o sprzataniu swiadczy brak WIERSZY, nie brak tabeli.
    """
    async def biegnij():
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.connect() as conn:
                wiersze = await conn.execute(text("SELECT version_num FROM alembic_version"))
                return {w[0] for w in wiersze}
        finally:
            await engine.dispose()

    if "alembic_version" not in _tabele_w_public():
        return set()
    return asyncio.run(biegnij())


def _sprawdz_baze_po_przelocie_jest_wyczyszczona() -> None:
    tabele = _tabele_w_public()
    assert TABELE_MODELU & tabele == set(), (
        f"po przelocie zostaly tabele modelu: {TABELE_MODELU & tabele}"
    )
    assert _wersje_alembica() == set(), "baza zostala na wersji migracji po przelocie"


@pytest.fixture
def czysta_baza():
    command.downgrade(_cfg(), "base")
    yield
    command.downgrade(_cfg(), "base")


@pytest.mark.baza
@wymaga_postgresa
def test_przelot_na_postgresie_dziala_na_wylot_i_sprzata_baze_po_sobie(czysta_baza):
    wynik = _uruchom(KORZEN, **{ZMIENNA: TEST_URL})

    assert wynik.returncode == 0, wynik.stderr
    assert "szkielet dziala na wylot" in wynik.stdout
    # dowod sprzatania w danych: ani tabel modelu, ani wersji w alembic_version
    _sprawdz_baze_po_przelocie_jest_wyczyszczona()


def _zasiej_runner_z_tokenem_przelotu_w_innym_zespole() -> None:
    """Pozostalosc po poprzednim przebiegu: runner o tym samym tokenie co w przelocie.

    `runner_po_tokenie` (src/cockpit/services/runs.py:30-32) wybiera runnera
    samym `token_hash` + `scalar_one_or_none()`, a kolumna nie ma ograniczenia
    unikalnosci (src/cockpit/db/models.py:53). Drugi runner z tym hashem daje
    MultipleResultsFound, wiec przelot zasiany na zostawionym schemacie
    sie nie udaje. Tylko `downgrade base` przed przelotem usuwa te pozostalosc.
    """
    token_hash = hashlib.sha256(b"demo-token").hexdigest()

    async def biegnij():
        engine = create_async_engine(TEST_URL)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO tenant (id, name, created_at) "
                    "VALUES ('stary-zespol', 'Stary zespol', now())"
                ))
                await conn.execute(text(
                    "INSERT INTO runner (id, tenant_id, name, version, token_hash) "
                    "VALUES ('stary-runner', 'stary-zespol', 'stary', '', :h)"
                ), {"h": token_hash})
        finally:
            await engine.dispose()

    asyncio.run(biegnij())


@pytest.mark.baza
@wymaga_postgresa
def test_przelot_startuje_od_czystej_bazy_mimo_pozostalosci_po_poprzednim_przebiegu(
    czysta_baza,
):
    command.upgrade(_cfg(), "head")
    _zasiej_runner_z_tokenem_przelotu_w_innym_zespole()
    assert TABELE_MODELU <= _tabele_w_public()

    wynik = _uruchom(KORZEN, **{ZMIENNA: TEST_URL})

    assert wynik.returncode == 0, wynik.stderr
    assert "szkielet dziala na wylot" in wynik.stdout
    _sprawdz_baze_po_przelocie_jest_wyczyszczona()


@pytest.mark.baza
@wymaga_postgresa
def test_nieudany_przelot_konczy_sie_bledem_i_mimo_to_sprzata_baze(czysta_baza):
    # port przelotu zajety: serwer nie wstanie, przelot sie nie udaje. Port to COCKPIT_DEMO_PORT
    # (testy_pelne.sh wybiera wolny port dla kazdego przebiegu), a bez zmiennej 8799 (HAR-236).
    port = os.environ.get(PORT_ZMIENNA) or str(PORT_DOMYSLNY)
    zajety = _zajmij_port(int(port))
    try:
        wynik = _uruchom(KORZEN, **{ZMIENNA: TEST_URL, PORT_ZMIENNA: port})
    finally:
        zajety.close()

    assert wynik.returncode != 0
    assert "szkielet dziala na wylot" not in wynik.stdout
    # baza dziala, wiec porazka przelotu nie jest "blad polaczenia z baza" (HAR-138)
    assert "Blad polaczenia z baza" not in wynik.stderr
    # porazka nie zostawia schematu ani wersji migracji w bazie
    _sprawdz_baze_po_przelocie_jest_wyczyszczona()


@pytest.mark.baza
@wymaga_postgresa
def test_przelot_zajmuje_port_z_cockpit_demo_port_a_nie_stale_8799(czysta_baza):
    # Port wybrany tu, niezaleznie od srodowiska: przelot ignorujacy zmienna wzialby wolny
    # port 8799 i skonczylby sie sukcesem.
    zajety = _zajmij_port(0)
    port = zajety.getsockname()[1]
    assert port != PORT_DOMYSLNY
    try:
        wynik = _uruchom(KORZEN, **{ZMIENNA: TEST_URL, PORT_ZMIENNA: str(port)})
    finally:
        zajety.close()

    assert wynik.returncode != 0
    assert "szkielet dziala na wylot" not in wynik.stdout
    _sprawdz_baze_po_przelocie_jest_wyczyszczona()


@pytest.mark.baza
@wymaga_postgresa
def test_przelot_ze_krokiem_ktory_nie_wystartowal_wraca_z_kodem_1_i_sprzata_baze(
    czysta_baza, tmp_path
):
    # runner startuje krok jako proces potomny (src/runner/client.py:41); bez
    # python3 w PATH create_subprocess_exec rzuca FileNotFoundError, listen()
    # pada, run nie dostaje step_finished i zostaje poza to_accept, a main()
    # WRACA z 1 (tools/demo_szkielet.py: ok == False -> return 1 -> SystemExit(kod))
    pusty_path = tmp_path / "bin"
    pusty_path.mkdir()

    wynik = _uruchom(KORZEN, **{ZMIENNA: TEST_URL, "PATH": str(pusty_path)})

    assert wynik.returncode == 1, (wynik.returncode, wynik.stderr)
    assert "WYNIK: BLAD" in wynik.stdout
    assert "szkielet dziala na wylot" not in wynik.stdout
    _sprawdz_baze_po_przelocie_jest_wyczyszczona()


# --- tlumaczenie bledow bazy (bez bazy) -----------------------------------

def _sprawdz_czytelny_blad_adresu_bazy(wynik, sekret: str | None = None):
    _sprawdz_blad_o_zmiennej(wynik)
    if sekret is not None:
        assert sekret not in wynik.stderr
        assert sekret not in wynik.stdout


def test_przelot_z_nieparsowalnym_adresem_bazy_konczy_sie_czytelnym_bledem(tmp_path):
    wynik = _uruchom(tmp_path, **{ZMIENNA: "foo"})

    _sprawdz_czytelny_blad_adresu_bazy(wynik)
    assert "nie da sie go sparsowac" in wynik.stderr
    assert "nieznany sterownik" not in wynik.stderr


def test_przelot_z_nieznanym_sterownikiem_bazy_konczy_sie_czytelnym_bledem(tmp_path):
    wynik = _uruchom(
        tmp_path, **{ZMIENNA: "postgresql+nieznany://u:p@localhost/x"}
    )
    _sprawdz_czytelny_blad_adresu_bazy(wynik)
    # osobna przyczyna niz nieparsowalny adres: sterownik, ktorego nie ma
    assert "nieznany sterownik" in wynik.stderr
    assert "nie da sie go sparsowac" not in wynik.stderr


def test_przelot_z_nieosiagalna_baza_konczy_sie_bledem_bez_wycieku_hasla(tmp_path):
    wynik = _uruchom(
        tmp_path,
        **{ZMIENNA: "postgresql+asyncpg://cockpit:tajnehaslo@127.0.0.1:1/cockpit_test"},
    )
    _sprawdz_czytelny_blad_adresu_bazy(wynik, sekret="tajnehaslo")


def test_przelot_z_haslem_zakodowanym_procentowo_dochodzi_do_proby_polaczenia(tmp_path):
    # `%40` w adresie: bez podwojenia `%` dla Alembica (ConfigParser) przelot
    # padalby surowym wyjatkiem interpolacji, a nie bledem polaczenia z baza
    wynik = _uruchom(
        tmp_path,
        **{ZMIENNA: "postgresql+asyncpg://cockpit:p%40s@127.0.0.1:1/cockpit_test"},
    )

    _sprawdz_czytelny_blad_adresu_bazy(wynik, sekret="p%40s")
    assert "Blad polaczenia z baza" in wynik.stderr
    assert "interpolation" not in wynik.stderr
    # ani forma zakodowana, ani zdekodowana nie moze wyciec
    assert "p@s" not in wynik.stderr
    assert "p@s" not in wynik.stdout


def test_przelot_ze_sterownikiem_bazy_ktorego_nie_zainstalowano_podaje_jego_nazwe(tmp_path):
    if importlib.util.find_spec("psycopg2") is not None:
        pytest.skip("psycopg2 jest zainstalowany: brak sterownika nie do odtworzenia")
    sekret = "zxq7unikalnyqwv"

    wynik = _uruchom(
        tmp_path, **{ZMIENNA: f"postgresql+psycopg2://u:{sekret}@127.0.0.1:1/db"}
    )

    _sprawdz_czytelny_blad_adresu_bazy(wynik, sekret=sekret)
    # osobna przyczyna niz nieznany sterownik i nieparsowalny adres: pakiet nie jest zainstalowany
    assert "nie jest zainstalowany" in wynik.stderr
    assert "psycopg2" in wynik.stderr
    assert "nieznany sterownik" not in wynik.stderr
    assert "nie da sie go sparsowac" not in wynik.stderr


def test_przelot_ze_sterownikiem_nieasynchronicznym_konczy_sie_bledem_i_nie_tworzy_bazy(
    tmp_path,
):
    baza = tmp_path / "x.db"
    sekret = "qv9unikalnyklucz7zt"

    wynik = _uruchom(tmp_path, **{ZMIENNA: f"sqlite:///{baza}?token={sekret}"})

    _sprawdz_czytelny_blad_adresu_bazy(wynik, sekret=sekret)
    assert "sterownik nie jest asynchroniczny" in wynik.stderr
    assert "nieznany sterownik" not in wynik.stderr
    # dowod w danych: skrypt nie siegnal po baze, wiec plik nie powstal
    assert not baza.exists()


def test_przelot_z_niepoprawnym_portem_adresu_bazy_konczy_sie_czytelnym_bledem(tmp_path):
    wynik = _uruchom(
        tmp_path,
        **{ZMIENNA: "postgresql+asyncpg://u:sekretportu@localhost:abc/cockpit_test"},
    )

    _sprawdz_czytelny_blad_adresu_bazy(wynik, sekret="sekretportu")
    # osobna przyczyna niz nieparsowalny adres: port, ktory nie jest liczba
    assert "niepoprawny port lub host" in wynik.stderr
    assert "nie da sie go sparsowac" not in wynik.stderr


# --- port przelotu z COCKPIT_DEMO_PORT (bez bazy, HAR-236) ------------------------------


@pytest.mark.parametrize("wartosc", ["0", "65536", "abc", "-1", "1.5", "99999999999999999999"])
def test_zly_port_przelotu_konczy_sie_kodem_1_z_komunikatem_o_porcie(tmp_path, wartosc):
    # Bez adresu bazy: walidacja portu idzie przed czymkolwiek innym, wiec komunikat
    # dotyczy portu, a nie braku COCKPIT_DATABASE_URL.
    wynik = _uruchom(tmp_path, **{PORT_ZMIENNA: wartosc})

    assert wynik.returncode == 1
    assert wynik.stderr == (
        f"COCKPIT_DEMO_PORT musi byc liczba calkowita 1..65535 (jest: '{wartosc}')\n"
    )
    assert wynik.stdout == ""


@pytest.mark.parametrize("wartosc", ["1", "8799", "65535", ""], ids=["1", "8799", "65535", "pusta"])
def test_poprawny_albo_pusty_port_przelotu_przechodzi_walidacje_i_dochodzi_do_adresu_bazy(
    tmp_path, wartosc
):
    # Granice 1 i 65535 sa dozwolone, pusta wartosc = domyslna: skrypt idzie dalej i dopiero
    # brak adresu bazy konczy go bledem (nie komunikat o porcie).
    wynik = _uruchom(tmp_path, **{PORT_ZMIENNA: wartosc})

    _sprawdz_blad_o_zmiennej(wynik)
    assert "COCKPIT_DEMO_PORT" not in wynik.stderr


# --- wybor portu bez uruchamiania przelotu (HAR-236, runda 4) ----------------------------------
# Skrypt przy imporcie robi przelot, wiec funkcje `port_demo` i wyrazenia z portu bierzemy z jego
# drzewa skladni: to kod produkcyjny z pliku, wykonany bez bazy.


def _drzewo_skryptu() -> ast.Module:
    return ast.parse(SKRYPT.read_text(encoding="utf-8"))


def _wyrazenie(wezel: ast.expr, **zmienne):
    kod = compile(ast.fix_missing_locations(ast.Expression(wezel)), str(SKRYPT), "eval")
    return eval(kod, dict(zmienne))  # noqa: S307 - wyrazenie z naszego pliku, nie z wejscia


def _port_demo_ze_skryptu(monkeypatch, wartosc: str | None) -> int:
    funkcja = next(
        n for n in _drzewo_skryptu().body if isinstance(n, ast.FunctionDef) and n.name == "port_demo"
    )
    modul = ast.fix_missing_locations(ast.Module(body=[funkcja], type_ignores=[]))
    przestrzen: dict = {"os": os, "sys": sys}
    exec(compile(modul, str(SKRYPT), "exec"), przestrzen)  # noqa: S102
    if wartosc is None:
        monkeypatch.delenv(PORT_ZMIENNA, raising=False)
    else:
        monkeypatch.setenv(PORT_ZMIENNA, wartosc)
    return przestrzen["port_demo"]()


@pytest.mark.parametrize("wartosc", [None, ""], ids=["nieustawiona", "pusta"])
def test_port_przelotu_bez_zmiennej_albo_z_pusta_zmienna_to_8799(monkeypatch, wartosc):
    assert _port_demo_ze_skryptu(monkeypatch, wartosc) == 8799


@pytest.mark.parametrize("wartosc", ["1", "80", "8800", "65535"])
def test_port_przelotu_z_zakresu_jest_zwracany_jako_liczba(monkeypatch, wartosc):
    port = _port_demo_ze_skryptu(monkeypatch, wartosc)
    assert port == int(wartosc)
    assert isinstance(port, int)


@pytest.mark.parametrize(
    "wartosc",
    ["0", "65536", "abc", "-1", "1.5", " 80", "٣٤", "²"],
    ids=["zero", "65536", "litery", "minus", "ulamek", "spacja", "cyfry_arabskie", "indeks_gorny"],
)
def test_zly_port_przelotu_to_wyjscie_z_komunikatem_a_nie_wyjatek_konwersji(monkeypatch, wartosc):
    # Cyfry arabskie i indeks gorny przechodza `isdigit()`, wiec odrzuca je tylko `isascii()`
    # (dla "²" `int()` rzucilby ValueError zamiast SystemExit z komunikatem).
    with pytest.raises(SystemExit) as wyjscie:
        _port_demo_ze_skryptu(monkeypatch, wartosc)
    assert wyjscie.value.code == (
        f"COCKPIT_DEMO_PORT musi byc liczba calkowita 1..65535 (jest: '{wartosc}')"
    )


def test_adres_serwera_i_port_uvicorna_przelotu_pochodza_z_portu_demo():
    drzewo = _drzewo_skryptu()
    przypisania = {
        cel.id: n
        for n in drzewo.body
        if isinstance(n, ast.Assign)
        for cel in n.targets
        if isinstance(cel, ast.Name)
    }
    assert _wyrazenie(przypisania["BASE"].value, PORT=12345) == "http://127.0.0.1:12345"
    wywolanie_portu = przypisania["PORT"].value
    assert isinstance(wywolanie_portu, ast.Call) and wywolanie_portu.func.id == "port_demo"
    konfiguracje = [
        n
        for n in ast.walk(drzewo)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Config"
    ]
    assert len(konfiguracje) == 1
    port_uvicorna = next(k.value for k in konfiguracje[0].keywords if k.arg == "port")
    assert _wyrazenie(port_uvicorna, PORT=12345) == 12345
    # Walidacja portu idzie przed odczytem adresu bazy (zly port to blad przed czymkolwiek innym).
    assert przypisania["PORT"].lineno < przypisania["URL"].lineno

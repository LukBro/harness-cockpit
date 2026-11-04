"""Brak osieroconych procesow agenta po zabiciu runnera (HAR-85).

`start_new_session=True` odcina agenta od sygnalow rodzica, wiec SIGKILL runnera
zostawial siroty. Straznik (`python -m runner.straznik`) wykrywa smierc runnera przez
EOF na stdin i zabija grupe agenta.

Prawdziwe procesy: mini-runner to osobny proces Pythona wolajacy `wykonaj_krok`;
agent-atrapa zapisuje "pid pgid" do pliku (atomowo) i spi. Kontrakt straznika, ktory
zakladaja testy:
  python -m runner.straznik --cwd <katalog> -- <argv agenta>
  * agent we wlasnej grupie (pid == pgid), cwd = <katalog>, stdout/stderr bez zmian,
  * EOF na stdin albo SIGTERM -> SIGTERM do grupy, po 5 s SIGKILL, wyjscie,
  * koniec agenta -> kod wyjscia agenta (zabity sygnalem N -> 128+N).
Proces straznika i mini-runner startuja z cwd = korzen repozytorium i PYTHONPATH =
<korzen>/src (pod mutmut korzen to `mutants/`).
"""
import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola
from procesy import (
    SIGKILL,
    SIGTERM,
    czekaj,
    czekaj_bez_anulowania,
    czekaj_na_warunek,
    grupa_istnieje,
    grupa_umarla,
    uruchom_proces,
    uruchom_proces_sync,
    zabij_grupe,
    zabij_proces,
)
from runner import krok, straznik
from runner.agent import SpecKroku
from runner.krok import WynikKroku, wykonaj_krok

KORZEN = Path(__file__).resolve().parent.parent
LIMIT_ZNIKNIECIA = 10.0
LIMIT_START = 20.0

# Agent: opcjonalnie ignoruje SIGTERM, zapisuje "pid pgid" (atomowo), potem spi.
KOD_SPIACEGO_AGENTA = r"""
import os, signal, sys, time
plik, ignoruj = sys.argv[1], sys.argv[2] == "ignoruj"
if ignoruj:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
tmp = plik + ".tmp"
with open(tmp, "w") as f:
    f.write(f"{os.getpid()} {os.getpgid(0)}")
os.replace(tmp, plik)
time.sleep(300)
"""

# Mini-runner: prawdziwy `wykonaj_krok` w osobnym procesie.
KOD_MINI_RUNNERA = r"""
import asyncio, json, sys
from pathlib import Path
from cockpit.services.invariants import Rola
from runner.agent import SpecKroku
from runner.krok import wykonaj_krok

p = json.loads(sys.argv[1])
spec = SpecKroku(run_id="r", step_id="s", agent_id="a", skill_id="k",
                 rola=Rola.author, wejscia=[], wyjscia=[], komendy={})

async def emit(zdarzenie):
    pass

asyncio.run(wykonaj_krok(spec, p["polecenie"], Path(p["katalog"]), emit))
"""

# Przypadek kontrolny: ten sam scenariusz, ale agent uruchamiany BEZPOSREDNIO.
KOD_MINI_RUNNERA_BEZ_STRAZNIKA = r"""
import asyncio, json, sys

p = json.loads(sys.argv[1])

async def main():
    await asyncio.create_subprocess_exec(
        *p["polecenie"], cwd=p["katalog"], start_new_session=True
    )
    await asyncio.sleep(300)

asyncio.run(main())
"""


def srodowisko() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(KORZEN / "src")}


def agent_spiacy(plik: Path, *, ignoruj_sigterm: bool = False) -> list[str]:
    return [
        sys.executable, "-I", "-c", KOD_SPIACEGO_AGENTA,
        str(plik), "ignoruj" if ignoruj_sigterm else "nie",
    ]


def spec() -> SpecKroku:
    return SpecKroku(
        run_id="run-17",
        step_id="krok-23",
        agent_id="agent-31",
        skill_id="pisanie",
        rola=Rola.author,
        wejscia=[],
        wyjscia=[],
        komendy={},
    )


async def uruchom_mini_runnera(
    kod: str, polecenie: list[str], katalog: Path, tmp_path: Path
) -> asyncio.subprocess.Process:
    blad = (tmp_path / "mini-runner.stderr").open("wb")
    try:
        return await uruchom_proces(
            sys.executable, "-c", kod,
            json.dumps({"polecenie": polecenie, "katalog": str(katalog)}),
            cwd=KORZEN,
            env=srodowisko(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=blad,
        )
    finally:
        blad.close()


async def czekaj_na_agenta(plik: Path, proces: asyncio.subprocess.Process) -> tuple[int, int]:
    """(pid, pgid) agenta z pliku; przerywa, gdy proces startujacy agenta padl wczesniej."""

    def gotowe() -> bool:
        return plik.exists() or proces.returncode is not None

    await czekaj_na_warunek(gotowe, LIMIT_START, "agent zapisal pid i pgid")
    assert plik.exists(), f"proces startujacy agenta zakonczyl sie kodem {proces.returncode}"
    pid, pgid = plik.read_text().split()
    return int(pid), int(pgid)


@pytest.fixture
def katalog_kroku(tmp_path: Path) -> Path:
    katalog = tmp_path / "krok"
    katalog.mkdir()
    return katalog


@pytest.fixture
def pgid_do_sprzatania():
    """Lista pgid agentow; po tescie kazda zywa grupa dostaje SIGKILL (sierota)."""
    lista: list[int] = []
    yield lista
    for pgid in lista:
        if grupa_istnieje(pgid):
            zabij_grupe(pgid)


# --- SIGKILL / SIGTERM runnera w trakcie kroku --------------------------------


async def test_po_sigkill_runnera_w_trakcie_kroku_grupa_agenta_znika_w_10_sekund(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    runner = await uruchom_mini_runnera(
        KOD_MINI_RUNNERA, agent_spiacy(plik), katalog_kroku, tmp_path
    )
    _, pgid = await czekaj_na_agenta(plik, runner)
    pgid_do_sprzatania.append(pgid)

    zabij_proces(runner.pid, SIGKILL)
    await czekaj(runner.wait(), 10, "koniec mini-runnera po SIGKILL")

    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)


async def test_po_sigterm_runnera_w_trakcie_kroku_grupa_agenta_znika_w_10_sekund(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    runner = await uruchom_mini_runnera(
        KOD_MINI_RUNNERA, agent_spiacy(plik), katalog_kroku, tmp_path
    )
    _, pgid = await czekaj_na_agenta(plik, runner)
    pgid_do_sprzatania.append(pgid)

    zabij_proces(runner.pid, SIGTERM)
    await czekaj(runner.wait(), 10, "koniec mini-runnera po SIGTERM")

    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)


async def test_agent_uruchomiony_bez_straznika_przezywa_sigkill_runnera_jako_sierota(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    # Kontrola: ten sam scenariusz bez straznika pokazuje sierote, wiec testy wyzej
    # odrozniaja regule od jej braku.
    plik = tmp_path / "agent.pid"
    runner = await uruchom_mini_runnera(
        KOD_MINI_RUNNERA_BEZ_STRAZNIKA, agent_spiacy(plik), katalog_kroku, tmp_path
    )
    _, pgid = await czekaj_na_agenta(plik, runner)
    pgid_do_sprzatania.append(pgid)

    zabij_proces(runner.pid, SIGKILL)
    await czekaj(runner.wait(), 10, "koniec mini-runnera po SIGKILL")
    await asyncio.sleep(2)

    assert grupa_istnieje(pgid)


# --- anulowanie zadania kroku -------------------------------------------------


async def test_anulowanie_zadania_kroku_zabija_grupe_agenta(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"

    async def emit(zdarzenie: dict) -> None:
        pass

    zadanie = asyncio.create_task(wykonaj_krok(spec(), agent_spiacy(plik), katalog_kroku, emit))
    try:
        await czekaj_na_warunek(plik.exists, LIMIT_START, "agent zapisal pid i pgid")
        pgid = int(plik.read_text().split()[1])
        pgid_do_sprzatania.append(pgid)

        zadanie.cancel()
        with pytest.raises(asyncio.CancelledError):
            await czekaj_bez_anulowania(zadanie, 15, "koniec zadania kroku po anulowaniu")
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie}, timeout=15)

    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)


async def test_wyjatek_z_odbiorcy_zdarzen_zabija_grupe_agenta(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    # Agent wypisuje linie po zapisaniu pliku; emit rzuca na pierwszej linii.
    plik = tmp_path / "agent.pid"
    kod = (
        "import os, sys, time\n"
        "plik = sys.argv[1]\n"
        "open(plik + '.tmp', 'w').write(f'{os.getpid()} {os.getpgid(0)}')\n"
        "os.replace(plik + '.tmp', plik)\n"
        "print('linia', flush=True)\n"
        "time.sleep(300)\n"
    )

    class BladOdbiorcy(RuntimeError):
        pass

    async def emit(zdarzenie: dict) -> None:
        raise BladOdbiorcy

    zadanie = asyncio.create_task(
        wykonaj_krok(spec(), [sys.executable, "-I", "-c", kod, str(plik)], katalog_kroku, emit)
    )
    try:
        await czekaj_na_warunek(plik.exists, LIMIT_START, "agent zapisal pid i pgid")
        pgid = int(plik.read_text().split()[1])
        pgid_do_sprzatania.append(pgid)

        with pytest.raises(BladOdbiorcy):
            await czekaj_bez_anulowania(zadanie, 15, "koniec zadania kroku po wyjatku emit")
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie}, timeout=15)

    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)


# --- straznik uruchomiony bezposrednio ---------------------------------------


async def uruchom_straznika(
    katalog: Path, agent: list[str], **kw
) -> asyncio.subprocess.Process:
    return await uruchom_proces(
        sys.executable, "-m", "runner.straznik", "--cwd", str(katalog), "--", *agent,
        cwd=KORZEN,
        env=srodowisko(),
        stdin=asyncio.subprocess.PIPE,
        **kw,
    )


async def test_agent_ignorujacy_sigterm_zyje_jeszcze_po_2_s_a_ginie_w_10_s_po_eof(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    straznik = await uruchom_straznika(
        katalog_kroku, agent_spiacy(plik, ignoruj_sigterm=True), stdout=asyncio.subprocess.DEVNULL
    )
    _, pgid = await czekaj_na_agenta(plik, straznik)
    pgid_do_sprzatania.append(pgid)

    straznik.stdin.close()
    await asyncio.sleep(2)
    zyje_po_2_s = grupa_istnieje(pgid)

    assert zyje_po_2_s, "SIGKILL przyszedl przed uplywem 5 s karencji po SIGTERM"
    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)


async def test_agent_ktory_konczy_sie_na_sigterm_ginie_po_eof_przed_uplywem_karencji(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    straznik = await uruchom_straznika(
        katalog_kroku, agent_spiacy(plik), stdout=asyncio.subprocess.DEVNULL
    )
    _, pgid = await czekaj_na_agenta(plik, straznik)
    pgid_do_sprzatania.append(pgid)

    straznik.stdin.close()

    assert await grupa_umarla(pgid, limit=3)
    await czekaj(straznik.wait(), 10, "koniec straznika po EOF")


async def test_sigterm_do_straznika_zabija_grupe_agenta(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    straznik = await uruchom_straznika(
        katalog_kroku, agent_spiacy(plik), stdout=asyncio.subprocess.DEVNULL
    )
    _, pgid = await czekaj_na_agenta(plik, straznik)
    pgid_do_sprzatania.append(pgid)

    zabij_proces(straznik.pid, SIGTERM)

    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)
    await czekaj(straznik.wait(), 10, "koniec straznika po SIGTERM")


async def test_agent_ma_wlasna_grupe_procesow_odrebna_od_straznika(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    straznik = await uruchom_straznika(
        katalog_kroku, agent_spiacy(plik), stdout=asyncio.subprocess.DEVNULL
    )
    pid, pgid = await czekaj_na_agenta(plik, straznik)
    pgid_do_sprzatania.append(pgid)

    assert pid == pgid
    assert pgid != straznik.pid


async def test_agent_pracuje_w_katalogu_podanym_w_cwd(tmp_path, katalog_kroku):
    straznik = await uruchom_straznika(
        katalog_kroku,
        [sys.executable, "-I", "-c", "import os; print(os.getcwd())"],
        stdout=asyncio.subprocess.PIPE,
    )
    wyjscie = await czekaj(straznik.stdout.read(), 20, "wyjscie agenta")
    await czekaj(straznik.wait(), 10, "koniec straznika")

    assert Path(wyjscie.decode().strip()).resolve() == katalog_kroku.resolve()


@pytest.mark.parametrize("kod", [0, 3, 77])
async def test_straznik_konczy_sie_kodem_wyjscia_agenta(katalog_kroku, kod):
    straznik = await uruchom_straznika(
        katalog_kroku,
        [sys.executable, "-I", "-c", f"import sys; sys.exit({kod})"],
        stdout=asyncio.subprocess.DEVNULL,
    )

    assert await czekaj(straznik.wait(), 20, "koniec straznika") == kod


async def test_agent_zabity_sygnalem_9_daje_straznikowi_kod_137(katalog_kroku):
    straznik = await uruchom_straznika(
        katalog_kroku,
        [
            sys.executable, "-I", "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        ],
        stdout=asyncio.subprocess.DEVNULL,
    )

    assert await czekaj(straznik.wait(), 20, "koniec straznika") == 128 + 9


async def test_wyjscie_agenta_przechodzi_przez_straznika_bajt_w_bajt(katalog_kroku):
    bajty_out = b"linia 1\n\xff\xfe\x00 nie-utf8\r\nbez konca linii"
    bajty_err = b"blad \xc3\x28 \n"
    kod = (
        "import sys\n"
        f"sys.stdout.buffer.write({bajty_out!r}); sys.stdout.buffer.flush()\n"
        f"sys.stderr.buffer.write({bajty_err!r}); sys.stderr.buffer.flush()\n"
    )
    straznik = await uruchom_straznika(
        katalog_kroku,
        [sys.executable, "-I", "-c", kod],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await czekaj(
        asyncio.gather(straznik.stdout.read(), straznik.stderr.read()),
        20,
        "wyjscie straznika",
    )
    await czekaj(straznik.wait(), 10, "koniec straznika")

    assert (out, err) == (bajty_out, bajty_err)


# --- ten sam wynik kroku co bez straznika -------------------------------------


async def test_krok_zakonczony_kodem_3_ma_ten_sam_kod_i_te_same_linie_wyjscia(katalog_kroku):
    kod = (
        "import sys\n"
        "print('pierwsza', flush=True)\n"
        "sys.stderr.write('druga\\n'); sys.stderr.flush()\n"
        "print('trzecia', flush=True)\n"
        "sys.exit(3)\n"
    )
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await czekaj(
        wykonaj_krok(spec(), [sys.executable, "-I", "-c", kod], katalog_kroku, emit),
        30,
        "krok z kodem 3",
    )

    assert wynik == WynikKroku("nieudany", "kod wyjścia 3", 3)
    assert [z["text"] for z in zdarzenia] == ["pierwsza", "druga", "trzecia"]
    assert {z["kind"] for z in zdarzenia} == {"output"}
    assert {z["step_id"] for z in zdarzenia} == {"krok-23"}


# --- straznik wolany w procesie testu (mutmut przypisuje testy po trafieniach) ----
# Straznik dziala tu w procesie testu: agent to zawsze osobny proces we wlasnej grupie,
# sprzatany przez `pgid_do_sprzatania` i autouse `sprzatnij_zapamietane`.

KOD_AGENTA_Z_WNUKIEM = r"""
import os, subprocess, sys, time
plik = sys.argv[1]
wnuk = subprocess.Popen(
    [sys.executable, "-I", "-c",
     "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
     "print('gotowy', flush=True)\ntime.sleep(300)"],
    stdout=subprocess.PIPE,
)
wnuk.stdout.readline()
tmp = plik + ".tmp"
with open(tmp, "w") as f:
    f.write(f"{os.getpid()} {os.getpgid(0)}")
os.replace(tmp, plik)
time.sleep(300)
"""

LIMIT_WATKU = 25.0


class Zadanie:
    """Wywolanie w watku demonie; wynik tylko z limitem czasu (zawieszenie = blad testu)."""

    def __init__(self, funkcja):
        self._wynik = None
        self._blad: BaseException | None = None
        self._funkcja = funkcja
        self.watek = threading.Thread(target=self._cel, daemon=True)
        self.watek.start()

    def _cel(self):
        try:
            self._wynik = self._funkcja()
        except BaseException as blad:  # noqa: BLE001 - przekazujemy do watku testu
            self._blad = blad

    def wynik(self, limit: float = LIMIT_WATKU):
        self.watek.join(limit)
        assert not self.watek.is_alive(), f"watek nie skonczyl sie w {limit} s"
        if self._blad is not None:
            raise self._blad
        return self._wynik


def czekaj_na_plik(plik: Path, limit: float = LIMIT_START) -> None:
    koniec = time.monotonic() + limit
    while not plik.exists():
        assert time.monotonic() < koniec, f"brak pliku {plik.name} po {limit} s"
        time.sleep(0.02)


def pgid_z_pliku(plik: Path) -> int:
    return int(plik.read_text().split()[1])


def grupa_zniknela(pgid: int, limit: float = LIMIT_ZNIKNIECIA) -> bool:
    koniec = time.monotonic() + limit
    while grupa_istnieje(pgid):
        if time.monotonic() >= koniec:
            return False
        time.sleep(0.05)
    return True


def czytaj_do_konca(fd: int, limit: float = 5.0) -> bytes:
    """Czyta do EOF z limitem; brak EOF (zapis wciaz otwarty) to blad testu."""
    koniec = time.monotonic() + limit
    dane = b""
    while True:
        pozostalo = koniec - time.monotonic()
        assert pozostalo > 0, "brak EOF: koniec zapisu nie zostal zamkniety"
        gotowe, _, _ = select.select([fd], [], [], pozostalo)
        assert gotowe, "brak EOF: koniec zapisu nie zostal zamkniety"
        porcja = os.read(fd, 4096)
        if not porcja:
            return dane
        dane += porcja


def liczba_deskryptorow() -> int:
    return len(os.listdir("/dev/fd"))


class Potok:
    """os.pipe() ze sprzataniem; nie zamyka dwa razy numeru, ktory mogl zostac uzyty ponownie."""

    def __init__(self):
        self.odczyt, self.zapis = os.pipe()
        self._otwarte = {self.odczyt, self.zapis}

    def zamknij(self, fd: int) -> None:
        if fd in self._otwarte:
            self._otwarte.discard(fd)
            os.close(fd)

    def uznaj_za_zamkniety(self, fd: int) -> None:
        self._otwarte.discard(fd)


@pytest.fixture
def potok():
    p = Potok()
    yield p
    p.zamknij(p.zapis)
    p.zamknij(p.odczyt)


@pytest.fixture
def handler_sigterm_przywrocony():
    poprzedni = signal.getsignal(signal.SIGTERM)
    yield poprzedni
    signal.signal(signal.SIGTERM, poprzedni)


def popen_agenta(argv: list[str], pgid_do_sprzatania: list[int]) -> subprocess.Popen:
    agent = uruchom_proces_sync(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid_do_sprzatania.append(agent.pid)
    return agent


class _SygnalyDoGrup:
    def __init__(self):
        self.wywolania: list[tuple] = []


def podmien_killpg(monkeypatch, modul, zachowanie):
    """Podmienia `os.killpg` widziany z modulu; `zachowanie(pgid, sygnal)` moze rzucic."""
    spy = _SygnalyDoGrup()

    def podstawiony(pgid, sygnal):
        spy.wywolania.append((pgid, sygnal))
        zachowanie(pgid, sygnal)

    monkeypatch.setattr(modul.os, "killpg", podstawiony)
    return spy


def rzuc(wyjatek: BaseException):
    def zachowanie(pgid, sygnal):
        raise wyjatek

    return zachowanie


def bez_skutku(pgid, sygnal):
    return None


# --- kod wyjscia ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("zwrocony", "oczekiwany"),
    [(-9, 137), (-15, 143), (-1, 129), (0, 0), (1, 1), (3, 3), (255, 255)],
)
def test_kod_wyjscia_agenta_zabitego_sygnalem_to_128_plus_numer_a_zwykly_bez_zmian(
    zwrocony, oczekiwany
):
    assert straznik._kod_wyjscia(zwrocony) == oczekiwany


# --- istnienie grupy i wysylanie sygnalu ----------------------------------------


def test_zywa_grupa_agenta_istnieje_a_po_jego_koncu_juz_nie(pgid_do_sprzatania):
    agent = popen_agenta(
        [sys.executable, "-I", "-c", "import time; time.sleep(300)"], pgid_do_sprzatania
    )
    assert straznik._grupa_istnieje(agent.pid) is True

    zabij_grupe(agent.pid)
    agent.wait(10)

    assert straznik._grupa_istnieje(agent.pid) is False


@pytest.mark.parametrize("wyjatek", [ProcessLookupError, PermissionError])
def test_brak_grupy_albo_odebrane_uprawnienie_do_grupy_znaczy_ze_grupa_nie_istnieje(
    monkeypatch, wyjatek
):
    podmien_killpg(monkeypatch, straznik, rzuc(wyjatek()))

    assert straznik._grupa_istnieje(4242) is False


def test_inny_blad_systemowy_przy_sprawdzaniu_grupy_nie_jest_polykany(monkeypatch):
    podmien_killpg(monkeypatch, straznik, rzuc(OSError(5, "inny blad")))

    with pytest.raises(OSError) as info:
        straznik._grupa_istnieje(4242)

    assert type(info.value) is OSError


def test_sprawdzenie_istnienia_grupy_wysyla_sygnal_zero_do_wlasciwej_grupy(monkeypatch):
    spy = podmien_killpg(monkeypatch, straznik, bez_skutku)

    assert straznik._grupa_istnieje(4242) is True
    assert spy.wywolania == [(4242, 0)]


def test_wyslanie_sygnalu_trafia_do_wskazanej_grupy_z_wlasciwym_numerem_sygnalu(monkeypatch):
    spy = podmien_killpg(monkeypatch, straznik, bez_skutku)

    straznik._wyslij_sygnal(4242, signal.SIGTERM)

    assert spy.wywolania == [(4242, signal.SIGTERM)]


def test_sygnal_do_nieistniejacej_grupy_nie_rzuca_wyjatku(pgid_do_sprzatania):
    agent = popen_agenta([sys.executable, "-I", "-c", "pass"], pgid_do_sprzatania)
    agent.wait(10)

    straznik._wyslij_sygnal(agent.pid, signal.SIGTERM)


@pytest.mark.parametrize("wyjatek", [ProcessLookupError, PermissionError])
def test_brak_grupy_albo_odebrane_uprawnienie_przy_wysylaniu_sygnalu_nie_rzuca(
    monkeypatch, wyjatek
):
    spy = podmien_killpg(monkeypatch, straznik, rzuc(wyjatek()))

    straznik._wyslij_sygnal(4242, signal.SIGKILL)

    assert spy.wywolania == [(4242, signal.SIGKILL)]


def test_inny_blad_systemowy_przy_wysylaniu_sygnalu_nie_jest_polykany(monkeypatch):
    podmien_killpg(monkeypatch, straznik, rzuc(OSError(5, "inny blad")))

    with pytest.raises(OSError) as info:
        straznik._wyslij_sygnal(4242, signal.SIGKILL)

    assert type(info.value) is OSError


# --- zakonczenie grupy agenta ---------------------------------------------------


def test_agent_ignorujacy_sigterm_zyje_po_3_s_karencji_a_potem_dostaje_sigkill_kod_137(
    tmp_path, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    agent = popen_agenta(agent_spiacy(plik, ignoruj_sigterm=True), pgid_do_sprzatania)
    czekaj_na_plik(plik)
    zadanie = Zadanie(lambda: straznik._zakoncz_grupe(agent))

    time.sleep(3)
    zyje_po_3_s = straznik._grupa_istnieje(agent.pid)
    kod = zadanie.wynik(12)

    assert zyje_po_3_s, "SIGKILL przyszedl przed uplywem 5 s karencji po SIGTERM"
    assert kod == 137
    assert grupa_zniknela(agent.pid)


def test_agent_konczacy_sie_na_sigterm_ma_kod_143_i_nie_czeka_do_konca_karencji(
    tmp_path, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    agent = popen_agenta(agent_spiacy(plik), pgid_do_sprzatania)
    czekaj_na_plik(plik)

    start = time.monotonic()
    kod = Zadanie(lambda: straznik._zakoncz_grupe(agent)).wynik(12)

    assert kod == 143
    assert time.monotonic() - start < 3
    assert grupa_zniknela(agent.pid)


def test_wnuk_agenta_ignorujacy_sigterm_ginie_od_sigkill_takze_po_smierci_lidera(
    tmp_path, pgid_do_sprzatania
):
    plik = tmp_path / "agent.pid"
    agent = popen_agenta(
        [sys.executable, "-I", "-c", KOD_AGENTA_Z_WNUKIEM, str(plik)], pgid_do_sprzatania
    )
    czekaj_na_plik(plik)

    kod = Zadanie(lambda: straznik._zakoncz_grupe(agent)).wynik(15)

    assert kod == 143
    assert grupa_zniknela(agent.pid), "wnuk przezyl koniec lidera grupy jako sierota"


# --- czytanie stdin do EOF ------------------------------------------------------


def test_zdarzenie_koncowe_ustawione_dopiero_po_zamknieciu_wejscia_a_nie_po_danych(potok):
    zdarzenie = threading.Event()
    watek = threading.Thread(
        target=straznik._czytaj_do_eof, args=(zdarzenie, potok.odczyt), daemon=True
    )
    watek.start()

    os.write(potok.zapis, b"x" * 10000)
    time.sleep(0.3)
    przed = zdarzenie.is_set()
    potok.zamknij(potok.zapis)
    po = zdarzenie.wait(5)
    watek.join(5)

    assert przed is False, "dane na wejsciu nie sa EOF"
    assert po is True
    assert not watek.is_alive()


def test_zamkniete_albo_bledne_wejscie_tez_ustawia_zdarzenie_i_nie_rzuca():
    odczyt, zapis = os.pipe()
    os.close(zapis)
    os.close(odczyt)
    zdarzenie = threading.Event()

    Zadanie(lambda: straznik._czytaj_do_eof(zdarzenie, odczyt)).wynik(10)

    assert zdarzenie.is_set()


# --- zgloszenie bledu startu ----------------------------------------------------


def test_blad_startu_z_deskryptorem_statusu_trafia_do_potoku_a_deskryptor_jest_zamkniety(
    potok, capfd
):
    kod = straznik._zglos_blad_startu(potok.zapis, OSError(2, "Brak pliku", "polecenie-har85"))
    potok.uznaj_za_zamkniety(potok.zapis)

    assert kod == 127
    assert czytaj_do_konca(potok.odczyt) == b"[Errno 2] Brak pliku: 'polecenie-har85'"
    assert capfd.readouterr().err == ""


def test_blad_startu_z_nieprzekodowalnym_znakiem_jest_zapisany_z_zamiennikiem(potok):
    straznik._zglos_blad_startu(potok.zapis, OSError("plik \udcff"))
    potok.uznaj_za_zamkniety(potok.zapis)

    assert czytaj_do_konca(potok.odczyt) == b"plik ?"


def test_deskryptor_statusu_jest_zamkniety_takze_gdy_zapis_komunikatu_sie_nie_uda():
    tylko_do_odczytu = os.open(os.devnull, os.O_RDONLY)

    with pytest.raises(OSError):
        straznik._zglos_blad_startu(tylko_do_odczytu, OSError("blad"))

    with pytest.raises(OSError):
        os.fstat(tylko_do_odczytu)


def test_blad_startu_bez_deskryptora_statusu_idzie_na_stderr_z_kodem_127(capfd):
    kod = straznik._zglos_blad_startu(None, OSError(2, "Brak pliku", "polecenie-har85"))

    assert kod == 127
    assert capfd.readouterr().err == (
        "straznik: nie można uruchomić polecenia: [Errno 2] Brak pliku: 'polecenie-har85'\n"
    )


# --- uzbrojenie: handler SIGTERM i watek czytajacy stdin ------------------------


def test_zamkniecie_wejscia_po_uzbrojeniu_ustawia_zdarzenie(potok, handler_sigterm_przywrocony):
    zdarzenie = threading.Event()

    straznik._uzbroj(zdarzenie, potok.odczyt)
    time.sleep(0.3)
    przed = zdarzenie.is_set()
    potok.zamknij(potok.zapis)

    assert przed is False
    assert zdarzenie.wait(5)


def test_sigterm_po_uzbrojeniu_ustawia_zdarzenie_a_proces_zyje_dalej(
    potok, handler_sigterm_przywrocony
):
    zdarzenie = threading.Event()
    straznik._uzbroj(zdarzenie, potok.odczyt)
    przed = zdarzenie.is_set()

    os.kill(os.getpid(), signal.SIGTERM)

    assert przed is False
    assert zdarzenie.wait(5), "SIGTERM nie ustawil zdarzenia (a wejscie wciaz otwarte)"


def test_watek_czytajacy_wejscie_nie_wstrzymuje_konca_straznika(
    potok, handler_sigterm_przywrocony
):
    """Watek nie-daemon trzymalby proces straznika po koncu agenta, dopoki runner nie
    zamknie potoku; dlatego watek czytajacy wejscie musi byc daemonem."""
    zdarzenie = threading.Event()
    przed = set(threading.enumerate())

    straznik._uzbroj(zdarzenie, potok.odczyt)
    try:
        nowe = set(threading.enumerate()) - przed
        assert len(nowe) == 1
        (watek,) = nowe
        assert watek.daemon is True
    finally:
        potok.zamknij(potok.zapis)
        zdarzenie.wait(5)


# Agent: kod 0, gdy jego stdin to /dev/null; 4, gdy cokolwiek innego (np. dziedziczony potok).
KOD_AGENTA_SPRAWDZAJACEGO_STDIN = (
    "import os, sys\n"
    "a = os.fstat(0)\n"
    "b = os.stat(os.devnull)\n"
    "sys.exit(0 if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) else 4)\n"
)


def test_agent_nie_dziedziczy_potoku_rodzica_na_stdin_tylko_pusty_stdin(tmp_path):
    # Rodzic (proces testu) ma na fd 0 POTOK, wiec dziedziczenie dalo by agentowi kod 4;
    # pod xdist/mutmut fd 0 jest zwykle /dev/null i bez podmiany test nie odroznialby reguly.
    zapas = os.dup(0)
    odczyt, zapis = os.pipe()
    try:
        os.dup2(odczyt, 0)
        wynik = Zadanie(
            lambda: straznik._uruchom(
                [sys.executable, "-I", "-c", KOD_AGENTA_SPRAWDZAJACEGO_STDIN],
                str(tmp_path), None, threading.Event(),
            )
        ).wynik()
    finally:
        os.dup2(zapas, 0)
        for fd in (zapas, odczyt, zapis):
            os.close(fd)

    assert wynik == 0


async def test_straznik_z_potokiem_od_runnera_na_stdin_daje_agentowi_pusty_stdin(katalog_kroku):
    straznik_proc = await uruchom_straznika(
        katalog_kroku,
        [sys.executable, "-I", "-c", KOD_AGENTA_SPRAWDZAJACEGO_STDIN],
        stdout=asyncio.subprocess.DEVNULL,
    )
    try:
        kod = await czekaj(straznik_proc.wait(), 20, "koniec straznika")
    finally:
        straznik_proc.stdin.close()

    assert kod == 0


def test_straznik_uruchomiony_jako_modul_czyta_potok_od_runnera_ze_standardowego_wejscia():
    """Straznik uruchomiony jako modul czyta potok od runnera na stdin, czyli fd 0."""
    assert straznik.FD_WEJSCIA == 0


# --- uruchomienie agenta pod straznikiem ----------------------------------------


@pytest.mark.parametrize("kod", [0, 3, 77])
def test_uruchomiony_agent_konczacy_sie_sam_daje_swoj_kod_i_bez_zwlekania(tmp_path, kod):
    start = time.monotonic()
    wynik = Zadanie(
        lambda: straznik._uruchom(
            [sys.executable, "-I", "-c", f"raise SystemExit({kod})"],
            str(tmp_path), None, threading.Event(),
        )
    ).wynik()

    assert wynik == kod
    assert time.monotonic() - start < 4


def test_uruchomiony_agent_zabity_sygnalem_9_daje_kod_137(tmp_path):
    wynik = Zadanie(
        lambda: straznik._uruchom(
            [sys.executable, "-I", "-c",
             "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
            str(tmp_path), None, threading.Event(),
        )
    ).wynik()

    assert wynik == 137


def test_ustawione_zdarzenie_zabija_grupe_agenta_i_daje_kod_143(tmp_path, pgid_do_sprzatania):
    plik = tmp_path / "agent.pid"
    zdarzenie = threading.Event()
    zadanie = Zadanie(
        lambda: straznik._uruchom(agent_spiacy(plik), str(tmp_path), None, zdarzenie)
    )
    czekaj_na_plik(plik)
    pgid = pgid_z_pliku(plik)
    pgid_do_sprzatania.append(pgid)

    zdarzenie.set()

    assert zadanie.wynik() == 143
    assert grupa_zniknela(pgid)


def test_agent_dziala_w_katalogu_podanym_straznikowi(tmp_path):
    katalog = tmp_path / "krok"
    katalog.mkdir()
    kod = (
        "import os, sys\n"
        "sys.exit(0 if os.path.realpath(os.getcwd()) == os.path.realpath(sys.argv[1]) else 9)"
    )

    wynik = Zadanie(
        lambda: straznik._uruchom(
            [sys.executable, "-I", "-c", kod, str(katalog)],
            str(katalog), None, threading.Event(),
        )
    ).wynik()

    assert wynik == 0


def test_udany_start_zamyka_deskryptor_statusu_bez_zadnych_danych(tmp_path, potok):
    wynik = Zadanie(
        lambda: straznik._uruchom(
            [sys.executable, "-I", "-c", "pass"], str(tmp_path), potok.zapis, threading.Event()
        )
    ).wynik()
    potok.uznaj_za_zamkniety(potok.zapis)

    assert wynik == 0
    assert czytaj_do_konca(potok.odczyt) == b""


def test_nieistniejace_polecenie_daje_kod_127_a_nazwe_polecenia_w_potoku_statusu(
    tmp_path, potok, capfd
):
    wynik = Zadanie(
        lambda: straznik._uruchom(
            ["polecenie-ktorego-nie-ma-har85"], str(tmp_path), potok.zapis, threading.Event()
        )
    ).wynik()
    potok.uznaj_za_zamkniety(potok.zapis)

    assert wynik == 127
    komunikat = czytaj_do_konca(potok.odczyt).decode()
    assert "polecenie-ktorego-nie-ma-har85" in komunikat
    assert capfd.readouterr().err == ""


def test_nieistniejacy_katalog_roboczy_daje_kod_127_a_sciezke_w_potoku_statusu(tmp_path, potok):
    brak = tmp_path / "brak-har85"

    wynik = Zadanie(
        lambda: straznik._uruchom(
            [sys.executable, "-I", "-c", "pass"], str(brak), potok.zapis, threading.Event()
        )
    ).wynik()
    potok.uznaj_za_zamkniety(potok.zapis)

    assert wynik == 127
    assert str(brak) in czytaj_do_konca(potok.odczyt).decode()


def test_nieistniejace_polecenie_bez_deskryptora_statusu_pisze_na_stderr_i_daje_127(
    tmp_path, capfd
):
    wynik = Zadanie(
        lambda: straznik._uruchom(
            ["polecenie-ktorego-nie-ma-har85"], str(tmp_path), None, threading.Event()
        )
    ).wynik()

    assert wynik == 127
    assert "polecenie-ktorego-nie-ma-har85" in capfd.readouterr().err


# --- punkt wejscia straznika ----------------------------------------------------


def test_brak_separatora_przed_poleceniem_daje_kod_2_bez_skutkow_ubocznych(
    tmp_path, capsys, handler_sigterm_przywrocony
):
    watkow_przed = threading.active_count()

    kod = straznik.main(["--cwd", str(tmp_path)], straznik.FD_WEJSCIA)

    assert kod == 2
    assert capsys.readouterr().err == "straznik: brak '--' przed poleceniem agenta\n"
    assert signal.getsignal(signal.SIGTERM) is handler_sigterm_przywrocony
    assert threading.active_count() == watkow_przed


def test_puste_polecenie_po_separatorze_daje_kod_2_bez_skutkow_ubocznych(
    tmp_path, capsys, handler_sigterm_przywrocony
):
    watkow_przed = threading.active_count()

    kod = straznik.main(["--cwd", str(tmp_path), "--"], straznik.FD_WEJSCIA)

    assert kod == 2
    assert capsys.readouterr().err == "straznik: puste polecenie agenta\n"
    assert signal.getsignal(signal.SIGTERM) is handler_sigterm_przywrocony
    assert threading.active_count() == watkow_przed


def test_brak_wymaganego_katalogu_roboczego_konczy_straznika_kodem_2(capsys):
    with pytest.raises(SystemExit) as info:
        straznik.main(["--", "true"], straznik.FD_WEJSCIA)

    blad = capsys.readouterr().err
    assert info.value.code == 2
    assert blad.startswith("usage: runner.straznik ")
    assert "--cwd" in blad


def test_main_zwraca_kod_agenta(tmp_path, potok, handler_sigterm_przywrocony):
    kod = straznik.main(
        ["--cwd", str(tmp_path), "--", sys.executable, "-I", "-c", "raise SystemExit(5)"],
        fd_wejscia=potok.odczyt,
    )

    assert kod == 5


def test_pierwszy_separator_dzieli_opcje_od_polecenia_a_dalsze_naleza_do_polecenia(
    tmp_path, potok, handler_sigterm_przywrocony
):
    # argv agenta: ['-c', '--', 'b'] -> 3; przy ostatnim separatorze byloby 2 (['-c', 'b']).
    kod = straznik.main(
        ["--cwd", str(tmp_path), "--", sys.executable, "-I", "-c",
         "import sys; raise SystemExit(len(sys.argv))", "--", "b"],
        fd_wejscia=potok.odczyt,
    )

    assert kod == 3


def test_main_przekazuje_deskryptor_statusu_a_udany_start_zamyka_go_bez_danych(
    tmp_path, potok, handler_sigterm_przywrocony
):
    status = Potok()
    try:
        kod = straznik.main(
            ["--cwd", str(tmp_path), "--status-fd", str(status.zapis), "--",
             sys.executable, "-I", "-c", "pass"],
            fd_wejscia=potok.odczyt,
        )
        status.uznaj_za_zamkniety(status.zapis)

        assert kod == 0
        assert czytaj_do_konca(status.odczyt) == b""
    finally:
        status.zamknij(status.zapis)
        status.zamknij(status.odczyt)


def test_main_uruchamia_agenta_w_katalogu_z_opcji_cwd(tmp_path, potok, handler_sigterm_przywrocony):
    kod_agenta = (
        "import os, sys\n"
        "sys.exit(0 if os.path.realpath(os.getcwd()) == os.path.realpath(sys.argv[1]) else 9)"
    )

    kod = straznik.main(
        ["--cwd", str(tmp_path), "--", sys.executable, "-I", "-c", kod_agenta, str(tmp_path)],
        fd_wejscia=potok.odczyt,
    )

    assert kod == 0


def test_zamkniecie_wejscia_przekazanego_do_main_zabija_grupe_agenta_kodem_143(
    tmp_path, potok, pgid_do_sprzatania, handler_sigterm_przywrocony
):
    plik = tmp_path / "agent.pid"

    def zamknij_po_starcie_agenta():
        # Gdy main nie czyta przekazanego wejscia, nikt nie zamknie agenta: po limicie
        # dobijamy grupe, a test oblewa na kodzie != 143.
        koniec = time.monotonic() + 15
        while not plik.exists() and time.monotonic() < koniec:
            time.sleep(0.02)
        if plik.exists():
            pgid_do_sprzatania.append(pgid_z_pliku(plik))
        time.sleep(0.3)
        potok.zamknij(potok.zapis)
        time.sleep(15)
        if plik.exists():
            zabij_grupe(pgid_z_pliku(plik))

    threading.Thread(target=zamknij_po_starcie_agenta, daemon=True).start()

    kod = straznik.main(
        ["--cwd", str(tmp_path), "--", *agent_spiacy(plik)], fd_wejscia=potok.odczyt
    )

    assert kod == 143
    assert grupa_zniknela(pgid_z_pliku(plik))


async def test_straznik_uruchomiony_jako_modul_przekazuje_kod_bledu_startu_127(katalog_kroku):
    straznik_proc = await uruchom_straznika(
        katalog_kroku,
        ["polecenie-ktorego-nie-ma-har85"],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    blad = await czekaj(straznik_proc.stderr.read(), 20, "stderr straznika")

    assert await czekaj(straznik_proc.wait(), 20, "koniec straznika") == 127
    assert b"polecenie-ktorego-nie-ma-har85" in blad


# --- srodowisko straznika (krok.py) ---------------------------------------------


def test_pakiet_biezacego_drzewa_stoi_przed_istniejacym_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/x")

    srodowisko_straznika = krok._srodowisko_straznika()

    assert srodowisko_straznika["PYTHONPATH"] == f"{krok.KATALOG_PAKIETU}{os.pathsep}/x"
    assert os.environ["PYTHONPATH"] == "/x"


def test_bez_pythonpath_srodowisko_straznika_ma_sam_pakiet_biezacego_drzewa(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    assert krok._srodowisko_straznika()["PYTHONPATH"] == str(krok.KATALOG_PAKIETU)


def test_pusty_pythonpath_nie_dokleja_pustego_wpisu(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "")

    assert krok._srodowisko_straznika()["PYTHONPATH"] == str(krok.KATALOG_PAKIETU)


def test_pozostale_zmienne_srodowiska_przechodza_do_straznika(monkeypatch):
    monkeypatch.setenv("HAR85_ZNACZNIK", "wartosc-har85")

    assert krok._srodowisko_straznika()["HAR85_ZNACZNIK"] == "wartosc-har85"


def test_straznik_dostaje_pierwotny_pythonpath_runnera_w_zmiennej_dla_agenta(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/x-har85")

    assert krok._srodowisko_straznika()["RUNNER_PYTHONPATH_AGENTA"] == "/x-har85"


def test_pusty_pythonpath_runnera_straznik_dostaje_jako_pusta_wartosc_a_nie_brak(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "")
    monkeypatch.delenv("RUNNER_BRAK_PYTHONPATH_AGENTA", raising=False)

    srodowisko_straznika = krok._srodowisko_straznika()

    assert srodowisko_straznika["RUNNER_PYTHONPATH_AGENTA"] == ""
    assert "RUNNER_BRAK_PYTHONPATH_AGENTA" not in srodowisko_straznika


def test_bez_pythonpath_runnera_straznik_dostaje_znacznik_braku_a_nie_wartosc(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("RUNNER_PYTHONPATH_AGENTA", raising=False)

    srodowisko_straznika = krok._srodowisko_straznika()

    assert srodowisko_straznika["RUNNER_BRAK_PYTHONPATH_AGENTA"] == "1"
    assert "RUNNER_PYTHONPATH_AGENTA" not in srodowisko_straznika


def test_wartosc_odziedziczona_z_otoczenia_nie_przeslania_braku_pythonpath_runnera(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_PYTHONPATH_AGENTA", "/stary-har157")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    srodowisko_straznika = krok._srodowisko_straznika()

    assert "RUNNER_PYTHONPATH_AGENTA" not in srodowisko_straznika
    assert srodowisko_straznika["RUNNER_BRAK_PYTHONPATH_AGENTA"] == "1"


def test_wartosc_odziedziczona_z_otoczenia_nie_przeslania_pierwotnego_pythonpath(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_PYTHONPATH_AGENTA", "/stary-har157")
    monkeypatch.setenv("PYTHONPATH", "/nowy-har157")

    assert krok._srodowisko_straznika()["RUNNER_PYTHONPATH_AGENTA"] == "/nowy-har157"


def test_znacznik_braku_odziedziczony_z_otoczenia_znika_gdy_runner_ma_pythonpath(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_BRAK_PYTHONPATH_AGENTA", "1")
    monkeypatch.setenv("PYTHONPATH", "/x-har157")

    srodowisko_straznika = krok._srodowisko_straznika()

    assert "RUNNER_BRAK_PYTHONPATH_AGENTA" not in srodowisko_straznika
    assert srodowisko_straznika["RUNNER_PYTHONPATH_AGENTA"] == "/x-har157"


def test_znacznik_braku_odziedziczony_z_otoczenia_znika_gdy_runner_ma_pusty_pythonpath(
    monkeypatch,
):
    monkeypatch.setenv("RUNNER_BRAK_PYTHONPATH_AGENTA", "1")
    monkeypatch.setenv("PYTHONPATH", "")

    srodowisko_straznika = krok._srodowisko_straznika()

    assert "RUNNER_BRAK_PYTHONPATH_AGENTA" not in srodowisko_straznika
    assert srodowisko_straznika["RUNNER_PYTHONPATH_AGENTA"] == ""


def test_nazwa_zmiennej_z_pythonpath_dla_agenta_jest_stala_i_jawna():
    assert straznik.ZMIENNA_PYTHONPATH_AGENTA == "RUNNER_PYTHONPATH_AGENTA"


def test_nazwa_zmiennej_z_brakiem_pythonpath_dla_agenta_jest_stala_i_jawna():
    assert straznik.ZMIENNA_BRAK_PYTHONPATH_AGENTA == "RUNNER_BRAK_PYTHONPATH_AGENTA"


def test_krok_oznacza_brak_pythonpath_ta_sama_zmienna_ktora_czyta_straznik(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    assert straznik.ZMIENNA_BRAK_PYTHONPATH_AGENTA in krok._srodowisko_straznika()


# --- srodowisko agenta (straznik.py) --------------------------------------------

M = "RUNNER_PYTHONPATH_AGENTA"
B = "RUNNER_BRAK_PYTHONPATH_AGENTA"


@pytest.mark.parametrize(
    ("wejscie", "oczekiwane"),
    [
        ({"A": "1"}, {"A": "1"}),
        ({"A": "1", "PYTHONPATH": "/p"}, {"A": "1", "PYTHONPATH": "/p"}),
        ({"A": "1", "PYTHONPATH": "/p", M: "/m"}, {"A": "1", "PYTHONPATH": "/m"}),
        ({"A": "1", M: "/m"}, {"A": "1", "PYTHONPATH": "/m"}),
        ({"A": "1", "PYTHONPATH": "/p", M: ""}, {"A": "1", "PYTHONPATH": ""}),
        ({"A": "1", M: ""}, {"A": "1", "PYTHONPATH": ""}),
        ({"A": "1", "PYTHONPATH": "/p", B: "1"}, {"A": "1"}),
        ({"A": "1", B: "1"}, {"A": "1"}),
        ({"A": "1", "PYTHONPATH": "/p", B: ""}, {"A": "1"}),
        ({"A": "1", "PYTHONPATH": "/p", M: "/m", B: "1"}, {"A": "1", "PYTHONPATH": "/m"}),
        ({"A": "1", "PYTHONPATH": "/p", M: "", B: "1"}, {"A": "1", "PYTHONPATH": ""}),
        ({"A": "1", M: "/m", B: "1"}, {"A": "1", "PYTHONPATH": "/m"}),
    ],
    ids=[
        "bez_znacznikow_bez_pythonpath",
        "bez_znacznikow_pythonpath_zostaje",
        "znacznik_zastepuje_pythonpath",
        "znacznik_ustawia_brakujacy_pythonpath",
        "pusty_znacznik_ustawia_pusty_pythonpath",
        "pusty_znacznik_bez_pythonpath_ustawia_pusty",
        "znacznik_braku_usuwa_pythonpath",
        "znacznik_braku_bez_pythonpath",
        "pusty_znacznik_braku_tez_usuwa_pythonpath",
        "wartosc_ma_pierwszenstwo_przed_brakiem",
        "pusta_wartosc_ma_pierwszenstwo_przed_brakiem",
        "wartosc_i_brak_bez_pythonpath",
    ],
)
def test_srodowisko_agenta_przywraca_pierwotny_pythonpath_runnera_a_reszte_zostawia(
    wejscie, oczekiwane
):
    wynik = straznik._srodowisko_agenta(wejscie)

    assert wynik == oczekiwane
    assert M not in wynik
    assert B not in wynik


def test_srodowisko_agenta_to_kopia_a_wejscie_zostaje_nietkniete():
    wejscie = {"A": "1", "PYTHONPATH": "/p", M: "/m", B: "1"}
    migawka_wejscia = dict(wejscie)

    wynik = straznik._srodowisko_agenta(wejscie)
    wynik["B"] = "2"

    assert wejscie == migawka_wejscia
    bez_znacznika = {"A": "1"}
    kopia = straznik._srodowisko_agenta(bez_znacznika)
    kopia["B"] = "2"
    assert kopia is not bez_znacznika
    assert bez_znacznika == {"A": "1"}


def test_srodowisko_agenta_przyjmuje_odwzorowanie_tylko_do_odczytu():
    wejscie = types.MappingProxyType({"PYTHONPATH": "/p", M: "/m"})

    assert straznik._srodowisko_agenta(wejscie) == {"PYTHONPATH": "/m"}


def test_straznik_daje_agentowi_pierwotny_pythonpath_runnera_a_nie_swoj(
    tmp_path, monkeypatch
):
    # U rodzica PYTHONPATH (to, co dostaje straznik) rozni sie od oczekiwanego u agenta.
    monkeypatch.setenv("RUNNER_PYTHONPATH_AGENTA", "/y-har85")
    monkeypatch.setenv("RUNNER_BRAK_PYTHONPATH_AGENTA", "1")
    monkeypatch.setenv("PYTHONPATH", "/z-inny-har85")
    kod = (
        "import os, sys\n"
        "ok = (os.environ.get('PYTHONPATH') == '/y-har85'\n"
        "      and 'RUNNER_PYTHONPATH_AGENTA' not in os.environ\n"
        "      and 'RUNNER_BRAK_PYTHONPATH_AGENTA' not in os.environ)\n"
        "sys.exit(0 if ok else 5)\n"
    )

    wynik = Zadanie(
        lambda: straznik._uruchom(
            [sys.executable, "-I", "-c", kod], str(tmp_path), None, threading.Event()
        )
    ).wynik()

    assert wynik == 0


KOD_WYPISUJACY_PYTHONPATH = "import os; print(repr(os.environ.get('PYTHONPATH')))"


async def _wyjscie_agenta_bez_straznika(srodowisko: dict[str, str]) -> str:
    proc = await uruchom_proces(
        sys.executable,
        "-I",
        "-c",
        KOD_WYPISUJACY_PYTHONPATH,
        env=srodowisko,
        cwd=str(KORZEN),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    wyjscie, _ = await czekaj(proc.communicate(), 20, "agent bez straznika")
    assert proc.returncode == 0
    return wyjscie.decode().strip()


@pytest.mark.parametrize(
    ("pythonpath_runnera", "oczekiwane_wyjscie"),
    [("/x-har85", "'/x-har85'"), (None, "None"), ("", "''")],
    ids=["ustawiony", "nieustawiony", "pusty_a_nie_brak"],
)
async def test_agent_pod_straznikiem_widzi_pythonpath_runnera_jak_bez_straznika(
    monkeypatch, katalog_kroku, pythonpath_runnera, oczekiwane_wyjscie
):
    if pythonpath_runnera is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", pythonpath_runnera)
    monkeypatch.delenv("RUNNER_PYTHONPATH_AGENTA", raising=False)
    monkeypatch.delenv("RUNNER_BRAK_PYTHONPATH_AGENTA", raising=False)
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await czekaj(
        wykonaj_krok(
            spec(),
            [sys.executable, "-I", "-c", KOD_WYPISUJACY_PYTHONPATH],
            katalog_kroku,
            emit,
        ),
        30,
        "krok wypisujacy PYTHONPATH",
    )
    bez_straznika = await _wyjscie_agenta_bez_straznika(dict(os.environ))

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert [z["text"] for z in zdarzenia] == [oczekiwane_wyjscie]
    assert bez_straznika == oczekiwane_wyjscie


def test_katalog_pakietu_to_drzewo_z_pakietem_runner_a_korzen_to_jego_rodzic():
    assert krok.KATALOG_PAKIETU == Path(krok.__file__).parent.parent
    assert (krok.KATALOG_PAKIETU / "runner" / "straznik.py").is_file()
    assert krok.KATALOG_KORZENIA == krok.KATALOG_PAKIETU.parent
    assert (krok.KATALOG_KORZENIA / "pyproject.toml").is_file()


# --- czekanie na straznika po kroku (krok.py) -----------------------------------

KOD_IGNORUJACY_SIGTERM_I_EOF = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('gotowy', flush=True)\n"
    "time.sleep(60)\n"
)
KOD_KONCZACY_SIE_NA_EOF = (
    "import sys\n"
    "print('gotowy', flush=True)\n"
    "sys.stdin.read()\n"
)


async def uruchom_straznika_atrape(kod: str) -> asyncio.subprocess.Process:
    proc = await uruchom_proces(
        sys.executable, "-I", "-c", kod,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert await czekaj(proc.stdout.readline(), 15, "gotowosc atrapy straznika") == b"gotowy\n"
    return proc


async def test_straznik_ktory_nie_konczy_sie_w_limicie_dostaje_sigkill_na_swoja_grupe(
    monkeypatch,
):
    proc = await uruchom_straznika_atrape(KOD_IGNORUJACY_SIGTERM_I_EOF)
    try:
        monkeypatch.setattr(krok, "LIMIT_NA_STRAZNIKA_S", 0.5)

        start = time.monotonic()
        await czekaj(krok._czekaj_na_straznika(proc), 10, "czekanie na straznika")

        assert proc.returncode == -9
        assert time.monotonic() - start >= 0.5
    finally:
        zabij_proces(proc.pid)
        await czekaj(proc.wait(), 5, "koniec atrapy straznika")


async def test_straznik_konczacy_sie_na_eof_nie_dostaje_sigkill_a_czekanie_trwa_krotko(
    monkeypatch,
):
    proc = await uruchom_straznika_atrape(KOD_KONCZACY_SIE_NA_EOF)
    try:
        monkeypatch.setattr(krok, "LIMIT_NA_STRAZNIKA_S", 3)

        start = time.monotonic()
        await czekaj(krok._czekaj_na_straznika(proc), 10, "czekanie na straznika")

        assert proc.returncode == 0
        assert time.monotonic() - start < 2.5
    finally:
        zabij_proces(proc.pid)
        await czekaj(proc.wait(), 5, "koniec atrapy straznika")


@pytest.mark.parametrize("wyjatek", [ProcessLookupError, PermissionError])
async def test_brak_grupy_straznika_przy_sigkill_ostatecznosci_nie_rzuca(monkeypatch, wyjatek):
    proc = await uruchom_straznika_atrape(KOD_IGNORUJACY_SIGTERM_I_EOF)
    try:
        monkeypatch.setattr(krok, "LIMIT_NA_STRAZNIKA_S", 0.3)

        def grupa_zniknela_miedzy_sprawdzeniem_a_sygnalem(pgid, sygnal):
            zabij_proces(proc.pid)
            raise wyjatek()

        podmien_killpg(monkeypatch, krok, grupa_zniknela_miedzy_sprawdzeniem_a_sygnalem)

        await czekaj(krok._czekaj_na_straznika(proc), 10, "czekanie na straznika")

        assert proc.returncode == -9
    finally:
        zabij_proces(proc.pid)
        await czekaj(proc.wait(), 5, "koniec atrapy straznika")


async def test_inny_blad_systemowy_przy_sigkill_ostatecznosci_nie_jest_polykany(monkeypatch):
    proc = await uruchom_straznika_atrape(KOD_IGNORUJACY_SIGTERM_I_EOF)
    try:
        monkeypatch.setattr(krok, "LIMIT_NA_STRAZNIKA_S", 0.3)
        podmien_killpg(monkeypatch, krok, rzuc(OSError(5, "inny blad")))

        with pytest.raises(OSError) as info:
            await czekaj(krok._czekaj_na_straznika(proc), 10, "czekanie na straznika")

        assert type(info.value) is OSError
    finally:
        zabij_proces(proc.pid)
        await czekaj(proc.wait(), 5, "koniec atrapy straznika")


# --- wykonaj_krok: blad startu straznika i parametry startu ----------------------


async def emit_pusty(zdarzenie: dict) -> None:
    pass


async def test_nieistniejace_polecenie_kroku_daje_porazke_z_pelnym_komunikatem_i_bez_kodu(
    katalog_kroku,
):
    nazwa = "polecenie-ktorego-nie-ma-har85"

    wynik = await czekaj(
        wykonaj_krok(spec(), [nazwa], katalog_kroku, emit_pusty), 30, "krok z brakujacym poleceniem"
    )

    assert wynik == WynikKroku(
        "nieudany",
        f"nie można uruchomić polecenia: [Errno 2] No such file or directory: {nazwa!r}",
        None,
    )


async def test_nieistniejacy_katalog_roboczy_kroku_daje_porazke_ze_sciezka_katalogu(tmp_path):
    brak = tmp_path / "brak-har85"

    wynik = await czekaj(
        wykonaj_krok(spec(), [sys.executable, "-I", "-c", "pass"], brak, emit_pusty),
        30,
        "krok z brakujacym katalogiem",
    )

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można uruchomić polecenia: ")
    assert str(brak) in wynik.powod


async def test_blad_systemowy_przy_starcie_straznika_daje_porazke_i_nie_wycieka_deskryptorow(
    monkeypatch, katalog_kroku
):
    async def start_ktory_pada(*argv, **kwargs):
        raise OSError("brak-har85")

    monkeypatch.setattr(krok.asyncio, "create_subprocess_exec", start_ktory_pada)
    przed = liczba_deskryptorow()

    wynik = await czekaj(
        wykonaj_krok(spec(), ["polecenie"], katalog_kroku, emit_pusty), 10, "krok z bledem startu"
    )

    assert wynik == WynikKroku("nieudany", "nie można uruchomić polecenia: brak-har85", None)
    assert liczba_deskryptorow() == przed


async def test_anulowanie_w_trakcie_startu_straznika_przepuszcza_wyjatek_bez_wycieku_deskryptorow(
    monkeypatch, katalog_kroku
):
    async def start_anulowany(*argv, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(krok.asyncio, "create_subprocess_exec", start_anulowany)
    przed = liczba_deskryptorow()

    with pytest.raises(asyncio.CancelledError) as info:
        await czekaj(
            wykonaj_krok(spec(), ["polecenie"], katalog_kroku, emit_pusty), 10, "krok anulowany"
        )

    assert info.value is not None
    assert liczba_deskryptorow() == przed


async def test_straznik_startuje_z_korzenia_repozytorium_nowa_sesja_i_potokiem_na_stdin(
    monkeypatch, tmp_path
):
    zapis: dict = {}

    async def spy(*argv, **kwargs):
        zapis["argv"] = list(argv)
        zapis["kwargs"] = kwargs
        raise OSError("koniec-har85")

    monkeypatch.setattr(krok.asyncio, "create_subprocess_exec", spy)
    monkeypatch.chdir(tmp_path)

    await czekaj(
        wykonaj_krok(spec(), ["agent", "--opcja", "x"], Path("podkatalog-har85"), emit_pusty),
        10,
        "krok ze szpiegiem startu",
    )

    argv, kwargs = zapis["argv"], zapis["kwargs"]
    numer_fd = int(argv[4])
    assert argv == [
        sys.executable, "-m", "runner.straznik",
        "--status-fd", str(numer_fd),
        "--cwd", os.path.join(os.getcwd(), "podkatalog-har85"),
        "--",
        "agent", "--opcja", "x",
    ]
    assert kwargs["cwd"] == krok.KATALOG_KORZENIA
    assert (krok.KATALOG_KORZENIA / "pyproject.toml").exists()
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(krok.KATALOG_PAKIETU)
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert kwargs["pass_fds"] == (numer_fd,)


async def test_anulowanie_kroku_z_agentem_ignorujacym_sigterm_zabija_jego_grupe_w_15_sekund(
    tmp_path, katalog_kroku, pgid_do_sprzatania
):
    # Straznik daje agentowi 5 s na SIGTERM, potem SIGKILL. Gdyby runner czekal na
    # straznika krocej (LIMIT_NA_STRAZNIKA_S < 5), zabilby go przed jego SIGKILL-em
    # i agent ignorujacy SIGTERM zostalby sierota.
    plik = tmp_path / "agent.pid"
    zadanie = asyncio.create_task(
        wykonaj_krok(spec(), agent_spiacy(plik, ignoruj_sigterm=True), katalog_kroku, emit_pusty)
    )
    try:
        await czekaj_na_warunek(plik.exists, LIMIT_START, "agent zapisal pid i pgid")
        pgid = int(plik.read_text().split()[1])
        pgid_do_sprzatania.append(pgid)

        zadanie.cancel()
        with pytest.raises(asyncio.CancelledError):
            await czekaj_bez_anulowania(zadanie, 15, "koniec zadania kroku po anulowaniu")
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie}, timeout=15)

    assert await grupa_umarla(pgid, limit=LIMIT_ZNIKNIECIA)

"""HAR-94: hooki kroku z `fanout: 5` daja w kokpicie piecioro subagentow (TDD, testy przed kodem).

Sciezka produkcyjna: `runner.client.Runner._run_workflow` -> prawdziwy `silnik.wykonaj_run` ->
prawdziwe `wykonaj_krok` na prawdziwych procesach -> `CzytnikHookow`/`przekazuj` -> `send_event` ->
prawdziwa aplikacja kokpitu na SQLite. Zaslepiony jest tylko agent: skrypt `claude` na PATH czyta
plik z `--settings`, uruchamia z niego polecenia hookow `SubagentStart`/`SubagentStop` (tak jak
robi to Claude Code, ze srodowiskiem agenta) i zapisuje znacznik z tym, co widzial w trakcie
kroku (sciezka i prawa pliku ustawien). Wszystkie piec procesow musi zyc naraz (bariera).
Asercje czytaja dane: tabele `event` i pliki w katalogu stanu runnera. Procesy startuje produkcyjny
`wykonaj_krok`, wiec w tescie nie ma wlasnych; kazde czekanie ma limit czasu.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from procesy import czekaj_na_warunek, zadanie_tla
from runner import client as modul_klienta
from runner import hooks, silnik
from runner.bufor import Bufor
from runner.client import Runner as RunnerKlienta
from runner.client import TokenOdrzucony
from test_polecenie_run_workflow import Kokpit
from test_silnik_runu import K, swiat

TOKEN = "token-hookow-fanoutu-9412"
LIMIT_S = 90
LICZBA = 5
KROK = "analiza"

# Atrapa `claude -p ... --settings <plik>`: czyta stdin (prompt), zapisuje znacznik startu ze
# stanem pliku ustawien widzianym w trakcie kroku, czeka na pozostale warianty (bariera z
# limitem czasu), uruchamia hooki z pliku ustawien i konczy zaraz po SubagentStop. Brak
# `--settings` = brak hookow (zadnego zdarzenia): tak dzis wyglada krok bez podpiecia.
# Kazdy wariant wysyla SubagentStart/SubagentStop z wlasnym agent_id i agent_type, bo tak ticket
# HAR-94 opisuje tozsamosc wariantu fanoutu w zdarzeniach hookow. Zmienne srodowiska sterujace:
# FANOUT_SPANIE (spanie po SubagentStop), FANOUT_KOD (kod wyjscia), FANOUT_HOOKI_NA_KONCU=1
# (wynik na stdout przed hookami), FANOUT_PADA_SKILL (pierwsze wywolanie tego skilla konczy sie
# kodem 42), FANOUT_STOP_OSTATNI (jeden wariant konczy po pozostalych), FANOUT_USUN_CLAUDE
# (agent po pracy usuwa wlasny skrypt `claude`).
KOD_CLAUDE = r"""
import json, os, re, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
prompt = sys.stdin.read()
znaczniki = Path(os.environ["FANOUT_ZNACZNIKI"])
liczba = int(os.environ["FANOUT_LICZBA"])
agent = "sub-%d" % os.getpid()
trafienie = re.search(r"/(s_[A-Za-z0-9_.-]+)/SKILL\.md", prompt)
skill = trafienie.group(1) if trafienie else None
opis = {"agent_id": agent, "ustawienia": None, "istnieje": False, "prawa": None,
        "plik_hookow": os.environ.get("COCKPIT_HOOK_FILE"), "skill": skill}
hooki = {}
if "--settings" in argv:
    plik = Path(argv[argv.index("--settings") + 1])
    opis["ustawienia"] = str(plik)
    opis["istnieje"] = plik.is_file()
    if plik.is_file():
        opis["prawa"] = plik.stat().st_mode & 0o777
        hooki = json.loads(plik.read_text())["hooks"]
tmp = znaczniki / (agent + ".tmp")
tmp.write_text(json.dumps(opis))
os.replace(tmp, znaczniki / ("start_" + agent + ".json"))


def hook(nazwa):
    grupy = hooki.get(nazwa)
    if not grupy:
        return
    polecenie = grupy[0]["hooks"][0]["command"]
    ladunek = {"session_id": "s-" + agent, "hook_event_name": nazwa,
               "agent_id": agent, "agent_type": "general-purpose"}
    subprocess.run(["sh", "-c", polecenie], input=json.dumps(ladunek).encode(), timeout=30)


def wynik():
    print(json.dumps({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": "ok"}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "ok"}), flush=True)


na_koncu = os.environ.get("FANOUT_HOOKI_NA_KONCU") == "1"
if na_koncu:
    wynik()
hook("SubagentStart")
koniec = time.time() + 30
while len(list(znaczniki.glob("start_*.json"))) < liczba:
    if time.time() > koniec:
        sys.exit(9)
    time.sleep(0.05)
if os.environ.get("FANOUT_STOP_OSTATNI") == "1":
    # wskazany wariant (najmniejszy agent_id) wysyla SubagentStop dopiero po koncu pozostalych
    # czterech i ~2 s marginesu: czytnik musi na niego poczekac
    wskazany = min(p.name[len("start_"):-len(".json")] for p in znaczniki.glob("start_*.json"))
    if agent == wskazany:
        koniec = time.time() + 30
        while len(list(znaczniki.glob("koniec_*.txt"))) < liczba - 1:
            if time.time() > koniec:
                sys.exit(9)
            time.sleep(0.05)
        time.sleep(2)
        (znaczniki / "wskazany").write_text(agent)
hook("SubagentStop")
time.sleep(float(os.environ.get("FANOUT_SPANIE", "0")))
if not na_koncu:
    wynik()
(znaczniki / ("koniec_" + agent + ".txt")).write_text("1")
if os.environ.get("FANOUT_USUN_CLAUDE") == "1":
    os.remove(sys.argv[0])      # kolejne kroki nie znajda programu `claude`
kod = int(os.environ.get("FANOUT_KOD", "0"))
if skill and skill == os.environ.get("FANOUT_PADA_SKILL") and not (znaczniki / "padl").exists():
    (znaczniki / "padl").write_text("1")
    kod = 42
sys.exit(kod)
"""


class RejestrujacyKokpit(httpx.AsyncBaseTransport):
    """Kokpit-atrapa dla testu, w ktorym run nie moze istniec w bazie: zapisuje zdarzenia."""

    def __init__(self) -> None:
        self.zdarzenia: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/events":
            self.zdarzenia.append(json.loads(request.content))
            return httpx.Response(200, json={"seq": len(self.zdarzenia)})
        return httpx.Response(200)


async def _bez_odczekania(sekundy: float) -> None:
    return None


@dataclass
class SwiatHookow:
    s: SimpleNamespace          # swiat z test_silnik_runu (repo, worktree, katalog runu)
    runner: RunnerKlienta
    polecenie: dict
    znaczniki: Path
    stan: Path

    async def przebieg(self) -> None:
        await asyncio.wait_for(self.runner._run_workflow(self.polecenie), LIMIT_S)

    def start_agentow(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.znaczniki.glob("start_*.json"))]

    def pliki_ustawien(self) -> list[Path]:
        return sorted((self.stan / "hooks").rglob("*.settings.json"))


def przygotuj(
    tmp_path: Path, monkeypatch, transport: httpx.AsyncBaseTransport, run_id: str,
    *, kod: int = 0, spanie: float = 0.0, krok: str = KROK, kroki: list[K] | None = None,
    z_claude: bool = True, hooki_na_koncu: bool = False, pada_skill: str = "",
    liczba: int = LICZBA, stop_ostatni: bool = False, usun_claude: bool = False,
) -> SwiatHookow:
    if kroki is None:
        s = swiat(tmp_path, [K(krok, skill="s_analiza", extra=f", fanout: {LICZBA}")],
                  skille_dodatkowe={"s_analiza": {}})
    else:
        s = swiat(tmp_path, kroki)
    stan = tmp_path / "stan"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    # PATH tylko z tego katalogu: prawdziwy `claude` z systemu nigdy nie zostanie uruchomiony,
    # a silnik i atrapa potrzebuja tylko gita, sh i env
    for program in ("git", "sh", "env"):
        (bin_ / program).symlink_to(shutil.which(program))
    if z_claude:
        claude = bin_ / "claude"
        claude.write_text(f"#!{sys.executable}\n{KOD_CLAUDE}")
        claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(stan))
    monkeypatch.setenv("FANOUT_ZNACZNIKI", str(s.znaczniki))
    monkeypatch.setenv("FANOUT_LICZBA", str(liczba))
    monkeypatch.setenv("FANOUT_KOD", str(kod))
    monkeypatch.setenv("FANOUT_SPANIE", str(spanie))
    monkeypatch.setenv("FANOUT_HOOKI_NA_KONCU", "1" if hooki_na_koncu else "0")
    monkeypatch.setenv("FANOUT_PADA_SKILL", pada_skill)
    monkeypatch.setenv("FANOUT_STOP_OSTATNI", "1" if stop_ostatni else "0")
    monkeypatch.setenv("FANOUT_USUN_CLAUDE", "1" if usun_claude else "0")

    prawdziwy = silnik.wykonaj_run
    prawdziwe_wznowienie = silnik.wznow_run

    async def z_swiatem(polecenie, emit, *args, **kw):
        # produkcyjny silnik; swiat testu tylko podaje profil, katalogi i zakres
        return await prawdziwy(
            polecenie, emit, *args, profil=s.profil, dom=s.dom, root=s.root, zakres=s.zakres, **kw)

    async def wznowienie_ze_swiatem(run_id, step_id, decyzja, emit, *args, **kw):
        return await prawdziwe_wznowienie(
            run_id, step_id, decyzja, emit, *args, profil=s.profil, dom=s.dom, root=s.root, **kw)

    monkeypatch.setattr(silnik, "wykonaj_run", z_swiatem)
    monkeypatch.setattr(silnik, "wznow_run", wznowienie_ze_swiatem)
    runner = RunnerKlienta(
        "http://test", TOKEN, transport=transport, bufor=Bufor(tmp_path / "bufor"),
        odczekaj=_bez_odczekania)
    polecenie = {**s.polecenie, "run_id": run_id}
    return SwiatHookow(s, runner, polecenie, s.znaczniki, stan)


@pytest.fixture
async def z_baza(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'baza.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as sesja:
        zespol = Tenant(name="Browarek")
        sesja.add(zespol)
        await sesja.flush()
        runner = Runner(
            tenant_id=zespol.id, name="macbook", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        sesja.add(runner)
        await sesja.flush()
        run = Run(tenant_id=zespol.id, runner_id=runner.id, status=RunStatus.running)
        sesja.add(run)
        await sesja.commit()
        run_id = run.id
    try:
        yield SimpleNamespace(run_id=run_id)
    finally:
        await engine.dispose()


def swiat_z_baza(tmp_path, monkeypatch, baza, **opcje) -> SwiatHookow:
    return przygotuj(tmp_path, monkeypatch, ASGITransport(app=app), baza.run_id, **opcje)


async def zdarzenia_hookow() -> list[Event]:
    async with db_session.SessionLocal() as sesja:
        wiersze = (await sesja.execute(select(Event).order_by(Event.seq))).scalars().all()
    return [e for e in wiersze if e.kind.startswith("hook:")]


def agenci_z_znacznikow(sw: SwiatHookow) -> set[str]:
    return {z["agent_id"] for z in sw.start_agentow()}


# --- kryterium 1: piecioro subagentow w bazie -----------------------------------------------------


async def test_krok_z_fanout_5_ma_w_bazie_piec_roznych_agent_id_subagentow_z_ich_agent_type(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    starty = [e for e in await zdarzenia_hookow() if e.kind == "hook:SubagentStart"]
    assert agenci_z_znacznikow(sw) != set()
    assert {e.agent_id for e in starty} == agenci_z_znacznikow(sw)     # dokladnie ci, ktorzy ruszyli
    assert len({e.agent_id for e in starty}) == LICZBA
    assert {e.agent_type for e in starty} == {"general-purpose"}


async def test_koniec_subagenta_dociera_do_bazy_takze_gdy_agent_konczy_proces_zaraz_po_nim(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    konce = [e for e in await zdarzenia_hookow() if e.kind == "hook:SubagentStop"]
    assert agenci_z_znacznikow(sw) != set()
    assert {e.agent_id for e in konce} == agenci_z_znacznikow(sw)


# --- kryterium 2: run_id i step_id kroku ------------------------------------------------------------


async def test_kazde_zdarzenie_hookow_kroku_ma_w_bazie_run_id_i_step_id_tego_kroku(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    hooki = await zdarzenia_hookow()
    assert hooki, "krok nie dal w bazie zadnego zdarzenia hookow"
    assert {(e.run_id, e.step_id) for e in hooki} == {(z_baza.run_id, KROK)}


# --- brak duplikatow przy pieciu wariantach ----------------------------------------------------------


async def test_pieciu_wariantow_jednego_kroku_nie_dubluje_zdarzen_hookow_w_bazie(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    hooki = await zdarzenia_hookow()
    pary = sorted((e.kind, e.agent_id) for e in hooki)
    assert len(pary) == 2 * LICZBA, f"oczekiwano 5 startow i 5 koncow subagentow: {pary}"
    assert len(set(pary)) == len(pary)


# --- kryterium 3: plik ustawien poza repo, 0600, znika po kroku ---------------------------------------


async def test_plik_ustawien_kroku_ma_w_trakcie_kroku_prawa_0600_dla_kazdego_z_pieciu_agentow(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    starty = sw.start_agentow()
    assert len(starty) == LICZBA
    assert [z["istnieje"] for z in starty] == [True] * LICZBA, "agent nie dostal pliku --settings"
    assert [z["prawa"] for z in starty] == [0o600] * LICZBA


async def test_plik_ustawien_kroku_lezy_w_katalogu_stanu_runnera_poza_repo_i_worktree(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    sciezki = {z["ustawienia"] for z in sw.start_agentow()}
    assert sciezki and None not in sciezki, "agent nie dostal pliku --settings"
    for sciezka in map(Path, sciezki):
        assert sciezka.is_relative_to(sw.stan / "hooks")
        assert not sciezka.is_relative_to(sw.s.repo)
        assert not sciezka.is_relative_to(sw.s.worktree)
    assert stat.S_IMODE((sw.stan / "hooks").stat().st_mode) == 0o700


async def test_plik_ustawien_znika_po_kroku_zakonczonym_sukcesem(tmp_path, monkeypatch, z_baza):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    sciezki = {z["ustawienia"] for z in sw.start_agentow()}
    assert sciezki and None not in sciezki, "agent nie dostal pliku --settings"
    assert [p for p in map(Path, sciezki) if p.exists()] == []
    assert sw.pliki_ustawien() == []


async def test_plik_ustawien_znika_po_kroku_ktorego_agent_pada(tmp_path, monkeypatch, z_baza):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, kod=7)

    await sw.przebieg()

    sciezki = {z["ustawienia"] for z in sw.start_agentow()}
    assert sciezki and None not in sciezki, "agent nie dostal pliku --settings"
    assert [p for p in map(Path, sciezki) if p.exists()] == []
    assert sw.pliki_ustawien() == []


async def test_plik_ustawien_znika_po_anulowaniu_runu_w_trakcie_kroku(tmp_path, monkeypatch, z_baza):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, spanie=120)

    async with zadanie_tla(sw.runner._run_workflow(sw.polecenie), "run z fanoutem") as zadanie:
        await czekaj_na_warunek(
            lambda: len(sw.start_agentow()) == LICZBA, LIMIT_S, "start pieciu agentow")
        sciezki = {z["ustawienia"] for z in sw.start_agentow()}
        assert sciezki and None not in sciezki, "agent nie dostal pliku --settings"
        assert all(Path(p).is_file() for p in sciezki)             # w trakcie kroku plik istnieje
        zadanie.cancel()
        zakonczone, _ = await asyncio.wait({zadanie}, timeout=LIMIT_S)
        assert zadanie in zakonczone

    assert [p for p in map(Path, sciezki) if p.exists()] == []
    assert sw.pliki_ustawien() == []


# --- zly identyfikator: blad dziedziny, nie traceback -------------------------------------------------


async def test_krok_ze_zlym_dla_hookow_identyfikatorem_konczy_sie_bledem_dziedziny_bez_tracebacku(
    tmp_path, monkeypatch, capsys,
):
    # kropka w id kroku przechodzi workflow i worktree, ale nie nazwe pliku hookow
    # `[A-Za-z0-9_-]{1,64}`: blad ma byc zgloszony jako koniec kroku i runu, nie wyjatek
    zly_krok = "analiza.v1"
    kokpit = RejestrujacyKokpit()
    sw = przygotuj(tmp_path, monkeypatch, kokpit, "run-zly-krok", krok=zly_krok)

    await sw.przebieg()

    porazki = [z for z in kokpit.zdarzenia if z["kind"] == "step_failed" and z["step_id"] == zly_krok]
    assert porazki, (
        "zly identyfikator nie skonczyl kroku zdarzeniem step_failed: "
        f"{[(z['kind'], z['step_id'], z['text']) for z in kokpit.zdarzenia]}")
    assert all("step_id" in z["text"] for z in porazki)
    assert [z["kind"] for z in kokpit.zdarzenia].count("run_finished") == 1
    assert kokpit.zdarzenia[-1]["kind"] == "run_finished"
    assert sw.start_agentow() == []                    # agent nie wystartowal bez hookow
    assert "Traceback" not in capsys.readouterr().err
    assert sw.pliki_ustawien() == []


# --- kolejne reguly implementacji: czytanie w trakcie kroku, odczyt koncowy, ponowienie, bramka -----


async def czekaj_az(warunek, limit: float, nazwa: str) -> None:
    koniec = time.monotonic() + limit
    while not await warunek():
        if time.monotonic() >= koniec:
            raise AssertionError(f"limit {limit} s: {nazwa}")
        await asyncio.sleep(0.05)


async def wszystkie_zdarzenia() -> list[Event]:
    async with db_session.SessionLocal() as sesja:
        return list((await sesja.execute(select(Event).order_by(Event.seq))).scalars().all())


def pary_hookow(zdarzenia: list[Event], krok: str = KROK) -> list[tuple[str, str]]:
    return sorted((e.kind, e.agent_id) for e in zdarzenia
                  if e.kind.startswith("hook:") and e.step_id == krok)


def oczekiwane_pary(agenci: set[str]) -> list[tuple[str, str]]:
    return sorted([("hook:SubagentStart", a) for a in agenci]
                  + [("hook:SubagentStop", a) for a in agenci])


async def test_start_subagenta_jest_w_bazie_zanim_krok_sie_skonczy(tmp_path, monkeypatch, z_baza):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, spanie=120)

    async with zadanie_tla(sw.runner._run_workflow(sw.polecenie), "run z fanoutem") as zadanie:
        async def piec_startow() -> bool:
            return len([e for e in await zdarzenia_hookow() if e.kind == "hook:SubagentStart"]) == LICZBA

        await czekaj_az(piec_startow, 30, "pieciu startow subagentow w bazie w trakcie kroku")

        assert not zadanie.done()                      # krok trwa: agenci spia po SubagentStop


async def test_koniec_subagenta_dociera_do_bazy_dzieki_odczytowi_po_kroku_gdy_czytnik_ma_dlugi_interwal(
    tmp_path, monkeypatch, z_baza,
):
    prawdziwy = hooks.CzytnikHookow
    monkeypatch.setattr(
        hooks, "CzytnikHookow",
        lambda plik, run_id, step_id: prawdziwy(plik, run_id, step_id, interwal=1000.0))
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    # petla czytnika czyta plik raz na starcie (pusty); wszystko dalej to odczyt po kroku
    assert pary_hookow(await zdarzenia_hookow()) == oczekiwane_pary(agenci_z_znacznikow(sw))
    assert len(agenci_z_znacznikow(sw)) == LICZBA


async def test_ponowienie_kroku_z_fanout_5_nie_powtarza_zdarzen_pierwszej_proby_a_druga_ma_wlasne(
    tmp_path, monkeypatch, z_baza,
):
    kroki = [K(KROK, extra=f", fanout: {LICZBA}"),
             K("testuj", (KROK,), extra=f", on_fail: {{retry: {KROK}, max: 1}}")]
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, kroki=kroki, pada_skill="s_testuj")

    await sw.przebieg()

    kinds = [e.kind for e in await wszystkie_zdarzenia()]
    assert kinds.count("run_finished") == 1
    assert (sw.znaczniki / "padl").is_file()                       # testuj naprawde padl raz
    agenci = {z["agent_id"] for z in sw.start_agentow() if z["skill"] == "s_analiza"}
    assert len(agenci) == 2 * LICZBA                               # dwie proby po piecioro
    assert pary_hookow(await zdarzenia_hookow()) == oczekiwane_pary(agenci)


async def test_krok_uruchomiony_po_decyzji_czlowieka_ma_w_bazie_zdarzenia_hookow_wszystkich_wariantow(
    tmp_path, monkeypatch, z_baza,
):
    kroki = [K(KROK, extra=f", approval: true, fanout: {LICZBA}")]
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, kroki=kroki)
    await sw.przebieg()
    assert sw.start_agentow() == []                                # bramka stoi przed procesami
    assert await zdarzenia_hookow() == []

    decyzja = {"type": "resume", "run_id": z_baza.run_id, "step_id": KROK, "decyzja": "approve"}
    await asyncio.wait_for(sw.runner._resume(decyzja), LIMIT_S)

    assert len(agenci_z_znacznikow(sw)) == LICZBA
    assert pary_hookow(await zdarzenia_hookow()) == oczekiwane_pary(agenci_z_znacznikow(sw))


class ZawieszonyKokpit(RejestrujacyKokpit):
    """Kokpit, ktory przyjmuje zdarzenia silnika, ale na zdarzenia hookow nie odpowiada nigdy."""

    def __init__(self) -> None:
        super().__init__()
        self.zawieszone = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/events" and json.loads(request.content)["kind"].startswith("hook:"):
            self.zawieszone += 1
            await asyncio.sleep(3600)
        return await super().handle_async_request(request)


async def test_krok_konczy_sie_po_limicie_doczytania_gdy_kokpit_nie_odpowiada_na_zdarzenia_hookow(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(modul_klienta, "CZAS_DOCZYTANIA_HOOKOW_S", 1.0)
    kokpit = ZawieszonyKokpit()
    # jeden wariant i hooki po wyniku agenta: zawieszona wysylka (trzyma blokade zapisu) nie
    # blokuje zdarzen silnika w kroku, wiec o koncu decyduje tylko limit doczytania
    sw = przygotuj(
        tmp_path, monkeypatch, kokpit, "run-zawieszony", hooki_na_koncu=True,
        kroki=[K(KROK)], liczba=1)

    # bufor wysyla po kolei: zalegle zdarzenie hooka zatrzymuje tez zapisy silnika po kroku, wiec
    # koniec doczytania widac po sprzatnieciu pliku ustawien, a nie po koncu runu
    async with zadanie_tla(sw.runner._run_workflow(sw.polecenie), "run z zawieszonym kokpitem"):
        poczatek = time.monotonic()
        await czekaj_na_warunek(
            lambda: sw.start_agentow() and sw.pliki_ustawien() == [], 20,
            "sprzatniecie pliku ustawien po limicie doczytania")

        assert time.monotonic() - poczatek < 15                    # limit doczytania to 1 s, nie 30 s
        assert kokpit.zawieszone >= 1                              # wysylka hooka naprawde stanela


async def test_anulowanie_runu_nie_czeka_na_kokpit_ktory_nie_odpowiada_na_zdarzenia_hookow(
    tmp_path, monkeypatch,
):
    kokpit = ZawieszonyKokpit()                                    # limit doczytania zostaje 30 s
    sw = przygotuj(tmp_path, monkeypatch, kokpit, "run-anulowany", spanie=120)

    async with zadanie_tla(sw.runner._run_workflow(sw.polecenie), "run z fanoutem") as zadanie:
        await czekaj_na_warunek(
            lambda: kokpit.zawieszone >= 1, 30, "wysylka zdarzenia hooka zawieszona")
        poczatek = time.monotonic()
        zadanie.cancel()
        zakonczone, _ = await asyncio.wait({zadanie}, timeout=20)

        assert zadanie in zakonczone
        assert time.monotonic() - poczatek < 15
    assert sw.pliki_ustawien() == []


class KokpitOdmawiajacyPierwszemuHookowi(Kokpit):
    """Strumien polecen jak u kokpitu; PIERWSZE zdarzenie hooka dostaje 401 (token odrzucony),
    kazde nastepne 200. Ponowienie tego zdarzenia z bufora przez silnik przechodzi, wiec odmowy
    nie zobaczy nikt poza zadaniem przekazywania hookow."""

    def __init__(self) -> None:
        super().__init__()
        self.odrzucone = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.path == "/api/events" and self.odrzucone == 0
                and json.loads(request.content)["kind"].startswith("hook:")):
            self.odrzucone += 1
            return httpx.Response(401, json={"detail": "SEKRET-ODPOWIEDZI-401"})
        return await super().handle_async_request(request)


async def test_odmowa_tokenu_na_zdarzenie_hooka_konczy_nasluch_odmowa_ze_sladem_bez_tresci_bledu(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(modul_klienta, "CZAS_DOCZYTANIA_HOOKOW_S", 60.0)   # tylko martwe zadanie skraca
    kokpit = KokpitOdmawiajacyPierwszemuHookowi()
    sw = przygotuj(tmp_path, monkeypatch, kokpit, "run-401")

    async with zadanie_tla(sw.runner.listen(), "nasluch runnera") as nasluch:
        kokpit.wyslij(sw.polecenie)
        await czekaj_na_warunek(nasluch.done, 25, "nasluch konczy sie odmowa tokenu")
        with pytest.raises(TokenOdrzucony):
            nasluch.result()

    assert kokpit.odrzucone == 1
    assert isinstance(sw.runner._token_odrzucony, TokenOdrzucony)
    linie = capsys.readouterr().err.splitlines()
    assert "hooki: przekazywanie kroku 'analiza' zakonczone bledem: TokenOdrzucony" in linie
    assert not any("SEKRET-ODPOWIEDZI-401" in linia for linia in linie)


async def test_blad_usuniecia_pliku_ustawien_zostawia_slad_z_nazwa_typu_a_krok_sie_konczy(
    tmp_path, monkeypatch, z_baza, capsys,
):
    prawdziwe = Path.unlink

    def unlink(self, *argumenty, **opcje):
        if self.name.endswith(".settings.json"):
            raise PermissionError(13, "SEKRET-BLEDU-USTAWIEN")
        return prawdziwe(self, *argumenty, **opcje)

    monkeypatch.setattr(Path, "unlink", unlink)
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    err = capsys.readouterr().err
    assert "hooki: plik ustawien kroku 'analiza' nie zostal usuniety: PermissionError" in err.splitlines()
    assert "SEKRET-BLEDU-USTAWIEN" not in err
    kinds = [e.kind for e in await wszystkie_zdarzenia()]
    assert kinds.count("run_finished") == 1
    assert "step_failed" not in kinds


async def test_blad_obciecia_pliku_hookow_zostawia_slad_z_nazwa_typu_a_krok_sie_konczy(
    tmp_path, monkeypatch, z_baza, capsys,
):
    prawdziwe = os.truncate

    def truncate(sciezka, dlugosc):
        if str(sciezka).endswith(".ndjson"):
            raise PermissionError(13, "SEKRET-BLEDU-OBCIECIA")
        return prawdziwe(sciezka, dlugosc)

    monkeypatch.setattr(os, "truncate", truncate)
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza)

    await sw.przebieg()

    err = capsys.readouterr().err
    assert "hooki: plik hookow kroku 'analiza' nie zostal obciety: PermissionError" in err.splitlines()
    assert "SEKRET-BLEDU-OBCIECIA" not in err
    kinds = [e.kind for e in await wszystkie_zdarzenia()]
    assert kinds.count("run_finished") == 1
    assert "step_failed" not in kinds


@pytest.mark.parametrize("kod", [0, 7], ids=["sukces", "porazka_agenta"])
async def test_po_kroku_nie_zostaje_zadne_zadanie_czytnika_ani_przekazywania_hookow(
    tmp_path, monkeypatch, z_baza, kod,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, kod=kod)

    await sw.przebieg()

    zostale = [
        getattr(t.get_coro(), "__qualname__", repr(t.get_coro()))
        for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert [n for n in zostale if "przekazuj" in n or "_petla" in n] == []
    assert sw.runner._hooki == {}


class OpoznionyKokpit(RejestrujacyKokpit):
    """Kazde zdarzenie trwa 50 ms; liczy najwieksza liczbe zdarzen wysylanych jednoczesnie."""

    def __init__(self) -> None:
        super().__init__()
        self.w_locie = 0
        self.maks_w_locie = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/events":
            return await super().handle_async_request(request)
        self.w_locie += 1
        self.maks_w_locie = max(self.maks_w_locie, self.w_locie)
        try:
            await asyncio.sleep(0.05)
            return await super().handle_async_request(request)
        finally:
            self.w_locie -= 1


async def test_zdarzenia_hookow_i_silnika_ida_do_kokpitu_po_jednym_bez_duplikatow(tmp_path, monkeypatch):
    kokpit = OpoznionyKokpit()
    # SubagentStop i wynik agenta powstaja w tej samej chwili, a kazdy zapis trwa 50 ms: wysylka
    # hookow (czytnik co 0,2 s) zachodzi na zapisy zdarzen silnika, gdyby nie wspolna blokada
    sw = przygotuj(tmp_path, monkeypatch, kokpit, "run-przeplot")

    await sw.przebieg()

    hooki = [z for z in kokpit.zdarzenia if z["kind"].startswith("hook:")]
    assert len(hooki) == 2 * LICZBA
    assert len([z for z in kokpit.zdarzenia if z["kind"] == "output"]) == LICZBA
    assert kokpit.maks_w_locie == 1
    ids = [z["event_id"] for z in kokpit.zdarzenia]
    assert len(set(ids)) == len(ids)


async def test_brak_programu_claude_konczy_krok_powodem_startu_i_jednym_run_finished_bez_tracebacku(
    tmp_path, monkeypatch, z_baza, capsys,
):
    sw = swiat_z_baza(
        tmp_path, monkeypatch, z_baza, kroki=[K(KROK)], z_claude=False, liczba=1)

    await sw.przebieg()

    zdarzenia = await wszystkie_zdarzenia()
    porazki = [e for e in zdarzenia if e.kind == "step_failed" and e.step_id == KROK]
    assert [e.text for e in porazki] == [POWOD_BRAKU_CLAUDE]
    assert [e.kind for e in zdarzenia].count("run_finished") == 1
    assert zdarzenia[-1].kind == "run_finished"
    assert sw.start_agentow() == []
    assert sw.pliki_ustawien() == []
    assert "Traceback" not in capsys.readouterr().err


POWOD_BRAKU_CLAUDE = "nie można uruchomić polecenia: [Errno 2] No such file or directory: 'claude'"


async def test_raport_porazki_kroku_bez_programu_claude_ma_powod_startu_i_kod_wyjscia_brak(
    tmp_path, monkeypatch, z_baza,
):
    # `analiza` dziala i usuwa program `claude`; `testuj` nie ma go juz na PATH (kod_wyjscia=None)
    # i wraca do `analiza` z raportem porazki
    kroki = [K(KROK), K("testuj", (KROK,), extra=f", on_fail: {{retry: {KROK}, max: 1}}")]
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, kroki=kroki, liczba=1, usun_claude=True)

    await sw.przebieg()

    # worktree runu lezy w katalogu o nazwie z run_id z polecenia (tu: id z bazy)
    raporty = sorted((sw.s.root / z_baza.run_id / "worktree").rglob("proba_*.md"))
    assert [r.name for r in raporty] == ["proba_1.md"]
    linie = raporty[0].read_text(encoding="utf-8").splitlines()
    assert "- krok: testuj" in linie
    assert "- kod wyjścia: brak" in linie
    assert f"- powód: {POWOD_BRAKU_CLAUDE}" in linie
    porazki = [e.text for e in await wszystkie_zdarzenia()
               if e.kind == "step_failed" and e.step_id == "testuj"]
    assert porazki[0] == POWOD_BRAKU_CLAUDE


async def test_wariant_konczacy_po_pozostalych_ma_swoj_koniec_subagenta_w_bazie_bo_hooki_trwaja_do_ostatniego(
    tmp_path, monkeypatch, z_baza,
):
    sw = swiat_z_baza(tmp_path, monkeypatch, z_baza, stop_ostatni=True)

    await sw.przebieg()

    wskazany = (sw.znaczniki / "wskazany").read_text()
    assert wskazany in agenci_z_znacznikow(sw)
    assert len(list(sw.znaczniki.glob("koniec_*.txt"))) == LICZBA
    pary = pary_hookow(await zdarzenia_hookow())
    assert ("hook:SubagentStop", wskazany) in pary
    assert pary == oczekiwane_pary(agenci_z_znacznikow(sw))

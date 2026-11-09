"""Odbior runu w widoku runu w przegladarce (HAR-121).

Run w stanie `to_accept` pokazuje sekcje odbioru: "Pokaz diff", "Integruj" z potwierdzeniem,
osobny przycisk "Commituj" dopiero po integracji oraz historie (kto, kiedy, SHA). Push nie
istnieje. Dwie grupy testow:

- scenariusze z prawdziwym runnerem (`python -m runner`), prawdziwym repozytorium gita
  w tmp_path i worktree runu pod `COCKPIT_RUNS_DIR`: skutki czytane z dysku i z gita,
  nie z samego interfejsu (asercja-na-danych);
- scenariusze galezi interfejsu (komunikaty, stany przejsciowe, nieczytelne wpisy) na
  podstawionych odpowiedziach (`page.route`), z wstrzymanym strumieniem SSE, zeby jedynym
  zrodlem zmiany widoku byl `odswiez()` po akcji.
"""
from __future__ import annotations

import asyncio
import faulthandler
import gc
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from procesy import grupa_istnieje, uruchom_proces_sync, zabij_grupe, zakoncz_proces_sync
from runner.worktree import utworz_worktree

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

KORZEN = Path(__file__).resolve().parents[2]    # katalog z pyproject.toml (cwd podprocesow)
LIMIT_WYNIKU_MS = 5000
LIMIT_STARTU_S = 20
TOZSAMOSC_REPO = ("Wlasciciel Repo", "wlasciciel@repo.example")
A_PO_RUNIE = "".join(
    "a.txt linia 5 zmieniona w runie\n" if i == 5 else f"a.txt linia {i}\n" for i in range(1, 11))
NOWY_PO_RUNIE = "nowy plik z runu\n"
STATUS_PO_INTEGRACJI = " M a.txt\n D b.txt\n?? nowy.txt\n"
KOMUNIKAT_SIEC = "Brak połączenia z kokpitem."
KOMUNIKAT_NIECZYTELNY = "Nie udało się odczytać szczegółów tego wpisu."
EMAIL_STUB = "ktos@example.com"
CZAS_STUB = "2026-03-05T14:07:09Z"
SHA_STUB = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Jezyk przegladarki en-US: format domyslny rozni sie od polskiego, wiec test formatu czasu moze oblac."""
    return {**browser_context_args, "locale": "en-US"}


LIMIT_TESTU_S = 300


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
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
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


@pytest.fixture(autouse=True)
def _git_bez_konfiguracji_maszyny(monkeypatch):
    """Config globalny i systemowy gita oraz tozsamosc ze srodowiska nie wplywaja na test ani na runnera."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for zmienna in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(zmienna, raising=False)


# =====================================================================================
# Pomocniki: baza, git, repozytorium uzytkownika
# =====================================================================================


def _w_bazie(zapytanie: str, parametry: dict | None = None, *, zapis: bool = False):
    """Wykonuje zapytanie na prawdziwej bazie (osobny watek i petla); odczyt zwraca jedna wartosc."""
    async def _wykonaj():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            if zapis:
                async with engine.begin() as conn:
                    await conn.execute(text(zapytanie), parametry or {})
                return None
            async with engine.connect() as conn:
                return (await conn.execute(text(zapytanie), parametry or {})).scalar_one()
        finally:
            await engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as pula:
        return pula.submit(lambda: asyncio.run(_wykonaj())).result(timeout=60)


def _git(repo: Path, *argumenty: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argumenty],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout


def _zapisz(plik: Path, tresc: str) -> None:
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_text(tresc, encoding="utf-8")


def _tresc_pliku(nazwa: str) -> str:
    return "".join(f"{nazwa} linia {i}\n" for i in range(1, 11))


def _migawka(repo: Path) -> dict:
    """Wszystko, co mogloby sie zmienic w repozytorium uzytkownika: HEAD, reflog, status, pliki."""
    return {
        "head": _git(repo, "rev-parse", "HEAD").strip(),
        "reflog": _git(repo, "reflog", "show", "--format=%gs", "HEAD").splitlines(),
        "status": _git(repo, "status", "--porcelain", "--untracked-files=all"),
        "pliki": {p.name: p.read_text(encoding="utf-8") for p in sorted(repo.iterdir()) if p.is_file()},
    }


@pytest.fixture
def repo_uzytkownika(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_uzytkownika"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", TOZSAMOSC_REPO[0])
    _git(repo, "config", "user.email", TOZSAMOSC_REPO[1])
    _git(repo, "config", "commit.gpgsign", "false")
    for nazwa in ("a.txt", "b.txt", "c.txt"):
        _zapisz(repo / nazwa, _tresc_pliku(nazwa))
    _git(repo, "add", "-A")
    # fixture testowy (nie silnik): commit startowy w repozytorium testowym w tmp_path
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def katalog_runow(tmp_path: Path) -> Path:
    return tmp_path / "runy"


def _zasiej_run_do_odbioru(repo: Path, katalog_runow: Path, run_id: str) -> SimpleNamespace:
    """Run `to_accept` z `base_commit` i worktree pod `katalog_runu(run_id)/worktree`, ze zmianami runu."""
    wt = utworz_worktree(repo, run_id, katalog_runow)
    sciezka = wt.sciezka
    _zapisz(sciezka / "a.txt", A_PO_RUNIE)
    (sciezka / "b.txt").unlink()
    _zapisz(sciezka / "nowy.txt", NOWY_PO_RUNIE)
    _w_bazie(
        "INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, status, created_at, "
        "base_commit) SELECT :id, tenant_id, id, 'local', 'szkielet', 'to_accept', now(), :base "
        "FROM runner LIMIT 1",
        {"id": run_id, "base": wt.base_commit}, zapis=True)
    return SimpleNamespace(id=run_id, repo=repo, worktree=sciezka, base_commit=wt.base_commit)


@pytest.fixture
def run_do_odbioru(token_runnera, repo_uzytkownika, katalog_runow) -> SimpleNamespace:
    return _zasiej_run_do_odbioru(repo_uzytkownika, katalog_runow, f"odbior-{uuid.uuid4().hex[:12]}")


@pytest.fixture
def runner_odbioru(adres_kokpitu, token_runnera, tmp_path, katalog_runow):
    """Produkcyjny `python -m runner` z katalogiem runow testu (`COCKPIT_RUNS_DIR`) i izolowanym gitem.

    Fixture `runner_online` z conftest nie ustawia `COCKPIT_RUNS_DIR`, wiec runner szukalby
    worktree w ~/.cockpit/runs. Token tylko w zmiennej srodowiskowej."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(KORZEN / "src"), *filter(None, [env.get("PYTHONPATH")])])
    env.update(
        COCKPIT_CONFIG=str(tmp_path / "credentials.json"),
        COCKPIT_RUNNER_TOKEN=token_runnera,
        COCKPIT_URL=adres_kokpitu,
        COCKPIT_RUNS_DIR=str(katalog_runow),
    )
    log = tmp_path / "runner-odbioru.log"
    with log.open("wb") as wyjscie:
        proc = uruchom_proces_sync(
            [sys.executable, "-m", "runner"], env=env, cwd=KORZEN,
            stdout=wyjscie, stderr=subprocess.STDOUT)
    try:
        koniec = time.monotonic() + LIMIT_STARTU_S
        while "strumien otwarty" not in log.read_text(errors="replace"):
            assert proc.poll() is None, f"runner zakonczyl prace: {log.read_text(errors='replace')[-2000:]}"
            assert time.monotonic() < koniec, (
                f"strumien runnera sie nie otworzyl: {log.read_text(errors='replace')[-2000:]}")
            time.sleep(0.1)
        yield proc
    finally:
        zakoncz_proces_sync(proc, nazwa=f"runner odbioru {proc.pid}", limit_term=10, limit_wait=10)
        zabij_grupe(proc.pid)    # start_new_session=True: pgid == pid
        koniec = time.monotonic() + 5
        while grupa_istnieje(proc.pid) and time.monotonic() < koniec:
            time.sleep(0.05)
        assert not grupa_istnieje(proc.pid), f"po tescie zostaly procesy grupy {proc.pid}"


# =====================================================================================
# Pomocniki: strona
# =====================================================================================


def _otworz_run(page, adres, run_id):
    page.goto(f"{adres}/?run={quote(run_id, safe='')}")
    expect(page.get_by_text(re.compile(r"status: \w+"))).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def _odbior(page):
    return page.get_by_test_id("odbior")


def _el(page, nazwa: str):
    return page.get_by_test_id(f"odbior-{nazwa}")


def _blad(page):
    return _odbior(page).get_by_role("alert")


def _wpisy(page):
    return _el(page, "historia-wpis")


def _czekaj_na(page, warunek, nazwa: str, limit_ms: int = 6000) -> None:
    """Czekanie z limitem na warunek spelniony przez handlery tras (wait_for_timeout pompuje zdarzenia)."""
    odliczanie = limit_ms
    while not warunek():
        assert odliczanie > 0, f"przekroczony limit {limit_ms} ms: {nazwa}"
        page.wait_for_timeout(50)
        odliczanie -= 50


def _wstrzymaj(page, wzorzec: str) -> list:
    """Zadania pasujace do wzorca zostaja bez odpowiedzi, az test wywola `route.fulfill`."""
    wstrzymane: list = []
    page.route(wzorzec, lambda r: wstrzymane.append(r))
    return wstrzymane


def _normalizuj(napis: str) -> str:
    return " ".join(napis.split())


def _czas_pl(page, at: str) -> str:
    return _normalizuj(page.evaluate("d => new Date(d).toLocaleString('pl-PL')", at))


def _czas_domyslny(page, at: str) -> str:
    return _normalizuj(page.evaluate("d => new Date(d).toLocaleString()", at))


def _kliknij_integruj_i_potwierdz(page):
    _el(page, "integruj").click()
    _el(page, "potwierdz-integracje").click()


# --- podstawione szczegoly runu -------------------------------------------------------


def _zdarzenie_stub(seq: int, kind: str, tekst: str = "", at: str = CZAS_STUB) -> dict:
    return {"seq": seq, "kind": kind, "text": tekst, "step_id": None, "agent_type": None,
            "text_truncated": False, "text_full_len": len(tekst), "at": at}


def _wpis(seq: int, kind: str, *, email: str = EMAIL_STUB, at: str = CZAS_STUB, **dodatkowe) -> dict:
    return _zdarzenie_stub(seq, kind, json.dumps({"user_id": "u-1", "email": email, **dodatkowe}), at)


class _Szczegoly:
    """Zmienny stan podstawianych szczegolow runu: handler POST zmienia `zdarzenia`, kolejny GET to widzi."""

    def __init__(self, run_id: str, status: str, zdarzenia: list[dict]) -> None:
        self.run_id = run_id
        self.status = status
        self.zdarzenia = zdarzenia

    def json(self) -> dict:
        return {"id": self.run_id, "workflow": "szkielet", "status": self.status,
                "runner_kind": "local", "reason": None, "events": self.zdarzenia, "steps": []}


def _podstaw_run(page, zdarzenia: list[dict] | None = None, status: str = "to_accept") -> _Szczegoly:
    """Run podstawiony w calosci: GET szczegolow z `page.route`, strumien SSE wstrzymany.

    Wstrzymany strumien nie otwiera sie, wiec `onopen` nie dociaga run po cichu i jedynym
    zrodlem zmiany widoku po akcji jest `odswiez()` z komponentu odbioru."""
    stan = _Szczegoly(str(uuid.uuid4()), status, list(zdarzenia or []))
    _wstrzymaj(page, "**/api/runs/*/stream")

    def szczegoly(r):
        if r.request.method == "GET":
            r.fulfill(json=stan.json())
        else:
            r.continue_()

    page.route(lambda url: url.split("?")[0].endswith(f"/api/runs/{stan.run_id}"), szczegoly)
    return stan


# =====================================================================================
# 1. Sekcja odbioru na runie do odbioru (prawdziwy run w bazie)
# =====================================================================================


def test_run_do_odbioru_pokazuje_sekcje_z_pokaz_diff_i_integruj_ale_bez_commituj_diffu_historii_i_bledu(
    zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona

    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    expect(page.get_by_role("region", name="Odbiór runu")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_odbior(page).get_by_role("heading", name="Odbiór", exact=True)).to_be_visible()
    expect(_el(page, "pokaz-diff")).to_have_text("Pokaż diff")
    expect(_el(page, "pokaz-diff")).to_be_enabled()
    expect(_el(page, "integruj")).to_have_text("Integruj")
    expect(_el(page, "integruj")).to_be_enabled()
    expect(_el(page, "commituj")).to_have_count(0)           # Commituj istnieje dopiero po integracji
    expect(_el(page, "diff")).to_have_count(0)
    expect(_el(page, "diff-pusty")).to_have_count(0)
    expect(_el(page, "pytanie-integracji")).to_have_count(0)
    expect(_blad(page)).to_have_count(0)
    expect(_el(page, "historia")).to_have_count(0)


@pytest.mark.parametrize("status", ["running", "failed"])
def test_run_w_innym_stanie_niz_do_odbioru_nie_pokazuje_sekcji_odbioru(
    zalogowana_strona, adres_kokpitu, run_do_odbioru, status
):
    page = zalogowana_strona
    _w_bazie("UPDATE run SET status = :s WHERE id = :id",
             {"s": status, "id": run_do_odbioru.id}, zapis=True)

    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    expect(page.get_by_text(f"status: {status}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_odbior(page)).to_have_count(0)
    expect(page.get_by_role("button", name="Integruj", exact=True)).to_have_count(0)


# =====================================================================================
# 2. Diff
# =====================================================================================


def test_pokaz_diff_wyswietla_zmiany_runu_i_nie_dotyka_repozytorium(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    przed = _migawka(run_do_odbioru.repo)
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    _el(page, "pokaz-diff").click()

    diff = _el(page, "diff")
    expect(diff).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(diff).to_contain_text("+a.txt linia 5 zmieniona w runie")
    expect(diff).to_contain_text("-b.txt linia 1")
    expect(diff).to_contain_text("+nowy plik z runu")
    expect(_el(page, "diff-pusty")).to_have_count(0)
    expect(_el(page, "pokaz-diff")).to_have_text("Pokaż diff")
    expect(_el(page, "pokaz-diff")).to_be_enabled()
    expect(_blad(page)).to_have_count(0)
    assert _migawka(run_do_odbioru.repo) == przed


def test_identyfikator_runu_ze_znakami_specjalnymi_jest_kodowany_w_adresie_odbioru(
    runner_odbioru, zalogowana_strona, adres_kokpitu, token_runnera, repo_uzytkownika, katalog_runow
):
    page = zalogowana_strona
    run_id = f"odbior #{uuid.uuid4().hex[:6]} x"      # '#' uciąłby adres, spacja wymaga kodowania
    _zasiej_run_do_odbioru(repo_uzytkownika, katalog_runow, run_id)
    _otworz_run(page, adres_kokpitu, run_id)

    _el(page, "pokaz-diff").click()

    expect(_el(page, "diff")).to_contain_text("+a.txt linia 5 zmieniona w runie", timeout=LIMIT_WYNIKU_MS)
    expect(_blad(page)).to_have_count(0)


# =====================================================================================
# 3. Integracja: potwierdzenie, skutek na dysku, anulowanie
# =====================================================================================


def test_integruj_pyta_o_potwierdzenie_i_do_potwierdzenia_nie_zmienia_repozytorium(
    zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    przed = _migawka(run_do_odbioru.repo)
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    _el(page, "integruj").click()

    expect(_el(page, "pytanie-integracji")).to_have_text(
        "Zmiany z runu trafią do kopii roboczej repozytorium. Kontynuować?", timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "potwierdz-integracje")).to_have_text("Potwierdź")
    expect(_el(page, "potwierdz-integracje")).to_be_enabled()
    expect(_el(page, "anuluj-integracje")).to_have_text("Anuluj")
    expect(_el(page, "anuluj-integracje")).to_be_enabled()
    expect(_el(page, "integruj")).to_have_count(0)
    assert _migawka(run_do_odbioru.repo) == przed


def test_anuluj_w_potwierdzeniu_wraca_do_integruj_bez_zadania_do_kokpitu_i_bez_zmian_w_repozytorium(
    zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    przed = _migawka(run_do_odbioru.repo)
    zadania_post: list[str] = []
    page.on("request", lambda r: zadania_post.append(r.url) if r.method == "POST" else None)
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)
    _el(page, "integruj").click()
    expect(_el(page, "anuluj-integracje")).to_be_visible(timeout=LIMIT_WYNIKU_MS)

    _el(page, "anuluj-integracje").click()

    expect(_el(page, "integruj")).to_be_visible()
    expect(_el(page, "pytanie-integracji")).to_have_count(0)
    expect(_el(page, "potwierdz-integracje")).to_have_count(0)
    expect(_el(page, "anuluj-integracje")).to_have_count(0)
    page.wait_for_timeout(300)
    assert [u for u in zadania_post if u.endswith("/integrate")] == []
    assert _migawka(run_do_odbioru.repo) == przed


def test_potwierdzona_integracja_przenosi_pliki_runu_na_dysk_bez_commita_i_bez_wpisu_w_reflogu(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    repo = run_do_odbioru.repo
    przed = _migawka(repo)
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    _kliknij_integruj_i_potwierdz(page)

    expect(_el(page, "commituj")).to_be_visible(timeout=LIMIT_WYNIKU_MS)    # integracja przyjeta
    po = _migawka(repo)
    assert po["pliki"]["a.txt"] == A_PO_RUNIE
    assert (repo / "nowy.txt").read_text(encoding="utf-8") == NOWY_PO_RUNIE
    assert not (repo / "b.txt").exists()
    assert po["status"] == STATUS_PO_INTEGRACJI          # zmiany w drzewie, nic w indeksie
    assert po["head"] == przed["head"]                    # bez commita
    assert po["reflog"] == przed["reflog"]                # bez nowego wpisu w reflogu
    expect(_el(page, "integruj")).to_have_count(0)
    expect(_el(page, "pytanie-integracji")).to_have_count(0)
    expect(_blad(page)).to_have_count(0)


def test_konflikt_z_lokalna_zmiana_pokazuje_komunikat_kokpitu_a_repozytorium_zostaje_nietkniete(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    repo = run_do_odbioru.repo
    linie = (repo / "a.txt").read_text(encoding="utf-8").splitlines(keepends=True)
    linie[4] = "a.txt linia 5 lokalna\n"
    (repo / "a.txt").write_text("".join(linie), encoding="utf-8")
    przed = _migawka(repo)
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    _kliknij_integruj_i_potwierdz(page)

    expect(_blad(page)).to_have_text(
        "Nie udało się zintegrować zmian: zmiany runu koliduja z lokalnymi zmianami: a.txt",
        timeout=LIMIT_WYNIKU_MS)
    assert _migawka(repo) == przed                        # drzewo bajtowo nietkniete
    assert not (repo / "nowy.txt").exists()
    expect(_el(page, "commituj")).to_have_count(0)
    expect(_el(page, "historia")).to_have_count(0)
    expect(_el(page, "potwierdz-integracje")).to_have_text("Potwierdź")
    expect(_el(page, "potwierdz-integracje")).to_be_enabled()


# =====================================================================================
# 4. Commit: osobny przycisk po integracji
# =====================================================================================


def _integruj_i_zaczekaj(page):
    _kliknij_integruj_i_potwierdz(page)
    expect(_el(page, "commituj")).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def test_commituj_dodaje_jeden_commit_z_plikami_runu_i_zostawia_czyste_drzewo(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    repo = run_do_odbioru.repo
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)
    _integruj_i_zaczekaj(page)
    przed = _migawka(repo)
    liczba_przed = int(_git(repo, "rev-list", "--count", "HEAD"))

    _el(page, "commituj").click()

    expect(_el(page, "commituj")).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    po = _migawka(repo)
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == liczba_przed + 1      # jeden nowy commit
    assert len(po["reflog"]) == len(przed["reflog"]) + 1
    assert po["head"] != przed["head"]
    assert po["status"] == ""                                # pliki runu w commicie, drzewo czyste
    assert _git(repo, "show", "-s", "--format=%s", "HEAD").strip() == f"cockpit: run {run_do_odbioru.id}"
    assert _git(repo, "show", "-s", "--format=%ae", "HEAD").strip() == TOZSAMOSC_REPO[1]
    zmienione = _git(repo, "show", "--name-status", "--format=", "HEAD").splitlines()
    assert {linia for linia in zmienione if linia} == {"M\ta.txt", "D\tb.txt", "A\tnowy.txt"}
    expect(_el(page, "integruj")).to_have_count(0)
    expect(_blad(page)).to_have_count(0)


def test_pelny_odbior_nie_zmienia_zdalnego_repozytorium(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru, tmp_path
):
    page = zalogowana_strona
    repo = run_do_odbioru.repo
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "fetch", "-q", "origin")

    def stan_zdalny() -> tuple[str, str, str]:
        return (_git(bare, "for-each-ref"), _git(repo, "for-each-ref", "refs/remotes"),
                _git(repo, "config", "--get-regexp", r"^remote\."))

    przed = stan_zdalny()
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)
    _integruj_i_zaczekaj(page)

    _el(page, "commituj").click()

    expect(_el(page, "commituj")).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    assert stan_zdalny() == przed


# =====================================================================================
# 5. Historia odbioru: kto, kiedy, SHA
# =====================================================================================


def _zdarzenia_runu(page, run_id: str) -> list[dict]:
    return page.evaluate(
        "async id => (await (await fetch(`/api/runs/${encodeURIComponent(id)}`)).json()).events",
        run_id)


def test_historia_odbioru_pokazuje_kto_i_kiedy_zintegrowal_i_zacommitowal_z_sha(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru, dane_wlasciciela
):
    page = zalogowana_strona
    email = dane_wlasciciela[0]
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)
    _integruj_i_zaczekaj(page)
    _el(page, "commituj").click()
    expect(_wpisy(page)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)

    sha = _git(run_do_odbioru.repo, "rev-parse", "HEAD").strip()
    zdarzenia = {z["kind"]: z for z in _zdarzenia_runu(page, run_do_odbioru.id)}
    at_integracji, at_commita = zdarzenia["integrated"]["at"], zdarzenia["committed"]["at"]
    assert at_integracji and at_commita                     # czas zapisany w danych, nie wymyslony przez UI
    expect(page.get_by_role("list", name="Historia odbioru")).to_be_visible()
    expect(_wpisy(page).nth(0)).to_have_attribute("data-kind", "integrated")
    expect(_wpisy(page).nth(1)).to_have_attribute("data-kind", "committed")
    expect(_wpisy(page).nth(0)).to_have_text(
        f"Zintegrowano · {email} · {_czas_pl(page, at_integracji)}")
    expect(_wpisy(page).nth(1)).to_have_text(
        f"Zacommitowano · {email}, {sha} · {_czas_pl(page, at_commita)}")
    expect(_wpisy(page).nth(0).locator("time")).to_have_attribute("datetime", at_integracji)
    expect(_wpisy(page).nth(1).locator("time")).to_have_attribute("datetime", at_commita)


def test_historia_odbioru_przetrwa_ponowne_otwarcie_runu_a_przyciski_akcji_nie_wracaja(
    runner_odbioru, zalogowana_strona, adres_kokpitu, run_do_odbioru
):
    page = zalogowana_strona
    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)
    _integruj_i_zaczekaj(page)
    _el(page, "commituj").click()
    expect(_wpisy(page)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)

    _otworz_run(page, adres_kokpitu, run_do_odbioru.id)

    expect(_wpisy(page)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "integruj")).to_have_count(0)
    expect(_el(page, "commituj")).to_have_count(0)


# =====================================================================================
# 6. Push nie istnieje
# =====================================================================================


def _bez_push(page) -> None:
    wzorzec = re.compile(r"push|wypchn", re.IGNORECASE)
    expect(_odbior(page).get_by_role("button")).not_to_have_count(0)    # sprawdzamy, nie na pustej sekcji
    expect(page.get_by_role("button", name=wzorzec)).to_have_count(0)
    expect(_odbior(page).get_by_text(wzorzec)).to_have_count(0)
    expect(page.locator("[data-testid*='push' i]")).to_have_count(0)


def test_na_stronie_odbioru_nie_ma_zadnej_akcji_push_na_zadnym_etapie(zalogowana_strona, adres_kokpitu):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    page.route("**/api/runs/*/integrate", lambda r: (
        stan.zdarzenia.append(_wpis(1, "integrated")), r.fulfill(json={"run_id": stan.run_id})))
    page.route("**/api/runs/*/commit", lambda r: (
        stan.zdarzenia.append(_wpis(2, "committed", sha=SHA_STUB)), r.fulfill(json={"run_id": stan.run_id})))
    _otworz_run(page, adres_kokpitu, stan.run_id)
    _bez_push(page)                                             # przed integracja

    _el(page, "integruj").click()
    expect(_el(page, "potwierdz-integracje")).to_be_visible()
    _bez_push(page)                                             # w potwierdzeniu

    _el(page, "potwierdz-integracje").click()
    expect(_el(page, "commituj")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    _bez_push(page)                                             # po integracji

    _el(page, "commituj").click()
    expect(_wpisy(page)).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "commituj")).to_have_count(0)
    _bez_push(page)                                             # po commicie


# =====================================================================================
# 7. Diff: komunikaty i stan przejsciowy (podstawione odpowiedzi)
# =====================================================================================


@pytest.mark.parametrize("tresc", [
    pytest.param('{"diff": ""}', id="pusty_napis"),
    pytest.param("{}", id="bez_pola_diff"),
    pytest.param('{"diff": 5}', id="diff_nie_jest_napisem"),
])
def test_diff_bez_zmian_pokazuje_komunikat_brak_zmian_do_pokazania(zalogowana_strona, adres_kokpitu, tresc):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    page.route("**/api/runs/*/diff", lambda r: r.fulfill(status=200, body=tresc))
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _el(page, "pokaz-diff").click()

    expect(_el(page, "diff-pusty")).to_have_text("Brak zmian do pokazania.", timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "diff")).to_have_count(0)


ODPOWIEDZI_BLEDU = {
    "detail_napisem": (409, '{"detail": "run nie jest do odbioru"}',
                       "Nie udało się pobrać diffu: run nie jest do odbioru"),
    "cialo_nie_json": (500, "to nie jest json", "Nie udało się pobrać diffu: błąd 500"),
    "detail_nie_jest_napisem": (422, '{"detail": [{"msg": "zle"}]}', "Nie udało się pobrać diffu: błąd 422"),
    "detail_pusty": (500, '{"detail": ""}', "Nie udało się pobrać diffu: błąd 500"),
    "json_bez_detail": (404, "{}", "Nie udało się pobrać diffu: błąd 404"),
    "json_null": (502, "null", "Nie udało się pobrać diffu: błąd 502"),
}


@pytest.mark.parametrize(("kod", "cialo", "oczekiwany"), list(ODPOWIEDZI_BLEDU.values()),
                         ids=list(ODPOWIEDZI_BLEDU))
def test_odmowa_pobrania_diffu_pokazuje_dokladny_komunikat_z_detail_albo_kodem_http(
    zalogowana_strona, adres_kokpitu, kod, cialo, oczekiwany
):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    page.route("**/api/runs/*/diff", lambda r: r.fulfill(status=kod, body=cialo))
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _el(page, "pokaz-diff").click()

    expect(_blad(page)).to_have_text(oczekiwany, timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "diff")).to_have_count(0)
    expect(_el(page, "pokaz-diff")).to_be_enabled()


AKCJE_ZERWANE = {
    "diff": ("diff", [], "pokaz-diff", "Nie udało się pobrać diffu"),
    "integracja": ("integrate", [], "integruj", "Nie udało się zintegrować zmian"),
    "commit": ("commit", [("integrated", {})], "commituj", "Nie udało się zacommitować zmian"),
}


@pytest.mark.parametrize(("sciezka", "wpisy", "przycisk", "poczatek"), list(AKCJE_ZERWANE.values()),
                         ids=list(AKCJE_ZERWANE))
def test_zerwane_polaczenie_przy_akcji_odbioru_pokazuje_dokladny_komunikat_z_brakiem_polaczenia(
    zalogowana_strona, adres_kokpitu, sciezka, wpisy, przycisk, poczatek
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_wpis(i, kind) for i, (kind, _) in enumerate(wpisy, start=1)])
    page.route(f"**/api/runs/*/{sciezka}", lambda r: r.abort())
    _otworz_run(page, adres_kokpitu, stan.run_id)
    _el(page, przycisk).click()
    if przycisk == "integruj":
        _el(page, "potwierdz-integracje").click()

    expect(_blad(page)).to_have_text(f"{poczatek}: {KOMUNIKAT_SIEC}", timeout=LIMIT_WYNIKU_MS)


def test_w_trakcie_pobierania_diffu_przycisk_jest_wylaczony_a_po_odpowiedzi_znowu_aktywny_i_pokazuje_diff(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    wstrzymane = _wstrzymaj(page, "**/api/runs/*/diff")
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _el(page, "pokaz-diff").click()

    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie diffu w locie")
    expect(_el(page, "pokaz-diff")).to_be_disabled()
    expect(_el(page, "pokaz-diff")).to_have_text("Pobieram diff…")
    expect(_el(page, "diff")).to_have_count(0)
    wstrzymane[0].fulfill(json={"diff": "diff --git a/x b/x\n+x\n"})
    expect(_el(page, "diff")).to_contain_text("+x", timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "pokaz-diff")).to_be_enabled()
    expect(_el(page, "pokaz-diff")).to_have_text("Pokaż diff")


def test_komunikat_o_bledzie_diffu_znika_na_poczatku_kolejnego_pobierania_a_nie_dopiero_z_odpowiedzia(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    zadania: list = []

    def obsluz(r):
        zadania.append(r)
        if len(zadania) == 1:
            r.fulfill(status=500, body='{"detail": "pierwszy"}')

    page.route("**/api/runs/*/diff", obsluz)
    _otworz_run(page, adres_kokpitu, stan.run_id)
    _el(page, "pokaz-diff").click()
    expect(_blad(page)).to_have_text("Nie udało się pobrać diffu: pierwszy", timeout=LIMIT_WYNIKU_MS)

    _el(page, "pokaz-diff").click()

    _czekaj_na(page, lambda: len(zadania) == 2, "drugie zadanie diffu")
    expect(_blad(page)).to_have_count(0)                      # druga proba jeszcze bez odpowiedzi
    zadania[1].fulfill(status=500, body='{"detail": "drugi"}')
    expect(_blad(page)).to_have_text("Nie udało się pobrać diffu: drugi", timeout=LIMIT_WYNIKU_MS)


# =====================================================================================
# 8. Integracja i commit: stany przejsciowe, komunikaty, odswiezenie (podstawione odpowiedzi)
# =====================================================================================


def test_komunikat_o_bledzie_integracji_znika_na_poczatku_kolejnej_proby_a_nie_dopiero_z_odpowiedzia(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    zadania: list = []

    def obsluz(r):
        zadania.append(r)
        if len(zadania) == 1:
            r.fulfill(status=409, body='{"detail": "pierwszy"}')

    page.route("**/api/runs/*/integrate", obsluz)
    _otworz_run(page, adres_kokpitu, stan.run_id)
    _kliknij_integruj_i_potwierdz(page)
    expect(_blad(page)).to_have_text("Nie udało się zintegrować zmian: pierwszy", timeout=LIMIT_WYNIKU_MS)

    _el(page, "potwierdz-integracje").click()

    _czekaj_na(page, lambda: len(zadania) == 2, "druga proba integracji")
    expect(_blad(page)).to_have_count(0)
    zadania[1].fulfill(status=409, body='{"detail": "drugi"}')
    expect(_blad(page)).to_have_text("Nie udało się zintegrować zmian: drugi", timeout=LIMIT_WYNIKU_MS)


def test_w_trakcie_integracji_potwierdz_i_anuluj_sa_wylaczone_a_po_odmowie_znowu_aktywne(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    wstrzymane = _wstrzymaj(page, "**/api/runs/*/integrate")
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _kliknij_integruj_i_potwierdz(page)

    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie integracji w locie")
    expect(_el(page, "potwierdz-integracje")).to_be_disabled()
    expect(_el(page, "potwierdz-integracje")).to_have_text("Integruję…")
    expect(_el(page, "anuluj-integracje")).to_be_disabled()
    expect(_blad(page)).to_have_count(0)
    wstrzymane[0].fulfill(status=500, body='{"detail": "x"}')
    expect(_blad(page)).to_have_text("Nie udało się zintegrować zmian: x", timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "potwierdz-integracje")).to_be_enabled()
    expect(_el(page, "potwierdz-integracje")).to_have_text("Potwierdź")
    expect(_el(page, "anuluj-integracje")).to_be_enabled()
    expect(_el(page, "pytanie-integracji")).to_be_visible()     # po odmowie potwierdzenie zostaje


def test_w_trakcie_commita_przycisk_jest_wylaczony_a_po_odmowie_znowu_aktywny(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_wpis(1, "integrated")])
    wstrzymane = _wstrzymaj(page, "**/api/runs/*/commit")
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _el(page, "commituj").click()

    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie commita w locie")
    expect(_el(page, "commituj")).to_be_disabled()
    expect(_el(page, "commituj")).to_have_text("Commituję…")
    wstrzymane[0].fulfill(status=409, body='{"detail": "y"}')
    expect(_blad(page)).to_have_text("Nie udało się zacommitować zmian: y", timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "commituj")).to_be_enabled()
    expect(_el(page, "commituj")).to_have_text("Commituj")


def test_udana_odpowiedz_bez_tresci_json_zamyka_potwierdzenie_bez_komunikatu_o_bledzie(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)
    page.route("**/api/runs/*/integrate", lambda r: r.fulfill(status=200, body="to nie jest json"))
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _kliknij_integruj_i_potwierdz(page)

    expect(_el(page, "pytanie-integracji")).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "integruj")).to_be_visible()                # brak zdarzenia w odswiezonym runie
    expect(_blad(page)).to_have_count(0)


def test_po_udanej_integracji_widok_pobiera_run_ponownie_i_pokazuje_commituj(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)

    def integrate(r):
        stan.zdarzenia.append(_wpis(1, "integrated"))
        r.fulfill(json={"run_id": stan.run_id, "at": CZAS_STUB, "pliki": ["a.txt"]})

    page.route("**/api/runs/*/integrate", integrate)
    _otworz_run(page, adres_kokpitu, stan.run_id)
    expect(_el(page, "commituj")).to_have_count(0)

    _kliknij_integruj_i_potwierdz(page)

    expect(_el(page, "commituj")).to_be_visible(timeout=LIMIT_WYNIKU_MS)    # tylko ponowne pobranie runu
    expect(_el(page, "integruj")).to_have_count(0)
    expect(_wpisy(page)).to_have_count(1)


def test_po_odmowie_409_widok_pokazuje_prawdziwy_stan_z_ponownego_pobrania_runu(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page)

    def integrate(r):
        # ktos zintegrowal run w miedzyczasie: kokpit odmawia, a prawda jest w runie
        stan.zdarzenia.append(_wpis(1, "integrated"))
        r.fulfill(status=409, body='{"detail": "zmiany runu zostaly juz zintegrowane"}')

    page.route("**/api/runs/*/integrate", integrate)
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _kliknij_integruj_i_potwierdz(page)

    expect(_blad(page)).to_have_text(
        "Nie udało się zintegrować zmian: zmiany runu zostaly juz zintegrowane", timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "commituj")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_el(page, "integruj")).to_have_count(0)


def test_po_udanym_commicie_widok_pobiera_run_ponownie_i_chowa_commituj(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_wpis(1, "integrated")])

    def commit(r):
        stan.zdarzenia.append(_wpis(2, "committed", sha=SHA_STUB))
        r.fulfill(json={"run_id": stan.run_id, "at": CZAS_STUB, "sha": SHA_STUB})

    page.route("**/api/runs/*/commit", commit)
    _otworz_run(page, adres_kokpitu, stan.run_id)

    _el(page, "commituj").click()

    expect(_el(page, "commituj")).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    expect(_wpisy(page)).to_have_count(2)


def test_odpowiedz_na_integracje_po_przejsciu_na_inny_run_nie_odswieza_nowego_runu(
    runner_odbioru, zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan_a = _podstaw_run(page)
    wstrzymane = _wstrzymaj(page, "**/api/runs/*/integrate")
    _otworz_run(page, adres_kokpitu, stan_a.run_id)
    _kliknij_integruj_i_potwierdz(page)
    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie integracji runu A w locie")
    run_b = str(uuid.uuid4())
    pobrania_b: list = []

    def szczegoly_b(r):
        # pierwsze pobranie: running; kazde kolejne: failed - widac, czy ktos dociagnal run B ponownie
        pobrania_b.append(r)
        status = "running" if len(pobrania_b) == 1 else "failed"
        r.fulfill(json=_Szczegoly(run_b, status, []).json())

    page.route(f"**/api/runs/{run_b}", szczegoly_b)
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": run_b})
               if r.request.method == "POST" else r.continue_())
    uruchom = page.get_by_role("button", name="Uruchom", exact=True)
    expect(uruchom).to_be_enabled(timeout=2 * LIMIT_WYNIKU_MS)
    uruchom.click()
    expect(page.get_by_text(f"run {run_b}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("status: running")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_odbior(page)).to_have_count(0)

    wstrzymane[0].fulfill(json={"run_id": stan_a.run_id, "at": CZAS_STUB, "pliki": ["a.txt"]})

    page.wait_for_timeout(500)
    expect(page.get_by_text("status: failed")).to_have_count(0)
    expect(page.get_by_text("status: running")).to_be_visible()
    assert len(pobrania_b) == 1


# =====================================================================================
# 9. Wpisy historii: nieczytelny tekst, SHA, czas
# =====================================================================================

NIECZYTELNE_WPISY = {
    "tekst_nie_jest_json": "to nie jest json",
    "json_null": "null",
    "email_nie_jest_napisem": '{"user_id": "u-1", "email": 5}',
    "email_pusty": '{"user_id": "u-1", "email": ""}',
    "bez_pola_email": '{"user_id": "u-1"}',
}


@pytest.mark.parametrize("tekst", list(NIECZYTELNE_WPISY.values()), ids=list(NIECZYTELNE_WPISY))
def test_wpis_historii_z_nieczytelnym_tekstem_pokazuje_komunikat_zamiast_pustego_wiersza(
    zalogowana_strona, adres_kokpitu, tekst
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_zdarzenie_stub(1, "integrated", tekst)])

    _otworz_run(page, adres_kokpitu, stan.run_id)

    expect(_wpisy(page)).to_have_text(
        f"Zintegrowano · {KOMUNIKAT_NIECZYTELNY} · {_czas_pl(page, CZAS_STUB)}", timeout=LIMIT_WYNIKU_MS)


WPISY_Z_SHA = {
    "commit_z_sha": ("committed", {"sha": SHA_STUB}, f"Zacommitowano · {EMAIL_STUB}, {SHA_STUB}"),
    "commit_bez_sha": ("committed", {}, f"Zacommitowano · {EMAIL_STUB}"),
    "commit_z_pustym_sha": ("committed", {"sha": ""}, f"Zacommitowano · {EMAIL_STUB}"),
    "commit_z_sha_nie_napisem": ("committed", {"sha": 5}, f"Zacommitowano · {EMAIL_STUB}"),
    "integracja_z_sha_w_tekscie": ("integrated", {"sha": SHA_STUB}, f"Zintegrowano · {EMAIL_STUB}"),
}


@pytest.mark.parametrize(("kind", "dodatkowe", "poczatek"), list(WPISY_Z_SHA.values()),
                         ids=list(WPISY_Z_SHA))
def test_wpis_historii_pokazuje_sha_tylko_dla_commita_i_tylko_gdy_jest_niepustym_napisem(
    zalogowana_strona, adres_kokpitu, kind, dodatkowe, poczatek
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_wpis(1, kind, **dodatkowe)])

    _otworz_run(page, adres_kokpitu, stan.run_id)

    expect(_wpisy(page)).to_have_text(
        f"{poczatek} · {_czas_pl(page, CZAS_STUB)}", timeout=LIMIT_WYNIKU_MS)


def test_czas_wpisu_historii_jest_w_formacie_polskim_a_nie_w_domyslnym_formacie_przegladarki(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_wpis(1, "integrated")])
    polski, domyslny = _czas_pl(page, CZAS_STUB), _czas_domyslny(page, CZAS_STUB)
    assert polski != domyslny, "test nie odroznia formatow: zmien jezyk przegladarki testu"

    _otworz_run(page, adres_kokpitu, stan.run_id)

    expect(_wpisy(page)).to_have_text(f"Zintegrowano · {EMAIL_STUB} · {polski}", timeout=LIMIT_WYNIKU_MS)
    expect(_wpisy(page).locator("time")).to_have_attribute("datetime", CZAS_STUB)


def test_nieparsowalny_czas_wpisu_historii_zostaje_surowym_napisem_a_nie_invalid_date(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [_wpis(1, "integrated", at="nie-data")])

    _otworz_run(page, adres_kokpitu, stan.run_id)

    expect(_wpisy(page)).to_have_text(f"Zintegrowano · {EMAIL_STUB} · nie-data", timeout=LIMIT_WYNIKU_MS)
    expect(_wpisy(page).locator("time")).to_have_attribute("datetime", "nie-data")


def test_historia_odbioru_pokazuje_tylko_wpisy_odbioru_a_nie_inne_zdarzenia_runu(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    stan = _podstaw_run(page, [
        _zdarzenie_stub(1, "run_finished", "koniec"),
        _wpis(2, "integrated"),
        _zdarzenie_stub(3, "step_finished", "krok"),
    ])

    _otworz_run(page, adres_kokpitu, stan.run_id)

    expect(_wpisy(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_wpisy(page)).to_have_attribute("data-kind", "integrated")

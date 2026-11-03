"""Runner offline to tryb tylko do odczytu, katalog definicji z cache w przegladarce (HAR-77).

Katalog leży w bazie (zasiew wierszy `definicja`), wiec lista jest widoczna bez runnera.
Kokpit i runner to procesy z fixture'ow (grupa procesow, sprzatanie, limity czasu).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from procesy import uruchom_proces_sync, zabij_grupe, zakoncz_proces_sync

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

KORZEN = Path(__file__).resolve().parents[2]
LIMIT_STARTU_S = 20
# WHY: dwa cykle pollingu stanu runnera po 5 s plus zapas na wolna odpowiedz pod obciazeniem
# CI i rownoleglych sesji (7 s = cykl + 2 s zapasu oblewalo, gdy zadna sonda nie doszla).
LIMIT_SONDY_MS = 12_000
LIMIT_ONLINE_MS = 10_000    # kryterium: po starcie runnera przyciski wracaja w 10 s
LIMIT_UI_MS = 5_000
OKNO_NA_POLLING_MS = 1_000  # czas na ewentualny stan offline, ktory blednie pojawilby sie po sondzie

TEKST_PASKA = "Runner offline — tryb tylko do odczytu"
TEKST_PODPOWIEDZI = "Wymaga runnera online. Uruchom runnera na swojej maszynie."
TEKST_PUSTEGO = "Katalog jeszcze nie odświeżony"
DATA_ZASIEWU = datetime(2026, 10, 30, 12, 5, tzinfo=timezone.utc)
NAPIS_DATY_ZASIEWU = "stan z 2026-10-30 12:05 UTC"
KOLOR_INFORMACJI = "rgb(88, 97, 109)"   # #58616D
TEKST_BLEDU_ODSWIEZENIA = "Nie udało się odświeżyć katalogu."
TEKST_BLEDU_POBRANIA = "Nie udało się pobrać katalogu."

# Wiersze zasiewu: (rodzaj, klucz, nazwa, rola); workflow nie ma roli.
ZASIEW = [
    ("skill", "autor-zasiewu", "Autor zasiewu", "author"),
    ("skill", "recenzent-zasiewu", "Recenzent zasiewu", "reviewer"),
    ("workflow", "przeplyw-zasiewu", "przeplyw-zasiewu", None),
]
NAZWY_Z_DYSKU = {"Autor z dysku", "przeplyw-z-dysku"}


@pytest.fixture(scope="module")
def browser_context_args(browser_context_args):
    """Przegladarka w strefie +13 h: data 'stan z' liczona w strefie przegladarki rozni sie od UTC."""
    return {**browser_context_args, "timezone_id": "Pacific/Auckland"}


# --------------------------------------------------------------------------- baza

def _w_osobnym_watku(koro):
    # sync API playwrighta trzyma petle zdarzen w watku testu: osobny watek i petla
    with ThreadPoolExecutor(max_workers=1) as pula:
        return pula.submit(lambda: asyncio.run(koro())).result()


def _zapytaj_baze(zapytanie: str, parametry: dict | None = None) -> list[tuple]:
    async def _pytaj():
        engine = create_async_engine(os.environ["COCKPIT_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                wynik = await conn.execute(text(zapytanie), parametry or {})
                return [tuple(w) for w in wynik.fetchall()] if wynik.returns_rows else []
        finally:
            await engine.dispose()

    return _w_osobnym_watku(_pytaj)


def _zasiej_katalog() -> None:
    (tenant_id,) = _zapytaj_baze("SELECT id FROM tenant")[0]
    (runner_id,) = _zapytaj_baze("SELECT id FROM runner WHERE tenant_id = :t", {"t": tenant_id})[0]
    for rodzaj, klucz, nazwa, rola in ZASIEW:
        _zapytaj_baze(
            "INSERT INTO definicja (id, tenant_id, runner_id, projekt, rodzaj, klucz, nazwa, rola,"
            " sciezka, odswiezono_at) VALUES (:id, :t, :r, :projekt, :rodzaj, :klucz, :nazwa,"
            " :rola, :sciezka, :at)",
            {"id": str(uuid.uuid4()), "t": tenant_id, "r": runner_id,
             "projekt": "/zasiew/projekt", "rodzaj": rodzaj, "klucz": klucz, "nazwa": nazwa,
             "rola": rola, "sciezka": f".cockpit/{klucz}", "at": DATA_ZASIEWU},
        )


@pytest.fixture
def katalog_zasiany(token_runnera):
    """Katalog w bazie z ustalona data; runner (jeden zespol, jeden runner) wciaz nie dziala."""
    _zasiej_katalog()


# ------------------------------------------------------------ runner z konfiguracja

def _srodowisko_runnera(tmp_path: Path, **dodatkowe: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(KORZEN / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    env["COCKPIT_CONFIG"] = str(tmp_path / "credentials.json")
    env.update(dodatkowe)
    return env


def _pisz(sciezka: Path, tresc: str) -> None:
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(tresc, encoding="utf-8")


@pytest.fixture
def runner_z_katalogiem_na_dysku(adres_kokpitu, token_runnera, tmp_path):
    """Runner online, ktorego dysk ma INNY katalog niz zasiew: jeden skill i jeden workflow."""
    projekt = tmp_path / "projekt-na-dysku"
    dom = tmp_path / "dom-uzytkownika"
    dom.mkdir()
    _pisz(projekt / ".cockpit" / "skills" / "dysk-autor" / "SKILL.md",
          "---\nid: dysk-autor\nname: Autor z dysku\nrole: author\n---\nBrief.\n")
    _pisz(projekt / ".cockpit" / "workflows" / "przeplyw-z-dysku.yaml", "name: przeplyw\n")
    konfiguracja = tmp_path / "runner.json"
    _pisz(konfiguracja, json.dumps({"projekty": [str(projekt)]}))
    log = tmp_path / "runner-z-katalogiem.log"
    with log.open("wb") as wyjscie:
        proc = uruchom_proces_sync(
            [sys.executable, "-m", "runner"],
            env=_srodowisko_runnera(
                tmp_path, COCKPIT_RUNNER_TOKEN=token_runnera, COCKPIT_URL=adres_kokpitu,
                COCKPIT_RUNNER_CONFIG=str(konfiguracja), HOME=str(dom)),
            cwd=KORZEN, stdout=wyjscie, stderr=subprocess.STDOUT,
        )
    try:
        koniec = time.monotonic() + LIMIT_STARTU_S
        while "strumien otwarty" not in log.read_text(errors="replace"):
            assert proc.poll() is None, f"runner zakonczyl prace: {log.read_text(errors='replace')[-2000:]}"
            assert time.monotonic() < koniec, "strumien runnera sie nie otworzyl"
            time.sleep(0.1)
        yield proc
    finally:
        pgid = proc.pid   # start_new_session=True: lider sesji ma pgid == pid
        zakoncz_proces_sync(proc, nazwa=f"runner z katalogiem {proc.pid}",
                            limit_term=10, limit_wait=10)
        zabij_grupe(pgid)


# ------------------------------------------------------------------- pomocnicze

def _sekcja_definicji(page):
    return page.get_by_role("region", name="Definicje")


def _przycisk_odswiez(page):
    return _sekcja_definicji(page).get_by_role("button", name="Odśwież", exact=True)


def _bez_opisu_offline(page) -> None:
    """Poza stanem offline przyciski nie niosa podpowiedzi (gałąź `: {}` w `opisOffline`).

    Na mutancie z atrybutami zawsze `title` rowna sie podpowiedzi i to z niego bierze sie opis."""
    for przycisk in (_przycisk_odswiez(page), _przycisk_uruchom(page)):
        expect(przycisk).not_to_have_attribute("title", TEKST_PODPOWIEDZI)
        expect(przycisk).not_to_have_attribute("aria-describedby", "podpowiedz-runner-offline")
        expect(przycisk).not_to_have_accessible_description(TEKST_PODPOWIEDZI)


def _przycisk_uruchom(page):
    return page.get_by_role("button", name="Uruchom", exact=True)


def _pasek(page):
    return page.get_by_role("status").filter(has_text=TEKST_PASKA)


def _poczekaj_na_tryb_offline(page) -> None:
    expect(_pasek(page)).to_be_visible(timeout=LIMIT_SONDY_MS)


def _wiersz(page, nazwa: str):
    return _sekcja_definicji(page).get_by_role("row").filter(has_text=nazwa)


# ------------------------------------------------------------- scenariusz offline

def test_runner_offline_pokazuje_pasek_trybu_tylko_do_odczytu_jako_informacje(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona

    # role=status i dokladny tekst; blad (role=alert) to inny stan niz informacja
    expect(_pasek(page)).to_have_text(TEKST_PASKA, timeout=LIMIT_SONDY_MS)
    expect(page.get_by_role("alert")).to_have_count(0)
    # kolor informacyjny z kontraktu, nie kolor bledu #A5201A = rgb(165, 32, 26)
    expect(_pasek(page)).to_have_css("color", KOLOR_INFORMACJI)


def test_runner_offline_pokazuje_zasiany_katalog_z_rodzajem_nazwa_i_rola(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona

    _poczekaj_na_tryb_offline(page)

    for rodzaj, _klucz, nazwa, rola in ZASIEW:
        # rola workflow to pauza, nie pusta komorka i nie "null"
        expect(_wiersz(page, nazwa).get_by_role("cell")).to_have_text(
            [rodzaj, nazwa, rola or "—"], timeout=LIMIT_UI_MS
        )
    expect(_sekcja_definicji(page).get_by_role("columnheader")).to_have_text(
        ["Rodzaj", "Nazwa", "Rola"]
    )


def test_runner_offline_pokazuje_date_stanu_katalogu_w_utc_niezaleznie_od_strefy_przegladarki(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona

    _poczekaj_na_tryb_offline(page)

    # przegladarka jest w strefie +13 h: lokalnie bylaby to 2026-10-31 01:05
    expect(_sekcja_definicji(page).get_by_text(NAPIS_DATY_ZASIEWU)).to_be_visible(
        timeout=LIMIT_UI_MS
    )


def test_runner_offline_wylacza_odswiez_i_uruchom_z_wyjasnieniem(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona

    _poczekaj_na_tryb_offline(page)

    expect(page.locator("#podpowiedz-runner-offline")).to_have_text(TEKST_PODPOWIEDZI)
    for przycisk in (_przycisk_odswiez(page), _przycisk_uruchom(page)):
        expect(przycisk).to_be_disabled()
        expect(przycisk).to_have_accessible_description(TEKST_PODPOWIEDZI)
        expect(przycisk).to_have_attribute("title", TEKST_PODPOWIEDZI)


def test_zatrzymany_runner_po_pierwszym_pobraniu_przelacza_widok_w_tryb_tylko_do_odczytu(
    katalog_zasiany, zatrzymaj_runnera, zalogowana_strona
):
    page = zalogowana_strona
    zatrzymaj_runnera()

    # przejscie online -> offline po rozlaczeniu: pasek pojawia sie bez przeladowania strony
    expect(_pasek(page)).to_be_visible(timeout=LIMIT_SONDY_MS)
    expect(_przycisk_uruchom(page)).to_be_disabled()
    expect(_przycisk_odswiez(page)).to_be_disabled()


def test_po_starcie_runnera_przyciski_wracaja_a_pasek_i_podpowiedz_znikaja(
    katalog_zasiany, zalogowana_strona, request
):
    page = zalogowana_strona
    _poczekaj_na_tryb_offline(page)
    expect(_przycisk_odswiez(page)).to_be_disabled()
    expect(_przycisk_uruchom(page)).to_be_disabled()

    request.getfixturevalue("runner_online")   # wraca, gdy strumien runnera jest otwarty

    expect(_przycisk_odswiez(page)).to_be_enabled(timeout=LIMIT_ONLINE_MS)
    expect(_przycisk_uruchom(page)).to_be_enabled(timeout=LIMIT_ONLINE_MS)
    expect(_pasek(page)).to_have_count(0)
    expect(page.locator("#podpowiedz-runner-offline")).to_have_count(0)
    # lista z cache zostaje: powrot runnera nie czysci katalogu
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible()


# -------------------------------------------------------------- scenariusz online

def test_runner_online_ma_aktywne_przyciski_i_nie_pokazuje_paska_trybu_tylko_do_odczytu(
    runner_online, katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    # dowod, ze kokpit juz widzi runnera jako online (bez tego "brak paska" to stan "nieznany")
    expect(page.get_by_label("Runnery").get_by_text("online", exact=True)).to_be_visible(
        timeout=LIMIT_SONDY_MS
    )
    page.wait_for_timeout(OKNO_NA_POLLING_MS)

    expect(_pasek(page)).to_have_count(0)
    expect(page.locator("#podpowiedz-runner-offline")).to_have_count(0)
    expect(_przycisk_odswiez(page)).to_be_enabled()
    expect(_przycisk_uruchom(page)).to_be_enabled()
    _bez_opisu_offline(page)


def test_odswiezenie_przy_runnerze_online_zastepuje_liste_nazwami_z_dysku(
    runner_z_katalogiem_na_dysku, katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)
    expect(_sekcja_definicji(page).get_by_text(NAPIS_DATY_ZASIEWU)).to_be_visible()

    _przycisk_odswiez(page).click()

    expect(_wiersz(page, "Autor z dysku").get_by_role("cell")).to_have_text(
        ["skill", "Autor z dysku", "author"], timeout=LIMIT_UI_MS
    )
    expect(_wiersz(page, "przeplyw-z-dysku").get_by_role("cell")).to_have_text(
        ["workflow", "przeplyw-z-dysku", "—"]
    )
    for _rodzaj, _klucz, nazwa, _rola in ZASIEW:
        expect(_wiersz(page, nazwa)).to_have_count(0)
    # data pokazana na ekranie = data z bazy, nie stara z zasiewu
    (data_z_bazy,) = _zapytaj_baze(
        "SELECT to_char(min(odswiezono_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') FROM definicja"
    )[0]
    assert data_z_bazy != "2026-10-30 12:05", "odswiezenie nie zmienilo daty w bazie"
    expect(_sekcja_definicji(page).get_by_text(f"stan z {data_z_bazy} UTC")).to_be_visible()
    expect(_sekcja_definicji(page).get_by_text(NAPIS_DATY_ZASIEWU)).to_have_count(0)


def test_odswiezenie_przy_runnerze_online_wymienia_wiersze_katalogu_w_bazie(
    runner_z_katalogiem_na_dysku, katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    przed = {w[0] for w in _zapytaj_baze("SELECT nazwa FROM definicja")}
    assert przed == {nazwa for _r, _k, nazwa, _ro in ZASIEW}

    _przycisk_odswiez(page).click()
    expect(_wiersz(page, "Autor z dysku")).to_be_visible(timeout=LIMIT_UI_MS)

    po = {w[0] for w in _zapytaj_baze("SELECT nazwa FROM definicja")}
    assert po == NAZWY_Z_DYSKU
    # zadna pozycja nie zostala ze starego zasiewu
    (starych,) = _zapytaj_baze(
        "SELECT count(*) FROM definicja WHERE odswiezono_at <= '2026-10-30T12:05:00+00:00'"
    )[0]
    assert starych == 0


# ---------------------------------------------------------------- pusty katalog

def test_pusty_katalog_mowi_ze_nie_byl_odswiezony_i_nie_pokazuje_tabeli_ani_daty(
    token_runnera, zalogowana_strona
):
    page = zalogowana_strona

    expect(_sekcja_definicji(page).get_by_text(TEKST_PUSTEGO)).to_be_visible(timeout=LIMIT_UI_MS)
    expect(_sekcja_definicji(page).get_by_role("table")).to_have_count(0)
    expect(_sekcja_definicji(page).get_by_text("stan z ")).to_have_count(0)


# ------------------------------------------- podstawione odpowiedzi w przegladarce

def _runner_json(online: bool) -> str:
    return json.dumps([{
        "id": "runner-podstawiony", "name": "runner-podstawiony", "version": "0",
        "backends": [], "online": online, "last_seen_at": "2026-10-30T12:05:00+00:00",
    }])


def _podstaw_runnery(page, stan: dict) -> None:
    """Podstawia tylko GET /api/runners; stan['online'] mozna zmienic w trakcie testu."""
    def _obsluz(route):
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json",
                      body=_runner_json(stan["online"]))
    page.route("**/api/runners", _obsluz)


def _lista_runnerow(zadanie) -> bool:
    return zadanie.method == "GET" and zadanie.url.endswith("/api/runners")


def _sprawdz_przejscie_offline_w_nieznany(page) -> None:
    expect(_pasek(page)).to_have_count(0, timeout=LIMIT_SONDY_MS)
    expect(page.locator("#podpowiedz-runner-offline")).to_have_count(0)
    expect(_przycisk_uruchom(page)).to_be_enabled()
    expect(_przycisk_odswiez(page)).to_be_enabled()


def test_blad_pobrania_stanu_runnera_odpowiedz_500_zdejmuje_tryb_offline_bez_przeladowania(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    # start ze stanu "offline" (zatrzymany runner), inaczej "nieznany" bylby i stanem poczatkowym
    _poczekaj_na_tryb_offline(page)
    expect(_przycisk_uruchom(page)).to_be_disabled()
    # 500 z POPRAWNYM JSON-em runnera offline: bez galezi `!r.ok` front zostalby w "offline";
    # tresc nie-JSON trafilaby do `catch` i nie chronila galezi
    page.route("**/api/runners", lambda route: route.fulfill(
        status=500, content_type="application/json", body=_runner_json(False))
        if route.request.method == "GET" else route.continue_())

    _sprawdz_przejscie_offline_w_nieznany(page)


def test_blad_pobrania_stanu_runnera_zerwane_polaczenie_zdejmuje_tryb_offline_bez_przeladowania(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    _poczekaj_na_tryb_offline(page)
    expect(_przycisk_uruchom(page)).to_be_disabled()
    page.route("**/api/runners", lambda route: route.abort() if route.request.method == "GET"
               else route.continue_())

    _sprawdz_przejscie_offline_w_nieznany(page)


def test_ostrzezenie_o_aktywnym_runie_ma_wylaczone_uruchom_mimo_to_gdy_runner_przejdzie_w_offline(
    zalogowana_strona
):
    page = zalogowana_strona
    stan = {"online": True}
    _podstaw_runnery(page, stan)
    wyslane_starty: list[str] = []

    def _start(route):
        if route.request.method != "POST":
            route.continue_()
            return
        wyslane_starty.append(route.request.post_data or "")
        route.fulfill(status=409, content_type="application/json", body=json.dumps(
            {"detail": "trwa run", "aktywny_run_id": str(uuid.uuid4())}))
    page.route("**/api/runs", _start)
    page.reload()
    expect(_przycisk_uruchom(page)).to_be_enabled(timeout=LIMIT_UI_MS)
    _przycisk_uruchom(page).click()
    mimo_to = page.get_by_role("button", name="Uruchom mimo to")
    expect(mimo_to).to_be_enabled(timeout=LIMIT_UI_MS)   # online: przycisk dziala

    stan["online"] = False
    _poczekaj_na_tryb_offline(page)

    expect(mimo_to).to_be_disabled()
    expect(mimo_to).to_have_accessible_description(TEKST_PODPOWIEDZI)
    # proba akcji: dispatch_event omija actionability Playwrighta, ale nie wylaczenie przycisku
    mimo_to.dispatch_event("click")
    page.wait_for_timeout(OKNO_NA_POLLING_MS)
    assert len(wyslane_starty) == 1, "wylaczony przycisk nie moze wyslac drugiego startu"


def test_blad_odswiezenia_katalogu_pokazuje_komunikat_i_zostawia_liste_bez_zmian(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    _podstaw_runnery(page, {"online": True})
    page.route("**/api/katalog/odswiez", lambda route: route.fulfill(
        status=409, content_type="application/json", body='{"detail": "offline"}'))
    page.reload()
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)

    _przycisk_odswiez(page).click()

    expect(_sekcja_definicji(page).get_by_role("status")).to_have_text(
        TEKST_BLEDU_ODSWIEZENIA, timeout=LIMIT_UI_MS
    )
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible()
    expect(_sekcja_definicji(page).get_by_text(NAPIS_DATY_ZASIEWU)).to_be_visible()
    expect(_przycisk_odswiez(page)).to_be_enabled()


def test_blad_pobrania_katalogu_pokazuje_komunikat_zamiast_listy(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    # 500 z JSON-em jak realna odpowiedz FastAPI: bez `if (!r.ok) throw` obiekt trafilby do stanu
    page.route("**/api/katalog", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail": "błąd serwera"}')
        if route.request.method == "GET" else route.continue_())

    page.reload()

    expect(_sekcja_definicji(page).get_by_text(TEKST_BLEDU_POBRANIA)).to_be_visible(
        timeout=LIMIT_UI_MS
    )
    expect(_sekcja_definicji(page).get_by_role("table")).to_have_count(0)
    # po bledzie "Ładowanie…" nie moze wisiec obok komunikatu (warunek `!blad`)
    expect(_sekcja_definicji(page).get_by_text("Ładowanie…")).to_have_count(0)


def test_zerwane_polaczenie_przy_odswiezaniu_katalogu_pokazuje_komunikat_i_zostawia_liste(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    _podstaw_runnery(page, {"online": True})
    page.route("**/api/katalog/odswiez", lambda route: route.abort())
    page.reload()
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)

    _przycisk_odswiez(page).click()

    expect(_sekcja_definicji(page).get_by_role("status")).to_have_text(
        TEKST_BLEDU_ODSWIEZENIA, timeout=LIMIT_UI_MS
    )
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible()
    expect(_sekcja_definicji(page).get_by_text(NAPIS_DATY_ZASIEWU)).to_be_visible()
    expect(_przycisk_odswiez(page)).to_be_enabled()


def test_udane_odswiezenie_po_bledzie_zdejmuje_komunikat_bledu_i_podmienia_liste(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    _podstaw_runnery(page, {"online": True})
    odpowiedz_ok = json.dumps({
        "odswiezono_at": "2026-10-31T08:30:00+00:00", "pominiete": 0,
        "definicje": [{
            "runner_id": "r", "projekt": "/p", "rodzaj": "skill", "klucz": "nowy",
            "nazwa": "Nowy z odpowiedzi", "rola": "executor", "sciezka": "s",
            "odswiezono_at": "2026-10-31T08:30:00+00:00"}],
    })
    proby: list[int] = []

    def _odswiez(route):
        proby.append(1)
        if len(proby) == 1:
            route.fulfill(status=409, content_type="application/json", body='{"detail": "x"}')
        else:
            route.fulfill(status=200, content_type="application/json", body=odpowiedz_ok)
    page.route("**/api/katalog/odswiez", _odswiez)
    page.reload()
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)
    _przycisk_odswiez(page).click()
    status = _sekcja_definicji(page).get_by_role("status")
    expect(status).to_have_text(TEKST_BLEDU_ODSWIEZENIA, timeout=LIMIT_UI_MS)

    _przycisk_odswiez(page).click()

    expect(_wiersz(page, "Nowy z odpowiedzi").get_by_role("cell")).to_have_text(
        ["skill", "Nowy z odpowiedzi", "executor"], timeout=LIMIT_UI_MS
    )
    expect(status).to_have_count(0)
    expect(_wiersz(page, "Autor zasiewu")).to_have_count(0)
    expect(_sekcja_definicji(page).get_by_text("stan z 2026-10-31 08:30 UTC")).to_be_visible()


def test_w_trakcie_odswiezania_katalogu_przycisk_jest_wylaczony_z_napisem_odswiezam(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    _podstaw_runnery(page, {"online": True})
    zatrzymane: list = []
    page.route("**/api/katalog/odswiez", lambda route: zatrzymane.append(route))
    page.reload()
    expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)

    _przycisk_odswiez(page).click()

    w_trakcie = _sekcja_definicji(page).get_by_role("button", name="Odświeżam…")
    expect(w_trakcie).to_be_disabled(timeout=LIMIT_UI_MS)
    zatrzymane[0].fulfill(status=409, content_type="application/json", body='{"detail": "x"}')
    expect(_przycisk_odswiez(page)).to_be_enabled(timeout=LIMIT_UI_MS)


def test_pusta_lista_runnerow_to_offline(katalog_zasiany, zalogowana_strona):
    page = zalogowana_strona
    page.route("**/api/runners", lambda route: route.fulfill(
        status=200, content_type="application/json", body="[]")
        if route.request.method == "GET" else route.continue_())

    page.reload()

    expect(_pasek(page)).to_be_visible(timeout=LIMIT_SONDY_MS)
    expect(_przycisk_uruchom(page)).to_be_disabled()


def test_jeden_runner_online_wsrod_offline_wystarcza_by_nie_byc_w_trybie_tylko_do_odczytu(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    lista = [json.loads(_runner_json(False))[0], {**json.loads(_runner_json(True))[0], "id": "r2"}]
    page.route("**/api/runners", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(lista))
        if route.request.method == "GET" else route.continue_())

    with page.expect_response(lambda r: _lista_runnerow(r.request) and r.status == 200):
        page.reload()
    page.wait_for_timeout(OKNO_NA_POLLING_MS)

    expect(_pasek(page)).to_have_count(0)
    expect(_przycisk_uruchom(page)).to_be_enabled()
    expect(_przycisk_odswiez(page)).to_be_enabled()


# ------------------------------------------------ stany przed pierwsza odpowiedzia

def _wstrzymaj_get(page, wzorzec: str) -> tuple[list, dict]:
    """Wstrzymuje GET pod wzorcem (nie realizuje trasy); zwraca (zatrzymane, flaga)."""
    zatrzymane: list = []
    flaga = {"wstrzymuj": True}

    def _obsluz(route):
        if route.request.method == "GET" and flaga["wstrzymuj"]:
            zatrzymane.append(route)
        else:
            route.continue_()
    page.route(wzorzec, _obsluz)
    return zatrzymane, flaga


def _zwolnij(zatrzymane: list, flaga: dict) -> None:
    flaga["wstrzymuj"] = False   # najpierw flaga: kolejne cykle pollingu ida juz do backendu
    for route in list(zatrzymane):
        route.continue_()


def test_przed_pierwsza_odpowiedzia_o_stanie_runnera_widok_nie_jest_w_trybie_tylko_do_odczytu(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    zatrzymane, flaga = _wstrzymaj_get(page, "**/api/runners")
    try:
        with page.expect_request(_lista_runnerow, timeout=LIMIT_UI_MS):
            page.reload()
        expect(_przycisk_uruchom(page)).to_be_visible(timeout=LIMIT_UI_MS)
        expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)

        # odpowiedzi jeszcze nie ma: stan "nieznany", mimo ze w bazie runner jest offline
        expect(_pasek(page)).to_have_count(0)
        expect(page.locator("#podpowiedz-runner-offline")).to_have_count(0)
        expect(_przycisk_uruchom(page)).to_be_enabled()
        expect(_przycisk_odswiez(page)).to_be_enabled()
        _bez_opisu_offline(page)
    finally:
        _zwolnij(zatrzymane, flaga)

    # dowod, ze brak paska wyzej wynikal ze stanu poczatkowego, nie z danych
    expect(_pasek(page)).to_be_visible(timeout=LIMIT_SONDY_MS)


def test_przed_pierwsza_odpowiedzia_o_katalogu_widok_mowi_ladowanie_bez_bledow_i_tabeli(
    katalog_zasiany, zalogowana_strona
):
    page = zalogowana_strona
    # runner online (podstawiony), zeby wlaczony "Odśwież" nie zalezal od stanu offline z bazy
    _podstaw_runnery(page, {"online": True})
    zatrzymane, flaga = _wstrzymaj_get(page, "**/api/katalog")
    sekcja = _sekcja_definicji(page)
    try:
        with page.expect_request(
            lambda z: z.method == "GET" and z.url.endswith("/api/katalog"), timeout=LIMIT_UI_MS
        ):
            page.reload()
        expect(sekcja).to_be_visible(timeout=LIMIT_UI_MS)

        # odpowiedzi jeszcze nie ma: katalog=null, blad=false, bladOdswiezenia=false, odswiezam=false
        expect(sekcja.get_by_text("Ładowanie…")).to_be_visible()
        expect(sekcja.get_by_text(TEKST_BLEDU_POBRANIA)).to_have_count(0)
        expect(sekcja.get_by_text(TEKST_PUSTEGO)).to_have_count(0)
        expect(sekcja.get_by_role("table")).to_have_count(0)
        expect(sekcja.get_by_role("status")).to_have_count(0)
        expect(_przycisk_odswiez(page)).to_be_enabled()
    finally:
        _zwolnij(zatrzymane, flaga)

    expect(_wiersz(page, "Autor zasiewu")).to_be_visible(timeout=LIMIT_UI_MS)
    expect(sekcja.get_by_text("Ładowanie…")).to_have_count(0)

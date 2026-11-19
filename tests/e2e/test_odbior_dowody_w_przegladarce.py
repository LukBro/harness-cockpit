"""Ekran dowodow odbioru w przegladarce (HAR-170).

Run w stanie `to_accept` ma pod sekcja odbioru ekran w czterech czesciach: Twierdzenia, Dowody,
Braki (czerwone wiersze z powodem) i Ryzyko, oraz decyzje czlowieka: "Przyjmij" jest
nieaktywne, dopoki jest czerwony wiersz, chyba ze decydent wpisze uzasadnienie, ktore trafia do
audytu runu (osoba, czas, tresc).

Zasiew idzie prawdziwa trasa wejscia runnera (`POST /api/events` z tokenem runnera) na prawdziwym
kokpicie i PostgreSQL, a skutki decyzji czytamy z bazy (`_w_bazie`), nie z samego interfejsu
(asercja-na-danych). Podstawione odpowiedzi (`page.route`) to odrzucenie 422 w tescie gałęzi
`!r.ok` z poprawnym JSON-em oraz sekcja "Ekran dowodow podstawiony w calosci" na koncu pliku
(tresc wierszy, stany przejsciowe, bledy odczytu i zapisu - tam liczy sie tylko to, co
przegladarka pokazuje po odebraniu danej odpowiedzi). Proces tylko przez fixture'y z conftest
(helper `procesy`), kazde czekanie ma limit czasu.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_WYNIKU_MS = 5000
LIMIT_HTTP_S = 15
SZARY = "rgb(88, 97, 109)"          # #58616D: deklaracja agenta
CZERWONY = "rgb(165, 32, 26)"       # #A5201A: braki
UZASADNIENIE = "Ryzyko zaakceptowane — bramka lint pominięta, zgoda właściciela"
SHA_ENGINE = "a" * 64
SHA_AGENT = "b" * 64
POWOD_BRAKU_BRAMKI = "bramka pominięta: brak_capability"
POWOD_NIESPRAWDZONY = "żadna bramka nie sprawdziła runu"
POWOD_WERDYKTU = "werdykt fail: 2 uwagi blokujące"
POWOD_KOSZTU = "koszt niezmierzony: lint, przeglad"


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


def _dowod(capability: str, source: str = "engine", exit_code: int | None = 0) -> dict:
    """Dowod w ksztalcie wejscia runnera (`runner.dowody`)."""
    return {
        "capability": capability, "exit_code": exit_code,
        "sha256": SHA_ENGINE if source == "engine" else SHA_AGENT,
        "started_at": "2026-03-05T14:07:09+00:00", "finished_at": "2026-03-05T14:07:10+00:00",
        "source": source, "plik": f"dowod-{capability}.json",
    }


class Zasiew:
    """Zasiew runow do odbioru przez wejscie runnera, jak robi to prawdziwy runner."""

    def __init__(self, adres: str, token: str) -> None:
        self.adres = adres
        self.naglowki = {"Authorization": f"Bearer {token}"}

    def run(self) -> SimpleNamespace:
        run_id = f"dowody-{uuid.uuid4().hex[:12]}"
        _w_bazie(
            "INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, status, created_at) "
            "SELECT :id, tenant_id, id, 'local', 'szkielet', 'running', now() FROM runner LIMIT 1",
            {"id": run_id}, zapis=True)
        return SimpleNamespace(id=run_id)

    def zdarzenie(self, run_id: str, kind: str, step_id: str, **pola) -> None:
        odpowiedz = httpx.post(
            f"{self.adres}/api/events", headers=self.naglowki, timeout=LIMIT_HTTP_S,
            json={"run_id": run_id, "kind": kind, "step_id": step_id,
                  "event_id": str(uuid.uuid4()), **pola})
        assert odpowiedz.status_code == 200, odpowiedz.text

    def krok(self, run_id: str, step_id: str, *, koszt: float | None = None, **pola) -> None:
        """Krok wykonany do konca; `koszt=None` to brak pomiaru, `0.0` to zmierzone zero."""
        self.zdarzenie(run_id, "step_started", step_id)
        pomiar = {"cost_usd": koszt, "cost_source": "measured"} if koszt is not None else {}
        self.zdarzenie(run_id, "step_finished", step_id, **pomiar, **pola)

    def koniec(self, run_id: str, ostatni: str, kroki_weryfikujace: list[str] | None = None) -> None:
        self.zdarzenie(run_id, "run_finished", ostatni, kroki_weryfikujace=kroki_weryfikujace)

    def czerwony_run(self) -> SimpleNamespace:
        """Do odbioru, z pominieta bramka, niesprawdzonym runem, werdyktem fail i kosztem bez pomiaru."""
        run = self.run()
        self.krok(run.id, "autor", koszt=0.0123, artefakty=["plan.md", "tdd.md"])
        self.zdarzenie(run.id, "step_skipped", "lint", text="brak_capability: brak capability lint")
        self.zdarzenie(run.id, "step_started", "przeglad")
        self.zdarzenie(run.id, "step_failed", "przeglad", text="2 uwagi blokujące", verdict="fail")
        self.krok(run.id, "typecheck", koszt=0.0, dowod=_dowod("typecheck"))
        self.koniec(run.id, "przeglad", ["lint"])
        # nieudany krok z werdyktem daje run `failed`; ekran dowodow dotyczy runu do odbioru
        _w_bazie("UPDATE run SET status = 'to_accept', reason = NULL WHERE id = :id",
                 {"id": run.id}, zapis=True)
        return run

    def czysty_run(self) -> SimpleNamespace:
        """Do odbioru, wszystkie bramki wykonane, koszt zmierzony, dowod silnika, bez braku."""
        run = self.run()
        self.krok(run.id, "lint", koszt=0.01, dowod=_dowod("lint"), verdict="pass")
        self.koniec(run.id, "lint", ["lint"])
        return run


@pytest.fixture
def zasiew(adres_kokpitu, token_runnera) -> Zasiew:
    return Zasiew(adres_kokpitu, token_runnera)


# --- strona ---------------------------------------------------------------------------------------


def _otworz(page, adres: str, run_id: str) -> None:
    page.goto(f"{adres}/?run={run_id}")
    expect(page.get_by_text(re.compile(r"status: \w+"))).to_be_visible(timeout=LIMIT_WYNIKU_MS)


def _sekcja(page, nazwa: str):
    return page.get_by_role("region", name=nazwa, exact=True)


def _przyjmij(page):
    return page.get_by_role("button", name="Przyjmij", exact=True)


def _odeslij(page):
    return page.get_by_role("button", name="Odeślij do poprawy", exact=True)


def _uzasadnienie(page):
    return page.get_by_role("textbox", name="Uzasadnienie", exact=True)


def _braki(page):
    return page.get_by_test_id("brak")


def _wpis_decyzji(page):
    return page.get_by_text(re.compile(r"Decyzja: (przyjęto|odesłano do poprawy)"))


def _decyzje_w_bazie(run_id: str) -> dict:
    """Jedyne zdarzenie `acceptance` runu (scalar_one pada, gdy jest 0 albo wiecej niz 1)."""
    return json.loads(_w_bazie(
        "SELECT text FROM event WHERE run_id = :id AND kind = 'acceptance'", {"id": run_id}))


def _liczba_decyzji(run_id: str) -> int:
    return _w_bazie(
        "SELECT count(*) FROM event WHERE run_id = :id AND kind = 'acceptance'", {"id": run_id})


def _tekst_strony(page) -> str:
    return page.inner_text("body")


# =====================================================================================
# Cztery sekcje
# =====================================================================================


def test_run_do_odbioru_ma_cztery_sekcje_dowodow_kazda_z_naglowkiem_trzeciego_poziomu(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()

    _otworz(page, adres_kokpitu, run.id)

    for nazwa in ("Twierdzenia", "Dowody", "Braki", "Ryzyko"):
        expect(_sekcja(page, nazwa)).to_be_visible(timeout=LIMIT_WYNIKU_MS)
        expect(page.get_by_role("heading", name=nazwa, exact=True, level=3)).to_have_count(1)


def test_run_w_trakcie_nie_pokazuje_ekranu_dowodow_odbioru(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.zdarzenie(run.id, "step_started", "autor")

    _otworz(page, adres_kokpitu, run.id)

    expect(page.get_by_text("status: running")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    for nazwa in ("Twierdzenia", "Dowody", "Braki", "Ryzyko"):
        expect(_sekcja(page, nazwa)).to_have_count(0)
    expect(_przyjmij(page)).to_have_count(0)


def test_twierdzenia_to_nazwy_artefaktow_zadeklarowanych_przez_kroki(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()

    _otworz(page, adres_kokpitu, run.id)

    twierdzenia = _sekcja(page, "Twierdzenia")
    expect(twierdzenia).to_contain_text("plan.md", timeout=LIMIT_WYNIKU_MS)
    expect(twierdzenia).to_contain_text("tdd.md")


# =====================================================================================
# Braki na czerwono
# =====================================================================================


def test_run_z_pominieta_bramka_i_werdyktem_fail_pokazuje_czerwone_wiersze_z_pelnym_powodem(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()

    _otworz(page, adres_kokpitu, run.id)

    wiersze = _sekcja(page, "Braki").get_by_test_id("brak")
    expect(wiersze).to_have_count(4, timeout=LIMIT_WYNIKU_MS)
    for i, powod in enumerate(
            [POWOD_BRAKU_BRAMKI, POWOD_NIESPRAWDZONY, POWOD_WERDYKTU, POWOD_KOSZTU]):
        expect(wiersze.nth(i)).to_have_text(powod)
        expect(wiersze.nth(i)).to_have_attribute("role", "alert")
        expect(wiersze.nth(i)).to_have_css("color", CZERWONY)


def test_werdykt_kroku_jest_widoczny_w_sekcji_dowodow_razem_z_wynikiem(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()

    _otworz(page, adres_kokpitu, run.id)

    werdykt = _sekcja(page, "Dowody").get_by_test_id("werdykt")
    expect(werdykt).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(werdykt).to_contain_text("fail")


def test_run_bez_brakow_pokazuje_brak_brakow_i_nie_ma_czerwonych_wierszy(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czysty_run()

    _otworz(page, adres_kokpitu, run.id)

    expect(_sekcja(page, "Braki")).to_have_text(re.compile(r"brak braków"), timeout=LIMIT_WYNIKU_MS)
    expect(_braki(page)).to_have_count(0)


# =====================================================================================
# Decyzja: "Przyjmij" przy czerwonym wierszu
# =====================================================================================


def test_przyjmij_przy_czerwonym_wierszu_jest_nieaktywne_dopoki_nie_ma_uzasadnienia(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()
    _otworz(page, adres_kokpitu, run.id)

    expect(_przyjmij(page)).to_be_disabled(timeout=LIMIT_WYNIKU_MS)
    expect(_odeslij(page)).to_be_enabled()
    _uzasadnienie(page).fill("   ")
    expect(_przyjmij(page)).to_be_disabled()           # same biale znaki nie sa uzasadnieniem
    _uzasadnienie(page).fill(UZASADNIENIE)
    expect(_przyjmij(page)).to_be_enabled()
    _uzasadnienie(page).fill("")
    expect(_przyjmij(page)).to_be_disabled()           # skasowanie uzasadnienia znow blokuje
    assert _liczba_decyzji(run.id) == 0


def test_przyjecie_z_uzasadnieniem_zapisuje_w_audycie_osobe_czas_i_tresc_i_pokazuje_wpis(
    zalogowana_strona, adres_kokpitu, zasiew, dane_wlasciciela
):
    page = zalogowana_strona
    email, _ = dane_wlasciciela
    run = zasiew.czerwony_run()
    _otworz(page, adres_kokpitu, run.id)
    _uzasadnienie(page).fill(f"  {UZASADNIENIE}  ")

    _przyjmij(page).click()

    wpis = _wpis_decyzji(page)
    expect(wpis).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(wpis).to_contain_text("Decyzja: przyjęto")
    expect(wpis).to_contain_text(email)
    expect(wpis).to_contain_text(f"uzasadnienie: {UZASADNIENIE}")
    expect(_przyjmij(page)).to_have_count(0)
    expect(_odeslij(page)).to_have_count(0)
    audyt = _decyzje_w_bazie(run.id)
    uzytkownik = _w_bazie('SELECT id FROM "user" WHERE email = :email', {"email": email})
    assert audyt == {"user_id": uzytkownik, "email": email, "decyzja": "accept",
                     "uzasadnienie": UZASADNIENIE}
    assert _w_bazie("SELECT at IS NOT NULL FROM event WHERE run_id = :id AND kind = 'acceptance'",
                    {"id": run.id}) is True
    assert _w_bazie("SELECT status FROM run WHERE id = :id", {"id": run.id}) == "to_accept"


def test_wpis_decyzji_przetrwa_przeladowanie_strony_a_przyciski_decyzji_nie_wracaja(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()
    _otworz(page, adres_kokpitu, run.id)
    _uzasadnienie(page).fill(UZASADNIENIE)
    _przyjmij(page).click()
    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)

    _otworz(page, adres_kokpitu, run.id)

    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_wpis_decyzji(page)).to_contain_text(f"uzasadnienie: {UZASADNIENIE}")
    expect(_przyjmij(page)).to_have_count(0)
    expect(_odeslij(page)).to_have_count(0)
    assert _liczba_decyzji(run.id) == 1


def test_odeslanie_do_poprawy_nie_wymaga_uzasadnienia_i_zapisuje_decyzje_send_back(
    zalogowana_strona, adres_kokpitu, zasiew, dane_wlasciciela
):
    page = zalogowana_strona
    email, _ = dane_wlasciciela
    run = zasiew.czerwony_run()
    _otworz(page, adres_kokpitu, run.id)

    _odeslij(page).click()

    wpis = _wpis_decyzji(page)
    expect(wpis).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(wpis).to_contain_text("Decyzja: odesłano do poprawy")
    expect(wpis).to_contain_text(email)
    audyt = _decyzje_w_bazie(run.id)
    assert (audyt["decyzja"], audyt["uzasadnienie"], audyt["email"]) == ("send_back", None, email)
    assert _w_bazie("SELECT status FROM run WHERE id = :id", {"id": run.id}) == "to_accept"


def test_run_bez_brakow_mozna_przyjac_bez_uzasadnienia_a_audyt_ma_brak_uzasadnienia(
    zalogowana_strona, adres_kokpitu, zasiew, dane_wlasciciela
):
    page = zalogowana_strona
    email, _ = dane_wlasciciela
    run = zasiew.czysty_run()
    _otworz(page, adres_kokpitu, run.id)

    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    _przyjmij(page).click()

    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    audyt = _decyzje_w_bazie(run.id)
    assert (audyt["decyzja"], audyt["uzasadnienie"], audyt["email"]) == ("accept", None, email)


def test_odrzucenie_422_przez_kokpit_pokazuje_komunikat_z_detail_i_nie_zapisuje_decyzji(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czerwony_run()
    komunikat = "uzasadnienie jest wymagane, gdy są braki"
    page.route(
        "**/api/runs/*/decyzja-odbioru",
        lambda r: r.fulfill(status=422, json={"detail": komunikat}))
    _otworz(page, adres_kokpitu, run.id)
    _uzasadnienie(page).fill(UZASADNIENIE)

    _przyjmij(page).click()

    blad = page.get_by_role("alert").filter(has_text=komunikat)
    expect(blad).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_wpis_decyzji(page)).to_have_count(0)
    expect(_przyjmij(page)).to_be_enabled()
    assert _liczba_decyzji(run.id) == 0


def test_api_odrzuca_przyjecie_z_brakami_bez_uzasadnienia_kodem_422_a_z_uzasadnieniem_przyjmuje(
    adres_kokpitu, zasiew, dane_wlasciciela
):
    email, haslo = dane_wlasciciela
    run = zasiew.czerwony_run()
    with httpx.Client(base_url=adres_kokpitu, timeout=LIMIT_HTTP_S) as klient:
        logowanie = klient.post("/api/konto/logowanie", json={"email": email, "haslo": haslo})
        assert logowanie.status_code == 200, logowanie.text
        naglowki = {"X-CSRF-Token": logowanie.json()["csrf"]}
        adres = f"/api/runs/{run.id}/decyzja-odbioru"

        bez = klient.post(adres, json={"decyzja": "accept"}, headers=naglowki)
        spacje = klient.post(adres, json={"decyzja": "accept", "uzasadnienie": "  "}, headers=naglowki)

        assert (bez.status_code, spacje.status_code) == (422, 422)
        assert _liczba_decyzji(run.id) == 0
        z = klient.post(adres, json={"decyzja": "accept", "uzasadnienie": UZASADNIENIE},
                        headers=naglowki)
        assert z.status_code == 200, z.text
    assert _decyzje_w_bazie(run.id)["uzasadnienie"] == UZASADNIENIE
    assert _liczba_decyzji(run.id) == 1


# =====================================================================================
# Dowod agenta a dowod silnika
# =====================================================================================


def test_dowod_agenta_ma_podpis_deklaracja_agenta_i_szary_kolor_a_dowod_silnika_podpis_dowod_silnika(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.krok(run.id, "typecheck", koszt=0.1, dowod=_dowod("typecheck", "engine"))
    zasiew.krok(run.id, "recenzja", koszt=0.1, dowod=_dowod("przeglad-reczny", "agent", None))
    zasiew.koniec(run.id, "recenzja", ["typecheck"])

    _otworz(page, adres_kokpitu, run.id)

    wiersze = _sekcja(page, "Dowody").get_by_test_id("dowod")
    expect(wiersze).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    agent = wiersze.filter(has_text="deklaracja agenta")
    silnik = wiersze.filter(has_text="dowód silnika")
    expect(agent).to_have_count(1)
    expect(silnik).to_have_count(1)
    expect(agent).to_have_css("color", SZARY)
    expect(silnik).not_to_have_css("color", SZARY)
    expect(agent).not_to_contain_text("dowód silnika")
    expect(silnik).not_to_contain_text("deklaracja agenta")


# =====================================================================================
# Koszt: niezmierzone to nie zero
# =====================================================================================


def test_koszt_bez_pomiaru_wyswietla_sie_jako_niezmierzone_a_na_stronie_nie_ma_dolara_zero(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.krok(run.id, "autor", koszt=None)
    zasiew.koniec(run.id, "autor", None)

    _otworz(page, adres_kokpitu, run.id)

    pomiar = page.get_by_test_id("pomiar")
    expect(pomiar).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(pomiar).to_contain_text("niezmierzone")
    assert "$0" not in _tekst_strony(page)


def test_zmierzony_koszt_zero_to_zero_z_czterema_miejscami_a_nie_niezmierzone(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.krok(run.id, "autor", koszt=0.0)
    zasiew.koniec(run.id, "autor", None)

    _otworz(page, adres_kokpitu, run.id)

    pomiar = page.get_by_test_id("pomiar")
    expect(pomiar).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(pomiar).to_contain_text("$0.0000")
    assert "niezmierzon" not in _tekst_strony(page)


def test_koszt_zmierzony_jest_pokazany_z_dolarem_i_czterema_miejscami_po_przecinku(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.krok(run.id, "a-krok", koszt=0.5)
    zasiew.krok(run.id, "b-krok", koszt=0.0123456)
    zasiew.koniec(run.id, "b-krok", None)

    _otworz(page, adres_kokpitu, run.id)

    pomiary = page.get_by_test_id("pomiar")
    expect(pomiary).to_have_count(2, timeout=LIMIT_WYNIKU_MS)
    expect(pomiary.nth(0)).to_contain_text("$0.5000")
    expect(pomiary.nth(1)).to_contain_text("$0.0123")


# =====================================================================================
# Ryzyko: brak danych to nie brak ryzyka
# =====================================================================================


def test_run_bez_kotwic_pokazuje_brak_danych_o_kotwicach_i_o_mutantach(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.czysty_run()

    _otworz(page, adres_kokpitu, run.id)

    ryzyko = _sekcja(page, "Ryzyko")
    expect(ryzyko).to_contain_text("kotwice: brak danych", timeout=LIMIT_WYNIKU_MS)
    expect(ryzyko).to_contain_text("mutanty: brak danych")


def test_pusta_lista_kotwic_to_kotwice_brak_a_nie_brak_danych(
    zalogowana_strona, adres_kokpitu, zasiew
):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.krok(run.id, "autor", koszt=0.1, kotwice=[])
    zasiew.koniec(run.id, "autor", None)

    _otworz(page, adres_kokpitu, run.id)

    ryzyko = _sekcja(page, "Ryzyko")
    expect(ryzyko).to_contain_text(re.compile(r"kotwice: brak(?! danych)"), timeout=LIMIT_WYNIKU_MS)
    expect(ryzyko).not_to_contain_text("kotwice: brak danych")
    expect(ryzyko).to_contain_text("mutanty: brak danych")


def test_kotwice_ryzyk_sa_wypisane_w_sekcji_ryzyka(zalogowana_strona, adres_kokpitu, zasiew):
    page = zalogowana_strona
    run = zasiew.run()
    zasiew.krok(run.id, "autor", koszt=0.1, kotwice=["src/a.py:12", "web/b.tsx:7"])
    zasiew.koniec(run.id, "autor", None)

    _otworz(page, adres_kokpitu, run.id)

    ryzyko = _sekcja(page, "Ryzyko")
    expect(ryzyko).to_contain_text("src/a.py:12", timeout=LIMIT_WYNIKU_MS)
    expect(ryzyko).to_contain_text("web/b.tsx:7")
    expect(ryzyko).not_to_contain_text("kotwice: brak")
    expect(ryzyko).to_contain_text("mutanty: brak danych")


# =====================================================================================
# Ekran dowodow podstawiony w calosci (page.route): tresc wiersza, stany przejsciowe, bledy
# =====================================================================================
#
# Dla tych testow nie liczy sie, co lezy w bazie, tylko to, co przegladarka pokazuje po odebraniu
# dokladnie takiej odpowiedzi kokpitu (zadanie `/dowody`, szczegoly runu i zapis decyzji sa
# podstawione, strumien SSE wstrzymany). Skutki zapisu w bazie sprawdzaja testy wyzej.

EMAIL_STUB = "decydent@example.com"
CZAS_DECYZJI = "2026-03-05T12:00:00+00:00"
_WSTRZYMANE_TRASY: list = []


@pytest.fixture(autouse=True)
def _zwolnij_wstrzymane_trasy(page):
    """Po tescie przerywa trasy wstrzymane przez `_wstrzymaj` i nigdy nie obsluzone (HAR-121)."""
    from playwright.sync_api import Error as BladPlaywrighta

    yield
    while _WSTRZYMANE_TRASY:
        trasa = _WSTRZYMANE_TRASY.pop()
        try:
            trasa.abort()
        except BladPlaywrighta:
            pass


def _wstrzymaj(page, wzorzec: str) -> list:
    """Zadania pasujace do wzorca zostaja bez odpowiedzi, az test wywola `route.fulfill`."""
    wstrzymane: list = []

    def wstrzymaj(trasa) -> None:
        wstrzymane.append(trasa)
        _WSTRZYMANE_TRASY.append(trasa)

    page.route(wzorzec, wstrzymaj)
    return wstrzymane


def _czekaj_na(page, warunek, nazwa: str, limit_ms: int = 6000) -> None:
    """Czekanie z limitem na warunek spelniony przez handlery tras."""
    odliczanie = limit_ms
    while not warunek():
        assert odliczanie > 0, f"przekroczony limit {limit_ms} ms: {nazwa}"
        page.wait_for_timeout(50)
        odliczanie -= 50


def _dowody_stub(run_id: str, **zmiany) -> dict:
    """Odpowiedz `GET /api/runs/{id}/dowody` w pelnym ksztalcie; domyslnie same puste wartosci."""
    baza = {
        "run_id": run_id, "twierdzenia": [], "dowody": [], "werdykty": [], "braki": [],
        "ryzyka": {"kotwice": None, "mutanty": None}, "pomiary": [], "decyzja": None,
    }
    return {**baza, **zmiany}


class _Ekran:
    """Run do odbioru bez wiersza w bazie: szczegoly, dowody i zapis decyzji z `page.route`."""

    def __init__(self, page, status: str = "to_accept", **zmiany) -> None:
        self.run_id = str(uuid.uuid4())
        self.status = status
        self.dowody = _dowody_stub(self.run_id, **zmiany)
        self.pobrania_runu = 0
        self.pobrania_dowodow = 0
        self.zapisy_decyzji: list[dict] = []
        self.obsluga_dowodow = None       # (trasa, numer pobrania) -> None; domyslnie odpowiedz z `dowody`
        self.obsluga_decyzji = None       # (trasa, numer zapisu) -> None; domyslnie `zapisz`
        self.strumienie = _wstrzymaj(page, "**/api/runs/*/events")
        adres = f"/api/runs/{self.run_id}"
        page.route(lambda url: url.split("?")[0].endswith(adres), self._szczegoly)
        page.route(lambda url: url.split("?")[0].endswith(f"{adres}/dowody"), self._odczyt_dowodow)
        page.route(lambda url: url.split("?")[0].endswith(f"{adres}/decyzja-odbioru"), self._zapis)

    def _szczegoly(self, trasa) -> None:
        if trasa.request.method != "GET":
            trasa.continue_()
            return
        self.pobrania_runu += 1
        trasa.fulfill(json={
            "id": self.run_id, "workflow": "szkielet", "status": self.status,
            "runner_kind": "local", "reason": None, "events": [], "steps": []})

    def _odczyt_dowodow(self, trasa) -> None:
        self.pobrania_dowodow += 1
        if self.obsluga_dowodow is None:
            trasa.fulfill(json=self.dowody)
        else:
            self.obsluga_dowodow(trasa, self.pobrania_dowodow)

    def _zapis(self, trasa) -> None:
        zadanie = trasa.request
        self.zapisy_decyzji.append({
            "metoda": zadanie.method, "typ": zadanie.headers.get("content-type"),
            "cialo": zadanie.post_data_json})
        if self.obsluga_decyzji is None:
            self.zapisz(trasa)
        else:
            self.obsluga_decyzji(trasa, len(self.zapisy_decyzji))

    def zapisz(self, trasa) -> None:
        """Przyjmuje decyzje jak kokpit: kolejny odczyt dowodow zwraca juz wpis decyzji."""
        cialo = trasa.request.post_data_json
        decyzja = {"decyzja": cialo["decyzja"], "user_id": "u-1", "email": EMAIL_STUB,
                   "uzasadnienie": cialo["uzasadnienie"], "at": CZAS_DECYZJI}
        self.dowody["decyzja"] = decyzja
        trasa.fulfill(json=decyzja)


def _blad_odczytu(page):
    return page.get_by_test_id("dowody-blad")


def _blad_decyzji(page):
    return page.get_by_test_id("dowody-decyzja-blad")


def _wstrzymaj_od(ekran: _Ekran, od: int, pula: list):
    """Obsluga: pobrania od numeru `od` wstrzymane (do `pula`), wczesniejsze zwraca odpowiedz `dowody`."""
    def obsluga(trasa, numer: int) -> None:
        if numer < od:
            trasa.fulfill(json=ekran.dowody)
        else:
            pula.append(trasa)
            _WSTRZYMANE_TRASY.append(trasa)

    return obsluga


ODPOWIEDZI_BEZ_CZYTELNEGO_DETAIL = {
    "detail_jest_lista": (422, {"json": {"detail": [{"msg": "zle pole"}]}}),
    "detail_jest_pusty": (500, {"json": {"detail": ""}}),
    "detail_to_liczba": (409, {"json": {"detail": 5}}),
    "cialo_nie_jest_json": (502, {"body": "Bad Gateway", "content_type": "text/plain"}),
    "json_null": (503, {"body": "null", "content_type": "application/json"}),
}


def test_blad_odczytu_dowodow_pokazuje_komunikat_z_detail_a_ponowienie_pokazuje_wczytywanie_i_ekran(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    wstrzymane: list = []

    def obsluga(trasa, numer: int) -> None:
        if numer == 1:
            trasa.fulfill(status=500, json={"detail": "baza niedostępna"})
        else:
            wstrzymane.append(trasa)
            _WSTRZYMANE_TRASY.append(trasa)

    ekran.obsluga_dowodow = obsluga
    _otworz(page, adres_kokpitu, ekran.run_id)

    blad = _blad_odczytu(page)
    expect(blad).to_have_text(
        "Nie udało się odczytać dowodów odbioru. baza niedostępna", timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_attribute("role", "alert")
    expect(blad).to_have_css("color", CZERWONY)
    expect(_sekcja(page, "Braki")).to_have_count(0)
    page.get_by_role("button", name="Spróbuj ponownie", exact=True).click()
    expect(page.get_by_text("Wczytuję dowody odbioru…", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_count(0)
    _czekaj_na(page, lambda: len(wstrzymane) == 1, "ponowne pobranie dowodow")
    wstrzymane[0].fulfill(json=ekran.dowody)
    expect(_sekcja(page, "Braki")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_count(0)
    assert ekran.pobrania_dowodow == 2


def test_dopoki_serwer_nie_odpowiedzial_ekran_dowodow_pokazuje_wczytywanie_a_nie_puste_sekcje(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    wstrzymane: list = []
    ekran.obsluga_dowodow = _wstrzymaj_od(ekran, 1, wstrzymane)
    _otworz(page, adres_kokpitu, ekran.run_id)

    expect(page.get_by_text("Wczytuję dowody odbioru…", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    for nazwa in ("Twierdzenia", "Dowody", "Braki", "Ryzyko"):
        expect(_sekcja(page, nazwa)).to_have_count(0)
    expect(_blad_odczytu(page)).to_have_count(0)
    _czekaj_na(page, lambda: len(wstrzymane) == 1, "pierwsze pobranie dowodow")
    wstrzymane[0].fulfill(json=ekran.dowody)
    expect(_sekcja(page, "Braki")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("Wczytuję dowody odbioru…", exact=True)).to_have_count(0)


def test_utrata_polaczenia_przy_odczycie_dowodow_pokazuje_komunikat_o_braku_polaczenia(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    ekran.obsluga_dowodow = lambda trasa, numer: trasa.abort()
    _otworz(page, adres_kokpitu, ekran.run_id)

    blad = _blad_odczytu(page)
    expect(blad).to_have_text("Brak połączenia z kokpitem.", timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_css("color", CZERWONY)
    expect(page.get_by_role("button", name="Spróbuj ponownie", exact=True)).to_be_visible()


@pytest.mark.parametrize(("kod", "odpowiedz"), list(ODPOWIEDZI_BEZ_CZYTELNEGO_DETAIL.values()),
                         ids=list(ODPOWIEDZI_BEZ_CZYTELNEGO_DETAIL))
def test_odczyt_dowodow_odrzucony_bez_czytelnego_detail_pokazuje_kod_http(
    zalogowana_strona, adres_kokpitu, kod, odpowiedz
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    ekran.obsluga_dowodow = lambda trasa, numer: trasa.fulfill(status=kod, **odpowiedz)
    _otworz(page, adres_kokpitu, ekran.run_id)

    expect(_blad_odczytu(page)).to_have_text(
        f"Nie udało się odczytać dowodów odbioru. błąd {kod}", timeout=LIMIT_WYNIKU_MS)


@pytest.mark.parametrize(("kod", "odpowiedz"), list(ODPOWIEDZI_BEZ_CZYTELNEGO_DETAIL.values()),
                         ids=list(ODPOWIEDZI_BEZ_CZYTELNEGO_DETAIL))
def test_odrzucenie_decyzji_bez_czytelnego_detail_pokazuje_kod_http_na_czerwono(
    zalogowana_strona, adres_kokpitu, kod, odpowiedz
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    ekran.obsluga_decyzji = lambda trasa, numer: trasa.fulfill(status=kod, **odpowiedz)
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)

    _przyjmij(page).click()

    blad = _blad_decyzji(page)
    expect(blad).to_have_text(f"błąd {kod}", timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_attribute("role", "alert")
    expect(blad).to_have_css("color", CZERWONY)
    expect(_wpis_decyzji(page)).to_have_count(0)


def test_utrata_polaczenia_przy_zapisie_decyzji_pokazuje_komunikat_i_odblokowuje_przyciski(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    ekran.obsluga_decyzji = lambda trasa, numer: trasa.abort()
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)

    _przyjmij(page).click()

    blad = _blad_decyzji(page)
    expect(blad).to_have_text("Brak połączenia z kokpitem.", timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_css("color", CZERWONY)
    expect(_przyjmij(page)).to_be_enabled()
    expect(_odeslij(page)).to_be_enabled()


def test_gdy_zapis_decyzji_trwa_oba_przyciski_sa_zablokowane_a_po_odpowiedzi_widac_wpis(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    wstrzymane: list = []

    def obsluga(trasa, numer: int) -> None:
        wstrzymane.append(trasa)
        _WSTRZYMANE_TRASY.append(trasa)

    ekran.obsluga_decyzji = obsluga
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)

    _przyjmij(page).click()

    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zapis decyzji w locie")
    expect(_przyjmij(page)).to_be_disabled()
    expect(_odeslij(page)).to_be_disabled()
    ekran.zapisz(wstrzymane[0])
    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    assert len(ekran.zapisy_decyzji) == 1


def test_kolejna_proba_zapisu_decyzji_od_razu_gasi_komunikat_o_poprzednim_odrzuceniu(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    wstrzymane: list = []

    def obsluga(trasa, numer: int) -> None:
        if numer == 1:
            trasa.fulfill(status=409, json={"detail": "decyzja zostala juz podjeta"})
        else:
            wstrzymane.append(trasa)
            _WSTRZYMANE_TRASY.append(trasa)

    ekran.obsluga_decyzji = obsluga
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    _przyjmij(page).click()
    expect(_blad_decyzji(page)).to_have_text("decyzja zostala juz podjeta", timeout=LIMIT_WYNIKU_MS)

    _przyjmij(page).click()

    _czekaj_na(page, lambda: len(wstrzymane) == 1, "druga proba zapisu w locie")
    expect(_blad_decyzji(page)).to_have_count(0)
    ekran.zapisz(wstrzymane[0])
    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)


ZAPISY_DECYZJI = {
    "przyjecie_z_uzasadnieniem_obcietym_ze_spacji": (
        "Przyjmij", "  uzasadnienie z brzegami  ",
        {"decyzja": "accept", "uzasadnienie": "uzasadnienie z brzegami"}),
    "odeslanie_z_samymi_spacjami_to_brak_uzasadnienia": (
        "Odeślij do poprawy", "   ", {"decyzja": "send_back", "uzasadnienie": None}),
    "odeslanie_z_pustym_polem_to_brak_uzasadnienia": (
        "Odeślij do poprawy", "", {"decyzja": "send_back", "uzasadnienie": None}),
}


@pytest.mark.parametrize(("przycisk", "wpisane", "cialo"), list(ZAPISY_DECYZJI.values()),
                         ids=list(ZAPISY_DECYZJI))
def test_zapis_decyzji_wysyla_do_kokpitu_post_z_json_bez_spacji_na_brzegach_uzasadnienia(
    zalogowana_strona, adres_kokpitu, przycisk, wpisane, cialo
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    _uzasadnienie(page).fill(wpisane)

    page.get_by_role("button", name=przycisk, exact=True).click()

    _czekaj_na(page, lambda: len(ekran.zapisy_decyzji) == 1, "zapis decyzji")
    assert ekran.zapisy_decyzji == [
        {"metoda": "POST", "typ": "application/json", "cialo": cialo}]


def test_udana_odpowiedz_na_decyzje_z_nieczytelnym_cialem_nie_jest_bledem_i_odswieza_ekran(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    ekran.obsluga_decyzji = lambda trasa, numer: trasa.fulfill(
        status=200, body="przyjeto", content_type="text/plain")
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    odczyty_przed = ekran.pobrania_dowodow

    _przyjmij(page).click()

    _czekaj_na(page, lambda: ekran.pobrania_dowodow == odczyty_przed + 1, "ponowny odczyt dowodow")
    expect(_blad_decyzji(page)).to_have_count(0)
    expect(_przyjmij(page)).to_be_enabled()


def _blad_ponownego_odczytu(page):
    return page.get_by_test_id("blad-odczytu-dowodow")


def test_nieudany_ponowny_odczyt_dowodow_po_decyzji_pokazuje_blad_nad_dotychczasowa_trescia(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page, twierdzenia=[{"step_id": "a", "artefakt": "plan.md"}])
    ekran.obsluga_dowodow = lambda trasa, numer: (
        trasa.fulfill(json=ekran.dowody) if numer == 1
        else trasa.fulfill(status=500, json={"detail": "awaria"}))
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    expect(_blad_ponownego_odczytu(page)).to_have_count(0)

    _przyjmij(page).click()

    blad = _blad_ponownego_odczytu(page)
    expect(blad).to_have_text(
        "Nie udało się odczytać dowodów odbioru. awaria", timeout=LIMIT_WYNIKU_MS)
    expect(blad).to_have_attribute("role", "alert")
    expect(blad).to_have_css("color", CZERWONY)
    expect(_blad_odczytu(page)).to_have_count(0)
    expect(page.get_by_test_id("twierdzenie")).to_have_text(["a: plan.md"])
    expect(_sekcja(page, "Braki")).to_be_visible()
    assert ekran.pobrania_dowodow == 2


def test_utrata_sieci_przy_ponownym_odczycie_dowodow_pokazuje_komunikat_o_braku_polaczenia_nad_trescia(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page, twierdzenia=[{"step_id": "a", "artefakt": "plan.md"}])
    ekran.obsluga_dowodow = lambda trasa, numer: (
        trasa.fulfill(json=ekran.dowody) if numer == 1 else trasa.abort())
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)

    _przyjmij(page).click()

    expect(_blad_ponownego_odczytu(page)).to_have_text(
        "Brak połączenia z kokpitem.", timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_test_id("twierdzenie")).to_have_text(["a: plan.md"])


def test_udany_kolejny_odczyt_dowodow_gasi_blad_ponownego_odczytu(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page, twierdzenia=[{"step_id": "a", "artefakt": "plan.md"}])
    ekran.obsluga_dowodow = lambda trasa, numer: (
        trasa.fulfill(status=500, json={"detail": "awaria"}) if numer == 2
        else trasa.fulfill(json=ekran.dowody))
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    _przyjmij(page).click()
    expect(_blad_ponownego_odczytu(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)

    _przyjmij(page).click()

    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(_blad_ponownego_odczytu(page)).to_have_count(0)
    assert ekran.pobrania_dowodow == 3


def test_po_zapisie_decyzji_ekran_pobiera_dowody_od_nowa_a_widok_runu_pobiera_run_ponownie(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    run_przed, dowody_przed = ekran.pobrania_runu, ekran.pobrania_dowodow

    _przyjmij(page).click()

    expect(_wpis_decyzji(page)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    _czekaj_na(page, lambda: ekran.pobrania_runu > run_przed, "ponowne pobranie runu po decyzji")
    assert ekran.pobrania_dowodow == dowody_przed + 1


def test_ekran_odmontowany_w_trakcie_zapisu_decyzji_nie_odswieza_runu_po_spoznionej_odpowiedzi(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)
    wstrzymane: list = []
    ekran.obsluga_dowodow = _wstrzymaj_od(ekran, 2, wstrzymane)
    _otworz(page, adres_kokpitu, ekran.run_id)
    expect(_przyjmij(page)).to_be_enabled(timeout=LIMIT_WYNIKU_MS)
    _przyjmij(page).click()
    _czekaj_na(page, lambda: len(wstrzymane) == 1, "ponowny odczyt dowodow po zapisie w locie")
    # run przestaje byc do odbioru: widok zamyka ekran dowodow, zanim odczyt wroci
    ekran.status = "failed"
    _czekaj_na(page, lambda: len(ekran.strumienie) >= 1, "strumien runu otwarty przez strone")
    ekran.strumienie[0].fulfill(status=200, content_type="text/event-stream", body=": otwarte\n\n")
    expect(page.get_by_text("status: failed")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_test_id("odbior-dowody")).to_have_count(0)
    pobrania_runu = ekran.pobrania_runu

    wstrzymane[0].fulfill(json=ekran.dowody)

    page.wait_for_timeout(500)
    assert ekran.pobrania_runu == pobrania_runu


def test_wpis_decyzji_pokazuje_osobe_czas_i_uzasadnienie_a_brak_uzasadnienia_jako_brak(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    czas = " ".join(page.evaluate("d => new Date(d).toLocaleString('pl-PL')", CZAS_DECYZJI).split())
    for decyzja, uzasadnienie, opis in [
        ("accept", None, "przyjęto"),
        ("send_back", "do poprawy", "odesłano do poprawy"),
    ]:
        ekran = _Ekran(page, decyzja={
            "decyzja": decyzja, "user_id": "u-1", "email": EMAIL_STUB,
            "uzasadnienie": uzasadnienie, "at": CZAS_DECYZJI})
        _otworz(page, adres_kokpitu, ekran.run_id)

        wpis = page.get_by_test_id("dowody-decyzja")

        expect(wpis).to_have_text(
            f"Decyzja: {opis} — {EMAIL_STUB}, {czas}, uzasadnienie: {uzasadnienie or 'brak'}",
            timeout=LIMIT_WYNIKU_MS)
        expect(_przyjmij(page)).to_have_count(0)
        expect(_odeslij(page)).to_have_count(0)


def test_wpis_decyzji_o_nieznanej_wartosci_pokazuje_ja_doslownie(zalogowana_strona, adres_kokpitu):
    page = zalogowana_strona
    ekran = _Ekran(page, decyzja={
        "decyzja": "wstrzymano", "user_id": "u-1", "email": EMAIL_STUB,
        "uzasadnienie": None, "at": "nie-data"})
    _otworz(page, adres_kokpitu, ekran.run_id)

    expect(page.get_by_test_id("dowody-decyzja")).to_have_text(
        f"Decyzja: wstrzymano — {EMAIL_STUB}, nie-data, uzasadnienie: brak", timeout=LIMIT_WYNIKU_MS)


def test_ekran_dowodow_pokazuje_dokladna_tresc_twierdzen_dowodow_werdyktow_i_pomiarow(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(
        page,
        twierdzenia=[{"step_id": "autor", "artefakt": "plan.md"}],
        dowody=[
            {"step_id": "typecheck", "capability": "typecheck", "source": "engine",
             "exit_code": 0, "sha256": SHA_ENGINE, "plik": "dowod-typecheck.json"},
            {"step_id": "recenzja", "capability": "przeglad-reczny", "source": "agent",
             "exit_code": None, "sha256": SHA_AGENT, "plik": "dowod-przeglad.json"},
        ],
        werdykty=[{"step_id": "lint", "verdict": "pass", "zaliczony": True},
                  {"step_id": "przeglad", "verdict": "fail", "zaliczony": False}],
        pomiary=[{"step_id": "autor", "cost_usd": 0.5, "tokens_in": None, "tokens_out": None},
                 {"step_id": "lint", "cost_usd": None, "tokens_in": None, "tokens_out": None}])

    _otworz(page, adres_kokpitu, ekran.run_id)

    expect(page.get_by_test_id("twierdzenie")).to_have_text(["autor: plan.md"], timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_test_id("dowod")).to_have_text([
        "typecheck: typecheck · kod wyjścia: 0 · dowod-typecheck.json · dowód silnika",
        "recenzja: przeglad-reczny · kod wyjścia: brak · dowod-przeglad.json · deklaracja agenta"])
    expect(page.get_by_test_id("werdykt")).to_have_text([
        "lint: werdykt pass (zaliczony)", "przeglad: werdykt fail (niezaliczony)"])
    expect(page.get_by_test_id("pomiar")).to_have_text([
        "autor: koszt $0.5000", "lint: koszt niezmierzone"])


def test_ekran_bez_zadnych_danych_pokazuje_komunikaty_braku_i_nie_rysuje_pustych_list(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    ekran = _Ekran(page)

    _otworz(page, adres_kokpitu, ekran.run_id)

    expect(_sekcja(page, "Twierdzenia")).to_have_text(
        "Twierdzenia brak twierdzeń", use_inner_text=True, timeout=LIMIT_WYNIKU_MS)
    expect(_sekcja(page, "Dowody")).to_have_text("Dowody brak dowodów", use_inner_text=True)
    expect(_sekcja(page, "Braki")).to_have_text("Braki brak braków", use_inner_text=True)
    expect(_sekcja(page, "Ryzyko")).to_have_text(
        "Ryzyko kotwice: brak danych mutanty: brak danych", use_inner_text=True)
    for nazwa in ("Twierdzenia", "Dowody", "Braki", "Ryzyko"):
        expect(_sekcja(page, nazwa).locator("ul")).to_have_count(0)


RYZYKA = {
    "kotwice_bez_danych_mutanty_puste": (
        {"kotwice": None, "mutanty": []}, ["kotwice: brak danych", "mutanty: brak"], []),
    "kotwice_puste_mutanty_z_lista": (
        {"kotwice": [], "mutanty": ["m1", "m2"]}, ["kotwice: brak", "mutanty:"], ["m1", "m2"]),
    "kotwice_z_lista_mutanty_bez_danych": (
        {"kotwice": ["a.py:1", "b.py:2"], "mutanty": None},
        ["kotwice:", "mutanty: brak danych"], ["a.py:1", "b.py:2"]),
}


@pytest.mark.parametrize(("ryzyka", "akapity", "wiersze"), list(RYZYKA.values()), ids=list(RYZYKA))
def test_ryzyko_odroznia_brak_danych_od_pustej_listy_i_od_listy_osobno_dla_kotwic_i_mutantow(
    zalogowana_strona, adres_kokpitu, ryzyka, akapity, wiersze
):
    page = zalogowana_strona
    ekran = _Ekran(page, ryzyka=ryzyka)

    _otworz(page, adres_kokpitu, ekran.run_id)

    sekcja = _sekcja(page, "Ryzyko")
    expect(sekcja.locator("p")).to_have_text(akapity, timeout=LIMIT_WYNIKU_MS)
    if wiersze:
        expect(sekcja.locator("li")).to_have_text(wiersze)
    else:
        expect(sekcja.locator("li")).to_have_count(0)

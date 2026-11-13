"""Bramka zatwierdzenia w widoku runu w przegladarce (HAR-173).

Run czekajacy na bramce pokazuje, na ktory krok czeka, i daje przyciski Zatwierdz oraz Odrzuc
(z wymaganym powodem); run odrzucony pokazuje "odrzucony" z powodem. Zdarzenia wysyla test
(jak prawdziwy runner: Bearer + POST /api/events), wiec proces runnera nie jest potrzebny:
decyzja `approve` tylko wstawia polecenie do kolejki runnera w pamieci kokpitu. Skutki sa
czytane z bazy, nie z samego interfejsu.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_WYNIKU_MS = 3000
KROK_BRAMKI = "wdrozenie"
# baner bramki od poczatku napisu: tekst zdarzenia step_waiting w dzienniku i powod kroku
# w tabeli ("czeka na decyzję człowieka (approval)") zawieraja tylko podnapis "czeka na decyzję"
BANER_BRAMKI = re.compile(r"^czeka na decyzję: krok ")


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
        return pula.submit(lambda: asyncio.run(_wykonaj())).result()


def _utworz_run_w_toku() -> str:
    """Run zespolu zasianego w bazie w stanie `running`, przypisany do jego runnera."""
    run_id = str(uuid.uuid4())
    _w_bazie(
        "INSERT INTO run (id, tenant_id, runner_id, runner_kind, workflow, status, created_at) "
        "SELECT :id, tenant_id, id, 'local', 'szkielet', 'running', now() FROM runner LIMIT 1",
        {"id": run_id}, zapis=True)
    return run_id


def _zdarzenie(adres, token, run_id, step_id, kind, *, tekst=""):
    odpowiedz = httpx.post(
        f"{adres}/api/events", headers={"Authorization": f"Bearer {token}"},
        json={"run_id": run_id, "event_id": str(uuid.uuid4()), "step_id": step_id,
              "agent_id": step_id, "kind": kind, "text": tekst},
        timeout=5)
    assert odpowiedz.status_code == 200, odpowiedz.text


def _run_czekajacy_na_bramce(adres, token) -> str:
    """Run doprowadzony zdarzeniami do `waiting`: krok `a` skonczony, bramka przed krokiem."""
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres, token, run_id, "a", "step_started")
    _zdarzenie(adres, token, run_id, "a", "step_finished")
    _zdarzenie(adres, token, run_id, KROK_BRAMKI, "step_waiting",
               tekst="czeka na decyzję człowieka (approval)")
    assert _status(run_id) == "waiting"
    return run_id


def _status(run_id: str) -> str:
    return str(_w_bazie("SELECT status FROM run WHERE id = :id", {"id": run_id}))


def _powod(run_id: str):
    return _w_bazie("SELECT reason FROM run WHERE id = :id", {"id": run_id})


def _liczba_decyzji(run_id: str) -> int:
    return _w_bazie(
        "SELECT count(*) FROM event WHERE run_id = :id AND kind = 'decision'", {"id": run_id})


def _otworz_run(page, adres, run_id):
    page.goto(f"{adres}/?run={run_id}")
    expect(page.get_by_text(re.compile(r"status: \w+"))).to_be_visible(timeout=LIMIT_WYNIKU_MS)


class _OpoznioneOtwarcie:
    """Strumien runu wstrzymany w przegladarce i licznik odpowiedzi na GET szczegolow runu."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.strumien: list = []
        self.odpowiedzi_get = 0

    def licz(self, odpowiedz) -> None:
        sciezka = odpowiedz.url.split("?")[0]
        if odpowiedz.request.method == "GET" and sciezka.endswith(f"/api/runs/{self.run_id}"):
            self.odpowiedzi_get += 1


def _opoznij_otwarcie_strumienia(page, run_id) -> _OpoznioneOtwarcie:
    """Przed wejsciem na strone: strumien SSE runu czeka na test, GET-y szczegolow sa liczone.

    Front pobiera run dwa razy: od razu przy wejsciu i drugi raz po otwarciu strumienia
    (`onopen`). Wstrzymany strumien nie otwiera sie sam, wiec drugi GET wychodzi dopiero
    po zwolnieniu strumienia przez test (HAR-232)."""
    stan = _OpoznioneOtwarcie(run_id)
    page.route("**/api/runs/*/events", lambda r: stan.strumien.append(r))
    page.on("response", stan.licz)
    return stan


def _poczekaj_na_otwarcie_strumienia(page, stan: _OpoznioneOtwarcie) -> None:
    """Zwalnia strumien i czeka, az serwer odpowie na oba GET-y szczegolow (wejscie i `onopen`).

    Po tym kroku zadne zapytanie o run nie jest w drodze, wiec boczna zmiana stanu w bazie
    nie wyprzedzi odczytu, ktory front wyslal w reakcji na wejscie."""
    while stan.strumien:
        stan.strumien.pop().continue_()
    _czekaj_na(page, lambda: stan.odpowiedzi_get >= 2, "odpowiedz serwera na GET po otwarciu strumienia")


def _zmien_stan_runu_bocznie(page, stan: _OpoznioneOtwarcie, run_id: str, status: str) -> None:
    """Zmiana statusu w bazie bez zdarzenia; zaleglosci sieci (wstrzymany strumien) dochodza po niej."""
    _w_bazie("UPDATE run SET status = :status WHERE id = :id",
             {"id": run_id, "status": status}, zapis=True)
    _poczekaj_na_otwarcie_strumienia(page, stan)


def _zatwierdz(page):
    return page.get_by_role("button", name="Zatwierdź", exact=True)


def _odrzuc(page):
    return page.get_by_role("button", name="Odrzuć", exact=True)


def _pole_powodu(page):
    return page.get_by_label("Powód odrzucenia")


# =====================================================================================
# 1. Run czeka na bramce: widac krok i przyciski
# =====================================================================================


def test_run_czekajacy_na_bramce_pokazuje_na_ktory_krok_czeka(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text(f"czeka na decyzję: krok {KROK_BRAMKI}")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    expect(_zatwierdz(page)).to_be_visible()
    expect(_odrzuc(page)).to_be_visible()
    expect(_pole_powodu(page)).to_be_visible()


def test_run_w_toku_nie_pokazuje_przyciskow_bramki(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "a", "step_started")

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text(BANER_BRAMKI)).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    expect(_zatwierdz(page)).to_have_count(0)
    expect(_odrzuc(page)).to_have_count(0)


# =====================================================================================
# 2. Zatwierdzenie
# =====================================================================================


def test_klikniecie_zatwierdz_wznawia_run_w_bazie_i_zapisuje_zdarzenie_decyzji(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    expect(_zatwierdz(page)).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    assert _liczba_decyzji(run_id) == 0

    _zatwierdz(page).click()

    expect(_zatwierdz(page)).to_have_count(0, timeout=LIMIT_WYNIKU_MS)    # decyzja przyjeta
    assert _status(run_id) == "running"
    assert _liczba_decyzji(run_id) == 1
    assert _powod(run_id) is None                                         # zatwierdzenie bez powodu odrzucenia
    expect(page.get_by_text(BANER_BRAMKI)).to_have_count(0)


def test_zatwierdzenie_nie_wysyla_powodu_wpisanego_w_polu_odrzucenia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    _pole_powodu(page).fill("to tekst do odrzucenia, nie do zatwierdzenia")

    _zatwierdz(page).click()

    expect(_zatwierdz(page)).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    tekst = _w_bazie(
        "SELECT text FROM event WHERE run_id = :id AND kind = 'decision'", {"id": run_id})
    assert tekst == "approve"            # bez powodu: zdarzenie to samo slowo decyzji


# =====================================================================================
# 3. Odrzucenie wymaga powodu
# =====================================================================================


@pytest.mark.parametrize("wpis", ["", "   "], ids=["pusty", "same_spacje"])
def test_odrzuc_jest_wylaczone_gdy_powod_jest_pusty_albo_z_samych_spacji(
    zalogowana_strona, adres_kokpitu, token_runnera, wpis
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    expect(_odrzuc(page)).to_be_visible(timeout=LIMIT_WYNIKU_MS)

    _pole_powodu(page).fill(wpis)

    expect(_odrzuc(page)).to_be_disabled()
    assert _status(run_id) == "waiting"       # nic nie poszlo do kokpitu


def test_odrzuc_staje_sie_aktywne_po_wpisaniu_powodu_i_znowu_wylaczone_po_jego_skasowaniu(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    expect(_odrzuc(page)).to_be_disabled(timeout=LIMIT_WYNIKU_MS)

    _pole_powodu(page).fill("za ryzykowne")
    expect(_odrzuc(page)).to_be_enabled()

    _pole_powodu(page).fill("")
    expect(_odrzuc(page)).to_be_disabled()


def test_odrzucenie_z_powodem_zapisuje_w_bazie_status_odrzucony_i_przyciety_powod(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    _pole_powodu(page).fill("  za ryzykowne wdrozenie  ")

    _odrzuc(page).click()

    expect(page.get_by_text("odrzucony: za ryzykowne wdrozenie")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    assert _status(run_id) == "rejected"
    assert _powod(run_id) == "za ryzykowne wdrozenie"          # bez spacji z brzegow
    assert _liczba_decyzji(run_id) == 1
    expect(_zatwierdz(page)).to_have_count(0)                  # po odrzuceniu bramka znika
    expect(_odrzuc(page)).to_have_count(0)
    # powod stoi tylko w napisie "odrzucony: ..." (nie drugi raz osobnym akapitem)
    expect(page.get_by_text("za ryzykowne wdrozenie", exact=True)).to_have_count(0)


def test_odrzucony_run_po_ponownym_otwarciu_strony_nadal_pokazuje_powod(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    _pole_powodu(page).fill("niezgodne z planem")
    _odrzuc(page).click()
    expect(page.get_by_text("odrzucony: niezgodne z planem")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("odrzucony: niezgodne z planem")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)
    expect(_zatwierdz(page)).to_have_count(0)


# =====================================================================================
# 4. Decyzja nieprzyjeta przez kokpit: nigdy cisza
# =====================================================================================


def test_decyzja_na_run_ktory_juz_nie_czeka_pokazuje_komunikat_decyzja_nieprzyjeta(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    stan = _opoznij_otwarcie_strumienia(page, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    expect(_zatwierdz(page)).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    _poczekaj_na_otwarcie_strumienia(page, stan)
    # stan zmieniony bocznie, bez zdarzenia: strona nadal pokazuje bramke, kokpit juz nie
    _zmien_stan_runu_bocznie(page, stan, run_id, "running")

    _zatwierdz(page).click()

    alert = page.get_by_role("alert").filter(has_text=re.compile(r"^\s*Decyzja nieprzyjęta:"))
    expect(alert).to_have_text(
        "Decyzja nieprzyjęta: run nie czeka na decyzje czlowieka", timeout=LIMIT_WYNIKU_MS)
    assert _status(run_id) == "running"
    assert _liczba_decyzji(run_id) == 0           # odrzucona decyzja nie zostawia zdarzenia
    # odmowa odswieza widok: strona pokazuje prawdziwy stan runu zamiast przyciskow bramki
    expect(_zatwierdz(page)).to_have_count(0, timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("status: running")).to_be_visible()


def test_odrzucenie_runu_ktory_juz_nie_czeka_pokazuje_komunikat_i_nie_zmienia_stanu_w_bazie(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    stan = _opoznij_otwarcie_strumienia(page, run_id)
    _otworz_run(page, adres_kokpitu, run_id)
    expect(_odrzuc(page)).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    _poczekaj_na_otwarcie_strumienia(page, stan)
    _zmien_stan_runu_bocznie(page, stan, run_id, "running")
    _pole_powodu(page).fill("za pozno")

    _odrzuc(page).click()

    alert = page.get_by_role("alert").filter(has_text=re.compile(r"^\s*Decyzja nieprzyjęta:"))
    expect(alert).to_have_text(
        "Decyzja nieprzyjęta: run nie czeka na decyzje czlowieka", timeout=LIMIT_WYNIKU_MS)
    assert _status(run_id) == "running"
    assert _powod(run_id) is None


# =====================================================================================
# 5. Tresc komunikatu i wysylane zadanie (wartosc, nie obecnosc)
# =====================================================================================

KOMUNIKAT_SIEC = "Brak połączenia z kokpitem."
KROK_CZEKA = {"step_id": KROK_BRAMKI, "agent_id": KROK_BRAMKI, "attempt": 1,
              "status": "waiting", "reason": None}


def _zadanie_decyzji(r) -> bool:
    return r.method == "POST" and r.url.endswith("/decision")


def _czekaj_na(page, warunek, nazwa: str, limit_ms: int = 2 * LIMIT_WYNIKU_MS) -> None:
    """Czekanie z limitem na warunek spelniony przez handlery tras (page.wait_for_timeout pompuje zdarzenia)."""
    odliczanie = limit_ms
    while not warunek():
        assert odliczanie > 0, f"przekroczony limit {limit_ms} ms: {nazwa}"
        page.wait_for_timeout(50)
        odliczanie -= 50


def _wstrzymaj_decyzje(page) -> list:
    """Zadania POST /decision zostaja bez odpowiedzi, az test wywola `route.fulfill`."""
    wstrzymane: list = []
    page.route("**/api/runs/*/decision", lambda r: wstrzymane.append(r))
    return wstrzymane


def test_zatwierdzenie_wysyla_zadanie_z_sama_decyzja_jako_json_nawet_gdy_pole_powodu_jest_wypelnione(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    _pole_powodu(page).fill("tekst odrzucenia")

    with page.expect_request(_zadanie_decyzji, timeout=LIMIT_WYNIKU_MS) as zadanie:
        _zatwierdz(page).click()

    assert zadanie.value.post_data_json == {"decyzja": "approve"}
    assert zadanie.value.headers["content-type"] == "application/json"


def test_odrzucenie_wysyla_powod_bez_spacji_z_brzegow_jako_json(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    _pole_powodu(page).fill("  za ryzykowne wdrozenie  ")

    with page.expect_request(_zadanie_decyzji, timeout=LIMIT_WYNIKU_MS) as zadanie:
        _odrzuc(page).click()

    # backend sam przycina powod, wiec o przycieciu we froncie swiadczy tylko tresc zadania
    assert zadanie.value.post_data_json == {"decyzja": "reject", "powod": "za ryzykowne wdrozenie"}
    assert zadanie.value.headers["content-type"] == "application/json"
    assert zadanie.value.url.endswith(f"/api/runs/{run_id}/decision")


ODPOWIEDZI_BLEDU = {
    "detail_napisem": (409, '{"detail": "run nie czeka na decyzje czlowieka"}',
                       "Decyzja nieprzyjęta: run nie czeka na decyzje czlowieka"),
    "cialo_nie_json": (500, "to nie jest json", "Decyzja nieprzyjęta: błąd 500"),
    "detail_nie_jest_napisem": (422, '{"detail": [{"msg": "zle"}]}', "Decyzja nieprzyjęta: błąd 422"),
    "detail_pusty": (500, '{"detail": ""}', "Decyzja nieprzyjęta: błąd 500"),
    "json_bez_detail": (404, "{}", "Decyzja nieprzyjęta: błąd 404"),
}


@pytest.mark.parametrize(("kod", "cialo", "oczekiwany"), list(ODPOWIEDZI_BLEDU.values()),
                         ids=list(ODPOWIEDZI_BLEDU))
def test_odmowa_kokpitu_pokazuje_dokladny_komunikat_z_detail_albo_kodem_http(
    zalogowana_strona, adres_kokpitu, token_runnera, kod, cialo, oczekiwany
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    page.route("**/api/runs/*/decision", lambda r: r.fulfill(status=kod, body=cialo))

    _zatwierdz(page).click()

    expect(page.get_by_role("alert")).to_have_text(oczekiwany, timeout=LIMIT_WYNIKU_MS)


def test_zerwane_polaczenie_przy_decyzji_pokazuje_komunikat_decyzja_nieprzyjeta_z_brakiem_polaczenia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    page.route("**/api/runs/*/decision", lambda r: r.abort())

    _zatwierdz(page).click()

    expect(page.get_by_role("alert")).to_have_text(
        f"Decyzja nieprzyjęta: {KOMUNIKAT_SIEC}", timeout=LIMIT_WYNIKU_MS)
    assert _status(run_id) == "waiting"


def test_komunikat_o_odmowie_znika_na_poczatku_kolejnej_proby_a_nie_dopiero_z_odpowiedzia(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    zadania: list = []

    def obsluz(r):
        zadania.append(r)
        if len(zadania) == 1:
            r.fulfill(status=500, body='{"detail": "chwilowa awaria"}')

    page.route("**/api/runs/*/decision", obsluz)
    _zatwierdz(page).click()
    expect(page.get_by_role("alert")).to_have_text(
        "Decyzja nieprzyjęta: chwilowa awaria", timeout=LIMIT_WYNIKU_MS)

    _zatwierdz(page).click()

    _czekaj_na(page, lambda: len(zadania) == 2, "drugie zadanie decyzji")
    expect(page.get_by_role("alert")).to_have_count(0)          # druga proba jeszcze bez odpowiedzi
    zadania[1].fulfill(status=500, body='{"detail": "druga awaria"}')
    expect(page.get_by_role("alert")).to_have_text(
        "Decyzja nieprzyjęta: druga awaria", timeout=LIMIT_WYNIKU_MS)


def test_w_trakcie_wysylania_decyzji_oba_przyciski_sa_wylaczone_a_po_odpowiedzi_znowu_aktywne(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    wstrzymane = _wstrzymaj_decyzje(page)
    _pole_powodu(page).fill("powod do odrzucenia")          # Odrzuc wylaczony tylko przez trwajace zadanie

    _zatwierdz(page).click()

    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie decyzji w locie")
    expect(_zatwierdz(page)).to_be_disabled()
    expect(_odrzuc(page)).to_be_disabled()
    wstrzymane[0].fulfill(status=500, body='{"detail": "x"}')
    expect(page.get_by_role("alert")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_zatwierdz(page)).to_be_enabled()
    expect(_odrzuc(page)).to_be_enabled()


def test_drugie_zadanie_decyzji_nie_wychodzi_gdy_pierwsze_jest_w_locie_nawet_przy_wlaczonym_przycisku(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    wstrzymane = _wstrzymaj_decyzje(page)
    _zatwierdz(page).click()
    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie decyzji w locie")

    # zdejmuje atrybut DOM i klika: drugie zadanie blokuje tylko disabled w propsach Reacta
    # (React nie wola onClick wylaczonego przycisku), nie sam atrybut na stronie
    _zatwierdz(page).evaluate("b => { b.disabled = false; b.click(); }")

    page.wait_for_timeout(400)
    assert len(wstrzymane) == 1
    wstrzymane[0].fulfill(status=500, body='{"detail": "x"}')


def test_odpowiedz_na_decyzje_zmienia_widok_od_razu_bez_czekania_na_ponowne_pobranie_runu(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    page.route(f"**/api/runs/{run_id}", lambda r: r.abort())      # GET szczegolow runu nie dziala

    _zatwierdz(page).click()

    expect(page.get_by_text("status: running")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_zatwierdz(page)).to_have_count(0)


def test_pole_powodu_jest_puste_gdy_run_znowu_czeka_na_kolejnej_bramce_po_zatwierdzeniu(
    zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_id = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_id)
    _pole_powodu(page).fill("pozostalosc po pierwszej bramce")
    _zatwierdz(page).click()
    expect(_zatwierdz(page)).to_have_count(0, timeout=LIMIT_WYNIKU_MS)

    _zdarzenie(adres_kokpitu, token_runnera, run_id, KROK_BRAMKI, "step_started")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, KROK_BRAMKI, "step_finished")
    _zdarzenie(adres_kokpitu, token_runnera, run_id, "druga", "step_waiting")

    expect(page.get_by_text("czeka na decyzję: krok druga")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(_pole_powodu(page)).to_have_value("")


def test_waiting_bez_czekajacego_kroku_pokazuje_myslnik_zamiast_nazwy_kroku(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _w_bazie("UPDATE run SET status = 'waiting' WHERE id = :id", {"id": run_id}, zapis=True)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("czeka na decyzję: krok —", exact=True)).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)


def test_odrzucony_run_bez_powodu_pokazuje_samo_odrzucony_bez_dwukropka(
    zalogowana_strona, adres_kokpitu
):
    page = zalogowana_strona
    run_id = _utworz_run_w_toku()
    _w_bazie("UPDATE run SET status = 'rejected' WHERE id = :id", {"id": run_id}, zapis=True)

    _otworz_run(page, adres_kokpitu, run_id)

    expect(page.get_by_text("odrzucony", exact=True)).to_have_count(1, timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text(re.compile(r"odrzucony:"))).to_have_count(0)
    expect(_zatwierdz(page)).to_have_count(0)


# =====================================================================================
# 6. Odpowiedz na decyzje po przejsciu na inny run nie dotyka nowego runu
# =====================================================================================


def _szczegoly_waiting(run_id: str) -> dict:
    return {"id": run_id, "workflow": "szkielet", "status": "waiting", "runner_kind": "local",
            "reason": None, "events": [], "steps": [KROK_CZEKA]}


def _przejdz_na_run_b(page, run_b: str) -> None:
    """Uruchom (odpowiedz podstawiona) przelacza widok na run B: czeka na bramce.

    Pierwsze pobranie runu B daje `waiting`; kazde kolejne zostaje bez odpowiedzi, wiec
    zmiana statusu B moze pochodzic tylko z odpowiedzi na decyzje runu A."""
    page.route("**/api/runs", lambda r: r.fulfill(json={"run_id": run_b})
               if r.request.method == "POST" else r.continue_())
    pobrania: list = []

    def szczegoly_b(r):
        pobrania.append(r)
        if len(pobrania) == 1:
            r.fulfill(json=_szczegoly_waiting(run_b))

    page.route(f"**/api/runs/{run_b}", szczegoly_b)
    przycisk = page.get_by_role("button", name="Uruchom", exact=True)
    expect(przycisk).to_be_enabled(timeout=2 * LIMIT_WYNIKU_MS)
    przycisk.click()
    expect(page.get_by_text(f"run {run_b}")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text(f"czeka na decyzję: krok {KROK_BRAMKI}")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS)


def _otworz_run_a_z_decyzja_w_locie(page, adres, token):
    run_a = _run_czekajacy_na_bramce(adres, token)
    _otworz_run(page, adres, run_a)
    wstrzymane = _wstrzymaj_decyzje(page)
    _zatwierdz(page).click()
    _czekaj_na(page, lambda: len(wstrzymane) == 1, "zadanie decyzji runu A w locie")
    return run_a, wstrzymane


ALERT_DECYZJI = re.compile(r"Decyzja nieprzyjęta")


def test_odmowa_na_decyzje_runu_a_po_przejsciu_na_run_b_nie_pokazuje_bledu_w_runie_b(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a, wstrzymane = _otworz_run_a_z_decyzja_w_locie(page, adres_kokpitu, token_runnera)
    _przejdz_na_run_b(page, str(uuid.uuid4()))

    wstrzymane[0].fulfill(status=500, body='{"detail": "blad runu A"}')

    page.wait_for_timeout(500)
    expect(page.get_by_role("alert").filter(has_text=ALERT_DECYZJI)).to_have_count(0)


def test_zerwane_polaczenie_przy_decyzji_runu_a_po_przejsciu_na_run_b_nie_pokazuje_bledu_w_runie_b(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a, wstrzymane = _otworz_run_a_z_decyzja_w_locie(page, adres_kokpitu, token_runnera)
    _przejdz_na_run_b(page, str(uuid.uuid4()))

    wstrzymane[0].abort()

    page.wait_for_timeout(500)
    expect(page.get_by_role("alert").filter(has_text=ALERT_DECYZJI)).to_have_count(0)


def test_przyjeta_decyzja_runu_a_po_przejsciu_na_run_b_nie_zmienia_stanu_runu_b(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a, wstrzymane = _otworz_run_a_z_decyzja_w_locie(page, adres_kokpitu, token_runnera)
    _przejdz_na_run_b(page, str(uuid.uuid4()))

    wstrzymane[0].fulfill(json={"run_id": run_a, "status": "running",
                                "step_id": KROK_BRAMKI, "reason": None})

    page.wait_for_timeout(500)
    expect(_zatwierdz(page)).to_be_visible()                      # run B nadal czeka na bramce
    expect(page.get_by_text("status: waiting")).to_be_visible()


def test_blad_decyzji_runu_a_nie_zostaje_na_ekranie_po_przejsciu_na_run_b(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_a)
    page.route("**/api/runs/*/decision", lambda r: r.fulfill(status=500, body='{"detail": "blad A"}'))
    _zatwierdz(page).click()
    expect(page.get_by_role("alert")).to_have_text(
        "Decyzja nieprzyjęta: blad A", timeout=LIMIT_WYNIKU_MS)

    _przejdz_na_run_b(page, str(uuid.uuid4()))

    expect(page.get_by_role("alert").filter(has_text=ALERT_DECYZJI)).to_have_count(0)


def test_powod_wpisany_w_runie_a_nie_przechodzi_do_pola_powodu_runu_b(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a = _run_czekajacy_na_bramce(adres_kokpitu, token_runnera)
    _otworz_run(page, adres_kokpitu, run_a)
    _pole_powodu(page).fill("powod z runu A")

    _przejdz_na_run_b(page, str(uuid.uuid4()))

    expect(_pole_powodu(page)).to_have_value("")
    expect(_odrzuc(page)).to_be_disabled()


def test_decyzja_w_locie_w_runie_a_nie_blokuje_przyciskow_runu_b(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a, wstrzymane = _otworz_run_a_z_decyzja_w_locie(page, adres_kokpitu, token_runnera)

    _przejdz_na_run_b(page, str(uuid.uuid4()))

    expect(_zatwierdz(page)).to_be_enabled()
    wstrzymane[0].fulfill(status=500, body='{"detail": "x"}')


def test_spozniona_odpowiedz_runu_a_nie_odblokowuje_przyciskow_runu_b_gdy_jego_wlasna_decyzja_trwa(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    run_a, wstrzymane = _otworz_run_a_z_decyzja_w_locie(page, adres_kokpitu, token_runnera)
    _przejdz_na_run_b(page, str(uuid.uuid4()))
    _zatwierdz(page).click()                                     # decyzja runu B w locie
    _czekaj_na(page, lambda: len(wstrzymane) == 2, "zadanie decyzji runu B w locie")
    expect(_zatwierdz(page)).to_be_disabled()

    wstrzymane[0].fulfill(status=500, body='{"detail": "blad runu A"}')

    page.wait_for_timeout(500)
    expect(_zatwierdz(page)).to_be_disabled()                    # nadal czeka na wlasna odpowiedz
    wstrzymane[1].fulfill(status=500, body='{"detail": "blad runu B"}')


# =====================================================================================
# 7. Build frontu zalezy od kontraktu decyzji
# =====================================================================================


@pytest.mark.parametrize(
    ("schemat", "pole"),
    [
        ("DecyzjaIn", "decyzja"),          # WidokRunu.tsx: wyslijDecyzje({ decyzja: "approve" })
        ("DecyzjaPrzyjeta", "status"),     # WidokRunu.tsx: setStatus(przyjeta.status)
    ],
)
def test_pole_usuniete_z_kontraktu_decyzji_oblewa_kompilacje_frontu(
    tmp_path, schemat: str, pole: str
) -> None:
    from test_typy_frontu_z_kontraktu import _kopia_frontu, _usun_pole, _zbuduj

    kopia = _kopia_frontu(tmp_path)
    _usun_pole(kopia, schemat, pole)
    kod, tekst = _zbuduj(kopia, tmp_path / "dist")
    assert kod != 0, f"build przeszedl mimo braku pola {schemat}.{pole}"
    assert "error TS" in tekst, tekst

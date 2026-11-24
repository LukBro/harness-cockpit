"""Runner: wynik komendy trafia do kokpitu jako zdarzenie końcowe kroku.

Transport HTTP jest przechwycony (`httpx.MockTransport`), a komendy to prawdziwe
procesy — sprawdzamy produkcyjny kod `Runner.run_command`, nie jego kopię.
Asercje dotyczą rodzaju zdarzenia (`kind`); pola pomiarowe (`duration_ms`, `cost_usd`,
`cost_source`) sprawdzają osobne testy z sekcji HAR-35.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

import runner.client as runner_client
from procesy import (
    czekaj,
    czekaj_bez_anulowania,
    czekaj_na_warunek,
    czy_zyje,
    grupa_istnieje,
    grupa_umarla,
    zadanie_tla,
    zapamietaj_proces,
)
from runner.bufor import BladBufora, BrakMiejsca, Bufor
from runner.client import NiewaznePoleZdarzenia, Runner, TokenOdrzucony

RUN_ID = "run-1"
STEP_ID = "krok-1"


@pytest.fixture(autouse=True)
def katalog_runow_runnera(tmp_path, monkeypatch):
    """`cancel_run` zamyka checkpoint runu w katalogu runow runnera (HAR-126): testy czytaja
    i pisza tylko w katalogu testu, nigdy w prawdziwym `~/.cockpit/runs`."""
    katalog = tmp_path / "katalog-runow-runnera"
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(katalog))
    return katalog


class Odczekania:
    """Atrapa odczekania (HAR-45): nie śpi, zapisuje argumenty i po `zatrzymaj_po`
    wywołaniach zatrzymuje runnera, dzięki czemu pętla ponawiania zawsze się kończy."""

    def __init__(self, zatrzymaj_po: int | None = None) -> None:
        self.argumenty: list[float] = []
        self.zatrzymaj_po = zatrzymaj_po
        self.runner: Runner | None = None

    async def __call__(self, sekundy: float) -> None:
        self.argumenty.append(sekundy)
        if self.zatrzymaj_po is not None and len(self.argumenty) >= self.zatrzymaj_po:
            await self.runner.stop()


def _runner(
    base_url: str = "http://kokpit.test", transport=None, zatrzymaj_po: int | None = None,
    name: str | None = None,
) -> tuple[Runner, Odczekania]:
    odczekania = Odczekania(zatrzymaj_po)
    nazwane = {} if name is None else {"name": name}
    runner = Runner(base_url, TOKEN, transport=transport, odczekaj=odczekania, **nazwane)
    odczekania.runner = runner
    return runner, odczekania


async def _uruchom(command: list[str], status: int = 200) -> list[httpx.Request]:
    """Uruchamia komendę przez Runnera i zwraca żądania wysłane do kokpitu.
    `status` to kod, którym kokpit odpowiada na każde zdarzenie. Odczekanie przed
    ponowieniem zatrzymuje runnera, więc odpowiedź 5xx nie zapętla testu."""
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(status)

    runner, _ = _runner(zatrzymaj_po=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await runner.run_command(client, RUN_ID, STEP_ID, command)
    return wyslane


def _zdarzenia(zadania: list[httpx.Request]) -> list[dict]:
    return [json.loads(z.content) for z in zadania]


async def test_komenda_z_niezerowym_kodem_wyjscia_konczy_krok_jako_nieudany():
    zadania = await _uruchom(["sh", "-c", "exit 3"])
    zdarzenia = _zdarzenia(zadania)

    koncowe = zdarzenia[-2]       # ostatnie jest zdarzenie konca runu (HAR-78)
    assert koncowe["kind"] == "step_failed"
    assert koncowe["text"] == "kod wyjscia 3"
    assert koncowe["run_id"] == RUN_ID
    assert koncowe["step_id"] == STEP_ID
    assert all(z["kind"] != "step_finished" for z in zdarzenia)


async def test_komenda_z_kodem_zero_konczy_krok_jako_zakonczony():
    zadania = await _uruchom(["sh", "-c", "exit 0"])
    zdarzenia = _zdarzenia(zadania)

    koncowe = zdarzenia[-2]       # ostatnie jest zdarzenie konca runu (HAR-78)
    assert koncowe["kind"] == "step_finished"
    assert koncowe["text"] == "kod wyjscia 0"
    assert koncowe["run_id"] == RUN_ID
    assert koncowe["step_id"] == STEP_ID
    assert all(z["kind"] != "step_failed" for z in zdarzenia)


async def test_nieudana_komenda_najpierw_zglasza_swoj_wynik_a_potem_koniec_runu():
    zadania = await _uruchom(["sh", "-c", "echo blad; exit 2"])
    zdarzenia = _zdarzenia(zadania)

    assert [z["kind"] for z in zdarzenia] == ["output", "step_failed", "run_finished"]
    assert zdarzenia[0]["text"] == "blad"
    assert zdarzenia[1]["text"] == "kod wyjscia 2"


# --- HAR-78: koniec runu wysyla szkielet, nie kokpit ---

@pytest.mark.parametrize("komenda,krok", [(["sh", "-c", "exit 0"], "step_finished"),
                                          (["sh", "-c", "exit 3"], "step_failed")])
async def test_po_zakonczeniu_kroku_runner_zglasza_koniec_runu_z_tym_samym_krokiem(komenda, krok):
    zdarzenia = _zdarzenia(await _uruchom(komenda))

    assert [z["kind"] for z in zdarzenia] == [krok, "run_finished"]
    koniec = zdarzenia[-1]
    assert koniec["run_id"] == RUN_ID
    assert koniec["step_id"] == STEP_ID
    assert koniec["text"] == ""
    assert koniec["cost_usd"] is None
    assert koniec["cost_source"] == "unavailable"
    assert koniec["event_id"] != zdarzenia[0]["event_id"]


async def test_koniec_runu_nie_niesie_liczby_wyslanych_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; exit 1"]))

    assert zdarzenia[-2]["wyslane_output"] == 1   # kontrast: krok niesie licznik
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1].get("wyslane_output") is None


async def test_krok_przerwany_z_powodu_braku_miejsca_nie_zglasza_konca_runu(monkeypatch):
    monkeypatch.setattr(Runner, "_powod_nie_startu", lambda self: "brak miejsca: test")
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"] == "brak miejsca: test"


async def test_zdarzenia_runnera_trafiaja_do_kokpitu_z_tokenem_runnera():
    zadania = await _uruchom(["sh", "-c", "exit 3"])

    assert zadania, "runner nie wysłał żadnego zdarzenia"
    for zadanie in zadania:
        assert zadanie.method == "POST"
        assert zadanie.url.path == "/api/events"
        assert zadanie.headers["Authorization"] == "Bearer tajny-token"
        assert json.loads(zadanie.content)["event_id"]


async def _wyslij_zdarzenie(status: int) -> bool:
    """Wysyła jedno zdarzenie; odpowiedź 5xx kończy się zatrzymaniem runnera (zdarzenie
    zostaje w buforze), więc wynik to `False` także wtedy."""
    runner, _ = _runner(zatrzymaj_po=1)
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    async with httpx.AsyncClient(transport=transport) as client:
        return await asyncio.wait_for(
            runner.send_event(client, run_id=RUN_ID, step_id=STEP_ID, kind="output", text="x"), 10)


@pytest.mark.parametrize("status", [400, 404])
async def test_zdarzenie_odrzucone_kodem_4xx_jest_widoczne_w_logu_runnera_jako_utracone(
    status, capsys,
):
    await _wyslij_zdarzenie(status)

    assert capsys.readouterr().err.splitlines() == [
        f"zdarzenie utracone: kind=output run_id={RUN_ID} http={status}"]


async def test_awaria_kokpitu_przy_wysylce_zdarzenia_jest_widoczna_w_logu_runnera(capsys):
    await _wyslij_zdarzenie(500)

    wpisy = capsys.readouterr().err.splitlines()
    assert len(wpisy) == 1
    assert re.fullmatch(r"kokpit niedostepny \(http=500\) - ponawiam za 1(\.0)? s", wpisy[0])


@pytest.mark.parametrize("status", [400, 500])
async def test_log_runnera_o_odrzuconym_zdarzeniu_nie_zawiera_tokenu(status, capsys):
    await _uruchom(["sh", "-c", "echo tajne; exit 1"], status=status)

    wyjscie = capsys.readouterr()
    assert wyjscie.err  # log istnieje, wiec brak tokenu cos znaczy
    assert "tajny-token" not in wyjscie.err
    assert "tajny-token" not in wyjscie.out


async def test_odrzucony_token_przy_zdarzeniu_kroku_przerywa_krok_bledem_tokenu(capsys):
    with pytest.raises(TokenOdrzucony):
        await _uruchom(["sh", "-c", "echo tajne; exit 1"], status=401)

    wyjscie = capsys.readouterr()
    assert "tajny-token" not in wyjscie.err
    assert "tajny-token" not in wyjscie.out


async def test_przyjete_zdarzenie_nie_zostawia_sladu_w_logu_runnera(capsys):
    assert await _wyslij_zdarzenie(200) is True
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [400, 404, 500])
async def test_wysylka_zdarzenia_utraconego_albo_niedoreczonego_zwraca_porazke(status):
    assert await _wyslij_zdarzenie(status) is False


async def test_zdarzenie_niedoreczone_z_powodu_awarii_kokpitu_zostaje_w_buforze():
    await _wyslij_zdarzenie(500)

    (w_buforze,) = Bufor().niepotwierdzone()
    assert (w_buforze["run_id"], w_buforze["kind"], w_buforze["text"]) == (RUN_ID, "output", "x")


async def test_zdarzenie_utracone_przez_odpowiedz_4xx_nie_zostaje_w_buforze():
    await _wyslij_zdarzenie(400)

    assert Bufor().niepotwierdzone() == []


async def test_wysylka_zdarzenia_z_kodem_2xx_innym_niz_200_zwraca_sukces(capsys):
    assert await _wyslij_zdarzenie(204) is True
    assert capsys.readouterr().err == ""


async def test_log_utraconego_zdarzenia_kroku_podaje_jego_rodzaj_i_brak_konca_runu(capsys):
    await _uruchom(["sh", "-c", "exit 3"], status=400)

    assert capsys.readouterr().err.splitlines() == [
        f"zdarzenie utracone: kind=step_failed run_id={RUN_ID} http=400",
        f"koniec runu niewyslany: run_id={RUN_ID} - "
        "zdarzenie kroku step_failed nie zostalo przyjete"]


async def _uruchom_z_odrzuconym_krokiem(
    command: list[str], odrzucany: set[str], status: int, **runner_kw
) -> list[str]:
    """Kokpit odpowiada `status` na zdarzenia wskazanych rodzajow, a 200 na pozostale.
    Zwraca rodzaje wszystkich wyslanych zdarzen w kolejnosci prob."""
    rodzaje: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = json.loads(request.content)["kind"]
        rodzaje.append(kind)
        return httpx.Response(status if kind in odrzucany else 200)

    runner, _ = _runner()
    runner._maks_prob_bledu_serwera = runner_kw.get("maks", runner._maks_prob_bledu_serwera)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.wait_for(runner.run_command(client, RUN_ID, STEP_ID, command), 20)
    return rodzaje


@pytest.mark.parametrize("komenda,krok", [("exit 3", "step_failed"), ("true", "step_finished")])
async def test_odrzucone_zdarzenie_kroku_nie_pozwala_zglosic_konca_runu(komenda, krok):
    rodzaje = await _uruchom_z_odrzuconym_krokiem(
        ["sh", "-c", komenda], {"step_failed", "step_finished"}, 400)

    assert "run_finished" not in rodzaje
    assert krok in rodzaje


async def test_zdarzenie_kroku_odrzucane_bledem_serwera_do_limitu_prob_nie_pozwala_zglosic_konca_runu(
    capsys,
):
    rodzaje = await _uruchom_z_odrzuconym_krokiem(
        ["sh", "-c", "exit 3"], {"step_failed"}, 500, maks=3)

    assert rodzaje.count("step_failed") == 3
    assert "run_finished" not in rodzaje
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"koniec runu niewyslany: run_id={RUN_ID} - "
        "zdarzenie kroku step_failed nie zostalo przyjete")


async def test_przyjete_zdarzenie_kroku_pozwala_zglosic_koniec_runu_mimo_odrzuconego_output():
    rodzaje = await _uruchom_z_odrzuconym_krokiem(
        ["sh", "-c", "echo x"], {"output"}, 400)

    assert rodzaje == ["output", "step_finished", "run_finished"]


async def test_zdarzenie_koncowe_niesie_liczbe_linii_output_ktore_runner_wyslal():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b"]))

    assert [z["kind"] for z in zdarzenia] == ["output", "output", "step_finished", "run_finished"]
    assert zdarzenia[-2]["wyslane_output"] == 2


async def test_zdarzenie_koncowe_liczy_tez_nieudane_proby_wyslania_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b; exit 1"], status=400))

    # kokpit odrzucil zdarzenie kroku, wiec konca runu nie ma: krok jest ostatni
    assert [z["kind"] for z in zdarzenia] == ["output", "output", "step_failed"]
    assert zdarzenia[-1]["wyslane_output"] == 2


async def test_komenda_bez_wyjscia_zglasza_zero_wyslanych_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    assert zdarzenia[-2]["wyslane_output"] == 0


# --- HAR-26: strumien runnera, zamkniecie przez kokpit i odrzucony token ---

TOKEN = "tajny-token"
POLACZONO = b'data: {"type": "polaczono"}\n\n'


def _kokpit(stream_status: int = 200, stream_body: bytes = b"", register_status: int = 200):
    """Atrapa kokpitu: zwraca (transport, odwiedzone sciezki). Strumien konczy sie po `stream_body`."""
    odwiedzone: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        odwiedzone.append(request.url.path)
        if request.url.path == "/api/runners/register":
            if register_status != 200:
                return httpx.Response(register_status)
            return httpx.Response(
                200, json={"runner_id": "r1", "tenant_id": "t1", "heartbeat_co_s": 15})
        if request.url.path == "/api/runners/stream":
            return httpx.Response(
                stream_status, content=stream_body,
                headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    return httpx.MockTransport(handler), odwiedzone


async def _nasluch(runner: Runner) -> None:
    """`listen()` z twardym limitem czasu; po `stop()` z atrapy odczekania wraca bez wyjątku."""
    assert await asyncio.wait_for(runner.listen(), 10) is None


async def test_zamkniecie_strumienia_przez_kokpit_nie_konczy_nasluchiwania_runner_laczy_sie_ponownie(
    capsys,
):
    transport, odwiedzone = _kokpit(stream_body=POLACZONO)
    runner, odczekania = _runner(transport=transport, zatrzymaj_po=2)

    await _nasluch(runner)

    assert odwiedzone == ["/api/runners/register", "/api/runners/stream"] * 2
    assert len(odczekania.argumenty) == 2
    assert capsys.readouterr().err.count("strumien otwarty") == 2


async def test_przerwany_strumien_jest_widoczny_w_logu_z_przyczyna_i_czasem_do_ponowienia(capsys):
    transport, _ = _kokpit(stream_body=POLACZONO)
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)

    await _nasluch(runner)

    wpisy = [w for w in capsys.readouterr().err.splitlines() if "przerwany" in w]
    assert len(wpisy) == 1
    assert re.fullmatch(
        r"strumien polecen przerwany \(.+\) - lacze ponownie za 1(\.0)? s", wpisy[0]), wpisy[0]


async def test_odczekanie_przed_ponownym_laczeniem_zaczyna_sie_od_nowa_po_kazdym_otwarciu_strumienia():
    transport, _ = _kokpit(stream_body=POLACZONO)
    runner, odczekania = _runner(transport=transport, zatrzymaj_po=3)

    await _nasluch(runner)

    assert odczekania.argumenty == [1.0, 1.0, 1.0]


async def test_odczekanie_przed_ponownym_laczeniem_rosnie_gdy_strumien_nigdy_sie_nie_otwiera():
    transport, _ = _kokpit(stream_body=b'data: {"type": "inne"}\n\n')
    runner, odczekania = _runner(transport=transport, zatrzymaj_po=4)

    await _nasluch(runner)

    assert odczekania.argumenty == [1.0, 2.0, 4.0, 8.0]


async def test_strumien_z_innym_poleceniem_niz_polaczono_nie_jest_zglaszany_jako_otwarty(capsys):
    transport, _ = _kokpit(stream_body=b'data: {"type": "inne"}\n\n')
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)

    await _nasluch(runner)

    assert "strumien otwarty" not in capsys.readouterr().err


async def test_runner_nie_zglasza_otwartego_strumienia_po_samej_odpowiedzi_200_bez_polecenia(
    capsys,
):
    transport, _ = _kokpit(stream_body=b"")
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)

    await _nasluch(runner)

    assert "strumien otwarty" not in capsys.readouterr().err


async def test_odrzucony_token_przy_zameldowaniu_konczy_nasluchiwanie_bez_ponawiania():
    transport, odwiedzone = _kokpit(register_status=401)
    runner, odczekania = _runner(transport=transport)

    with pytest.raises(TokenOdrzucony):
        await asyncio.wait_for(runner.listen(), 10)

    assert odwiedzone == ["/api/runners/register"]
    assert odczekania.argumenty == []


async def test_odrzucony_token_przy_otwieraniu_strumienia_konczy_nasluchiwanie_bez_ponawiania():
    transport, odwiedzone = _kokpit(stream_status=401)
    runner, odczekania = _runner(transport=transport)

    with pytest.raises(TokenOdrzucony):
        await asyncio.wait_for(runner.listen(), 10)

    assert odwiedzone == ["/api/runners/register", "/api/runners/stream"]
    assert odczekania.argumenty == []


# --- HAR-45: kokpit znika i wraca ---

class KokpitZawodny(httpx.AsyncBaseTransport):
    """Pierwsze `awarii` żądań kończy wyjątkiem transportu, potem odpowiada jak normalny kokpit."""

    def __init__(self, awarii: int, wyjatek: Exception | None = None, stream_body=POLACZONO):
        self.pozostalo = awarii
        self.wyjatek = wyjatek or httpx.ConnectError("kokpit nie odpowiada")
        self.stream_body = stream_body
        self.sciezki: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sciezki.append(request.url.path)
        if self.pozostalo > 0:
            self.pozostalo -= 1
            raise self.wyjatek
        if request.url.path == "/api/runners/register":
            return httpx.Response(
                200, json={"runner_id": "r1", "tenant_id": "t1", "heartbeat_co_s": 15})
        return httpx.Response(
            200, content=self.stream_body, headers={"content-type": "text/event-stream"})


async def test_kokpit_niedostepny_przy_zameldowaniu_runner_czeka_i_probuje_ponownie_z_rosnacym_odczekaniem():
    kokpit = KokpitZawodny(awarii=3)
    runner, odczekania = _runner(transport=kokpit, zatrzymaj_po=4)

    await _nasluch(runner)

    assert odczekania.argumenty[:3] == [1.0, 2.0, 4.0]
    assert kokpit.sciezki[:5] == ["/api/runners/register"] * 3 + [
        "/api/runners/register", "/api/runners/stream"]


async def test_blad_polaczenia_przy_otwieraniu_strumienia_nie_konczy_runnera(capsys):
    class RozlaczonyStrumien(KokpitZawodny):
        async def handle_async_request(self, request):
            if request.url.path == "/api/runners/stream" and not self.sciezki.count(
                    "/api/runners/stream"):
                self.sciezki.append(request.url.path)
                raise httpx.ReadError("polaczenie zerwane Bearer tajny-token")
            return await super().handle_async_request(request)

    kokpit = RozlaczonyStrumien(awarii=0)
    runner, odczekania = _runner(transport=kokpit, zatrzymaj_po=2)

    await _nasluch(runner)

    assert kokpit.sciezki.count("/api/runners/register") == 2
    assert odczekania.argumenty == [1.0, 1.0]
    err = capsys.readouterr().err
    assert "ReadError" in err
    assert "tajny-token" not in err and "zerwane" not in err


@pytest.mark.parametrize(
    ("rejestracja", "strumien"),
    [(500, 200), (503, 200), (200, 502)],
    ids=["rejestracja_500", "rejestracja_503", "strumien_502"],
)
async def test_blad_serwera_kokpitu_przy_zameldowaniu_albo_strumieniu_jest_ponawiany_z_odczekaniem(
    rejestracja, strumien, capsys,
):
    """Awaria serwera nie jest mylona z odrzuconym tokenem: `listen()` wraca bez wyjątku
    (gdyby poleciał `TokenOdrzucony` albo `HTTPStatusError`, `_nasluch` by oblał), a na
    odpowiedzi 5xx strumień nigdy nie jest zgłoszony jako otwarty."""
    transport, odwiedzone = _kokpit(
        stream_status=strumien, stream_body=POLACZONO, register_status=rejestracja)
    runner, odczekania = _runner(transport=transport, zatrzymaj_po=3)

    await _nasluch(runner)

    assert odczekania.argumenty == [1.0, 2.0, 4.0]
    assert odwiedzone.count("/api/runners/register") == 3
    assert "strumien otwarty" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("rejestracja", "strumien"),
    [(403, 200), (404, 200), (200, 403)],
    ids=["rejestracja_403", "rejestracja_404", "strumien_403"],
)
async def test_odpowiedz_4xx_inna_niz_401_przy_zameldowaniu_albo_strumieniu_nie_jest_ponawiana_ani_mylona_z_tokenem(
    rejestracja, strumien,
):
    transport, odwiedzone = _kokpit(
        stream_status=strumien, stream_body=POLACZONO, register_status=rejestracja)
    runner, odczekania = _runner(transport=transport)

    with pytest.raises(httpx.HTTPStatusError) as wyjatek:
        await asyncio.wait_for(runner.listen(), 10)

    assert not isinstance(wyjatek.value, TokenOdrzucony)
    assert wyjatek.value.response.status_code == (rejestracja if rejestracja != 200 else strumien)
    assert odczekania.argumenty == []
    assert odwiedzone.count("/api/runners/register") == 1


async def test_przerwanie_strumienia_przez_kokpit_ma_w_logu_dokladna_przyczyne(capsys):
    transport, _ = _kokpit(stream_body=POLACZONO)
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)

    await _nasluch(runner)

    assert capsys.readouterr().err.splitlines() == [
        "strumien otwarty",
        "strumien polecen przerwany (kokpit zamknal strumien) - lacze ponownie za 1.0 s"]


async def test_awaria_serwera_kokpitu_ma_w_logu_dokladna_przyczyne_z_kodem_http(capsys):
    transport, _ = _kokpit(register_status=502)
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)

    await _nasluch(runner)

    assert capsys.readouterr().err.splitlines() == [
        "strumien polecen przerwany (http=502) - lacze ponownie za 1.0 s"]


async def test_blad_polaczenia_ma_w_logu_dokladna_przyczyne_z_nazwa_klasy_wyjatku(capsys):
    runner, _ = _runner(transport=KokpitZawodny(awarii=1), zatrzymaj_po=1)

    await _nasluch(runner)

    assert capsys.readouterr().err.splitlines() == [
        "strumien polecen przerwany (ConnectError) - lacze ponownie za 1.0 s"]


async def test_runner_z_wlasnym_buforem_trzyma_niewyslane_zdarzenia_w_nim_a_nie_w_domyslnym(
    tmp_path,
):
    wlasny = Bufor(tmp_path / "wlasny-katalog")
    odczekania = Odczekania(zatrzymaj_po=1)
    runner = Runner("http://kokpit.test", TOKEN, bufor=wlasny, odczekaj=odczekania)
    odczekania.runner = runner
    transport = httpx.MockTransport(lambda request: httpx.Response(500))

    async with httpx.AsyncClient(transport=transport) as client:
        assert await asyncio.wait_for(
            runner.send_event(client, run_id=RUN_ID, step_id=STEP_ID, kind="output", text="x"), 10) is False

    assert [z["text"] for z in wlasny.niepotwierdzone()] == ["x"]
    assert Bufor().niepotwierdzone() == []          # katalog domyslny (COCKPIT_STATE_DIR) pusty


async def test_domyslne_odczekanie_wraca_po_zadanym_czasie_bez_stop():
    runner = Runner("http://kokpit.test", TOKEN)

    await asyncio.wait_for(runner._czekaj(0.01), 2)      # TimeoutError = czeka bez konca


def _wait_for_polykajacy_anulowanie():
    """`asyncio.wait_for` z Pythona 3.11 w wyscigu: gdy anulowanie trafia w chwile konca
    czekania wewnetrznego, `CancelledError` ginie, a wolajacy dostaje zwykly wynik
    (naprawione w 3.12). Lokalnie jest nowszy Python, wiec to zachowanie symulujemy."""
    prawdziwy = asyncio.wait_for

    async def wait_for(aw, timeout):
        try:
            return await prawdziwy(aw, timeout)
        except asyncio.CancelledError:
            return None

    return wait_for


async def test_anulowanie_domyslnego_odczekania_dochodzi_do_wolajacego_takze_gdy_biblioteka_polyka_anulowanie(
    monkeypatch,
):
    """HAR-224: odczekanie nie moze polknac anulowania. Gdy `wait_for` je polknie (3.11, symulowane
    podmiana), zadanie runu stoi w odczekaniu `dosylaj`, wychodzi przez `_zatrzymany` zamiast
    przez anulowanie, workflow biegnie dalej, a `stop()` wisi w `_anuluj_runy` (`asyncio.wait`
    bez limitu). Poza tym jednym `cancel()` test niczego nie anuluje - sprawdza, co z nim zrobi `_czekaj`."""
    runner = Runner("http://kokpit.test", TOKEN)
    zadanie = asyncio.ensure_future(runner._czekaj(30))
    try:
        with monkeypatch.context() as m:
            m.setattr(asyncio, "wait_for", _wait_for_polykajacy_anulowanie())
            await asyncio.sleep(0.05)                    # odczekanie trwa
            zadanie.cancel()
            zakonczone, _ = await asyncio.wait({zadanie}, timeout=2)
        assert zadanie in zakonczone, "odczekanie przezylo anulowanie"
        assert zadanie.cancelled()
    finally:
        runner._zatrzymany.set()
        zadanie.cancel()
        await asyncio.wait({zadanie}, timeout=2)


async def test_anulowanie_odczekania_w_tym_samym_kroku_co_ustawienie_flagi_zatrzymania_dochodzi_do_wolajacego():
    """Odtwarza warunek wyscigu z `stop()` (client.py): flaga `_zatrzymany` i `cancel()` w tym
    samym kroku petli, bez `await` pomiedzy - czekanie wewnetrzne konczy sie razem z anulowaniem.
    Prawdziwy `_czekaj`, bez podmiany `wait_for`. Na Pythonie 3.12+ jest zielony takze na starym
    kodzie; dowod oblewania daje dopiero 3.11, gdzie `wait_for` gubi wtedy `CancelledError`."""
    runner = Runner("http://kokpit.test", TOKEN)
    zadanie = asyncio.ensure_future(runner._czekaj(30))
    try:
        await asyncio.sleep(0.05)                        # odczekanie trwa
        runner._zatrzymany.set()                         # kolejnosc jak w stop(): flaga, potem anulowanie
        zadanie.cancel()
        zakonczone, _ = await asyncio.wait({zadanie}, timeout=2)
        assert zadanie in zakonczone, "odczekanie nie skonczylo sie po stop()"
        assert zadanie.cancelled(), "anulowanie odczekania zostalo polkniete"
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie}, timeout=2)


async def test_domyslne_odczekanie_pozwala_dokonczyc_ponowienie_po_awarii_kokpitu():
    odpowiedzi = [500, 200]
    transport = httpx.MockTransport(lambda request: httpx.Response(odpowiedzi.pop(0)))
    runner = Runner("http://kokpit.test", TOKEN)
    runner._odczekaj = lambda sekundy: runner._czekaj(0.01)      # prawdziwe, tylko krótsze

    async with httpx.AsyncClient(transport=transport) as client:
        przyjete = await asyncio.wait_for(
            runner.send_event(client, run_id=RUN_ID, step_id=STEP_ID, kind="output", text="x"), 5)

    assert przyjete is True


async def test_stop_w_trakcie_odczekiwania_przed_ponownym_laczeniem_konczy_nasluchiwanie_bez_bledu(
    capsys,
):
    transport, odwiedzone = _kokpit(stream_body=POLACZONO)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)   # prawdziwe odczekanie
    log = ""
    async with zadanie_tla(runner.listen(), "nasluch runnera") as nasluch:
        for _ in range(300):
            log += capsys.readouterr().err
            if "lacze ponownie" in log:
                break
            await asyncio.sleep(0.01)
        assert "lacze ponownie" in log, "runner nie zaczal odczekiwania przed ponownym laczeniem"
        await czekaj(runner.stop(), 10, "stop() runnera")
        # Bez anulowania: nasluch ma skonczyc sie sam po stop(), anulowanie by to zamaskowalo.
        await czekaj_bez_anulowania(
            nasluch, 3, "nasluch nie skonczyl sie po stop() w trakcie odczekiwania"
        )

    assert not nasluch.cancelled()
    assert nasluch.exception() is None
    assert odwiedzone == ["/api/runners/register", "/api/runners/stream"]


def _uruchom_main(monkeypatch, wyjatek: str) -> SystemExit:
    """Uruchamia prawdziwe `main()`; `listen` konczy sie wskazanym wyjatkiem dziedziny."""
    import runner.client as klient
    from runner.__main__ import main

    async def listen(self):
        raise getattr(klient, wyjatek)()

    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setattr(Runner, "listen", listen)
    with pytest.raises(SystemExit) as wyjscie:
        main()
    return wyjscie.value


@pytest.mark.parametrize("sygnal", ["SIGTERM", "SIGINT"])
def test_zamkniecie_strumienia_przez_kokpit_nie_konczy_procesu_runnera_ten_laczy_sie_ponownie(
    monkeypatch, sygnal,
):
    """Prawdziwe `main()` i prawdziwy `Runner`; kokpit zamyka strumień za każdym razem.
    Proces kończy się dopiero sygnałem (kod 0), po dwóch kolejnych połączeniach. Sygnał idzie
    do własnego procesu testu, więc siatka bezpieczeństwa: no-op handlery na czas testu."""
    import os
    import signal

    import runner.__main__ as modul

    numer = getattr(signal, sygnal)
    transport, odwiedzone = _kokpit(stream_body=POLACZONO)
    odczekania: list[float] = []

    async def odczekaj(sekundy: float) -> None:
        odczekania.append(sekundy)
        if len(odczekania) == 2:
            os.kill(os.getpid(), numer)
        await asyncio.sleep(0.05)       # stop() z obsługi sygnału zdąży ruszyć

    monkeypatch.setattr(
        modul, "Runner",
        lambda base, token: Runner(base, token, transport=transport, odczekaj=odczekaj))
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("COCKPIT_URL", "http://kokpit.test")
    stare = {s: signal.signal(s, lambda *a: None) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            modul.main()
            kod = None
        except SystemExit as wyjscie:
            kod = wyjscie.code
    finally:
        for s, obsluga in stare.items():
            signal.signal(s, obsluga)

    assert kod in (0, None)                 # nie 75: zamkniecie strumienia nie konczy procesu
    assert odwiedzone.count("/api/runners/register") == 2      # zameldowal sie ponownie
    assert len(odczekania) == 2


def test_zamkniecie_strumienia_w_trakcie_zatrzymywania_runnera_konczy_jego_proces_kodem_0_a_nie_75(
    monkeypatch, capsys,
):
    """Prawdziwy `Runner`: strumien konczy sie, gdy `stop()` juz trwa (np. po sygnale) -
    to zatrzymanie na zyczenie, nie awaria kokpitu."""
    import runner.__main__ as modul

    utworzone: list[Runner] = []

    class StrumienZatrzymujacyRunnera(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield POLACZONO
            await utworzone[0].stop()

    class Kokpit(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/runners/register":
                return httpx.Response(
                    200, json={"runner_id": "r1", "tenant_id": "t1", "heartbeat_co_s": 15})
            if request.url.path == "/api/runners/stream":
                return httpx.Response(
                    200, stream=StrumienZatrzymujacyRunnera(),
                    headers={"content-type": "text/event-stream"})
            return httpx.Response(404)

    def fabryka(base, token):
        utworzone.append(Runner(base, token, transport=Kokpit()))
        return utworzone[0]

    monkeypatch.setattr(modul, "Runner", fabryka)
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("COCKPIT_URL", "http://kokpit.test")
    try:
        modul.main()
        kod = None
    except SystemExit as wyjscie:
        kod = wyjscie.code

    assert utworzone[0]._zatrzymany.is_set()
    assert kod in (0, None)
    wynik = capsys.readouterr()
    assert "kod 75" not in wynik.err
    assert TOKEN not in wynik.err


def test_odrzucony_token_konczy_proces_runnera_kodem_2(monkeypatch, capsys):
    wyjscie = _uruchom_main(monkeypatch, "TokenOdrzucony")

    assert wyjscie.code == 2
    wyjscie_procesu = capsys.readouterr()
    assert wyjscie_procesu.err.strip()
    assert TOKEN not in wyjscie_procesu.err
    assert TOKEN not in wyjscie_procesu.out


def test_brak_tokenu_konczy_proces_runnera_kodem_2(monkeypatch, tmp_path):
    from runner import secrets
    from runner.__main__ import main

    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "brak" / "credentials.json")
    with pytest.raises(SystemExit) as wyjscie:
        main()
    assert wyjscie.value.code == 2


# --- HAR-26: kontrakt rejestracji, strumienia i komunikatow (mutanty) ---

def _nagrywajacy_kokpit(stream_body: bytes = POLACZONO, runner_id: str = "r-42"):
    """Atrapa kokpitu zapisujaca cale zadania. Zwraca (transport, zadania)."""
    zadania: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        zadania.append(request)
        if request.url.path.endswith("/api/runners/register"):
            return httpx.Response(
                200, json={"runner_id": runner_id, "tenant_id": "t1", "heartbeat_co_s": 15})
        if request.url.path.endswith("/api/runners/stream"):
            return httpx.Response(
                200, content=stream_body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200)

    return httpx.MockTransport(handler), zadania


async def _nasluchuj(runner: Runner) -> None:
    """Jedno połączenie: runner zbudowany przez `_runner(..., zatrzymaj_po=1)` zatrzymuje się
    przy pierwszym odczekaniu po zamknięciu strumienia przez kokpit."""
    await _nasluch(runner)


def _zadanie(zadania: list[httpx.Request], koncowka: str) -> httpx.Request:
    pasujace = [z for z in zadania if z.url.path.endswith(koncowka)]
    assert len(pasujace) == 1, f"oczekiwano jednego zadania {koncowka}: {len(pasujace)}"
    return pasujace[0]


def test_swiezy_runner_nie_ma_jeszcze_identyfikatora_od_kokpitu():
    assert Runner("http://kokpit.test", TOKEN).runner_id is None


async def test_rejestracja_zglasza_kokpitowi_nazwe_wersje_i_backendy_z_tokenem_runnera(
    monkeypatch,
):
    monkeypatch.setattr("runner.client.shutil.which", lambda nazwa, *a, **k: "/usr/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("importlib.metadata.version", lambda nazwa: "1.2.3-test")
    transport, zadania = _nagrywajacy_kokpit()
    runner, _ = _runner(transport=transport, name="moj-runner", zatrzymaj_po=1)
    await _nasluchuj(runner)

    zadanie = _zadanie(zadania, "/api/runners/register")
    assert zadanie.method == "POST"
    assert zadanie.url.path == "/api/runners/register"
    assert zadanie.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(zadanie.content) == {
        "name": "moj-runner", "version": "1.2.3-test",
        "backends": [{"nazwa": "claude", "auth": "subscription"}]}


async def test_runner_bez_podanej_nazwy_melduje_sie_jako_lokalny():
    transport, zadania = _nagrywajacy_kokpit()
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    assert json.loads(_zadanie(zadania, "/api/runners/register").content)["name"] == "lokalny"


async def test_po_zameldowaniu_runner_zna_identyfikator_nadany_przez_kokpit():
    transport, _ = _nagrywajacy_kokpit(runner_id="r-42")
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    assert runner.runner_id == "r-42"


async def test_strumien_polecen_otwierany_jest_przez_get_z_tokenem_runnera():
    transport, zadania = _nagrywajacy_kokpit()
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    zadanie = _zadanie(zadania, "/api/runners/stream")
    assert zadanie.method == "GET"
    assert zadanie.url.path == "/api/runners/stream"
    assert zadanie.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("adres", "prefiks"),
    [("http://kokpit.test/", ""), ("http://kokpit.test/X/", "/X")],
)
async def test_adres_kokpitu_z_koncowym_ukosnikiem_nie_daje_podwojnego_ukosnika_w_sciezkach(
    adres, prefiks,
):
    transport, zadania = _nagrywajacy_kokpit()
    runner, _ = _runner(adres, transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    assert [z.url.path for z in zadania] == [
        f"{prefiks}/api/runners/register", f"{prefiks}/api/runners/stream"]


async def test_linie_strumienia_inne_niz_dane_nie_przerywaja_czytania_polecen(capsys):
    cialo = b"event: command\n: ping\n\n" + POLACZONO
    transport, _ = _nagrywajacy_kokpit(stream_body=cialo)
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    wpisy = [w for w in capsys.readouterr().err.splitlines()
             if not w.startswith("strumien polecen przerwany")]
    assert wpisy == ["strumien otwarty"]


async def test_polecenie_run_step_ze_strumienia_uruchamia_komende_i_zglasza_jej_wynik(
    monkeypatch,
):
    monkeypatch.setattr(
        runner_client, "OPERACJE_RUNU", {"powiedz_hej": ("sh", "-c", "echo hej")})
    polecenie = {"type": "run_step", "run_id": "run-1", "step_id": "s0",
                 "operacja": "powiedz_hej"}
    # strumien zostaje otwarty: krok to osobne zadanie (HAR-126), a zamkniecie strumienia
    # zatrzymaloby runnera przed jego zakonczeniem
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(polecenie)
            await czekaj_na_warunek(
                lambda: any(z["kind"] == "run_finished" for z in zdarzenia),
                LIMIT_ANULOWANIA_S, "koniec runu zgloszony przez runnera")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert [z["kind"] for z in zdarzenia] == ["output", "step_finished", "run_finished"]
    assert zdarzenia[0]["text"] == "hej"
    assert all(z["run_id"] == "run-1" and z["step_id"] == "s0" for z in zdarzenia)
    assert len({z["event_id"] for z in zdarzenia}) == 3


async def test_klient_runnera_ma_limit_czasu_a_nie_czeka_bez_konca(monkeypatch):
    kwargs_klienta: list[dict] = []
    prawdziwy = httpx.AsyncClient

    def klient_z_zapisem(*args, **kwargs):
        kwargs_klienta.append(kwargs)
        return prawdziwy(*args, **kwargs)

    monkeypatch.setattr("runner.client.httpx.AsyncClient", klient_z_zapisem)
    transport, _ = _nagrywajacy_kokpit()
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    assert len(kwargs_klienta) == 1
    assert "timeout" in kwargs_klienta[0]
    assert kwargs_klienta[0]["timeout"] is not None


ZDARZENIE_W_BUFORZE = {
    "run_id": RUN_ID, "step_id": STEP_ID, "kind": "output", "text": "x", "event_id": "ev-1"}


async def _jedno_polaczenie_z_zaleglym_zdarzeniem(tmp_path):
    """Runner z zaleglym zdarzeniem w buforze, jedno polaczenie z nagrywajacym kokpitem.
    Zwraca nagrane zadania."""
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(ZDARZENIE_W_BUFORZE)
    transport, zadania = _nagrywajacy_kokpit()
    odczekania = Odczekania(zatrzymaj_po=1)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport, bufor=bufor,
                    odczekaj=odczekania)
    odczekania.runner = runner
    await _nasluch(runner)
    return zadania


async def test_zameldowanie_runnera_czeka_na_polaczenie_10_s_i_na_odpowiedz_30_s(tmp_path):
    zadania = await _jedno_polaczenie_z_zaleglym_zdarzeniem(tmp_path)

    limity = _zadanie(zadania, "/api/runners/register").extensions["timeout"]
    assert limity == {"connect": 10, "read": 30, "write": 30, "pool": 30}


async def test_wysylka_zdarzenia_czeka_na_polaczenie_10_s_i_na_odpowiedz_30_s(tmp_path):
    zadania = await _jedno_polaczenie_z_zaleglym_zdarzeniem(tmp_path)

    limity = _zadanie(zadania, "/api/events").extensions["timeout"]
    assert limity == {"connect": 10, "read": 30, "write": 30, "pool": 30}


async def test_strumien_polecen_uznaje_polaczenie_za_martwe_po_45_s_ciszy_czyli_trzech_pingach(
    tmp_path,
):
    zadania = await _jedno_polaczenie_z_zaleglym_zdarzeniem(tmp_path)

    limity = _zadanie(zadania, "/api/runners/stream").extensions["timeout"]
    assert limity == {"connect": 10, "read": 45, "write": 45, "pool": 45}


# --- HAR-198: prawdziwe gniazdo TCP (MockTransport nie egzekwuje limitow czasu) ---

LIMIT_TESTU_S = 5


class KokpitNaGniezdzie:
    """Minimalny kokpit na prawdziwym gniezdzie TCP (HTTP/1.1). Strumien polecen wysyla
    `polaczono` i milknie. Na pierwsze `bez_odpowiedzi` zdarzen nie odpowiada wcale."""

    def __init__(self, bez_odpowiedzi: int = 0) -> None:
        self.bez_odpowiedzi = bez_odpowiedzi
        self.strumienie = 0
        self.zdarzenia: list[dict] = []
        self._pisarze: list[asyncio.StreamWriter] = []

    @staticmethod
    def _odpowiedz(pisarz: asyncio.StreamWriter, status: int, tresc: bytes = b"") -> None:
        pisarz.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(tresc)}\r\n\r\n".encode() + tresc)

    async def obsluz(self, czytelnik: asyncio.StreamReader, pisarz: asyncio.StreamWriter) -> None:
        self._pisarze.append(pisarz)
        try:
            while True:
                naglowek = await czytelnik.readuntil(b"\r\n\r\n")
                linie = naglowek.decode().split("\r\n")
                sciezka = linie[0].split(" ")[1]
                dlugosc = 0
                for linia in linie[1:]:
                    if linia.lower().startswith("content-length:"):
                        dlugosc = int(linia.split(":")[1])
                cialo = await czytelnik.readexactly(dlugosc)
                if sciezka == "/api/runners/register":
                    self._odpowiedz(pisarz, 200, json.dumps(
                        {"runner_id": "r1", "tenant_id": "t1", "heartbeat_co_s": 15}).encode())
                elif sciezka == "/api/runners/stream":
                    self.strumienie += 1
                    pisarz.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n"
                        + b"%x\r\n%s\r\n" % (len(POLACZONO), POLACZONO))
                    await pisarz.drain()
                    await czytelnik.read()          # cisza do zamkniecia przez klienta
                    return
                elif sciezka == "/api/events":
                    self.zdarzenia.append(json.loads(cialo))
                    if len(self.zdarzenia) > self.bez_odpowiedzi:
                        self._odpowiedz(pisarz, 200)
                else:
                    self._odpowiedz(pisarz, 404)
                await pisarz.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            pisarz.close()


@contextlib.asynccontextmanager
async def _kokpit_na_gniezdzie(bez_odpowiedzi: int = 0):
    kokpit = KokpitNaGniezdzie(bez_odpowiedzi)
    serwer = await asyncio.start_server(kokpit.obsluz, "127.0.0.1", 0)
    port = serwer.sockets[0].getsockname()[1]
    try:
        yield kokpit, f"http://127.0.0.1:{port}"
    finally:
        serwer.close()
        for pisarz in kokpit._pisarze:
            pisarz.close()
        await czekaj(serwer.wait_closed(), LIMIT_TESTU_S, "zamkniecie serwera testowego")


async def test_runner_laczy_sie_ponownie_gdy_strumien_polecen_milknie_na_dluzej_niz_limit(
    monkeypatch, capsys, tmp_path,
):
    monkeypatch.setattr(runner_client, "CZAS_ODCZYTU_STRUMIENIA_S", 0.3)
    async with _kokpit_na_gniezdzie() as (kokpit, adres):
        odczekania = Odczekania(zatrzymaj_po=2)
        runner = Runner(adres, TOKEN, odczekaj=odczekania, bufor=Bufor(tmp_path / "bufor"))
        odczekania.runner = runner
        async with zadanie_tla(runner.listen(), "nasluch runnera") as nasluch:
            await czekaj_bez_anulowania(
                nasluch, LIMIT_TESTU_S, "runner nie polaczyl sie ponownie po ciszy na strumieniu")

        assert kokpit.strumienie == 2
        assert odczekania.argumenty == [1.0, 1.0]
        assert "strumien polecen przerwany (ReadTimeout)" in capsys.readouterr().err


async def test_zdarzenie_bez_odpowiedzi_kokpitu_zostaje_w_buforze_i_jest_ponowione_z_tym_samym_id(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(runner_client, "CZAS_ODCZYTU_POST_S", 0.3)
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(ZDARZENIE_W_BUFORZE)
    w_buforze_podczas_przerwy: list[list[str]] = []
    odczekania: list[float] = []

    async def odczekaj(sekundy: float) -> None:
        odczekania.append(sekundy)
        w_buforze_podczas_przerwy.append([w["event_id"] for w in bufor.niepotwierdzone()])

    async with _kokpit_na_gniezdzie(bez_odpowiedzi=1) as (kokpit, adres):
        runner = Runner(adres, TOKEN, bufor=bufor, odczekaj=odczekaj)
        async with zadanie_tla(runner.listen(), "nasluch runnera") as nasluch:
            await czekaj_na_warunek(
                lambda: len(kokpit.zdarzenia) >= 2 and not bufor.niepotwierdzone(),
                LIMIT_TESTU_S, "zdarzenie bez odpowiedzi nie zostalo ponowione")
            await czekaj(runner.stop(), LIMIT_TESTU_S, "stop() runnera")
            await czekaj_bez_anulowania(nasluch, LIMIT_TESTU_S, "koniec nasluchu po stop()")

    assert w_buforze_podczas_przerwy == [["ev-1"]]
    assert odczekania == [1.0]
    assert [z["event_id"] for z in kokpit.zdarzenia] == ["ev-1", "ev-1"]
    assert bufor.niepotwierdzone() == []


async def test_zamkniecie_strumienia_przez_kokpit_ma_wewnetrzny_sygnal_z_komunikatem_przyczyny():
    from runner.client import StrumienZamkniety
    transport, _ = _nagrywajacy_kokpit()
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(StrumienZamkniety) as wyjatek:
        await asyncio.wait_for(runner._nasluchuj(), 10)

    assert str(wyjatek.value) == "kokpit zamknal strumien polecen"


def _main_z_atrapa_runnera(
    monkeypatch, tmp_path, wyjatek=None, env=None, plik=None, komunikat=None,
):
    """Uruchamia prawdziwe `main()` z atrapa klasy Runner. Zwraca (SystemExit, argumenty Runnera).
    Srodowisko i plik konfiguracji sa izolowane: nic z zewnatrz nie przecieka.
    `listen` konczy sie wskazanym wyjatkiem dziedziny (domyslnie odrzucony token -> kod 2)."""
    import runner.__main__ as modul
    import runner.bufor as bufor
    import runner.client as klient
    from runner import secrets

    zapis: dict = {}

    class AtrapaRunnera:
        def __init__(self, base_url, token, *reszta, **nazwane):
            zapis["base_url"] = base_url
            zapis["token"] = token

        async def listen(self):
            nazwa = wyjatek or "TokenOdrzucony"
            klasa = getattr(bufor, nazwa, None) or getattr(klient, nazwa)
            raise klasa(*([] if komunikat is None else [komunikat]))

        async def stop(self):
            return None

    config = tmp_path / "credentials.json"
    if plik is not None:
        config.write_text(json.dumps(plik))
    monkeypatch.setattr(secrets, "CONFIG", config)
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    monkeypatch.delenv("COCKPIT_URL", raising=False)
    for nazwa, wartosc in (env or {}).items():
        monkeypatch.setenv(nazwa, wartosc)
    monkeypatch.setattr(modul, "Runner", AtrapaRunnera)
    with pytest.raises(SystemExit) as wyjscie:
        modul.main()
    return wyjscie.value, zapis


def test_brak_tokenu_podpowiada_uzytkownikowi_jak_go_podac_i_nic_nie_wypisuje_na_stdout(
    monkeypatch, tmp_path, capsys,
):
    wyjscie, zapis = _main_z_atrapa_runnera(monkeypatch, tmp_path)

    assert wyjscie.code == 2
    assert zapis == {}      # bez tokenu runner w ogole nie powstaje
    wynik = capsys.readouterr()
    assert wynik.err.splitlines() == [
        "Brak tokenu. Wygeneruj go w Ustawieniach i zapisz:",
        "  COCKPIT_RUNNER_TOKEN=... cockpit-runner",
    ]
    assert wynik.out == ""


def test_token_runnera_bez_zmiennej_srodowiskowej_jest_czytany_z_pliku_konfiguracji(
    monkeypatch, tmp_path,
):
    _, zapis = _main_z_atrapa_runnera(monkeypatch, tmp_path, plik={"runner": "tok-z-pliku"})

    assert zapis["token"] == "tok-z-pliku"


def test_runner_dostaje_token_i_adres_kokpitu_ze_srodowiska(monkeypatch, tmp_path):
    _, zapis = _main_z_atrapa_runnera(
        monkeypatch, tmp_path,
        env={"COCKPIT_RUNNER_TOKEN": "tok-z-env", "COCKPIT_URL": "http://inny:9000"})

    assert zapis == {"base_url": "http://inny:9000", "token": "tok-z-env"}


def test_bez_adresu_w_srodowisku_runner_laczy_sie_z_kokpitem_na_localhoscie(
    monkeypatch, tmp_path,
):
    _, zapis = _main_z_atrapa_runnera(
        monkeypatch, tmp_path, env={"COCKPIT_RUNNER_TOKEN": "tok-z-env"})

    assert zapis["base_url"] == "http://127.0.0.1:8765"


def test_blad_bufora_zdarzen_konczy_proces_runnera_kodem_2_z_komunikatem_o_buforze(
    monkeypatch, tmp_path, capsys,
):
    wyjscie, _ = _main_z_atrapa_runnera(
        monkeypatch, tmp_path, "BladBufora", env={"COCKPIT_RUNNER_TOKEN": TOKEN},
        komunikat="katalog stanu nie do zapisu")

    assert wyjscie.code == 2
    wynik = capsys.readouterr()
    assert wynik.err.splitlines() == ["bufor zdarzen: katalog stanu nie do zapisu"]
    assert TOKEN not in wynik.err and TOKEN not in wynik.out


def test_uszkodzony_bufor_na_dysku_konczy_prawdziwy_proces_runnera_kodem_2_bez_sladu_stosu(
    monkeypatch, tmp_path, capsys,
):
    import runner.__main__ as modul

    # plik runu jest katalogiem: odczyt zaleglych zdarzen konczy sie OSError, nie wyjatkiem biblioteki
    (tmp_path / "bufor" / "run-1.ndjson").mkdir(parents=True)
    transport, _ = _kokpit(stream_body=POLACZONO)
    monkeypatch.setattr(
        modul, "Runner", lambda base, token: Runner(base, token, transport=transport))
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("COCKPIT_URL", "http://kokpit.test")

    with pytest.raises(SystemExit) as wyjscie:
        modul.main()

    assert wyjscie.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("bufor zdarzen: ")
    assert "Traceback" not in err and TOKEN not in err


def test_komunikat_o_odrzuconym_tokenie_odsyla_do_wygenerowania_nowego_w_ustawieniach(
    monkeypatch, tmp_path, capsys,
):
    wyjscie, _ = _main_z_atrapa_runnera(
        monkeypatch, tmp_path, "TokenOdrzucony", env={"COCKPIT_RUNNER_TOKEN": TOKEN})

    assert wyjscie.code == 2
    assert capsys.readouterr().err.splitlines() == [
        "token odrzucony przez kokpit - wygeneruj nowy w Ustawieniach "
        "i zapisz go w COCKPIT_RUNNER_TOKEN albo w ~/.config/cockpit/credentials.json"]


# --- HAR-35: odpowiedz kokpitu lamiaca kontrakt i zatrzymanie sygnalem -----------

def test_odpowiedz_kokpitu_lamiaca_kontrakt_konczy_proces_runnera_kodem_2_z_czytelnym_komunikatem(
    monkeypatch, capsys,
):
    wyjscie = _uruchom_main(monkeypatch, "KokpitNiezgodny")

    assert wyjscie.code == 2
    wynik = capsys.readouterr()
    assert "kokpit odpowiedzial niezgodnie z kontraktem" in wynik.err
    assert "Traceback" not in wynik.err
    assert TOKEN not in wynik.err
    assert TOKEN not in wynik.out


def test_rejestracja_bez_czestotliwosci_heartbeatu_konczy_proces_runnera_kodem_2_bez_sladu_stosu(
    monkeypatch, capsys,
):
    from runner import __main__ as modul

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runners/register":
            return httpx.Response(200, json={"runner_id": "r1", "tenant_id": "t1"})
        return httpx.Response(404)

    monkeypatch.setattr(
        modul, "Runner",
        lambda base, token: Runner(base, token, transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("COCKPIT_URL", "http://kokpit.test")

    with pytest.raises(SystemExit) as wyjscie:
        modul.main()

    assert wyjscie.value.code == 2
    wynik = capsys.readouterr()
    assert ("kokpit odpowiedzial niezgodnie z kontraktem: "
            "odpowiedz rejestracji bez poprawnego heartbeat_co_s") in wynik.err.splitlines()
    assert "Traceback" not in wynik.err
    assert TOKEN not in wynik.err


@pytest.mark.parametrize("sygnal", ["SIGTERM", "SIGINT"])
def test_runner_zatrzymany_sygnalem_zatrzymuje_sie_porzadnie_i_konczy_prace_kodem_0(
    monkeypatch, tmp_path, sygnal,
):
    """Sygnal idzie do wlasnego procesu testu, wiec siatka bezpieczenstwa: no-op handlery
    na czas testu - brak obslugi sygnalu w `main` ma sie skonczyc oblanym testem, a nie
    zabiciem calego przebiegu testow."""
    import asyncio
    import os
    import signal

    import runner.__main__ as modul
    from runner import secrets

    numer = getattr(signal, sygnal)
    zapis = {"stop": 0}

    class AtrapaRunnera:
        def __init__(self, base_url, token, *reszta, **nazwane):
            self._zatrzymany = asyncio.Event()

        async def listen(self):
            await asyncio.sleep(0.1)         # main zdazyl zarejestrowac obsluge sygnalu
            os.kill(os.getpid(), numer)
            await asyncio.wait_for(self._zatrzymany.wait(), 5)

        async def stop(self):
            zapis["stop"] += 1
            self._zatrzymany.set()

    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "credentials.json")
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    monkeypatch.setattr(modul, "Runner", AtrapaRunnera)
    stare = {s: signal.signal(s, lambda *a: None) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            modul.main()
            kod = None
        except SystemExit as wyjscie:
            kod = wyjscie.code
    finally:
        for s, obsluga in stare.items():
            signal.signal(s, obsluga)

    assert zapis["stop"] >= 1
    assert kod in (0, None)


# --- HAR-35: pomiary i kanaly wyjscia kroku (zdarzenia faktycznie wyslane) -------

async def test_linia_kroku_wypisana_na_stderr_trafia_do_zdarzen_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo blad-kroku >&2"]))

    assert [z["text"] for z in zdarzenia if z["kind"] == "output"] == ["blad-kroku"]


async def test_bajty_niepoprawne_w_utf8_nie_przerywaja_kroku_i_trafiaja_do_wyjscia_jako_znak_zastepczy():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "printf '\\377ok\\n'"]))

    assert [z["text"] for z in zdarzenia if z["kind"] == "output"] == ["\ufffdok"]
    assert zdarzenia[-2]["kind"] == "step_finished"


async def test_czas_trwania_kroku_to_calkowite_milisekundy_zmierzone_na_zegarze_kroku():
    zdarzenia = _zdarzenia(await _uruchom(["sleep", "0.2"]))

    ms = zdarzenia[-2]["duration_ms"]
    assert type(ms) is int
    assert 150 <= ms < 5000


async def test_czas_trwania_kroku_w_zdarzeniu_to_dokladna_roznica_zegara_w_milisekundach(
    monkeypatch,
):
    odczyty = iter([100.0, 102.5])
    # podmieniamy tylko `time` w module runnera: petla zdarzen asyncio ma wlasny zegar
    monkeypatch.setattr(
        runner_client, "time", SimpleNamespace(monotonic=lambda: next(odczyty)),
    )

    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    assert zdarzenia[-2]["duration_ms"] == 2500


async def test_koniec_kroku_bez_pomiaru_kosztu_ma_zrodlo_niedostepne_i_koszt_null_a_nie_zero():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    koncowe = zdarzenia[-2]
    assert "cost_usd" in koncowe and koncowe["cost_usd"] is None
    assert koncowe["cost_source"] == "unavailable"


# --- HAR-126: polecenie cancel_run przerywa krok, a petla polecen go nie czeka ---------------

LIMIT_ANULOWANIA_S = 10
LIMIT_STRUMIENIA_S = 120    # zabezpieczenie przed zawieszeniem atrapy, nie oczekiwany czas

# Agent kroku: zapisuje "pid pgid pid_wnuka" (atomowo) i spi razem z wnukiem w tej samej grupie.
KOD_AGENTA_Z_WNUKIEM = r"""
import os, subprocess, sys, time
plik = sys.argv[1]
wnuk = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(300)"])
tmp = plik + ".tmp"
with open(tmp, "w") as f:
    f.write(f"{os.getpid()} {os.getpgid(0)} {wnuk.pid}")
os.replace(tmp, plik)
time.sleep(300)
"""


class StrumienPolecen(httpx.AsyncByteStream):
    """Otwarty strumien SSE kokpitu: polecenia wysyla test, strumien milknie az do `zamknij()`.
    Dzieki temu zamkniecie strumienia nie zatrzymuje runnera w trakcie kroku."""

    def __init__(self) -> None:
        self._kolejka: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.wyslij({"type": "polaczono"})

    def wyslij(self, polecenie: dict) -> None:
        self._kolejka.put_nowait(f"data: {json.dumps(polecenie)}\n\n".encode())

    def zamknij(self) -> None:
        self._kolejka.put_nowait(None)

    async def __aiter__(self):
        while True:
            kawalek = await czekaj(self._kolejka.get(), LIMIT_STRUMIENIA_S, "dane strumienia polecen")
            if kawalek is None:
                return
            yield kawalek

    async def aclose(self) -> None:
        pass


def kokpit_ze_strumieniem(
    strumien: StrumienPolecen, runner_id: str = "r-42", status_zdarzen: int = 200,
    statusy_wg_rodzaju: dict[str, int] | None = None,
):
    """Atrapa kokpitu ze strumieniem z testu. Zwraca (transport, zdarzenia): lista dostaje
    ciala wszystkich zadan POST /api/events (kazde odpowiedziane `status_zdarzen`, a zdarzenia
    rodzajow z `statusy_wg_rodzaju` - podanym kodem)."""
    zdarzenia: list[dict] = []
    statusy_wg_rodzaju = statusy_wg_rodzaju or {}

    def handler(request: httpx.Request) -> httpx.Response:
        sciezka = request.url.path
        if sciezka == "/api/runners/register":
            return httpx.Response(
                200, json={"runner_id": runner_id, "tenant_id": "t1", "heartbeat_co_s": 15})
        if sciezka == "/api/runners/stream":
            return httpx.Response(
                200, stream=strumien, headers={"content-type": "text/event-stream"})
        if sciezka == "/api/events":
            zdarzenie = json.loads(request.content)
            zdarzenia.append(zdarzenie)
            return httpx.Response(statusy_wg_rodzaju.get(zdarzenie["kind"], status_zdarzen))
        return httpx.Response(200)

    return httpx.MockTransport(handler), zdarzenia


def _agent_kroku(plik) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", KOD_AGENTA_Z_WNUKIEM, str(plik))


def _krok(run_id: str, operacja: str) -> dict:
    return {"type": "run_step", "run_id": run_id, "step_id": "s0", "operacja": operacja}


async def _czekaj_na_agenta(plik) -> int:
    """pgid agenta z pliku; grupa trafia do sprzatania po tescie (czerwony test nie zostawia siroty)."""
    await czekaj_na_warunek(plik.exists, LIMIT_ANULOWANIA_S, "agent zapisal pid i pgid")
    pgid = int(plik.read_text().split()[1])
    zapamietaj_proces(pgid)
    return pgid


async def _krok_anulowany_w_trakcie(monkeypatch, tmp_path) -> SimpleNamespace:
    """Krok z prawdziwym procesem (i wnukiem), `cancel_run` na otwartym strumieniu.
    Runner NIE jest zatrzymywany przed sprawdzeniem: zatrzymanie samo zabiloby grupe."""
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"spij": _agent_kroku(plik)})
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    wynik = SimpleNamespace(zdarzenia=zdarzenia)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(_krok(RUN_ID, "spij"))
            pgid = await _czekaj_na_agenta(plik)
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            wynik.grupa_umarla = await grupa_umarla(pgid, limit=LIMIT_ANULOWANIA_S)
            wynik.runner_zatrzymany = runner._zatrzymany.is_set()
            if wynik.grupa_umarla:
                await czekaj_na_warunek(
                    lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania kroku")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")
    return wynik


async def test_polecenie_cancel_run_odebrane_w_trakcie_kroku_zabija_cala_grupe_procesow_kroku(
    monkeypatch, tmp_path,
):
    wynik = await _krok_anulowany_w_trakcie(monkeypatch, tmp_path)

    assert wynik.grupa_umarla, "krok, ktory trwa, zablokowal odbior polecenia cancel_run"
    assert not wynik.runner_zatrzymany


TEKST_ANULOWANIA = "anulowany przez człowieka"
RODZAJE_KONCA = {"step_finished", "step_failed", "run_finished"}


def _step_aborted(zdarzenia: list[dict]) -> list[tuple[str, str, str]]:
    return [(z["run_id"], z["step_id"], z["text"]) for z in zdarzenia if z["kind"] == "step_aborted"]


async def test_po_cancel_run_kokpit_dostaje_dokladnie_jedno_step_aborted_kroku_run_step_z_powodem_anulowania(
    monkeypatch, tmp_path,
):
    wynik = await _krok_anulowany_w_trakcie(monkeypatch, tmp_path)

    assert wynik.grupa_umarla
    assert _step_aborted(wynik.zdarzenia) == [(RUN_ID, "s0", TEKST_ANULOWANIA)]
    # run_step nie ma agenta ani proby: kokpit czyta jego zdarzenia jako ("", 1), wiec abort tez
    assert [(z["agent_id"], z["attempt"]) for z in wynik.zdarzenia
            if z["kind"] == "step_aborted"] == [("", 1)]
    # runner nie nadpisuje wyniku runu: stan `aborted` zapisal kokpit
    assert not {z["kind"] for z in wynik.zdarzenia} & RODZAJE_KONCA


async def test_cancel_run_jednego_runu_nie_zabija_procesu_kroku_innego_runu(
    monkeypatch, tmp_path,
):
    plik_a, plik_b = tmp_path / "a.pid", tmp_path / "b.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "spij_a": _agent_kroku(plik_a), "spij_b": _agent_kroku(plik_b)})
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(_krok("run-a", "spij_a"))
            strumien.wyslij(_krok("run-b", "spij_b"))
            pgid_a = await _czekaj_na_agenta(plik_a)
            pgid_b = await _czekaj_na_agenta(plik_b)

            # anulujemy run zarejestrowany jako DRUGI: wybor "pierwszego z zadan" zabilby run-a
            strumien.wyslij({"type": "cancel_run", "run_id": "run-b"})
            assert await grupa_umarla(pgid_b, limit=LIMIT_ANULOWANIA_S)
            await czekaj_na_warunek(
                lambda: len(runner._runy) <= 1, LIMIT_ANULOWANIA_S, "koniec zadania run-b")

            assert len(runner._runy) == 1
            assert grupa_istnieje(pgid_a)

            strumien.wyslij({"type": "cancel_run", "run_id": "run-a"})
            assert await grupa_umarla(pgid_a, limit=LIMIT_ANULOWANIA_S)
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")


@pytest.mark.parametrize("polecenie_anulowania", [
    {"type": "cancel_run", "run_id": "run-bez-zadania"},     # run `waiting`, nieznany albo zakonczony
    {"type": "cancel_run"},                                  # bez identyfikatora runu
    {"type": "cancel_run", "run_id": 7},                     # identyfikator nie jest napisem
], ids=["run_bez_zadania", "bez_run_id", "run_id_nie_napis"])
async def test_cancel_run_dla_runu_bez_zadania_nie_psuje_runnera_a_nastepny_krok_dziala(
    monkeypatch, capsys, polecenie_anulowania,
):
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"hej": ("sh", "-c", "echo hej")})
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(polecenie_anulowania)
            strumien.wyslij(_krok("run-2", "hej"))
            await czekaj_na_warunek(
                lambda: any(z["kind"] == "run_finished" for z in zdarzenia),
                LIMIT_ANULOWANIA_S, "koniec kroku po cancel_run bez zadania")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert [z["kind"] for z in zdarzenia] == ["output", "step_finished", "run_finished"]
    assert {z["run_id"] for z in zdarzenia} == {"run-2"}
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(("polecenie", "funkcja_silnika"), [
    ({"type": "run_workflow", "run_id": RUN_ID, "workflow": "w", "sciezka": "/repo"}, "wykonaj_run"),
    ({"type": "resume", "run_id": RUN_ID, "step_id": "s0", "decyzja": "approve"}, "wznow_run"),
], ids=["run_workflow", "resume"])
async def test_cancel_run_anuluje_zadanie_runu_workflow_albo_wznowienia(
    monkeypatch, polecenie, funkcja_silnika,
):
    wystartowal, anulowane = asyncio.Event(), []

    async def zawieszone(*args, **kwargs):
        wystartowal.set()
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            anulowane.append(RUN_ID)
            raise

    monkeypatch.setattr(runner_client.silnik, funkcja_silnika, zawieszone)
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(polecenie)
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start zadania silnika")

            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(
                lambda: anulowane == [RUN_ID], LIMIT_ANULOWANIA_S, "anulowanie zadania silnika")
            assert not runner._zatrzymany.is_set()
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")


async def test_wznowienie_anulowanego_runu_jest_ignorowane_a_wznowienie_innego_runu_dziala(
    monkeypatch,
):
    async def wznowienie(run_id, step_id, decyzja, emit, powod=None):
        await emit({"run_id": run_id, "step_id": step_id, "kind": "step_started"})

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)

    def resume(run_id: str) -> dict:
        return {"type": "resume", "run_id": run_id, "step_id": "s0", "decyzja": "approve"}

    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "cancel_run", "run_id": "run-anulowany"})
            strumien.wyslij(resume("run-anulowany"))       # spozniona decyzja po anulowaniu
            strumien.wyslij(resume("run-zywy"))
            await czekaj_na_warunek(
                lambda: any(z["run_id"] == "run-zywy" for z in zdarzenia),
                LIMIT_ANULOWANIA_S, "wznowienie runu, ktory nie byl anulowany")
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec wznowien")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert [z["run_id"] for z in zdarzenia] == ["run-zywy"]


async def test_koniec_starszego_zadania_runu_nie_odpina_nowszego_wznowienia_tego_samego_runu(
    monkeypatch,
):
    # run_workflow doszedl do bramki i sie skonczyl, ale wznowienie tego samego runu jest juz
    # zarejestrowane: cancel_run musi nadal trafic w wznowienie
    wznowienie_wystartowalo, zwolnij_workflow, anulowane = asyncio.Event(), asyncio.Event(), []
    workflow_wystartowal = asyncio.Event()

    async def workflow(*args, **kwargs):
        workflow_wystartowal.set()
        await czekaj(zwolnij_workflow.wait(), LIMIT_STRUMIENIA_S, "zwolnienie workflow przez test")

    async def wznowienie(*args, **kwargs):
        wznowienie_wystartowalo.set()
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            anulowane.append(RUN_ID)
            raise

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", workflow)
    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_workflow", "run_id": RUN_ID, "workflow": "w",
                             "sciezka": "/repo"})
            await czekaj(workflow_wystartowal.wait(), LIMIT_ANULOWANIA_S, "start workflow")
            stare_zadanie = runner._runy[RUN_ID]
            strumien.wyslij({"type": "resume", "run_id": RUN_ID, "step_id": "s0",
                             "decyzja": "approve"})
            await czekaj(wznowienie_wystartowalo.wait(), LIMIT_ANULOWANIA_S, "start wznowienia")

            zwolnij_workflow.set()
            await czekaj_na_warunek(stare_zadanie.done, LIMIT_ANULOWANIA_S, "koniec starego zadania")
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})

            await czekaj_na_warunek(
                lambda: anulowane == [RUN_ID], LIMIT_ANULOWANIA_S, "anulowanie wznowienia")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")


# --- HAR-126: mutanty client.py (_nasluchuj, _zarejestruj_run) --------------------------------

async def _przetworz(polecenia: list[dict], warunek, *, limit_nazwa: str) -> list[dict]:
    """Runner na otwartym strumieniu dostaje `polecenia` po kolei; wraca, gdy `warunek(zdarzenia)`."""
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            for polecenie in polecenia:
                strumien.wyslij(polecenie)
            await czekaj_na_warunek(lambda: warunek(zdarzenia), LIMIT_ANULOWANIA_S, limit_nazwa)
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")
    return zdarzenia


@pytest.mark.parametrize("run_id", [7, ""], ids=["nie_napis", "pusty"])
async def test_run_workflow_bez_poprawnego_run_id_jest_odrzucony_z_komunikatem_i_nie_startuje(
    monkeypatch, capsys, run_id,
):
    wywolania: list[str] = []

    async def wykonaj(cmd, emit):
        wywolania.append(cmd["run_id"])

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)

    def workflow(r):
        return {"type": "run_workflow", "run_id": r, "workflow": "w", "sciezka": "/repo"}

    await _przetworz(
        [workflow(run_id), workflow(RUN_ID)], lambda _: RUN_ID in wywolania,
        limit_nazwa="start poprawnego run_workflow")

    assert wywolania == [RUN_ID]
    assert capsys.readouterr().err.splitlines().count(
        "polecenie run_workflow bez poprawnego run_id odrzucone") == 1


@pytest.mark.parametrize("run_id", [7, "", None], ids=["nie_napis", "pusty", "brak"])
async def test_krok_bez_poprawnego_run_id_jest_odrzucony_bez_procesu_a_runner_dziala_dalej(
    monkeypatch, tmp_path, capsys, run_id,
):
    plik = tmp_path / "start"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "odrzucony": ("sh", "-c", f"touch {plik}"), "hej": ("sh", "-c", "echo hej")})
    # polecenie bez poprawnego run_id odrzuca juz strumien: zadnego zadania, wiec `_krok_z_polecenia`
    # (ktore wypisuje ten sam komunikat) nie moze byc nawet wywolane
    wywolane: list[object] = []
    prawdziwy_krok = Runner._krok_z_polecenia

    async def krok_ze_sledzeniem(self, client, cmd):
        wywolane.append(cmd.get("run_id"))
        await prawdziwy_krok(self, client, cmd)

    monkeypatch.setattr(Runner, "_krok_z_polecenia", krok_ze_sledzeniem)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    polecenie = {"type": "run_step", "step_id": "s0", "operacja": "odrzucony"}
    if run_id is not None:
        polecenie["run_id"] = run_id
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(polecenie)
            strumien.wyslij(_krok("run-2", "hej"))     # po odrzuconym: runner nadal pracuje
            await czekaj_na_warunek(
                lambda: any(z["kind"] == "run_finished" for z in zdarzenia),
                LIMIT_ANULOWANIA_S, "koniec kroku po odrzuconym poleceniu")

            assert not runner._zatrzymany.is_set()
            assert runner._runy == {} or list(runner._runy) == ["run-2"]
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert not plik.exists()                           # proces odrzuconego kroku nie wystartowal
    assert wywolane == ["run-2"]
    assert {z["run_id"] for z in zdarzenia} == {"run-2"}
    assert capsys.readouterr().err.splitlines().count(
        "polecenie run_step bez poprawnego run_id odrzucone") == 1


async def test_wznowienie_anulowanego_runu_jest_odnotowane_w_logu_z_obcietym_do_36_znakow_id(
    monkeypatch, capsys,
):
    zywe: list[str] = []

    async def wznowienie(run_id, step_id, decyzja, emit, powod=None):
        zywe.append(run_id)

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)
    anulowany = "a" * 40

    def resume(r):
        return {"type": "resume", "run_id": r, "step_id": "s0", "decyzja": "approve"}

    await _przetworz(
        [{"type": "cancel_run", "run_id": anulowany}, resume(anulowany), resume("run-zywy")],
        lambda _: "run-zywy" in zywe, limit_nazwa="wznowienie zywego runu")

    assert zywe == ["run-zywy"]
    assert f"resume ignorowane: run '{'a' * 36}' anulowany" in capsys.readouterr().err.splitlines()


async def test_zadanie_runu_ktore_sie_zepsulo_przy_za_dlugim_id_runu_zostawia_log_bez_zdarzen_dla_kokpitu(
    monkeypatch, capsys,
):
    # id dluzsze niz 36 znakow lamie limit pola: zdarzenia konca nie da sie wyslac (kokpit
    # odpowiedzialby 4xx), wiec zostaje tylko log z nazwa typu, bez tresci wyjatku
    wystartowal = asyncio.Event()

    async def wykonaj(cmd, emit):
        wystartowal.set()
        raise RuntimeError("sekret-xyz")

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    run_id = "r" * 40
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(
                {"type": "run_workflow", "run_id": run_id, "workflow": "w", "sciezka": "/repo"})
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start zadania silnika")
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    err = capsys.readouterr().err
    assert f"zadanie runu '{'r' * 36}' zakonczone bledem: RuntimeError" in err.splitlines()
    assert "sekret-xyz" not in err
    assert zdarzenia == []


@pytest.mark.parametrize("koniec", ["sukces", "anulowanie"])
async def test_zadanie_runu_zakonczone_sukcesem_albo_anulowaniem_nie_jest_zglaszane_jako_blad(
    monkeypatch, capsys, caplog, koniec,
):
    wystartowal = asyncio.Event()

    async def wykonaj(cmd, emit):
        wystartowal.set()
        if koniec == "anulowanie":
            await asyncio.sleep(300)

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(
                {"type": "run_workflow", "run_id": RUN_ID, "workflow": "w", "sciezka": "/repo"})
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start zadania silnika")
            if koniec == "anulowanie":
                strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert "zakonczone bledem" not in capsys.readouterr().err
    assert [r for r in caplog.records if r.levelname == "ERROR"] == []
    assert zdarzenia == []          # ani sukces silnika, ani anulowanie nie wysylaja zdarzen z runnera


@pytest.mark.parametrize("polecenie", [
    {"type": "cancel_run"}, {"type": "cancel_run", "run_id": 7}, {"type": "cancel_run", "run_id": ""},
], ids=["bez_run_id", "run_id_nie_napis", "pusty_run_id"])
async def test_niepoprawne_cancel_run_jest_odnotowane_w_logu_z_cala_trescia_polecenia(
    monkeypatch, capsys, polecenie,
):
    zywe: list[str] = []

    async def wznowienie(run_id, step_id, decyzja, emit, powod=None):
        zywe.append(run_id)

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)
    await _przetworz(
        [polecenie, {"type": "resume", "run_id": "zywy", "step_id": "s0", "decyzja": "approve"}],
        lambda _: zywe == ["zywy"], limit_nazwa="polecenie po niepoprawnym cancel_run")

    assert f"cancel_run odrzucone: niepoprawne polecenie {polecenie!r}" in (
        capsys.readouterr().err.splitlines())


async def test_cancel_run_dla_runu_bez_zadania_jest_w_logu_z_obcietym_do_36_znakow_id(
    monkeypatch, capsys,
):
    zywe: list[str] = []

    async def wznowienie(run_id, step_id, decyzja, emit, powod=None):
        zywe.append(run_id)

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)
    await _przetworz(
        [{"type": "cancel_run", "run_id": "b" * 40},
         {"type": "resume", "run_id": "zywy", "step_id": "s0", "decyzja": "approve"}],
        lambda _: zywe == ["zywy"], limit_nazwa="polecenie po cancel_run bez zadania")

    assert f"cancel_run: run '{'b' * 36}' nie ma zadania u runnera" in (
        capsys.readouterr().err.splitlines())


async def test_runner_pamieta_dokladnie_limit_anulowanych_runow_a_najstarszy_wypada_jako_pierwszy(
    monkeypatch,
):
    monkeypatch.setattr(runner_client, "LIMIT_ANULOWANYCH", 3)
    wznowione: list[str] = []

    async def wznowienie(run_id, step_id, decyzja, emit, powod=None):
        wznowione.append(run_id)

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)

    def cancel(r):
        return {"type": "cancel_run", "run_id": r}

    def resume(r):
        return {"type": "resume", "run_id": r, "step_id": "s0", "decyzja": "approve"}

    await _przetworz(
        [cancel("c0"), cancel("c1"), cancel("c2"),   # dokladnie limit: nikt nie wypada
         resume("c0"),                               # nadal pamietany -> ignorowany
         cancel("c3"),                               # ponad limit: wypada najstarszy (c0)
         resume("c0"), resume("c3"), resume("zywy")],
        lambda _: "zywy" in wznowione, limit_nazwa="wznowienie po limicie anulowanych")

    assert wznowione == ["c0", "zywy"]


async def test_resume_z_run_id_ktory_nie_jest_napisem_jest_odrzucone_a_runner_dziala_dalej(
    monkeypatch, capsys,
):
    zywe: list[str] = []

    async def wznowienie(run_id, step_id, decyzja, emit, powod=None):
        zywe.append(run_id)

    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznowienie)
    zle = {"type": "resume", "run_id": ["x"], "step_id": "s0", "decyzja": "approve"}
    dobre = {"type": "resume", "run_id": "zywy", "step_id": "s0", "decyzja": "approve"}

    await _przetworz([zle, dobre], lambda _: zywe == ["zywy"], limit_nazwa="resume po odrzuconym")

    assert f"resume odrzucone: niepoprawne polecenie {zle!r}" in capsys.readouterr().err.splitlines()


# --- HAR-126: porazka startu procesu i niedostepny bufor zdarzen -------------------------------

class BuforZepsuty(Bufor):
    """Bufor, ktorego zapis zawsze konczy sie zadanym bledem (BladBufora albo BrakMiejsca)."""

    def __init__(self, katalog, blad: Exception) -> None:
        super().__init__(katalog)
        self._blad = blad

    def dopisz(self, zdarzenie: dict) -> None:
        raise self._blad


BLEDY_BUFORA = [
    (BladBufora("uszkodzony"),
     "bufor zdarzen runnera niedostepny: zapis albo odczyt nie powiodl sie"),
    (BrakMiejsca("pelny dysk"), "brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)"),
]
BLEDY_BUFORA_IDS = ["bufor_niedostepny", "brak_miejsca"]
POWOD_BUFORA = "bufor zdarzen runnera niedostepny: zapis albo odczyt nie powiodl sie"


async def _z_zepsutym_buforem(tmp_path, blad: Exception, wywolanie) -> list[dict]:
    """`wywolanie(runner, client)` na runnerze z buforem, ktory odrzuca kazdy zapis; zwraca
    zdarzenia wyslane poza buforem (POST bezposredni)."""
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200)

    odczekania = Odczekania(zatrzymaj_po=1)
    runner = Runner("http://kokpit.test", TOKEN, odczekaj=odczekania,
                    bufor=BuforZepsuty(tmp_path / "bufor", blad))
    odczekania.runner = runner
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(wywolanie(runner, client), LIMIT_ANULOWANIA_S, "wywolanie runnera")
    return _zdarzenia(wyslane)


def _plik_bez_praw_wykonania(tmp_path) -> str:
    plik = tmp_path / "nie-do-uruchomienia"
    plik.write_text("#!/bin/sh\n")
    plik.chmod(0o644)
    return str(plik)


@pytest.mark.parametrize("program, nazwa_bledu", [
    ("nie-ma-takiego-programu-har126", "FileNotFoundError"),
    (None, "PermissionError"),
], ids=["brak_programu", "brak_praw"])
async def test_operacja_ktorej_proces_nie_wystartowal_konczy_krok_step_failed_a_potem_run_finished(
    tmp_path, program, nazwa_bledu,
):
    komenda = program or _plik_bez_praw_wykonania(tmp_path)

    zdarzenia = _zdarzenia(await _uruchom([komenda]))

    assert [z["kind"] for z in zdarzenia] == ["step_failed", "run_finished"]
    assert zdarzenia[0]["text"] == f"nie mozna uruchomic operacji: {nazwa_bledu}"
    assert zdarzenia[0]["cost_usd"] is None and zdarzenia[0]["cost_source"] == "unavailable"
    assert {(z["run_id"], z["step_id"]) for z in zdarzenia} == {(RUN_ID, STEP_ID)}


async def test_nieprzyjety_step_failed_po_nieudanym_starcie_procesu_nie_pozwala_zglosic_konca_runu():
    zdarzenia = _zdarzenia(await _uruchom(["nie-ma-takiego-programu-har126"], status=400))

    assert [z["kind"] for z in zdarzenia] == ["step_failed"]


async def test_nieprzyjety_step_failed_z_odmowy_operacji_spoza_listy_jest_w_logu_z_rodzajem_zdarzenia(
    capsys,
):
    polecenie = {"type": "run_step", "run_id": RUN_ID, "step_id": STEP_ID, "operacja": "nie-ma"}
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(400)

    runner, _ = _runner(zatrzymaj_po=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(runner._krok_z_polecenia(client, polecenie), LIMIT_ANULOWANIA_S, "odmowa")

    assert [z["kind"] for z in _zdarzenia(wyslane)] == ["step_failed"]
    assert (f"koniec runu niewyslany: run_id={RUN_ID} - "
            "zdarzenie kroku step_failed nie zostalo przyjete") in capsys.readouterr().err.splitlines()


async def test_przyjeta_odmowa_operacji_spoza_listy_jest_po_step_failed_zamknieta_run_finished():
    polecenie = {"type": "run_step", "run_id": RUN_ID, "step_id": STEP_ID, "operacja": "nie-ma"}
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200)

    runner, _ = _runner(zatrzymaj_po=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(runner._krok_z_polecenia(client, polecenie), LIMIT_ANULOWANIA_S, "odmowa")

    assert [z["kind"] for z in _zdarzenia(wyslane)] == ["step_failed", "run_finished"]


async def test_nieprzyjety_step_failed_po_nieudanym_starcie_jest_w_logu_z_rodzajem_zdarzenia(
    capsys,
):
    await _uruchom(["nie-ma-takiego-programu-har126"], status=400)

    assert (f"koniec runu niewyslany: run_id={RUN_ID} - "
            "zdarzenie kroku step_failed nie zostalo przyjete") in capsys.readouterr().err.splitlines()


async def test_runner_po_nieudanym_starcie_procesu_przyjmuje_kolejne_polecenia(monkeypatch):
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "zla": ("nie-ma-takiego-programu-har126",), "hej": ("sh", "-c", "echo hej")})

    zdarzenia = await _przetworz(
        [_krok("run-zly", "zla"), _krok("run-2", "hej")],
        lambda z: sum(e["kind"] == "run_finished" for e in z) == 2,
        limit_nazwa="koniec obu krokow")

    assert [e["kind"] for e in zdarzenia if e["run_id"] == "run-zly"] == [
        "step_failed", "run_finished"]
    assert [e["kind"] for e in zdarzenia if e["run_id"] == "run-2"] == [
        "output", "step_finished", "run_finished"]


@pytest.mark.parametrize("blad", [BladBufora("uszkodzony"), BrakMiejsca("pelny dysk")],
                         ids=["bufor_niedostepny", "brak_miejsca"])
async def test_porazka_startu_procesu_przy_niedzialajacym_buforze_daje_step_aborted_z_powodem_bufora(
    tmp_path, blad,
):
    zdarzenia = await _z_zepsutym_buforem(
        tmp_path, blad,
        lambda r, c: r.run_command(c, RUN_ID, STEP_ID, ["nie-ma-takiego-programu-har126"]))

    assert [(z["kind"], z["text"]) for z in zdarzenia] == [("step_aborted", POWOD_BUFORA)]


@pytest.mark.parametrize("blad, tekst", BLEDY_BUFORA, ids=BLEDY_BUFORA_IDS)
async def test_krok_z_dzialajacym_procesem_przy_niedzialajacym_buforze_ma_powod_zalezny_od_bledu(
    tmp_path, blad, tekst,
):
    zdarzenia = await _z_zepsutym_buforem(
        tmp_path, blad,
        lambda r, c: r.run_command(c, RUN_ID, STEP_ID, ["sh", "-c", "echo hej"]))

    assert [(z["kind"], z["text"]) for z in zdarzenia] == [("step_aborted", tekst)]


@pytest.mark.parametrize("blad, tekst", BLEDY_BUFORA, ids=BLEDY_BUFORA_IDS)
async def test_odmowa_operacji_spoza_listy_przy_niedzialajacym_buforze_ma_powod_zalezny_od_bledu(
    tmp_path, blad, tekst,
):
    polecenie = {"type": "run_step", "run_id": RUN_ID, "step_id": STEP_ID,
                 "operacja": "nie-ma-takiej-operacji"}

    zdarzenia = await _z_zepsutym_buforem(
        tmp_path, blad, lambda r, c: r._krok_z_polecenia(c, polecenie))

    assert [(z["kind"], z["text"]) for z in zdarzenia] == [("step_aborted", tekst)]


# --- HAR-126: krok bez step_id, wyjatek w zadaniu runu i odrzucony token w zadaniu -------------

KOMUNIKAT_BEZ_RUN_ID = "polecenie run_step bez poprawnego run_id odrzucone"
POWOD_BEZ_STEP_ID = "polecenie run_step bez poprawnego step_id"
TEKST_WYJATKU_ZADANIA = "zadanie runu zakonczone bledem: RuntimeError"


@pytest.mark.parametrize("pola", [{}, {"step_id": ""}, {"step_id": 5}],
                         ids=["brak_step_id", "pusty_step_id", "step_id_nie_napis"])
async def test_krok_z_run_id_ale_bez_poprawnego_step_id_konczy_run_porazka_kroku_run_bez_procesu(
    monkeypatch, tmp_path, capsys, pola,
):
    plik = tmp_path / "start"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "odrzucony": ("sh", "-c", f"touch {plik}"), "hej": ("sh", "-c", "echo hej")})
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_step", "run_id": "r1", "operacja": "odrzucony", **pola})
            strumien.wyslij(_krok("run-2", "hej"))
            await czekaj_na_warunek(
                lambda: sum(z["kind"] == "run_finished" for z in zdarzenia) == 2,
                LIMIT_ANULOWANIA_S, "koniec obu runow po odrzuconym poleceniu")
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadan runow")

            assert not runner._zatrzymany.is_set()
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert not plik.exists()
    # skutek w danych kokpitu: run bez poprawnego step_id konczy sie porazka kroku "run"
    assert [(z["kind"], z["step_id"], z["text"]) for z in zdarzenia if z["run_id"] == "r1"] == [
        ("step_failed", "run", POWOD_BEZ_STEP_ID), ("run_finished", "run", "")]
    assert [z["kind"] for z in zdarzenia if z["run_id"] == "run-2"] == [
        "output", "step_finished", "run_finished"]
    assert capsys.readouterr().err.splitlines().count(KOMUNIKAT_BEZ_RUN_ID) == 0


def _zepsute_zadania(monkeypatch) -> asyncio.Event:
    """Silnik, wznowienie i krok rzucaja RuntimeError z sekretem w tresci; Event sygnalizuje start."""
    wystartowal = asyncio.Event()

    async def rzuc(*args, **kwargs):
        wystartowal.set()
        raise RuntimeError("sekret-xyz")

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", rzuc)
    monkeypatch.setattr(runner_client.silnik, "wznow_run", rzuc)
    monkeypatch.setattr(Runner, "_krok_z_polecenia", rzuc)
    return wystartowal


def _polecenie_runu(rodzaj: str, **pola) -> dict:
    podstawa = {
        "run_step": {"type": "run_step", "run_id": "r1", "step_id": "s9", "operacja": "hej"},
        "run_workflow": {"type": "run_workflow", "run_id": "r1", "workflow": "w",
                         "sciezka": "/repo"},
        "resume": {"type": "resume", "run_id": "r1", "step_id": "s3", "decyzja": "approve"},
    }[rodzaj]
    return {**podstawa, **pola}


async def _zadanie_runu_do_konca(
    polecenie: dict, wystartowal: asyncio.Event, *, status_zdarzen: int = 200,
    bufor: Bufor | None = None,
) -> list[dict]:
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien, status_zdarzen=status_zdarzen)
    odczekania = Odczekania()
    nazwane = {} if bufor is None else {"bufor": bufor}
    runner = Runner("http://kokpit.test", TOKEN, transport=transport, odczekaj=odczekania,
                    **nazwane)
    odczekania.runner = runner
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(polecenie)
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start zadania runu")
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")
    return zdarzenia


@pytest.mark.parametrize("polecenie, step_id", [
    (_polecenie_runu("run_step"), "s9"),
    (_polecenie_runu("resume"), "s3"),
    (_polecenie_runu("run_workflow"), "run"),
    (_polecenie_runu("run_workflow", step_id=""), "run"),
    (_polecenie_runu("run_workflow", step_id=7), "run"),
    (_polecenie_runu("run_workflow", step_id="abc"), "abc"),
], ids=["krok", "wznowienie", "workflow_bez_step_id", "workflow_pusty_step_id",
        "workflow_step_id_nie_napis", "workflow_z_step_id"])
async def test_zadanie_runu_ktore_sie_zepsulo_konczy_krok_i_run_porazka_a_w_logu_nie_ma_tresci_bledu(
    monkeypatch, capsys, polecenie, step_id,
):
    zdarzenia = await _zadanie_runu_do_konca(polecenie, _zepsute_zadania(monkeypatch))

    assert [(z["kind"], z["step_id"], z["run_id"]) for z in zdarzenia] == [
        ("step_failed", step_id, "r1"), ("run_finished", step_id, "r1")]
    assert zdarzenia[0]["text"] == TEKST_WYJATKU_ZADANIA
    assert zdarzenia[0]["cost_usd"] is None and zdarzenia[0]["cost_source"] == "unavailable"
    assert "sekret-xyz" not in json.dumps(zdarzenia)
    err = capsys.readouterr().err
    assert "zadanie runu 'r1' zakonczone bledem: RuntimeError" in err.splitlines()
    assert "sekret-xyz" not in err


async def test_nieprzyjety_step_failed_z_wyjatku_zadania_nie_pozwala_zglosic_konca_runu(monkeypatch):
    zdarzenia = await _zadanie_runu_do_konca(
        _polecenie_runu("run_workflow"), _zepsute_zadania(monkeypatch), status_zdarzen=400)

    assert [z["kind"] for z in zdarzenia] == ["step_failed"]


@pytest.mark.parametrize("blad", [BladBufora("uszkodzony"), BrakMiejsca("pelny dysk")],
                         ids=["bufor_niedostepny", "brak_miejsca"])
async def test_wyjatek_w_zadaniu_runu_przy_niedzialajacym_buforze_daje_step_aborted_poza_buforem(
    monkeypatch, tmp_path, blad,
):
    zdarzenia = await _zadanie_runu_do_konca(
        _polecenie_runu("run_workflow"), _zepsute_zadania(monkeypatch),
        bufor=BuforZepsuty(tmp_path / "bufor", blad))

    assert [(z["kind"], z["text"]) for z in zdarzenia] == [("step_aborted", POWOD_BUFORA)]


async def _nasluch_do_konca(runner: Runner) -> None:
    """`listen()` w zadaniu z limitem; jego wyjatek (np. `TokenOdrzucony`) wychodzi do testu."""
    nasluch = asyncio.ensure_future(runner.listen())
    try:
        await czekaj_bez_anulowania(nasluch, LIMIT_ANULOWANIA_S, "koniec nasluchu runnera")
    finally:
        if not nasluch.done():
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=LIMIT_ANULOWANIA_S)


def _emitujace_zadania(monkeypatch) -> None:
    """Silnik i wznowienie probuja wyslac zdarzenie przez `emit` (kokpit odpowie 401)."""

    async def wykonaj(cmd, emit):
        await emit({"run_id": cmd["run_id"], "step_id": "s0", "kind": "output", "text": "x"})

    async def wznow(run_id, step_id, decyzja, emit, powod=None):
        await emit({"run_id": run_id, "step_id": step_id, "kind": "output", "text": "x"})

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznow)


@pytest.mark.parametrize("rodzaj", ["run_step", "run_workflow", "resume"])
async def test_odrzucony_token_w_zadaniu_runu_konczy_nasluch_bledem_tokenu_i_zabija_inne_procesy(
    monkeypatch, tmp_path, capsys, rodzaj,
):
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "spij": _agent_kroku(plik), "hej": ("sh", "-c", "echo hej")})
    _emitujace_zadania(monkeypatch)
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien, status_zdarzen=401)
    runner, _ = _runner(transport=transport)
    strumien.wyslij(_krok("run-a", "spij"))
    nasluch = asyncio.ensure_future(runner.listen())
    try:
        pgid = await _czekaj_na_agenta(plik)
        strumien.wyslij(_polecenie_runu(rodzaj))
        with pytest.raises(TokenOdrzucony):
            await czekaj_bez_anulowania(nasluch, LIMIT_ANULOWANIA_S, "koniec nasluchu po 401")
        assert await grupa_umarla(pgid, limit=LIMIT_ANULOWANIA_S)
    finally:
        if not nasluch.done():
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=LIMIT_ANULOWANIA_S)
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert "zakonczone bledem" not in capsys.readouterr().err   # 401 to nie zwykly wyjatek zadania


@pytest.mark.parametrize("rodzaj", ["run_step", "run_workflow", "resume"])
async def test_odrzucony_token_przy_zglaszaniu_bledu_zadania_konczy_nasluch_bledem_tokenu(
    monkeypatch, capsys, rodzaj,
):
    wystartowal = _zepsute_zadania(monkeypatch)
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien, status_zdarzen=401)
    runner, _ = _runner(transport=transport)
    strumien.wyslij(_polecenie_runu(rodzaj))

    try:
        with pytest.raises(TokenOdrzucony):
            await _nasluch_do_konca(runner)
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert wystartowal.is_set()
    assert "zadanie runu 'r1' zakonczone bledem: RuntimeError" in (
        capsys.readouterr().err.splitlines())


@pytest.mark.parametrize("pola", [
    {"step_id": "s0"}, {}, {"run_id": 5, "step_id": "s0"}, {"run_id": "", "step_id": "s0"},
    {"run_id": None, "step_id": "s0"},
], ids=["brak_run_id", "brak_obu", "run_id_nie_napis", "pusty_run_id", "run_id_null"])
async def test_polecenie_run_step_bez_poprawnego_run_id_jest_odrzucone_komunikatem_bez_zdarzen(
    capsys, pola,
):
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200)

    runner, _ = _runner(zatrzymaj_po=1)
    polecenie = {"type": "run_step", "operacja": "nie-ma-takiej", **pola}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(runner._krok_z_polecenia(client, polecenie), LIMIT_ANULOWANIA_S, "odmowa")

    odczyt = capsys.readouterr()
    assert wyslane == []
    assert odczyt.err.splitlines() == [KOMUNIKAT_BEZ_RUN_ID]
    assert odczyt.out == ""


def _krok_bez_step_id(operacja: str, run_id: str = "r1", **pola) -> dict:
    return {"type": "run_step", "run_id": run_id, "operacja": operacja, **pola}


POLA_BEZ_POPRAWNEGO_STEP_ID = [{}, {"step_id": ""}, {"step_id": 7}, {"step_id": None}]
IDS_BEZ_POPRAWNEGO_STEP_ID = ["brak_step_id", "pusty_step_id", "step_id_nie_napis", "step_id_null"]


@pytest.mark.parametrize("operacja", ["nie-ma-takiej", "znana"],
                         ids=["operacja_spoza_listy", "operacja_z_listy"])
@pytest.mark.parametrize("pola", POLA_BEZ_POPRAWNEGO_STEP_ID, ids=IDS_BEZ_POPRAWNEGO_STEP_ID)
async def test_run_step_z_run_id_bez_poprawnego_step_id_zglasza_porazke_kroku_run_i_koniec_runu(
    monkeypatch, tmp_path, capsys, pola, operacja,
):
    plik = tmp_path / "start"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"znana": ("sh", "-c", f"touch {plik}")})
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200)

    runner, _ = _runner(zatrzymaj_po=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(
            runner._krok_z_polecenia(client, _krok_bez_step_id(operacja, **pola)),
            LIMIT_ANULOWANIA_S, "krok bez step_id")

    zdarzenia = _zdarzenia(wyslane)
    assert [(z["kind"], z["run_id"], z["step_id"], z["text"]) for z in zdarzenia] == [
        ("step_failed", "r1", "run", POWOD_BEZ_STEP_ID), ("run_finished", "r1", "run", "")]
    assert all(z["cost_usd"] is None and z["cost_source"] == "unavailable" for z in zdarzenia)
    assert not plik.exists()                       # krok z listy tez nie startuje bez step_id
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("blad", [BladBufora("uszkodzony"), BrakMiejsca("pelny dysk")],
                         ids=["bufor_niedostepny", "brak_miejsca"])
@pytest.mark.parametrize("pola", POLA_BEZ_POPRAWNEGO_STEP_ID, ids=IDS_BEZ_POPRAWNEGO_STEP_ID)
async def test_porazka_kroku_bez_step_id_przy_niedzialajacym_buforze_daje_step_aborted_poza_buforem(
    tmp_path, blad, pola,
):
    zdarzenia = await _z_zepsutym_buforem(
        tmp_path, blad,
        lambda r, c: r._krok_z_polecenia(c, _krok_bez_step_id("nie-ma-takiej", **pola)))

    assert [(z["kind"], z["run_id"], z["step_id"], z["text"]) for z in zdarzenia] == [
        ("step_aborted", "r1", "run", POWOD_BUFORA)]


async def test_krok_bez_step_id_z_za_dlugim_id_runu_zostawia_tylko_wpis_w_logu_i_nie_psuje_runnera(capsys):
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(200)

    runner, _ = _runner(zatrzymaj_po=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await czekaj(
            runner._krok_z_polecenia(client, _krok_bez_step_id("nie-ma-takiej", run_id="c" * 40)),
            LIMIT_ANULOWANIA_S, "krok bez step_id")

    assert wyslane == []
    (linia,) = capsys.readouterr().err.splitlines()
    assert linia.startswith(
        f"koniec runu niewyslany: run_id={'c' * 36!r} - zdarzenie step_failed: run_id: ")


async def test_nasluch_zatrzymany_przez_stop_po_zapamietanym_odrzuceniu_tokenu_konczy_sie_bez_bledu_tokenu():
    # zadanie runu dostalo 401 wczesniej, ale to czlowiek zatrzymuje runnera: nasluch ma wrocic
    # normalnie, a nie podniesc zapamietany `TokenOdrzucony`
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    nasluch = asyncio.ensure_future(runner.listen())
    try:
        await czekaj_na_warunek(
            lambda: runner.runner_id is not None, LIMIT_ANULOWANIA_S, "rejestracja runnera")
        runner._token_odrzucony = TokenOdrzucony()

        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")
        wynik = await czekaj_bez_anulowania(nasluch, LIMIT_ANULOWANIA_S, "koniec nasluchu po stop()")
    finally:
        if not nasluch.done():
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=LIMIT_ANULOWANIA_S)

    assert wynik is None
    assert nasluch.cancelling() == 0


async def test_nasluch_zakonczony_odrzuconym_tokenem_zadania_runu_wraca_do_stanu_bez_anulowania(
    monkeypatch,
):
    # prosba o anulowanie, ktora zadanie runu wystawilo nasluchowi, jest zdjeta przed podniesieniem
    # `TokenOdrzucony`: inaczej zadanie nasluchu konczy sie z zaleglym licznikiem anulowan
    _emitujace_zadania(monkeypatch)
    strumien = StrumienPolecen()
    transport, _ = kokpit_ze_strumieniem(strumien, status_zdarzen=401)
    runner, _ = _runner(transport=transport)
    strumien.wyslij(_polecenie_runu("run_workflow"))
    nasluch = asyncio.ensure_future(runner.listen())
    try:
        with pytest.raises(TokenOdrzucony):
            await czekaj_bez_anulowania(nasluch, LIMIT_ANULOWANIA_S, "koniec nasluchu po 401")
    finally:
        if not nasluch.done():
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=LIMIT_ANULOWANIA_S)
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert nasluch.cancelling() == 0


async def test_token_odrzucony_w_zadaniu_bez_nasluchu_jest_zapamietany_bez_bledu():
    # `_zadanie` (zadanie `listen()`) nie istnieje: nie ma czego anulowac i nie wolno siegac po nie
    runner = Runner("http://kokpit.test", TOKEN)

    async def odrzucony():
        raise TokenOdrzucony()

    await czekaj(runner._pilnuj_zadania("r1", {}, odrzucony()), LIMIT_ANULOWANIA_S, "zadanie")

    assert isinstance(runner._token_odrzucony, TokenOdrzucony)


async def test_token_odrzucony_przy_zglaszaniu_bledu_zadania_bez_nasluchu_jest_zapamietany_bez_bledu():
    runner = Runner(
        "http://kokpit.test", TOKEN,
        transport=httpx.MockTransport(lambda request: httpx.Response(401)))

    async def zepsute():
        raise RuntimeError("sekret-xyz")

    await czekaj(
        runner._pilnuj_zadania("r1", {"step_id": "s0"}, zepsute()), LIMIT_ANULOWANIA_S, "zadanie")

    assert isinstance(runner._token_odrzucony, TokenOdrzucony)


async def test_nieudane_zgloszenie_porazki_zadania_runu_zostawia_w_logu_obciete_id_runu_bez_tresci_bledu(
    monkeypatch, capsys,
):
    wystartowal = asyncio.Event()

    async def wykonaj(cmd, emit):
        wystartowal.set()
        raise RuntimeError("tresc-pierwszego")

    async def zgloszenie_pada(self, *args, **kwargs):
        raise ValueError("sekret-xyz")

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    monkeypatch.setattr(Runner, "_zglos_porazke_kroku", zgloszenie_pada)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_workflow", "run_id": "q" * 40, "workflow": "w",
                             "sciezka": "/repo"})
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start zadania silnika")
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    odczyt = capsys.readouterr()
    assert f"zadanie runu '{'q' * 36}' zakonczone bledem: ValueError" in odczyt.err.splitlines()
    assert "sekret-xyz" not in odczyt.err + odczyt.out
    assert odczyt.out == ""
    assert zdarzenia == []


# --- HAR-126: step_aborted trwajacych krokow po cancel_run --------------------------------------

POLECENIA_RUNU = {
    "run_workflow": {"type": "run_workflow", "run_id": RUN_ID, "workflow": "w", "sciezka": "/repo"},
    "resume": {"type": "resume", "run_id": RUN_ID, "step_id": "s0", "decyzja": "approve"},
}


def _silnik_ktory_emituje_i_wisi(monkeypatch, emitowane: list[dict]) -> asyncio.Event:
    """Atrapa silnika (workflow i wznowienia): emituje `emitowane` (z run_id polecenia), potem wisi.
    Zwrocone zdarzenie jest ustawiane po wyemitowaniu wszystkich zdarzen (tuz przed zawieszeniem)."""
    wystartowal = asyncio.Event()

    async def zawies(emit, run_id: str) -> None:
        for zdarzenie in emitowane:
            with contextlib.suppress(NiewaznePoleZdarzenia):     # zdarzenie ponad limit pola
                await emit({"run_id": run_id, **zdarzenie})
        wystartowal.set()
        await asyncio.sleep(300)

    async def wykonaj(cmd, emit):
        await zawies(emit, cmd["run_id"])

    async def wznow(run_id, step_id, decyzja, emit, powod=None):
        await zawies(emit, run_id)

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznow)
    return wystartowal


async def _anuluj_run_z_krokami_w_toku(
    monkeypatch, rodzaj: str, emitowane: list[dict], *,
    dochodzi_do_kokpitu: int | None = None, statusy_wg_rodzaju: dict[str, int] | None = None,
) -> tuple[Runner, list[dict]]:
    """Run (`run_workflow` albo `resume`) emituje `emitowane` i wisi; potem `cancel_run`.
    Wraca po koncu zadania runu i `stop()`: zdarzenia kokpitu sa kompletne.
    `dochodzi_do_kokpitu`: ile zdarzen silnika dotrze do kokpitu (domyslnie wszystkie)."""
    wystartowal = _silnik_ktory_emituje_i_wisi(monkeypatch, emitowane)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(
        strumien, statusy_wg_rodzaju=statusy_wg_rodzaju)
    runner, _ = _runner(transport=transport)
    oczekiwane = len(emitowane) if dochodzi_do_kokpitu is None else dochodzi_do_kokpitu
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(POLECENIA_RUNU[rodzaj])
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "silnik wyemitowal zdarzenia")
            await czekaj_na_warunek(
                lambda: len(zdarzenia) >= oczekiwane, LIMIT_ANULOWANIA_S,
                "zdarzenia silnika w kokpicie")
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")
    return runner, zdarzenia


def _start(step_id: str) -> dict:
    return {"step_id": step_id, "kind": "step_started"}


@pytest.mark.parametrize("rodzaj", ["run_workflow", "resume"])
async def test_po_cancel_run_kokpit_dostaje_step_aborted_kroku_w_toku_z_workflow_i_z_wznowienia(
    monkeypatch, rodzaj,
):
    runner, zdarzenia = await _anuluj_run_z_krokami_w_toku(monkeypatch, rodzaj, [_start("s1")])

    assert _step_aborted(zdarzenia) == [(RUN_ID, "s1", TEKST_ANULOWANIA)]
    assert not {z["kind"] for z in zdarzenia} & RODZAJE_KONCA
    # po koncu zadania runner nie trzyma sladu po runie ani po anulowanym zadaniu
    assert runner._kroki_w_toku == {}
    assert runner._anulowane_przez_czlowieka == set()
    assert runner._zamykania == set()


@pytest.mark.parametrize("rodzaj", ["run_step", "run_workflow", "resume"])
async def test_stop_runnera_z_trwajacym_krokiem_nie_zglasza_step_aborted_bo_to_nie_anulowanie_przez_czlowieka(
    monkeypatch, tmp_path, rodzaj,
):
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"spij": _agent_kroku(plik)})
    _silnik_ktory_emituje_i_wisi(monkeypatch, [_start("s1")])
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    pgid = None
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            if rodzaj == "run_step":
                strumien.wyslij(_krok(RUN_ID, "spij"))
                pgid = await _czekaj_na_agenta(plik)
            else:
                strumien.wyslij(POLECENIA_RUNU[rodzaj])
                await czekaj_na_warunek(
                    lambda: any(z["kind"] == "step_started" for z in zdarzenia),
                    LIMIT_ANULOWANIA_S, "start kroku w silniku")
            assert RUN_ID in runner._runy           # krok trwa, zadanie runu zyje
            await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")
            assert runner._runy == {}               # zadanie runu skonczylo sie przed powrotem stop()
            if pgid is not None:                    # HAR-201: po stop() krok nie zostawia sieroty
                pid_agenta = int(plik.read_text().split()[0])
                assert await grupa_umarla(pgid, limit=LIMIT_ANULOWANIA_S), "grupa kroku zyje po stop()"
                assert not czy_zyje(pid_agenta)
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert _step_aborted(zdarzenia) == []
    assert not {z["kind"] for z in zdarzenia} & RODZAJE_KONCA


def _kokpit_z_odpowiedziami(strumien: StrumienPolecen):
    """Jak `kokpit_ze_strumieniem`, a POST /api/runners/odpowiedzi trafia do trzeciej listy."""
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    odpowiedzi: list[dict] = []
    bazowy = transport.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runners/odpowiedzi":
            odpowiedzi.append(json.loads(request.content))
            return httpx.Response(204)
        return bazowy(request)

    return httpx.MockTransport(handler), zdarzenia, odpowiedzi


@pytest.mark.parametrize("rodzaj", ["run_step", "run_workflow", "resume"])
async def test_zapytanie_kokpitu_w_trakcie_trwajacego_kroku_dostaje_odpowiedz_przed_koncem_kroku(
    monkeypatch, tmp_path, rodzaj,
):
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {"spij": _agent_kroku(plik)})
    _silnik_ktory_emituje_i_wisi(monkeypatch, [_start("s1")])
    strumien = StrumienPolecen()
    transport, zdarzenia, odpowiedzi = _kokpit_z_odpowiedziami(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            if rodzaj == "run_step":
                strumien.wyslij(_krok(RUN_ID, "spij"))
                await _czekaj_na_agenta(plik)
            else:
                strumien.wyslij(POLECENIA_RUNU[rodzaj])
                await czekaj_na_warunek(
                    lambda: any(z["kind"] == "step_started" for z in zdarzenia),
                    LIMIT_ANULOWANIA_S, "start kroku w silniku")
            assert RUN_ID in runner._runy                   # krok trwa i nie skonczy sie sam

            strumien.wyslij({"type": "ping", "req_id": "z-w-trakcie",
                             "nadano_o": time.time(), "wazne_s": 15.0})
            await czekaj_na_warunek(
                lambda: odpowiedzi, LIMIT_ANULOWANIA_S, "odpowiedz na zapytanie w trakcie kroku")

            assert odpowiedzi == [{"req_id": "z-w-trakcie", "ok": True, "wynik": {"pong": True}}]
            assert RUN_ID in runner._runy                   # odpowiedz przyszla, a krok dalej trwa
            assert not {z["kind"] for z in zdarzenia} & RODZAJE_KONCA
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")


async def test_dwanascie_krokow_w_toku_jednego_runu_dostaje_step_aborted_po_kolei_posortowane(
    monkeypatch,
):
    kroki = [f"s{n:02d}" for n in range(12)]

    _, zdarzenia = await _anuluj_run_z_krokami_w_toku(
        monkeypatch, "run_workflow", [_start(s) for s in reversed(kroki)])

    assert _step_aborted(zdarzenia) == [(RUN_ID, s, TEKST_ANULOWANIA) for s in kroki]


@pytest.mark.parametrize("koniec", ["step_finished", "step_failed"])
@pytest.mark.parametrize("rodzaj", ["run_workflow", "resume"])
async def test_krok_zakonczony_przed_cancel_run_nie_dostaje_step_aborted_a_trwajacy_dostaje(
    monkeypatch, koniec, rodzaj,
):
    emitowane = [_start("s1"), {"step_id": "s1", "kind": koniec}, _start("s2")]

    _, zdarzenia = await _anuluj_run_z_krokami_w_toku(monkeypatch, rodzaj, emitowane)

    assert _step_aborted(zdarzenia) == [(RUN_ID, "s2", TEKST_ANULOWANIA)]


@pytest.mark.parametrize("rodzaj", ["run_workflow", "resume"])
async def test_step_aborted_niesie_agenta_i_proba_ze_step_started_dla_dwoch_prob_i_dwoch_agentow_jednego_kroku(
    monkeypatch, rodzaj,
):
    def start(step_id, agent_id=None, attempt=None):
        zdarzenie = _start(step_id)
        if agent_id is not None:
            zdarzenie["agent_id"] = agent_id
        if attempt is not None:
            zdarzenie["attempt"] = attempt
        return zdarzenie

    emitowane = [
        start("s1", "a1", 1), start("s1", "a2", 1),
        {"step_id": "s1", "agent_id": "a1", "attempt": 1, "kind": "step_finished"},
        start("s1", "a1", 2), start("s2"),
    ]

    _, zdarzenia = await _anuluj_run_z_krokami_w_toku(monkeypatch, rodzaj, emitowane)

    aborty = [z for z in zdarzenia if z["kind"] == "step_aborted"]
    # pierwsza proba agenta a1 jest zakonczona; zostaja: a1 w probie 2, a2 w probie 1 i s2 bez agenta
    assert [(z["step_id"], z["agent_id"], z["attempt"], z["text"]) for z in aborty] == [
        ("s1", "a1", 2, TEKST_ANULOWANIA), ("s1", "a2", 1, TEKST_ANULOWANIA),
        ("s2", "", 1, TEKST_ANULOWANIA)]
    assert {z["run_id"] for z in aborty} == {RUN_ID}


@pytest.mark.parametrize("przyczyna, koniec, statusy, dochodzi", [
    ("pole_ponad_limit", {"step_id": "s1", "kind": "step_finished", "cost_usd": -1}, None, 1),
    ("kokpit_odrzucil_koniec", {"step_id": "s1", "kind": "step_finished"},
     {"step_finished": 400}, 2),
], ids=["pole_ponad_limit", "kokpit_odrzucil_koniec"])
@pytest.mark.parametrize("rodzaj", ["run_workflow", "resume"])
async def test_krok_ktorego_koniec_nie_dotarl_do_kokpitu_dostaje_po_anulowaniu_step_aborted(
    monkeypatch, rodzaj, przyczyna, koniec, statusy, dochodzi,
):
    # kokpit nie zna konca kroku s1 (pole ponad limit nie wychodzi z runnera, kokpit odpowiada 400),
    # wiec krok jest dla niego nadal w toku i runner ma go przerwac
    _, zdarzenia = await _anuluj_run_z_krokami_w_toku(
        monkeypatch, rodzaj, [_start("s1"), koniec],
        dochodzi_do_kokpitu=dochodzi, statusy_wg_rodzaju=statusy)

    assert _step_aborted(zdarzenia) == [(RUN_ID, "s1", TEKST_ANULOWANIA)]


async def test_zdarzenie_inne_niz_start_kroku_nie_robi_z_kroku_trwajacego_w_cancel_run(monkeypatch):
    emitowane = [{"step_id": "o1", "kind": "output", "text": "x"}, _start("s1")]

    _, zdarzenia = await _anuluj_run_z_krokami_w_toku(monkeypatch, "run_workflow", emitowane)

    assert _step_aborted(zdarzenia) == [(RUN_ID, "s1", TEKST_ANULOWANIA)]


@pytest.mark.parametrize("emitowane, oczekiwane_otwarcia", [
    ([], 0),
    ([_start("s1")], 1),
], ids=["bez_krokow_w_toku", "z_krokiem_w_toku"])
async def test_anulowanie_runu_bez_krokow_w_toku_nie_laczy_sie_z_kokpitem_a_z_krokiem_zglasza_mu_przerwanie(
    monkeypatch, emitowane, oczekiwane_otwarcia,
):
    wystartowal = _silnik_ktory_emituje_i_wisi(monkeypatch, emitowane)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    otwarcia: list[int] = []
    prawdziwy_klient = runner._klient_http

    def klient_z_licznikiem():
        otwarcia.append(1)
        return prawdziwy_klient()

    runner._klient_http = klient_z_licznikiem
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(POLECENIA_RUNU["run_workflow"])
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start atrapy silnika")
            await czekaj_na_warunek(
                lambda: len(zdarzenia) >= len(emitowane), LIMIT_ANULOWANIA_S,
                "zdarzenia silnika w kokpicie")
            przed_anulowaniem = len(otwarcia)
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert len(otwarcia) - przed_anulowaniem == oczekiwane_otwarcia


@pytest.mark.parametrize("rodzaj", ["run_workflow", "resume"])
async def test_step_aborted_z_za_dlugim_step_id_jest_w_logu_a_nastepny_krok_dostaje_step_aborted(
    monkeypatch, capsys, rodzaj,
):
    za_dlugi = "a" * 101            # sortuje sie przed "s0", wiec idzie pierwszy
    wystartowal = asyncio.Event()

    async def przebieg(emit, run_id):
        with contextlib.suppress(NiewaznePoleZdarzenia):
            await emit({"run_id": run_id, **_start(za_dlugi)})   # krok sledzony, start odrzucony
        await emit({"run_id": run_id, **_start("s0")})
        wystartowal.set()
        await asyncio.sleep(300)

    async def wykonaj(cmd, emit):
        await przebieg(emit, cmd["run_id"])

    async def wznow(run_id, step_id, decyzja, emit, powod=None):
        await przebieg(emit, run_id)

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    monkeypatch.setattr(runner_client.silnik, "wznow_run", wznow)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij(POLECENIA_RUNU[rodzaj])
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start kroku s0")
            zadanie_runu = runner._runy[RUN_ID]
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    err = capsys.readouterr().err
    assert _step_aborted(zdarzenia) == [(RUN_ID, "s0", TEKST_ANULOWANIA)]
    assert f"step_aborted niewyslany: run {RUN_ID!r} - pola ponad limit" in err.splitlines()
    assert "zakonczone bledem" not in err
    assert zadanie_runu.cancelled()                 # anulowanie leci dalej mimo bledu pola


async def test_step_aborted_z_za_dlugim_run_id_zostawia_w_logu_obciete_do_36_znakow_id_i_nic_w_kokpicie(
    monkeypatch, capsys,
):
    run_id = "w" * 40
    wystartowal = asyncio.Event()

    async def wykonaj(cmd, emit):
        with contextlib.suppress(NiewaznePoleZdarzenia):
            await emit({"run_id": cmd["run_id"], **_start("s1")})        # krok sledzony, start odrzucony
        wystartowal.set()
        await asyncio.sleep(300)

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({**POLECENIA_RUNU["run_workflow"], "run_id": run_id})
            await czekaj(wystartowal.wait(), LIMIT_ANULOWANIA_S, "start kroku")
            strumien.wyslij({"type": "cancel_run", "run_id": run_id})
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert zdarzenia == []
    assert (f"step_aborted niewyslany: run {'w' * 36!r} - pola ponad limit"
            in capsys.readouterr().err.splitlines())


async def test_odrzucony_token_przy_wysylce_step_aborted_konczy_nasluch_bledem_tokenu(monkeypatch):
    _silnik_ktory_emituje_i_wisi(monkeypatch, [_start("s1")])
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien, statusy_wg_rodzaju={"step_aborted": 401})
    runner, _ = _runner(transport=transport)
    strumien.wyslij(POLECENIA_RUNU["run_workflow"])
    nasluch = asyncio.ensure_future(runner.listen())
    try:
        await czekaj_na_warunek(
            lambda: any(z["kind"] == "step_started" for z in zdarzenia),
            LIMIT_ANULOWANIA_S, "start kroku w kokpicie")
        strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
        with pytest.raises(TokenOdrzucony):
            await czekaj_bez_anulowania(nasluch, LIMIT_ANULOWANIA_S, "koniec nasluchu po 401")
    finally:
        if not nasluch.done():
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=LIMIT_ANULOWANIA_S)
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")


def _runner_z_krokiem_w_toku_i_kokpitem_odrzucajacym_token() -> Runner:
    runner = Runner("http://kokpit.test", TOKEN, transport=httpx.MockTransport(
        lambda request: httpx.Response(401)))
    runner._kroki_w_toku = {RUN_ID: {("s1", "", 1)}}
    return runner


async def test_odrzucony_token_przy_step_aborted_bez_nasluchu_jest_zapamietany_bez_bledu():
    # `_zadanie` (zadanie `listen()`) nie istnieje: nie ma czego anulowac i nie wolno siegac po nie
    runner = _runner_z_krokiem_w_toku_i_kokpitem_odrzucajacym_token()
    assert runner._zadanie is None

    await czekaj(runner._zglos_anulowanie_krokow(RUN_ID), LIMIT_ANULOWANIA_S, "step_aborted")

    assert isinstance(runner._token_odrzucony, TokenOdrzucony)


class _NasluchAtrapa:
    """Zadanie nasluchu z licznikiem anulowan; `zakonczone` mowi, czy zadanie juz sie skonczylo."""

    def __init__(self, zakonczone: bool) -> None:
        self.zakonczone = zakonczone
        self.anulowania = 0

    def done(self) -> bool:
        return self.zakonczone

    def cancel(self) -> None:
        self.anulowania += 1


@pytest.mark.parametrize("zakonczone, oczekiwane_anulowania", [(True, 0), (False, 1)],
                         ids=["nasluch_juz_zakonczony", "nasluch_trwa"])
async def test_odrzucony_token_przy_step_aborted_anuluje_nasluch_tylko_gdy_ten_jeszcze_trwa(
    zakonczone, oczekiwane_anulowania,
):
    runner = _runner_z_krokiem_w_toku_i_kokpitem_odrzucajacym_token()
    runner._zadanie = _NasluchAtrapa(zakonczone)

    await czekaj(runner._zglos_anulowanie_krokow(RUN_ID), LIMIT_ANULOWANIA_S, "step_aborted")

    assert isinstance(runner._token_odrzucony, TokenOdrzucony)
    assert runner._zadanie.anulowania == oczekiwane_anulowania


@pytest.mark.parametrize("run_id, zdarzenie", [
    (5, _start("s")),
    (None, _start("s")),
    (["r"], _start("s")),
    ("r", _start(7)),
    ("r", _start(None)),
    ("r", _start(["s"])),
    ("r", {"kind": "step_started"}),
    ("r", {"kind": "output", "step_id": "s"}),
    ("r", {"kind": "step_finished", "step_id": "s"}),
    ("r", {"step_id": "s"}),
    ("r", {**_start("s"), "agent_id": 7}),
    ("r", {**_start("s"), "attempt": "2"}),
    ("r", {**_start("s"), "attempt": True}),
], ids=["run_id_liczba", "run_id_null", "run_id_lista", "step_id_liczba", "step_id_null",
        "step_id_lista", "brak_step_id", "output", "step_finished", "brak_rodzaju",
        "agent_id_liczba", "attempt_napis", "attempt_bool"])
async def test_krok_nie_jest_sledzony_bez_step_started_z_poprawna_tozsamoscia(
    run_id, zdarzenie,
):
    runner = Runner("http://kokpit.test", TOKEN)

    runner._sledz_start_kroku(run_id, zdarzenie)

    assert runner._kroki_w_toku == {}


async def test_start_kroku_dopisuje_tozsamosc_kroku_do_trwajacych_tego_runu_obok_wczesniejszych():
    runner = Runner("http://kokpit.test", TOKEN)

    runner._sledz_start_kroku("r", _start("s"))
    runner._sledz_start_kroku("r", _start("t"))
    runner._sledz_start_kroku("r", {**_start("s"), "agent_id": "a", "attempt": 2})
    runner._sledz_start_kroku("q", _start("s"))

    assert runner._kroki_w_toku == {
        "r": {("s", "", 1), ("t", "", 1), ("s", "a", 2)}, "q": {("s", "", 1)}}


@pytest.mark.parametrize("zdarzenie, oczekiwana", [
    ({"step_id": "s"}, ("s", "", 1)),
    ({"step_id": "s", "agent_id": "a", "attempt": 3}, ("s", "a", 3)),
    ({"step_id": "s", "agent_id": "", "attempt": 0}, ("s", "", 0)),
    ({"step_id": "s", "attempt": 2}, ("s", "", 2)),
    ({"step_id": "s", "agent_id": "a"}, ("s", "a", 1)),
    ({"step_id": "s", "agent_id": None, "attempt": None}, ("s", "", 1)),
    ({"step_id": "s", "agent_id": None, "attempt": 4}, ("s", "", 4)),
    ({"step_id": "s", "agent_id": "a", "attempt": None}, ("s", "a", 1)),
    ({"step_id": 7}, None),
    ({"step_id": None}, None),
    ({}, None),
    ({"step_id": "s", "agent_id": 7}, None),
    ({"step_id": "s", "attempt": "2"}, None),
    ({"step_id": "s", "attempt": 1.5}, None),
    ({"step_id": "s", "attempt": True}, None),
    ({"step_id": "s", "attempt": False}, None),
], ids=["domyslne", "pelna", "proba_zero", "domyslny_agent", "domyslna_proba",
        "jawne_null_w_agencie_i_probie", "jawne_null_w_agencie", "jawne_null_w_probie",
        "step_id_liczba", "step_id_null", "pusty", "agent_id_liczba", "attempt_napis",
        "attempt_ulamek", "attempt_true", "attempt_false"])
def test_tozsamosc_kroku_to_krok_agent_i_proba_z_domyslnymi_pustym_agentem_i_proba_1(
    zdarzenie, oczekiwana,
):
    assert runner_client._tozsamosc_kroku(zdarzenie) == oczekiwana


@pytest.mark.parametrize("kind", ["step_finished", "step_failed"])
async def test_koniec_kroku_zdejmuje_z_trwajacych_tylko_krok_o_tej_samej_tozsamosci_w_swoim_runie(
    kind,
):
    runner = Runner("http://kokpit.test", TOKEN)
    runner._kroki_w_toku = {
        "r": {("s", "", 1), ("s", "a", 2), ("s", "b", 2), ("s", "a", 3), ("t", "", 1)},
        "q": {("s", "", 1)},
    }

    runner._sledz_koniec_kroku("r", {"kind": kind, "step_id": "s", "agent_id": "a", "attempt": 2})
    runner._sledz_koniec_kroku("r", {"kind": kind, "step_id": "s"})

    assert runner._kroki_w_toku == {
        "r": {("s", "b", 2), ("s", "a", 3), ("t", "", 1)}, "q": {("s", "", 1)}}


@pytest.mark.parametrize("run_id, zdarzenie", [
    ("r", {"kind": "output", "step_id": "s"}),
    ("r", {"kind": "step_started", "step_id": "s"}),
    ("r", {"kind": "step_aborted", "step_id": "s"}),
    ("r", {"step_id": "s"}),
    ("r", {"kind": "step_finished", "step_id": "inny"}),
    ("r", {"kind": "step_finished", "step_id": 7}),
    ("r", {"kind": "step_finished", "step_id": ["s"]}),
    ("r", {"kind": "step_finished"}),
    ("nieznany", {"kind": "step_finished", "step_id": "s"}),
    (5, {"kind": "step_finished", "step_id": "s"}),
    (["r"], {"kind": "step_finished", "step_id": "s"}),
    ("r", {"kind": "step_finished", "step_id": "s", "agent_id": "inny"}),
    ("r", {"kind": "step_finished", "step_id": "s", "attempt": 2}),
    ("r", {"kind": "step_finished", "step_id": "s", "agent_id": 7}),
    ("r", {"kind": "step_finished", "step_id": "s", "attempt": "1"}),
    ("r", {"kind": "step_finished", "step_id": "s", "attempt": True}),
], ids=["output", "start", "przerwanie", "brak_rodzaju", "inny_krok", "step_id_liczba",
        "step_id_lista", "brak_step_id", "nieznany_run", "run_id_liczba", "run_id_lista",
        "inny_agent", "inna_proba", "agent_id_liczba", "attempt_napis", "attempt_bool"])
async def test_zdarzenie_ktore_nie_konczy_kroku_runu_zostawia_trwajace_kroki_bez_zmian(
    run_id, zdarzenie,
):
    runner = Runner("http://kokpit.test", TOKEN)
    runner._kroki_w_toku = {"r": {("s", "", 1)}}

    runner._sledz_koniec_kroku(run_id, zdarzenie)

    assert runner._kroki_w_toku == {"r": {("s", "", 1)}}


@pytest.mark.parametrize("run_id, step_id, w_chwili_wywolania", [
    ("r1", "s0", {"r1": {("s0", "", 1)}}),
    ("r1", None, {}),
    ("r1", "", {}),
    ("r1", 7, {}),
    (5, "s0", {}),
    ("", "s0", {}),
    (None, "s0", {}),
    (["r1"], "s0", {}),
], ids=["poprawne", "brak_step_id", "pusty_step_id", "step_id_liczba", "run_id_liczba",
        "pusty_run_id", "brak_run_id", "run_id_lista"])
async def test_krok_runu_jest_sledzony_od_polecenia_do_konca_tylko_dla_poprawnych_identyfikatorow(
    monkeypatch, run_id, step_id, w_chwili_wywolania,
):
    migawki: list[dict] = []

    async def krok(self, client, cmd):
        migawki.append({k: set(v) for k, v in self._kroki_w_toku.items()})

    monkeypatch.setattr(Runner, "_krok_z_polecenia", krok)
    runner = Runner("http://kokpit.test", TOKEN, transport=httpx.MockTransport(
        lambda request: httpx.Response(200)))
    polecenie = {"type": "run_step", "run_id": run_id, "step_id": step_id, "operacja": "x"}

    await czekaj(runner._krok_runu(polecenie), LIMIT_ANULOWANIA_S, "krok runu")

    assert migawki == [w_chwili_wywolania]
    # po powrocie z kroku nie jest juz w toku (wpis runu znika dopiero z koncem zadania)
    assert runner._kroki_w_toku == ({"r1": set()} if w_chwili_wywolania else {})


async def test_krok_ktory_skonczyl_sie_przed_cancel_run_nie_dostaje_step_aborted_gdy_run_ma_nowsze_zadanie(
    monkeypatch, tmp_path,
):
    # run_step s_a konczy sie, ale `_runy` wskazuje juz na nowsze zadanie runu (s_b): wpis s_a
    # zniknie z trwajacych tylko dzieki zdjeciu kroku w `_krok_runu`, nie dzieki koncowi zadania
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "szybki": ("sh", "-c", "sleep 0.3; echo hej"), "spij": _agent_kroku(plik)})
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({**_krok(RUN_ID, "szybki"), "step_id": "s_a"})
            strumien.wyslij({**_krok(RUN_ID, "spij"), "step_id": "s_b"})
            pgid = await _czekaj_na_agenta(plik)
            await czekaj_na_warunek(
                lambda: any(z["step_id"] == "s_a" and z["kind"] == "step_finished"
                            for z in zdarzenia),
                LIMIT_ANULOWANIA_S, "koniec kroku s_a")
            strumien.wyslij({"type": "cancel_run", "run_id": RUN_ID})
            assert await grupa_umarla(pgid, limit=LIMIT_ANULOWANIA_S)
            await czekaj_na_warunek(
                lambda: not runner._runy, LIMIT_ANULOWANIA_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert _step_aborted(zdarzenia) == [(RUN_ID, "s_b", TEKST_ANULOWANIA)]


async def test_krok_konczacy_sie_po_nowszym_zadaniu_tego_samego_runu_konczy_sie_bez_bledu(
    monkeypatch, capsys,
):
    # nowsze zadanie (s_b) konczy sie pierwsze i zdejmuje wpis runu; starszy krok (s_a) nie ma
    # juz czego zdejmowac, ale jego koniec nie moze sie wywrocic
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "wolny": ("sh", "-c", "sleep 0.5; echo hej"), "szybki": ("sh", "-c", "echo hej")})
    prawdziwy_krok_runu = Runner._krok_runu
    zakonczone: list[str] = []

    async def krok_runu_z_zapisem_konca(self, cmd):
        await prawdziwy_krok_runu(self, cmd)
        zakonczone.append(cmd["step_id"])

    monkeypatch.setattr(Runner, "_krok_runu", krok_runu_z_zapisem_konca)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner, _ = _runner(transport=transport)
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({**_krok(RUN_ID, "wolny"), "step_id": "s_a"})
            strumien.wyslij({**_krok(RUN_ID, "szybki"), "step_id": "s_b"})
            await czekaj_na_warunek(
                lambda: sorted(zakonczone) == ["s_a", "s_b"], LIMIT_ANULOWANIA_S,
                "koniec obu krokow")
    finally:
        await czekaj(runner.stop(), LIMIT_ANULOWANIA_S, "stop() runnera")

    assert zakonczone == ["s_b", "s_a"]
    assert "zakonczone bledem" not in capsys.readouterr().err

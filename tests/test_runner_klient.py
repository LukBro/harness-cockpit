"""Runner: wynik komendy trafia do kokpitu jako zdarzenie końcowe kroku.

Transport HTTP jest przechwycony (`httpx.MockTransport`), a komendy to prawdziwe
procesy — sprawdzamy produkcyjny kod `Runner.run_command`, nie jego kopię.
Asercje dotyczą rodzaju zdarzenia (`kind`); pola pomiarowe (`duration_ms`, `cost_usd`,
`cost_source`) sprawdzają osobne testy z sekcji HAR-35.
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from runner.bufor import Bufor
from runner.client import Runner, TokenOdrzucony

RUN_ID = "run-1"
STEP_ID = "krok-1"


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

    koncowe = zdarzenia[-1]
    assert koncowe["kind"] == "step_failed"
    assert koncowe["text"] == "kod wyjscia 3"
    assert koncowe["run_id"] == RUN_ID
    assert koncowe["step_id"] == STEP_ID
    assert all(z["kind"] != "step_finished" for z in zdarzenia)


async def test_komenda_z_kodem_zero_konczy_krok_jako_zakonczony():
    zadania = await _uruchom(["sh", "-c", "exit 0"])
    zdarzenia = _zdarzenia(zadania)

    koncowe = zdarzenia[-1]
    assert koncowe["kind"] == "step_finished"
    assert koncowe["text"] == "kod wyjscia 0"
    assert koncowe["run_id"] == RUN_ID
    assert koncowe["step_id"] == STEP_ID
    assert all(z["kind"] != "step_failed" for z in zdarzenia)


async def test_nieudana_komenda_najpierw_zglasza_swoj_wynik_a_zdarzenie_koncowe_idzie_ostatnie():
    zadania = await _uruchom(["sh", "-c", "echo blad; exit 2"])
    zdarzenia = _zdarzenia(zadania)

    assert [z["kind"] for z in zdarzenia] == ["output", "step_failed"]
    assert zdarzenia[0]["text"] == "blad"
    assert zdarzenia[-1]["text"] == "kod wyjscia 2"


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
            runner.send_event(client, run_id=RUN_ID, kind="output", text="x"), 10)


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


async def test_log_utraconego_zdarzenia_koncowego_podaje_jego_rodzaj(capsys):
    await _uruchom(["sh", "-c", "exit 3"], status=400)

    assert capsys.readouterr().err.splitlines() == [
        f"zdarzenie utracone: kind=step_failed run_id={RUN_ID} http=400"]


async def test_zdarzenie_koncowe_niesie_liczbe_linii_output_ktore_runner_wyslal():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b"]))

    assert [z["kind"] for z in zdarzenia] == ["output", "output", "step_finished"]
    assert zdarzenia[-1]["wyslane_output"] == 2


async def test_zdarzenie_koncowe_liczy_tez_nieudane_proby_wyslania_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b; exit 1"], status=400))

    assert zdarzenia[-1]["kind"] == "step_failed"
    assert zdarzenia[-1]["wyslane_output"] == 2


async def test_komenda_bez_wyjscia_zglasza_zero_wyslanych_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    assert zdarzenia[-1]["wyslane_output"] == 0


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
            runner.send_event(client, run_id=RUN_ID, kind="output", text="x"), 10) is False

    assert [z["text"] for z in wlasny.niepotwierdzone()] == ["x"]
    assert Bufor().niepotwierdzone() == []          # katalog domyslny (COCKPIT_STATE_DIR) pusty


async def test_domyslne_odczekanie_wraca_po_zadanym_czasie_bez_stop():
    runner = Runner("http://kokpit.test", TOKEN)

    await asyncio.wait_for(runner._czekaj(0.01), 2)      # TimeoutError = czeka bez konca


async def test_domyslne_odczekanie_pozwala_dokonczyc_ponowienie_po_awarii_kokpitu():
    odpowiedzi = [500, 200]
    transport = httpx.MockTransport(lambda request: httpx.Response(odpowiedzi.pop(0)))
    runner = Runner("http://kokpit.test", TOKEN)
    runner._odczekaj = lambda sekundy: runner._czekaj(0.01)      # prawdziwe, tylko krótsze

    async with httpx.AsyncClient(transport=transport) as client:
        przyjete = await asyncio.wait_for(
            runner.send_event(client, run_id=RUN_ID, kind="output", text="x"), 5)

    assert przyjete is True


async def test_stop_w_trakcie_odczekiwania_przed_ponownym_laczeniem_konczy_nasluchiwanie_bez_bledu(
    capsys,
):
    transport, odwiedzone = _kokpit(stream_body=POLACZONO)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)   # prawdziwe odczekanie
    nasluch = asyncio.create_task(runner.listen())
    log = ""
    try:
        for _ in range(300):
            log += capsys.readouterr().err
            if "lacze ponownie" in log:
                break
            await asyncio.sleep(0.01)
        assert "lacze ponownie" in log, "runner nie zaczal odczekiwania przed ponownym laczeniem"
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        zakonczone, _ = await asyncio.wait({nasluch}, timeout=3)
        if nasluch not in zakonczone:
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=5)

    assert nasluch in zakonczone, "nasluch nie skonczyl sie po stop() w trakcie odczekiwania"
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


async def test_polecenie_run_step_ze_strumienia_uruchamia_komende_i_zglasza_jej_wynik():
    polecenie = {"type": "run_step", "run_id": "run-1", "step_id": "s0",
                 "command": ["sh", "-c", "echo hej"]}
    cialo = f"data: {json.dumps(polecenie)}\n\n".encode()
    transport, zadania = _nagrywajacy_kokpit(stream_body=cialo)
    runner, _ = _runner(transport=transport, zatrzymaj_po=1)
    await _nasluchuj(runner)

    zdarzenia = [json.loads(z.content) for z in zadania if z.url.path == "/api/events"]
    assert [z["kind"] for z in zdarzenia] == ["output", "step_finished"]
    assert zdarzenia[0]["text"] == "hej"
    assert all(z["run_id"] == "run-1" and z["step_id"] == "s0" for z in zdarzenia)
    assert len({z["event_id"] for z in zdarzenia}) == 2


async def test_strumien_polecen_nie_ma_limitu_czasu_bo_trwa_godzinami(monkeypatch):
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
    assert kwargs_klienta[0].get("timeout", "brak") is None


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
    assert zdarzenia[-1]["kind"] == "step_finished"


async def test_czas_trwania_kroku_to_calkowite_milisekundy_zmierzone_na_zegarze_kroku():
    zdarzenia = _zdarzenia(await _uruchom(["sleep", "0.2"]))

    ms = zdarzenia[-1]["duration_ms"]
    assert type(ms) is int
    assert 150 <= ms < 5000


async def test_koniec_kroku_bez_pomiaru_kosztu_ma_zrodlo_niedostepne_i_koszt_null_a_nie_zero():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    koncowe = zdarzenia[-1]
    assert "cost_usd" in koncowe and koncowe["cost_usd"] is None
    assert koncowe["cost_source"] == "unavailable"

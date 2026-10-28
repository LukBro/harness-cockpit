"""Runner: wynik komendy trafia do kokpitu jako zdarzenie końcowe kroku.

Transport HTTP jest przechwycony (`httpx.MockTransport`), a komendy to prawdziwe
procesy — sprawdzamy produkcyjny kod `Runner.run_command`, nie jego kopię.
Asercje dotyczą rodzaju zdarzenia (`kind`); pola pomiarowe (`duration_ms`, `cost_usd`,
`cost_source`) sprawdzają osobne testy z sekcji HAR-35.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import runner.client as runner_client
from runner.client import Runner

RUN_ID = "run-1"
STEP_ID = "krok-1"


async def _uruchom(command: list[str], status: int = 200) -> list[httpx.Request]:
    """Uruchamia komendę przez Runnera i zwraca żądania wysłane do kokpitu.
    `status` to kod, którym kokpit odpowiada na każde zdarzenie."""
    wyslane: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(request)
        return httpx.Response(status)

    runner = Runner("http://kokpit.test", "tajny-token")
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
    runner = Runner("http://kokpit.test", "tajny-token")
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    async with httpx.AsyncClient(transport=transport) as client:
        return await runner.send_event(client, run_id=RUN_ID, kind="output", text="x")


@pytest.mark.parametrize("status", [401, 500])
async def test_odpowiedz_odrzucajaca_zdarzenie_jest_widoczna_w_logu_runnera(status, capsys):
    await _wyslij_zdarzenie(status)

    err = capsys.readouterr().err
    assert "output" in err
    assert RUN_ID in err
    assert str(status) in err


@pytest.mark.parametrize("status", [401, 500])
async def test_log_runnera_o_odrzuconym_zdarzeniu_nie_zawiera_tokenu(status, capsys):
    await _uruchom(["sh", "-c", "echo tajne; exit 1"], status=status)

    wyjscie = capsys.readouterr()
    assert wyjscie.err  # log istnieje, wiec brak tokenu cos znaczy
    assert "tajny-token" not in wyjscie.err
    assert "tajny-token" not in wyjscie.out


async def test_przyjete_zdarzenie_nie_zostawia_sladu_w_logu_runnera(capsys):
    assert await _wyslij_zdarzenie(200) is True
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [401, 500, 302])
async def test_wysylka_zdarzenia_odrzuconego_przez_kokpit_zwraca_porazke(status):
    assert await _wyslij_zdarzenie(status) is False


async def test_wysylka_zdarzenia_z_kodem_2xx_innym_niz_200_zwraca_sukces(capsys):
    assert await _wyslij_zdarzenie(204) is True
    assert capsys.readouterr().err == ""


async def test_log_odrzuconego_zdarzenia_koncowego_podaje_jego_rodzaj(capsys):
    await _uruchom(["sh", "-c", "exit 3"], status=500)

    err = capsys.readouterr().err
    assert "step_failed" in err
    assert "500" in err


async def test_zdarzenie_koncowe_niesie_liczbe_linii_output_ktore_runner_wyslal():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b"]))

    assert [z["kind"] for z in zdarzenia] == ["output", "output", "step_finished"]
    assert zdarzenia[-1]["wyslane_output"] == 2


async def test_zdarzenie_koncowe_liczy_tez_nieudane_proby_wyslania_output():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "echo a; echo b; exit 1"], status=500))

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


async def test_zamkniecie_strumienia_przez_kokpit_konczy_nasluchiwanie_bledem_a_nie_cichym_powrotem(
    capsys,
):
    from runner.client import StrumienZamkniety
    transport, _ = _kokpit(stream_body=POLACZONO)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(StrumienZamkniety):
        await runner.listen()

    assert "strumien otwarty" in capsys.readouterr().err


async def test_strumien_z_innym_poleceniem_niz_polaczono_nie_jest_zglaszany_jako_otwarty(capsys):
    from runner.client import StrumienZamkniety
    transport, _ = _kokpit(stream_body=b'data: {"type": "inne"}\n\n')
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(StrumienZamkniety):
        await runner.listen()

    assert "strumien otwarty" not in capsys.readouterr().err


async def test_runner_nie_zglasza_otwartego_strumienia_po_samej_odpowiedzi_200_bez_polecenia(
    capsys,
):
    from runner.client import StrumienZamkniety
    transport, _ = _kokpit(stream_body=b"")
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(StrumienZamkniety):
        await runner.listen()

    assert "strumien otwarty" not in capsys.readouterr().err


async def test_odrzucony_token_przy_zameldowaniu_konczy_nasluchiwanie_bez_ponawiania():
    from runner.client import TokenOdrzucony
    transport, odwiedzone = _kokpit(register_status=401)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(TokenOdrzucony):
        await runner.listen()

    assert odwiedzone == ["/api/runners/register"]


async def test_odrzucony_token_przy_otwieraniu_strumienia_konczy_nasluchiwanie_bez_ponawiania():
    from runner.client import TokenOdrzucony
    transport, odwiedzone = _kokpit(stream_status=401)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(TokenOdrzucony):
        await runner.listen()

    assert odwiedzone == ["/api/runners/register", "/api/runners/stream"]


async def test_awaria_kokpitu_przy_zameldowaniu_nie_jest_mylona_z_odrzuconym_tokenem():
    from runner.client import TokenOdrzucony
    transport, _ = _kokpit(register_status=500)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(httpx.HTTPStatusError) as wyjatek:
        await runner.listen()

    assert not isinstance(wyjatek.value, TokenOdrzucony)


async def test_awaria_kokpitu_przy_otwieraniu_strumienia_nie_jest_mylona_z_odrzuconym_tokenem(
    capsys,
):
    from runner.client import TokenOdrzucony
    transport, _ = _kokpit(stream_status=500, stream_body=POLACZONO)
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(httpx.HTTPStatusError) as wyjatek:
        await runner.listen()

    assert not isinstance(wyjatek.value, TokenOdrzucony)
    assert wyjatek.value.response.status_code == 500
    assert "strumien otwarty" not in capsys.readouterr().err


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


def test_zamkniecie_strumienia_przez_kokpit_konczy_proces_runnera_kodem_nie_zero(
    monkeypatch, capsys,
):
    wyjscie = _uruchom_main(monkeypatch, "StrumienZamkniety")

    assert wyjscie.code == 75
    wyjscie_procesu = capsys.readouterr()
    assert wyjscie_procesu.err.strip()      # powod jest widoczny w logu runnera
    assert TOKEN not in wyjscie_procesu.err
    assert TOKEN not in wyjscie_procesu.out


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

    assert utworzone[0]._zatrzymany
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
    from runner.client import StrumienZamkniety
    with pytest.raises(StrumienZamkniety):
        await runner.listen()


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
    await _nasluchuj(Runner("http://kokpit.test", TOKEN, name="moj-runner", transport=transport))

    zadanie = _zadanie(zadania, "/api/runners/register")
    assert zadanie.method == "POST"
    assert zadanie.url.path == "/api/runners/register"
    assert zadanie.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(zadanie.content) == {
        "name": "moj-runner", "version": "1.2.3-test",
        "backends": [{"nazwa": "claude", "auth": "subscription"}]}


async def test_runner_bez_podanej_nazwy_melduje_sie_jako_lokalny():
    transport, zadania = _nagrywajacy_kokpit()
    await _nasluchuj(Runner("http://kokpit.test", TOKEN, transport=transport))

    assert json.loads(_zadanie(zadania, "/api/runners/register").content)["name"] == "lokalny"


async def test_po_zameldowaniu_runner_zna_identyfikator_nadany_przez_kokpit():
    transport, _ = _nagrywajacy_kokpit(runner_id="r-42")
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)
    await _nasluchuj(runner)

    assert runner.runner_id == "r-42"


async def test_strumien_polecen_otwierany_jest_przez_get_z_tokenem_runnera():
    transport, zadania = _nagrywajacy_kokpit()
    await _nasluchuj(Runner("http://kokpit.test", TOKEN, transport=transport))

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
    await _nasluchuj(Runner(adres, TOKEN, transport=transport))

    assert [z.url.path for z in zadania] == [
        f"{prefiks}/api/runners/register", f"{prefiks}/api/runners/stream"]


async def test_linie_strumienia_inne_niz_dane_nie_przerywaja_czytania_polecen(capsys):
    cialo = b"event: command\n: ping\n\n" + POLACZONO
    transport, _ = _nagrywajacy_kokpit(stream_body=cialo)
    await _nasluchuj(Runner("http://kokpit.test", TOKEN, transport=transport))

    assert capsys.readouterr().err.splitlines() == ["strumien otwarty"]


async def test_polecenie_run_step_ze_strumienia_uruchamia_komende_i_zglasza_jej_wynik():
    polecenie = {"type": "run_step", "run_id": "run-1", "step_id": "s0",
                 "command": ["sh", "-c", "echo hej"]}
    cialo = f"data: {json.dumps(polecenie)}\n\n".encode()
    transport, zadania = _nagrywajacy_kokpit(stream_body=cialo)
    await _nasluchuj(Runner("http://kokpit.test", TOKEN, transport=transport))

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
    await _nasluchuj(Runner("http://kokpit.test", TOKEN, transport=transport))

    assert len(kwargs_klienta) == 1
    assert kwargs_klienta[0].get("timeout", "brak") is None


async def test_zamkniecie_strumienia_ma_komunikat_wyjasniajacy_przyczyne():
    from runner.client import StrumienZamkniety
    transport, _ = _nagrywajacy_kokpit()
    runner = Runner("http://kokpit.test", TOKEN, transport=transport)

    with pytest.raises(StrumienZamkniety) as wyjatek:
        await runner.listen()

    assert str(wyjatek.value) == "kokpit zamknal strumien polecen"


def _main_z_atrapa_runnera(monkeypatch, tmp_path, wyjatek=None, env=None, plik=None):
    """Uruchamia prawdziwe `main()` z atrapa klasy Runner. Zwraca (SystemExit, argumenty Runnera).
    Srodowisko i plik konfiguracji sa izolowane: nic z zewnatrz nie przecieka."""
    import runner.__main__ as modul
    import runner.client as klient
    from runner import secrets

    zapis: dict = {}

    class AtrapaRunnera:
        def __init__(self, base_url, token, *reszta, **nazwane):
            zapis["base_url"] = base_url
            zapis["token"] = token

        async def listen(self):
            raise getattr(klient, wyjatek or "StrumienZamkniety")()

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


def test_komunikat_o_zamknietym_strumieniu_podaje_kod_75_i_odsyla_do_ponownego_laczenia(
    monkeypatch, tmp_path, capsys,
):
    wyjscie, _ = _main_z_atrapa_runnera(
        monkeypatch, tmp_path, "StrumienZamkniety", env={"COCKPIT_RUNNER_TOKEN": TOKEN})

    assert wyjscie.code == 75
    assert capsys.readouterr().err.splitlines() == [
        "strumien zamkniety przez kokpit - koncze prace (kod 75); ponowne laczenie: BRO-75"]


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


async def test_czas_trwania_kroku_w_zdarzeniu_to_dokladna_roznica_zegara_w_milisekundach(
    monkeypatch,
):
    odczyty = iter([100.0, 102.5])
    # podmieniamy tylko `time` w module runnera: petla zdarzen asyncio ma wlasny zegar
    monkeypatch.setattr(
        runner_client, "time", SimpleNamespace(monotonic=lambda: next(odczyty)),
    )

    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    assert zdarzenia[-1]["duration_ms"] == 2500


async def test_koniec_kroku_bez_pomiaru_kosztu_ma_zrodlo_niedostepne_i_koszt_null_a_nie_zero():
    zdarzenia = _zdarzenia(await _uruchom(["sh", "-c", "exit 0"]))

    koncowe = zdarzenia[-1]
    assert "cost_usd" in koncowe and koncowe["cost_usd"] is None
    assert koncowe["cost_source"] == "unavailable"

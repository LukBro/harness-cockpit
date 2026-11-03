"""Maskowanie sekretow w logach runnera i kokpitu (HAR-86, BRO-98).

Kazdy rekord `logging` przechodzi przez filtr, ktory zamienia znane sekrety na
prefiks + gwiazdki. Zapis idzie przez wlasny handler `StringIO` (nie caplog:
handler caplog pojawia sie po instalacji maskowania).
"""
from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from procesy import uruchom_proces_sync, zakoncz_proces_sync

import cockpit.__main__ as kokpit_main
import runner.__main__ as runner_main
from runner import secrets
from runner.maskowanie import (
    _FILTR,
    _LOGGERY_MASKOWANE,
    _SEKRETY,
    FiltrSekretow,
    zainstaluj_maskowanie,
    zarejestruj_sekret,
)
from runner.secrets import get_token, mask

TOKEN = "rnr_" + "a1b2c3d4" * 4
KORZEN = Path(__file__).resolve().parent.parent
WZORZEC_ZMIENNEJ = re.compile(r"COCKPIT_.*_TOKEN")


def _wszystkie_loggery() -> list[logging.Logger]:
    return [logging.getLogger(), *(logging.getLogger(n) for n in _LOGGERY_MASKOWANE)]


def _zdejmij_filtry() -> None:
    for logger in _wszystkie_loggery():
        for obiekt in (logger, *logger.handlers):
            for f in [f for f in obiekt.filters if isinstance(f, FiltrSekretow)]:
                obiekt.removeFilter(f)
    if logging.lastResort is not None:
        for f in [f for f in logging.lastResort.filters if isinstance(f, FiltrSekretow)]:
            logging.lastResort.removeFilter(f)


@pytest.fixture(autouse=True)
def czysty_stan_sekretow(monkeypatch):
    """Stan globalny (rejestr, filtry loggerow, zmienne srodowiska) nie przecieka miedzy testami."""
    for nazwa in list(os.environ):
        if WZORZEC_ZMIENNEJ.fullmatch(nazwa):
            monkeypatch.delenv(nazwa, raising=False)
    _SEKRETY.clear()
    _zdejmij_filtry()
    yield
    _SEKRETY.clear()
    _zdejmij_filtry()


class Zapis:
    """Prywatny logger z handlerem StringIO i wlasnym filtrem (bez instalacji globalnej)."""

    def __init__(self, nazwa: str, filtr: logging.Filter | None) -> None:
        self.bufor = io.StringIO()
        self.handler = logging.StreamHandler(self.bufor)
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        if filtr is not None:
            self.handler.addFilter(filtr)
        self.logger = logging.getLogger(nazwa)
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)

    @property
    def tekst(self) -> str:
        return self.bufor.getvalue()

    def zamknij(self) -> None:
        self.logger.removeHandler(self.handler)


@pytest.fixture
def zapis():
    otwarte: list[Zapis] = []

    def utworz(filtr: logging.Filter | None = None) -> Zapis:
        z = Zapis(f"test.sekrety.{len(otwarte)}", FiltrSekretow() if filtr is None else filtr)
        otwarte.append(z)
        return z

    yield utworz
    for z in otwarte:
        z.zamknij()


# --- trzy drogi maskowania ---------------------------------------------------


def test_zarejestrowany_token_w_tresci_logu_jest_zamaskowany(zapis):
    z = zapis()
    zarejestruj_sekret(TOKEN)
    z.logger.warning("polaczono z tokenem " + TOKEN)
    assert mask(TOKEN) in z.tekst
    assert TOKEN not in z.tekst


def test_zarejestrowany_token_w_argumentach_logu_jest_zamaskowany(zapis):
    z = zapis()
    zarejestruj_sekret(TOKEN)
    z.logger.warning("token %s", TOKEN)
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_zarejestrowany_token_w_argumentach_slownikowych_jest_zamaskowany(zapis):
    z = zapis()
    zarejestruj_sekret(TOKEN)
    z.logger.warning("token %(t)s", {"t": TOKEN})
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_naglowek_bearer_z_niezarejestrowanym_tokenem_jest_zamaskowany(zapis):
    z = zapis()
    z.logger.warning("Authorization: Bearer " + TOKEN)
    assert z.tekst.strip() == "Authorization: Bearer " + mask(TOKEN)


def test_naglowek_bearer_z_tokenem_z_kropkami_i_myslnikiem_jest_zamaskowany_w_calosci(zapis):
    z = zapis()
    jwt = "eyJhbGciOi.eyJzdWIiOi-abc.sig_123"
    z.logger.warning("Authorization: Bearer " + jwt)
    assert z.tekst.strip() == "Authorization: Bearer " + mask(jwt)


def test_naglowek_bearer_z_kilkoma_spacjami_jest_zamaskowany(zapis):
    z = zapis()
    z.logger.warning("Authorization: Bearer  \t" + TOKEN)
    assert TOKEN not in z.tekst
    assert mask(TOKEN) in z.tekst


def test_krotki_token_w_naglowku_bearer_jest_zastapiony_samymi_gwiazdkami(zapis):
    z = zapis()
    z.logger.warning("Authorization: Bearer abc12345")
    assert z.tekst.strip() == "Authorization: Bearer ********"


def test_naglowek_bearer_przekazany_w_argumentach_jest_zamaskowany(zapis):
    z = zapis()
    z.logger.warning("naglowek %s", "Authorization: Bearer " + TOKEN)
    assert z.tekst.strip() == "naglowek Authorization: Bearer " + mask(TOKEN)


def test_zwykly_tekst_bez_sekretu_przechodzi_bez_zmian(zapis):
    z = zapis()
    z.logger.warning("run %s zakonczony", "r-1")
    assert z.tekst.strip() == "run r-1 zakonczony"


# --- zmienne srodowiska COCKPIT_*_TOKEN --------------------------------------


def test_token_ze_zmiennej_runnera_jest_maskowany_bez_rejestracji(zapis, monkeypatch):
    z = zapis()
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    z.logger.warning("token " + TOKEN)
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_token_z_innej_zmiennej_cockpit_token_jest_maskowany(zapis, monkeypatch):
    z = zapis()
    monkeypatch.setenv("COCKPIT_JIRA_TOKEN", TOKEN)
    z.logger.warning("token " + TOKEN)
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_zmienna_spoza_wzorca_token_nie_jest_maskowana(zapis, monkeypatch):
    z = zapis()
    adres = "http://kokpit.example.invalid:8765/dlugi/adres"
    monkeypatch.setenv("COCKPIT_URL", adres)
    z.logger.warning("laczenie z " + adres)
    assert adres in z.tekst


def test_zmienna_z_sufiksem_token_bez_prefiksu_cockpit_nie_jest_maskowana(zapis, monkeypatch):
    z = zapis()
    wartosc = "obca_wartosc_" + "z9y8x7w6" * 3
    monkeypatch.setenv("INNY_SERWIS_TOKEN", wartosc)
    z.logger.warning("wartosc " + wartosc)
    assert z.tekst.strip() == "wartosc " + wartosc


def test_zmienna_cockpit_token_z_pustym_srodkiem_nazwy_jest_maskowana(zapis, monkeypatch):
    z = zapis()
    monkeypatch.setenv("COCKPIT__TOKEN", TOKEN)
    z.logger.warning("token " + TOKEN)
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_zmienna_ustawiona_po_utworzeniu_filtra_tez_jest_maskowana(zapis, monkeypatch):
    z = zapis()                 # filtr istnieje, zmiennej jeszcze nie ma
    z.logger.warning("pierwszy wpis")
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    z.logger.warning("token " + TOKEN)
    assert "token " + mask(TOKEN) in z.tekst
    assert TOKEN not in z.tekst


# --- zachowanie filtra --------------------------------------------------------


def test_argument_niebedacy_napisem_nie_psuje_formatowania_logu(zapis):
    z = zapis()
    zarejestruj_sekret(TOKEN)
    z.logger.warning("licznik %d token %s", 7, TOKEN)
    assert z.tekst.strip() == "licznik 7 token " + mask(TOKEN)


def test_filtr_nie_odrzuca_rekordu_i_zwraca_prawde():
    zarejestruj_sekret(TOKEN)
    rekord = logging.makeLogRecord({"msg": "token " + TOKEN, "args": ()})
    assert FiltrSekretow().filter(rekord) is True


def test_filtr_nie_odrzuca_rekordu_bez_sekretu(zapis):
    z = zapis()
    z.logger.warning("zwykly wpis")
    assert z.tekst.strip() == "zwykly wpis"


def test_pusta_zmienna_tokenu_w_srodowisku_nie_zmienia_tekstu_logu(zapis, monkeypatch):
    z = zapis()
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", "")
    z.logger.warning("zwykly komunikat")
    assert z.tekst.strip() == "zwykly komunikat"


def test_tresc_logu_niebedaca_napisem_przechodzi_bez_zmian_i_bez_wyjatku(zapis):
    z = zapis()
    zarejestruj_sekret(TOKEN)
    z.logger.warning(123)
    z.logger.error(ValueError("x"))
    assert z.tekst.splitlines() == ["123", "x"]


def test_dluzszy_sekret_zawierajacy_krotszy_jest_maskowany_w_calosci(zapis):
    z = zapis()
    krotki = "rnr_" + "abcd1234" * 2
    dlugi = krotki + "XYZ123"
    zarejestruj_sekret(krotki)
    zarejestruj_sekret(dlugi)
    z.logger.warning("token " + dlugi)
    assert "XYZ123" not in z.tekst
    assert z.tekst.strip() == "token " + mask(dlugi)


def test_dluzszy_sekret_maskowany_w_calosci_takze_gdy_jest_alfabetycznie_przed_krotszym(zapis):
    z = zapis()
    krotki = "zz_sekret_krotki_1"
    dlugi = "AA_" + krotki          # zawiera krotki, ale alfabetycznie jest przed nim
    zarejestruj_sekret(krotki)
    zarejestruj_sekret(dlugi)
    z.logger.warning("token " + dlugi)
    assert z.tekst.strip() == "token " + mask(dlugi)
    assert "AA_" + mask(krotki) not in z.tekst


def test_rejestracja_pustego_sekretu_nie_zmienia_tekstu_logu(zapis):
    z = zapis()
    zarejestruj_sekret("")
    z.logger.warning("zwykly wpis o runie")
    assert z.tekst.strip() == "zwykly wpis o runie"


# --- get_token rejestruje sekret ----------------------------------------------


def test_token_odczytany_ze_zmiennej_jest_maskowany_nawet_po_jej_usunieciu(zapis, monkeypatch):
    z = zapis()
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", TOKEN)
    assert get_token("runner") == TOKEN
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN")
    z.logger.warning("token " + TOKEN)
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_token_odczytany_z_pliku_jest_zwrocony_bez_zmian_i_maskowany(zapis, monkeypatch, tmp_path):
    z = zapis()
    plik = tmp_path / "credentials.json"
    plik.write_text('{"runner": "%s"}' % TOKEN)
    monkeypatch.setattr(secrets, "CONFIG", plik)
    assert get_token("runner") == TOKEN
    z.logger.warning("token " + TOKEN)
    assert z.tekst.strip() == "token " + mask(TOKEN)


def test_brak_tokenu_nie_dodaje_niczego_do_rejestru_sekretow(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "nie-ma-pliku.json")
    assert get_token("runner") is None
    assert _SEKRETY == set()


# --- instalacja maskowania ----------------------------------------------------


def test_lista_maskowanych_loggerow_to_uvicorn_i_httpx():
    assert _LOGGERY_MASKOWANE == ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx")


def test_po_instalacji_log_z_loggera_potomnego_korzenia_jest_zamaskowany():
    bufor = io.StringIO()
    handler = logging.StreamHandler(bufor)
    handler.setFormatter(logging.Formatter("%(message)s"))
    korzen = logging.getLogger()
    korzen.addHandler(handler)           # handler istnieje PRZED instalacja
    try:
        zarejestruj_sekret(TOKEN)
        zainstaluj_maskowanie()
        logging.getLogger("cockpit.cos").warning("token " + TOKEN)
    finally:
        korzen.removeHandler(handler)
    assert bufor.getvalue().strip() == "token " + mask(TOKEN)


def test_po_instalacji_filtr_jest_na_ostatecznym_handlerze_logowania():
    zainstaluj_maskowanie()
    assert _FILTR in logging.lastResort.filters


def test_korzen_bez_handlerow_wypisuje_log_potomnego_z_zamaskowanym_tokenem(monkeypatch, capsys):
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    zarejestruj_sekret(TOKEN)
    zainstaluj_maskowanie()
    logging.getLogger("cockpit.api.bledy").error("token %s", TOKEN)
    wypisane = capsys.readouterr().err
    assert "token " + mask(TOKEN) in wypisane
    assert TOKEN not in wypisane


def test_po_instalacji_filtr_jest_na_loggerze_korzenia():
    zainstaluj_maskowanie()
    assert _FILTR in logging.getLogger().filters


@pytest.mark.parametrize("nazwa", ["uvicorn", "uvicorn.error", "uvicorn.access", "httpx"])
def test_po_instalacji_filtr_jest_na_loggerze_serwera_i_klienta_http(nazwa):
    zainstaluj_maskowanie()
    assert _FILTR in logging.getLogger(nazwa).filters


@pytest.mark.parametrize("nazwa", ["uvicorn", "uvicorn.error", "uvicorn.access", "httpx"])
def test_po_instalacji_filtr_jest_na_handlerze_loggera_serwera_i_klienta_http(nazwa):
    logger = logging.getLogger(nazwa)
    handler = logging.StreamHandler(io.StringIO())
    logger.addHandler(handler)
    try:
        zainstaluj_maskowanie()
        assert _FILTR in handler.filters
    finally:
        logger.removeHandler(handler)


def test_po_instalacji_filtr_jest_na_handlerze_korzenia():
    handler = logging.StreamHandler(io.StringIO())
    korzen = logging.getLogger()
    korzen.addHandler(handler)
    try:
        zainstaluj_maskowanie()
        assert _FILTR in handler.filters
    finally:
        korzen.removeHandler(handler)


def test_dwukrotna_instalacja_nie_dubluje_filtra_na_loggerach():
    zainstaluj_maskowanie()
    zainstaluj_maskowanie()
    for logger in _wszystkie_loggery():
        liczba = sum(isinstance(f, FiltrSekretow) for f in logger.filters)
        assert liczba == 1, logger.name or "root"


def test_dwukrotna_instalacja_nie_dubluje_filtra_na_handlerze():
    handler = logging.StreamHandler(io.StringIO())
    korzen = logging.getLogger()
    korzen.addHandler(handler)
    try:
        zainstaluj_maskowanie()
        zainstaluj_maskowanie()
        assert sum(isinstance(f, FiltrSekretow) for f in handler.filters) == 1
    finally:
        korzen.removeHandler(handler)


# --- dictConfig uvicorna (w podprocesie: dictConfig zamyka globalnie handlery) ---


SKRYPT_DICTCONFIG = """
import copy, io, logging, logging.config, sys
sys.path.insert(0, "src")
import uvicorn.config
from runner import maskowanie
token = sys.argv[1]
maskowanie.zarejestruj_sekret(token)
maskowanie.zainstaluj_maskowanie()
logging.config.dictConfig(copy.deepcopy(uvicorn.config.LOGGING_CONFIG))
bufor = io.StringIO()
handler = logging.StreamHandler(bufor)
handler.setFormatter(logging.Formatter("%(message)s"))
logger = logging.getLogger("uvicorn.access")
logger.addHandler(handler)
logger.warning("token %s", token)
sys.stdout.write("WYNIK:" + bufor.getvalue())
"""


def test_maskowanie_przetrwa_konfiguracje_logowania_uvicorna():
    srodowisko = {k: v for k, v in os.environ.items() if not WZORZEC_ZMIENNEJ.fullmatch(k)}
    proc = uruchom_proces_sync(
        [sys.executable, "-c", SKRYPT_DICTCONFIG, TOKEN],
        cwd=KORZEN,
        env=srodowisko,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wyjscie, bledy = proc.communicate(timeout=60)
    finally:
        zakoncz_proces_sync(proc, nazwa="dictConfig uvicorna")
    assert proc.returncode == 0, bledy
    assert wyjscie.strip() == "WYNIK:token " + mask(TOKEN)


# --- start procesow -------------------------------------------------------------


def test_start_runnera_instaluje_maskowanie_logow_klienta_http(monkeypatch):
    monkeypatch.setattr(runner_main, "get_token", lambda n: None)
    with pytest.raises(SystemExit):
        runner_main.main()
    assert _FILTR in logging.getLogger("httpx").filters


def test_start_kokpitu_instaluje_maskowanie_logow_serwera(monkeypatch):
    monkeypatch.setattr(kokpit_main.uvicorn, "run", lambda *a, **k: None)
    kokpit_main.main()
    assert _FILTR in logging.getLogger("uvicorn").filters


@pytest.mark.parametrize(
    "kolejnosc",
    [("runner.maskowanie", "runner.secrets"), ("runner.secrets", "runner.maskowanie")],
    ids=["maskowanie_pierwsze", "secrets_pierwsze"],
)
def test_moduly_sekretow_i_maskowania_importuja_sie_w_dowolnej_kolejnosci(kolejnosc):
    skrypt = "import sys; sys.path.insert(0, 'src'); " + "; ".join(f"import {m}" for m in kolejnosc)
    proc = uruchom_proces_sync(
        [sys.executable, "-c", skrypt],
        cwd=KORZEN,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _, bledy = proc.communicate(timeout=60)
    finally:
        zakoncz_proces_sync(proc, nazwa="import " + kolejnosc[0])
    assert proc.returncode == 0, bledy


def test_maskowanie_dziala_gdy_logging_nie_ma_handlera_ostatniej_szansy(monkeypatch):
    monkeypatch.setattr(logging, "lastResort", None)
    zainstaluj_maskowanie()
    assert _FILTR in logging.getLogger().filters

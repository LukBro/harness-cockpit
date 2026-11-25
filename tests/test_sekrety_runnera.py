"""Plik sekretow runnera: uszkodzony plik to blad dziedziny `BladSekretow`, nie traceback (HAR-210).

Plik czytamy i zapisujemy naprawde (`tmp_path`); prawa dostepu i awarie dysku symulujemy
podmiana wywolan, bo root czyta i zapisuje wszystko. Proces runnera uruchamiamy tez naprawde
(`python -m runner`). Plik sekretow ma nazwe inna niz domyslna, wiec asercje na pelny komunikat
sprawdzaja cala sciezke, a nie tylko `credentials.json`.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from procesy import uruchom_i_zbierz

from runner import __main__ as runner_main
from runner import secrets
from runner.client import TokenOdrzucony
from runner.secrets import BladSekretow

KORZEN = Path(__file__).resolve().parent.parent
SEKRET_W_PLIKU = "SEKRET-W-PLIKU-sk-ant-4417"
ZNACZNIK_TRESCI = "TRESC-PLIKU-ZNACZNIK-8821"
NAPRAW = ": napraw go albo usun"


@pytest.fixture(autouse=True)
def bez_tokenu_w_srodowisku(monkeypatch):
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)


@pytest.fixture
def plik(tmp_path, monkeypatch) -> Path:
    sciezka = tmp_path / "cockpit" / "sekrety-runnera.json"
    sciezka.parent.mkdir()
    monkeypatch.setattr(secrets, "CONFIG", sciezka)
    return sciezka


def _komunikat(plik: Path, powod: str) -> str:
    return f"plik sekretow {plik} {powod}{NAPRAW}"


class Zawieszenie(Exception):
    """Nie `OSError` (jak `TimeoutError`), zeby `except OSError` w kodzie nie polknal limitu."""


@contextlib.contextmanager
def limit_czasu(sekundy: float = 5):
    """Odczyt FIFO bez piszacego zawisa na zawsze; limit zamienia zawieszenie w oblany test."""
    def przerwij(*_):
        raise Zawieszenie("odczyt pliku sekretow zawisl")

    stary = signal.signal(signal.SIGALRM, przerwij)
    signal.setitimer(signal.ITIMER_REAL, sekundy)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, stary)


# Kazdy wpis psuje plik inaczej; tresc zawiera sekret, ktory nie moze trafic do komunikatu.
USZKODZONE_PLIKI = {
    "zly_json_ucieta_tresc": f'{{"runner": "{SEKRET_W_PLIKU} {ZNACZNIK_TRESCI}'.encode(),
    "pusty_plik": b"",
    "nie_utf8": b'{"runner": "\xff\xfe\xfd"}',
    "json_lista_zamiast_slownika": f'["{SEKRET_W_PLIKU}", "{ZNACZNIK_TRESCI}"]'.encode(),
    "json_napis_zamiast_slownika": f'"{SEKRET_W_PLIKU}"'.encode(),
    "json_null_zamiast_slownika": b"null",
    "json_zagniezdzony_ponad_limit": b"[" * 100000,
}
POWODY = {
    "zly_json_ucieta_tresc": "nie jest poprawnym JSON",
    "pusty_plik": "nie jest poprawnym JSON",
    "nie_utf8": "nie jest tekstem UTF-8",
    "json_lista_zamiast_slownika": "nie zawiera slownika JSON",
    "json_napis_zamiast_slownika": "nie zawiera slownika JSON",
    "json_null_zamiast_slownika": "nie zawiera slownika JSON",
    "json_zagniezdzony_ponad_limit": "jest zagniezdzony ponad limit",
}
PRZYPADKI = [pytest.param(USZKODZONE_PLIKI[n], POWODY[n], id=n) for n in USZKODZONE_PLIKI]

# Poprawny JSON-slownik, ale token runnera nie jest napisem. Czytanie pliku oddaje wartosc jak
# dawniej (adaptery same mowia "brak tokenu"); typ tokenu runnera sprawdza `main`.
TOKEN_NIE_NAPIS = {
    "wartosc_liczba": (b'{"runner": 5}', 5),
    "wartosc_lista": (f'{{"runner": ["{SEKRET_W_PLIKU}"]}}'.encode(), [SEKRET_W_PLIKU]),
    "wartosc_slownik": (
        f'{{"runner": {{"t": "{SEKRET_W_PLIKU}"}}}}'.encode(), {"t": SEKRET_W_PLIKU}),
    "wartosc_bool": (b'{"runner": true}', True),
}
POWOD_NIE_NAPIS = 'ma wartosc klucza "runner" inna niz napis'


def test_blad_pliku_sekretow_jest_bledem_wartosci_bo_adaptery_ticketow_tak_go_lapia():
    assert issubclass(BladSekretow, ValueError)


@pytest.mark.parametrize(
    ("tresc", "oczekiwana"), TOKEN_NIE_NAPIS.values(), ids=TOKEN_NIE_NAPIS.keys())
def test_wartosc_tokenu_inna_niz_napis_jest_oddawana_jak_dawniej_bo_adaptery_same_mowia_brak_tokenu(
    plik, tresc, oczekiwana,
):
    plik.write_bytes(tresc)

    assert secrets.get_token("runner") == oczekiwana


@pytest.mark.parametrize(("tresc", "powod"), PRZYPADKI)
def test_uszkodzony_plik_sekretow_to_blad_sekretow_z_pelnym_komunikatem_bez_tresci_pliku(
    plik, tresc, powod,
):
    plik.write_bytes(tresc)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.get_token("runner")

    assert str(wyjatek.value) == _komunikat(plik, powod)
    assert SEKRET_W_PLIKU not in str(wyjatek.value)
    assert ZNACZNIK_TRESCI not in str(wyjatek.value)


def _odmow_odczytu(monkeypatch, cel: Path, wyjatek: OSError | None = None) -> None:
    """Odczyt `cel` (kazdym sposobem: Path, open, io.open, os.open) konczy sie bledem.
    Root czyta kazdy plik, wiec odmowe symulujemy; inne pliki dzialaja normalnie."""
    import builtins
    import io

    def pilnuj(oryginal):
        def owiniete(pierwszy, *a, **kw):
            if isinstance(pierwszy, (str, bytes, os.PathLike)) and Path(os.fsdecode(pierwszy)) == cel:
                raise wyjatek or PermissionError(13, f"Permission denied: {cel}")
            return oryginal(pierwszy, *a, **kw)
        return owiniete

    for obiekt, nazwa in (
        (Path, "read_text"), (Path, "read_bytes"), (Path, "open"),
        (builtins, "open"), (io, "open"), (os, "open"),
    ):
        monkeypatch.setattr(obiekt, nazwa, pilnuj(getattr(obiekt, nazwa)))


def test_plik_sekretow_bez_prawa_odczytu_to_blad_sekretow_z_nazwa_przyczyny(plik, monkeypatch):
    plik.write_text(json.dumps({"runner": SEKRET_W_PLIKU}))
    _odmow_odczytu(monkeypatch, plik)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.get_token("runner")

    assert str(wyjatek.value) == _komunikat(plik, "nie daje sie odczytac (PermissionError)")
    assert SEKRET_W_PLIKU not in str(wyjatek.value)


def test_plik_sekretow_ktory_zniknal_tuz_przed_odczytem_to_brak_tokenu_a_nie_blad(
    plik, monkeypatch,
):
    plik.write_text(json.dumps({"runner": SEKRET_W_PLIKU}))
    _odmow_odczytu(monkeypatch, plik, FileNotFoundError(errno.ENOENT, "zniknal"))

    assert secrets.get_token("runner") is None


def _stat_z_awaria(monkeypatch, cel: Path, wyjatek: OSError) -> None:
    prawdziwe = Path.stat

    def stat_z_awaria(self, *a, **kw):
        if self == cel:
            raise wyjatek
        return prawdziwe(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", stat_z_awaria)


@pytest.mark.parametrize(
    ("wyjatek", "nazwa_przyczyny"),
    [
        (PermissionError(errno.EACCES, "Permission denied"), "PermissionError"),
        (OSError(errno.ELOOP, "Too many levels of symbolic links"), "OSError"),
        (NotADirectoryError(errno.ENOTDIR, "Not a directory"), "NotADirectoryError"),
    ],
    ids=["brak_praw_do_katalogu", "petla_dowiazan", "element_sciezki_nie_jest_katalogiem"],
)
def test_blad_dysku_przy_sprawdzaniu_pliku_sekretow_to_blad_sekretow_a_nie_brak_tokenu(
    plik, monkeypatch, wyjatek, nazwa_przyczyny,
):
    plik.write_text(json.dumps({"runner": SEKRET_W_PLIKU}))
    _stat_z_awaria(monkeypatch, plik, wyjatek)

    with pytest.raises(BladSekretow) as wyjatek_dziedziny:
        secrets.get_token("runner")

    assert str(wyjatek_dziedziny.value) == _komunikat(
        plik, f"nie daje sie sprawdzic ({nazwa_przyczyny})")


def test_brak_pliku_przy_sprawdzaniu_to_brak_tokenu_a_nie_blad(plik, monkeypatch):
    plik.write_text(json.dumps({"runner": SEKRET_W_PLIKU}))
    _stat_z_awaria(monkeypatch, plik, FileNotFoundError(errno.ENOENT, "brak"))

    assert secrets.get_token("runner") is None


def _katalog_w_miejscu_pliku(plik: Path) -> None:
    plik.mkdir()


def _fifo_w_miejscu_pliku(plik: Path) -> None:
    os.mkfifo(plik)


MIEJSCE_PLIKU = {"katalog": _katalog_w_miejscu_pliku, "kolejka_fifo": _fifo_w_miejscu_pliku}


@pytest.mark.parametrize("zrob", MIEJSCE_PLIKU.values(), ids=MIEJSCE_PLIKU.keys())
def test_katalog_albo_kolejka_fifo_w_miejscu_pliku_sekretow_to_blad_sekretow_bez_odczytu(
    plik, zrob,
):
    zrob(plik)

    with limit_czasu(), pytest.raises(BladSekretow) as wyjatek:
        secrets.get_token("runner")

    assert str(wyjatek.value) == _komunikat(plik, "nie jest zwyklym plikiem")


def test_token_ze_zmiennej_srodowiskowej_ma_pierwszenstwo_przed_uszkodzonym_plikiem(
    plik, monkeypatch,
):
    plik.write_bytes(USZKODZONE_PLIKI["zly_json_ucieta_tresc"])
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", "tok-z-env")

    assert secrets.get_token("runner") == "tok-z-env"


def test_brak_pliku_sekretow_to_brak_tokenu_a_nie_blad(plik):
    assert not plik.exists()

    assert secrets.get_token("runner") is None


def test_plik_sekretow_bez_tokenu_runnera_to_brak_tokenu_a_nie_blad(plik):
    plik.write_text(json.dumps({"inny": "tok-innego"}))

    assert secrets.get_token("runner") is None


def test_poprawny_plik_sekretow_daje_token_runnera(plik):
    plik.write_text(json.dumps({"runner": "tok-z-pliku", "inny": "x"}))

    assert secrets.get_token("runner") == "tok-z-pliku"


# --- zapis tokenu: nie nadpisujemy pliku, ktorego nie umiemy przeczytac ----------------------

@pytest.mark.parametrize(("tresc", "powod"), PRZYPADKI)
def test_zapis_tokenu_do_uszkodzonego_pliku_to_blad_sekretow_a_plik_zostaje_bez_zmian(
    plik, tresc, powod,
):
    plik.write_bytes(tresc)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.set_token("runner", "nowy-token")

    assert str(wyjatek.value) == _komunikat(plik, powod)
    assert plik.read_bytes() == tresc            # stare dane nie przepadly
    assert [p.name for p in plik.parent.iterdir()] == [plik.name]   # bez plikow tymczasowych


def test_zapis_tokenu_przy_braku_prawa_odczytu_pliku_to_blad_sekretow_a_plik_zostaje(
    plik, monkeypatch,
):
    zawartosc = json.dumps({"runner": "stary", "inny": "x"})
    plik.write_text(zawartosc)
    przed = plik.stat()
    _odmow_odczytu(monkeypatch, plik)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.set_token("runner", "nowy-token")

    assert str(wyjatek.value) == _komunikat(plik, "nie daje sie odczytac (PermissionError)")
    po = plik.stat()                  # odczyt tresci jest zablokowany: sprawdzamy ten sam plik
    assert (po.st_ino, po.st_size, po.st_mtime_ns) == (
        przed.st_ino, przed.st_size, przed.st_mtime_ns)
    assert [p.name for p in plik.parent.iterdir()] == [plik.name]


STARA_ZAWARTOSC = json.dumps({"runner": "stary", "inny": "x"})


def _plik_ze_starymi_sekretami(plik: Path) -> None:
    plik.write_text(STARA_ZAWARTOSC)


def _wymagaj_nietknietego_pliku(plik: Path) -> None:
    assert plik.read_text() == STARA_ZAWARTOSC
    assert [p.name for p in plik.parent.iterdir()] == [plik.name]   # bez `.tmp`


def test_brak_praw_do_katalogu_sekretow_to_blad_sekretow_a_plik_zostaje(plik, monkeypatch):
    _plik_ze_starymi_sekretami(plik)
    prawdziwe = Path.mkdir

    def mkdir_z_awaria(self, *a, **kw):
        if self == plik.parent:
            raise PermissionError(errno.EACCES, f"Permission denied: {ZNACZNIK_TRESCI}")
        return prawdziwe(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", mkdir_z_awaria)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.set_token("runner", "nowy-token")

    assert str(wyjatek.value) == _komunikat(plik, "nie daje sie utworzyc katalogu (PermissionError)")
    _wymagaj_nietknietego_pliku(plik)


def test_awaria_przy_tworzeniu_pliku_tymczasowego_to_blad_sekretow_a_plik_zostaje(
    plik, monkeypatch,
):
    _plik_ze_starymi_sekretami(plik)
    prawdziwe = os.open

    def open_z_awaria(sciezka, *a, **kw):
        if str(sciezka).endswith(".tmp"):
            raise OSError(errno.ENOSPC, f"No space left: {ZNACZNIK_TRESCI}")
        return prawdziwe(sciezka, *a, **kw)

    monkeypatch.setattr(os, "open", open_z_awaria)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.set_token("runner", "nowy-token")

    assert str(wyjatek.value) == _komunikat(plik, "nie daje sie zapisac (OSError)")
    assert ZNACZNIK_TRESCI not in str(wyjatek.value)
    _wymagaj_nietknietego_pliku(plik)


def test_awaria_przy_zapisie_tresci_to_blad_sekretow_plik_zostaje_a_tymczasowy_znika(
    plik, monkeypatch,
):
    _plik_ze_starymi_sekretami(plik)

    def zapis_z_awaria(dane, f, **kw):
        f.write('{"runner": "pol')          # polowa tresci juz w pliku tymczasowym
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(secrets.json, "dump", zapis_z_awaria)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.set_token("runner", "nowy-token")

    assert str(wyjatek.value) == _komunikat(plik, "nie daje sie zapisac (OSError)")
    _wymagaj_nietknietego_pliku(plik)


def test_awaria_przy_podmianie_pliku_to_blad_sekretow_plik_zostaje_a_tymczasowy_znika(
    plik, monkeypatch,
):
    _plik_ze_starymi_sekretami(plik)

    def replace_z_awaria(*a, **kw):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", replace_z_awaria)

    with pytest.raises(BladSekretow) as wyjatek:
        secrets.set_token("runner", "nowy-token")

    assert str(wyjatek.value) == _komunikat(plik, "nie daje sie zapisac (OSError)")
    _wymagaj_nietknietego_pliku(plik)


def test_wartosc_ktorej_nie_da_sie_zapisac_jako_json_zostawia_plik_i_nie_zostawia_tymczasowego(
    plik,
):
    """Wyjatek spoza `OSError` (tu `TypeError`) leci dalej bez zmiany, ale tymczasowy plik znika."""
    _plik_ze_starymi_sekretami(plik)

    with pytest.raises(TypeError):
        secrets.set_token("runner", object())

    _wymagaj_nietknietego_pliku(plik)


def test_zapis_tokenu_dopisuje_klucz_i_zachowuje_pozostale_wpisy_oraz_prawa_0600(plik):
    plik.write_text(json.dumps({"inny": "tok-innego"}))

    secrets.set_token("runner", "tok-nowy")

    assert json.loads(plik.read_text()) == {"inny": "tok-innego", "runner": "tok-nowy"}
    assert (plik.stat().st_mode & 0o777) == 0o600
    assert secrets.get_token("runner") == "tok-nowy"


def test_zapis_tokenu_bez_istniejacego_pliku_tworzy_go(plik):
    secrets.set_token("runner", "tok-nowy")

    assert json.loads(plik.read_text()) == {"runner": "tok-nowy"}


# --- main: uszkodzony plik konczy proces komunikatem, a nie tracebackiem --------------------

def _main_konczy_sie_kodem_2(monkeypatch, capsys) -> tuple[list, str, str]:
    """Uruchamia prawdziwe `main()`; zwraca (powstale Runnery, stderr, stdout)."""
    powstale: list = []
    monkeypatch.setattr(runner_main, "Runner", lambda *a, **kw: powstale.append(a))

    with limit_czasu(), pytest.raises(SystemExit) as e:
        runner_main.main()

    assert e.value.code == 2
    wyjscie = capsys.readouterr()
    assert "Traceback" not in wyjscie.err
    assert "BladSekretow" not in wyjscie.err
    return powstale, wyjscie.err, wyjscie.out


@pytest.mark.parametrize(("tresc", "powod"), PRZYPADKI)
def test_uszkodzony_plik_sekretow_konczy_main_kodem_2_z_pelnym_komunikatem_i_bez_runnera(
    plik, monkeypatch, capsys, tresc, powod,
):
    plik.write_bytes(tresc)

    powstale, err, out = _main_konczy_sie_kodem_2(monkeypatch, capsys)

    assert powstale == []
    assert err.splitlines() == [_komunikat(plik, powod)]
    for strumien in (err, out):
        assert SEKRET_W_PLIKU not in strumien
        assert ZNACZNIK_TRESCI not in strumien


@pytest.mark.parametrize(
    "tresc", [t for t, _ in TOKEN_NIE_NAPIS.values()], ids=TOKEN_NIE_NAPIS.keys())
def test_token_runnera_inny_niz_napis_konczy_main_kodem_2_z_pelnym_komunikatem_i_bez_runnera(
    plik, monkeypatch, capsys, tresc,
):
    plik.write_bytes(tresc)

    powstale, err, out = _main_konczy_sie_kodem_2(monkeypatch, capsys)

    assert powstale == []
    assert err.splitlines() == [_komunikat(plik, POWOD_NIE_NAPIS)]
    assert SEKRET_W_PLIKU not in err + out


@pytest.mark.parametrize("zrob", MIEJSCE_PLIKU.values(), ids=MIEJSCE_PLIKU.keys())
def test_katalog_albo_kolejka_fifo_w_miejscu_pliku_sekretow_konczy_main_kodem_2_bez_zawieszenia(
    plik, monkeypatch, capsys, zrob,
):
    zrob(plik)

    powstale, err, _ = _main_konczy_sie_kodem_2(monkeypatch, capsys)

    assert powstale == []
    assert err.splitlines() == [_komunikat(plik, "nie jest zwyklym plikiem")]


# Kazdy token psuje inna czesc warunku: znak spoza ASCII (drukowalny), spacja (drukowalna ASCII),
# znak sterujacy ASCII (nie-drukowalny), DEL.
ZLE_TOKENY = {
    "znak_spoza_ascii": "tok-zażółć",
    "spacja_w_srodku": "tok en",
    "znak_sterujacy": "tok\x01en",
    "znak_del": "tok\x7fen",
    "nowa_linia": "tok\nen",
}
POWOD_ZLEGO_TOKENU = (
    "albo zmienna COCKPIT_RUNNER_TOKEN ma token z niedozwolonymi znakami "
    "(dozwolone: drukowalne ASCII bez bialych znakow)")


def _wymagaj_odmowy_tokenu(monkeypatch, capsys, plik: Path, token: str) -> None:
    powstale, err, out = _main_konczy_sie_kodem_2(monkeypatch, capsys)

    assert powstale == []
    assert err.splitlines() == [_komunikat(plik, POWOD_ZLEGO_TOKENU)]
    for strumien in (err, out):
        assert token not in strumien


@pytest.mark.parametrize("token", ZLE_TOKENY.values(), ids=ZLE_TOKENY.keys())
def test_token_ze_zmiennej_srodowiskowej_z_niedozwolonymi_znakami_konczy_main_kodem_2_bez_tresci_tokenu(
    plik, monkeypatch, capsys, token,
):
    monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", token)

    _wymagaj_odmowy_tokenu(monkeypatch, capsys, plik, token)


@pytest.mark.parametrize("token", ZLE_TOKENY.values(), ids=ZLE_TOKENY.keys())
def test_token_z_pliku_z_niedozwolonymi_znakami_konczy_main_kodem_2_bez_tresci_tokenu(
    plik, monkeypatch, capsys, token,
):
    plik.write_text(json.dumps({"runner": token}))

    _wymagaj_odmowy_tokenu(monkeypatch, capsys, plik, token)


@pytest.mark.parametrize("zrodlo", ["zmienna", "plik"])
def test_token_z_samych_dozwolonych_znakow_przechodzi_do_runnera_bez_zmian(
    plik, monkeypatch, capsys, zrodlo,
):
    token = "tok-A_z.9~+/=:!"
    if zrodlo == "zmienna":
        monkeypatch.setenv("COCKPIT_RUNNER_TOKEN", token)
    else:
        plik.write_text(json.dumps({"runner": token}))
    otrzymane: list[str] = []

    class AtrapaRunnera:
        def __init__(self, base, tok, *a, **kw):
            otrzymane.append(tok)

        async def listen(self):
            raise TokenOdrzucony()

        async def stop(self):
            return None

    monkeypatch.setattr(runner_main, "Runner", AtrapaRunnera)

    with limit_czasu(), pytest.raises(SystemExit) as e:
        runner_main.main()

    assert e.value.code == 2
    assert otrzymane == [token]
    assert "token odrzucony przez kokpit" in capsys.readouterr().err


LOCALE_ASCII = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}


def _uruchom_proces(argv: list[str], config: Path, srodowisko_dodatkowe: dict | None = None):
    srodowisko = {k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_")}
    srodowisko["COCKPIT_CONFIG"] = str(config)
    srodowisko["PYTHONPATH"] = str(KORZEN / "src")
    srodowisko.update(srodowisko_dodatkowe or {})
    wynik = uruchom_i_zbierz(
        [sys.executable, *argv], cwd=KORZEN, env=srodowisko, timeout=60,
        capture_output=True, text=True,
    )
    return wynik.returncode, wynik.stdout, wynik.stderr


def _uruchom_proces_runnera(config: Path, srodowisko_dodatkowe: dict | None = None):
    return _uruchom_proces(["-m", "runner"], config, srodowisko_dodatkowe)


def _plik_utf8_z_polskimi_znakami(sciezka: Path, dane: dict) -> Path:
    sciezka.write_bytes(json.dumps(dane, ensure_ascii=False).encode("utf-8"))
    return sciezka


def test_plik_sekretow_z_polskimi_znakami_jest_czytany_jako_utf8_takze_w_srodowisku_z_locale_ascii(
    tmp_path,
):
    config = _plik_utf8_z_polskimi_znakami(
        tmp_path / "sekrety-utf8.json", {"runner": "tok-1", "inny": "zażółć"})

    kod, out, err = _uruchom_proces(
        ["-c", "from runner import secrets; print(repr(secrets.get_token('runner')))"],
        config, LOCALE_ASCII)

    assert (kod, out.strip(), err) == (0, "'tok-1'", "")


def test_prawdziwy_proces_runnera_z_polskimi_znakami_w_pliku_sekretow_nie_zglasza_zlego_utf8_w_locale_ascii(
    tmp_path,
):
    config = _plik_utf8_z_polskimi_znakami(
        tmp_path / "sekrety-utf8.json", {"inny": "zażółć"})   # bez klucza `runner`

    kod, out, err = _uruchom_proces_runnera(config, LOCALE_ASCII)

    assert kod == 2, err
    assert err.splitlines() == [
        "Brak tokenu. Wygeneruj go w Ustawieniach i zapisz:",
        "  COCKPIT_RUNNER_TOKEN=... cockpit-runner",
    ]
    assert out == ""


@pytest.mark.parametrize(
    "nazwa", ["zly_json_ucieta_tresc", "nie_utf8", "json_lista_zamiast_slownika"])
def test_prawdziwy_proces_runnera_z_uszkodzonym_plikiem_sekretow_konczy_sie_kodem_2_bez_sladu_stosu(
    tmp_path, nazwa,
):
    config = tmp_path / "sekrety-procesu.json"
    config.write_bytes(USZKODZONE_PLIKI[nazwa])

    kod, out, err = _uruchom_proces_runnera(config)

    assert kod == 2, err
    assert err.splitlines() == [_komunikat(config, POWODY[nazwa])]
    assert "Traceback" not in err
    assert SEKRET_W_PLIKU not in out + err
    assert ZNACZNIK_TRESCI not in out + err

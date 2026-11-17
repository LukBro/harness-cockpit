"""Bramki lokalne rownolegle (HAR-236): `tools/bramki_lokalne.sh`.

Skrypt uruchamia JEDNOCZESNIE testy (`COCKPIT_BRAMKI_TESTY`, domyslnie `pytest -q -n 4`, przez
`sh -c`) i mutanty zmienionych funkcji (`$PYTHON tools/mutanty_zmian.py --max-children N`), kazde do
osobnego pliku w `COCKPIT_BRAMKI_KATALOG`, i konczy sie jednym kodem z obu wynikow. Testy
uruchamiaja PRAWDZIWY skrypt (jawnie przez `/bin/bash`) z atrapami zamiast pytesta i mutmuta:
atrapa testow (`COCKPIT_BRAMKI_TESTY`, a dla wariantu domyslnego `pytest` w PATH) i atrapa `python`
(`PYTHON`) zapisuja argumenty, srodowisko i katalog roboczy i koncza sie zadanym kodem.
Procesy: `tests/procesy.py`, cwd = korzen repo, kazde czekanie ma limit czasu.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from procesy import (
    LimitCzasuTestu,
    czy_zyje,
    uruchom_proces_sync,
    zabij_proces,
    zakoncz_proces_sync,
)

KORZEN = Path(__file__).resolve().parent.parent
SKRYPT = KORZEN / "tools" / "bramki_lokalne.sh"
# Zabezpieczenia przed zawieszeniem, nie czasy, ktorych test oczekuje.
LIMIT_PROCESU = 90
LIMIT_ZDARZENIA = 90

CZEKAJ_NA = """czekaj_na() {
  i=0
  while [ ! -e "$1" ]; do
    sleep 0.1
    i=$((i + 1))
    [ "$i" -le 900 ] || return 1
  done
}
"""

# Wspolna tresc obu atrap: dowody uruchomienia w $FALSZYWY_KAT, tryby sterowane zmiennymi.
# `%NAZWA%` = testy | mutanty, `%KOD%` = zmienna z kodem wyjscia, `%INNA%` = druga atrapa.
SZABLON_ATRAPY = (
    """#!/bin/sh
"""
    + CZEKAJ_NA
    + """printf '%s\\n' "$@" > "$FALSZYWY_KAT/%NAZWA%.argv.$$"
cat "$FALSZYWY_KAT/%NAZWA%.argv.$$" >> "$FALSZYWY_KAT/%NAZWA%.argv"
pwd -P > "$FALSZYWY_KAT/%NAZWA%.pwd"
env > "$FALSZYWY_KAT/%NAZWA%.env"
echo "$$" > "$FALSZYWY_KAT/%NAZWA%.pid"
if [ -n "${FALSZYWY_CZYTAJ_STDIN:-}" ]; then cat > "$FALSZYWY_KAT/%NAZWA%.stdin"; fi
echo "%NAZWA%-stdout"
echo "%NAZWA%-stderr" >&2
case "${FALSZYWY_TRYB:-}" in
  rownolegle)
    : > "$FALSZYWY_KAT/%NAZWA%_start"
    czekaj_na "$FALSZYWY_KAT/%INNA%_start" || exit 97 ;;
  zawies)
    # dowod dostarczenia SIGTERM: znacznik `.term`, potem koniec (bez znacznika = zabity KILL-em)
    trap ': > "$FALSZYWY_KAT/%NAZWA%.term"; exit 0' TERM
    sleep 300 &
    echo "$!" > "$FALSZYWY_KAT/%NAZWA%.wnuk"
    wait ;;
  uparty)
    # ignoruje SIGTERM: zabic go moze tylko KILL do grupy po 10 s straznika
    trap ': > "$FALSZYWY_KAT/%NAZWA%.term"' TERM
    while :; do sleep 1; done ;;
esac
sleep "${%OPOZNIENIE%:-0}"
exit "${%KOD%:-0}"
"""
)


def _atrapa(nazwa: str, inna: str, kod: str, opoznienie: str) -> str:
    return (
        SZABLON_ATRAPY.replace("%NAZWA%", nazwa)
        .replace("%INNA%", inna)
        .replace("%KOD%", kod)
        .replace("%OPOZNIENIE%", opoznienie)
    )


ATRAPA_TESTOW = _atrapa("testy", "mutanty", "FALSZYWY_KOD_TESTOW", "FALSZYWE_OPOZNIENIE_TESTOW")
ATRAPA_PYTHONA = _atrapa(
    "mutanty", "testy", "FALSZYWY_KOD_MUTANTOW", "FALSZYWE_OPOZNIENIE_MUTANTOW"
)

# Python z domyslnymi dyspozycjami sygnalow (SIGINT bywa odziedziczony jako ignorowany po zadaniu w
# tle) i exec basha: skrypt dostaje sygnaly tak jak z terminala.
START_Z_DOMYSLNYMI_SYGNALAMI = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_DFL); signal.signal(signal.SIGTERM, signal.SIG_DFL); "
    "os.execv('/bin/bash', ['/bin/bash', *sys.argv[1:]])"
)


def _zapisz_program(sciezka: Path, tresc: str) -> None:
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(tresc)
    sciezka.chmod(0o755)


def _falszywe(tmp_path: Path) -> Path:
    return tmp_path / "falszywe"


def _kat(tmp_path: Path) -> Path:
    return tmp_path / "kat"


def _srodowisko(tmp_path: Path, **dodatkowe: str | None) -> dict[str, str]:
    """Srodowisko skryptu: atrapy, wlasny katalog logow, bez cudzych zmiennych COCKPIT_*.
    Wartosc `None` usuwa zmienna."""
    falszywe = _falszywe(tmp_path)
    _zapisz_program(falszywe / "testy.sh", ATRAPA_TESTOW)
    _zapisz_program(falszywe / "python.sh", ATRAPA_PYTHONA)
    env = {k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_") and k != "PYTHON"}
    env["PYTHON"] = str(falszywe / "python.sh")
    env["COCKPIT_BRAMKI_TESTY"] = shlex.quote(str(falszywe / "testy.sh"))
    env["COCKPIT_BRAMKI_KATALOG"] = str(_kat(tmp_path))
    env["FALSZYWY_KAT"] = str(falszywe)
    for nazwa, wartosc in dodatkowe.items():
        if wartosc is None:
            env.pop(nazwa, None)
        else:
            env[nazwa] = wartosc
    return env


def _start(env: dict[str, str], cwd: Path = KORZEN, **kw) -> subprocess.Popen:
    return uruchom_proces_sync(
        ["/bin/bash", str(SKRYPT)],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kw,
    )


def _uruchom(env: dict[str, str], **kw) -> tuple[int, str, str]:
    proc = _start(env, **kw)
    try:
        out, err = proc.communicate(timeout=LIMIT_PROCESU)
        return proc.returncode, out, err
    finally:
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")


def _czekaj_na(warunek: Callable[[], object], nazwa: str) -> None:
    koniec = time.monotonic() + LIMIT_ZDARZENIA
    while not warunek():
        if time.monotonic() >= koniec:
            raise LimitCzasuTestu(f"{nazwa}: przekroczono limit {LIMIT_ZDARZENIA} s")
        time.sleep(0.05)


def _wiersze(plik: Path) -> list[str]:
    return plik.read_text(errors="replace").splitlines() if plik.exists() else []


def _zmienne(plik: Path) -> dict[str, str]:
    """Zmienne z zapisu `env` atrapy (wiersze `NAZWA=wartosc`)."""
    return dict(w.split("=", 1) for w in _wiersze(plik) if "=" in w)


def _podsumowanie(testy: int, mutanty: int, bramki: int) -> str:
    return f"testy: {testy}\nmutanty: {mutanty}\nbramki: {bramki}\n"


# --- kod bramek z kodu testow i kodu mutantow -------------------------------------------------


@pytest.mark.parametrize(
    ("kod_testow", "kod_mutantow", "oczekiwany"),
    [
        pytest.param(0, 0, 0, id="oba_zielone"),
        pytest.param(1, 0, 1, id="pada_tylko_testy"),
        pytest.param(0, 1, 1, id="pada_tylko_mutanty_przezywajacy"),
        pytest.param(1, 1, 1, id="padaja_oba"),
        pytest.param(7, 0, 1, id="kazdy_niezerowy_kod_testow_to_jeden"),
        pytest.param(0, 3, 3, id="zmiany_poza_zasiegiem_mutmuta"),
        pytest.param(1, 3, 1, id="czerwone_testy_wygrywaja_z_kodem_3_mutantow"),
        pytest.param(0, 2, 2, id="blad_narzedzia_mutantow"),
        pytest.param(1, 2, 2, id="blad_narzedzia_mutantow_wygrywa_z_czerwonymi_testami"),
        pytest.param(0, 4, 2, id="nieznany_kod_mutantow_to_brak_dowodu"),
        pytest.param(0, 127, 2, id="brak_programu_mutantow_to_brak_dowodu"),
        pytest.param(0, 255, 2, id="kod_255_mutantow_to_brak_dowodu"),
    ],
)
def test_kod_bramek_wynika_z_kodow_testow_i_mutantow_a_stdout_ma_trzy_linie(
    tmp_path, kod_testow, kod_mutantow, oczekiwany
):
    env = _srodowisko(
        tmp_path, FALSZYWY_KOD_TESTOW=str(kod_testow), FALSZYWY_KOD_MUTANTOW=str(kod_mutantow)
    )

    kod, out, err = _uruchom(env)

    assert kod == oczekiwany, out + err
    assert out == _podsumowanie(kod_testow, kod_mutantow, oczekiwany)


@pytest.mark.parametrize(
    ("opozniona", "kod_testow", "kod_mutantow", "oczekiwany"),
    [
        pytest.param("TESTY", 1, 0, 1, id="wolniejsze_testy_czerwone"),
        pytest.param("MUTANTY", 0, 3, 3, id="wolniejsze_mutanty_z_kodem_3"),
        pytest.param("MUTANTY", 0, 1, 1, id="wolniejsze_mutanty_z_survivorami"),
    ],
)
def test_skrypt_czeka_na_obie_bramki_takze_gdy_jedna_konczy_sie_wyraznie_pozniej(
    tmp_path, opozniona, kod_testow, kod_mutantow, oczekiwany
):
    env = _srodowisko(
        tmp_path,
        FALSZYWY_KOD_TESTOW=str(kod_testow),
        FALSZYWY_KOD_MUTANTOW=str(kod_mutantow),
        **{f"FALSZYWE_OPOZNIENIE_{opozniona}": "1"},
    )

    kod, out, err = _uruchom(env)

    assert kod == oczekiwany, out + err
    assert out == _podsumowanie(kod_testow, kod_mutantow, oczekiwany)


# --- rownolegle uruchomienie ------------------------------------------------------------------


def test_testy_i_mutanty_biegna_rownolegle_a_nie_jedne_po_drugich(tmp_path):
    # Kazda atrapa czeka na znacznik startu drugiej (do ~90 s). Przy uruchomieniu szeregowym
    # pierwsza nie doczeka sie drugiej i konczy sie kodem 97.
    kod, out, err = _uruchom(_srodowisko(tmp_path, FALSZYWY_TRYB="rownolegle"))

    assert kod == 0, out + err
    assert out == _podsumowanie(0, 0, 0)
    assert (_falszywe(tmp_path) / "testy_start").exists()
    assert (_falszywe(tmp_path) / "mutanty_start").exists()


# --- polecenia, katalog roboczy i srodowisko ---------------------------------------------------


def test_testy_ida_domyslnie_przez_pytest_q_n_4_z_korzenia_repo(tmp_path):
    bin_dir = tmp_path / "bin"
    _zapisz_program(bin_dir / "pytest", ATRAPA_TESTOW)
    env = _srodowisko(tmp_path, COCKPIT_BRAMKI_TESTY=None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    inny_katalog = tmp_path / "gdzie-indziej"
    inny_katalog.mkdir()

    kod, out, err = _uruchom(env, cwd=inny_katalog)

    assert kod == 0, out + err
    assert _wiersze(_falszywe(tmp_path) / "testy.argv") == ["-q", "-n", "4"]
    assert (_falszywe(tmp_path) / "testy.pwd").read_text().strip() == str(KORZEN.resolve())


def test_komenda_testow_ze_zmiennej_idzie_przez_powloke_z_argumentami_i_cytowaniem(tmp_path):
    falszywe = _falszywe(tmp_path)
    komenda = f"{shlex.quote(str(falszywe / 'testy.sh'))} --opcja 'a b'"

    kod, out, err = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_TESTY=komenda))

    assert kod == 0, out + err
    assert _wiersze(falszywe / "testy.argv") == ["--opcja", "a b"]


@pytest.mark.parametrize(
    ("zmienna", "oczekiwane"),
    [
        pytest.param(None, "8", id="domyslnie_8"),
        pytest.param("3", "3", id="ze_zmiennej_3"),
        pytest.param("1", "1", id="najmniejsza_dozwolona_1"),
        pytest.param("64", "64", id="duza_liczba_64"),
    ],
)
def test_mutanty_ida_przez_interpreter_z_python_z_liczba_dzieci_osiem_albo_ze_zmiennej(
    tmp_path, zmienna, oczekiwane
):
    inny_katalog = tmp_path / "gdzie-indziej"
    inny_katalog.mkdir()

    kod, out, err = _uruchom(
        _srodowisko(tmp_path, COCKPIT_MUTMUT_DZIECI=zmienna), cwd=inny_katalog
    )

    assert kod == 0, out + err
    assert _wiersze(_falszywe(tmp_path) / "mutanty.argv") == [
        "tools/mutanty_zmian.py",
        "--max-children",
        oczekiwane,
    ]
    assert (_falszywe(tmp_path) / "mutanty.pwd").read_text().strip() == str(KORZEN.resolve())


def test_mutanty_nie_dostaja_adresu_bazy_ani_wymogu_bazy_a_reszta_srodowiska_zostaje(tmp_path):
    env = _srodowisko(
        tmp_path,
        COCKPIT_TEST_DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/baza_rodzica",
        COCKPIT_WYMAGAJ_BAZY="1",
    )

    kod, out, err = _uruchom(env)

    assert kod == 0, out + err
    zmienne = _zmienne(_falszywe(tmp_path) / "mutanty.env")
    assert "COCKPIT_TEST_DATABASE_URL" not in zmienne
    assert "COCKPIT_WYMAGAJ_BAZY" not in zmienne
    assert zmienne["FALSZYWY_KAT"] == str(_falszywe(tmp_path))


# --- osobne pliki wynikow ----------------------------------------------------------------------


def test_testy_i_mutanty_zapisuja_wyniki_do_osobnych_plikow_w_katalogu_logow(tmp_path):
    kod, out, err = _uruchom(_srodowisko(tmp_path))

    assert kod == 0, out + err
    pliki = [p for p in _kat(tmp_path).iterdir() if p.is_file()]
    tresci = {p: p.read_text() for p in pliki}
    testy = [p for p, t in tresci.items() if "testy-stdout" in t]
    mutanty = [p for p, t in tresci.items() if "mutanty-stdout" in t]
    assert len(testy) == 1 and len(mutanty) == 1 and testy != mutanty
    assert "mutanty-" not in tresci[testy[0]]
    assert "testy-" not in tresci[mutanty[0]]
    # stderr obu bramek tez trafia do plikow, a nie na stdout skryptu
    assert "testy-stderr" in "".join(tresci.values())
    assert "mutanty-stderr" in "".join(tresci.values())
    assert "-stdout" not in out and "-stderr" not in out
    assert err == f"logi: {_kat(tmp_path)}\n"  # sciezka logow na stderr, nie na stdout


def test_argumenty_skryptu_trafiaja_do_mutanty_zmian_po_max_children(tmp_path):
    env = _srodowisko(tmp_path)
    proc = uruchom_proces_sync(
        ["/bin/bash", str(SKRYPT), "--baza", "origin/develop", "--od-przebiegu", "katalog ze spacja"],
        cwd=KORZEN, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=LIMIT_PROCESU)
    finally:
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")

    assert proc.returncode == 0, out + err
    assert _wiersze(_falszywe(tmp_path) / "mutanty.argv") == [
        "tools/mutanty_zmian.py", "--max-children", "8",
        "--baza", "origin/develop", "--od-przebiegu", "katalog ze spacja",
    ]


def test_bez_zmiennej_python_mutanty_ida_przez_python_z_path(tmp_path):
    bin_dir = tmp_path / "bin"
    _zapisz_program(bin_dir / "python", ATRAPA_PYTHONA)
    env = _srodowisko(tmp_path, PYTHON=None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    kod, out, err = _uruchom(env)

    assert kod == 0, out + err
    assert _wiersze(_falszywe(tmp_path) / "mutanty.argv")[0] == "tools/mutanty_zmian.py"


def test_bramki_nie_dziedzicza_standardowego_wejscia_skryptu(tmp_path):
    env = _srodowisko(tmp_path, FALSZYWY_CZYTAJ_STDIN="1")
    proc = uruchom_proces_sync(
        ["/bin/bash", str(SKRYPT)], cwd=KORZEN, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(input="dane-z-terminala\n", timeout=LIMIT_PROCESU)
    finally:
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")

    assert proc.returncode == 0, out + err
    assert (_falszywe(tmp_path) / "testy.stdin").read_text() == ""
    assert (_falszywe(tmp_path) / "mutanty.stdin").read_text() == ""


# --- bledy konfiguracji: kod 2, jeden komunikat, nic nie startuje ------------------------------


def _nic_nie_wystartowalo(tmp_path: Path) -> bool:
    return not any((_falszywe(tmp_path) / n).exists() for n in ("testy.pid", "mutanty.pid"))


@pytest.mark.parametrize("wartosc", ["0", "abc", "-2", "1.5", "8x", "+3", " 3"])
def test_zla_liczba_dzieci_mutmuta_to_kod_2_z_jednym_komunikatem_i_nic_nie_startuje(
    tmp_path, wartosc
):
    kod, out, err = _uruchom(_srodowisko(tmp_path, COCKPIT_MUTMUT_DZIECI=wartosc))

    assert kod == 2
    assert out == ""
    assert err == (
        f"BLAD: COCKPIT_MUTMUT_DZIECI musi byc liczba calkowita >= 1 (jest: '{wartosc}')\n"
    )
    assert _nic_nie_wystartowalo(tmp_path)


def test_katalog_logow_ktorego_nie_da_sie_utworzyc_to_kod_2_z_sciezka_i_nic_nie_startuje(tmp_path):
    plik = tmp_path / "zwykly-plik"
    plik.write_text("to nie jest katalog")

    kod, out, err = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=str(plik / "kat")))

    assert kod == 2
    assert out == ""
    assert f"BLAD: nie udalo sie utworzyc katalogu logow '{plik}/kat'\n" in err
    assert _nic_nie_wystartowalo(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root zapisuje do katalogu bez prawa zapisu")
def test_istniejacy_katalog_logow_bez_prawa_zapisu_to_kod_2_i_nic_nie_startuje(tmp_path):
    kat = tmp_path / "tylko-do-odczytu"
    kat.mkdir()
    kat.chmod(0o555)
    try:
        kod, out, err = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=str(kat)))
    finally:
        kat.chmod(0o755)

    assert kod == 2
    assert out == ""
    assert err == f"BLAD: katalog logow '{kat}' nie jest katalogiem do zapisu\n"
    assert _nic_nie_wystartowalo(tmp_path)


def test_bez_katalogu_w_zmiennej_logi_trafiaja_do_nowego_katalogu_pod_tmpdir(tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    kod, out, err = _uruchom(
        _srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=None, TMPDIR=str(tmpdir))
    )

    assert kod == 0, out + err
    (kat,) = tmpdir.iterdir()
    assert kat.name.startswith("bramki_lokalne.") and len(kat.name) == len("bramki_lokalne.") + 6
    assert err == f"logi: {kat}\n"
    assert any("mutanty-stdout" in p.read_text() for p in kat.iterdir())


def test_nieistniejacy_tmpdir_bez_katalogu_w_zmiennej_to_kod_2_z_komunikatem(tmp_path):
    brak = tmp_path / "nie-ma-tmpdir"

    kod, out, err = _uruchom(
        _srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=None, TMPDIR=str(brak))
    )

    assert kod == 2
    assert out == ""
    assert f"BLAD: nie udalo sie utworzyc katalogu tymczasowego w '{brak}'\n" in err
    assert _nic_nie_wystartowalo(tmp_path)


def test_wzgledny_katalog_logow_jest_liczony_od_biezacego_katalogu_a_nie_od_korzenia_repo(tmp_path):
    wywolanie = (tmp_path / "wywolanie").resolve()
    wywolanie.mkdir()

    kod, out, err = _uruchom(
        _srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG="logi-wzgledne"), cwd=wywolanie
    )

    assert kod == 0, out + err
    assert any(p.is_file() for p in (wywolanie / "logi-wzgledne").iterdir())
    assert not (KORZEN / "logi-wzgledne").exists()
    assert err == f"logi: {wywolanie / 'logi-wzgledne'}\n"


# --- przerwanie: straznik dobija bramke ignorujaca SIGTERM ---------------------------------------


def test_bramka_ignorujaca_sigterm_jest_dobita_po_straznikowi_a_skrypt_konczy_sie_kodem_143(
    tmp_path,
):
    env = _srodowisko(tmp_path, FALSZYWY_TRYB="uparty")
    falszywe = _falszywe(tmp_path)
    proc = uruchom_proces_sync(
        [sys.executable, "-c", START_Z_DOMYSLNYMI_SYGNALAMI, str(SKRYPT)],
        cwd=KORZEN, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(_pid_z_pliku(falszywe / p) for p in ("testy.pid", "mutanty.pid")),
            "start bramek",
        )
        pidy = [_pid_z_pliku(falszywe / p) for p in ("testy.pid", "mutanty.pid")]
        zabij_proces(proc.pid, signal.SIGTERM)
        # Atrapy ignoruja SIGTERM (zapisuja znacznik i dzialaja dalej): konczy je dopiero KILL
        # do grupy po 10 s, a skrypt czeka na nie i oddaje kod sygnalu.
        proc.wait(timeout=LIMIT_PROCESU)
        assert proc.returncode == 143
        assert (falszywe / "testy.term").exists() and (falszywe / "mutanty.term").exists()
        _czekaj_na(lambda: not any(czy_zyje(p) for p in pidy), "koniec bramek po KILL")
    finally:
        for pid in pidy:
            if czy_zyje(pid):
                zabij_proces(pid, signal.SIGKILL)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")


# --- przerwanie: zero sierot -------------------------------------------------------------------

PLIKI_PID = ("testy.pid", "testy.wnuk", "mutanty.pid", "mutanty.wnuk")


def _pid_z_pliku(plik: Path) -> int | None:
    try:
        tekst = plik.read_text().strip()
    except OSError:
        return None
    return int(tekst) if tekst.isdigit() else None


@pytest.mark.parametrize(
    ("sygnal", "kod"),
    [
        pytest.param(signal.SIGTERM, 143, id="sigterm"),
        pytest.param(signal.SIGINT, 130, id="sigint"),
    ],
)
def test_przerwanie_bramek_zabija_obie_grupy_procesow_z_ich_dziecmi(tmp_path, sygnal, kod):
    env = _srodowisko(tmp_path, FALSZYWY_TRYB="zawies")
    falszywe = _falszywe(tmp_path)
    proc = uruchom_proces_sync(
        [sys.executable, "-c", START_Z_DOMYSLNYMI_SYGNALAMI, str(SKRYPT)],
        cwd=KORZEN,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(_pid_z_pliku(falszywe / p) for p in PLIKI_PID),
            "start procesow bramek i ich dzieci",
        )
        pidy = [_pid_z_pliku(falszywe / p) for p in PLIKI_PID]
        assert all(czy_zyje(p) for p in pidy)
        # Sygnal tylko do samego skryptu (nie do jego grupy): potomkow zabija jego obsluga sygnalu.
        zabij_proces(proc.pid, sygnal)
        proc.wait(timeout=LIMIT_PROCESU)
        assert proc.returncode == kod
        # SIGTERM dotarl do obu grup (a nie tylko KILL po 10 s): obie atrapy zapisaly znacznik
        assert (falszywe / "testy.term").exists()
        assert (falszywe / "mutanty.term").exists()
        _czekaj_na(lambda: not any(czy_zyje(p) for p in pidy), "koniec bramek i ich dzieci")
    finally:
        for pid in pidy:
            if czy_zyje(pid):
                zabij_proces(pid, signal.SIGKILL)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")
    assert not [p for p in pidy if czy_zyje(p)], "po przerwaniu zostaly sieroty"


def test_kazda_bramka_ma_wlasna_grupe_procesow_rozna_od_grupy_skryptu(tmp_path):
    env = _srodowisko(tmp_path, FALSZYWY_TRYB="zawies")
    falszywe = _falszywe(tmp_path)
    proc = uruchom_proces_sync(
        ["/bin/bash", str(SKRYPT)], cwd=KORZEN, env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(_pid_z_pliku(falszywe / p) for p in PLIKI_PID), "start bramek i ich dzieci"
        )
        pidy = [_pid_z_pliku(falszywe / p) for p in PLIKI_PID]
        testy, wnuk_testow, mutanty, wnuk_mutantow = pidy
        grupy = {p: os.getpgid(p) for p in pidy}
        # lider grupy to proces zadania (`sh -c` dla testow): bash na macOS robi exec atrapy,
        # dash w CI nie, wiec atrapa nie musi byc liderem - liczy sie wspolna, osobna grupa
        assert grupy[testy] == grupy[wnuk_testow]
        assert grupy[mutanty] == grupy[wnuk_mutantow]
        assert grupy[testy] != grupy[mutanty]
        assert os.getpgid(proc.pid) not in (grupy[testy], grupy[mutanty])
    finally:
        for pid in pidy:
            if czy_zyje(pid):
                zabij_proces(pid, signal.SIGKILL)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")

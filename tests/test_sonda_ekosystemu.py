"""HAR-171: sonda ekosystemu runnera - czy komendy projektu (testy, lint, typecheck) w ogole dzialaja.

Warstwy testow:
 1. `runner.sonda.sprawdz_capability`: jedna komenda, atrapa jako skrypt w katalogu na PATH;
 2. `runner.sonda.zaproponuj`: stos wykryty po znacznikach i propozycja `project.yaml`;
 3. operacja runnera `probe` przez `operacje.wykonaj` (zakres z COCKPIT_RUNNER_CONFIG).

Atrapy (`pytest`, `ruff`, `bundle`, `go`, `npm`, `npx`) to skrypty `/bin/sh`: nie wymagamy Ruby, Go
ani Node. Kazda zapisuje swoje wywolanie (katalog, argumenty, PYTHONDONTWRITEBYTECODE) do dziennika
POZA repozytorium fikstury, a `pytest` i `ruff` zachowuja sie jak prawdziwe narzedzia: bez flagi
wylaczajacej cache zakladaja katalog `.pytest_cache` / `.ruff_cache` w katalogu roboczym, a `pytest`
bez PYTHONDONTWRITEBYTECODE=1 takze `__pycache__`. Dzieki temu porownanie drzewa przed i po
sondzie oblewa, gdy sonda zapomni o ktorejs fladze.
Modul `runner.sonda` importujemy leniwie, zeby brak implementacji czerwienil kazdy test osobno.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import importlib
import json
import math
import os
import shlex
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from procesy import LimitCzasuTestu, czekaj, czekaj_na_warunek, uruchom_i_zbierz, zabij_proces

from cockpit.services.projekt import wczytaj_projekt
from runner import operacje

SEKRET = "zxqv7-SEKRET-token-482913"
LIMIT_TESTU_S = 30.0   # twardy limit kazdego czekania na sonde; sama sonda dostaje ulamki sekundy
KROTKI_LIMIT_S = 0.5
KOMUNIKAT_POZA_ZAKRESEM = "sciezka poza zakresem runnera"

TYPECHECK_NODE = "npx --no -- tsc --noEmit --extendedDiagnostics --tsBuildInfoFile /dev/null"

# ekosystem -> atrapa -> dokladne listy argumentow wywolan proby (osobne literaly, nie z kodu sondy);
# `bundle` ma dwa wywolania, wiec porownujemy listy posortowane
PROBY = {
    "python": {"pytest": [["-q", "-p", "no:cacheprovider"]], "ruff": [["check", "--no-cache", "."]]},
    "ruby": {"bundle": [["exec", "rspec"], ["exec", "rubocop"]]},
    "go": {"go": [["test", "./..."], ["vet", "./..."]]},
    "node": {"npm": [["test"]],
             "npx": [["--no", "--", "tsc", "--noEmit", "--extendedDiagnostics",
                      "--tsBuildInfoFile", "/dev/null"]]},
}

# ekosystem -> (plik znacznika, capabilities proponowane, gdy wszystkie komendy dzialaja)
OCZEKIWANE = {
    "python": ("pyproject.toml", {"test_narrow": "pytest -q", "lint": "ruff check ."}),
    "ruby": ("Gemfile", {"test_narrow": "bundle exec rspec", "lint": "bundle exec rubocop"}),
    "go": ("go.mod", {"test_narrow": "go test ./...", "lint": "go vet ./..."}),
    "node": ("package.json", {"test_narrow": "npm test", "typecheck": TYPECHECK_NODE}),
}
WSZYSTKIE_ATRAPY = ("pytest", "ruff", "bundle", "go", "npm", "npx")


def _sonda():
    return importlib.import_module("runner.sonda")


# =================================================================================
# Pomocnicy: atrapy komend, fikstury repozytoriow, zdjecie drzewa
# =================================================================================

@pytest.fixture
def swiat(tmp_path, monkeypatch):
    """Zakres runnera (`korzen`), obcy katalog poza nim i katalog atrap jedyny na PATH.
    Sciezki rozwiazane (macOS: /tmp to dowiazanie)."""
    baza = tmp_path.resolve()
    korzen, obcy, dom, bin_, logi = (baza / n for n in ("projekt", "obcy", "dom", "bin", "logi"))
    for k in (korzen, obcy, dom, bin_, logi):
        k.mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(korzen)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    monkeypatch.setenv("PATH", str(bin_))   # prawdziwy ruff/pytest z maszyny nie moze odpowiedziec
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)

    def atrapa(nazwa, **opcje):
        if nazwa == "npx":   # prawdziwy tsc --noEmit przy sukcesie milczy (kod 0, puste wyjscie)
            opcje.setdefault("milczy_bez", "--extendedDiagnostics")
        _zainstaluj_atrape(bin_, logi, nazwa, **opcje)

    def wszystkie(**opcje):
        for nazwa in WSZYSTKIE_ATRAPY:
            atrapa(nazwa, **opcje)

    return SimpleNamespace(baza=baza, korzen=korzen, obcy=obcy, bin=bin_, logi=logi,
                           atrapa=atrapa, wszystkie=wszystkie)


def _zainstaluj_atrape(bin_: Path, logi: Path, nazwa: str, *, kod: int = 0,
                       wyjscie: str = "atrapa dziala\n", spij: bool = False,
                       cache: tuple[str, str] | None = None, bytecode: bool = False,
                       na_stderr: bool = False, milczy_bez: str | None = None) -> None:
    """Skrypt `bin_/nazwa`: dziennik wywolania, opcjonalny zapis cache jak prawdziwe narzedzie,
    potem `wyjscie` na stdout (albo stderr) i kod wyjscia `kod`; `spij` zawiesza go (z potomkiem)
    do zabicia; `milczy_bez`: bez tego argumentu nic nie wypisuje (jak tsc bez diagnostyki)."""
    (bin_ / f"{nazwa}.out").write_text(wyjscie, encoding="utf-8")
    sk = shlex.quote
    linie = [
        "#!/bin/sh",
        "LOG=" + sk(str(logi / f"{nazwa}.log")),
        # jeden zapis na wywolanie: sonda uruchamia atrapy rownolegle (`bundle`, `go` dwa razy)
        "BLOK=$({ printf 'PWD=%s\\n' \"$(pwd -P)\"; printf 'BYTECODE=%s\\n' \"$PYTHONDONTWRITEBYTECODE\";"
        " printf 'FROZEN=%s\\n' \"$BUNDLE_FROZEN\"; printf 'STDIN=%s\\n' \"$(/bin/readlink /proc/$$/fd/0)\";"
        " for a in \"$@\"; do printf 'ARG=%s\\n' \"$a\"; done; })",
        "printf '%s\\n@@KONIEC@@\\n' \"$BLOK\" >> \"$LOG\"",
    ]
    if cache is not None:
        flaga, katalog = cache
        linie.append(f"case \" $* \" in *{sk(flaga)}*) ;; *) /bin/mkdir -p {sk(katalog)};; esac")
    if bytecode:
        linie.append("[ \"$PYTHONDONTWRITEBYTECODE\" = 1 ] || /bin/mkdir -p __pycache__")
    if spij:
        linie += [
            "echo $$ > " + sk(str(logi / f"{nazwa}.pid")),
            "/bin/sleep 30 &",
            "echo $! > " + sk(str(logi / f"{nazwa}.potomek")),
            "wait",
        ]
    wypisz = "/bin/cat " + sk(str(bin_ / f"{nazwa}.out")) + (" >&2" if na_stderr else "")
    if milczy_bez is not None:
        wypisz = f"case \" $* \" in *{sk(milczy_bez)}*) {wypisz};; esac"
    linie += [wypisz, f"exit {kod}"]
    skrypt = bin_ / nazwa
    skrypt.write_text("\n".join(linie) + "\n", encoding="utf-8")
    skrypt.chmod(0o755)


def _zainstaluj_narzedzia_zapisujace_cache(swiat, **opcje) -> None:
    """Wszystkie atrapy; pytest i ruff dodatkowo zapisuja cache, gdy sonda nie wylaczy go flaga."""
    for nazwa in WSZYSTKIE_ATRAPY:
        dodatki = dict(opcje)
        if nazwa == "pytest":
            dodatki.update(cache=("no:cacheprovider", ".pytest_cache"), bytecode=True)
        if nazwa == "ruff":
            dodatki.update(cache=("--no-cache", ".ruff_cache"))
        swiat.atrapa(nazwa, **dodatki)


def _wywolania(swiat, nazwa: str) -> list[dict]:
    """Wywolania atrapy z dziennika: [{"pwd", "bytecode", "args"}]; brak dziennika = brak wywolan."""
    plik = swiat.logi / f"{nazwa}.log"
    if not plik.exists():
        return []
    wynik = []
    for blok in plik.read_text(encoding="utf-8").split("@@KONIEC@@\n")[:-1]:
        wpis = {"args": []}
        for linia in blok.splitlines():
            klucz, _, wartosc = linia.partition("=")
            if klucz == "ARG":
                wpis["args"].append(wartosc)
            else:
                wpis[klucz.lower()] = wartosc
        wynik.append(wpis)
    return wynik


def _repo(swiat, nazwa: str, ekosystem: str) -> Path:
    """Repozytorium w zakresie runnera: znacznik ekosystemu i zwykly plik z trescia."""
    katalog = swiat.korzen / nazwa
    katalog.mkdir()
    (katalog / OCZEKIWANE[ekosystem][0]).write_text("{}\n", encoding="utf-8")
    (katalog / "README.md").write_text("projekt\n", encoding="utf-8")
    return katalog


def _drzewo(korzen: Path) -> dict:
    """Wszystko pod korzeniem (ukryte tez): sciezka -> (katalog?, rozmiar pliku, mtime_ns)."""
    zdjecie = {}
    for rodzic, katalogi, pliki in os.walk(korzen):
        for nazwa in [*katalogi, *pliki]:
            sciezka = Path(rodzic) / nazwa
            st = sciezka.lstat()
            jest_katalogiem = stat.S_ISDIR(st.st_mode)
            zdjecie[str(sciezka.relative_to(korzen))] = (
                jest_katalogiem, None if jest_katalogiem else st.st_size, st.st_mtime_ns)
    st = korzen.lstat()
    zdjecie["."] = (True, None, st.st_mtime_ns)
    return zdjecie


def _zyje(pid: int) -> bool:
    """Zywy, a nie zombie (zombie odpowiada na kill 0)."""
    try:
        stan = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(")")[2].split()[0]
    except FileNotFoundError:
        return False
    return stan != "Z"


async def _w_limicie(awaitable, nazwa: str):
    return await czekaj(awaitable, LIMIT_TESTU_S, nazwa)


async def _sprawdz(komenda: str, worktree: Path, limit_s: float = 10.0):
    return await _w_limicie(_sonda().sprawdz_capability(komenda, worktree, limit_s),
                            f"sprawdz_capability({komenda!r})")


async def _zaproponuj(worktree: Path, limit_s: float = 10.0) -> dict:
    return await _w_limicie(_sonda().zaproponuj(worktree, limit_s), "zaproponuj")


def _wynik(obecna: bool, powod: str):
    return _sonda().WynikCapability(obecna=obecna, powod=powod)


# =================================================================================
# 1. sprawdz_capability
# =================================================================================

async def test_komenda_z_kodem_zero_i_niepustym_wyjsciem_jest_obecna_bez_powodu(swiat):
    swiat.atrapa("narzedzie", kod=0, wyjscie="wersja 1.2.3\n")

    assert await _sprawdz("narzedzie --version", swiat.korzen) == _wynik(True, "")


async def test_komenda_ktorej_nie_ma_w_systemie_jest_nieobecna_z_powodem_brak_komendy(swiat):
    assert await _sprawdz("nie-ma-takiej-komendy-xyz", swiat.korzen) == _wynik(
        False, "brak komendy")


@pytest.mark.parametrize("komenda", ["", "   ", "\t\n"], ids=["pusty", "spacje", "bialy_znak"])
async def test_pusty_napis_zamiast_komendy_to_brak_komendy(swiat, komenda):
    assert await _sprawdz(komenda, swiat.korzen) == _wynik(False, "brak komendy")


async def test_komenda_bez_prawa_wykonania_nie_startuje_wiec_to_brak_komendy(swiat):
    plik = swiat.bin / "niewykonywalna"
    plik.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
    plik.chmod(0o644)

    assert await _sprawdz(str(plik), swiat.korzen) == _wynik(False, "brak komendy")


async def test_bezwzgledna_sciezka_do_nieistniejacego_pliku_to_brak_komendy(swiat):
    assert await _sprawdz(str(swiat.bin / "nie-ma"), swiat.korzen) == _wynik(
        False, "brak komendy")


@pytest.mark.parametrize("kod", [1, 7, 127])
async def test_komenda_z_kodem_innym_niz_zero_jest_nieobecna_z_powodem_zawierajacym_kod(swiat, kod):
    swiat.atrapa("narzedzie", kod=kod, wyjscie="cos wypisalo\n")

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(False, f"kod wyjścia {kod}")


async def test_kod_niezerowy_wygrywa_z_pustym_wyjsciem(swiat):
    swiat.atrapa("narzedzie", kod=2, wyjscie="")

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(False, "kod wyjścia 2")


@pytest.mark.parametrize("wyjscie", ["", "   ", "\n", " \t\n\n  "],
                         ids=["nic", "spacje", "nowa_linia", "same_biale_znaki"])
async def test_kod_zero_bez_zadnego_wyjscia_to_nieobecna_z_powodem_puste_wyjscie(swiat, wyjscie):
    swiat.atrapa("narzedzie", kod=0, wyjscie=wyjscie)

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(False, "puste wyjście")


async def test_jeden_znak_wyjscia_wsrod_bialych_znakow_wystarcza_do_obecnosci(swiat):
    swiat.atrapa("narzedzie", kod=0, wyjscie="\n  .  \n")

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(True, "")


async def test_powod_nie_niesie_tresci_wyjscia_ani_argumentow_komendy(swiat):
    swiat.atrapa("narzedzie", kod=4, wyjscie=f"token {SEKRET}\n")

    wynik = await _sprawdz(f"narzedzie --token {SEKRET}", swiat.korzen)
    brak = await _sprawdz(f"nie-ma-takiej-komendy-xyz --token {SEKRET}", swiat.korzen)

    assert wynik == _wynik(False, "kod wyjścia 4")
    assert brak == _wynik(False, "brak komendy")


async def test_komenda_po_limicie_czasu_jest_nieobecna_a_jej_procesy_sa_zabite(swiat):
    swiat.atrapa("wolne", spij=True)

    wynik = await _sprawdz("wolne", swiat.korzen, limit_s=KROTKI_LIMIT_S)

    assert wynik == _wynik(False, "limit czasu")
    pid = int((swiat.logi / "wolne.pid").read_text())
    potomek = int((swiat.logi / "wolne.potomek").read_text())
    for kto, numer in (("lider", pid), ("potomek", potomek)):
        try:
            await czekaj_na_warunek(lambda n=numer: not _zyje(n), 5.0, f"koniec procesu {kto}")
        except LimitCzasuTestu:
            zabij_proces(numer)   # nie zostawiamy sieroty po nieudanym tescie
            raise


async def test_komenda_piszaca_tylko_na_stderr_z_kodem_zero_jest_obecna(swiat):
    swiat.atrapa("narzedzie", kod=0, wyjscie="statystyki\n", na_stderr=True)

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(True, "")


async def test_komenda_z_samymi_bialymi_znakami_na_stderr_ma_puste_wyjscie(swiat):
    swiat.atrapa("narzedzie", kod=0, wyjscie="\n", na_stderr=True)   # stderr tylko z bialych znakow

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(False, "puste wyjście")


@contextlib.contextmanager
def _stdin_procesu_jako_potok():
    """fd 0 tego procesu to potok z danymi (jak u runnera uruchomionego z terminala/rury)."""
    zapas = os.dup(0)
    pisz = None
    try:
        czytaj, pisz = os.pipe()
        os.dup2(czytaj, 0)
        os.close(czytaj)
        os.write(pisz, b"dane rodzica\n")
        yield
    finally:
        os.dup2(zapas, 0)
        os.close(zapas)
        if pisz is not None:
            os.close(pisz)


async def test_komenda_nie_dziedziczy_stdin_rodzica_tylko_dev_null(swiat):
    swiat.atrapa("narzedzie")

    with _stdin_procesu_jako_potok():
        assert os.readlink("/proc/self/fd/0").startswith("pipe:")   # kontrola zalozenia testu
        await _sprawdz("narzedzie", swiat.korzen)

    [wywolanie] = _wywolania(swiat, "narzedzie")
    assert wywolanie["stdin"] == "/dev/null"


@pytest.mark.parametrize(("frozen", "bytecode"), [("false", "0"), ("", ""), (None, None)],
                         ids=["rodzic_false_i_0", "rodzic_puste", "rodzic_bez_zmiennych"])
async def test_komenda_dostaje_bundle_frozen_true_i_wylaczony_bytecode_niezaleznie_od_rodzica(
    swiat, monkeypatch, frozen, bytecode,
):
    for nazwa, wartosc in (("BUNDLE_FROZEN", frozen), ("PYTHONDONTWRITEBYTECODE", bytecode)):
        if wartosc is None:
            monkeypatch.delenv(nazwa, raising=False)
        else:
            monkeypatch.setenv(nazwa, wartosc)
    swiat.atrapa("narzedzie")

    await _sprawdz("narzedzie", swiat.korzen)

    [wywolanie] = _wywolania(swiat, "narzedzie")
    assert (wywolanie["frozen"], wywolanie["bytecode"]) == ("true", "1")


async def test_komenda_z_niedomknietym_cudzyslowem_to_brak_komendy(swiat):
    swiat.atrapa("narzedzie")

    assert await _sprawdz('narzedzie "abc', swiat.korzen) == _wynik(False, "brak komendy")
    assert _wywolania(swiat, "narzedzie") == []


async def test_komenda_ze_znakiem_nul_w_argumencie_to_brak_komendy(swiat):
    swiat.atrapa("narzedzie")

    assert await _sprawdz("narzedzie a\x00b", swiat.korzen) == _wynik(False, "brak komendy")


async def test_komenda_ze_sciezka_przez_plik_zamiast_katalogu_to_brak_komendy(swiat):
    plik = swiat.korzen / "plik.txt"
    plik.write_text("x", encoding="utf-8")

    assert await _sprawdz(f"{plik}/narzedzie", swiat.korzen) == _wynik(False, "brak komendy")


async def test_plik_wykonywalny_bez_shebanga_nie_jest_binarka_wiec_to_brak_komendy(swiat):
    plik = swiat.bin / "bez-shebanga"
    plik.write_text("echo x\n", encoding="utf-8")
    plik.chmod(0o755)

    assert await _sprawdz(str(plik), swiat.korzen) == _wynik(False, "brak komendy")


@pytest.mark.parametrize(("numer", "powod"), [
    (errno.ENOEXEC, "brak komendy"),
    (errno.EMFILE, "błąd uruchomienia"),
    (errno.ENOMEM, "błąd uruchomienia"),
], ids=["enoexec", "emfile", "enomem"])
async def test_blad_systemu_przy_starcie_komendy_ma_osobny_powod_niz_brak_komendy(
    swiat, monkeypatch, numer, powod,
):
    async def start_z_bledem(*args, **kwargs):
        raise OSError(numer, f"blad {SEKRET}")

    monkeypatch.setattr(_sonda(), "asyncio", SimpleNamespace(
        subprocess=asyncio.subprocess, create_subprocess_exec=start_z_bledem))

    assert await _sprawdz("narzedzie", swiat.korzen) == _wynik(False, powod)


async def test_komenda_dostaje_argumenty_podzielone_jak_w_powloce_ale_bez_powloki(swiat):
    swiat.atrapa("narzedzie")
    znacznik = swiat.baza / "powloka-zadzialala"

    wynik = await _sprawdz(
        f"narzedzie 'dwa slowa' \"a b\" ; touch {znacznik} $HOME `id`", swiat.korzen)

    assert wynik == _wynik(True, "")
    assert not znacznik.exists()
    [wywolanie] = _wywolania(swiat, "narzedzie")
    assert wywolanie["args"] == [
        "dwa slowa", "a b", ";", "touch", str(znacznik), "$HOME", "`id`"]


async def test_komenda_dziala_w_katalogu_worktree(swiat):
    repo = swiat.korzen / "repo"
    repo.mkdir()
    swiat.atrapa("narzedzie")

    await _sprawdz("narzedzie", repo)

    [wywolanie] = _wywolania(swiat, "narzedzie")
    assert Path(wywolanie["pwd"]) == repo


async def test_komenda_dostaje_wylaczony_zapis_bytecode_w_srodowisku(swiat):
    swiat.atrapa("narzedzie")

    await _sprawdz("narzedzie", swiat.korzen)

    [wywolanie] = _wywolania(swiat, "narzedzie")
    assert wywolanie["bytecode"] == "1"


# ---- kontrola samych atrap: bez niej test "nie zapisuje w repo" moglby byc zielony z zalozenia ----

def _uruchom_atrape(swiat, nazwa, args, katalog, *, bytecode):
    srodowisko = {**os.environ}
    srodowisko.pop("PYTHONDONTWRITEBYTECODE", None)
    if bytecode:
        srodowisko["PYTHONDONTWRITEBYTECODE"] = "1"
    return uruchom_i_zbierz([str(swiat.bin / nazwa), *args], cwd=katalog, env=srodowisko,
                            capture_output=True, timeout=30)


def test_atrapa_pytest_bez_flag_ochronnych_zostawia_cache_i_bytecode_w_katalogu(swiat):
    _zainstaluj_narzedzia_zapisujace_cache(swiat)
    katalog = swiat.baza / "kontrola-bez"
    katalog.mkdir()

    _uruchom_atrape(swiat, "pytest", ["-q"], katalog, bytecode=False)

    assert sorted(p.name for p in katalog.iterdir()) == [".pytest_cache", "__pycache__"]


def test_atrapa_pytest_z_flagami_ochronnymi_nie_zostawia_nic_w_katalogu(swiat):
    _zainstaluj_narzedzia_zapisujace_cache(swiat)
    katalog = swiat.baza / "kontrola-z"
    katalog.mkdir()

    _uruchom_atrape(swiat, "pytest", ["-q", "-p", "no:cacheprovider"], katalog, bytecode=True)

    assert list(katalog.iterdir()) == []


def test_atrapa_ruff_bez_flagi_zostawia_cache_a_z_flaga_nie(swiat):
    _zainstaluj_narzedzia_zapisujace_cache(swiat)
    bez, z = swiat.baza / "ruff-bez", swiat.baza / "ruff-z"
    bez.mkdir()
    z.mkdir()

    _uruchom_atrape(swiat, "ruff", ["check", "."], bez, bytecode=True)
    _uruchom_atrape(swiat, "ruff", ["check", "--no-cache", "."], z, bytecode=True)

    assert [p.name for p in bez.iterdir()] == [".ruff_cache"]
    assert list(z.iterdir()) == []


# =================================================================================
# 2. zaproponuj
# =================================================================================

@pytest.mark.parametrize("ekosystem", list(OCZEKIWANE))
async def test_repozytorium_z_jednym_znacznikiem_ma_stos_swojego_ekosystemu(swiat, ekosystem):
    swiat.wszystkie()
    repo = _repo(swiat, "app", ekosystem)

    wynik = await _zaproponuj(repo)

    assert wynik["stack"] == [ekosystem]


@pytest.mark.parametrize("ekosystem", list(OCZEKIWANE))
async def test_komendy_dzialajace_trafiaja_do_propozycji_w_postaci_docelowej(swiat, ekosystem):
    swiat.wszystkie()
    repo = _repo(swiat, "app", ekosystem)

    wynik = await _zaproponuj(repo)

    assert wynik["capabilities"] == OCZEKIWANE[ekosystem][1]


@pytest.mark.parametrize("ekosystem", list(OCZEKIWANE))
async def test_status_ma_wpis_dla_kazdej_proponowanej_capability_z_komenda_docelowa(
    swiat, ekosystem,
):
    swiat.wszystkie()
    repo = _repo(swiat, "app", ekosystem)

    wynik = await _zaproponuj(repo)

    assert wynik["status"] == {
        nazwa: {"komenda": komenda, "obecna": True, "powod": ""}
        for nazwa, komenda in OCZEKIWANE[ekosystem][1].items()}


async def test_wynik_ma_dokladnie_cztery_pola(swiat):
    swiat.wszystkie()

    wynik = await _zaproponuj(_repo(swiat, "app", "python"))

    assert sorted(wynik) == ["capabilities", "stack", "status", "yaml"]
    assert isinstance(wynik["yaml"], str)


@pytest.mark.parametrize("ekosystem", ["python", "ruby", "go"])
async def test_ekosystem_bez_typecheck_nie_dostaje_propozycji_typecheck(swiat, ekosystem):
    swiat.wszystkie()
    repo = _repo(swiat, "app", ekosystem)

    wynik = await _zaproponuj(repo)

    assert sorted(wynik["capabilities"]) == ["lint", "test_narrow"]
    assert sorted(wynik["status"]) == ["lint", "test_narrow"]


async def test_node_nie_dostaje_propozycji_lint_bo_typecheck_to_tsc(swiat):
    swiat.wszystkie()

    wynik = await _zaproponuj(_repo(swiat, "app", "node"))

    assert sorted(wynik["capabilities"]) == ["test_narrow", "typecheck"]


async def test_repozytorium_bez_znacznikow_ma_pusty_stos_i_zadnej_propozycji_i_nic_nie_uruchamia(
    swiat,
):
    swiat.wszystkie()
    repo = swiat.korzen / "pusty"
    repo.mkdir()
    (repo / "README.md").write_text("x", encoding="utf-8")

    wynik = await _zaproponuj(repo)

    assert (wynik["stack"], wynik["capabilities"], wynik["status"]) == ([], {}, {})
    assert list(swiat.logi.iterdir()) == []


async def test_znacznik_bez_reguly_w_tabeli_daje_stos_ale_nie_uruchamia_komend(swiat):
    swiat.wszystkie()
    repo = swiat.korzen / "rdza"
    repo.mkdir()
    (repo / "Cargo.toml").write_text("", encoding="utf-8")

    wynik = await _zaproponuj(repo)

    assert wynik["stack"] == ["rust"]
    assert list(swiat.logi.iterdir()) == []


async def test_repozytorium_z_dwoma_ekosystemami_ma_oba_w_stosie_posortowane(swiat):
    swiat.wszystkie()
    repo = _repo(swiat, "app", "python")
    (repo / "package.json").write_text("{}", encoding="utf-8")

    wynik = await _zaproponuj(repo)

    assert wynik["stack"] == ["node", "python"]


async def test_brakujace_narzedzie_jest_w_statusie_z_powodem_a_poza_capabilities(swiat):
    swiat.wszystkie()
    (swiat.bin / "ruff").unlink()   # pytest jest, ruffa nie ma
    repo = _repo(swiat, "app", "python")

    wynik = await _zaproponuj(repo)

    assert wynik["capabilities"] == {"test_narrow": "pytest -q"}
    assert wynik["status"] == {
        "test_narrow": {"komenda": "pytest -q", "obecna": True, "powod": ""},
        "lint": {"komenda": "ruff check .", "obecna": False, "powod": "brak komendy"},
    }


async def test_komenda_z_kodem_bledu_jest_w_statusie_z_kodem_a_poza_capabilities(swiat):
    swiat.wszystkie()
    swiat.atrapa("pytest", kod=1, wyjscie=f"FAILED {SEKRET}\n")
    repo = _repo(swiat, "app", "python")

    wynik = await _zaproponuj(repo)

    assert wynik["capabilities"] == {"lint": "ruff check ."}
    assert wynik["status"]["test_narrow"] == {
        "komenda": "pytest -q", "obecna": False, "powod": "kod wyjścia 1"}
    assert wynik["status"]["lint"] == {"komenda": "ruff check .", "obecna": True, "powod": ""}
    assert SEKRET not in json.dumps(wynik)


async def test_komenda_z_pustym_wyjsciem_jest_w_statusie_jako_puste_wyjscie(swiat):
    swiat.wszystkie()
    swiat.atrapa("npx", kod=0, wyjscie="")
    repo = _repo(swiat, "app", "node")

    wynik = await _zaproponuj(repo)

    assert wynik["capabilities"] == {"test_narrow": "npm test"}
    assert wynik["status"]["typecheck"] == {
        "komenda": TYPECHECK_NODE, "obecna": False, "powod": "puste wyjście"}


async def test_komenda_ponad_limit_czasu_nie_blokuje_pozostalych_i_ma_powod_limit_czasu(swiat):
    swiat.wszystkie()
    swiat.atrapa("ruff", spij=True)
    repo = _repo(swiat, "app", "python")

    wynik = await _zaproponuj(repo, limit_s=KROTKI_LIMIT_S)

    assert wynik["capabilities"] == {"test_narrow": "pytest -q"}
    assert wynik["status"]["lint"] == {
        "komenda": "ruff check .", "obecna": False, "powod": "limit czasu"}
    assert wynik["status"]["test_narrow"]["obecna"] is True


@pytest.mark.parametrize("ekosystem", list(PROBY))
async def test_proba_uruchamia_dokladnie_te_argumenty_w_repozytorium_ze_srodowiskiem_bez_zapisu(
    swiat, ekosystem,
):
    swiat.wszystkie()
    repo = _repo(swiat, "app", ekosystem)

    await _zaproponuj(repo)

    for nazwa, oczekiwane in PROBY[ekosystem].items():
        wywolania = _wywolania(swiat, nazwa)
        assert wywolania, f"sonda nie uruchomila {nazwa}"
        assert sorted(w["args"] for w in wywolania) == sorted(oczekiwane)
        for w in wywolania:
            assert Path(w["pwd"]) == repo
            assert w["bytecode"] == "1"
            assert w["frozen"] == "true"
            assert w["stdin"] == "/dev/null"


@pytest.mark.parametrize(("ekosystem", "atrapa", "tylko_dla", "lint"), [
    ("go", "go", "test", "go vet ./..."),
    ("ruby", "bundle", "rspec", "bundle exec rubocop"),
], ids=["go", "ruby"])
async def test_status_przypisuje_wynik_do_wlasciwej_capability_gdy_atrapa_odpowiada_roznie_na_podkomendy(
    swiat, ekosystem, atrapa, tylko_dla, lint,
):
    swiat.wszystkie()
    swiat.atrapa(atrapa, milczy_bez=tylko_dla)   # wypisuje tylko dla testow, lint milczy
    repo = _repo(swiat, "app", ekosystem)

    wynik = await _zaproponuj(repo)

    assert wynik["status"]["lint"] == {"komenda": lint, "obecna": False, "powod": "puste wyjście"}
    assert wynik["status"]["test_narrow"]["obecna"] is True
    assert wynik["capabilities"] == {"test_narrow": OCZEKIWANE[ekosystem][1]["test_narrow"]}


async def test_wynik_dotyczy_tej_samej_komendy_co_proba_bo_tsc_bez_diagnostyki_milczy(swiat):
    swiat.wszystkie()   # atrapa npx milczy bez --extendedDiagnostics, jak prawdziwy tsc --noEmit
    repo = _repo(swiat, "app", "node")

    wynik = await _zaproponuj(repo)

    assert wynik["capabilities"]["typecheck"] == TYPECHECK_NODE
    assert wynik["status"]["typecheck"]["obecna"] is True


async def test_atrapa_tsc_bez_diagnostyki_milczy_wiec_sonda_widzi_brak_dowodu(swiat):
    swiat.wszystkie()

    wynik = await _sprawdz("npx --no -- tsc --noEmit", swiat.korzen)

    assert wynik == _wynik(False, "puste wyjście")


# ---- ta sama nazwa capability w dwoch ekosystemach (test_narrow) ------------------------------

def _repo_python_i_node(swiat) -> Path:
    repo = _repo(swiat, "app", "python")
    (repo / "package.json").write_text("{}", encoding="utf-8")
    return repo


async def test_gdy_dzialaja_oba_test_narrow_pochodzi_od_pierwszego_ekosystemu_w_stosie(swiat):
    swiat.wszystkie()

    wynik = await _zaproponuj(_repo_python_i_node(swiat))

    assert wynik["stack"] == ["node", "python"]
    assert wynik["capabilities"] == {
        "test_narrow": "npm test", "typecheck": TYPECHECK_NODE, "lint": "ruff check ."}
    assert wynik["status"]["test_narrow"] == {
        "komenda": "npm test", "obecna": True, "powod": ""}


async def test_gdy_npm_test_oblewa_a_pytest_dziala_test_narrow_to_pytest_i_status_to_pytest(swiat):
    swiat.wszystkie()
    swiat.atrapa("npm", kod=1)

    wynik = await _zaproponuj(_repo_python_i_node(swiat))

    assert wynik["capabilities"]["test_narrow"] == "pytest -q"
    assert wynik["status"]["test_narrow"] == {
        "komenda": "pytest -q", "obecna": True, "powod": ""}


async def test_gdy_oba_test_narrow_oblewaja_status_opisuje_pierwszego_kandydata_a_capability_brak(
    swiat,
):
    swiat.wszystkie()
    swiat.atrapa("npm", kod=1)
    swiat.atrapa("pytest", kod=2)

    wynik = await _zaproponuj(_repo_python_i_node(swiat))

    assert "test_narrow" not in wynik["capabilities"]
    assert wynik["status"]["test_narrow"] == {
        "komenda": "npm test", "obecna": False, "powod": "kod wyjścia 1"}


@pytest.mark.parametrize("ekosystem", list(OCZEKIWANE))
async def test_sonda_nie_zmienia_drzewa_repozytorium_nawet_gdy_narzedzia_chca_zapisac_cache(
    swiat, ekosystem,
):
    _zainstaluj_narzedzia_zapisujace_cache(swiat)
    repo = _repo(swiat, "app", ekosystem)
    przed = _drzewo(repo)

    wynik = await _zaproponuj(repo)

    assert wynik["capabilities"] == OCZEKIWANE[ekosystem][1]   # sonda faktycznie cos uruchomila
    assert _drzewo(repo) == przed


async def test_sonda_nie_zapisuje_propozycji_do_repozytorium_tylko_ja_zwraca(swiat):
    swiat.wszystkie()
    repo = _repo(swiat, "app", "python")

    await _zaproponuj(repo)

    assert not (repo / ".cockpit").exists()


# ---- yaml propozycji ---------------------------------------------------------------------------

def _wczytaj_yaml(swiat, tresc: str):
    repo = swiat.baza / "z-yaml"
    (repo / ".cockpit").mkdir(parents=True)
    (repo / ".cockpit" / "project.yaml").write_text(tresc, encoding="utf-8")
    return wczytaj_projekt(repo)


@pytest.mark.parametrize("ekosystem", list(OCZEKIWANE))
async def test_yaml_propozycji_przechodzi_wczytywanie_projektu_i_niesie_stos_i_capabilities(
    swiat, ekosystem,
):
    swiat.wszystkie()
    repo = _repo(swiat, "app", ekosystem)

    wynik = await _zaproponuj(repo)
    projekt, problemy = _wczytaj_yaml(swiat, wynik["yaml"])

    assert problemy == []
    assert projekt is not None
    assert projekt.stack == [ekosystem]
    assert projekt.capabilities == OCZEKIWANE[ekosystem][1]


async def test_yaml_propozycji_nie_zawiera_capability_ktorej_komenda_nie_dziala(swiat):
    swiat.wszystkie()
    swiat.atrapa("ruff", kod=1)
    repo = _repo(swiat, "app", "python")

    wynik = await _zaproponuj(repo)
    projekt, problemy = _wczytaj_yaml(swiat, wynik["yaml"])

    assert problemy == []
    assert projekt.capabilities == {"test_narrow": "pytest -q"}


async def test_yaml_repozytorium_bez_stosu_jest_poprawny_i_pusty(swiat):
    swiat.wszystkie()
    repo = swiat.korzen / "pusty"
    repo.mkdir()

    wynik = await _zaproponuj(repo)
    projekt, problemy = _wczytaj_yaml(swiat, wynik["yaml"])

    assert problemy == []
    assert (projekt.stack, projekt.capabilities) == ([], {})


# =================================================================================
# 3. Operacja runnera `probe`
# =================================================================================

async def _probe(**dane) -> dict:
    return await _w_limicie(
        operacje.wykonaj({"type": "probe", "req_id": "r-1", **dane}), "operacja probe")


def _blad(odp: dict, kod: str) -> None:
    assert odp["ok"] is False, odp
    assert odp["kod"] == kod, odp
    assert odp["req_id"] == "r-1"
    assert isinstance(odp["blad"], str) and odp["blad"]


def test_runner_zna_operacje_sondy_ekosystemu():
    assert "probe" in operacje.OPERACJE


async def test_sonda_przez_runnera_zwraca_stos_capabilities_status_i_yaml_repozytorium(swiat):
    swiat.wszystkie()
    repo = _repo(swiat, "app", "python")

    odp = await _probe(sciezka=str(repo))

    assert odp["ok"] is True, odp
    assert odp["req_id"] == "r-1"
    wynik = odp["wynik"]
    assert sorted(wynik) == ["capabilities", "stack", "status", "yaml"]
    assert wynik["stack"] == ["python"]
    assert wynik["capabilities"] == OCZEKIWANE["python"][1]
    assert wynik["status"]["lint"] == {"komenda": "ruff check .", "obecna": True, "powod": ""}
    json.dumps(odp)   # odpowiedz idzie do kokpitu jako JSON


async def test_sonda_przez_runnera_uruchamia_komendy_w_podanym_repozytorium(swiat):
    swiat.wszystkie()
    repo = _repo(swiat, "app", "go")

    await _probe(sciezka=str(repo))

    wywolania = _wywolania(swiat, "go")
    assert wywolania
    assert {Path(w["pwd"]) for w in wywolania} == {repo}


async def test_sonda_przez_runnera_nie_zmienia_drzewa_repozytorium(swiat):
    _zainstaluj_narzedzia_zapisujace_cache(swiat)
    repo = _repo(swiat, "app", "python")
    przed = _drzewo(repo)

    odp = await _probe(sciezka=str(repo))

    assert odp["ok"] is True, odp
    assert _drzewo(repo) == przed


async def test_katalog_poza_zakresem_runnera_jest_odrzucony_a_zadna_komenda_nie_rusza(swiat):
    swiat.wszystkie()
    obcy = swiat.obcy / "cudze"
    obcy.mkdir()
    (obcy / "pyproject.toml").write_text("", encoding="utf-8")

    odp = await _probe(sciezka=str(obcy))

    _blad(odp, "poza_zakresem")
    assert odp["blad"] == KOMUNIKAT_POZA_ZAKRESEM
    assert str(obcy) not in json.dumps(odp)
    assert list(swiat.logi.iterdir()) == []


async def test_dowiazanie_z_zakresu_do_katalogu_poza_zakresem_jest_odrzucone_bez_uruchamiania(
    swiat,
):
    swiat.wszystkie()
    obcy = swiat.obcy / "cudze"
    obcy.mkdir()
    (obcy / "pyproject.toml").write_text("", encoding="utf-8")
    (swiat.korzen / "furtka").symlink_to(obcy, target_is_directory=True)

    odp = await _probe(sciezka=str(swiat.korzen / "furtka"))

    _blad(odp, "poza_zakresem")
    assert list(swiat.logi.iterdir()) == []


async def test_sciezka_wzgledna_jest_odrzucona_jak_przez_wszystkie_operacje_runnera(swiat):
    swiat.wszystkie()
    _repo(swiat, "app", "python")

    _blad(await _probe(sciezka="app"), "poza_zakresem")
    assert list(swiat.logi.iterdir()) == []


@pytest.mark.parametrize("dane", [{}, {"sciezka": 5}, {"sciezka": None}],
                         ids=["brak_pola", "liczba", "null"])
async def test_sonda_bez_sciezki_tekstowej_to_blad_zle_dane(swiat, dane):
    swiat.wszystkie()

    _blad(await _probe(**dane), "zle_dane")
    assert list(swiat.logi.iterdir()) == []


async def test_nieistniejacy_katalog_to_blad_nie_istnieje_i_nic_nie_jest_uruchamiane(swiat):
    swiat.wszystkie()

    odp = await _probe(sciezka=str(swiat.korzen / "nie-ma"))

    _blad(odp, "nie_istnieje")
    assert odp["blad"] == "sciezka nie istnieje"
    assert list(swiat.logi.iterdir()) == []


@pytest.mark.parametrize(
    "limit", [0, -1, 0.0, -0.5, "5", True, False, [1], math.nan, math.inf, 10**400],
    ids=["zero", "ujemny", "zero_float", "ujemny_float", "napis", "prawda",
         "falsz", "lista", "nan", "nieskonczonosc", "int_401_cyfr"])
async def test_zly_limit_czasu_sondy_to_blad_zle_dane_bez_uruchamiania_komend(swiat, limit):
    swiat.wszystkie()
    repo = _repo(swiat, "app", "python")

    odp = await _probe(sciezka=str(repo), limit_s=limit)

    _blad(odp, "zle_dane")
    assert odp["blad"] == "pole limit_s musi byc liczba wieksza od zera"
    assert list(swiat.logi.iterdir()) == []


@pytest.mark.parametrize("limit", [5, 0.75], ids=["calkowity", "ulamkowy"])
async def test_dodatni_limit_czasu_calkowity_albo_ulamkowy_jest_przyjety(swiat, limit):
    swiat.wszystkie()
    repo = _repo(swiat, "app", "python")

    odp = await _probe(sciezka=str(repo), limit_s=limit)

    assert odp["ok"] is True, odp
    assert odp["wynik"]["capabilities"] == OCZEKIWANE["python"][1]


async def test_limit_czasu_z_zapytania_obowiazuje_komendy_sondy(swiat):
    swiat.wszystkie()
    swiat.atrapa("ruff", spij=True)
    repo = _repo(swiat, "app", "python")

    odp = await _probe(sciezka=str(repo), limit_s=KROTKI_LIMIT_S)

    assert odp["ok"] is True, odp
    assert odp["wynik"]["status"]["lint"] == {
        "komenda": "ruff check .", "obecna": False, "powod": "limit czasu"}
    assert odp["wynik"]["capabilities"] == {"test_narrow": "pytest -q"}


async def test_sonda_przez_runnera_konczy_sie_w_rozsadnym_czasie_mimo_zawieszonej_komendy(swiat):
    swiat.wszystkie()
    swiat.atrapa("pytest", spij=True)
    swiat.atrapa("ruff", spij=True)
    repo = _repo(swiat, "app", "python")
    start = time.monotonic()

    odp = await _probe(sciezka=str(repo), limit_s=KROTKI_LIMIT_S)

    assert odp["ok"] is True, odp
    assert odp["wynik"]["capabilities"] == {}
    assert time.monotonic() - start < LIMIT_TESTU_S

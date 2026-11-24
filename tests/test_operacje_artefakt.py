"""HAR-182: operacja runnera `read_artifact` - podglad artefaktu runu z katalogu runu.

Kontrakt (przyjety przez test-writera, opisany w raporcie):
 * dane `{run_id, sciezka}`; katalog runu liczy runner (`katalog_runu(run_id)`, `COCKPIT_RUNS_DIR`),
   a sciezka jest WZGLEDNA wobec niego; zakres projektow z `runner.json` nie bierze udzialu;
 * wynik `{"text", "truncated", "full_len"}`; limit to `operacje.LIMIT_ARTEFAKTU_B` (bajty,
   czytany przy kazdym wywolaniu); plik dluzszy niz limit to `truncated=True` z `text` = pierwsze
   `limit` bajtow (urwany znak wielobajtowy odpada), a `full_len` to rozmiar calego pliku w BAJTACH
   liczony przed obcieciem - nie blad `za_duzy`;
 * odmowy: `..`, sciezka bezwzgledna, dowiazanie wychodzace z katalogu runu, sekret -> `poza_zakresem`;
   NUL w sciezce, pusta sciezka, zly `run_id`, pole nie-napis -> `zle_dane`; binarny -> `binarny`;
   katalog -> `to_katalog`; brak pliku albo katalogu runu -> `nie_istnieje`; FIFO -> `blad_dysku`;
 * limit jest mniejszy niz `runner_odpowiedz_max_kb` z zapasem na rozdmuchanie JSON (do 6x dla
   znakow sterujacych C0: jeden bajt to szesc znakow w JSON), wiec odpowiedz z pelnym limitem nie jest odrzucona przez kokpit.
Testy chodza przez `operacje.wykonaj` (prawdziwy punkt wejscia runnera) na prawdziwych plikach.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cockpit.services import artefakty
from cockpit.services import zapytania_runnera as zapytania
from cockpit.settings import settings
from runner import operacje

RUN_ID = "run-art-1"
TRESC_SEKRETNA = "wnetrze-sekretu-artefaktu-77413"
MALY_LIMIT = 100


@pytest.fixture
def swiat(tmp_path, monkeypatch):
    """Katalog runow i projekt w zakresie runnera LEZA OSOBNO: odczyt artefaktu nie moze zalezec
    od zakresu projektow. Obcy katalog sluzy do prob ucieczki dowiazaniem."""
    baza = tmp_path.resolve()
    runy, projekt, obcy, dom = baza / "runy", baza / "projekt", baza / "obcy", baza / "dom"
    for k in (runy, projekt, obcy, dom):
        k.mkdir()
    katalog = runy / RUN_ID
    katalog.mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(projekt)]}), encoding="utf-8")
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(runy))
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    monkeypatch.delenv("COCKPIT_MAX_READ_KB", raising=False)
    return SimpleNamespace(baza=baza, runy=runy, katalog=katalog, obcy=obcy, projekt=projekt)


@pytest.fixture(params=["maly_limit", "domyslny_limit"])
def limit(request, monkeypatch) -> int:
    """Granice obcinania sprawdzane i na malym limicie (szybko), i na domyslnym (prawdziwa wartosc)."""
    if request.param == "maly_limit":
        monkeypatch.setattr(operacje, "LIMIT_ARTEFAKTU_B", MALY_LIMIT)
        return MALY_LIMIT
    return operacje.LIMIT_ARTEFAKTU_B


@pytest.fixture
def maly_limit(monkeypatch) -> int:
    monkeypatch.setattr(operacje, "LIMIT_ARTEFAKTU_B", MALY_LIMIT)
    return MALY_LIMIT


async def _czytaj(sciezka, run_id=RUN_ID, **dodatkowe) -> dict:
    return await operacje.wykonaj(
        {"type": "read_artifact", "req_id": "r-1", "run_id": run_id, "sciezka": sciezka,
         **dodatkowe})


def _zapisz(swiat, sciezka: str, tresc: bytes):
    plik = swiat.katalog / sciezka
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_bytes(tresc)
    return plik


def _blad(odp: dict, kod: str, komunikat: str | None = None) -> None:
    assert odp["ok"] is False, odp
    assert odp["kod"] == kod, odp
    assert odp["req_id"] == "r-1"
    assert isinstance(odp["blad"], str) and odp["blad"]
    if komunikat is not None:   # komunikaty sa stale: kokpit pokazuje je czlowiekowi
        assert odp["blad"] == komunikat, odp


def _wzorzec(n: int) -> bytes:
    """n bajtow ASCII, w ktorych kazdy prefiks da sie odroznic od innego."""
    return (b"abcdefghij" * (n // 10 + 1))[:n]


# =================================================================================
# 1. Rejestracja i odczyt
# =================================================================================

def test_runner_zna_operacje_podgladu_artefaktu_obok_odczytu_plikow():
    assert "read_artifact" in operacje.OPERACJE
    assert "read_file" in operacje.OPERACJE


async def test_artefakt_runu_jest_czytany_z_katalogu_runu_mimo_ze_zakres_projektow_go_nie_obejmuje(
    swiat,
):
    _zapisz(swiat, "plan.md", "# Plan zażółć\n".encode("utf-8"))

    odp = await _czytaj("plan.md")

    assert odp["ok"] is True, odp
    assert odp["req_id"] == "r-1"
    assert odp["wynik"]["text"] == "# Plan zażółć\n"
    assert odp["wynik"]["truncated"] is False
    assert odp["wynik"]["full_len"] == len("# Plan zażółć\n".encode("utf-8"))


async def test_artefakt_w_podkatalogu_runu_jest_odczytany_po_sciezce_wzglednej(swiat):
    _zapisz(swiat, "wyniki/etap-1/raport.md", b"raport")

    odp = await _czytaj("wyniki/etap-1/raport.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["text"] == "raport"


async def test_odpowiedz_z_artefaktem_nie_zdradza_ukladu_dysku_runnera(swiat):
    _zapisz(swiat, "plan.md", b"plan")

    odp = await _czytaj("plan.md")

    assert odp["ok"] is True, odp
    assert str(swiat.baza) not in json.dumps(odp)


async def test_pusty_artefakt_to_pusty_tekst_bez_obciecia_a_nie_brak_pomiaru(swiat):
    _zapisz(swiat, "pusty.md", b"")

    odp = await _czytaj("pusty.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["text"] == ""
    assert odp["wynik"]["truncated"] is False
    assert odp["wynik"]["full_len"] == 0


async def test_dowiazanie_w_obrebie_katalogu_runu_jest_czytane_bo_nie_wychodzi_na_zewnatrz(swiat):
    _zapisz(swiat, "prawdziwy.md", b"w srodku")
    (swiat.katalog / "alias.md").symlink_to(swiat.katalog / "prawdziwy.md")

    odp = await _czytaj("alias.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["text"] == "w srodku"


# =================================================================================
# 2. Limit: truncated i full_len na granicach
# =================================================================================

async def test_artefakt_o_bajt_mniejszy_niz_limit_przychodzi_w_calosci(swiat, limit):
    tresc = _wzorzec(limit - 1)
    _zapisz(swiat, "a.md", tresc)

    odp = await _czytaj("a.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"] == {"text": tresc.decode(), "truncated": False, "full_len": limit - 1}


async def test_artefakt_dokladnie_rowny_limitowi_przychodzi_w_calosci_bez_obciecia(swiat, limit):
    tresc = _wzorzec(limit)
    _zapisz(swiat, "a.md", tresc)

    odp = await _czytaj("a.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"] == {"text": tresc.decode(), "truncated": False, "full_len": limit}


async def test_artefakt_o_bajt_wiekszy_niz_limit_jest_obciety_do_limitu_a_nie_odrzucony(swiat, limit):
    tresc = _wzorzec(limit + 1)
    _zapisz(swiat, "a.md", tresc)

    odp = await _czytaj("a.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"] == {"text": tresc[:limit].decode(), "truncated": True, "full_len": limit + 1}


async def test_pelna_dlugosc_obcietego_artefaktu_to_rozmiar_calego_pliku_a_nie_limit_plus_jeden(
    swiat, limit,
):
    rozmiar = 5 * limit + 7
    tresc = _wzorzec(rozmiar)
    _zapisz(swiat, "duzy.md", tresc)

    odp = await _czytaj("duzy.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["truncated"] is True
    assert odp["wynik"]["full_len"] == rozmiar
    assert odp["wynik"]["text"] == tresc[:limit].decode()


async def test_podglad_artefaktu_obcina_plik_dopiero_powyzej_512_kb_bez_zmiany_limitu(swiat):
    # prawdziwy limit z kodu produkcyjnego, bez podmiany: 512 KB to granica podgladu
    limit = 512 * 1024
    tresc = _wzorzec(limit + 1)
    _zapisz(swiat, "duzy.md", tresc)

    odp = await _czytaj("duzy.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["truncated"] is True
    assert len(odp["wynik"]["text"]) == limit
    assert odp["wynik"]["full_len"] == limit + 1


async def test_limit_artefaktu_jest_czytany_przy_kazdym_wywolaniu_a_nie_zapamietany(swiat, monkeypatch):
    _zapisz(swiat, "a.md", _wzorzec(150))

    monkeypatch.setattr(operacje, "LIMIT_ARTEFAKTU_B", 100)
    maly = await _czytaj("a.md")
    monkeypatch.setattr(operacje, "LIMIT_ARTEFAKTU_B", 200)
    duzy = await _czytaj("a.md")

    assert (maly["wynik"]["truncated"], duzy["wynik"]["truncated"]) == (True, False)
    assert len(maly["wynik"]["text"]) == 100
    assert len(duzy["wynik"]["text"]) == 150


async def test_obciecie_w_srodku_znaku_wielobajtowego_daje_tekst_bez_urwanego_znaku_a_nie_binarny(
    swiat, maly_limit,
):
    # bajt numer 99 (liczac od 0) to pierwszy bajt "ą": ciecie po 100 bajtach rozcina znak
    tresc = b"a" * 99 + "ą".encode("utf-8") * 10
    _zapisz(swiat, "pl.md", tresc)

    odp = await _czytaj("pl.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"] == {"text": "a" * 99, "truncated": True, "full_len": 99 + 20}


async def test_pelna_dlugosc_artefaktu_jest_w_bajtach_a_nie_w_znakach(swiat, maly_limit):
    _zapisz(swiat, "pl.md", "ą".encode("utf-8") * 30)   # 60 bajtow, 30 znakow

    odp = await _czytaj("pl.md")

    assert odp["wynik"]["full_len"] == 60
    assert odp["wynik"]["truncated"] is False


async def test_artefakt_binarny_wiekszy_niz_limit_jest_binarny_a_nie_obcietym_tekstem(
    swiat, maly_limit,
):
    _zapisz(swiat, "obraz.bin", b"\xff\xfe" + b"a" * 300)

    odp = await _czytaj("obraz.bin")

    _blad(odp, "binarny")


async def test_artefakt_z_bajtem_zerowym_w_poczatku_pliku_wiekszego_niz_limit_jest_binarny(
    swiat, maly_limit,
):
    _zapisz(swiat, "nul.dat", b"ab\x00cd" + b"a" * 300)

    odp = await _czytaj("nul.dat")

    _blad(odp, "binarny")


async def test_limit_artefaktu_jest_dodatni_i_miesci_sie_w_limicie_odpowiedzi_runnera_z_zapasem():
    # najgorszy rozrost w JSON to 6x: znak sterujacy C0 ("\x01" -> "\u0001"); emoji to 3x
    assert 0 < operacje.LIMIT_ARTEFAKTU_B
    assert operacje.LIMIT_ARTEFAKTU_B * 6 < settings.runner_odpowiedz_max_kb * 1024


@pytest.mark.parametrize("znak", ["ą", "€", "😀", "\x01"],
                         ids=["dwa_bajty", "trzy_bajty", "cztery_bajty", "znak_sterujacy_szesciokrotny_rozrost"])
async def test_artefakt_dokladnie_o_dlugosci_limitu_ze_znakow_spoza_ascii_przechodzi_limit_odpowiedzi_kokpitu(
    swiat, znak,
):
    bajty_znaku = len(znak.encode("utf-8"))
    tresc = znak * (operacje.LIMIT_ARTEFAKTU_B // bajty_znaku)
    _zapisz(swiat, "obcy.md", tresc.encode("utf-8"))

    async def wyslij(runner_id, typ, dane, timeout_s):
        return await operacje.wykonaj({"type": typ, "req_id": "r-1", **dane})

    # prawdziwa kontrola rozmiaru odpowiedzi po stronie kokpitu
    wynik = await zapytania._wyslij_i_sprawdz(
        "runner-x", "read_artifact", {"run_id": RUN_ID, "sciezka": "obcy.md"}, wyslij)

    assert wynik["truncated"] is False
    assert wynik["text"] == tresc


# =================================================================================
# 3. Odmowy: wyjscie poza katalog runu, sekrety, zle dane
# =================================================================================

@pytest.mark.parametrize("sciezka", [
    "../poza.md", "a/../../poza.md", "..", "a/..", "./../poza.md", "a/../b.md",
], ids=["dwie_kropki_na_poczatku", "dwie_kropki_w_glebi", "same_dwie_kropki", "kropki_na_koncu",
        "kropka_i_dwie_kropki", "kropki_wracajace_do_srodka"])
async def test_sciezka_z_dwiema_kropkami_jest_odmowiona_takze_gdy_konczy_w_katalogu_runu(
    swiat, sciezka,
):
    (swiat.runy / "poza.md").write_text(TRESC_SEKRETNA, encoding="utf-8")
    _zapisz(swiat, "b.md", b"w srodku")
    (swiat.katalog / "a").mkdir(exist_ok=True)

    odp = await _czytaj(sciezka)

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


@pytest.mark.parametrize("sciezka", ["plan..md", "..plan.md", "a/..b/c.md"],
                         ids=["kropki_w_nazwie_pliku", "kropki_na_poczatku_nazwy", "kropki_w_nazwie_katalogu"])
async def test_nazwa_pliku_z_dwiema_kropkami_wewnatrz_skladnika_nie_jest_wyjsciem_poza_katalog_runu(
    swiat, sciezka,
):
    # `..` odmawiamy tylko jako caly skladnik sciezki; "plan..md" to zwykla nazwa pliku
    # (kontrola po podciagu `..` odmowilaby tez jej)
    _zapisz(swiat, sciezka, b"zwykla tresc")

    odp = await _czytaj(sciezka)

    assert odp["ok"] is True, odp
    assert odp["wynik"]["text"] == "zwykla tresc"


async def test_sciezka_bezwzgledna_jest_odmowiona_takze_gdy_wskazuje_plik_w_katalogu_runu(swiat):
    plik = _zapisz(swiat, "plan.md", TRESC_SEKRETNA.encode())

    odp = await _czytaj(str(plik))

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_sciezka_bezwzgledna_poza_katalogiem_runu_jest_odmowiona(swiat):
    cudzy = swiat.obcy / "cudzy.txt"
    cudzy.write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _czytaj(str(cudzy))

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_dowiazanie_do_pliku_poza_katalogiem_runu_jest_odmowione_bez_tresci_celu(swiat):
    cudzy = swiat.obcy / "cudzy.txt"
    cudzy.write_text(TRESC_SEKRETNA, encoding="utf-8")
    (swiat.katalog / "wyjscie.md").symlink_to(cudzy)

    odp = await _czytaj("wyjscie.md")

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)
    assert str(swiat.obcy) not in json.dumps(odp)


async def test_plik_za_dowiazaniem_do_katalogu_poza_runem_jest_odmowiony(swiat):
    (swiat.obcy / "cudzy.txt").write_text(TRESC_SEKRETNA, encoding="utf-8")
    (swiat.katalog / "katalog_obcy").symlink_to(swiat.obcy, target_is_directory=True)

    odp = await _czytaj("katalog_obcy/cudzy.txt")

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_dowiazanie_do_katalogu_runu_innego_runu_jest_odmowione(swiat):
    inny = swiat.runy / "run-inny"
    inny.mkdir()
    (inny / "plan.md").write_text(TRESC_SEKRETNA, encoding="utf-8")
    (swiat.katalog / "sasiad").symlink_to(inny, target_is_directory=True)

    odp = await _czytaj("sasiad/plan.md")

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


@pytest.mark.parametrize("nazwa", [".env", ".env.local", "klucz.pem", "serwer.key", "id_rsa",
                                   "podkatalog/.env", "podkatalog/id_ed25519"])
async def test_sekret_w_katalogu_runu_nie_jest_pokazywany_jako_artefakt(swiat, nazwa):
    _zapisz(swiat, nazwa, TRESC_SEKRETNA.encode())

    odp = await _czytaj(nazwa)

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_sciezka_z_bajtem_zerowym_to_zle_dane(swiat):
    _zapisz(swiat, "a.md", b"x")

    odp = await _czytaj("a.md\x00.txt")

    _blad(odp, "zle_dane")


@pytest.mark.parametrize("dane", [
    {"sciezka": 5}, {"sciezka": None}, {"sciezka": ["a.md"]}, {"sciezka": ""}, {},
], ids=["liczba", "none", "lista", "pusta", "brak_pola"])
async def test_zle_pole_sciezki_w_zapytaniu_o_artefakt_to_zle_dane(swiat, dane):
    odp = await operacje.wykonaj(
        {"type": "read_artifact", "req_id": "r-1", "run_id": RUN_ID, **dane})

    _blad(odp, "zle_dane")


@pytest.mark.parametrize("run_id", ["..", ".", "", "a/b", "a\\b", 5, None, "a\x00b"])
async def test_zly_identyfikator_runu_w_zapytaniu_o_artefakt_to_zle_dane(swiat, run_id):
    (swiat.runy / "a").mkdir(exist_ok=True)
    (swiat.runy / "poza.md").write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _czytaj("poza.md", run_id=run_id)

    _blad(odp, "zle_dane")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_brak_pola_identyfikatora_runu_w_zapytaniu_o_artefakt_to_zle_dane(swiat):
    odp = await operacje.wykonaj({"type": "read_artifact", "req_id": "r-1", "sciezka": "a.md"})

    _blad(odp, "zle_dane")


# =================================================================================
# 4. Brak pliku, katalog, nie-plik
# =================================================================================

async def test_artefakt_ktorego_nie_ma_to_kod_nie_istnieje(swiat):
    odp = await _czytaj("brak.md")

    _blad(odp, "nie_istnieje")


async def test_run_bez_katalogu_u_runnera_to_kod_nie_istnieje_a_nie_wyjatek(swiat):
    odp = await _czytaj("plan.md", run_id="run-ktorego-nie-ma")

    _blad(odp, "nie_istnieje")


async def test_katalog_zamiast_artefaktu_to_kod_to_katalog(swiat):
    (swiat.katalog / "podkatalog").mkdir()

    odp = await _czytaj("podkatalog")

    _blad(odp, "to_katalog")


async def test_binarny_artefakt_to_kod_binarny_bez_tresci(swiat):
    _zapisz(swiat, "obraz.bin", TRESC_SEKRETNA.encode() + b"\xff\xfe\x80")

    odp = await _czytaj("obraz.bin")

    _blad(odp, "binarny")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_artefakt_z_bajtem_zerowym_w_poprawnym_utf8_jest_binarny(swiat):
    _zapisz(swiat, "nul.dat", b"ab\x00cd")

    odp = await _czytaj("nul.dat")

    _blad(odp, "binarny")


async def test_kolejka_fifo_w_katalogu_runu_jest_odmowiona_bez_zawieszenia(swiat):
    os.mkfifo(swiat.katalog / "kolejka")

    odp = await asyncio.wait_for(_czytaj("kolejka"), 10)

    _blad(odp, "blad_dysku")


# =================================================================================
# 5. Komunikaty odmow sa stale i czytelne dla czlowieka (HAR-182, poprawka 1)
# =================================================================================

@pytest.mark.parametrize("run_id", ["..", "a\x00b"], ids=["kropki", "z_bajtem_zerowym"])
async def test_zly_identyfikator_runu_ma_staly_komunikat_bez_tresci_zadania(swiat, run_id):
    odp = await _czytaj("a.md", run_id=run_id)

    _blad(odp, "zle_dane", "niedozwolony identyfikator runu")


@pytest.mark.parametrize("sciezka", ["", "a.md\x00.txt"], ids=["pusta", "z_bajtem_zerowym"])
async def test_pusta_sciezka_albo_z_bajtem_zerowym_ma_staly_komunikat(swiat, sciezka):
    odp = await _czytaj(sciezka)

    _blad(odp, "zle_dane", "sciezka artefaktu jest pusta albo zawiera znak NUL")


async def test_katalog_zamiast_artefaktu_ma_staly_komunikat(swiat):
    (swiat.katalog / "podkatalog").mkdir()

    odp = await _czytaj("podkatalog")

    _blad(odp, "to_katalog", "sciezka jest katalogiem")


async def test_kolejka_fifo_zamiast_artefaktu_ma_staly_komunikat(swiat):
    os.mkfifo(swiat.katalog / "kolejka")

    odp = await asyncio.wait_for(_czytaj("kolejka"), 10)

    _blad(odp, "blad_dysku", "sciezka nie jest zwyklym plikiem")


async def test_artefakt_z_bajtem_zerowym_ma_staly_komunikat_bez_tresci_pliku(swiat):
    _zapisz(swiat, "nul.dat", TRESC_SEKRETNA.encode() + b"\x00")

    odp = await _czytaj("nul.dat")

    _blad(odp, "binarny", "plik nie jest tekstem (zawiera bajt NUL)")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_artefakt_z_niepoprawnym_utf8_ma_staly_komunikat_bez_tresci_pliku(swiat):
    _zapisz(swiat, "obraz.bin", TRESC_SEKRETNA.encode() + b"\xff\xfe\x80")

    odp = await _czytaj("obraz.bin")

    _blad(odp, "binarny", "plik nie jest tekstem UTF-8")
    assert TRESC_SEKRETNA not in json.dumps(odp)


# =================================================================================
# 6. Ukosniki odwrotne, rozdzielenie kontroli sciezki bezwzglednej, urwany koniec pliku
# =================================================================================

def _podmien_isabs_w_runnerze(monkeypatch, isabs) -> None:
    """`os` widziany przez modul runnera ma inna ocene `isabs`; reszta `os` (takze `os.path`) jest
    prawdziwa. Globalna podmiana `os.path.isabs` zepsulaby `Path.resolve` i zakres."""
    prawdziwy_os = os

    class _Path:
        def __getattr__(self, nazwa):
            return getattr(prawdziwy_os.path, nazwa)

    _Path.isabs = staticmethod(isabs)

    class _Os:
        path = _Path()

        def __getattr__(self, nazwa):
            return getattr(prawdziwy_os, nazwa)

    monkeypatch.setattr(operacje, "os", _Os())


@pytest.mark.parametrize("sciezka", ["..\\poza.md", "a\\..\\b.md", "a\\..", "a/..\\b.md"],
                         ids=["na_poczatku", "w_srodku", "na_koncu", "z_ukosnikiem_zwyklym"])
async def test_sciezka_z_dwiema_kropkami_rozdzielonymi_ukosnikiem_odwrotnym_jest_odmowiona(
    swiat, sciezka,
):
    # runner moze dzialac tam, gdzie `\` rozdziela katalogi; na Linuksie to zwykly znak nazwy,
    # wiec bez kontroli ten wariant skonczylby jako `nie_istnieje`, nie jako odmowa
    (swiat.runy / "poza.md").write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _czytaj(sciezka)

    _blad(odp, "poza_zakresem", operacje.KOMUNIKAT_POZA_ZAKRESEM)
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_sciezka_zaczynajaca_sie_od_ukosnika_jest_odmowiona_takze_gdy_system_nie_uzna_jej_za_bezwzgledna(
    swiat, monkeypatch,
):
    # na Linuksie `isabs` i `startswith("/")` zawsze sie zgadzaja, wiec kazda z kontroli osobno
    # sprawdzamy, rozjezdzajac je w tescie; plik lezy WEWNATRZ katalogu runu, zeby dopiero
    # kontrola sciezki bezwzglednej (a nie zakres) go odmowila
    plik = _zapisz(swiat, "plan.md", TRESC_SEKRETNA.encode())
    sciezka = str(plik)
    _podmien_isabs_w_runnerze(monkeypatch, lambda p: False if p == sciezka else os.path.isabs(p))

    odp = await _czytaj(sciezka)

    _blad(odp, "poza_zakresem", operacje.KOMUNIKAT_POZA_ZAKRESEM)
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_sciezka_uznana_przez_system_za_bezwzgledna_jest_odmowiona_takze_bez_poczatkowego_ukosnika(
    swiat, monkeypatch,
):
    _zapisz(swiat, "plan.md", TRESC_SEKRETNA.encode())
    _podmien_isabs_w_runnerze(monkeypatch, lambda p: True if p == "plan.md" else os.path.isabs(p))

    odp = await _czytaj("plan.md")

    _blad(odp, "poza_zakresem", operacje.KOMUNIKAT_POZA_ZAKRESEM)
    assert TRESC_SEKRETNA not in json.dumps(odp)


@pytest.mark.parametrize("koniec", [b"\xc4", b"\xe2\x82", b"\xf0\x9f\x98"],
                         ids=["z_dwoch_bajtow", "z_trzech_bajtow", "z_czterech_bajtow"])
async def test_artefakt_mniejszy_niz_limit_z_urwanym_znakiem_na_koncu_jest_binarny_a_nie_przyciety(
    swiat, koniec,
):
    # bez obciecia po limicie urwany znak to uszkodzony plik, nie "tekst bez ostatniego znaku"
    _zapisz(swiat, "uciety.md", b"poczatek " + koniec)

    odp = await _czytaj("uciety.md")

    _blad(odp, "binarny", "plik nie jest tekstem UTF-8")


async def test_artefakt_dokladnie_o_dlugosci_limitu_z_urwanym_znakiem_na_koncu_jest_binarny(
    swiat, maly_limit,
):
    # plik konczy sie na granicy limitu (brak obciecia), a ostatni znak jest niepelny
    _zapisz(swiat, "uciety.md", b"a" * (maly_limit - 1) + b"\xc4")

    odp = await _czytaj("uciety.md")

    _blad(odp, "binarny", "plik nie jest tekstem UTF-8")


# =================================================================================
# 7. Rozmiar z fstat, wzgledny katalog runow, petla dowiazan, blad dysku przy budowie zakresu
# =================================================================================

async def test_pelna_dlugosc_nie_jest_mniejsza_niz_przeczytane_bajty_gdy_rozmiar_z_systemu_jest_zanizony(
    swiat, monkeypatch,
):
    # plik urosl miedzy `fstat` a odczytem: pelna dlugosc to co najmniej tyle, ile przeczytano,
    # bo kokpit odrzuca `full_len` krotszy niz przyslany tekst
    tresc = b"0123456789" * 5
    _zapisz(swiat, "rosnie.md", tresc)
    prawdziwy_os = os

    class _Os:
        @staticmethod
        def fstat(fd):
            return SimpleNamespace(st_size=10)   # zanizony rozmiar

        def __getattr__(self, nazwa):
            return getattr(prawdziwy_os, nazwa)

    monkeypatch.setattr(operacje, "os", _Os())

    async def wyslij(runner_id, typ, dane, timeout_s):
        return await operacje.wykonaj({"type": typ, "req_id": "r-1", **dane})

    wynik = await zapytania._wyslij_i_sprawdz(
        "runner-x", "read_artifact", {"run_id": RUN_ID, "sciezka": "rosnie.md"}, wyslij)

    assert wynik == {"text": tresc.decode(), "truncated": False, "full_len": len(tresc)}
    assert artefakty._ksztalt_artefaktu(wynik) == (tresc.decode(), False, len(tresc))


async def test_wzgledny_katalog_runow_nie_blokuje_odczytu_artefaktu_bo_runner_liczy_sciezke_bezwzgledna(
    swiat, monkeypatch,
):
    _zapisz(swiat, "plan.md", b"plan z katalogu wzglednego")
    monkeypatch.chdir(swiat.baza)
    monkeypatch.setenv("COCKPIT_RUNS_DIR", "runy")   # wzgledny wobec biezacego katalogu

    odp = await _czytaj("plan.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["text"] == "plan z katalogu wzglednego"


def _resolve_rzuca_dla(monkeypatch, cel, wyjatek) -> None:
    """Path.resolve rzuca `wyjatek` tylko dla `cel`, pozostale sciezki rozwiazuje naprawde.
    Prawdziwa petla dowiazan daje RuntimeError tylko na czesci wersji Pythona (3.13 go nie rzuca),
    wiec wstrzykujemy wyjatek zamiast polegac na zachowaniu `resolve`."""
    oryginal = Path.resolve

    def owijka(self, *a, **kw):
        if self == cel:
            raise wyjatek("sztuczny blad resolve")
        return oryginal(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", owijka)


async def test_petla_dowiazan_w_miejscu_katalogu_runu_to_odmowa_poza_zakresem_a_nie_blad_operacji(
    swiat, monkeypatch,
):
    _zapisz(swiat, "plan.md", TRESC_SEKRETNA.encode())
    _resolve_rzuca_dla(monkeypatch, swiat.katalog, RuntimeError)

    odp = await _czytaj("plan.md")

    _blad(odp, "poza_zakresem", operacje.KOMUNIKAT_POZA_ZAKRESEM)
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_blad_dysku_przy_budowie_zakresu_artefaktu_ma_kod_bledu_dysku_a_nie_ogolny_blad_operacji(
    swiat, monkeypatch,
):
    # np. usuniety biezacy katalog przy wzglednym katalogu runow: `absolute()` pyta o cwd
    monkeypatch.setenv("COCKPIT_RUNS_DIR", "runy")

    def brak_praw():
        raise PermissionError("tajna-sciezka-cwd")

    monkeypatch.setattr(os, "getcwd", brak_praw)

    odp = await _czytaj("plan.md")

    _blad(odp, "brak_uprawnien", "brak uprawnien do pliku lub katalogu")
    assert "tajna-sciezka-cwd" not in json.dumps(odp)

"""HAR-168: `tools/raport_mutantow.py` - cienka nakladka na `tools/mutanty_zmian.py` i mutmuta (TDD).

Nakladka wola `python tools/mutanty_zmian.py --baza HEAD`, potem `python -m mutmut results --all
true`, liczy zbadane mutanty (status rozstrzygniety: killed, survived, timeout, no tests,
caught by type check, segfault; `suspicious` to nieznany kod wyjscia, nie werdykt; tylko ze wzorcow
zmienionych funkcji), a dla kazdego przezylego wola
`python -m mutmut show <klucz>` i wypisuje na stdout JSON `{"zbadane": int, "przezyle":
[{"plik","linia","opis_mutanta"}], "kod_narzedzia": int, "poza_zasiegiem": [{"plik","linia"}]}`.
Kod 2 narzedzia, blad `mutmut results`/`show`, status spoza znanych, niezgodnosc kluczy przezylych
(stdout narzedzia przy kodzie 1 vs `mutmut results`), kod 3 bez listy zmian poza zasiegiem i
niesparsowany hunk to kod 2 nakladki z komunikatem na stderr (brak dowodu to blad). Kod 3 trafia do
raportu jako `kod_narzedzia: 3` z lista `poza_zasiegiem` sparsowana ze stdout narzedzia.

Lista `poza_zasiegiem` jest wyliczana przy kazdym kodzie przez `lista_poza_zasiegiem(baza, korzen)`
(trzeci szew, podmieniany w `uruchom_nakladke` przez `FalszywyMutmut(poza=...)`); kod 3 porownuje ja
ze stdout narzedzia, kod 0 z niepusta lista to kod 2 nakladki.

Testy nie uruchamiaja ani gita, ani mutmuta: podmieniane sa dwa szwy modulu
- `raport_mutantow.uruchom(argv) -> subprocess.CompletedProcess[str]` (jedyne miejsce procesow),
- `raport_mutantow.wzorce_zmian(baza: str, korzen: Path) -> list[str]` (wzorce mutantow zmienionych
  funkcji, jak `mutanty_zmian.wzorce`),
a wejscie to `raport_mutantow.main(argv: list[str] | None = None) -> int`. Sama `wzorce_zmian` ma
osobne testy z podmieniona `mutanty_zmian.uruchom` (bez gita).

`mutmut show` liczy `@@ -N` od kodu SAMEJ funkcji (`get_diff_for_mutant`: `unified_diff` dwoch
tekstow `cst.Module([funkcja]).code.strip()`), wiec `linia` w raporcie to numer przeliczony po
prawdziwym zrodle. Testy kladą w `tmp_path` prawdziwy `src/cockpit/services/modul.py` (`chdir` -
nakladka bierze korzen z `Path.cwd()`), a wyjscie `mutmut show` budują tym samym `difflib.unified_diff`
co mutmut. Numery linii w oczekiwaniach sa wpisane recznie (liczone po ukladzie pliku w tescie).
"""
from __future__ import annotations

import json
import subprocess
import sys
from difflib import unified_diff
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

KLUCZE_PRZEZYLEGO = {"plik", "linia", "opis_mutanta"}
PLIK_ZRODLA = "src/cockpit/services/modul.py"
WZORZEC_F = "cockpit.services.modul.x_f__mutmut_*"
WZORZEC_G = "cockpit.services.modul.x_g__mutmut_*"
WZORZEC_K = "cockpit.services.modul.x_k__mutmut_*"
F1, F2, F3 = (f"cockpit.services.modul.x_f__mutmut_{i}" for i in (1, 2, 3))
G1 = "cockpit.services.modul.x_g__mutmut_1"
K1 = "cockpit.services.modul.x_k__mutmut_1"
H1, H2 = (f"cockpit.services.inny.x_h__mutmut_{i}" for i in (1, 2))


def klucz_metody(klasa: str, metoda: str, numer: int = 1) -> str:
    """Klucz mutanta metody: `modul.xǁKlasaǁmetoda__mutmut_N` (separator to U+01C1)."""
    return f"cockpit.services.modul.x\u01c1{klasa}\u01c1{metoda}__mutmut_{numer}"


def wyniki(*pary: tuple[str, str]) -> str:
    return "".join(f"    {klucz}: {status}\n" for klucz, status in pary)


def zrodlo_funkcji(**ciala: str) -> str:
    """Modul: 3 linie naglowka, potem funkcje po 3 linie i 2 puste (f od linii 4, g od 9, k od 14)."""
    tekst = '"""Modul testowy."""\n\n\n'
    for nazwa, cialo in ciala.items():
        tekst += f"def {nazwa}(x):\n    {cialo}\n    # koniec\n\n\n"
    return tekst


ZRODLO_DOMYSLNE = zrodlo_funkcji(f="return x", g="return x")


def pokaz_roznicy(
    klucz: str, stary: str, nowy: str, *, status: str = "survived", plik: str = PLIK_ZRODLA,
) -> str:
    """Wyjscie `mutmut show`: `# klucz: status`, potem `unified_diff` jak w `get_diff_for_mutant`."""
    roznica = unified_diff(
        stary.split("\n"), nowy.split("\n"), fromfile=plik, tofile=plik, lineterm="")
    return f"# {klucz}: {status}\n" + "\n".join(roznica) + "\n"


def pokaz(klucz: str, status: str = "survived", *, stare: str = "return x",
          nowe: str = "return None", plik: str = PLIK_ZRODLA) -> str:
    """Pokaz mutanta funkcji `f`/`g`/`k` (z klucza) o ciele jednej linii, jak w `zrodlo_funkcji`."""
    nazwa = klucz.rpartition("__mutmut_")[0].rpartition(".x_")[2]

    def kod(cialo: str) -> str:
        return f"def {nazwa}(x):\n    {cialo}\n    # koniec"

    return pokaz_roznicy(klucz, kod(stare), kod(nowe), status=status, plik=plik)


def szkielet(hunk: str, *, klucz: str = F1, plik: str = PLIK_ZRODLA) -> str:
    """Wyjscie `mutmut show` z recznie napisanym hunkiem (po naglowkach `---`/`+++`)."""
    return f"# {klucz}: survived\n--- {plik}\n+++ {plik}\n{hunk}"


def proces(argv, kod: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, kod, stdout=stdout, stderr=stderr)


POZA_ZASIEGIEM_STDOUT = "bez mutantów — sprawdź ręcznie\nsrc/cockpit/services/modul.py:5\n"


def klucze_przezylych(linie_wynikow: str) -> str:
    """Stdout `mutanty_zmian.py` przy kodzie 1: klucze przezylych (`survived`, `no tests`), po jednym w linii."""
    klucze = []
    for linia in linie_wynikow.splitlines():
        klucz, _, status = linia.strip().rpartition(": ")
        if "__mutmut_" in klucz and status in ("survived", "no tests"):
            klucze.append(klucz)
    return "".join(f"{klucz}\n" for klucz in klucze)


class FalszywyMutmut:
    """Zastepuje `uruchom`: zapisuje argv, odpowiada wedlug rodzaju polecenia.

    Bez jawnego `stdout_zmian` narzedzie mowi to, co `mutmut results`: przy kodzie 1 klucze przezylych
    z wynikow, przy kodzie 3 naglowek i jedna zmiane poza zasiegiem, inaczej nic."""

    def __init__(self, *, kod_zmian=1, stdout_zmian=None, stderr_zmian="", linie_wynikow=None,
                 kod_wynikow=0, pokazy=None, kod_show=0, poza=None):
        self.kod_zmian = kod_zmian
        # lista "poza zasiegiem" wyliczona przez nakladke (`lista_poza_zasiegiem`): lista wpisow
        # albo wywolywalna `(baza, korzen)`; domyslnie zgodna z domyslnym stdout narzedzia
        self.poza = poza if poza is not None else (
            [{"plik": PLIK_ZRODLA, "linia": 5}] if kod_zmian == 3 else [])
        self.stderr_zmian = stderr_zmian
        self.linie_wynikow = linie_wynikow if linie_wynikow is not None else wyniki((F1, "survived"))
        if stdout_zmian is None:
            stdout_zmian = {1: klucze_przezylych(self.linie_wynikow), 3: POZA_ZASIEGIEM_STDOUT}.get(
                kod_zmian, "")
        self.stdout_zmian = stdout_zmian
        self.kod_wynikow = kod_wynikow
        self.pokazy = pokazy if pokazy is not None else {F1: pokaz(F1)}
        self.kod_show = kod_show
        self.wywolania: list[list[str]] = []

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        self.wywolania.append(argv)
        if "results" in argv:
            return proces(argv, self.kod_wynikow, stdout=self.linie_wynikow,
                          stderr="results: blad" if self.kod_wynikow else "")
        if "show" in argv:
            return proces(argv, self.kod_show, stdout=self.pokazy.get(argv[-1], ""),
                          stderr="show: blad" if self.kod_show else "")
        return proces(argv, self.kod_zmian, stdout=self.stdout_zmian, stderr=self.stderr_zmian)

    def wolane(self, rodzaj: str) -> list[list[str]]:
        if rodzaj == "zmian":
            return [a for a in self.wywolania if "results" not in a and "show" not in a]
        return [a for a in self.wywolania if rodzaj in a]


def zapisz_zrodlo(korzen: Path, zrodlo: str | bytes | None, plik: str = PLIK_ZRODLA) -> None:
    if zrodlo is None:
        return
    sciezka = korzen / plik
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_bytes(zrodlo if isinstance(zrodlo, bytes) else zrodlo.encode("utf-8"))


def uruchom_nakladke(
    monkeypatch, capsys, falszywy: FalszywyMutmut, tmp_path: Path,
    *, wzorce=(WZORZEC_F, WZORZEC_G), zrodlo: str | bytes | None = ZRODLO_DOMYSLNE, args=(),
):
    import raport_mutantow

    zapisz_zrodlo(tmp_path, zrodlo)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(raport_mutantow, "uruchom", falszywy)
    # zrodlo listy poza zasiegiem to szew `lista_poza_zasiegiem`: testy nie wolaja gita ani `bez_mutantow`
    if callable(falszywy.poza):
        monkeypatch.setattr(raport_mutantow, "lista_poza_zasiegiem", falszywy.poza)
    else:
        monkeypatch.setattr(
            raport_mutantow, "lista_poza_zasiegiem",
            lambda baza, korzen: [dict(wpis) for wpis in falszywy.poza])
    if callable(wzorce):
        monkeypatch.setattr(raport_mutantow, "wzorce_zmian", wzorce)
    else:
        monkeypatch.setattr(raport_mutantow, "wzorce_zmian", lambda baza, korzen: list(wzorce))
    kod = raport_mutantow.main(list(args))
    wyjscie = capsys.readouterr()
    return kod, wyjscie.out, wyjscie.err


def raport_jednego(
    monkeypatch, capsys, tmp_path: Path, zrodlo: str | bytes | None, tekst_pokazu: str,
    klucz: str = F1,
):
    """Nakladka dla jednego przezylego mutanta `klucz` z danym wyjsciem `mutmut show` i zrodlem."""
    wzorzec = klucz.rpartition("__mutmut_")[0] + "__mutmut_*"
    f = FalszywyMutmut(linie_wynikow=wyniki((klucz, "survived")), pokazy={klucz: tekst_pokazu})
    return uruchom_nakladke(monkeypatch, capsys, f, tmp_path, wzorce=(wzorzec,), zrodlo=zrodlo)


def raport_z(out: str) -> dict:
    return json.loads(out)


def linia_przezylego(out: str) -> int:
    (przezyl,) = raport_z(out)["przezyle"]
    return przezyl["linia"]


# --- wywolania narzedzi ------------------------------------------------------------------------


def test_nakladka_wola_mutanty_zmian_z_baza_head_potem_wyniki_mutmuta_potem_pokaz_przezylego(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut()

    kod, _, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    zmian, wyniki_mutmuta, pokaz_mutanta = f.wywolania
    assert zmian[0] == sys.executable and Path(zmian[1]).name == "mutanty_zmian.py"
    assert zmian[zmian.index("--baza") + 1] == "HEAD"
    assert wyniki_mutmuta == [sys.executable, "-m", "mutmut", "results", "--all", "true"]
    assert pokaz_mutanta == [sys.executable, "-m", "mutmut", "show", F1]


def test_baza_z_wiersza_polecen_trafia_do_mutanty_zmian_i_do_wzorcow_a_korzenia_szuka_w_biezacym_katalogu(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow="")
    zapytania = []

    def wzorce(baza, korzen):
        zapytania.append((baza, korzen))
        return [WZORZEC_F]

    kod, _, _ = uruchom_nakladke(
        monkeypatch, capsys, f, tmp_path, wzorce=wzorce, args=["--baza", "origin/main"])

    assert kod == 0
    (zmian,) = f.wolane("zmian")
    assert zmian[zmian.index("--baza") + 1] == "origin/main"
    assert zapytania == [("origin/main", tmp_path.resolve())]


def test_raport_to_dokladnie_jeden_dokument_json_na_stdout_z_czterema_kluczami(
    monkeypatch, capsys, tmp_path,
):
    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, FalszywyMutmut(), tmp_path)

    assert kod == 0
    assert out.endswith("\n") and out.count("\n") == 1
    r = raport_z(out)
    assert set(r) == {"zbadane", "przezyle", "kod_narzedzia", "poza_zasiegiem"}
    assert r["zbadane"] == 1 and r["kod_narzedzia"] == 1 and r["poza_zasiegiem"] == []
    (przezyl,) = r["przezyle"]
    assert set(przezyl) == KLUCZE_PRZEZYLEGO


def test_przezyly_mutant_ma_plik_zrodla_i_prawdziwa_linie_pierwszej_zmienionej_linii(
    monkeypatch, capsys, tmp_path,
):
    # f zaczyna sie w linii 4 pliku, zmieniona jest jej druga linia `return x` => linia 5
    _, out, _ = uruchom_nakladke(monkeypatch, capsys, FalszywyMutmut(), tmp_path)

    (przezyl,) = raport_z(out)["przezyle"]
    assert przezyl == {
        "plik": PLIK_ZRODLA, "linia": 5, "opis_mutanta": "-    return x\n+    return None"}
    assert type(przezyl["linia"]) is int


def test_mutant_drugiej_funkcji_pliku_ma_linie_liczona_od_jej_wlasnego_def(
    monkeypatch, capsys, tmp_path,
):
    # g zaczyna sie w linii 9, `return x` w linii 10 (nie w linii 2 hunku liczonej od kodu funkcji)
    f = FalszywyMutmut(linie_wynikow=wyniki((G1, "survived")), pokazy={G1: pokaz(G1)})

    _, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert linia_przezylego(out) == 10


def test_opis_mutanta_zawiera_linie_usunieta_i_dodana_z_diffu_mutmuta(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(pokazy={F1: pokaz(F1, stare="a = alfa + 1", nowe="a = beta - 1")})

    _, out, _ = uruchom_nakladke(
        monkeypatch, capsys, f, tmp_path, zrodlo=zrodlo_funkcji(f="a = alfa + 1"))

    assert raport_z(out)["przezyle"][0]["opis_mutanta"] == "-    a = alfa + 1\n+    a = beta - 1"


def test_polskie_znaki_w_opisie_mutanta_sa_w_raporcie_wprost_a_nie_jako_sekwencje_ucieczki(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(pokazy={F1: pokaz(F1, stare="return 'zażółć'", nowe="return 'gęślą'")})

    _, out, _ = uruchom_nakladke(
        monkeypatch, capsys, f, tmp_path, zrodlo=zrodlo_funkcji(f="return 'zażółć'"))

    assert "zażółć" in out and "gęślą" in out
    assert "\\u" not in out


# --- kody narzedzia ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kod_narzedzia", "linie_wynikow"),
    [(0, wyniki((F1, "killed"))), (1, wyniki((F1, "survived"))), (3, wyniki((F1, "killed")))],
    ids=["kod_0_bez_przezylych", "kod_1_z_przezylym", "kod_3_bez_przezylych"],
)
def test_kody_zero_jeden_i_trzy_narzedzia_daja_raport_i_kod_zero_nakladki(
    monkeypatch, capsys, tmp_path, kod_narzedzia, linie_wynikow,
):
    # kod 1 (sa przezyle) to normalny wynik, nie awaria: o oblaniu kroku decyduje fail_on_survivors
    f = FalszywyMutmut(kod_zmian=kod_narzedzia, linie_wynikow=linie_wynikow)

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["kod_narzedzia"] == kod_narzedzia


@pytest.mark.parametrize("kod_narzedzia", [2, 4, 127, -9])
def test_kod_dwa_i_kazdy_nieznany_kod_narzedzia_to_kod_dwa_nakladki_bez_raportu(
    monkeypatch, capsys, tmp_path, kod_narzedzia,
):
    f = FalszywyMutmut(kod_zmian=kod_narzedzia, stderr_zmian="blad: brak mutmuta w interpreterze")

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == (
        f"blad: mutanty_zmian.py zakonczyl sie kodem {kod_narzedzia}:\n"
        "blad: brak mutmuta w interpreterze\n")
    assert f.wolane("results") == [] and f.wolane("show") == []


def test_komunikat_o_kodzie_narzedzia_niesie_tylko_dziesiec_ostatnich_linii_jego_wyjscia(
    monkeypatch, capsys, tmp_path,
):
    stderr = "\n".join(f"w{i}" for i in range(1, 13))
    f = FalszywyMutmut(kod_zmian=2, stderr_zmian=stderr)

    _, _, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert err == (
        "blad: mutanty_zmian.py zakonczyl sie kodem 2:\n"
        + "\n".join(f"w{i}" for i in range(3, 13)) + "\n")


def test_komunikat_o_kodzie_narzedzia_laczy_stdout_i_stderr_bez_pustych_brzegow(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(kod_zmian=2, stdout_zmian="\n\nz-stdout\n", stderr_zmian="z-stderr\n\n")

    _, _, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert err == "blad: mutanty_zmian.py zakonczyl sie kodem 2:\nz-stdout\n\nz-stderr\n"


def test_blad_mutmut_results_to_kod_dwa_nakladki_bez_raportu(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(kod_wynikow=1, linie_wynikow="")

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == "blad: mutmut results zakonczyl sie bledem:\nresults: blad\n"
    assert f.wolane("show") == []


def test_blad_mutmut_show_to_kod_dwa_nakladki_bez_raportu(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(kod_show=1, pokazy={F1: ""})

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == f"blad: mutmut show {F1} zakonczyl sie bledem:\nshow: blad\n"


def test_nieznana_baza_z_wzorcow_zmian_to_kod_dwa_nakladki_z_komunikatem_bez_raportu(
    monkeypatch, capsys, tmp_path,
):
    from mutanty_zmian import BladNarzedzia

    def wzorce(baza, korzen):
        raise BladNarzedzia(f"nieznana baza {baza!r}: fatal: brak")

    f = FalszywyMutmut()

    kod, out, err = uruchom_nakladke(
        monkeypatch, capsys, f, tmp_path, wzorce=wzorce, args=["--baza", "zla"])

    assert kod == 2
    assert out == ""
    assert err == "blad: nieznana baza 'zla': fatal: brak\n"
    assert f.wolane("results") == [] and f.wolane("show") == []


# --- niesparsowany hunk: brak dowodu to blad ---------------------------------------------------

BRAK_PLIKU = "w wyjsciu mutmut show nie ma naglowka +++ z plikiem"
BRAK_HUNKA = "w wyjsciu mutmut show nie ma hunka diffu"
ZLE_POKAZY = {
    "pusty_pokaz": ("", BRAK_PLIKU),
    "sam_naglowek_bez_diffu": ("# {k}: survived\n", BRAK_PLIKU),
    "hunk_bez_naglowka_pliku": ("# {k}: survived\n@@ -1,2 +1,2 @@\n def f(x):\n-    a\n+    b\n", BRAK_PLIKU),
    "naglowek_pliku_bez_sciezki": (
        "# {k}: survived\n--- m.py\n+++ \n@@ -1,2 +1,2 @@\n def f(x):\n-    a\n+    b\n", BRAK_PLIKU),
    "diff_bez_naglowka_hunka": ("# {k}: survived\n--- m.py\n+++ m.py\n def f(x):\n-    a\n+    b\n", BRAK_HUNKA),
    "naglowek_hunka_bez_numeru": (
        "# {k}: survived\n--- m.py\n+++ m.py\n@@ -x +y @@\n def f(x):\n-    a\n+    b\n",
        "niesparsowany naglowek hunka: '@@ -x +y @@'"),
    "naglowek_hunka_bez_nowej_strony": (
        "# {k}: survived\n--- m.py\n+++ m.py\n@@ -1,3 @@\n def f(x):\n-    a\n+    b\n",
        "niesparsowany naglowek hunka: '@@ -1,3 @@'"),
    "hunk_bez_linii_zmienionych": (
        "# {k}: survived\n--- m.py\n+++ m.py\n@@ -1,2 +1,2 @@\n def f(x):\n     return x\n",
        "hunk mutmut show nie ma zmienionych linii"),
    "nieznana_linia_w_hunku": (
        "# {k}: survived\n--- m.py\n+++ m.py\n@@ -1,2 +1,2 @@\n def f(x):\n-    a\n?cos\n+    b\n",
        "nieznana linia hunka: '?cos'"),
}


@pytest.mark.parametrize(
    ("szablon", "komunikat"), list(ZLE_POKAZY.values()), ids=list(ZLE_POKAZY))
def test_niesparsowany_hunk_przezylego_mutanta_to_kod_dwa_nakladki_z_komunikatem(
    monkeypatch, capsys, tmp_path, szablon, komunikat,
):
    f = FalszywyMutmut(pokazy={F1: szablon.format(k=F1)})

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == f"blad: {F1}: {komunikat}\n"


def test_ten_sam_przezyly_mutant_z_poprawnym_hunkiem_daje_raport_a_nie_kod_dwa(
    monkeypatch, capsys, tmp_path,
):
    # blizniak testow niesparsowanego hunka: rozni sie tylko wyjsciem `mutmut show`
    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, FalszywyMutmut(), tmp_path)

    assert kod == 0
    assert len(raport_z(out)["przezyle"]) == 1


# --- ksztalt wyjscia `mutmut show`: naglowki, kontekst, hunki -----------------------------------


def test_prefiks_mutants_w_sciezce_pliku_z_mutmut_show_jest_odciety_w_raporcie(
    monkeypatch, capsys, tmp_path,
):
    tekst = pokaz(F1, plik=f"mutants/{PLIK_ZRODLA}")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, ZRODLO_DOMYSLNE, tekst)

    assert kod == 0
    assert raport_z(out)["przezyle"][0]["plik"] == PLIK_ZRODLA


def test_znaki_biale_po_sciezce_pliku_w_naglowku_plus_nie_psuja_odczytu_zrodla(
    monkeypatch, capsys, tmp_path,
):
    tekst = pokaz(F1).replace(f"+++ {PLIK_ZRODLA}", f"+++ {PLIK_ZRODLA}\t ")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, ZRODLO_DOMYSLNE, tekst)

    assert kod == 0
    assert raport_z(out)["przezyle"][0]["plik"] == PLIK_ZRODLA


@pytest.mark.parametrize("pusta", [" ", ""], ids=["spacja_jak_w_difflib", "calkiem_pusta"])
def test_pusta_linia_kontekstu_w_hunku_nie_jest_bledem_w_obu_postaciach(
    monkeypatch, capsys, tmp_path, pusta,
):
    zrodlo = '"""M."""\n\n\ndef f(x):\n    a = 1\n\n    return x\n'
    hunk = f"@@ -1,4 +1,4 @@\n def f(x):\n-    a = 1\n+    a = 2\n{pusta}\n     return x\n"

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 0
    assert linia_przezylego(out) == 5


def test_marker_braku_konca_linii_w_hunku_jest_pominiety_i_nie_trafia_do_opisu(
    monkeypatch, capsys, tmp_path,
):
    hunk = (
        "@@ -1,3 +1,3 @@\n def f(x):\n-    return x\n\\ No newline at end of file\n"
        "+    return None\n     # koniec\n")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, ZRODLO_DOMYSLNE, szkielet(hunk))

    assert kod == 0
    (przezyl,) = raport_z(out)["przezyle"]
    assert przezyl["linia"] == 5
    assert przezyl["opis_mutanta"] == "-    return x\n+    return None"


def test_naglowek_hunka_bez_licznikow_linii_jest_poprawny(monkeypatch, capsys, tmp_path):
    zrodlo = '"""M."""\n\n\ndef f(x): return x\n'
    hunk = "@@ -1 +1 @@\n-def f(x): return x\n+def f(x): return None\n"

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 0
    assert linia_przezylego(out) == 4


def test_zmiana_pierwszej_linii_funkcji_wskazuje_linie_def(monkeypatch, capsys, tmp_path):
    zrodlo = '"""M."""\n\n\ndef f(x):\n    return x\n'
    hunk = "@@ -1,2 +1,2 @@\n-def f(x):\n+def f(x, y=0):\n     return x\n"

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 0
    assert linia_przezylego(out) == 4


def test_kilka_kolejnych_zmienionych_linii_daje_numer_pierwszej_z_nich(monkeypatch, capsys, tmp_path):
    zrodlo = '"""M."""\n\n\ndef f(x):\n    a = 1\n    b = 2\n    return x\n'
    hunk = (
        "@@ -1,4 +1,4 @@\n def f(x):\n-    a = 1\n-    b = 2\n+    a = 2\n+    b = 3\n"
        "     return x\n")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 0
    (przezyl,) = raport_z(out)["przezyle"]
    assert przezyl["linia"] == 5
    assert przezyl["opis_mutanta"] == "-    a = 1\n-    b = 2\n+    a = 2\n+    b = 3"


def test_mutant_tylko_dodajacy_linie_wskazuje_miejsce_wstawienia(monkeypatch, capsys, tmp_path):
    # wstawiona linia stoi po `a = 1` (linia 5), czyli przed starym `b = 2` z linii 6
    zrodlo = '"""M."""\n\n\ndef f(x):\n    a = 1\n    b = 2\n    return x\n'
    hunk = "@@ -1,4 +1,5 @@\n def f(x):\n     a = 1\n+    mid = 0\n     b = 2\n     return x\n"

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 0
    (przezyl,) = raport_z(out)["przezyle"]
    assert przezyl["linia"] == 6
    assert przezyl["opis_mutanta"] == "+    mid = 0"


def test_zmiana_gleboko_w_funkcji_ma_poczatek_hunka_wiekszy_od_jedynki_i_prawdziwa_linie(
    monkeypatch, capsys, tmp_path,
):
    stary = "def f(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    return x"
    nowy = stary.replace("e = 5", "e = 6")
    tekst = pokaz_roznicy(F1, stary, nowy)
    assert "@@ -3,5 +3,5 @@" in tekst
    zrodlo = '"""M."""\n\n\n' + stary + "\n"

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst)

    assert kod == 0
    assert linia_przezylego(out) == 9


def test_dwa_hunki_jednej_funkcji_daja_linie_pierwszego_a_opis_z_obu(monkeypatch, capsys, tmp_path):
    cialo = [f"    v{i} = {i}" for i in range(1, 12)]
    stary = "\n".join(["def f(x):", *cialo])
    nowy = "\n".join(["def f(x):", "    v1 = 100", *cialo[1:-1], "    v11 = 110"])
    tekst = pokaz_roznicy(F1, stary, nowy)
    assert tekst.count("@@ -") == 2
    zrodlo = '"""M."""\n\n\n' + stary + "\n"

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst)

    assert kod == 0
    (przezyl,) = raport_z(out)["przezyle"]
    assert przezyl["linia"] == 5
    assert przezyl["opis_mutanta"] == "-    v1 = 1\n+    v1 = 100\n-    v11 = 11\n+    v11 = 110"


def test_hunk_bez_zmian_przed_hunkiem_ze_zmiana_nie_przesuwa_numeru_linii(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = '"""M."""\n\n\ndef f(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    return x\n'
    hunk = (
        "@@ -1,2 +1,2 @@\n def f(x):\n     a = 1\n"
        "@@ -4,3 +4,3 @@\n     c = 3\n-    d = 4\n+    d = 5\n     return x\n")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 0
    assert linia_przezylego(out) == 8


# --- ustalanie linii w prawdziwym zrodle: funkcja, metoda, dekorator, komentarz ----------------


def test_metoda_klasy_ma_linie_w_pliku_mimo_wciecia_klasy(monkeypatch, capsys, tmp_path):
    zrodlo = '"""M."""\nclass Klasa:\n    x = 1\n\n    def metoda(self):\n        return 1\n'
    klucz = klucz_metody("Klasa", "metoda")
    tekst = pokaz_roznicy(
        klucz, "def metoda(self):\n    return 1", "def metoda(self):\n    return 2")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst, klucz)

    assert kod == 0
    assert linia_przezylego(out) == 6


def test_metoda_jest_szukana_w_klasie_z_klucza_a_nie_w_innej_klasie_z_metoda_o_tej_samej_nazwie(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = (
        "class A:\n    def dubel(self):\n        return 1\n\n\n"
        "class K:\n    def dubel(self):\n        return 7\n")
    klucz = klucz_metody("K", "dubel")
    tekst = pokaz_roznicy(klucz, "def dubel(self):\n    return 7", "def dubel(self):\n    return 8")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst, klucz)

    assert kod == 0
    assert linia_przezylego(out) == 8


def test_metoda_z_klucza_ma_pierwszenstwo_przed_funkcja_modulu_o_tej_samej_nazwie(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = "def dubel(x):\n    return x\n\n\nclass K:\n    def dubel(self):\n        return 7\n"
    klucz = klucz_metody("K", "dubel")
    tekst = pokaz_roznicy(klucz, "def dubel(self):\n    return 7", "def dubel(self):\n    return 8")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst, klucz)

    assert kod == 0
    assert linia_przezylego(out) == 7


def test_funkcja_modulu_z_klucza_nie_bierze_metody_o_tej_samej_nazwie(monkeypatch, capsys, tmp_path):
    zrodlo = "class K:\n    def dubel(self):\n        return 7\n\n\ndef dubel(x):\n    return x\n"
    klucz = "cockpit.services.modul.x_dubel__mutmut_1"
    tekst = pokaz_roznicy(klucz, "def dubel(x):\n    return x", "def dubel(x):\n    return None")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst, klucz)

    assert kod == 0
    assert linia_przezylego(out) == 7


def test_funkcja_asynchroniczna_ma_linie_w_pliku(monkeypatch, capsys, tmp_path):
    zrodlo = "import asyncio\n\n\nasync def af(x):\n    return x\n"
    klucz = "cockpit.services.modul.x_af__mutmut_1"
    tekst = pokaz_roznicy(klucz, "async def af(x):\n    return x", "async def af(x):\n    return None")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst, klucz)

    assert kod == 0
    assert linia_przezylego(out) == 5


def test_funkcja_z_dekoratorem_ma_hunk_od_linii_dekoratora_a_linia_jest_prawdziwa(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = '"""M."""\n\n@dekorator\ndef f(x):\n    return x\n'
    tekst = pokaz_roznicy(
        F1, "@dekorator\ndef f(x):\n    return x", "@dekorator\ndef f(x):\n    return None")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst)

    assert kod == 0
    assert linia_przezylego(out) == 5


def test_komentarz_nad_funkcja_w_hunku_przesuwa_poczatek_o_linie_komentarza(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = 'x = 1\n\n\n# komentarz\ndef f(x):\n    return x\n'
    tekst = pokaz_roznicy(
        F1, "# komentarz\ndef f(x):\n    return x", "# komentarz\ndef f(x):\n    return None")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst)

    assert kod == 0
    assert linia_przezylego(out) == 6


def test_komentarz_nad_funkcja_nie_obecny_w_hunku_nie_przesuwa_numeru(monkeypatch, capsys, tmp_path):
    zrodlo = 'x = 1\n\n\n# komentarz\ndef f(x):\n    return x\n    # koniec\n'

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, pokaz(F1))

    assert kod == 0
    assert linia_przezylego(out) == 6


def test_dwa_komentarze_rozdzielone_pusta_linia_nad_funkcja_sa_w_hunku_od_pierwszego(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = 'x = 1\n# pierwszy\n\n# drugi\ndef f(x):\n    return x\n'
    stary = "# pierwszy\n\n# drugi\ndef f(x):\n    return x"
    tekst = pokaz_roznicy(F1, stary, stary.replace("return x", "return None"))
    assert "@@ -2,4 +2,4 @@" in tekst

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst)

    assert kod == 0
    assert linia_przezylego(out) == 6


def test_komentarz_w_pierwszej_linii_pliku_nad_funkcja_jest_poczatkiem_hunku(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = '# komentarz\ndef f(x):\n    return x\n'
    tekst = pokaz_roznicy(
        F1, "# komentarz\ndef f(x):\n    return x", "# komentarz\ndef f(x):\n    return None")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst)

    assert kod == 0
    assert linia_przezylego(out) == 3


def test_wciety_komentarz_nad_metoda_jest_poczatkiem_hunku(monkeypatch, capsys, tmp_path):
    zrodlo = 'class K:\n    # komentarz\n    def metoda(self):\n        return 1\n'
    klucz = klucz_metody("K", "metoda")
    stary = "# komentarz\ndef metoda(self):\n    return 1"
    tekst = pokaz_roznicy(klucz, stary, stary.replace("return 1", "return 2"))

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, tekst, klucz)

    assert kod == 0
    assert linia_przezylego(out) == 4


def test_funkcja_konczaca_plik_bez_koncowego_znaku_nowej_linii_ma_linie(monkeypatch, capsys, tmp_path):
    zrodlo = "def f(x):\n    return x"

    kod, out, _ = raport_jednego(
        monkeypatch, capsys, tmp_path, zrodlo,
        pokaz_roznicy(F1, "def f(x):\n    return x", "def f(x):\n    return None"))

    assert kod == 0
    assert linia_przezylego(out) == 2


def test_zrodlo_z_zakonczeniami_linii_crlf_ma_te_same_numery_linii(monkeypatch, capsys, tmp_path):
    zrodlo = ZRODLO_DOMYSLNE.replace("\n", "\r\n").encode("utf-8")

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, pokaz(F1))

    assert kod == 0
    assert linia_przezylego(out) == 5


def test_inne_wciecie_i_biale_znaki_na_koncu_linii_w_zrodle_nie_przeszkadzaja_w_dopasowaniu(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = '"""M."""\n\n\ndef f(x):  \n        return x   \n    # koniec\n'

    kod, out, _ = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, pokaz(F1))

    assert kod == 0
    assert linia_przezylego(out) == 5


# --- ustalanie linii: brak dowodu to blad (kazda galaz osobno) ---------------------------------


def test_brak_pliku_zrodla_to_kod_dwa_nakladki_z_komunikatem(monkeypatch, capsys, tmp_path):
    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, None, pokaz(F1))

    assert kod == 2
    assert out == ""
    assert err.startswith(f"blad: {F1}: nie da sie odczytac {PLIK_ZRODLA}: ")


def test_katalog_w_miejscu_pliku_zrodla_to_kod_dwa_nakladki_z_komunikatem(
    monkeypatch, capsys, tmp_path,
):
    (tmp_path / PLIK_ZRODLA).mkdir(parents=True)

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, None, pokaz(F1))

    assert kod == 2
    assert out == ""
    assert err.startswith(f"blad: {F1}: nie da sie odczytac {PLIK_ZRODLA}: ")


def test_zrodlo_nie_w_utf8_to_kod_dwa_nakladki_z_komunikatem(monkeypatch, capsys, tmp_path):
    zrodlo = b"def f(x):\n    return '\xff'\n    # koniec\n"

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, pokaz(F1))

    assert kod == 2
    assert out == ""
    assert err.startswith(f"blad: {F1}: nie da sie odczytac {PLIK_ZRODLA}: ")


def test_zrodlo_z_samotnym_cr_to_kod_dwa_nakladki_bo_numery_linii_nie_sa_wiarygodne(
    monkeypatch, capsys, tmp_path,
):
    zrodlo = b"def f(x):\r    return x\r    # koniec\r"

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, pokaz(F1))

    assert kod == 2
    assert out == ""
    assert "samotny \\r" in err and PLIK_ZRODLA in err


def test_zrodlo_z_bledem_skladni_to_kod_dwa_nakladki_z_komunikatem(monkeypatch, capsys, tmp_path):
    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, "def f(x:\n", pokaz(F1))

    assert kod == 2
    assert out == ""
    assert err.startswith(f"blad: {F1}: {PLIK_ZRODLA}: nie da sie sparsowac: ")


def test_zrodlo_odrzucone_przez_parser_z_innego_powodu_niz_skladnia_to_kod_dwa_nakladki_z_komunikatem(
    monkeypatch, capsys, tmp_path,
):
    # starsze Pythony zglaszaja przy bajcie zerowym w zrodle ValueError, nowsze SyntaxError
    import ast

    import raport_mutantow

    prawdziwy = ast.parse

    def parse(zrodlo, filename="<unknown>", *args, **kwargs):
        if filename == PLIK_ZRODLA:
            raise ValueError("bajt zerowy w zrodle")
        return prawdziwy(zrodlo, filename, *args, **kwargs)

    monkeypatch.setattr(raport_mutantow.ast, "parse", parse)

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, ZRODLO_DOMYSLNE, pokaz(F1))

    assert kod == 2
    assert out == ""
    assert err == f"blad: {F1}: {PLIK_ZRODLA}: nie da sie sparsowac: bajt zerowy w zrodle\n"


@pytest.mark.parametrize(
    ("zrodlo", "klucz", "nazwa"),
    [
        ("def inna(x):\n    return x\n", F1, "f"),
        ("def f(x):\n    return x\n", klucz_metody("K", "f"), "f"),
        ("class K:\n    def inna(self):\n        return 1\n", klucz_metody("K", "f"), "f"),
        ("class A:\n    def f(self):\n        return 1\n", klucz_metody("K", "f"), "f"),
        ("class K:\n    def f(self):\n        return 1\n", F1, "f"),
        ("def zewnetrzna(x):\n    def f(y):\n        return y\n", F1, "f"),
        ("if True:\n    def f(x):\n        return x\n", F1, "f"),
    ],
    ids=["inna_nazwa", "metoda_bez_klasy", "klasa_bez_metody", "inna_klasa", "funkcja_tylko_jako_metoda",
         "funkcja_zagniezdzona", "funkcja_w_bloku_if"],
)
def test_funkcji_z_klucza_mutanta_nie_ma_w_zrodle_to_kod_dwa_nakladki_z_komunikatem(
    monkeypatch, capsys, tmp_path, zrodlo, klucz, nazwa,
):
    kod, out, err = raport_jednego(
        monkeypatch, capsys, tmp_path, zrodlo, pokaz_roznicy(klucz, "def f(x):\n    return x", "def f(x):\n    return 1"), klucz)

    assert kod == 2
    assert out == ""
    assert err == f"blad: {klucz}: w {PLIK_ZRODLA} nie ma funkcji {nazwa}\n"


@pytest.mark.parametrize(
    "nazwa_w_kluczu",
    ["x", "x_","x\u01c1K\u01c1", "x\u01c1\u01c1m", "x\u01c1K\u01c1m\u01c1z",
     "x\u01c1K", "inna_nazwa"],
    ids=["sam_x", "x_bez_nazwy", "metoda_bez_nazwy", "klasa_bez_nazwy", "za_duzo_czesci", "klasa_bez_metody",
         "bez_prefiksu_x"],
)
def test_klucz_mutanta_o_nieznanej_budowie_to_kod_dwa_nakladki_z_komunikatem(
    monkeypatch, capsys, tmp_path, nazwa_w_kluczu,
):
    klucz = f"cockpit.services.modul.{nazwa_w_kluczu}__mutmut_1"

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, ZRODLO_DOMYSLNE, pokaz(F1), klucz)

    assert kod == 2
    assert out == ""
    assert err == f"blad: {klucz}: nie umiem odczytac nazwy funkcji z klucza mutanta\n"


def test_hunk_od_linii_zero_to_kod_dwa_nakladki_z_komunikatem(monkeypatch, capsys, tmp_path):
    hunk = "@@ -0,0 +1,2 @@\n+    a = 1\n+    b = 2\n"

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, ZRODLO_DOMYSLNE, szkielet(hunk))

    assert kod == 2
    assert out == ""
    assert err == f"blad: {F1}: poczatek hunka 0 nie wskazuje linii funkcji\n"


def test_hunk_niezgodny_z_tekstem_zrodla_to_kod_dwa_nakladki_a_nie_zgadnieta_linia(
    monkeypatch, capsys, tmp_path,
):
    zmienione_zrodlo = zrodlo_funkcji(f="return y", g="return x")

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, zmienione_zrodlo, pokaz(F1))

    assert kod == 2
    assert out == ""
    assert err == (
        f"blad: {F1}: hunk mutmut show nie zgadza sie z tekstem {PLIK_ZRODLA}; "
        "nie ustalono numeru linii\n")


def test_hunk_dluzszy_niz_funkcja_do_konca_pliku_to_kod_dwa_nakladki(monkeypatch, capsys, tmp_path):
    hunk = "@@ -1,4 +1,4 @@\n def f(x):\n-    return x\n+    return None\n     # koniec\n     # jeszcze\n"

    kod, out, err = raport_jednego(
        monkeypatch, capsys, tmp_path, "def f(x):\n    return x\n    # koniec", szkielet(hunk))

    assert kod == 2
    assert out == ""
    assert err == (
        f"blad: {F1}: hunk mutmut show nie zgadza sie z tekstem {PLIK_ZRODLA}; "
        "nie ustalono numeru linii\n")


def test_hunk_wymagajacy_tekstu_sprzed_poczatku_pliku_to_kod_dwa_nakladki_a_nie_zgadnieta_linia(
    monkeypatch, capsys, tmp_path,
):
    # plik: `# k`, `def f(x):`, `return x`, `# koniec` (f od linii 2). Hunk od linii 2 ze stara strona
    # `# k`, `def f(x):`, `return x` pasowalby dopiero do tekstu od linii 0, czyli spoza pliku. Szukanie
    # komentarzy nad funkcja konczy sie na linii 1; poza nia indeks -1 wskazywalby ostatnia linie pliku
    # (`# koniec`), co dalo by falszywe dopasowanie i linie 3. Poprawny wynik to kod 2.
    zrodlo = "# k\ndef f(x):\n    return x\n# koniec\n"
    hunk = "@@ -2,3 +2,3 @@\n # k\n def f(x):\n-    return x\n+    return None\n"

    kod, out, err = raport_jednego(monkeypatch, capsys, tmp_path, zrodlo, szkielet(hunk))

    assert kod == 2
    assert out == ""
    assert err == (
        f"blad: {F1}: hunk mutmut show nie zgadza sie z tekstem {PLIK_ZRODLA}; "
        "nie ustalono numeru linii\n")


# --- liczenie zbadanych i przezylych ------------------------------------------------------------


def test_zbadane_to_mutanty_ze_wzorcow_zmian_o_statusie_innym_niz_not_checked(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(
        linie_wynikow=wyniki(
            (F1, "killed"), (F2, "survived"), (F3, "timeout"),
            (G1, "not checked"),
            (H1, "killed"), (H2, "survived"),     # inna funkcja: poza wzorcami zmian
        ),
        stdout_zmian=f"{F2}\n",                   # narzedzie liczy przezyle tylko ze wzorcow zmian
        pokazy={F2: pokaz(F2)},
    )

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    r = raport_z(out)
    assert r["zbadane"] == 3
    assert len(r["przezyle"]) == 1
    assert [a[-1] for a in f.wolane("show")] == [F2]


def test_mutanty_ze_wzorcow_zmian_ktore_nie_byly_sprawdzone_daja_zero_zbadanych_ale_raport(
    monkeypatch, capsys, tmp_path,
):
    # zero zbadanych to dane dla silnika (evidence_required przy niepustym diffie), nie awaria nakladki
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "not checked"), (G1, "not checked")))

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out) == {
        "zbadane": 0, "przezyle": [], "kod_narzedzia": 0, "poza_zasiegiem": []}


def test_wyniki_mutmuta_bez_zadnego_mutanta_ze_wzorcow_daja_zero_zbadanych(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((H1, "killed"), (H2, "survived")))

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["zbadane"] == 0
    assert f.wolane("show") == []


def test_brak_wzorcow_zmian_nie_wola_mutmut_results_i_daje_pusty_raport(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(kod_zmian=0, kod_wynikow=1)

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path, wzorce=())

    assert kod == 0
    assert raport_z(out) == {
        "zbadane": 0, "przezyle": [], "kod_narzedzia": 0, "poza_zasiegiem": []}
    assert f.wolane("results") == [] and f.wolane("show") == []


def test_linie_wyniku_mutmuta_bez_znacznika_mutanta_nie_licza_sie_jako_zbadane(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(
        kod_zmian=0,
        linie_wynikow="    cockpit.services.modul: ostrzezenie\n" + wyniki((F1, "killed")))

    kod, out, _ = uruchom_nakladke(
        monkeypatch, capsys, f, tmp_path, wzorce=("cockpit.services.modul*",))

    assert kod == 0
    assert raport_z(out)["zbadane"] == 1


@pytest.mark.parametrize(
    ("status", "przezyl"),
    [("survived", True), ("no tests", True), ("killed", False), ("timeout", False),
     ("caught by type check", False), ("segfault", False)],
)
def test_status_rozstrzygniety_to_zbadany_mutant_a_przezyly_tylko_survived_i_no_tests(
    monkeypatch, capsys, tmp_path, status, przezyl,
):
    f = FalszywyMutmut(
        kod_zmian=1 if przezyl else 0,
        linie_wynikow=wyniki((F1, status)), pokazy={F1: pokaz(F1, status)})

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    r = raport_z(out)
    assert r["zbadane"] == 1
    assert len(r["przezyle"]) == (1 if przezyl else 0)
    assert [a[-1] for a in f.wolane("show")] == ([F1] if przezyl else [])


@pytest.mark.parametrize(
    "status", ["not checked", "skipped", "check was interrupted by user", "suspicious"])
def test_status_bez_werdyktu_mutmuta_nie_liczy_sie_jako_zbadany(
    monkeypatch, capsys, tmp_path, status,
):
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, status)))

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out) == {
        "zbadane": 0, "przezyle": [], "kod_narzedzia": 0, "poza_zasiegiem": []}
    assert f.wolane("show") == []


def test_mutant_suspicious_nie_jest_zbadany_a_zabity_obok_niego_tak(monkeypatch, capsys, tmp_path):
    # `suspicious` to nieznany kod wyjscia runnera testow, nie werdykt; bliźniak `killed` jest zbadany
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "killed"), (F2, "suspicious")))

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out) == {
        "zbadane": 1, "przezyle": [], "kod_narzedzia": 0, "poza_zasiegiem": []}
    assert f.wolane("show") == []


def test_status_mutmuta_spoza_znanych_to_kod_dwa_nakladki_a_nie_zgadniete_zbadanie(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "killed"), (F2, "nowy status")))

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == f"blad: {F2}: nieznany status mutmuta 'nowy status'; nie ustalono, czy zbadany\n"


def test_status_spoza_znanych_mutanta_poza_wzorcami_zmian_nie_jest_bledem(
    monkeypatch, capsys, tmp_path,
):
    # bliźniak: ten sam nieznany status, ale mutant innej funkcji niz zmienione
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "killed"), (H1, "nowy status")))

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["zbadane"] == 1


# --- zgodnosc przezylych ze stdout narzedzia i z `mutmut results` --------------------------------

ROZNICA = (
    "blad: przezyle mutanty z mutanty_zmian.py i z mutmut results sie roznia (stary cache mutants/?): "
    "tylko w narzedziu {narzedzie}, tylko w wynikach {wyniki}\n")
F4, F5 = (f"cockpit.services.modul.x_f__mutmut_{i}" for i in (4, 5))


def test_przezyly_widoczny_tylko_w_wynikach_mutmuta_to_kod_dwa_nakladki(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(
        stdout_zmian=f"{F1}\n", linie_wynikow=wyniki((F1, "survived"), (F2, "survived")),
        pokazy={F1: pokaz(F1), F2: pokaz(F2)})

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == ROZNICA.format(narzedzie=[], wyniki=[F2])
    assert f.wolane("show") == []


def test_przezyly_widoczny_tylko_na_stdout_narzedzia_to_kod_dwa_nakladki(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(
        stdout_zmian=f"{F1}\n{F2}\n", linie_wynikow=wyniki((F1, "survived"), (F2, "killed")),
        pokazy={F1: pokaz(F1)})

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == ROZNICA.format(narzedzie=[F2], wyniki=[])


def test_przy_kodzie_zero_przezyly_w_wynikach_mutmuta_to_kod_dwa_nakladki(monkeypatch, capsys, tmp_path):
    # kod 0 mowi "brak przezylych", wiec survivor z `mutmut results` to stary cache, nie wynik
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "survived")), pokazy={F1: pokaz(F1)})

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == ROZNICA.format(narzedzie=[], wyniki=[F1])


def test_przy_kodzie_trzy_przezyly_w_wynikach_mutmuta_to_kod_dwa_nakladki(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(kod_zmian=3, linie_wynikow=wyniki((F1, "no tests")), pokazy={F1: pokaz(F1)})

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == ROZNICA.format(narzedzie=[], wyniki=[F1])


def test_komunikat_o_roznicy_przezylych_podaje_najwyzej_trzy_posortowane_klucze_z_wynikow(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(
        stdout_zmian=f"{F1}\n",
        linie_wynikow=wyniki((F5, "survived"), (F4, "survived"), (F3, "survived"), (F2, "survived"), (F1, "survived")))

    _, _, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert err == ROZNICA.format(narzedzie=[], wyniki=[F2, F3, F4])


def test_komunikat_o_roznicy_przezylych_podaje_najwyzej_trzy_posortowane_klucze_z_narzedzia(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(
        stdout_zmian=f"{F5}\n{F4}\n{F3}\n{F2}\n{F1}\n", linie_wynikow=wyniki((F1, "survived")),
        pokazy={F1: pokaz(F1)})

    _, _, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert err == ROZNICA.format(narzedzie=[F2, F3, F4], wyniki=[])


def test_kod_jeden_bez_kluczy_przezylych_na_stdout_narzedzia_to_kod_dwa_nakladki(
    monkeypatch, capsys, tmp_path,
):
    # w wynikach mutmuta jest przezyly, ale narzedzie nie wskazalo zadnego klucza
    f = FalszywyMutmut(stdout_zmian="znaleziono przezyle mutanty\n")

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == "blad: mutanty_zmian.py zakonczyl sie kodem 1 bez kluczy przezylych mutantow na stdout\n"
    assert f.wolane("show") == []


def test_kod_jeden_z_pustym_stdout_narzedzia_to_kod_dwa_nakladki(monkeypatch, capsys, tmp_path):
    f = FalszywyMutmut(stdout_zmian="")

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == "blad: mutanty_zmian.py zakonczyl sie kodem 1 bez kluczy przezylych mutantow na stdout\n"


def test_stdout_narzedzia_z_innymi_liniami_i_wcieciami_nie_przeszkadza_w_zgodnosci_kluczy(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(stdout_zmian=f"ostrzezenie bez klucza\n   {F1}  \n\n")

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert len(raport_z(out)["przezyle"]) == 1


# --- kod 3: zmiany poza zasiegiem mutmuta trafiaja do raportu ----------------------------------


def test_kod_trzy_przenosi_linie_ze_stdout_narzedzia_do_listy_poza_zasiegiem_w_kolejnosci(
    monkeypatch, capsys, tmp_path,
):
    stdout = (
        "bez mutantów — sprawdź ręcznie\nsrc/runner/inny.py:12\nsrc/cockpit/services/modul.py:5\n")
    f = FalszywyMutmut(
        kod_zmian=3, stdout_zmian=stdout, linie_wynikow=wyniki((F1, "killed")),
        poza=[{"plik": "src/runner/inny.py", "linia": 12}, {"plik": PLIK_ZRODLA, "linia": 5}])

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    r = raport_z(out)
    assert r["kod_narzedzia"] == 3
    assert r["poza_zasiegiem"] == [
        {"plik": "src/runner/inny.py", "linia": 12}, {"plik": PLIK_ZRODLA, "linia": 5}]
    assert r["zbadane"] == 1 and r["przezyle"] == []


def test_kod_trzy_bez_wzorcow_zmian_ma_liste_poza_zasiegiem_i_zero_zbadanych(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(kod_zmian=3)

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path, wzorce=())

    assert kod == 0
    assert raport_z(out) == {
        "zbadane": 0, "przezyle": [], "kod_narzedzia": 3,
        "poza_zasiegiem": [{"plik": PLIK_ZRODLA, "linia": 5}]}
    assert f.wolane("results") == []


def test_kod_trzy_z_pustymi_liniami_i_wcieciami_na_stdout_narzedzia_jest_odczytany(
    monkeypatch, capsys, tmp_path,
):
    stdout = "\n  bez mutantów — sprawdź ręcznie  \n\n   a:b.py:1  \n"
    f = FalszywyMutmut(
        kod_zmian=3, stdout_zmian=stdout, linie_wynikow="", poza=[{"plik": "a:b.py", "linia": 1}])

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["poza_zasiegiem"] == [{"plik": "a:b.py", "linia": 1}]


@pytest.mark.parametrize("kod_narzedzia", [0, 1])
def test_stdout_narzedzia_przy_kodach_zero_i_jeden_nie_jest_czytany_jako_lista_poza_zasiegiem(
    monkeypatch, capsys, tmp_path, kod_narzedzia,
):
    # lista pochodzi z wyliczenia nakladki; linia `plik:linia` na stdout narzedzia jest ignorowana
    f = FalszywyMutmut(
        kod_zmian=kod_narzedzia, stdout_zmian=f"{F1}\nsrc/a.py:3\n" if kod_narzedzia else "src/a.py:3\n",
        linie_wynikow=wyniki((F1, "survived")) if kod_narzedzia else wyniki((F1, "killed")))

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["poza_zasiegiem"] == []


def test_kod_jeden_przenosi_wyliczona_liste_poza_zasiegiem_obok_przezylych(
    monkeypatch, capsys, tmp_path,
):
    # przy kodzie 1 narzedzie wypisuje te liste tylko na stderr, wiec nakladka wyznacza ja sama
    poza = [{"plik": "src/runner/inny.py", "linia": 12}, {"plik": PLIK_ZRODLA, "linia": 5}]
    f = FalszywyMutmut(kod_zmian=1, poza=poza)

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    r = raport_z(out)
    assert r["kod_narzedzia"] == 1
    assert r["poza_zasiegiem"] == poza
    assert len(r["przezyle"]) == 1


def test_kod_zero_z_niepusta_lista_poza_zasiegiem_to_kod_dwa_nakladki_bo_narzedzie_dalo_by_trzy(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(
        kod_zmian=0, linie_wynikow=wyniki((F1, "killed")), poza=[{"plik": PLIK_ZRODLA, "linia": 5}])

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == (
        "blad: mutanty_zmian.py zakonczyl sie kodem 0, a zmiany poza zasiegiem istnieja "
        "(oczekiwano 3)\n")


def test_kod_zero_z_pusta_lista_poza_zasiegiem_daje_raport_a_nie_kod_dwa(monkeypatch, capsys, tmp_path):
    # bliźniak poprzedniego: rozni sie tylko wyliczona lista
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "killed")), poza=[])

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["poza_zasiegiem"] == []


NAGLOWEK_POZA = "bez mutantów — sprawdź ręcznie\n"
ROZNICA_POZA = "blad: lista poza zasiegiem z mutanty_zmian.py i z nakladki sie roznia\n"
POZA_5 = {"plik": PLIK_ZRODLA, "linia": 5}
POZA_9 = {"plik": PLIK_ZRODLA, "linia": 9}


@pytest.mark.parametrize(
    ("stdout", "wyliczona"),
    [
        (NAGLOWEK_POZA + f"{PLIK_ZRODLA}:5\n", [POZA_9]),
        (NAGLOWEK_POZA + f"{PLIK_ZRODLA}:5\n", [POZA_5, POZA_9]),
        (NAGLOWEK_POZA + f"{PLIK_ZRODLA}:5\n{PLIK_ZRODLA}:9\n", [POZA_5]),
        (NAGLOWEK_POZA + f"{PLIK_ZRODLA}:5\n{PLIK_ZRODLA}:9\n", [POZA_9, POZA_5]),
        (NAGLOWEK_POZA + f"{PLIK_ZRODLA}:5\n", []),
    ],
    ids=["inna_linia", "wyliczona_ma_nadmiar", "stdout_ma_nadmiar", "inna_kolejnosc", "wyliczona_pusta"],
)
def test_kod_trzy_z_lista_poza_zasiegiem_rozna_od_wyliczonej_to_kod_dwa_nakladki(
    monkeypatch, capsys, tmp_path, stdout, wyliczona,
):
    f = FalszywyMutmut(
        kod_zmian=3, stdout_zmian=stdout, linie_wynikow=wyniki((F1, "killed")), poza=wyliczona)

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == ROZNICA_POZA


def test_kod_trzy_z_ta_sama_lista_na_stdout_i_wyliczona_daje_raport_a_nie_kod_dwa(
    monkeypatch, capsys, tmp_path,
):
    # bliźniak testu rozbieznosci: te same dwa wpisy w tej samej kolejnosci
    stdout = NAGLOWEK_POZA + f"{PLIK_ZRODLA}:5\n{PLIK_ZRODLA}:9\n"
    f = FalszywyMutmut(
        kod_zmian=3, stdout_zmian=stdout, linie_wynikow=wyniki((F1, "killed")), poza=[POZA_5, POZA_9])

    kod, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 0
    assert raport_z(out)["poza_zasiegiem"] == [POZA_5, POZA_9]


def test_lista_poza_zasiegiem_jest_wyliczana_dla_bazy_z_wiersza_polecen_i_biezacego_korzenia(
    monkeypatch, capsys, tmp_path,
):
    zapytania = []

    def poza(baza, korzen):
        zapytania.append((baza, korzen))
        return []

    f = FalszywyMutmut(kod_zmian=0, linie_wynikow="", poza=poza)

    kod, _, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path, args=["--baza", "origin/main"])

    assert kod == 0
    assert zapytania == [("origin/main", tmp_path.resolve())]


def test_blad_wyliczenia_listy_poza_zasiegiem_to_kod_dwa_nakladki_bez_raportu(
    monkeypatch, capsys, tmp_path,
):
    from mutanty_zmian import BladNarzedzia

    def poza(baza, korzen):
        raise BladNarzedzia("src/a.py: nie da sie odczytac: brak praw")

    f = FalszywyMutmut(kod_zmian=0, linie_wynikow="", poza=poza)

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == "blad: src/a.py: nie da sie odczytac: brak praw\n"


BEZ_LISTY_POZA = "blad: mutanty_zmian.py zakonczyl sie kodem 3 bez listy zmian poza zasiegiem na stdout\n"
ZLE_STDOUT_KODU_TRZY = {
    "pusty_stdout": ("", BEZ_LISTY_POZA),
    "same_biale_znaki": ("  \n\n", BEZ_LISTY_POZA),
    "sam_naglowek": (NAGLOWEK_POZA, BEZ_LISTY_POZA),
    "inny_naglowek": ("zmiany poza zasiegiem\nsrc/a.py:3\n", BEZ_LISTY_POZA),
    "naglowek_nie_na_poczatku": ("src/a.py:3\n" + NAGLOWEK_POZA, BEZ_LISTY_POZA),
    "wpis_bez_pliku": (NAGLOWEK_POZA + ":5\n", "blad: niesparsowany wpis poza zasiegiem: ':5'\n"),
    "wpis_bez_dwukropka": (
        NAGLOWEK_POZA + "src/a.py\n", "blad: niesparsowany wpis poza zasiegiem: 'src/a.py'\n"),
    "wpis_bez_numeru": (
        NAGLOWEK_POZA + "src/a.py:\n", "blad: niesparsowany wpis poza zasiegiem: 'src/a.py:'\n"),
    "numer_nie_jest_liczba": (
        NAGLOWEK_POZA + "src/a.py:x\n", "blad: niesparsowany wpis poza zasiegiem: 'src/a.py:x'\n"),
    "numer_zero": (
        NAGLOWEK_POZA + "src/a.py:0\n", "blad: niesparsowany wpis poza zasiegiem: 'src/a.py:0'\n"),
    "numer_ujemny": (
        NAGLOWEK_POZA + "src/a.py:-1\n", "blad: niesparsowany wpis poza zasiegiem: 'src/a.py:-1'\n"),
    "cyfra_spoza_ascii": (
        NAGLOWEK_POZA + "src/a.py:٣\n",
        "blad: niesparsowany wpis poza zasiegiem: 'src/a.py:٣'\n"),
    "numer_z_dopiskiem": (
        NAGLOWEK_POZA + "src/a.py:5x\n", "blad: niesparsowany wpis poza zasiegiem: 'src/a.py:5x'\n"),
    "drugi_wpis_zly": (
        NAGLOWEK_POZA + "src/a.py:3\nsrc/b.py:0\n",
        "blad: niesparsowany wpis poza zasiegiem: 'src/b.py:0'\n"),
}


@pytest.mark.parametrize(
    ("stdout", "komunikat"), list(ZLE_STDOUT_KODU_TRZY.values()), ids=list(ZLE_STDOUT_KODU_TRZY))
def test_kod_trzy_ze_zlym_stdout_narzedzia_to_kod_dwa_nakladki_a_nie_raport_bez_listy(
    monkeypatch, capsys, tmp_path, stdout, komunikat,
):
    f = FalszywyMutmut(kod_zmian=3, stdout_zmian=stdout, linie_wynikow=wyniki((F1, "killed")))

    kod, out, err = uruchom_nakladke(monkeypatch, capsys, f, tmp_path)

    assert kod == 2
    assert out == ""
    assert err == komunikat


def test_wzorzec_funkcji_nie_lapie_mutantow_funkcji_o_dluzszej_nazwie(monkeypatch, capsys, tmp_path):
    dluzsza = "cockpit.services.modul.x_ff__mutmut_1"
    f = FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "killed"), (dluzsza, "survived")))

    _, out, _ = uruchom_nakladke(monkeypatch, capsys, f, tmp_path, wzorce=(WZORZEC_F,))

    r = raport_z(out)
    assert r["zbadane"] == 1 and r["przezyle"] == []


def test_kazdy_przezyly_mutant_ma_wlasny_wpis_wlasna_linie_i_wlasne_wywolanie_show(
    monkeypatch, capsys, tmp_path,
):
    f = FalszywyMutmut(
        linie_wynikow=wyniki((K1, "survived"), (G1, "survived"), (F1, "survived")),
        pokazy={
            F1: pokaz(F1, stare="alfa", nowe="beta"),
            K1: pokaz(K1, stare="gamma", nowe="delta"),
            G1: pokaz(G1, stare="epsilon", nowe="zeta"),
        },
    )

    _, out, _ = uruchom_nakladke(
        monkeypatch, capsys, f, tmp_path, wzorce=(WZORZEC_F, WZORZEC_G, WZORZEC_K),
        zrodlo=zrodlo_funkcji(f="alfa", g="epsilon", k="gamma"))

    r = raport_z(out)
    assert r["zbadane"] == 3
    # kolejnosc raportu to kolejnosc kluczy, nie kolejnosc linii wynikow mutmuta
    assert [a[-1] for a in f.wolane("show")] == [F1, G1, K1]
    assert [(p["linia"], p["opis_mutanta"]) for p in r["przezyle"]] == [
        (5, "-    alfa\n+    beta"), (10, "-    epsilon\n+    zeta"), (15, "-    gamma\n+    delta")]


# --- uruchom: jedyne miejsce procesow (prawdziwa funkcja) ---------------------------------------


def test_uruchom_nieistniejacy_program_to_blad_narzedzia_z_nazwa_programu():
    import raport_mutantow
    from mutanty_zmian import BladNarzedzia

    program = "/nie/ma/takiego/programu-har168"

    with pytest.raises(BladNarzedzia) as wyjatek:
        raport_mutantow.uruchom([program, "--cos"])

    assert str(wyjatek.value).startswith(f"nie da sie uruchomic {program}: ")


def test_uruchom_zwraca_kod_i_wyjscie_a_bajty_spoza_utf8_zamienia_na_znak_zastepczy():
    import raport_mutantow

    kod = (
        "import sys; sys.stdout.buffer.write(b'ok\\xff'); sys.stdout.flush(); "
        "sys.stderr.buffer.write(b'e\\xfe'); sys.exit(3)")

    wynik = raport_mutantow.uruchom([sys.executable, "-c", kod])

    assert (wynik.returncode, wynik.stdout, wynik.stderr) == (3, "ok\ufffd", "e\ufffd")


# --- wzorce_zmian: git podmieniony na szwie mutanty_zmian.uruchom ------------------------------

DIFF_MODULU = (
    "diff --git a/src/cockpit/services/modul.py b/src/cockpit/services/modul.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/cockpit/services/modul.py\n"
    "+++ b/src/cockpit/services/modul.py\n"
    "@@ -5 +5 @@\n"
    "-    return y\n"
    "+    return x\n"
)
NOWY_MODUL = "def h(x):\n    return x\n"
WZORZEC_NOWY = "cockpit.services.nowy.x_h__mutmut_*"


def falszywy_git(monkeypatch, *, merge=(0, "abc123\n", ""), roznica=(0, DIFF_MODULU, ""),
                 nowe=(0, "", ""), nie_utf8: str | None = None) -> list[list[str]]:
    """Git podmieniony na szwie `mutanty_zmian.uruchom`; `nie_utf8` = podpolecenie, ktorego wyjscia
    nie da sie zdekodowac jako UTF-8 (`UnicodeDecodeError`, jak ze scislego dekodowania strumienia)."""
    import mutanty_zmian

    wolane: list[list[str]] = []
    odpowiedzi = {"merge-base": merge, "diff": roznica, "ls-files": nowe}

    def uruchom(argv, na_linie=None):
        wolane.append(list(argv))
        if argv[1] == nie_utf8:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        kod, stdout, stderr = odpowiedzi[argv[1]]
        return proces(argv, kod, stdout, stderr)

    monkeypatch.setattr(mutanty_zmian, "uruchom", uruchom)
    return wolane


def repo_z_zrodlami(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mutmut]\nsource_paths = ["src/"]\n', encoding="utf-8")
    zapisz_zrodlo(tmp_path, ZRODLO_DOMYSLNE)
    zapisz_zrodlo(tmp_path, NOWY_MODUL, "src/cockpit/services/nowy.py")
    return tmp_path


def test_wzorce_zmian_obejmuja_funkcje_ze_zmienionych_linii_i_z_nowych_plikow(monkeypatch, tmp_path):
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    wolane = falszywy_git(monkeypatch, nowe=(0, "src/cockpit/services/nowy.py\n", ""))

    wzorce = raport_mutantow.wzorce_zmian("zla-baza", korzen)

    assert wzorce == [WZORZEC_F, WZORZEC_NOWY]
    merge, roznica, pliki = wolane
    assert merge == ["git", "merge-base", "zla-baza", "HEAD"]
    assert roznica == [
        "git", "diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/",
        "abc123", "--", "src/", "src/"]
    assert pliki == [
        "git", "ls-files", "--others", "--exclude-standard", "--", "src/", "src/"]


def test_wzorce_zmian_dla_samych_nowych_plikow_zawieraja_ich_funkcje(monkeypatch, tmp_path):
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch, roznica=(0, "", ""), nowe=(0, "src/cockpit/services/nowy.py\n", ""))

    assert raport_mutantow.wzorce_zmian("HEAD", korzen) == [WZORZEC_NOWY]


def test_wzorce_zmian_dla_zmiany_poza_zmienionymi_funkcjami_nie_obejmuja_innych_funkcji(
    monkeypatch, tmp_path,
):
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch)

    assert raport_mutantow.wzorce_zmian("HEAD", korzen) == [WZORZEC_F]


def test_nieznana_baza_to_blad_narzedzia_z_komunikatem_gita(monkeypatch, tmp_path):
    import raport_mutantow
    from mutanty_zmian import BladNarzedzia

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch, merge=(128, "", "fatal: Not a valid object name zla\n"))

    with pytest.raises(BladNarzedzia) as wyjatek:
        raport_mutantow.wzorce_zmian("zla", korzen)

    assert str(wyjatek.value) == "nieznana baza 'zla': fatal: Not a valid object name zla"


def test_blad_git_diff_to_blad_narzedzia_z_komunikatem_gita(monkeypatch, tmp_path):
    import raport_mutantow
    from mutanty_zmian import BladNarzedzia

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch, roznica=(128, "", "fatal: diff pad\n"))

    with pytest.raises(BladNarzedzia) as wyjatek:
        raport_mutantow.wzorce_zmian("HEAD", korzen)

    assert str(wyjatek.value) == "git diff nie powiodl sie: fatal: diff pad"


def test_blad_git_ls_files_to_blad_narzedzia_z_komunikatem_gita(monkeypatch, tmp_path):
    import raport_mutantow
    from mutanty_zmian import BladNarzedzia

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch, nowe=(128, "", "fatal: ls pad\n"))

    with pytest.raises(BladNarzedzia) as wyjatek:
        raport_mutantow.wzorce_zmian("HEAD", korzen)

    assert str(wyjatek.value) == "git ls-files nie powiodl sie: fatal: ls pad"


@pytest.mark.parametrize("podpolecenie", ["merge-base", "diff", "ls-files"])
def test_wyjscie_gita_spoza_utf8_to_kod_dwa_nakladki_z_komunikatem_a_nie_traceback(
    monkeypatch, capsys, tmp_path, podpolecenie,
):
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    prawdziwe_wzorce = raport_mutantow.wzorce_zmian
    falszywy_git(monkeypatch, nie_utf8=podpolecenie)

    kod, out, err = uruchom_nakladke(
        monkeypatch, capsys, FalszywyMutmut(kod_zmian=0, linie_wynikow=""), korzen,
        wzorce=prawdziwe_wzorce)

    assert kod == 2
    assert out == ""
    assert err == (
        f"blad: git {podpolecenie}: wyjscie gita nie jest poprawnym UTF-8: "
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte\n")


def test_to_samo_wyjscie_gita_w_utf8_daje_raport_a_nie_kod_dwa(monkeypatch, capsys, tmp_path):
    # bliźniak: rozni sie od testow wyzej tylko tym, ze git nie zwraca bajtow spoza UTF-8
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    prawdziwe_wzorce = raport_mutantow.wzorce_zmian
    falszywy_git(monkeypatch)

    kod, out, _ = uruchom_nakladke(
        monkeypatch, capsys,
        FalszywyMutmut(kod_zmian=0, linie_wynikow=wyniki((F1, "killed"))), korzen,
        wzorce=prawdziwe_wzorce)

    assert kod == 0
    assert raport_z(out)["zbadane"] == 1


# --- lista_poza_zasiegiem: te same zmiany co wzorce, `bez_mutantow` podmienione -------------------


def falszywe_bez_mutantow(monkeypatch, wynik: list[str]) -> list[tuple]:
    import mutanty_zmian

    wolane: list[tuple] = []

    def bez_mutantow(zmiany, source_paths, korzen):
        wolane.append((zmiany, source_paths, korzen))
        return wynik

    monkeypatch.setattr(mutanty_zmian, "bez_mutantow", bez_mutantow)
    return wolane


def test_lista_poza_zasiegiem_liczy_bez_mutantow_na_zmianach_z_diffu_i_nowych_plikow(
    monkeypatch, tmp_path,
):
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch, nowe=(0, "src/cockpit/services/nowy.py\n", ""))
    wolane = falszywe_bez_mutantow(monkeypatch, ["src/a.py:3", "src/b.py:40"])

    wynik = raport_mutantow.lista_poza_zasiegiem("HEAD", korzen)

    assert wynik == [{"plik": "src/a.py", "linia": 3}, {"plik": "src/b.py", "linia": 40}]
    assert wolane == [(
        {PLIK_ZRODLA: {5}, "src/cockpit/services/nowy.py": {1, 2}}, ["src/"], korzen)]


def test_lista_poza_zasiegiem_bez_zmian_poza_zasiegiem_jest_pusta(monkeypatch, tmp_path):
    import raport_mutantow

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch)
    falszywe_bez_mutantow(monkeypatch, [])

    assert raport_mutantow.lista_poza_zasiegiem("HEAD", korzen) == []


def test_lista_poza_zasiegiem_z_niesparsowanym_wpisem_to_blad_narzedzia(monkeypatch, tmp_path):
    import raport_mutantow
    from mutanty_zmian import BladNarzedzia

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch)
    falszywe_bez_mutantow(monkeypatch, ["src/a.py:0"])

    with pytest.raises(BladNarzedzia) as wyjatek:
        raport_mutantow.lista_poza_zasiegiem("HEAD", korzen)

    assert str(wyjatek.value) == "niesparsowany wpis poza zasiegiem: 'src/a.py:0'"


def test_lista_poza_zasiegiem_dla_nieznanej_bazy_to_blad_narzedzia_z_komunikatem_gita(
    monkeypatch, tmp_path,
):
    import raport_mutantow
    from mutanty_zmian import BladNarzedzia

    korzen = repo_z_zrodlami(tmp_path)
    falszywy_git(monkeypatch, merge=(128, "", "fatal: Not a valid object name zla\n"))
    falszywe_bez_mutantow(monkeypatch, [])

    with pytest.raises(BladNarzedzia) as wyjatek:
        raport_mutantow.lista_poza_zasiegiem("zla", korzen)

    assert str(wyjatek.value) == "nieznana baza 'zla': fatal: Not a valid object name zla"

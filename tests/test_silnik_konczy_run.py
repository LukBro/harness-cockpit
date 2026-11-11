"""HAR-242 (G2): wyjatek z KAZDEJ granicy wywolan silnika konczy run `step_failed` + `run_finished`.

Silnik (`wykonaj_run`, `wznow_run` i funkcje, do ktorych dochodzi) wola kod, ktory czyta dysk,
git, YAML i checkpoint. Wyjatek z takiego wywolania nie moze uciec z silnika: bez
`run_finished` kokpit trzyma run w `running` na zawsze (retro M1.8, HAR-173).

Lista granic pochodzi z AST `src/runner/silnik.py` (wzorzec `tools/check_layers.py` i
`tests/test_warstwy.py`: wlasne wezly funkcji, `asyncio.to_thread(f, ...)` liczy sie jako
wywolanie `f`). Stala lista w tym pliku jest niezalezna od kodu: kazde wywolanie z AST ma
w niej wpis w jednej z trzech kategorii (wstrzykniete, czyste, niewstrzykiwalne z
uzasadnieniem), a meta-test oblewa, gdy AST i lista sie rozjada albo AST jest pusty.
Nowe wywolanie w silniku = swiadoma decyzja, nie ciche pominiecie.

Wstrzykniecie: atrapa podmienia granice i rzuca `OSError`, `KeyError`, `ValueError` albo
blad dziedziny. Asercja czyta zdarzenia zapisane przez silnik (to dane, nie odpowiedz).
"""
from __future__ import annotations

import ast
import asyncio
import re
import shutil
from pathlib import Path

import pytest

from cockpit.services.errors import BladDziedziny
from runner import silnik
from runner.agent import ProfilBackendu
from runner.zakres import Zakres
from test_krok_diff import (
    SKILLE_Z_DIFFEM,
    SzpiegZmieniajacyDrzewo,
    WORKFLOW_Z_DIFFEM,
    swiat_z_diffem,
    zmien_drzewo,
)
from test_silnik_runu import RUN_ID, K, Szpieg, jedno, rodzaje, swiat, uruchom

KORZEN = Path(__file__).resolve().parent.parent
LIMIT_S = 60
WEJSCIA = ("wykonaj_run", "wznow_run")

# --- stala lista granic (niezalezna od kodu) ---------------------------------------------------

# scenariusz -> funkcja wejsciowa silnika, ktora przez niego przechodzi
SCENARIUSZ_TRYB = {
    "run": "wykonaj_run",
    "bramka": "wykonaj_run",
    "profil": "wykonaj_run",
    "dom": "wykonaj_run",
    "zakres": "wykonaj_run",
    "kontrola": "wykonaj_run",
    "wznowienie": "wznow_run",
    "odrzucenie": "wznow_run",
    "wznowienie_profil": "wznow_run",
    "wznowienie_dom": "wznow_run",
    "wznowienie_druga_bramka": "wznow_run",
}

# granica -> scenariusze, w ktorych ja wstrzykujemy (kazdy tryb, w ktorym jest osiagalna)
WSTRZYKNIETE = {
    "ProfilBackendu.z_konfiguracji": ("profil", "wznowienie_profil"),
    "Path.home": ("dom", "wznowienie_dom"),
    "Zakres.z_konfiguracji": ("zakres",),
    "zakres.rozwiaz": ("run",),
    "_przygotuj": ("run",),
    "utworz_worktree": ("run",),
    "rozstrzygnij_rejestr": ("run", "wznowienie"),
    "wczytaj_workflow": ("run", "wznowienie"),
    "waliduj_workflow": ("run", "wznowienie"),
    "wczytaj_projekt": ("run", "wznowienie"),
    "shutil.copyfile": ("run",),
    "materializuj": ("run",),
    "_wykonaj_plan": ("run", "wznowienie"),
    "nastepne": ("run", "wznowienie"),
    "_krok": ("run", "wznowienie"),
    "uruchom_agenta": ("run", "wznowienie"),
    "zbuduj_prompt": ("run", "wznowienie"),
    "polecenie_agenta": ("run", "wznowienie"),
    "_krok_systemowy": ("run",),
    "wykonawca": ("run",),
    "wykonaj_diff": ("run",),
    "wykonaj_check": ("kontrola",),
    "opis_kroku": ("run", "wznowienie"),
    "_krok_producenta": ("run", "wznowienie"),
    "_wejscie": ("run", "wznowienie"),
    "_artefakty_kroku": ("run", "wznowienie"),
    "_produkuje_notatke": ("run", "wznowienie"),
    "_pola_pomiaru": ("run", "wznowienie"),
    "kroki_weryfikujace": ("run", "wznowienie"),
    "_checkpoint": ("bramka", "wznowienie"),
    "_zapisz": ("bramka", "wznowienie_druga_bramka"),
    "_pauza_testowa": ("bramka", "wznowienie_druga_bramka"),
    "zapisz_checkpoint": ("bramka", "wznowienie", "odrzucenie"),
    "_przyjmij_decyzje": ("wznowienie",),
    "katalog_runu": ("wznowienie",),
    "wczytaj_checkpoint": ("wznowienie",),
    "_przygotuj_wznowienie": ("wznowienie",),
    "_stany_z_checkpointu": ("wznowienie",),
    "_powod_ignorowania": ("wznowienie",),
    "_zapisz_decyzje": ("wznowienie",),
    "_zapisz_atomowo": ("wznowienie",),
    "worktree.is_dir": ("wznowienie",),
}


def _grupa(powod: str, *klucze: str) -> dict[str, str]:
    return {k: powod for k in klucze}


# granica -> uzasadnienie, dlaczego nie ma wejscia, ktore z niej wyrzuca wyjatek z granicy
CZYSTE = {
    **_grupa(
        "wbudowana funkcja albo metoda kolekcji na danych w pamieci; nie dotyka dysku ani sieci",
        "dict", "float", "frozenset", "int", "isinstance", "list", "next", "print", "set", "str",
        "'\\n'.join", "cp.kroki.items", "plan.do_pominiecia.items", "stany.items",
        "startowane.add", "odwiedzeni.add", "stos.extend", "stos.pop", "w_toku.pop",
        "wejscia.append", "polecenie.get", "tresc.encode", "os.environ.get", "time.monotonic",
        "datetime.now", "datetime.now(timezone.utc).isoformat", "json.dumps", "replace",
        "NAZWA_WORKFLOW.fullmatch",
    ),
    **_grupa(
        "konstruktor albo wyjatek dziedziny z samych pol, bez walidacji i bez I/O",
        "BladStartu", "BladWznowienia", "Checkpoint", "Oczekiwanie", "Path", "PomiarKroku",
        "PorazkaKroku", "Przygotowany", "SpecKroku", "StanKroku", "StanKrokuCheckpointu",
        "WorktreeRunu",
    ),
    **_grupa(
        "czysta funkcja pomocnicza silnika: formatowanie napisu, sciezki albo zdarzenia z danych",
        "_bledy", "_linia", "_sciezka_decyzji", "_sciezka_decyzji(id_kroku).as_posix",
        "_tekst_bledu", "_zdarzenie", "ma_bledy",
    ),
    **_grupa(
        "wyszukanie w slowniku wykonawcow; blad wykonawcy pokrywa wpis `wykonawca`",
        "WYKONAWCY_SYSTEMOWI.get",
    ),
    **_grupa(
        "prymityw petli zdarzen: blad to blad programisty albo wyczerpanie zasobow, nie wejscie",
        "asyncio.Lock", "asyncio.create_task", "asyncio.gather", "asyncio.sleep",
        "asyncio.to_thread", "asyncio.wait", "zadanie.cancel", "zadanie.result",
        "_BLOKADY.setdefault",
    ),
}

_PO_WZNOWIENIU = (
    "po wznowieniu kroki systemowe z checkpointu sa juz zakonczone i nie startuja drugi raz; "
    "ta sama funkcja jest wstrzykiwana w `wykonaj_run`"
)
_OBSLUGA_BLEDU = (
    "sama sciezka obslugi bledu wywolanej granicy: wyjatek z niej nie ma wyzszej granicy, "
    "ktora by go zamienila na zdarzenia"
)
# (granica, funkcja wejsciowa) -> uzasadnienie
NIEWSTRZYKIWALNE = {
    ("emit", "wykonaj_run"): "bledy `emit` (np. BrakMiejsca) opisuje docstring modulu: decyduje o nich wolajacy",
    ("emit", "wznow_run"): "bledy `emit` (np. BrakMiejsca) opisuje docstring modulu: decyduje o nich wolajacy",
    ("_blad_przed_startem", "wykonaj_run"): _OBSLUGA_BLEDU,
    ("_porazka_wznowienia", "wznow_run"): _OBSLUGA_BLEDU,
    ("_krok_systemowy", "wznow_run"): _PO_WZNOWIENIU,
    ("wykonawca", "wznow_run"): _PO_WZNOWIENIU,
    ("wykonaj_diff", "wznow_run"): _PO_WZNOWIENIU,
    ("wykonaj_check", "wznow_run"): _PO_WZNOWIENIU,
}


# --- lista granic z AST -----------------------------------------------------------------------

_ORYGINAL_MUTMUTA = re.compile(r"x_(.+)__mutmut_orig")


def _wlasne_wezly(funkcja: ast.AST):
    """Wezly ciala funkcji bez wchodzenia w funkcje zagniezdzone (maja wlasne nazwy)."""
    kolejka = list(ast.iter_child_nodes(funkcja))
    while kolejka:
        w = kolejka.pop()
        yield w
        if not isinstance(w, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kolejka.extend(ast.iter_child_nodes(w))


def wywolania_z_ast(zrodlo: str, wejscia: tuple[str, ...] = WEJSCIA) -> dict[str, set[str]]:
    """Wywolanie (tekst wyrazenia funkcji) -> funkcje wejsciowe, z ktorych jest osiagalne.

    Osiagalnosc: wywolanie albo odwolanie po nazwie do funkcji modulu (takze przez wartosc
    stalej modulu, np. slownik wykonawcow). `asyncio.to_thread(f, ...)` wola `f`."""
    drzewo = ast.parse(zrodlo)
    funkcje: dict[str, ast.AST] = {}
    for n in drzewo.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # pod mutmutem cialo lezy w `x_<nazwa>__mutmut_orig`, a `<nazwa>` to tylko trampolina
            orygi = _ORYGINAL_MUTMUTA.fullmatch(n.name)
            funkcje[orygi.group(1) if orygi else n.name] = n
    stale: dict[str, ast.AST] = {}
    for n in drzewo.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    stale[t.id] = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value:
            stale[n.target.id] = n.value
    wynik: dict[str, set[str]] = {}
    for wejscie in wejscia:
        if wejscie not in funkcje:
            continue
        osiagniete: set[str] = set()
        kolejka = [wejscie]
        while kolejka:
            nazwa = kolejka.pop()
            if nazwa in osiagniete:
                continue
            osiagniete.add(nazwa)
            for w in _wlasne_wezly(funkcje[nazwa]):
                if isinstance(w, ast.Name):
                    if w.id in funkcje:
                        kolejka.append(w.id)
                    elif w.id in stale:
                        kolejka.extend(
                            x.id for x in ast.walk(stale[w.id])
                            if isinstance(x, ast.Name) and x.id in funkcje)
                if isinstance(w, ast.Call):
                    tekst = ast.unparse(w.func)
                    wynik.setdefault(tekst, set()).add(wejscie)
                    if tekst == "asyncio.to_thread" and w.args:
                        wynik.setdefault(ast.unparse(w.args[0]), set()).add(wejscie)
    return wynik


def rozbieznosci(
    z_ast: dict[str, set[str]], *, czyste=CZYSTE, wstrzykniete=WSTRZYKNIETE,
    niewstrzykiwalne=NIEWSTRZYKIWALNE, tryb_scenariusza=SCENARIUSZ_TRYB,
) -> list[str]:
    """Roznice miedzy lista z AST a lista w tescie; pusta lista = zgodne i niepuste."""
    if not z_ast:
        return ["lista granic z AST jest pusta"]
    wady: list[str] = []
    niewstrz_klucze = {k for k, _ in niewstrzykiwalne}
    wady += [f"granica w dwoch kategoriach: {k}" for k in sorted(set(czyste) & set(wstrzykniete))]
    wady += [f"granica w dwoch kategoriach: {k}" for k in sorted(set(czyste) & niewstrz_klucze)]
    znane = set(czyste) | set(wstrzykniete) | niewstrz_klucze
    wady += [f"granica spoza listy testu: {k}" for k in sorted(set(z_ast) - znane)]
    wady += [f"granica z listy testu nie wystepuje w AST: {k}" for k in sorted(znane - set(z_ast))]
    for klucz in sorted(set(z_ast) & (set(wstrzykniete) | niewstrz_klucze)):
        for tryb in sorted(z_ast[klucz]):
            wstrzykniety = any(tryb_scenariusza[s] == tryb for s in wstrzykniete.get(klucz, ()))
            uzasadniony = (klucz, tryb) in niewstrzykiwalne
            if wstrzykniety and uzasadniony:
                wady.append(
                    f"granica {klucz} w trybie {tryb} ma wstrzykniecie i uzasadnienie niewstrzykiwalnej")
            if not wstrzykniety and not uzasadniony:
                wady.append(f"granica {klucz} w trybie {tryb} bez wstrzykniecia ani uzasadnienia")
    for klucz, tryb in sorted(niewstrzykiwalne):
        if tryb not in z_ast.get(klucz, set()):
            wady.append(f"uzasadnienie niewstrzykiwalnej {klucz} w trybie {tryb} nie pasuje do AST")
    return wady


def _z_ast_silnika() -> dict[str, set[str]]:
    return wywolania_z_ast((KORZEN / "src" / "runner" / "silnik.py").read_text(encoding="utf-8"))


# --- meta-testy: lista z AST i lista w tescie --------------------------------------------------


def test_lista_granic_silnika_z_ast_jest_niepusta_i_obejmuje_oba_wejscia():
    z_ast = _z_ast_silnika()

    assert len(z_ast) >= 1
    assert {t for tryby in z_ast.values() for t in tryby} == {"wykonaj_run", "wznow_run"}


def test_lista_granic_silnika_z_ast_zgadza_sie_z_lista_w_tescie():
    assert rozbieznosci(_z_ast_silnika()) == []


def test_pusta_lista_granic_z_ast_jest_zgloszona_a_nie_uznana_za_zgodna():
    assert rozbieznosci({}) == ["lista granic z AST jest pusta"]
    assert wywolania_z_ast("def inna():\n    f()\n") == {}


def test_nowe_wywolanie_w_silniku_bez_wpisu_w_liscie_jest_zgloszone():
    z_ast = {"nowa_granica": {"wykonaj_run"}, "emit": {"wykonaj_run"}}

    wady = rozbieznosci(
        z_ast, czyste={}, wstrzykniete={}, niewstrzykiwalne={("emit", "wykonaj_run"): "x"},
        tryb_scenariusza={})

    assert wady == ["granica spoza listy testu: nowa_granica"]


def test_wpis_listy_bez_wywolania_w_silniku_jest_zgloszony():
    wady = rozbieznosci(
        {"emit": {"wykonaj_run"}}, czyste={"dawna_granica": "x"}, wstrzykniete={},
        niewstrzykiwalne={("emit", "wykonaj_run"): "x"}, tryb_scenariusza={})

    assert wady == ["granica z listy testu nie wystepuje w AST: dawna_granica"]


def test_granica_w_dwoch_kategoriach_jest_zgloszona():
    wady = rozbieznosci(
        {"f": {"wykonaj_run"}}, czyste={"f": "x"}, wstrzykniete={"f": ("run",)},
        niewstrzykiwalne={}, tryb_scenariusza={"run": "wykonaj_run"})

    assert wady == ["granica w dwoch kategoriach: f"]


def test_granica_wstrzyknieta_i_uzasadniona_jako_niewstrzykiwalna_w_tym_samym_trybie_jest_zgloszona():
    wady = rozbieznosci(
        {"f": {"wykonaj_run"}}, czyste={}, wstrzykniete={"f": ("run",)},
        niewstrzykiwalne={("f", "wykonaj_run"): "x"}, tryb_scenariusza={"run": "wykonaj_run"})

    assert wady == ["granica f w trybie wykonaj_run ma wstrzykniecie i uzasadnienie niewstrzykiwalnej"]


def test_granica_wstrzykiwana_w_jednym_trybie_moze_byc_niewstrzykiwalna_w_drugim():
    assert rozbieznosci(
        {"f": {"wykonaj_run", "wznow_run"}}, czyste={}, wstrzykniete={"f": ("run",)},
        niewstrzykiwalne={("f", "wznow_run"): "x"}, tryb_scenariusza={"run": "wykonaj_run"}) == []


def test_granica_czysta_i_niewstrzykiwalna_naraz_jest_zgloszona_jako_podwojna():
    wady = rozbieznosci(
        {"f": {"wykonaj_run"}}, czyste={"f": "x"}, wstrzykniete={},
        niewstrzykiwalne={("f", "wykonaj_run"): "x"}, tryb_scenariusza={})

    assert wady == ["granica w dwoch kategoriach: f"]


def test_granica_osiagalna_w_trybie_bez_scenariusza_i_bez_uzasadnienia_jest_zgloszona():
    # wstrzykniecie tylko w `wykonaj_run`; w `wznow_run` brak scenariusza i wpisu
    wady = rozbieznosci(
        {"f": {"wykonaj_run", "wznow_run"}}, czyste={}, wstrzykniete={"f": ("run",)},
        niewstrzykiwalne={}, tryb_scenariusza={"run": "wykonaj_run"})

    assert wady == ["granica f w trybie wznow_run bez wstrzykniecia ani uzasadnienia"]


def test_granica_niewstrzykiwalna_tylko_w_jednym_trybie_jest_zgloszona_w_drugim():
    wady = rozbieznosci(
        {"emit": {"wykonaj_run", "wznow_run"}}, czyste={}, wstrzykniete={},
        niewstrzykiwalne={("emit", "wykonaj_run"): "x"}, tryb_scenariusza={})

    assert wady == ["granica emit w trybie wznow_run bez wstrzykniecia ani uzasadnienia"]


def test_uzasadnienie_niewstrzykiwalnej_dla_trybu_spoza_ast_jest_zgloszone():
    wady = rozbieznosci(
        {"emit": {"wykonaj_run"}}, czyste={}, wstrzykniete={},
        niewstrzykiwalne={("emit", "wykonaj_run"): "x", ("emit", "wznow_run"): "y"},
        tryb_scenariusza={})

    assert wady == ["uzasadnienie niewstrzykiwalnej emit w trybie wznow_run nie pasuje do AST"]


def test_granica_czysta_nie_wymaga_scenariusza_w_zadnym_trybie():
    assert rozbieznosci(
        {"f": {"wykonaj_run", "wznow_run"}}, czyste={"f": "x"}, wstrzykniete={},
        niewstrzykiwalne={}, tryb_scenariusza={}) == []


def test_lista_granic_ma_uzasadnienia_i_scenariusze_bez_martwych_wpisow():
    assert {s for v in WSTRZYKNIETE.values() for s in v} == set(SCENARIUSZ_TRYB)
    assert all(CZYSTE.values()) and all(NIEWSTRZYKIWALNE.values())


# --- skaner AST na atrapach: kazdy element osobno ----------------------------------------------


def test_skaner_widzi_wywolanie_w_ciele_funkcji_wejsciowej():
    assert wywolania_z_ast("def wykonaj_run():\n    f()\n") == {"f": {"wykonaj_run"}}


def test_skaner_widzi_wywolanie_w_funkcji_asynchronicznej():
    assert wywolania_z_ast("async def wznow_run():\n    await f()\n") == {"f": {"wznow_run"}}


def test_skaner_wchodzi_do_funkcji_wolanej_przez_wejscie():
    zrodlo = "def wykonaj_run():\n    pomoc()\n\ndef pomoc():\n    g()\n"

    assert wywolania_z_ast(zrodlo) == {"pomoc": {"wykonaj_run"}, "g": {"wykonaj_run"}}


def test_skaner_pomija_funkcje_nieosiagalna_z_wejsc():
    zrodlo = "def wykonaj_run():\n    f()\n\ndef osierocona():\n    g()\n"

    assert wywolania_z_ast(zrodlo) == {"f": {"wykonaj_run"}}


def test_skaner_pomija_ciala_funkcji_zagniezdzonych():
    zrodlo = "def wykonaj_run():\n    def wewn():\n        g()\n    f()\n"

    assert wywolania_z_ast(zrodlo) == {"f": {"wykonaj_run"}}


def test_skaner_liczy_pierwszy_argument_to_thread_jako_wywolanie():
    zrodlo = (
        "async def wykonaj_run():\n    await asyncio.to_thread(czytaj, 1)\n\n"
        "def czytaj(x):\n    g()\n")

    assert wywolania_z_ast(zrodlo) == {
        "asyncio.to_thread": {"wykonaj_run"}, "czytaj": {"wykonaj_run"}, "g": {"wykonaj_run"}}


def test_skaner_nie_liczy_drugiego_argumentu_to_thread_jako_wywolania():
    zrodlo = "async def wykonaj_run():\n    await asyncio.to_thread(a, b)\n"

    assert wywolania_z_ast(zrodlo) == {"asyncio.to_thread": {"wykonaj_run"}, "a": {"wykonaj_run"}}


def test_skaner_to_thread_bez_argumentow_nie_rzuca_wyjatku():
    assert wywolania_z_ast("def wykonaj_run():\n    asyncio.to_thread()\n") == {
        "asyncio.to_thread": {"wykonaj_run"}}


def test_skaner_idzie_za_funkcja_podana_jako_wartosc_stalej_modulu():
    zrodlo = (
        "WYKONAWCY = {'a': wykonawca_a}\n\n"
        "def wykonawca_a():\n    g()\n\n"
        "def wykonaj_run():\n    WYKONAWCY.get('a')\n")

    assert wywolania_z_ast(zrodlo) == {"WYKONAWCY.get": {"wykonaj_run"}, "g": {"wykonaj_run"}}


def test_skaner_idzie_za_funkcja_ze_stalej_z_adnotacja_typu():
    zrodlo = (
        "WYKONAWCY: dict = {'a': wykonawca_a}\n\n"
        "def wykonawca_a():\n    g()\n\n"
        "def wykonaj_run():\n    WYKONAWCY.get('a')\n")

    assert wywolania_z_ast(zrodlo) == {"WYKONAWCY.get": {"wykonaj_run"}, "g": {"wykonaj_run"}}


def test_skaner_nie_idzie_za_stala_modulu_bez_wartosci():
    zrodlo = "WYKONAWCY: dict\n\ndef wykonaj_run():\n    WYKONAWCY.get('a')\n"

    assert wywolania_z_ast(zrodlo) == {"WYKONAWCY.get": {"wykonaj_run"}}


def test_skaner_rozroznia_tryb_wywolania_dla_kazdej_funkcji_wejsciowej():
    zrodlo = (
        "def wykonaj_run():\n    tylko_start()\n    wspolne()\n\n"
        "def wznow_run():\n    tylko_wznowienie()\n    wspolne()\n")

    assert wywolania_z_ast(zrodlo) == {
        "tylko_start": {"wykonaj_run"}, "tylko_wznowienie": {"wznow_run"},
        "wspolne": {"wykonaj_run", "wznow_run"}}


def test_skaner_czyta_cialo_oryginalu_spod_trampoliny_mutmuta():
    zrodlo = (
        "def wykonaj_run(*a):\n    return _mutmut_trampoline(x_wykonaj_run__mutmut_orig, a)\n\n"
        "def x_wykonaj_run__mutmut_orig():\n    f()\n")

    wynik = wywolania_z_ast(zrodlo)

    assert wynik["f"] == {"wykonaj_run"}


def test_skaner_pomija_wejscie_ktorego_nie_ma_w_zrodle():
    assert wywolania_z_ast("def wykonaj_run():\n    f()\n", ("wznow_run",)) == {}


# --- wstrzykiwanie wyjatkow ---------------------------------------------------------------------


class BladTestowy(BladDziedziny):
    """Blad dziedziny wstrzykiwany w granice (podklasa `BladDziedziny`, jak `BladCheckpointu`)."""


WYJATKI = [
    pytest.param(OSError, id="blad_dysku"),
    pytest.param(KeyError, id="brak_klucza"),
    pytest.param(ValueError, id="zla_wartosc"),
    pytest.param(BladTestowy, id="blad_dziedziny"),
]

ASYNCHRONICZNE = {
    "_krok", "_krok_systemowy", "wykonaj_diff", "wykonaj_check", "_zapisz", "_pauza_testowa",
    "_wykonaj_plan", "_przyjmij_decyzje",
}


class Rzucacz:
    """Atrapa granicy: zlicza wywolania i rzuca wyjatek ze znacznikiem granicy."""

    def __init__(self, wyjatek: type[Exception], klucz: str) -> None:
        self.wyjatek = wyjatek
        self.znacznik = f"granica-{klucz}"
        self.wywolania = 0

    def synchroniczna(self, *argumenty, **opcje):
        self.wywolania += 1
        raise self.wyjatek(self.znacznik)

    async def asynchroniczna(self, *argumenty, **opcje):
        self.wywolania += 1
        raise self.wyjatek(self.znacznik)


def _naloz(monkeypatch, klucz: str, rz: Rzucacz) -> None:
    """Podmienia granice `klucz` na atrape. Globalne funkcje (`Path.home`, `shutil.copyfile`,
    `Path.is_dir`) rzucaja tylko dla wejscia silnika, reszta wywolan idzie do prawdziwych."""
    if klucz == "ProfilBackendu.z_konfiguracji":
        monkeypatch.setattr(ProfilBackendu, "z_konfiguracji", staticmethod(rz.synchroniczna))
    elif klucz == "Path.home":
        monkeypatch.setattr(Path, "home", staticmethod(rz.synchroniczna))
    elif klucz == "Zakres.z_konfiguracji":
        monkeypatch.setattr(Zakres, "z_konfiguracji", staticmethod(rz.synchroniczna))
    elif klucz == "zakres.rozwiaz":
        monkeypatch.setattr(Zakres, "rozwiaz", rz.synchroniczna)
    elif klucz == "wykonawca":
        monkeypatch.setitem(silnik.WYKONAWCY_SYSTEMOWI, "diff", rz.asynchroniczna)
    elif klucz == "shutil.copyfile":
        prawdziwa = shutil.copyfile

        def copyfile(zrodlo, cel, *argumenty, **opcje):
            if Path(cel).name == "workflow.yaml":
                rz.synchroniczna()
            return prawdziwa(zrodlo, cel, *argumenty, **opcje)

        monkeypatch.setattr(shutil, "copyfile", copyfile)
    elif klucz == "worktree.is_dir":
        prawdziwa_is_dir = Path.is_dir

        def is_dir(self, *argumenty, **opcje):
            if self.name == "worktree" and self.parent.name == RUN_ID:
                rz.synchroniczna()
            return prawdziwa_is_dir(self, *argumenty, **opcje)

        monkeypatch.setattr(Path, "is_dir", is_dir)
    else:
        podmiana = rz.asynchroniczna if klucz in ASYNCHRONICZNE else rz.synchroniczna
        assert hasattr(silnik, klucz), f"silnik nie ma nazwy {klucz}"
        monkeypatch.setattr(silnik, klucz, podmiana)


WORKFLOW_Z_BRAMKA = WORKFLOW_Z_DIFFEM.replace(
    "rec: {skill: s_rec, needs: [diff]}", "rec: {skill: s_rec, needs: [diff], approval: true}")
WORKFLOW_DWIE_BRAMKI = WORKFLOW_Z_BRAMKA + (
    "  koniec: {skill: s_koniec, needs: [rec], approval: true}\n")
WORKFLOW_Z_KONTROLA = (
    "name: w\nsteps:\n  autor: {skill: s_autor}\n"
    "  spr: {system: check, capability: lint, needs: [autor]}\n")


def _agent_autora() -> SzpiegZmieniajacyDrzewo:
    """Agent bez procesu: autor zmienia drzewo (inaczej `diff` konczy sie porazka i recenzent
    nie startuje), reszta krokow konczy sie sukcesem."""
    return SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})


def _swiat_z_bramka(tmp_path: Path, tresc: str = WORKFLOW_Z_BRAMKA):
    assert "approval: true" in tresc    # inaczej podmiana tekstu w WORKFLOW_Z_BRAMKA nic nie zmienia
    kroki = [K("autor"), K("rec")] + ([K("koniec")] if "koniec" in tresc else [])
    return swiat(tmp_path, kroki, tresc=tresc, opcje_skilli=SKILLE_Z_DIFFEM)


async def _wykonaj(s, agent, **opcje) -> list[dict]:
    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    parametry = {"profil": s.profil, "dom": s.dom, "root": s.root, "zakres": s.zakres}
    parametry.update(opcje)
    await asyncio.wait_for(
        silnik.wykonaj_run(s.polecenie, zapisz, agent, **parametry), LIMIT_S)
    return zdarzenia


async def _wznow(s, agent, decyzja: str = "approve", **opcje) -> list[dict]:
    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    parametry = {"profil": s.profil, "dom": s.dom, "root": s.root}
    parametry.update(opcje)
    await asyncio.wait_for(
        silnik.wznow_run(RUN_ID, "rec", decyzja, zapisz, agent, powod="nie", **parametry),
        LIMIT_S)
    return zdarzenia


async def _przebieg(
    tmp_path: Path, monkeypatch, scenariusz: str, klucz: str, rz: Rzucacz,
) -> list[dict]:
    """Jeden przebieg silnika z granica `klucz` podmieniona na `rz`; zwraca zdarzenia."""
    agent = rz.asynchroniczna if klucz == "uruchom_agenta" else None

    def naloz() -> None:
        if klucz != "uruchom_agenta":
            _naloz(monkeypatch, klucz, rz)

    if scenariusz in ("run", "profil", "dom", "zakres"):
        s = swiat_z_diffem(tmp_path)
        naloz()
        opcje = {"run": {}, "profil": {"profil": None}, "dom": {"dom": None},
                 "zakres": {"zakres": None}}[scenariusz]
        return await _wykonaj(s, agent or _agent_autora(), **opcje)
    if scenariusz == "bramka":
        s = _swiat_z_bramka(tmp_path)
        naloz()
        return await _wykonaj(s, _agent_autora())
    if scenariusz == "kontrola":
        s = swiat(tmp_path, [K("autor")], tresc=WORKFLOW_Z_KONTROLA)
        naloz()
        return await _wykonaj(s, Szpieg())
    # wznowienie: pierwszy przebieg bez atrap, atrapa dopiero przed `wznow_run`
    tresc = WORKFLOW_DWIE_BRAMKI if scenariusz == "wznowienie_druga_bramka" else WORKFLOW_Z_BRAMKA
    s = _swiat_z_bramka(tmp_path, tresc)
    pierwszy = await uruchom(s, _agent_autora())
    assert pierwszy[-1]["kind"] == "step_waiting"
    naloz()
    opcje = {"wznowienie_profil": {"profil": None}, "wznowienie_dom": {"dom": None}}.get(
        scenariusz, {})
    decyzja = "reject" if scenariusz == "odrzucenie" else "approve"
    return await _wznow(s, agent or Szpieg(), decyzja, **opcje)


PRZYPADKI = [
    pytest.param(klucz, scenariusz, id=f"{klucz}-{scenariusz}")
    for klucz, scenariusze in WSTRZYKNIETE.items()
    for scenariusz in scenariusze
]


@pytest.mark.parametrize("wyjatek", WYJATKI)
@pytest.mark.parametrize(("klucz", "scenariusz"), PRZYPADKI)
async def test_wyjatek_z_granicy_silnika_konczy_run_porazka_kroku_i_run_finished(
    tmp_path, monkeypatch, klucz, scenariusz, wyjatek
):
    rz = Rzucacz(wyjatek, klucz)

    zdarzenia = await _przebieg(tmp_path, monkeypatch, scenariusz, klucz, rz)

    assert rz.wywolania >= 1, f"granica {klucz} nie zostala wywolana w scenariuszu {scenariusz}"
    rodzaje_zdarzen = rodzaje(zdarzenia)
    assert rodzaje_zdarzen.count("run_finished") == 1
    assert rodzaje_zdarzen[-1] == "run_finished"
    assert "step_waiting" not in rodzaje_zdarzen
    porazki = [z for z in zdarzenia if z["kind"] == "step_failed" and rz.znacznik in z["text"]]
    assert len(porazki) == 1, f"porazka z przyczyna {rz.znacznik} w {rodzaje_zdarzen}"
    assert zdarzenia.index(porazki[0]) < len(zdarzenia) - 1
    assert zdarzenia[-1]["run_id"] == RUN_ID


async def test_krok_bez_wyjatku_granic_konczy_sie_bez_porazki_kontrast_dla_wstrzykniec(tmp_path):
    # kontrast: ten sam swiat bez atrap daje czysty run, wiec porazki wyzej pochodza z atrap
    s = swiat_z_diffem(tmp_path)

    zdarzenia = await _wykonaj(s, _agent_autora())

    assert "step_failed" not in rodzaje(zdarzenia)
    assert jedno(zdarzenia, "step_finished", "rec")["text"] == ""
    assert rodzaje(zdarzenia)[-1] == "run_finished"


# --- tekst porazki: pelna tresc, nie "zawiera znacznik" ------------------------------------------

TEKSTY_PORAZKI = [
    pytest.param(OSError, "granica-nastepne", id="blad_dysku"),
    pytest.param(ValueError, "granica-nastepne", id="zla_wartosc"),
    pytest.param(BladTestowy, "granica-nastepne", id="blad_dziedziny"),
    pytest.param(KeyError, "brak klucza 'granica-nastepne'", id="brak_klucza_z_nazwa_klucza"),
]


@pytest.mark.parametrize(("wyjatek", "tekst"), TEKSTY_PORAZKI)
@pytest.mark.parametrize(("scenariusz", "krok"), [("run", "walidacja"), ("wznowienie", "rec")])
async def test_porazka_granicy_niesie_pelny_tekst_bledu_w_obu_zdarzeniach_konca(
    tmp_path, monkeypatch, scenariusz, krok, wyjatek, tekst
):
    rz = Rzucacz(wyjatek, "nastepne")

    zdarzenia = await _przebieg(tmp_path, monkeypatch, scenariusz, "nastepne", rz)

    assert jedno(zdarzenia, "step_failed", krok)["text"] == tekst
    assert jedno(zdarzenia, "run_finished", krok)["text"] == tekst


@pytest.mark.parametrize(
    ("wyjatek", "oczekiwany"),
    [
        (OSError("dysk pelny"), "dysk pelny"),
        (ValueError("zla wartosc"), "zla wartosc"),
        (BladTestowy("blad dziedziny"), "blad dziedziny"),
        (KeyError("klucz"), "brak klucza 'klucz'"),
        (KeyError(7), "brak klucza 7"),
    ],
)
def test_tekst_bledu_granicy_dla_braku_klucza_wskazuje_klucz_a_dla_reszty_jest_komunikatem(
    wyjatek, oczekiwany
):
    assert silnik._tekst_bledu(wyjatek) == oczekiwany


# --- wyjatek zadania kroku konczy tylko ten krok, nie run ----------------------------------------


class SzpiegRzucajacyDlaKroku(Szpieg):
    """Agent bez procesu: dla kroku `rzuca` rzuca wyjatek, kroki rownolegle dluzej czekaja."""

    def __init__(self, rzuca: str, wyjatek: Exception, **opcje) -> None:
        super().__init__(**opcje)
        self.rzuca, self.wyjatek = rzuca, wyjatek

    async def __call__(self, spec, polecenie, katalog, emit, **reszta):
        if spec.step_id == self.rzuca:
            self.wywolania.append(spec)
            raise self.wyjatek
        return await super().__call__(spec, polecenie, katalog, emit, **reszta)


@pytest.mark.parametrize("wyjatek", [OSError, KeyError, ValueError, BladTestowy])
async def test_wyjatek_agenta_jednego_kroku_konczy_tylko_ten_krok_a_rownolegly_krok_sie_konczy(
    tmp_path, wyjatek
):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = SzpiegRzucajacyDlaKroku("a", wyjatek("blad-a"), opoznienia={"b": 0.2})

    zdarzenia = await uruchom(s, agent)

    assert "blad-a" in jedno(zdarzenia, "step_failed", "a")["text"]
    assert jedno(zdarzenia, "step_finished", "b")["text"] == ""
    assert [z["step_id"] for z in zdarzenia if z["kind"] == "step_failed"] == ["a"]
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia[-1]["kind"] == "run_finished"


@pytest.mark.parametrize("klucz", ["_krok", "uruchom_agenta", "zbuduj_prompt"])
async def test_porazka_wstrzyknieta_w_krok_dostaje_id_tego_kroku_a_nie_kroku_walidacji(
    tmp_path, monkeypatch, klucz
):
    rz = Rzucacz(OSError, klucz)

    zdarzenia = await _przebieg(tmp_path, monkeypatch, "run", klucz, rz)

    porazki = [z["step_id"] for z in zdarzenia if z["kind"] == "step_failed"]
    assert porazki == ["autor"]
    assert "walidacja" not in [z["step_id"] for z in zdarzenia]

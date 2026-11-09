"""HAR-179: silnik nie zapisuje w gicie. Strazik po zrodlach (AST), nie po literalach.

Niezmiennik `no_git_write`: push nie istnieje w zadnej sciezce kodu, a `commit` w argv gita
wystepuje wylacznie w `runner.odbior.commituj` (osobna, jawna akcja czlowieka po `integrate`).
Silnik (`src/runner/`) nie ma sciezki do `integruj`/`commituj`: referencje do nich lezą tylko
w `operacje.py` (zapytanie z kokpitu), a nie w kodzie wykonujacym kroki runu.

Strazik skanuje ARGV WYWOLAN PROCESU (`subprocess.*`, `asyncio.create_subprocess_*`, `os.exec*`,
`os.spawn*`, `os.system`, `os.popen`) oraz wrappery gita (nazwa zaczyna sie od `_git`), razem z
argv zlozonym w zmiennej. Docstringi, f-stringi komunikatow i zbiory nazw (`walidacja._ZAPIS_GITA`)
nie sa argv, wiec nie sa zgłaszane. Zrodla czytamy wzgledem pliku testu (pod mutmut to kopia w
`mutants/`). Strazik ma testy wlasne na zrodlach syntetycznych w tmp_path: bez nich zielony wynik
na prawdziwych zrodlach nie dowodzi, ze strazik cokolwiek widzi (`evidence_required`).
"""
from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
KATALOGI = ("runner", "cockpit")
ODBIOR = "runner/odbior.py"
OPERACJE = "runner/operacje.py"
FUNKCJA_COMMITU = "commituj"
ZAKAZANE_SLOWA = {"commit", "push"}
FUNKCJE_ODBIORU = {"integruj", "commituj"}

_PROCES_SUBPROCESS = {"run", "Popen", "call", "check_call", "check_output"}
_PROCES_OS = {"system", "popen"}


_WARIANT_MUTMUTA = re.compile(
    r"^(?:x_(?P<funkcja>.+)|xǁ(?P<klasa>.+?)ǁ(?P<metoda>.+))__mutmut_(?:orig|\d+)$")


def nazwa_oryginalu(nazwa: str) -> str:
    """Pod mutmutem funkcja `f` zostaje trampolina, a jej ciało żyje w `x_f__mutmut_orig` i
    `x_f__mutmut_N` (metody: `xǁKlasaǁmetoda__mutmut_N`). Wariant liczy się jako `f`;
    każda inna nazwa zostaje bez zmian."""
    m = _WARIANT_MUTMUTA.match(nazwa)
    if m is None:
        return nazwa
    return m.group("funkcja") or m.group("metoda")


def _proces_os(nazwa: str) -> bool:
    return nazwa.startswith(("exec", "spawn")) or nazwa in _PROCES_OS


def _wrapper_gita(nazwa: str) -> bool:
    return nazwa.startswith("_git")


def _aliasy_procesu(drzewo: ast.AST) -> set[str]:
    """Nazwy wprowadzone przez `from subprocess/asyncio/os import ...`, ktore uruchamiaja proces."""
    wynik: set[str] = set()
    for w in ast.walk(drzewo):
        if not isinstance(w, ast.ImportFrom) or w.module is None:
            continue
        for alias in w.names:
            if ((w.module == "subprocess" and alias.name in _PROCES_SUBPROCESS)
                    or (w.module == "asyncio" and alias.name.startswith("create_subprocess"))
                    or (w.module == "os" and _proces_os(alias.name))):
                wynik.add(alias.asname or alias.name)
    return wynik


def _wywolanie_procesu(wywolanie: ast.Call, aliasy: set[str]) -> bool:
    f = wywolanie.func
    if isinstance(f, ast.Name):
        return f.id in aliasy or _wrapper_gita(f.id)
    if isinstance(f, ast.Attribute):
        if _wrapper_gita(f.attr):
            return True
        if isinstance(f.value, ast.Name):
            if f.value.id == "subprocess":
                return f.attr in _PROCES_SUBPROCESS
            if f.value.id == "asyncio":
                return f.attr.startswith("create_subprocess")
            if f.value.id == "os":
                return _proces_os(f.attr)
    return False


_FUNKCJE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _przypisania(korzen: ast.AST, w_funkcjach: bool = True) -> dict[str, list[ast.AST]]:
    """Nazwa -> przypisane wartosci w poddrzewie `korzen`. Zakres to funkcja (albo modul bez
    cial funkcji): przypisania z calego pliku zlaczylyby zmienne niezwiazanych funkcji, a
    pod mutmutem plik ma tysiace wariantow tej samej funkcji (kwadratowy koszt)."""
    wynik: dict[str, list[ast.AST]] = {}
    stos = [korzen]
    while stos:
        for w in ast.iter_child_nodes(stos.pop()):
            if not w_funkcjach and isinstance(w, _FUNKCJE):
                continue
            stos.append(w)
            if isinstance(w, ast.Assign):
                for cel in w.targets:
                    if isinstance(cel, ast.Name):
                        wynik.setdefault(cel.id, []).append(w.value)
            elif isinstance(w, ast.AnnAssign | ast.AugAssign):
                if isinstance(w.target, ast.Name) and w.value is not None:
                    wynik.setdefault(w.target.id, []).append(w.value)
    return wynik


class _Zakresy:
    """Przypisania widoczne w miejscu wywolania: biezaca funkcja i modul."""

    def __init__(self, *mapy: dict[str, list[ast.AST]]) -> None:
        self.mapy = mapy

    def get(self, nazwa: str, domyslnie=()):
        return [w for m in self.mapy for w in m.get(nazwa, ())] or list(domyslnie)


def _napisy(wezel: ast.AST, przypisania,
            odwiedzone: frozenset[str] = frozenset()) -> Iterator[str]:
    """Literaly napisowe argv: wprost, w listach i krotkach oraz zlozone w zmiennej.
    F-stringi (komunikaty) pomijamy."""
    if isinstance(wezel, ast.JoinedStr):
        return
    if isinstance(wezel, ast.Constant):
        if isinstance(wezel.value, str):
            yield wezel.value
        return
    if isinstance(wezel, ast.Name):
        if wezel.id in odwiedzone:
            return
        for wartosc in przypisania.get(wezel.id, []):
            yield from _napisy(wartosc, przypisania, odwiedzone | {wezel.id})
        return
    for dziecko in ast.iter_child_nodes(wezel):
        yield from _napisy(dziecko, przypisania, odwiedzone)


def _zakazane_slowa(tekst: str) -> set[str]:
    """Element argv rowny `commit`/`push` albo komenda powloki zaczynajaca sie od `git `."""
    if tekst in ZAKAZANE_SLOWA:
        return {tekst}
    if tekst.startswith("git "):
        return set(tekst.split()) & ZAKAZANE_SLOWA
    return set()


class _Skaner(ast.NodeVisitor):
    def __init__(self, rel: str, drzewo: ast.AST) -> None:
        self.rel = rel
        self.aliasy = _aliasy_procesu(drzewo)
        self.modul = _przypisania(drzewo, w_funkcjach=False)
        self.zakresy: list[dict[str, list[ast.AST]]] = []
        self.stos: list[str] = []
        self.naruszenia: set[tuple[str, int, str, str]] = set()
        self.dozwolone: set[tuple[str, int, str, str]] = set()

    def visit_FunctionDef(self, wezel: ast.FunctionDef) -> None:
        self.stos.append(nazwa_oryginalu(wezel.name))
        self.zakresy.append(_przypisania(wezel))
        self.generic_visit(wezel)
        self.zakresy.pop()
        self.stos.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, wezel: ast.Call) -> None:
        if _wywolanie_procesu(wezel, self.aliasy):
            argumenty = [*wezel.args, *(k.value for k in wezel.keywords)]
            widoczne = _Zakresy(*self.zakresy, self.modul)
            for argument in argumenty:
                for tekst in _napisy(argument, widoczne):
                    for slowo in _zakazane_slowa(tekst):
                        wpis = (self.rel, wezel.lineno, slowo,
                                self.stos[-1] if self.stos else "<modul>")
                        w_commituj = FUNKCJA_COMMITU in self.stos
                        if slowo == "commit" and self.rel == ODBIOR and w_commituj:
                            self.dozwolone.add(wpis)
                        else:
                            self.naruszenia.add(wpis)
        self.generic_visit(wezel)


def zapisy_gita(zrodlo: str, rel: str) -> tuple[list[tuple], list[tuple]]:
    """(naruszenia, dozwolone) dla jednego pliku; `rel` to sciezka wzgledem `src/` (POSIX)."""
    drzewo = ast.parse(zrodlo)
    skaner = _Skaner(rel, drzewo)
    skaner.visit(drzewo)
    return sorted(skaner.naruszenia), sorted(skaner.dozwolone)


def pliki_zrodlowe(src: Path) -> list[Path]:
    """Wszystkie `.py` w katalogach silnika i kokpitu; nieczytelny katalog to blad, nie pusty wynik."""
    def blad(e: OSError) -> None:
        raise e

    wynik: list[Path] = []
    for katalog in KATALOGI:
        glowny = src / katalog
        assert glowny.is_dir(), f"brak katalogu zrodel: {glowny}"
        for korzen, _, pliki in os.walk(glowny, onerror=blad):
            wynik += [Path(korzen, n) for n in pliki if n.endswith(".py")]
    return sorted(wynik)


def skanuj_zapisy_gita(src: Path) -> tuple[list[tuple], list[tuple], list[str]]:
    naruszenia: list[tuple] = []
    dozwolone: list[tuple] = []
    przeskanowane: list[str] = []
    for plik in pliki_zrodlowe(src):
        rel = plik.relative_to(src).as_posix()
        n, d = zapisy_gita(plik.read_text(encoding="utf-8"), rel)
        naruszenia += n
        dozwolone += d
        przeskanowane.append(rel)
    return naruszenia, dozwolone, przeskanowane


# --- referencje do integruj/commituj ----------------------------------------------------------

def referencje_odbioru(zrodlo: str) -> set[str]:
    """Nazwy `integruj`/`commituj` uzyte w kodzie: nazwa, atrybut, import i `getattr(x, "nazwa")`.
    Definicja funkcji, docstring i komentarz nie sa referencja."""
    wynik: set[str] = set()
    for w in ast.walk(ast.parse(zrodlo)):
        if isinstance(w, ast.Name) and w.id in FUNKCJE_ODBIORU:
            wynik.add(w.id)
        elif isinstance(w, ast.Attribute) and w.attr in FUNKCJE_ODBIORU:
            wynik.add(w.attr)
        elif isinstance(w, ast.ImportFrom | ast.Import):
            wynik |= {a.name.rsplit(".", 1)[-1] for a in w.names} & FUNKCJE_ODBIORU
        elif (isinstance(w, ast.Call) and isinstance(w.func, ast.Name) and w.func.id == "getattr"
              and len(w.args) >= 2 and isinstance(w.args[1], ast.Constant)
              and w.args[1].value in FUNKCJE_ODBIORU):
            wynik.add(w.args[1].value)
    return wynik


def skanuj_referencje(src: Path) -> dict[str, set[str]]:
    """Plik (wzgledem `src/`) -> referencje do funkcji odbioru; tylko pliki, ktore je maja."""
    wynik: dict[str, set[str]] = {}
    for plik in pliki_zrodlowe(src):
        ref = referencje_odbioru(plik.read_text(encoding="utf-8"))
        if ref:
            wynik[plik.relative_to(src).as_posix()] = ref
    return wynik


NAZWY_OPERACJI_ODBIORU = {"integrate", "commit"}


def _rejestr_operacji(wezel: ast.AST) -> bool:
    return ((isinstance(wezel, ast.Name) and wezel.id == "OPERACJE")
            or (isinstance(wezel, ast.Attribute) and wezel.attr == "OPERACJE"))


def referencje_operacji_odbioru(zrodlo: str) -> set[str]:
    """Operacje `integrate`/`commit` uzyte poza modulem operacji: `operacje.commit`,
    `from ...operacje import commit`, `OPERACJE["commit"]`, `OPERACJE.get("commit")`,
    `getattr(operacje, "commit")`. Inne obiekty z metoda `commit` (sesja bazy) nie licza sie."""
    wynik: set[str] = set()
    for w in ast.walk(ast.parse(zrodlo)):
        if (isinstance(w, ast.Attribute) and w.attr in NAZWY_OPERACJI_ODBIORU
                and isinstance(w.value, ast.Name) and w.value.id == "operacje"):
            wynik.add(w.attr)
        elif isinstance(w, ast.ImportFrom) and (w.module or "").rsplit(".", 1)[-1] == "operacje":
            wynik |= {a.name for a in w.names} & NAZWY_OPERACJI_ODBIORU
        elif (isinstance(w, ast.Subscript) and _rejestr_operacji(w.value)
              and isinstance(w.slice, ast.Constant) and w.slice.value in NAZWY_OPERACJI_ODBIORU):
            wynik.add(w.slice.value)
        elif (isinstance(w, ast.Call) and isinstance(w.func, ast.Attribute)
              and w.func.attr == "get" and _rejestr_operacji(w.func.value) and w.args
              and isinstance(w.args[0], ast.Constant)
              and w.args[0].value in NAZWY_OPERACJI_ODBIORU):
            wynik.add(w.args[0].value)
        elif (isinstance(w, ast.Call) and isinstance(w.func, ast.Name) and w.func.id == "getattr"
              and len(w.args) >= 2 and isinstance(w.args[0], ast.Name)
              and w.args[0].id == "operacje" and isinstance(w.args[1], ast.Constant)
              and w.args[1].value in NAZWY_OPERACJI_ODBIORU):
            wynik.add(w.args[1].value)
    return wynik


def skanuj_referencje_operacji(src: Path) -> dict[str, set[str]]:
    """Plik (wzgledem `src/`) -> uzyte operacje odbioru; sam modul operacji jest pominiety."""
    wynik: dict[str, set[str]] = {}
    for plik in pliki_zrodlowe(src):
        rel = plik.relative_to(src).as_posix()
        if rel == OPERACJE:
            continue
        ref = referencje_operacji_odbioru(plik.read_text(encoding="utf-8"))
        if ref:
            wynik[rel] = ref
    return wynik


# =================================================================================
# Prawdziwe zrodla
# =================================================================================

def test_skaner_widzi_pliki_silnika_i_kokpitu_a_nie_pusty_katalog():
    _, _, przeskanowane = skanuj_zapisy_gita(SRC)

    for oczekiwany in ("runner/odbior.py", "runner/worktree.py", "runner/silnik.py",
                       "runner/operacje.py", "cockpit/services/walidacja.py",
                       "cockpit/api/app.py"):
        assert oczekiwany in przeskanowane


def test_w_zrodlach_silnika_i_kokpitu_push_nie_wystepuje_w_zadnym_wywolaniu_procesu():
    naruszenia, _, _ = skanuj_zapisy_gita(SRC)

    assert [n for n in naruszenia if n[2] == "push"] == []


def test_w_zrodlach_silnika_i_kokpitu_commit_wystepuje_wylacznie_w_commituj_z_odbior_py():
    naruszenia, _, _ = skanuj_zapisy_gita(SRC)

    assert naruszenia == []


def test_strazik_widzi_commit_w_prawdziwym_commituj_wiec_zielony_wynik_nie_jest_pusty():
    _, dozwolone, _ = skanuj_zapisy_gita(SRC)

    assert dozwolone, "strazik nie znalazl commita w commituj: nie widzi argv gita"
    assert {(rel, slowo, funkcja) for rel, _, slowo, funkcja in dozwolone} == {
        (ODBIOR, "commit", FUNKCJA_COMMITU)}


def test_zbior_nazw_zapisu_gita_w_walidacji_nie_jest_zapisem_w_gicie():
    """`walidacja._ZAPIS_GITA` to lista nazw do wykrywania w definicjach workflow, nie argv."""
    zrodlo = (SRC / "cockpit" / "services" / "walidacja.py").read_text(encoding="utf-8")
    assert "_ZAPIS_GITA" in zrodlo

    naruszenia, dozwolone = zapisy_gita(zrodlo, "cockpit/services/walidacja.py")

    assert naruszenia == []
    assert dozwolone == []


def test_integruj_i_commituj_sa_uzywane_w_runnerze_wylacznie_w_operacjach():
    referencje = skanuj_referencje(SRC)

    assert referencje == {OPERACJE: {"integruj", "commituj"}}


def test_operacje_integrate_i_commit_nie_sa_uzywane_w_silniku_ani_w_kokpicie_poza_operacjami():
    assert skanuj_referencje_operacji(SRC) == {}


# =================================================================================
# Testy wlasne straznika: zrodla syntetyczne w tmp_path
# =================================================================================

def _zbadaj(tmp_path: Path, rel: str, zrodlo: str) -> tuple[list[tuple], list[tuple]]:
    """Zapisuje zrodlo w drzewie `src/` w tmp_path i skanuje je skanerem calego drzewa."""
    plik = tmp_path / "src" / rel
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_text(zrodlo, encoding="utf-8")
    for katalog in KATALOGI:
        (tmp_path / "src" / katalog).mkdir(parents=True, exist_ok=True)
    naruszenia, dozwolone, przeskanowane = skanuj_zapisy_gita(tmp_path / "src")
    assert rel in przeskanowane
    return naruszenia, dozwolone


@pytest.mark.parametrize(("zrodlo", "slowo"), [
    ('import subprocess\n\ndef f():\n    subprocess.run(["git", "commit", "-m", "x"])\n', "commit"),
    ('import subprocess\n\ndef f():\n    subprocess.run(["git", "push"])\n', "push"),
    ('import subprocess\n\ndef f():\n    subprocess.Popen(["git", "-C", p, "push", "origin"])\n', "push"),
    ('import subprocess\n\ndef f():\n    subprocess.check_output(("git", "commit"))\n', "commit"),
    ('import subprocess\n\ndef f():\n    subprocess.call(["git", "push"])\n', "push"),
    ('import subprocess\n\ndef f():\n    subprocess.check_call(["git", "commit"])\n', "commit"),
    ('import subprocess\n\ndef f():\n    subprocess.run(args=["git", "push"])\n', "push"),
    ('def f(r):\n    _git(r, "push")\n', "push"),
    ('def f(r):\n    _git(r, ["push", "origin"], (0,))\n', "push"),
    ('def f(r):\n    _git_z_kodem(r, ["commit", "-m", "x"], (0,))\n', "commit"),
    ('def f(r):\n    odbior._git(r, ["commit"], (0,))\n', "commit"),
    ('def f(r):\n    _git_inny_wrapper(r, "push")\n', "push"),
    ('import asyncio\n\nasync def f():\n    await asyncio.create_subprocess_exec("git", "push")\n', "push"),
    ('import asyncio\n\nasync def f():\n    await asyncio.create_subprocess_exec("git", "-C", p, "commit")\n', "commit"),
    ('import os\n\ndef f():\n    os.execvp("git", ["git", "push"])\n', "push"),
    ('import os\n\ndef f():\n    os.spawnlp(os.P_WAIT, "git", "git", "commit")\n', "commit"),
    ('import os\n\ndef f():\n    os.system("git commit -m x")\n', "commit"),
    ('import subprocess\n\ndef f():\n    subprocess.run("git push origin main", shell=True)\n', "push"),
    ('from subprocess import run as uruchom\n\ndef f():\n    uruchom(["git", "push"])\n', "push"),
    ('from subprocess import Popen\n\ndef f():\n    Popen(["git", "commit"])\n', "commit"),
    ('from asyncio import create_subprocess_exec\n\nasync def f():\n    await create_subprocess_exec("git", "push")\n', "push"),
    ('import subprocess\n\ndef f():\n    komenda = ["git", "commit"]\n    subprocess.run(komenda)\n', "commit"),
    ('import subprocess\n\ndef f():\n    subprocess.run(["git", "-C", repo, *["push"]])\n', "push"),
    ('import subprocess\nKOMENDA = ("git", "push")\n\ndef f():\n    subprocess.run(KOMENDA)\n', "push"),
    ('import subprocess\n\ndef f():\n    subprocess.run(["git"] + ["push"])\n', "push"),
    ('import subprocess\n\nsubprocess.run(["git", "push"])\n', "push"),
    ('import subprocess\n\nclass K:\n    def m(self):\n        subprocess.run(["git", "push"])\n', "push"),
])
def test_strazik_wykrywa_zapis_w_gicie_w_wywolaniu_procesu(tmp_path, zrodlo, slowo):
    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/inny.py", zrodlo)

    assert [n[2] for n in naruszenia] == [slowo]
    assert dozwolone == []


def test_strazik_wykrywa_commit_w_innej_funkcji_odbior_py(tmp_path):
    zrodlo = (
        "def zbierz_diff(r):\n"
        '    _git(r, ["commit", "-m", "x"], (0,))\n'
    )

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert [(n[2], n[3]) for n in naruszenia] == [("commit", "zbierz_diff")]
    assert dozwolone == []


def test_strazik_pozwala_na_commit_wylacznie_w_commituj_z_odbior_py(tmp_path):
    zrodlo = (
        "def commituj(w, pliki, opis):\n"
        '    _git(w, ["add", "--", *pliki], (0,))\n'
        '    _git(w, ["commit", "-m", opis, "--", *pliki], (0,))\n'
    )

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert naruszenia == []
    assert [(d[2], d[3]) for d in dozwolone] == [("commit", "commituj")]


def test_strazik_pozwala_na_commit_w_funkcji_zagniezdzonej_w_commituj(tmp_path):
    zrodlo = (
        "def commituj(w, pliki, opis):\n"
        "    def wykonaj():\n"
        '        _git(w, ["commit", "-m", opis], (0,))\n'
        "    wykonaj()\n"
    )

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert naruszenia == []
    assert len(dozwolone) == 1


def test_strazik_nie_pozwala_na_commit_w_funkcji_commituj_z_innego_pliku(tmp_path):
    zrodlo = 'def commituj(w, pliki, opis):\n    _git(w, ["commit", "-m", opis], (0,))\n'

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/operacje.py", zrodlo)

    assert [(n[0], n[2]) for n in naruszenia] == [("runner/operacje.py", "commit")]
    assert dozwolone == []


def test_strazik_nie_pozwala_na_push_nawet_w_commituj_z_odbior_py(tmp_path):
    zrodlo = (
        "def commituj(w, pliki, opis):\n"
        '    _git(w, ["commit", "-m", opis], (0,))\n'
        '    _git(w, ["push", "origin"], (0,))\n'
    )

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert [n[2] for n in naruszenia] == ["push"]
    assert [d[2] for d in dozwolone] == ["commit"]


@pytest.mark.parametrize("nazwa", ["x_commituj__mutmut_orig", "x_commituj__mutmut_3",
                                   "x_commituj__mutmut_12"])
def test_strazik_traktuje_wariant_mutmuta_funkcji_commituj_jak_commituj(tmp_path, nazwa):
    zrodlo = f'def {nazwa}(w, pliki, opis):\n    _git(w, ["commit", "-m", opis], (0,))\n'

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert naruszenia == []
    assert [(d[2], d[3]) for d in dozwolone] == [("commit", "commituj")]


@pytest.mark.parametrize("nazwa", [
    "x_inna__mutmut_1",
    "x_inna__mutmut_orig",
    "x_commituj_inaczej__mutmut_1",
    "x_commituj__mutmut_",
    "x_commituj__mutmut_x",
    "commituj__mutmut_1",
    "x_commituj",
    "xǁKlasaǁinna__mutmut_2",
])
def test_strazik_nie_poluzuje_reguly_dla_innych_nazw_podobnych_do_wariantu_mutmuta(
    tmp_path, nazwa,
):
    zrodlo = f'def {nazwa}(w, pliki, opis):\n    _git(w, ["commit", "-m", opis], (0,))\n'

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert [(n[2], n[3]) for n in naruszenia] == [("commit", nazwa_oryginalu(nazwa))]
    assert dozwolone == []


def test_strazik_traktuje_wariant_mutmuta_metody_commituj_jak_commituj(tmp_path):
    zrodlo = (
        "class K:\n"
        "    def xǁKǁcommituj__mutmut_4(self, w, pliki, opis):\n"
        '        _git(w, ["commit", "-m", opis], (0,))\n'
    )

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert naruszenia == []
    assert len(dozwolone) == 1


def test_strazik_nie_pozwala_na_push_w_wariancie_mutmuta_commituj(tmp_path):
    zrodlo = 'def x_commituj__mutmut_1(w):\n    _git(w, ["push"], (0,))\n'

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/odbior.py", zrodlo)

    assert [n[2] for n in naruszenia] == ["push"]
    assert dozwolone == []


def test_strazik_laczy_zmienna_argv_tylko_z_jej_funkcji_i_modulu(tmp_path):
    zrodlo = (
        "import subprocess\n\n"
        'def a():\n    k = ["git", "push"]\n    return k\n\n'
        "def b(k):\n    subprocess.run(k)\n"
    )

    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/inny.py", zrodlo)

    assert naruszenia == []
    assert dozwolone == []


def test_strazik_widzi_argv_zlozony_w_funkcji_zewnetrznej_i_uzyty_w_zagniezdzonej(tmp_path):
    zrodlo = (
        "import subprocess\n\n"
        'def a():\n    k = ["git", "push"]\n\n    def b():\n        subprocess.run(k)\n\n    b()\n'
    )

    naruszenia, _ = _zbadaj(tmp_path, "runner/inny.py", zrodlo)

    assert [(n[2], n[3]) for n in naruszenia] == [("push", "b")]


def test_strazik_wykrywa_zapis_w_gicie_takze_w_kodzie_kokpitu(tmp_path):
    zrodlo = 'import subprocess\n\ndef f():\n    subprocess.run(["git", "push"])\n'

    naruszenia, _ = _zbadaj(tmp_path, "cockpit/services/cos.py", zrodlo)

    assert [(n[0], n[2]) for n in naruszenia] == [("cockpit/services/cos.py", "push")]


@pytest.mark.parametrize("zrodlo", [
    '_ZAPIS_GITA = frozenset({"commit", "push"})\n',
    'def f():\n    """Silnik nie robi git commit ani git push."""\n    return 1\n',
    'class K:\n    """git push jest zakazany"""\n\n    def m(self):\n        """git commit tez"""\n',
    'def f(argumenty):\n    raise BladOdbioru(f"git commit {argumenty} nie dziala")\n',
    'def f():\n    raise BladOdbioru("git commit nie dziala")\n',
    'def f(sesja):\n    sesja.commit()\n',
    'import subprocess\n\ndef f():\n    subprocess.run(["git", "status"])\n',
    'def f(r):\n    _git(r, ["worktree", "add", "--detach", "x"], (0,))\n',
    'import subprocess\n\ndef f(x):\n    subprocess.run(["git", "-C", x, f"commit {x}"])\n',
    'import subprocess\n\ndef f():\n    subprocess.run(["claude", "-p", "Nie rob git commit ani git push."])\n',
    'ZAKAZANE = ("commit", "push")\n\ndef f(komenda):\n    return komenda in ZAKAZANE\n',
    'def f(tokeny):\n    return "push" in tokeny\n',
    'import subprocess\n\ndef f():\n    subprocess.run(["git", "committer"])\n',
    'import subprocess\n\ndef f():\n    subprocess.run(["git", "pushd"])\n',
])
def test_strazik_nie_zglasza_nazw_w_zbiorach_docstringach_i_komunikatach(tmp_path, zrodlo):
    naruszenia, dozwolone = _zbadaj(tmp_path, "runner/inny.py", zrodlo)

    assert naruszenia == []
    assert dozwolone == []


def test_strazik_oblewa_na_brakujacym_katalogu_zrodel_zamiast_zglosic_czysto(tmp_path):
    (tmp_path / "src" / "runner").mkdir(parents=True)

    with pytest.raises(AssertionError, match="brak katalogu zrodel"):
        skanuj_zapisy_gita(tmp_path / "src")


def test_strazik_oblewa_na_pliku_z_bledem_skladni_zamiast_go_pominac(tmp_path):
    with pytest.raises(SyntaxError):
        _zbadaj(tmp_path, "runner/zepsuty.py", "def f(:\n")


# --- referencje do integruj/commituj: testy wlasne -----------------------------------------

def _referencje(tmp_path: Path, pliki: dict[str, str]) -> dict[str, set[str]]:
    for rel, zrodlo in pliki.items():
        plik = tmp_path / "src" / rel
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(zrodlo, encoding="utf-8")
    for katalog in KATALOGI:
        (tmp_path / "src" / katalog).mkdir(parents=True, exist_ok=True)
    return skanuj_referencje(tmp_path / "src")


@pytest.mark.parametrize(("zrodlo", "oczekiwane"), [
    ("from runner.odbior import integruj\n", {"integruj"}),
    ("from .odbior import commituj, zbierz_diff\n", {"commituj"}),
    ("from runner.odbior import integruj as zintegruj\n", {"integruj"}),
    ("import runner.odbior as o\n\ndef f(w):\n    return o.integruj(w, 'x')\n", {"integruj"}),
    ("from runner import odbior\n\ndef f(w):\n    return odbior.commituj(w, [], 'x')\n", {"commituj"}),
    ("def f(w):\n    return integruj(w, 'x')\n", {"integruj"}),
    ("def f(m):\n    return getattr(m, 'commituj')\n", {"commituj"}),
    ("def f(m):\n    return [integruj, commituj]\n", {"integruj", "commituj"}),
])
def test_strazik_referencji_widzi_uzycie_funkcji_odbioru_w_silniku(tmp_path, zrodlo, oczekiwane):
    assert _referencje(tmp_path, {"runner/silnik.py": zrodlo}) == {"runner/silnik.py": oczekiwane}


@pytest.mark.parametrize("zrodlo", [
    "def integruj(w, b):\n    return []\n\n\ndef commituj(w, p, o):\n    return 'x'\n",
    '"""Docstring o integruj i commituj."""\n\n# integruj commituj w komentarzu\nX = "integruj"\n',
    "def f():\n    return integruj_run(1)\n",
    "def f(s):\n    return s.commituj_run(1)\n",
    "from cockpit.services.odbior import integruj_run, commituj_run\n",
])
def test_strazik_referencji_nie_zglasza_definicji_docstringow_komentarzy_i_innych_nazw(
    tmp_path, zrodlo,
):
    assert _referencje(tmp_path, {"runner/odbior.py": zrodlo}) == {}


@pytest.mark.parametrize(("zrodlo", "oczekiwane"), [
    ("from runner import operacje\n\ndef f(d):\n    return operacje.OPERACJE['commit'](d)\n",
     {"commit"}),
    ("from runner.operacje import OPERACJE\n\ndef f(d):\n    return OPERACJE['integrate'](d)\n",
     {"integrate"}),
    ("from runner import operacje\n\ndef f(d):\n    return operacje.commit(d)\n", {"commit"}),
    ("from runner import operacje\n\nasync def f(d):\n    return await operacje.integrate(d)\n",
     {"integrate"}),
    ("from runner.operacje import commit\n", {"commit"}),
    ("from .operacje import integrate as zintegruj, ping\n", {"integrate"}),
    ("from runner.operacje import OPERACJE\n\ndef f():\n    return OPERACJE.get('commit')\n",
     {"commit"}),
    ("from runner import operacje\n\ndef f():\n    return getattr(operacje, 'integrate')\n",
     {"integrate"}),
])
def test_strazik_operacji_widzi_uzycie_integrate_i_commit_w_silniku(tmp_path, zrodlo, oczekiwane):
    for rel, tresc in {"runner/silnik.py": zrodlo}.items():
        plik = tmp_path / "src" / rel
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    for katalog in KATALOGI:
        (tmp_path / "src" / katalog).mkdir(parents=True, exist_ok=True)

    assert skanuj_referencje_operacji(tmp_path / "src") == {"runner/silnik.py": oczekiwane}


@pytest.mark.parametrize("zrodlo", [
    "def f(sesja):\n    sesja.commit()\n",
    "from runner import operacje\n\nasync def f(p):\n    return await operacje.wykonaj(p)\n",
    "from runner import operacje\n\ndef f():\n    return operacje.OPERACJE['read_file']\n",
    "from runner.operacje import OPERACJE, wykonaj\n",
    "def f(d):\n    return d['commit']\n",
    "def f(slownik):\n    return slownik.get('integrate')\n",
    "def integrate(d):\n    return d\n",
])
def test_strazik_operacji_nie_zglasza_innych_obiektow_z_metoda_commit_ani_innych_operacji(
    tmp_path, zrodlo,
):
    plik = tmp_path / "src" / "runner" / "silnik.py"
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_text(zrodlo, encoding="utf-8")
    (tmp_path / "src" / "cockpit").mkdir(parents=True, exist_ok=True)

    assert skanuj_referencje_operacji(tmp_path / "src") == {}


def test_strazik_operacji_pomija_tylko_modul_operacji(tmp_path):
    zrodlo = "from runner import operacje\n\ndef f(d):\n    return operacje.commit(d)\n"
    for rel in ("runner/operacje.py", "runner/client.py"):
        plik = tmp_path / "src" / rel
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(zrodlo, encoding="utf-8")
    (tmp_path / "src" / "cockpit").mkdir(parents=True, exist_ok=True)

    assert skanuj_referencje_operacji(tmp_path / "src") == {"runner/client.py": {"commit"}}


def test_strazik_referencji_zglasza_uzycie_w_kokpicie_i_w_kazdym_pliku_runnera_poza_operacjami(
    tmp_path,
):
    pliki = {
        "runner/silnik.py": "from runner.odbior import integruj\n",
        "runner/odbior.py": "def f(w):\n    return commituj(w, [], 'x')\n",
        "runner/operacje.py": "from runner.odbior import integruj, commituj\n",
        "cockpit/services/cos.py": "from runner.odbior import integruj\n",
    }

    referencje = _referencje(tmp_path, pliki)

    assert referencje == {
        "runner/silnik.py": {"integruj"},
        "runner/odbior.py": {"commituj"},
        "runner/operacje.py": {"integruj", "commituj"},
        "cockpit/services/cos.py": {"integruj"},
    }
    poza_operacjami = {p for p in referencje if p != OPERACJE}
    assert poza_operacjami == {"runner/silnik.py", "runner/odbior.py", "cockpit/services/cos.py"}

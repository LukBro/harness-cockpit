"""HAR-181: runner usuwa plik (`delete_file`) i zapisuje plik w nieistniejacym katalogu
(`write_file` z opcja `utworz_katalogi`). Prawdziwe operacje runnera (`runner.operacje.wykonaj`)
na plikach w katalogu testu; asercje czytaja skutek na DYSKU (plik, dowiazanie, katalog, mtime),
nie tylko odpowiedz. Fixture `projekt` i pomocniki pochodza z `test_operacje_plikowe.py`.
Zakres `delete_file` jest taki sam jak `write_file` (`do_zapisu=True`): odmowy maja ten sam kod
`poza_zakresem`, a powod widac w linii `odmowa powod=...` na stderr.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import errno
import json
import os

import pytest

from runner import operacje
from test_operacje_plikowe import JEST_ROOT, SEKRET, _blad, _nazwy, _wyk, projekt

FIXTURY_Z_TEST_OPERACJE_PLIKOWE = (projekt,)   # fixture pytest zaimportowana z innego pliku testow

KOMUNIKAT_BLAD_DYSKU = "blad dysku przy operacji na pliku"


def _powod_odmowy(capsys) -> str:
    """Powod odmowy z linii `odmowa powod=<powod> sciezka=...` na stderr runnera."""
    linie = [w for w in capsys.readouterr().err.splitlines() if " odmowa powod=" in w]
    assert len(linie) == 1, linie
    return linie[0].split(" odmowa powod=", 1)[1].split(" ", 1)[0]


# =================================================================================
# 1. delete_file: kontrakt
# =================================================================================

async def test_katalog_definicji_oddaje_katalog_domowy_runnera_ustawiony_w_tescie(projekt):
    odp = await _wyk("katalog_definicji")

    assert odp["ok"] is True, odp
    assert odp["wynik"]["dom"] == str(projekt.dom)
    assert odp["wynik"]["dom"] != "None"


async def test_usuniecie_katalogu_jest_odmowione_jako_katalog_takze_gdy_system_odpowiada_eperm_zamiast_eisdir(
    projekt, monkeypatch,
):
    # Linux oddaje EISDIR dla unlink katalogu, inne systemy EPERM: o `to_katalog` decyduje
    # sprawdzenie typu przed unlink, a nie kod bledu systemu
    katalog = projekt.korzen / "podkatalog"
    katalog.mkdir()

    def unlink_eperm(*a, **kw):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "unlink", unlink_eperm)

    odp = await _wyk("delete_file", sciezka=str(katalog))

    _blad(odp, "to_katalog")
    assert odp["blad"] == "sciezka jest katalogiem"
    assert katalog.is_dir()


def test_runner_zna_operacje_usuniecia_pliku():
    assert "delete_file" in operacje.OPERACJE


async def test_usuniecie_pliku_w_projekcie_usuwa_go_z_dysku_a_sasiednie_pliki_i_katalog_zostaja(
    projekt,
):
    katalog = projekt.korzen / ".cockpit" / "skills" / "x"
    katalog.mkdir(parents=True)
    skill = katalog / "SKILL.md"
    skill.write_text("---\nid: x\n---\n", encoding="utf-8")
    sasiad = katalog / "notatki.md"
    sasiad.write_text("zostaje", encoding="utf-8")

    odp = await _wyk("delete_file", sciezka=str(skill))

    assert odp == {"req_id": "r-1", "ok": True, "wynik": {"sciezka": str(skill)}}
    assert not os.path.lexists(skill)
    assert sasiad.read_text(encoding="utf-8") == "zostaje"
    assert katalog.is_dir()


async def test_usuniecie_pliku_z_katalogu_domowego_przez_tylde_usuwa_ten_plik(projekt):
    plik = projekt.dom / ".cockpit" / "skills" / "glob" / "SKILL.md"
    plik.parent.mkdir(parents=True)
    plik.write_text("---\nid: glob\n---\n", encoding="utf-8")

    odp = await _wyk("delete_file", sciezka="~/.cockpit/skills/glob/SKILL.md")

    assert odp["ok"] is True, odp
    assert odp["wynik"] == {"sciezka": str(plik)}
    assert not os.path.lexists(plik)


async def test_usuniecie_pliku_ktorego_nie_ma_daje_kod_nie_istnieje(projekt):
    odp = await _wyk("delete_file", sciezka=str(projekt.korzen / "brak.md"))

    _blad(odp, "nie_istnieje")
    assert _nazwy(projekt.korzen) == []


async def test_usuniecie_pliku_w_nieistniejacym_katalogu_daje_nie_istnieje_a_nie_brak_katalogu(
    projekt,
):
    odp = await _wyk("delete_file", sciezka=str(projekt.korzen / "nie-ma" / "a.md"))

    _blad(odp, "nie_istnieje")
    assert not (projekt.korzen / "nie-ma").exists()


async def test_usuniecie_katalogu_jest_odmowione_a_katalog_z_zawartoscia_zostaje(projekt):
    katalog = projekt.korzen / "podkatalog"
    katalog.mkdir()
    (katalog / "a.md").write_text("tresc", encoding="utf-8")

    odp = await _wyk("delete_file", sciezka=str(katalog))

    _blad(odp, "to_katalog")
    assert odp["blad"] == "sciezka jest katalogiem"
    assert (katalog / "a.md").read_text(encoding="utf-8") == "tresc"


async def test_usuniecie_pliku_przez_sciezke_z_plikiem_zamiast_katalogu_daje_nie_katalog(projekt):
    plik = projekt.korzen / "plik.txt"
    plik.write_text("zostaje", encoding="utf-8")

    odp = await _wyk("delete_file", sciezka=str(plik / "x.md"))

    _blad(odp, "nie_katalog")
    assert plik.read_text(encoding="utf-8") == "zostaje"


# ---- zakres: odmowy, po ktorych plik zostaje na dysku ---------------------------------------

async def test_usuniecie_pliku_poza_zakresem_jest_odmowione_a_plik_zostaje_nietkniety(
    projekt, capsys,
):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    mtime = cudzy.stat().st_mtime_ns

    odp = await _wyk("delete_file", sciezka=str(cudzy))

    _blad(odp, "poza_zakresem")
    assert odp["blad"] == operacje.KOMUNIKAT_POZA_ZAKRESEM
    assert _powod_odmowy(capsys) == "poza_zakresem"
    assert cudzy.read_text(encoding="utf-8") == "oryginal"
    assert cudzy.stat().st_mtime_ns == mtime


async def test_usuniecie_pliku_w_katalogu_git_projektu_jest_odmowione_a_plik_zostaje(
    projekt, capsys,
):
    (projekt.korzen / ".git").mkdir()
    cel = projekt.korzen / ".git" / "config"
    cel.write_text("[core]", encoding="utf-8")

    odp = await _wyk("delete_file", sciezka=str(cel))

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "no_git_write"
    assert cel.read_text(encoding="utf-8") == "[core]"


async def test_usuniecie_sekretu_w_projekcie_jest_odmowione_a_plik_zostaje(projekt, capsys):
    cel = projekt.korzen / ".env"
    cel.write_text("X=1", encoding="utf-8")

    odp = await _wyk("delete_file", sciezka=str(cel))

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "sekret"
    assert cel.read_text(encoding="utf-8") == "X=1"


async def test_usuniecie_dowiazania_do_pliku_w_zakresie_jest_odmowione_a_dowiazanie_i_cel_zostaja(
    projekt, capsys,
):
    cel = projekt.korzen / "cel.md"
    cel.write_text("tresc", encoding="utf-8")
    dowiazanie = projekt.korzen / "link.md"
    dowiazanie.symlink_to(cel)

    odp = await _wyk("delete_file", sciezka=str(dowiazanie))

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "dowiazanie"
    assert dowiazanie.is_symlink()
    assert cel.read_text(encoding="utf-8") == "tresc"


async def test_usuniecie_dowiazania_wychodzacego_poza_zakres_nie_rusza_celu_ani_dowiazania(
    projekt, capsys,
):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    dowiazanie = projekt.korzen / "wyjscie.txt"
    dowiazanie.symlink_to(cudzy)

    odp = await _wyk("delete_file", sciezka=str(dowiazanie))

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "dowiazanie"
    assert dowiazanie.is_symlink()
    assert cudzy.read_text(encoding="utf-8") == "oryginal"


async def test_usuniecie_wiszacego_dowiazania_jest_odmowione_a_dowiazanie_zostaje(projekt, capsys):
    dowiazanie = projekt.korzen / "wiszace.md"
    dowiazanie.symlink_to(projekt.korzen / "nie-ma.md")

    odp = await _wyk("delete_file", sciezka=str(dowiazanie))

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "dowiazanie"
    assert dowiazanie.is_symlink()


async def test_usuniecie_pliku_przez_katalog_bedacy_dowiazaniem_poza_zakres_nie_rusza_celu(projekt):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    (projekt.korzen / "katalog-link").symlink_to(projekt.obcy)

    odp = await _wyk("delete_file", sciezka=str(projekt.korzen / "katalog-link" / "cudzy.txt"))

    _blad(odp, "poza_zakresem")
    assert cudzy.read_text(encoding="utf-8") == "oryginal"


async def test_usuniecie_pliku_ze_sciezka_wzgledna_jest_odmowione(projekt, capsys):
    odp = await _wyk("delete_file", sciezka="wzgledna/a.md")

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "wzgledna"


@pytest.mark.parametrize("dane", [{"sciezka": 5}, {"sciezka": None}, {}],
                         ids=["sciezka_liczba", "sciezka_none", "brak_sciezki"])
async def test_usuniecie_ze_zla_sciezka_w_zapytaniu_daje_zle_dane(projekt, dane):
    odp = await _wyk("delete_file", **dane)

    _blad(odp, "zle_dane")


# ---- bledy dysku: tlumaczone na kod dziedziny, bez tresci wyjatku -----------------------------

@pytest.mark.skipif(JEST_ROOT, reason="root usuwa pliki bez wzgledu na prawa katalogu")
async def test_usuniecie_pliku_z_katalogu_bez_prawa_zapisu_daje_brak_uprawnien_a_plik_zostaje(
    projekt,
):
    zamkniety = projekt.korzen / "zamkniety"
    zamkniety.mkdir()
    plik = zamkniety / "a.md"
    plik.write_text("zostaje", encoding="utf-8")
    zamkniety.chmod(0o500)
    try:
        odp = await _wyk("delete_file", sciezka=str(plik))
    finally:
        zamkniety.chmod(0o700)

    _blad(odp, "brak_uprawnien")
    assert odp["blad"] == "brak uprawnien do pliku lub katalogu"
    assert str(zamkniety) not in json.dumps(odp)
    assert plik.read_text(encoding="utf-8") == "zostaje"


async def test_inny_blad_dysku_przy_usuwaniu_daje_blad_dysku_bez_tresci_wyjatku(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.md"
    plik.write_text("zostaje", encoding="utf-8")

    def zepsute(*a, **kw):
        raise OSError(errno.EIO, f"blad {SEKRET}")

    monkeypatch.setattr(os, "unlink", zepsute)   # Path.unlink i os.unlink
    monkeypatch.setattr(os, "remove", zepsute)

    odp = await _wyk("delete_file", sciezka=str(plik))

    _blad(odp, "blad_dysku")
    assert odp["blad"] == KOMUNIKAT_BLAD_DYSKU
    assert SEKRET not in json.dumps(odp)
    assert plik.read_text(encoding="utf-8") == "zostaje"


# =================================================================================
# 2. write_file z opcja utworz_katalogi
# =================================================================================

async def test_zapis_nowego_skilla_w_nieistniejacym_katalogu_tworzy_katalogi_i_plik(projekt):
    plik = projekt.korzen / ".cockpit" / "skills" / "nowy" / "SKILL.md"
    tresc = "---\nid: nowy\nname: N\nrole: author\n---\nBrief ✓\r\n"

    odp = await _wyk("write_file", sciezka=str(plik), tresc=tresc, utworz_katalogi=True)

    assert odp == {"req_id": "r-1", "ok": True, "wynik": {
        "sciezka": str(plik), "rozmiar": len(tresc.encode("utf-8"))}}
    assert plik.read_bytes() == tresc.encode("utf-8")
    assert _nazwy(projekt.korzen / ".cockpit" / "skills") == ["nowy"]


async def test_zapis_z_opcja_utworz_katalogi_w_istniejacym_katalogu_nadpisuje_plik_i_nic_nie_tworzy(
    projekt,
):
    plik = projekt.korzen / "a.md"
    plik.write_text("stare", encoding="utf-8")

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe", utworz_katalogi=True)

    assert odp["ok"] is True, odp
    assert plik.read_text(encoding="utf-8") == "nowe"
    assert _nazwy(projekt.korzen) == ["a.md"]


async def test_zapis_z_opcja_utworz_katalogi_w_katalogu_domowym_przez_tylde_tworzy_katalogi(projekt):
    odp = await _wyk("write_file", sciezka="~/.cockpit/skills/glob/SKILL.md", tresc="x",
                     utworz_katalogi=True)

    assert odp["ok"] is True, odp
    assert (projekt.dom / ".cockpit" / "skills" / "glob" / "SKILL.md").read_text() == "x"


async def test_zapis_z_wylaczona_opcja_utworz_katalogi_daje_brak_katalogu_i_nic_nie_tworzy(projekt):
    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "nowy_kat" / "a.md"), tresc="x",
                     utworz_katalogi=False)

    _blad(odp, "brak_katalogu")
    assert not (projekt.korzen / "nowy_kat").exists()


@pytest.mark.parametrize("wartosc", ["tak", 1, 0, None, []],
                         ids=["napis", "jeden", "zero", "none", "lista"])
async def test_zapis_z_opcja_utworz_katalogi_niebedaca_wartoscia_logiczna_daje_zle_dane(
    projekt, wartosc,
):
    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "nowy_kat" / "a.md"), tresc="x",
                     utworz_katalogi=wartosc)

    _blad(odp, "zle_dane")
    assert odp["blad"] == "pole utworz_katalogi musi byc wartoscia logiczna"
    assert not (projekt.korzen / "nowy_kat").exists()


def test_zapis_pliku_bez_podanej_opcji_utworz_katalogi_nie_tworzy_katalogow(projekt):
    sciezka = projekt.korzen / "nowy_kat" / "a.md"

    with pytest.raises(operacje.BladOperacji) as wyjatek:
        operacje._zapisz_plik(str(sciezka), b"x")

    assert wyjatek.value.kod == "brak_katalogu"
    assert not (projekt.korzen / "nowy_kat").exists()


def test_zapis_pliku_wymusza_na_dysk_komplet_danych_dopiero_po_ich_zapisaniu_i_oproznieniu_bufora(
    projekt, monkeypatch,
):
    dane = b"tresc skilla \xe2\x9c\x93\n"
    rozmiary_w_chwili_fsync: list[int] = []
    prawdziwy_fsync = os.fsync

    def fsync_ktory_zapamietuje_rozmiar_pliku_na_dysku(fd):
        rozmiary_w_chwili_fsync.append(os.fstat(fd).st_size)
        prawdziwy_fsync(fd)

    monkeypatch.setattr(operacje.os, "fsync", fsync_ktory_zapamietuje_rozmiar_pliku_na_dysku)
    plik = projekt.korzen / "a.md"

    odp = operacje._zapisz_plik(str(plik), dane)

    assert odp == {"sciezka": str(plik), "rozmiar": len(dane)}
    assert plik.read_bytes() == dane
    assert rozmiary_w_chwili_fsync == [len(dane)]


async def test_zapis_bez_pola_utworz_katalogi_w_zapytaniu_nie_tworzy_katalogow(projekt):
    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "nowy_kat" / "a.md"), tresc="x")

    _blad(odp, "brak_katalogu")
    assert not (projekt.korzen / "nowy_kat").exists()


async def test_katalogi_nie_powstaja_gdy_sciezka_prowadzi_przez_dowiazanie_poza_zakres(
    projekt, capsys,
):
    (projekt.korzen / "link").symlink_to(projekt.obcy)

    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "link" / "nowy" / "x.md"),
                     tresc="x", utworz_katalogi=True)

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "poza_zakresem"
    assert _nazwy(projekt.obcy) == []   # ani `nowy`, ani plik


async def test_katalogi_nie_powstaja_poza_zakresem_runnera(projekt):
    odp = await _wyk("write_file", sciezka=str(projekt.obcy / "nowy" / "x.md"), tresc="x",
                     utworz_katalogi=True)

    _blad(odp, "poza_zakresem")
    assert _nazwy(projekt.obcy) == []


async def test_katalogi_nie_powstaja_w_katalogu_git_projektu(projekt, capsys):
    (projekt.korzen / ".git").mkdir()

    odp = await _wyk("write_file", sciezka=str(projekt.korzen / ".git" / "nowy" / "x"), tresc="x",
                     utworz_katalogi=True)

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "no_git_write"
    assert _nazwy(projekt.korzen / ".git") == []


async def test_katalogi_nie_powstaja_dla_pliku_sekretu(projekt, capsys):
    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "nowy" / ".env"), tresc="X=1",
                     utworz_katalogi=True)

    _blad(odp, "poza_zakresem")
    assert _powod_odmowy(capsys) == "sekret"
    assert not (projekt.korzen / "nowy").exists()


async def test_katalogi_nie_powstaja_dla_sciezki_wzglednej(projekt):
    odp = await _wyk("write_file", sciezka="nowy/x.md", tresc="x", utworz_katalogi=True)

    _blad(odp, "poza_zakresem")
    assert not (projekt.korzen / "nowy").exists()


async def test_zapis_z_tworzeniem_katalogow_przez_plik_zamiast_katalogu_daje_nie_katalog(projekt):
    plik = projekt.korzen / "plik.txt"
    plik.write_text("zostaje", encoding="utf-8")

    odp = await _wyk("write_file", sciezka=str(plik / "x" / "y.md"), tresc="x",
                     utworz_katalogi=True)

    _blad(odp, "nie_katalog")
    assert plik.read_text(encoding="utf-8") == "zostaje"


@pytest.mark.skipif(JEST_ROOT, reason="root tworzy katalogi bez wzgledu na prawa")
async def test_tworzenie_katalogow_w_katalogu_bez_prawa_zapisu_daje_brak_uprawnien(projekt):
    zamkniety = projekt.korzen / "zamkniety"
    zamkniety.mkdir()
    zamkniety.chmod(0o500)
    try:
        odp = await _wyk("write_file", sciezka=str(zamkniety / "nowy" / "x.md"), tresc="x",
                         utworz_katalogi=True)
    finally:
        zamkniety.chmod(0o700)

    _blad(odp, "brak_uprawnien")
    assert odp["blad"] == "brak uprawnien do pliku lub katalogu"
    assert str(zamkniety) not in json.dumps(odp)
    assert _nazwy(zamkniety) == []


async def test_inny_blad_dysku_przy_tworzeniu_katalogow_daje_blad_dysku_bez_tresci_wyjatku(
    projekt, monkeypatch,
):
    def zepsute_mkdir(*a, **kw):
        raise OSError(errno.EIO, f"blad {SEKRET}")

    monkeypatch.setattr(os, "mkdir", zepsute_mkdir)

    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "nowy" / "x.md"), tresc="x",
                     utworz_katalogi=True)

    _blad(odp, "blad_dysku")
    assert odp["blad"] == KOMUNIKAT_BLAD_DYSKU
    assert SEKRET not in json.dumps(odp)
    assert not (projekt.korzen / "nowy").exists()

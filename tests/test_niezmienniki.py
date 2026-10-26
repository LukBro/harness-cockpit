"""Nazwy testow w jezyku dziedziny, nie technologii (HAR-6).

Niezmienniki jako kod: rola ustawia tier i prawo zapisu razem, recenzent nie
zmienia katalogu, krok z approval czeka na czlowieka, brak dowodu to blad kroku.
Wszystko na czystych funkcjach i tmp_path, bez bazy.
"""
import dataclasses
import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cockpit.services.invariants import (
    PROFILE_ROL,
    WSZYSTKIE,
    Niezmiennik,
    ProfilRoli,
    Rola,
    Tier,
    profil_roli,
)
from cockpit.services.errors import NaruszonyNiezmiennik
from cockpit.services.runs import zapisz_zdarzenie
from cockpit.services.skills import Skill
from cockpit.services.walidacja import (
    KLUCZE_KOMENDY,
    KLUCZE_ZAPISU,
    Naruszenie,
    dowod_z_komendy,
    dowod_z_pliku,
    migawka,
    sprawdz_decyzje,
    sprawdz_katalog_runu,
    sprawdz_pomiar,
    sprawdz_tylko_odczyt,
    waliduj_definicje,
)


def nazwy(naruszenia: list[Naruszenie]) -> list[Niezmiennik]:
    return [n.niezmiennik for n in naruszenia]


def tekst(wynik: Naruszenie | list[Naruszenie] | None) -> str:
    """Komunikaty tak, jak zobaczy je czlowiek (str kazdego naruszenia)."""
    if wynik is None:
        return ""
    if isinstance(wynik, Naruszenie):
        return str(wynik)
    return "\n".join(str(n) for n in wynik)


def plik(korzen: Path, sciezka: str, tresc: str) -> Path:
    cel = korzen / sciezka
    cel.parent.mkdir(parents=True, exist_ok=True)
    cel.write_text(tresc)
    return cel


# --- role: tier i prawo zapisu razem ---------------------------------------

@pytest.mark.parametrize(
    ("rola", "tier", "zapis"),
    [
        (Rola.author, Tier.judgment, True),
        (Rola.reviewer, Tier.judgment, False),
        (Rola.executor, Tier.execution, True),
        (Rola.gatherer, Tier.retrieval, False),
    ],
)
def test_kazda_rola_ma_jeden_ustalony_tier_i_prawo_zapisu(rola, tier, zapis):
    profil = profil_roli(rola)
    assert profil.tier == tier
    assert profil.zapis is zapis
    assert PROFILE_ROL[rola] == profil


def test_profile_rol_obejmuja_dokladnie_cztery_role():
    assert set(PROFILE_ROL) == set(Rola)
    assert len(PROFILE_ROL) == 4


def test_profile_rol_nie_da_sie_zmienic_w_trakcie_dzialania():
    with pytest.raises(TypeError):
        PROFILE_ROL[Rola.reviewer] = ProfilRoli(tier=Tier.judgment, zapis=True)  # type: ignore[index]
    with pytest.raises(TypeError):
        del PROFILE_ROL[Rola.reviewer]  # type: ignore[attr-defined]
    assert profil_roli(Rola.reviewer).zapis is False


def test_profil_roli_nie_pozwala_dopisac_zapisu_recenzentowi_po_fakcie():
    with pytest.raises(dataclasses.FrozenInstanceError):
        profil_roli(Rola.reviewer).zapis = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        profil_roli(Rola.reviewer).tier = Tier.execution  # type: ignore[misc]
    assert profil_roli(Rola.reviewer).zapis is False


def test_skill_nie_ma_wlasnego_pola_tieru_ani_prawa_zapisu():
    """Tier i zapis wynikaja wylacznie z roli - skill niesie role, nic wiecej."""
    pola = set(Skill.model_fields)
    assert "role" in pola
    for zabronione in ("zapis", "write", "can_write", "tier", "model"):
        assert zabronione not in pola


def test_profil_roli_to_jedyna_struktura_z_tierem_i_zapisem():
    assert {f.name for f in dataclasses.fields(ProfilRoli)} == {"tier", "zapis"}
    assert ProfilRoli.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


@pytest.mark.parametrize("rola", list(Rola))
def test_krok_z_tierem_zgodnym_z_rola_nie_ma_naruszen(rola):
    definicja = {"role": rola.value, "tier": profil_roli(rola).tier.value}
    assert waliduj_definicje(definicja) == []


@pytest.mark.parametrize("rola", list(Rola))
def test_krok_bez_tieru_i_bez_zapisu_jest_poprawny_dla_kazdej_roli(rola):
    assert waliduj_definicje({"role": rola.value}) == []


def test_pusta_definicja_nie_ma_naruszen():
    assert waliduj_definicje({}) == []


def test_tier_niezgodny_z_rola_lamie_role_binding():
    naruszenia = waliduj_definicje({"role": "reviewer", "tier": "execution"})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    szczegol = naruszenia[0].szczegol
    assert "execution" in szczegol
    assert "judgment" in szczegol
    assert "reviewer" in szczegol


def test_tier_gatherera_nie_moze_byc_tierem_autora():
    naruszenia = waliduj_definicje({"role": "gatherer", "tier": "judgment"})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]


def test_nieznana_rola_lamie_role_binding():
    naruszenia = waliduj_definicje({"role": "szef"})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    assert "nieznana rola" in naruszenia[0].szczegol


def test_rola_niebedaca_napisem_lamie_role_binding_zamiast_wywalic_walidator():
    naruszenia = waliduj_definicje({"role": 5})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    assert "nieznana rola" in naruszenia[0].szczegol


# --- jawne prawo zapisu przy dowolnej roli ---------------------------------

@pytest.mark.parametrize("rola", list(Rola))
@pytest.mark.parametrize("klucz", KLUCZE_ZAPISU)
@pytest.mark.parametrize("wartosc", [True, False])
def test_jawne_prawo_zapisu_przy_dowolnej_roli_lamie_role_binding(rola, klucz, wartosc):
    """Takze `write: false` - prawo zapisu wynika z roli, nie z osobnego klucza."""
    naruszenia = waliduj_definicje({"role": rola.value, klucz: wartosc})
    wiazanie = [n for n in naruszenia if n.niezmiennik == Niezmiennik.role_binding]
    assert len(wiazanie) == 1
    assert klucz in wiazanie[0].szczegol


def test_klucze_zapisu_to_write_zapis_i_can_write():
    assert set(KLUCZE_ZAPISU) == {"write", "zapis", "can_write"}


def test_author_z_write_true_lamie_role_binding_choc_autor_i_tak_pisze():
    naruszenia = waliduj_definicje({"role": "author", "write": True})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]


def test_author_z_write_false_tez_lamie_role_binding():
    naruszenia = waliduj_definicje({"role": "author", "write": False})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]


def test_brak_roli_z_kluczem_zapisu_lamie_role_binding():
    naruszenia = waliduj_definicje({"write": True})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]


def test_recenzent_z_dopisanym_prawem_zapisu_nie_przechodzi_walidacji():
    naruszenia = waliduj_definicje({"role": "reviewer", "write": True})
    assert Niezmiennik.reviewer_readonly in nazwy(naruszenia)
    assert Niezmiennik.role_binding in nazwy(naruszenia)
    assert "reviewer_readonly" in tekst(naruszenia)


def test_recenzent_z_kilkoma_kluczami_zapisu_ma_jedno_naruszenie_reviewer_readonly():
    naruszenia = waliduj_definicje(
        {"role": "reviewer", "write": True, "zapis": "tak", "can_write": 1}
    )
    assert nazwy(naruszenia).count(Niezmiennik.reviewer_readonly) == 1
    assert nazwy(naruszenia).count(Niezmiennik.role_binding) == 3


@pytest.mark.parametrize("klucz", KLUCZE_ZAPISU)
def test_recenzent_z_kazdym_kluczem_zapisu_lamie_reviewer_readonly(klucz):
    naruszenia = waliduj_definicje({"role": "reviewer", klucz: True})
    assert Niezmiennik.reviewer_readonly in nazwy(naruszenia)


@pytest.mark.parametrize(
    "wartosc",
    [True, "true", "TRUE", " Yes ", "tak", "on", "1", 1, 2, 0.5, -1],
)
def test_wartosc_dajaca_zapis_recenzentowi_lamie_reviewer_readonly(wartosc):
    naruszenia = waliduj_definicje({"role": "reviewer", "write": wartosc})
    assert Niezmiennik.reviewer_readonly in nazwy(naruszenia)


@pytest.mark.parametrize(
    "wartosc",
    [False, "false", "no", "nie", "off", "0", "", "  ", None, 0, 0.0],
)
def test_wartosc_niedajaca_zapisu_recenzentowi_nie_lamie_reviewer_readonly(wartosc):
    naruszenia = waliduj_definicje({"role": "reviewer", "write": wartosc})
    assert Niezmiennik.reviewer_readonly not in nazwy(naruszenia)
    assert Niezmiennik.role_binding in nazwy(naruszenia)  # sam klucz jest juz bledem


def test_napis_false_i_true_daja_rozny_wynik_dla_recenzenta():
    z_true = waliduj_definicje({"role": "reviewer", "write": "true"})
    z_false = waliduj_definicje({"role": "reviewer", "write": "false"})
    assert Niezmiennik.reviewer_readonly in nazwy(z_true)
    assert Niezmiennik.reviewer_readonly not in nazwy(z_false)


def test_recenzent_bez_prawa_zapisu_nie_ma_naruszen():
    assert waliduj_definicje({"role": "reviewer", "tier": "judgment"}) == []


def test_gatherer_z_prawdziwym_zapisem_ma_dokladnie_jedno_naruszenie_role_binding():
    naruszenia = waliduj_definicje({"role": "gatherer", "write": True})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    assert Niezmiennik.reviewer_readonly not in nazwy(naruszenia)


def test_zapis_bez_klucza_roli_nie_jest_mylony_z_recenzentem():
    naruszenia = waliduj_definicje({"write": True})
    assert Niezmiennik.reviewer_readonly not in nazwy(naruszenia)


# --- model zamiast tieru ---------------------------------------------------

def test_krok_z_nazwa_modelu_zamiast_tieru_wskazuje_profil_backendu():
    naruszenia = waliduj_definicje({"role": "author", "model": "claude-opus-4"})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    assert "profilu backendu u runnera" in naruszenia[0].szczegol
    assert "BRO-98" in naruszenia[0].szczegol


def test_krok_z_tierem_bedacym_nazwa_modelu_wskazuje_profil_backendu():
    naruszenia = waliduj_definicje({"role": "author", "tier": "opus"})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    assert "profilu backendu u runnera" in naruszenia[0].szczegol
    assert "BRO-98" in naruszenia[0].szczegol


def test_nazwa_modelu_jest_bledem_takze_gdy_brakuje_roli():
    naruszenia = waliduj_definicje({"model": "sonnet"})
    assert nazwy(naruszenia) == [Niezmiennik.role_binding]
    assert "profilu backendu u runnera" in naruszenia[0].szczegol


# --- no_git_write ----------------------------------------------------------

@pytest.mark.parametrize("klucz", KLUCZE_KOMENDY)
@pytest.mark.parametrize(
    "komenda",
    [
        "git commit -m x",
        "git push",
        "git push origin main",
        "git -C repo push",
        "git -c user.name=a commit -m x",
        "GIT  Commit -m x",
        "git   push",
        "make build && git push origin HEAD",
        ["git", "commit", "-m", "x"],
        ["git", "-C", "repo", "push"],
        ("git", "push"),
        'bash -c "git push"',
        "sh -c 'git commit -m x'",
        "cd r&&git push",
        "cd r;git commit -m x",
        "/usr/bin/git push",
        "(git push)",
        "ls | git commit -m x",
        "ls|git push",
        "git --git-dir .git push",
        "git --work-tree w push",
        "git --namespace n push",
        "echo `git push`",
        "make\ngit push",
        "make\rgit commit -m x",
        "git --no-pager push",
        "git --no-pager -C repo commit -m x",
        ["git", "--no-pager", "push"],
    ],
)
def test_komenda_z_zapisem_do_gita_lamie_no_git_write(klucz, komenda):
    naruszenia = waliduj_definicje({klucz: komenda})
    assert nazwy(naruszenia) == [Niezmiennik.no_git_write]
    assert "git" in naruszenia[0].szczegol


@pytest.mark.parametrize("klucz", KLUCZE_KOMENDY)
@pytest.mark.parametrize(
    "komenda",
    ["git", "git --version", "ls git", "git -C", "git --no-pager", "/usr/bin/git", "", "   "],
)
def test_komenda_konczaca_sie_na_git_nie_wywraca_walidacji(klucz, komenda):
    assert waliduj_definicje({klucz: komenda}) == []


@pytest.mark.parametrize("klucz", KLUCZE_KOMENDY)
@pytest.mark.parametrize(
    "wartosc",
    [5, 0, 1.5, True, None, {"a": 1}, ["git", 1], ["git", "push", 1], [1, 2], b"git push"],
)
def test_niezrozumiala_wartosc_komendy_nie_wywraca_walidacji(klucz, wartosc):
    assert waliduj_definicje({klucz: wartosc}) == []


@pytest.mark.parametrize("klucz", KLUCZE_KOMENDY)
@pytest.mark.parametrize(
    "komenda",
    [
        "git status",
        "git log --oneline",
        "git diff",
        "echo commit",
        "echo push",
        "gitpush",
        "/usr/bin/git log",
        'bash -c "git status"',
        ["git", "status"],
        ["git", "-C", "repo", "log"],
        "pytest -q",
    ],
)
def test_komenda_bez_zapisu_do_gita_nie_lamie_no_git_write(klucz, komenda):
    assert waliduj_definicje({klucz: komenda}) == []


def test_naruszenie_no_git_write_cytuje_komende():
    naruszenia = waliduj_definicje({"command": "git push origin main"})
    assert "git push origin main" in naruszenia[0].szczegol


def test_klucze_komendy_to_command_i_run():
    assert set(KLUCZE_KOMENDY) == {"command", "run"}


# --- approval_required -----------------------------------------------------

@pytest.mark.parametrize("decyzja", [None, "", "   ", "\n\t"])
def test_krok_z_approval_bez_decyzji_czlowieka_nie_przechodzi(decyzja):
    naruszenie = sprawdz_decyzje(True, decyzja)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.approval_required


def test_krok_z_approval_i_decyzja_czlowieka_przechodzi():
    assert sprawdz_decyzje(True, "approved") is None


def test_krok_bez_approval_przechodzi_bez_decyzji():
    assert sprawdz_decyzje(False, None) is None


# --- run_dir_outside_repo --------------------------------------------------

def test_katalog_runu_w_repozytorium_lamie_run_dir_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    run_dir = repo / ".cockpit" / "run-1"
    run_dir.mkdir(parents=True)
    naruszenie = sprawdz_katalog_runu(run_dir, repo)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.run_dir_outside_repo
    assert str(run_dir) in naruszenie.szczegol
    assert str(repo) in naruszenie.szczegol


def test_katalog_runu_rowny_repozytorium_lamie_run_dir_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    naruszenie = sprawdz_katalog_runu(repo, repo)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.run_dir_outside_repo


def test_katalog_runu_poza_repozytorium_jest_poprawny(tmp_path):
    repo = tmp_path / "repo"
    run_dir = tmp_path / "runs" / "run-1"
    repo.mkdir()
    run_dir.mkdir(parents=True)
    assert sprawdz_katalog_runu(run_dir, repo) is None


def test_katalog_obok_repozytorium_o_tym_samym_przedrostku_nie_jest_w_repozytorium(tmp_path):
    repo = tmp_path / "repo"
    obok = tmp_path / "repo-runs"
    repo.mkdir()
    obok.mkdir()
    assert sprawdz_katalog_runu(obok, repo) is None


def test_dowiazanie_do_katalogu_w_repozytorium_nie_ukrywa_run_dir_w_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "wnetrze").mkdir(parents=True)
    dowiazanie = tmp_path / "pozornie-poza"
    dowiazanie.symlink_to(repo / "wnetrze")
    naruszenie = sprawdz_katalog_runu(dowiazanie, repo)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.run_dir_outside_repo


# --- measured_or_null ------------------------------------------------------

def test_koszt_zero_przy_zrodle_unavailable_to_zmyslony_pomiar():
    naruszenie = sprawdz_pomiar(0.0, "unavailable")
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.measured_or_null
    assert naruszenie.szczegol == "koszt 0 przy zrodle 'unavailable' - brak pomiaru zapisz jako None"


async def test_zero_kosztu_bez_pomiaru_odrzuca_zdarzenie_z_tym_samym_komunikatem():
    """Warunek sprawdzany przed dostepem do sesji i runu - atrapy wystarcza."""
    with pytest.raises(NaruszonyNiezmiennik) as blad:
        await zapisz_zdarzenie(None, object(), kind="cost", cost_usd=0.0)  # type: ignore[arg-type]
    assert str(blad.value) == (
        "niezmiennik measured_or_null: koszt 0 przy zrodle 'unavailable' - "
        "brak pomiaru zapisz jako None"
    )


def test_zmierzone_zero_jest_prawdziwym_pomiarem():
    assert sprawdz_pomiar(0.0, "measured") is None


def test_brak_pomiaru_zapisany_jako_brak_jest_poprawny():
    assert sprawdz_pomiar(None, "unavailable") is None


def test_niezerowy_koszt_przy_zrodle_unavailable_nie_jest_zerem_zmyslonym():
    assert sprawdz_pomiar(0.5, "unavailable") is None


# --- evidence_required -----------------------------------------------------

@pytest.mark.parametrize("stdout", ["", "   ", "\n", " \t\r\n "])
def test_pusty_wynik_komendy_to_blad_kroku_nie_zielone_przejscie(stdout):
    naruszenie = dowod_z_komendy(stdout)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.evidence_required


@pytest.mark.parametrize("stdout", ["ok", "  3 passed \n", "0"])
def test_niepusty_wynik_komendy_jest_dowodem(stdout):
    assert dowod_z_komendy(stdout) is None


def test_brak_pliku_werdyktu_to_blad_kroku(tmp_path):
    sciezka = tmp_path / "werdykt.md"
    naruszenie = dowod_z_pliku(sciezka)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.evidence_required
    assert str(sciezka) in naruszenie.szczegol


@pytest.mark.parametrize("tresc", ["", "  \n\t"])
def test_pusty_plik_werdyktu_to_blad_kroku(tmp_path, tresc):
    sciezka = plik(tmp_path, "werdykt.md", tresc)
    naruszenie = dowod_z_pliku(sciezka)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.evidence_required
    assert str(sciezka) in naruszenie.szczegol


def test_katalog_zamiast_pliku_werdyktu_to_blad_kroku(tmp_path):
    katalog = tmp_path / "werdykt.md"
    katalog.mkdir()
    naruszenie = dowod_z_pliku(katalog)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.evidence_required


def test_niepusty_plik_werdyktu_jest_dowodem(tmp_path):
    assert dowod_z_pliku(plik(tmp_path, "werdykt.md", "zatwierdzam\n")) is None


def test_werdykt_w_niepoprawnym_kodowaniu_jest_dowodem(tmp_path):
    sciezka = tmp_path / "werdykt.md"
    sciezka.write_bytes(b"\xff\xfe werdykt")
    assert dowod_z_pliku(sciezka) is None


# --- reviewer_readonly: migawka katalogu -----------------------------------

def test_migawka_zapamietuje_tresc_kazdego_pliku_pod_jego_sciezka(tmp_path):
    plik(tmp_path, "a/b.txt", "tresc")
    assert migawka(tmp_path) == {"a/b.txt": hashlib.sha256(b"tresc").hexdigest()}


def test_katalog_bez_zmian_po_kroku_recenzenta_nie_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a", "1")
    plik(tmp_path, "b/c", "2")
    przed = migawka(tmp_path)
    assert sprawdz_tylko_odczyt(przed, tmp_path) is None


def test_zmiana_w_katalogu_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "zmieniany", "stary")
    plik(tmp_path, "usuwany", "x")
    plik(tmp_path, "staly", "s")
    przed = migawka(tmp_path)
    (tmp_path / "zmieniany").write_text("nowy")
    (tmp_path / "usuwany").unlink()
    plik(tmp_path, "podkatalog/nowy.txt", "n")

    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)

    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.reviewer_readonly
    assert "nowe: podkatalog/nowy.txt" in naruszenie.szczegol
    assert "usuniete: usuwany" in naruszenie.szczegol
    assert "zmienione: zmieniany" in naruszenie.szczegol
    assert "staly" not in naruszenie.szczegol


def test_sam_nowy_plik_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a", "1")
    przed = migawka(tmp_path)
    plik(tmp_path, "b", "2")
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert "nowe: b" in naruszenie.szczegol
    assert "usuniete" not in naruszenie.szczegol
    assert "zmienione" not in naruszenie.szczegol


def test_sam_usuniety_plik_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a", "1")
    plik(tmp_path, "b", "2")
    przed = migawka(tmp_path)
    (tmp_path / "b").unlink()
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert "usuniete: b" in naruszenie.szczegol
    assert "nowe" not in naruszenie.szczegol
    assert "zmienione" not in naruszenie.szczegol


def test_sama_zmiana_tresci_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a", "1")
    przed = migawka(tmp_path)
    (tmp_path / "a").write_text("2")
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert "zmienione: a" in naruszenie.szczegol
    assert "nowe" not in naruszenie.szczegol
    assert "usuniete" not in naruszenie.szczegol


def test_naruszenie_wymienia_sciezki_posortowane(tmp_path):
    plik(tmp_path, "bazowy", "0")
    przed = migawka(tmp_path)
    plik(tmp_path, "z.txt", "1")
    plik(tmp_path, "a.txt", "1")
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert naruszenie.szczegol.index("a.txt") < naruszenie.szczegol.index("z.txt")


def test_zmiany_w_wyjatkach_nie_sa_naruszeniem(tmp_path):
    wyjatki = [Path(".cockpit/_run")]
    plik(tmp_path, "a", "1")
    plik(tmp_path, ".cockpit/skills/x", "s")
    przed = migawka(tmp_path, wyjatki)
    plik(tmp_path, ".cockpit/_run/skills/nowy", "m")
    assert sprawdz_tylko_odczyt(przed, tmp_path, wyjatki) is None


def test_wyjatek_obejmuje_tez_pojedynczy_plik(tmp_path):
    wyjatki = [Path("log")]
    plik(tmp_path, "a", "1")
    plik(tmp_path, "log", "x")
    przed = migawka(tmp_path, wyjatki)
    (tmp_path / "log").write_text("y")
    assert sprawdz_tylko_odczyt(przed, tmp_path, wyjatki) is None


def test_zmiana_poza_wyjatkami_jest_naruszeniem_mimo_wyjatkow(tmp_path):
    wyjatki = [Path(".cockpit/_run")]
    plik(tmp_path, "a", "1")
    plik(tmp_path, ".cockpit/_run/x", "m")
    przed = migawka(tmp_path, wyjatki)
    (tmp_path / "a").write_text("2")
    (tmp_path / ".cockpit" / "_run" / "x").write_text("m2")
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path, wyjatki)
    assert naruszenie is not None
    assert "zmienione: a" in naruszenie.szczegol
    assert "_run" not in naruszenie.szczegol


def test_plik_o_nazwie_wyjatku_w_innym_katalogu_nie_jest_pomijany(tmp_path):
    wyjatki = [Path("log")]
    plik(tmp_path, "inny/log", "1")
    przed = migawka(tmp_path, wyjatki)
    (tmp_path / "inny" / "log").write_text("2")
    assert sprawdz_tylko_odczyt(przed, tmp_path, wyjatki) is not None


def test_migawka_pomija_katalog_git(tmp_path):
    plik(tmp_path, "a", "1")
    plik(tmp_path, ".git/HEAD", "ref")
    plik(tmp_path, ".git/objects/xx", "o")
    assert set(migawka(tmp_path)) == {"a"}


def test_migawka_pomija_plik_git_z_worktree(tmp_path):
    plik(tmp_path, "a", "1")
    plik(tmp_path, ".git", "gitdir: /gdzies")
    assert set(migawka(tmp_path)) == {"a"}


def test_zmiana_w_katalogu_git_nie_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a", "1")
    plik(tmp_path, ".git/index", "i")
    przed = migawka(tmp_path)
    (tmp_path / ".git" / "index").write_text("inny")
    assert sprawdz_tylko_odczyt(przed, tmp_path) is None


def test_zmiana_celu_dowiazania_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "cel1", "1")
    plik(tmp_path, "cel2", "1")
    (tmp_path / "link").symlink_to("cel1")
    przed = migawka(tmp_path)
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("cel2")
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert "zmienione: link" in naruszenie.szczegol


def test_przestawienie_dowiazania_do_katalogu_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a/x", "1")
    plik(tmp_path, "b/x", "1")
    (tmp_path / "docs").symlink_to("a", target_is_directory=True)
    przed = migawka(tmp_path)
    (tmp_path / "docs").unlink()
    (tmp_path / "docs").symlink_to("b", target_is_directory=True)
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.reviewer_readonly
    assert "zmienione: docs" in naruszenie.szczegol


def test_nowe_dowiazanie_do_katalogu_po_kroku_recenzenta_jest_naruszeniem(tmp_path):
    plik(tmp_path, "a/x", "1")
    przed = migawka(tmp_path)
    (tmp_path / "docs").symlink_to("a", target_is_directory=True)
    naruszenie = sprawdz_tylko_odczyt(przed, tmp_path)
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.reviewer_readonly
    assert "nowe: docs" in naruszenie.szczegol


def zwykle_pliki_sasiadow(katalog: Path) -> list[str]:
    """Wiele zwyklych plikow w jednym katalogu - kolejnosc przegladania jest dowolna."""
    nazwy_plikow = [f"sasiad_{i:02d}.txt" for i in range(12)] + ["A_pierwszy", "z_ostatni"]
    for nazwa in nazwy_plikow:
        plik(katalog, nazwa, "stary")
    return nazwy_plikow


def zmien_wszystkie(katalog: Path, nazwy_plikow: list[str]) -> None:
    for nazwa in nazwy_plikow:
        (katalog / nazwa).write_text("nowy")


def zmienione(naruszenie: Naruszenie | None) -> set[str]:
    assert naruszenie is not None
    assert naruszenie.niezmiennik == Niezmiennik.reviewer_readonly
    assert naruszenie.szczegol.startswith("zmienione: ")
    return set(naruszenie.szczegol.removeprefix("zmienione: ").split(", "))


def test_pominiety_plik_git_nie_ukrywa_zmian_w_sasiednich_plikach(tmp_path):
    nazwy_plikow = zwykle_pliki_sasiadow(tmp_path)
    plik(tmp_path, ".git", "gitdir: /gdzies")
    przed = migawka(tmp_path)
    zmien_wszystkie(tmp_path, nazwy_plikow)
    assert zmienione(sprawdz_tylko_odczyt(przed, tmp_path)) == set(nazwy_plikow)


def test_pominiety_wyjatek_nie_ukrywa_zmian_w_sasiednich_plikach(tmp_path):
    nazwy_plikow = zwykle_pliki_sasiadow(tmp_path)
    plik(tmp_path, "wyjatek.log", "x")
    wyjatki = [Path("wyjatek.log")]
    przed = migawka(tmp_path, wyjatki)
    zmien_wszystkie(tmp_path, nazwy_plikow)
    assert zmienione(sprawdz_tylko_odczyt(przed, tmp_path, wyjatki)) == set(nazwy_plikow)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="brak kolejek FIFO na tej platformie")
def test_pominieta_kolejka_fifo_nie_ukrywa_zmian_w_sasiednich_plikach(tmp_path):
    nazwy_plikow = zwykle_pliki_sasiadow(tmp_path)
    os.mkfifo(tmp_path / "kolejka")
    przed = migawka(tmp_path)
    assert "kolejka" not in przed
    zmien_wszystkie(tmp_path, nazwy_plikow)
    assert zmienione(sprawdz_tylko_odczyt(przed, tmp_path)) == set(nazwy_plikow)


def git(repo: Path, *argumenty: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *argumenty],
        check=True, capture_output=True, text=True,
    )


def test_odczyt_git_status_przez_recenzenta_nie_jest_naruszeniem(tmp_path):
    """`git status` odswieza .git/index, choc nic nie zmienia w pracy - stad .git/ jest pomijany."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    sledzony = plik(repo, "plik.txt", "tresc\n")
    git(repo, "add", "plik.txt")
    git(repo, "commit", "-q", "-m", "start")
    # zmieniony mtime przy tej samej tresci: index jest nieswiezy, status go odswiezy
    stary = sledzony.stat().st_mtime
    os.utime(sledzony, (stary + 100, stary + 100))
    indeks = repo / ".git" / "index"
    indeks_przed = hashlib.sha256(indeks.read_bytes()).hexdigest()

    przed = migawka(repo)
    git(repo, "status")

    assert hashlib.sha256(indeks.read_bytes()).hexdigest() != indeks_przed
    assert sprawdz_tylko_odczyt(przed, repo) is None


def test_zmiana_pliku_roboczego_w_repozytorium_git_jest_naruszeniem(tmp_path):
    """Przeciwienstwo odczytu: pomijanie .git/ nie ukrywa zmian w samej pracy."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    sledzony = plik(repo, "plik.txt", "a\n")
    przed = migawka(repo)
    sledzony.write_text("b\n")
    assert sprawdz_tylko_odczyt(przed, repo) is not None


# --- Naruszenie i komunikaty -----------------------------------------------

def test_naruszenie_bez_szczegolu_ma_sama_nazwe_niezmiennika():
    assert str(Naruszenie(Niezmiennik.no_git_write, "")) == "niezmiennik no_git_write"


def test_naruszenie_ze_szczegolem_laczy_nazwe_i_szczegol():
    n = Naruszenie(Niezmiennik.approval_required, "brak decyzji")
    assert str(n) == "niezmiennik approval_required: brak decyzji"


def test_naruszenie_nie_pozwala_zmienic_szczegolu_po_fakcie():
    n = Naruszenie(Niezmiennik.approval_required, "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        n.szczegol = "y"  # type: ignore[misc]


def przyklady_naruszen(tmp_path: Path) -> dict[Niezmiennik, Callable[[], Any]]:
    """Dla kazdego niezmiennika wejscie, ktore go lamie."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def odczyt_zmieniony() -> Naruszenie | None:
        sciezka = plik(tmp_path / "praca", "plik", "1")
        przed = migawka(tmp_path / "praca")
        sciezka.write_text("2")
        return sprawdz_tylko_odczyt(przed, tmp_path / "praca")

    return {
        Niezmiennik.role_binding: lambda: waliduj_definicje({"role": "author", "tier": "retrieval"}),
        Niezmiennik.reviewer_readonly: odczyt_zmieniony,
        Niezmiennik.approval_required: lambda: sprawdz_decyzje(True, None),
        Niezmiennik.no_git_write: lambda: waliduj_definicje({"command": "git push"}),
        Niezmiennik.run_dir_outside_repo: lambda: sprawdz_katalog_runu(repo / "run", repo),
        Niezmiennik.measured_or_null: lambda: sprawdz_pomiar(0.0, "unavailable"),
        Niezmiennik.evidence_required: lambda: dowod_z_komendy(""),
    }


def test_tabela_przykladow_naruszen_obejmuje_wszystkie_niezmienniki(tmp_path):
    assert set(przyklady_naruszen(tmp_path)) == WSZYSTKIE


@pytest.mark.parametrize("niezmiennik", sorted(WSZYSTKIE))
def test_komunikat_kazdego_naruszenia_zawiera_nazwe_niezmiennika(tmp_path, niezmiennik):
    wynik = przyklady_naruszen(tmp_path)[niezmiennik]()
    assert wynik, f"przyklad dla {niezmiennik} nie zlamal niczego"
    naruszenia = [wynik] if isinstance(wynik, Naruszenie) else list(wynik)
    assert niezmiennik in nazwy(naruszenia)
    assert f"niezmiennik {niezmiennik.value}" in tekst(naruszenia)

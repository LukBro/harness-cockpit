"""HAR-176: krok systemowy `system: diff` - dowod dla recenzentow (TDD, przed implementacja).

Silnik bez LLM zbiera diff worktree wzgledem `base_commit` i zapisuje w katalogu runu
`<krok>/zmiana.diff` oraz `<krok>/zmienione-linie.json`. Skille recenzji dostaja je przez `consumes`.
Pusty diff to brak dowodu (`evidence_required`): krok nieudany, recenzenci pominieci z `brak_wejscia`.

Prawdziwy kod: `zmienione_linie` (czysta funkcja na recznie napisanych diffach), `wykonaj_diff`
na prawdziwym repozytorium git w tmp_path (oczekiwane zakresy wyliczone recznie, nie funkcja pod
testem) i `wykonaj_run` z wstrzyknietym agentem (`Szpieg` z `test_silnik_runu`, ktory przed
krokiem moze zmienic worktree). Nazwy testow w jezyku dziedziny (CLAUDE.md).
"""
import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from cockpit.services.kroki_systemowe import KROKI_SYSTEMOWE, OpisKrokuSystemowego
from runner.krok_diff import PLIK_DIFFU, PLIK_LINII, wykonaj_diff, zmienione_linie
from runner.krok import WynikKroku
from runner.odbior import zbierz_diff
from test_silnik_runu import RUN_ID, K, Szpieg, _git, indeks, jedno, rodzaje, swiat, uruchom

# --- zmienione_linie: czysta funkcja na diffach napisanych recznie -------------------------


def _zmiana(plik: str, *hunki: str) -> str:
    """Diff jednego zmodyfikowanego pliku sledzonego."""
    return (
        f"diff --git a/{plik} b/{plik}\nindex 1111111..2222222 100644\n"
        f"--- a/{plik}\n+++ b/{plik}\n" + "".join(hunki)
    )


def test_nazwy_artefaktow_kroku_diff_to_zmiana_diff_i_zmienione_linie_json():
    assert PLIK_DIFFU == "zmiana.diff"
    assert PLIK_LINII == "zmienione-linie.json"


def test_opis_kroku_systemowego_jest_niezmienny_po_utworzeniu():
    opis = OpisKrokuSystemowego(produces=("a.txt",))

    with pytest.raises(FrozenInstanceError):
        opis.produces = ("b.txt",)  # type: ignore[misc]

    assert opis.produces == ("a.txt",)


def test_opis_kroku_systemowego_wymaga_podania_artefaktow_ktore_produkuje():
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'produces'"):
        OpisKrokuSystemowego()  # type: ignore[call-arg]


def test_rejestr_krokow_systemowych_jest_tylko_do_odczytu():
    with pytest.raises(TypeError, match="does not support item assignment"):
        KROKI_SYSTEMOWE["x"] = OpisKrokuSystemowego(produces=())  # type: ignore[index]

    assert "x" not in KROKI_SYSTEMOWE


def test_diff_bez_zmian_nie_ma_zadnego_pliku_ze_zmienionymi_liniami():
    assert zmienione_linie("") == []


def test_modyfikacja_w_srodku_pliku_liczy_numer_od_kontekstu_a_usunieta_linia_go_nie_przesuwa():
    diff = _zmiana(
        "f.py",
        "@@ -10,7 +10,7 @@ def x():\n"
        " k1\n k2\n k3\n-stara\n+nowa\n k4\n k5\n k6\n",
    )

    # k1=10, k2=11, k3=12, nowa=13: bez przesuniecia kontekstem wyszloby 10, z `-` jako 14
    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[13, 13]]}]


def test_dwa_rozdzielne_zakresy_w_jednym_hunku_daja_dwa_zakresy_a_kolejne_dodane_linie_jeden():
    diff = _zmiana(
        "f.py",
        "@@ -1,5 +1,7 @@\n a\n+b1\n+b2\n c\n d\n-e\n+e2\n f\n",
    )

    # a=1, b1=2, b2=3, c=4, d=5, e2=6 (usuniete `e` nie liczy sie)
    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[2, 3], [6, 6]]}]


def test_dwa_hunki_jednego_pliku_licza_numery_od_wlasnych_naglowkow():
    diff = _zmiana(
        "f.py",
        "@@ -1,3 +1,4 @@\n a\n+b\n c\n d\n"
        "@@ -20,3 +21,4 @@\n x\n y\n+z\n w\n",
    )

    # hunk drugi startuje od 21: x=21, y=22, z=23
    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[2, 2], [23, 23]]}]


def test_naglowek_hunka_bez_licznika_oznacza_jedna_linie():
    diff = _zmiana("f.py", "@@ -5 +5 @@\n-x\n+y\n")

    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[5, 5]]}]


def test_hunk_z_samym_dodaniem_po_linii_piatej_zaczyna_sie_od_szostej():
    diff = _zmiana("f.py", "@@ -5,0 +6 @@\n+z\n")

    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[6, 6]]}]


def test_nowy_plik_jest_w_calosci_jednym_zakresem_od_pierwszej_do_ostatniej_linii():
    diff = (
        "diff --git a/nowy.py b/nowy.py\nnew file mode 100644\nindex 0000000..3333333\n"
        "--- /dev/null\n+++ b/nowy.py\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n"
    )

    assert zmienione_linie(diff) == [{"plik": "nowy.py", "linie": [[1, 3]]}]


def test_nowy_plik_z_jedna_linia_ma_zakres_od_jednego_do_jednego():
    diff = (
        "diff --git a/jeden.py b/jeden.py\nnew file mode 100644\nindex 0000000..3333333\n"
        "--- /dev/null\n+++ b/jeden.py\n@@ -0,0 +1 @@\n+a\n"
    )

    assert zmienione_linie(diff) == [{"plik": "jeden.py", "linie": [[1, 1]]}]


def test_usuniety_plik_ma_nazwe_z_naglowka_starej_wersji_i_puste_linie():
    diff = (
        "diff --git a/stary.py b/stary.py\ndeleted file mode 100644\nindex 3333333..0000000\n"
        "--- a/stary.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )

    assert zmienione_linie(diff) == [{"plik": "stary.py", "linie": []}]


def test_zmieniony_plik_binarny_jest_na_liscie_z_pustymi_liniami():
    diff = (
        "diff --git a/obraz.png b/obraz.png\nindex 1111111..2222222 100644\n"
        "Binary files a/obraz.png and b/obraz.png differ\n"
    )

    assert zmienione_linie(diff) == [{"plik": "obraz.png", "linie": []}]


def test_nowy_plik_binarny_ma_nazwe_z_nowej_wersji_i_puste_linie():
    diff = (
        "diff --git a/img/nowy.png b/img/nowy.png\nnew file mode 100644\nindex 0000000..2222222\n"
        "Binary files /dev/null and b/img/nowy.png differ\n"
    )

    assert zmienione_linie(diff) == [{"plik": "img/nowy.png", "linie": []}]


def test_marker_braku_konca_linii_nie_liczy_sie_jako_linia_pliku():
    diff = _zmiana(
        "f.py",
        "@@ -1 +1,2 @@\n-x\n\\ No newline at end of file\n+y\n+z\n",
    )

    # gdyby marker byl kontekstem, `y` mialoby numer 2 i wyszloby [[2, 3]]
    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[1, 2]]}]


def test_linie_tresci_wygladajace_jak_naglowki_plikow_sa_zwyklymi_liniami_hunka():
    # usunieta linia `-- c` wyglada w diffie jak `--- c`, dodana `++ d` jak `+++ d`
    diff = _zmiana("f.py", "@@ -1,2 +1,3 @@\n a\n--- c\n+++ d\n+e\n")

    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[2, 3]]}]


def test_pliki_w_wyniku_sa_posortowane_po_nazwie_a_licznik_linii_zaczyna_sie_w_kazdym_od_nowa():
    diff = (
        _zmiana("z.py", "@@ -40,2 +40,3 @@\n a\n+b\n c\n")
        + _zmiana("a.py", "@@ -1,2 +1,3 @@\n a\n+b\n c\n")
    )

    assert zmienione_linie(diff) == [
        {"plik": "a.py", "linie": [[2, 2]]},
        {"plik": "z.py", "linie": [[41, 41]]},
    ]


def test_hunk_przed_naglowkiem_jakiegokolwiek_pliku_jest_pomijany_bez_bledu():
    # wejscie spoza git (obciety diff): bez pliku nie ma do czego dopisac zakresu
    assert zmienione_linie("@@ -1 +1 @@\n-a\n+b\n") == []


def test_hunk_przed_naglowkiem_pliku_nie_psuje_zakresow_nastepnego_pliku():
    diff = "@@ -1 +1 @@\n-a\n+b\n" + _zmiana("f.py", "@@ -3 +3 @@\n-x\n+y\n")

    assert zmienione_linie(diff) == [{"plik": "f.py", "linie": [[3, 3]]}]


def test_katalog_o_nazwie_b_nie_jest_ucinany_z_nazwy_pliku():
    diff = _zmiana("b/x.py", "@@ -1 +1 @@\n-a\n+b\n")

    assert zmienione_linie(diff) == [{"plik": "b/x.py", "linie": [[1, 1]]}]


def test_zmiana_pliku_i_usuniecie_innego_w_jednym_diffie_daja_oba_wpisy():
    diff = _zmiana("k.py", "@@ -1 +1 @@\n-a\n+b\n") + (
        "diff --git a/stary.py b/stary.py\ndeleted file mode 100644\nindex 3333333..0000000\n"
        "--- a/stary.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    )

    assert zmienione_linie(diff) == [
        {"plik": "k.py", "linie": [[1, 1]]},
        {"plik": "stary.py", "linie": []},
    ]


# --- wykonaj_diff na prawdziwym repozytorium git ----------------------------------------------


def _pliki_pod(korzen: Path) -> list[str]:
    """Wszystkie wpisy pod katalogiem (takze pliki tymczasowe); brak katalogu = pusto."""
    if not korzen.exists():
        return []
    return sorted(str(p.relative_to(korzen)) for p in korzen.rglob("*"))


@pytest.fixture
def repo_ze_zmianami(tmp_path: Path) -> tuple[Path, str]:
    """Repo z commitem bazowym i trzema zmianami: dwie w sledzonych, dwa nowe nieśledzone pliki."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("".join(f"l{i}\n" for i in range(1, 11)), encoding="utf-8")
    (repo / "b.txt").write_text("x\ny\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baza")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    nowe = [f"l{i}\n" for i in range(1, 11)]
    nowe[1] = "L2\n"
    nowe[8] = "L9\n"
    (repo / "a.py").write_text("".join(nowe), encoding="utf-8")
    (repo / "b.txt").write_text("x\ny\nz\n", encoding="utf-8")
    (repo / "c").mkdir()
    (repo / "c" / "nowy.txt").write_text("p\nq\nr\n", encoding="utf-8")
    (repo / "zażółć.txt").write_text("1\n2\n", encoding="utf-8")
    return repo, sha


OCZEKIWANE_ZAKRESY = [
    {"plik": "a.py", "linie": [[2, 2], [9, 9]]},
    {"plik": "b.txt", "linie": [[3, 3]]},
    {"plik": "c/nowy.txt", "linie": [[1, 3]]},
    {"plik": "zażółć.txt", "linie": [[1, 2]]},
]


async def test_krok_diff_zapisuje_zmiana_diff_rowny_wynikowi_zbierania_diffu_runu(
    tmp_path, repo_ze_zmianami
):
    repo, sha = repo_ze_zmianami
    katalog = tmp_path / "run" / "diff"

    wynik = await wykonaj_diff(repo, sha, katalog)

    assert wynik == WynikKroku("zakonczony", None, 0)
    oczekiwany = zbierz_diff(repo, sha)
    assert oczekiwany.strip()
    assert (katalog / "zmiana.diff").read_bytes() == oczekiwany.encode("utf-8")


async def test_krok_diff_zawiera_w_zmiana_diff_zmiany_sledzone_i_nowy_plik_nieslediony(
    tmp_path, repo_ze_zmianami
):
    repo, sha = repo_ze_zmianami
    katalog = tmp_path / "run" / "diff"

    await wykonaj_diff(repo, sha, katalog)

    tekst = (katalog / "zmiana.diff").read_text(encoding="utf-8")
    assert "+++ b/a.py" in tekst
    assert "+++ b/b.txt" in tekst
    assert "+++ b/c/nowy.txt" in tekst
    assert "+++ b/zażółć.txt" in tekst


async def test_zmienione_linie_json_ma_dokladnie_wyliczone_zakresy_zmian_i_nowych_plikow(
    tmp_path, repo_ze_zmianami
):
    repo, sha = repo_ze_zmianami
    katalog = tmp_path / "run" / "diff"

    await wykonaj_diff(repo, sha, katalog)

    tekst = (katalog / "zmienione-linie.json").read_text(encoding="utf-8")
    assert json.loads(tekst) == OCZEKIWANE_ZAKRESY
    # polskie znaki zapisane doslownie, wciecie 2 spacje
    assert tekst == json.dumps(OCZEKIWANE_ZAKRESY, ensure_ascii=False, indent=2)


async def test_krok_diff_zostawia_w_katalogu_kroku_tylko_dwa_artefakty_bez_plikow_tymczasowych(
    tmp_path, repo_ze_zmianami
):
    repo, sha = repo_ze_zmianami
    katalog = tmp_path / "run" / "diff"

    await wykonaj_diff(repo, sha, katalog)

    assert _pliki_pod(katalog) == ["zmiana.diff", "zmienione-linie.json"]


async def test_czysty_worktree_konczy_krok_diff_porazka_evidence_required_bez_plikow(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baza")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    katalog = tmp_path / "run" / "diff"

    wynik = await wykonaj_diff(repo, sha, katalog)

    assert wynik == WynikKroku(
        "nieudany", "niezmiennik evidence_required: brak zmian do recenzji", None)
    assert _pliki_pod(katalog) == []


async def test_diff_z_samych_bialych_znakow_jest_pusty_i_konczy_krok_porazka_bez_plikow(
    tmp_path, monkeypatch
):
    # prawdziwy git nie zwroci diffu z samych bialych znakow, wiec tylko ta galaz dostaje zaslepke
    biale = lambda worktree, base_commit: " \n\t\n"  # noqa: E731
    monkeypatch.setattr("runner.odbior.zbierz_diff", biale)
    monkeypatch.setattr("runner.krok_diff.zbierz_diff", biale, raising=False)
    katalog = tmp_path / "run" / "diff"

    wynik = await wykonaj_diff(tmp_path, "0" * 40, katalog)

    assert wynik == WynikKroku(
        "nieudany", "niezmiennik evidence_required: brak zmian do recenzji", None)
    assert _pliki_pod(katalog) == []


async def test_base_commit_ktory_nie_jest_sha_konczy_krok_diff_trescia_bledu_odbioru_bez_plikow(
    tmp_path, repo_ze_zmianami
):
    repo, _ = repo_ze_zmianami
    katalog = tmp_path / "run" / "diff"

    wynik = await wykonaj_diff(repo, "HEAD", katalog)

    assert wynik == WynikKroku("nieudany", "base_commit nie jest SHA", None)
    assert _pliki_pod(katalog) == []


async def test_katalog_bez_repozytorium_konczy_krok_diff_trescia_bledu_gita_bez_plikow(tmp_path):
    pusty = tmp_path / "nie-repo"
    pusty.mkdir()
    katalog = tmp_path / "run" / "diff"

    wynik = await wykonaj_diff(pusty, "a" * 40, katalog)

    assert wynik == WynikKroku("nieudany", "git nie zebral diffu runu", None)
    assert _pliki_pod(katalog) == []


async def test_katalog_kroku_w_repozytorium_konczy_krok_diff_naruszeniem_run_dir_outside_repo(
    repo_ze_zmianami
):
    repo, sha = repo_ze_zmianami
    katalog = repo / "wyniki" / "diff"

    wynik = await wykonaj_diff(repo, sha, katalog)

    assert wynik == WynikKroku(
        "nieudany",
        f"niezmiennik run_dir_outside_repo: katalog runu {katalog} lezy w repozytorium {repo}",
        None,
    )
    assert _pliki_pod(repo / "wyniki") == []


async def test_blad_zapisu_artefaktow_konczy_krok_diff_porazka_z_przyczyna_systemu_bez_kodu(
    tmp_path, repo_ze_zmianami
):
    repo, sha = repo_ze_zmianami
    blokada = tmp_path / "blokada"
    blokada.write_text("to jest plik, nie katalog", encoding="utf-8")

    wynik = await wykonaj_diff(repo, sha, blokada / "diff")

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można zapisać artefaktów kroku diff: ")
    assert "blokada" in wynik.powod
    assert wynik.kotwice == ()


# --- silnik: krok systemowy w runie --------------------------------------------------------------


@dataclass
class SzpiegZmieniajacyDrzewo(Szpieg):
    """Agent bez procesu, ktory przed zwroceniem wyniku modyfikuje worktree (jak autor)."""

    zmiany: dict[str, Callable[[Path], None]] = field(default_factory=dict)

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, **dodatkowe):
        zmiana = self.zmiany.get(spec.step_id)
        if zmiana is not None:
            zmiana(katalog)
        return await super().__call__(
            spec, polecenie, katalog, emit, katalog_runu=katalog_runu, **dodatkowe)


def zmien_drzewo(katalog: Path) -> None:
    """Jak autor: dopisuje dwie linie do sledzonego README.md i tworzy nowy plik."""
    (katalog / "README.md").write_text("repo\ndwa\ntrzy\n", encoding="utf-8")
    (katalog / "nowy.txt").write_text("a\nb\n", encoding="utf-8")


WORKFLOW_Z_DIFFEM = """\
name: w
steps:
  autor: {skill: s_autor}
  diff: {system: diff, needs: [autor]}
  rec: {skill: s_rec, needs: [diff]}
"""

SKILLE_Z_DIFFEM = {
    "autor": {},
    "rec": {"role": "reviewer", "consumes": ["zmiana.diff"], "produces": ["zastrzezenia.md"]},
}


def swiat_z_diffem(tmp_path: Path, tresc: str = WORKFLOW_Z_DIFFEM, skille=None):
    return swiat(
        tmp_path, [K("autor"), K("rec")], tresc=tresc,
        opcje_skilli=skille if skille is not None else SKILLE_Z_DIFFEM)


async def test_run_z_krokiem_diff_zapisuje_oba_artefakty_w_katalogu_runu_pod_id_kroku(tmp_path):
    s = swiat_z_diffem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    await uruchom(s, agent)

    katalog = s.katalog_runu / "diff"
    assert _pliki_pod(katalog) == ["zmiana.diff", "zmienione-linie.json"]
    base = _git(s.repo, "rev-parse", "HEAD").strip()
    assert (katalog / "zmiana.diff").read_bytes() == zbierz_diff(s.worktree, base).encode("utf-8")
    assert json.loads((katalog / "zmienione-linie.json").read_text(encoding="utf-8")) == [
        {"plik": "README.md", "linie": [[2, 3]]},
        {"plik": "nowy.txt", "linie": [[1, 2]]},
    ]


async def test_krok_diff_nie_uruchamia_agenta_a_zdarzenia_ma_jak_kazdy_krok(tmp_path):
    s = swiat_z_diffem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    zdarzenia = await uruchom(s, agent)

    assert agent.kroki == ["autor", "rec"]
    start = jedno(zdarzenia, "step_started", "diff")
    koniec = jedno(zdarzenia, "step_finished", "diff")
    assert (start["run_id"], start["agent_id"], koniec["agent_id"]) == (RUN_ID, "diff", "diff")
    assert koniec["text"] == ""
    assert indeks(zdarzenia, "step_finished", "autor") < indeks(zdarzenia, "step_started", "diff")
    assert indeks(zdarzenia, "step_finished", "diff") < indeks(zdarzenia, "step_started", "rec")
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_recenzent_dostaje_diff_jako_sciezke_bezwzgledna_a_wejscie_z_kroku_skillowego_nazwa(
    tmp_path
):
    tresc = (
        "name: w\nsteps:\n"
        "  autor: {skill: s_autor}\n"
        "  roznica: {system: diff, needs: [autor]}\n"
        "  rec: {skill: s_rec, needs: [autor, roznica]}\n"
    )
    skille = {
        "autor": {"produces": ["plan.md"]},
        "rec": {
            "role": "reviewer",
            "consumes": ["plan.md", "zmiana.diff", "zmienione-linie.json"],
            "produces": ["zastrzezenia.md"],
        },
    }
    s = swiat_z_diffem(tmp_path, tresc, skille)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    await uruchom(s, agent)

    katalog = s.katalog_runu / "roznica"
    assert agent.spec("rec").wejscia == [
        "plan.md", str(katalog / "zmiana.diff"), str(katalog / "zmienione-linie.json")]
    assert Path(agent.spec("rec").wejscia[1]).is_absolute()
    assert Path(agent.spec("rec").wejscia[1]).is_file()


async def test_pusty_diff_konczy_krok_diff_porazka_bez_plikow_a_run_nie_wisi(tmp_path):
    s = swiat_z_diffem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo()       # autor niczego nie zmienia

    zdarzenia = await uruchom(s, agent)

    nieudany = jedno(zdarzenia, "step_failed", "diff")
    assert nieudany["text"] == "niezmiennik evidence_required: brak zmian do recenzji"
    assert nieudany["agent_id"] == "diff"
    assert _pliki_pod(s.katalog_runu / "diff") == []
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "diff"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_pusty_diff_pomija_recenzenta_z_powodem_brak_wejscia_a_nie_zaleznosc_nieudana(
    tmp_path
):
    s = swiat_z_diffem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    pominiety = jedno(zdarzenia, "step_skipped", "rec")
    assert pominiety["text"] == "brak_wejscia: brak wejścia: zmiana.diff z kroku diff"
    assert agent.kroki == ["autor"]
    assert "step_started" not in [z["kind"] for z in zdarzenia if z["step_id"] == "rec"]


async def test_krok_systemowy_bez_wykonawcy_konczy_sie_porazka_z_nazwa_a_recenzent_jest_pominiety(
    tmp_path, monkeypatch
):
    # rejestry rozjechane: opis kroku `diff` istnieje (walidacja przechodzi), wykonawcy brak
    monkeypatch.setattr("runner.silnik.WYKONAWCY_SYSTEMOWI", {})
    s = swiat_z_diffem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo})

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_failed", "diff")["text"] == "nieznany krok systemowy: diff"
    assert _pliki_pod(s.katalog_runu / "diff") == []
    assert agent.kroki == ["autor"]
    assert jedno(zdarzenia, "step_skipped", "rec")["text"] == (
        "brak_wejscia: brak wejścia: zmiana.diff z kroku diff")
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_recenzent_niekonsumujacy_diffu_jest_przy_pustym_diffie_pominiety_jako_zaleznosc_nieudana(
    tmp_path
):
    # recenzent bez `consumes` to krok z nieudanym przodkiem, ktory nic z niego nie bierze
    skille = {"autor": {}, "rec": {"role": "reviewer", "produces": ["zastrzezenia.md"]}}
    s = swiat_z_diffem(tmp_path, skille=skille)
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    pominiety = jedno(zdarzenia, "step_skipped", "rec")
    assert pominiety["text"] == "zaleznosc_nieudana: zależność nieudana: diff"
    assert rodzaje(zdarzenia).count("step_skipped") == 1


# --- szukanie producenta artefaktu wsrod przodkow (silnik) -----------------------------------------


class KrokZLicznikiem:
    """Krok workflow, ktory liczy odczyty swojego `needs` (ile razy silnik go przeglada)."""

    def __init__(self, id: str, needs: list[str], system: str | None = None) -> None:
        self.id = id
        self.skill = None if system is not None else f"s_{id}"
        self.system = system
        self._needs = needs
        self.odczyty = 0

    @property
    def needs(self) -> list[str]:
        self.odczyty += 1
        return self._needs


def _przygotowany(*kroki: KrokZLicznikiem) -> SimpleNamespace:
    return SimpleNamespace(workflow=SimpleNamespace(kroki=list(kroki)))


def test_producent_diffu_jest_znaleziony_mimo_nieznanego_kroku_wsrod_zaleznosci():
    from runner.silnik import _krok_producenta

    p = _przygotowany(
        KrokZLicznikiem("diff", [], system="diff"), KrokZLicznikiem("rec", ["nikt", "diff"]))

    assert _krok_producenta(p, "rec", "zmiana.diff") == "diff"


def test_producent_diffu_jest_znaleziony_mimo_powtorzonej_zaleznosci_przed_nim():
    from runner.silnik import _krok_producenta

    p = _przygotowany(
        KrokZLicznikiem("z", []),
        KrokZLicznikiem("diff", [], system="diff"),
        KrokZLicznikiem("rec", ["z", "z", "diff"]),
    )

    assert _krok_producenta(p, "rec", "zmiana.diff") == "diff"


def test_krok_producenta_nie_ma_producenta_artefaktu_ktorego_nie_produkuje_zaden_przodek():
    from runner.silnik import _krok_producenta

    p = _przygotowany(
        KrokZLicznikiem("diff", [], system="diff"), KrokZLicznikiem("rec", ["diff"]))

    assert _krok_producenta(p, "rec", "plan.md") is None


def test_przodek_wspolny_dla_dwoch_galezi_romba_jest_przegladany_tylko_raz():
    from runner.silnik import _krok_producenta

    z = KrokZLicznikiem("z", [])
    p = _przygotowany(
        z,
        KrokZLicznikiem("x", ["z"]),
        KrokZLicznikiem("y", ["z"]),
        KrokZLicznikiem("rec", ["x", "y"]),
    )

    assert _krok_producenta(p, "rec", "zmiana.diff") is None
    assert z.odczyty == 1

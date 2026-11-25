"""HAR-242 (G3): limity pól wejścia zgodne z kolumnami `String(n)` i walidacja zdarzeń w runnerze.

Pole modelu wejścia bez `max_length` przy kolumnie `String(36)` to 500 (`DataError` z PostgreSQL)
zamiast 422, a zdarzenie z takim polem przepada po stronie kokpitu mimo prób runnera. Sprawdza to
`tools/check_granica.py` (wzorzec: `tests/test_lekcje.py`): na atrapach (model pydantic +
tabela SQLAlchemy), na prawdziwym repozytorium i jako prawdziwy proces z kodami wyjścia.
Runner waliduje zdarzenie tym samym `EventIn` przed buforem i POST: wartość o jeden ponad
limit to `NiewaznePoleZdarzenia`, a zdarzenie nie trafia ani do bufora, ani na sieć.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal, Optional

import httpx
import pytest
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from procesy import uruchom_proces_sync, zakoncz_proces_sync
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cockpit.api.schematy import EventIn, WpisManifestuIn
from runner import client as modul_klienta
from runner.bufor import BrakMiejsca, Bufor
from runner.client import NiewaznePoleZdarzenia, Runner

KORZEN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KORZEN / "tools"))
import check_granica  # noqa: E402
from check_granica import BladNarzedzia, sprawdz  # noqa: E402

NARZEDZIE = KORZEN / "tools" / "check_granica.py"
LIMIT = 60
KLIENT_RUNNERA = KORZEN / "src" / "runner" / "client.py"
KLIENT_POPRAWNY = "from x import EventIn\n\n\ndef wyslij(kw):\n    EventIn.model_validate(kw)\n"
TEKST_CZYSTO = "limity pól wejścia zgodne z kolumnami - w porządku\n"


# --- atrapy: tabela SQLAlchemy i modele pydantic ---------------------------------------------


class Baza(DeclarativeBase):
    pass


class Tabela(Baza):
    __tablename__ = "tabela_atrapa"

    id: Mapped[int] = mapped_column(primary_key=True)
    nazwa: Mapped[str] = mapped_column(String(36))
    dluga: Mapped[str] = mapped_column(String(40))
    opis: Mapped[str] = mapped_column(Text)
    bez_dlugosci: Mapped[str] = mapped_column(String())
    licznik: Mapped[int] = mapped_column(Integer)


class BezLimitu(BaseModel):
    nazwa: str


class LimitRowny(BaseModel):
    nazwa: str = Field(max_length=36)


class LimitMniejszy(BaseModel):
    nazwa: str = Field(max_length=10)


class LimitWiekszy(BaseModel):
    nazwa: str = Field(max_length=50)


class TylkoMinimum(BaseModel):
    nazwa: str = Field(min_length=1)


class MinimumIMaksimum(BaseModel):
    nazwa: str = Field(min_length=1, max_length=36)


class DwaLimitySzerszyPierwszy(BaseModel):
    # pydantic trzyma oba limity; obowiazuje wezszy (36), wiec przy kolumnie String(40) jest czysto
    dluga: Annotated[str, Field(max_length=50), Field(max_length=36)]


class DwaLimityPonadKolumne(BaseModel):
    dluga: Annotated[str, Field(max_length=50), Field(max_length=45)]


class PolaBezKolumnyStringN(BaseModel):
    # kazde z pol nie ma limitu, ale zadne nie odpowiada kolumnie `String(n)`
    nazwa: str = Field(max_length=36)
    nieznane: str          # brak takiej kolumny
    licznik: int           # kolumna Integer
    opis: str              # kolumna Text (String bez dlugosci)
    bez_dlugosci: str      # String() bez dlugosci


class TylkoPolaBezDlugosci(BaseModel):
    opis: str
    bez_dlugosci: str


class TylkoLicznik(BaseModel):
    licznik: int


class DwaPolaNaruszone(BaseModel):
    nazwa: str
    dluga: str = Field(max_length=41)


@pytest.fixture
def klient(tmp_path: Path) -> Path:
    plik = tmp_path / "client.py"
    plik.write_text(KLIENT_POPRAWNY, encoding="utf-8")
    return plik


# --- sprawdz: limity pol -----------------------------------------------------------------------


def test_pole_bez_max_length_przy_kolumnie_string_36_jest_naruszeniem_z_nazwa_pola(klient):
    assert sprawdz([(BezLimitu, Tabela)], klient) == ["BezLimitu.nazwa: String(36) bez max_length"]


def test_pole_z_max_length_rownym_dlugosci_kolumny_jest_czyste(klient):
    assert sprawdz([(LimitRowny, Tabela)], klient) == []


def test_pole_z_max_length_mniejszym_niz_kolumna_jest_czyste(klient):
    assert sprawdz([(LimitMniejszy, Tabela)], klient) == []


def test_pole_z_max_length_wiekszym_niz_kolumna_jest_naruszeniem_z_obiema_wartosciami(klient):
    assert sprawdz([(LimitWiekszy, Tabela)], klient) == [
        "LimitWiekszy.nazwa: String(36) max_length=50 > 36"]


def test_pole_tylko_z_minimum_dlugosci_nadal_nie_ma_limitu_gornego(klient):
    assert sprawdz([(TylkoMinimum, Tabela)], klient) == [
        "TylkoMinimum.nazwa: String(36) bez max_length"]


def test_pole_z_minimum_i_maksimum_dlugosci_jest_czyste(klient):
    assert sprawdz([(MinimumIMaksimum, Tabela)], klient) == []


def test_przy_dwoch_limitach_pola_liczy_sie_wezszy(klient):
    assert sprawdz([(DwaLimitySzerszyPierwszy, Tabela)], klient) == []


def test_przy_dwoch_limitach_pola_oba_ponad_kolumne_to_naruszenie_z_wezszym(klient):
    assert sprawdz([(DwaLimityPonadKolumne, Tabela)], klient) == [
        "DwaLimityPonadKolumne.dluga: String(40) max_length=45 > 40"]


def test_pola_bez_kolumny_albo_z_kolumna_innego_typu_albo_bez_dlugosci_nie_sa_naruszeniem(klient):
    assert sprawdz([(PolaBezKolumnyStringN, Tabela)], klient) == []


def test_kazde_naruszone_pole_modelu_daje_osobna_linie_w_kolejnosci_pol(klient):
    assert sprawdz([(DwaPolaNaruszone, Tabela)], klient) == [
        "DwaPolaNaruszone.nazwa: String(36) bez max_length",
        "DwaPolaNaruszone.dluga: String(40) max_length=41 > 40",
    ]


def test_naruszenia_wielu_par_ida_w_kolejnosci_par(klient):
    assert sprawdz([(LimitWiekszy, Tabela), (BezLimitu, Tabela)], klient) == [
        "LimitWiekszy.nazwa: String(36) max_length=50 > 36",
        "BezLimitu.nazwa: String(36) bez max_length",
    ]


# --- sprawdz: Literal i unie (HAR-208: `kind` to Literal albo napis `hook:<zdarzenie>`) ---------


class LiteralMiesciSie(BaseModel):
    # najdluzsza wartosc ma dokladnie 36 znakow, tyle co kolumna
    nazwa: Literal["a", "b" * 36]


class LiteralZaDlugi(BaseModel):
    nazwa: Literal["a", "b" * 37]


class UniaLiteralIStrBezLimitu(BaseModel):
    nazwa: Literal["a"] | str


class UniaLiteralIStrZLimitemPonadKolumne(BaseModel):
    nazwa: Literal["a"] | Annotated[str, Field(max_length=50)]


class UniaGalezieKrotszaIDluzszaOdKolumny(BaseModel):
    # minimum po galeziach (10) dalo by czysty wynik; obowiazuje maksimum (50)
    nazwa: Annotated[str, Field(max_length=10)] | Annotated[str, Field(max_length=50)]


class UniaGalezieWKolumnie(BaseModel):
    nazwa: Annotated[str, Field(max_length=10)] | Annotated[str, Field(max_length=36)]


class UniaLiteralDluzszyOdGaleziNapisu(BaseModel):
    # Literal (37) dluzszy od galezi napisu (10): maksimum to dlugosc Literalu
    nazwa: Literal["c" * 37] | Annotated[str, Field(max_length=10)]


class UniaLiteralIStrZLimitemWKolumnie(BaseModel):
    nazwa: Literal["a"] | Annotated[str, Field(max_length=36)]


class UniaStrTylkoZWzorcem(BaseModel):
    nazwa: Literal["a"] | Annotated[str, StringConstraints(pattern=r"^hook:[A-Za-z0-9_]+$")]


class UniaStrZWzorcemILimitem(BaseModel):
    nazwa: Literal["a"] | Annotated[
        str, StringConstraints(pattern=r"^hook:[A-Za-z0-9_]+$", max_length=36)]


class TypNieoceniany(BaseModel):
    nazwa: list[str]


class UniaZTypemNieocenianym(BaseModel):
    nazwa: Literal["a"] | list[str]


def test_literal_z_wartosciami_miescicymi_sie_w_kolumnie_jest_czysty(klient):
    assert sprawdz([(LiteralMiesciSie, Tabela)], klient) == []


def test_literal_z_wartoscia_dluzsza_od_kolumny_jest_naruszeniem_z_dlugoscia_najdluzszej(klient):
    assert sprawdz([(LiteralZaDlugi, Tabela)], klient) == [
        "LiteralZaDlugi.nazwa: String(36) max_length=37 > 36"]


def test_unia_literalu_i_napisu_bez_limitu_jest_naruszeniem_bez_max_length(klient):
    assert sprawdz([(UniaLiteralIStrBezLimitu, Tabela)], klient) == [
        "UniaLiteralIStrBezLimitu.nazwa: String(36) bez max_length"]


def test_unia_literalu_i_napisu_z_limitem_ponad_kolumne_jest_naruszeniem(klient):
    assert sprawdz([(UniaLiteralIStrZLimitemPonadKolumne, Tabela)], klient) == [
        "UniaLiteralIStrZLimitemPonadKolumne.nazwa: String(36) max_length=50 > 36"]


def test_unia_galezi_z_limitami_bierze_maksimum_a_nie_minimum(klient):
    assert sprawdz([(UniaGalezieKrotszaIDluzszaOdKolumny, Tabela)], klient) == [
        "UniaGalezieKrotszaIDluzszaOdKolumny.nazwa: String(36) max_length=50 > 36"]


def test_unia_galezi_z_maksimum_rownym_kolumnie_jest_czysta(klient):
    assert sprawdz([(UniaGalezieWKolumnie, Tabela)], klient) == []


def test_unia_gdzie_literal_jest_dluzszy_od_limitu_napisu_bierze_dlugosc_literalu(klient):
    assert sprawdz([(UniaLiteralDluzszyOdGaleziNapisu, Tabela)], klient) == [
        "UniaLiteralDluzszyOdGaleziNapisu.nazwa: String(36) max_length=37 > 36"]


def test_unia_literalu_i_napisu_z_limitem_w_kolumnie_jest_czysta(klient):
    assert sprawdz([(UniaLiteralIStrZLimitemWKolumnie, Tabela)], klient) == []


def test_galaz_napisu_z_samym_wzorcem_bez_max_length_jest_naruszeniem(klient):
    assert sprawdz([(UniaStrTylkoZWzorcem, Tabela)], klient) == [
        "UniaStrTylkoZWzorcem.nazwa: String(36) bez max_length"]


def test_galaz_napisu_ze_wzorcem_i_max_length_w_kolumnie_jest_czysta(klient):
    assert sprawdz([(UniaStrZWzorcemILimitem, Tabela)], klient) == []


class TypNieocenianyZLimitemPola(BaseModel):
    # limit z pola nie zastepuje oceny typu: lista nie jest napisem
    nazwa: list[str] = Field(max_length=10)


class NapisZBrakiemWartosciBezAdnotacji(BaseModel):
    nazwa: str | None = None            # `types.UnionType`, gałąź `None` pomijana


class OpcjonalnyNapisZLimitemPonadKolumne(BaseModel):
    nazwa: Optional[Annotated[str, Field(max_length=50)]] = None    # `typing.Union`


class UniaGalezZDwomaLimitami(BaseModel):
    # w gałęzi obowiązuje węższy (30), między gałęziami szerszy z gałęzi: 30 <= 36
    nazwa: Annotated[str, Field(max_length=50), Field(max_length=30)] | Literal["a"]


class LiteralZLimitemZBazyAnnotated(BaseModel):
    # limit pochodzi z bazy `Annotated` (Literal 37), a metadane nie niosą max_length
    nazwa: Annotated[Literal["b" * 37], StringConstraints(pattern="^b+$")] | Literal["a"]


class LiteralZNieNapisami(BaseModel):
    nazwa: Literal["a", None, 123456]    # `str(None)` to 4 znaki, `str(123456)` to 6


@pytest.mark.parametrize(
    "model", [TypNieoceniany, UniaZTypemNieocenianym, TypNieocenianyZLimitemPola])
def test_typ_ktorego_narzedzie_nie_umie_ocenic_to_blad_narzedzia_a_nie_wynik(klient, model):
    with pytest.raises(BladNarzedzia, match=r"^nie umiem ocenić limitu typu "):
        sprawdz([(model, Tabela)], klient)


def test_napis_opcjonalny_bez_limitu_jest_naruszeniem_bez_max_length(klient):
    assert sprawdz([(NapisZBrakiemWartosciBezAdnotacji, Tabela)], klient) == [
        "NapisZBrakiemWartosciBezAdnotacji.nazwa: String(36) bez max_length"]


def test_napis_opcjonalny_z_limitem_ponad_kolumne_jest_naruszeniem_mimo_galezi_none(klient):
    assert sprawdz([(OpcjonalnyNapisZLimitemPonadKolumne, Tabela)], klient) == [
        "OpcjonalnyNapisZLimitemPonadKolumne.nazwa: String(36) max_length=50 > 36"]


def test_galaz_unii_z_dwoma_limitami_liczy_wezszy_a_miedzy_galeziami_szerszy(klient):
    assert sprawdz([(UniaGalezZDwomaLimitami, Tabela)], klient) == []


def test_literal_w_annotated_bierze_limit_z_bazy_typu_mimo_metadanych_bez_limitu(klient):
    assert sprawdz([(LiteralZLimitemZBazyAnnotated, Tabela)], klient) == [
        "LiteralZLimitemZBazyAnnotated.nazwa: String(36) max_length=37 > 36"]


def test_literal_z_wartosciami_niebedacymi_napisami_liczy_dlugosc_ich_zapisu(klient):
    assert sprawdz([(LiteralZNieNapisami, Tabela)], klient) == []


# --- sprawdz: brak dowodu to blad narzedzia ----------------------------------------------------


def test_puste_pary_to_blad_narzedzia_a_nie_czysty_wynik(klient):
    with pytest.raises(BladNarzedzia, match=r"^brak par modeli do sprawdzenia$"):
        sprawdz([], klient)


def test_modele_bez_zadnego_pola_odpowiadajacego_kolumnie_string_n_to_blad_narzedzia(klient):
    with pytest.raises(
        BladNarzedzia, match=r"^żaden model nie miał pola odpowiadającego kolumnie String\(n\)$"
    ):
        sprawdz([(TylkoPolaBezDlugosci, Tabela)], klient)


def test_model_z_polem_tylko_przy_kolumnie_innego_typu_to_blad_narzedzia(klient):
    with pytest.raises(BladNarzedzia):
        sprawdz([(TylkoLicznik, Tabela)], klient)


def test_model_pokryty_pierwszy_a_niepokryty_drugi_nie_jest_bledem_narzedzia(klient):
    assert sprawdz([(LimitRowny, Tabela), (TylkoPolaBezDlugosci, Tabela)], klient) == []


def test_model_niepokryty_pierwszy_a_pokryty_drugi_nie_jest_bledem_narzedzia(klient):
    assert sprawdz([(TylkoPolaBezDlugosci, Tabela), (LimitRowny, Tabela)], klient) == []


# --- sprawdz: statyczna kontrola klienta runnera ----------------------------------------------


def test_klient_runnera_uzywajacy_event_in_nie_jest_naruszeniem(klient):
    assert sprawdz([(LimitRowny, Tabela)], klient) == []


def test_klient_runnera_bez_event_in_to_naruszenie_wyslanej_bez_walidacji(tmp_path):
    plik = tmp_path / "client.py"
    plik.write_text("def wyslij(kw):\n    pass\n", encoding="utf-8")

    assert sprawdz([(LimitRowny, Tabela)], plik) == [
        "src/runner/client.py: wysyłka bez walidacji EventIn"]


def test_samo_wspomnienie_event_in_w_komentarzu_i_napisie_nie_jest_walidacja(tmp_path):
    plik = tmp_path / "client.py"
    plik.write_text('# EventIn\n"""EventIn"""\n\ndef wyslij(kw):\n    return "EventIn"\n')

    assert sprawdz([(LimitRowny, Tabela)], plik) == [
        "src/runner/client.py: wysyłka bez walidacji EventIn"]


def test_nazwa_inna_niz_event_in_nie_jest_walidacja_zdarzenia(tmp_path):
    plik = tmp_path / "client.py"
    plik.write_text("def wyslij(kw):\n    EventInX.model_validate(kw)\n")

    assert sprawdz([(LimitRowny, Tabela)], plik) == [
        "src/runner/client.py: wysyłka bez walidacji EventIn"]


def test_naruszenie_klienta_idzie_po_naruszeniach_limitow(tmp_path):
    plik = tmp_path / "client.py"
    plik.write_text("def wyslij(kw):\n    pass\n", encoding="utf-8")

    assert sprawdz([(BezLimitu, Tabela)], plik) == [
        "BezLimitu.nazwa: String(36) bez max_length",
        "src/runner/client.py: wysyłka bez walidacji EventIn",
    ]


def test_brakujacy_plik_klienta_to_blad_narzedzia_z_nazwa_pliku(tmp_path):
    brak = tmp_path / "nie-ma.py"

    with pytest.raises(BladNarzedzia, match=r"^nie można odczytać .*nie-ma\.py: ") as wyjatek:
        sprawdz([(LimitRowny, Tabela)], brak)

    assert isinstance(wyjatek.value.__cause__, OSError)


def test_plik_klienta_nie_w_utf8_to_blad_narzedzia(tmp_path):
    plik = tmp_path / "client.py"
    plik.write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(BladNarzedzia, match=r"^nie można odczytać ") as wyjatek:
        sprawdz([(LimitRowny, Tabela)], plik)

    assert isinstance(wyjatek.value.__cause__, UnicodeDecodeError)


def test_plik_klienta_ze_skladnia_python_nieparsowalna_to_blad_narzedzia(tmp_path):
    plik = tmp_path / "client.py"
    plik.write_text("def (:\n", encoding="utf-8")

    with pytest.raises(BladNarzedzia, match=r"^nie można odczytać ") as wyjatek:
        sprawdz([(LimitRowny, Tabela)], plik)

    assert isinstance(wyjatek.value.__cause__, SyntaxError)


# --- prawdziwe repozytorium ---------------------------------------------------------------------


def test_prawdziwe_modele_wejscia_maja_limity_zgodne_z_kolumnami():
    assert sprawdz(check_granica.domyslne_pary()) == []


def test_domyslne_pary_obejmuja_zdarzenie_rejestracje_runnera_i_utworzenie_runu():
    nazwy = [(m.__name__, t.__name__) for m, t in check_granica.domyslne_pary()]

    assert nazwy == [("EventIn", "Event"), ("RunnerRegister", "Runner"), ("RunCreate", "Run")]


@pytest.mark.parametrize("indeks", [0, 1, 2])
def test_kazda_domyslna_para_ma_pole_odpowiadajace_kolumnie_string_n(indeks, klient):
    # pojedyncza para: gdyby model nie mial zadnego pokrytego pola, narzedzie zglosi blad
    assert sprawdz([check_granica.domyslne_pary()[indeks]], klient) == []


def test_klient_runnera_z_repozytorium_waliduje_event_in():
    assert sprawdz(check_granica.domyslne_pary(), KLIENT_RUNNERA) == []


def test_po_usunieciu_walidacji_z_klienta_runnera_narzedzie_zglasza_naruszenie(tmp_path):
    zrodlo = KLIENT_RUNNERA.read_text(encoding="utf-8")
    assert "EventIn" in zrodlo      # kontrast: prawdziwy klient go uzywa
    atrapa = tmp_path / "client.py"
    atrapa.write_text(zrodlo.replace("EventIn", "ZdarzenieBezWalidacji"), encoding="utf-8")

    assert sprawdz(check_granica.domyslne_pary(), atrapa) == [
        "src/runner/client.py: wysyłka bez walidacji EventIn"]


def test_ci_uruchamia_sprawdzenie_granicy_wejscia():
    ci = (KORZEN / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "run: python tools/check_granica.py\n" in ci


# --- main i kody wyjscia -------------------------------------------------------------------------


def test_main_na_czystych_parach_zwraca_zero_i_pisze_potwierdzenie(monkeypatch, capsys):
    monkeypatch.setattr(check_granica, "domyslne_pary", lambda: [(LimitRowny, Tabela)])

    assert check_granica.main() == 0

    assert capsys.readouterr().out == TEKST_CZYSTO


def test_main_z_naruszeniami_zwraca_jeden_i_wypisuje_je_na_stdout(monkeypatch, capsys):
    monkeypatch.setattr(check_granica, "domyslne_pary", lambda: [(DwaPolaNaruszone, Tabela)])

    assert check_granica.main([]) == 1

    wyjscie = capsys.readouterr()
    assert wyjscie.out == (
        "Granica wejścia bez limitów:\n"
        "DwaPolaNaruszone.nazwa: String(36) bez max_length\n"
        "DwaPolaNaruszone.dluga: String(40) max_length=41 > 40\n")
    assert wyjscie.err == ""


def test_main_bez_par_zwraca_dwa_z_przyczyna_na_stderr(monkeypatch, capsys):
    monkeypatch.setattr(check_granica, "domyslne_pary", lambda: [])

    assert check_granica.main() == 2

    wyjscie = capsys.readouterr()
    assert wyjscie.err == "brak par modeli do sprawdzenia\n"
    assert wyjscie.out == ""


def test_main_z_modelami_bez_pol_string_n_zwraca_dwa(monkeypatch, capsys):
    monkeypatch.setattr(check_granica, "domyslne_pary", lambda: [(TylkoLicznik, Tabela)])

    assert check_granica.main() == 2

    assert capsys.readouterr().err == (
        "żaden model nie miał pola odpowiadającego kolumnie String(n)\n")


def test_main_przy_bledzie_importu_modeli_zwraca_dwa_a_nie_traceback(monkeypatch, capsys):
    def importuj():
        raise ImportError("brak modułu modeli")

    monkeypatch.setattr(check_granica, "domyslne_pary", importuj)

    assert check_granica.main() == 2

    wyjscie = capsys.readouterr()
    assert wyjscie.err == "brak modułu modeli\n"
    assert wyjscie.out == ""


def _uruchom_narzedzie(*argv: str) -> tuple[int, str, str]:
    proc = uruchom_proces_sync(
        [sys.executable, *argv], cwd=KORZEN, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    try:
        out, err = proc.communicate(timeout=LIMIT)
    finally:
        zakoncz_proces_sync(proc, nazwa="check_granica")
    return proc.returncode, out, err


def test_narzedzie_na_prawdziwym_repozytorium_konczy_sie_kodem_zero():
    assert _uruchom_narzedzie(str(NARZEDZIE)) == (0, TEKST_CZYSTO, "")


def _kopia_narzedzia_z_klientem(tmp_path: Path, klient: str | None) -> Path:
    """Kopia `tools/check_granica.py` w `<tmp>/tools`, z `src/cockpit` z repozytorium i wlasnym
    `src/runner/client.py` (`None` = bez pliku); KORZEN skryptu to wtedy katalog kopii."""
    (tmp_path / "tools").mkdir()
    skrypt = tmp_path / "tools" / "check_granica.py"
    skrypt.write_text(NARZEDZIE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cockpit").symlink_to(KORZEN / "src" / "cockpit")
    (tmp_path / "src" / "runner").mkdir()
    if klient is not None:
        (tmp_path / "src" / "runner" / "client.py").write_text(klient, encoding="utf-8")
    return skrypt


def test_narzedzie_uruchomione_jako_skrypt_konczy_sie_kodem_jeden_gdy_klient_nie_waliduje(tmp_path):
    skrypt = _kopia_narzedzia_z_klientem(tmp_path, "def wyslij(kw):\n    pass\n")

    assert _uruchom_narzedzie(str(skrypt)) == (
        1,
        "Granica wejścia bez limitów:\nsrc/runner/client.py: wysyłka bez walidacji EventIn\n",
        "")


def test_narzedzie_uruchomione_jako_skrypt_konczy_sie_kodem_zero_gdy_klient_waliduje(tmp_path):
    skrypt = _kopia_narzedzia_z_klientem(tmp_path, KLIENT_POPRAWNY)

    assert _uruchom_narzedzie(str(skrypt)) == (0, TEKST_CZYSTO, "")


def test_narzedzie_uruchomione_jako_skrypt_konczy_sie_kodem_dwa_gdy_brak_klienta(tmp_path):
    skrypt = _kopia_narzedzia_z_klientem(tmp_path, None)

    kod, out, err = _uruchom_narzedzie(str(skrypt))

    assert (kod, out) == (2, "")
    assert err.startswith("nie można odczytać ")


def test_narzedzie_bez_zadnych_modeli_konczy_proces_kodem_dwa_a_nie_zerem():
    kod = (
        "import sys; sys.path.insert(0, 'tools'); import check_granica as c; "
        "c.domyslne_pary = lambda: []; sys.exit(c.main())")

    assert _uruchom_narzedzie("-c", kod) == (2, "", "brak par modeli do sprawdzenia\n")


# --- runner: zdarzenie z polem ponad limit EventIn nie wychodzi z runnera ----------------------

# Lista niezalezna od kodu: (pole, wartosc na limicie, wartosc o jeden ponad limit).
POLA_Z_LIMITEM = [
    pytest.param("run_id", "r" * 36, "r" * 37, id="run_id"),
    pytest.param("event_id", "e" * 36, "e" * 37, id="event_id"),
    pytest.param("step_id", "s" * 100, "s" * 101, id="step_id"),
    pytest.param("agent_id", "a" * 100, "a" * 101, id="agent_id"),
    pytest.param("agent_type", "t" * 100, "t" * 101, id="agent_type"),
    # rodzaje stale to zamkniety Literal; limit 50 znakow dotyczy tylko rodzaju `hook:<zdarzenie>`
    pytest.param("kind", "hook:" + "k" * 45, "hook:" + "k" * 46, id="kind"),
    pytest.param("tokens", 2_147_483_647, 2_147_483_648, id="tokens"),
    pytest.param("duration_ms", 2_147_483_647, 2_147_483_648, id="duration_ms"),
    pytest.param("session_id", "i" * 100, "i" * 101, id="session_id"),
    pytest.param("verdict", "v" * 200, "v" * 201, id="verdict"),
    pytest.param("kroki_weryfikujace", ["k"] * 1000, ["k"] * 1001, id="kroki_weryfikujace_liczba"),
    pytest.param("kroki_weryfikujace", ["k" * 200], ["k" * 201], id="kroki_weryfikujace_element"),
    pytest.param("kotwice", ["a.py:1"] * 1000, ["a.py:1"] * 1001, id="kotwice_liczba"),
    pytest.param("kotwice", ["k" * 1000], ["k" * 1001], id="kotwice_element"),
    pytest.param("artefakty", ["a"] * 100, ["a"] * 101, id="artefakty_liczba"),
    pytest.param("artefakty", ["a" * 255], ["a" * 256], id="artefakty_element"),
    pytest.param("tokens_in", 2_147_483_647, 2_147_483_648, id="tokens_in"),
    pytest.param("tokens_out", 2_147_483_647, 2_147_483_648, id="tokens_out"),
    pytest.param("num_turns", 2_147_483_647, 2_147_483_648, id="num_turns"),
    pytest.param("text_full_len", 2_147_483_647, 2_147_483_648, id="text_full_len"),
    pytest.param("attempt", 1, 0, id="attempt_poniżej_minimum"),
    pytest.param("cost_usd", 0.0, -0.01, id="cost_usd_poniżej_minimum"),
]
DOWOD = {
    "capability": "lint", "exit_code": 0, "sha256": "a" * 64, "started_at": "t0",
    "finished_at": "t1", "source": "engine", "plik": "dowod.json",
}
BAZA_ZDARZENIA = {"run_id": "run-1", "step_id": "krok-1", "kind": "output", "text": "x"}
FUNKCJE_WYSYLKI = ["send_event", "_wyslij_bezposrednio"]


class _Kokpit:
    """Transport kokpitu: odpowiada 200 i zapisuje kazde zadanie."""

    def __init__(self) -> None:
        self.zadania: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.zadania.append(request)
        return httpx.Response(200)


async def _bez_odczekania(sekundy: float) -> None:
    return None


async def _wyslij(funkcja: str, kokpit: _Kokpit, bufor: Bufor, **pola) -> bool:
    runner = Runner("http://kokpit.test", "tajny-token", bufor=bufor, odczekaj=_bez_odczekania)
    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        return await getattr(runner, funkcja)(client, **{**BAZA_ZDARZENIA, **pola})


def _pole(nazwa: str, wartosc) -> dict:
    return {nazwa: wartosc}


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
@pytest.mark.parametrize(("pole", "na_limicie", "ponad_limit"), POLA_Z_LIMITEM)
async def test_zdarzenie_z_polem_o_jeden_ponad_limit_nie_trafia_do_bufora_ani_na_siec(
    tmp_path, funkcja, pole, na_limicie, ponad_limit
):
    kokpit = _Kokpit()
    bufor = Bufor(tmp_path / "bufor")

    with pytest.raises(NiewaznePoleZdarzenia):
        await _wyslij(funkcja, kokpit, bufor, **_pole(pole, ponad_limit))

    assert bufor.niepotwierdzone() == []
    assert kokpit.zadania == []


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
@pytest.mark.parametrize(("pole", "na_limicie", "ponad_limit"), POLA_Z_LIMITEM)
async def test_zdarzenie_z_polem_dokladnie_na_limicie_jest_przyjete_i_wyslane(
    tmp_path, funkcja, pole, na_limicie, ponad_limit
):
    kokpit = _Kokpit()
    bufor = Bufor(tmp_path / "bufor")

    przyjete = await _wyslij(funkcja, kokpit, bufor, **_pole(pole, na_limicie))

    assert przyjete is True
    assert len(kokpit.zadania) == 1
    assert bufor.niepotwierdzone() == []        # przyjete przez kokpit znika z bufora


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_blad_dowodu_zdarzenia_jest_zgloszony_przed_wyslaniem(tmp_path, funkcja):
    kokpit = _Kokpit()
    bufor = Bufor(tmp_path / "bufor")

    for dowod in ({**DOWOD, "capability": "c" * 201}, {**DOWOD, "argv": ["sekret"]}):
        with pytest.raises(NiewaznePoleZdarzenia):
            await _wyslij(funkcja, kokpit, bufor, kind="step_finished", dowod=dowod)

    assert kokpit.zadania == []
    assert bufor.niepotwierdzone() == []


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_poprawny_dowod_zdarzenia_jest_wyslany(tmp_path, funkcja):
    kokpit = _Kokpit()

    przyjete = await _wyslij(
        funkcja, kokpit, Bufor(tmp_path / "bufor"), kind="step_finished", dowod=DOWOD)

    assert przyjete is True
    assert len(kokpit.zadania) == 1


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_komunikat_niewaznego_pola_nazywa_rodzaj_zdarzenia_i_pole_z_przyczyna(
    tmp_path, funkcja
):
    with pytest.raises(NiewaznePoleZdarzenia) as wyjatek:
        await _wyslij(
            funkcja, _Kokpit(), Bufor(tmp_path / "bufor"), step_id="s" * 101, kind="step_failed")

    assert str(wyjatek.value) == (
        "zdarzenie step_failed: step_id: String should have at most 100 characters")
    assert isinstance(wyjatek.value.__cause__, ValidationError)


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_komunikat_kilku_niewaznych_pol_laczy_je_srednikiem_w_kolejnosci_pol(
    tmp_path, funkcja
):
    with pytest.raises(NiewaznePoleZdarzenia) as wyjatek:
        await _wyslij(
            funkcja, _Kokpit(), Bufor(tmp_path / "bufor"), run_id="r" * 37, cost_source="x")

    assert str(wyjatek.value) == (
        "zdarzenie output: run_id: String should have at most 36 characters; "
        "cost_source: Input should be 'measured' or 'unavailable'")


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_komunikat_bledu_elementu_listy_wskazuje_jego_indeks_kropka(tmp_path, funkcja):
    with pytest.raises(NiewaznePoleZdarzenia) as wyjatek:
        await _wyslij(
            funkcja, _Kokpit(), Bufor(tmp_path / "bufor"), kind="step_finished",
            kotwice=["a.py:1", "k" * 1001])

    assert str(wyjatek.value) == (
        "zdarzenie step_finished: kotwice.1: String should have at most 1000 characters")


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_zdarzenie_bez_rodzaju_ma_w_komunikacie_none_a_nie_wyjatek_klucza(tmp_path, funkcja):
    runner = Runner(
        "http://kokpit.test", "tajny-token", bufor=Bufor(tmp_path / "bufor"),
        odczekaj=_bez_odczekania)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_Kokpit())) as client:
        with pytest.raises(NiewaznePoleZdarzenia) as wyjatek:
            await getattr(runner, funkcja)(client, run_id="run-1", step_id="krok-1")

    assert str(wyjatek.value) == "zdarzenie None: kind: Field required"


@pytest.mark.parametrize("funkcja", FUNKCJE_WYSYLKI)
async def test_event_id_nadany_przez_runner_przechodzi_walidacje_a_za_dlugi_od_wolajacego_nie(
    tmp_path, funkcja
):
    kokpit = _Kokpit()
    bufor = Bufor(tmp_path / "bufor")

    assert await _wyslij(funkcja, kokpit, bufor) is True        # bez `event_id`: nadaje runner
    with pytest.raises(NiewaznePoleZdarzenia):
        await _wyslij(funkcja, kokpit, bufor, event_id="e" * 37)

    assert len(kokpit.zadania) == 1


def test_niewazne_pole_zdarzenia_jest_wartoscia_bledna_a_nie_bledem_sieci():
    assert issubclass(NiewaznePoleZdarzenia, ValueError)
    assert not issubclass(NiewaznePoleZdarzenia, (OSError, httpx.HTTPError))


# --- EventIn: wartosci domyslne i granice pol liczbowych (kod 3 w schematy.py) -------------------

BAZA_MODELU = {**BAZA_ZDARZENIA, "event_id": "e-1"}
BEZ_TEKSTU = {k: v for k, v in BAZA_MODELU.items() if k != "text"}


def test_zdarzenie_bez_tekstu_ma_pusty_tekst():
    assert EventIn(**BEZ_TEKSTU).text == ""


def test_zdarzenie_bez_pomiarow_ma_brak_pomiaru_a_nie_zero():
    z = EventIn(**BAZA_MODELU)

    assert (z.tokens, z.duration_ms, z.wyslane_output, z.text_full_len) == (None, None, None, None)


def test_pelna_dlugosc_tekstu_zero_jest_dozwolona():
    assert EventIn(**BAZA_MODELU, text_full_len=0).text_full_len == 0


def test_pelna_dlugosc_tekstu_ujemna_jest_odrzucona():
    with pytest.raises(ValidationError):
        EventIn(**BAZA_MODELU, text_full_len=-1)


# --- WpisManifestuIn i pole manifest zdarzenia: limity i ksztalt wpisu (kod 3 w schematy.py) -----

WPIS = {"warstwa": "skill", "sciezka": "a.py", "sha256": "a" * 64, "rozmiar": 5, "id": "x"}


def _wpis(**pola) -> WpisManifestuIn:
    return WpisManifestuIn(**{**WPIS, **pola})


def test_wpis_manifestu_bez_pol_opcjonalnych_ma_brak_pomiaru_a_nie_zero():
    w = WpisManifestuIn(warstwa="skill")

    assert (w.sciezka, w.sha256, w.rozmiar, w.id) == (None, None, None, None)


@pytest.mark.parametrize(("pole", "dlugosc"), [("warstwa", 64), ("sciezka", 255), ("id", 64)])
def test_wpis_manifestu_z_polem_dokladnie_na_limicie_dlugosci_jest_przyjety(pole, dlugosc):
    assert getattr(_wpis(**{pole: "x" * dlugosc}), pole) == "x" * dlugosc


@pytest.mark.parametrize(("pole", "dlugosc"), [("warstwa", 65), ("sciezka", 256), ("id", 65)])
def test_wpis_manifestu_z_polem_o_jeden_ponad_limit_jest_odrzucony(pole, dlugosc):
    with pytest.raises(ValidationError):
        _wpis(**{pole: "x" * dlugosc})


@pytest.mark.parametrize("pole", ["warstwa", "sciezka", "id"])
def test_wpis_manifestu_z_pustym_polem_tekstowym_jest_odrzucony(pole):
    with pytest.raises(ValidationError):
        _wpis(**{pole: ""})


@pytest.mark.parametrize(
    "sha",
    [
        pytest.param("a" * 63, id="za_krotki"),
        pytest.param("a" * 65, id="za_dlugi"),
        pytest.param("A" * 64, id="wielkie_litery"),
        pytest.param("g" * 64, id="znak_spoza_hex"),
        pytest.param("\n" + "a" * 64, id="przed_hashem_nowa_linia"),
        pytest.param("a" * 64 + "\n", id="po_hashu_nowa_linia"),
        pytest.param("x" + "a" * 64, id="przed_hashem_znak"),
    ],
)
def test_wpis_manifestu_z_haszem_innym_niz_64_znaki_hex_malymi_literami_jest_odrzucony(sha):
    with pytest.raises(ValidationError):
        _wpis(sha256=sha)


def test_wpis_manifestu_z_haszem_ze_wszystkimi_cyframi_hex_jest_przyjety():
    assert _wpis(sha256="0123456789abcdef" * 4).sha256 == "0123456789abcdef" * 4


@pytest.mark.parametrize("rozmiar", [0, 2_147_483_647])
def test_wpis_manifestu_z_rozmiarem_na_granicach_zakresu_jest_przyjety(rozmiar):
    assert _wpis(rozmiar=rozmiar).rozmiar == rozmiar


@pytest.mark.parametrize("rozmiar", [-1, 2_147_483_648])
def test_wpis_manifestu_z_rozmiarem_poza_zakresem_jest_odrzucony(rozmiar):
    with pytest.raises(ValidationError):
        _wpis(rozmiar=rozmiar)


def test_wpis_manifestu_z_nieznanym_kluczem_jest_odrzucony():
    with pytest.raises(ValidationError):
        _wpis(tresc="sekret")


@pytest.mark.parametrize(("pole", "zla"), [("rozmiar", "5"), ("rozmiar", True)])
def test_wpis_manifestu_z_wartoscia_zlego_typu_jest_odrzucony_bez_konwersji(pole, zla):
    with pytest.raises(ValidationError):
        _wpis(**{pole: zla})


def test_zdarzenie_z_tysiacem_wpisow_manifestu_jest_przyjete():
    assert len(EventIn(**BAZA_MODELU, manifest=[WPIS] * 1000).manifest) == 1000


def test_zdarzenie_z_wpisami_manifestu_ponad_tysiac_jest_odrzucone():
    with pytest.raises(ValidationError):
        EventIn(**BAZA_MODELU, manifest=[WPIS] * 1001)


def test_zdarzenie_bez_manifestu_ma_brak_manifestu_a_nie_pusta_liste():
    z = EventIn(**BAZA_MODELU)

    assert z.manifest is None
    assert z.dane() is None


def test_manifest_zdarzenia_jest_lista_wpisow_a_nie_slownikow():
    z = EventIn(**BAZA_MODELU, manifest=[WPIS])

    assert isinstance(z.manifest[0], WpisManifestuIn)
    assert z.dane() == {"manifest": [WPIS]}


# --- wolajacy zamienia NiewaznePoleZdarzenia na koniec runu (HAR-242, rework) ---------------

def _zdarzenia_z_transportu(kokpit: _Kokpit) -> list[dict]:
    return [json.loads(z.content) for z in kokpit.zadania]


def _runner_z_kokpitem(tmp_path, kokpit: _Kokpit) -> Runner:
    return Runner("http://kokpit.test", "tajny-token", bufor=Bufor(tmp_path / "bufor"),
                  odczekaj=_bez_odczekania, transport=httpx.MockTransport(kokpit))


def _asercje_konca_runu(kokpit: _Kokpit, tmp_path) -> None:
    zdarzenia = _zdarzenia_z_transportu(kokpit)
    assert [(z["kind"], z["run_id"], z["step_id"]) for z in zdarzenia] == [
        ("step_failed", "run-1", "run"), ("run_finished", "run-1", "run")]
    assert zdarzenia[0]["text"].startswith(
        "zdarzenie przekracza limity pol: zdarzenie output: step_id:")
    assert zdarzenia[1]["text"] == ""
    # klucze musza byc na drucie: domyslne wartosci EventIn maskowalyby ich brak w walidacji
    assert [(z["cost_usd"], z["cost_source"]) for z in zdarzenia] == [(None, "unavailable")] * 2
    assert Bufor(tmp_path / "bufor").niepotwierdzone() == []


async def test_run_ze_zdarzeniem_ponad_limit_konczy_sie_step_failed_i_run_finished(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()

    async def silnik_z_za_dlugim_krokiem(cmd, emit, **kw):
        await emit({**BAZA_ZDARZENIA, "step_id": "s" * 101})

    monkeypatch.setattr(modul_klienta.silnik, "wykonaj_run", silnik_z_za_dlugim_krokiem)

    await _runner_z_kokpitem(tmp_path, kokpit)._run_workflow({"run_id": "run-1"})

    _asercje_konca_runu(kokpit, tmp_path)


async def test_wznowienie_ze_zdarzeniem_ponad_limit_konczy_sie_step_failed_i_run_finished(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()

    async def silnik_wznowienia(run_id, step_id, decyzja, emit, powod=None, **kw):
        await emit({**BAZA_ZDARZENIA, "step_id": "s" * 101})

    monkeypatch.setattr(modul_klienta.silnik, "wznow_run", silnik_wznowienia)

    await _runner_z_kokpitem(tmp_path, kokpit)._resume(
        {"run_id": "run-1", "step_id": "krok-1", "decyzja": "approve"})

    _asercje_konca_runu(kokpit, tmp_path)


async def test_krok_polecenia_ze_zdarzeniem_ponad_limit_konczy_sie_step_failed_i_run_finished(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()
    runner = _runner_z_kokpitem(tmp_path, kokpit)

    async def run_command(client, run_id, step_id, command):
        await runner.send_event(client, **{**BAZA_ZDARZENIA, "step_id": "s" * 101})

    monkeypatch.setattr(modul_klienta, "argv_operacji", lambda cmd: ("x",))
    monkeypatch.setattr(runner, "run_command", run_command)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._krok_z_polecenia(client, {"run_id": "run-1", "step_id": "krok-1"})

    _asercje_konca_runu(kokpit, tmp_path)


async def test_polecenie_z_pustym_step_id_konczy_run_porazka_kroku_run_z_powodem_braku_step_id(
    tmp_path,
):
    kokpit = _Kokpit()
    runner = _runner_z_kokpitem(tmp_path, kokpit)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._krok_z_polecenia(
            client, {"run_id": "run-1", "step_id": "", "operacja": "nieznana-operacja"})

    zdarzenia = _zdarzenia_z_transportu(kokpit)
    assert [(z["kind"], z["run_id"], z["step_id"]) for z in zdarzenia] == [
        ("step_failed", "run-1", "run"), ("run_finished", "run-1", "run")]
    assert zdarzenia[0]["text"] == "polecenie run_step bez poprawnego step_id"
    assert Bufor(tmp_path / "bufor").niepotwierdzone() == []


async def test_polecenie_spoza_listy_z_za_dlugim_step_id_konczy_run_step_failed_i_run_finished(
    tmp_path,
):
    kokpit = _Kokpit()
    runner = _runner_z_kokpitem(tmp_path, kokpit)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._krok_z_polecenia(
            client, {"run_id": "run-1", "step_id": "s" * 101, "operacja": "nieznana-operacja"})

    zdarzenia = _zdarzenia_z_transportu(kokpit)
    assert [(z["kind"], z["run_id"], z["step_id"]) for z in zdarzenia] == [
        ("step_failed", "run-1", "run"), ("run_finished", "run-1", "run")]
    assert zdarzenia[0]["text"].startswith("zdarzenie przekracza limity pol: zdarzenie step_failed")
    assert Bufor(tmp_path / "bufor").niepotwierdzone() == []


async def test_run_z_run_id_ponad_limit_nie_ma_komu_zglosic_konca_i_nie_rzuca(
    tmp_path, monkeypatch, capsys
):
    kokpit = _Kokpit()

    async def silnik_z_zlym_run_id(cmd, emit, **kw):
        await emit({**BAZA_ZDARZENIA, "run_id": "r" * 37})

    monkeypatch.setattr(modul_klienta.silnik, "wykonaj_run", silnik_z_zlym_run_id)

    await _runner_z_kokpitem(tmp_path, kokpit)._run_workflow({"run_id": "r" * 37})

    assert kokpit.zadania == []
    err = capsys.readouterr().err
    assert f"koniec runu niewyslany: run_id={'r' * 36!r} - " in err
    assert "r" * 37 not in err
    assert "zdarzenie output: run_id:" in err


async def test_tekst_step_failed_ponad_limit_jest_obciety_do_2000_znakow(tmp_path):
    kokpit = _Kokpit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await _runner_z_kokpitem(tmp_path, kokpit)._zglos_niewazne_pole(
            client, "run-1", NiewaznePoleZdarzenia("z" * 5000))

    assert len(_zdarzenia_z_transportu(kokpit)[0]["text"]) == 2000


# --- koniec runu po niewaznym polu: step_failed przez bufor, run_finished po jego przyjeciu ------

def _runner_ktorego_odczekanie_go_zatrzymuje(tmp_path, kokpit) -> Runner:
    """Pierwsze odczekanie przed ponowieniem zatrzymuje runner: wpis zostaje w buforze."""
    runner: Runner

    async def odczekaj_i_zatrzymaj(sekundy: float) -> None:
        runner._zatrzymany.set()

    runner = Runner("http://kokpit.test", "tajny-token", bufor=Bufor(tmp_path / "bufor"),
                    odczekaj=odczekaj_i_zatrzymaj, transport=httpx.MockTransport(kokpit))
    return runner


class _KokpitOdrzucajacy(_Kokpit):
    def __init__(self, blad_transportu: bool) -> None:
        super().__init__()
        self._blad_transportu = blad_transportu

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.zadania.append(request)
        if self._blad_transportu:
            raise httpx.ConnectError("brak polaczenia", request=request)
        return httpx.Response(503)


@pytest.mark.parametrize("blad_transportu", [False, True], ids=["http_503", "blad_transportu"])
async def test_kokpit_odrzucajacy_step_failed_nie_dostaje_run_finished_a_step_failed_czeka_w_buforze(
    tmp_path, blad_transportu, capsys
):
    kokpit = _KokpitOdrzucajacy(blad_transportu)
    runner = _runner_ktorego_odczekanie_go_zatrzymuje(tmp_path, kokpit)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._zglos_niewazne_pole(client, "run-1", NiewaznePoleZdarzenia("za dlugi"))

    assert [z["kind"] for z in _zdarzenia_z_transportu(kokpit)] == ["step_failed"]
    assert [w["kind"] for w in Bufor(tmp_path / "bufor").niepotwierdzone()] == ["step_failed"]
    stderr = capsys.readouterr().err
    assert "run_id=run-1" in stderr
    assert "zdarzenie kroku step_failed nie zostalo przyjete" in stderr


async def test_przyjety_step_failed_po_niewaznym_polu_jest_przed_run_finished(tmp_path):
    kokpit = _Kokpit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await _runner_z_kokpitem(tmp_path, kokpit)._zglos_niewazne_pole(
            client, "run-1", NiewaznePoleZdarzenia("za dlugi"))

    assert [(z["kind"], z["run_id"], z["step_id"]) for z in _zdarzenia_z_transportu(kokpit)] == [
        ("step_failed", "run-1", "run"), ("run_finished", "run-1", "run")]


async def test_wznowienie_runu_bez_checkpointu_ze_step_id_ponad_limit_konczy_sie_krokiem_run(
    tmp_path, monkeypatch
):
    """Prawdziwy `wznow_run`: brak checkpointu to porazka kroku `s*101`, ktorej zdarzenie
    lamie limit `step_id`; kokpit dostaje step_failed i run_finished z krokiem `run`."""
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "runy"))
    kokpit = _Kokpit()

    await _runner_z_kokpitem(tmp_path, kokpit)._resume(
        {"run_id": "run-1", "step_id": "s" * 101, "decyzja": "approve"})

    zdarzenia = _zdarzenia_z_transportu(kokpit)
    assert [(z["kind"], z["run_id"], z["step_id"]) for z in zdarzenia] == [
        ("step_failed", "run-1", "run"), ("run_finished", "run-1", "run")]
    assert Bufor(tmp_path / "bufor").niepotwierdzone() == []


# --- brak miejsca w buforze przy zgloszeniu niewaznego pola: step_aborted poza buforem -------

def _runner_z_buforem_bez_miejsca(tmp_path, kokpit: _Kokpit, monkeypatch) -> Runner:
    runner = _runner_z_kokpitem(tmp_path, kokpit)

    def bufor_bez_miejsca(wpis):
        raise BrakMiejsca("zapis: OSError")

    monkeypatch.setattr(runner._bufor, "dopisz", bufor_bez_miejsca)
    return runner


def _asercje_przerwania_bez_miejsca(kokpit: _Kokpit) -> None:
    zdarzenia = _zdarzenia_z_transportu(kokpit)
    assert [(z["kind"], z["run_id"], z["step_id"]) for z in zdarzenia] == [
        ("step_aborted", "run-1", "run")]
    assert zdarzenia[0]["text"] == "brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)"


async def test_brak_miejsca_w_buforze_przy_zgloszeniu_niewaznego_pola_konczy_krok_run_przerwaniem(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()
    runner = _runner_z_buforem_bez_miejsca(tmp_path, kokpit, monkeypatch)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._zglos_niewazne_pole(client, "run-1", NiewaznePoleZdarzenia("x"))

    _asercje_przerwania_bez_miejsca(kokpit)


async def test_run_ze_zdarzeniem_ponad_limit_i_pelnym_buforem_konczy_sie_przerwaniem_a_nie_wyjatkiem(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()
    runner = _runner_z_buforem_bez_miejsca(tmp_path, kokpit, monkeypatch)

    async def silnik_z_za_dlugim_krokiem(cmd, emit, **kw):
        await emit({**BAZA_ZDARZENIA, "step_id": "s" * 101})

    monkeypatch.setattr(modul_klienta.silnik, "wykonaj_run", silnik_z_za_dlugim_krokiem)

    await runner._run_workflow({"run_id": "run-1"})

    _asercje_przerwania_bez_miejsca(kokpit)


async def test_wznowienie_ze_zdarzeniem_ponad_limit_i_pelnym_buforem_konczy_sie_przerwaniem_a_nie_wyjatkiem(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()
    runner = _runner_z_buforem_bez_miejsca(tmp_path, kokpit, monkeypatch)

    async def silnik_wznowienia(run_id, step_id, decyzja, emit, powod=None, **kw):
        await emit({**BAZA_ZDARZENIA, "step_id": "s" * 101})

    monkeypatch.setattr(modul_klienta.silnik, "wznow_run", silnik_wznowienia)

    await runner._resume({"run_id": "run-1", "step_id": "krok-1", "decyzja": "approve"})

    _asercje_przerwania_bez_miejsca(kokpit)


async def test_krok_polecenia_ze_zdarzeniem_ponad_limit_i_pelnym_buforem_konczy_sie_przerwaniem_a_nie_wyjatkiem(
    tmp_path, monkeypatch
):
    kokpit = _Kokpit()
    runner = _runner_z_buforem_bez_miejsca(tmp_path, kokpit, monkeypatch)

    async def run_command(client, run_id, step_id, command):
        await runner.send_event(client, **{**BAZA_ZDARZENIA, "step_id": "s" * 101})

    monkeypatch.setattr(modul_klienta, "argv_operacji", lambda cmd: ("x",))
    monkeypatch.setattr(runner, "run_command", run_command)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._krok_z_polecenia(client, {"run_id": "run-1", "step_id": "krok-1"})

    _asercje_przerwania_bez_miejsca(kokpit)


async def test_przerwanie_z_braku_miejsca_ponad_limit_run_id_nie_ucieka_a_zostaje_na_stderr(
    tmp_path, monkeypatch, capsys
):
    """`send_event` waliduje ten sam `run_id` przed buforem, wiec przez prawdziwy bufor ta galaz
    nie jest osiagalna; atrapa `send_event` rzucajaca BrakMiejsca sprawdza tylko zagniezdzenie."""
    kokpit = _Kokpit()
    runner = _runner_z_kokpitem(tmp_path, kokpit)

    async def send_event_bez_miejsca(client, **kw):
        raise BrakMiejsca("zapis: OSError")

    monkeypatch.setattr(runner, "send_event", send_event_bez_miejsca)

    async with httpx.AsyncClient(transport=httpx.MockTransport(kokpit)) as client:
        await runner._zglos_niewazne_pole(client, "r" * 37, NiewaznePoleZdarzenia("x"))

    assert kokpit.zadania == []
    assert f"koniec runu niewyslany: run_id={'r' * 36!r} - x" in capsys.readouterr().err

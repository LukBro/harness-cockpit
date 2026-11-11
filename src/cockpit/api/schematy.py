"""Modele odpowiedzi API i opisy błędów w schemacie OpenAPI (HAR-69).

Modele odwzorowują 1:1 słowniki, które trasy zwracały przed tym ticketem — kontrakt jest
jawny, ale kształt JSON się nie zmienia. Wszystkie pola są wymagane: pole, którego wartość
bywa pusta (`reason`, `text_full_len`, `last_seen_at`), jest w odpowiedzi zawsze jako `null`,
nigdy pominięte (`measured_or_null`). Jedyny wyjątek to `Blad.aktywny_run_id`, który
handler błędów dokleja tylko przy konflikcie równoległego runu (bledy.py).

Plik z modelami nie ma logiki ani dostępu do bazy; z `cockpit.db.models` bierze wyłącznie
wyliczenia stanu runu i rodzaju runnera (dozwolone w `api/` przez tools/check_layers.py).
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cockpit.db.models import RunnerKind, RunStatus


_ListaKotwic = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=1000)]
_ListaArtefaktow = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=255)]], Field(max_length=100)]


class DowodIn(BaseModel):
    """Dowod kroku w ksztalcie, jaki wysyla silnik (`runner.dowody`); `argv` i `stdout_tail`
    zostaja w pliku dowodu u runnera (sekrety, HAR-66), wiec nieznany klucz to 422 (HAR-170)."""
    model_config = ConfigDict(extra="forbid", strict=True)

    capability: str = Field(max_length=200, min_length=1)
    # None przy przekroczeniu limitu czasu; strict odrzuca bool, napis i liczbe zmiennoprzecinkowa
    exit_code: int | None = Field(ge=-2_147_483_648, le=2_147_483_647)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: str = Field(min_length=1, max_length=64)
    finished_at: str = Field(min_length=1, max_length=64)
    source: Literal["engine", "agent"]
    plik: str = Field(min_length=1, max_length=255)


class EventIn(BaseModel):
    run_id: str = Field(max_length=36)     # kolumny String(n): max_length = n (HAR-242)
    event_id: str = Field(min_length=1, max_length=36)    # nadaje runner; idempotencja (HAR-39)
    step_id: str = Field(min_length=1, max_length=100)
    agent_id: str = Field(default="", max_length=100)
    agent_type: str = Field(default="", max_length=100)
    kind: str = Field(max_length=50)
    text: str = ""
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)  # None = brak pomiaru, nigdy 0 (BRO-87)
    cost_source: str = Field(default="unavailable", max_length=20)
    tokens: int | None = None
    duration_ms: int | None = None
    wyslane_output: int | None = None      # ile output runner probowal wyslac (HAR-24)
    # pelna dlugosc tekstu przed obcieciem przez runnera; le = int4 PostgreSQL (HAR-56)
    text_full_len: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # numer proby kroku; brak to None, nie 1; le = int4 PostgreSQL (HAR-78)
    attempt: int | None = Field(default=None, ge=1, le=2_147_483_647)
    # pomiar kroku z wyniku agenta (HAR-174); brak to None, nigdy 0; le = int4 PostgreSQL
    tokens_in: int | None = Field(default=None, ge=0, le=2_147_483_647)
    tokens_out: int | None = Field(default=None, ge=0, le=2_147_483_647)
    num_turns: int | None = Field(default=None, ge=0, le=2_147_483_647)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    # kroki weryfikujace (D27) w `run_finished`; None = runner nie sklasyfikowal (HAR-178)
    kroki_weryfikujace: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None, max_length=1000)
    # dane konca kroku (HAR-170), tylko w step_finished/step_failed; brak to None
    dowod: DowodIn | None = None
    verdict: str | None = Field(default=None, min_length=1, max_length=200)
    kotwice: _ListaKotwic | None = None
    artefakty: _ListaArtefaktow | None = None

    def dane(self) -> dict | None:
        """Podane pola dowodowe jako `Event.dane`; zadne nie podane to None (SQL NULL), nie `{}`."""
        dane: dict = {}
        if self.dowod is not None:
            dane["dowod"] = self.dowod.model_dump()
        if self.verdict is not None:
            dane["verdict"] = self.verdict
        if self.kotwice is not None:
            dane["kotwice"] = self.kotwice
        if self.artefakty is not None:
            dane["artefakty"] = self.artefakty
        return dane or None


class Zdrowie(BaseModel):
    status: str


class RunnerZameldowany(BaseModel):
    runner_id: str
    tenant_id: str
    heartbeat_co_s: int


class RunUtworzony(BaseModel):
    run_id: str


class DecyzjaIn(BaseModel):
    """Decyzja czlowieka na bramce; powod wymagany przy odrzuceniu (sprawdza serwis)."""
    decyzja: Literal["approve", "reject"]
    powod: str | None = Field(None, max_length=2000)


class DecyzjaPrzyjeta(BaseModel):
    run_id: str
    status: RunStatus
    step_id: str
    reason: str | None


class ZdarzeniePrzyjete(BaseModel):
    seq: int


class ZdarzenieRunu(BaseModel):
    seq: int
    kind: str
    text: str
    step_id: str
    agent_type: str
    text_truncated: bool
    text_full_len: int | None   # brak pomiaru pełnej długości to null, nigdy 0
    at: datetime                # czas zapisu zdarzenia w UTC (HAR-121: „kto i kiedy” w odbiorze)


class KrokRunu(BaseModel):
    step_id: str
    agent_id: str
    attempt: int
    # wartosci StepStatus; Literal, bo api/ importuje z modeli tylko RunStatus i RunnerKind
    status: Literal["running", "finished", "failed", "skipped", "waiting"]
    reason: str | None
    # pomiar kroku (HAR-174): null = brak pomiaru, nigdy 0
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    num_turns: int | None
    session_id: str | None


class PokrycieRunu(BaseModel):
    zaplanowane: int
    wykonane: int
    pominiete: int
    powody: dict[str, str]    # krok -> kod powodu pominiecia (lista D4)
    stan: Literal["sprawdzony", "czesciowo", "niesprawdzony"]


class RunSzczegoly(BaseModel):
    id: str
    workflow: str
    status: RunStatus
    runner_kind: RunnerKind
    reason: str | None
    events: list[ZdarzenieRunu]
    steps: list[KrokRunu]
    pokrycie: PokrycieRunu | None    # brak pomiaru to null, nigdy "0 z 0" (HAR-178)


class RunNaLiscie(BaseModel):
    id: str
    workflow: str
    status: RunStatus
    created_at: str
    project_id: str | None


class Projekt(BaseModel):
    id: str
    runner_id: str
    path: str
    name: str
    stack: list[str]
    created_at: str


class DiffRunu(BaseModel):
    diff: str


class Zintegrowano(BaseModel):
    run_id: str
    at: datetime
    pliki: list[str]


class Zacommitowano(BaseModel):
    run_id: str
    at: datetime
    sha: str


class PlikOdczytany(BaseModel):
    sciezka: str
    tresc: str
    rozmiar: int


class WpisPliku(BaseModel):
    sciezka: str
    katalog: bool


class ListaPlikow(BaseModel):
    pliki: list[WpisPliku]
    obcieto: bool


class PlikZapisany(BaseModel):
    sciezka: str
    rozmiar: int


class BackendRunnera(BaseModel):
    nazwa: str
    auth: Literal["subscription", "api_key"]


class RunnerZespolu(BaseModel):
    id: str
    name: str
    version: str
    backends: list[BackendRunnera]
    online: bool
    last_seen_at: str | None


class DefinicjaKatalogu(BaseModel):
    runner_id: str
    projekt: str
    rodzaj: str
    klucz: str
    nazwa: str
    rola: str | None
    sciezka: str
    odswiezono_at: str


class Katalog(BaseModel):
    odswiezono_at: str | None
    definicje: list[DefinicjaKatalogu]


class KatalogOdswiezony(Katalog):
    pominiete: int


class TokenCsrf(BaseModel):
    csrf: str


class Profil(BaseModel):
    email: str
    zespol: str
    rola: str   # wartość RolaWZespole; samo wyliczenie jest poza api/ (check_layers)
    csrf: str


class Blad(BaseModel):
    """Ciało każdej odpowiedzi błędu. `detail` to napis albo — przy 422 z walidacji — lista."""

    detail: str | list[dict[str, Any]]
    aktywny_run_id: str | None = None   # tylko 409 przy równoległym runie na tym samym tickecie


_OPISY_BLEDOW: dict[int, str] = {
    401: "Brak uwierzytelnienia: sesja albo token runnera nieprawidłowy",
    403: "Zabronione: brak uprawnienia, tokenu CSRF albo ścieżka poza zakresem runnera",
    404: "Nie znaleziono: zasób nie istnieje albo należy do innego zespołu",
    409: "Konflikt: stan nie pozwala na tę operację (runner offline, aktywny run, zajęty adres)",
    422: "Niepoprawne dane wejściowe",
    429: "Zbyt wiele prób",
    500: "Błąd wewnętrzny kokpitu",
}


def odpowiedzi_bledow(*kody: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` trasy: każdy podany kod opisany modelem `Blad`; 500 zawsze.

    Nadpisanie 422 własnym modelem zapobiega temu, by FastAPI dołożył domyślne
    `HTTPValidationError` (dokłada je tylko trasom z parametrami, które 422 nie opisały).
    Dlatego każda trasa z parametrami (ścieżka, zapytanie, nagłówek, treść) MUSI podać 422
    jawnie: pominięcie go nie usuwa odpowiedzi z schematu, tylko zostawia domyślną, której
    kształt nie zgadza się z `Blad`. Pomocnik tego sam nie dokłada.
    """
    wszystkie = sorted({*kody, 500})
    nieznane = [k for k in wszystkie if k not in _OPISY_BLEDOW]
    if nieznane:
        raise ValueError(f"kod bez opisu błędu: {nieznane}")
    return {k: {"model": Blad, "description": _OPISY_BLEDOW[k]} for k in wszystkie}


LIMIT_UZASADNIENIA = 2000   # znaki uzasadnienia decyzji odbioru


class TwierdzenieRunu(BaseModel):
    step_id: str
    artefakt: str


class DowodKroku(BaseModel):
    step_id: str
    capability: str
    source: Literal["engine", "agent"]
    exit_code: int | None       # brak kodu (limit czasu) to null, nigdy 0
    sha256: str
    plik: str


class WerdyktKroku(BaseModel):
    step_id: str
    verdict: str
    zaliczony: bool            # koncowe zdarzenie kroku to step_finished


class BrakOdbioru(BaseModel):
    rodzaj: Literal["pominieta_bramka", "niesprawdzony", "werdykt_fail", "brak_dowodu", "niezmierzone"]
    step_id: str | None
    powod: str


class RyzykaRunu(BaseModel):
    kotwice: list[str] | None   # null = brak danych, [] = brak kotwic
    mutanty: list[str] | None   # zawsze null do czasu HAR-168


class PomiarKrokuOdbioru(BaseModel):
    step_id: str
    cost_usd: float | None      # null = niezmierzone, nigdy 0
    tokens_in: int | None       # brak pomiaru to null, nigdy 0
    tokens_out: int | None


class DecyzjaOdbioru(BaseModel):
    decyzja: Literal["accept", "send_back"]
    user_id: str
    email: str
    uzasadnienie: str | None
    at: datetime


class DowodyRunu(BaseModel):
    run_id: str
    twierdzenia: list[TwierdzenieRunu]
    dowody: list[DowodKroku]
    werdykty: list[WerdyktKroku]
    braki: list[BrakOdbioru]
    ryzyka: RyzykaRunu
    pomiary: list[PomiarKrokuOdbioru]
    decyzja: DecyzjaOdbioru | None


class DecyzjaOdbioruIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decyzja: Literal["accept", "send_back"]
    uzasadnienie: str | None = Field(default=None, max_length=LIMIT_UZASADNIENIA)


class SkrotTicketu(BaseModel):
    id: str
    tytul: str
    stan: str


class KomentarzTicketu(BaseModel):
    author: str
    body: str
    at: datetime | None


class TicketJsonOdp(BaseModel):
    """Kształt `ticket.json` (HAR-41) odwzorowany bez importu z runnera."""
    source: str
    id: str
    url: str | None
    title: str
    description: str
    acceptance_criteria: list[str]
    status: str | None
    priority: str | None
    labels: list[str]
    comments: list[KomentarzTicketu]
    fetched_at: datetime
    raw: dict[str, Any]


class TicketPobrany(BaseModel):
    ticket: TicketJsonOdp
    ostrzezenia: list[str]

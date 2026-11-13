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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from cockpit.db.models import RunnerKind, RunStatus


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


MAX_NAZWA_CAPABILITY = 100
MAX_KOMENDA_CAPABILITY = 1000


class ZmianaProjektu(BaseModel):
    """Zmiana capabilities projektu (HAR-180): nazwa -> komenda; pusta komenda albo null usuwa.
    Poza `capabilities` nic sie nie zmienia (`stack`, `tickets` to 422)."""

    model_config = ConfigDict(extra="forbid")

    capabilities: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAZWA_CAPABILITY)],
        Annotated[str, StringConstraints(max_length=MAX_KOMENDA_CAPABILITY)] | None,
    ]

    @field_validator("capabilities")
    @classmethod
    def _komenda_nie_jest_samymi_bialymi_znakami(
        cls, capabilities: dict[str, str | None],
    ) -> dict[str, str | None]:
        for nazwa, komenda in capabilities.items():
            if komenda and not komenda.strip():
                raise ValueError(f"komenda capability {nazwa} sklada sie z samych bialych znakow")
        return capabilities


class CapabilitiesProjektu(BaseModel):
    project_id: str
    capabilities: dict[str, str]   # pelna mapa z pliku po zmianie, w kolejnosci pliku


ZakresSkilla = Literal["globalny", "lokalny", "nadpisany"]


class SkillNaLiscie(BaseModel):
    id: str
    name: str
    role: str | None   # null przy problemie: skill zepsuty nie ma roli
    consumes: list[str] | None   # null przy problemie: brak pomiaru, nie pusta lista
    produces: list[str] | None
    zakres: ZakresSkilla
    problem: str | None   # komunikat BlednySkill; null, gdy skill jest poprawny


class ListaSkilli(BaseModel):
    odswiezono_at: str | None   # null, gdy katalog projektu nie byl jeszcze odswiezony
    skille: list[SkillNaLiscie]


class SkillSzczegoly(BaseModel):
    id: str
    name: str
    role: str
    consumes: list[str]
    produces: list[str]
    zakres: ZakresSkilla
    sciezka: str
    frontmatter: str   # tekst miedzy liniami granicznymi, bajt w bajt
    brief: str         # wszystko po zamykajacej linii granicznej, bajt w bajt


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

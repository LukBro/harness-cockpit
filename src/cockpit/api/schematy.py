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
from typing import Any, Literal

from pydantic import BaseModel, Field

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

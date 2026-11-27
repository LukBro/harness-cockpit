"""Reguly runow. Jedyne miejsce, ktore dotyka bazy w sprawie runow.

api/ nie wykonuje zapytan - sprawdza to `tools/check_layers.py` w CI.
"""
from __future__ import annotations

import hashlib
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Project, Run, Runner, RunStatus, StepStatus

from cockpit.settings import settings

from .errors import (
    BrakDostepu, NaruszonyNiezmiennik, NiespojneDane, NieZnaleziono, StanNiemozliwy,
    WymagaPotwierdzenia,
)
from .invariants import Niezmiennik
from .kroki import (
    brak_dowodu_ukonczenia, kroki_runu, nieudane_kroki, zastosuj_zdarzenie_kroku,
)
from .pokrycie import (
    PokrycieBramek, WynikBramki, WynikKrokuWeryfikujacego, pokrycie_bramek,
)
from .sesje import na_utc
from .walidacja import sprawdz_pomiar


POWOD_RUNNER_OFFLINE = "runner offline"  # (HAR-37)
KIND_PRZERWANIA = "run_aborted"
KIND_ANULOWANIA = "run_cancelled"   # anulowanie przez czlowieka (HAR-126); w `RODZAJE_CZLOWIEKA`
POWOD_ANULOWANIA = "anulowany przez człowieka"
# Zdarzenia odbioru zapisuje wylacznie kokpit, na zadanie czlowieka (HAR-179): runner nie moze
# ich zglosic (oba sa w `RODZAJE_CZLOWIEKA`, HAR-234), bo `committed` odblokowywalby sie na
# sfalszowanym `integrated`.
KIND_ZINTEGROWANO = "integrated"
KIND_ZACOMMITOWANO = "committed"
KIND_ODBIORU = "acceptance"   # decyzja odbioru dowodow (HAR-170); tez w `RODZAJE_CZLOWIEKA`
# "uruchom mimo to": czlowiek pomija ostrzezenie o atrapie bramki (HAR-246); w `RODZAJE_CZLOWIEKA`
KIND_POMINIECIA = "atrapa_pominieta"
TEKST_POMINIECIA = "ostrzeżenie pominięte przez człowieka"
# poczatek tekstu `warning` o atrapie z `runner.silnik`; odroznia go od ostrzezen walidatora
PREFIKS_ATRAPY = "atrapa bramki — "
LIMIT_LISTY_RUNOW = 100  # staly limit listy, bez paginacji (HAR-72)
STATUSY_AKTYWNE = (RunStatus.pending, RunStatus.running, RunStatus.waiting)  # (HAR-58)
_KONCZACE_KROK = frozenset({"step_finished", "step_failed"})
KIND_MANIFESTU = "context_manifest"   # manifest warstw instrukcji kroku (HAR-169); nie rusza statusu runu
_PROBY_SEQ = 5  # ile razy powtarzamy wyliczenie seq po kolizji (HAR-25)
_OGRANICZENIE_SEQ = "uq_event_run_seq"  # nazwa z UniqueConstraint(run_id, seq) w modelu
_KOLUMNY_SEQ_SQLITE = frozenset({"event.run_id", "event.seq"})
_OGRANICZENIE_ZDARZENIA = "uq_event_run_event"  # UniqueConstraint(run_id, event_id) w modelu
_KOLUMNY_ZDARZENIA_SQLITE = frozenset({"event.run_id", "event.event_id"})


def _kolizja_unikalnosci(
    e: IntegrityError, nazwa_pg: str, kolumny_sqlite: frozenset[str],
) -> bool:
    """Czy to naruszenie wlasnie tego ograniczenia unikalnosci, a nie inny blad zapisu."""
    sqlstate = getattr(e.orig, "sqlstate", None)
    if sqlstate is not None:
        # PostgreSQL: rozpoznajemy po kodzie i nazwie ograniczenia, nie po tresci
        nazwa = getattr(getattr(e.orig, "orig", None), "constraint_name", None)
        return sqlstate == "23505" and nazwa == nazwa_pg
    # SQLite nie niesie nazwy ograniczenia, tylko liste kolumn w komunikacie
    prefiks = "UNIQUE constraint failed: "
    komunikat = str(e.orig)
    if not komunikat.startswith(prefiks):
        return False
    kolumny = {k.strip() for k in komunikat.removeprefix(prefiks).split(",")}
    return kolumny == kolumny_sqlite


def _kolizja_numeru(e: IntegrityError) -> bool:
    """Czy to naruszenie unikalnosci (run_id, seq), a nie inny blad zapisu (HAR-138)."""
    return _kolizja_unikalnosci(e, _OGRANICZENIE_SEQ, _KOLUMNY_SEQ_SQLITE)


def _kolizja_zdarzenia(e: IntegrityError) -> bool:
    """Czy to naruszenie unikalnosci (run_id, event_id): to samo zdarzenie dwa razy (HAR-39)."""
    return _kolizja_unikalnosci(e, _OGRANICZENIE_ZDARZENIA, _KOLUMNY_ZDARZENIA_SQLITE)


async def _zdarzenie_o_id(
    s: AsyncSession, run_id: str, tenant_id: str, event_id: str,
) -> Event | None:
    return (
        await s.execute(
            select(Event).where(
                Event.run_id == run_id, Event.tenant_id == tenant_id,
                Event.event_id == event_id,
            )
        )
    ).scalar_one_or_none()


async def _nastepny_seq(s: AsyncSession, run_id: str, tenant_id: str) -> int:
    return (
        await s.execute(
            select(func.coalesce(func.max(Event.seq), 0)).where(
                Event.run_id == run_id, Event.tenant_id == tenant_id
            )
        )
    ).scalar_one() + 1


async def _z_ponowieniem_seq(s: AsyncSession, podejscie):
    """Jedyna petla ponawiania numeru zdarzenia (HAR-25, HAR-211).

    `podejscie()` przygotowuje zapis (liczy seq, dodaje zdarzenie) i zwraca wynik; helper
    robi `commit`. Kolizja (run_id, seq) cofa cala transakcje i wola `podejscie` od nowa,
    kazdy inny `IntegrityError` leci wyzej po rollbacku. Po `_PROBY_SEQ` probach StanNiemozliwy.
    """
    for _ in range(_PROBY_SEQ):
        wynik = await podejscie()
        try:
            await s.commit()
        except IntegrityError as e:
            await s.rollback()
            if not _kolizja_numeru(e):
                raise
            continue
        return wynik
    raise StanNiemozliwy(
        f"nie udalo sie nadac numeru zdarzenia po {_PROBY_SEQ} probach - ponow zapis"
    )


def komunikat_niepelnego_strumienia(zapisano: int, wyslano: int) -> str | None:
    if zapisano == wyslano:
        return None
    return f"niepelny strumien: zapisano {zapisano} z {wyslano} zdarzen output"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def runner_po_tokenie(
    s: AsyncSession, authorization: str | None, teraz: datetime | None = None,
) -> Runner:
    """Runner z tokenu. Odwolany i wygasly token jest odrzucany (odwolanie wazniejsze od
    wygasniecia); dopiero przyjety token zapisuje chwile ostatniego uzycia (HAR-119)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise BrakDostepu("brak tokenu runnera")
    token = authorization.removeprefix("Bearer ")
    r = (
        await s.execute(select(Runner).where(Runner.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if r is None:
        raise BrakDostepu("token nieznany - wygeneruj nowy w Ustawieniach")
    teraz = teraz or datetime.now(timezone.utc)
    runner_id, tenant_id = r.id, r.tenant_id
    # Warunek w samym zapisie: odwolanie miedzy odczytem a zapisem nie przepusci tokenu.
    zmienione = (
        await s.execute(
            update(Runner)
            .where(
                Runner.id == runner_id, Runner.tenant_id == tenant_id,
                Runner.odwolany_at.is_(None),
                (Runner.token_wazny_do.is_(None)) | (Runner.token_wazny_do > teraz),
            )
            .values(token_uzyty_at=teraz)
            .execution_options(synchronize_session=False)
        )
    ).rowcount
    # Ponowny odczyt: po zapisie (swieze wartosci) albo po odrzuceniu (wybor komunikatu).
    r = (
        await s.execute(
            select(Runner).where(Runner.id == runner_id, Runner.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if zmienione == 0:
        if r is not None and r.odwolany_at is not None:
            raise BrakDostepu("token odwołany - wygeneruj nowy w Ustawieniach")
        raise BrakDostepu("token wygasł - wygeneruj nowy w Ustawieniach")
    await s.commit()
    return r


async def zamelduj_runnera(
    s: AsyncSession, runner: Runner, nazwa: str, wersja: str,
    backendy: Sequence[dict],
) -> Runner:
    runner.name, runner.version = nazwa, wersja
    runner.backends = list(backendy)    # zastepuje deklaracje, nie mutuje w miejscu
    runner.last_seen_at = datetime.now(timezone.utc)
    await s.commit()
    return runner


async def odnotuj_heartbeat(s: AsyncSession, runner: Runner) -> Runner:
    runner.last_seen_at = datetime.now(timezone.utc)
    await s.commit()
    return runner


def runner_online(
    runner: Runner, polaczone: Collection[str], teraz: datetime | None = None,
) -> bool:
    """Online: otwarty strumien ORAZ heartbeat mlodszy niz `runner_offline_after_s`.

    Zmiana reguly HAR-26 (HAR-35, D12): sam strumien nie wystarcza. Strumien
    polotwarty (runner zabity `kill -9`, zerwana siec) bywa po stronie serwera
    "otwarty" dlugo, wiec dowodem zycia jest heartbeat. Wiek rowny progowi to offline.
    """
    if runner.id not in polaczone or runner.last_seen_at is None:
        return False
    teraz = teraz or datetime.now(timezone.utc)
    return teraz - na_utc(runner.last_seen_at) < timedelta(seconds=settings.runner_offline_after_s)


async def runnery_zespolu(
    s: AsyncSession, tenant_id: str, polaczone: Collection[str],
    teraz: datetime | None = None,
) -> list[dict]:
    """Runnery zespolu ze stanem online (regula `runner_online`), wersja i backendami (HAR-68)."""
    teraz = teraz or datetime.now(timezone.utc)   # jeden czas dla calej listy
    runnery = (await s.execute(
        select(Runner).where(Runner.tenant_id == tenant_id).order_by(Runner.name, Runner.id)
    )).scalars().all()
    lista = []
    for runner in runnery:
        widziany = runner.last_seen_at
        if widziany is not None:
            widziany = na_utc(widziany).astimezone(timezone.utc).isoformat()
        lista.append({
            "id": runner.id,
            "name": runner.name,
            "version": runner.version,
            "backends": [dict(b) for b in runner.backends],
            "online": runner_online(runner, polaczone, teraz),
            "last_seen_at": widziany,
        })
    return lista


async def pierwszy_runner_online(
    s: AsyncSession, tenant_id: str, polaczone: Collection[str], runner_id: str | None = None,
) -> Runner | None:
    """Online runner zespolu o najmniejszym id albo None; z `runner_id` tylko ten runner."""
    zapytanie = select(Runner).where(Runner.tenant_id == tenant_id)
    if runner_id is not None:
        zapytanie = zapytanie.where(Runner.id == runner_id)
    runnery = (await s.execute(zapytanie.order_by(Runner.id))).scalars().all()
    return next((r for r in runnery if runner_online(r, polaczone)), None)


async def utworz_run(
    s: AsyncSession,
    tenant_id: str,
    workflow: str,
    polaczone: Collection[str] = frozenset(),
    *,
    ticket_id: str | None = None,
    potwierdz_rownolegly: bool = False,
    project_id: str | None = None,
) -> Run:
    """Run powstaje tylko wtedy, gdy zespol ma runnera z otwartym strumieniem
    (`polaczone`; HAR-2, HAR-26).

    Z `project_id` (HAR-80) run idzie do runnera tego projektu: projekt obcego zespolu
    albo nieistniejacy to NieZnaleziono, a runner projektu offline albo spoza zespolu to
    StanNiemozliwy - bez przekierowania na inny runner.

    Drugi run na tickecie z aktywnym runem wymaga `potwierdz_rownolegly` (HAR-58).
    To ostrzezenie, nie blokada: dwa rownoczesne starty moga oba przejsc."""
    if ticket_id is not None and not potwierdz_rownolegly:
        aktywny = (
            await s.execute(
                select(Run.id)
                .where(Run.tenant_id == tenant_id, Run.ticket_id == ticket_id,
                       Run.status.in_(STATUSY_AKTYWNE))
                .order_by(Run.created_at, Run.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if aktywny is not None:
            raise WymagaPotwierdzenia(aktywny)
    runner_projektu = None
    if project_id is not None:
        runner_projektu = (
            await s.execute(
                select(Project.runner_id).where(
                    Project.id == project_id, Project.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if runner_projektu is None:
            raise NieZnaleziono("projekt nieznany")
    runner = await pierwszy_runner_online(s, tenant_id, polaczone, runner_projektu)
    if runner is None:
        raise StanNiemozliwy(
            "runner offline - uruchom runnera na swojej maszynie (cockpit-runner)"
        )

    run = Run(tenant_id=tenant_id, runner_id=runner.id, workflow=workflow,
              status=RunStatus.running, ticket_id=ticket_id, project_id=project_id)
    s.add(run)
    await s.commit()
    return run


async def run_dla_najemcy(s: AsyncSession, run_id: str, tenant_id: str) -> Run:
    """Kazde siegniecie po run przechodzi przez tenant_id (BRO-85)."""
    run = await s.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id:
        raise NieZnaleziono("run nieznany")
    return run


async def run_runnera(s: AsyncSession, run_id: str, runner: Runner) -> Run:
    """Zdarzenia przyjmujemy tylko od runnera, ktoremu run zlecono (HAR-19).

    Run cudzego runnera jest dla niego nieznany - ten sam komunikat co dla
    nieistniejacego, zeby nie ujawniac istnienia runu.
    """
    run = await s.get(Run, run_id)
    if (
        run is None
        or run.tenant_id != runner.tenant_id
        or run.runner_id != runner.id
    ):
        raise NieZnaleziono("run nieznany")
    return run


# Status runu wylicza kokpit ze stanow krokow i ze zdarzenia `run_finished` (HAR-78);
# deklaracja runnera nie jest zrodlem prawdy. Zdarzenia kroku same nie koncza runu.
_POLECENIA_RUNU = frozenset({"run_step", "run_workflow"})  # (HAR-80)
_KONIEC_RUNU = "run_finished"
# Rodzaje zdarzen zarezerwowane dla decyzji czlowieka; runner ich nie zapisuje
# (HAR-234, approval_required; HAR-179 rozszerza te sama liste).
RODZAJE_CZLOWIEKA: frozenset[str] = frozenset({
    "decision",
    "integrated",
    "committed",
    "acceptance",
    "run_cancelled",    # anulowanie runu przez czlowieka (HAR-126); zapisuje je kokpit
    "atrapa_pominieta",     # pominiecie ostrzezenia o atrapie (HAR-246); zapisuje je kokpit
})
# `step_aborted` nie rusza runu `waiting` (HAR-47)
_STATUSY_PRZERYWANE_KROKIEM = frozenset({RunStatus.running, RunStatus.to_accept})
# Statusy, w ktorych `run_finished` wymaga dowodu ukonczenia krokow (takze `to_accept`:
# krok dodany po odbiorze bez konca nie zostawia runu do odbioru).
_STATUSY_WYMAGAJACE_DOWODU_PO_RUN_FINISHED = frozenset({RunStatus.running, RunStatus.to_accept})
# `waiting` czeka na decyzje czlowieka: `run_finished` go nie konczy, ale nieudany krok tak
_STATUSY_ROZSTRZYGANE_RUN_FINISHED = _STATUSY_WYMAGAJACE_DOWODU_PO_RUN_FINISHED | {
    RunStatus.waiting,
}
POWOD_BRAK_DOWODU = "brak dowodu ukończenia kroków"


def nastepny_status(
    obecny: RunStatus, kind: str, nieudane: Sequence[str] = (), bez_dowodu: bool = False,
) -> RunStatus:
    """Czysta maszyna przejsc runu; `failed`, `aborted`, `skipped`, `pending` sa koncowe.

    `bez_dowodu`: brak krokow albo krok bez konca przy `run_finished`; run `running`
    albo `to_accept` dostaje wtedy `failed` zamiast `to_accept` (`evidence_required`)."""
    if kind == "step_waiting" and obecny == RunStatus.running:
        return RunStatus.waiting
    if kind == "step_started" and obecny == RunStatus.waiting:
        return RunStatus.running
    if kind == "step_aborted" and obecny in _STATUSY_PRZERYWANE_KROKIEM:
        return RunStatus.aborted     # runner przerywa krok sam, np. brak miejsca (HAR-47)
    if kind == _KONIEC_RUNU and obecny in _STATUSY_ROZSTRZYGANE_RUN_FINISHED:
        # porazka kroku przykrywa sukces, ale sukces nie cofa porazki
        if nieudane:
            return RunStatus.failed
        if obecny in _STATUSY_WYMAGAJACE_DOWODU_PO_RUN_FINISHED:
            return RunStatus.failed if bez_dowodu else RunStatus.to_accept
    return obecny


def powod_nieudanych_krokow(nieudane: Sequence[str]) -> str:
    return "nieudane kroki: " + ", ".join(nieudane)


_STATUSY_Z_POKRYCIEM = frozenset({RunStatus.to_accept, RunStatus.failed})


async def _powod_pominiecia(s: AsyncSession, run: Run, step_id: str) -> str | None:
    """Kod D4 z poczatku tekstu ostatniego `step_skipped` kroku w tym runie i zespole."""
    tekst = (
        await s.execute(
            select(Event.text).where(
                Event.run_id == run.id, Event.tenant_id == run.tenant_id,
                Event.step_id == step_id, Event.kind == "step_skipped",
            ).order_by(Event.seq.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if tekst is None:
        return None
    return tekst.partition(":")[0]


async def pokrycie_runu(
    s: AsyncSession, run: Run, kroki_weryfikujace: Sequence[str] | None,
) -> PokrycieBramek | None:
    """Pokrycie bramkami z listy runnera i wierszy krokow (HAR-178); liczy `pokrycie_bramek`.

    Brak pomiaru (None): brak listy, pusta lista, krok bez wiersza albo wciaz w toku, powod
    pominiecia spoza listy D4. Nigdy wyjatek: nie da sie zmierzyc to nie zero bramek."""
    if not kroki_weryfikujace:
        return None
    ostatnie: dict[tuple[str, str], StepStatus] = {}
    for w in await kroki_runu(s, run):   # rosnaco po probie: pozniejsza podmienia wczesniejsza
        ostatnie[(w.step_id, w.agent_id)] = w.status
    wyniki: list[WynikKrokuWeryfikujacego] = []
    for krok in dict.fromkeys(kroki_weryfikujace):
        stany = {st for (id_kroku, _), st in ostatnie.items() if id_kroku == krok}
        if not stany or stany & {StepStatus.running, StepStatus.waiting}:
            return None
        powod = None
        if StepStatus.failed in stany:
            wynik = WynikBramki.nieudana
        elif StepStatus.finished in stany:
            wynik = WynikBramki.wykonana     # jakikolwiek agent kroku sprawdzil
        else:
            wynik = WynikBramki.pominieta
            powod = await _powod_pominiecia(s, run, krok)
        try:
            wyniki.append(WynikKrokuWeryfikujacego(krok, wynik, powod))
        except ValueError:
            return None      # kod spoza D4 albo brak powodu: brak pomiaru
    return pokrycie_bramek(wyniki)


def _zapisz_pokrycie(run: Run, pokrycie: PokrycieBramek) -> None:
    run.bramki_zaplanowane = pokrycie.zaplanowane
    run.bramki_wykonane = pokrycie.wykonane
    run.bramki_pominiete = pokrycie.pominiete
    run.stan_pokrycia = pokrycie.stan.value
    run.bramki_powody = {krok: powod.value for krok, powod in pokrycie.powody.items()}


def pokrycie_z_kolumn(run: Run) -> dict | None:
    """Zapisane pokrycie runu; wszystkie kolumny puste to brak pomiaru (None)."""
    if run.stan_pokrycia is None:
        return None
    return {
        "zaplanowane": run.bramki_zaplanowane, "wykonane": run.bramki_wykonane,
        "pominiete": run.bramki_pominiete, "powody": run.bramki_powody or {},
        "stan": run.stan_pokrycia,
    }


async def zapisz_zdarzenie(
    s: AsyncSession,
    run: Run,
    *,
    event_id: str,
    kind: str,
    step_id: str = "",
    agent_id: str = "",
    agent_type: str = "",
    text: str = "",
    dane: dict | None = None,
    cost_usd: float | None = None,
    cost_source: str = "unavailable",
    tokens: int | None = None,
    duration_ms: int | None = None,
    wyslane_output: int | None = None,
    text_full_len: int | None = None,
    attempt: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    num_turns: int | None = None,
    session_id: str | None = None,
    kroki_weryfikujace: Sequence[str] | None = None,
) -> tuple[Event, bool]:
    """Zapisuje zdarzenie od runnera; zwraca `(zdarzenie, nowe)`.

    Ponowienie z tym samym `event_id` w tym runie zwraca zapisane zdarzenie
    i `False`: bez zapisu, bez zmiany statusu i powodu runu (HAR-39).
    Rodzaj z `RODZAJE_CZLOWIEKA` jest odrzucany (decyzje zapisuje tylko czlowiek).
    """
    if kind in RODZAJE_CZLOWIEKA:
        raise NaruszonyNiezmiennik(
            Niezmiennik.approval_required,
            f"rodzaj zdarzenia {kind!r} jest zarezerwowany dla decyzji czlowieka; "
            "runner nie moze go zapisac",
        )
    naruszenie = sprawdz_pomiar(cost_usd, cost_source)
    if naruszenie is not None:
        raise NaruszonyNiezmiennik(naruszenie.niezmiennik, naruszenie.szczegol)
    if text_full_len is not None and text_full_len < len(text):
        raise NiespojneDane(
            f"text_full_len ({text_full_len} znakow) jest krotsza niz przyslany tekst "
            f"({len(text)} znakow)"
        )
    if kroki_weryfikujace and kind != _KONIEC_RUNU:
        raise NiespojneDane("kroki_weryfikujace wolno przeslac tylko w zdarzeniu run_finished")
    if dane and "manifest" in dane and kind != KIND_MANIFESTU:
        raise NiespojneDane("manifest wolno przeslac tylko w zdarzeniu context_manifest")
    if dane and kind not in _KONCZACE_KROK and dane.keys() - {"manifest"}:
        raise NiespojneDane(
            "dowod, verdict, kotwice i artefakty wolno przeslac tylko w zdarzeniu konca kroku")
    # tekst ponad limit nigdy nie udaje calosci: znacznik i pelna dlugosc (HAR-56)
    pelna = max(len(text), text_full_len or 0)
    limit = settings.limit_tekstu_zdarzenia
    zapisany = text[:limit]
    obciety = pelna > len(zapisany)

    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy
    run_id, tenant_id = run.id, run.tenant_id

    istniejace = await _zdarzenie_o_id(s, run_id, tenant_id, event_id)
    if istniejace is not None:
        return istniejace, False

    async def podejscie() -> tuple[Event, bool]:
        # blokada wiersza: status odczytany tu nie zmieni sie do commitu, wiec powod
        # przerwania dostaje tylko zdarzenie, ktore faktycznie przestawilo run (HAR-47)
        # obiekt spoza sesji nie ma wiersza do zablokowania, a zapis zdarzenia i tak
        # padnie na kluczu obcym - blad ma wyjsc stamtad, nie z odswiezenia.
        # Obiekt wygaszony rollbackiem kolizji nadal jest w sesji (`in` daje True).
        if run in s:
            await s.refresh(run, with_for_update=True)
        status_przed = run.status
        seq = await _nastepny_seq(s, run_id, tenant_id)
        if kind in _KONCZACE_KROK and wyslane_output is not None:
            zapisano = (
                await s.execute(
                    select(func.count()).select_from(Event).where(
                        Event.run_id == run_id,
                        Event.tenant_id == tenant_id,
                        Event.step_id == step_id,
                        Event.kind == "output",
                    )
                )
            ).scalar_one()
            komunikat = komunikat_niepelnego_strumienia(zapisano, wyslane_output)
            if komunikat is not None:
                run.reason = komunikat

        ev = Event(tenant_id=tenant_id, run_id=run_id, seq=seq, step_id=step_id,
                   event_id=event_id, agent_id=agent_id, agent_type=agent_type, kind=kind,
                   text=zapisany, cost_usd=cost_usd,
                   cost_source=cost_source, tokens=tokens, duration_ms=duration_ms,
                   text_truncated=obciety, text_full_len=pelna, attempt=attempt,
                   dane=dane or None)
        await _zastosuj_zdarzenie(
            s, run, ev, status_przed, kind=kind, step_id=step_id, agent_id=agent_id,
            attempt=attempt, zapisany=zapisany, cost_usd=cost_usd, tokens_in=tokens_in,
            tokens_out=tokens_out, num_turns=num_turns, session_id=session_id,
            kroki_weryfikujace=kroki_weryfikujace)
        return ev, True

    try:
        return await _z_ponowieniem_seq(s, podejscie)
    except IntegrityError as e:
        if not _kolizja_zdarzenia(e):
            raise
        # ten sam event_id zapisany rownolegle: oddajemy zapisane, bez zmian runu
        # (helper cofnal transakcje)
        istniejace = await _zdarzenie_o_id(s, run_id, tenant_id, event_id)
        if istniejace is None:
            raise   # kolizja z cudzym zespolem: nie jest to nasz duplikat
        return istniejace, False


async def _zastosuj_zdarzenie(
    s: AsyncSession, run: Run, ev: Event, status_przed: RunStatus, *,
    kind: str, step_id: str, agent_id: str, attempt: int | None, zapisany: str,
    cost_usd: float | None, tokens_in: int | None, tokens_out: int | None,
    num_turns: int | None, session_id: str | None,
    kroki_weryfikujace: Sequence[str] | None,
) -> None:
    """Dodaje zdarzenie do sesji i przelicza krok, status, powod i pokrycie runu (bez commitu)."""
    # bez autoflush: zdarzenie i zmiany krokow ida do bazy w `commit`; zapytania widza
    # wiersze krokow z wczesniejszych zdarzen, a w tej transakcji jest jedno zdarzenie.
    with s.no_autoflush:
        s.add(ev)
        await zastosuj_zdarzenie_kroku(
            s, run, kind, step_id, agent_id, attempt, zapisany,
            cost_usd=cost_usd, tokens_in=tokens_in, tokens_out=tokens_out,
            num_turns=num_turns, session_id=session_id)
        nieudane = await nieudane_kroki(s, run) if kind == _KONIEC_RUNU else []
        # Trzy czlony tylko oszczedzaja zapytanie (wynik daje `nastepny_status`, ktory
        # czyta `bez_dowodu` wylacznie dla tych statusow, `run_finished` i braku nieudanych).
        bez_dowodu = (
            kind == _KONIEC_RUNU and not nieudane
            and status_przed in _STATUSY_WYMAGAJACE_DOWODU_PO_RUN_FINISHED
            and await brak_dowodu_ukonczenia(s, run)
        )
        run.status = nastepny_status(status_przed, kind, nieudane, bez_dowodu)
        if (kind == "step_aborted" and status_przed != RunStatus.aborted
                and run.status == RunStatus.aborted):
            run.reason = zapisany
        if (kind == _KONIEC_RUNU and status_przed != RunStatus.failed
                and run.status == RunStatus.failed):
            run.reason = (
                powod_nieudanych_krokow(nieudane) if nieudane else POWOD_BRAK_DOWODU
            )
        if kind == _KONIEC_RUNU and run.status in _STATUSY_Z_POKRYCIEM:
            pokrycie = await pokrycie_runu(s, run, kroki_weryfikujace)
            if pokrycie is not None:
                _zapisz_pokrycie(run, pokrycie)


async def zdarzenia_runu(s: AsyncSession, run: Run, po_seq: int | None = None) -> list[Event]:
    zapytanie = select(Event).where(Event.run_id == run.id, Event.tenant_id == run.tenant_id)
    if po_seq is not None:
        zapytanie = zapytanie.where(Event.seq > po_seq)
    return list((await s.execute(zapytanie.order_by(Event.seq))).scalars().all())


_STATUSY_TRWAJACE = (RunStatus.running, RunStatus.waiting)


async def przerwij_runy_offline(
    s: AsyncSession, teraz: datetime, polaczone: Collection[str],
) -> list[str]:
    """Run w toku lub oczekujacy na runnerze offline dostaje `aborted` (HAR-37).

    Status i zdarzenie przerwania zapisujemy w jednej transakcji. Update jest
    warunkowy: gdy `step_finished` z innej sesji zdazyl zmienic status, run
    zostaje jak byl i nie dostaje zdarzenia. Zwraca id faktycznie przerwanych.
    """
    wiersze = (
        await s.execute(
            select(Run, Runner)
            .join(Runner, (Run.runner_id == Runner.id) & (Run.tenant_id == Runner.tenant_id))
            .where(Run.status.in_(_STATUSY_TRWAJACE))
        )
    ).all()
    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy
    cele = [(run.id, run.tenant_id) for run, runner in wiersze
            if not runner_online(runner, polaczone, teraz)]

    przerwane: list[str] = []
    for run_id, tenant_id in cele:
        async def podejscie(run_id=run_id, tenant_id=tenant_id) -> bool:
            wynik = await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.tenant_id == tenant_id,
                       Run.status.in_(_STATUSY_TRWAJACE))
                .values(status=RunStatus.aborted, reason=POWOD_RUNNER_OFFLINE)
            )
            if wynik.rowcount != 1:
                await s.rollback()      # wyscig: status zmieniony przez inna sesje
                return False
            s.add(Event(tenant_id=tenant_id, run_id=run_id, kind=KIND_PRZERWANIA,
                        text=POWOD_RUNNER_OFFLINE,
                        text_full_len=len(POWOD_RUNNER_OFFLINE),
                        seq=await _nastepny_seq(s, run_id, tenant_id)))
            return True

        if await _z_ponowieniem_seq(s, podejscie):
            przerwane.append(run_id)
    return przerwane


async def zdarzenia_przerwania(
    s: AsyncSession, run_ids: Sequence[str],
) -> list[tuple[str, dict]]:
    """Ostatnie zdarzenie przerwania kazdego runu, w ksztalcie dla szyny (HAR-37)."""
    wynik: list[tuple[str, dict]] = []
    for run_id in run_ids:
        run = await s.get(Run, run_id)
        if run is None:
            continue
        ev = (
            await s.execute(
                select(Event)
                .where(Event.run_id == run_id, Event.tenant_id == run.tenant_id,
                       Event.kind == KIND_PRZERWANIA)
                .order_by(Event.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ev is None:
            continue
        wynik.append((run_id, {
            **zdarzenie_na_szyne(ev), "status": run.status.value, "reason": run.reason,
        }))
    return wynik


async def anuluj_run(
    s: AsyncSession, run_id: str, tenant_id: str, user_id: str,
) -> tuple[Run, Event, dict]:
    """Czlowiek anuluje run `running` albo `waiting`: `aborted` z powodem i zdarzenie
    `run_cancelled` z `user_id` (HAR-126). Zwraca `(run, zdarzenie, polecenie dla runnera)`.

    Obcy lub nieznany run to NieZnaleziono; kazdy inny status to StanNiemozliwy. Status
    czytamy pod blokada wiersza (jak `zapisz_zdarzenie`), wiec rownolegly `run_finished` albo
    drugie anulowanie nie przejdzie tego samego sprawdzenia: przegrany widzi nowy status.
    """
    run = await run_dla_najemcy(s, run_id, tenant_id)
    run_id = run.id    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy
    async def podejscie() -> tuple[Run, Event, dict]:
        await s.refresh(run, with_for_update=True)
        if run.status not in _STATUSY_TRWAJACE:
            stan = run.status.value     # przed rollbackiem, ktory wygasza obiekt
            await s.rollback()      # zwalnia blokade wiersza
            raise StanNiemozliwy(
                f"run w stanie {stan} nie moze byc anulowany "
                "(tylko run w toku albo oczekujacy)"
            )
        run.status = RunStatus.aborted
        run.reason = POWOD_ANULOWANIA
        ev = Event(tenant_id=tenant_id, run_id=run_id, kind=KIND_ANULOWANIA,
                   text=POWOD_ANULOWANIA, text_full_len=len(POWOD_ANULOWANIA),
                   dane={"user_id": user_id},
                   seq=await _nastepny_seq(s, run_id, tenant_id))
        s.add(ev)
        return run, ev, {"type": "cancel_run", "run_id": run_id}

    return await _z_ponowieniem_seq(s, podejscie)


async def pomin_ostrzezenie(
    s: AsyncSession, run_id: str, tenant_id: str, user_id: str, step_id: str,
) -> Event:
    """Czlowiek pomija ostrzezenie o atrapie kroku (HAR-246): zdarzenie z `user_id`, status
    runu bez zmian.

    Obcy lub nieznany run to NieZnaleziono; brak ostrzezenia atrapy nowszego niz ostatnie
    pominiecie tego kroku to StanNiemozliwy. Warunek pod blokada wiersza runu, po wyliczeniu
    numeru: zapis, ktory wejdzie pozniej, zajmie ten numer i wymusi ponowne sprawdzenie.
    """
    run = await run_dla_najemcy(s, run_id, tenant_id)
    run_id = run.id    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy

    async def podejscie() -> Event:
        await s.refresh(run, with_for_update=True)
        seq = await _nastepny_seq(s, run_id, tenant_id)
        ostatni = (
            await s.execute(
                select(Event.kind).where(
                    Event.run_id == run_id, Event.tenant_id == tenant_id,
                    Event.step_id == step_id,
                    or_(
                        Event.kind == KIND_POMINIECIA,
                        and_(Event.kind == "warning",
                             Event.text.startswith(PREFIKS_ATRAPY, autoescape=True)),
                    ),
                ).order_by(Event.seq.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if ostatni != "warning":
            await s.rollback()      # zwalnia blokade wiersza
            raise StanNiemozliwy("krok nie ma ostrzeżenia o atrapie bramki do pominięcia")
        ev = Event(tenant_id=tenant_id, run_id=run_id, kind=KIND_POMINIECIA, step_id=step_id,
                   text=TEKST_POMINIECIA, text_full_len=len(TEKST_POMINIECIA),
                   dane={"user_id": user_id}, seq=seq)
        s.add(ev)
        return ev

    return await _z_ponowieniem_seq(s, podejscie)


def czas_zdarzenia(ev: Event) -> str:
    """Czas zdarzenia jako ISO 8601 w UTC (HAR-121); SQLite oddaje daty bez strefy - to UTC."""
    return na_utc(ev.at).astimezone(timezone.utc).isoformat()


def zdarzenie_na_szyne(ev: Event) -> dict:
    """Ksztalt zdarzenia runu w strumieniu SSE; `at` jak w `GET /api/runs/{id}` (HAR-121)."""
    return {
        "seq": ev.seq, "kind": ev.kind, "text": ev.text, "step_id": ev.step_id,
        "text_truncated": ev.text_truncated, "text_full_len": ev.text_full_len,
        "at": czas_zdarzenia(ev),
    }


async def polecenie_aktualne(
    s: AsyncSession, polecenie: dict, runner_id: str, tenant_id: str,
) -> bool:
    """Czy polecenie wolno jeszcze wyslac runnerowi (HAR-37).

    Odstepstwo od sygnatury z ticketu: `runner_id` i `tenant_id`, bo CLAUDE.md
    wymaga `tenant_id` w kazdym zapytaniu, a polecenie go nie niesie. Krok runu
    jest aktualny tylko dla runu `running` tego runnera i zespolu.
    """
    if polecenie.get("type") not in _POLECENIA_RUNU:
        return True
    run_id = polecenie.get("run_id")
    if not run_id:
        return False
    status = (
        await s.execute(
            select(Run.status).where(
                Run.id == run_id, Run.tenant_id == tenant_id, Run.runner_id == runner_id
            )
        )
    ).scalar_one_or_none()
    return status == RunStatus.running


async def runy_zespolu(
    s: AsyncSession, tenant_id: str, limit: int = LIMIT_LISTY_RUNOW,
    project_id: str | None = None,
) -> list[dict]:
    """Runy zespolu od najnowszego, bez zdarzen (HAR-72); opcjonalnie jednego projektu (HAR-79)."""
    zapytanie = select(Run).where(Run.tenant_id == tenant_id)
    if project_id is not None:   # filtr przed limitem
        zapytanie = zapytanie.where(Run.project_id == project_id)
    runy = (await s.execute(
        zapytanie.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit)
    )).scalars().all()
    lista = []
    for run in runy:
        lista.append({
            "id": run.id,
            "workflow": run.workflow,
            "status": run.status,
            "created_at": na_utc(run.created_at).astimezone(timezone.utc).isoformat(),
            "project_id": run.project_id,
        })
    return lista

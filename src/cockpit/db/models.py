"""Model danych szkieletu. tenant_id od pierwszej migracji (BRO-85, BRO-118)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    waiting = "waiting"        # czeka na decyzje czlowieka (BRO-107)
    to_accept = "to_accept"    # sukces to "do odbioru", nie "gotowe" (BRO-108)
    failed = "failed"
    skipped = "skipped"
    aborted = "aborted"        # runner offline, brak miejsca (BRO-109)


class RunnerKind(str, enum.Enum):
    local = "local"
    remote = "remote"
    platform = "platform"      # tryb platformowy, odlozony (BRO-88)


class RolaWZespole(str, enum.Enum):
    """Rola uzytkownika w zespole (BRO-118). Nie mylic z `Rola` skilla w services/invariants.py."""
    owner = "owner"
    admin = "admin"
    member = "member"
    viewer = "viewer"


class Tenant(Base):
    __tablename__ = "tenant"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Runner(Base):
    __tablename__ = "runner"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(50), default="")
    token_hash: Mapped[str] = mapped_column(String(128))   # nigdy sekret jawnie (BRO-98)
    # [{"nazwa": str, "auth": "subscription" | "api_key"}] - deklaracja runnera (HAR-34)
    backends: Mapped[list[dict]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Run(Base):
    __tablename__ = "run"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"), index=True)
    runner_id: Mapped[str | None] = mapped_column(ForeignKey("runner.id"), default=None)
    runner_kind: Mapped[RunnerKind] = mapped_column(Enum(RunnerKind), default=RunnerKind.local)
    workflow: Mapped[str] = mapped_column(String(200), default="szkielet")
    base_commit: Mapped[str | None] = mapped_column(String(40), default=None)  # SHA, nie nazwa galezi
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.pending)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    events: Mapped[list["Event"]] = relationship(back_populates="run")


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),
        UniqueConstraint("run_id", "event_id", name="uq_event_run_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # identyfikator nadany przez runnera; ponowienie z tym samym nie tworzy duplikatu (HAR-39)
    event_id: Mapped[str] = mapped_column(String(36), default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)      # numeracja pod Last-Event-ID (BRO-76)
    step_id: Mapped[str] = mapped_column(String(100), default="")
    agent_id: Mapped[str] = mapped_column(String(100), default="")
    agent_type: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(Text, default="")
    # brak pomiaru to None, nigdy 0 (BRO-87)
    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    cost_source: Mapped[str] = mapped_column(String(20), default="unavailable")
    tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="events")


class User(Base):
    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320))
    haslo_skrot: Mapped[str] = mapped_column(String(255))   # skrot argon2id, nigdy haslo
    email_zweryfikowany_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    haslo_zmienione_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @validates("email")
    def _normalizuj_email(self, _klucz: str, wartosc: str) -> str:
        """Adres zapisujemy bez bialych znakow i malymi literami.

        Walidator dziala tylko przez ORM (zapis SQL-em wprost go omija).
        """
        return wartosc.strip().lower()


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    rola: Mapped[RolaWZespole] = mapped_column(Enum(RolaWZespole))   # rola zyje na czlonkostwie (BRO-118)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Sesja(Base):
    """Sesja: w bazie tylko skroty (BRO-115); jawny identyfikator zyje w ciasteczku httpOnly."""
    __tablename__ = "sesja"
    __table_args__ = (UniqueConstraint("skrot", name="uq_sesja_skrot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skrot: Mapped[str] = mapped_column(String(64))         # sha256 identyfikatora z ciasteczka
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id"))   # aktywny zespol sesji
    csrf_skrot: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    wygasa_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ostatnio_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    uniewazniona_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

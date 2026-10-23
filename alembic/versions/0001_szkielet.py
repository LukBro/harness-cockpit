"""Szkielet: tenant, runner, run, event (HAR-1).

Odwzorowanie 1:1 cockpit.db.models. Zmiana modelu = nowa migracja.

Revision ID: 0001
Revises:
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Wartosci to NAZWY czlonkow enumow z modelu (tak zapisuje SQLAlchemy).
_STATUSY = ("pending", "running", "waiting", "to_accept", "failed", "skipped", "aborted")
_RODZAJE = ("local", "remote", "platform")


def upgrade() -> None:
    # Typy tworzymy jawnie raz; kolumny maja create_type=False (zero podwojnego CREATE TYPE).
    postgresql.ENUM(*_RODZAJE, name="runnerkind").create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(*_STATUSY, name="runstatus").create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenant",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "runner",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_runner_tenant_id", "runner", ["tenant_id"])

    op.create_table(
        "run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner.id"), nullable=True),
        sa.Column(
            "runner_kind",
            postgresql.ENUM(*_RODZAJE, name="runnerkind", create_type=False),
            nullable=False,
        ),
        sa.Column("workflow", sa.String(200), nullable=False),
        sa.Column("base_commit", sa.String(40), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(*_STATUSY, name="runstatus", create_type=False),
            nullable=False,
        ),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_tenant_id", "run", ["tenant_id"])

    op.create_table(
        "event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),  # bez FK, jak w modelu
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("step_id", sa.String(100), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=True),    # brak pomiaru to NULL, nie 0
        sa.Column("cost_source", sa.String(20), nullable=False),
        sa.Column("tokens", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_tenant_id", "event", ["tenant_id"])
    op.create_index("ix_event_run_id", "event", ["run_id"])
    op.create_index("ix_event_run_seq", "event", ["run_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_event_run_seq", table_name="event")
    op.drop_index("ix_event_run_id", table_name="event")
    op.drop_index("ix_event_tenant_id", table_name="event")
    op.drop_table("event")
    op.drop_index("ix_run_tenant_id", table_name="run")
    op.drop_table("run")
    op.drop_index("ix_runner_tenant_id", table_name="runner")
    op.drop_table("runner")
    op.drop_table("tenant")
    postgresql.ENUM(name="runstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="runnerkind").drop(op.get_bind(), checkfirst=True)

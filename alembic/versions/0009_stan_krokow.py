"""Stan krokow ze zdarzen (HAR-78): tabela step_run i kolumna event.attempt.

Odwzorowanie 1:1 cockpit.db.models.StepRun i Event.attempt.

Downgrade usuwa kolumne, tabele i typ enum, wiec GUBI zapisane stany krokow;
status runow zostaje, a kroki odtworzy dopiero kolejny run.

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Wartosci to NAZWY czlonkow enumu z modelu (tak zapisuje SQLAlchemy).
_STANY = ("running", "finished", "failed", "skipped", "waiting")


def upgrade() -> None:
    postgresql.ENUM(*_STANY, name="stepstatus").create(op.get_bind(), checkfirst=True)

    op.add_column("event", sa.Column("attempt", sa.Integer(), nullable=True))
    op.create_table(
        "step_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("step_id", sa.String(100), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_STANY, name="stepstatus", create_type=False),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "step_id", "agent_id", "attempt", name="uq_step_run_klucz"),
    )
    op.create_index("ix_step_run_tenant_id", "step_run", ["tenant_id"])
    op.create_index("ix_step_run_run_id", "step_run", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_step_run_run_id", table_name="step_run")
    op.drop_index("ix_step_run_tenant_id", table_name="step_run")
    op.drop_table("step_run")
    op.drop_column("event", "attempt")
    postgresql.ENUM(name="stepstatus").drop(op.get_bind(), checkfirst=True)

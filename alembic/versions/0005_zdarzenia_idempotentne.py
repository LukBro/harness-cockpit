"""Zdarzenia idempotentne po event_id od runnera i agent_type (HAR-39).

Dodaje event.event_id (nadawany przez runnera; istniejace zdarzenia dostaja
swoje id), event.agent_type (domyslnie pusty napis) oraz ograniczenie
uq_event_run_event na (run_id, event_id).

Downgrade usuwa kolumny i ograniczenie, wiec GUBI event_id nadane przez runnera
i agent_type; same zdarzenia zostaja.

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event", sa.Column("event_id", sa.String(36), nullable=True))
    op.execute(sa.text("UPDATE event SET event_id = id"))
    op.alter_column("event", "event_id", existing_type=sa.String(36), nullable=False)
    op.add_column(
        "event",
        sa.Column("agent_type", sa.String(100), nullable=False, server_default=""),
    )
    op.create_unique_constraint("uq_event_run_event", "event", ["run_id", "event_id"])


def downgrade() -> None:
    op.drop_constraint("uq_event_run_event", "event", type_="unique")
    op.drop_column("event", "agent_type")
    op.drop_column("event", "event_id")

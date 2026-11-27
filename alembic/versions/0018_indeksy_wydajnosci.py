"""Indeksy pod gorace zapytania: token runnera, zdarzenia kroku, runy w toku (HAR-202).

Odwzorowanie `Index` w cockpit.db.models. Indeks na `token_hash` nie jest unikalny.
Indeks na `run.status` obejmuje tylko statusy przegladane przez sweepera runnerow offline.

Revision ID: 0018
Revises: 0017
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_runner_token_hash", "runner", ["token_hash"])
    op.create_index("ix_event_run_step_kind", "event", ["run_id", "step_id", "kind"])
    op.create_index(
        "ix_run_status_w_toku", "run", ["status"],
        postgresql_where=sa.text("status IN ('running', 'waiting')"),
    )


def downgrade() -> None:
    op.drop_index("ix_run_status_w_toku", table_name="run")
    op.drop_index("ix_event_run_step_kind", table_name="event")
    op.drop_index("ix_runner_token_hash", table_name="runner")

"""Identyfikator ticketu w runie i znacznik obciecia tekstu zdarzenia (HAR-51).

Odwzorowanie 1:1 cockpit.db.models. run.ticket_id jest opcjonalny, z indeksem
(tenant_id, ticket_id). event.text_truncated domyslnie false (istniejace wiersze
dostaja false), event.text_full_len to NULL, gdy brak pomiaru (nigdy 0).

Downgrade usuwa kolumny, wiec GUBI dane ticket_id i znacznikow obciecia.

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("run", sa.Column("ticket_id", sa.String(200), nullable=True))
    op.create_index("ix_run_tenant_ticket", "run", ["tenant_id", "ticket_id"])
    op.add_column(
        "event",
        sa.Column("text_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("event", sa.Column("text_full_len", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("event", "text_full_len")
    op.drop_column("event", "text_truncated")
    op.drop_index("ix_run_tenant_ticket", table_name="run")
    op.drop_column("run", "ticket_id")

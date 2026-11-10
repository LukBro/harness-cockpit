"""Dane zdarzenia (HAR-170): dowod, werdykt, kotwice i artefakty konca kroku w event.dane.

Kolumna dopuszcza NULL: brak danych to brak, nigdy `{}` (measured_or_null). Wiersze sprzed
migracji zostaja z NULL.

Downgrade usuwa kolumne, wiec gubi zapisane dowody, werdykty, kotwice i artefakty.

Revision ID: 0014
Revises: 0013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event", sa.Column("dane", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("event", "dane")

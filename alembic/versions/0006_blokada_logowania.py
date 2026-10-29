"""Blokada logowania: liczniki nieudanych prob w bazie (HAR-46).

Odwzorowanie 1:1 cockpit.db.models. Klucz to sha256 pary (adres, IP) albo samego
adresu; jawnego adresu ani IP w tabeli nie ma.

Downgrade usuwa tabele, wiec GUBI liczniki i aktywne blokady.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blokada_logowania",
        sa.Column("klucz", sa.String(64), primary_key=True),
        sa.Column("nieudane", sa.Integer(), nullable=False),
        sa.Column("pierwsza_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("zablokowane_do", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("blokada_logowania")

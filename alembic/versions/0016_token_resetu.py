"""Tokeny resetu hasla (HAR-105).

Odwzorowanie 1:1 cockpit.db.models.TokenResetu. W tabeli tylko sha256 tokenu.

Downgrade usuwa tabele, wiec GUBI nieuzyte tokeny: linki z juz wyslanych listow przestana dzialac.

Revision ID: 0016
Revises: 0015
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "token_resetu",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("skrot", sa.String(64), nullable=False),
        sa.Column("utworzony_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wygasa_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uzyty_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("skrot", name="uq_token_resetu_skrot"),
    )
    op.create_index("ix_token_resetu_user_id", "token_resetu", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_token_resetu_user_id", table_name="token_resetu")
    op.drop_table("token_resetu")

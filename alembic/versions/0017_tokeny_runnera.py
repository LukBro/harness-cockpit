"""Metadane tokenu runnera: prefiks, utworzenie, ostatnie uzycie, waznosc, odwolanie (HAR-119).

Odwzorowanie kolumn `Runner` w cockpit.db.models. Wszystkie kolumny dopuszczaja NULL:
runnery sprzed zmiany nie maja prefiksu ani chwili utworzenia i dzialaja dalej.

Downgrade usuwa kolumny, wiec GUBI informacje o odwolaniach: odwolany token znow zadziala.

Revision ID: 0017
Revises: 0016
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runner", sa.Column("token_prefiks", sa.String(16), nullable=True))
    op.add_column("runner", sa.Column("token_utworzony_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runner", sa.Column("token_uzyty_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runner", sa.Column("token_wazny_do", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runner", sa.Column("odwolany_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runner", "odwolany_at")
    op.drop_column("runner", "token_wazny_do")
    op.drop_column("runner", "token_uzyty_at")
    op.drop_column("runner", "token_utworzony_at")
    op.drop_column("runner", "token_prefiks")

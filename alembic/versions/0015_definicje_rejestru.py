"""Definicje rejestru (HAR-180): consumes, produces, zakres i problem skilla w katalogu.

Wszystkie cztery kolumny dopuszczaja NULL: brak danych to brak, nigdy `[]` ani pusty napis
(measured_or_null). Wiersze sprzed migracji i wiersze workflow zostaja z NULL do nastepnego
odswiezenia katalogu przez runnera.

Downgrade usuwa kolumny, wiec gubi zapisane listy, zakresy i komunikaty problemow skilli
(katalog odbuduje sie przy odswiezeniu).

Revision ID: 0015
Revises: 0014
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("definicja", sa.Column("consumes", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("definicja", sa.Column("produces", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("definicja", sa.Column("zakres", sa.String(length=20), nullable=True))
    op.add_column("definicja", sa.Column("problem", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("definicja", "problem")
    op.drop_column("definicja", "zakres")
    op.drop_column("definicja", "produces")
    op.drop_column("definicja", "consumes")

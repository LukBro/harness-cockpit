"""Katalog skilli i workflow w bazie (HAR-67).

Odwzorowanie 1:1 cockpit.db.models.Definicja. Tabela trzyma tylko metadane (id, nazwa,
rola, sciezka) odswiezane przez runnera; tresc briefu nie trafia do bazy (BRO-86).

Downgrade usuwa tabele, wiec GUBI zapisany katalog; odtworzy go kolejne odswiezenie.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "definicja",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner.id"), nullable=False),
        sa.Column("projekt", sa.String(1024), nullable=False),
        sa.Column("rodzaj", sa.String(20), nullable=False),
        sa.Column("klucz", sa.String(200), nullable=False),
        sa.Column("nazwa", sa.String(200), nullable=False),
        sa.Column("rola", sa.String(20), nullable=True),
        sa.Column("sciezka", sa.String(1024), nullable=False),
        sa.Column("odswiezono_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "runner_id", "projekt", "rodzaj", "klucz", name="uq_definicja_klucz"
        ),
    )
    op.create_index("ix_definicja_tenant_id", "definicja", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_definicja_tenant_id", table_name="definicja")
    op.drop_table("definicja")

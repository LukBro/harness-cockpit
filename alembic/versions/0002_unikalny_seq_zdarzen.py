"""Unikalny numer zdarzenia w obrebie runu (HAR-25).

Zastepuje zwykly indeks ix_event_run_seq ograniczeniem uq_event_run_seq.
Migracja niczego nie usuwa: przy duplikatach (run_id, seq) konczy sie bledem.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    duplikaty = op.get_bind().execute(sa.text(
        "SELECT run_id, seq, count(*) AS ile FROM event "
        "GROUP BY run_id, seq HAVING count(*) > 1 ORDER BY run_id, seq"
    )).fetchall()
    if duplikaty:
        przyklady = ", ".join(f"run {d[0]} seq {d[1]} (x{d[2]})" for d in duplikaty[:5])
        raise RuntimeError(
            f"znaleziono duplikaty (run_id, seq) w tabeli event: {len(duplikaty)} par, "
            f"np. {przyklady}. Migracja niczego nie usuwa - uporzadkuj dane recznie "
            "i uruchom migracje ponownie."
        )
    op.drop_index("ix_event_run_seq", table_name="event")
    op.create_unique_constraint("uq_event_run_seq", "event", ["run_id", "seq"])


def downgrade() -> None:
    op.drop_constraint("uq_event_run_seq", "event", type_="unique")
    op.create_index("ix_event_run_seq", "event", ["run_id", "seq"])

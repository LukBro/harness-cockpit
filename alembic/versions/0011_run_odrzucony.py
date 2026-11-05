"""Status runu `rejected` (HAR-172): run odrzucony przez czlowieka na bramce.

Odwzorowanie 1:1 cockpit.db.models.RunStatus.rejected.

Upgrade dodaje wartosc do natywnego typu PostgreSQL `runstatus`. `ADD VALUE` nie dziala
w transakcji z uzyciem nowej wartosci, wiec idzie w bloku autocommit; `IF NOT EXISTS`
pozwala przejsc migracje, gdy wartosc juz jest w typie.

Downgrade: PostgreSQL nie umie usunac wartosci z enuma, wiec downgrade robi przebudowe
typu. Najpierw runy ze statusem `rejected` dostaja status `failed` (powod odrzucenia
zostaje w `reason`, wiec odrzucenie przestaje byc odroznialne od porazki), potem stary typ
dostaje nowa nazwe, powstaje typ bez `rejected`, kolumna `run.status` jest do niego
przepisana, a stary typ znika. Wartosci typu sa wpisane jawnie, nie brane z modelu.

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Wartosci sprzed tej migracji (NAZWY czlonkow enumu, jak w 0001), bez `rejected`.
_STATUSY_PRZED = ("pending", "running", "waiting", "to_accept", "failed", "skipped", "aborted")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE runstatus ADD VALUE IF NOT EXISTS 'rejected'")


def downgrade() -> None:
    op.execute("UPDATE run SET status = 'failed' WHERE status = 'rejected'")
    op.execute("ALTER TYPE runstatus RENAME TO runstatus_old")
    wartosci = ", ".join(f"'{w}'" for w in _STATUSY_PRZED)
    op.execute(f"CREATE TYPE runstatus AS ENUM ({wartosci})")
    op.execute(
        "ALTER TABLE run ALTER COLUMN status TYPE runstatus "
        "USING status::text::runstatus"
    )
    op.execute("DROP TYPE runstatus_old")

"""Konta: user, membership (rola w zespole), sesja (HAR-36).

Odwzorowanie 1:1 cockpit.db.models. Zmiana modelu = nowa migracja.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Wartosci to NAZWY czlonkow enumu z modelu (tak zapisuje SQLAlchemy).
_ROLE = ("owner", "admin", "member", "viewer")


def upgrade() -> None:
    postgresql.ENUM(*_ROLE, name="rolawzespole").create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("haslo_skrot", sa.String(255), nullable=False),
        sa.Column("email_zweryfikowany_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("haslo_zmienione_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )

    op.create_table(
        "membership",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), nullable=False),
        sa.Column(
            "rola",
            postgresql.ENUM(*_ROLE, name="rolawzespole", create_type=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
    )
    op.create_index("ix_membership_tenant_id", "membership", ["tenant_id"])
    op.create_index("ix_membership_user_id", "membership", ["user_id"])

    op.create_table(
        "sesja",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skrot", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("csrf_skrot", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wygasa_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ostatnio_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uniewazniona_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("skrot", name="uq_sesja_skrot"),
    )
    op.create_index("ix_sesja_user_id", "sesja", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sesja_user_id", table_name="sesja")
    op.drop_table("sesja")
    op.drop_index("ix_membership_user_id", table_name="membership")
    op.drop_index("ix_membership_tenant_id", table_name="membership")
    op.drop_table("membership")
    op.drop_table("user")
    postgresql.ENUM(name="rolawzespole").drop(op.get_bind(), checkfirst=True)

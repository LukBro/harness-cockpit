"""Projekt zespolu (HAR-79): tabela project i kolumna run.project_id.

Odwzorowanie 1:1 cockpit.db.models.Project i Run.project_id.

Downgrade usuwa kolumne i tabele, wiec GUBI zapisane projekty zespolow oraz powiazanie
runow z projektem; same runy zostaja.

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("runner_id", sa.String(36), sa.ForeignKey("runner.id"), nullable=False),
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("stack", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "runner_id", "path", name="uq_project_runner_path"),
    )
    op.create_index("ix_project_tenant_id", "project", ["tenant_id"])
    op.add_column(
        "run", sa.Column("project_id", sa.String(36), sa.ForeignKey("project.id"), nullable=True))
    op.create_index("ix_run_project_id", "run", ["project_id"])


def downgrade() -> None:
    # Bez jawnego drop_index: indeksy znikaja razem z kolumna i tabela, a jawny DROP INDEX
    # wywraca cofniecie schematu, w ktorym indeksu nie ma.
    # Kolejnosc: najpierw kolumna z FK do project, potem tabela.
    op.drop_column("run", "project_id")
    op.drop_table("project")

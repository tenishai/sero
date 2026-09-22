"""the apply job an approved namespace request started

Revision ID: c7d2b58e0146
Revises: 9a1f6c30b7d4
Create Date: 2026-09-22 10:41:00.000000

Approving a request starts an apply run, and the two are read together: the
decision on the row, and the run that carries it out. Nullable, because every
request that predates this has no run, and a rejected one never gets one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c7d2b58e0146'
down_revision: str | None = '9a1f6c30b7d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('namespace_requests', sa.Column('job_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('namespace_requests', 'job_id')

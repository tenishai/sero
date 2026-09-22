"""jobs table for background apply runs

Revision ID: 728c24f39bc0
Revises: f05387625ecc
Create Date: 2026-09-22 01:03:55.325125

Autogenerate also proposed alter_column on the existing BIGINT primary keys.
That is a no-op restatement of the SQLite variant on the same type, and SQLite
cannot ALTER COLUMN, so it is dropped: this revision only adds the new table.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '728c24f39bc0'
down_revision: str | None = 'f05387625ecc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('jobs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), server_default='queued', nullable=False),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('report_json', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_kind'), 'jobs', ['kind'], unique=False)
    op.create_index(op.f('ix_jobs_requested_by'), 'jobs', ['requested_by'], unique=False)
    op.create_index(op.f('ix_jobs_state'), 'jobs', ['state'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_jobs_state'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_requested_by'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_kind'), table_name='jobs')
    op.drop_table('jobs')

"""Idempotency records for operations replayed by the offline outbox of the web client."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261109_idempotency'
down_revision = 'f20261109_sub_payments'
branch_labels = None
depends_on = None


def upgrade():
    if 'idempotency_records' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'idempotency_records',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(80), nullable=False),
        sa.Column('auth_hash', sa.String(64), nullable=False),
        sa.Column('method', sa.String(10), nullable=False),
        sa.Column('path', sa.String(255), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('content_type', sa.String(100), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_idempotency_records_key', 'idempotency_records', ['key'], unique=True)


def downgrade():
    if 'idempotency_records' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index('ix_idempotency_records_key', table_name='idempotency_records')
        op.drop_table('idempotency_records')

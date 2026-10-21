"""Persist immutable offline receipts independently of commercial documents."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261021'
down_revision = 'f20261019'
branch_labels = depends_on = None


def upgrade():
    op.create_table('sync_operations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('client_operation_uuid', sa.String(64), nullable=False),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('branch_id', sa.Integer(), sa.ForeignKey('branches.id'), nullable=False),
        sa.Column('operation_type', sa.String(20), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('receipt', sa.JSON(), nullable=False),
        sa.UniqueConstraint('tenant_id', 'client_operation_uuid', name='uq_sync_tenant_uuid'))


def downgrade():
    op.drop_table('sync_operations')

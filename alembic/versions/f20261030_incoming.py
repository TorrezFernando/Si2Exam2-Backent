"""Scheduled supplier receipts."""
from alembic import op
import sqlalchemy as sa
revision = 'f20261030_incoming'
down_revision = 'f20261030_attributes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('incoming_stock',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('branch_id', sa.Integer(), sa.ForeignKey('branches.id'), nullable=False),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('variant_id', sa.Integer(), sa.ForeignKey('product_variants.id'), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('received_quantity', sa.Integer(), nullable=False),
        sa.Column('expected_date', sa.Date(), nullable=False),
        sa.Column('reference', sa.String(200), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('request_key', sa.String(64), nullable=False),
        sa.Column('request_fingerprint', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('tenant_id', 'request_key', name='uq_incoming_request'),
        sa.CheckConstraint('quantity > 0 AND received_quantity >= 0 AND received_quantity <= quantity', name='ck_incoming_quantity'))
    op.create_index('ix_incoming_stock_tenant_id', 'incoming_stock', ['tenant_id'])
    op.create_table('incoming_receipts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('incoming_id', sa.Integer(), sa.ForeignKey('incoming_stock.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('request_key', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('tenant_id', 'request_key', name='uq_incoming_receipt_request'))


def downgrade():
    op.drop_table('incoming_receipts')
    op.drop_index('ix_incoming_stock_tenant_id', table_name='incoming_stock')
    op.drop_table('incoming_stock')

"""Recorded full cash returns after garment exchanges."""
from alembic import op
import sqlalchemy as sa
revision = 'f20261031_returns'
down_revision = 'f20261030_promotions'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('full_returns',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('tenant_id',sa.Integer(),sa.ForeignKey('tenants.id'),nullable=False),
        sa.Column('order_id',sa.Integer(),sa.ForeignKey('orders.id'),nullable=False,unique=True),
        sa.Column('branch_id',sa.Integer(),sa.ForeignKey('branches.id'),nullable=False),
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('request_key',sa.String(64),nullable=False),
        sa.Column('refund_amount',sa.Float(),nullable=False),
        sa.Column('items',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('tenant_id','request_key',name='uq_full_return_request'))
    op.create_index('ix_full_returns_tenant_id','full_returns',['tenant_id'])


def downgrade():
    op.drop_index('ix_full_returns_tenant_id',table_name='full_returns')
    op.drop_table('full_returns')

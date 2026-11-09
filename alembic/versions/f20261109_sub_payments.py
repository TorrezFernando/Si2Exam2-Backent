"""Subscription payments: company plan changes and renewals are charged before being applied."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261109_sub_payments'
down_revision = 'f20261109_plan_features'
branch_labels = None
depends_on = None


def upgrade():
    if 'subscription_payments' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'subscription_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('subscription_plans.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='USD'),
        sa.Column('provider', sa.String(20), nullable=False, server_default='PayPal'),
        sa.Column('reference', sa.String(64), nullable=False, unique=True),
        sa.Column('provider_order_id', sa.String(64), nullable=True, unique=True),
        sa.Column('payment_url', sa.String(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('error', sa.String(255), nullable=True),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_subscription_payments_id', 'subscription_payments', ['id'])
    op.create_index('ix_subscription_payments_tenant_id', 'subscription_payments', ['tenant_id'])


def downgrade():
    if 'subscription_payments' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index('ix_subscription_payments_tenant_id', table_name='subscription_payments')
        op.drop_index('ix_subscription_payments_id', table_name='subscription_payments')
        op.drop_table('subscription_payments')

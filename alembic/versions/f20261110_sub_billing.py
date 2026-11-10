"""Billing details (name, tax id, email) stored with each subscription payment."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261110_sub_billing'
down_revision = 'f20261109_audit_encrypted'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'subscription_payments' in inspector.get_table_names() and \
            'billing' not in {c['name'] for c in inspector.get_columns('subscription_payments')}:
        with op.batch_alter_table('subscription_payments') as batch:
            batch.add_column(sa.Column('billing', sa.JSON(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if 'subscription_payments' in inspector.get_table_names() and \
            'billing' in {c['name'] for c in inspector.get_columns('subscription_payments')}:
        with op.batch_alter_table('subscription_payments') as batch:
            batch.drop_column('billing')

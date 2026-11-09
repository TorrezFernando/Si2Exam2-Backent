"""Editable plan limits (users, products) and AI features/models per subscription plan.

Existing plans keep their current behaviour: AI shopper and try-on stay enabled,
new limits are NULL (unlimited) and models fall back to the server defaults.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f20261109_plan_features'
down_revision = 'f20261108_returns_alt'
branch_labels = None
depends_on = None

COLUMNS = (
    ('description', lambda: sa.Column('description', sa.String(255), nullable=True)),
    ('max_users', lambda: sa.Column('max_users', sa.Integer(), nullable=True)),
    ('max_products', lambda: sa.Column('max_products', sa.Integer(), nullable=True)),
    ('has_ai_shopper', lambda: sa.Column('has_ai_shopper', sa.Boolean(), nullable=False, server_default=sa.true())),
    ('has_ai_tryon', lambda: sa.Column('has_ai_tryon', sa.Boolean(), nullable=False, server_default=sa.true())),
    ('ai_text_model', lambda: sa.Column('ai_text_model', sa.String(60), nullable=True)),
    ('ai_image_model', lambda: sa.Column('ai_image_model', sa.String(60), nullable=True)),
)


def existing():
    inspector = sa.inspect(op.get_bind())
    if 'subscription_plans' not in inspector.get_table_names():
        return None
    return {c['name'] for c in inspector.get_columns('subscription_plans')}


def upgrade():
    cols = existing()
    if cols is None:
        return
    with op.batch_alter_table('subscription_plans') as batch:
        for name, column in COLUMNS:
            if name not in cols:
                batch.add_column(column())


def downgrade():
    cols = existing()
    if cols is None:
        return
    with op.batch_alter_table('subscription_plans') as batch:
        for name, _ in reversed(COLUMNS):
            if name in cols:
                batch.drop_column(name)

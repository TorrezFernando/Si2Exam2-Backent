"""Add monthly subscription auto-cancellation fields and backfill period end dates."""
from datetime import datetime, timedelta, timezone
from alembic import op
import sqlalchemy as sa

revision = 'f20261108_sub_autocancel'
down_revision = 'f20261031_returns'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' not in inspector.get_table_names():
        return

    existing_cols = {c['name'] for c in inspector.get_columns('subscriptions')}
    with op.batch_alter_table('subscriptions') as batch:
        if 'cancel_at_period_end' not in existing_cols:
            batch.add_column(sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False, server_default=sa.true()))
        if 'cancelled_at' not in existing_cols:
            batch.add_column(sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))

    now = datetime.now(timezone.utc)
    default_end = now + timedelta(days=30)
    bind.execute(
        sa.text(
            "UPDATE subscriptions SET end_date = :default_end "
            "WHERE end_date IS NULL AND status = 'active'"
        ),
        {'default_end': default_end},
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' not in inspector.get_table_names():
        return

    existing_cols = {c['name'] for c in inspector.get_columns('subscriptions')}
    with op.batch_alter_table('subscriptions') as batch:
        if 'cancelled_at' in existing_cols:
            batch.drop_column('cancelled_at')
        if 'cancel_at_period_end' in existing_cols:
            batch.drop_column('cancel_at_period_end')

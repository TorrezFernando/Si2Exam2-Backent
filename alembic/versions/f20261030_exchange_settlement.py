"""Idempotent exchanges and explicit cash settlement; maximum 48-hour window."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261030'
down_revision = 'f20261021'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('garment_exchanges') as batch:
        batch.add_column(sa.Column('request_key', sa.String(64), nullable=True))
        batch.add_column(sa.Column('request_fingerprint', sa.String(64), nullable=True))
        batch.add_column(sa.Column('settlement_method', sa.String(20), nullable=True))
        batch.create_unique_constraint('uq_exchange_tenant_request', ['tenant_id', 'request_key'])
    op.execute(sa.text('UPDATE tenants SET exchange_window_days = 2 WHERE exchange_window_days > 2'))


def downgrade():
    with op.batch_alter_table('garment_exchanges') as batch:
        batch.drop_constraint('uq_exchange_tenant_request', type_='unique')
        batch.drop_column('settlement_method')
        batch.drop_column('request_fingerprint')
        batch.drop_column('request_key')
    # Do not invent the former per-company policy values.

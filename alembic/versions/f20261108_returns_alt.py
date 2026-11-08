"""Add defective exchange, additional payment method, and month-end return fields."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261108_returns_alt'
down_revision = 'f20261108_sub_autocancel'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'garment_exchanges' in tables:
        ex_cols = {c['name'] for c in inspector.get_columns('garment_exchanges')}
        with op.batch_alter_table('garment_exchanges') as batch:
            if 'exchange_type' not in ex_cols:
                batch.add_column(sa.Column('exchange_type', sa.String(30), nullable=False, server_default='variant_or_product'))
            if 'additional_payment_method' not in ex_cols:
                batch.add_column(sa.Column('additional_payment_method', sa.String(20), nullable=True))
            if 'disbursement_status' not in ex_cols:
                batch.add_column(sa.Column('disbursement_status', sa.String(30), nullable=True))
            if 'scheduled_for' not in ex_cols:
                batch.add_column(sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True))
            if 'settled_at' not in ex_cols:
                batch.add_column(sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True))

    if 'full_returns' in tables:
        fr_cols = {c['name'] for c in inspector.get_columns('full_returns')}
        with op.batch_alter_table('full_returns') as batch:
            if 'refund_timing' not in fr_cols:
                batch.add_column(sa.Column('refund_timing', sa.String(20), nullable=False, server_default='IMMEDIATE'))
            if 'disbursement_status' not in fr_cols:
                batch.add_column(sa.Column('disbursement_status', sa.String(30), nullable=False, server_default='COMPLETED'))
            if 'scheduled_for' not in fr_cols:
                batch.add_column(sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True))
            if 'settled_at' not in fr_cols:
                batch.add_column(sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True))
            if 'return_reason' not in fr_cols:
                batch.add_column(sa.Column('return_reason', sa.String(255), nullable=True))
            if 'is_defective' not in fr_cols:
                batch.add_column(sa.Column('is_defective', sa.Boolean(), nullable=False, server_default=sa.false()))
            if 'desired_variant_id' not in fr_cols:
                batch.add_column(sa.Column('desired_variant_id', sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'full_returns' in tables:
        fr_cols = {c['name'] for c in inspector.get_columns('full_returns')}
        with op.batch_alter_table('full_returns') as batch:
            for col in ('desired_variant_id', 'is_defective', 'return_reason', 'settled_at', 'scheduled_for', 'disbursement_status', 'refund_timing'):
                if col in fr_cols:
                    batch.drop_column(col)

    if 'garment_exchanges' in tables:
        ex_cols = {c['name'] for c in inspector.get_columns('garment_exchanges')}
        with op.batch_alter_table('garment_exchanges') as batch:
            for col in ('settled_at', 'scheduled_for', 'disbursement_status', 'additional_payment_method', 'exchange_type'):
                if col in ex_cols:
                    batch.drop_column(col)


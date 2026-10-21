"""Multitenant SaaS and Offline Sync schema evolution.

Revision ID: f20261019
Revises: f20261015
"""
from alembic import op
import sqlalchemy as sa

revision = 'f20261019'
down_revision = 'f20261015'
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    def create(name, *columns):
        if name not in tables:
            op.create_table(name, *columns)

    def pk():
        return sa.Column('id', sa.Integer(), primary_key=True)

    # 1. Crear tablas de Multitenancy
    create(
        'tenants',
        pk(),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('subdomain', sa.String(), unique=True, nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('exchange_window_days', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )

    create(
        'subscription_plans',
        pk(),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('code', sa.String(), unique=True, nullable=False),
        sa.Column('max_branches', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('has_ai_reports', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('has_offline_sync', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('price', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    create(
        'subscriptions',
        pk(),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', name='fk_subscriptions_tenant_id'), nullable=False),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('subscription_plans.id', name='fk_subscriptions_plan_id'), nullable=False),
        sa.Column('start_date', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('end_date', sa.DateTime(timezone=True)),
        sa.Column('status', sa.String(), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    create(
        'garment_exchanges',
        pk(),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', name='fk_garment_exchanges_tenant_id'), nullable=True),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id', name='fk_garment_exchanges_order_id'), nullable=False),
        sa.Column('branch_id', sa.Integer(), sa.ForeignKey('branches.id', name='fk_garment_exchanges_branch_id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', name='fk_garment_exchanges_user_id'), nullable=False),
        sa.Column('original_variant_id', sa.Integer(), sa.ForeignKey('product_variants.id', name='fk_garment_exchanges_orig_var'), nullable=False),
        sa.Column('new_variant_id', sa.Integer(), sa.ForeignKey('product_variants.id', name='fk_garment_exchanges_new_var'), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('difference_amount', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('status', sa.String(), nullable=False, server_default='APPROVED'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # 2. Agregar tenant_id y columnas offline
    tenant_tables = [
        'users', 'branches', 'categories', 'products', 'product_variants',
        'inventories', 'orders', 'order_items', 'reservations', 'reservation_items',
        'audit_logs', 'suppliers', 'seasons', 'collections', 'inventory_movements'
    ]

    for table in tenant_tables:
        existing = {c['name'] for c in sa.inspect(bind).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            if 'tenant_id' not in existing:
                batch.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
                batch.create_foreign_key(f'fk_{table}_tenant_id', 'tenants', ['tenant_id'], ['id'])

    # Columnas específicas para pedidos y reservas offline
    order_columns = {
        'orders': [
            sa.Column('client_operation_uuid', sa.String(), nullable=True),
            sa.Column('is_offline_operation', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('sync_status', sa.String(), nullable=False, server_default='CONFIRMADA'),
            sa.Column('rejection_reason', sa.String(), nullable=True),
        ],
        'reservations': [
            sa.Column('client_operation_uuid', sa.String(), nullable=True),
            sa.Column('is_offline_operation', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('sync_status', sa.String(), nullable=False, server_default='CONFIRMADA'),
            sa.Column('rejection_reason', sa.String(), nullable=True),
        ],
    }

    for table, columns in order_columns.items():
        existing = {c['name'] for c in sa.inspect(bind).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for col in columns:
                if col.name not in existing:
                    batch.add_column(col)

    # 3. Restricciones UNIQUE compuestas para idempotencia offline
    for table, name, columns in [
        ('orders', 'uq_order_tenant_client_uuid', ['tenant_id', 'client_operation_uuid']),
        ('reservations', 'uq_reservation_tenant_client_uuid', ['tenant_id', 'client_operation_uuid']),
    ]:
        existing_uqs = [u['name'] for u in sa.inspect(bind).get_unique_constraints(table)]
        if name not in existing_uqs:
            with op.batch_alter_table(table) as batch:
                batch.create_unique_constraint(name, columns)


def downgrade():
    raise RuntimeError('Esta migración evoluciona a arquitectura multitenant. Usar snapshot previo para revertir.')

from sqlalchemy import Column, Integer, String, ForeignKey, Date, DateTime, UniqueConstraint, CheckConstraint
from sqlalchemy.sql import func
from db.database import Base


class IncomingStock(Base):
    __tablename__ = 'incoming_stock'
    __table_args__ = (UniqueConstraint('tenant_id', 'request_key', name='uq_incoming_request'),
        CheckConstraint('quantity > 0 AND received_quantity >= 0 AND received_quantity <= quantity', name='ck_incoming_quantity'))
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)
    branch_id = Column(Integer, ForeignKey('branches.id'), nullable=False)
    supplier_id = Column(Integer, ForeignKey('suppliers.id'), nullable=False)
    variant_id = Column(Integer, ForeignKey('product_variants.id'), nullable=False)
    quantity = Column(Integer, nullable=False)
    received_quantity = Column(Integer, nullable=False, default=0)
    expected_date = Column(Date, nullable=False)
    reference = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    request_key = Column(String(64), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IncomingReceipt(Base):
    __tablename__ = 'incoming_receipts'
    __table_args__ = (UniqueConstraint('tenant_id', 'request_key', name='uq_incoming_receipt_request'),)
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    incoming_id = Column(Integer, ForeignKey('incoming_stock.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    quantity = Column(Integer, nullable=False)
    request_key = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime, JSON, UniqueConstraint
from sqlalchemy.sql import func, false
from db.database import Base


class FullReturn(Base):
    __tablename__ = 'full_returns'
    __table_args__ = (UniqueConstraint('tenant_id','request_key',name='uq_full_return_request'),)
    id = Column(Integer,primary_key=True)
    tenant_id = Column(Integer,ForeignKey('tenants.id'),nullable=False,index=True)
    order_id = Column(Integer,ForeignKey('orders.id'),nullable=False,unique=True)
    branch_id = Column(Integer,ForeignKey('branches.id'),nullable=False)
    user_id = Column(Integer,ForeignKey('users.id'),nullable=False)
    request_key = Column(String(64),nullable=False)
    refund_amount = Column(Float,nullable=False)
    items = Column(JSON,nullable=False)
    refund_timing = Column(String(20),default='IMMEDIATE',server_default='IMMEDIATE',nullable=False)  # 'IMMEDIATE' | 'MONTH_END'
    disbursement_status = Column(String(30),default='COMPLETED',server_default='COMPLETED',nullable=False)  # 'COMPLETED' | 'PENDING_MONTH_END'
    scheduled_for = Column(DateTime(timezone=True),nullable=True)
    settled_at = Column(DateTime(timezone=True),nullable=True)
    return_reason = Column(String(255),nullable=True)
    is_defective = Column(Boolean,default=False,server_default=false(),nullable=False)
    desired_variant_id = Column(Integer,ForeignKey('product_variants.id'),nullable=True)
    created_at = Column(DateTime(timezone=True),server_default=func.now(),nullable=False)

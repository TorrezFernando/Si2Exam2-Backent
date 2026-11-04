from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, JSON, UniqueConstraint
from sqlalchemy.sql import func
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
    created_at = Column(DateTime(timezone=True),server_default=func.now(),nullable=False)

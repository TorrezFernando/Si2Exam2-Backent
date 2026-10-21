from sqlalchemy import Column, Integer, String, ForeignKey, JSON, UniqueConstraint
from db.database import Base


class SyncOperation(Base):
    __tablename__ = 'sync_operations'
    __table_args__ = (UniqueConstraint('tenant_id', 'client_operation_uuid', name='uq_sync_tenant_uuid'),)
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    client_operation_uuid = Column(String(64), nullable=False)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    branch_id = Column(Integer, ForeignKey('branches.id'), nullable=False)
    operation_type = Column(String(20), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    receipt = Column(JSON, nullable=False)

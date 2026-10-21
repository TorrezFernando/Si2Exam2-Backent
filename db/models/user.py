import enum
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base


class User(Base):
    """
    CU-01 / CU-02 / CU-03: Registro, Login y Gestión de Usuarios/Roles.

    Campos adicionales respecto al diseño anterior:
    - branch_id : FK opcional a Branch. Se asigna cuando el rol es 'encargado' o 'cajero'
                  para vincular al empleado con su sucursal de trabajo.
    - phone     : Teléfono de contacto (útil para notificaciones de reservas).
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)

    # Tenant / Multitenancy
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)

    # Nuevo sistema RBAC
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True) # Todos deberían tener rol, pero nullable en migración

    # Sucursal asignada (solo relevante para cajero/encargado)
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relación con Tenant
    tenant = relationship("Tenant", back_populates="users")

    # Relación con Branch
    branch = relationship("Branch", foreign_keys=[branch_id])
    
    # Relación con Role
    role = relationship("Role", back_populates="users")
    
    # Permisos individuales que sobreescriben los del rol
    user_permissions = relationship("UserPermission")



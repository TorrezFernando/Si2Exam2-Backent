from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from db.database import Base


class Tenant(Base):
    """
    RNF01 / RNF02: Entidad principal de empresa contratante para arquitectura SaaS Multitenant.
    Cada tenant representa una organización aislada con sus propios usuarios, catálogo,
    sucursales, inventario y ventas.
    """
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    subdomain = Column(String, unique=True, index=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    exchange_window_days = Column(Integer, default=30, nullable=False)  # Plazo configurable para cambio de prendas
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relaciones
    subscriptions = relationship("Subscription", back_populates="tenant", cascade="all, delete-orphan")
    users = relationship("User", back_populates="tenant")
    branches = relationship("Branch", back_populates="tenant")
    products = relationship("Product", back_populates="tenant")

    @property
    def active_subscription(self):
        """Retorna la suscripción activa actual si existe."""
        for sub in self.subscriptions:
            if sub.status == "active":
                return sub
        return None


class SubscriptionPlan(Base):
    """
    Planes de suscripción con límites y habilitación de funcionalidades:
    - Plan Básico: Hasta 2 sucursales, sin reportes con IA.
    - Plan Profesional: Hasta 10 sucursales, todas las funciones incluidas IA.
    """
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    code = Column(String, unique=True, index=True, nullable=False)  # 'basic', 'professional'
    max_branches = Column(Integer, default=2, nullable=False)
    has_ai_reports = Column(Boolean, default=False, nullable=False)
    has_offline_sync = Column(Boolean, default=True, nullable=False)
    price = Column(Float, default=0.0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    subscriptions = relationship("Subscription", back_populates="plan")


class Subscription(Base):
    """
    Suscripción de un Tenant a un Plan específico con vigencia y estado.
    """
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"), nullable=False, index=True)
    start_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, default="active", nullable=False)  # 'active', 'expired', 'cancelled'
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tenant = relationship("Tenant", back_populates="subscriptions")
    plan = relationship("SubscriptionPlan", back_populates="subscriptions")

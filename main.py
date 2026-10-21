"""
FashionStore API — Punto de entrada principal
=============================================
Ejecutar en desarrollo:
    .\\venv\\Scripts\\uvicorn main:app --reload

Documentación interactiva (Swagger):
    http://localhost:8000/api/v1/openapi.json  →  http://localhost:8000/docs
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from core.config import settings
from db.database import engine, Base
# Importar todos los modelos para que Alembic y Base los reconozcan
from db.models import *  # noqa: F401,F403


# ─── Lifespan: Inicialización y cierre de la app ─────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Evento de inicio: verifica conexión a la BD.
    En producción las migraciones se aplican con Alembic (no create_all).
    """
    from core.config import validate_production
    validate_production()
    print(f"[START] Iniciando {settings.PROJECT_NAME} v{settings.VERSION}")
    print("[DB]    Configuracion de base de datos cargada.")
    yield
    print("[STOP] Cerrando la aplicacion.")


# ─── Instancia FastAPI ────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "API REST para FashionStore — plataforma de e-commerce de ropa "
        "con catálogo, reservas, ventas presenciales, IA y AR."
    ),
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ─── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Archivos Estáticos (Imágenes subidas) ────────────────────────────────────
from fastapi.staticfiles import StaticFiles
import os

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/", tags=["Health"])
def root():
    """Endpoint de verificación de salud de la API."""
    return {
        "status": "ok",
        "project": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
    }


# ─── Routers (CU = Caso de Uso) ───────────────────────────────────────────────
from api.routers import auth, users, products, branches, orders, roles, audit, reservations, ai_tryon, inventory, reports
# CU-01, CU-02: Registro y Login
app.include_router(
    auth.router,
    prefix=f"{settings.API_V1_STR}/auth",
    tags=["🔐 Autenticación"],
)

app.include_router(
    reports.router,
    prefix=f"{settings.API_V1_STR}/reports",
    tags=["📊 Reportes"],
)

app.include_router(
    inventory.router,
    prefix=f"{settings.API_V1_STR}/inventory",
    tags=["📦 Inventario"],
)

# CU-03: Gestión de Usuarios y Roles (Admin)
app.include_router(
    users.router,
    prefix=f"{settings.API_V1_STR}/users",
    tags=["👥 Usuarios y Roles"],
)

# CU-04: Catálogo y Productos
app.include_router(
    products.router,
    prefix=f"{settings.API_V1_STR}/products",
    tags=["🛍️ Catálogo y Productos"],
)

# CU-05: Sucursales
app.include_router(
    branches.router,
    prefix=f"{settings.API_V1_STR}/branches",
    tags=["🏢 Sucursales"],
)

# CU-10, CU-11: Ventas y Órdenes
app.include_router(
    orders.router,
    prefix=f"{settings.API_V1_STR}/orders",
    tags=["🛒 Ventas y Órdenes"],
)

# RBAC y Auditoría
app.include_router(
    roles.router,
    prefix=f"{settings.API_V1_STR}/roles",
    tags=["🛡️ Roles y Permisos"],
)

app.include_router(
    audit.router,
    prefix=f"{settings.API_V1_STR}/audit",
    tags=["📋 Bitácora (Audit)"],
)

# CU-06: Reservas
app.include_router(
    reservations.router,
    prefix=f"{settings.API_V1_STR}/reservations",
    tags=["📅 Reservas"],
)

# IA: Virtual Try-On con Gemini
app.include_router(
    ai_tryon.router,
    prefix=f"{settings.API_V1_STR}/ai/tryon",
    tags=["🤖 IA Try-On"],
)

from api.routers import ai_chat
app.include_router(
    ai_chat.router,
    prefix=f"{settings.API_V1_STR}/ai/chat",
    tags=["🤖 IA Chat"],
)

from api.routers import payments, operations, saas, sync
app.include_router(payments.router, prefix=f"{settings.API_V1_STR}/payments", tags=["Pagos"])
app.include_router(operations.router, prefix=f"{settings.API_V1_STR}/operations", tags=["Gestion comercial"])
app.include_router(saas.router, prefix=f"{settings.API_V1_STR}/saas", tags=["☁️ SaaS & Planes"])
app.include_router(sync.router, prefix=f"{settings.API_V1_STR}/sync", tags=["⚡ Sincronización Offline"])


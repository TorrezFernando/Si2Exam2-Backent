"""
Script de Seed: Poblamiento completo y funcional de Base de Datos para FashionStore
==================================================================================
Ejecutar con:
    python seed.py

Crea:
  1. Recreación limpia de tablas en la BD (SQLite/PostgreSQL).
  2. Directorios y archivos de imagen estáticos (/uploads).
  3. Usuarios con roles (Admin, Encargados, Cajeros, Clientes).
  4. Sucursales (Múltiples Tiendas) vinculadas a empleados.
  5. Categorías, Productos y Variantes (Tallas, Colores, SKUs, Precios) con imágenes.
  6. Inventario distribuido por variante en cada sucursal.
  7. Reservas vinculadas a tiendas específicas elegidas por el cliente.
  8. Ventas/Órdenes registradas con métodos de pago (Tarjeta, Efectivo, QR).

Este script es seguro para desarrollo y reinicializa la BD con datos realistas y funcionales.
"""
import sys
import os
from datetime import datetime, timedelta

# Aseguramos que el directorio raíz esté en el path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from db.database import SessionLocal, engine, Base
from db.models import *  # noqa: F401,F403 - importa todos los modelos
from db.models.user import User
from db.models.branch import Branch, Inventory
from db.models.product import Category, Product, ProductVariant
from db.models.reservation import Reservation, ReservationItem, ReservationStatus
from db.models.sale import Order, OrderItem, OrderTypeEnum, PaymentMethodEnum, PaymentStatusEnum
from core.config import settings
from core import security


def create_placeholder_images():
    """Crea los directorios de subida y genera archivos de imagen placeholder mínimos en /uploads."""
    dirs = [
        os.path.join("uploads", "branches"),
        os.path.join("uploads", "categories"),
        os.path.join("uploads", "products"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    # SVG Placeholder simple para simular imágenes válidas
    placeholder_svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">
      <rect width="100%" height="100%" fill="#1e293b"/>
      <circle cx="200" cy="180" r="60" fill="#6366f1"/>
      <text x="50%" y="280" font-family="sans-serif" font-size="20" fill="#ffffff" text-anchor="middle">FashionStore Item</text>
    </svg>'''

    sample_images = [
        ("branches", "centro.svg"),
        ("branches", "norte.svg"),
        ("branches", "equipetrol.svg"),
        ("categories", "hombre.svg"),
        ("categories", "mujer.svg"),
        ("categories", "calzado.svg"),
        ("categories", "accesorios.svg"),
        ("categories", "deportes.svg"),
        ("products", "chamarra_cuero.svg"),
        ("products", "jeans_slim.svg"),
        ("products", "vestido_gala.svg"),
        ("products", "blusa_seda.svg"),
        ("products", "tenis_urbanos.svg"),
        ("products", "zapatos_oxford.svg"),
        ("products", "bolso_mano.svg"),
        ("products", "gafas_sol.svg"),
    ]

    for subfolder, filename in sample_images:
        path = os.path.join("uploads", subfolder, filename)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(placeholder_svg)
    print("[OK] Directorios y placeholder de imágenes creados en /uploads.")


def seed():
    sep = "=" * 70
    print(sep)
    print("   FashionStore -- Inicialización y Poblamiento de Base de Datos")
    print(sep)

    # Recreación limpia de la base de datos
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("[OK] Tablas recreadas limpiamente en la base de datos.\n")

    create_placeholder_images()

    db = SessionLocal()

    try:
        # ─── 0. PLANES Y TENANTS (SAAS MULTITENANT) ────────────────────────────
        print("\n--- 0. Creando Planes de Suscripción y Tenants ---")
        plan_basic = SubscriptionPlan(
            name="Plan Básico",
            code="basic",
            max_branches=2,
            has_ai_reports=False,
            has_offline_sync=True,
            price=29.99,
            is_active=True,
        )
        plan_pro = SubscriptionPlan(
            name="Plan Profesional",
            code="professional",
            max_branches=10,
            has_ai_reports=True,
            has_offline_sync=True,
            price=99.99,
            is_active=True,
        )
        db.add_all([plan_basic, plan_pro])
        db.commit()
        db.refresh(plan_basic)
        db.refresh(plan_pro)
        print(f"   [+] Plan: {plan_basic.name} (Max sucursales: {plan_basic.max_branches}, IA: {plan_basic.has_ai_reports})")
        print(f"   [+] Plan: {plan_pro.name} (Max sucursales: {plan_pro.max_branches}, IA: {plan_pro.has_ai_reports})")

        tenant_a = Tenant(
            name="Empresa A (FashionStore Corp)",
            subdomain="fashionstore",
            is_active=True,
            exchange_window_days=30,
        )
        tenant_b = Tenant(
            name="Empresa B (Boutique Express)",
            subdomain="boutique-express",
            is_active=True,
            exchange_window_days=15,
        )
        db.add_all([tenant_a, tenant_b])
        db.commit()
        db.refresh(tenant_a)
        db.refresh(tenant_b)

        sub_a = Subscription(tenant_id=tenant_a.id, plan_id=plan_pro.id, status="active")
        sub_b = Subscription(tenant_id=tenant_b.id, plan_id=plan_basic.id, status="active")
        db.add_all([sub_a, sub_b])
        db.commit()
        print(f"   [+] Tenant A (ID {tenant_a.id}): {tenant_a.name} -> Plan: {plan_pro.name}")
        print(f"   [+] Tenant B (ID {tenant_b.id}): {tenant_b.name} -> Plan: {plan_basic.name}")

        # ─── 1. SUCURSALES EMPRESA A (Múltiples Tiendas) ────────────────────────
        print("\n--- 1. Creando Sucursales de Empresa A (Tiendas) ---")
        branches_data = [
            {
                "tenant_id": tenant_a.id,
                "name": "Comercial Brasil",
                "address": "Av. Brasil",
                "phone": "591-70000001",
                "image_url": "/uploads/branches/centro.svg",
                "is_active": True,
            },
            {
                "tenant_id": tenant_a.id,
                "name": "Comercial Cañoto",
                "address": "Av. Cañoto",
                "phone": "591-70000002",
                "image_url": "/uploads/branches/norte.svg",
                "is_active": True,
            },
            {
                "tenant_id": tenant_a.id,
                "name": "Ventura Mall",
                "address": "4to Anillo Equipetrol",
                "phone": "591-70000003",
                "image_url": "/uploads/branches/equipetrol.svg",
                "is_active": True,
            },
        ]

        branches = []
        for b_data in branches_data:
            b = Branch(**b_data)
            db.add(b)
            branches.append(b)
        db.commit()
        for b in branches:
            db.refresh(b)
            print(f"   [+] Sucursal ID {b.id}: {b.name} ({b.address}) [Tenant: {tenant_a.name}]")

        b_centro, b_norte, b_equipetrol = branches


        # ─── 1.5 ROLES Y PERMISOS (RBAC) ──────────────────────────────────────────
        print("\n--- 1.5 Creando Permisos y Roles (RBAC) ---")
        
        # 1. Crear permisos
        permissions_data = [
            # Auditoría
            {"name": "audit:read", "description": "Ver bitácora de auditoría"},
            # Roles y Usuarios
            {"name": "roles:manage", "description": "Crear, editar y eliminar roles y permisos"},
            {"name": "users:read", "description": "Ver usuarios"},
            {"name": "users:write", "description": "Crear y editar usuarios"},
            # Sucursales
            {"name": "branches:read", "description": "Ver sucursales"},
            {"name": "branches:write", "description": "Gestionar sucursales"},
            # Catálogo e Inventario
            {"name": "catalog:read", "description": "Ver catálogo"},
            {"name": "catalog:write", "description": "Gestionar productos y categorías"},
            {"name": "inventory:read", "description": "Ver inventario"},
            {"name": "inventory:write", "description": "Modificar inventario"},
            # SaaS Global
            {"name": "saas:manage", "description": "Gestionar empresas y planes SaaS"},
            # Ventas y Reservas
            {"name": "sales:read", "description": "Ver ventas"},
            {"name": "sales:write", "description": "Registrar ventas"},
            {"name": "reservations:read", "description": "Ver reservas"},
            {"name": "reservations:write", "description": "Gestionar reservas"},
        ]
        
        perms_dict = {}
        for p_data in permissions_data:
            p = Permission(**p_data)
            db.add(p)
            db.commit()
            db.refresh(p)
            perms_dict[p.name] = p
        
        # 2. Crear Roles Predeterminados
        roles_data = [
            {
                "name": "superadmin",
                "description": "Superadministrador Global SaaS (Acceso Total y Multi-Empresa)",
                "is_predefined": True,
                "perms": list(perms_dict.keys()) # Todos los permisos
            },
            {
                "name": "admin",
                "description": "Administrador del sistema (Acceso Total)",
                "is_predefined": True,
                "perms": [p for p in perms_dict.keys() if p != "saas:manage"] # Todos excepto saas global
            },
            {
                "name": "encargado",
                "description": "Encargado de Sucursal",
                "is_predefined": True,
                "perms": ["users:read", "branches:read", "catalog:read", "catalog:write", "inventory:read", "inventory:write", "sales:read", "reservations:read", "reservations:write"]
            },
            {
                "name": "cajero",
                "description": "Cajero de Sucursal",
                "is_predefined": True,
                "perms": ["catalog:read", "inventory:read", "sales:write", "reservations:read", "reservations:write"]
            },
            {
                "name": "cliente",
                "description": "Cliente Final",
                "is_predefined": True,
                "perms": ["catalog:read"]
            }
        ]
        
        roles_dict = {}
        for r_data in roles_data:
            perms_list = r_data.pop("perms")
            r = Role(**r_data)
            db.add(r)
            db.commit()
            db.refresh(r)
            roles_dict[r.name] = r
            print(f"   [+] Rol ID {r.id}: {r.name}")
            
            # Asignar permisos al rol
            for p_name in perms_list:
                rp = RolePermission(role_id=r.id, permission_id=perms_dict[p_name].id)
                db.add(rp)
            db.commit()

        # ─── 2. USUARIOS ───────────────────────────────────────────────────────
        print("\n--- 2. Creando Usuarios ---")
        # Superadmin Global SaaS (sin tenant_id fijo)
        superadmin_user = User(
            email="superadmin@fashionstore.com",
            hashed_password=security.get_password_hash("SuperAdmin@1234"),
            full_name="Super Administrador Global SaaS",
            role_id=roles_dict["superadmin"].id,
            tenant_id=None,
            phone="591-77777777",
            branch_id=None,
            is_active=True,
        )
        db.add(superadmin_user)
        db.commit()
        db.refresh(superadmin_user)
        print(f"   [+] Superadmin Creado: {superadmin_user.email} (Rol: superadmin, Tenant: Global)")
        users_data = [
            # Admin Principal
            {
                "email": settings.FIRST_ADMIN_EMAIL,
                "hashed_password": security.get_password_hash(settings.FIRST_ADMIN_PASSWORD),
                "full_name": settings.FIRST_ADMIN_NAME,
                "role_id": roles_dict["admin"].id,
                "phone": "591-70011111",
                "branch_id": None,
            },
            # Encargados por sucursal
            {
                "email": "encargado.centro@fashionstore.com",
                "hashed_password": security.get_password_hash("Encargado123"),
                "full_name": "Roberto Encargado Centro",
                "role_id": roles_dict["encargado"].id,
                "phone": "591-70022221",
                "branch_id": b_centro.id,
            },
            {
                "email": "encargado.norte@fashionstore.com",
                "hashed_password": security.get_password_hash("Encargado123"),
                "full_name": "Lucía Encargada Norte",
                "role_id": roles_dict["encargado"].id,
                "phone": "591-70022222",
                "branch_id": b_norte.id,
            },
            {
                "email": "encargado.equipetrol@fashionstore.com",
                "hashed_password": security.get_password_hash("Encargado123"),
                "full_name": "Mateo Encargado Equipetrol",
                "role_id": roles_dict["encargado"].id,
                "phone": "591-70022223",
                "branch_id": b_equipetrol.id,
            },
            # Cajeros por sucursal
            {
                "email": "cajero.centro@fashionstore.com",
                "hashed_password": security.get_password_hash("Cajero123"),
                "full_name": "Daniela Cajera Centro",
                "role_id": roles_dict["cajero"].id,
                "phone": "591-70033331",
                "branch_id": b_centro.id,
            },
            {
                "email": "cajero.norte@fashionstore.com",
                "hashed_password": security.get_password_hash("Cajero123"),
                "full_name": "Jorge Cajero Norte",
                "role_id": roles_dict["cajero"].id,
                "phone": "591-70033332",
                "branch_id": b_norte.id,
            },
            {
                "email": "cajero.equipetrol@fashionstore.com",
                "hashed_password": security.get_password_hash("Cajero123"),
                "full_name": "Valeria Cajera Equipetrol",
                "role_id": roles_dict["cajero"].id,
                "phone": "591-70033333",
                "branch_id": b_equipetrol.id,
            },
            # Clientes Finales
            {
                "email": "cliente1@gmail.com",
                "hashed_password": security.get_password_hash("Cliente123"),
                "full_name": "Juan Pérez",
                "role_id": roles_dict["cliente"].id,
                "phone": "591-71044441",
                "branch_id": None,
            },
            {
                "email": "cliente2@gmail.com",
                "hashed_password": security.get_password_hash("Cliente123"),
                "full_name": "María Gómez",
                "role_id": roles_dict["cliente"].id,
                "phone": "591-71044442",
                "branch_id": None,
            },
            {
                "email": "carlos.mendoza@gmail.com",
                "hashed_password": security.get_password_hash("Cliente123"),
                "full_name": "Carlos Mendoza",
                "role_id": roles_dict["cliente"].id,
                "phone": "591-71044443",
                "branch_id": None,
            },
            {
                "email": "ana.torres@gmail.com",
                "hashed_password": security.get_password_hash("Cliente123"),
                "full_name": "Ana Torres",
                "role_id": roles_dict["cliente"].id,
                "phone": "591-71044444",
                "branch_id": None,
            },
        ]

        users_dict = {}
        for u_data in users_data:
            u = User(tenant_id=tenant_a.id, **u_data)
            db.add(u)
            db.commit()
            db.refresh(u)
            users_dict[u.email] = u
            print(f"   [+] Usuario {u.email} -> Sucursal ID: {u.branch_id}")

        # ─── 3. CATEGORÍAS ────────────────────────────────────────────────────
        print("\n--- 3. Creando Categorías con Imágenes ---")
        categories_data = [
            {"name": "Ropa Hombre", "description": "Colección exclusiva urbana y formal para varones", "image_url": "/uploads/categories/hombre.svg"},
            {"name": "Ropa Mujer",  "description": "Vestidos, blusas y tendencias elegantes para damas", "image_url": "/uploads/categories/mujer.svg"},
            {"name": "Calzado",     "description": "Calzado urbano, formal y deportivo multimarca",     "image_url": "/uploads/categories/calzado.svg"},
            {"name": "Accesorios",  "description": "Bolsos de cuero, cinturones, gafas y accesorios",   "image_url": "/uploads/categories/accesorios.svg"},
            {"name": "Deportes",    "description": "Ropa y accesorios para entrenamiento y gimnasio",    "image_url": "/uploads/categories/deportes.svg"},
        ]

        categories_dict = {}
        for c_data in categories_data:
            c = Category(tenant_id=tenant_a.id, **c_data)
            db.add(c)
            db.commit()
            db.refresh(c)
            categories_dict[c.name] = c
            print(f"   [+] Categoría ID {c.id}: {c.name}")


        # ─── 4. PRODUCTOS Y VARIANTES ──────────────────────────────────────────
        print("\n--- 4. Creando Catálogo de Productos y Variantes (Tallas/Colores/SKUs) ---")
        products_data = [
            {
                "name": "Chamarra de Cuero Premium",
                "description": "Chamarra de cuero genuino vacuno con forro térmico y cierres metálicos.",
                "base_price": 120.0,
                "category_id": categories_dict["Ropa Hombre"].id,
                "season": "Invierno 2026",
                "image_url": "/uploads/products/chamarra_cuero.svg",
                "variants": [
                    {"size": "M", "color": "Negro", "sku": "CHA-CUE-NEGRO-M", "price_override": None, "image_url": "/uploads/products/chamarra_cuero.svg"},
                    {"size": "L", "color": "Negro", "sku": "CHA-CUE-NEGRO-L", "price_override": None, "image_url": "/uploads/products/chamarra_cuero.svg"},
                    {"size": "XL", "color": "Café", "sku": "CHA-CUE-CAFE-XL", "price_override": 135.0, "image_url": "/uploads/products/chamarra_cuero.svg"},
                ],
            },
            {
                "name": "Jeans Slim Fit Classic",
                "description": "Pantalón de mezclilla azul con elastano para máxima comodidad.",
                "base_price": 45.0,
                "category_id": categories_dict["Ropa Hombre"].id,
                "season": "Atemporal",
                "image_url": "/uploads/products/jeans_slim.svg",
                "variants": [
                    {"size": "30", "color": "Azul", "sku": "JEA-SLI-AZUL-30", "price_override": None, "image_url": "/uploads/products/jeans_slim.svg"},
                    {"size": "32", "color": "Azul", "sku": "JEA-SLI-AZUL-32", "price_override": None, "image_url": "/uploads/products/jeans_slim.svg"},
                    {"size": "34", "color": "Negro", "sku": "JEA-SLI-NEGRO-34", "price_override": 48.0, "image_url": "/uploads/products/jeans_slim.svg"},
                ],
            },
            {
                "name": "Vestido de Noche Gala Elegante",
                "description": "Vestido largo de fiesta en tela satinada con corte imperial.",
                "base_price": 150.0,
                "category_id": categories_dict["Ropa Mujer"].id,
                "season": "Primavera 2026",
                "image_url": "/uploads/products/vestido_gala.svg",
                "variants": [
                    {"size": "S", "color": "Rojo", "sku": "VES-GAL-ROJO-S", "price_override": None, "image_url": "/uploads/products/vestido_gala.svg"},
                    {"size": "M", "color": "Rojo", "sku": "VES-GAL-ROJO-M", "price_override": None, "image_url": "/uploads/products/vestido_gala.svg"},
                    {"size": "M", "color": "Negro", "sku": "VES-GAL-NEGRO-M", "price_override": 160.0, "image_url": "/uploads/products/vestido_gala.svg"},
                ],
            },
            {
                "name": "Blusa de Seda Floral",
                "description": "Blusa manga larga de seda con estampado floral veraniego.",
                "base_price": 55.0,
                "category_id": categories_dict["Ropa Mujer"].id,
                "season": "Verano 2026",
                "image_url": "/uploads/products/blusa_seda.svg",
                "variants": [
                    {"size": "S", "color": "Blanco", "sku": "BLU-SED-BLANCO-S", "price_override": None, "image_url": "/uploads/products/blusa_seda.svg"},
                    {"size": "M", "color": "Rosa", "sku": "BLU-SED-ROSA-M", "price_override": None, "image_url": "/uploads/products/blusa_seda.svg"},
                ],
            },
            {
                "name": "Tenis Urbanos Street Runner",
                "description": "Tenis casuales de suela alta, ideales para uso diario y caminatas.",
                "base_price": 85.0,
                "category_id": categories_dict["Calzado"].id,
                "season": "2026",
                "image_url": "/uploads/products/tenis_urbanos.svg",
                "variants": [
                    {"size": "40", "color": "Blanco", "sku": "TEN-URB-BLANCO-40", "price_override": None, "image_url": "/uploads/products/tenis_urbanos.svg"},
                    {"size": "41", "color": "Blanco", "sku": "TEN-URB-BLANCO-41", "price_override": None, "image_url": "/uploads/products/tenis_urbanos.svg"},
                    {"size": "42", "color": "Negro", "sku": "TEN-URB-NEGRO-42", "price_override": None, "image_url": "/uploads/products/tenis_urbanos.svg"},
                ],
            },
            {
                "name": "Zapatos de Vestir Oxford",
                "description": "Zapatos de vestir masculinos en fino cuero lustrado.",
                "base_price": 110.0,
                "category_id": categories_dict["Calzado"].id,
                "season": "Ejecutivo",
                "image_url": "/uploads/products/zapatos_oxford.svg",
                "variants": [
                    {"size": "40", "color": "Café", "sku": "ZAP-OXF-CAFE-40", "price_override": None, "image_url": "/uploads/products/zapatos_oxford.svg"},
                    {"size": "41", "color": "Negro", "sku": "ZAP-OXF-NEGRO-41", "price_override": None, "image_url": "/uploads/products/zapatos_oxford.svg"},
                ],
            },
            {
                "name": "Bolso de Mano Leather Bag",
                "description": "Bolso femenino espacioso de cuero genuino con compartimentos internos.",
                "base_price": 75.0,
                "category_id": categories_dict["Accesorios"].id,
                "season": "2026",
                "image_url": "/uploads/products/bolso_mano.svg",
                "variants": [
                    {"size": "Única", "color": "Negro", "sku": "BOL-MAN-NEGRO-U", "price_override": None, "image_url": "/uploads/products/bolso_mano.svg"},
                    {"size": "Única", "color": "Café", "sku": "BOL-MAN-CAFE-U", "price_override": None, "image_url": "/uploads/products/bolso_mano.svg"},
                ],
            },
            {
                "name": "Gafas de Sol Polarizadas UV400",
                "description": "Gafas de sol unisex con protección UV400 y marco de titanio ligero.",
                "base_price": 40.0,
                "category_id": categories_dict["Accesorios"].id,
                "season": "Verano 2026",
                "image_url": "/uploads/products/gafas_sol.svg",
                "variants": [
                    {"size": "Única", "color": "Negro", "sku": "GAF-SOL-NEGRO-U", "price_override": None, "image_url": "/uploads/products/gafas_sol.svg"},
                ],
            },
        ]

        all_variants = []
        for p_data in products_data:
            variants_list = p_data.pop("variants")
            p = Product(tenant_id=tenant_a.id, **p_data)
            db.add(p)
            db.commit()
            db.refresh(p)
            print(f"   [+] Producto ID {p.id}: {p.name}")

            for v_data in variants_list:
                v = ProductVariant(tenant_id=tenant_a.id, product_id=p.id, **v_data)
                db.add(v)
                db.commit()
                db.refresh(v)
                all_variants.append(v)
                print(f"       -> Variante ID {v.id}: {v.sku} ({v.color} / Talla {v.size})")

        # ─── 5. INVENTARIO MULTI-TIENDA ─────────────────────────────────────────
        print("\n--- 5. Registrando Inventarios por Sucursal ---")
        # Distribuir stock entre las 3 sucursales
        inventory_count = 0
        for i, variant in enumerate(all_variants):
            # Asignar stock diferenciado según sucursal
            stock_centro = 15 + (i * 2) % 10
            stock_norte = 8 + (i * 3) % 12
            stock_equipetrol = 20 + (i * 4) % 15

            inv_c = Inventory(tenant_id=tenant_a.id, branch_id=b_centro.id, variant_id=variant.id, stock=stock_centro)
            inv_n = Inventory(tenant_id=tenant_a.id, branch_id=b_norte.id, variant_id=variant.id, stock=stock_norte)
            inv_e = Inventory(tenant_id=tenant_a.id, branch_id=b_equipetrol.id, variant_id=variant.id, stock=stock_equipetrol)

            db.add_all([inv_c, inv_n, inv_e])
            inventory_count += 3

        db.commit()
        print(f"   [OK] {inventory_count} registros de inventario creados en las 3 sucursales.")


        # ─── 6. RESERVAS CON SELECCIÓN DE TIENDA ────────────────────────────────
        print("\n--- 6. Creando Reservas (Con Selección de Tienda por Cliente) ---")
        now = datetime.now()

        reservations_data = [
            {
                "user_id": users_dict["cliente1@gmail.com"].id,
                "branch_id": b_centro.id,  # Tienda seleccionada: Centro
                "status": ReservationStatus.confirmed,
                "pickup_date": now + timedelta(days=2),
                "notes": "Retiro el sábado por la tarde en Sucursal Centro.",
                "items": [
                    {"variant_id": all_variants[0].id, "quantity": 1, "unit_price": all_variants[0].price_override or 120.0},
                ],
            },
            {
                "user_id": users_dict["cliente2@gmail.com"].id,
                "branch_id": b_equipetrol.id,  # Tienda seleccionada: Equipetrol
                "status": ReservationStatus.pending,
                "pickup_date": now + timedelta(days=1),
                "notes": "Deseo probarme la talla M en Sucursal Equipetrol antes de abonar.",
                "items": [
                    {"variant_id": all_variants[6].id, "quantity": 1, "unit_price": all_variants[6].price_override or 150.0},
                    {"variant_id": all_variants[12].id, "quantity": 1, "unit_price": all_variants[12].price_override or 75.0},
                ],
            },
            {
                "user_id": users_dict["carlos.mendoza@gmail.com"].id,
                "branch_id": b_norte.id,  # Tienda seleccionada: Norte
                "status": ReservationStatus.completed,
                "pickup_date": now - timedelta(days=1),
                "notes": "Reserva entregada y pagada en caja Sucursal Norte.",
                "items": [
                    {"variant_id": all_variants[10].id, "quantity": 1, "unit_price": all_variants[10].price_override or 85.0},
                ],
            },
        ]

        for res_data in reservations_data:
            items_data = res_data.pop("items")
            r = Reservation(tenant_id=tenant_a.id, **res_data)
            db.add(r)
            db.commit()
            db.refresh(r)

            for item_d in items_data:
                ri = ReservationItem(tenant_id=tenant_a.id, reservation_id=r.id, **item_d)
                db.add(ri)

            db.commit()
            print(f"   [+] Reserva ID {r.id}: Cliente ID {r.user_id} en Tienda ID {r.branch_id} ({r.branch.name}) -- Estado: {r.status.value}")

        # ─── 7. VENTAS Y ÓRDENES (Pagos con Tarjeta, Efectivo y QR) ────────────
        print("\n--- 7. Registrando Ventas y Órdenes con Métodos de Pago ---")
        orders_data = [
            # 1. Venta Presencial en Sucursal Centro con Tarjeta VISA
            {
                "user_id": users_dict["cliente1@gmail.com"].id,
                "branch_id": b_centro.id,
                "order_type": OrderTypeEnum.presencial,
                "payment_method": PaymentMethodEnum.tarjeta,
                "payment_status": PaymentStatusEnum.paid,
                "card_type": "VISA",
                "card_last_four": "4242",
                "transaction_ref": "POS-AUTH-994120",
                "total_amount": 130.0,
                "is_paid": True,
                "payment_provider": "POS_RedBanb",
                "items": [
                    {"variant_id": all_variants[3].id, "quantity": 1, "unit_price": 45.0},
                    {"variant_id": all_variants[10].id, "quantity": 1, "unit_price": 85.0},
                ],
            },
            # 2. Pago de Reserva Pickup en Sucursal Equipetrol con Tarjeta Mastercard
            {
                "user_id": users_dict["cliente2@gmail.com"].id,
                "branch_id": b_equipetrol.id,
                "order_type": OrderTypeEnum.reserva_pickup,
                "payment_method": PaymentMethodEnum.tarjeta,
                "payment_status": PaymentStatusEnum.paid,
                "card_type": "Mastercard",
                "card_last_four": "8812",
                "transaction_ref": "POS-AUTH-551923",
                "total_amount": 225.0,
                "is_paid": True,
                "payment_provider": "POS_RedBanb",
                "items": [
                    {"variant_id": all_variants[6].id, "quantity": 1, "unit_price": 150.0},
                    {"variant_id": all_variants[12].id, "quantity": 1, "unit_price": 75.0},
                ],
            },
            # 3. Venta Presencial en Sucursal Norte en Efectivo
            {
                "user_id": users_dict["carlos.mendoza@gmail.com"].id,
                "branch_id": b_norte.id,
                "order_type": OrderTypeEnum.presencial,
                "payment_method": PaymentMethodEnum.efectivo,
                "payment_status": PaymentStatusEnum.paid,
                "card_type": None,
                "card_last_four": None,
                "transaction_ref": "EF-CAJA-00412",
                "total_amount": 110.0,
                "is_paid": True,
                "payment_provider": "Caja_Local",
                "items": [
                    {"variant_id": all_variants[11].id, "quantity": 1, "unit_price": 110.0},
                ],
            },
            # 4. Venta Online Delivery pagada con Código QR
            {
                "user_id": users_dict["ana.torres@gmail.com"].id,
                "branch_id": None, # Envío online directo
                "order_type": OrderTypeEnum.online_delivery,
                "payment_method": PaymentMethodEnum.qr,
                "payment_status": PaymentStatusEnum.paid,
                "card_type": None,
                "card_last_four": None,
                "transaction_ref": "QR-BNB-774912",
                "total_amount": 95.0,
                "is_paid": True,
                "payment_provider": "PagoQR_Simple",
                "items": [
                    {"variant_id": all_variants[8].id, "quantity": 1, "unit_price": 55.0},
                    {"variant_id": all_variants[14].id, "quantity": 1, "unit_price": 40.0},
                ],
            },
        ]

        for ord_data in orders_data:
            items_data = ord_data.pop("items")
            o = Order(tenant_id=tenant_a.id, **ord_data)
            db.add(o)
            db.commit()
            db.refresh(o)

            for item_d in items_data:
                oi = OrderItem(tenant_id=tenant_a.id, order_id=o.id, **item_d)
                db.add(oi)

            db.commit()
            print(f"   [+] Orden ID {o.id}: Tipo: {o.order_type.value} | Método: {o.payment_method.value} ({o.card_type or 'N/A'} {o.card_last_four or ''}) | Total: ${o.total_amount}")

        # ─── 8. EMPRESA B (BOUTIQUE EXPRESS - PLAN BÁSICO) -- AISLAMIENTO TOTAL ───
        print("\n--- 8. Creando Datos Aislados para Empresa B (Plan Básico) ---")
        # 1 Sucursal para Empresa B (Plan Básico permite máx 2)
        b_boutique = Branch(
            tenant_id=tenant_b.id,
            name="Boutique Central",
            address="Calle Sucre #123",
            city="Santa Cruz",
            phone="591-72200001",
            image_url="/uploads/branches/centro.svg",
            is_active=True,
        )
        db.add(b_boutique)
        db.commit()
        db.refresh(b_boutique)
        print(f"   [+] Sucursal Empresa B: ID {b_boutique.id} - {b_boutique.name}")

        # Usuarios de Empresa B
        admin_b = User(
            email="admin@boutique.com",
            hashed_password=security.get_password_hash("Admin123!"),
            full_name="Admin Boutique Express",
            role_id=roles_dict["admin"].id,
            tenant_id=tenant_b.id,
            phone="591-72211111",
        )
        cajero_b = User(
            email="cajero@boutique.com",
            hashed_password=security.get_password_hash("Cajero123!"),
            full_name="Cajero Boutique Express",
            role_id=roles_dict["cajero"].id,
            branch_id=b_boutique.id,
            tenant_id=tenant_b.id,
            phone="591-72222222",
        )
        cliente_b = User(
            email="cliente@boutique.com",
            hashed_password=security.get_password_hash("Cliente123!"),
            full_name="Cliente Exclusivo Boutique",
            role_id=roles_dict["cliente"].id,
            tenant_id=tenant_b.id,
            phone="591-72233333",
        )
        db.add_all([admin_b, cajero_b, cliente_b])
        db.commit()
        db.refresh(admin_b)
        db.refresh(cajero_b)
        db.refresh(cliente_b)
        print(f"   [+] Usuarios Empresa B: {admin_b.email}, {cajero_b.email}, {cliente_b.email}")

        # Categoría y Catálogo propio de Empresa B
        cat_b = Category(
            tenant_id=tenant_b.id,
            name="Moda Exclusiva Boutique",
            description="Línea de ropa premium exclusiva de Boutique Express",
            image_url="/uploads/categories/hombre.svg",
        )
        db.add(cat_b)
        db.commit()
        db.refresh(cat_b)

        prod_b1 = Product(
            tenant_id=tenant_b.id,
            name="Blazer Italiano Boutique",
            description="Blazer de corte entallado en paño italiano.",
            base_price=350.0,
            category_id=cat_b.id,
            season="Otoño 2026",
            image_url="/uploads/products/chamarra_cuero.svg",
        )
        prod_b2 = Product(
            tenant_id=tenant_b.id,
            name="Pantalón Casual de Lino",
            description="Pantalón fresco 100% lino para clima cálido.",
            base_price=180.0,
            category_id=cat_b.id,
            season="Verano 2026",
            image_url="/uploads/products/jeans_slim.svg",
        )
        db.add_all([prod_b1, prod_b2])
        db.commit()
        db.refresh(prod_b1)
        db.refresh(prod_b2)

        v_b1_s = ProductVariant(tenant_id=tenant_b.id, product_id=prod_b1.id, size="M", color="Azul Marino", sku="BOU-BLA-AZUL-M")
        v_b1_l = ProductVariant(tenant_id=tenant_b.id, product_id=prod_b1.id, size="L", color="Azul Marino", sku="BOU-BLA-AZUL-L")
        v_b2_32 = ProductVariant(tenant_id=tenant_b.id, product_id=prod_b2.id, size="32", color="Beige", sku="BOU-LIN-BEIGE-32")
        db.add_all([v_b1_s, v_b1_l, v_b2_32])
        db.commit()
        db.refresh(v_b1_s)
        db.refresh(v_b1_l)
        db.refresh(v_b2_32)

        # Inventarios de Empresa B en su única sucursal
        inv_b1 = Inventory(tenant_id=tenant_b.id, branch_id=b_boutique.id, variant_id=v_b1_s.id, stock=12)
        inv_b2 = Inventory(tenant_id=tenant_b.id, branch_id=b_boutique.id, variant_id=v_b1_l.id, stock=8)
        inv_b3 = Inventory(tenant_id=tenant_b.id, branch_id=b_boutique.id, variant_id=v_b2_32.id, stock=15)
        db.add_all([inv_b1, inv_b2, inv_b3])
        db.commit()
        print(f"   [+] Catálogo Empresa B: 2 productos, 3 variantes, stock asignado a {b_boutique.name}")

        # Venta propia de Empresa B
        order_b = Order(
            tenant_id=tenant_b.id,
            user_id=cliente_b.id,
            branch_id=b_boutique.id,
            order_type=OrderTypeEnum.presencial,
            payment_method=PaymentMethodEnum.efectivo,
            payment_status=PaymentStatusEnum.paid,
            total_amount=350.0,
            is_paid=True,
            payment_provider="Caja_Boutique",
            transaction_ref="EF-BOU-0001",
        )
        db.add(order_b)
        db.commit()
        db.refresh(order_b)
        db.add(OrderItem(tenant_id=tenant_b.id, order_id=order_b.id, variant_id=v_b1_s.id, quantity=1, unit_price=350.0))
        db.commit()
        print(f"   [+] Venta Empresa B: ID {order_b.id} por Bs. {order_b.total_amount}")

        # Reserva propia de Empresa B
        res_b = Reservation(
            tenant_id=tenant_b.id,
            user_id=cliente_b.id,
            branch_id=b_boutique.id,
            status=ReservationStatus.pending,
            pickup_date=datetime.now() + timedelta(days=2),
            notes="Reserva exclusiva para cliente Boutique.",
        )
        db.add(res_b)
        db.commit()
        db.refresh(res_b)
        db.add(ReservationItem(tenant_id=tenant_b.id, reservation_id=res_b.id, variant_id=v_b2_32.id, quantity=1, unit_price=180.0))
        db.commit()
        print(f"   [+] Reserva Empresa B: ID {res_b.id} en {b_boutique.name}")

        print()
        print(sep)
        print("  ¡Poblamiento Multitenant (Empresa A Profesional + Empresa B Básico) completado con éxito!")
        print(sep)


    except Exception as e:
        db.rollback()
        print(f"\n[ERROR] Error durante el seed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()

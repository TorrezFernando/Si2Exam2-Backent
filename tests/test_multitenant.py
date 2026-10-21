import pytest
from fastapi.testclient import TestClient
from fastapi import APIRouter, Depends
from main import app
from api.deps import require_feature, get_db
from db.models.branch import Branch


# Endpoint temporal para verificar la guardia de feature en tests
feature_test_router = APIRouter()

@feature_test_router.get("/api/v1/test/ai-feature")
def dummy_ai_feature(current_user=Depends(require_feature("ai_reports"))):
    return {"status": "ok", "tenant_id": current_user.tenant_id, "feature": "ai_reports"}

app.include_router(feature_test_router)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_login_returns_multitenant_token(client):
    # 1. Login Tenant A (Empresa A - Plan Profesional)
    res_a = client.post("/api/v1/auth/login", data={"username": "admin@fashionstore.com", "password": "Admin@1234"})
    assert res_a.status_code == 200, res_a.text
    data_a = res_a.json()
    assert data_a["tenant_id"] == 1
    assert data_a["plan_code"] == "professional"
    assert "access_token" in data_a

    # 2. Login Tenant B (Empresa B - Plan Básico)
    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    assert res_b.status_code == 200, res_b.text
    data_b = res_b.json()
    assert data_b["tenant_id"] == 2
    assert data_b["plan_code"] == "basic"
    assert "access_token" in data_b


def test_tenant_data_isolation_branches(client):
    res_a = client.post("/api/v1/auth/login", data={"username": "admin@fashionstore.com", "password": "Admin@1234"})
    token_a = res_a.json()["access_token"]

    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    token_b = res_b.json()["access_token"]

    # Tenant A lista sus sucursales
    resp_branches_a = client.get("/api/v1/branches", headers={"Authorization": f"Bearer {token_a}"})
    assert resp_branches_a.status_code == 200
    branches_a = resp_branches_a.json()
    assert len(branches_a) == 3
    for b in branches_a:
        assert b["tenant_id"] == 1

    # Tenant B lista sus sucursales (nunca ve las de Tenant A)
    resp_branches_b = client.get("/api/v1/branches", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_branches_b.status_code == 200
    branches_b = resp_branches_b.json()
    assert len(branches_b) >= 1
    for b in branches_b:
        assert b["tenant_id"] == 2
        assert b["name"] != "Comercial Brasil"
        assert b["name"] != "Comercial Cañoto"
        assert b["name"] != "Ventura Mall"

    # Tenant B no puede acceder por ID a una sucursal de Tenant A
    resp_cross = client.get("/api/v1/branches/1", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_cross.status_code == 404


def test_tenant_data_isolation_users(client):
    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    token_b = res_b.json()["access_token"]

    # Listar usuarios como Tenant B -> solo usuarios con tenant_id == 2
    resp_users_b = client.get("/api/v1/users", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_users_b.status_code == 200
    users_b = resp_users_b.json()
    assert len(users_b) == 3
    for u in users_b:
        assert u["tenant_id"] == 2

    # Intentar acceder a usuario ID 1 (admin de Tenant A) desde Tenant B -> 404
    resp_user_cross = client.get("/api/v1/users/1", headers={"Authorization": f"Bearer {token_b}"})
    assert resp_user_cross.status_code == 404


def test_plan_branch_limit_enforcement(client):
    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    token_b = res_b.json()["access_token"]

    # Limpiar cualquier sucursal extra creada en corridas anteriores
    branches_now = client.get("/api/v1/branches", headers={"Authorization": f"Bearer {token_b}"}).json()
    for b in branches_now:
        if b["name"] in ("Boutique Sucursal Norte", "Boutique Sucursal 3 Ilegal"):
            client.delete(f"/api/v1/branches/{b['id']}", headers={"Authorization": f"Bearer {token_b}"})

    # Verificar que tras limpieza hay 1 sucursal activa
    active_branches = client.get("/api/v1/branches?active_only=true", headers={"Authorization": f"Bearer {token_b}"}).json()
    assert len(active_branches) == 1

    # Tenant B tiene 1 sucursal activa. El Plan Básico permite máx 2.
    # Crear la 2da sucursal activa (debe ser exitoso)
    res_create_2 = client.post(
        "/api/v1/branches",
        headers={"Authorization": f"Bearer {token_b}"},
        json={
            "name": "Boutique Sucursal Norte",
            "city": "Santa Cruz",
            "address": "Av. Banzer Km 5",
            "phone": "591-72244444",
            "is_active": True,
        },
    )
    assert res_create_2.status_code == 201, res_create_2.text
    created_branch_2_id = res_create_2.json()["id"]

    try:
        # Intentar crear la 3ra sucursal activa -> Debe ser bloqueado por HTTP 403 (Límite alcanzado)
        res_create_3 = client.post(
            "/api/v1/branches",
            headers={"Authorization": f"Bearer {token_b}"},
            json={
                "name": "Boutique Sucursal 3 Ilegal",
                "city": "Santa Cruz",
                "address": "Calle Falsa 123",
                "phone": "591-72255555",
                "is_active": True,
            },
        )
        assert res_create_3.status_code == 403
        assert "Límite de sucursales alcanzado" in res_create_3.json()["detail"]
    finally:
        # Limpiar para mantener idempotencia
        client.delete(f"/api/v1/branches/{created_branch_2_id}", headers={"Authorization": f"Bearer {token_b}"})


def test_plan_feature_flag_ai_reports_guard(client):
    res_a = client.post("/api/v1/auth/login", data={"username": "admin@fashionstore.com", "password": "Admin@1234"})
    token_a = res_a.json()["access_token"]

    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    token_b = res_b.json()["access_token"]

    # Tenant B (Plan Básico) intenta acceder a endpoint de IA -> 403 Forbidden
    res_b_ai = client.get("/api/v1/test/ai-feature", headers={"Authorization": f"Bearer {token_b}"})
    assert res_b_ai.status_code == 403
    assert "no está disponible en el Plan Básico" in res_b_ai.json()["detail"]

    # Tenant A (Plan Profesional) accede a endpoint de IA -> 200 OK
    res_a_ai = client.get("/api/v1/test/ai-feature", headers={"Authorization": f"Bearer {token_a}"})
    assert res_a_ai.status_code == 200
    assert res_a_ai.json()["status"] == "ok"
    assert res_a_ai.json()["tenant_id"] == 1


def test_superadmin_access_and_tenant_management(client):
    # 1. Login como Superadmin Global
    res_super = client.post("/api/v1/auth/login", data={"username": "superadmin@fashionstore.com", "password": "SuperAdmin@1234"})
    assert res_super.status_code == 200, res_super.text
    data_super = res_super.json()
    assert data_super["role"] == "superadmin"
    assert data_super["plan_code"] == "enterprise_superadmin"
    token_super = data_super["access_token"]

    # 2. Superadmin lista todas las empresas registradas (Tenant A y Tenant B)
    res_tenants = client.get("/api/v1/saas/tenants", headers={"Authorization": f"Bearer {token_super}"})
    assert res_tenants.status_code == 200, res_tenants.text
    tenants = res_tenants.json()
    assert len(tenants) >= 2
    tenant_names = [t["name"] for t in tenants]
    assert any("FashionStore Corp" in n for n in tenant_names)
    assert any("Boutique Express" in n for n in tenant_names)

    # 3. Superadmin puede modificar una empresa (ej. Empresa B ventana de cambio)
    res_patch = client.patch(
        "/api/v1/saas/tenants/2",
        headers={"Authorization": f"Bearer {token_super}"},
        json={"exchange_window_days": 45, "name": "Boutique Express Modificada"}
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["exchange_window_days"] == 45
    assert res_patch.json()["name"] == "Boutique Express Modificada"

    # 4. Superadmin puede consultar métricas SaaS globales
    res_stats = client.get("/api/v1/saas/stats", headers={"Authorization": f"Bearer {token_super}"})
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["total_tenants"] >= 2
    assert stats["total_branches"] >= 4

    # 5. Usuario normal (admin de boutique) NO puede listar todos los tenants
    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    token_b = res_b.json()["access_token"]
    res_forbidden = client.get("/api/v1/saas/tenants", headers={"Authorization": f"Bearer {token_b}"})
    assert res_forbidden.status_code == 403


def test_tenant_admin_view_and_change_plan(client):
    # 1. Login Tenant B
    res_b = client.post("/api/v1/auth/login", data={"username": "admin@boutique.com", "password": "Admin123!"})
    token_b = res_b.json()["access_token"]

    # 2. Consultar lista pública de planes SaaS
    res_plans = client.get("/api/v1/saas/plans")
    assert res_plans.status_code == 200
    plans = res_plans.json()
    assert len(plans) >= 2
    codes = [p["code"] for p in plans]
    assert "basic" in codes
    assert "professional" in codes

    # 3. Consultar mi suscripción actual
    res_my_sub = client.get("/api/v1/saas/my-subscription", headers={"Authorization": f"Bearer {token_b}"})
    assert res_my_sub.status_code == 200
    my_sub = res_my_sub.json()
    assert my_sub["plan"]["code"] == "basic"
    assert my_sub["usage"]["branches_count"] >= 1
    assert my_sub["usage"]["branches_limit"] == 2

    # 4. Actualizar plan a Profesional
    res_upgrade = client.post(
        "/api/v1/saas/my-subscription/change-plan",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"plan_code": "professional"}
    )
    assert res_upgrade.status_code == 200
    assert res_upgrade.json()["active_plan"]["code"] == "professional"

    # 5. Verificar que ahora tiene acceso a reportes IA gracias al upgrade
    res_b_ai = client.get("/api/v1/test/ai-feature", headers={"Authorization": f"Bearer {token_b}"})
    assert res_b_ai.status_code == 200

    # 6. Revertir a plan básico para mantener estado limpio
    res_downgrade = client.post(
        "/api/v1/saas/my-subscription/change-plan",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"plan_code": "basic"}
    )
    assert res_downgrade.status_code == 200
    assert res_downgrade.json()["active_plan"]["code"] == "basic"


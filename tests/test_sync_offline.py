import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from main import app
from db.database import SessionLocal
from db.models.tenant import Tenant
from db.models.branch import Branch, Inventory
from db.models.product import Product, ProductVariant
from db.models.sale import Order, OrderItem, PaymentStatusEnum, PaymentMethodEnum, GarmentExchange
from db.models.reservation import Reservation, ReservationStatus


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def auth_headers(client):
    # Tenant 1 (FashionStore - Plan Profesional)
    res_a = client.post(
        "/api/v1/auth/login",
        data={"username": "admin@fashionstore.com", "password": "Admin@1234"},
    )
    assert res_a.status_code == 200, res_a.text
    token_a = res_a.json()["access_token"]

    # Tenant 2 (Boutique Elegance - Plan Básico)
    res_b = client.post(
        "/api/v1/auth/login",
        data={"username": "admin@boutique.com", "password": "Admin123!"},
    )
    assert res_b.status_code == 200, res_b.text
    token_b = res_b.json()["access_token"]

    return {
        "tenant_a": {"Authorization": f"Bearer {token_a}"},
        "tenant_b": {"Authorization": f"Bearer {token_b}"},
    }


# ─── 1. TEST CATÁLOGO OFFLINE (GET /api/v1/sync/catalog) ─────────────────────

def test_sync_catalog_success(client, auth_headers):
    # Tenant A consulta catálogo para la sucursal 1
    resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=auth_headers["tenant_a"])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["tenant_id"] == 1
    assert data["branch_id"] == 1
    assert "branch_name" in data
    assert data["exchange_window_days"] >= 1
    assert len(data["products"]) > 0
    assert len(data["categories"]) > 0
    assert "server_timestamp" in data

    # Verificar que cada producto traiga sus variantes y stock de la sucursal 1
    first_prod = data["products"][0]
    assert len(first_prod["variants"]) > 0
    first_var = first_prod["variants"][0]
    assert "stock" in first_var
    assert "sku" in first_var


def test_sync_catalog_tenant_isolation(client, auth_headers):
    # Tenant B intenta sincronizar inventario de una sucursal de Tenant A (branch_id=1)
    resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=auth_headers["tenant_b"])
    assert resp.status_code == 403
    assert "no pertenece a su empresa" in resp.json()["detail"]


# ─── 2. TEST SINCRONIZACIÓN BATCH DE VENTAS E IDEMPOTENCIA ─────────────────────

def test_sync_batch_sales_and_idempotency(client, auth_headers):
    headers = auth_headers["tenant_a"]

    # 1. Obtener catálogo para elegir variante y conocer su stock
    cat_resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers)
    cat_data = cat_resp.json()
    variant = None
    for p in cat_data["products"]:
        for v in p["variants"]:
            if v["stock"] >= 5:
                variant = v
                break
        if variant:
            break
    assert variant is not None, "Se requiere al menos una variante con stock >= 5 en sucursal 1"

    initial_stock = variant["stock"]
    sale_uuid = f"test-uuid-sale-{datetime.now().timestamp()}"

    # 2. Enviar lote con venta offline
    payload = {
        "branch_id": 1,
        "sales": [
            {
                "client_operation_uuid": sale_uuid,
                "payment_method": "efectivo",
                "total_amount": variant["price"] * 2,
                "items": [
                    {
                        "variant_id": variant["id"],
                        "quantity": 2,
                        "unit_price": variant["price"],
                    }
                ],
            }
        ],
        "reservations": [],
    }

    sync_resp = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert sync_resp.status_code == 200, sync_resp.text
    sync_data = sync_resp.json()
    assert sync_data["total_processed"] == 1
    assert sync_data["confirmed_count"] == 1
    assert sync_data["conflict_count"] == 0
    receipt = sync_data["receipts"][0]
    assert receipt["client_operation_uuid"] == sale_uuid
    assert receipt["status"] == "CONFIRMADA"
    server_id = receipt["server_id"]
    assert server_id is not None

    # Verificar que el stock se descontó exactamente en 2 unidades
    cat_after = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    updated_var = next(v for p in cat_after["products"] for v in p["variants"] if v["id"] == variant["id"])
    assert updated_var["stock"] == initial_stock - 2

    # 3. Test de Idempotencia: re-enviar la MISMA operación (mismo UUID)
    idemp_resp = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert idemp_resp.status_code == 200
    idemp_data = idemp_resp.json()
    assert idemp_data["confirmed_count"] == 1
    assert idemp_data["conflict_count"] == 0
    idemp_receipt = idemp_data["receipts"][0]
    assert idemp_receipt["status"] == "CONFIRMADA"
    assert idemp_receipt["server_id"] == server_id

    # El stock NO debe descontarse por segunda vez
    cat_idemp = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    var_idemp = next(v for p in cat_idemp["products"] for v in p["variants"] if v["id"] == variant["id"])
    assert var_idemp["stock"] == initial_stock - 2


# ─── 3. TEST MANEJO DE CONFLICTO (RECHAZADA_CONFLICTO) ───────────────────────

def test_sync_sale_stock_conflict_handling(client, auth_headers):
    headers = auth_headers["tenant_a"]

    cat_resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers)
    variant = cat_resp.json()["products"][0]["variants"][0]
    current_stock = variant["stock"]

    # Intentar vender más unidades de las disponibles en el servidor
    excess_qty = current_stock + 9999
    conflict_uuid = f"test-conflict-uuid-{datetime.now().timestamp()}"

    payload = {
        "branch_id": 1,
        "sales": [
            {
                "client_operation_uuid": conflict_uuid,
                "payment_method": "efectivo",
                "total_amount": round(excess_qty * variant["price"], 2),
                "items": [
                    {
                        "variant_id": variant["id"],
                        "quantity": excess_qty,
                        "unit_price": variant["price"],
                    }
                ],
            }
        ],
        "reservations": [],
    }

    res = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total_processed"] == 1
    assert data["confirmed_count"] == 0
    assert data["conflict_count"] == 1

    receipt = data["receipts"][0]
    assert receipt["client_operation_uuid"] == conflict_uuid
    assert receipt["status"] == "RECHAZADA_CONFLICTO"
    assert "Stock insuficiente" in receipt["rejection_reason"]
    assert receipt["server_id"] is None

    # El stock en la BD debe permanecer intacto
    cat_after = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    var_after = next(v for p in cat_after["products"] for v in p["variants"] if v["id"] == variant["id"])
    assert var_after["stock"] == current_stock


# ─── 4. TEST SINCRONIZACIÓN BATCH DE RESERVAS ────────────────────────────────

def test_sync_batch_reservations(client, auth_headers):
    headers = auth_headers["tenant_a"]

    cat_resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers)
    variant = next(v for p in cat_resp.json()["products"] for v in p["variants"] if v["stock"] >= 2)
    stock_before = variant["stock"]
    customer = cat_resp.json()["customers"][0]

    res_uuid = f"test-res-uuid-{datetime.now().timestamp()}"
    payload = {
        "branch_id": 1,
        "sales": [],
        "reservations": [
            {
                "client_operation_uuid": res_uuid,
                "user_id": customer["id"],
                "notes": "Reserva offline de prueba",
                "items": [
                    {
                        "variant_id": variant["id"],
                        "quantity": 1,
                    }
                ],
            }
        ],
    }

    res = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["confirmed_count"] == 1
    receipt = data["receipts"][0]
    assert receipt["status"] == "CONFIRMADA"
    assert receipt["operation_type"] == "reservation"
    reservation_id = receipt["server_id"]

    # Stock retenido
    cat_after = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    var_after = next(v for p in cat_after["products"] for v in p["variants"] if v["id"] == variant["id"])
    assert var_after["stock"] == stock_before - 1

    # Idempotencia de reserva
    res_idemp = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert res_idemp.status_code == 200
    assert res_idemp.json()["confirmed_count"] == 1
    assert res_idemp.json()["receipts"][0]["server_id"] == reservation_id


# ─── 5. TEST GESTIÓN DE CAMBIO DE PRENDAS (GARMENT EXCHANGE) ─────────────────

def test_garment_exchange_flow_and_window(client, auth_headers):
    headers = auth_headers["tenant_a"]

    # 1. Crear una orden pagada en la sucursal 1 con 2 variantes distintas
    cat_resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    all_vars = [v for p in cat_resp["products"] for v in p["variants"] if v["stock"] >= 5]
    assert len(all_vars) >= 2, "Se necesitan al menos 2 variantes con stock para probar el cambio"
    var_original = all_vars[0]
    var_nueva = all_vars[1]

    order_payload = {
        "branch_id": 1,
        "items": [
            {"variant_id": var_original["id"], "quantity": 2, "unit_price": var_original["price"]}
        ],
        "payment_method": "efectivo",
        "order_type": "presencial",
    }
    ord_resp = client.post("/api/v1/orders", json=order_payload, headers=headers)
    assert ord_resp.status_code == 201, ord_resp.text
    order = ord_resp.json()
    order_id = order["id"]

    stock_orig_before = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    stock_orig_val = next(v["stock"] for p in stock_orig_before["products"] for v in p["variants"] if v["id"] == var_original["id"])
    stock_nueva_val = next(v["stock"] for p in stock_orig_before["products"] for v in p["variants"] if v["id"] == var_nueva["id"])

    # 2. Realizar cambio válido de 1 unidad de var_original por var_nueva
    exchange_payload = {
        "branch_id": 1,
        "original_variant_id": var_original["id"],
        "new_variant_id": var_nueva["id"],
        "quantity": 1,
        "reason": "Cambio por talla incorrecta",
    }
    ex_resp = client.post(f"/api/v1/orders/{order_id}/exchange", json=exchange_payload, headers=headers)
    assert ex_resp.status_code == 200, ex_resp.text
    ex_data = ex_resp.json()
    assert ex_data["status"] == "APPROVED"
    assert ex_data["quantity"] == 1
    assert ex_data["order_id"] == order_id
    assert ex_data["exchange_window_days"] >= 1

    # Verificar ajuste de stock: la devuelta reingresa (+1) y la nueva egresa (-1)
    stock_after = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    stock_orig_after = next(v["stock"] for p in stock_after["products"] for v in p["variants"] if v["id"] == var_original["id"])
    stock_nueva_after = next(v["stock"] for p in stock_after["products"] for v in p["variants"] if v["id"] == var_nueva["id"])

    assert stock_orig_after == stock_orig_val + 1
    assert stock_nueva_after == stock_nueva_val - 1

    # 3. Intentar cambiar más de lo comprado (quedaba solo 1 disponible para cambiar, pedir 2)
    ex_excess = client.post(
        f"/api/v1/orders/{order_id}/exchange",
        json={**exchange_payload, "quantity": 2},
        headers=headers,
    )
    assert ex_excess.status_code == 400
    assert "No puedes cambiar" in ex_excess.json()["detail"]

    # 4. Consultar historial de cambios de la orden
    hist_resp = client.get(f"/api/v1/orders/{order_id}/exchanges", headers=headers)
    assert hist_resp.status_code == 200
    assert len(hist_resp.json()) == 1


def test_garment_exchange_window_expired(client, auth_headers):
    headers = auth_headers["tenant_a"]

    # Crear una orden directamente en DB con fecha de compra de hace 60 días
    with SessionLocal() as db:
        order = Order(
            tenant_id=1,
            user_id=1,
            branch_id=1,
            order_type="presencial",
            payment_method="efectivo",
            payment_status=PaymentStatusEnum.paid,
            is_paid=True,
            total_amount=100.0,
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        db.add(order)
        db.flush()

        cat = db.query(ProductVariant).first()
        item = OrderItem(
            tenant_id=1,
            order_id=order.id,
            variant_id=cat.id,
            quantity=1,
            unit_price=100.0,
        )
        db.add(item)
        db.commit()
        expired_order_id = order.id
        variant_id = cat.id

    # Intentar cambiar la prenda: debe ser rechazada por ventana de días expirada
    payload = {
        "branch_id": 1,
        "original_variant_id": variant_id,
        "new_variant_id": variant_id,
        "quantity": 1,
        "reason": "Intento de cambio fuera de plazo",
    }
    resp = client.post(f"/api/v1/orders/{expired_order_id}/exchange", json=payload, headers=headers)
    assert resp.status_code == 400
    assert "Plazo para cambio de prendas vencido" in resp.json()["detail"]


def test_sync_rejects_card_payment_for_offline(client, auth_headers):
    headers = auth_headers["tenant_a"]
    cat_resp = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers).json()
    variant = cat_resp["products"][0]["variants"][0]

    uuid_test = f"test-card-reject-{datetime.now().timestamp()}"
    payload = {
        "branch_id": 1,
        "sales": [
            {
                "client_operation_uuid": uuid_test,
                "payment_method": "tarjeta",  # Tarjeta no permitida en offline
                "total_amount": 100.0,
                "items": [{"variant_id": variant["id"], "quantity": 1, "unit_price": variant["price"]}],
            }
        ],
        "reservations": [],
    }

    res = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["conflict_count"] == 1
    assert data["receipts"][0]["status"] == "RECHAZADA_CONFLICTO"
    assert "Solo se admite efectivo" in data["receipts"][0]["rejection_reason"]

    # Reintento con mismo UUID debe ser estrictamente idempotente y conservar conflicto
    res_repeat = client.post("/api/v1/sync/operations", json=payload, headers=headers)
    assert res_repeat.status_code == 200
    data_repeat = res_repeat.json()
    assert data_repeat["conflict_count"] == 1
    assert data_repeat["receipts"][0]["status"] == "RECHAZADA_CONFLICTO"


def test_sync_rejects_cross_tenant_customer(client, auth_headers):
    headers_a = auth_headers["tenant_a"]

    with SessionLocal() as db:
        from db.models.user import User
        user_b = db.query(User).filter(User.tenant_id == 2).first()
        customer_b_id = user_b.id if user_b else 99999

    cat_a = client.get("/api/v1/sync/catalog?branch_id=1", headers=headers_a).json()
    variant_a = cat_a["products"][0]["variants"][0]

    uuid_test = f"test-cross-tenant-{datetime.now().timestamp()}"
    payload = {
        "branch_id": 1,
        "sales": [
            {
                "client_operation_uuid": uuid_test,
                "user_id": customer_b_id,  # Cliente ajeno al Tenant A
                "payment_method": "efectivo",
                "total_amount": variant_a["price"],
                "items": [{"variant_id": variant_a["id"], "quantity": 1, "unit_price": variant_a["price"]}],
            }
        ],
        "reservations": [],
    }

    res = client.post("/api/v1/sync/operations", json=payload, headers=headers_a)
    assert res.status_code == 200
    data = res.json()
    assert data["conflict_count"] == 1
    assert data["receipts"][0]["status"] == "RECHAZADA_CONFLICTO"
    assert "no pertenece a la empresa" in data["receipts"][0]["rejection_reason"] or "no existe" in data["receipts"][0]["rejection_reason"]


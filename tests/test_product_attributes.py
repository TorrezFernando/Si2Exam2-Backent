import pytest
from test_security_regressions import shop, client
from db.models import Permission
from api.routers.sync import get_offline_catalog


def test_attributes_roundtrip_partial_update_clear_and_offline(shop):
    shop.user.role.permissions.append(Permission(name='catalog:write'))
    shop.db.commit()
    api = client(shop, shop.user)
    response = api.post('/products', json={'name': 'Camisa', 'base_price': 100,
        'weight_grams': 250.5, 'fabric_type': ' Algodón ', 'design': ' Floral '})
    assert response.status_code == 201, response.text
    product = response.json()
    assert product['fabric_type'] == 'Algodón'
    product_id = product['id']
    response = api.patch(f'/products/{product_id}', json={'name': 'Camisa nueva'})
    assert response.json()['weight_grams'] == 250.5
    catalog = get_offline_catalog(branch_id=shop.branch.id, last_sync_at=None, db=shop.db, current_user=shop.user)
    row = next(p for p in catalog.products if p.id == product_id)
    assert (row.weight_grams, row.fabric_type, row.design) == (250.5, 'Algodón', 'Floral')
    response = api.patch(f'/products/{product_id}', json={'weight_grams': None, 'fabric_type': ' ', 'design': None})
    assert response.status_code == 200
    assert all(response.json()[k] is None for k in ('weight_grams', 'fabric_type', 'design'))


@pytest.mark.parametrize('attributes', [
    {'weight_grams': 0}, {'weight_grams': -1}, {'weight_grams': 1000001},
    {'weight_grams': 'NaN'}, {'fabric_type': 'x'*101}, {'design': 'x'*256},
])
def test_invalid_attributes_are_rejected(shop, attributes):
    shop.user.role.permissions.append(Permission(name='catalog:write'))
    shop.db.commit()
    api = client(shop, shop.user)
    assert api.post('/products', json={'name': 'Bad', 'base_price': 10, **attributes}).status_code == 422

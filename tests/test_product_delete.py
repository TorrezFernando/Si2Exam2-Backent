from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.routers.products import delete_product
from db.database import Base
from db.models.branch import Branch, Inventory
from db.models.product import Product, ProductVariant
from db.models.reservation import Reservation, ReservationItem
from db.models.sale import Order, OrderItem


def test_delete_product_preserves_related_records():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        branch = Branch(name="Sucursal 1", address="Calle 1")
        product = Product(name="Camisa", description="Test", base_price=120.0, category_id=None, season="2026")
        variant = ProductVariant(product=product, size="M", color="Blanco", sku="CAM-TEST-M")
        order = Order(user_id=1, branch_id=1, total_amount=120.0)
        reservation = Reservation(user_id=1, branch_id=1)

        db.add_all([branch, product, variant, order, reservation])
        db.flush()

        db.add(Inventory(branch_id=branch.id, variant_id=variant.id, stock=5))
        db.add(OrderItem(order_id=order.id, variant_id=variant.id, quantity=1, unit_price=120.0))
        db.add(ReservationItem(reservation_id=reservation.id, variant_id=variant.id, quantity=1, unit_price=120.0))
        db.commit()

        deleted = delete_product(db=db, product_id=product.id, current_user=object())

        assert deleted.id == product.id
        assert not deleted.is_active
        assert db.query(Product).filter(Product.id == product.id).count() == 1
        assert db.query(ProductVariant).filter(ProductVariant.id == variant.id).count() == 1
        assert db.query(Inventory).filter(Inventory.variant_id == variant.id).count() == 1
        assert db.query(OrderItem).filter(OrderItem.variant_id == variant.id).count() == 1
        assert db.query(ReservationItem).filter(ReservationItem.variant_id == variant.id).count() == 1

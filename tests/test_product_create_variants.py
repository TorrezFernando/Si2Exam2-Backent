from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.database import Base
from db.models.branch import Branch, Inventory
from db.models.product import Product, ProductVariant
from api.routers.products import create_product, update_product
from schemas.product import ProductCreate, ProductUpdate
from db.models import User, Role, Permission


def admin(db):
    user = db.query(User).first()
    if user:
        return user
    user = User(email='admin@example.com', hashed_password='unused', role=Role(name='admin', permissions=[Permission(name='branches:write')]))
    db.add(user)
    db.commit()
    return user


def test_create_product_with_variants_and_inventory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        db.add_all([
            Branch(name="Sucursal A", address="Calle A"),
            Branch(name="Sucursal B", address="Calle B"),
        ])
        db.commit()

        payload = ProductCreate(
            name="Camisa",
            description="Prueba",
            base_price=120.0,
            season="2026",
            variants=[
                {"size": "M", "color": "Blanco", "quantity": 5, "sku": "CAM-M-BLANCO"},
                {"size": "L", "color": "Negro", "quantity": 3, "sku": "CAM-L-NEGRO"},
            ],
        )

        created = create_product(db=db, product_in=payload, current_user=admin(db))

        assert created.id is not None
        assert db.query(ProductVariant).count() == 2
        assert db.query(Inventory).count() == 4
        assert db.query(Product).filter(Product.name == "Camisa").count() == 1


def test_update_product_variant_quantity_and_size():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        db.add_all([
            Branch(name="Sucursal A", address="Calle A"),
            Branch(name="Sucursal B", address="Calle B"),
        ])
        db.commit()

        payload = ProductCreate(
            name="Pantalón",
            description="Prueba",
            base_price=200.0,
            season="2026",
            variants=[
                {"size": "M", "color": "Azul", "quantity": 4, "sku": "PAN-M-AZUL"},
            ],
        )

        created = create_product(db=db, product_in=payload, current_user=admin(db))
        variant = db.query(ProductVariant).filter(ProductVariant.product_id == created.id).one()

        updated = update_product(
            db=db,
            product_id=created.id,
            product_in=ProductUpdate(
                name="Pantalón",
                base_price=220.0,
                variants=[
                    {
                        "id": variant.id,
                        "size": "XL",
                        "color": "Azul",
                        "quantity": 9,
                        "sku": "PAN-XL-AZUL",
                    }
                ],
            ),
            current_user=admin(db),
        )

        assert updated.id == created.id
        variant_after = db.query(ProductVariant).filter(ProductVariant.id == variant.id).one()
        assert variant_after.size == "XL"
        assert variant_after.quantity == 18
        assert db.query(Inventory).filter(Inventory.variant_id == variant.id).first().stock == 9

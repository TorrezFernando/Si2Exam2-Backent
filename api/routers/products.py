from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_

from api.deps import get_db, require_permission, get_current_active_user, get_current_user_optional, get_user_permissions
from db.models.branch import Branch, Inventory
from db.models.product import Product, Category, ProductVariant
from db.models.user import User
from db.models.recommendation import UserPreference
import json
from schemas.product import (
    Product as ProductSchema,
    ProductCreate,
    ProductUpdate,
    Category as CategorySchema
)
from core.ai_service import generate_recommendations
from core.commerce import check_branch
from db.models.operations import Supplier, Season, Collection, InventoryMovement

from core.storefront import selected_store, storefront_metadata

router = APIRouter()

@router.get('/storefront')
def get_storefront(store: Optional[str] = None, db: Session = Depends(get_db),
                   current_user: Optional[User] = Depends(get_current_user_optional), request: Request = None):
    return storefront_metadata(db, current_user, store, request)


# --- CATEGORIES ---

@router.get("/categories", response_model=List[CategorySchema])
def list_categories(
    db: Session = Depends(get_db),
    store: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    request: Request = None
) -> Any:
    """Lista todas las categorías disponibles."""
    tenant = selected_store(db, current_user, store, request)
    if not tenant: return []
    return db.query(Category).filter(or_(Category.tenant_id == tenant.id, Category.tenant_id.is_(None))).all()

# --- PRODUCTS ---

@router.get("", response_model=List[ProductSchema])
def list_products(
    db: Session = Depends(get_db),
    category_id: Optional[int] = Query(None, description="Filtrar por categoría"),
    season: Optional[str] = Query(None, description="Filtrar por temporada"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    store: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    request: Request = None
) -> Any:
    """Lista productos (Catálogo). Accesible públicamente o requiere auth según diseño."""
    tenant = selected_store(db, current_user, store, request)
    if not tenant: return []
    query = db.query(Product).filter(Product.is_active.is_(True), Product.tenant_id == tenant.id)
    
    if category_id is not None:
        query = query.filter(Product.category_id == category_id)
    if season:
        query = query.filter(Product.season == season)
        
    return query.offset(skip).limit(limit).all()


@router.get("/search", response_model=List[ProductSchema])
def search_products(
    q: str = Query(..., min_length=1, description="Texto a buscar en nombre o descripción"),
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    store: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    request: Request = None
) -> Any:
    """Busca productos por nombre o descripción (búsqueda full-text simple)."""
    tenant = selected_store(db, current_user, store, request)
    if not tenant: return []
    pattern = f"%{q}%"
    results = (
        db.query(Product)
        .filter(Product.is_active.is_(True), Product.tenant_id == tenant.id)
        .filter(
            or_(
                Product.name.ilike(pattern),
                Product.description.ilike(pattern),
            )
        )
        .offset(skip)
        .limit(limit)
        .all()
    )
    return results

@router.get("/recommended", response_model=List[ProductSchema])
async def get_recommended_products(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    limit: int = Query(10, ge=1, le=50)
) -> Any:
    """Devuelve productos recomendados basados en las interacciones guardadas del usuario."""
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    
    # Obtener todos los productos (idealmente solo los activos o en stock)
    tenant = selected_store(db, current_user)
    if not tenant: return []
    all_products = db.query(Product).filter(Product.is_active.is_(True), Product.tenant_id == tenant.id).all()
    if not all_products:
        return []
        
    try:
        prefs_dict = json.loads(pref.preferences_json) if (pref and pref.preferences_json) else {}
        cat_prefs = prefs_dict.get("categories", {})
        viewed_products = prefs_dict.get("viewed_products", [])
    except json.JSONDecodeError:
        cat_prefs = {}
        viewed_products = []
        
    if not viewed_products and not cat_prefs:
        # Si no hay preferencias, devolver los primeros
        return all_products[:limit]
        
    # Preparar el catálogo para Gemini
    available_catalog = [
        {"id": p.id, "name": p.name, "category": p.category.name if p.category else "Otros"}
        for p in all_products
    ]
    
    # Consultar a Gemini
    recommended_ids = await generate_recommendations(
        viewed_products=viewed_products,
        category_prefs=cat_prefs,
        available_products=available_catalog,
        limit=limit
    )
    
    # Fallback si Gemini falla o no devuelve suficientes
    if not recommended_ids:
        def score_product(p: Product):
            score = 0
            cat_name = p.category.name if p.category else ""
            if cat_name in cat_prefs:
                score += cat_prefs[cat_name]
            return score
        return sorted(all_products, key=score_product, reverse=True)[:limit]
        
    # Ordenar los productos devueltos según el orden de recommended_ids
    product_map = {p.id: p for p in all_products}
    result = []
    for rid in recommended_ids:
        if rid in product_map:
            result.append(product_map[rid])
            
    # Rellenar si faltan
    if len(result) < limit:
        for p in all_products:
            if p.id not in recommended_ids:
                result.append(p)
            if len(result) >= limit:
                break
                
    return result


@router.post("/{product_id}/interact")
def interact_with_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
) -> Any:
    """Registra una interacción (vista) con un producto para mejorar las recomendaciones."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product or not product.is_active:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
        
    cat_name = product.category.name if product.category else "Otros"
    
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if not pref:
        pref = UserPreference(user_id=current_user.id, preferences_json="{}")
        db.add(pref)
        db.flush()
        
    try:
        prefs_dict = json.loads(pref.preferences_json)
    except json.JSONDecodeError:
        prefs_dict = {"categories": {}, "viewed_products": []}
        
    if "categories" not in prefs_dict:
        prefs_dict["categories"] = {}
    if "viewed_products" not in prefs_dict:
        prefs_dict["viewed_products"] = []
        
    # Incrementar score de la categoría
    prefs_dict["categories"][cat_name] = prefs_dict["categories"].get(cat_name, 0) + 1
    
    # Añadir producto visto (manteniendo un máximo de 10)
    product_desc = f"{product.name} ({cat_name})"
    if product_desc in prefs_dict["viewed_products"]:
        prefs_dict["viewed_products"].remove(product_desc)
    prefs_dict["viewed_products"].insert(0, product_desc)
    prefs_dict["viewed_products"] = prefs_dict["viewed_products"][:10]
    
    pref.preferences_json = json.dumps(prefs_dict)
    db.commit()
    return {"status": "ok"}


@router.get("/{product_id}", response_model=ProductSchema)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    store: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    request: Request = None
) -> Any:
    """Obtiene un producto por su ID."""
    tenant = selected_store(db, current_user, store, request)
    if not tenant: raise HTTPException(404, 'Selecciona una tienda.')
    product = db.query(Product).filter(Product.id == product_id, Product.tenant_id == tenant.id).first()
    if not product or not product.is_active:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return product

def generate_unique_sku(db: Session, base_name: str, size: str, color: str, requested_sku: Optional[str] = None) -> str:
    if requested_sku and requested_sku.strip():
        clean_req = requested_sku.strip()
        existing = db.query(ProductVariant).filter(ProductVariant.sku == clean_req).first()
        if not existing:
            return clean_req
    
    clean_name = "".join(c for c in base_name.upper() if c.isalnum() or c == ' ').replace(' ', '-')[:8] or "PROD"
    clean_size = "".join(c for c in size.upper() if c.isalnum()) or "UNI"
    clean_color = "".join(c for c in color.upper() if c.isalnum()) or "UNI"
    base_sku = f"{clean_name}-{clean_size}-{clean_color}"
    
    candidate = base_sku
    counter = 1
    while db.query(ProductVariant).filter(ProductVariant.sku == candidate).first():
        candidate = f"{base_sku}-{counter}"
        counter += 1
    return candidate

@router.post("", response_model=ProductSchema, status_code=status.HTTP_201_CREATED)
def create_product(
    *,
    db: Session = Depends(get_db),
    product_in: ProductCreate,
    current_user: User = Depends(require_permission("catalog:write", "inventory:write"))
) -> Any:
    """Crea un nuevo producto (Admin/Encargado)."""
    if product_in.category_id:
        category = db.query(Category).filter(Category.id == product_in.category_id).first()
        if not category:
            raise HTTPException(status_code=404, detail="Categoría no encontrada")

    validate_references(db, product_in)
    product = Product(
        name=product_in.name,
        description=product_in.description,
        base_price=product_in.base_price,
        category_id=product_in.category_id,
        season=product_in.season,
        image_url=product_in.image_url,
        garment_image_url=product_in.garment_image_url, garment_type=product_in.garment_type,
        supplier_id=product_in.supplier_id, season_id=product_in.season_id, collection_id=product_in.collection_id
    )
    db.add(product)
    db.flush()

    # Restrict stock writes using effective permissions and assigned branch.
    target_branch_id = None
    if "branches:write" not in get_user_permissions(db, current_user):
        check_branch(db, current_user, current_user.branch_id)
        target_branch_id = current_user.branch_id
    elif product_in.branch_id:
        target_branch_id = product_in.branch_id

    active_branches = db.query(Branch).filter(Branch.is_active == True).all()
    if not active_branches:
        active_branches = db.query(Branch).all()

    for variant_data in product_in.variants or []:
        normalized_size = (variant_data.size or "").strip() or "M"
        normalized_color = (variant_data.color or "").strip() or "Único"

        generated_sku = generate_unique_sku(db, product.name, normalized_size, normalized_color, variant_data.sku)
        variant = ProductVariant(
            product_id=product.id,
            size=normalized_size,
            color=normalized_color,
            sku=generated_sku,
            price_override=variant_data.price_override,
            image_url=variant_data.image_url or product_in.image_url,
        )
        db.add(variant)
        db.flush()

        variant_branch_id = variant_data.branch_id or target_branch_id
        quantity = max(int(variant_data.quantity or 0), 0)
        inv_map = {inv.branch_id: max(int(inv.stock or 0), 0) for inv in (variant_data.inventories or [])}

        for branch in active_branches:
            if "branches:write" not in get_user_permissions(db, current_user) and branch.id != current_user.branch_id:
                stock = 0
            elif branch.id in inv_map:
                stock = inv_map[branch.id]
            elif variant_branch_id is not None:
                stock = quantity if branch.id == variant_branch_id else 0
            else:
                stock = quantity
            db.add(Inventory(branch_id=branch.id, variant_id=variant.id, stock=stock))
            if stock:
                db.add(InventoryMovement(branch_id=branch.id, variant_id=variant.id, quantity=stock, kind="receipt", user_id=current_user.id, reference=f"product:{product.id}"))

    db.commit()
    db.refresh(product)
    return product

@router.patch("/{product_id}", response_model=ProductSchema)
@router.put("/{product_id}", response_model=ProductSchema)
def update_product(
    *,
    db: Session = Depends(get_db),
    product_id: int,
    product_in: ProductUpdate,
    current_user: User = Depends(require_permission("catalog:write", "inventory:write"))
) -> Any:
    """Actualiza un producto (Admin/Encargado)."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product or not product.is_active:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    if product_in.category_id is not None:
        category = db.query(Category).filter(Category.id == product_in.category_id).first()
        if not category:
            raise HTTPException(status_code=404, detail="Categoría no encontrada")

    validate_references(db, product_in)
    update_data = product_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field in ("variants", "branch_id"):
            continue
        setattr(product, field, value)

    is_encargado = "branches:write" not in get_user_permissions(db, current_user)
    if is_encargado:
        check_branch(db, current_user, current_user.branch_id)
    encargado_branch_id = current_user.branch_id if is_encargado else None

    if product_in.variants is not None:
        active_branches = db.query(Branch).filter(Branch.is_active == True).all()
        if not active_branches:
            active_branches = db.query(Branch).all()

        for variant_data in product_in.variants:
            normalized_size = (variant_data.size or "").strip() or "M"
            normalized_color = (variant_data.color or "").strip() or "Único"

            variant = None
            if variant_data.id is not None:
                variant = db.query(ProductVariant).filter(ProductVariant.id == variant_data.id, ProductVariant.product_id == product.id).first()
                if variant is None:
                    raise HTTPException(400, "La variante no pertenece al producto")
            if variant is None:
                variant = (
                    db.query(ProductVariant)
                    .filter(
                        ProductVariant.product_id == product.id,
                        ProductVariant.size == normalized_size,
                        ProductVariant.color == normalized_color,
                    )
                    .first()
                )

            if variant is None:
                generated_sku = generate_unique_sku(db, product.name, normalized_size, normalized_color, variant_data.sku)
                variant = ProductVariant(
                    product_id=product.id,
                    size=normalized_size,
                    color=normalized_color,
                    sku=generated_sku,
                    price_override=variant_data.price_override,
                    image_url=variant_data.image_url or product.image_url,
                )
                db.add(variant)
                db.flush()
            else:
                variant.size = normalized_size
                variant.color = normalized_color
                if variant_data.sku and variant_data.sku.strip() != variant.sku:
                    variant.sku = generate_unique_sku(db, product.name, normalized_size, normalized_color, variant_data.sku)
                variant.price_override = variant_data.price_override
                variant.image_url = variant_data.image_url or product.image_url

            variant_branch_id = variant_data.branch_id or (current_user.branch_id if not is_encargado else None)
            qty = max(int(variant_data.quantity or 0), 0)
            inv_map = {inv.branch_id: max(int(inv.stock or 0), 0) for inv in (variant_data.inventories or [])}

            for branch in active_branches:
                inventory = (
                    db.query(Inventory)
                    .filter(Inventory.branch_id == branch.id, Inventory.variant_id == variant.id)
                    .with_for_update().first()
                )
                if is_encargado:
                    # Encargado: only update their own branch, leave others untouched
                    if branch.id == encargado_branch_id:
                        stock_value = inv_map.get(branch.id, qty)
                        if inventory:
                            record_adjustment(db, inventory, stock_value, current_user.id)
                        else:
                            db.add(Inventory(branch_id=branch.id, variant_id=variant.id, stock=stock_value))
                            db.add(InventoryMovement(branch_id=branch.id, variant_id=variant.id, quantity=stock_value, kind="receipt", user_id=current_user.id, reference=f"product:{product.id}"))
                else:
                    # Admin: update all branches from inv_map (per-branch values)
                    if branch.id in inv_map:
                        stock_value = inv_map[branch.id]
                    elif variant_branch_id is not None:
                        stock_value = qty if branch.id == variant_branch_id else (inventory.stock if inventory else 0)
                    else:
                        stock_value = qty

                    if inventory:
                        record_adjustment(db, inventory, stock_value, current_user.id)
                    else:
                        db.add(Inventory(branch_id=branch.id, variant_id=variant.id, stock=stock_value))
                        db.add(InventoryMovement(branch_id=branch.id, variant_id=variant.id, quantity=stock_value, kind="receipt", user_id=current_user.id, reference=f"product:{product.id}"))

    db.commit()
    db.refresh(product)
    return product

@router.delete("/{product_id}", response_model=ProductSchema)
def delete_product(
    *,
    db: Session = Depends(get_db),
    product_id: int,
    current_user: User = Depends(require_permission("catalog:write", "inventory:write"))
) -> Any:
    """Elimina un producto (Admin/Encargado)."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product or not product.is_active:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
        
    # Preserve purchased/reserved lines and their variant relationships.
    product.is_active = False
    db.commit()
    db.refresh(product)
    return product


def validate_references(db, payload):
    for field, model in [('supplier_id', Supplier), ('season_id', Season), ('collection_id', Collection)]:
        value = getattr(payload, field, None)
        if value is not None and not db.get(model, value):
            raise HTTPException(400, f'{field} no existe')


def record_adjustment(db, inventory, stock, user_id):
    delta = stock - inventory.stock
    if delta:
        db.add(InventoryMovement(branch_id=inventory.branch_id, variant_id=inventory.variant_id,
            quantity=delta, kind='adjustment', user_id=user_id, reference='catalog update'))
        inventory.stock = stock

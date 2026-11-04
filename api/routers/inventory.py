from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List

from db.database import get_db
from db.models.branch import Inventory
from schemas.product import InventorySchema
from api.deps import get_current_user_optional
from core.storefront import selected_store

router = APIRouter()

@router.get("/variant/{variant_id}", response_model=List[InventorySchema])
def get_inventory_by_variant(variant_id: int, request: Request, store: str | None = None,
    db: Session = Depends(get_db), user=Depends(get_current_user_optional)):
    """
    Obtener el inventario (stock en sucursales) de una variante específica.
    """
    tenant = selected_store(db, user, store, request)
    if tenant is None:
        raise HTTPException(403, 'Selecciona una tienda para consultar disponibilidad.')
    inventories = db.query(Inventory).filter(Inventory.tenant_id == tenant.id, Inventory.variant_id == variant_id).all()
    # Si no hay inventario (0 o vacío) se devuelve lista vacía, no 404, para que la UI lo maneje.
    return inventories

@router.get("/branch/{branch_id}", response_model=List[InventorySchema])
def get_inventory_by_branch(branch_id: int, request: Request, store: str | None = None,
    db: Session = Depends(get_db), user=Depends(get_current_user_optional)):
    """
    Obtener todo el inventario de una sucursal.
    """
    tenant = selected_store(db, user, store, request)
    if tenant is None:
        raise HTTPException(403, 'Selecciona una tienda para consultar disponibilidad.')
    inventories = db.query(Inventory).filter(Inventory.tenant_id == tenant.id, Inventory.branch_id == branch_id).all()
    return inventories

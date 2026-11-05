"""
FashionStore — Virtual Try-On Router
=====================================
POST /api/v1/ai/tryon
Recibe foto del usuario + product_id, llama a Gemini 2.5 Flash
y retorna la URL de la imagen generada.
"""
import asyncio
from pathlib import Path
import time
import httpx
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.deps import get_db, get_current_active_user
from core.ai_service import generate_tryon, save_tryon_result
from core.config import settings
from db.models.product import Product, ProductVariant
from db.models.user import User
from core.access import company_id

router = APIRouter()

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE_MB = 10


@router.post("", summary="Virtual Try-On con Gemini IA")
async def virtual_tryon(
    variant_id: int = Form(..., gt=0, description="Variante con talla y color seleccionados"),
    product_id: int = Form(..., description="ID del producto a probar"),
    user_photo: UploadFile = File(..., description="Foto del usuario (JPG/PNG, max 10MB)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Genera una imagen del usuario vistiendo la prenda seleccionada usando Gemini 2.5 Flash.

    - **product_id**: ID del producto en el catálogo
    - **user_photo**: Foto del usuario (cuerpo completo o medio cuerpo funciona mejor)

    Retorna la URL de la imagen generada.
    """
    if not settings.AI_TRYON_ENABLED:
        raise HTTPException(status_code=503, detail="El servicio de Try-On está deshabilitado.")

    # ── Validar foto del usuario ────────────────────────────────────────────────
    if user_photo.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado: {user_photo.content_type}. Usa JPG, PNG o WebP."
        )

    user_photo_bytes = await user_photo.read(MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
    if len(user_photo_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"La foto supera el límite de {MAX_FILE_SIZE_MB}MB.")

    # ── Obtener producto ────────────────────────────────────────────────────────
    product = db.query(Product).filter(Product.id == product_id, Product.tenant_id == company_id(current_user), Product.is_active.is_(True)).first()
    if not product:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")

    variant = db.query(ProductVariant).filter_by(id=variant_id, product_id=product.id, tenant_id=product.tenant_id).first()
    if not variant:
        raise HTTPException(404, 'Talla o variante no disponible para este producto.')
    # Prefer the exact variant reference for color and garment identity.
    # Usar garment_image_url o fallback a image_url
    garment_url = variant.image_url or product.garment_image_url or product.image_url
    if not garment_url:
        raise HTTPException(
            status_code=400,
            detail="Este producto no tiene imagen disponible para el Try-On."
        )

    # ── Descargar imagen de la prenda ───────────────────────────────────────────
    try:
        if garment_url.startswith("data:"):
            # Base64-encoded image (data:image/jpeg;base64,...)
            import base64
            header, _, b64data = garment_url.partition(",")
            if not b64data:
                raise ValueError("URL base64 inválida: no se encontró datos después de la coma.")
            garment_bytes = base64.b64decode(b64data)
        elif garment_url.startswith("/uploads/") or garment_url.startswith("uploads/"):
            # Ruta local relativa al servidor
            local_path = Path(garment_url.lstrip('/')).resolve()
            if not local_path.is_relative_to(Path('uploads').resolve()):
                raise ValueError('Invalid upload path')
            with open(local_path, "rb") as f:
                garment_bytes = f.read(MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(garment_url)
                resp.raise_for_status()
                garment_bytes = resp.content
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="No se pudo obtener la imagen de esta variante. Revisa su imagen en el catálogo."
        )

    from core.ai_service import _to_png_bytes
    try:
        if len(garment_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValueError('La imagen de la prenda supera 10 MB.')
        user_photo_bytes = _to_png_bytes(user_photo_bytes)
        garment_bytes = _to_png_bytes(garment_bytes)
    except Exception:
        raise HTTPException(400, 'La foto o imagen de prenda no es válida. Usa JPG, PNG o WebP de hasta 10 MB y 16 megapíxeles.')
    # ── Llamar a Gemini ─────────────────────────────────────────────────────────
    start_ms = time.time()
    try:
        result_bytes = await asyncio.wait_for(generate_tryon(
            user_photo_bytes=user_photo_bytes,
            garment_image_bytes=garment_bytes,
            garment_type=product.garment_type or "upper_body",
            product_name=product.name, selected_size=variant.size, selected_color=variant.color,
        ), timeout=150)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail="El proveedor de imágenes no está disponible o tardó demasiado. Reintenta en unos minutos."
        )

    processing_ms = round((time.time() - start_ms) * 1000)

    # ── Guardar resultado ───────────────────────────────────────────────────────
    result_url = save_tryon_result(result_bytes, current_user.id)

    return JSONResponse({
        "result_url": result_url,
        "product_name": product.name,
        "variant_id": variant.id, "selected_size": variant.size, "selected_color": variant.color,
        "fit_notice": "Visualización orientativa. La talla no garantiza el ajuste sin medidas corporales y de la prenda.",
        "garment_type": product.garment_type or "upper_body",
        "processing_ms": processing_ms,
    })

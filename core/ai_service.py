"""
FashionStore AI Service — Virtual Try-On con Gemini + Fallback Pollinations.ai
================================================================================
Usa el SDK google-genai para Gemini.
Fallback: Pollinations.ai (gratuito, sin API key) para visualización de moda.
Recibe foto del usuario + foto de prenda → retorna bytes de imagen generada.
"""
import asyncio
import base64
import uuid
import urllib.request
import urllib.parse
from pathlib import Path
from io import BytesIO

from google import genai
from google.genai import types
from PIL import Image, ImageOps

from core.config import settings


def _get_api_keys() -> list[str]:
    keys = []
    if getattr(settings, 'GEMINI_API_KEY_1', None): keys.append(settings.GEMINI_API_KEY_1)
    if getattr(settings, 'GEMINI_API_KEY_2', None): keys.append(settings.GEMINI_API_KEY_2)
    if getattr(settings, 'GEMINI_API_KEY_3', None): keys.append(settings.GEMINI_API_KEY_3)
    if getattr(settings, 'GEMINI_API_KEY_4', None): keys.append(settings.GEMINI_API_KEY_4)
    return keys

def _resize_if_needed(img: Image.Image, max_size: int = 512) -> Image.Image:
    """Redimensiona si la imagen es muy grande (ahorra tokens Gemini y reduce costos)."""
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    return img

def _to_png_bytes(image_bytes: bytes) -> bytes:
    """Convierte cualquier formato de imagen a PNG para Gemini y corrige la rotación EXIF."""
    img = Image.open(BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = _resize_if_needed(img)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

async def generate_tryon(
    user_photo_bytes: bytes,
    garment_image_bytes: bytes,
    garment_type: str = "upper_body",
    product_name: str = "la prenda",
) -> bytes:
    user_png = _to_png_bytes(user_photo_bytes)
    garment_png = _to_png_bytes(garment_image_bytes)

    ACCESSORY_TYPES = {"accessories", "eyewear", "sunglasses", "bags", "bag", "handbag", "jewelry", "hat", "caps"}
    is_accessory = garment_type.lower() in ACCESSORY_TYPES

    garment_labels = {
        "upper_body": "upper body garment (shirt, blouse, jacket, sweater, top)",
        "lower_body": "lower body garment (pants, skirt, shorts, jeans)",
        "dresses": "dress or full-body garment",
        "full_outfit": "complete outfit",
        "accessories": "fashion accessory",
        "sunglasses": "sunglasses accessory",
        "bags": "handbag or bag",
    }
    garment_label = garment_labels.get(garment_type.lower(), "fashion item")

    if is_accessory:
        prompt = f"""You are a professional fashion photographer and digital stylist.
I will provide two images:
1. A photo of a person
2. A {garment_label} called "{product_name}"
Create a high-quality fashion editorial photograph showing the person styled with the accessory.
Requirements: Maintain the person's appearance, skin tone, and general pose. Output only the final composed image."""
    else:
        prompt = f"""You are a virtual fitting room AI assistant.
I will provide two images:
1. A photo of a person
2. A {garment_label} called "{product_name}"
Generate a single photorealistic image of the person wearing the garment.
Rules: Preserve the person's face, hair, body shape exactly. Only change the clothing. Output only the image."""

    api_keys = _get_api_keys()
    gemini_errors = []

    if not api_keys:
        raise ValueError("No hay API Keys de Gemini configuradas.")

    # Intentar generación de imagen iterando por las API keys
    for key in api_keys:
        try:
            client = genai.Client(api_key=key)
            response = await asyncio.to_thread(client.models.generate_content,
                model=settings.GEMINI_MODEL,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=user_png, mime_type="image/png"),
                    types.Part.from_bytes(data=garment_png, mime_type="image/png"),
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                    temperature=0.4,
                ),
            )
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
            gemini_errors.append(f"Key {key[-4:]}: Respondió solo texto.")
        except Exception as e:
            gemini_errors.append(f"Key {key[-4:]}: {str(e)[:150]}")

    raise ValueError(f"Fallo en todas las API Keys de Gemini: {gemini_errors}")

async def generate_recommendations(
    viewed_products: list[str],
    category_prefs: dict,
    available_products: list[dict],
    limit: int = 4
) -> list[int]:
    """
    Pide a Gemini que recomiende productos basándose en el historial.
    Devuelve una lista de IDs de productos recomendados.
    """
    prompt = f"""You are an expert fashion personal shopper AI.
The user has recently viewed the following products: {viewed_products}
Their category engagement is: {category_prefs}

Here is the catalog of available products (JSON list):
{available_products}

Based on their viewed products and style, recommend up to {limit} product IDs that they would most likely buy.
Consider complementary items (e.g. if they viewed a shirt, recommend matching pants) or similar styles.
Return ONLY a valid JSON array of integer IDs. Example: [1, 5, 12, 3]"""

    api_keys = _get_api_keys()
    gemini_errors = []

    if not api_keys:
        return []

    for key in api_keys:
        try:
            client = genai.Client(api_key=key)
            response = await asyncio.to_thread(client.models.generate_content,
                model=settings.GEMINI_TEXT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.4,
                ),
            )
            # Intentar parsear la respuesta como JSON
            import json
            text = response.text
            if not text:
                continue
                
            try:
                # Extraer JSON si está envuelto en backticks
                if text.startswith("```json"):
                    text = text.replace("```json", "").replace("```", "").strip()
                elif text.startswith("```"):
                    text = text.replace("```", "").strip()
                    
                recommended_ids = json.loads(text)
                if isinstance(recommended_ids, list):
                    return [int(id) for id in recommended_ids if isinstance(id, (int, str)) and str(id).isdigit()][:limit]
            except Exception:
                pass
                
        except Exception as e:
            gemini_errors.append(str(e))

    return []


def save_tryon_result(image_bytes: bytes, user_id: int) -> str:
    """
    Guarda la imagen generada en /uploads/tryon/{user_id}/
    y retorna la URL pública relativa.
    """
    upload_dir = Path(settings.TRYON_UPLOAD_DIR) / str(user_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}.png"
    filepath = upload_dir / filename
    filepath.write_bytes(image_bytes)

    return f"/uploads/tryon/{user_id}/{filename}"

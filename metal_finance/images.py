"""Zapis zdjęć paragonów: obrót wg EXIF, konwersja (także HEIC z iPhone'a) do JPEG, zmniejszenie."""
from __future__ import annotations

import os
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # zdjęcia HEIC/HEIF z iPhone'a
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # pragma: no cover
    pass

MAX_SIDE = 2400


def save_image(file_storage, upload_dir: str) -> str:
    """Zapisuje przesłany plik jako JPEG i zwraca nazwę pliku. ValueError gdy to nie obraz."""
    try:
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Plik „{file_storage.filename}” nie jest obsługiwanym obrazem.") from exc
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    name = f"{uuid.uuid4().hex}.jpg"
    os.makedirs(upload_dir, exist_ok=True)
    img.save(os.path.join(upload_dir, name), "JPEG", quality=88, optimize=True)
    return name

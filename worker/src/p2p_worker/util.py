"""Small helpers: hashing, atomic writes, data URIs, image downscaling, GLB face counts."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import struct
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".glb": "model/gltf-binary",
    ".3mf": "model/3mf",
    ".json": "application/json",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode())


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def append_jsonl(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, sort_keys=True, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[Any]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line after a crash
    return out


def mime_for(path: Path | str) -> str:
    ext = Path(str(path)).suffix.lower()
    return MIME_BY_EXT.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def downscale_image(path: Path, max_px: int = 1536, fmt: str | None = None) -> tuple[bytes, str]:
    """Return (bytes, mime) with the long edge at most max_px. Keeps PNG alpha; photos go to JPEG."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.load()
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px))
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        out_fmt = fmt or ("PNG" if has_alpha or path.suffix.lower() == ".png" else "JPEG")
        buf = io.BytesIO()
        if out_fmt == "JPEG":
            im.convert("RGB").save(buf, "JPEG", quality=90)
            return buf.getvalue(), "image/jpeg"
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"


_SIGNED_STORAGE = re.compile(r"^https?://[^/]+/storage/v1/object/(?:sign|authenticated|public)/"
                             r"([^/?]+)/([^?]+)")


def manifest_safe(obj: Any) -> Any:
    """Copy fit to persist in a manifest: no data URIs, signed URLs or tokens.

    - data URI -> "data:<mime>;sha256=<16 hex>;len=<n>"
    - Supabase Storage URL -> "storage:<bucket>/<key>" (sign again at call time)
    - any other URL with a query string (CloudFront Signature/Expires, ?token=) -> the URL
      without its query, which identifies the file but cannot fetch it
    """
    if isinstance(obj, str):
        if obj.startswith("data:"):
            head, _, payload = obj.partition(",")
            return (f"{head.split(';')[0]};sha256={sha256_bytes(payload.encode())[:16]};"
                    f"len={len(payload)}")
        if obj.startswith(("http://", "https://")):
            m = _SIGNED_STORAGE.match(obj)
            if m:
                return f"storage:{m.group(1)}/{unquote(m.group(2))}"
            return obj.split("?", 1)[0]
        return obj
    if isinstance(obj, dict):
        return {k: manifest_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [manifest_safe(v) for v in obj]
    return obj


strip_data_uris = manifest_safe  # older name


def glb_face_count(path: Path) -> int | None:
    """Triangle count of a GLB from accessor metadata. None if it cannot be read."""
    try:
        data = path.read_bytes()
        magic, _version, _length = struct.unpack_from("<4sII", data, 0)
        if magic != b"glTF":
            return None
        clen, ctype = struct.unpack_from("<I4s", data, 12)
        if ctype != b"JSON":
            return None
        gltf = json.loads(data[20 : 20 + clen])
        total = 0
        for mesh in gltf.get("meshes", []):
            for prim in mesh.get("primitives", []):
                if prim.get("mode", 4) != 4:
                    continue
                if "indices" in prim:
                    total += gltf["accessors"][prim["indices"]]["count"] // 3
                else:
                    total += gltf["accessors"][prim["attributes"]["POSITION"]]["count"] // 3
        return total
    except Exception:  # noqa: BLE001
        return None


def ms_to_dt(ms: Any) -> datetime | None:
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return datetime.fromtimestamp(v / 1000, UTC)

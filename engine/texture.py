from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import numpy as np
import rasterio
import trimesh
from PIL import Image
from rasterio.transform import from_bounds

from blender_lidartool.engine.crsutil import to_3857, to_wgs
from blender_lidartool.engine.cuzk import (
    ORTHO_EXPORT,
    ORTHO_MAX_PX,
    ORTHO_TILE,
    choose_zoom,
    iter_mercator_tiles,
    iter_ortho_blocks,
    ortho_export_params,
    split_zone_face_groups,
    webmercator_resolution,
)
from blender_lidartool.engine.http import fetch_bytes, session
from blender_lidartool.engine.mesh import load_mesh, y_up_vertices


def download_ortho(bounds_5514, zoom: int, dest: Path, cache: Path, progress) -> dict:
    image, frame, used_zoom = render_ortho(bounds_5514, zoom, cache, progress)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_geotiff(dest, image, frame)
    return {
        "message": f"Ortofoto uloženo, zoom {used_zoom}, {image.size[0]}×{image.size[1]} px.",
        "zoom": used_zoom,
        "width": image.size[0],
        "height": image.size[1],
    }


def texture_mesh(folder: Path, zoom: int, progress) -> dict:
    mesh = load_mesh(folder)
    cache = folder.parent / "TEMP" / "ortho_cache"
    groups = _faces_by_zone(mesh)
    scene = trimesh.Scene()
    used = []
    total = len(groups)
    for index, (zone_id, faces) in enumerate(groups):
        progress(10 + 80 * index / max(total, 1), f"Texturuji zónu {zone_id}")
        zone_zoom = max(6, int(zoom) - (int(zone_id) - 1))
        subset = _zone_mesh(mesh, faces, zone_zoom, cache, progress)
        scene.add_geometry(subset, geom_name=f"zona_{zone_id}")
        used.append(zone_id)
    out = folder / "terrain_textured.glb"
    scene.export(out)
    return {
        "message": f"Texturovaný mesh uložen, zóny {', '.join(str(zone) for zone in used)}.",
        "files": ["terrain_textured.glb"],
    }


def render_ortho(bounds_5514, zoom: int, cache: Path, progress, max_px: int = ORTHO_MAX_PX, refresh: bool = False) -> tuple[Image.Image, dict, int]:
    minx, miny, maxx, maxy = _to_3857_bounds(bounds_5514)
    used_zoom = choose_zoom(minx, miny, maxx, maxy, int(zoom), max_px=max_px)
    resolution = webmercator_resolution(used_zoom)
    width = max(1, int(round((maxx - minx) / resolution)))
    height = max(1, int(round((maxy - miny) / resolution)))
    image = Image.new("RGB", (width, height), (30, 30, 30))
    blocks = iter_ortho_blocks(minx, miny, maxx, maxy, resolution)
    if not blocks:
        raise ValueError("Pro ortofoto nevznikl žádný výřez.")
    client = session()
    found = 0
    for index, block in enumerate(blocks, start=1):
        progress(
            5 + 90 * index / len(blocks),
            f"Ortofoto výřez {index}/{len(blocks)} ({block['width']}×{block['height']})",
        )
        piece = _block_image(client, cache, block, used_zoom, resolution, refresh)
        if piece is None:
            continue
        found += 1
        image.paste(piece, (int(block["px"]), int(block["py"])))
    if found == 0:
        raise ValueError("ČÚZK nevrátil ortofoto pro zvolený rozsah. Zkuste to znovu, služba občas zahazuje spojení.")
    frame = {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy, "zoom": used_zoom}
    return image, frame, used_zoom


def _block_image(client, cache: Path, block: dict, zoom: int, resolution: float, refresh: bool = False) -> Image.Image | None:
    piece = _export_image(client, cache, block, zoom, refresh)
    if piece is not None:
        return piece
    if max(int(block["width"]), int(block["height"])) > 2048:
        canvas = Image.new("RGB", (int(block["width"]), int(block["height"])), (30, 30, 30))
        pasted = 0
        for part in _pixel_subblocks(block, 2048, resolution):
            image = _export_image(client, cache, part, zoom, refresh)
            if image is None:
                image = _tiles_for_block(client, cache, part, zoom, resolution, refresh)
            if image is None:
                continue
            canvas.paste(image, (int(part["px"]), int(part["py"])))
            pasted += 1
        return canvas if pasted else None
    return _tiles_for_block(client, cache, block, zoom, resolution, refresh)


def _pixel_subblocks(block: dict, max_side: int, resolution: float) -> list[dict]:
    parts = []
    height = int(block["height"])
    width = int(block["width"])
    max_side = max(256, int(max_side))
    for row in range(0, height, max_side):
        block_h = min(max_side, height - row)
        for col in range(0, width, max_side):
            block_w = min(max_side, width - col)
            left = float(block["minx"]) + col * resolution
            top = float(block["maxy"]) - row * resolution
            parts.append(
                {
                    "minx": left,
                    "maxx": left + block_w * resolution,
                    "maxy": top,
                    "miny": top - block_h * resolution,
                    "width": block_w,
                    "height": block_h,
                    "px": col,
                    "py": row,
                }
            )
    return parts


def _export_image(client, cache: Path, block: dict, zoom: int, refresh: bool = False) -> Image.Image | None:
    name = f"{block['minx']:.1f}_{block['miny']:.1f}_{block['width']}x{block['height']}.jpg"
    path = cache / "export" / str(zoom) / name
    if refresh and path.exists():
        path.unlink()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        params = ortho_export_params(block["minx"], block["miny"], block["maxx"], block["maxy"], block["width"], block["height"])
        try:
            content = fetch_bytes(client, ORTHO_EXPORT, params=params, timeout=(20, 180), attempts=4)
        except RuntimeError:
            return None
        if not _is_raster(content):
            return None
        path.write_bytes(content)
    try:
        image = Image.open(path).convert("RGB")
    except OSError:
        path.unlink(missing_ok=True)
        return None
    if image.size != (int(block["width"]), int(block["height"])):
        image = image.resize((int(block["width"]), int(block["height"])), Image.BILINEAR)
    return image


def _tiles_for_block(client, cache: Path, block: dict, zoom: int, resolution: float, refresh: bool = False) -> Image.Image | None:
    tiles = iter_mercator_tiles(block["minx"], block["miny"], block["maxx"], block["maxy"], zoom)
    if not tiles:
        return None
    canvas = Image.new("RGB", (int(block["width"]), int(block["height"])), (30, 30, 30))
    found = 0
    for tile in tiles:
        tile_image = _tile_image(client, cache, tile, refresh)
        if tile_image is None:
            continue
        found += 1
        px = int(round((tile["minx"] - block["minx"]) / resolution))
        py = int(round((block["maxy"] - tile["maxy"]) / resolution))
        canvas.paste(tile_image, (px, py))
    return canvas if found else None


def _is_raster(content: bytes) -> bool:
    return bool(content) and (content[:2] in (b"\xff\xd8", b"\x89P") or content[:4] == b"\x89PNG")


def _tile_image(client, cache: Path, tile: dict, refresh: bool = False) -> Image.Image | None:
    path = cache / str(tile["z"]) / str(tile["row"]) / f"{tile['col']}.jpg"
    if refresh and path.exists():
        path.unlink()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = ORTHO_TILE.format(z=tile["z"], row=tile["row"], col=tile["col"])
        try:
            content = fetch_bytes(client, url, timeout=(15, 90), attempts=4)
        except RuntimeError:
            return None
        if not _is_raster(content):
            return None
        path.write_bytes(content)
    try:
        return Image.open(path).convert("RGB")
    except OSError:
        return None


def _faces_by_zone(mesh: dict) -> list[tuple[int, np.ndarray]]:
    zones = mesh["zones"]
    faces = mesh["faces"]
    face_zone = np.minimum(np.minimum(zones[faces[:, 0]], zones[faces[:, 1]]), zones[faces[:, 2]])
    groups = []
    for zone_id in sorted(set(face_zone.tolist())):
        groups.append((int(zone_id), faces[face_zone == zone_id]))
    return groups


def uv_in_frame(xs, ys, frame: dict) -> np.ndarray:
    xs = np.atleast_1d(np.asarray(xs, dtype=np.float64))
    ys = np.atleast_1d(np.asarray(ys, dtype=np.float64))
    span_x = float(frame["maxx"] - frame["minx"]) or 1.0
    span_y = float(frame["maxy"] - frame["miny"]) or 1.0
    return np.column_stack([(xs - frame["minx"]) / span_x, (ys - frame["miny"]) / span_y]).astype(np.float32)


def zone_texture_layers(mesh: dict, zoom: int, cache: Path, image_dir: Path, progress, refresh: bool = False) -> list[dict]:
    zones = mesh["zones"]
    all_faces = mesh["faces"]
    face_zone = np.minimum(np.minimum(zones[all_faces[:, 0]], zones[all_faces[:, 1]]), zones[all_faces[:, 2]])
    zone_ids = sorted(set(face_zone.tolist()))
    image_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for zone_id in zone_ids:
        rows = np.flatnonzero(face_zone == zone_id).astype(np.int32)
        faces = all_faces[rows]
        zone_zoom = max(6, int(zoom) - (int(zone_id) - 1))
        jobs.extend(split_zone_face_groups(mesh["vertices_abs"], faces, rows, int(zone_id), zone_zoom))
    layers = []
    total = max(len(jobs), 1)
    for index, job in enumerate(jobs):
        zone_id = int(job["zone_id"])
        start = 10 + 80 * index / total
        end = 10 + 80 * (index + 1) / total

        def nested(pct, message, start=start, end=end, zone_id=zone_id, index=index, total=total):
            progress(start + (end - start) * float(pct) / 100.0, f"Zóna {zone_id} díl {index + 1}/{total} · {message}")

        nested(0, f"zoom {job['zoom']}")
        image, frame, used_zoom = render_ortho(job["bounds"], job["zoom"], cache, nested, refresh=refresh)
        path = image_dir / f"zone_{zone_id}_{index}.png"
        image.save(path, format="PNG")
        faces = job["faces"]
        absolute = mesh["vertices_abs"][faces]
        flat = absolute.reshape(-1, 2)
        xs, ys = to_3857(flat[:, 0], flat[:, 1])
        uv = np.clip(uv_in_frame(xs, ys, frame), 0.0, 1.0).reshape(len(faces), 3, 2)
        parts = int(job["parts"])
        part = int(job["part"])
        key = f"layer_{index}"
        layers.append(
            {
                "zone_id": zone_id,
                "faces": np.asarray(faces, dtype=np.int32),
                "rows": np.asarray(job["rows"], dtype=np.int32),
                "uv": uv.astype(np.float32),
                "image": str(path),
                "key": key,
                "name": f"teren_zona_{zone_id}_{part}" if parts > 1 else f"teren_zona_{zone_id}",
                "part": part,
                "parts": parts,
                "zoom": int(used_zoom),
            }
        )
        nested(100, f"zoom {used_zoom}, {image.size[0]}×{image.size[1]} px")
    return layers


def _zone_mesh(mesh: dict, faces: np.ndarray, zoom: int, cache: Path, progress) -> trimesh.Trimesh:
    used = np.unique(faces)
    remap = -np.ones(len(mesh["vertices"]), dtype=np.int32)
    remap[used] = np.arange(len(used))
    local_faces = remap[faces]
    vertices = y_up_vertices(mesh["vertices"][used])
    absolute = mesh["vertices_abs"][used]
    minx = float(absolute[:, 0].min())
    miny = float(absolute[:, 1].min())
    maxx = float(absolute[:, 0].max())
    maxy = float(absolute[:, 1].max())
    pad = 20.0
    image, frame, _zoom = render_ortho((minx - pad, miny - pad, maxx + pad, maxy + pad), zoom, cache, progress)
    image.format = "PNG"
    xs, ys = to_3857(absolute[:, 0], absolute[:, 1])
    uv = uv_in_frame(xs, ys, frame)
    material = trimesh.visual.material.PBRMaterial(
        doubleSided=True,
        baseColorTexture=image,
        metallicFactor=0.0,
        roughnessFactor=1.0,
    )
    visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
    return trimesh.Trimesh(vertices=vertices, faces=local_faces, visual=visual, process=False)


def _to_3857_bounds(bounds) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bounds
    corners_x = [minx, minx, maxx, maxx]
    corners_y = [miny, maxy, miny, maxy]
    xs, ys = to_3857(corners_x, corners_y)
    return float(np.min(xs)), float(np.min(ys)), float(np.max(xs)), float(np.max(ys))


def _write_geotiff(path: Path, image: Image.Image, frame: dict) -> None:
    array = np.asarray(image)
    height, width = array.shape[:2]
    transform = from_bounds(frame["minx"], frame["miny"], frame["maxx"], frame["maxy"], width, height)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 3,
        "dtype": "uint8",
        "crs": "EPSG:3857",
        "transform": transform,
        "compress": "lzw",
    }
    with rasterio.open(path, "w", **profile) as dataset:
        for band in range(3):
            dataset.write(array[:, :, band], band + 1)


def bounds_wgs_from_5514(bounds) -> list[list[float]]:
    minx, miny, maxx, maxy = bounds
    corners_x = [minx, maxx]
    corners_y = [miny, maxy]
    lons, lats = to_wgs(corners_x, corners_y)
    return [[float(np.min(lats)), float(np.min(lons))], [float(np.max(lats)), float(np.max(lons))]]


def write_preview_meta(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_preview_meta(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def empty_bytes() -> BytesIO:
    return BytesIO()

from __future__ import annotations

import numpy as np

from blender_lidartool.engine.crsutil import to_3857

SERVICE_BOUNDS = (-904703.6, -1227414.12, -431605.6, -935118.12)
DMR_URL = "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmr5g/ImageServer/exportImage"
DMP_URL = "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmp1g/ImageServer/exportImage"
DMPOK_URL = "https://ags.cuzk.gov.cz/arcgis2/rest/services/dmp/ImageServer/exportImage"
ORTHO_TILE = "https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO_WM/MapServer/tile/{z}/{row}/{col}"
ORTHO_EXPORT = "https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO_WM/MapServer/export"
ORTHO_EXPORT_MAX = 4096

ORIGIN_X = -20037508.342787001
ORIGIN_Y = 20037508.342787001
INITIAL_RESOLUTION = 156543.03392799999

ELEVATION_URLS = {"dmr5g": DMR_URL, "dmp1g": DMP_URL}


def export_image_params(minx: float, miny: float, maxx: float, maxy: float, width: int, height: int) -> dict:
    return {
        "bbox": f"{minx:.4f},{miny:.4f},{maxx:.4f},{maxy:.4f}",
        "bboxSR": "5514",
        "imageSR": "5514",
        "size": f"{int(width)},{int(height)}",
        "format": "tiff",
        "pixelType": "F32",
        "interpolation": "RSP_BilinearInterpolation",
        "adjustAspectRatio": "false",
        "compression": "LZ77",
        "renderingRule": '{"rasterFunction":"None"}',
        "f": "image",
    }


def webmercator_resolution(zoom: int) -> float:
    return INITIAL_RESOLUTION / (2 ** int(zoom))


def iter_mercator_tiles(minx: float, miny: float, maxx: float, maxy: float, zoom: int) -> list[dict]:
    resolution = webmercator_resolution(zoom)
    span = resolution * 256
    col0 = int(np.floor((minx - ORIGIN_X) / span))
    col1 = int(np.floor((max(minx, maxx - 1e-6) - ORIGIN_X) / span))
    row0 = int(np.floor((ORIGIN_Y - maxy) / span))
    row1 = int(np.floor((ORIGIN_Y - miny) / span))
    tiles = []
    for row in range(row0, row1 + 1):
        for col in range(col0, col1 + 1):
            x0 = ORIGIN_X + col * span
            y1 = ORIGIN_Y - row * span
            tiles.append(
                {
                    "z": int(zoom),
                    "row": int(row),
                    "col": int(col),
                    "minx": x0,
                    "miny": y1 - span,
                    "maxx": x0 + span,
                    "maxy": y1,
                }
            )
    return tiles


def ortho_export_params(minx: float, miny: float, maxx: float, maxy: float, width: int, height: int) -> dict:
    return {
        "bbox": f"{minx:.4f},{miny:.4f},{maxx:.4f},{maxy:.4f}",
        "bboxSR": "3857",
        "imageSR": "3857",
        "size": f"{int(width)},{int(height)}",
        "format": "jpg",
        "f": "image",
        "transparent": "false",
        "dpi": "96",
    }


def iter_ortho_blocks(minx: float, miny: float, maxx: float, maxy: float, resolution: float, max_side: int = ORTHO_EXPORT_MAX) -> list[dict]:
    if resolution <= 0:
        raise ValueError("Rozlišení ortofota musí být větší než nula.")
    width = max(1, int(round((maxx - minx) / resolution)))
    height = max(1, int(round((maxy - miny) / resolution)))
    max_side = max(256, int(max_side))
    blocks = []
    row = 0
    while row < height:
        block_h = min(max_side, height - row)
        col = 0
        while col < width:
            block_w = min(max_side, width - col)
            left = minx + col * resolution
            top = maxy - row * resolution
            blocks.append(
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
            col += block_w
        row += block_h
    return blocks


def choose_zoom(minx: float, miny: float, maxx: float, maxy: float, zoom: int, max_px: int = 4096) -> int:
    zoom = int(np.clip(zoom, 6, 20))
    while zoom > 6:
        resolution = webmercator_resolution(zoom)
        width = (maxx - minx) / resolution
        height = (maxy - miny) / resolution
        if width <= max_px and height <= max_px:
            return zoom
        zoom -= 1
    return 6


ORTHO_MAX_PX = 8192


def split_zone_face_groups(
    vertices_abs: np.ndarray,
    faces: np.ndarray,
    rows: np.ndarray,
    zone_id: int,
    zoom: int,
    max_px: int = ORTHO_MAX_PX,
    pad_m: float = 20.0,
) -> list[dict]:
    faces = np.asarray(faces, dtype=np.int32)
    rows = np.asarray(rows, dtype=np.int32)
    if len(faces) == 0:
        return []
    absolute = np.asarray(vertices_abs, dtype=np.float64)[faces]
    bounds = _padded_bounds(absolute, pad_m)
    if ortho_fits(bounds, zoom, max_px):
        return [_face_job(zone_id, faces, rows, bounds, zoom, 0, 1)]
    resolution = webmercator_resolution(int(np.clip(zoom, 6, 20)))
    cell_m = max(resolution * 256, float(max_px) * resolution * 0.82)
    centroids = absolute.mean(axis=1)
    xs, ys = to_3857(centroids[:, 0], centroids[:, 1])
    cols = np.floor(xs / cell_m).astype(np.int64)
    grid_rows = np.floor(ys / cell_m).astype(np.int64)
    keys = sorted(set(zip(cols.tolist(), grid_rows.tolist())))
    jobs = []
    for col, grid_row in keys:
        pick = (cols == col) & (grid_rows == grid_row)
        idx = np.flatnonzero(pick)
        sub_faces = faces[idx]
        sub_rows = rows[idx]
        sub_abs = np.asarray(vertices_abs, dtype=np.float64)[sub_faces]
        jobs.append(_face_job(zone_id, sub_faces, sub_rows, _padded_bounds(sub_abs, pad_m), zoom, len(jobs), 0))
    parts = max(len(jobs), 1)
    for job in jobs:
        job["parts"] = parts
    return jobs


def ortho_fits(bounds_5514, zoom: int, max_px: int) -> bool:
    minx, miny, maxx, maxy = mercator_bounds(bounds_5514)
    resolution = webmercator_resolution(int(np.clip(zoom, 6, 20)))
    width = (maxx - minx) / resolution
    height = (maxy - miny) / resolution
    return width <= max_px and height <= max_px


def mercator_bounds(bounds) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bounds
    corners_x = [minx, minx, maxx, maxx]
    corners_y = [miny, maxy, miny, maxy]
    xs, ys = to_3857(corners_x, corners_y)
    return float(np.min(xs)), float(np.min(ys)), float(np.max(xs)), float(np.max(ys))


def _padded_bounds(absolute: np.ndarray, pad_m: float) -> tuple[float, float, float, float]:
    return (
        float(absolute[:, :, 0].min()) - pad_m,
        float(absolute[:, :, 1].min()) - pad_m,
        float(absolute[:, :, 0].max()) + pad_m,
        float(absolute[:, :, 1].max()) + pad_m,
    )


def _face_job(zone_id, faces, rows, bounds, zoom, part, parts) -> dict:
    return {
        "zone_id": int(zone_id),
        "faces": np.asarray(faces, dtype=np.int32),
        "rows": np.asarray(rows, dtype=np.int32),
        "bounds": bounds,
        "zoom": int(zoom),
        "part": int(part),
        "parts": int(parts),
    }

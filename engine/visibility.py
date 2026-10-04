from __future__ import annotations

import numpy as np
from rasterio.transform import Affine


def viewshed_mask(elev: np.ndarray, xs: np.ndarray, ys: np.ndarray, eyes: np.ndarray, n_bins: int = 360) -> np.ndarray:
    elev = np.asarray(elev, dtype=np.float64)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    height, width = elev.shape
    visible = np.zeros((height, width), dtype=bool)
    finite = np.isfinite(elev)
    x_step = float(xs[0, 1] - xs[0, 0]) if width > 1 else 1.0
    y_step = float(ys[1, 0] - ys[0, 0]) if height > 1 else 1.0
    if x_step == 0 or y_step == 0:
        return visible
    pixel = max(min(abs(x_step), abs(y_step)), 1e-6)
    x0 = float(xs[0, 0])
    y0 = float(ys[0, 0])
    max_radius = float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min()))
    ray_count = int(np.clip(max(n_bins, 2 * np.pi * max_radius / pixel), 180, 1440))
    angles = np.linspace(-np.pi, np.pi, ray_count, endpoint=False)
    steps = np.arange(pixel, max_radius + pixel, pixel)
    if steps.size == 0:
        return visible
    for eye_x, eye_y, eye_z in np.asarray(eyes, dtype=np.float64):
        ray_x = eye_x + np.cos(angles)[:, None] * steps[None, :]
        ray_y = eye_y + np.sin(angles)[:, None] * steps[None, :]
        cols = np.rint((ray_x - x0) / x_step).astype(np.int64)
        rows = np.rint((ray_y - y0) / y_step).astype(np.int64)
        inside = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
        ground = np.full(ray_x.shape, np.nan, dtype=np.float64)
        ground[inside] = elev[rows[inside], cols[inside]]
        slope = np.full(ground.shape, -np.inf, dtype=np.float64)
        valid = inside & np.isfinite(ground)
        slope[valid] = (ground[valid] - eye_z) / np.broadcast_to(steps, slope.shape)[valid]
        running = np.maximum.accumulate(np.where(np.isfinite(slope), slope, -np.inf), axis=1)
        previous = np.full_like(slope, -np.inf)
        previous[:, 1:] = running[:, :-1]
        hit = valid & (slope + 1e-6 >= previous)
        visible[rows[hit], cols[hit]] = True
    visible &= finite
    return visible


def downsample(array: np.ndarray, transform: Affine, max_cells: int = 350_000):
    height, width = array.shape
    if height * width <= max_cells:
        return array, transform
    scale = int(np.ceil(np.sqrt((height * width) / max_cells)))
    reduced = array[::scale, ::scale]
    return reduced, transform * Affine.scale(scale, scale)

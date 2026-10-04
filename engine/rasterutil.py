from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import Affine, array_bounds


def bilinear_sample(array: np.ndarray, transform: Affine, xs, ys, nodata=-9999.0) -> np.ndarray:
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    shape = xs.shape
    xs = xs.ravel()
    ys = ys.ravel()
    inverse = ~transform
    cols = inverse.a * xs + inverse.b * ys + inverse.c - 0.5
    rows = inverse.d * xs + inverse.e * ys + inverse.f - 0.5
    col0 = np.floor(cols).astype(np.int64)
    row0 = np.floor(rows).astype(np.int64)
    dx = cols - col0
    dy = rows - row0
    height, width = array.shape

    def take(rows_idx, cols_idx):
        values = np.full(xs.shape, np.nan, dtype=np.float64)
        ok = (rows_idx >= 0) & (rows_idx < height) & (cols_idx >= 0) & (cols_idx < width)
        if ok.any():
            sampled = array[rows_idx[ok], cols_idx[ok]].astype(np.float64)
            if nodata is not None and np.isfinite(nodata):
                sampled[sampled == nodata] = np.nan
            sampled[(sampled < -500) | (sampled > 5000)] = np.nan
            values[ok] = sampled
        return values

    samples = (
        (take(row0, col0), (1 - dx) * (1 - dy)),
        (take(row0, col0 + 1), dx * (1 - dy)),
        (take(row0 + 1, col0), (1 - dx) * dy),
        (take(row0 + 1, col0 + 1), dx * dy),
    )
    numerator = np.zeros(xs.shape, dtype=np.float64)
    denominator = np.zeros(xs.shape, dtype=np.float64)
    for values, weight in samples:
        valid = np.isfinite(values)
        numerator[valid] += values[valid] * weight[valid]
        denominator[valid] += weight[valid]
    result = np.full(xs.shape, np.nan, dtype=np.float64)
    good = denominator > 0
    result[good] = numerator[good] / denominator[good]
    return result.reshape(shape)


class ElevationSampler:
    def __init__(self, paths: list) -> None:
        self.layers = []
        for path in paths:
            with rasterio.open(path) as dataset:
                data = dataset.read(1).astype(np.float32)
                nodata = dataset.nodata if dataset.nodata is not None else -9999.0
                self.layers.append((data, dataset.transform, float(nodata)))
        if not self.layers:
            raise ValueError("Chybí výškový rastr. Nejdřív stáhněte DMR 5G, DMP 1G, nebo importujte soubor.")

    def sample(self, xs, ys) -> np.ndarray:
        xs = np.asarray(xs, dtype=np.float64)
        ys = np.asarray(ys, dtype=np.float64)
        output = np.full(xs.shape, np.nan, dtype=np.float64)
        for data, transform, nodata in self.layers:
            sampled = bilinear_sample(data, transform, xs, ys, nodata)
            valid = np.isfinite(sampled)
            output[valid] = sampled[valid]
        return output


def grid_centers(transform: Affine, height: int, width: int):
    columns, rows = np.meshgrid(np.arange(width), np.arange(height))
    xs = transform.c + transform.a * (columns + 0.5) + transform.b * (rows + 0.5)
    ys = transform.f + transform.d * (columns + 0.5) + transform.e * (rows + 0.5)
    return xs, ys


def hillshade(elev: np.ndarray) -> np.ndarray:
    filled = np.array(elev, dtype=np.float64, copy=True)
    valid = np.isfinite(filled)
    if not valid.any():
        return np.zeros(filled.shape, dtype=np.uint8)
    filled[~valid] = np.nanmean(filled)
    gy, gx = np.gradient(filled)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    azimuth = np.radians(315.0)
    altitude = np.radians(45.0)
    shaded = np.sin(altitude) * np.sin(slope) + np.cos(altitude) * np.cos(slope) * np.cos(azimuth - aspect)
    shaded = np.clip(shaded, 0, 1)
    image = (shaded * 255).astype(np.uint8)
    image[~valid] = 0
    return image


def bounds_from_transform(transform: Affine, height: int, width: int):
    left, bottom, right, top = array_bounds(height, width, transform)
    return left, bottom, right, top

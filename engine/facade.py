"""Opakovatelná fasáda pro stěny budov. Jeden čtverec se dlaždicuje po 6 m."""

from __future__ import annotations

from pathlib import Path

import numpy as np

TILE_M = 6.0
METAL_TILE_M = 2.0
SIZE = 1024
LOW_BUILDING_M = 4.5

PLASTER_COLORS = (
    (214, 206, 190),
    (196, 164, 112),
    (168, 178, 164),
    (186, 142, 124),
    (214, 196, 132),
    (148, 150, 152),
    (176, 132, 96),
    (164, 176, 188),
    (198, 176, 154),
    (128, 138, 128),
    (176, 160, 148),
    (154, 118, 96),
    (188, 188, 180),
    (142, 116, 108),
    (168, 148, 92),
    (120, 128, 136),
)
BRICK_COLORS = (
    (158, 62, 46),
    (112, 48, 42),
    (176, 92, 58),
    (132, 68, 46),
    (96, 54, 48),
    (168, 78, 68),
    (140, 88, 56),
    (84, 46, 40),
    (150, 72, 64),
    (120, 64, 52),
    (184, 104, 72),
    (102, 58, 50),
    (146, 80, 60),
    (90, 42, 38),
    (160, 86, 64),
    (124, 52, 44),
)
METAL_COLORS = (
    (176, 180, 184),
    (78, 86, 92),
    (62, 108, 74),
    (58, 88, 128),
    (148, 58, 52),
    (124, 92, 62),
    (214, 214, 208),
    (186, 154, 64),
    (48, 56, 64),
    (150, 108, 72),
    (96, 112, 108),
    (132, 72, 64),
    (168, 172, 160),
    (72, 78, 96),
    (160, 120, 48),
    (100, 104, 108),
)


def generate_facades(folder: Path, facade_count: int, metal_count: int, style: str, seed: int, progress) -> str:
    facade_count = max(0, min(16, int(facade_count)))
    metal_count = max(0, min(16, int(metal_count)))
    if facade_count + metal_count == 0:
        raise ValueError("Nejsou budovy, na které jde položit fasádu.")
    folder.mkdir(parents=True, exist_ok=True)
    for pattern in ("facade_*.png", "metal_*.png"):
        for stale in folder.glob(pattern):
            stale.unlink()
    from PIL import Image

    total = facade_count + metal_count
    done = 0
    wall = "brick" if style == "brick" else "plaster"
    for index in range(facade_count):
        done += 1
        progress(8 + 84 * done / total, f"Kreslím fasádu {index + 1}/{facade_count}")
        image = render_facade(wall, _palette_color(wall, index), int(seed) + index * 10007)
        Image.fromarray(image, "RGB").save(folder / f"facade_{index:02d}.png")
    for index in range(metal_count):
        done += 1
        progress(8 + 84 * done / total, f"Kreslím plech {index + 1}/{metal_count}")
        image = render_facade("metal", _palette_color("metal", index), int(seed) + 50000 + index * 10007)
        Image.fromarray(image, "RGB").save(folder / f"metal_{index:02d}.png")
    progress(100, "Fasády jsou hotové")
    parts = []
    if facade_count:
        label = "cihla" if wall == "brick" else "omítka"
        parts.append(f"{facade_count}× {label}")
    if metal_count:
        parts.append(f"{metal_count}× plech")
    return "Hotovo: " + ", ".join(parts) + ". Nižší budovy mají plech, ostatní fasádu. Blízké budovy mají jinou barvu."


def _palette_color(style: str, index: int):
    if style == "metal":
        palette = METAL_COLORS
    elif style == "brick":
        palette = BRICK_COLORS
    else:
        palette = PLASTER_COLORS
    return palette[int(index) % len(palette)]


def assign_variants(points, count: int) -> np.ndarray:
    """Každá budova dostane variantu, která je od stejné fasády nejdál."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    total = len(points)
    count = max(1, min(16, int(count), total or 1))
    variant = np.full(total, -1, dtype=np.int32)
    if total == 0:
        return variant
    for index in _spatial_order(points):
        best = 0
        best_key = None
        here = points[index]
        for choice in range(count):
            same = np.flatnonzero(variant == choice)
            if len(same) == 0:
                distance = np.inf
            else:
                delta = points[same] - here
                distance = float(np.min(np.hypot(delta[:, 0], delta[:, 1])))
            key = (distance, -int(len(same)))
            if best_key is None or key > best_key:
                best_key = key
                best = choice
        variant[index] = best
    return variant


def _spatial_order(points: np.ndarray) -> list[int]:
    total = len(points)
    center = points.mean(axis=0)
    start = int(np.argmin(np.hypot(points[:, 0] - center[0], points[:, 1] - center[1])))
    remaining = set(range(total))
    remaining.remove(start)
    order = [start]
    while remaining:
        visited = points[order]
        nearest = None
        nearest_distance = np.inf
        for index in remaining:
            delta = visited - points[index]
            distance = float(np.min(np.hypot(delta[:, 0], delta[:, 1])))
            if distance < nearest_distance:
                nearest_distance = distance
                nearest = index
        remaining.remove(nearest)
        order.append(nearest)
    return order


def render_facade(style: str, color, seed: int) -> np.ndarray:
    rng = np.random.default_rng(int(seed) & 0x7FFFFFFF)
    if style == "metal":
        return _metal(color)
    if style == "brick":
        image = _brick(rng, color)
        _windows(image, rng, "brick")
        return image
    image = _plaster(rng, color)
    _windows(image, rng, "plaster")
    return image


def _plaster(rng: np.random.Generator, color) -> np.ndarray:
    image = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    image[:] = color
    grain = _periodic_noise((SIZE, SIZE), 96, rng) * 14
    stain = _periodic_noise((SIZE, SIZE), 12, rng) * 8
    image += (grain + stain)[..., None]
    return np.clip(image, 0, 255).astype(np.uint8)


def _metal(color) -> np.ndarray:
    image = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    image[:] = color
    xs = np.arange(SIZE, dtype=np.float32)
    wave = np.sin(xs / 28.0 * 2.0 * np.pi)
    shade = 0.72 + 0.28 * (wave * 0.5 + 0.5)
    groove = (xs.astype(np.int32) % 28) < 3
    shade = np.where(groove, shade * 0.62, shade)
    image *= shade[None, :, None]
    ys = np.arange(SIZE)
    seam = (ys % 256) < 4
    image[seam] *= 0.7
    return np.clip(image, 0, 255).astype(np.uint8)


def _brick(rng: np.random.Generator, color) -> np.ndarray:
    period_x = 64
    period_y = 32
    mortar = 5
    mortar_color = (168, 160, 150)
    image = np.empty((SIZE, SIZE, 3), dtype=np.uint8)
    ys, xs = np.mgrid[0:SIZE, 0:SIZE]
    row = ys // period_y
    offset = np.where(row % 2 == 1, period_x // 2, 0)
    local_x = (xs + offset) % period_x
    local_y = ys % period_y
    joint = (local_x < mortar) | (local_y < mortar)
    column = (xs + offset) // period_x
    mix = _hash01(row, column)
    red = 132 + mix * 48
    green = 62 + mix * 28
    blue = 48 + mix * 16
    brick = np.stack([red, green, blue], axis=2)
    target = np.array(color, dtype=np.float32)
    brick = brick * 0.28 + target * 0.72
    image[:] = np.clip(brick, 0, 255).astype(np.uint8)
    image[joint] = mortar_color
    speckle = _periodic_noise((SIZE, SIZE), 80, rng)
    image = np.clip(image.astype(np.float32) + speckle[..., None] * 8, 0, 255).astype(np.uint8)
    return image


def _windows(image: np.ndarray, rng: np.random.Generator, kind: str) -> None:
    cell = SIZE // 2
    window_w = 168 if kind == "plaster" else 150
    window_h = 228 if kind == "plaster" else 210
    frame = 14
    for row in range(2):
        for col in range(2):
            cx = col * cell + cell // 2
            cy = row * cell + int(cell * 0.56)
            x0 = cx - window_w // 2
            y0 = cy - window_h // 2
            x1 = x0 + window_w
            y1 = y0 + window_h
            _fill(image, x0 - 8, y1, x1 + 8, y1 + 10, (92, 86, 78) if kind == "brick" else (206, 196, 180))
            _fill(image, x0 - frame, y0 - frame, x1 + frame, y1 + frame, (78, 64, 54))
            glass = (
                int(np.clip(96 + rng.integers(-8, 9), 0, 255)),
                int(np.clip(118 + rng.integers(-8, 9), 0, 255)),
                int(np.clip(132 + rng.integers(-6, 7), 0, 255)),
            )
            _fill(image, x0, y0, x1, y1, glass)
            mullion = (210, 206, 198)
            mid_x = (x0 + x1) // 2
            mid_y = (y0 + y1) // 2
            _fill(image, mid_x - 3, y0, mid_x + 3, y1, mullion)
            _fill(image, x0, mid_y - 3, x1, mid_y + 3, mullion)
            _shade_glass(image, x0, y0, x1, y1)


def _shade_glass(image: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> None:
    pane = image[y0:y1, x0:x1].astype(np.float32)
    height, width = pane.shape[:2]
    yy = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    highlight = np.clip(1.15 - (xx * 0.35 + yy * 0.25), 0.82, 1.12)
    pane *= highlight[..., None]
    image[y0:y1, x0:x1] = np.clip(pane, 0, 255).astype(np.uint8)


def _fill(image: np.ndarray, x0: int, y0: int, x1: int, y1: int, color) -> None:
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(SIZE, x1)
    y1 = min(SIZE, y1)
    if x1 <= x0 or y1 <= y0:
        return
    image[y0:y1, x0:x1] = color


def _periodic_noise(shape, cells: int, rng: np.random.Generator) -> np.ndarray:
    lattice = rng.random((cells, cells)).astype(np.float32) * 2 - 1
    ys = np.linspace(0, cells, shape[0], endpoint=False)
    xs = np.linspace(0, cells, shape[1], endpoint=False)
    y0 = np.floor(ys).astype(np.int32) % cells
    x0 = np.floor(xs).astype(np.int32) % cells
    y1 = (y0 + 1) % cells
    x1 = (x0 + 1) % cells
    fy = (ys - np.floor(ys)).astype(np.float32)
    fx = (xs - np.floor(xs)).astype(np.float32)
    fy = fy * fy * (3 - 2 * fy)
    fx = fx * fx * (3 - 2 * fx)
    n00 = lattice[y0][:, x0]
    n10 = lattice[y0][:, x1]
    n01 = lattice[y1][:, x0]
    n11 = lattice[y1][:, x1]
    top = n00 * (1 - fx) + n10 * fx
    bottom = n01 * (1 - fx) + n11 * fx
    return top * (1 - fy)[:, None] + bottom * fy[:, None]


def _hash01(row: np.ndarray, column: np.ndarray) -> np.ndarray:
    value = (row.astype(np.uint32) * 374761393 + column.astype(np.uint32) * 668265263) & 0xFFFFFFFF
    value = (value ^ (value >> 13)) * 1274126177 & 0xFFFFFFFF
    return (value & 255).astype(np.float32) / 255.0

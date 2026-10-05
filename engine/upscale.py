"""Zvětšení ortofota po dlaždicích. Model je funkce, která z obrázku udělá čtyřnásobek."""

from __future__ import annotations

import gc
import os
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


class GpuOutOfMemory(RuntimeError):
    pass


UPSCALE_SCALES = (1, 2, 4, 8, 16)
# Síť rozmazává zhruba 100 px originálu od okraje vstupu. 32 px z toho nechalo měkký pruh.
TILE_OVERLAP = 96
_RAM_OUTPUT = 400 * 1024 * 1024


def output_size(width: int, height: int, scale: int) -> tuple[int, int]:
    _check_scale(scale)
    return width * scale, height * scale


def upscaled_problem(
    result: Path,
    source: Path,
    scale: int,
    output_dir: Path,
    source_size: tuple[int, int] | None = None,
) -> str | None:
    """None, když je ve složce OUTPUT celý soubor ve správném rozměru. Jinak důvod."""
    if scale <= 1:
        return None
    if source_size is None and not source.is_file():
        return "chybí originál"
    if not result.is_file():
        return "chybí soubor ve složce OUTPUT"
    root = output_dir.resolve()
    try:
        resolved = result.resolve()
    except OSError:
        return "soubor nejde přečíst"
    if resolved != root and root not in resolved.parents:
        return "soubor není ve složce OUTPUT"
    if not _image_complete(result):
        return "soubor je nedopsaný"
    if source_size is None:
        source_size = _image_size(source)
    actual = _image_size(result)
    if source_size is None or actual is None:
        return "nejde přečíst rozměr"
    expected = output_size(source_size[0], source_size[1], scale)
    if actual != expected:
        return f"rozměr {actual[0]}×{actual[1]} místo {expected[0]}×{expected[1]}"
    return None


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def _image_complete(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < 32:
        return False
    with path.open("rb") as handle:
        handle.seek(max(0, size - 32))
        tail = handle.read()
    suffix = path.suffix.lower()
    if suffix == ".png":
        return b"IEND" in tail
    if suffix in {".jpg", ".jpeg"}:
        return tail.rstrip(b"\x00\r\n ").endswith(b"\xff\xd9")
    return False


def upscale_array(
    image: np.ndarray,
    scale: int,
    tile: int,
    overlap: int,
    upscale_x4,
    progress=None,
    label: str = "",
) -> np.ndarray:
    reporter = _Reporter(progress, label)
    result, scratch = _upscale_core(image, scale, tile, overlap, upscale_x4, reporter)
    if scratch is None:
        return result
    copied = np.array(result, copy=True)
    _release_scratch(result, scratch)
    return copied


def upscale_png(
    path: Path,
    scale: int,
    tile: int,
    overlap: int,
    upscale_x4,
    *,
    lossless: bool = False,
    keep_source: bool = False,
    progress=None,
    label: str = "",
) -> Path:
    reporter = _Reporter(progress, label)
    image = np.asarray(Image.open(path).convert("RGB"))
    height, width = image.shape[:2]
    out_w, out_h = output_size(width, height, scale)
    reporter.emit(0.0, f"načteno {width}×{height}, cíl {out_w}×{out_h}", force=True, event=f"načteno {width}×{height}, cíl {out_w}×{out_h}")
    result, scratch = _upscale_core(image, scale, tile, overlap, upscale_x4, reporter)
    del image
    dest = path if lossless else path.with_suffix(".jpg")
    kind = "PNG" if lossless else "JPEG"
    reporter.span(0.93, 1.0)
    reporter.emit(0.0, f"ukládám {kind} {result.shape[1]}×{result.shape[0]}", force=True, event=f"ukládám {kind} {result.shape[1]}×{result.shape[0]}")
    try:
        _save_rgb(result, dest, lossless)
    finally:
        _release_scratch(result, scratch)
    if not keep_source and dest.resolve() != path.resolve() and path.exists():
        path.unlink()
    reporter.emit(1.0, f"uloženo {dest.name}", force=True, event=f"uloženo {dest.name}")
    return dest


def downsample_half(image: np.ndarray) -> np.ndarray:
    reduced = _downsample_half_mean(image)
    return np.clip(np.rint(reduced), 0, 255).astype(np.uint8)


def _upscale_core(image: np.ndarray, scale: int, tile: int, overlap: int, upscale_x4, reporter=None):
    _check_scale(scale)
    image = np.asarray(image)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Ortofoto musí být barevný obrázek.")
    if scale == 1:
        return image.copy(), None
    passes = 2 if scale in (8, 16) else 1
    shrink_last = scale in (2, 8)
    height, width = image.shape[:2]
    works = []
    h_now, w_now = height, width
    for index in range(passes):
        shrink = shrink_last and index == passes - 1
        works.append(_gpu_steps(h_now, w_now, tile, overlap, shrink))
        factor = 2 if shrink else 4
        h_now *= factor
        w_now *= factor
    total_work = sum(works) or 1
    current = image
    scratch = None
    cursor = 0
    for index in range(passes):
        shrink = shrink_last and index == passes - 1
        span0 = 0.02 + 0.90 * cursor / total_work
        span1 = 0.02 + 0.90 * (cursor + works[index]) / total_work
        if reporter is not None:
            reporter.span(span0, span1)
        nxt, nxt_scratch = _upscale_x4_retry(
            current,
            tile,
            overlap,
            upscale_x4,
            shrink,
            reporter,
            pass_index=index,
            passes=passes,
            scale=scale,
        )
        old, old_scratch = current, scratch
        current, scratch = nxt, nxt_scratch
        if old_scratch:
            _release_scratch(old, old_scratch)
        gc.collect()
        cursor += works[index]
    return current, scratch


def _check_scale(scale: int) -> None:
    if scale not in UPSCALE_SCALES:
        raise ValueError("Násobek upscale musí být 1, 2, 4, 8 nebo 16.")


def _upscale_x4_retry(
    image: np.ndarray,
    tile: int,
    overlap: int,
    upscale_x4,
    shrink: bool,
    reporter=None,
    pass_index: int = 0,
    passes: int = 1,
    scale: int = 4,
):
    size = max(128, min(512, int(tile)))
    last_error = None
    while size >= 128:
        try:
            return _upscale_x4_tiled(
                image,
                size,
                min(overlap, size // 3),
                upscale_x4,
                shrink,
                reporter,
                pass_index,
                passes,
                scale,
            )
        except GpuOutOfMemory as exc:
            last_error = exc
            size = 384 if size > 384 else size // 2
            gc.collect()
            if reporter is not None and size >= 128:
                reporter.emit(0.0, f"málo VRAM, zkouším dlaždici {size} px", force=True, event=f"málo VRAM, zkouším dlaždici {size} px")
    raise RuntimeError("Grafika nemá dost paměti ani pro dlaždici 128 px. Zmenšete ji v panelu a zkuste znovu.") from last_error


def _upscale_x4_tiled(
    image: np.ndarray,
    tile: int,
    overlap: int,
    upscale_x4,
    shrink: bool,
    reporter=None,
    pass_index: int = 0,
    passes: int = 1,
    scale: int = 4,
):
    """Dlaždice dostane přesah jako kontext a ten se po modelu ořízne.

    U dlaždice 360 px je přesah 96 px. Menší přesah nechává rozmazaný pruh v obraze.
    Průměrování překryvu míchalo nejisté okraje a v pruzích obraz rozmývalo.
    """
    height, width = image.shape[:2]
    factor = 2 if shrink else 4
    out_h = height * factor
    out_w = width * factor
    output, scratch = _uint8_canvas((out_h, out_w, 3))
    pad, inner = _useful_tile(height, width, tile, overlap)
    ys = _origins(height, inner)
    xs = _origins(width, inner)
    total = max(1, len(ys) * len(xs))
    done = 0
    started = time.monotonic()
    goal = _pass_goal(scale, pass_index, shrink)
    if reporter is not None:
        reporter.emit(
            0.0,
            f"průchod {pass_index + 1}/{passes} {goal} · {_px(width, height)} → {_px(out_w, out_h)} · dlaždice 0/{total}",
            force=True,
            event=f"průchod {pass_index + 1}/{passes} {goal} začátek · {_px(width, height)} → {_px(out_w, out_h)}",
        )
    for top in ys:
        bottom = min(height, top + inner)
        for left in xs:
            right = min(width, left + inner)
            scaled = _scale_region(image, top, bottom, left, right, pad, upscale_x4, shrink)
            y0 = top * factor
            x0 = left * factor
            output[y0 : y0 + scaled.shape[0], x0 : x0 + scaled.shape[1]] = scaled
            del scaled
            done += 1
            if reporter is not None:
                eta = _eta_text(started, done, total)
                extras = f" · {eta}" if eta else ""
                reporter.emit(
                    0.9 * done / total,
                    f"průchod {pass_index + 1}/{passes} {goal} · {_px(width, height)} → {_px(out_w, out_h)} · dlaždice {done}/{total}{extras}",
                    force=done == total,
                    event=f"průchod {pass_index + 1}/{passes} {goal} konec · dlaždice {done}/{total}" if done == total else None,
                )
    _repair_soft_tiles(
        image,
        output,
        ys,
        xs,
        inner,
        pad,
        tile,
        upscale_x4,
        shrink,
        factor,
        reporter,
    )
    _match_source_tone(image, output, factor, reporter)
    if scratch:
        output.flush()
    return output, scratch


def _scale_region(image, top, bottom, left, right, pad, upscale_x4, shrink):
    crop = _context_crop(image, top, bottom, left, right, pad)
    scaled = np.asarray(upscale_x4(crop))
    expected = (crop.shape[0] * 4, crop.shape[1] * 4, 3)
    if scaled.shape != expected:
        raise ValueError("Model nevrátil čtyřnásobek dlaždice.")
    if pad:
        margin = pad * 4
        scaled = scaled[margin:-margin, margin:-margin]
    if shrink:
        scaled = downsample_half(scaled)
    elif scaled.dtype != np.uint8:
        scaled = np.clip(np.rint(scaled), 0, 255).astype(np.uint8)
    return scaled


def _lanczos_up(source: np.ndarray, factor: int) -> np.ndarray:
    image = Image.fromarray(np.ascontiguousarray(source), "RGB")
    resized = image.resize((source.shape[1] * factor, source.shape[0] * factor), Image.Resampling.LANCZOS)
    return np.asarray(resized)


def _repair_soft_tiles(image, output, ys, xs, inner, _pad, tile, upscale_x4, shrink, factor, reporter):
    """Měkký pruh dlaždice vykreslí znovu uprostřed okna, kde model nerozmazává okraj."""
    height, width = image.shape[:2]
    jobs = []
    for top in ys:
        bottom = min(height, top + inner)
        for left in xs:
            right = min(width, left + inner)
            if bottom - top < 16 or right - left < 16:
                continue
            source = image[top:bottom, left:right]
            current = output[top * factor : bottom * factor, left * factor : right * factor]
            boxes = _soft_boxes(source, current, factor)
            if boxes:
                jobs.append((top, bottom, left, right, boxes))
    if reporter is not None:
        reporter.emit(
            0.9,
            f"kontrola ostrosti · měkké dlaždice {len(jobs)}",
            force=True,
            event=f"kontrola ostrosti · měkké dlaždice {len(jobs)}",
        )
    if not jobs:
        return 0
    span = max(128, min(1024, int(tile)))
    piece_limit = max(32, span - 256)
    allow_gpu = True
    ai_fixed = 0
    origin_fixed = 0
    for index, (top, bottom, left, right, boxes) in enumerate(jobs, start=1):
        for box in boxes:
            for rel_top, rel_bottom, rel_left, rel_right in _split_box(*box, piece_limit):
                src_top = top + rel_top
                src_bottom = top + rel_bottom
                src_left = left + rel_left
                src_right = left + rel_right
                y0, y1 = src_top * factor, src_bottom * factor
                x0, x1 = src_left * factor, src_right * factor
                current = np.array(output[y0:y1, x0:x1])
                source = image[src_top:src_bottom, src_left:src_right]
                old_sharp = _sharpness(current)
                chosen = None
                rendered = None
                if allow_gpu:
                    try:
                        rendered = _render_centered(
                            image, src_top, src_bottom, src_left, src_right, span, upscale_x4, shrink, factor
                        )
                    except GpuOutOfMemory:
                        allow_gpu = False
                        rendered = None
                        gc.collect()
                        if reporter is not None:
                            reporter.emit(
                                0.9,
                                "na korekci není paměť, další beru z originálu",
                                force=True,
                                event="korekce ostrosti · na korekci není paměť",
                            )
                if rendered is not None and _sharpness(rendered) >= old_sharp * 1.2:
                    chosen = rendered
                    ai_fixed += 1
                else:
                    origin = _lanczos_up(source, factor)
                    if origin.shape == current.shape and _sharpness(origin) >= old_sharp * 1.2:
                        chosen = origin
                        origin_fixed += 1
                if chosen is not None:
                    output[y0:y1, x0:x1] = chosen
                del chosen, current, rendered
        if reporter is not None:
            last = index == len(jobs)
            reporter.emit(
                0.9 + 0.1 * index / len(jobs),
                f"korekce ostrosti · ostřejší průchod {ai_fixed}, z originálu {origin_fixed}" if last else f"korekce ostrosti · dlaždice {index}/{len(jobs)}",
                force=last,
                event=f"korekce ostrosti · měkké {len(jobs)}, ostřejší průchod {ai_fixed}, z originálu {origin_fixed}" if last else None,
            )
    return ai_fixed + origin_fixed


def _soft_boxes(source: np.ndarray, scaled: np.ndarray, factor: int) -> list[tuple[int, int, int, int]]:
    """Čtvrtiny, ve kterých plné rozlišení ztratilo ostrost originálu.

    Průměr celé dlaždice takový pruh schová, protože zbytek zůstane ostrý.
    """
    height, width = source.shape[:2]
    boxes: list[tuple[int, int, int, int]] = []
    if _lost_detail(source, scaled, factor):
        boxes.append((0, height, 0, width))
    else:
        for top, bottom in _quarters(height):
            part = scaled[top * factor : bottom * factor]
            if _washed(source[top:bottom], part):
                boxes.append((top, bottom, 0, width))
        for left, right in _quarters(width):
            part = scaled[:, left * factor : right * factor]
            if _washed(source[:, left:right], part):
                boxes.append((0, height, left, right))
    if len(boxes) == 1 and boxes[0] == (0, height, 0, width):
        return boxes
    return _merge_bands(boxes, height, width)


def _washed(source: np.ndarray, scaled: np.ndarray) -> bool:
    if source.shape[0] < 8 or source.shape[1] < 8:
        return False
    source_sharp = _sharpness(source)
    if source_sharp < 4.0:
        return False
    return _sharpness(scaled) < 0.55 * source_sharp


def _quarters(length: int) -> list[tuple[int, int]]:
    if length < 32:
        return [(0, length)]
    return [(index * length // 4, (index + 1) * length // 4) for index in range(4)]


def _merge_bands(boxes, height: int, width: int) -> list[tuple[int, int, int, int]]:
    rows = [box for box in boxes if box[2] == 0 and box[3] == width]
    cols = [box for box in boxes if box[0] == 0 and box[1] == height]
    return _merge_touching(rows, along_x=False) + _merge_touching(cols, along_x=True)


def _merge_touching(boxes, along_x: bool) -> list[tuple[int, int, int, int]]:
    ordered = sorted(boxes, key=(lambda box: box[2]) if along_x else (lambda box: box[0]))
    merged: list[tuple[int, int, int, int]] = []
    for top, bottom, left, right in ordered:
        if not merged:
            merged.append((top, bottom, left, right))
            continue
        prev_top, prev_bottom, prev_left, prev_right = merged[-1]
        touches = prev_right == left if along_x else prev_bottom == top
        aligned = (prev_top, prev_bottom) == (top, bottom) if along_x else (prev_left, prev_right) == (left, right)
        if touches and aligned:
            merged[-1] = (prev_top, bottom, prev_left, right) if not along_x else (prev_top, prev_bottom, prev_left, right)
        else:
            merged.append((top, bottom, left, right))
    return merged


def _split_box(top: int, bottom: int, left: int, right: int, limit: int) -> list[tuple[int, int, int, int]]:
    pieces = []
    y = top
    while y < bottom:
        y2 = min(bottom, y + limit)
        x = left
        while x < right:
            x2 = min(right, x + limit)
            pieces.append((y, y2, x, x2))
            x = x2
        y = y2
    return pieces


def _render_centered(image, top, bottom, left, right, span, upscale_x4, shrink, factor):
    """Okno stejné velikosti jako hlavní dlaždice, měkký výřez v jeho středu."""
    span = int(span)
    y0 = int(round((top + bottom) / 2 - span / 2))
    x0 = int(round((left + right) / 2 - span / 2))
    crop = _window(image, y0, x0, span)
    scaled = np.asarray(upscale_x4(crop))
    rel_top = top - y0
    rel_left = left - x0
    piece = scaled[rel_top * 4 : (rel_top + (bottom - top)) * 4, rel_left * 4 : (rel_left + (right - left)) * 4]
    if shrink:
        piece = downsample_half(piece)
    elif piece.dtype != np.uint8:
        piece = np.clip(np.rint(piece), 0, 255).astype(np.uint8)
    expected = ((bottom - top) * factor, (right - left) * factor, 3)
    if piece.shape != expected:
        return None
    return np.ascontiguousarray(piece)


def _pad_to_window(crop: np.ndarray, above: int, below: int, before: int, after: int) -> np.ndarray:
    """Reflect umí doplnit jen méně, než je samotný výřez. Zbytek dorovná okrajem."""
    pad_y = (max(0, above), max(0, below))
    pad_x = (max(0, before), max(0, after))
    while pad_y[0] or pad_y[1] or pad_x[0] or pad_x[1]:
        take_y = (min(pad_y[0], max(0, crop.shape[0] - 1)), min(pad_y[1], max(0, crop.shape[0] - 1)))
        take_x = (min(pad_x[0], max(0, crop.shape[1] - 1)), min(pad_x[1], max(0, crop.shape[1] - 1)))
        if take_y == (0, 0) and take_x == (0, 0):
            return np.pad(crop, (pad_y, pad_x, (0, 0)), mode="edge")
        crop = np.pad(crop, (take_y, take_x, (0, 0)), mode="reflect")
        pad_y = (pad_y[0] - take_y[0], pad_y[1] - take_y[1])
        pad_x = (pad_x[0] - take_x[0], pad_x[1] - take_x[1])
    return crop


def _window(image: np.ndarray, y0: int, x0: int, span: int) -> np.ndarray:
    height, width = image.shape[:2]
    y1 = y0 + span
    x1 = x0 + span
    src_y0 = max(0, y0)
    src_y1 = min(height, y1)
    src_x0 = max(0, x0)
    src_x1 = min(width, x1)
    crop = image[src_y0:src_y1, src_x0:src_x1]
    if crop.size == 0:
        crop = image[:1, :1]
    above = src_y0 - y0
    below = y1 - src_y1
    before = src_x0 - x0
    after = x1 - src_x1
    if above or below or before or after:
        crop = _pad_to_window(crop, above, below, before, after)
    if crop.shape[0] != span or crop.shape[1] != span:
        crop = np.pad(
            crop,
            ((0, max(0, span - crop.shape[0])), (0, max(0, span - crop.shape[1])), (0, 0)),
            mode="edge",
        )
        crop = crop[:span, :span]
    return np.ascontiguousarray(crop)


def _lost_detail(source: np.ndarray, scaled: np.ndarray, factor: int) -> bool:
    keep, source_sharp = _detail_keep(source, scaled, factor, with_source=True)
    return source_sharp >= 6.0 and keep < 0.85


def _detail_keep(source: np.ndarray, scaled: np.ndarray, factor: int, with_source: bool = False):
    reduced = _box_down(scaled, factor)
    height = min(reduced.shape[0], source.shape[0])
    width = min(reduced.shape[1], source.shape[1])
    if height < 3 or width < 3:
        return (1.0, 0.0) if with_source else 1.0
    source_sharp = _sharpness(source[:height, :width])
    output_sharp = _sharpness(reduced[:height, :width])
    keep = output_sharp / (source_sharp + 1e-3)
    if with_source:
        return keep, source_sharp
    return keep


def _sharpness(image: np.ndarray) -> float:
    luma = np.asarray(image, dtype=np.float32)
    if luma.ndim == 3:
        luma = 0.2126 * luma[:, :, 0] + 0.7152 * luma[:, :, 1] + 0.0722 * luma[:, :, 2]
    if luma.shape[0] < 3 or luma.shape[1] < 3:
        return 0.0
    vertical = np.abs(luma[2:, 1:-1] - 2.0 * luma[1:-1, 1:-1] + luma[:-2, 1:-1])
    horizontal = np.abs(luma[1:-1, 2:] - 2.0 * luma[1:-1, 1:-1] + luma[1:-1, :-2])
    return float(vertical.mean() + horizontal.mean())


def _match_source_tone(source: np.ndarray, output, factor: int, reporter=None) -> None:
    """Jas, expozice a kontrast srovná s originálem.

    Průměr drží jas a expozici, směrodatná odchylka kontrast. Počítá se až po
    zmenšení na velikost originálu, takže ostrost nových pixelů zůstane.
    """
    src_mean, src_std = _channel_stats(source)
    out_mean, out_std = _downscaled_stats(output, factor, source.shape[0], source.shape[1])
    scale = np.ones(3, dtype=np.float32)
    usable = out_std > 0.5
    scale[usable] = (src_std[usable] / out_std[usable]).astype(np.float32)
    shift = (src_mean - out_mean * scale).astype(np.float32)
    if np.allclose(scale, 1.0, atol=0.004) and np.allclose(shift, 0.0, atol=0.2):
        return
    _apply_tone(output, scale, shift)
    if reporter is not None:
        reporter.emit(
            1.0,
            "jas a kontrast srovnány s originálem",
            force=True,
            event="jas a kontrast srovnány s originálem",
        )


def _channel_stats(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(image, dtype=np.float64)
    return values.mean(axis=(0, 1)), values.std(axis=(0, 1))


def _downscaled_stats(image, factor: int, height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    factor = max(1, int(factor))
    total = np.zeros(3, dtype=np.float64)
    squares = np.zeros(3, dtype=np.float64)
    count = 0
    step = 64
    for y in range(0, height, step):
        rows = min(step, height - y)
        slab = np.asarray(image[y * factor : (y + rows) * factor, : width * factor], dtype=np.float64)
        block = slab.reshape(rows, factor, width, factor, 3).mean(axis=(1, 3))
        total += block.sum(axis=(0, 1))
        squares += np.square(block).sum(axis=(0, 1))
        count += rows * width
    mean = total / max(1, count)
    variance = np.maximum(squares / max(1, count) - np.square(mean), 0.0)
    return mean, np.sqrt(variance)


def _apply_tone(output, scale: np.ndarray, shift: np.ndarray) -> None:
    rows = 32
    for y in range(0, output.shape[0], rows):
        slab = np.asarray(output[y : y + rows], dtype=np.float32)
        slab *= scale
        slab += shift
        output[y : y + rows] = np.clip(np.rint(slab), 0, 255).astype(np.uint8)


def _box_down(image: np.ndarray, factor: int) -> np.ndarray:
    factor = max(1, int(factor))
    height = int(image.shape[0]) // factor * factor
    width = int(image.shape[1]) // factor * factor
    if height < factor or width < factor:
        return np.asarray(image, dtype=np.float32)
    cropped = np.asarray(image[:height, :width], dtype=np.float32)
    channels = cropped.shape[2]
    return cropped.reshape(height // factor, factor, width // factor, factor, channels).mean(axis=(1, 3))


def _useful_tile(height: int, width: int, tile: int, overlap: int) -> tuple[int, int]:
    """Přesah je kontext uvnitř dlaždice, ne pixely navíc pro grafiku.

    U dlaždice 360 px a přesahu 96 px se z každé strany zahodí 96 px.
    """
    tile = max(1, int(tile))
    limit = min(height, width) - 1
    if limit <= 0 or tile <= 1:
        return 0, tile
    pad = max(0, min(int(overlap), tile // 3, limit, (tile - 1) // 2))
    return pad, max(1, tile - 2 * pad)


def _context_crop(image: np.ndarray, top: int, bottom: int, left: int, right: int, pad: int) -> np.ndarray:
    if pad <= 0:
        return np.ascontiguousarray(image[top:bottom, left:right])
    height, width = image.shape[:2]
    y0 = top - pad
    y1 = bottom + pad
    x0 = left - pad
    x1 = right + pad
    src_y0 = max(0, y0)
    src_y1 = min(height, y1)
    src_x0 = max(0, x0)
    src_x1 = min(width, x1)
    crop = image[src_y0:src_y1, src_x0:src_x1]
    above = src_y0 - y0
    below = y1 - src_y1
    before = src_x0 - x0
    after = x1 - src_x1
    if above or below or before or after:
        crop = np.pad(crop, ((above, below), (before, after), (0, 0)), mode="reflect")
    return np.ascontiguousarray(crop)


def _uint8_canvas(shape: tuple[int, int, int]):
    nbytes = int(np.prod(shape))
    if nbytes <= _RAM_OUTPUT:
        return np.zeros(shape, dtype=np.uint8), None
    handle = tempfile.NamedTemporaryFile(prefix="lidar_upscale_", suffix=".rgb", delete=False)
    handle.close()
    canvas = np.memmap(handle.name, dtype=np.uint8, mode="w+", shape=shape)
    return canvas, handle.name


def _release_scratch(array, scratch: str | None) -> None:
    if scratch is None:
        return
    if isinstance(array, np.memmap):
        array.flush()
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()
    del array
    gc.collect()
    Path(scratch).unlink(missing_ok=True)


def _gpu_steps(height: int, width: int, tile: int, overlap: int, shrink: bool) -> int:
    del shrink
    _pad, inner = _useful_tile(height, width, tile, overlap)
    return max(1, len(_origins(height, inner)) * len(_origins(width, inner)))


def _pass_goal(scale: int, pass_index: int, shrink: bool) -> str:
    if scale == 2:
        return "na 2×"
    if scale == 4:
        return "na 4×"
    if scale == 8:
        return "na 8×" if shrink else "na 4×"
    if pass_index == 0:
        return "na 4×"
    return "na 16×"


def _px(width: int, height: int) -> str:
    mpx = width * height / 1_000_000
    if mpx >= 10:
        extra = f", {mpx:.0f} Mpx"
    elif mpx >= 0.15:
        extra = f", {mpx:.1f} Mpx".replace(".", ",")
    else:
        extra = ""
    return f"{width}×{height}{extra}"


def _eta_text(started: float, done: int, total: int) -> str:
    if done < 2 or total <= done:
        return ""
    elapsed = time.monotonic() - started
    if elapsed < 0.8:
        return ""
    remaining = elapsed / done * (total - done)
    if remaining >= 5400:
        return f"zbývá ~{remaining / 3600:.1f} h".replace(".", ",")
    if remaining >= 90:
        return f"zbývá ~{max(1, int(round(remaining / 60)))} min"
    return f"zbývá ~{max(1, int(remaining))} s"


class _Reporter:
    def __init__(self, callback, label: str) -> None:
        self.callback = callback
        self.label = (label or "").strip()
        self.last = 0.0
        self.span0 = 0.0
        self.span1 = 1.0

    def span(self, start: float, end: float) -> None:
        self.span0 = start
        self.span1 = end
        self.last = 0.0

    def emit(self, local: float, message: str, *, force: bool = False, event: str | None = None) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if not force and event is None and now - self.last < 0.3 and local < 0.999:
            return
        self.last = now
        frac = self.span0 + (self.span1 - self.span0) * min(1.0, max(0.0, local))
        text = f"{self.label} · {message}" if self.label else message
        logged = f"{self.label} · {event}" if event and self.label else event
        if logged:
            try:
                self.callback(frac, text, logged)
                return
            except TypeError:
                pass
        self.callback(frac, text)


def _downsample_half_mean(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    height -= height % 2
    width -= width % 2
    cropped = image[:height, :width].astype(np.float32, copy=False)
    if cropped.ndim == 2:
        return cropped.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
    channels = cropped.shape[2]
    return cropped.reshape(height // 2, 2, width // 2, 2, channels).mean(axis=(1, 3))


def _save_rgb(array: np.ndarray, dest: Path, lossless: bool) -> None:
    if not array.flags["C_CONTIGUOUS"]:
        array = np.ascontiguousarray(array)
    image = Image.fromarray(array, "RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".writing")
    try:
        with temporary.open("wb") as handle:
            if lossless:
                image.save(handle, format="PNG", optimize=True, compress_level=6)
            else:
                huge = int(array.shape[0]) * int(array.shape[1]) > 8_000_000
                image.save(handle, format="JPEG", quality=95, subsampling=0, optimize=not huge)
        os.replace(temporary, dest)
    except OSError as exc:
        if exc.errno == 22:
            raise OSError(
                f"Soubor {dest.name} je otevřený v Blenderu a Windows ho nedovolí přepsat. Zavři náhled textury a spusť upscale znovu."
            ) from exc
        raise
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _origins(length: int, tile: int) -> list[int]:
    if length <= tile:
        return [0]
    return list(range(0, length, tile))

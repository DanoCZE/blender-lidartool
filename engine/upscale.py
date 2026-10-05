"""Zvětšení ortofota po dlaždicích. Model je funkce, která z obrázku udělá čtyřnásobek."""

from __future__ import annotations

import gc
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


class GpuOutOfMemory(RuntimeError):
    pass


UPSCALE_SCALES = (1, 2, 4, 8, 16)
_RAM_OUTPUT = 400 * 1024 * 1024


def output_size(width: int, height: int, scale: int) -> tuple[int, int]:
    _check_scale(scale)
    return width * scale, height * scale


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
    reporter.emit(0.0, f"načteno {width}×{height}, cíl {out_w}×{out_h}", force=True)
    result, scratch = _upscale_core(image, scale, tile, overlap, upscale_x4, reporter)
    del image
    dest = path if lossless else path.with_suffix(".jpg")
    kind = "PNG" if lossless else "JPEG"
    reporter.span(0.93, 1.0)
    reporter.emit(0.0, f"ukládám {kind} {result.shape[1]}×{result.shape[0]}", force=True)
    try:
        _save_rgb(result, dest, lossless)
    finally:
        _release_scratch(result, scratch)
    if not keep_source and dest.resolve() != path.resolve() and path.exists():
        path.unlink()
    reporter.emit(1.0, f"uloženo {dest.name}", force=True)
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
    size = max(128, int(tile))
    last_error = None
    while size >= 128:
        try:
            return _upscale_x4_tiled(
                image,
                size,
                min(overlap, size // 4),
                upscale_x4,
                shrink,
                reporter,
                pass_index,
                passes,
                scale,
            )
        except GpuOutOfMemory as exc:
            last_error = exc
            size //= 2
            gc.collect()
            if reporter is not None:
                reporter.emit(0.0, f"málo VRAM, zkouším dlaždici {size} px", force=True)
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

    Okraj je měkčí, protože síť tam sahá mimo dlaždici. Ve výsledku zůstane jen střed.
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


def _repair_context(tile: int, pad: int, inner: int) -> int:
    """Větší okolí pro druhý průchod. Vejde se do 512 px, což je strop dlaždice."""
    room = 512 - (inner + 2 * pad)
    if room < 32:
        return pad
    return pad + min(room // 2, pad)


def _repair_soft_tiles(image, output, ys, xs, inner, pad, tile, upscale_x4, shrink, factor, reporter):
    """Dlaždice měkčí než originál se vykreslí znovu a nahradí, jen když je ostřejší."""
    height, width = image.shape[:2]
    soft = []
    for top in ys:
        bottom = min(height, top + inner)
        for left in xs:
            right = min(width, left + inner)
            if bottom - top < 8 or right - left < 8:
                continue
            source = image[top:bottom, left:right]
            current = output[top * factor : bottom * factor, left * factor : right * factor]
            if _lost_detail(source, current, factor):
                soft.append((top, bottom, left, right))
    if reporter is not None:
        reporter.emit(0.9, f"kontrola ostrosti · měkké dlaždice {len(soft)}", force=True)
    if not soft:
        return 0
    context = _repair_context(tile, pad, inner)
    if context <= pad:
        if reporter is not None:
            reporter.emit(1.0, "kontrola ostrosti · větší okolí se nevejde, dlaždice nechávám", force=True)
        return 0
    allow_bigger = True
    fixed = 0
    for index, (top, bottom, left, right) in enumerate(soft, start=1):
        source = image[top:bottom, left:right]
        y0 = top * factor
        x0 = left * factor
        y1 = bottom * factor
        x1 = right * factor
        current = np.array(output[y0:y1, x0:x1])
        keep_old = _detail_keep(source, current, factor)
        chosen = None
        if allow_bigger:
            try:
                chosen = _scale_region(image, top, bottom, left, right, context, upscale_x4, shrink)
            except GpuOutOfMemory:
                allow_bigger = False
                chosen = None
                gc.collect()
                if reporter is not None:
                    reporter.emit(0.9, "na větší okolí není paměť, další měkké dlaždice nechávám", force=True)
        if chosen is not None and chosen.shape != current.shape:
            chosen = None
        keep_new = _detail_keep(source, chosen, factor) if chosen is not None else 0.0
        if chosen is not None and (keep_new < keep_old * 1.15 or keep_new < keep_old + 0.12):
            chosen = None
        if chosen is not None:
            output[y0:y1, x0:x1] = chosen
            fixed += 1
        del chosen, current
        if reporter is not None:
            reporter.emit(
                0.9 + 0.1 * index / len(soft),
                f"kontrola ostrosti · přerender {index}/{len(soft)} · opraveno {fixed}",
                force=index == len(soft),
            )
    return fixed


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


def _box_down(image: np.ndarray, factor: int) -> np.ndarray:
    factor = max(1, int(factor))
    height = int(image.shape[0]) // factor * factor
    width = int(image.shape[1]) // factor * factor
    if height < factor or width < factor:
        return np.asarray(image, dtype=np.float32)
    cropped = np.asarray(image[:height, :width], dtype=np.float32)
    channels = cropped.shape[2]
    return cropped.reshape(height // factor, factor, width // factor, factor, channels).mean(axis=(1, 3))


def _edge_pad(tile: int) -> int:
    """Kolik pixelů z každé strany zahodit, aby ve výsledku nezůstal měkký okraj.

    U dlaždice 360 px je to 120 px. Menší přesah nechával pruhy, ve kterých
    je ostrost zhruba o pětinu nižší než uprostřed.
    """
    tile = int(tile)
    if tile <= 1:
        return 0
    return max(16, min(tile // 3, (tile - 32) // 2))


def _useful_tile(height: int, width: int, tile: int, overlap: int) -> tuple[int, int]:
    """Přesah je kontext uvnitř dlaždice, ne pixely navíc pro grafiku."""
    tile = max(1, int(tile))
    limit = min(height, width) - 1
    if limit <= 0 or tile <= 1:
        return 0, tile
    wanted = max(int(overlap), _edge_pad(tile))
    pad = max(0, min(wanted, (tile - 1) // 2, limit))
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

    def emit(self, local: float, message: str, *, force: bool = False) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if not force and now - self.last < 0.3 and local < 0.999:
            return
        self.last = now
        frac = self.span0 + (self.span1 - self.span0) * min(1.0, max(0.0, local))
        text = f"{self.label} · {message}" if self.label else message
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
    if lossless:
        image.save(dest, format="PNG", optimize=True, compress_level=6)
        return
    huge = int(array.shape[0]) * int(array.shape[1]) > 8_000_000
    image.save(dest, format="JPEG", quality=95, subsampling=0, optimize=not huge)


def _origins(length: int, tile: int) -> list[int]:
    if length <= tile:
        return [0]
    return list(range(0, length, tile))

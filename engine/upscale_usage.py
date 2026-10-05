"""Odhad paměti pro AI upscale. Bez numpy a Pillow, aby to šlo v panelu Blenderu."""

from __future__ import annotations

import json
import struct
from pathlib import Path

_BAND_BUDGET = 512 * 1024 * 1024
_RAM_OUTPUT = 400 * 1024 * 1024


GPU_TILES = {"4": 192, "6": 256, "8": 384, "12": 448, "16": 512}


def tile_for_gpu(preset: str) -> int:
    """Dlaždice, která se vejde do zvolené grafiky. 8 GB je výchozí."""
    return GPU_TILES.get(str(preset), GPU_TILES["8"])


def tile_vram_bytes(tile: int) -> float:
    tile = max(128, min(512, int(tile)))
    return (0.75 + (tile / 256) ** 2 * 3.3) * 1024**3


def tile_cpu_bytes(tile: int) -> float:
    tile = max(128, min(512, int(tile)))
    input_rgb = tile * tile * 3
    output_float = (tile * 4) * (tile * 4) * 3 * 4
    return input_rgb + output_float


def gpu_hint(vram_bytes: float) -> str:
    gb = vram_bytes / 1024**3
    if gb <= 3.2:
        return "4 GB grafika"
    if gb <= 5.5:
        return "6 GB grafika"
    if gb <= 9.0:
        return "8 GB grafika"
    if gb <= 13.0:
        return "12 GB grafika"
    return "16 GB+ grafika"


def format_bytes(count: float) -> str:
    if count >= 1024**3:
        value = count / 1024**3
        text = f"{value:.1f} GB" if value < 10 else f"{value:.0f} GB"
        return text.replace(".", ",")
    if count >= 1024**2:
        value = count / 1024**2
        text = f"{value:.0f} MB" if value >= 10 else f"{value:.1f} MB"
        return text.replace(".", ",")
    return f"{max(1, count / 1024):.0f} kB"


def output_ram_bytes(width: int, height: int, scale: int) -> float:
    scale = max(1, int(scale))
    out_w = width * scale
    out_h = height * scale
    out = out_w * out_h * 3
    mid = width * 4 * height * 4 * 3 if scale in (8, 16) else 0
    held_mid = min(mid, _RAM_OUTPUT) if mid else 0
    held_out = min(out, _RAM_OUTPUT)
    return held_mid + held_out + _BAND_BUDGET


def output_disk_bytes(width: int, height: int, scale: int) -> float:
    return width * scale * height * scale * 3 / 12.0


def estimate_upscale_usage(tile: int, jobs: list[dict] | None = None) -> dict:
    vram = tile_vram_bytes(tile)
    cpu_tile = tile_cpu_bytes(tile)
    ram = cpu_tile + _BAND_BUDGET
    disk = 0.0
    biggest = None
    for job in jobs or []:
        width = int(job["width"])
        height = int(job["height"])
        scale = int(job["scale"])
        if scale <= 1:
            continue
        job_ram = output_ram_bytes(width, height, scale) + cpu_tile
        job_disk = output_disk_bytes(width, height, scale)
        ram = max(ram, job_ram)
        disk += job_disk
        area = width * scale * height * scale
        if biggest is None or area > biggest[0]:
            biggest = (area, width * scale, height * scale, scale, job.get("label") or "")
    result = {
        "vram_bytes": vram,
        "ram_bytes": ram,
        "disk_bytes": disk,
        "gpu_line": f"~{format_bytes(vram)} VRAM",
        "hint_line": gpu_hint(vram),
        "size_line": "",
        "ram_line": "RAM výstupu se dopočítá po Ortofoto na materiál",
    }
    if biggest is None:
        return result
    _area, out_w, out_h, _scale, label = biggest
    extra = f"{label} · " if label else ""
    result["size_line"] = f"{extra}{out_w}×{out_h} px"
    result["ram_line"] = f"RAM ~{format_bytes(ram)} · JPEG ~{format_bytes(disk)}"
    return result


def probe_image_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                width, height = struct.unpack(">II", head[16:24])
                if width > 0 and height > 0:
                    return int(width), int(height)
            if head[:2] != b"\xff\xd8":
                return None
            handle.seek(2)
            while True:
                marker = handle.read(4)
                if len(marker) < 4 or marker[0] != 0xFF:
                    return None
                kind = marker[1]
                length = int.from_bytes(marker[2:4], "big")
                if kind in (0xC0, 0xC1, 0xC2, 0xC3):
                    rest = handle.read(max(length - 2, 0))
                    if len(rest) < 5:
                        return None
                    height = int.from_bytes(rest[1:3], "big")
                    width = int.from_bytes(rest[3:5], "big")
                    if width > 0 and height > 0:
                        return int(width), int(height)
                    return None
                handle.seek(max(length - 2, 0), 1)
    except OSError:
        return None
    return None


def _job_image(item: dict, source: str) -> Path:
    current = Path(str(item.get("image") or ""))
    if source != "original":
        return current
    original = Path(str(item.get("original") or ""))
    if original.is_file():
        return original
    if current.suffix.lower() == ".png" and current.is_file():
        return current
    sibling = current.with_suffix(".png")
    if current.suffix and sibling.is_file():
        return sibling
    return current


def jobs_from_manifest(folder: Path, scales: dict[int, int], source: str = "image") -> list[dict]:
    manifest_path = folder / "layers.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    jobs = []
    for item in manifest.get("zones") or []:
        zone_id = int(item.get("id") or 0)
        path = _job_image(item, source)
        size = probe_image_size(path)
        if size is None:
            continue
        width, height = size
        scale = int(scales.get(zone_id, 1))
        part = int(item.get("part") or 0)
        parts = int(item.get("parts") or 1)
        label = f"Zóna {zone_id}" if parts <= 1 else f"Zóna {zone_id} díl {part + 1}/{parts}"
        jobs.append({"width": width, "height": height, "scale": scale, "label": label})
    return jobs

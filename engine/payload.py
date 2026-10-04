from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def assemble_payload(mesh: dict, layers: list[dict], driveline: np.ndarray) -> dict:
    vertices = np.asarray(mesh["vertices"], dtype=np.float32)
    faces = np.asarray(mesh["faces"], dtype=np.int32)
    drive = np.asarray(driveline, dtype=np.float32).reshape(-1, 3)
    packed = []
    for layer in layers:
        uv = np.asarray(layer["uv"], dtype=np.float32)
        if uv.size and (float(uv.min()) < -1e-4 or float(uv.max()) > 1 + 1e-4):
            raise ValueError("UV je mimo rozsah 0 až 1.")
        packed.append(
            {
                "zone_id": int(layer["zone_id"]),
                "faces": np.asarray(layer["faces"], dtype=np.int32),
                "rows": np.asarray(layer.get("rows", []), dtype=np.int32),
                "uv": uv,
                "image": layer.get("image", ""),
            }
        )
    return {"vertices": vertices, "faces": faces, "layers": packed, "driveline": drive}


def write_layers(folder: Path, layers: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    arrays = {}
    manifest = []
    for index, layer in enumerate(layers):
        zone_id = int(layer["zone_id"])
        key = str(layer.get("key") or f"layer_{index}")
        arrays[f"{key}_faces"] = np.asarray(layer["faces"], dtype=np.int32)
        arrays[f"{key}_rows"] = np.asarray(layer["rows"], dtype=np.int32)
        arrays[f"{key}_uv"] = np.asarray(layer["uv"], dtype=np.float32)
        part = int(layer.get("part") or 0)
        parts = int(layer.get("parts") or 1)
        manifest.append(
            {
                "id": zone_id,
                "key": key,
                "name": layer.get("name") or (f"teren_zona_{zone_id}_{part}" if parts > 1 else f"teren_zona_{zone_id}"),
                "image": layer["image"],
                "original": layer.get("original") or layer["image"],
                "part": part,
                "parts": parts,
                "zoom": int(layer.get("zoom") or 0) or None,
            }
        )
    if not arrays:
        raise ValueError("Textura nemá žádnou zónu.")
    np.savez_compressed(folder / "layers.npz", **arrays)
    (folder / "layers.json").write_text(json.dumps({"zones": manifest}, ensure_ascii=False), encoding="utf-8")


def save_driveline(folder: Path, lines: list[np.ndarray]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    arrays = {"count": np.array([len(lines)], dtype=np.int32)}
    for index, line in enumerate(lines):
        arrays[f"line_{index}"] = np.asarray(line, dtype=np.float32)
    np.savez_compressed(folder / "driveline.npz", **arrays)

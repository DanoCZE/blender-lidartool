"""Načtení Real-ESRGAN x4plus. Volá se jen z workeru ve venv."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from blender_lidartool.engine.upscale import GpuOutOfMemory


def load_upscaler(weights: Path):
    try:
        import torch

        from blender_lidartool.engine.rrdb import RRDBNet
    except ModuleNotFoundError as exc:
        if "torch" not in str(exc).lower():
            raise
        raise ValueError("Ve workeru chybí PyTorch. V panelu Terén klikněte Nainstalovat AI model.") from exc

    if not weights.is_file():
        raise ValueError("Chybí váhy RealESRGAN_x4plus.pth. Nejdřív nainstalujte AI model.")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda":
        raise ValueError("PyTorch nevidí grafiku NVIDIA. Zkontrolujte ovladač a instalaci AI modelu.")
    network = RRDBNet()
    state = torch.load(weights, map_location="cpu", weights_only=False)
    if isinstance(state, dict) and "params_ema" in state:
        state = state["params_ema"]
    elif isinstance(state, dict) and "params" in state:
        state = state["params"]
    cleaned = {}
    for key, value in state.items():
        cleaned[key[7:] if key.startswith("module.") else key] = value
    network.load_state_dict(cleaned, strict=True)
    network.eval().to(device).half()

    def upscale_x4(image: np.ndarray) -> np.ndarray:
        tensor = torch.from_numpy(np.ascontiguousarray(image))
        tensor = tensor.permute(2, 0, 1).unsqueeze(0).to(device=device, dtype=torch.float16).div_(255.0)
        try:
            with torch.no_grad():
                output = network(tensor)
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                torch.cuda.empty_cache()
                raise GpuOutOfMemory(str(exc)) from exc
            raise
        array = output.clamp_(0, 1).float().squeeze(0).permute(1, 2, 0).cpu().numpy()
        del tensor, output
        return np.clip(np.rint(array * 255.0), 0, 255).astype(np.uint8)

    return upscale_x4

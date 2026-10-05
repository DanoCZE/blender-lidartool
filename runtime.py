from __future__ import annotations

import locale
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import bpy


def config_root() -> Path:
    base = bpy.utils.user_resource("CONFIG")
    if not base:
        base = str(Path.home() / ".blender_lidartool")
    path = Path(base) / "blender_lidartool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_cache() -> Path:
    path = config_root() / "cache"
    for name in ("DEM", "ORTHO", "OUTPUT", "TEMP"):
        (path / name).mkdir(parents=True, exist_ok=True)
    return path


def venv_dir() -> Path:
    return config_root() / "venv"


def venv_python() -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    return venv_dir() / scripts / name


def venv_ready() -> bool:
    return venv_python().is_file() and (venv_dir() / ".lidar_ready").is_file()


WEIGHTS_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
TORCH_INDEX = "https://download.pytorch.org/whl/cu124"


WEIGHTS_NAME = "RealESRGAN_x4plus.pth"
LEGACY_WEIGHTS_NAME = "4xSPANkendata.pth"
TORCH_DIR_NAME = "lidar_torch"
AI_MARKER = ".lidar_ai"


def _clean_dir(raw: str) -> Path | None:
    text = (raw or "").strip().strip('"')
    if text.startswith("//"):
        text = bpy.path.abspath(text)
    text = text.rstrip("/\\")
    if not text:
        return None
    return Path(text)


def _same_dir(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _unique_dirs(candidates: list[Path | None]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def set_ai_dir(raw: str) -> Path:
    """Zapamatuje složku instalace. Shodná s výchozí složkou nastavení se neukládá."""
    chosen = _clean_dir(raw)
    marker = config_root() / "ai_dir.txt"
    default = config_root()
    if chosen is None or _same_dir(chosen, default):
        if marker.is_file():
            marker.unlink()
        return default
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(chosen), encoding="utf-8")
    return chosen


def ai_dir() -> Path:
    marker = config_root() / "ai_dir.txt"
    if marker.is_file():
        chosen = _clean_dir(marker.read_text(encoding="utf-8"))
        if chosen is not None:
            return chosen
    return config_root()


def ai_dir_text() -> str:
    return str(ai_dir()) + os.sep


def install_record_path() -> Path:
    return config_root() / "ai_installed.txt"


def recorded_install_dir() -> Path | None:
    marker = install_record_path()
    if not marker.is_file():
        return None
    return _clean_dir(marker.read_text(encoding="utf-8"))


def ai_locations() -> list[Path]:
    return _unique_dirs([recorded_install_dir(), ai_dir(), config_root()])


def weights_path() -> Path:
    return ai_dir() / WEIGHTS_NAME


def venv_site_packages(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Lib" / "site-packages"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return venv / "lib" / version / "site-packages"


def torch_link_path(venv: Path) -> Path:
    return venv_site_packages(venv) / "lidar-ai-torch.pth"


def _torch_package_ready(directory: Path) -> bool:
    return (directory / TORCH_DIR_NAME / "torch" / "__init__.py").is_file()


def _torch_payload(directory: Path) -> bool:
    return (directory / TORCH_DIR_NAME).exists()


def ai_ready() -> bool:
    if not venv_ready() or not weights_path().is_file():
        return False
    directory = ai_dir()
    if (directory / AI_MARKER).is_file() and _torch_package_ready(directory):
        link = torch_link_path(venv_dir())
        if not link.is_file():
            try:
                _write_torch_link(venv_dir(), directory / TORCH_DIR_NAME)
            except OSError:
                return False
        return link.is_file()
    legacy_weights = config_root() / WEIGHTS_NAME
    return (venv_dir() / AI_MARKER).is_file() and _same_dir(weights_path(), legacy_weights)


def ai_present() -> bool:
    if ai_ready() or (venv_dir() / AI_MARKER).is_file():
        return True
    try:
        if torch_link_path(venv_dir()).is_file():
            return True
    except OSError:
        pass
    for directory in ai_locations():
        if (directory / WEIGHTS_NAME).is_file() or (directory / AI_MARKER).is_file() or _torch_payload(directory):
            return True
    return False


def blender_python() -> Path:
    prefix = Path(sys.prefix)
    names = ["python.exe", "python3", "python"]
    for name in names:
        candidate = prefix / "bin" / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("V instalaci Blenderu se nenašel Python. Očekávám ho v python/bin/python.exe.")


def addon_root() -> Path:
    return Path(__file__).resolve().parent


def pythonpath() -> str:
    parent = str(addon_root().parent)
    current = os.environ.get("PYTHONPATH", "")
    if not current:
        return parent
    return parent + os.pathsep + current


def worker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath()
    for key in (
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
        "GDAL_DATA",
        "GDAL_DRIVER_PATH",
        "PROJ_LIB",
        "PROJ_DATA",
        "PROJ_NETWORK",
    ):
        env.pop(key, None)
    return env


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


_CURRENT_PROCESS: subprocess.Popen | None = None
_INSTALL_STATUS = ""


def install_status() -> str:
    return _INSTALL_STATUS


def _set_install_status(text: str) -> None:
    global _INSTALL_STATUS
    _INSTALL_STATUS = text


def _fmt_size(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f} GB".replace(".", ",")
    if count >= 1_000_000:
        return f"{count / 1_000_000:.0f} MB"
    if count >= 1_000:
        return f"{count / 1_000:.0f} kB"
    return f"{count} B"


def _fmt_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 5400:
        return f"{seconds / 3600:.1f} h".replace(".", ",")
    if seconds >= 90:
        return f"{max(1, round(seconds / 60))} min"
    return f"{max(1, seconds)} s"


class _PipWatch:
    """Z průběhu pipu složí krátký text: balíček, procenta a odhad času."""

    def __init__(self) -> None:
        self.package = ""
        self.t0 = 0.0
        self.b0 = 0

    def feed(self, line: str) -> str | None:
        if line.startswith("Collecting "):
            self.package = line.split()[1]
            self.t0 = 0.0
            return f"hledám {self.package}"
        if line.startswith("Downloading "):
            body = line[len("Downloading ") :]
            size = ""
            found = re.search(r"\(([^)]+)\)\s*$", body)
            if found:
                size = found.group(1)
                body = body[: found.start()].strip()
            self.package = body.split("-", 1)[0]
            self.t0 = 0.0
            return f"stahuji {self.package} {size}".strip()
        if line.startswith("Installing collected"):
            return "rozbaluji stažené balíčky"
        if line.startswith("Successfully installed"):
            return "nainstalováno"
        if line.startswith("Requirement already satisfied"):
            return "už je nainstalované"
        found = re.match(r"Progress (\d+) of (\d+)$", line)
        if not found:
            return None
        current = int(found.group(1))
        total = int(found.group(2))
        if total <= 0:
            return None
        now = time.monotonic()
        if self.t0 <= 0:
            self.t0 = now
            self.b0 = current
        name = self.package or "balíček"
        percent = current * 100 // total
        text = f"{name} · {percent} % · {_fmt_size(current)}/{_fmt_size(total)}"
        elapsed = now - self.t0
        done = current - self.b0
        if elapsed >= 1 and done > 0 and current < total:
            text += f" · zbývá {_fmt_eta((total - current) * elapsed / done)}"
        return text


def watch_process(process: subprocess.Popen | None) -> None:
    global _CURRENT_PROCESS
    _CURRENT_PROCESS = process


def cancel_current() -> None:
    process = _CURRENT_PROCESS
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_no_window(),
            check=False,
        )
    else:
        process.kill()


def _run(command: list[str], log: Path, on_line=None) -> None:
    global _CURRENT_PROCESS
    with log.open("ab") as handle:
        env = worker_env()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=_no_window(),
        )
        _CURRENT_PROCESS = process
        pending = b""
        stream = process.stdout
        assert stream is not None
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()
            pending += chunk
            parts = re.split(br"[\r\n]+", pending)
            pending = parts.pop() if parts else b""
            if on_line is None:
                continue
            for part in parts:
                text = part.decode("utf-8", errors="replace").strip()
                if text:
                    on_line(text)
        if pending and on_line is not None:
            text = pending.decode("utf-8", errors="replace").strip()
            if text:
                on_line(text)
        return_code = process.wait()
        if _CURRENT_PROCESS is process:
            _CURRENT_PROCESS = None
    if return_code != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(tail.strip() or "Příkaz selhal.")


def _note_pip(prefix: str):
    watch = _PipWatch()

    def on_line(line: str) -> None:
        extra = watch.feed(line)
        if extra:
            _set_install_status(f"{prefix} · {extra}")

    return on_line


def _download(url: str, dest: Path, title: str) -> None:
    started = time.monotonic()
    last = 0.0

    def hook(block: int, block_size: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if block > 1 and now - last < 0.3:
            return
        last = now
        got = block * block_size
        if total and total > 0:
            got = min(got, total)
            elapsed = max(now - started, 0.001)
            eta = f" · zbývá {_fmt_eta((total - got) * elapsed / got)}" if got > 50_000 and got < total else ""
            _set_install_status(f"{title} · {got * 100 // total} % · {_fmt_size(got)}/{_fmt_size(total)}{eta}")
            return
        _set_install_status(f"{title} · {_fmt_size(got)}")

    urllib.request.urlretrieve(url, dest, reporthook=hook)


def _ensure_pip(python: Path, get_pip: Path, log: Path) -> None:
    probe = subprocess.run(
        [str(python), "-c", "import pip"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=worker_env(),
        creationflags=_no_window(),
        check=False,
    )
    if probe.returncode == 0:
        return
    urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", get_pip)
    _run([str(python), str(get_pip)], log)


def prepare_venv(log: Path, python: Path, target: Path, requirements: Path, get_pip: Path) -> None:
    """Instalace běží mimo hlavní vlákno, proto sem nepatří volání bpy."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    legacy_ai = config_root() / ".lidar_ai"
    if legacy_ai.exists():
        legacy_ai.unlink()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    try:
        _run([str(python), "-m", "venv", str(target)], log)
    except RuntimeError:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        _run([str(python), "-m", "venv", "--without-pip", str(target)], log)
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    venv_py = target / scripts / name
    _ensure_pip(venv_py, get_pip, log)
    _run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"], log)
    _run([str(venv_py), "-m", "pip", "install", "-r", str(requirements)], log)
    (target / ".lidar_ready").write_text("ok\n", encoding="utf-8")
    relink_installed_torch(target, log.parent)


def _write_torch_link(venv: Path, target: Path) -> None:
    link = torch_link_path(venv)
    link.parent.mkdir(parents=True, exist_ok=True)
    encoding = locale.getpreferredencoding(False) or "utf-8"
    try:
        payload = (str(target.resolve()) + "\n").encode(encoding)
    except UnicodeEncodeError as exc:
        raise RuntimeError("Cesta ke složce AI modelu obsahuje znaky, které nejde zapsat do propojení PyTorchu.") from exc
    link.write_bytes(payload)


def relink_installed_torch(venv: Path, root: Path) -> None:
    """Po novém venv znovu napojí PyTorch ve zvolené složce, pokud tam už je."""
    record = root / "ai_installed.txt"
    directory = _clean_dir(record.read_text(encoding="utf-8")) if record.is_file() else None
    if directory is None:
        saved = root / "ai_dir.txt"
        directory = _clean_dir(saved.read_text(encoding="utf-8")) if saved.is_file() else root
    packages = directory / TORCH_DIR_NAME
    if (packages / "torch" / "__init__.py").is_file() or (packages / "torch").is_dir():
        _write_torch_link(venv, packages)


def _remove_payload_dir(directory: Path) -> None:
    for name in (WEIGHTS_NAME, LEGACY_WEIGHTS_NAME):
        weights = directory / name
        if weights.is_file() or weights.is_symlink():
            weights.unlink()
    marker = directory / AI_MARKER
    if marker.is_file() or marker.is_symlink():
        marker.unlink()
    packages = directory / TORCH_DIR_NAME
    if packages.is_symlink():
        packages.unlink()
    elif packages.is_dir():
        shutil.rmtree(packages)


def remove_ai_files(directories: list[Path], venv: Path, record: Path) -> None:
    link = torch_link_path(venv)
    if link.is_file() or link.is_symlink():
        link.unlink()
    for directory in directories:
        _remove_payload_dir(directory)
    legacy = venv / AI_MARKER
    if legacy.is_file() or legacy.is_symlink():
        legacy.unlink()
    if record.is_file() or record.is_symlink():
        record.unlink()


def _venv_ai_packages(python: Path) -> list[str]:
    probe = subprocess.run(
        [str(python), "-m", "pip", "freeze"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=worker_env(),
        creationflags=_no_window(),
        check=False,
    )
    if probe.returncode != 0:
        return []
    names: list[str] = []
    for line in (probe.stdout or b"").decode("utf-8", errors="replace").splitlines():
        name = line.split("==", 1)[0].split(" @ ", 1)[0].strip()
        if not name or name.startswith("-"):
            continue
        folded = name.lower().replace("_", "-")
        if folded in {"torch", "torchvision", "torchaudio"} or folded.startswith("nvidia-"):
            names.append(name)
    return names


def _probe_torch(python: Path) -> None:
    probe = subprocess.run(
        [str(python), "-c", "import torch; raise SystemExit(0 if torch.cuda.is_available() else 2)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=worker_env(),
        creationflags=_no_window(),
        check=False,
    )
    if probe.returncode == 2:
        raise RuntimeError("PyTorch nevidí grafiku NVIDIA. Zkontrolujte ovladač a spusťte instalaci znovu.")
    if probe.returncode != 0:
        tail = (probe.stdout or b"").decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(tail.strip() or "PyTorch se nepodařilo naimportovat.")


def install_ai(
    log: Path,
    python: Path,
    weights: Path,
    venv: Path | None = None,
    record: Path | None = None,
    cleanup_dirs: list[Path] | None = None,
) -> None:
    """Do zvolené složky nainstaluje CUDA PyTorch a stáhne váhy. Venv už musí existovat."""
    if not python.is_file():
        raise RuntimeError("Nejdřív připravte prostředí addonu.")
    if venv is None:
        venv = venv_dir()
    if record is None:
        record = install_record_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    directory = weights.parent
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / TORCH_DIR_NAME
    previous = _clean_dir(record.read_text(encoding="utf-8")) if record.is_file() else None
    link = torch_link_path(venv)
    if _torch_package_ready(directory):
        _set_install_status("1/4 · PyTorch už je ve složce")
        if not link.is_file():
            _write_torch_link(venv, target)
    else:
        if link.is_file() or link.is_symlink():
            link.unlink()
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        _set_install_status("1/4 · aktualizuji pip")
        _run([str(python), "-m", "pip", "install", "--upgrade", "pip"], log, _note_pip("1/4 · pip"))
        _set_install_status("2/4 · stahuji PyTorch pro CUDA, to je největší část")
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--no-cache-dir",
                "--progress-bar",
                "raw",
                "--target",
                str(target),
                "torch",
                "--index-url",
                TORCH_INDEX,
            ],
            log,
            _note_pip("2/4 · PyTorch"),
        )
        if not (target / "torch" / "__init__.py").is_file():
            raise RuntimeError("PyTorch se do zvolené složky nenainstaloval.")
        names = _venv_ai_packages(python)
        if names:
            _set_install_status("2/4 · odstraňuji starší PyTorch z prostředí")
            _run([str(python), "-m", "pip", "uninstall", "-y", *names], log)
        _set_install_status("3/4 · propojuji PyTorch")
        _write_torch_link(venv, target)
    if not weights.is_file() or weights.stat().st_size < 1_000_000:
        _download(WEIGHTS_URL, weights, "3/4 · váhy Real-ESRGAN")
    else:
        _set_install_status("3/4 · váhy Real-ESRGAN už jsou na disku")
    legacy = directory / LEGACY_WEIGHTS_NAME
    if legacy.is_file() and legacy.resolve() != weights.resolve():
        legacy.unlink()
    _set_install_status("4/4 · kontroluji, že grafika vidí CUDA")
    _probe_torch(python)
    _set_install_status("4/4 · uklízím starší instalaci")
    stale = list(cleanup_dirs or [])
    if previous is not None:
        stale.append(previous)
    for old in _unique_dirs(stale):
        if not _same_dir(old, directory):
            _remove_payload_dir(old)
    (directory / AI_MARKER).write_text("ok\n", encoding="utf-8")
    legacy = venv / AI_MARKER
    if legacy.is_file() or legacy.is_symlink():
        legacy.unlink()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(str(directory), encoding="utf-8")


def uninstall_ai(log: Path, python: Path, directories: list[Path], venv: Path, record: Path) -> None:
    """Smaže váhy, PyTorch ve zvolené složce i starší PyTorch z venv."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    link = torch_link_path(venv)
    if link.is_file() or link.is_symlink():
        link.unlink()
    if python.is_file():
        names = _venv_ai_packages(python)
        if names:
            _run([str(python), "-m", "pip", "uninstall", "-y", *names], log)
    remove_ai_files(directories, venv, record)


def start_worker(args: list[str], log: Path) -> tuple[subprocess.Popen, object]:
    if not venv_ready():
        raise RuntimeError("Nejdřív připravte prostředí addonu.")
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("wb")
    process = subprocess.Popen(
        [str(venv_python()), "-m", "blender_lidartool.worker", *args],
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=worker_env(),
        creationflags=_no_window(),
    )
    watch_process(process)
    return process, handle

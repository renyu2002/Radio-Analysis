"""Windows 下的 CUDA 运行库检测与 DLL 加载。

ctranslate2（faster-whisper 的推理后端）在 Windows 上推理 CUDA 需要
CUDA 12 的 cuBLAS 与 cuDNN 9 动态库。用 pip 安装的 ``nvidia-*`` 包会把 DLL
放在 ``<venv>/Lib/site-packages/nvidia/<lib>/bin``，但 Python 进程默认不会
搜索这些目录。本模块在导入 ctranslate2 之前显式调用
``os.add_dll_directory()``，并把目录加入 ``PATH``（兼容仍使用旧式搜索路径的
库），随后用真实加载测试判断 CUDA 是否可用。

对外接口：
- :func:`configure` 加载 DLL 搜索目录（幂等）；
- :func:`status` 返回 CUDA 可用性详情（缓存，可 refresh）；
- :func:`cuda_available` 供 asr.detect_device 快速判断；
- :func:`invalidate` 清空缓存（便于安装依赖后重新检测）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import sysconfig
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 允许用户显式追加 DLL 目录（分号分隔），便于自定义 CUDA 安装位置
_ENV_DLL_DIRS = "VANALYZE_CUDA_DLL_DIRS"
_TRUE_VALUES = {"1", "true", "yes", "on"}

_LOCK = threading.RLock()
_STATE: Optional[Dict[str, object]] = None
_DLL_DIR_HANDLES: List[object] = []
_LOADED_DLLS: List[object] = []
_CONFIGURED = False


# ---------------------------------------------------------------------------
# 目录发现
# ---------------------------------------------------------------------------
def _site_packages_roots() -> List[Path]:
    """返回可能包含 nvidia / torch 包的 site-packages 目录（去重）。"""
    roots: List[Path] = []

    def _add(p: Optional[str | Path]) -> None:
        if not p:
            return
        try:
            path = Path(p).resolve()
        except OSError:
            return
        if path.is_dir() and path not in roots:
            roots.append(path)

    for prefix in {sys.prefix, sys.base_prefix}:
        _add(Path(prefix) / "Lib" / "site-packages")
    try:
        paths = sysconfig.get_paths()
        for key in ("purelib", "platlib"):
            _add(paths.get(key))
    except Exception:  # pragma: no cover - 极端环境
        pass
    try:
        import site as _site

        for p in _site.getsitepackages():
            _add(p)
        _add(_site.getusersitepackages())
    except Exception:  # pragma: no cover
        pass
    return roots


def candidate_dll_dirs() -> List[str]:
    """列出所有需要加入 DLL 搜索路径的目录（存在的才返回）。"""
    dirs: List[Path] = []
    for root in _site_packages_roots():
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            try:
                libs = sorted(p for p in nvidia.iterdir() if p.is_dir())
            except OSError:
                libs = []
            for lib in libs:
                for sub in ("bin", "lib", "lib/x64"):
                    d = lib / sub
                    if d.is_dir():
                        dirs.append(d)
        # 有些环境（例如装了 torch 的用户）会把 cuDNN/cuBLAS 放在 torch/lib
        torch_lib = root / "torch" / "lib"
        if torch_lib.is_dir():
            dirs.append(torch_lib)

    raw = os.environ.get(_ENV_DLL_DIRS, "")
    for item in raw.split(os.pathsep):
        item = item.strip().strip('"')
        if not item:
            continue
        p = Path(item)
        if p.is_dir():
            dirs.append(p)

    seen: List[str] = []
    for d in dirs:
        try:
            s = str(d.resolve())
        except OSError:
            continue
        if s not in seen:
            seen.append(s)
    return seen


def configure() -> List[str]:
    """把候选目录加入 DLL 搜索路径（幂等），返回已配置的目录列表。"""
    global _CONFIGURED
    with _LOCK:
        dirs = candidate_dll_dirs()
        if _CONFIGURED:
            return dirs
        for d in dirs:
            try:
                handle = os.add_dll_directory(d)
                _DLL_DIR_HANDLES.append(handle)
            except (AttributeError, OSError) as exc:  # pragma: no cover
                logger.debug("add_dll_directory 失败 %s: %s", d, exc)
            # 兼容仍使用 PATH 搜索的库（ctranslate2 部分版本）
            current = os.environ.get("PATH", "")
            if d not in current.split(os.pathsep):
                os.environ["PATH"] = d + os.pathsep + current
        _CONFIGURED = True
        if dirs:
            logger.debug("CUDA DLL 搜索目录：%s", dirs)
        return dirs


# ---------------------------------------------------------------------------
# DLL 加载测试
# ---------------------------------------------------------------------------
def _search_dirs() -> List[str]:
    return candidate_dll_dirs()


def _find_matching_dlls(patterns: List[str]) -> List[str]:
    """在候选目录里按文件名模式查找 DLL，返回完整路径列表。"""
    found: List[str] = []
    dirs = _search_dirs()
    for pattern in patterns:
        for d in dirs:
            try:
                for p in sorted(Path(d).glob(pattern)):
                    if p.is_file():
                        found.append(str(p))
            except OSError:
                continue
    # 再去 PATH 里找一层（系统级 CUDA 安装）
    for pattern in patterns:
        for d in os.environ.get("PATH", "").split(os.pathsep):
            d = d.strip()
            if not d:
                continue
            try:
                for p in sorted(Path(d).glob(pattern)):
                    if p.is_file():
                        found.append(str(p))
            except OSError:
                continue
    return found


def _try_load_dll(patterns: List[str]) -> bool:
    """尝试加载第一个能加载的 DLL；成功返回 True。"""
    candidates = _find_matching_dlls(patterns)
    if not candidates:
        return False
    for path in candidates:
        try:
            handle = ctypes.WinDLL(path)  # type: ignore[attr-defined]
            _LOADED_DLLS.append(handle)
            return True
        except OSError as exc:
            logger.debug("加载 DLL 失败 %s: %s", path, exc)
    return False


def _probe() -> Dict[str, object]:
    configure()

    state: Dict[str, object] = {
        "cuda_devices": 0,
        "cudnn": False,
        "cublas": False,
        "ok": False,
        "compute_types": [],
        "dll_dirs": candidate_dll_dirs(),
        "error": None,
    }
    reasons: List[str] = []

    try:
        import ctranslate2  # noqa: F401  延迟导入，避免无谓开销

        try:
            state["cuda_devices"] = int(ctranslate2.get_cuda_device_count())
        except Exception as exc:  # pragma: no cover - 驱动异常
            state["error"] = f"无法检测 CUDA 设备：{exc}"
            reasons.append("cuda_devices")
        if int(state["cuda_devices"]) > 0:
            try:
                state["compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
            except Exception as exc:
                logger.debug("get_supported_compute_types(cuda) 失败：%s", exc)
    except Exception as exc:  # pragma: no cover
        state["error"] = f"ctranslate2 导入失败：{exc}"
        reasons.append("ctranslate2")

    # cuDNN 9 / cuBLAS 12：名字按 pip wheel 与官方安装包常见命名排列
    state["cudnn"] = _try_load_dll(
        ["cudnn64_9.dll", "cudnn_ops_infer64_9.dll", "cudnn_ops64_9.dll", "cudnn64*.dll"]
    )
    state["cublas"] = _try_load_dll(["cublas64_12.dll", "cublasLt64_12.dll"])

    if int(state["cuda_devices"]) <= 0:
        reasons.append("no_cuda_device")
    if not state["cudnn"]:
        reasons.append("no_cudnn")
    if not state["cublas"]:
        reasons.append("no_cublas")
    # 明确要求 CPU 时报告不可用
    if os.environ.get("VANALYZE_FORCE_CPU", "").strip().lower() in _TRUE_VALUES:
        reasons.append("force_cpu")

    state["ok"] = (
        int(state["cuda_devices"]) > 0
        and bool(state["cudnn"])
        and bool(state["cublas"])
        and "force_cpu" not in reasons
    )
    state["reasons"] = reasons
    return state


def status(refresh: bool = False) -> Dict[str, object]:
    """返回 CUDA 状态字典（进程内缓存；refresh=True 强制重新检测）。"""
    global _STATE
    with _LOCK:
        if _STATE is None or refresh:
            try:
                _STATE = _probe()
            except Exception as exc:  # pragma: no cover - 兜底，任何异常都视为不可用
                _STATE = {
                    "cuda_devices": 0,
                    "cudnn": False,
                    "cublas": False,
                    "ok": False,
                    "compute_types": [],
                    "dll_dirs": [],
                    "error": f"GPU 检测异常：{exc}",
                    "reasons": ["probe_error"],
                }
        return dict(_STATE)


def cuda_available() -> bool:
    """CUDA 是否真正可用（驱动 + cuDNN + cuBLAS 均可加载）。"""
    try:
        return bool(status()["ok"])
    except Exception:  # pragma: no cover
        return False


def invalidate() -> None:
    """清空缓存（测试或安装依赖后调用）。"""
    global _STATE
    with _LOCK:
        _STATE = None


def log_summary() -> None:
    """把 GPU 状态写入日志，不做任何耗时操作。"""
    try:
        st = status()
    except Exception:  # pragma: no cover
        return
    if st.get("ok"):
        logger.info(
            "CUDA 可用：设备数 %s，compute_types=%s",
            st.get("cuda_devices"),
            st.get("compute_types"),
        )
    else:
        logger.warning(
            "CUDA 不可用（%s），将使用 CPU 推理。DLL 目录：%s",
            ", ".join(st.get("reasons") or []) or "unknown",
            st.get("dll_dirs"),
        )

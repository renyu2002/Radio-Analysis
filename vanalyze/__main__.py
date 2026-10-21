"""``python -m vanalyze`` 启动入口。

用法::

    python -m vanalyze                      # 默认 serve，127.0.0.1:8765
    python -m vanalyze serve --port 8766
    python -m vanalyze --host 127.0.0.1 --port 8765

启动时打印访问地址与 DeepSeek API Key 配置状态。Windows 控制台强制 UTF-8，
避免中文输出乱码（与 ``PYTHONUTF8=1`` 等效）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import config, __version__


def _setup_console() -> None:
    """尽量把 stdout/stderr 切到 UTF-8，兼容 Windows GBK 控制台。"""
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 控制台不支持时忽略
            pass


def _print_banner(host: str, port: int) -> None:
    try:
        from .settings import load_settings

        settings = load_settings()
        key_state = "已配置" if settings.deepseek_api_key.strip() else "未配置（请在网页「设置」中填写）"
        model = settings.whisper_model
        device = settings.whisper_device
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻止启动
        key_state = f"读取配置失败（{exc}）"
        model, device = "未知", "未知"
    url = f"http://{host}:{port}"
    lines = [
        "",
        "═" * 56,
        f"  视频分析 · DeepSeek  v{__version__}",
        "═" * 56,
        f"  访问地址      : {url}",
        f"  DeepSeek Key  : {key_state}",
        f"  Whisper 模型  : {model}（设备：{device}）",
        f"  数据目录      : {config.DATA_DIR}",
        "─" * 56,
        "  按 Ctrl+C 停止服务",
        "═" * 56,
        "",
    ]
    print("\n".join(lines), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vanalyze",
        description="视频分析 · DeepSeek —— 本地视频转写、总结与问答服务",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve"],
        help="子命令（默认 serve）",
    )
    parser.add_argument(
        "--host",
        default=config.DEFAULT_HOST,
        help=f"监听地址（默认 {config.DEFAULT_HOST}）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.DEFAULT_PORT,
        help=f"监听端口（默认 {config.DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别（默认 info）",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (1 <= int(args.port) <= 65535):
        print(f"端口无效：{args.port}（应在 1-65535 之间）", file=sys.stderr, flush=True)
        return 2

    config.ensure_dirs()
    try:
        from .settings import load_settings

        load_settings()  # 预读一次，尽早暴露配置损坏问题
    except Exception:  # noqa: BLE001 - pragma: no cover
        pass

    _print_banner(args.host, int(args.port))
    try:
        import uvicorn
    except Exception as exc:  # noqa: BLE001
        print(
            f"启动失败：未安装 uvicorn（{exc}）。请先执行：pip install -r requirements.txt",
            file=sys.stderr,
            flush=True,
        )
        return 1

    try:
        from .server.app import app as fastapi_app

        fastapi_app.state.port = int(args.port)
    except Exception as exc:  # noqa: BLE001
        print(f"启动失败：无法加载服务端应用（{exc}）", file=sys.stderr, flush=True)
        return 1

    try:
        uvicorn.run(
            fastapi_app,
            host=str(args.host),
            port=int(args.port),
            log_level=str(args.log_level),
        )
    except KeyboardInterrupt:  # pragma: no cover - 正常 Ctrl+C
        pass
    finally:
        print("\n服务已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

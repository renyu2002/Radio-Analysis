"""项目路径与常量。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("VANALYZE_DATA_DIR") or (PROJECT_ROOT / "data"))
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "app.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
WEB_DIR = PROJECT_ROOT / "web"
LOG_DIR = DATA_DIR / "logs"

APP_NAME = "视频分析 · DeepSeek"
APP_SLUG = "vanalyze"
VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def ensure_dirs() -> None:
    """确保运行期目录存在。"""
    for p in (DATA_DIR, JOBS_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)

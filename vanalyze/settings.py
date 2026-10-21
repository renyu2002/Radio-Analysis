"""运行配置：data/settings.json + 环境变量（环境变量优先级更高）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from . import config
from .models import SettingsUpdate, SettingsView


class Settings(BaseModel):
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_reasoner_model: str = "deepseek-reasoner"
    summary_model: str = ""            # 空=用 deepseek_model

    whisper_model: str = "large-v3"
    whisper_device: str = "auto"       # auto|cuda|cpu
    whisper_compute_type: str = "auto" # auto|float16|int8_float16|int8
    whisper_language: str = "auto"     # auto|zh|en|...
    max_frames: int = 100
    frame_interval: float = 20.0

    vision_enabled: bool = False
    vision_base_url: str = ""
    vision_model: str = ""
    vision_api_key: str = ""

    keep_media: bool = True
    port: int = config.DEFAULT_PORT

    def model_post_init(self, __context: Any) -> None:  # pragma: no cover
        pass

    # ---------- 派生 ----------
    @property
    def resolved_summary_model(self) -> str:
        return (self.summary_model or self.deepseek_model).strip()

    @property
    def has_deepseek_key(self) -> bool:
        return bool(self.deepseek_api_key.strip())

    def to_view(self) -> SettingsView:
        key = self.deepseek_api_key.strip()
        masked = ""
        if key:
            masked = key[:4] + "…" + key[-4:] if len(key) > 10 else "已设置"
        vkey = self.vision_api_key.strip()
        return SettingsView(
            deepseek_api_key_set=bool(key),
            deepseek_api_key_masked=masked,
            deepseek_base_url=self.deepseek_base_url,
            deepseek_model=self.deepseek_model,
            deepseek_reasoner_model=self.deepseek_reasoner_model,
            summary_model=self.summary_model,
            whisper_model=self.whisper_model,
            whisper_device=self.whisper_device,
            whisper_compute_type=self.whisper_compute_type,
            whisper_language=self.whisper_language,
            max_frames=self.max_frames,
            frame_interval=self.frame_interval,
            vision_enabled=self.vision_enabled,
            vision_base_url=self.vision_base_url,
            vision_model=self.vision_model,
            vision_api_key_set=bool(vkey),
            keep_media=self.keep_media,
            port=self.port,
            data_dir=str(config.DATA_DIR),
        )


_ENV_MAP: Dict[str, str] = {
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "DEEPSEEK_BASE_URL": "deepseek_base_url",
    "DEEPSEEK_MODEL": "deepseek_model",
    "VANALYZE_WHISPER_MODEL": "whisper_model",
    "VANALYZE_WHISPER_DEVICE": "whisper_device",
    "VANALYZE_WHISPER_COMPUTE_TYPE": "whisper_compute_type",
    "VANALYZE_WHISPER_LANGUAGE": "whisper_language",
    "VANALYZE_MAX_FRAMES": "max_frames",
    "VANALYZE_FRAME_INTERVAL": "frame_interval",
    "VANALYZE_VISION_BASE_URL": "vision_base_url",
    "VANALYZE_VISION_MODEL": "vision_model",
    "VANALYZE_VISION_API_KEY": "vision_api_key",
    "VANALYZE_PORT": "port",
}


def settings_path() -> Path:
    return config.SETTINGS_PATH


def load_settings() -> Settings:
    data: Dict[str, Any] = {}
    p = settings_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    # 兼容旧键名
    if isinstance(data, dict):
        for k in list(data.keys()):
            if k not in Settings.model_fields:
                data.pop(k, None)
    s = Settings(**data)
    # 环境变量覆盖
    for env_name, field in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            setattr(s, field, int(raw) if field in ("max_frames", "port") else float(raw) if field == "frame_interval" else raw)
        except (TypeError, ValueError):
            pass
    return s


def save_settings(s: Settings) -> None:
    config.ensure_dirs()
    tmp = settings_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(settings_path())


def update_settings(update: SettingsUpdate) -> Settings:
    s = load_settings()
    patch = update.model_dump(exclude_unset=True, exclude_none=True)
    for k, v in patch.items():
        setattr(s, k, v)
    save_settings(s)
    return s


def get_api_key(s: Optional[Settings] = None) -> str:
    if s is None:
        s = load_settings()
    return s.deepseek_api_key.strip()


_KEYS = ("deepseek_api_key", "vision_api_key")


def mask_secrets(d: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(d)
    for k in _KEYS:
        if out.get(k):
            out[k] = "***"
    return out

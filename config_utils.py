from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_VERSION = "3.8"
APP_DIR_NAME = "AudioCleanerPro"
SETTINGS_FILE_NAME = "audio_cleaner_settings.json"

DEFAULT_SETTINGS = {
    "theme": "system",
    "auto_save": False,
    "censor_mode": "meow",
    "noise_reduction": 0.5,
    "padding_ms": 100,
    "disable_censor": False,
    "processing_preset": "balanced",
}

VALID_THEMES = {"system", "light", "dark"}
VALID_CENSOR_MODES = {"beep", "meow", "off"}
VALID_PRESETS = {"fast", "balanced", "quality"}


def get_app_data_dir() -> Path:
    """Каталог для настроек и логов.

    В установленной версии это %LOCALAPPDATA%\\AudioCleanerPro, где у обычного
    пользователя есть права на запись (в отличие от Program Files).
    """
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def get_settings_path(settings_path: str | Path | None = None) -> Path:
    if settings_path is None:
        return get_app_data_dir() / SETTINGS_FILE_NAME
    path = Path(settings_path).expanduser()
    if not path.is_absolute():
        path = get_app_data_dir() / path
    return path


def _coerce_int(value: Any, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return minimum
    if maximum is not None and parsed > maximum:
        return maximum
    return parsed


def _coerce_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return round(parsed, 2)


def normalize_settings(raw_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = DEFAULT_SETTINGS.copy()
    if not isinstance(raw_settings, dict):
        return settings

    theme = raw_settings.get("theme")
    if isinstance(theme, str) and theme.strip().lower() in VALID_THEMES:
        settings["theme"] = theme.strip().lower()

    auto_save = raw_settings.get("auto_save")
    if isinstance(auto_save, bool):
        settings["auto_save"] = auto_save

    censor_mode = raw_settings.get("censor_mode")
    if isinstance(censor_mode, str) and censor_mode in VALID_CENSOR_MODES:
        settings["censor_mode"] = censor_mode

    settings["noise_reduction"] = _coerce_float(raw_settings.get("noise_reduction"), DEFAULT_SETTINGS["noise_reduction"])
    settings["padding_ms"] = _coerce_int(raw_settings.get("padding_ms"), DEFAULT_SETTINGS["padding_ms"], minimum=0)

    disable_censor = raw_settings.get("disable_censor")
    if isinstance(disable_censor, bool):
        settings["disable_censor"] = disable_censor

    preset = raw_settings.get("processing_preset")
    if isinstance(preset, str) and preset in VALID_PRESETS:
        settings["processing_preset"] = preset

    return settings


def _read_settings_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return normalize_settings(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_settings(settings_path: str | Path | None = None) -> dict[str, Any]:
    path = get_settings_path(settings_path)
    loaded = _read_settings_file(path)
    if loaded is not None:
        return loaded
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict[str, Any], settings_path: str | Path | None = None) -> dict[str, Any]:
    normalized = normalize_settings(settings)
    path = get_settings_path(settings_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass
    return normalized

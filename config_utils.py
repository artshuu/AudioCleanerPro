from __future__ import annotations

import json
from pathlib import Path
from typing import Any

APP_VERSION = "3.7"

DEFAULT_SETTINGS = {
    "theme": "system",
    "auto_save": False,
    "censor_mode": "meow",
    "noise_reduction": 0.5,
    "censor_chunk_size": 8000,
    "padding_ms": 100,
    "disable_censor": False,
    "processing_preset": "balanced",
}


def get_settings_path(settings_path: str | Path | None = None) -> Path:
    if settings_path is None:
        return Path(__file__).with_name("audio_cleaner_settings.json")
    return Path(settings_path).expanduser()


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
    if isinstance(theme, str) and theme.strip():
        settings["theme"] = theme.strip()

    auto_save = raw_settings.get("auto_save")
    if isinstance(auto_save, bool):
        settings["auto_save"] = auto_save

    censor_mode = raw_settings.get("censor_mode")
    if isinstance(censor_mode, str) and censor_mode in {"beep", "meow", "off"}:
        settings["censor_mode"] = censor_mode

    settings["noise_reduction"] = _coerce_float(raw_settings.get("noise_reduction"), DEFAULT_SETTINGS["noise_reduction"])
    settings["censor_chunk_size"] = _coerce_int(
        raw_settings.get("censor_chunk_size"), DEFAULT_SETTINGS["censor_chunk_size"], minimum=1000
    )
    settings["padding_ms"] = _coerce_int(raw_settings.get("padding_ms"), DEFAULT_SETTINGS["padding_ms"], minimum=0)

    disable_censor = raw_settings.get("disable_censor")
    if isinstance(disable_censor, bool):
        settings["disable_censor"] = disable_censor

    preset = raw_settings.get("processing_preset")
    if isinstance(preset, str) and preset in {"fast", "balanced", "quality"}:
        settings["processing_preset"] = preset

    return settings


def load_settings(settings_path: str | Path | None = None) -> dict[str, Any]:
    path = get_settings_path(settings_path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return normalize_settings(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict[str, Any], settings_path: str | Path | None = None) -> dict[str, Any]:
    normalized = normalize_settings(settings)
    path = get_settings_path(settings_path)
    try:
        path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass
    return normalized

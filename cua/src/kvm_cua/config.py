"""Settings, read from the environment (and a local .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # JetKVM device (firmware /api/cua)
    kvm_url: str = "http://jetkvm.local"
    kvm_token: str = ""
    kvm_keyboard_layout: str = "en-US"
    kvm_timeout_s: float = 10.0

    # TypeSafe Jev
    typesafe_api_key: str = ""
    typesafe_base_url: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"

    # Perception
    cua_parser: Literal["omniparser", "ocr", "null"] = "omniparser"
    cua_device: Literal["auto", "mps", "cuda", "cpu"] = "auto"
    cua_weights_dir: Path = Path("weights")
    cua_caption_icons: bool = True
    cua_ocr: Literal["auto", "vision", "easyocr"] = "auto"  # auto = Apple Vision on macOS, else EasyOCR

    # Agent loop
    cua_max_steps: int = 15
    cua_runs_dir: Path = Path("runs")

    # MCP server
    cua_mcp_host: str = "127.0.0.1"
    cua_mcp_port: int = 8765
    cua_mcp_token: str = ""

    # Dashboard
    cua_dashboard_port: int = 8766


@lru_cache
def get_settings() -> Settings:
    return Settings()

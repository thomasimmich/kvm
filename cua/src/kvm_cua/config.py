"""Settings, read from the environment (and a local .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
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
    # Florence-2 captions for icons without text: ~0.2 s per new icon (8–20 s on a new screen)
    cua_caption_icons: bool = False
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

    # Human steering: when stuck, ask the human (dashboard or remote control) and wait this long
    cua_human_wait_s: float = 45.0

    # Rough screen description (fast image-to-text model, runs next to OmniParser)
    cua_vision_provider: Literal["openai", "none"] = "openai"  # openai = any OpenAI-compatible API
    cua_vision_model: str = "gpt-4.1-mini"
    cua_vision_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = Field("", validation_alias=AliasChoices("OPENAI_API_KEY", "LEANSCOPE_OPENAI_API_KEY"))

    # Inner voice: the acting persona of the picked choreo thinks the activity log aloud (dashboard).
    # Its thoughts come from the OpenAI-compatible API above, its voice from ElevenLabs.
    cua_inner_voice: bool = True  # on at start; the dashboard switch turns it on and off
    cua_inner_voice_model: str = "gpt-4.1-mini"
    cua_inner_voice_persona_guid: str = ""  # empty: the picked choreo's primary actor
    cua_inner_voice_id: str = ""  # ElevenLabs voice id when the persona has none attached
    elevenlabs_api_key: str = ""
    elevenlabs_tts_model: str = "eleven_multilingual_v2"

    # Live narration into a LeanScope agent-server session (off while the URL is empty).
    # Login and the choice of choreo happen in the dashboard and are kept in cua_state_file.
    leanscope_agent_server_url: str = ""
    leanscope_api_url: str = "https://api.leanscope.io"
    # LeanScope's login service (Supabase); the anon key is public by design
    leanscope_supabase_url: str = "https://ezmofhpfycarkvhcfvje.supabase.co"
    leanscope_supabase_anon_key: str = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImV6bW9maHBmeWNhcmt2aGNmdmplIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzA5OTM3MDYsImV4cCI6MjA4NjU2OTcwNn0.8tWAQD8c3c8pvb6-wDoJ5kFeKl-DLxevYkE6s6rhy8M"
    )
    cua_state_file: Path = Path(".leanscope-session.json")  # refresh token + choreo choice; git-ignored
    leanscope_remote_control_url: str = ""  # the remote control app; the QR code links here
    cua_public_url: str = ""  # how the agent server reaches this service (the choreo's Worker dispatcher)
    cua_worker_key: str = ""  # ?key=… the dispatcher URL must carry
    cua_dispatch_timeout_s: float = 20.0

    @property
    def narration_enabled(self) -> bool:
        return bool(self.leanscope_agent_server_url)

    @property
    def worker_dispatch_url(self) -> str:
        base = (self.cua_public_url or f"http://{self.cua_mcp_host}:{self.cua_mcp_port}").rstrip("/")
        return base + "/worker/dispatch" + (f"?key={self.cua_worker_key}" if self.cua_worker_key else "")


@lru_cache
def get_settings() -> Settings:
    return Settings()

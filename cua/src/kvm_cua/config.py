"""Settings, read from the environment (and a local .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # JetKVM device (firmware /api/cua). Defaults only: the dashboard's JetKVM section overrides
    # both and keeps them in cua_kvm_state_file.
    kvm_url: str = "http://jetkvm.local"
    kvm_token: str = ""
    cua_kvm_state_file: Path = Path(".kvm-device.json")  # git-ignored
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
    # "lan": listen on all interfaces and show this machine's LAN address (192.168.…);
    # 127.0.0.1 keeps it local. Off loopback a token is required: without CUA_MCP_TOKEN one is
    # made once and kept in cua_mcp_token_file. Loopback clients need no token then.
    cua_mcp_host: str = "lan"
    cua_mcp_port: int = 8765
    cua_mcp_token: str = ""
    cua_mcp_token_file: Path = Path(".cua-mcp-token")  # git-ignored

    # Dashboard
    cua_dashboard_port: int = 8766

    # Physical Sphero mouse that mirrors the pointer (dashboard switch; the URL can be changed there)
    cua_sphero_url: str = ""  # e.g. http://192.168.1.20:8765, from the Sphero server's log
    cua_sphero_state_file: Path = Path(".sphero-mouse.json")  # on/off + URL from the dashboard; git-ignored

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
    def bind_host(self) -> str:
        """The address the server listens on."""
        return "0.0.0.0" if self.cua_mcp_host == "lan" else self.cua_mcp_host

    @property
    def public_host(self) -> str:
        """The address others reach the server at: the LAN address when listening on all interfaces."""
        if self.bind_host in ("0.0.0.0", "::"):
            return lan_ip() or "127.0.0.1"
        return self.cua_mcp_host

    @property
    def worker_dispatch_url(self) -> str:
        base = (self.cua_public_url or f"http://{self.public_host}:{self.cua_mcp_port}").rstrip("/")
        return base + "/worker/dispatch" + (f"?key={self.cua_worker_key}" if self.cua_worker_key else "")


def lan_ip() -> str:
    """This machine's address on the local network: the source address of a route to the
    internet (connecting a UDP socket sends nothing)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return ""


@lru_cache
def get_settings() -> Settings:
    return Settings()

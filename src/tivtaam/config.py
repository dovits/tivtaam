from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YAML_PATH = REPO_ROOT / "config.yaml"


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(REPO_ROOT / ".env"), extra="ignore")

    tivtaam_username: str = ""
    tivtaam_password: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0
    run_headless: bool = False
    log_level: str = "INFO"


class BrowserCfg(BaseModel):
    user_data_dir: str = "./.pw-userdata"
    base_url: str = "https://www.tivtaam.co.il"
    default_timeout_ms: int = 15000
    navigation_timeout_ms: int = 30000
    navigation_jitter_ms: tuple[int, int] = (200, 600)


class HistoryCfg(BaseModel):
    sync_min_age_hours: int = 24
    full_sync_pages: int = 50
    fuzzy_floor: int = 70


class Thresholds(BaseModel):
    alias_max_age_days: int = 90
    history_high: float = 0.80
    history_margin: float = 0.15
    search_high: int = 88
    search_margin: int = 10
    claude_high: float = 0.85
    ask_user_below: float = 0.75


class BasketCfg(BaseModel):
    lookback_weeks: int = 12
    min_weeks: int = 6
    max_suggestions: int = 8


class CouponsCfg(BaseModel):
    auto_clip: bool = True
    surface_unrelated: bool = False


class ClaudeCfg(BaseModel):
    model_default: str = "claude-haiku-4-5-20251001"
    model_escalate: str = "claude-sonnet-4-6"
    max_tokens: int = 600
    temperature: float = 0.0
    cache_system: bool = True


class RunCfg(BaseModel):
    timeout_minutes: int = 20
    cart_total_drift_pct: int = 5


class TelegramCfg(BaseModel):
    poll_interval_s: int = 2


class CategoryCfg(BaseModel):
    """A broad generic (e.g. 'חטיפים') that should surface ALL matching
    previously-purchased items for the user to multi-pick, instead of
    resolving to a single SKU."""
    keywords: list[str] = []
    exclude: list[str] = []


class AppConfig(BaseModel):
    browser: BrowserCfg = Field(default_factory=BrowserCfg)
    history: HistoryCfg = Field(default_factory=HistoryCfg)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    basket: BasketCfg = Field(default_factory=BasketCfg)
    coupons: CouponsCfg = Field(default_factory=CouponsCfg)
    claude: ClaudeCfg = Field(default_factory=ClaudeCfg)
    run: RunCfg = Field(default_factory=RunCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    categories: dict[str, CategoryCfg] = Field(default_factory=dict)
    secrets: Secrets = Field(default_factory=Secrets)


@lru_cache(maxsize=1)
def load_config(yaml_path: Path | None = None) -> AppConfig:
    path = yaml_path or CONFIG_YAML_PATH
    raw: dict = {}
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    cfg = AppConfig.model_validate({**raw, "secrets": Secrets()})
    return cfg


def repo_root() -> Path:
    return REPO_ROOT

"""配置加载：默认值 + config/settings.yaml + 环境变量覆盖。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "project": {
        "name": "nasdaq-intel",
        "report_timezone": "America/New_York",
        "data_dir": "data",
        "cache_dir": "data/cache",
    },
    "ranking": {
        "exchange": "NASDAQ",
        "top_n": 10,
        "min_universe_size": 3000,
        "exclude_etf": True,
        "exclude_test_issues": True,
        "merge_share_classes": True,
    },
    "collect": {
        "lookback_hours": 24,
        "news_providers": ["nasdaq_news"],
        "news_limit_per_request": 30,
        "max_news_per_company": 25,
        "sec_forms": ["8-K", "10-Q", "10-K"],
        "sec_max_per_company": 12,
        "prnewswire_rss": "https://www.prnewswire.com/rss/news-releases-list.rss",
    },
    "dedupe": {"title_similarity_threshold": 0.86},
    "analysis": {
        "importance_threshold": 4,
        "max_deep_analysis": 6,
        "max_events_in_report": 8,
    },
    "llm": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
        "temperature": 0.2,
        "max_tokens": 1200,
        "timeout": 90,
        "generate_report": False,
    },
    "http": {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "sec_user_agent": "NasdaqIntelMVP/0.1 (research; contact: you@example.com)",
        "timeout": 60,
        "max_retries": 3,
        "sec_min_interval": 0.2,
        "nasdaq_min_interval": 0.3,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_env_file(path: Path) -> None:
    """读取项目根目录的 .env（KEY=VALUE），已存在的环境变量优先，避免密钥写进配置文件。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config" / "settings.yaml"
    _load_env_file(PROJECT_ROOT / ".env")
    file_cfg: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            file_cfg = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, file_cfg)

    # 环境变量覆盖：仅暴露最常用的几项，避免配置文件里写密钥
    if os.getenv("NASDAQ_INTEL_DATA_DIR"):
        cfg["project"]["data_dir"] = os.environ["NASDAQ_INTEL_DATA_DIR"]
    if os.getenv("LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["LLM_BASE_URL"]
    if os.getenv("LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["LLM_MODEL"]
    if os.getenv("LLM_API_KEY_ENV"):
        cfg["llm"]["api_key_env"] = os.environ["LLM_API_KEY_ENV"]

    cfg["_root"] = str(PROJECT_ROOT)
    cfg["_config_path"] = str(cfg_path)
    return cfg


def resolve_path(cfg: dict[str, Any], key: str) -> Path:
    """把 project.* 下的相对路径解析为项目根目录下的绝对路径。"""
    return Path(cfg["_root"]) / cfg["project"][key]

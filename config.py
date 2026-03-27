"""Конфигурация бота — двойной источник: bot_settings.json (приоритет) + .env (фоллбэк)"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Пути
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CHARTS_DIR = BASE_DIR / "charts"
DATA_DIR.mkdir(exist_ok=True)
CHARTS_DIR.mkdir(exist_ok=True)

# JSON-файл настроек (приоритет над .env)
SETTINGS_FILE = DATA_DIR / "bot_settings.json"


# ── Helpers ──────────────────────────────────────────────────

def _load_json_settings() -> dict:
    """Загрузить настройки из JSON (если файл существует)"""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _get(js: dict, key: str, env_key: str, default):
    """JSON → .env → default  (для скаляров)"""
    if key in js:
        return js[key]
    val = os.getenv(env_key)
    if val is not None:
        if isinstance(default, bool):
            return val.lower() in ("true", "1", "yes")
        if isinstance(default, int):
            try:
                return int(val)
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(val)
            except ValueError:
                return default
        return val
    return default


def _get_list(js: dict, key: str, env_key: str, default_csv: str) -> list[str]:
    """JSON array → .env CSV → default"""
    if key in js and isinstance(js[key], list):
        return js[key]
    val = os.getenv(env_key, default_csv)
    return [x.strip() for x in val.split(",") if x.strip()]


# ── Загрузка ─────────────────────────────────────────────────

_js = _load_json_settings()

# Telegram
TELEGRAM_BOT_TOKEN = _get(_js, "telegram_bot_token", "TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = _get(_js, "telegram_channel_id", "TELEGRAM_CHANNEL_ID", "")
ADMIN_TELEGRAM_ID = int(_get(_js, "admin_telegram_id", "ADMIN_TELEGRAM_ID", 0))

# Polymarket API
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
POLYMARKET_CHAIN_ID = 137  # Polygon

# Категории рынков
CATEGORIES = _get_list(_js, "categories", "CATEGORIES", "politics,economics")

# Ключевые слова для фильтрации рынков по категориям
CATEGORY_KEYWORDS = {
    "politics": [
        "election", "president", "trump", "biden", "congress", "senate",
        "governor", "vote", "ballot", "democrat", "republican",
        "political", "impeach", "cabinet", "minister", "parliament",
        "nato", "ceasefire", "treaty", "executive order",
    ],
    "economics": [
        "gdp", "inflation", "interest rate", "fed rate", "federal reserve",
        "ecb", "tariff", "trade war", "recession", "unemployment",
        "s&p 500", "nasdaq", "dow jones", "stock market",
        "national debt", "deficit", "fiscal", "monetary policy",
        "oil price", "opec",
    ],
}

# Сканирование
SCAN_INTERVAL_MINUTES = int(_get(_js, "scan_interval_minutes", "SCAN_INTERVAL_MINUTES", 10))
DEEP_ANALYSIS_INTERVAL_MINUTES = int(_get(_js, "deep_analysis_interval_minutes", "DEEP_ANALYSIS_INTERVAL_MINUTES", 60))

# Пороги сигналов
PROBABILITY_SHIFT_THRESHOLD = float(_get(_js, "probability_shift_threshold", "PROBABILITY_SHIFT_THRESHOLD", 0.08))
VOLUME_SPIKE_MULTIPLIER = float(_get(_js, "volume_spike_multiplier", "VOLUME_SPIKE_MULTIPLIER", 2.0))

# Риск-менеджмент
MAX_BET_SIZE_USDC = float(_get(_js, "max_bet_size_usdc", "MAX_BET_SIZE_USDC", 10.0))
MAX_DAILY_LOSS_USDC = float(_get(_js, "max_daily_loss_usdc", "MAX_DAILY_LOSS_USDC", 50.0))
MAX_POSITIONS = int(_get(_js, "max_positions", "MAX_POSITIONS", 5))
STOP_LOSS_PERCENT = float(_get(_js, "stop_loss_percent", "STOP_LOSS_PERCENT", 0.20))

# Расписание
TIMEZONE = _get(_js, "timezone", "TIMEZONE", "Etc/GMT-3")
DAILY_SUMMARY_HOUR = int(_get(_js, "daily_summary_hour", "DAILY_SUMMARY_HOUR", 23))

# Web Admin
WEB_ADMIN_PORT = int(_get(_js, "web_admin_port", "WEB_ADMIN_PORT", 0))
WEB_ADMIN_TOKEN = _get(_js, "web_admin_token", "WEB_ADMIN_TOKEN", "")

# OpenAI (опционально)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# БД
DB_PATH = DATA_DIR / "polymarket.db"


# ── Сохранение / перезагрузка ────────────────────────────────

def save_settings(data: dict):
    """Сохранить настройки в bot_settings.json"""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def get_all_settings() -> dict:
    """Все текущие настройки как dict (для API). Исключает приватный ключ!"""
    return {
        "telegram_bot_token": TELEGRAM_BOT_TOKEN,
        "telegram_channel_id": TELEGRAM_CHANNEL_ID,
        "admin_telegram_id": ADMIN_TELEGRAM_ID,
        "categories": CATEGORIES,
        "scan_interval_minutes": SCAN_INTERVAL_MINUTES,
        "deep_analysis_interval_minutes": DEEP_ANALYSIS_INTERVAL_MINUTES,
        "probability_shift_threshold": PROBABILITY_SHIFT_THRESHOLD,
        "volume_spike_multiplier": VOLUME_SPIKE_MULTIPLIER,
        "max_bet_size_usdc": MAX_BET_SIZE_USDC,
        "max_daily_loss_usdc": MAX_DAILY_LOSS_USDC,
        "max_positions": MAX_POSITIONS,
        "stop_loss_percent": STOP_LOSS_PERCENT,
        "timezone": TIMEZONE,
        "daily_summary_hour": DAILY_SUMMARY_HOUR,
        "web_admin_port": WEB_ADMIN_PORT,
        "web_admin_token": WEB_ADMIN_TOKEN,
    }


def reload_dynamic():
    """Перечитать JSON и обновить «горячие» переменные модуля (без перезапуска)"""
    global CATEGORIES, SCAN_INTERVAL_MINUTES, DEEP_ANALYSIS_INTERVAL_MINUTES
    global PROBABILITY_SHIFT_THRESHOLD, VOLUME_SPIKE_MULTIPLIER
    global MAX_BET_SIZE_USDC, MAX_DAILY_LOSS_USDC, MAX_POSITIONS, STOP_LOSS_PERCENT
    global DAILY_SUMMARY_HOUR

    js = _load_json_settings()

    CATEGORIES = _get_list(js, "categories", "CATEGORIES", "politics,economics")
    SCAN_INTERVAL_MINUTES = int(_get(js, "scan_interval_minutes", "SCAN_INTERVAL_MINUTES", 10))
    DEEP_ANALYSIS_INTERVAL_MINUTES = int(_get(js, "deep_analysis_interval_minutes", "DEEP_ANALYSIS_INTERVAL_MINUTES", 60))
    PROBABILITY_SHIFT_THRESHOLD = float(_get(js, "probability_shift_threshold", "PROBABILITY_SHIFT_THRESHOLD", 0.05))
    VOLUME_SPIKE_MULTIPLIER = float(_get(js, "volume_spike_multiplier", "VOLUME_SPIKE_MULTIPLIER", 2.0))
    MAX_BET_SIZE_USDC = float(_get(js, "max_bet_size_usdc", "MAX_BET_SIZE_USDC", 10.0))
    MAX_DAILY_LOSS_USDC = float(_get(js, "max_daily_loss_usdc", "MAX_DAILY_LOSS_USDC", 50.0))
    MAX_POSITIONS = int(_get(js, "max_positions", "MAX_POSITIONS", 5))
    STOP_LOSS_PERCENT = float(_get(js, "stop_loss_percent", "STOP_LOSS_PERCENT", 0.20))
    DAILY_SUMMARY_HOUR = int(_get(js, "daily_summary_hour", "DAILY_SUMMARY_HOUR", 23))

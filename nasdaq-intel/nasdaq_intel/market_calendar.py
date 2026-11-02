"""美股交易日历与时区工具。

- 系统采集时间统一用 UTC 存储；
- 美股交易日期用 America/New_York（考虑周末、节假日、夏令时）；
- 日报展示时间用 Asia/Shanghai。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TRADING_TZ = ZoneInfo("America/New_York")
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")

# 纽交所/纳斯达克休市日（2025-2027，含周末顺延的观察日）
HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def ny_today(now: datetime | None = None) -> date:
    current = now.astimezone(TRADING_TZ) if now else datetime.now(TRADING_TZ)
    return current.date()


def display_now(now: datetime | None = None) -> datetime:
    current = now.astimezone(UTC) if now else datetime.now(UTC)
    return current.astimezone(DISPLAY_TZ)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def latest_trading_day(day: date) -> date:
    """不晚于 day 的最近一个交易日。"""
    cursor = day
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def market_phase(now: datetime | None = None) -> str:
    """返回 pre / open / after / closed；仅覆盖常规交易时段，不处理提前收盘。"""
    current = now.astimezone(TRADING_TZ) if now else datetime.now(TRADING_TZ)
    if not is_trading_day(current.date()):
        return "closed"
    clock = current.time()
    if clock < time(9, 30):
        return "pre"
    if clock < time(16, 0):
        return "open"
    if clock < time(20, 0):
        return "after"
    return "closed"


PHASE_LABELS = {
    "pre": "盘前",
    "open": "交易中",
    "after": "盘后",
    "closed": "已收盘/休市",
}


def session_label(phase: str) -> str:
    return PHASE_LABELS.get(phase, phase)

"""
時刻ユーティリティ

GitHub ActionsのランナーはUTCで動くため、曜日テーマや日付表記が
日本時間とずれないよう、現在時刻は必ずここから取得する。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    """日本時間の現在時刻（タイムゾーン情報なしのnaive datetime）を返す"""
    return datetime.now(JST).replace(tzinfo=None)

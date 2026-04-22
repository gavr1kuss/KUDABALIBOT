from datetime import date, datetime
from zoneinfo import ZoneInfo

BALI_TZ = ZoneInfo("Asia/Makassar")


def bali_today() -> date:
    return datetime.now(BALI_TZ).date()


def bali_now() -> datetime:
    return datetime.now(BALI_TZ)

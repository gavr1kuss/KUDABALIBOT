import asyncio
import os
import logging
import aiohttp
import hashlib
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from sqlalchemy import select
from database.models import AsyncSessionMaker as async_session, ScrapedEvent

load_dotenv()
log = logging.getLogger(__name__)

BALI_TZ = ZoneInfo("Asia/Makassar")

BALIEVENTS_URL = os.getenv("BALIEVENTS_URL", "https://uyueqyedxphgdkaiwwsj.supabase.co/rest/v1/public_events")
BALIEVENTS_KEY = os.getenv("BALIEVENTS_KEY", "")

APPBALI_URL = os.getenv("APPBALI_URL", "https://wbwgwytdqubtrdxfbjrr.supabase.co/rest/v1/events")
APPBALI_KEY = os.getenv("APPBALI_KEY", "")

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


def _bali_today():
    return datetime.now(BALI_TZ).date()


async def _get_json(session, url, headers, params):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status >= 500:
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message=await r.text())
                if r.status != 200:
                    txt = await r.text()
                    log.error("GET %s failed %s: %s", url, r.status, txt[:300])
                    return None
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            wait = RETRY_BACKOFF * (attempt + 1)
            log.warning("GET %s attempt %s/%s failed: %s — retry in %ss", url, attempt + 1, MAX_RETRIES, e, wait)
            await asyncio.sleep(wait)
    log.error("GET %s gave up after %s attempts: %s", url, MAX_RETRIES, last_exc)
    return None


async def fetch_balievents(session):
    if not BALIEVENTS_KEY:
        log.error("BALIEVENTS_KEY not set in .env")
        return []
    today = _bali_today()
    week_ahead = today + timedelta(days=7)
    headers = {"apikey": BALIEVENTS_KEY, "Authorization": f"Bearer {BALIEVENTS_KEY}"}
    params = {
        "select": "*",
        "is_validated": "eq.true",
        "in_review": "eq.false",
        "limit": "1000",
    }
    data = await _get_json(session, BALIEVENTS_URL, headers, params)
    if data is None:
        return []
    if not isinstance(data, list):
        log.error("balievents.co: unexpected response shape: %r", data)
        return []
    events = []
    for ev in data:
        title = ev.get("title") or ""
        desc = ev.get("description") or ""
        slug = ev.get("slug") or ev.get("id")
        base_link = f"https://balievents.co/events/{slug}"
        freq = (ev.get("frequency") or "").lower()
        ts = ev.get("event_timestamp")
        base_dt = None
        if ts:
            try:
                base_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError as e:
                log.warning("balievents.co: bad event_timestamp %r: %s", ts, e)
        occurrences = []
        if freq == "daily":
            for i in range(7):
                occurrences.append(today + timedelta(days=i))
        elif freq == "weekly" and base_dt:
            wd = base_dt.weekday()
            for i in range(7):
                d = today + timedelta(days=i)
                if d.weekday() == wd:
                    occurrences.append(d)
        else:
            if base_dt and today <= base_dt.date() < week_ahead:
                occurrences.append(base_dt.date())
        body = (title + "\n\n" + desc).strip()
        for occ in occurrences:
            events.append({
                "chat_title": "balievents.co",
                "link": f"{base_link}?d={occ.isoformat()}",
                "raw_text": body,
                "event_date": occ,
            })
    return events


async def fetch_app_bali(session):
    if not APPBALI_KEY:
        log.error("APPBALI_KEY not set in .env")
        return []
    today = _bali_today()
    week_ahead = today + timedelta(days=7)
    headers = {"apikey": APPBALI_KEY, "Authorization": f"Bearer {APPBALI_KEY}"}
    params = [
        ("select", "*,places:venue_place_id(place_name,place_area,place_url_slug)"),
        ("event_status", "eq.active"),
        ("event_date", f"gte.{today.isoformat()}"),
        ("event_date", f"lt.{week_ahead.isoformat()}"),
        ("order", "event_date.asc,event_sort_id.asc"),
        ("limit", "1000"),
    ]
    data = await _get_json(session, APPBALI_URL, headers, params)
    if data is None:
        return []
    if not isinstance(data, list):
        log.error("app.bali.com: unexpected response shape: %r", data)
        return []
    events = []
    for ev in data:
        title = ev.get("event_name") or ""
        desc = ev.get("event_description") or ""
        slug = ev.get("event_url_slug") or ev.get("id")
        link = f"https://app.bali.com/events/{slug}"
        evd_raw = ev.get("event_date")
        evd = None
        if evd_raw:
            try:
                evd = datetime.fromisoformat(evd_raw).date() if "T" in evd_raw else datetime.strptime(evd_raw, "%Y-%m-%d").date()
            except ValueError as e:
                log.warning("app.bali.com: bad event_date %r: %s", evd_raw, e)
        events.append({
            "chat_title": "app.bali.com",
            "link": link,
            "raw_text": (title + "\n\n" + desc).strip(),
            "event_date": evd,
        })
    return events


async def save_events(events):
    saved = 0
    async with async_session() as s:
        for e in events:
            exists = await s.execute(select(ScrapedEvent).where(ScrapedEvent.link == e["link"]))
            if exists.scalar_one_or_none():
                continue
            h = hashlib.md5((e["raw_text"] + "::" + str(e["event_date"])).encode("utf-8")).hexdigest()
            row = ScrapedEvent(
                chat_title=e["chat_title"],
                link=e["link"],
                raw_text=e["raw_text"],
                text_hash=h,
                status="pending",
                event_date=e["event_date"],
            )
            s.add(row)
            saved += 1
        await s.commit()
    return saved


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async with aiohttp.ClientSession() as session:
        be = await fetch_balievents(session)
        ab = await fetch_app_bali(session)
        print(f"balievents.co: {len(be)}, app.bali.com: {len(ab)}")
        saved = await save_events(be + ab)
        print(f"saved: {saved}")


if __name__ == "__main__":
    asyncio.run(main())

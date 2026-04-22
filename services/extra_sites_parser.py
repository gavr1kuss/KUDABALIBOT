import asyncio
import aiohttp
import hashlib
from datetime import date, timedelta, datetime
from sqlalchemy import select
from database.models import AsyncSessionMaker as async_session, ScrapedEvent

BALIEVENTS_URL = "https://uyueqyedxphgdkaiwwsj.supabase.co/rest/v1/public_events"
BALIEVENTS_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV5dWVxeWVkeHBoZ2RrYWl3d3NqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDcxMzgxNzMsImV4cCI6MjA2MjcxNDE3M30.ZjOlKUcEQDlFp9S3jeTwRqcINcOrz6z-5wClJvLkjXA"

APPBALI_URL = "https://wbwgwytdqubtrdxfbjrr.supabase.co/rest/v1/events"
APPBALI_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indid2d3eXRkcXVidHJkeGZianJyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTE1NjIyMzAsImV4cCI6MjA2NzEzODIzMH0.NwPXF8UYbOqhjxUka0kzb47IPgx146_7V7Ats4IUDUs"


async def fetch_balievents(session):
    today = date.today()
    week_ahead = today + timedelta(days=7)
    headers = {"apikey": BALIEVENTS_KEY, "Authorization": f"Bearer {BALIEVENTS_KEY}"}
    params = {
        "select": "*",
        "is_validated": "eq.true",
        "in_review": "eq.false",
        "limit": "1000",
    }
    async with session.get(BALIEVENTS_URL, headers=headers, params=params) as r:
        data = await r.json()
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
            except Exception:
                base_dt = None
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
    today = date.today()
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
    async with session.get(APPBALI_URL, headers=headers, params=params) as r:
        data = await r.json()
    events = []
    for ev in data:
        title = ev.get("event_name") or ""
        desc = ev.get("event_description") or ""
        slug = ev.get("event_url_slug") or ev.get("id")
        link = f"https://app.bali.com/events/{slug}"
        evd_raw = ev.get("event_date")
        try:
            evd = date.fromisoformat(evd_raw) if evd_raw else None
        except Exception:
            evd = None
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
    async with aiohttp.ClientSession() as session:
        be = await fetch_balievents(session)
        ab = await fetch_app_bali(session)
        print(f"balievents.co: {len(be)}, app.bali.com: {len(ab)}")
        saved = await save_events(be + ab)
        print(f"saved: {saved}")


if __name__ == "__main__":
    asyncio.run(main())

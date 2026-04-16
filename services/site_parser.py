import asyncio
import logging
import random
import aiohttp
import re as _re
from html.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.models import AsyncSessionMaker, ScrapedEvent, compute_text_hash


class _MetaExtractor(HTMLParser):
    """Извлекает title, meta-теги, JSON-LD и текст из HTML без JS-рендеринга."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta_desc = ""
        self.og_title = ""
        self.json_ld_blocks: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._in_script_ld = False
        self._in_body = False
        self._skip_tags = {"script", "style", "noscript", "svg", "path"}
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            if d.get("property") == "og:description" or d.get("name") == "description":
                self.meta_desc = self.meta_desc or d.get("content", "")
            if d.get("property") == "og:title":
                self.og_title = d.get("content", "")
        elif tag == "script" and d.get("type") == "application/ld+json":
            self._in_script_ld = True
        elif tag == "body":
            self._in_body = True
        elif tag in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_script_ld = False
        elif tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_script_ld:
            self.json_ld_blocks.append(data)
        elif self._in_body and self._skip_depth == 0:
            txt = data.strip()
            if txt:
                self.text_parts.append(txt)


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


async def parse_event_details_light(session: aiohttp.ClientSession, link: str) -> dict | None:
    """Лёгкий парсинг страницы через aiohttp (без Playwright)."""
    try:
        await asyncio.sleep(random.uniform(0.3, 1.5))
        async with session.get(link, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                logging.warning(f"⚠️ HTTP {resp.status}: {link}")
                return None
            html = await resp.text()
    except Exception as e:
        logging.warning(f"⚠️ Ошибка загрузки {link}: {e}")
        return None

    parser = _MetaExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass

    title = (parser.og_title or parser.title or "").strip()
    meta = parser.meta_desc.strip()
    json_ld = "\n".join(parser.json_ld_blocks).strip()
    body_text = " ".join(parser.text_parts)[:4000]

    parts = [f"TITLE: {title}", f"LINK: {link}"]
    if json_ld:
        parts.append(f"JSON_LD: {json_ld}")
    if meta:
        parts.append(f"META_DESC: {meta}")
    if body_text and len(body_text) > 20:
        parts.append(f"CONTENT:\n{body_text}")

    return {
        "link": link,
        "raw_text": "\n".join(parts),
        "chat_title": "baliforum.ru",
    }


async def parse_baliforum_events() -> list[dict]:
    """Парсинг baliforum.ru — лёгкая версия через aiohttp."""
    events_data = []

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Получаем список ссылок с главной страницы
            logging.info("📄 Загружаем список событий...")
            try:
                async with session.get(
                    "https://baliforum.ru/events",
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    html = await resp.text()
            except Exception as e:
                logging.error(f"❌ Не удалось загрузить список: {e}")
                return []

            # Ищем ссылки вида /events/slug
            raw_links = set(_re.findall(r'href="(/events/[a-z0-9\-]+)"', html))
            links = {f"https://baliforum.ru{href}" for href in raw_links if href != "/events"}
            logging.info(f"🔎 Найдено {len(links)} ссылок. Парсим детально...")

            # 2. Парсим каждую страницу (ограничиваем параллелизм)
            semaphore = asyncio.Semaphore(6)

            async def protected_parse(link):
                async with semaphore:
                    res = await parse_event_details_light(session, link)
                    if res:
                        logging.info(f"✅ OK: {link.split('/')[-1]}")
                    return res

            tasks = [protected_parse(link) for link in links]
            results = await asyncio.gather(*tasks)
            events_data = [r for r in results if r is not None]

    except Exception as e:
        logging.error(f"❌ Критическая ошибка парсера: {e}")

    return events_data


async def save_site_events(events: list[dict]) -> int:
    """Сохранить события"""
    count = 0
    if not events:
        logging.warning("📭 Нечего сохранять (пустой список событий)")
        return 0

    async with AsyncSessionMaker() as session:
        for event in events:
            text_hash = compute_text_hash(event["raw_text"])
            
            # Проверка только по ссылке (самая надежная)
            exists = await session.scalar(
                select(ScrapedEvent).where(ScrapedEvent.link == event["link"])
            )
            
            if exists:
                continue
            
            try:
                new_event = ScrapedEvent(
                    chat_title=event["chat_title"],
                    link=event["link"],
                    raw_text=event["raw_text"],
                    text_hash=text_hash,
                    status="pending"
                )
                session.add(new_event)
                await session.commit()
                count += 1
            except IntegrityError:
                await session.rollback()
    
    logging.info(f"💾 Итого сохранено в БД: {count} (из {len(events)})")
    return count


async def run_site_parser():
    logging.info("🌐 Запуск парсера сайта v3 (Reliable)...")
    events = await parse_baliforum_events()
    saved = await save_site_events(events)
    return saved


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    asyncio.run(run_site_parser())

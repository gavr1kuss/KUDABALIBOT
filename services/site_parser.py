import asyncio
import logging
import random
from playwright.async_api import async_playwright
from datetime import datetime, date, timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.models import AsyncSessionMaker, ScrapedEvent, compute_text_hash


async def parse_event_details(browser, link: str, attempt=1) -> dict | None:
    """Парсинг детальной страницы с повторными попытками"""
    if attempt > 3:
        logging.warning(f"💀 Сдаюсь после 3 попыток: {link}")
        return None

    page = await browser.new_page()
    # Маскируемся под обычного пользователя
    await page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    try:
        # Случайная задержка перед запросом
        await asyncio.sleep(random.uniform(0.5, 2.0))
        
        await page.goto(link, timeout=60000, wait_until="networkidle")
        # Ждём пока контент отрисуется (SPA)
        await page.wait_for_timeout(3000)

        title = await page.title()

        # Пробуем дождаться конкретного контента
        try:
            await page.wait_for_selector('article, main, .event-detail, .event-info, [class*="event"]', timeout=5000)
        except Exception:
            pass  # Если не нашли — берём что есть

        content = await page.evaluate("""() => {
            const article = document.querySelector('article') || document.querySelector('main') || document.body;
            return article ? article.innerText : '';
        }""")

        json_ld = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            return Array.from(scripts).map(s => s.innerText).join('\\n\\n');
        }""")

        meta_description = await page.evaluate("""() => {
            const meta = document.querySelector('meta[property="og:description"]') || document.querySelector('meta[name="description"]');
            return meta ? meta.content : '';
        }""")

        # Собираем все данные
        parts = [f"TITLE: {title}", f"LINK: {link}"]
        if json_ld and json_ld.strip():
            parts.append(f"JSON_LD: {json_ld}")
        if meta_description and meta_description.strip():
            parts.append(f"META_DESC: {meta_description}")
        if content and content.strip():
            parts.append(f"CONTENT:\n{content[:4000]}")

        full_text = "\n".join(parts)
        
        await page.close()
        return {
            "link": link,
            "raw_text": full_text,
            "chat_title": "baliforum.ru"
        }

    except Exception as e:
        await page.close()
        logging.warning(f"⚠️ Ошибка {link} (попытка {attempt}): {e}")
        await asyncio.sleep(2 * attempt) # Пауза перед ретраем
        return await parse_event_details(browser, link, attempt + 1)


async def parse_baliforum_events() -> list[dict]:
    """Парсинг baliforum.ru"""
    events_data = []
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            logging.info("📄 Открываем список событий...")
            try:
                await page.goto("https://baliforum.ru/events", timeout=60000)
                await page.wait_for_timeout(3000)
                
                # Прокручиваем больше раз, чтобы собрать больше
                for _ in range(3): 
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)
                
                cards = await page.query_selector_all('a[href^="/events/"]')
                links = set()
                for card in cards:
                    href = await card.get_attribute("href")
                    if href and href != "/events":
                        links.add(f"https://baliforum.ru{href}")
                
                logging.info(f"🔎 Найдено {len(links)} ссылок. Парсим детально...")
            finally:
                await page.close()

            # Ограничиваем параллелизм, чтобы не забанили
            semaphore = asyncio.Semaphore(4)
            
            async def protected_parse(link):
                async with semaphore:
                    res = await parse_event_details(browser, link)
                    if res:
                        logging.info(f"✅ OK: {link.split('/')[-1]}")
                    return res

            tasks = [protected_parse(link) for link in links]
            results = await asyncio.gather(*tasks)
            
            events_data = [r for r in results if r is not None]
            await browser.close()
            
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
    # Уменьшаем шум от библиотек
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    asyncio.run(run_site_parser())

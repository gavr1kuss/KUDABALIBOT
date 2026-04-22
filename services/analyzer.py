import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, delete, and_
from openai import AsyncOpenAI, APIError, APITimeoutError, APIConnectionError
from database.models import AsyncSessionMaker, ScrapedEvent, compute_text_hash
from data.categories import VALID_CATEGORIES
from config import config

log = logging.getLogger(__name__)

BALI_TZ = ZoneInfo("Asia/Makassar")


def bali_today() -> date:
    return datetime.now(BALI_TZ).date()


client = AsyncOpenAI(
    api_key=config.ai_api_key.get_secret_value(),
    base_url=config.ai_base_url
)

BATCH_SIZE = 20
RECURRING_WEEKS_AHEAD = 3

DEEPSEEK_RETRIES = 3
DEEPSEEK_BACKOFF = 3.0

BOT_PATTERNS = re.compile(r'@BaliForumRuBot|@BaliChatBot|@BaliInfoBot|via @\w+Bot', re.IGNORECASE)

PROMPT = """Ты — редактор афиши мероприятий на Бали. Наша афиша для экспатов и туристов: события, которые человек может посетить. Твоя задача — отсеять мусор из Telegram-чатов и классифицировать оставшееся.

## ГЛАВНОЕ ПРАВИЛО
Сообщение попадает в афишу, ТОЛЬКО ЕСЛИ это приглашение куда-то прийти/поучаствовать, с хотя бы одним из признаков:
- конкретная дата или день недели
- указано место/адрес/заведение
- указано время
- есть ссылка на регистрацию/билет/контакт организатора

Если ничего из перечисленного нет — скорее всего это болтовня или реклама услуг, не событие. Используй здравый смысл: «я провожу индивидуальные занятия по йоге» без даты/места — это реклама услуги, не событие. «Групповая йога в субботу в 10 утра, Echo Beach, по донейшн» — событие.

Серые зоны оценивай сам. Если сомневаешься и похоже на реальное мероприятие — оставляй, модератор решит. Если явно болтовня — отклоняй как Spam.

## ТЕМАТИЧЕСКИЕ КАТЕГОРИИ (одна или несколько через запятую):
- Развлечения: вечеринки, DJ, клубы, концерты, квизы, шоу, стендап, импровизация, кино, трансляции матчей, маркеты
- Практики: йога, медитации, звуковые ванны, кундалини, какао-церемонии, энергопрактики, ребефинг, дыхание, тантра
- Нетворкинг: бизнес-завтраки, speed dating, женские бранчи, комьюнити-встречи
- Спорт: волейбол, сёрф, падел, бойцовские шоу, футбол, фитнес
- Путешествия: туры, экскурсии, сёрф-сафари, трекинг, групповые поездки
- Творчество: мастер-классы рисования, арт-выставки, воркшопы, рукоделие
- Образование: курсы, лекции, тренинги, трансформационные игры
- Без даты: регулярные занятия/услуги без конкретной даты НО с явным приглашением (место + расписание по дням недели). Используй ВМЕСТЕ с тематической, например ["Практики","Без даты"]. НЕ используй эту категорию для одноразовых реклам без привязки к месту.

## СПАМ (Spam) — отклоняй:
- Обсуждения, вопросы, ответы, флуд
- Поиск/предложение работы, резюме, вакансии
- Аренда, продажа, покупка вещей/жилья/транспорта
- Отзывы, рекомендации, советы без приглашения на событие
- Просто реклама услуг/товаров без конкретного события (место+время+дата)
- Личные истории, мнения, новости
- Сообщения от ботов (@BaliForumRuBot, @BaliChatBot и т.п.)

## ЦЕНОВОЙ ТЕГ:
- is_free: true — бесплатно, donation, донейшн, вход свободный
- is_free: false — указана цена/стоимость/билеты
- is_free: null — цена не указана

## РЕГУЛЯРНОСТЬ:
Если текст явно говорит «каждый понедельник», «по вторникам и четвергам», «еженедельно»:
- is_recurring: true
- recurrence: ["mon","tue","wed","thu","fri","sat","sun"] — массив дней
Иначе: is_recurring: false, recurrence: null

## ФОРМАТ ОТВЕТА (строго JSON-массив, без markdown, без комментариев):
[{
  "id": 123,
  "categories": ["Развлечения"],
  "is_free": false,
  "summary": "Краткое описание события одной строкой: что, где, во сколько",
  "event_date": "2026-04-19",
  "is_recurring": false,
  "recurrence": null
}]

Для спама: {"id":123, "categories":["Spam"], "summary":"", "event_date":null, "is_free":null, "is_recurring":false, "recurrence":null}

Сегодня: TODAY_PLACEHOLDER. event_date определяй максимально точно на основе текста и даты публикации (поле posted).

Данные:
DATA_PLACEHOLDER
"""


async def call_deepseek(prompt: str) -> str:
    last_exc = None
    for attempt in range(DEEPSEEK_RETRIES):
        try:
            response = await client.chat.completions.create(
                model=config.ai_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=4000,
                timeout=60,
            )
            return response.choices[0].message.content or ""
        except (APITimeoutError, APIConnectionError, APIError) as exc:
            last_exc = exc
            wait = DEEPSEEK_BACKOFF * (attempt + 1)
            log.warning("DeepSeek attempt %s/%s failed: %s — retry in %ss", attempt + 1, DEEPSEEK_RETRIES, exc, wait)
            await asyncio.sleep(wait)
        except Exception as exc:
            log.error("DeepSeek non-retryable error: %s", exc)
            return ""
    log.error("DeepSeek gave up after %s attempts: %s", DEEPSEEK_RETRIES, last_exc)
    return ""


def parse_event_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        log.warning("Bad event_date from AI: %r", date_str)
        return None


def normalize_categories(raw_cats) -> str:
    if isinstance(raw_cats, str):
        cats = [c.strip() for c in raw_cats.split(",")]
    elif isinstance(raw_cats, list):
        cats = [str(c).strip() for c in raw_cats]
    else:
        return "Развлечения"

    valid = [c for c in cats if c in VALID_CATEGORIES]
    return ",".join(valid) if valid else "Развлечения"


def parse_recurrence(raw) -> str | None:
    if not raw:
        return None
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if isinstance(raw, list):
        days = [d.lower().strip() for d in raw if d.lower().strip() in valid_days]
    elif isinstance(raw, str):
        days = [d.strip() for d in raw.split(",") if d.strip().lower() in valid_days]
    else:
        return None
    return ",".join(days) if days else None


async def check_dedup(session, summary: str, event_date: date | None) -> bool:
    if not summary or not event_date:
        return False
    result = await session.execute(
        select(ScrapedEvent).where(
            and_(
                ScrapedEvent.summary == summary,
                ScrapedEvent.event_date == event_date,
                ScrapedEvent.status.in_(["review", "approved"])
            )
        )
    )
    return result.scalar() is not None


async def cleanup_old_events():
    today = bali_today()
    async with AsyncSessionMaker() as session:
        result = await session.execute(
            delete(ScrapedEvent)
            .where(ScrapedEvent.event_date < today)
            .where(ScrapedEvent.parent_id.is_(None))
            .where(ScrapedEvent.event_date.isnot(None))
        )
        result2 = await session.execute(
            delete(ScrapedEvent)
            .where(ScrapedEvent.event_date < today)
            .where(ScrapedEvent.parent_id.isnot(None))
            .where(ScrapedEvent.event_date.isnot(None))
        )
        await session.commit()
        total = (result.rowcount or 0) + (result2.rowcount or 0)
        if total:
            log.info("🗑 Удалено прошедших: %s", total)


def _apply_ai_result(res: dict) -> dict:
    return {
        "category": normalize_categories(res.get("categories", res.get("category", "Развлечения"))),
        "is_free": res.get("is_free"),
        "summary": res.get("summary", ""),
        "event_date": parse_event_date(res.get("event_date")),
        "is_recurring": bool(res.get("is_recurring", False)),
        "recurrence": parse_recurrence(res.get("recurrence")),
    }


async def run_batch_analysis(auto_approve: bool = False) -> str:
    await cleanup_old_events()

    async with AsyncSessionMaker() as session:
        result = await session.execute(
            select(ScrapedEvent).where(ScrapedEvent.status == "pending")
        )
        batch = result.scalars().all()

        if not batch:
            return "📭 Нет новых сообщений."

        total = len(batch)
        log.info("📊 Pending: %s", total)

        current_date = bali_today().isoformat()
        total_processed = 0
        total_spam = 0
        total_duplicates = 0
        total_errors = 0

        target_status = "approved" if auto_approve else "review"

        for i in range(0, total, BATCH_SIZE):
            chunk = batch[i:i + BATCH_SIZE]
            chunk_ids = [e.id for e in chunk]

            data_for_ai = []
            for e in chunk:
                raw_text = e.raw_text or ""
                if BOT_PATTERNS.search(raw_text):
                    await session.execute(
                        update(ScrapedEvent)
                        .where(ScrapedEvent.id == e.id)
                        .values(status="rejected", category="Spam")
                    )
                    total_spam += 1
                    continue

                posted = e.created_at.strftime("%Y-%m-%d") if e.created_at else current_date
                text = re.sub(r'\s+', ' ', raw_text[:500]).strip()
                data_for_ai.append({"id": e.id, "text": text, "posted": posted})

            if not data_for_ai:
                await session.commit()
                continue

            prompt = (
                PROMPT
                .replace("TODAY_PLACEHOLDER", current_date)
                .replace("DATA_PLACEHOLDER", json.dumps(data_for_ai, ensure_ascii=False))
            )
            raw = await call_deepseek(prompt)

            if not raw:
                log.error("DeepSeek returned empty for chunk ids=%s", chunk_ids)
                total_errors += len(data_for_ai)
                continue

            match = re.search(r'\[.*\]', raw.replace('\n', ' '), re.DOTALL)
            if not match:
                log.error("No JSON array in DeepSeek response for chunk ids=%s; raw[:500]=%r", chunk_ids, raw[:500])
                total_errors += len(data_for_ai)
                continue

            try:
                ai_results = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                log.error("JSON parse failed for chunk ids=%s: %s; raw[:500]=%r", chunk_ids, exc, match.group(0)[:500])
                total_errors += len(data_for_ai)
                continue

            if not isinstance(ai_results, list):
                log.error("AI returned non-list for chunk ids=%s: %r", chunk_ids, ai_results)
                total_errors += len(data_for_ai)
                continue

            results_map = {item.get("id"): item for item in ai_results if isinstance(item, dict)}

            for e in chunk:
                res = results_map.get(e.id)

                if not res:
                    log.warning("AI did not return result for id=%s", e.id)
                    total_errors += 1
                    continue

                try:
                    parsed = _apply_ai_result(res)
                except Exception as exc:
                    log.error("Failed to apply AI result for id=%s: %s; res=%r", e.id, exc, res)
                    total_errors += 1
                    continue

                if "Spam" in (parsed["category"] or ""):
                    await session.execute(
                        update(ScrapedEvent)
                        .where(ScrapedEvent.id == e.id)
                        .values(status="rejected", category="Spam")
                    )
                    total_spam += 1
                    continue

                if await check_dedup(session, parsed["summary"], parsed["event_date"]):
                    await session.execute(
                        update(ScrapedEvent)
                        .where(ScrapedEvent.id == e.id)
                        .values(status="rejected", category="Duplicate")
                    )
                    total_duplicates += 1
                    continue

                update_values = {
                    "status": target_status,
                    "category": parsed["category"],
                    "is_recurring": parsed["is_recurring"],
                    "recurrence": parsed["recurrence"],
                }
                if parsed["event_date"] or not e.event_date:
                    update_values["event_date"] = parsed["event_date"]
                if parsed["is_free"] is not None or e.is_free is None:
                    update_values["is_free"] = parsed["is_free"]
                if parsed["summary"] or not e.summary:
                    update_values["summary"] = parsed["summary"]

                await session.execute(
                    update(ScrapedEvent)
                    .where(ScrapedEvent.id == e.id)
                    .values(**update_values)
                )
                total_processed += 1

            await session.commit()

        status_word = "Одобрено" if auto_approve else "На модерацию"
        return f"✅ {status_word}: {total_processed}, Спам: {total_spam}, Дубли: {total_duplicates}, Ошибок: {total_errors}"


async def analyze_realtime_event(event_id: int) -> None:
    async with AsyncSessionMaker() as session:
        ev = await session.get(ScrapedEvent, event_id)
        if not ev or ev.status != "pending":
            return

        text = re.sub(r'\s+', ' ', (ev.raw_text or "")[:500]).strip()
        posted = ev.created_at.strftime("%Y-%m-%d") if ev.created_at else bali_today().isoformat()

        data = [{"id": ev.id, "text": text, "posted": posted}]
        prompt = (
            PROMPT
            .replace("TODAY_PLACEHOLDER", bali_today().isoformat())
            .replace("DATA_PLACEHOLDER", json.dumps(data, ensure_ascii=False))
        )

        raw = await call_deepseek(prompt)
        if not raw:
            log.error("Realtime: empty DeepSeek response for id=%s", ev.id)
            return

        match = re.search(r'\[.*\]', raw.replace('\n', ' '), re.DOTALL)
        if not match:
            log.error("Realtime: no JSON array for id=%s; raw[:500]=%r", ev.id, raw[:500])
            return

        try:
            ai_results = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            log.error("Realtime: JSON parse failed for id=%s: %s", ev.id, exc)
            return

        if not ai_results:
            return

        try:
            parsed = _apply_ai_result(ai_results[0])
        except Exception as exc:
            log.error("Realtime: apply result failed for id=%s: %s", ev.id, exc)
            return

        if "Spam" in (parsed["category"] or ""):
            ev.status = "rejected"
            ev.category = "Spam"
        elif await check_dedup(session, parsed["summary"], parsed["event_date"]):
            ev.status = "rejected"
            ev.category = "Duplicate"
        else:
            ev.status = "review"
            ev.category = parsed["category"]
            ev.is_recurring = parsed["is_recurring"]
            ev.recurrence = parsed["recurrence"]
            if parsed["event_date"] or not ev.event_date:
                ev.event_date = parsed["event_date"]
            if parsed["is_free"] is not None or ev.is_free is None:
                ev.is_free = parsed["is_free"]
            if parsed["summary"] or not ev.summary:
                ev.summary = parsed["summary"]

        await session.commit()
        log.info("📊 Realtime: %s -> %s [%s]", ev.id, ev.status, ev.category)


# ---------- Регулярные события ----------

DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def create_recurring_entries(parent_id: int, weeks: int = RECURRING_WEEKS_AHEAD) -> int:
    async with AsyncSessionMaker() as session:
        parent = await session.get(ScrapedEvent, parent_id)
        if not parent or not parent.recurrence:
            return 0

        days = [d.strip().lower() for d in parent.recurrence.split(",")]
        day_numbers = [DAY_MAP[d] for d in days if d in DAY_MAP]

        if not day_numbers:
            return 0

        today = bali_today()
        created = 0

        for week_offset in range(weeks):
            for day_num in day_numbers:
                days_ahead = day_num - today.weekday()
                if days_ahead < 0:
                    days_ahead += 7
                target_date = today + timedelta(days=days_ahead + 7 * week_offset)

                if target_date < today:
                    continue

                if await check_dedup(session, parent.summary, target_date):
                    continue

                child = ScrapedEvent(
                    chat_title=parent.chat_title,
                    link=parent.link,
                    raw_text=parent.raw_text,
                    text_hash=compute_text_hash(f"{parent.raw_text}_{target_date.isoformat()}"),
                    status="approved",
                    category=parent.category,
                    is_free=parent.is_free,
                    summary=parent.summary,
                    event_date=target_date,
                    is_recurring=True,
                    recurrence=parent.recurrence,
                    parent_id=parent.id,
                )
                session.add(child)
                created += 1

        await session.commit()
        log.info("🔄 Создано %s регулярных записей для parent=%s", created, parent_id)
        return created


async def cancel_recurring_series(parent_id: int) -> int:
    today = bali_today()
    async with AsyncSessionMaker() as session:
        result = await session.execute(
            delete(ScrapedEvent).where(
                and_(
                    ScrapedEvent.parent_id == parent_id,
                    ScrapedEvent.event_date >= today,
                )
            )
        )
        await session.commit()
        count = result.rowcount or 0
        log.info("🗑 Отменена серия parent=%s, удалено: %s", parent_id, count)
        return count

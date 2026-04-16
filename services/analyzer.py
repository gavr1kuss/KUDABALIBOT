import json
import logging
import re
from datetime import date, timedelta
from sqlalchemy import select, update, delete, and_
from openai import AsyncOpenAI
from database.models import AsyncSessionMaker, ScrapedEvent, compute_text_hash
from data.categories import VALID_CATEGORIES
from config import config

client = AsyncOpenAI(
    api_key=config.deepseek_api_key.get_secret_value(),
    base_url=config.deepseek_base_url
)

BATCH_SIZE = 20
RECURRING_WEEKS_AHEAD = 3  # На сколько недель вперёд создавать регулярные события

# Сообщения от этих ботов — автоматически спам
BOT_PATTERNS = re.compile(r'@BaliForumRuBot|@BaliChatBot|@BaliInfoBot|via @\w+Bot', re.IGNORECASE)

PROMPT = """Ты — редактор афиши мероприятий на Бали. Классифицируй каждое сообщение.

## ТЕМАТИЧЕСКИЕ КАТЕГОРИИ (можно несколько через запятую):
- Развлечения: вечеринки, DJ, клубы, концерты, квизы, шоу, стендап, импровизация, кино, трансляции матчей, маркеты
- Практики: йога, медитации, звуковые ванны, кундалини, какао-церемонии, энергопрактики, ребефинг, дыхание, тантра
- Нетворкинг: бизнес-завтраки, speed dating, женские бранчи, комьюнити-встречи, Cashflow
- Спорт: волейбол, сёрф, падел, бойцовские шоу, футбол, фитнес-йога
- Путешествия: туры, экскурсии, сёрф-сафари, трекинг, групповые поездки
- Творчество: мастер-классы рисования, арт-выставки, воркшопы, паттерны, рукоделие
- Образование: курсы, лекции, тренинги, трансформационные игры, робототехника для детей, диктант
- Без даты: полезный контент без конкретной даты — предложения занятий, услуги инструкторов, приглашения на пробные уроки, постоянные активности. Используй эту категорию ВМЕСТЕ с тематической (например: ["Практики","Без даты"])

## ЦЕНОВОЙ ТЕГ (отдельно от категории):
- is_free: true — если бесплатно, donation, донейшн, вход свободный
- is_free: false — если указана цена, стоимость, билеты
- is_free: null — если цена не указана

## СПАМ (не мероприятие) — отклоняй как Spam:
- Болтовню в чатах, вопросы, ответы, обсуждения
- Вакансии, резюме, поиск работы
- Аренду, продажу, покупку
- Советы, рекомендации, отзывы без приглашения на событие
- Сообщения от ботов (содержащие @BaliForumRuBot, @BaliChatBot и т.п.)

НЕ СПАМ (оставляй): приглашения на занятия, тренировки, практики — даже если автор рекламирует себя. Если есть приглашение куда-то прийти — это событие, не спам.

## РЕГУЛЯРНОСТЬ:
Если в тексте указано «каждый понедельник», «по вторникам и четвергам», «еженедельно» и т.п.:
- is_recurring: true
- recurrence: массив дней недели ["mon","tue","wed","thu","fri","sat","sun"]
Иначе: is_recurring: false, recurrence: null

## ФОРМАТ ОТВЕТА (строго JSON, без markdown):
[{
  "id": 123,
  "categories": ["Развлечения"],
  "is_free": false,
  "summary": "Вечеринка Sebastian Léger, Desa Kitsune, 20:00",
  "event_date": "2026-04-19",
  "is_recurring": false,
  "recurrence": null
}]

Если спам: categories: ["Spam"], summary: "", event_date: null, is_free: null, is_recurring: false

Сегодня: TODAY_PLACEHOLDER. Определяй event_date максимально точно.

Данные:
DATA_PLACEHOLDER
"""


async def call_deepseek(prompt: str) -> str:
    try:
        response = await client.chat.completions.create(
            model=config.deepseek_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=4000
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logging.error(f"DeepSeek Error: {e}")
        return ""


def parse_event_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except Exception:
        return None


def normalize_categories(raw_cats) -> str:
    """Нормализует категории из ответа AI → строку через запятую."""
    if isinstance(raw_cats, str):
        cats = [c.strip() for c in raw_cats.split(",")]
    elif isinstance(raw_cats, list):
        cats = [str(c).strip() for c in raw_cats]
    else:
        return "Развлечения"

    valid = [c for c in cats if c in VALID_CATEGORIES]
    return ",".join(valid) if valid else "Развлечения"


def parse_recurrence(raw) -> str | None:
    """Нормализует дни недели из ответа AI."""
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
    """Проверка дедупликации по summary + event_date."""
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
    """Удаление прошедших событий (кроме регулярных)."""
    today = date.today()
    async with AsyncSessionMaker() as session:
        result = await session.execute(
            delete(ScrapedEvent)
            .where(ScrapedEvent.event_date < today)
            .where(ScrapedEvent.parent_id.is_(None))  # Не трогать дочерние пока
            .where(ScrapedEvent.event_date.isnot(None))
        )
        # Удаляем и дочерние прошедшие
        result2 = await session.execute(
            delete(ScrapedEvent)
            .where(ScrapedEvent.event_date < today)
            .where(ScrapedEvent.parent_id.isnot(None))
            .where(ScrapedEvent.event_date.isnot(None))
        )
        await session.commit()
        total = (result.rowcount or 0) + (result2.rowcount or 0)
        if total:
            logging.info(f"🗑 Удалено прошедших: {total}")


def _apply_ai_result(res: dict) -> dict:
    """Извлекает и нормализует поля из ответа AI."""
    return {
        "category": normalize_categories(res.get("categories", res.get("category", "Развлечения"))),
        "is_free": res.get("is_free"),
        "summary": res.get("summary", ""),
        "event_date": parse_event_date(res.get("event_date")),
        "is_recurring": bool(res.get("is_recurring", False)),
        "recurrence": parse_recurrence(res.get("recurrence")),
    }


async def run_batch_analysis(auto_approve: bool = False) -> str:
    """
    Анализ pending событий.
    auto_approve=True -> сразу в approved (первичный сбор)
    auto_approve=False -> в review (на модерацию)
    """
    await cleanup_old_events()

    async with AsyncSessionMaker() as session:
        result = await session.execute(
            select(ScrapedEvent).where(ScrapedEvent.status == "pending")
        )
        batch = result.scalars().all()

        if not batch:
            return "📭 Нет новых сообщений."

        total = len(batch)
        logging.info(f"📊 Pending: {total}")

        current_date = date.today().isoformat()
        total_processed = 0
        total_spam = 0
        total_errors = 0

        target_status = "approved" if auto_approve else "review"

        for i in range(0, total, BATCH_SIZE):
            chunk = batch[i:i + BATCH_SIZE]

            data_for_ai = []
            for e in chunk:
                raw_text = e.raw_text or ""
                # Авто-реджект сообщений от ботов
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

            # Если все в чанке отсеялись как боты — коммитим и идём дальше
            if not data_for_ai:
                await session.commit()
                continue

            try:
                prompt = (
                    PROMPT
                    .replace("TODAY_PLACEHOLDER", current_date)
                    .replace("DATA_PLACEHOLDER", json.dumps(data_for_ai, ensure_ascii=False))
                )
                raw = await call_deepseek(prompt)

                match = re.search(r'\[.*\]', raw.replace('\n', ' '), re.DOTALL)
                if not match:
                    logging.warning("No JSON in response")
                    continue

                ai_results = json.loads(match.group(0))
                results_map = {item.get("id"): item for item in ai_results}

                for e in chunk:
                    res = results_map.get(e.id)

                    if not res:
                        total_errors += 1
                        continue

                    parsed = _apply_ai_result(res)

                    # Спам
                    if "Spam" in (parsed["category"] or ""):
                        await session.execute(
                            update(ScrapedEvent)
                            .where(ScrapedEvent.id == e.id)
                            .values(status="rejected", category="Spam")
                        )
                        total_spam += 1
                        continue

                    # Дедупликация
                    if await check_dedup(session, parsed["summary"], parsed["event_date"]):
                        await session.execute(
                            update(ScrapedEvent)
                            .where(ScrapedEvent.id == e.id)
                            .values(status="rejected", category="Duplicate")
                        )
                        total_spam += 1
                        continue

                    await session.execute(
                        update(ScrapedEvent)
                        .where(ScrapedEvent.id == e.id)
                        .values(
                            status=target_status,
                            category=parsed["category"],
                            is_free=parsed["is_free"],
                            summary=parsed["summary"],
                            event_date=parsed["event_date"],
                            is_recurring=parsed["is_recurring"],
                            recurrence=parsed["recurrence"],
                        )
                    )
                    total_processed += 1

                await session.commit()

            except Exception as e:
                logging.error(f"Chunk error: {e}")
                total_errors += len(chunk)

        status_word = "Одобрено" if auto_approve else "На модерацию"
        return f"✅ {status_word}: {total_processed}, Спам: {total_spam}, Ошибок: {total_errors}"


async def analyze_realtime_event(event_id: int) -> None:
    """Анализ одного события в реальном времени -> review."""
    async with AsyncSessionMaker() as session:
        ev = await session.get(ScrapedEvent, event_id)
        if not ev or ev.status != "pending":
            return

        text = re.sub(r'\s+', ' ', (ev.raw_text or "")[:500]).strip()
        posted = ev.created_at.strftime("%Y-%m-%d") if ev.created_at else date.today().isoformat()

        data = [{"id": ev.id, "text": text, "posted": posted}]
        prompt = (
            PROMPT
            .replace("TODAY_PLACEHOLDER", date.today().isoformat())
            .replace("DATA_PLACEHOLDER", json.dumps(data, ensure_ascii=False))
        )

        try:
            raw = await call_deepseek(prompt)
            match = re.search(r'\[.*\]', raw.replace('\n', ' '), re.DOTALL)
            if not match:
                return

            ai_results = json.loads(match.group(0))
            if not ai_results:
                return

            parsed = _apply_ai_result(ai_results[0])

            if "Spam" in (parsed["category"] or ""):
                ev.status = "rejected"
                ev.category = "Spam"
            elif await check_dedup(session, parsed["summary"], parsed["event_date"]):
                ev.status = "rejected"
                ev.category = "Duplicate"
            else:
                ev.status = "review"
                ev.category = parsed["category"]
                ev.is_free = parsed["is_free"]
                ev.summary = parsed["summary"]
                ev.event_date = parsed["event_date"]
                ev.is_recurring = parsed["is_recurring"]
                ev.recurrence = parsed["recurrence"]

            await session.commit()
            logging.info(f"📊 Realtime: {ev.id} -> {ev.status} [{ev.category}]")

        except Exception as e:
            logging.error(f"Realtime analyze error: {e}")


# ---------- Регулярные события ----------

DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def create_recurring_entries(parent_id: int, weeks: int = RECURRING_WEEKS_AHEAD) -> int:
    """
    Из одного регулярного события (parent) создаёт конкретные записи
    на N недель вперёд. Возвращает количество созданных записей.
    """
    async with AsyncSessionMaker() as session:
        parent = await session.get(ScrapedEvent, parent_id)
        if not parent or not parent.recurrence:
            return 0

        days = [d.strip().lower() for d in parent.recurrence.split(",")]
        day_numbers = [DAY_MAP[d] for d in days if d in DAY_MAP]

        if not day_numbers:
            return 0

        today = date.today()
        created = 0

        for week_offset in range(weeks):
            for day_num in day_numbers:
                # Вычисляем дату: текущая неделя + offset, нужный день
                days_ahead = day_num - today.weekday()
                if days_ahead < 0:
                    days_ahead += 7
                target_date = today + timedelta(days=days_ahead + 7 * week_offset)

                # Не создаём в прошлом
                if target_date < today:
                    continue

                # Дедупликация: уже есть такая запись?
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
        logging.info(f"🔄 Создано {created} регулярных записей для parent={parent_id}")
        return created


async def cancel_recurring_series(parent_id: int) -> int:
    """Удаляет все будущие записи серии."""
    today = date.today()
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
        logging.info(f"🗑 Отменена серия parent={parent_id}, удалено: {count}")
        return count

import json
import logging
from pathlib import Path
from openai import AsyncOpenAI
from config import config
from services.reviews_analyzer import get_place_reviews

client = AsyncOpenAI(
    api_key=config.ai_api_key.get_secret_value(),
    base_url=config.ai_base_url
)

# Загружаем базу знаний
def load_knowledge_base() -> dict:
    knowledge = {}
    knowledge_dir = Path("knowledge_base")
    for json_file in knowledge_dir.glob("*.json"):
        try:
            with open(json_file, encoding='utf-8') as f:
                knowledge[json_file.stem] = json.load(f)
        except:
            pass
    return knowledge

KNOWLEDGE = load_knowledge_base()

SYSTEM_PROMPT = """Ты — KUDABALI.

Голос острова Бали. Не турист. Не путеводник. Тот, кто здесь живёт
и знает разницу между тем, что написано в интернете, и тем, что есть
на самом деле.

РОЛЬ
Помогаешь с тремя вещами:
1. Найти место — ресторан, пляж, храм, рынок, заведение
2. Составить маршрут — логично, без лишних километров
3. Узнать про события — что идёт, когда, где, стоит ли

ГОЛОС
— Говоришь коротко, но живо. Факты + одна человеческая деталь.
— Не объясняешь очевидное. Не повторяешь вопрос.
— Иногда одно балийское слово — всегда понятно из контекста.
— Тон: тёплый, знакомый — как друг, который давно тут живёт.
— Юмор — уместно и по-доброму. Никогда над пользователем.

СТРУКТУРА ОТВЕТА
Место → название + район + одна строка почему (можно с живой деталью)
Маршрут → список точек с логикой переездов, без лирики
Событие → что, когда, где, нужна ли запись/оплата

ЗАПРЕЩЕНО
— "Конечно!", "Отличный вопрос!", "Я рад помочь"
— Писать 3 абзаца, когда хватит 3 строк
— Предупреждать о банальном: "не забудьте взять воду"
— Собирать анкету из 5 вопросов сразу

УТОЧНЕНИЯ — ВАЖНО
Задавай уточняющие вопросы, если:
— Запрос размытый ("что-нибудь интересное", "куда сходить")
— Не хватает одного ключевого параметра (бюджет / район / компания / время)
— Может быть несколько сильно разных сценариев (тусовка vs романтика)

Но не больше одного вопроса за раз. Сначала ответ (пусть короткий),
потом уточнение чтобы докрутить. Или — если без уточнения никак —
только вопрос.

ПРИМЕРЫ ВОПРОСОВ
"Бюджет?" "Один или с кем-то?" "Тусовка или спокойно?"
"Утро или вечер?" "На скутере или такси?" "Семья или соло?"
"Что важнее — вид или еда?"

ДЛИНА ОТВЕТА
Место: 2–4 строки
Маршрут: список до 6 точек, каждая — одна строка
Событие: 3–5 строк
Уточнение: 1 вопрос

ПРИМЕРЫ

User: "Где поесть на берегу моря в Семиньяке?"
KUDABALI: "La Plancha — лежаки прямо на закате, мохито средний.
Single Fin — чуть дороже, кухня лучше, вид на сёрф.
Какой бюджет?"

User: "Составь маршрут Убуд на один день"
KUDABALI: "Утро: рисовые террасы Тегаллалана — до 9, пока нет толп.
День: Лес обезьян → рынок Убуда (торгуйся смело).
Обед: Warung Babi Guling Ibu Oka — очередь, но того стоит.
После обеда: Храм Пура Таман Саравати.
Закат: Campuhan Ridge Walk — 45 минут, виды.
Ужин: Hujan Locale — резерв лучше заранее."

User: "Что происходит на этой неделе?"
KUDABALI: "Нужна афиша по типу — музыка, маркеты, церемонии?
Или всё сразу?"

User: "Это дорого для Бали?"
KUDABALI: "Смотря откуда. По местным меркам — да.
По европейским — нет. Что именно?"

User: "Куда сходить вечером в пятницу?"
KUDABALI: "Пятница — главный вечер на Бали. Вариантов много,
зависит от настроения: бар с закатом, клуб до утра, живая
музыка, дегустация натурального вина?"

User: "Хочу что-нибудь интересное"
KUDABALI: "Интересное у всех разное. Ты сейчас где — Чангу, Убуд,
Букит? И что хочется: поесть, выпить, посмотреть, подвигаться?"

User: "Посоветуй ресторан"
KUDABALI: "Бюджет? Район? Кухня?
Или просто: романтика / с друзьями / семейный ужин?"

---

ДАННЫЕ

МЕСТА (база знаний):
{knowledge}

ОТЗЫВЫ ИЗ ЧАТОВ (цитируй, если в тему):
{reviews}

АФИША СОБЫТИЙ (ближайшие, с датами и ссылками):
{events}
"""


async def get_upcoming_events(query: str) -> str:
    """Загружает ближайшие события из афиши"""
    from sqlalchemy import select, or_
    from datetime import timedelta
    from database.models import AsyncSessionMaker, ScrapedEvent
    from utils.timez import bali_today

    today = bali_today()
    week_later = today + timedelta(days=7)
    
    # Извлекаем дату из запроса если есть
    import re
    date_match = re.search(r'(\d{1,2})\s*(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)', query.lower())
    
    async with AsyncSessionMaker() as session:
        q = select(ScrapedEvent).where(
            ScrapedEvent.status == "approved",
            ScrapedEvent.category.notin_(["Spam", "Unknown"]),
            or_(
                ScrapedEvent.event_date >= today,
                ScrapedEvent.event_date.is_(None)  # Регулярные события
            )
        ).order_by(ScrapedEvent.event_date.asc().nulls_last()).limit(30)
        
        result = await session.execute(q)
        events = result.scalars().all()
    
    if not events:
        return "Нет актуальных событий"
    
    lines = []
    for e in events:
        date_str = e.event_date.strftime("%d.%m") if e.event_date else "регулярно"
        cat = e.category or ""
        summary = e.summary or e.raw_text[:100] if e.raw_text else "Без описания"
        link = e.link or ""
        lines.append(f"[{date_str}] {cat}: {summary} | {link}")
    
    return "\n".join(lines)

async def get_ai_response(user_message: str, chat_history: list = None) -> str:
    """Получает ответ от AI с учётом базы знаний и афиши"""
    
    # Ищем релевантные места в базе
    relevant_knowledge = find_relevant_knowledge(user_message)
    
    # Ищем события из афиши
    events_text = await get_upcoming_events(user_message)
    
    # Ищем отзывы
    reviews_text = await find_relevant_reviews(user_message)
    
    system = SYSTEM_PROMPT.format(
        knowledge=json.dumps(relevant_knowledge, ensure_ascii=False)[:4000],
        reviews=reviews_text[:2000],
        events=events_text[:3000]
    )
    
    messages = [{"role": "system", "content": system}]
    
    # Добавляем историю (последние 6 сообщений)
    if chat_history:
        messages.extend(chat_history[-6:])
    
    messages.append({"role": "user", "content": user_message})
    
    try:
        response = await client.chat.completions.create(
            model=config.ai_model,
            messages=messages,
            temperature=0.5,
            max_tokens=800
        )
        answer = response.choices[0].message.content or "Не могу ответить 😔"
        # Убираем markdown форматирование
        answer = answer.replace("**", "").replace("*", "")
        return answer
    except Exception as e:
        logging.error(f"AI error: {e}")
        return "Произошла ошибка. Попробуй ещё раз 🔄"


def find_relevant_knowledge(query: str) -> list:
    """Ищет релевантные места в базе знаний"""
    query_lower = query.lower()
    results = []
    
    # Ключевые слова для категорий
    category_keywords = {
        "restaurants": ["ресторан", "поесть", "еда", "кухня", "ужин", "обед", "завтрак"],
        "cafes": ["кафе", "кофе", "бранч", "завтрак"],
        "beaches": ["пляж", "море", "купаться", "песок"],
        "coworkings": ["коворкинг", "работать", "wifi", "ноутбук", "удалёнка"],
        "temples": ["храм", "temple", "достопримечательност"],
        "waterfalls": ["водопад", "waterfall"],
        "surf_spots": ["серф", "surf", "волн"],
        "yoga": ["йога", "yoga", "медитац"],
        "spas": ["спа", "массаж", "spa"],
        "clubs": ["клуб", "club", "вечеринк", "тусовк", "ночн"],
    }
    
    # Определяем релевантные категории
    relevant_categories = []
    for cat, keywords in category_keywords.items():
        if any(kw in query_lower for kw in keywords):
            relevant_categories.append(cat)
    
    # Если категория не определена — ищем по всем
    if not relevant_categories:
        relevant_categories = list(KNOWLEDGE.keys())
    
    # Собираем данные
    for cat in relevant_categories:
        if cat in KNOWLEDGE:
            for item in KNOWLEDGE[cat][:10]:  # Макс 10 из категории
                results.append(item)
    
    return results[:20]  # Макс 20 результатов


async def find_relevant_reviews(query: str) -> str:
    """Ищет релевантные отзывы"""
    query_lower = query.lower()
    reviews_text = ""
    
    # Ищем упоминания мест в запросе
    for cat, items in KNOWLEDGE.items():
        for item in items:
            name = item.get("name", "").lower()
            if name and len(name) > 3 and name in query_lower:
                reviews = await get_place_reviews(item["name"], limit=3)
                if reviews:
                    reviews_text += f"\n📍 {item['name']}:\n"
                    for r in reviews:
                        sentiment_icon = "👍" if r["sentiment"] == "positive" else "👎" if r["sentiment"] == "negative" else "💬"
                        reviews_text += f"  {sentiment_icon} @{r['username']}: {r['text'][:100]}\n"
    
    return reviews_text

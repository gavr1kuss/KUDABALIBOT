from enum import Enum


class EventCategory(str, Enum):
    """Тематические категории событий (8 штук)."""
    ENTERTAINMENT = "Развлечения"
    PRACTICES = "Практики"
    NETWORKING = "Нетворкинг"
    SPORT = "Спорт"
    TRAVEL = "Путешествия"
    CREATIVITY = "Творчество"
    EDUCATION = "Образование"
    NODATE = "Без даты"


# Эмодзи для каждой тематической категории
CATEGORY_ICONS = {
    "Развлечения": "🎭",
    "Практики": "🧘",
    "Нетворкинг": "🤝",
    "Спорт": "⚽",
    "Путешествия": "✈️",
    "Творчество": "🎨",
    "Образование": "🎓",
    "Без даты": "📌",
    # legacy / служебные
    "Spam": "💩",
    "Unknown": "❓",
}

# Эмодзи для ценового тега
PRICE_ICONS = {
    True: "🆓",   # is_free = True
    False: "💰",  # is_free = False
    None: "❔",   # не указано
}

# Все допустимые значения категорий (для валидации ответа DeepSeek)
VALID_CATEGORIES = {c.value for c in EventCategory}

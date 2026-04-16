import operator
from datetime import date
from typing import Optional

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Calendar, Cancel, Column, Row, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format, Jinja

from data.categories import EventCategory, CATEGORY_ICONS, PRICE_ICONS
from database.models import AsyncSessionMaker, ScrapedEvent
from database.requests import (
    create_manual_event,
    update_event_category,
    update_event_date,
    update_event_is_free,
    update_event_status,
    update_event_summary,
)
from services.analyzer import create_recurring_entries, cancel_recurring_series
from sqlalchemy import select, delete

from states import AdminCreateSG, AdminReviewSG


# ---------- HELPERS ----------

def _format_categories(category_str: str | None) -> str:
    """Превращает 'Спорт,Путешествия' в '⚽ Спорт, ✈️ Путешествия'."""
    if not category_str:
        return "❓ Не указана"
    cats = [c.strip() for c in category_str.split(",")]
    parts = []
    for c in cats:
        icon = CATEGORY_ICONS.get(c, "❓")
        parts.append(f"{icon} {c}")
    return ", ".join(parts)


def _format_price(is_free: bool | None) -> str:
    icon = PRICE_ICONS.get(is_free, "❔")
    if is_free is True:
        return f"{icon} Бесплатно"
    elif is_free is False:
        return f"{icon} Платно"
    return f"{icon} Не указано"


# ---------- GETTERS ----------

async def get_next_review_event(dialog_manager: DialogManager, **kwargs):
    async with AsyncSessionMaker() as session:
        ev: Optional[ScrapedEvent] = await session.scalar(
            select(ScrapedEvent)
            .where(ScrapedEvent.status == "review")
            .order_by(ScrapedEvent.created_at.asc())
            .limit(1)
        )

        if not ev:
            dialog_manager.dialog_data["event_id"] = None
            return {"has_event": False}

        dialog_manager.dialog_data["event_id"] = int(ev.id)

        return {
            "has_event": True,
            "id": ev.id,
            "category_display": _format_categories(ev.category),
            "price_display": _format_price(ev.is_free),
            "date": ev.event_date.isoformat() if ev.event_date else "Не указана",
            "link": ev.link or "",
            "summary": ev.summary or "-",
            "raw": (ev.raw_text or "")[:1200],
            "is_recurring": ev.is_recurring,
            "recurrence": ev.recurrence or "",
        }


def _require_event_id(manager: DialogManager) -> int:
    event_id = manager.dialog_data.get("event_id")
    if not event_id:
        raise RuntimeError("event_id is missing in dialog_data")
    return int(event_id)


# ---------- ACTIONS ----------

async def on_approve(c: CallbackQuery, button: Button, manager: DialogManager):
    event_id = _require_event_id(manager)
    async with AsyncSessionMaker() as session:
        ev = await session.get(ScrapedEvent, event_id)
        if ev and ev.is_recurring and ev.recurrence:
            # Регулярное → предлагаем подтвердить серию
            await update_event_status(session, event_id, "approved")
            await c.answer("✅ Подтверждено")
            await manager.switch_to(AdminReviewSG.confirm_recurring)
            return

        await update_event_status(session, event_id, "approved")
    await c.answer("✅ Подтверждено")
    await manager.switch_to(AdminReviewSG.view)


async def on_reject(c: CallbackQuery, button: Button, manager: DialogManager):
    event_id = _require_event_id(manager)
    async with AsyncSessionMaker() as session:
        await update_event_status(session, event_id, "rejected")
    await c.answer("❌ Отклонено")
    await manager.switch_to(AdminReviewSG.view)


# ---------- RECURRING ----------

async def on_approve_recurring(c: CallbackQuery, button: Button, manager: DialogManager):
    """Создать записи на 3 недели вперёд."""
    event_id = _require_event_id(manager)
    count = await create_recurring_entries(event_id, weeks=3)
    await c.answer(f"🔄 Создано {count} записей на 3 недели")
    await manager.switch_to(AdminReviewSG.view)


async def on_skip_recurring(c: CallbackQuery, button: Button, manager: DialogManager):
    """Одобрить без создания серии."""
    await c.answer("✅ Без серии")
    await manager.switch_to(AdminReviewSG.view)


async def on_cancel_series(c: CallbackQuery, button: Button, manager: DialogManager):
    """Отменить серию регулярного события."""
    event_id = _require_event_id(manager)
    count = await cancel_recurring_series(event_id)
    await c.answer(f"🗑 Удалено {count} будущих записей")
    await manager.switch_to(AdminReviewSG.view)


# ---------- EDIT ACTIONS ----------

async def on_summary_input(message: Message, widget: MessageInput, manager: DialogManager):
    event_id = _require_event_id(manager)
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустой текст не сохранён.")
        return
    async with AsyncSessionMaker() as session:
        await update_event_summary(session, event_id, text)
    await message.answer("✅ Текст обновлён")
    await manager.switch_to(AdminReviewSG.view)


async def on_clear_date_review(c: CallbackQuery, button, manager: DialogManager):
    """Убрать дату."""
    event_id = _require_event_id(manager)
    async with AsyncSessionMaker() as session:
        await update_event_date(session, event_id, None)
    await c.answer("✅ Дата убрана")
    await manager.switch_to(AdminReviewSG.view)


async def on_date_selected(c: CallbackQuery, widget: Calendar, manager: DialogManager, selected_date: date):
    event_id = _require_event_id(manager)
    async with AsyncSessionMaker() as session:
        await update_event_date(session, event_id, selected_date)
    await c.answer(f"✅ Дата: {selected_date}")
    await manager.switch_to(AdminReviewSG.view)


async def on_category_selected(c: CallbackQuery, widget: Select, manager: DialogManager, item_id: str):
    event_id = _require_event_id(manager)
    # Добавляем к существующим категориям, если ещё нет
    async with AsyncSessionMaker() as session:
        ev = await session.get(ScrapedEvent, event_id)
        if ev:
            existing = set(c.strip() for c in (ev.category or "").split(",") if c.strip())
            existing.add(item_id)
            # Убираем служебные
            existing.discard("Unknown")
            existing.discard("Spam")
            existing.discard("Duplicate")
            new_cat = ",".join(sorted(existing))
            await update_event_category(session, event_id, new_cat)
    await c.answer(f"✅ + {item_id}")
    await manager.switch_to(AdminReviewSG.view)


async def get_categories(**kwargs):
    return {
        "cats": [(f"{CATEGORY_ICONS.get(c.value, '')} {c.value}", c.value) for c in EventCategory]
    }


# ---------- PRICE EDIT ----------

async def on_set_free(c: CallbackQuery, button: Button, manager: DialogManager):
    event_id = _require_event_id(manager)
    async with AsyncSessionMaker() as session:
        await update_event_is_free(session, event_id, True)
    await c.answer("🆓 Бесплатно")
    await manager.switch_to(AdminReviewSG.view)


async def on_set_paid(c: CallbackQuery, button: Button, manager: DialogManager):
    event_id = _require_event_id(manager)
    async with AsyncSessionMaker() as session:
        await update_event_is_free(session, event_id, False)
    await c.answer("💰 Платно")
    await manager.switch_to(AdminReviewSG.view)


# ---------- CREATE MANUAL EVENT ACTIONS (/add) ----------

async def on_create_summary(message: Message, widget: MessageInput, manager: DialogManager):
    txt = (message.text or "").strip()
    if not txt:
        await message.answer("Пустой текст не сохранён.")
        return
    manager.dialog_data["new_summary"] = txt
    manager.dialog_data["new_date"] = None
    manager.dialog_data["new_category"] = None
    await manager.switch_to(AdminCreateSG.date)


async def on_create_date_selected(c: CallbackQuery, widget: Calendar, manager: DialogManager, selected_date: date):
    manager.dialog_data["new_date"] = selected_date
    await c.answer(f"✅ Дата: {selected_date}")
    await manager.switch_to(AdminCreateSG.category)


async def on_create_skip_date(c: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data["new_date"] = None
    await c.answer("✅ Без даты")
    await manager.switch_to(AdminCreateSG.category)


async def on_create_category_selected(c: CallbackQuery, widget: Select, manager: DialogManager, item_id: str):
    manager.dialog_data["new_category"] = item_id

    summary = manager.dialog_data.get("new_summary")
    new_date = manager.dialog_data.get("new_date")
    category = manager.dialog_data.get("new_category")

    async with AsyncSessionMaker() as session:
        new_id = await create_manual_event(
            session=session,
            summary=summary,
            category=category,
            event_date=new_date,
        )

    await c.answer(f"✅ Добавлено (ID: {new_id})")
    await manager.done()

    await c.message.answer(
        f"🎉 <b>Событие успешно добавлено!</b>\n"
        f"🆔 ID: {new_id}\n"
        f"📂 {category}\n"
        f"📝 {summary[:100]}...",
        parse_mode="HTML"
    )


# ---------- DIALOG 1: REVIEW ----------

review_dialog = Dialog(
    # --- Главный экран ---
    Window(
        Jinja(
            "{% if has_event %}"
            "🚦 <b>Очередь модерации</b>\n\n"
            "🆔 <b>ID:</b> {{id}}\n"
            "📂 <b>Категория:</b> {{category_display}}\n"
            "💵 <b>Цена:</b> {{price_display}}\n"
            "📅 <b>Дата:</b> {{date}}\n"
            "{% if is_recurring %}🔄 <b>Регулярное:</b> {{recurrence}}\n{% endif %}"
            "🔗 <a href='{{link}}'>Источник</a>\n\n"
            "📝 <b>Summary:</b>\n{{summary}}\n\n"
            "📄 <b>Raw:</b>\n<tg-spoiler>{{raw}}</tg-spoiler>\n"
            "{% else %}"
            "✅ Очередь пуста\n"
            "{% endif %}"
        ),
        Row(
            Button(Const("✅ Подтвердить"), id="approve", on_click=on_approve, when="has_event"),
            Button(Const("❌ Отклонить"), id="reject", on_click=on_reject, when="has_event"),
        ),
        Row(
            SwitchTo(Const("✏️ Редактировать"), id="to_edit", state=AdminReviewSG.edit_menu, when="has_event"),
        ),
        Button(Const("🔄 Обновить"), id="refresh", on_click=lambda c, b, m: m.switch_to(AdminReviewSG.view)),
        Cancel(Const("✖ Закрыть")),
        state=AdminReviewSG.view,
        getter=get_next_review_event,
        parse_mode="HTML",
    ),

    # --- Меню редактирования ---
    Window(
        Const("✏️ Редактирование:\nВыбери, что менять."),
        Column(
            SwitchTo(Const("📝 Изменить текст"), id="ed_sum", state=AdminReviewSG.edit_summary),
            SwitchTo(Const("📅 Изменить дату"), id="ed_date", state=AdminReviewSG.edit_date),
            SwitchTo(Const("📂 Добавить категорию"), id="ed_cat", state=AdminReviewSG.edit_category),
            SwitchTo(Const("💵 Изменить цену"), id="ed_price", state=AdminReviewSG.edit_price),
            SwitchTo(Const("🔙 Назад"), id="back1", state=AdminReviewSG.view),
        ),
        state=AdminReviewSG.edit_menu,
    ),

    # --- Редактирование текста ---
    Window(
        Const("📝 Отправь новый текст одним сообщением:"),
        MessageInput(on_summary_input),
        SwitchTo(Const("🔙 Назад"), id="back2", state=AdminReviewSG.view),
        state=AdminReviewSG.edit_summary,
    ),

    # --- Редактирование даты ---
    Window(
        Const("📅 Выбери дату:"),
        Calendar(id="cal_edit", on_click=on_date_selected),
        Button(Const("🚫 Без даты"), id="no_date_review", on_click=on_clear_date_review),
        SwitchTo(Const("🔙 Назад"), id="back3", state=AdminReviewSG.view),
        state=AdminReviewSG.edit_date,
    ),

    # --- Редактирование категории (добавляет к существующим) ---
    Window(
        Const("📂 Выбери категорию (добавится к текущим):"),
        Select(
            Format("{item[0]}"),
            id="cat_edit",
            items="cats",
            item_id_getter=operator.itemgetter(1),
            on_click=on_category_selected,
        ),
        SwitchTo(Const("🔙 Назад"), id="back4", state=AdminReviewSG.view),
        state=AdminReviewSG.edit_category,
        getter=get_categories,
    ),

    # --- Редактирование цены ---
    Window(
        Const("💵 Укажи ценовой тег:"),
        Row(
            Button(Const("🆓 Бесплатно"), id="set_free", on_click=on_set_free),
            Button(Const("💰 Платно"), id="set_paid", on_click=on_set_paid),
        ),
        SwitchTo(Const("🔙 Назад"), id="back5", state=AdminReviewSG.view),
        state=AdminReviewSG.edit_price,
    ),

    # --- Подтверждение регулярного события ---
    Window(
        Const(
            "🔄 <b>Регулярное событие</b>\n\n"
            "Создать записи на 3 недели вперёд?\n"
            "Каждая запись будет отдельным событием в афише."
        ),
        Row(
            Button(Const("✅ Да, на 3 недели"), id="do_recurring", on_click=on_approve_recurring),
            Button(Const("⏭ Без серии"), id="skip_recurring", on_click=on_skip_recurring),
        ),
        Button(Const("🗑 Отменить серию"), id="cancel_series", on_click=on_cancel_series),
        SwitchTo(Const("🔙 Назад"), id="back6", state=AdminReviewSG.view),
        state=AdminReviewSG.confirm_recurring,
        parse_mode="HTML",
    ),
)

# ---------- DIALOG 2: CREATE ----------

create_dialog = Dialog(
    Window(
        Const("➕ Добавление события вручную\n\nОтправь текст (что увидит пользователь) одним сообщением:"),
        MessageInput(on_create_summary),
        Cancel(Const("✖ Отмена")),
        state=AdminCreateSG.summary,
    ),

    Window(
        Const("📅 Выбери дату события (или пропусти):"),
        Calendar(id="cal_new", on_click=on_create_date_selected),
        Button(Const("⏭ Без даты"), id="skip_date", on_click=on_create_skip_date),
        Cancel(Const("✖ Отмена")),
        state=AdminCreateSG.date,
    ),

    Window(
        Const("📂 Выбери категорию:"),
        Select(
            Format("{item[0]}"),
            id="cat_new",
            items="cats",
            item_id_getter=operator.itemgetter(1),
            on_click=on_create_category_selected,
        ),
        Cancel(Const("✖ Отмена")),
        state=AdminCreateSG.category,
        getter=get_categories,
    ),
)

"""
Миграция БД под новую схему категоризации.

1. Добавляет новые колонки: is_free, parent_id, recurrence
2. Конвертирует старые англ. категории → новые русские
3. Проставляет is_free на основе старых Free/Paid

Запуск:
    python3 migrate_categories.py
    python3 migrate_categories.py /path/to/events.db
"""
import os
import sys
import sqlite3


def find_db() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    candidates = ["events.db", "data/events.db"]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # поиск
    for root, _, files in os.walk("."):
        if "venv" in root or ".git" in root:
            continue
        for f in files:
            if f.endswith(".db"):
                return os.path.join(root, f)
    sys.exit("❌ Не нашёл events.db")


# Маппинг старых категорий → новые
OLD_TO_NEW = {
    "Free": "Развлечения",    # Бесплатные — были без темы, ставим дефолт
    "Paid": "Развлечения",    # Платные — были без темы, ставим дефолт
    "Networking": "Нетворкинг",
    "Party": "Развлечения",
    "Unknown": "Развлечения",
}

# is_free на основе старой категории
OLD_TO_FREE = {
    "Free": 1,      # True
    "Paid": 0,       # False
    "Networking": None,
    "Party": None,
    "Unknown": None,
}


def main():
    db_path = find_db()
    print(f"🗄 БД: {db_path}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # 1. Проверяем какие колонки уже есть
    cols = {r[1] for r in cur.execute("PRAGMA table_info(scraped_events)")}
    print(f"📋 Существующие колонки: {sorted(cols)}")

    # 2. Добавляем недостающие колонки
    new_cols = {
        "is_free": "BOOLEAN",
        "parent_id": "INTEGER REFERENCES scraped_events(id)",
        "recurrence": "TEXT",
    }

    for col_name, col_type in new_cols.items():
        if col_name not in cols:
            cur.execute(f"ALTER TABLE scraped_events ADD COLUMN {col_name} {col_type}")
            print(f"  ✅ Добавлена колонка: {col_name}")
        else:
            print(f"  ⏭ Уже есть: {col_name}")

    con.commit()

    # 3. Конвертируем старые категории
    rows = cur.execute("SELECT id, category FROM scraped_events WHERE category IS NOT NULL").fetchall()
    updated = 0
    for row_id, old_cat in rows:
        if old_cat in OLD_TO_NEW:
            new_cat = OLD_TO_NEW[old_cat]
            is_free = OLD_TO_FREE.get(old_cat)
            cur.execute(
                "UPDATE scraped_events SET category = ?, is_free = ? WHERE id = ?",
                (new_cat, is_free, row_id)
            )
            updated += 1

    con.commit()
    print(f"🔄 Конвертировано категорий: {updated}")

    # 4. Статистика
    stats = cur.execute(
        "SELECT category, COUNT(*) FROM scraped_events WHERE status != 'rejected' GROUP BY category"
    ).fetchall()
    print("\n📊 Текущее распределение:")
    for cat, cnt in stats:
        print(f"  {cat}: {cnt}")

    total_review = cur.execute("SELECT COUNT(*) FROM scraped_events WHERE status = 'review'").fetchone()[0]
    total_approved = cur.execute("SELECT COUNT(*) FROM scraped_events WHERE status = 'approved'").fetchone()[0]
    total_pending = cur.execute("SELECT COUNT(*) FROM scraped_events WHERE status = 'pending'").fetchone()[0]
    print(f"\n📦 Review: {total_review}, Approved: {total_approved}, Pending: {total_pending}")

    con.close()
    print("\n✅ Миграция завершена!")


if __name__ == "__main__":
    main()

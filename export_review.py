"""
Выгрузка всех записей из очереди /review в CSV для ручной категоризации.

Использует голый sqlite3 — никаких зависимостей, работает в любом окружении.

Запуск:
    python3 export_review.py
    python3 export_review.py /path/to/events.db   # если БД не найдена автоматически

Итог: review_export.csv рядом со скриптом.
"""
import csv
import os
import sys
import sqlite3


OUTPUT = "review_export.csv"


def find_db() -> str:
    # 1) аргумент командной строки
    if len(sys.argv) > 1:
        p = sys.argv[1]
        if not os.path.isfile(p):
            sys.exit(f"❌ Файл не найден: {p}")
        return p

    # 2) переменная окружения DATABASE_URL (sqlite+aiosqlite:///events.db → events.db)
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and os.path.isfile(path):
            return path

    # 3) типовые имена рядом со скриптом / в CWD
    candidates = [
        "events.db",
        "kudabali.db",
        "bot.db",
        "data/events.db",
        "data/kudabali.db",
    ]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), script_dir):
        for name in candidates:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p

    # 4) рекурсивный поиск *.db в проекте (неглубоко)
    found = []
    for base in (os.getcwd(), script_dir):
        for root, _, files in os.walk(base):
            # не лезем в venv и служебные папки
            if any(skip in root for skip in ("venv", ".venv", "__pycache__", ".git", "node_modules")):
                continue
            for f in files:
                if f.endswith(".db") or f.endswith(".sqlite") or f.endswith(".sqlite3"):
                    found.append(os.path.join(root, f))
        if found:
            break

    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        print("Найдено несколько БД, уточни аргументом:")
        for p in found:
            print(" -", p)
        sys.exit(1)

    sys.exit("❌ Не нашёл файл БД. Запусти так:  python3 export_review.py /путь/к/events.db")


def main():
    db_path = find_db()
    print(f"🗄  БД: {db_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # какая таблица?
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    table = None
    for candidate in ("scraped_events", "events"):
        if candidate in tables:
            table = candidate
            break
    if not table:
        sys.exit(f"❌ В БД нет ни scraped_events, ни events. Есть таблицы: {sorted(tables)}")
    print(f"📋 Таблица: {table}")

    # какие колонки реально есть
    cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}

    # строим SELECT с теми колонками, что есть
    def col(name, default="''"):
        return name if name in cols else f"{default} AS {name}"

    select_sql = f"""
        SELECT
            id,
            {col('chat_title')},
            {col('link')},
            {col('category')},
            {col('summary')},
            {col('raw_text') if 'raw_text' in cols else col('text') + ' AS raw_text'}
        FROM {table}
        WHERE { "status = 'review'" if 'status' in cols else "1=1" }
        ORDER BY { 'created_at ASC' if 'created_at' in cols else 'id ASC' }
    """
    rows = cur.execute(select_sql).fetchall()
    print(f"📦 Записей в /review: {len(rows)}")

    with open(OUTPUT, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writerow([
            "id", "chat_title", "link",
            "current_cat", "my_category",
            "summary", "raw_text",
        ])
        for r in rows:
            raw = (r["raw_text"] or "").replace("\r", " ").replace("\n", " ")
            summary = (r["summary"] or "").replace("\r", " ").replace("\n", " ")
            writer.writerow([
                r["id"],
                r["chat_title"] or "",
                r["link"] or "",
                r["category"] or "",
                "",  # my_category — заполнить вручную
                summary,
                raw,
            ])

    print(f"✅ Сохранено: {OUTPUT}")
    con.close()


if __name__ == "__main__":
    main()

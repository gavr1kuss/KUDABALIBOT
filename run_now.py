import asyncio
from database.session import init_db
from services.site_parser import run_site_parser
from services.collector import scheduled_chat_scan
from services.analyzer import run_batch_analysis
from services.dedup import run_full_dedup

async def main():
    await init_db()
    print('=== ПАРСИНГ САЙТА ===')
    saved_site = await run_site_parser()
    print(f'Сайт: сохранено {saved_site}')
    print('=== ПАРСИНГ ЧАТОВ ===')
    saved_chats = await scheduled_chat_scan()
    print(f'Чаты: сохранено {saved_chats}')
    print('=== ДЕДУПЛИКАЦИЯ ===')
    dedup = await run_full_dedup()
    print(f'Удалено дублей: {dedup}')
    print('=== АНАЛИЗ ===')
    result = await run_batch_analysis()
    print(result)

asyncio.run(main())

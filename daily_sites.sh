#!/bin/bash
cd /opt/kudabali

exec 9>/tmp/kudabali_daily.lock
flock -n 9 || { echo "=== $(date) === Daily уже запущен, пропускаю" >> /var/log/kudabali.log; exit 0; }

source venv/bin/activate

echo "=== $(date) === DAILY sites START" >> /var/log/kudabali.log

timeout 900 python3 -m services.extra_sites_parser >> /var/log/kudabali.log 2>&1

# Прогнать AI-анализ для свежих записей
timeout 1800 python3 << 'PYEOF' >> /var/log/kudabali.log 2>&1
import asyncio
from services.analyzer import run_batch_analysis
asyncio.run(run_batch_analysis())
PYEOF

echo "✅ DAILY done $(date)" >> /var/log/kudabali.log

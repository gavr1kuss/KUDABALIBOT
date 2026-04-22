#!/bin/bash
cd /opt/kudabali

exec 9>/tmp/kudabali_pipeline.lock
flock -n 9 || { echo "=== $(date) === Уже запущен, пропускаю" >> /var/log/kudabali.log; exit 0; }

source venv/bin/activate

echo "=== $(date) === START" >> /var/log/kudabali.log

# 1. Telegram scan
echo "--- TG scan ---" >> /var/log/kudabali.log
timeout 1800 python3 scan_last_2_days.py >> /var/log/kudabali.log 2>&1

# 2. Site parser (baliforum.ru)
echo "--- Site parser ---" >> /var/log/kudabali.log
timeout 900 python3 -m services.site_parser >> /var/log/kudabali.log 2>&1

# 3. AI analyzer
echo "--- AI analyzer ---" >> /var/log/kudabali.log
timeout 1800 python3 << 'PYEOF' >> /var/log/kudabali.log 2>&1
import asyncio
from services.analyzer import run_batch_analysis
asyncio.run(run_batch_analysis())
PYEOF

echo "✅ Завершено $(date)" >> /var/log/kudabali.log

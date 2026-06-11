#!/bin/bash
set -e

mkdir -p /app/data
touch /var/log/cron.log

# 讓 cron job 可以讀到容器環境變數
printenv | grep -E "^(BFX_)" >> /etc/environment

# 啟動 crond（背景）
service cron start

# 若 settings.json 記錄排程已啟用，自動還原 crontab
python3 - <<'PYEOF'
import json, os, subprocess

p = '/app/data/settings.json'
if not os.path.exists(p):
    exit()

s = json.load(open(p))
if not s.get('schedule_enabled'):
    exit()

interval = s.get('cron_interval_minutes', 60)
if interval <= 0:
    exit()

if interval < 60:
    cron_time = f'*/{interval} *'
elif interval == 60:
    cron_time = '0 *'
elif interval == 1440:
    cron_time = '0 0'
else:
    cron_time = f'0 */{interval // 60}'

entry = f'{cron_time} * * * /usr/local/bin/python3 /app/lending.py > /app/data/lending_last.log 2>&1\n'
subprocess.run(['crontab', '-'], input=entry, text=True, check=True)
print(f'[entrypoint] crontab restored: {entry.strip()}')
PYEOF

# 啟動 Cloudflare Tunnel（背景，僅在有 token 時啟動）
if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then
    cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
fi

# 啟動 Flask（前景，保持容器存活）
cd /app/dashboard
exec python3 app.py

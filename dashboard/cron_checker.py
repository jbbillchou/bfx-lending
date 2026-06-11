import subprocess
import datetime

TZ_OFFSET = datetime.timezone(datetime.timedelta(hours=8))


def _now_tw():
    return datetime.datetime.now(tz=TZ_OFFSET)


def _calc_next_run(cron_line):
    parts = cron_line.split()
    if len(parts) < 5:
        return None
    minute_field = parts[0]
    now = _now_tw()

    if minute_field.startswith('*/'):
        interval = int(minute_field[2:])
        next_minute = ((now.minute // interval) + 1) * interval
        if next_minute >= 60:
            next_run = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        else:
            next_run = now.replace(minute=next_minute, second=0, microsecond=0)
        return next_run.strftime('%Y-%m-%d %H:%M')

    if minute_field.isdigit():
        minute = int(minute_field)
        next_run = now.replace(minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += datetime.timedelta(hours=1)
        return next_run.strftime('%Y-%m-%d %H:%M')

    return None


def get_cron_status():
    try:
        result = subprocess.run(
            ['crontab', '-l'],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return {'is_configured': False, 'cron_line': None, 'next_run': None}

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith('#') or not stripped:
                continue
            if 'lending.py' in stripped:
                return {
                    'is_configured': True,
                    'cron_line': stripped,
                    'next_run': _calc_next_run(stripped),
                }

        return {'is_configured': False, 'cron_line': None, 'next_run': None}

    except FileNotFoundError:
        return {'is_configured': False, 'cron_line': None, 'next_run': None}

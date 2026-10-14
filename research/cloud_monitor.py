"""One public-data snapshot plus a rolling report; no AI and no order API."""
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import zipfile
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / 'reports/monitor'

def restore():
    token, repo = os.getenv('GITHUB_TOKEN'), os.getenv('GITHUB_REPOSITORY')
    if not token or not repo: return 'local_run_no_restore'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    url = f'https://api.github.com/repos/{repo}/actions/artifacts?name=crypto-monitor-state&per_page=10'
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    available = [a for a in response.json()['artifacts'] if not a['expired']]
    if not available: return 'new_history_no_previous_artifact'
    artifact = max(available, key=lambda a: a['created_at'])
    # Never pass repository credentials on a cross-host redirect.
    response = requests.get(artifact['archive_download_url'], headers=headers, timeout=30, allow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers['Location']
        if not location.startswith('https://'): raise ValueError('unsafe artifact redirect')
        response = requests.get(location, timeout=60)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        if 'market.sqlite' not in archive.namelist(): raise ValueError('missing market.sqlite')
        if archive.getinfo('market.sqlite').file_size > 100_000_000: raise ValueError('state too large')
        (OUT / 'market.sqlite').write_bytes(archive.read('market.sqlite'))
    return 'restored_artifact_' + str(artifact['id'])

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    history_error = None
    try: history = restore()
    except Exception as exc:
        history = 'HISTORY_UNAVAILABLE'
        history_error = type(exc).__name__
    print('History continuity: ' + history)
    spec = importlib.util.spec_from_file_location('watch', ROOT / 'crypto-monitor/crypto_watch.py')
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
    data = watch.collect()
    if history_error:
        data['errors'].append('History restore failed: ' + history_error)
        data['status'] = 'PARZIALE'
    watch.save(data, OUT)
    now = dt.datetime.now(dt.timezone.utc)
    with sqlite3.connect(OUT / 'market.sqlite') as db:
        db.execute('DELETE FROM runs WHERE collected_at < ?', ((now-dt.timedelta(days=30)).isoformat(),))
        db.commit()
        rows = db.execute('SELECT collected_at,status,payload FROM runs WHERE collected_at >= ? ORDER BY collected_at',
                          ((now-dt.timedelta(hours=24)).isoformat(),)).fetchall()
    report = '\n'.join([
        '# Crypto research: rolling 24 hours',
        'Generated UTC: ' + now.isoformat(),
        'History: ' + history,
        'Snapshots in last 24h: ' + str(len(rows)),
        'Partial snapshots: ' + str(sum(r[1] == 'PARZIALE' for r in rows)),
        'Scheduled hourly; gaps and delays are possible. Count is not a coverage guarantee.',
        'No validated entries. Security, holders and executable exits remain unverified.',
        'Historical Crypto/Noise/Carry backtests remain blocked by missing datasets.',
        '',
    ] + [r[0] + ' | ' + r[1] + ' | pools=' + str(len(json.loads(r[2])['pairs'])) for r in rows])
    (OUT / 'daily_report.md').write_text(report + '\n', encoding='utf-8')
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        # Only controlled metadata appears in Markdown; provider text stays in downloadable artifacts.
        with open(summary, 'a', encoding='utf-8') as f: f.write(report + '\n')
    return 0 if data['sources'] and not history_error else 2

if __name__ == '__main__':
    sys.exit(main())

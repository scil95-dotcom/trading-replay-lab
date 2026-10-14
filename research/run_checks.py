"""Execute recovered regressions; never connects to a broker or market provider."""
import ast
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def main():
    out = ROOT.parent / 'reports'
    out.mkdir(exist_ok=True)
    results = []
    for name, args in [
        ('crypto_carry_regressions', [str(ROOT / 'verify_repairs.py')]),
        ('monitor_offline_regressions', [str(ROOT / 'verify_monitor.py'), str(ROOT / 'crypto-monitor/crypto_watch.py')]),
        ('noise_order_cli_block', [str(ROOT / 'reviewed/noise/02_Bot/noise_bot.py'), 'run', '--config', 'MUST_NOT_BE_READ.json']),
    ]:
        p = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=180)
        passed = (p.returncode != 0 and 'AVVIO ORDINI BLOCCATO' in p.stderr) if name == 'noise_order_cli_block' else p.returncode == 0
        results.append(dict(check=name, passed=passed, exit_code=p.returncode, stdout=p.stdout, stderr=p.stderr))
    for file in ROOT.rglob('*.py'):
        ast.parse(file.read_text(encoding='utf-8-sig'))
    payload = dict(generated_at=dt.datetime.now(dt.timezone.utc).isoformat(), checks=results,
                   historical_backtests='BLOCKED_MISSING_DATASETS', real_capital='NOT_APPROVED')
    (out / 'checks.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    for r in results:
        print(('PASS' if r['passed'] else 'FAIL') + ': ' + r['check'])
        if not r['passed']: print(r['stdout'], r['stderr'])
    return 0 if all(r['passed'] for r in results) else 1

if __name__ == '__main__':
    sys.exit(main())

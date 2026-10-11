"""Repair independently missing A-share series and gate Pages on usable coverage.

Recent-only recovery is explicitly labelled, retried on future updates, and never
joined to old adjusted prices from a different adjustment basis.
"""
import argparse
import datetime
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from pipeline import sources_market as sm


def valid_bars(bars):
    if not isinstance(bars, list) or not bars:
        return False
    try:
        dates = [b['d'] for b in bars]
        if dates != sorted(set(dates)):
            return False
        for b in bars:
            datetime.date.fromisoformat(b['d'])
            o, c, h, l, v = (float(b[k]) for k in ('o', 'c', 'h', 'l', 'v'))
            if not all(math.isfinite(x) for x in (o, c, h, l, v)):
                return False
            if h < max(o, c, l) or l > min(o, c, h) or v < 0:
                return False
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def read_series(path):
    try:
        p = json.loads(path.read_text(encoding='utf-8'))
        return p if isinstance(p, dict) and valid_bars(p.get('bars')) else None
    except (OSError, ValueError, TypeError):
        return None


def universe(data, cfg):
    snap = json.loads((data / 'snapshot.json').read_text(encoding='utf-8'))
    stocks = {str(x['code']): x.get('name', '') for x in snap['items']}
    # Detail pages are also reachable through these independent stock lists.
    for name in ('soe', 'social_security', 'dma_low30', 'insider_buys'):
        try:
            p = json.loads((data / 'meta' / (name + '.json')).read_text(encoding='utf-8'))
            def collect(x):
                if isinstance(x, dict):
                    code = str(x.get('code', ''))
                    if len(code) == 6 and code.isdigit():
                        stocks.setdefault(code, x.get('name', ''))
                    for value in x.values():
                        if isinstance(value, (dict, list)):
                            collect(value)
                elif isinstance(x, list):
                    for value in x:
                        collect(value)
            collect(p)
        except (OSError, ValueError, TypeError):
            pass
    excluded = tuple(cfg.get('exclude_code_prefixes', ['300', '688']))
    return snap['date'], {c: n for c, n in stocks.items() if not c.startswith(excluded)}


def audit(data, stocks, target):
    missing, stale, partial = [], [], []
    for code in sorted(stocks):
        for mode, folder in (('qfq', 'kline'), ('raw', 'kline_raw')):
            p = read_series(data / folder / (code + '.json'))
            key = code + ':' + mode
            if not p:
                missing.append(key)
                continue
            if p['bars'][-1]['d'] < target:
                stale.append(key)
            if p.get('history_complete') is False:
                partial.append(key)
    return dict(target_date=target, stocks=len(stocks), expected_series=len(stocks)*2,
                usable_series=len(stocks)*2-len(missing), missing=missing,
                stale=stale, partial_history=partial)


def repair_one(data, code, name, target, cfg, recent_first=False):
    results = []
    for qfq, folder in ((True, 'kline'), (False, 'kline_raw')):
        path = data / folder / (code + '.json')
        old = read_series(path)
        if old and old.get('history_complete') is not False:
            continue
        if recent_first and old and old['bars'][-1]['d'] >= target:
            continue
        errors, bars, complete = [], [], False
        # Sequential retry at bounded concurrency; don't throw away the other mode.
        for _ in range(0 if recent_first else 2):
            try:
                if qfq:
                    _, bars = sm.fetch_kline(code, cfg.get('kline_start', '19900101'), '20500101', target)
                else:
                    bars = sm.fetch_kline_raw(code, cfg.get('kline_start', '19900101'), '20500101', target)
                if not valid_bars(bars):
                    raise RuntimeError('historical series empty or invalid')
                complete = True
                break
            except Exception as exc:
                errors.append(str(exc)[:400])
                bars = []
        if not bars:
            try:
                recent = sm.fetch_kline_recent(code, qfq=qfq)
                if not valid_bars(recent):
                    raise RuntimeError('recent series empty or invalid')
                # Keep an existing longer series if the new one cannot improve it.
                if old and old['bars'][-1]['d'] >= recent[-1]['d']:
                    results.append(dict(code=code, mode=folder, status='preserved', errors=errors))
                    continue
                bars = recent
            except Exception as exc:
                errors.append(str(exc)[:400])
                results.append(dict(code=code, mode=folder, status='failed', errors=errors))
                continue
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        payload = dict(code=code, name=name, date=bars[-1]['d'], updated=now,
                       full_date=now[:10] if complete else '', bars=bars,
                       coverage_start=bars[0]['d'], coverage_end=bars[-1]['d'],
                       coverage_complete=complete and bars[-1]['d'] >= target, history_complete=complete,
                       source='eastmoney-or-tencent' if complete else 'tencent-recent',
                       repair_errors=errors)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A failed fetch never opens an existing file for writing.
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        results.append(dict(code=code, mode=folder, status='full' if complete else 'recent',
                            count=len(bars), start=bars[0]['d'], end=bars[-1]['d'], errors=errors))
    return results


def write_report(data, report):
    (data / 'meta').mkdir(parents=True, exist_ok=True)
    (data / 'meta/kline_coverage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    line = ('K线覆盖：%d/%d 套可用；缺失 %d；陈旧 %d；早期历史待补 %d'
            % (report['usable_series'], report['expected_series'], len(report['missing']),
               len(report['stale']), len(report['partial_history'])))
    print(line, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('\n### 个股K线覆盖\n' + line + '\n')
            for label, key in (('缺失', 'missing'), ('陈旧', 'stale'), ('早期历史待补', 'partial_history')):
                if report[key]:
                    f.write('\n' + label + ': ' + ', '.join(report[key]) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repair', action='store_true')
    ap.add_argument('--recent-first', action='store_true', help='Restore visible charts promptly; daily update retries full history')
    ap.add_argument('--data-root', type=Path, default=BASE / 'data')
    ap.add_argument('--codes', default='', help='Targeted local repair; deployment gate always audits the entire universe')
    args = ap.parse_args()
    cfg = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
    target, stocks = universe(args.data_root, cfg)
    report = audit(args.data_root, stocks, target)
    repaired = []
    if args.repair:
        todo = sorted({x.split(':')[0] for x in report['missing'] + report['partial_history']})
        if args.codes:
            wanted = set(args.codes.split(','))
            todo = [c for c in todo if c in wanted]
        print('补抓 %d 只，最大并发4' % len(todo), flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(repair_one, args.data_root, c, stocks[c], target, cfg, args.recent_first): c for c in todo}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    result = future.result()
                except Exception as exc:
                    result = [dict(code=futures[future], status='failed', errors=[str(exc)[:400]])]
                repaired.extend(result)
                print('补抓 %d/%d %s %s' % (i, len(todo), futures[future], ','.join(x['status'] for x in result)), flush=True)
        report = audit(args.data_root, stocks, target)
    report['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    report['repairs'] = repaired
    write_report(args.data_root, report)
    if not stocks or report['missing']:
        print('拒绝部署：页面股票池仍有缺失/空白/损坏的K线文件', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())

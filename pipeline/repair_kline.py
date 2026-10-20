"""Repair independently missing A-share series and gate Pages on usable coverage.

Recent-only recovery is explicitly labelled, retried on future updates, and never
joined to old adjusted prices from a different adjustment basis.
"""
import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from pipeline import sources_market as sm
from pipeline.kline import (valid_bars, read_series, merge_bars, fetched_span_complete,
                            align_adjusted_history)

# 陈旧比例阈值：超过 WARN 输出 ::warning::，超过 FAIL 让本步骤失败（不提交、不部署陈旧数据）。
STALE_WARN_RATIO = float(os.environ.get('KLINE_STALE_WARN_RATIO', '0.10'))
STALE_FAIL_RATIO = float(os.environ.get('KLINE_STALE_FAIL_RATIO', '0.50'))




def universe(data, cfg):
    snap = json.loads((data / 'snapshot.json').read_text(encoding='utf-8'))
    stocks = {str(x['code']): x.get('name', '') for x in snap['items']}
    # Detail pages are also reachable through these independent stock lists.
    for name in ('soe', 'social_security', 'dma_low30', 'ma60_year_low', 'insider_buys'):
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


def _improves(old, bars):
    """True when the candidate adds history or a newer end without shrinking."""
    if not old:
        return True
    prev = old['bars']
    if len(bars) > len(prev) and bars[-1]['d'] >= prev[-1]['d']:
        return True
    return bars[-1]['d'] > prev[-1]['d'] and len(bars) >= len(prev)


def refresh_stale(path, old, code, target, qfq, folder):
    """完整历史但最后一根K线落后于目标交易日：从末尾前 15 天增量补抓并合并。"""
    last = old['bars'][-1]['d']
    begin = (datetime.date.fromisoformat(last) - datetime.timedelta(days=15)).strftime('%Y%m%d')
    try:
        if qfq:
            _, bars = sm.fetch_kline(code, begin, '20500101', target)
        else:
            bars = sm.fetch_kline_raw(code, begin, '20500101', target)
        if not valid_bars(bars) or bars[-1]['d'] <= last:
            raise RuntimeError('各数据源均未取到 %s 之后的K线' % last)
        prev = align_adjusted_history(old['bars'], bars) if qfq else old['bars']
        merged = merge_bars(prev, bars)
        if not valid_bars(merged):
            raise RuntimeError('merged series invalid')
    except Exception as exc:
        return dict(code=code, mode=folder, status='failed', errors=[str(exc)[:400]])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    payload = dict(old, date=merged[-1]['d'], updated=now, bars=merged,
                   coverage_end=merged[-1]['d'], coverage_complete=merged[-1]['d'] >= target)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return dict(code=code, mode=folder, status='updated', count=len(merged),
                start=merged[0]['d'], end=merged[-1]['d'], errors=[])


def repair_one(data, code, name, target, cfg, recent_first=False):
    results = []
    for qfq, folder in ((True, 'kline'), (False, 'kline_raw')):
        path = data / folder / (code + '.json')
        old = read_series(path)
        if old and old.get('history_complete') is not False:
            if old['bars'][-1]['d'] < target:
                results.append(refresh_stale(path, old, code, target, qfq, folder))
            continue
        if recent_first and old and old['bars'][-1]['d'] >= target:
            continue
        errors, bars, complete = [], [], False
        # Sequential retry at bounded concurrency; don't throw away the other mode.
        # A hard 400/404/501 walk is not repeated; transient failures still retry once.
        for _ in range(0 if recent_first else 2):
            try:
                if qfq:
                    _, bars = sm.fetch_kline(code, cfg.get('kline_start', '19900101'), '20500101', target)
                else:
                    bars = sm.fetch_kline_raw(code, cfg.get('kline_start', '19900101'), '20500101', target)
                if not valid_bars(bars):
                    raise RuntimeError('historical series empty or invalid')
                complete = fetched_span_complete(bars)
                errors.extend(str(g)[:400] for g in (getattr(bars, 'gaps', None) or [])[:5])
                break
            except Exception as exc:
                errors.append(str(exc)[:400])
                bars = []
                if getattr(exc, 'unavailable', False):
                    break
        if not bars:
            try:
                recent = sm.fetch_kline_recent(code, qfq=qfq)
                if not valid_bars(recent):
                    raise RuntimeError('recent series empty or invalid')
                bars = recent
                complete = False
                errors.extend(str(g)[:400] for g in (getattr(recent, 'gaps', None) or [])[:5])
            except Exception as exc:
                errors.append(str(exc)[:400])
                results.append(dict(code=code, mode=folder, status='failed', errors=errors))
                continue
        # Raw prices are stable, so an incomplete fetch can extend the cache.
        # Adjusted prices stay unmerged; a shorter partial must not replace a longer file.
        if old and not complete and folder == 'kline_raw':
            bars = merge_bars(old['bars'], bars)
            if not valid_bars(bars):
                results.append(dict(code=code, mode=folder, status='failed', errors=errors + ['merged raw series invalid']))
                continue
        elif not _improves(old, bars):
            results.append(dict(code=code, mode=folder, status='preserved', errors=errors))
            continue
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        payload = dict(code=code, name=name, date=bars[-1]['d'], updated=now,
                       full_date=now[:10] if complete else '', bars=bars,
                       coverage_start=bars[0]['d'], coverage_end=bars[-1]['d'],
                       coverage_complete=complete and bars[-1]['d'] >= target, history_complete=complete,
                       source='eastmoney-or-tencent' if complete else 'tencent-partial',
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
    line = ('K线覆盖：%d/%d 套可用；缺失 %d；陈旧 %d（%.1f%%，目标日 %s）；早期历史待补 %d'
            % (report['usable_series'], report['expected_series'], len(report['missing']),
               len(report['stale']), report.get('stale_ratio', 0) * 100, report['target_date'],
               len(report['partial_history'])))
    print(line, flush=True)
    if os.environ.get('GITHUB_ACTIONS'):
        print('::notice title=Kline coverage::' + line, flush=True)
        for key in report['missing']:
            code, mode = key.split(':')
            folder = 'kline' if mode == 'qfq' else 'kline_raw'
            errors = [e for r in report.get('repairs', [])
                      if r.get('code') == code and r.get('mode') == folder
                      for e in r.get('errors', [])]
            detail = (key + ' ' + ' | '.join(errors)).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print('::error title=Missing Kline::' + detail, flush=True)
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
        todo = sorted({x.split(':')[0] for x in report['missing'] + report['partial_history'] + report['stale']})
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
    report['stale_ratio'] = round(len(report['stale']) / report['expected_series'], 4) if report['expected_series'] else 0
    write_report(args.data_root, report)
    ratio = report['stale_ratio']
    if ratio > STALE_WARN_RATIO:
        print('::warning title=K线陈旧::%d/%d 套K线未更新到 %s（%.1f%%，阈值 %.0f%%）'
              % (len(report['stale']), report['expected_series'], target, ratio * 100,
                 STALE_WARN_RATIO * 100), flush=True)
    if ratio > STALE_FAIL_RATIO:
        print('::error title=K线陈旧超限::陈旧 %.1f%% 超过 %.0f%%，拒绝提交/部署陈旧指标'
              % (ratio * 100, STALE_FAIL_RATIO * 100), flush=True)
        return 1
    # Early-history gaps (history_complete=false) are usable and do not block deploy.
    # Absent files, blank JSON, and broken OHLC stay in `missing` and still refuse deploy.
    if not stocks or report['missing']:
        print('拒绝部署：页面股票池仍有缺失/空白/损坏的K线文件', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())

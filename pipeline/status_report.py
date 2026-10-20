"""汇报各模块真实数据日期（写入 GITHUB_STEP_SUMMARY），陈旧模块输出 ::warning::。

步骤结果来自环境变量 OUTCOME_<模块>；数据日期从 data/ 下的产物读取，不使用运行日期。
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(rel):
    try:
        return json.loads((BASE / 'data' / rel).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _kline_dates(folder):
    """统计目录内各序列最后一根K线日期 -> Counter。"""
    dates = Counter()
    for p in sorted((BASE / 'data' / folder).glob('*.json')):
        try:
            bars = json.loads(p.read_text(encoding='utf-8')).get('bars') or []
        except (OSError, ValueError):
            continue
        if bars and isinstance(bars[-1], dict):
            dates[str(bars[-1].get('d', ''))] += 1
    return dates


def _kline_note(dates, target):
    total = sum(dates.values())
    if not total:
        return '', '无K线文件'
    latest = max(dates)
    behind = sum(n for d, n in dates.items() if d < target) if target else 0
    return latest, '最新 %s · 落后于 %s 的 %d/%d（%.0f%%）' % (
        latest, target or '—', behind, total, behind * 100.0 / total)


def rows():
    a = _load('snapshot.json')
    trade = a.get('date', '')
    cov = _load('meta/kline_coverage.json')
    hk = _load('hk/snapshot.json')
    cb = _load('cbonds.json')
    out = [('A股估值快照', 'update', trade, '%s 只' % a.get('total', '—'))]
    stale = len(cov.get('stale') or [])
    exp = cov.get('expected_series') or 0
    adates = _kline_dates('kline') + _kline_dates('kline_raw')
    latest, note = _kline_note(adates, cov.get('target_date') or trade)
    out.append(('A股K线', 'repair', latest,
                note + ' · 覆盖报告陈旧 %d/%d' % (stale, exp) if exp else note,
                bool(exp) and stale / exp > 0.10))
    for name, rel in (('MA60年低表', 'meta/ma60_year_low.json'), ('DMA低位', 'meta/dma_low30.json')):
        out.append((name, 'update', _load(rel).get('date', ''), '依赖A股K线'))
    hk_latest, hk_note = _kline_note(_kline_dates('hk/kline'), hk.get('date', ''))
    out.append(('港股通', 'hk', hk.get('date', ''),
                '成分 %s（%s）· K线最新 %s · 本轮K线失败 %s · %s' % (
                    hk.get('components_asof') or '—', hk.get('components_source') or '—',
                    hk.get('kline_date') or hk_latest or '—', hk.get('kline_failed', '—'), hk_note)))
    out.append(('可转债', 'cbonds', cb.get('date', ''), '来源 %s · 已上市 %s / 未上市 %s' % (
        cb.get('source') or 'push2', (cb.get('total') or {}).get('listed', '—'),
        (cb.get('total') or {}).get('pending', '—'))))
    out.append(('红利/宽基基金', 'funds', _load('funds.json').get('date', ''), ''))
    out.append(('社保基金', 'social', _load('meta/social_security.json').get('report_date', ''), '季报口径'))
    out.append(('国资红利', 'soe', _load('meta/soe.json').get('date', ''), ''))
    out.append(('调仓提醒', 'rebalance', _load('meta/rebalance.json').get('as_of', ''), ''))
    return trade, out


def main():
    trade, table = rows()
    lines = ['| 模块 | 执行结果 | 数据日期 | 说明 |', '| --- | --- | --- | --- |']
    for name, step, date, note, *forced in table:
        outcome = os.environ.get('OUTCOME_' + step.upper(), '') or '—'
        flag = ''
        # 社保是季报口径，不和交易日比较；A股K线另按陈旧比例 >10% 判定
        if any(forced) or (step != 'social' and trade and (not date or date < trade)):
            flag = ' ⚠️陈旧'
            print('::warning title=%s 数据陈旧::数据日期 %s，A股交易日 %s' % (name, date or '无', trade))
        lines.append('| %s | %s | %s%s | %s |' % (name, outcome, date or '—', flag, note))
    lines.append('')
    lines.append('失败或陈旧模块保留此前快照；“数据日期”取自各模块产物，不是运行日期。')
    text = '\n'.join(lines) + '\n'
    print(text)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('\n### 模块数据日期\n' + text)
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())

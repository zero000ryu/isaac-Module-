#!/usr/bin/env python3
"""Taipei City Dental Association clinic directory (www.tda.org.tw/people/hospital/content.asp?ID=N).

Each page gives the clinic name, the responsible dentist (負責醫師, i.e. 院長) with one character
masked (e.g. 劉O川), address, phone, fax and website. build_dental.py matches the page to a clinic by
phone or name and resolves the masked name against that clinic's known dentists.

Writes data/tda.json. Polite: one request every --delay seconds, stops after a run of empty IDs.
"""
import argparse, html, json, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = 'https://www.tda.org.tw/people/hospital/content.asp?ID={}'
UA = 'Mozilla/5.0 (compatible; dental-roster-builder/1.0)'
FIELDS = {'診所名稱': 'name', '負責醫師': 'ownerMasked', '診所地址': 'address', '診所電話': 'phone', '診所傳真': 'fax', '網站': 'website'}


def page(i, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(URL.format(i), headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                s = r.read().decode('utf-8', errors='replace')
            break
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(3 * (k + 1))
    s = re.sub(r'<script.*?</script>|<style.*?</style>', '', s, flags=re.S)
    t = html.unescape(re.sub(r'<[^>]+>', '|', s))
    cells = [x.strip() for x in re.split(r'\|+', re.sub(r'\s+', ' ', t)) if x.strip()]
    out = {}
    for k, f in FIELDS.items():
        if k in cells:
            j = cells.index(k)
            v = cells[j + 1] if j + 1 < len(cells) else ''
            if v not in FIELDS and v not in ('診所介紹', '返回'):
                out[f] = v
    return out if out.get('name') else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', type=int, default=1)
    ap.add_argument('--max-id', type=int, default=4000)
    ap.add_argument('--stop-after-misses', type=int, default=300)
    ap.add_argument('--delay', type=float, default=0.5)
    a = ap.parse_args()
    path = os.path.join(ROOT, 'data', 'tda.json')
    out, misses = {}, 0
    for i in range(a.start, a.max_id + 1):
        try:
            p = page(i)
        except Exception as e:
            print('  skip', i, repr(e)[:100], file=sys.stderr)
            p = None
        if p:
            out[str(i)] = p
            misses = 0
        else:
            misses += 1
            if misses >= a.stop_after_misses and i > 1700:
                break
        if i % 200 == 0:
            print(f'  tda id {i}: {len(out)} clinics', file=sys.stderr)
        time.sleep(a.delay)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f'tda: {len(out)} clinics', file=sys.stderr)


if __name__ == '__main__':
    main()

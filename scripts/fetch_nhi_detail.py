#!/usr/bin/env python3
"""Per-clinic details from the public NHI clinic search (info.nhi.gov.tw, 院所查詢 detail page):
NHI programmes the clinic takes part in, registration fee, accessibility, preventive services,
plus the phone and address NHI shows today (used to cross-check the open-data files).

Writes data/nhi_detail.json keyed by 醫事機構代碼. Sequential, with a delay, and incremental:
each run refreshes at most --limit clinics, oldest first.
"""
import argparse, concurrent.futures as cf, datetime as dt, json, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
URL = 'https://info.nhi.gov.tw/api/inae1000/inae1000s00/SQL300_1'
UA = 'Mozilla/5.0 (compatible; dental-roster-builder/1.0)'


def detail(code, tries=3):
    body = json.dumps({'C_HospID': code, 'C_planType': ''}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(URL, data=body, method='POST', headers={'User-Agent': UA, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                rows = json.load(r)
            return rows[0] if rows else None
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def save(path, db):
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    os.replace(path + '.tmp', path)


def clean(v):
    v = (v or '').strip()
    return '' if v in ('-', '無', '（無提供無障礙服務）') else v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=600)
    ap.add_argument('--max-age-days', type=int, default=30)
    ap.add_argument('--delay', type=float, default=0.4)
    ap.add_argument('--workers', type=int, default=1)
    a = ap.parse_args()
    clinics = json.load(open(os.path.join(DATA, 'dental.json'), encoding='utf-8'))['clinics']
    path = os.path.join(DATA, 'nhi_detail.json')
    db = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=a.max_age_days)).isoformat()
    # only NHI-contracted clinics have a detail page
    todo = [c['code'] for c in clinics if c['status'] == '健保特約' and db.get(c['code'], {}).get('fetched', '') < cutoff]
    todo.sort(key=lambda k: db.get(k, {}).get('fetched', ''))
    def get(code):
        time.sleep(a.delay)
        try:
            return code, detail(code)
        except Exception as e:
            print('  skip', code, repr(e)[:120], file=sys.stderr)
            return code, False

    done = 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for code, d in ex.map(get, todo[:a.limit]):
            if d is False:
                continue
            rec = {'fetched': today}
            if d:
                fee = re.sub(r'^門診掛號費\s*', '', clean((d.get('fee') or {}).get('regisT_FEE_NAME'))).rstrip('。')
                rec.update({
                    'name': clean(d.get('hosP_NAME')), 'phone': clean(d.get('hosptel')), 'address': clean(d.get('hosP_ADDR')),
                    'programs': [p for p in clean(d.get('plans')).split('、') if p],
                    'regFee': '' if '未提供' in fee else fee,
                    'accessibility': clean(d.get('nonE_OBS')),
                    'preventive': [p for p in clean(d.get('prE_SRV_LIST')).split('、') if p],
                    'services': [p for p in clean(d.get('srV_LIST')).split('、') if p],
                })
            db[code] = {k: v for k, v in rec.items() if v not in ('', [], None)}
            done += 1
            if done % 250 == 0:
                print(f'  detail {done}/{min(len(todo), a.limit)}', file=sys.stderr)
                save(path, db)
    save(path, db)
    print(f'nhi detail: fetched {done}, remaining {max(0, len(todo) - done)}, total {len(db)}', file=sys.stderr)


if __name__ == '__main__':
    main()

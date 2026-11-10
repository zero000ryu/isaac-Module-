#!/usr/bin/env python3
"""Collect the names of dentists practising at each dental clinic, from public NHI sources.

There is no open dataset listing every dentist per clinic. The NHI clinic search (info.nhi.gov.tw
INAE1020S01 「牙醫計畫及外傷急症院所查詢」) shows, for clinics in three NHI dental programmes, the
dentists qualified for that programme at that clinic:
  BD 牙周病統合治療方案   C6 牙醫身心障礙服務計畫   CT 牙齒外傷急症
The NHI 「醫療團醫師名單」 PDF adds dentists in the special-needs outreach teams.

Together these cover most, not all, dentists. Results go to data/dentists.json keyed by
醫事機構代碼; build_dental.py merges them into each clinic as `doctors`.

Polite by design: sequential requests with a delay, and --limit / --max-age-days so a run only
refreshes the oldest part of the data.
"""
import argparse, concurrent.futures as cf, datetime as dt, io, json, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
API = 'https://info.nhi.gov.tw/api/inae1000'
UA = 'Mozilla/5.0 (compatible; dental-roster-builder/1.0)'
PLANS = {'BD': '牙周病統合治療', 'C6': '身心障礙服務', 'CT': '牙齒外傷急症'}
# NHI 醫療團醫師名單 (special-needs outreach teams). Update the URL when NHI publishes a new list.
TEAM_PDFS = ['https://www.nhi.gov.tw/ch/dl-73019-15b07dc1c0f8403a91f97fded7bc57aa-1.pdf']


def call(path, body=None, tries=3):
    for i in range(tries):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(API + path, data=data, method='POST' if data else 'GET',
                                         headers={'User-Agent': UA, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                raise
            print('  retry', path, repr(e)[:120], file=sys.stderr)
            time.sleep(5 * (i + 1))


def save(path, db):
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    os.replace(path + '.tmp', path)


def team_pdf(url):
    """Rows of (name, clinic code) from an NHI 醫療團醫師名單 PDF. Needs pypdf; skipped without it."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print('pypdf not installed; skipping team list', file=sys.stderr)
        return []
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        pdf = PdfReader(io.BytesIO(r.read()))
    rows = []
    for p in pdf.pages:
        for line in (p.extract_text() or '').splitlines():
            m = re.search(r'([一-鿿]{2,4})\s+\S*?(?:診所|醫院|中心)\S*\s+(\d{10})\s*$', line)
            if m:
                rows.append((m.group(1), m.group(2)))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=1500, help='max clinic×programme lookups per run')
    ap.add_argument('--max-age-days', type=int, default=30)
    ap.add_argument('--delay', type=float, default=0.4)
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--areas', default='', help='comma list of NHI area codes, for testing')
    a = ap.parse_args()

    clinics = json.load(open(os.path.join(DATA, 'dental.json'), encoding='utf-8'))['clinics']
    ours = {c['code'] for c in clinics}
    path = os.path.join(DATA, 'dentists.json')
    db = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=a.max_age_days)).isoformat()

    # 1. which clinics take part in each programme (one request per programme and county)
    areas = [x['value'] for x in call('/inae1020s01/SQL001')['getAreaCoditem']]
    if a.areas:
        areas = [x for x in areas if x in a.areas.split(',')]
    members = {p: set() for p in PLANS}
    for p in PLANS:
        for area in areas:
            for h in call('/inae1020s01/getlist', {'C_HospType': '', 'C_PlanVal': p, 'C_AreaCod': area, 'C_AreaName': '', 'C_HospName': ''}) or []:
                if h.get('hosP_ID') in ours:
                    members[p].add(h['hosP_ID'])
            time.sleep(a.delay)
        print(f'{p} {PLANS[p]}: {len(members[p])} dental clinics', file=sys.stderr)

    # a clinic that left a programme loses those names
    for code, rec in db.items():
        for p in PLANS:
            if p in rec.get('plans', {}) and code not in members[p] and not a.areas:
                del rec['plans'][p]

    # 2. dentist names per clinic and programme, oldest first
    todo = [(code, p) for p in PLANS for code in members[p]
            if db.get(code, {}).get('fetched', {}).get(p, '') < cutoff]
    todo.sort(key=lambda t: db.get(t[0], {}).get('fetched', {}).get(t[1], ''))
    def lookup(item):
        code, p = item
        time.sleep(a.delay)
        try:
            r = call('/inae1000s00/SQL300_2', {'C_HospID': code, 'C_planType': p})
        except Exception as e:
            print('  skip', code, p, repr(e)[:120], file=sys.stderr)
            return item, None
        return item, [d['prsn_name'].strip() for d in (r.get('hosp_dr') or []) if d.get('prsn_name')]

    done = 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for (code, p), names in ex.map(lookup, todo[:a.limit]):
            if names is None:
                continue
            rec = db.setdefault(code, {'plans': {}, 'fetched': {}})
            rec['plans'][p] = names
            rec['fetched'][p] = today
            done += 1
            if done % 200 == 0:
                print(f'  {done}/{min(len(todo), a.limit)}', file=sys.stderr)
                save(path, db)

    # 3. outreach team lists
    for url in TEAM_PDFS:
        try:
            rows = team_pdf(url)
        except Exception as e:
            print('team list unavailable:', repr(e)[:160], file=sys.stderr)
            continue
        team = {}
        for name, code in rows:
            if code in ours:
                team.setdefault(code, []).append(name)
        for code, rec in db.items():
            rec.get('plans', {}).pop('team', None)
        for code, names in team.items():
            db.setdefault(code, {'plans': {}, 'fetched': {}})['plans']['team'] = sorted(set(names))
        print(f'team list: {len(rows)} dentists at {len(team)} clinics', file=sys.stderr)

    db = {k: v for k, v in db.items() if any(v.get('plans', {}).values())}
    save(path, db)
    names = sum(len({n for l in v['plans'].values() for n in l}) for v in db.values())
    print(f'dentists: looked up {done}, remaining {max(0, len(todo) - done)}; {names} dentists at {len(db)} clinics', file=sys.stderr)


if __name__ == '__main__':
    main()

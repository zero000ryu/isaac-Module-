#!/usr/bin/env python3
"""Send dental clinics from data/dental.json into the 牙材 ERP (erp.top-geng.com) 客戶／診所名單.

Uses the ERP's own batch import (POST /api/records {type:'customers', action:'import'}), which
skips any customer whose name already exists. So the first run adds every clinic, later runs add
only newly opened ones, and nothing a person edited in the ERP is ever overwritten.

Needs env ERP_EMAIL and ERP_PASSWORD (an ERP account with the customers.write permission).
Without them it does nothing, so the scheduled job still succeeds.

  ERP_URL      default https://erp.top-geng.com
  ERP_SCOPE    open (default: every clinic still operating) | nhi | 臺北市,新北市,...
  --dry-run    print what would be sent
"""
import argparse, http.cookiejar, json, os, sys, urllib.error, urllib.request
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATCH = 500


class ERP:
    def __init__(self, base):
        self.base = base.rstrip('/')
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ('POST' if data else 'GET'),
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'dental-roster-sync/1.0'})
        try:
            with self.http.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get('error')
            except Exception:
                msg = ''
            raise SystemExit(f'ERP {path} -> HTTP {e.code} {msg}')


def note_for(c):
    parts = [f"醫事機構代碼 {c['code']}", c.get('category') or c['status']]
    if c.get('hospitalLevel'):
        parts.append(c['hospitalLevel'])
    if c.get('zip'):
        parts.append('郵遞區號 ' + c['zip'] + (('，' + c['village']) if c.get('village') else ''))
    if c.get('owner'):
        parts.append('院長 ' + c['owner'])
    if c.get('dentists'):
        parts.append(f"牙醫師 {c['dentists']} 位")
    if c.get('doctors'):
        parts.append('看診牙醫 ' + '、'.join(c['doctors']))
    if c.get('otherPhones'):
        parts.append('其他電話 ' + '、'.join(c['otherPhones']))
    if c.get('regFee'):
        parts.append('掛號費 ' + c['regFee'])
    if c.get('checks'):
        parts.append('需查證：' + '、'.join(c['checks']))
    if c.get('line'):
        parts.append(f"LINE {c['line']}")
    if c.get('googleRating') is not None:
        parts.append(f"Google {c['googleRating']}★（{c.get('googleReviews', 0)} 則）")
    parts.append('來源：全台牙醫診所資料庫')
    return '；'.join(parts)


def rows_for(clinics, scope):
    if scope == 'nhi':
        pick = [c for c in clinics if c['status'] == '健保特約']
    elif scope in ('', 'open', 'all'):
        pick = [c for c in clinics if c['status'] != '已終止']
    else:
        cities = {s.strip().replace('台', '臺') for s in scope.split(',') if s.strip()}
        pick = [c for c in clinics if c['status'] != '已終止' and c.get('city') in cities]
    # the ERP matches customers by name, so clinics sharing a name get their district appended
    dup = Counter(c['name'] for c in pick)
    rows = []
    for c in pick:
        name = c['name'] if dup[c['name']] == 1 else f"{c['name']}（{c.get('city', '')}{c.get('district', '')}）"
        r = {'name': name, 'kind': 'clinic', 'phone': c.get('phone', ''), 'address': c.get('address', ''),
             'tax_id': c.get('taxId', ''), 'contact': c.get('owner') or (c.get('doctors') or [''])[0], 'email': c.get('email', ''),
             'website': c.get('website', ''), 'note': note_for(c)}
        rows.append({k: v for k, v in r.items() if v not in ('', None)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    clinics = json.load(open(os.path.join(ROOT, 'data', 'dental.json'), encoding='utf-8'))['clinics']
    rows = rows_for(clinics, os.environ.get('ERP_SCOPE', 'open').strip())
    if a.dry_run:
        print(json.dumps(rows[:3], ensure_ascii=False, indent=1))
        print(f'{len(rows)} clinics would be sent', file=sys.stderr)
        return
    email, pw = os.environ.get('ERP_EMAIL', '').strip(), os.environ.get('ERP_PASSWORD', '')
    if not email or not pw:
        print('ERP_EMAIL / ERP_PASSWORD not set; skipping ERP sync', file=sys.stderr)
        return
    erp = ERP(os.environ.get('ERP_URL', 'https://erp.top-geng.com'))
    me = erp.call('/api/auth', {'action': 'login', 'email': email, 'password': pw})
    perms = me.get('perms') or []
    if '*' not in perms and 'customers.write' not in perms:
        raise SystemExit('這個 ERP 帳號沒有「客戶名單」寫入權限（customers.write）')
    inserted, skipped, errors = 0, 0, []
    for i in range(0, len(rows), BATCH):
        r = erp.call('/api/records', {'type': 'customers', 'action': 'import', 'rows': rows[i:i + BATCH]})
        inserted += r.get('inserted', 0)
        skipped += len(r.get('skipped', []))
        errors += r.get('errors', [])
    erp.call('/api/auth', {'action': 'logout'})
    print(f'ERP: sent {len(rows)}, added {inserted}, already there {skipped}, errors {len(errors)}', file=sys.stderr)
    for e in errors[:20]:
        print('  ', e, file=sys.stderr)


if __name__ == '__main__':
    main()

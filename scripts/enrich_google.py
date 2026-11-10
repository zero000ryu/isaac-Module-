#!/usr/bin/env python3
"""Add Google Maps rating / review count / website to data/google.json via the official Places API (New).

Needs env GOOGLE_MAPS_API_KEY (a Google Cloud key with "Places API (New)" enabled; billing applies,
see README). Without the key this script does nothing, so the scheduled job still succeeds.

Each run refreshes at most --limit clinics, oldest first, so cost stays predictable and every clinic
is re-checked about every (open clinics / limit) runs.
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
URL = 'https://places.googleapis.com/v1/places:searchText'
FIELDS = ','.join('places.' + f for f in (
    'id', 'displayName', 'formattedAddress', 'nationalPhoneNumber', 'rating', 'userRatingCount',
    'websiteUri', 'googleMapsUri', 'businessStatus'))


def digits(s):
    d = re.sub(r'\D', '', s or '')
    return d[-8:]  # local number without area code differences


def norm(s):
    return re.sub(r'[\s()（）]', '', s or '').replace('台', '臺')


def search(key, query):
    body = json.dumps({'textQuery': query, 'languageCode': 'zh-TW', 'regionCode': 'TW', 'pageSize': 3}).encode()
    req = urllib.request.Request(URL, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'X-Goog-Api-Key': key, 'X-Goog-FieldMask': FIELDS})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get('places', [])


def pick(clinic, places):
    """Accept a result only if the phone matches, or the name matches and it sits in the same district."""
    pd = digits(clinic.get('phone'))
    for p in places:
        if pd and len(pd) >= 7 and digits(p.get('nationalPhoneNumber')) == pd:
            return p, 'phone'
    n = norm(clinic['name'])
    for p in places:
        pn = norm((p.get('displayName') or {}).get('text'))
        addr = norm(p.get('formattedAddress'))
        if pn and (pn == n or n in pn or pn in n) and clinic.get('district', '') in addr:
            return p, 'name'
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=int(os.environ.get('GOOGLE_LIMIT', 300)))
    ap.add_argument('--max-age-days', type=int, default=30)
    a = ap.parse_args()
    key = os.environ.get('GOOGLE_MAPS_API_KEY', '').strip()
    if not key:
        print('GOOGLE_MAPS_API_KEY not set; skipping Google enrichment', file=sys.stderr)
        return
    clinics = json.load(open(os.path.join(DATA, 'dental.json'), encoding='utf-8'))['clinics']
    gpath = os.path.join(DATA, 'google.json')
    g = json.load(open(gpath, encoding='utf-8')) if os.path.exists(gpath) else {}
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=a.max_age_days)).isoformat()
    todo = [c for c in clinics if c['status'] != '已終止' and g.get(c['code'], {}).get('fetched', '') < cutoff]
    todo.sort(key=lambda c: g.get(c['code'], {}).get('fetched', ''))
    done = found = 0
    for c in todo[:a.limit]:
        try:
            places = search(key, f"{c['name']} {c.get('address', '')}")
        except urllib.error.HTTPError as e:
            print('Places API error', e.code, e.read()[:300], file=sys.stderr)
            if e.code in (400, 401, 403):
                break  # bad key / API not enabled / quota: stop instead of burning requests
            continue
        p, how = pick(c, places)
        rec = {'fetched': today.isoformat()}
        if p:
            found += 1
            rec.update({'googlePlaceId': p.get('id'), 'googleRating': p.get('rating'), 'googleReviews': p.get('userRatingCount'),
                        'website': p.get('websiteUri'), 'googleUrl': p.get('googleMapsUri'),
                        'businessStatus': p.get('businessStatus'), 'matchedBy': how})
        g[c['code']] = {k: v for k, v in rec.items() if v not in (None, '')}
        done += 1
        time.sleep(0.1)
    with open(gpath, 'w', encoding='utf-8') as f:
        json.dump(g, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f'google: checked {done}, matched {found}, remaining stale {max(0, len(todo) - done)}', file=sys.stderr)


if __name__ == '__main__':
    main()

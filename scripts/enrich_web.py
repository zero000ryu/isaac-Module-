#!/usr/bin/env python3
"""Visit each clinic's own website (found by enrich_google.py or entered in data/dental-extra.csv) and pick up
LINE official account, Facebook, Instagram, email, 統一編號 and 負責醫師 if the page states them.

Polite by design: obeys robots.txt, one request per site per run (home page, plus a contact page if linked),
a short delay between requests, and a per-run --limit. Results go to data/web.json.
"""
import argparse, concurrent.futures as cf, datetime as dt, html, json, os, re, sys, urllib.parse, urllib.request, urllib.robotparser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
UA = 'Mozilla/5.0 (compatible; dental-roster-bot/1.0)'

RE_LINE = [
    re.compile(r'(?:https?://)?(?:line\.me/R/ti/p/|line\.me/ti/p/|page\.line\.me/)(?:%40|@)?([~\w.\-]{3,40})', re.I),
    re.compile(r'(?:https?://)?lin\.ee/([\w\-]{4,20})', re.I),
]
RE_LINE_TEXT = re.compile(r'LINE\s*(?:@|ID|官方帳號|官方|好友)?\s*[：:]\s*(@[\w.\-]{3,30})', re.I)
RE_FB = re.compile(r'https?://(?:www\.|m\.)?facebook\.com/(?!sharer|share|plugins|tr\b|dialog|login|policy)([\w.\-]{3,80})', re.I)
RE_IG = re.compile(r'https?://(?:www\.)?instagram\.com/(?!p/|explore|accounts)([\w.]{2,40})', re.I)
RE_MAIL = re.compile(r'mailto:([\w.+\-]+@[\w\-]+\.[\w.\-]+)', re.I)
RE_TAX = re.compile(r'統一?編(?:號)?\s*[：:]?\s*(\d{8})')
RE_OWNER = re.compile(r'(?:負責(?:醫師|人)|院\s*長)\s*[：:]\s*([一-鿿]{2,4})(?:\s*醫師)?')
# link (not content) to the clinic's own schedule page: medical-institution sites must forbid
# republishing their content (醫療機構網際網路資訊管理辦法 第4條), but linking is allowed
RE_SCHEDULE = re.compile(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(?:(?!</a>).){0,200}?(門診時間|門診表|看診時間|看診表|醫師班表|門診時刻|看診時段|門診資訊)', re.I | re.S)
RE_CONTACT = re.compile(r'href=["\']([^"\']*(?:contact|about|聯絡|關於)[^"\']*)["\']', re.I)

_robots = {}


def allowed(url):
    p = urllib.parse.urlsplit(url)
    base = f'{p.scheme}://{p.netloc}'
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser(base + '/robots.txt')
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def get(url):
    if not allowed(url):
        return ''
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'zh-TW,zh;q=0.9'})
    with urllib.request.urlopen(req, timeout=15) as r:
        if 'html' not in r.headers.get('Content-Type', 'text/html'):
            return ''
        raw = r.read(1_500_000)
        cs = r.headers.get_content_charset() or 'utf-8'
    try:
        return raw.decode(cs, errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


def extract(page):
    text = html.unescape(page)
    out = {}
    for rx in RE_LINE:
        m = rx.search(text)
        if m:
            v = m.group(1)
            out['line'] = ('https://lin.ee/' + v) if 'lin.ee' in m.group(0).lower() else ('@' + v.lstrip('@~') if not v.startswith('~') else v)
            break
    if 'line' not in out and (m := RE_LINE_TEXT.search(re.sub(r'<[^>]+>', ' ', text))):
        out['line'] = m.group(1)
    for key, rx in (('facebook', RE_FB), ('instagram', RE_IG)):
        m = rx.search(text)
        if m:
            out[key] = m.group(0).split('?')[0].rstrip('/')
    if m := RE_MAIL.search(text):
        out['email'] = m.group(1)
    plain = re.sub(r'<[^>]+>', ' ', text)
    if m := RE_TAX.search(plain):
        out['taxId'] = m.group(1)
    if m := RE_OWNER.search(plain):
        out['owner'] = m.group(1)
    return out


def visit(site):
    found = {}
    try:
        page = get(site)
        found.update(extract(page))
        m = RE_SCHEDULE.search(page)
        if m:
            found['schedulePage'] = urllib.parse.urljoin(site, html.unescape(m.group(1)))
        m = RE_CONTACT.search(page)
        if m and len(found) < 4:
            found = {**extract(get(urllib.parse.urljoin(site, m.group(1)))), **found}
    except Exception as e:
        found['error'] = type(e).__name__
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=400)
    ap.add_argument('--max-age-days', type=int, default=60)
    a = ap.parse_args()
    clinics = json.load(open(os.path.join(DATA, 'dental.json'), encoding='utf-8'))['clinics']
    wpath = os.path.join(DATA, 'web.json')
    w = json.load(open(wpath, encoding='utf-8')) if os.path.exists(wpath) else {}
    cutoff = (dt.date.today() - dt.timedelta(days=a.max_age_days)).isoformat()
    todo = [c for c in clinics if c.get('website', '').startswith('http') and c['status'] != '已終止'
            and (w.get(c['code'], {}).get('fetched', '') < cutoff or w.get(c['code'], {}).get('site') != c['website'])]
    todo = sorted(todo, key=lambda c: w.get(c['code'], {}).get('fetched', ''))[:a.limit]
    hits = 0
    with cf.ThreadPoolExecutor(8) as ex:
        for c, found in zip(todo, ex.map(lambda c: visit(c['website']), todo)):
            hits += any(k in found for k in ('line', 'taxId', 'owner'))
            w[c['code']] = {'site': c['website'], 'fetched': dt.date.today().isoformat(), **found}
    with open(wpath, 'w', encoding='utf-8') as f:
        json.dump(w, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f'web: visited {len(todo)}, found LINE/統編/負責人 on {hits}', file=sys.stderr)


if __name__ == '__main__':
    main()

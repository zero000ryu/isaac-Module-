#!/usr/bin/env python3
"""Build data/dental.json: every dental clinic in Taiwan from official open data.

Sources (all government open data, no key needed):
  1. 健保署「健保特約醫事機構-診所」(data.gov.tw/dataset/39283, daily)       -> NHI contracted clinics
  2. 衛福部「醫療機構與人員基本資料」(data.gov.tw/dataset/15393)              -> all registered clinics incl. self-pay,
                                                                                  dentist head-count, district
  3. 財政部「全國營業(稅籍)登記資料集」(data.gov.tw/dataset/9400)            -> 統一編號 (only the few clinics that
                                                                                  registered for tax; optional, --tax)
Then merges, keyed by 醫事機構代碼:
  data/google.json     (scripts/enrich_google.py: Google rating / website, needs an API key)
  data/web.json        (scripts/enrich_web.py: LINE / Facebook / email / 統編 / 負責人 found on the clinic's own site)
  data/dental-extra.csv (hand-maintained corrections; always wins)

Standard library only, so it runs anywhere (GitHub Actions, a laptop).
"""
import argparse, csv, datetime as dt, io, json, os, re, sys, time, urllib.request, zipfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
NHI_URL = 'https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-D21004-009'
MOHW_META = 'https://data.gov.tw/api/v2/rest/dataset/15393'
TAX_URL = 'https://eip.fia.gov.tw/data/BGMOPEN1.csv'

CITIES = ['臺北市','新北市','基隆市','桃園市','新竹市','新竹縣','苗栗縣','臺中市','彰化縣','南投縣','雲林縣','嘉義市','嘉義縣',
          '臺南市','高雄市','屏東縣','宜蘭縣','花蓮縣','臺東縣','澎湖縣','金門縣','連江縣']
FW = str.maketrans('０１２３４５６７８９－（）', '0123456789-()')
EXTRA_COLS = ['code','taxId','owner','website','line','facebook','email','googleRating','googleReviews','googlePlaceId','note']


def fetch(url, path, cache, tries=4):
    if cache and os.path.exists(path):
        return path
    last = None
    for i in range(tries):
        print('download', url, f'(try {i + 1})', file=sys.stderr)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; dental-roster-builder/1.0)'})
        try:
            with urllib.request.urlopen(req, timeout=600) as r, open(path + '.part', 'wb') as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(path + '.part', path)
            return path
        except Exception as e:  # government hosts sometimes drop connections from overseas data centres
            last = e
            print('  failed:', repr(e)[:200], file=sys.stderr)
            time.sleep(10 * (i + 1))
    raise SourceUnavailable(f'{url}: {last!r}')


class SourceUnavailable(Exception):
    pass


def norm_text(s):
    return (s or '').translate(FW).replace('　', '').strip()


VARIANTS = str.maketrans({'台': '臺', '峯': '峰', '群': '群', '館': '館', '綫': '線', '鉅': '巨', '濬': '浚', '衆': '眾', '啓': '啟', '眞': '真'})


def norm_name(s):
    return re.sub(r'[\s()（）.。,，、·・]', '', norm_text(s)).translate(VARIANTS)


def split_area(addr):
    a = norm_text(addr).replace('台', '臺')
    city = next((c for c in CITIES if a.startswith(c)), '')
    rest = a[len(city):]
    m = re.match(r'(.{1,4}?[區鄉鎮市])', rest)
    return city, (m.group(1) if m else '')


def phone(s):
    s = norm_text(s)
    return '' if s in ('', '-') else s


def hours_bits(text):
    """'星期一上午看診、…' -> 21 chars, Mon..Sun x (上午,下午,晚上), '1' = open."""
    if not text:
        return ''
    days = '一二三四五六日'
    out = []
    for d in days:
        for p in ('上午', '下午', '晚上'):
            out.append('1' if f'星期{d}{p}看診' in text else '0')
    return ''.join(out)


def read_ods(path):
    """Minimal .ods reader (first sheet) so we don't need odfpy."""
    ns = {'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0', 'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'}
    T = '{%s}' % ns['table']
    rows = []
    with zipfile.ZipFile(path) as z:
        for _, el in ET.iterparse(z.open('content.xml'), events=('end',)):
            if el.tag == T + 'table-row':
                r = []
                for c in el:
                    if c.tag not in (T + 'table-cell', T + 'covered-table-cell'):
                        continue
                    n = int(c.get(T + 'number-columns-repeated', '1'))
                    v = '\n'.join(''.join(p.itertext()) for p in c.findall('text:p', ns))
                    r.extend([v] * min(n, 64))
                while r and r[-1] == '':
                    r.pop()
                if r:
                    rows.append(r)
                el.clear()
    return rows


def is_dental_name(name):
    return bool(re.search(r'牙|齒|口腔', name or ''))


def load_nhi(path):
    out = {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            kind = r.get('醫事機構種類', '')
            specs = r.get('診療科別', '')
            if '牙' not in kind and not ('牙' in specs and is_dental_name(r.get('醫事機構名稱'))):
                continue
            out[r['醫事機構代碼'].strip()] = r
    return out


def load_mohw(path):
    rows = read_ods(path)
    head = rows[0]
    idx = {h: i for i, h in enumerate(head)}
    out = {}
    for r in rows[1:]:
        g = lambda k: r[idx[k]].strip() if k in idx and idx[k] < len(r) else ''
        name, dept = g('機構名稱'), g('科別')
        depts = [d for d in re.split(r'[,，、]', dept) if d.strip()]
        all_dental = bool(depts) and all(re.search(r'牙|齒|口腔', d) for d in depts)
        # a 衛生所 or 療養院 with a dental department is not a dental clinic
        if not (is_dental_name(name) or all_dental) or re.search(r'醫院|衛生所|療養院', name):
            continue
        def num(k):
            try:
                return int(float(g(k) or 0))
            except ValueError:
                return 0
        out[g('機構代碼')] = {'name': name, 'tel': g('電話'), 'area': g('縣市區名'), 'addr': g('地址'),
                              'dept': dept, 'dentists': num('C牙醫師'), 'physicians': num('A醫師'),
                              'nurses': num('F護理師') + num('G護士'), 'techs': num('2牙體技術師') + num('4牙體技術士')}
    return out


def mohw_download_url(cache_dir, cache):
    meta_path = os.path.join(cache_dir, 'mohw_meta.json')
    fetch(MOHW_META, meta_path, cache)
    meta = json.load(open(meta_path, encoding='utf-8'))
    for d in meta.get('result', {}).get('distribution', []):
        if d.get('resourceDownloadUrl'):
            return d['resourceDownloadUrl'], meta.get('result', {}).get('modifiedDate') or ''
    raise RuntimeError('MOHW dataset has no download URL')


def load_tax(path, names):
    """Return {normalized clinic name: [(taxId, address)]} for names we care about. Streams the 300 MB file."""
    hits = {}
    with open(path, encoding='utf-8', errors='replace', newline='') as f:
        r = csv.reader(f)
        for row in r:
            if len(row) < 4 or not row[1].isdigit():
                continue
            n = norm_name(row[3])
            if n in names:
                hits.setdefault(n, []).append((row[1], norm_text(row[0]).replace('台', '臺')))
    return hits


def load_json(name):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_extra():
    p = os.path.join(DATA, 'dental-extra.csv')
    out = {}
    if os.path.exists(p):
        with open(p, encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f):
                code = (r.get('code') or '').strip()
                if code:
                    out[code] = {k: (v or '').strip() for k, v in r.items() if k in EXTRA_COLS and (v or '').strip()}
    else:
        os.makedirs(DATA, exist_ok=True)
        with open(p, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow(EXTRA_COLS)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', action='store_true', help='reuse files already downloaded in --cache-dir')
    ap.add_argument('--cache-dir', default=os.path.join(ROOT, '.cache'))
    ap.add_argument('--tax', action='store_true', help='also scan the 300 MB tax registry for 統一編號')
    a = ap.parse_args()
    os.makedirs(a.cache_dir, exist_ok=True)
    today = dt.date.today()
    tstr = today.strftime('%Y%m%d')

    try:
        nhi = load_nhi(fetch(NHI_URL, os.path.join(a.cache_dir, 'nhi_clinics.csv'), a.cache))
        mohw_url, mohw_date = mohw_download_url(a.cache_dir, a.cache)
        mohw = load_mohw(fetch(mohw_url, os.path.join(a.cache_dir, 'mohw_staff.ods'), a.cache))
    except SourceUnavailable as e:
        if os.path.exists(os.path.join(DATA, 'dental.json')):
            print(f'::warning::source unavailable, keeping previous data: {e}', file=sys.stderr)
            return
        raise
    print(f'NHI dental: {len(nhi)}  MOHW dental: {len(mohw)}', file=sys.stderr)

    google, web, extra, dentists = load_json('google.json'), load_json('web.json'), load_extra(), load_json('dentists.json')
    detail = load_json('nhi_detail.json')
    prev = {c['code']: c for c in load_json('dental.json').get('clinics', [])} if os.path.exists(os.path.join(DATA, 'dental.json')) else {}

    clinics = []
    for code in sorted(set(nhi) | set(mohw)):
        n, m = nhi.get(code), mohw.get(code)
        end = (n or {}).get('終止合約或歇業日期', '').strip()
        nhi_active = bool(n) and (not end or end >= tstr)
        if nhi_active:
            status = '健保特約'
        elif m:
            status = '自費'          # registered with MOHW but no current NHI contract
        else:
            status = '已終止'        # NHI contract ended and no longer in the MOHW register
        name = norm_text((n or {}).get('醫事機構名稱') or m['name'])
        addr = norm_text((n or {}).get('地址') or m['addr']).replace('台', '臺')
        city, district = split_area(addr)
        if m and m['area']:
            mc = next((c for c in CITIES if m['area'].replace('台', '臺').startswith(c)), '')
            if mc:
                city, district = mc, m['area'].replace('台', '臺')[len(mc):] or district
        c = {
            'code': code, 'name': name, 'status': status, 'city': city, 'district': district, 'address': addr,
            'phone': phone((n or {}).get('電話')) or phone((m or {}).get('tel')),
            'kind': (n or {}).get('醫事機構種類', '') or '牙醫診所',
            'specs': [s for s in re.split(r'[,，]', (n or {}).get('診療科別') or (m or {}).get('dept', '')) if s.strip()],
            'hours': hours_bits((n or {}).get('固定看診時段')),
            'hoursNote': '' if (n or {}).get('備註', '-') in ('-', '') else norm_text(n['備註']),
            'nhiSince': (n or {}).get('合約起日', ''), 'nhiUntil': end,
            'dentists': (m or {}).get('dentists'), 'staff': {k: m[k] for k in ('nurses', 'techs')} if m else None,
            'sources': [s for s, ok in (('健保署', n), ('衛福部', m)) if ok],
        }
        # carry forward fields that only later steps fill, so a run without --tax keeps last known 統編
        if code in prev and prev[code].get('taxId'):
            c['taxId'] = prev[code]['taxId']
        g = google.get(code)
        if g:
            c.update({k: g[k] for k in ('googleRating', 'googleReviews', 'googlePlaceId', 'googleUrl') if g.get(k) not in (None, '')})
            if g.get('website'):
                c['website'] = g['website']
            c['googleFetched'] = g.get('fetched', '')
        dr = dentists.get(code)
        if dr:
            # names from every NHI programme list, in first-seen order
            names = list(dict.fromkeys(n for p in ('BD', 'C6', 'CT', 'team') for n in dr.get('plans', {}).get(p, [])))
            if names:
                c['doctors'] = names
                c['doctorPrograms'] = {p: v for p, v in dr['plans'].items() if v}
        w = web.get(code)
        if w:
            for k in ('line', 'facebook', 'instagram', 'email', 'owner', 'taxId'):
                if w.get(k) and not c.get(k):
                    c[k] = w[k]
        for k, v in extra.get(code, {}).items():
            if k == 'code':
                continue
            c[k] = float(v) if k == 'googleRating' else int(v) if k == 'googleReviews' else v
        clinics.append(c)

    if a.tax:
        want = {norm_name(c['name']): c for c in clinics}
        try:
            hits = load_tax(fetch(TAX_URL, os.path.join(a.cache_dir, 'tax.csv'), a.cache), set(want))
        except SourceUnavailable as e:
            print(f'::warning::tax registry unavailable, keeping previous 統編: {e}', file=sys.stderr)
            hits = {}
        matched = 0
        for nn, lst in hits.items():
            c = want[nn]
            # prefer the registration whose address shares the clinic's street
            best = max(lst, key=lambda t: len(os.path.commonprefix([t[1], c['address']])))
            if len(lst) == 1 or len(os.path.commonprefix([best[1], c['address']])) >= 6:
                if extra.get(c['code'], {}).get('taxId'):
                    continue
                c['taxId'] = best[0]
                matched += 1
        print(f'tax registry matches: {matched}', file=sys.stderr)

    quality = reconcile(clinics, nhi, mohw, detail)

    # drop empty values to keep the file small
    for c in clinics:
        for k in [k for k, v in c.items() if v in ('', None, [], {})]:
            del c[k]

    by_city = {}
    for c in clinics:
        s = by_city.setdefault(c.get('city') or '未知', {'健保特約': 0, '自費': 0, '已終止': 0})
        s[c['status']] += 1
    meta = {
        'generatedAt': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds'),
        'sources': {
            'nhi': {'name': '健保署 健保特約醫事機構-診所', 'url': 'https://data.gov.tw/dataset/39283', 'records': len(nhi)},
            'mohw': {'name': '衛福部 醫療機構與人員基本資料', 'url': 'https://data.gov.tw/dataset/15393', 'records': len(mohw), 'modified': mohw_date},
            'tax': {'name': '財政部 全國營業(稅籍)登記資料集', 'url': 'https://data.gov.tw/dataset/9400', 'used': a.tax},
        },
        'total': len(clinics), 'byCity': by_city, 'quality': quality['summary'],
    }
    meta['sources']['nhiDetail'] = {'name': '健保署 院所查詢（計畫、掛號費、無障礙、預防保健）', 'url': 'https://info.nhi.gov.tw/INAE1000/INAE1000S01', 'records': len(detail)}
    meta['sources']['nhiDoctors'] = {'name': '健保署 牙醫計畫院所查詢醫師名單與醫療團名單', 'url': 'https://info.nhi.gov.tw/INAE1000/INAE1020S01', 'records': len(dentists)}
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, 'dental.json'), 'w', encoding='utf-8') as f:
        json.dump({'meta': meta, 'clinics': clinics}, f, ensure_ascii=False, separators=(',', ':'))
    write_csv(clinics)
    with open(os.path.join(DATA, 'quality.json'), 'w', encoding='utf-8') as f:
        json.dump(quality, f, ensure_ascii=False, indent=1)
    q = quality['summary']
    print(f"reconcile: phone conflicts {q['phoneConflicts']}, name diffs {q['nameDiffs']}, address diffs {q['addressDiffs']}, "
          f"relocations {q['relocations']}, shared addresses {q['sharedAddress']}, avg completeness {q['avgCompleteness']}%", file=sys.stderr)
    print(json.dumps({k: meta[k] for k in ('generatedAt', 'total')}, ensure_ascii=False), file=sys.stderr)


def digits(p):
    d = re.sub(r'\D', '', p or '')
    return d[3:] if d.startswith('886') else d


def same_phone(a, b):
    a, b = digits(a), digits(b)
    if len(a) < 7 or len(b) < 7:
        return False
    # the MOHW register often drops the area code or a digit of old 7-digit numbers; last 7 digits decide
    return a == b or a.endswith(b) or b.endswith(a) or a[-7:] == b[-7:]


def is_mobile(p):
    return digits(p).startswith('09')


def addr_key(a):
    """Address up to the house number, normalised, so floor/room notes don't count as a difference."""
    a = norm_addr(a)
    # road + first house number; floors, rooms and extra numbers ("21、23號") don't count
    m = re.match(r'(.*?\D)(\d+(?:-\d+)?)(?=號|、|,)', a)
    return m.group(1) + m.group(2) if m else a


def norm_addr(a):
    a = norm_text(a).replace('台', '臺').replace('－', '-').replace('—', '-').replace(' ', '')
    a = re.sub(r'^\d{3,6}', '', a)  # postcode
    a = re.sub(r'[ＯO○]', '〇', a)
    a = re.sub(r'\s*[．，,.]\s*', '、', a).replace('–', '-').replace('─', '-').replace('~', '-')
    a = re.sub(r'[〇零一二三四五六七八九十百0-9]+(?=[段巷弄號樓路街之、])', lambda m: zh_to_num(m.group(0)), a)
    a = a.replace('之', '-')
    a = re.sub(r'(?<=[區鄉鎮市])[\u4e00-\u9fff]{1,3}村(?=[\u4e00-\u9fff\d])', '', a)
    a = re.sub(r'(?<=[區鄉鎮市街路道])[\u4e00-\u9fff]{1,3}?里(?=[\u4e00-\u9fff\d])', '', a)  # 里 is optional in addresses
    a = re.sub(r'\d+鄰', '', a)
    return a


def same_address(x, y):
    """Same road and at least one shared house number ('237、239號' matches '239號')."""
    kx, ky = addr_key(x), addr_key(y)
    if kx == ky:
        return True
    rx, ry = re.match(r'(.*?\D)(\d+(?:-\d+)?)$', kx), re.match(r'(.*?\D)(\d+(?:-\d+)?)$', ky)
    if not (rx and ry) or rx.group(1) != ry.group(1):
        return False
    nums = lambda a: set(re.findall(r'\d+(?:-\d+)?', re.split(r'號(?!、)', a.split(rx.group(1)[-2:], 1)[-1])[0]))
    return bool(nums(norm_addr(x)) & nums(norm_addr(y)))


def zh_to_num(t):
    """'十一'->'11', '九九二'->'992', '四0七'->'407', '二'->'2'; digits pass through."""
    digit = {'〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if t.isdigit():
        return t
    if '十' in t or '百' in t:
        total, cur = 0, 0
        for ch in t:
            if ch in digit:
                cur = digit[ch]
            elif ch.isdigit():
                cur = int(ch)
            elif ch == '十':
                total += (cur or 1) * 10; cur = 0
            elif ch == '百':
                total += (cur or 1) * 100; cur = 0
        return str(total + cur)
    return ''.join(str(digit[ch]) if ch in digit else ch for ch in t)


ZH_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


COMPLETENESS_FIELDS = ['phone', 'address', 'hours', 'dentists', 'doctors', 'programs', 'regFee',
                       'website', 'line', 'email', 'taxId', 'owner', 'googleRating']


def reconcile(clinics, nhi, mohw, detail):
    """Cross-check every source against the others and fold the results into one record per clinic."""
    issues = []
    by_code = {c['code']: c for c in clinics}

    for c in clinics:
        code, n, m, d = c['code'], nhi.get(c['code']), mohw.get(c['code']), detail.get(c['code']) or {}
        checks = []
        # NHI live detail page: programmes, fee, accessibility, preventive services
        for k in ('programs', 'regFee', 'accessibility', 'preventive'):
            if d.get(k):
                c[k] = d[k]
        # phones: NHI live page (most current) > NHI open data > MOHW register
        cands = [x for x in (d.get('phone'), c.get('phone'), phone((m or {}).get('tel'))) if x and len(digits(x)) >= 7]
        phones = []
        for x in cands:
            if not any(same_phone(x, y) for y in phones):
                phones.append(x)
        if phones:
            c['phone'] = phones[0]
            if len(phones) > 1:
                c['otherPhones'] = phones[1:]
                landlines = [x for x in phones if not is_mobile(x)]
                if len(landlines) > 1:  # a mobile next to a landline is an extra contact, not a conflict
                    checks.append('電話不一致')
                    issues.append({'code': code, 'name': c['name'], 'type': '電話不一致', 'values': phones})
        # names
        names = [x for x in (d.get('name'), norm_text((n or {}).get('醫事機構名稱')), norm_text((m or {}).get('name'))) if x]
        uniq = list(dict.fromkeys(norm_name(x) for x in names))
        if len(uniq) > 1:
            c['aliases'] = [x for x in dict.fromkeys(names) if norm_name(x) != norm_name(c['name'])]
            checks.append('名稱不一致')
            issues.append({'code': code, 'name': c['name'], 'type': '名稱不一致', 'values': list(dict.fromkeys(names))})
        # addresses (compared up to the house number)
        addrs = [x for x in (d.get('address'), (n or {}).get('地址'), (m or {}).get('addr')) if x]
        distinct = []
        for x in addrs:
            if not any(same_address(x, y) for y in distinct):
                distinct.append(x)
        if len(distinct) > 1:
            c['otherAddresses'] = [norm_text(x).replace('台', '臺') for x in distinct if not same_address(x, c['address'])][:2]
            checks.append('地址不一致')
            issues.append({'code': code, 'name': c['name'], 'type': '地址不一致', 'values': list(dict.fromkeys(norm_text(x) for x in addrs))})
        # dentist names vs MOHW head count
        if c.get('doctors') and c.get('dentists'):
            c['doctorCoverage'] = min(100, round(len(c['doctors']) / c['dentists'] * 100))
            if len(c['doctors']) > c['dentists']:
                checks.append('醫師名單多於登記人數')
        if checks:
            c['checks'] = checks

    # same building, different institution code: relocation / re-registration, or shared premises
    by_addr = {}
    for c in clinics:
        if c.get('address'):
            by_addr.setdefault(addr_key(c['address']), []).append(c)
    relocations = shared = 0
    for group in by_addr.values():
        if len(group) < 2:
            continue
        live = [g for g in group if g['status'] != '已終止']
        for g in group:
            if g['status'] == '已終止' and live:
                g['replacedBy'] = live[0]['code']
                relocations += 1
        if len(live) > 1:
            shared += len(live)
            for g in live:
                g['sameAddressAs'] = [x['code'] for x in live if x is not g]

    # dentists named at more than one clinic (part-time support, or two people with the same name)
    seen = {}
    for c in clinics:
        for name in c.get('doctors', []):
            seen.setdefault(name, []).append(c['code'])
    multi = {k: v for k, v in seen.items() if len(v) > 1}

    # completeness score
    open_ = [c for c in clinics if c['status'] != '已終止']
    for c in clinics:
        c['completeness'] = round(sum(1 for f in COMPLETENESS_FIELDS if c.get(f) not in (None, '', [], {})) / len(COMPLETENESS_FIELDS) * 100)
    coverage = {f: sum(1 for c in open_ if c.get(f) not in (None, '', [], {})) for f in COMPLETENESS_FIELDS}

    count = lambda t: sum(1 for i in issues if i['type'] == t)
    summary = {
        'openClinics': len(open_), 'fieldCoverage': coverage,
        'phoneConflicts': count('電話不一致'), 'nameDiffs': count('名稱不一致'), 'addressDiffs': count('地址不一致'),
        'relocations': relocations, 'sharedAddress': shared,
        'namedDentists': len(seen), 'dentistsAtSeveralClinics': len(multi),
        'clinicsWithDoctors': sum(1 for c in open_ if c.get('doctors')),
        'avgCompleteness': round(sum(c['completeness'] for c in open_) / max(1, len(open_))),
    }
    return {'summary': summary, 'issues': issues[:3000],
            'dentistsAtSeveralClinics': dict(sorted(multi.items(), key=lambda kv: -len(kv[1]))[:500])}


# Flat CSV for ERP / Excel import. Column names are stable; add new ones only at the end.
CSV_COLS = [('code', '醫事機構代碼'), ('name', '診所名稱'), ('status', '狀態'), ('city', '縣市'), ('district', '鄉鎮市區'),
            ('address', '地址'), ('phone', '電話'), ('taxId', '統一編號'), ('owner', '負責人'), ('website', '網站'),
            ('line', 'LINE'), ('facebook', 'Facebook'), ('email', 'Email'), ('googleRating', 'Google評分'),
            ('googleReviews', 'Google評論數'), ('dentists', '牙醫師人數'), ('doctors', '看診牙醫'), ('kind', '機構類別'), ('specs', '診療科別'),
            ('nhiSince', '健保合約起日'), ('nhiUntil', '健保合約迄日'), ('hours', '看診時段碼'), ('hoursNote', '看診備註'),
            ('otherPhones', '其他電話'), ('programs', '參與健保計畫'), ('regFee', '掛號費'), ('accessibility', '無障礙'),
            ('preventive', '預防保健服務'), ('completeness', '資料完整度'), ('checks', '比對注意事項')]
LIST_COLS = {'specs', 'doctors', 'otherPhones', 'programs', 'preventive', 'checks'}


def write_csv(clinics):
    with open(os.path.join(DATA, 'dental.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([zh for _, zh in CSV_COLS])
        for c in clinics:
            w.writerow(['、'.join(c.get(k, [])) if k in LIST_COLS else c.get(k, '') for k, _ in CSV_COLS])


if __name__ == '__main__':
    main()

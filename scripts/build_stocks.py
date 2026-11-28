#!/usr/bin/env python3
"""Build data/stocks.json: fundamentals and returns for US stocks and ETFs, for stocks.html.

Universe
  - S&P 500 and Nasdaq-100 members (Wikipedia constituent tables)
  - the ETFs in ETFS below
  - any extra tickers listed in data/stocks-extra.txt (one per line, # comments allowed)

Data (Yahoo Finance through the yfinance package: pip install yfinance)
  - Ticker.info: name, sector, valuation (P/E, P/B, P/S, PEG), dividends, growth, margins, ROE, debt, ETF expense ratio
  - Ticker.option_chain: at-the-money implied volatility of the expiry nearest 30 days out (iv, ivDte)
  - Ticker.news (Google News RSS when Yahoo has none): recent headlines per ticker, tagged by keyword rules
    as negative / positive and by risk category, written to data/news.json; counts go into stocks.json
  - yf.download (dividend-adjusted closes, 5 years): 1-month, YTD, 1-year, 3-year and 5-year returns,
    distance from the 52-week high, volatility and max drawdown, computed here the same way for stocks and ETFs

A ticker that fails this run keeps its previous record (marked with its old asOf date), so one bad run
never empties the page.

Usage: python scripts/build_stocks.py [--limit N] [--workers 4] [--only AAPL,MSFT]
"""
import argparse, concurrent.futures as cf, datetime as dt, email.utils, html, json, math, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
OUT = os.path.join(DATA, 'stocks.json')
EXTRA = os.path.join(DATA, 'stocks-extra.txt')
NEWS_OUT = os.path.join(DATA, 'news.json')
NEWS_DAYS, NEWS_KEEP = 30, 8          # headlines kept per ticker for the page
RISK_DAYS = 14                        # window that counts toward the risk score
# Headline keyword rules (English headlines). Negative categories feed the risk score; the page shows their names.
NEG = {k: re.compile(v, re.I) for k, v in {
    'legal': r'\b(lawsuits?|sued|sues|class action|litigation|probe[sd]?|investigat\w*|subpoena\w*|indict\w*|fraud|antitrust|doj|ftc|'
             r'sec (charges|probe|investigation)|penalt\w+|fined|fines? of|violation|regulator\w* (crack|scrutiny)|ban(s|ned)?)\b',
    'rating': r'\b(downgrade[sd]?|cuts? to (sell|underperform|neutral|hold)|(lowers?|cuts?|slash\w*) (its |the )?price target|bearish call)\b',
    'earnings': r'\b(miss(es|ed)? (estimates|expectations|forecasts?)|(lowers?|cuts?|slash\w*|withdraws?) (its |full-year |annual )?(guidance|outlook|forecast)|'
                r'profit warning|weak (guidance|outlook|forecast)|revenue (decline|drop|falls?)|losses? widen\w*)\b',
    'finance': r'\b(bankrupt\w*|chapter 11|default\w*|going concern|liquidity (crunch|concerns?)|dilut\w+|(stock|share) offering|'
               r'credit rating (cut|downgrade)|junk status|dividend cut|cuts? (its )?dividend|suspends? (its )?dividend)\b',
    'ops': r'\b(recalls?|outage|data breach|breach|hack(ed|ers?)?|cyberattack|(workers|union) strike|on strike|plant shutdown|'
           r'production halt|supply (chain )?(disruption|shortage)|layoffs?|job cuts|cuts? \d[\d,]* jobs|restructuring)\b',
    'mgmt': r'\b((ceo|cfo|chief executive|chief financial officer) (resigns?|steps? down|departs?|exits?|ousted|fired)|'
            r'accounting (issues?|irregularit\w+|probe)|restate\w*|whistleblower)\b',
    'market': r'\b(plunge[sd]?|plummet\w*|tumble[sd]?|sinks?|sank|slump\w*|crash\w*|sell-?off|short sellers?|short report|delist\w*|52-week low)\b',
}.items()}
POS = re.compile(r'\b(upgrade[sd]?|beats?|tops? (estimates|expectations)|raises? (its |full-year |annual )?(guidance|outlook|forecast|dividend)|'
                 r'record (revenue|sales|profit|earnings|high)|buybacks?|repurchases?|approval|approved|surge[sd]?|soar\w*|jumps?|'
                 r'rall(y|ies|ied)|outperform\w*|strong demand|wins? (a )?contract|dividend (hike|increase))\b', re.I)
UA = 'Mozilla/5.0 (compatible; stock-screener-builder/1.0)'
INDEX_PAGES = {  # name -> (Wikipedia page, minimum members for the parse to count)
    'S&P 500': ('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies', 450),
    'Nasdaq-100': ('https://en.wikipedia.org/wiki/Nasdaq-100', 90),
}
SP500_CSV = 'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv'
MIN_STOCKS = 300  # fewer individual stocks than this means the index lists failed: do not publish
# ETF -> category shown on the page
ETFS = {
    'VOO': '大型股指數', 'SPY': '大型股指數', 'IVV': '大型股指數', 'VTI': '全市場', 'ITOT': '全市場',
    'QQQ': '科技／成長', 'QQQM': '科技／成長', 'VGT': '科技／成長', 'XLK': '科技／成長', 'SMH': '半導體',
    'SOXX': '半導體', 'VUG': '科技／成長', 'SCHG': '科技／成長', 'MGK': '科技／成長',
    'SCHD': '高股息', 'VYM': '高股息', 'HDV': '高股息', 'DGRO': '股息成長', 'VIG': '股息成長',
    'NOBL': '股息成長', 'SDY': '股息成長', 'DVY': '高股息', 'SPYD': '高股息', 'JEPI': '備兌權利金',
    'JEPQ': '備兌權利金', 'QYLD': '備兌權利金',
    'VTV': '價值', 'SCHV': '價值', 'IWD': '價值', 'RPV': '價值', 'AVUV': '小型價值',
    'IWM': '小型股', 'VB': '小型股', 'IJH': '中型股', 'RSP': '等權重',
    'VXUS': '國際股', 'VEA': '國際股', 'VWO': '新興市場',
    'BND': '債券', 'AGG': '債券', 'TLT': '長天期公債', 'IEF': '中天期公債', 'SGOV': '短期國庫券',
    'BIL': '短期國庫券', 'LQD': '投資級公司債', 'HYG': '高收益債',
    'VNQ': '不動產', 'GLD': '黃金', 'IAU': '黃金',
    'XLF': '金融', 'XLV': '醫療', 'XLE': '能源', 'XLI': '工業', 'XLP': '必需消費', 'XLY': '非必需消費',
    'XLU': '公用事業', 'XLC': '通訊',
}
SECTOR_ZH = {
    'Technology': '資訊科技', 'Healthcare': '醫療保健', 'Financial Services': '金融', 'Consumer Cyclical': '非必需消費',
    'Consumer Defensive': '必需消費', 'Communication Services': '通訊服務', 'Industrials': '工業', 'Energy': '能源',
    'Utilities': '公用事業', 'Real Estate': '不動產', 'Basic Materials': '原物料',
}


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:  # network hiccup: back off and retry
            if i == tries - 1:
                raise
            print(f'  retry {url}: {e}', file=sys.stderr)
            time.sleep(2 ** i * 3)


def _cell(c):
    c = re.sub(r'<sup.*?</sup>', '', c, flags=re.S)           # footnote markers
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', c))).strip()


def index_members(url, minimum):
    """Ticker -> company name from the wikitable that lists the most tickers.

    Does not depend on table ids or bare <tr> tags, which change when Wikipedia changes its HTML."""
    page = get(url)
    best = {}
    for table in re.findall(r'<table[^>]*class="[^"]*wikitable[^"]*"[^>]*>.*?</table>', page, re.S):
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S)
        if not rows:
            continue
        cols = [_cell(c).lower() for c in re.findall(r'<th[^>]*>(.*?)</th>', rows[0], re.S)]
        ti = next((i for i, c in enumerate(cols) if c in ('symbol', 'ticker', 'ticker symbol')), None)
        if ti is None:
            continue
        ni = next((i for i, c in enumerate(cols) if c in ('security', 'company', 'company name')), None)
        out = {}
        for row in rows[1:]:
            cells = [_cell(c) for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)]
            if len(cells) > ti and re.fullmatch(r'[A-Z][A-Z.\-]{0,6}', cells[ti]):
                out[cells[ti]] = cells[ni] if ni is not None and ni < len(cells) else ''
        if len(out) > len(best):
            best = out
    if len(best) < minimum:
        raise RuntimeError(f'only {len(best)} tickers found at {url} (expected at least {minimum})')
    return best


def sp500_csv(minimum):
    """Backup S&P 500 list (github.com/datasets/s-and-p-500-companies, refreshed from Wikipedia)."""
    import csv, io
    rows = list(csv.DictReader(io.StringIO(get(SP500_CSV))))
    out = {r['Symbol'].strip(): (r.get('Security') or r.get('Name') or '').strip() for r in rows if r.get('Symbol')}
    if len(out) < minimum:
        raise RuntimeError(f'backup list has only {len(out)} tickers')
    return out


def load_previous():
    try:
        with open(OUT, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'meta': {}, 'items': []}


def build_universe(prev, only):
    if only:
        return {t: {'indexes': []} for t in only}
    uni = {}
    for name, (url, minimum) in INDEX_PAGES.items():
        try:
            members = index_members(url, minimum)
            print(f'{name}: {len(members)} members')
        except Exception as e:
            print(f'{name}: Wikipedia failed ({e})', file=sys.stderr)
            members = {}
            if name == 'S&P 500':
                try:
                    members = sp500_csv(minimum)
                    print(f'{name}: {len(members)} members from the backup list')
                except Exception as e2:
                    print(f'{name}: backup list failed ({e2})', file=sys.stderr)
            if not members:  # keep last run's membership rather than drop the index
                members = {it['symbol']: it.get('name', '') for it in prev['items'] if name in it.get('indexes', [])}
                print(f'{name}: reusing previous membership ({len(members)})', file=sys.stderr)
        for t in members:
            uni.setdefault(t, {'indexes': []})['indexes'].append(name)
    for t in ETFS:
        uni.setdefault(t, {'indexes': []})
    if os.path.exists(EXTRA):
        for line in open(EXTRA, encoding='utf-8'):
            t = line.split('#')[0].strip().upper()
            if t:
                uni.setdefault(t, {'indexes': []})
    return uni


def yf_symbol(t):
    return t.replace('.', '-')  # BRK.B -> BRK-B on Yahoo


def num(v, scale=1, nd=2):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v * scale, nd)


def pct_auto(v):
    """Yahoo reports some fund ratios as fractions and some as percents; expense ratios under 0.01 are fractions."""
    v = num(v, 1, 6)
    if v is None:
        return None
    return round(v * 100 if v < 0.01 else v, 3)


def fetch_info(t):
    import yfinance as yf
    last = None
    for i in range(3):
        try:
            info = yf.Ticker(yf_symbol(t)).info or {}
            if info.get('quoteType') or info.get('regularMarketPrice') or info.get('currentPrice'):
                return info
            last = 'empty info'
        except Exception as e:
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(str(last))


def fetch_iv(t, price):
    """At-the-money implied volatility (%) and days to expiry of the listed expiry nearest 30 days out."""
    import yfinance as yf
    if not price:
        return None
    try:
        tk = yf.Ticker(yf_symbol(t))
        today = dt.date.today()
        exps = [(abs((dt.date.fromisoformat(e) - today).days - 30), e) for e in (tk.options or [])
                if 14 <= (dt.date.fromisoformat(e) - today).days <= 75]
        if not exps:
            return None
        exp = min(exps)[1]
        ch = tk.option_chain(exp)
        ivs = []
        for side in (ch.calls, ch.puts):
            side = side[(side['impliedVolatility'] > 0.01) & (side['impliedVolatility'] < 5)]
            if len(side):
                row = side.iloc[(side['strike'] - price).abs().argsort()[:1]]
                ivs.append(float(row['impliedVolatility'].iloc[0]))
        if not ivs:
            return None
        return {'iv': round(sum(ivs) / len(ivs) * 100, 1), 'ivDte': (dt.date.fromisoformat(exp) - today).days}
    except Exception as e:  # options are a bonus: never fail the ticker over them
        print(f'  {t} options: {e}', file=sys.stderr)
        return None


def tag_headline(title):
    """(-1/0/+1, [negative categories]) from keyword rules."""
    cats = [k for k, rx in NEG.items() if rx.search(title)]
    pos = bool(POS.search(title))
    s = -1 if cats and (not pos or len(cats) > 1) else 1 if pos and not cats else 0
    return s, cats


def _iso(ts):
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            return dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except ValueError:
            try:
                return email.utils.parsedate_to_datetime(ts)
            except (TypeError, ValueError):
                return None
    return None


def yahoo_news(t):
    import yfinance as yf
    out = []
    for n in yf.Ticker(yf_symbol(t)).news or []:
        c = n.get('content') or n  # yfinance >= 0.2.50 nests the story under "content"
        url = ((c.get('canonicalUrl') or {}).get('url') or (c.get('clickThroughUrl') or {}).get('url')
               or n.get('link'))
        out.append({'title': c.get('title'), 'pub': (c.get('provider') or {}).get('displayName') or n.get('publisher'),
                    'url': url, 'when': _iso(c.get('pubDate') or c.get('displayTime') or n.get('providerPublishTime'))})
    return out


def google_news(t):
    q = urllib.parse.quote(f'{t} stock')
    root = ET.fromstring(get(f'https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en', tries=2))
    out = []
    for it in root.iter('item'):
        title, src = it.findtext('title') or '', it.find('source')
        pub = src.text if src is not None else ''
        if pub and title.endswith(' - ' + pub):
            title = title[:-len(pub) - 3]
        out.append({'title': title, 'pub': pub, 'url': it.findtext('link'), 'when': _iso(it.findtext('pubDate'))})
    return out


def fetch_news(t):
    try:
        raw = yahoo_news(t)
    except Exception as e:
        print(f'  {t} yahoo news: {e}', file=sys.stderr)
        raw = []
    if not raw:
        try:
            raw = google_news(t)
        except Exception as e:
            print(f'  {t} google news: {e}', file=sys.stderr)
            return None
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=NEWS_DAYS)
    seen, out = set(), []
    for n in sorted((n for n in raw if n['title'] and n['when'] and n['when'] >= cut), key=lambda n: n['when'], reverse=True):
        key = re.sub(r'\W+', ' ', n['title'].lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        s, cats = tag_headline(n['title'])
        out.append({'d': n['when'].isoformat(timespec='minutes'), 't': n['title'], 'p': n['pub'] or '', 'u': n['url'] or '',
                    's': s, 'c': cats})
    return out[:NEWS_KEEP]


def news_summary(news):
    """Recent-news fields for stocks.json; newer headlines weigh more."""
    now = dt.datetime.now(dt.timezone.utc)
    recent = []
    for n in news or []:
        age = (now - dt.datetime.fromisoformat(n['d'])).days
        if age <= RISK_DAYS:
            recent.append((1 if age <= 3 else .6 if age <= 7 else .3, n))
    w = sum(x for x, _ in recent)
    neg = [(x, n) for x, n in recent if n['s'] < 0]
    return {
        'newsN': len(recent), 'newsNeg': len(neg), 'newsPos': sum(n['s'] > 0 for _, n in recent),
        'newsSent': round(sum(x * n['s'] for x, n in recent) / w, 2) if w else None,
        'newsRisk': round(max(0, sum(x * (1 + .5 * (len(n['c']) - 1)) for x, n in neg)
                              - .5 * sum(x for x, n in recent if n['s'] > 0)), 2),
        'newsFlags': sorted({c for _, n in neg for c in n['c']}),
    }


def fetch_all(t, with_news=True):
    info = fetch_info(t)
    price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('navPrice')
    return info, fetch_iv(t, price), fetch_news(t) if with_news else None


def record(t, info, meta):
    etf = info.get('quoteType') == 'ETF' or t in ETFS
    price = num(info.get('currentPrice') or info.get('regularMarketPrice') or info.get('navPrice'))
    rate = num(info.get('dividendRate') or info.get('trailingAnnualDividendRate'), 1, 4)
    if etf:
        dy = num(info.get('yield'), 100) if info.get('yield') is not None else None
    else:
        dy = (round(rate / price * 100, 2) if rate and price else 0.0 if rate == 0
              else num(info.get('trailingAnnualDividendYield'), 100))
    r = {
        'symbol': t,
        'name': info.get('shortName') or info.get('longName') or '',
        'type': 'etf' if etf else 'stock',
        'indexes': meta['indexes'],
        'sector': ETFS.get(t) if etf else SECTOR_ZH.get(info.get('sector'), info.get('sector') or ''),
        'industry': '' if etf else (info.get('industry') or ''),
        'price': price,
        'mcap': num(info.get('marketCap') or info.get('totalAssets'), 1e-9, 2),  # US$ billions (ETF: AUM)
        'pe': num(info.get('trailingPE'), 1, 1),
        'fpe': num(info.get('forwardPE'), 1, 1),
        'peg': num(info.get('trailingPegRatio') or info.get('pegRatio')),
        'pb': num(info.get('priceToBook')),
        'ps': num(info.get('priceToSalesTrailing12Months')),
        'evEbitda': num(info.get('enterpriseToEbitda'), 1, 1),
        'divYield': dy,
        'divRate': rate,
        'payout': num(info.get('payoutRatio'), 100, 1),
        'div5y': num(info.get('fiveYearAvgDividendYield')),  # already in percent on Yahoo
        'revGrowth': num(info.get('revenueGrowth'), 100, 1),
        'epsGrowth': num(info.get('earningsGrowth') or info.get('earningsQuarterlyGrowth'), 100, 1),
        'roe': num(info.get('returnOnEquity'), 100, 1),
        'margin': num(info.get('profitMargins'), 100, 1),
        'grossMargin': num(info.get('grossMargins'), 100, 1),
        'de': num(info.get('debtToEquity'), 0.01),  # Yahoo gives percent; store as a ratio
        'currentRatio': num(info.get('currentRatio')),
        'fcfYield': (num(info.get('freeCashflow') / info['marketCap'], 100, 2)
                     if info.get('freeCashflow') and info.get('marketCap') else None),
        'beta': num(info.get('beta') or info.get('beta3Year')),
        'target': num(info.get('targetMeanPrice')),
        'rating': num(info.get('recommendationMean'), 1, 1),  # 1 strong buy .. 5 sell
        'analysts': info.get('numberOfAnalystOpinions'),
        'expense': pct_auto(info.get('netExpenseRatio') or info.get('annualReportExpenseRatio')) if etf else None,
        'asOf': dt.date.today().isoformat(),
    }
    prev = num(info.get('regularMarketPreviousClose') or info.get('previousClose'))
    if prev and price:
        r['prevClose'] = prev
        r['chg1d'] = round((price / prev - 1) * 100, 2)
    if r['target'] and price:
        r['upside'] = round((r['target'] / price - 1) * 100, 1)
    return r


def add_returns(items):
    """Dividend-adjusted price returns, computed from one batch download."""
    import yfinance as yf
    syms = [yf_symbol(it['symbol']) for it in items]
    closes = {}
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period='5y', interval='1d', auto_adjust=True, progress=False,
                                 group_by='column', threads=True)['Close']
                break
            except Exception as e:
                print(f'  download retry: {e}', file=sys.stderr)
                time.sleep(10 * (attempt + 1))
        else:
            continue
        if hasattr(df, 'columns'):
            for s in df.columns:
                closes[s] = df[s].dropna()
        else:  # single ticker comes back as a Series
            closes[chunk[0]] = df.dropna()
    today = dt.date.today()
    for it in items:
        s = closes.get(yf_symbol(it['symbol']))
        if s is None or len(s) < 30:
            continue
        last, last_day = float(s.iloc[-1]), s.index[-1].date()

        def back(days):
            cut = s[s.index.date <= last_day - dt.timedelta(days=days)]
            return float(cut.iloc[-1]) if len(cut) and (last_day - cut.index[-1].date()).days < days + 10 else None

        def ret(days, years=None):
            p = back(days)
            if not p:
                return None
            g = last / p
            return round(((g ** (1 / years)) - 1) * 100 if years else (g - 1) * 100, 1)

        ytd = s[s.index.date < dt.date(today.year, 1, 1)]
        yr = s[s.index.date > last_day - dt.timedelta(days=365)]
        it['r1m'] = ret(30)
        it['rYtd'] = round((last / float(ytd.iloc[-1]) - 1) * 100, 1) if len(ytd) else None
        it['r1y'] = ret(365)
        it['r3y'] = ret(365 * 3, 3)   # annualised
        it['r5y'] = ret(365 * 5 - 7, 5)  # annualised; a few days of slack for the 5y window edge
        it['fromHigh'] = round((last / float(yr.max()) - 1) * 100, 1)
        daily = yr.pct_change().dropna()
        it['vol'] = round(float(daily.std()) * math.sqrt(252) * 100, 1) if len(daily) > 20 else None
        peak = s.cummax()
        it['maxDD'] = round(float((s / peak - 1).min()) * 100, 1)
        if it.get('iv') and it.get('vol') and it['vol'] >= 3:  # near-cash funds barely move: the ratio means nothing
            it['ivHv'] = round(it['iv'] / it['vol'], 2)  # >1: options priced richer than the stock has actually moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='only the first N tickers (testing)')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--only', default='', help='comma separated tickers (testing)')
    ap.add_argument('--no-news', action='store_true', help='skip headlines (keeps the previous data/news.json)')
    a = ap.parse_args()

    prev = load_previous()
    prev_by = {it['symbol']: it for it in prev['items']}
    uni = build_universe(prev, [t.strip().upper() for t in a.only.split(',') if t.strip()])
    tickers = sorted(uni)[:a.limit] if a.limit else sorted(uni)
    print(f'{len(tickers)} tickers')
    n_stocks = sum(1 for t in tickers if t not in ETFS)
    if not a.only and not a.limit and n_stocks < MIN_STOCKS:
        sys.exit(f'only {n_stocks} individual stocks in the universe (index lists failed); not publishing')

    try:
        prev_news = json.load(open(NEWS_OUT, encoding='utf-8')).get('news', {})
    except (OSError, ValueError):
        prev_news = {}
    news = {}
    items, failed = [], []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fetch_all, t, not a.no_news): t for t in tickers}
        for n, f in enumerate(cf.as_completed(futs), 1):
            t = futs[f]
            try:
                info, iv, nw = f.result()
                news[t] = nw if nw is not None else prev_news.get(t, [])  # a failed news fetch keeps yesterday's
                items.append({**record(t, info, uni[t]), **(iv or {}), **news_summary(news[t])})
            except Exception as e:
                failed.append(t)
                if t in prev_by:  # keep yesterday's numbers rather than lose the row
                    items.append({**prev_by[t], 'indexes': uni[t]['indexes']})
                if t in prev_news:
                    news[t] = prev_news[t]
                print(f'  {t}: {e}', file=sys.stderr)
            if n % 50 == 0:
                print(f'  {n}/{len(tickers)}')

    fresh = len(tickers) - len(failed)
    if fresh < len(tickers) * 0.5 and prev['items'] and not a.only:
        sys.exit(f'only {fresh}/{len(tickers)} tickers fetched; keeping the previous data/stocks.json')

    add_returns(items)
    items.sort(key=lambda it: (it['type'] != 'stock', -(it.get('mcap') or 0)))
    out = {
        'meta': {
            'generatedAt': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'source': 'Yahoo Finance (yfinance); index members from Wikipedia',
            'count': len(items), 'stocks': sum(it['type'] == 'stock' for it in items),
            'etfs': sum(it['type'] == 'etf' for it in items), 'failed': sorted(failed),
        },
        'items': items,
    }
    os.makedirs(DATA, exist_ok=True)
    with open(OUT + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(OUT + '.tmp', OUT)
    with open(NEWS_OUT + '.tmp', 'w', encoding='utf-8') as f:
        json.dump({'meta': {'generatedAt': out['meta']['generatedAt'], 'days': NEWS_DAYS},
                   'news': {t: n for t, n in sorted(news.items()) if n}}, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(NEWS_OUT + '.tmp', NEWS_OUT)
    print(f'wrote {OUT}: {out["meta"]["stocks"]} stocks, {out["meta"]["etfs"]} ETFs, {len(failed)} failed')


if __name__ == '__main__':
    main()

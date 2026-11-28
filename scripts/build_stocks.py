#!/usr/bin/env python3
"""Build data/stocks.json: fundamentals and returns for US stocks and ETFs, for stocks.html.

Universe
  - S&P 500 and Nasdaq-100 members (Wikipedia constituent tables)
  - the ETFs in ETFS below
  - any extra tickers listed in data/stocks-extra.txt (one per line, # comments allowed)

Data (Yahoo Finance through the yfinance package: pip install yfinance)
  - Ticker.info: name, sector, valuation (P/E, P/B, P/S, PEG), dividends, growth, margins, ROE, debt, ETF expense ratio
  - Ticker.option_chain: at-the-money implied volatility of the expiry nearest 30 days out (iv, ivDte)
  - yf.download (dividend-adjusted closes, 5 years): 1-month, YTD, 1-year, 3-year and 5-year returns,
    distance from the 52-week high, volatility and max drawdown, computed here the same way for stocks and ETFs

A ticker that fails this run keeps its previous record (marked with its old asOf date), so one bad run
never empties the page.

Usage: python scripts/build_stocks.py [--limit N] [--workers 4] [--only AAPL,MSFT]
"""
import argparse, concurrent.futures as cf, datetime as dt, html, json, math, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
OUT = os.path.join(DATA, 'stocks.json')
EXTRA = os.path.join(DATA, 'stocks-extra.txt')
UA = 'Mozilla/5.0 (compatible; stock-screener-builder/1.0)'
INDEX_PAGES = {
    'S&P 500': 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
    'Nasdaq-100': 'https://en.wikipedia.org/wiki/Nasdaq-100',
}
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


def index_members(url):
    """Ticker -> company name from the first wikitable with id="constituents"."""
    page = get(url)
    m = re.search(r'<table[^>]*id="constituents".*?</table>', page, re.S)
    if not m:
        raise RuntimeError(f'constituents table not found at {url}')
    table = m.group(0)
    head = re.search(r'<tr>(.*?)</tr>', table, re.S).group(1)
    cols = [re.sub(r'<[^>]+>', '', c).strip().lower() for c in re.findall(r'<th[^>]*>(.*?)</th>', head, re.S)]
    ti = next(i for i, c in enumerate(cols) if c in ('symbol', 'ticker'))
    ni = next((i for i, c in enumerate(cols) if c in ('security', 'company')), None)
    out = {}
    for row in re.findall(r'<tr>(.*?)</tr>', table, re.S)[1:]:
        cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)]
        if len(cells) <= ti or not re.fullmatch(r'[A-Z][A-Z.\-]{0,6}', cells[ti]):
            continue
        out[cells[ti]] = cells[ni] if ni is not None and ni < len(cells) else ''
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
    for name, url in INDEX_PAGES.items():
        try:
            members = index_members(url)
            print(f'{name}: {len(members)} members')
        except Exception as e:  # keep last run's membership rather than drop the index
            print(f'{name}: failed ({e}); reusing previous membership', file=sys.stderr)
            members = {it['symbol']: it.get('name', '') for it in prev['items'] if name in it.get('indexes', [])}
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


def fetch_all(t):
    info = fetch_info(t)
    price = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('navPrice')
    return info, fetch_iv(t, price)


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
        if it.get('iv') and it.get('vol'):
            it['ivHv'] = round(it['iv'] / it['vol'], 2)  # >1: options priced richer than the stock has actually moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='only the first N tickers (testing)')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--only', default='', help='comma separated tickers (testing)')
    a = ap.parse_args()

    prev = load_previous()
    prev_by = {it['symbol']: it for it in prev['items']}
    uni = build_universe(prev, [t.strip().upper() for t in a.only.split(',') if t.strip()])
    tickers = sorted(uni)[:a.limit] if a.limit else sorted(uni)
    print(f'{len(tickers)} tickers')

    items, failed = [], []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(fetch_all, t): t for t in tickers}
        for n, f in enumerate(cf.as_completed(futs), 1):
            t = futs[f]
            try:
                info, iv = f.result()
                items.append({**record(t, info, uni[t]), **(iv or {})})
            except Exception as e:
                failed.append(t)
                if t in prev_by:  # keep yesterday's numbers rather than lose the row
                    items.append({**prev_by[t], 'indexes': uni[t]['indexes']})
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
    print(f'wrote {OUT}: {out["meta"]["stocks"]} stocks, {out["meta"]["etfs"]} ETFs, {len(failed)} failed')


if __name__ == '__main__':
    main()

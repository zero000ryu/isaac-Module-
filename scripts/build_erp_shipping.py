#!/usr/bin/env python3
"""Wrap shipping.html into a full page for the 牙材 ERP (dental-erp repo, tools/shipping.html).

shipping.html is written as page content (the claude.ai artifact adds <html>/<head> itself). The ERP
serves it as a static file, so this adds the document skeleton and <meta name="erp">, which turns on
the "匯入 ERP" panel (same-origin calls to /api/auth, /api/records, /api/orders).

  python scripts/build_erp_shipping.py ../dental-erp/tools/shipping.html
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = '''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="erp" content="1">
<meta name="robots" content="noindex,nofollow">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='%230b7a3e'/><text x='16' y='22' font-size='16' text-anchor='middle' fill='white' font-family='sans-serif'>牙</text></svg>">
<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}img{max-width:100%}[hidden]{display:none!important}</style>
</head>
<body>
<!-- 由 isaac-Module- 的 shipping.html 產生（scripts/build_erp_shipping.py），請改那邊再重新產生 -->
'''

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, '..', 'dental-erp', 'tools', 'shipping.html')
    body = open(os.path.join(ROOT, 'shipping.html'), encoding='utf-8').read()
    with open(out, 'w', encoding='utf-8') as f:
        f.write(HEAD + body.rstrip() + '\n</body>\n</html>\n')
    print('wrote', out)

if __name__ == '__main__':
    main()

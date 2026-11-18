#!/usr/bin/env python3
"""Upload a dataset to the ERP (erp.top-geng.com /api/lookup) so it is only readable after ERP login.

  python scripts/upload_erp_data.py --name clinics --file data/dental.json

Needs env DENTAL_DATA_TOKEN (same value as the ERP's Vercel environment variable). Without it the
script does nothing, so scheduled jobs still succeed. ERP_URL defaults to https://erp.top-geng.com.
The data is sent in parts (Vercel limits a request to about 4.5 MB) and switched over atomically.
"""
import argparse, hashlib, json, os, sys, time, urllib.error, urllib.request

PART_CHARS = 1_000_000


def post(base, token, body, tries=4):
    data = json.dumps(body, ensure_ascii=False).encode()
    for i in range(tries):
        req = urllib.request.Request(base.rstrip('/') + '/api/lookup', data=data, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': f'Bearer {token}', 'User-Agent': 'dental-data-uploader/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read()[:300].decode('utf-8', 'replace')
            if e.code < 500 or i == tries - 1:
                raise SystemExit(f'ERP upload failed: HTTP {e.code} {msg}')
        except Exception as e:
            if i == tries - 1:
                raise SystemExit(f'ERP upload failed: {e!r}')
        time.sleep(5 * (i + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', required=True, choices=['clinics', 'hospital_doctors'])
    ap.add_argument('--file', required=True)
    a = ap.parse_args()
    token = os.environ.get('DENTAL_DATA_TOKEN', '').strip()
    if not token:
        print('DENTAL_DATA_TOKEN not set; skipping ERP data upload', file=sys.stderr)
        return
    base = os.environ.get('ERP_URL', 'https://erp.top-geng.com')
    text = open(a.file, encoding='utf-8').read()
    json.loads(text)  # refuse to upload a broken file
    version = time.strftime('%Y%m%d%H%M%S') + '-' + hashlib.sha256(text.encode()).hexdigest()[:8]
    parts = [text[i:i + PART_CHARS] for i in range(0, len(text), PART_CHARS)]
    for i, p in enumerate(parts):
        post(base, token, {'action': 'part', 'name': a.name, 'version': version, 'part': i, 'body': p})
    r = post(base, token, {'action': 'commit', 'name': a.name, 'version': version, 'parts': len(parts)})
    print(f'ERP: uploaded {a.name} {version} in {len(parts)} parts ({r.get("bytes", 0):,} bytes)', file=sys.stderr)


if __name__ == '__main__':
    main()

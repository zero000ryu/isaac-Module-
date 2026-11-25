#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Excel 彙整工具：把一個資料夾裡所有 Excel / CSV 合併成一份總表，依月份排列。

用法
  1. 直接雙擊（或執行 run.bat）：彙整「這支程式所在資料夾」裡的檔案
  2. 指令列：
       python merge_excel_by_month.py 資料夾路徑
       python merge_excel_by_month.py 資料夾路徑 -o 總表.xlsx
       python merge_excel_by_month.py 資料夾路徑 --date-col 出貨日期
       python merge_excel_by_month.py 資料夾路徑 --no-split      (不要每月分頁)
       python merge_excel_by_month.py 資料夾路徑 --recursive     (含子資料夾)

輸出的 Excel 包含
  - 總表：所有資料合併，依月份（再依日期）排序，並加上「月份 / 來源檔案 / 工作表」欄
  - 月份彙總：每月筆數與數字欄位加總
  - 每個月一個分頁（例如 2026-03），可用 --no-split 關閉
  - 檔案清單：每個檔案、工作表讀到幾筆，月份怎麼判斷的

月份判斷順序
  1. 資料裡的日期欄（欄名含「日期 / 日 / date / 月份 ...」，或用 --date-col 指定）
     支援西元與民國（115/03/05、1150305、115年3月5日）
  2. 工作表名稱（例如「2026-03」「115年3月」「3月」）
  3. 檔名（例如「2026_03出貨.xlsx」「11503報表.xlsx」）

需要套件：pip install pandas openpyxl     （讀舊版 .xls 另需 pip install xlrd）
"""
import argparse
import datetime as dt
import os
import re
import sys

try:
    import pandas as pd
except ImportError:
    print("缺少 pandas，請先執行：pip install pandas openpyxl")
    if os.name == "nt":
        input("按 Enter 結束...")
    sys.exit(1)

# ─── 可自行調整的設定 ──────────────────────────────────────────────

# 欄位別名：不同檔案欄名不一樣時，統一成左邊的名稱
COLUMN_ALIASES = {
    "日期": ["日期", "Date", "date", "交易日期", "單據日期"],
    "品名": ["品名", "產品名稱", "商品名稱", "品項", "產品"],
    "數量": ["數量", "Qty", "qty", "QTY"],
    "單價": ["單價", "價格", "Price"],
    "金額": ["金額", "小計", "總額", "Amount", "合計金額"],
    "客戶": ["客戶", "客戶名稱", "顧客", "診所", "診所名稱"],
}

# 欄名含這些字就當成日期欄候選（越前面越優先）
DATE_KEYWORDS = ["日期", "date", "出貨日", "訂單日", "交易日", "銷貨日", "進貨日", "時間", "月份", "年月", "日"]

# 欄名含這些字的數字欄不做加總（編號類）
NO_SUM_KEYWORDS = ["編號", "代號", "號碼", "電話", "統編", "序號", "單號", "料號", "id", "ID", "郵遞", "年", "月份", "項次"]

SUPPORTED_EXT = (".xlsx", ".xlsm", ".xls", ".csv")
DEFAULT_OUTPUT = "彙整總表.xlsx"
UNKNOWN_MONTH = "未知月份"

# ─────────────────────────────────────────────────────────────────


def norm_name(s):
    return re.sub(r"\s+", "", str(s)).strip()


ALIAS_LOOKUP = {}
for target, names in COLUMN_ALIASES.items():
    for n in names:
        ALIAS_LOOKUP[norm_name(n).lower()] = target


def unify_column(name):
    n = norm_name(name)
    return ALIAS_LOOKUP.get(n.lower(), n)


# ─── 日期 / 月份解析 ──────────────────────────────────────────────

def _valid(y, m, d=1):
    try:
        if 1900 <= y <= 2200:
            return dt.datetime(y, m, d)
    except ValueError:
        pass
    return None


def parse_date(v):
    """把各種寫法的日期轉成 datetime；失敗回傳 None。"""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.to_pydatetime()
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        if 20000 <= v <= 80000:  # Excel 序號日期（約 1954～2119 年）
            return dt.datetime(1899, 12, 30) + dt.timedelta(days=float(v))
        s = str(int(v))
    else:
        s = str(v).strip()
    if not s:
        return None

    s = s.replace("　", " ")
    # 115年3月5日 / 2026年3月5日 / 115年3月
    m = re.search(r"(\d{2,4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日?)?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        return _valid(y + 1911 if y < 1911 else y, mo, d)
    # 2026/03/05、115/3/5、2026-03-05 12:00
    m = re.match(r"^(\d{2,4})[/\-.](\d{1,2})(?:[/\-.](\d{1,2}))?(?:[ T].*)?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        return _valid(y + 1911 if y < 1911 else y, mo, d)
    # 20260305 / 1150305 / 202603 / 11503
    if re.fullmatch(r"\d{5,8}", s):
        if len(s) == 8:
            return _valid(int(s[:4]), int(s[4:6]), int(s[6:]))
        if len(s) == 7:
            return _valid(int(s[:3]) + 1911, int(s[3:5]), int(s[5:]))
        if len(s) == 6:
            return _valid(int(s[:4]), int(s[4:]))
        if len(s) == 5:
            return _valid(int(s[:3]) + 1911, int(s[3:]))
    # 03/05/2026（美式）
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return _valid(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    try:
        r = pd.to_datetime(s, errors="coerce")
        return None if pd.isna(r) else r.to_pydatetime()
    except Exception:
        return None


def year_from_text(text):
    """從檔名猜年份（2026、115年），回傳西元年或 None。"""
    t = os.path.splitext(str(text))[0]
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", t) or re.search(r"(?<!\d)(20\d{2})", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<!\d)(1\d{2})\s*年", t) or re.search(r"(?<!\d)(1[01]\d)(?!\d)", t)
    if m:
        return int(m.group(1)) + 1911
    return None


def month_from_text(text, default_year=None):
    """從工作表名稱或檔名猜月份，回傳 'YYYY-MM' 或 None。"""
    t = os.path.splitext(str(text))[0]
    pats = [
        (r"(20\d{2})\s*[年\-_/.]?\s*(0?[1-9]|1[0-2])\s*月", None),
        (r"(1\d{2})\s*年\s*(0?[1-9]|1[0-2])\s*月", 1911),
        (r"(20\d{2})[\-_/.](0?[1-9]|1[0-2])(?!\d)", None),
        (r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)", None),
        (r"(?<!\d)(1\d{2})[\-_/.]?(0[1-9]|1[0-2])(?!\d)", 1911),
    ]
    for p, add in pats:
        m = re.search(p, t)
        if m:
            y = int(m.group(1)) + (add or 0)
            return f"{y:04d}-{int(m.group(2)):02d}"
    m = re.search(r"(?<!\d)(0?[1-9]|1[0-2])\s*月", t)
    if m and default_year:
        return f"{default_year:04d}-{int(m.group(1)):02d}"
    return None


# ─── 讀檔 ────────────────────────────────────────────────────────

def find_header_row(raw):
    """在前 15 列裡找最像表頭的一列（非空格最多、且大多是文字）。"""
    best, best_score = 0, -1
    for i in range(min(15, len(raw))):
        row = raw.iloc[i]
        vals = [v for v in row if not (v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "")]
        if not vals:
            continue
        texts = sum(1 for v in vals if isinstance(v, str) and not re.fullmatch(r"[\d.,\-/ ]+", v))
        score = len(vals) + texts
        if score > best_score:
            best, best_score = i, score
    return best


def tidy(raw):
    """原始表格 → 有表頭的 DataFrame。"""
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty:
        return raw
    raw = raw.reset_index(drop=True)
    h = find_header_row(raw)
    headers, seen = [], {}
    for j, v in enumerate(raw.iloc[h]):
        name = unify_column(v) if not (v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "") else f"欄{j + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        headers.append(name)
    df = raw.iloc[h + 1:].copy()
    df.columns = headers
    df = df.dropna(how="all")
    # 去掉「合計 / 總計」列
    total_re = re.compile(r"\s*(合計|總計|小計|total)\s*", re.I)
    mask = df.apply(lambda r: any(isinstance(x, str) and total_re.fullmatch(x) for x in r), axis=1)
    df = df[~mask]
    return df.reset_index(drop=True)


def read_csv_any(path):
    for enc in ("utf-8-sig", "cp950", "big5", "utf-16", "latin-1"):
        try:
            return pd.read_csv(path, header=None, dtype=object, encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError("無法判斷 CSV 編碼")


def read_file(path):
    """回傳 [(工作表名稱, DataFrame), ...]"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return [("", tidy(read_csv_any(path)))]
    engine = "xlrd" if ext == ".xls" else "openpyxl"
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine=engine)
    return [(name, tidy(df)) for name, df in sheets.items()]


# ─── 主流程 ──────────────────────────────────────────────────────

def pick_date_column(df, forced=None):
    cols = list(df.columns)
    if forced:
        f = unify_column(forced)
        return f if f in cols else None
    candidates = []
    for col in cols:
        lc = str(col).lower()
        for rank, kw in enumerate(DATE_KEYWORDS):
            if kw.lower() in lc:
                candidates.append((rank, cols.index(col), col))
                break
    for _, _, col in sorted(candidates):
        sample = df[col].dropna().head(50)
        if len(sample) and sum(parse_date(v) is not None for v in sample) >= len(sample) * 0.6:
            return col
    # 沒有日期欄名，就找內容大多是日期型態的欄
    for col in cols:
        sample = df[col].dropna().head(50)
        if len(sample) >= 1 and all(isinstance(v, (dt.datetime, dt.date, pd.Timestamp)) for v in sample):
            return col
    return None


def collect_files(folder, recursive, output_path):
    out_abs = os.path.abspath(output_path)
    files = []
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    for root, _, names in walker:
        for n in sorted(names):
            p = os.path.join(root, n)
            if not os.path.isfile(p) or n.startswith("~$") or n.startswith("."):
                continue
            if not n.lower().endswith(SUPPORTED_EXT):
                continue
            if os.path.abspath(p) == out_abs or n.startswith("彙整總表"):
                continue
            files.append(p)
    return files


def to_number(series):
    s = series.map(lambda x: "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)).str.replace(",", "", regex=False).str.replace("$", "", regex=False).str.strip()
    s = s.replace({"": None, "nan": None, "None": None, "-": None})
    return pd.to_numeric(s, errors="coerce")


def build(folder, output, date_col=None, split=True, recursive=False):
    files = collect_files(folder, recursive, output)
    if not files:
        print(f"在「{folder}」找不到 Excel / CSV 檔案。")
        return False
    print(f"找到 {len(files)} 個檔案，開始彙整...\n")

    this_year = dt.date.today().year
    frames, log = [], []
    for path in files:
        fname = os.path.relpath(path, folder)
        try:
            sheets = read_file(path)
        except ImportError:
            print(f"  ✗ {fname}：讀 .xls 需要 xlrd，請執行 pip install xlrd")
            log.append([fname, "", 0, "讀取失敗：缺少 xlrd"])
            continue
        except Exception as e:
            print(f"  ✗ {fname}：讀取失敗（{e}）")
            log.append([fname, "", 0, f"讀取失敗：{e}"])
            continue
        file_month = month_from_text(os.path.basename(path), this_year)
        file_year = int(file_month[:4]) if file_month else (year_from_text(os.path.basename(path)) or this_year)
        for sheet, df in sheets:
            if df.empty:
                continue
            dcol = pick_date_column(df, date_col)
            sheet_month = month_from_text(sheet, file_year) if sheet else None
            if dcol:
                dates = pd.Series([parse_date(v) for v in df[dcol]], index=df.index, dtype=object)
                months = pd.Series([d.strftime("%Y-%m") if d is not None and not pd.isna(d) else None for d in dates], index=df.index, dtype=object)
                fallback = sheet_month or file_month
                if fallback:
                    months = months.fillna(fallback)
                df[dcol] = [d if d is not None and not pd.isna(d) else o for d, o in zip(dates, df[dcol])]
                how = f"日期欄「{dcol}」"
                if dcol != "日期" and "日期" not in df.columns:  # 不同檔案的日期欄統一成「日期」
                    df = df.rename(columns={dcol: "日期"})
                sort_key = dates
            else:
                m = sheet_month or file_month
                months = pd.Series([m] * len(df), index=df.index)
                how = f"工作表名稱「{sheet}」" if sheet_month else (f"檔名" if file_month else "無法判斷")
                sort_key = pd.Series([None] * len(df), index=df.index)
            df.insert(0, "月份", months.fillna(UNKNOWN_MONTH))
            df.insert(1, "來源檔案", fname)
            df.insert(2, "工作表", sheet)
            df["_排序日期"] = pd.to_datetime(sort_key, errors="coerce")
            df["_原始順序"] = range(len(df))
            df["_檔案順序"] = len(frames)
            frames.append(df)
            got = ", ".join(sorted(set(df["月份"])))
            label = f"{fname}" + (f" [{sheet}]" if sheet else "")
            print(f"  ✓ {label}：{len(df)} 筆，月份依{how} → {got}")
            log.append([fname, sheet, len(df), f"依{how}判斷：{got}"])

    if not frames:
        print("\n沒有讀到任何資料。")
        return False

    # 合併：欄位依出現順序排列
    all_cols = []
    for f in frames:
        for c in f.columns:
            if c not in all_cols:
                all_cols.append(c)
    merged = pd.concat(frames, ignore_index=True, sort=False)[all_cols]
    merged["_未知"] = merged["月份"] == UNKNOWN_MONTH
    merged = merged.sort_values(["_未知", "月份", "_排序日期", "_檔案順序", "_原始順序"], na_position="last", kind="stable")
    merged = merged.drop(columns=["_未知", "_排序日期", "_原始順序", "_檔案順序"]).reset_index(drop=True)

    # 數字欄轉數字
    meta = {"月份", "來源檔案", "工作表"}
    num_cols = []
    for c in merged.columns:
        if c in meta:
            continue
        col = merged[c]
        nonnull = col.dropna()
        nonnull = nonnull[nonnull.astype(str).str.strip() != ""]
        if nonnull.empty or any(isinstance(v, (dt.datetime, dt.date, pd.Timestamp)) for v in nonnull.head(50)):
            continue
        conv = to_number(col)
        if conv.notna().sum() >= len(nonnull) * 0.9:
            merged[c] = conv
            if not any(k.lower() in str(c).lower() for k in NO_SUM_KEYWORDS):
                num_cols.append(c)

    # 月份彙總
    g = merged.groupby("月份", sort=False)
    summary = pd.DataFrame({"筆數": g.size()})
    for c in num_cols:
        summary[f"{c} 合計"] = g[c].sum(min_count=1)
    summary = summary.reset_index()
    total = {"月份": "總計", "筆數": summary["筆數"].sum()}
    for c in num_cols:
        total[f"{c} 合計"] = summary[f"{c} 合計"].sum(min_count=1)
    summary = pd.concat([summary, pd.DataFrame([total])], ignore_index=True)

    log_df = pd.DataFrame(log, columns=["檔案", "工作表", "筆數", "月份判斷"])
    write_output(output, merged, summary, log_df, split)

    print(f"\n完成！共 {len(merged)} 筆、{merged['月份'].nunique()} 個月份")
    print(f"輸出檔案：{os.path.abspath(output)}")
    if (merged["月份"] == UNKNOWN_MONTH).any():
        n = (merged["月份"] == UNKNOWN_MONTH).sum()
        print(f"注意：有 {n} 筆判斷不出月份，放在最後（月份欄＝{UNKNOWN_MONTH}），可用 --date-col 指定日期欄。")
    return True


def write_output(path, merged, summary, log_df, split):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    def safe_sheet(name):
        return re.sub(r"[\[\]:*?/\\]", "_", str(name))[:31]

    with pd.ExcelWriter(path, engine="openpyxl", datetime_format="yyyy-mm-dd", date_format="yyyy-mm-dd") as xw:
        merged.to_excel(xw, sheet_name="總表", index=False)
        summary.to_excel(xw, sheet_name="月份彙總", index=False)
        if split:
            for month, part in merged.groupby("月份", sort=False):
                part.drop(columns=["月份"]).to_excel(xw, sheet_name=safe_sheet(month), index=False)
        log_df.to_excel(xw, sheet_name="檔案清單", index=False)

        head_fill = PatternFill("solid", fgColor="1F4E78")
        head_font = Font(bold=True, color="FFFFFF")
        for ws in xw.book.worksheets:
            for cell in ws[1]:
                cell.fill, cell.font = head_fill, head_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.freeze_panes = "A2"
            if ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
            for idx, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)), 1):
                width = 6
                for c in col:
                    if c.value is None:
                        continue
                    if isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
                        if c.row > 1:
                            c.number_format = "#,##0" if float(c.value).is_integer() else "#,##0.00"
                        txt = f"{c.value:,}"
                    elif isinstance(c.value, (dt.datetime, dt.date)):
                        txt = "2026-01-01"
                    else:
                        txt = str(c.value)
                    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in txt)
                    width = max(width, min(w + 2, 50))
                ws.column_dimensions[get_column_letter(idx)].width = width
        ws = xw.book["月份彙總"]
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)


def main():
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    ap = argparse.ArgumentParser(description="把資料夾裡的 Excel / CSV 彙整成一份依月份排列的總表")
    ap.add_argument("folder", nargs="?", default=here, help="放 Excel 檔的資料夾（預設：程式所在資料夾）")
    ap.add_argument("-o", "--output", help=f"輸出檔名（預設：資料夾裡的 {DEFAULT_OUTPUT}）")
    ap.add_argument("--date-col", help="指定日期欄名稱（自動判斷不準時使用）")
    ap.add_argument("--no-split", action="store_true", help="不要產生每月分頁")
    ap.add_argument("-r", "--recursive", action="store_true", help="包含子資料夾")
    a = ap.parse_args()

    folder = os.path.abspath(a.folder)
    if not os.path.isdir(folder):
        print(f"找不到資料夾：{folder}")
        sys.exit(1)
    output = a.output or os.path.join(folder, DEFAULT_OUTPUT)
    try:
        ok = build(folder, output, a.date_col, not a.no_split, a.recursive)
    except PermissionError:
        print(f"\n無法寫入 {output}，可能檔案正開在 Excel 裡，請關閉後再執行。")
        ok = False
    if len(sys.argv) == 1 and os.name == "nt":  # 雙擊執行時，讓視窗停住
        input("\n按 Enter 結束...")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

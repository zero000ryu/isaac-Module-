# Excel 彙整工具（依月份排列）

把一個資料夾裡所有 Excel（.xlsx / .xlsm / .xls）和 CSV 合併成一份 `彙整總表.xlsx`，依月份由舊到新排列。

## 安裝（第一次）
1. 安裝 Python 3（https://www.python.org/ ，安裝時勾選「Add Python to PATH」）
2. 開命令提示字元執行：`pip install pandas openpyxl xlrd`

## 使用
- **最簡單**：把 `merge_excel_by_month.py` 和 `run.bat` 放進存放 Excel 的資料夾，雙擊 `run.bat`
- **指令列**：
  ```
  python merge_excel_by_month.py D:\報表資料夾
  python merge_excel_by_month.py D:\報表資料夾 -o D:\總表.xlsx
  python merge_excel_by_month.py D:\報表資料夾 --date-col 出貨日期   # 指定日期欄
  python merge_excel_by_month.py D:\報表資料夾 --no-split           # 不要每月分頁
  python merge_excel_by_month.py D:\報表資料夾 -r                   # 含子資料夾
  ```

## 輸出內容
| 分頁 | 內容 |
|---|---|
| 總表 | 所有資料合併，依月份→日期排序，加上「月份／來源檔案／工作表」欄 |
| 月份彙總 | 每月筆數、數字欄加總，最後一列是總計 |
| 2026-01、2026-02… | 每個月一頁 |
| 檔案清單 | 每個檔案讀到幾筆、月份是怎麼判斷的 |

## 月份怎麼判斷
1. 資料中的日期欄（西元、民國都可以：`2026/3/5`、`115/03/05`、`1150305`、`115年3月5日`）
2. 工作表名稱（`2026-03`、`115年3月`、`3月`）
3. 檔名（`2026_03出貨.xlsx`、`11503報表.xlsx`）

判斷不出來的放在最後，月份欄顯示「未知月份」。

## 自訂
程式最上方的 `COLUMN_ALIASES` 可以設定欄名對應，例如「產品名稱」「品項」都統一成「品名」。

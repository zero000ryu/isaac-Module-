@echo off
chcp 65001 >nul
cd /d "%~dp0"
python merge_excel_by_month.py %*
pause

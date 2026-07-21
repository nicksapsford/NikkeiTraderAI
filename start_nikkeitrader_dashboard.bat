@echo off
title NikkeiTrader A.I. Dashboard - Port 5008
cd /d C:\Users\abc\Desktop\NikkeiTraderAI
start /min "NikkeiTrader A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_nikkei.py
timeout /t 5 /nobreak >nul
start http://localhost:5008

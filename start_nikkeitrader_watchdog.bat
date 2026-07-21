@echo off
title NikkeiTrader A.I. Engine (Watchdog) - Port 5008
cd /d C:\Users\abc\Desktop\NikkeiTraderAI
start /min "NikkeiTrader A.I. Engine" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_nikkei.py

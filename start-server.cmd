@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PTCGO Local Server
python server.py
pause

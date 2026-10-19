@echo off
title Short Borrow Monitor - local test
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% -c "import yfinance" 2>nul || (
  echo First run: installing yfinance...
  %PY% -m pip install yfinance
)
start "" http://localhost:8000
%PY% app.py
pause

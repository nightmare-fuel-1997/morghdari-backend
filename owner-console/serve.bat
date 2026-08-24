@echo off
REM =============================================================================
REM serve.bat - launch the owner console locally. OWNER-ONLY, never shipped.
REM
REM REM: فارسی - راه‌اندازی کنسول مالک روی http://localhost:8788 ؛ این پوشه هرگز
REM توزیع نمی‌شود و خارج از قراردادهای API است.
REM
REM Uses Python's stdlib static server (no dependencies). If "python" is not
REM found, try the 3.12 launcher (this machine's quirk - CONSTRAINTS C-01).
REM =============================================================================

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    start "" http://localhost:8788
    python -m http.server 8788
    goto :eof
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" http://localhost:8788
    py -V:3.12 -m http.server 8788
    goto :eof
)

echo Python not found - install Python 3.12 first.
echo REM: پایتون پیدا نشد؛ ابتدا Python 3.12 را نصب کنید.
exit /b 1

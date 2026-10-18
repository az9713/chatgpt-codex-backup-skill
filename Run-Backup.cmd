@echo off
python "%~dp0scripts\backup.py" backup --allow-live
if errorlevel 3 goto failed
if errorlevel 2 goto saved
if errorlevel 1 goto failed
:saved
echo.
echo Backup saved in D:\backups\chatgpt-codex. See the receipt for details.
pause
exit /b 0
:failed
echo Backup failed. Review the message above.
pause
exit /b 1

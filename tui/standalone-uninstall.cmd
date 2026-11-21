@echo off
rem Standalone TUI uninstaller. ASCII only -- see crannia.cmd for why.
rem
rem Removes the crannia command from PATH and the program directory.
rem User data is untouched: it lives in %APPDATA%\Crannia.
rem
rem The directory cannot delete itself while this script runs from inside it,
rem so the actual removal is handed to a tiny batch in %TEMP% after we exit.

setlocal

set "DEST=%LOCALAPPDATA%\Programs\CranniaTUI"
set "SHIM=%LOCALAPPDATA%\Microsoft\WindowsApps\crannia.cmd"

rem Only remove the command if it points at this installation. The same
rem directory is shared with the GUI installer's shim and with other apps.
if exist "%SHIM%" (
    findstr /x /c:"@rem Owner: %DEST%" "%SHIM%" >nul 2>&1
    if errorlevel 1 (
        echo [crannia] left the crannia command alone: it belongs to another install.
    ) else (
        del "%SHIM%" >nul 2>&1
        echo [crannia] removed the crannia command.
    )
)
echo [crannia] your data stays in %APPDATA%\Crannia

if not exist "%DEST%" (
    echo [crannia] program directory not found: %DEST%
    exit /b 0
)

echo [crannia] removing %DEST%
> "%TEMP%\crannia-tui-rm.cmd" (
    echo @echo off
    echo cd /d "%TEMP%"
    echo ping -n 3 127.0.0.1 ^>nul
    echo rd /s /q "%DEST%"
    echo del "%%~f0"
)
start "" /min cmd /c "%TEMP%\crannia-tui-rm.cmd"

echo [crannia] done.
exit /b 0

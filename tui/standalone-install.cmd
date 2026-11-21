@echo off
rem Standalone TUI installer. ASCII only -- see crannia.cmd for why.
rem
rem Run this from the extracted zip root. It copies the package to
rem %LOCALAPPDATA%\Programs\CranniaTUI and places the `crannia` command in
rem %LOCALAPPDATA%\Microsoft\WindowsApps -- a directory already on every
rem user's PATH, so nothing touches the registry.
rem
rem Data is never here: conversations, settings and model credentials live in
rem %APPDATA%\Crannia (shared with the GUI).

setlocal

set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\Programs\CranniaTUI"
set "SHIM=%LOCALAPPDATA%\Microsoft\WindowsApps"

if not exist "%SRC%runtime\python\python.exe" (
    echo [crannia] this does not look like the extracted package:
    echo           %SRC%
    echo [crannia] extract the zip first, then run install.cmd from inside it.
    exit /b 1
)

if /i "%SRC%"=="%DEST%\" (
    echo [crannia] already installed at %DEST%
) else (
    echo [crannia] installing to %DEST%
    robocopy "%SRC%." "%DEST%" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
    rem robocopy: 0-7 are success, 8+ is a real failure.
    if errorlevel 8 (
        echo [crannia] copy failed: %DEST%
        exit /b 1
    )
)

if not exist "%SHIM%" mkdir "%SHIM%" >nul 2>&1

rem Write a forwarder, not a copy of the launcher: the launcher finds its
rem runtime and code next to itself, and this directory has neither.
rem The first line records the owner so uninstallers do not delete each
rem other's command.
(
    echo @rem Owner: %DEST%
    echo @echo off
    echo call "%DEST%\crannia.cmd" %%*
    echo exit /b %%ERRORLEVEL%%
) > "%SHIM%\crannia.cmd"
rem Check the file, not ERRORLEVEL: robocopy returns 1 on success, and the
rem redirection above does not reset it -- `if errorlevel 1` would report a
rem failure after a perfectly good install.
if not exist "%SHIM%\crannia.cmd" (
    echo [crannia] could not put the crannia command on PATH.
    echo [crannia] run %DEST%\crannia.cmd directly instead.
    exit /b 1
)

echo [crannia] installed.
echo [crannia] open a new console and type: crannia
echo [crannia] uninstall: %DEST%\uninstall.cmd
exit /b 0

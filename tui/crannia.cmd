@echo off
rem crannia-tui launcher: type `crannia` anywhere to start.
rem
rem NOTE: this file is ASCII-only on purpose. cmd.exe reads .cmd files using the
rem system code page (GBK on a Chinese Windows), so UTF-8 Chinese in here turns
rem into garbage and breaks the rem lines themselves. Keep it ASCII.
rem
rem Why the shell dance below: the full-screen UI draws with ANSI cursor
rem addressing. cmd's legacy console does not interpret escapes, so it would
rem show a screenful of `?[2m`. Windows Terminal sets WT_SESSION; if that is
rem missing we relaunch ourselves inside it, so nobody has to remember which
rem window to use.
rem
rem `--once` is the exception: that path prints to a pipe or a log, needs no
rem UI, and must not pop a new window.
rem
rem Three layouts share this one file:
rem   A) next to Crannia.exe (TUI ships inside the GUI installer)
rem        <install>\crannia.cmd
rem        <install>\resources\app\tui\crannia_tui
rem        <install>\resources\primary-runtime\python\python.exe
rem   B) the dev tree: this file lives in <repo>\tui\
rem        <repo>\tui\crannia_tui
rem        <repo>\tui\.venv\Scripts\python.exe
rem   C) the standalone package (no GUI, no system Python)
rem        <pkg>\crannia.cmd + <pkg>\runtime\python\python.exe
rem        <pkg>\releases\<current>\crannia_tui
rem Layout C is exactly what `crannia update --from` produces, so the launcher
rem and the updater agree on where code lives.

setlocal

set "HERE=%~dp0"

if "%~1"=="doctor" goto run
if "%~1"=="config" goto run
if "%~1"=="profile" goto run
if "%~1"=="channel" goto run
if "%~1"=="setup" goto run
if "%~1"=="update" goto run
if "%~1"=="--version" goto run
if "%~1"=="--help" goto run
if "%~1"=="-h" goto run

echo %* | findstr /c:"--once" >nul && goto run

if defined WT_SESSION goto run

where wt.exe >nul 2>&1
if errorlevel 1 (
    echo [crannia] this console does not support ANSI; the UI would be garbled.
    echo [crannia] install Windows Terminal and retry, or set CRANNIA_TUI_FORCE=1
    if not defined CRANNIA_TUI_FORCE exit /b 1
    goto run
)

rem Relaunch inside Windows Terminal. `-d` keeps the current directory --
rem otherwise the new window starts in the home directory and "which repo am
rem I in" silently changes.
rem
rem `call` is not decoration: without it, a path containing a space is split
rem by the time the inner cmd sees it. (The `cmd /c ""%~f0" ..."` form works
rem when invoked directly, but not through wt.exe's argument parsing.)
wt.exe -d "%CD%" cmd /c call "%~f0" %*
exit /b 0

:run
rem Window title: without this the tab shows the full path of cmd.exe, and a
rem row of them is unreadable. The app sets it too (see app._set_console_title),
rem but only after Python starts -- this covers the startup second.
title Crannia

rem The directory we were called from is the session's working directory, so
rem remember it before pushd changes it -- the UI shows it and session lookup
rem is keyed by it.
set "CRANNIA_LAUNCH_CWD=%CD%"

rem Where the update chain lives (`releases\` + `current`). This is the
rem directory this file sits in, not the code root we pushd into below:
rem in the standalone layout the running code is under releases\<current>,
rem and updates must land next to this file or the launcher never sees them.
set "CRANNIA_LAUNCH_ROOT=%HERE%"

rem Layout A0: the shim installed on PATH lives in
rem %LOCALAPPDATA%\Microsoft\WindowsApps and points back here:
rem   <install>\bin\crannia.cmd -> ..\resources\app\tui + ..\resources\primary-runtime
if exist "%HERE%..\resources\app\tui\crannia_tui" (
    set "PY=%HERE%..\resources\primary-runtime\python\python.exe"
    set "RUN=%HERE%..\resources\app\tui"
    goto have_layout
)

rem A) inside the GUI installation: this file sits next to Crannia.exe.
if exist "%HERE%resources\app\tui\crannia_tui" (
    set "PY=%HERE%resources\primary-runtime\python\python.exe"
    set "RUN=%HERE%resources\app\tui"
    goto have_layout
)

rem B) dev tree, or C) standalone package. Dev prefers its own venv; the
rem standalone package has runtime\ next to this file.
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "%HERE%runtime\python\python.exe" (
    set "PY=%HERE%runtime\python\python.exe"
    rem Standalone is an installation too, but its code root is
    rem releases\<version>, not <install>\resources\app -- the path-based
    rem check in core.paths cannot tell. Say it explicitly, otherwise user
    rem data lands inside releases\<version>\data instead of %APPDATA%.
    set "CRANNIA_INSTALLED=1"
)
if not defined PY (
    echo [crannia] no Python runtime found next to %~f0
    echo [crannia] expected .venv\Scripts\python.exe or runtime\python\python.exe
    exit /b 1
)

rem Release pointer: `current` names a directory under `releases\`. If it is
rem there, run that copy; otherwise run this tree (dev layout / first start).
set "RUN=%HERE%"
if exist "%HERE%current" set /p CUR=<"%HERE%current"
if defined CUR if exist "%HERE%releases\%CUR%\crannia_tui" set "RUN=%HERE%releases\%CUR%"

:have_layout
rem `-m` needs the package importable from the current directory. pushd to the
rem code root, otherwise running `crannia` from any other folder fails with
rem "No module named crannia_tui".
pushd "%RUN%"
"%PY%" -m crannia_tui %*
rem popd clears ERRORLEVEL on success, so take the code before it. The
rem non-interactive commands (doctor / --version / --once) exist to be
rem checked by scripts; losing their exit code would make them useless.
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

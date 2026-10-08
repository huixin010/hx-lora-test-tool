@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title HX-YanDanLu V0.4.3

rem ====================================================================
rem  IMPORTANT - do not "prettify" this file:
rem    * line endings MUST be CRLF. A .bat with LF-only endings breaks
rem      on Windows: variables stop expanding, text turns into mojibake,
rem      and it prints bogus errors such as "this Python has no Pillow".
rem    * keep it ASCII-only. No chinese here, so it renders the same on
rem      any codepage.
rem  If you edit this, verify with:  file start.bat  (should say CRLF)
rem ====================================================================

set "PY="
set "PYFALL="
set "COMFYROOT="

echo [HX-YanDanLu] looking for a Python ...

rem ---- 1) manual override: put the full path of python.exe into python_path.txt
if exist "%~dp0python_path.txt" set /p PY=<"%~dp0python_path.txt"
if defined PY if not exist "!PY!" set "PY="

rem ---- 2) derive from config.json: comfy_root\python_embeded\python.exe
if not defined PY call :from_config

rem ---- 3) V0.4.3: ask PowerShell where ComfyUI is.
rem         This runs BEFORE the directory-name guessing below, and it is
rem         the step that actually makes "unzip and double-click" work.
rem         Reason: on many machines the only Python on the box is the one
rem         inside ComfyUI, so the old chain had a deadlock -- it needed a
rem         Python to start the server, and needed the server to find
rem         ComfyUI. PowerShell ships with Windows and needs no Python,
rem         so it can break the loop. It reads the path straight out of a
rem         running ComfyUI process, which is exact regardless of how
rem         deeply or under what name ComfyUI is installed.
rem         It also hands us comfy_root, which we write into config.json
rem         below so the server does not have to scan all over again.
if not defined PY call :from_comfy

rem ---- 4) the usual portable-ComfyUI folder names, on every drive
if not defined PY call :common

rem ---- 5) one level down on every drive (windows / system folders skipped)
if not defined PY call :scan1

rem ---- 6) one more level, but only inside folders named *comfy* or AI
if not defined PY call :scan2

rem ---- 7) python launcher, then whatever is on PATH.
rem         Resolve them to a real file and probe it: on plenty of machines
rem         "py" exists but has no Python registered behind it, and running
rem         it just prints "Can't find a default Python."
if not defined PY (
  for /f "usebackq delims=" %%i in (`where py 2^>nul`) do if not defined PY call :probe "%%i"
)
if not defined PY (
  for /f "usebackq delims=" %%i in (`where python 2^>nul`) do if not defined PY call :probe "%%i"
)

rem ---- 8) no Pillow anywhere? any python beats none: the server relaunches
rem         itself with ComfyUI's own python once it finds the ComfyUI folder.
if not defined PY if defined PYFALL set "PY=!PYFALL!"

rem ---- 9) V0.4.3: persist the ComfyUI root we detected, so the server
rem         starts already knowing it (no second scan, no waiting).
rem         Only writes when config.json has no comfy_root yet, so it can
rem         never overwrite a path the user chose on purpose.
if defined COMFYROOT call :save_comfy_root

if not defined PY (
  echo.
  echo   [ERROR] No Python found on this computer.
  echo.
  echo   This tool needs a Python. If you have ComfyUI installed, it already
  echo   ships one - just tell the tool where ComfyUI is:
  echo.
  echo     Option A - copy config.example.json to config.json, open it and
  echo                set "comfy_root" to your ComfyUI folder, for example:
  echo                    "comfy_root": "D:\\ComfyUI_windows_portable"
  echo     Option B - create a file named python_path.txt in this folder
  echo                and put one line in it, the full path of python.exe
  echo                    D:\\ComfyUI_windows_portable\\python_embeded\\python.exe
  echo.
  echo   Then run this file again. Full guide: README.md
  echo.
  call :hint
  pause
  exit /b 1
)

"!PY!" -c "import PIL" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [WARN] this Python has no Pillow installed:
  echo       !PY!
  echo   Starting anyway. The tool looks for your ComfyUI folder on its own
  echo   and, if that folder ships a python with Pillow, restarts itself
  echo   with it - you do not have to do anything.
  echo.
)

echo [HX-lora] using Python: !PY!
echo.

rem switch the console to UTF-8 so the server's chinese messages are
rem readable instead of mojibake (server.py does the same for itself)
"%SystemRoot%\System32\chcp.com" 65001 >nul 2>nul

"!PY!" server.py

echo.
echo [HX-lora] server stopped.
pause
exit /b 0


:from_config
rem  delims=%%09 (a literal TAB) on purpose. A bare delims= does NOT mean
rem  "do not split" -- it means "use the default set", and the default set
rem  contains the SPACE. ComfyUI folders are routinely installed under a
rem  path with a space in it (for example D:\AI Tools\ComfyUI), and the
rem  default set would hand us only "D:\AI" and silently start the wrong
rem  thing. A TAB never appears in a Windows path, so it is the safe
rem  sentinel. No non-ASCII in here, not even in a comment -- see the
rem  header of this file for why that matters.
for /f "usebackq delims=	" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$c=Get-Content -Raw -Encoding UTF8 '%~dp0config.json'|ConvertFrom-Json;$r=$c.comfy_root;if($r){$p=Join-Path $r 'python_embeded\python.exe';if(Test-Path $p){Write-Output $p}}}catch{}"`) do set "PY=%%i"
if not defined PY if defined COMFYROOT call :probe "!COMFYROOT!\python_embeded\python.exe"
exit /b 0


rem --------------------------------------------------------------------
rem  V0.4.3 : ask find_comfy.ps1 where ComfyUI is.
rem  The script prints "ROOT=..." and possibly "PY=...". We read both.
rem  -File is used rather than -Command so we do not have to escape a whole
rem  script into a one-liner. ExecutionPolicy Bypass is per-process and
rem  does not touch the machine's settings.
rem  Every failure mode here is non-fatal on purpose: no PowerShell, the
rem  script missing, policy blocking it -- all of them just leave PY
rem  empty and the older lookup chain below takes over.
rem
rem  V0.4.3 fix -- delims== , and compare against ROOT / PY without "=".
rem  A bare delims= does NOT mean "no splitting": it means "use the
rem  default delimiter set", which is SPACE TAB COMMA SEMICOLON -- there
rem  is no "=" in it. So with delims= the loop variable %%a came out as
rem  the whole line "ROOT=G:\...", and the test
rem       if /i "%%a"=="ROOT="
rem  could never be true. The whole probe therefore never contributed
rem  anything; it looked like a detection or encoding problem the whole
rem  time. With delims== the '=' itself is the delimiter, so %%a is
rem  exactly ROOT and the comparison must drop the '='.
rem  Measured after the fix, on this machine:
rem      ROOT=G:\HeiHe\comfyuizhb20260122\ComfyUI_windows_portable-G312-0122
rem      PY=...\python_embeded\python.exe
rem  A path containing chinese characters and spaces round-trips fine
rem  through this pipe -- that was verified separately, so no encoding
rem  workaround is needed here.
rem --------------------------------------------------------------------
:from_comfy
if not exist "%~dp0find_comfy.ps1" exit /b 0
for /f "usebackq tokens=1,* delims==" %%a in (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0find_comfy.ps1" 2^>nul`) do (
  if /i "%%a"=="ROOT" set "COMFYROOT=%%b"
  if /i "%%a"=="PY" if not defined PY call :probe "%%b"
)
exit /b 0


rem --------------------------------------------------------------------
rem  V0.4.3 : write the detected ComfyUI root into config.json.
rem  Runs through python rather than PowerShell on purpose -- python is
rem  guaranteed to exist at this point (we only get here when PY is set),
rem  and it edits JSON correctly instead of doing text surgery on it.
rem  Skips when comfy_root is already filled, so a path the user set by
rem  hand is never overwritten.
rem --------------------------------------------------------------------
:save_comfy_root
if not exist "%~dp0config.json" exit /b 0
rem  Delegate to server.py instead of inlining python here. Reasons:
rem   * a multi-statement -c one-liner inside a .bat needs exec() and
rem     line continuations, which is exactly the kind of thing that
rem     breaks silently on someone else's machine;
rem   * server.py is the only place that knows the config.json shape, so
rem     writing it from two places guarantees they drift apart.
rem  The subcommand is a no-op when comfy_root is already set, so a path
rem  the user chose by hand is never overwritten.
"!PY!" "%~dp0server.py" --set-comfy-root "!COMFYROOT!" >nul 2>nul
exit /b 0


:common
for %%d in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
  if not defined PY if exist "%%d:\" (
    call :probedir "%%d:\ComfyUI"
    if not defined PY call :probedir "%%d:\comfyui"
    if not defined PY call :probedir "%%d:\ComfyUI_windows_portable"
    if not defined PY call :probedir "%%d:\ComfyUI-aki"
    if not defined PY call :probedir "%%d:\ComfyUI-aki-v1.3"
    if not defined PY call :probedir "%%d:\ComfyUI-aki-v2"
    if not defined PY call :probedir "%%d:\AI\ComfyUI"
    if not defined PY call :probedir "%%d:\AI\comfyui"
    if not defined PY call :probedir "%%d:\"
  )
)
exit /b 0


:scan1
rem  every folder directly on a drive root, minus the noisy system ones
for %%d in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
  if not defined PY if exist "%%d:\" (
    for /f "usebackq delims=" %%s in (`dir /b /ad "%%d:\" 2^>nul`) do (
      if not defined PY (
        set "SKIP=0"
        for %%x in ("Windows" "Program Files" "Program Files (x86)" "ProgramData" "Users" "$Recycle.Bin" "System Volume Information" "Recovery" "PerfLogs" "Documents and Settings" "MSOCache" "WinSxS" "Intel" "AMD" "NVIDIA" "Python") do if /i "%%s"=="%%~x" set "SKIP=1"
        if "!SKIP!"=="0" call :probedir "%%d:\%%s"
      )
    )
  )
)
exit /b 0


:scan2
rem  one level deeper, but only inside folders whose name hints at ComfyUI
for %%d in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
  if not defined PY if exist "%%d:\" (
    for /f "usebackq delims=" %%s in (`dir /b /ad "%%d:\" 2^>nul`) do (
      if not defined PY (
        set "N1=%%s"
        if /i not "!N1:comfy=!"=="!N1!" (
          call :probedir "%%d:\%%s"
          if not defined PY (
            for /f "usebackq delims=" %%t in (`dir /b /ad "%%d:\%%s" 2^>nul`) do (
              if not defined PY call :probedir "%%d:\%%s\%%t"
            )
          )
        )
        if not defined PY if /i "%%s"=="AI" (
          for /f "usebackq delims=" %%t in (`dir /b /ad "%%d:\AI" 2^>nul`) do (
            if not defined PY call :probedir "%%d:\AI\%%t"
          )
        )
      )
    )
  )
)
exit /b 0


:probedir
rem  %~1 = a folder that may be a ComfyUI root (or contain one)
if defined PY exit /b 0
if not exist "%~1\" exit /b 0
if exist "%~1\python_embeded\python.exe" call :probe "%~1\python_embeded\python.exe"
if defined PY exit /b 0
if exist "%~1\ComfyUI\python_embeded\python.exe" call :probe "%~1\ComfyUI\python_embeded\python.exe"
if defined PY exit /b 0
if exist "%~1\python\python.exe" call :probe "%~1\python\python.exe"
if defined PY exit /b 0
if exist "%~1\venv\Scripts\python.exe" call :probe "%~1\venv\Scripts\python.exe"
if defined PY exit /b 0
if exist "%~1\.venv\Scripts\python.exe" call :probe "%~1\.venv\Scripts\python.exe"
exit /b 0


:probe
rem  %~1 = candidate python.exe; keeps the first one that has Pillow,
rem        and remembers a plain one as a fallback.
rem        A command that cannot even start (the "py" launcher with nothing
rem        registered behind it) is skipped entirely - not even as fallback.
if defined PY exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 exit /b 0
"%~1" -c "import PIL" >nul 2>nul
if not errorlevel 1 (
  set "PY=%~1"
  exit /b 0
)
if not defined PYFALL set "PYFALL=%~1"
exit /b 0


:hint
rem --------------------------------------------------------------------
rem  Print the simplified-chinese help straight to the console.
rem  Uses nothing but cmd's own built-in "type", so it cannot fail for
rem  lack of powershell / .net / com - and it works on any machine.
rem  Three things matter here:
rem    * call chcp via its FULL PATH. A bare "chcp" is looked up on PATH,
rem      and if PATH is unusual the lookup fails - the codepage then stays
rem      at the system default and the UTF-8 text below shows up as
rem      mojibake. (That is exactly what happened once.)
rem    * if chcp is unavailable, skip the chinese entirely rather than
rem      printing garbage - the english error above is still readable.
rem    * help-zh.txt must be UTF-8 *with CRLF*, or "type" renders it as
rem      one long unreadable line.
rem  The path is chinese-safe: "type" is a cmd builtin, so it receives the
rem  name through cmd's internal (unicode) command line, not as an ANSI
rem  byte string.
rem --------------------------------------------------------------------
"%SystemRoot%\System32\chcp.com" 65001 >nul 2>nul
if errorlevel 1 exit /b 0
echo.
type "%~dp0help-zh.txt"
echo.
exit /b 0

# Locate the local ComfyUI and print its root to stdout.
#
# Why a separate .ps1: start.bat needs a Python before server.py can run,
# but on many machines the ONLY Python installed is the one bundled inside
# ComfyUI. That deadlocks startup -- no python, no server, and no server
# means nothing can auto-detect ComfyUI either.
#
# This script breaks the deadlock using PowerShell, which every Windows
# box already has and which needs no Python. It asks "is a python.exe
# right now running ComfyUI's main.py?" If yes, the exact path is written
# in the command line -- no directory-name guessing, no scan-depth
# guessing, and it works no matter how deeply ComfyUI is nested.
#
# Output, one per line (the bat reads them one by one):
#   ROOT=<ComfyUI root>       only when found
#   PY=<full path to python>  only when that folder ships a python
# Nothing is printed when nothing is found; the bat then falls back to
# its own older lookup chain.
#
# THIS FILE MUST BE ASCII-ONLY, and must be saved as UTF-8 *with BOM*.
# Both are load-bearing:
#   * PowerShell 5.1 (still the default on Win10/Win11) reads a .ps1
#     without a BOM as ANSI, so UTF-8 Chinese comments turn into mojibake
#     and -- worse -- stray bytes can terminate a string literal early,
#     producing a ParserError that makes the whole script silently do
#     nothing. The bat would then look like "detection never worked".
#   * The same reasoning applies to non-ASCII bytes anywhere in here.
# The Chinese explanation of this logic lives in server.py's
# detect_comfy_from_process() instead, where UTF-8 is safe.
$ErrorActionPreference = 'SilentlyContinue'

function Test-ComfyDir([string]$p) {
    if (-not $p) { return $false }
    # Source install: <root>\main.py plus a models\ folder
    if ((Test-Path (Join-Path $p 'main.py')) -and (Test-Path (Join-Path $p 'models'))) {
        return $true
    }
    # Portable install: <root>\ComfyUI\main.py, or <root>\ComfyUI\ with python
    $inner = Join-Path $p 'ComfyUI'
    if (Test-Path $inner) {
        if (Test-Path (Join-Path $inner 'main.py')) { return $true }
        if (Test-Path (Join-Path $inner 'models')) { return $true }
    }
    return $false
}

# ---------------------------------------------------------------------------
# V0.4.3: Resolve a ComfyUI CODE folder to the ROOT that owns it.
#
# Why this exists. The user reported "unzip it and it still asks me to type
# the ComfyUI path in by hand", i.e. the headline V0.4.3 promise was broken.
# Root cause, measured on this machine:
#
#     G:\...\ComfyUI_windows_portable-G312-0122\          <- ROOT
#         python_embeded\python.exe                        <- the interpreter
#         run_nvidia_gpu.bat                              <- the launcher
#         ComfyUI\main.py, ComfyUI\models                 <- the code
#
# The old code reported the INNER folder (the one with main.py). Every
# later python lookup then searched under a folder that has no interpreter
# at all -- python lives one level UP. Hence: no python found, and the
# tool fell back to asking the user for a path.
#
# The trap: the fix cannot be "climb one level when the name is ComfyUI",
# because the outer folder here is NOT called ComfyUI -- it carries a build
# id and a date (ComfyUI_windows_portable-G312-0122). Any name-based test
# misses it. So climb on STRUCTURE: whoever owns python_embeded\ (or a
# venv, or a run_*.bat launcher) is the root, whatever it is named.
# ---------------------------------------------------------------------------
function Resolve-Root([string]$p) {
    if (-not $p) { return '' }
    # Already a root: it owns an interpreter, or it is a plain source tree.
    if ($p -match '(?i)\\(python_embeded|python|venv|\.venv)$') {
        return (Split-Path $p -Parent)
    }
    # $p is a ComfyUI CODE folder (has main.py + models). If its parent
    # owns an interpreter, that parent is the root.
    $par = Split-Path $p -Parent
    if ($par) {
        foreach ($rel in @('python_embeded\python.exe', 'python\python.exe',
                           'venv\Scripts\python.exe', '.venv\Scripts\python.exe')) {
            if (Test-Path (Join-Path $par $rel)) { return $par }
        }
        # A run_*.bat launcher is the other reliable marker of a portable
        # root -- some builds ship it without shipping python_embeded.
        if (Get-ChildItem -Path $par -Filter 'run_*.bat' -ErrorAction SilentlyContinue) {
            return $par
        }
    }
    return $p
}

$root = ''

# ---- 1) A running ComfyUI process: the most trustworthy clue ----------
# Only python processes whose command line mentions main.py qualify.
# Without that restriction we would happily grab some unrelated
# interpreter (there are always several on a dev machine).
#
# V0.4.3 : the anchor is the interpreter's OWN executable path, not the
# main.py token in the command line.
#
# Reason, measured on this machine: ComfyUI's own run_nvidia_gpu.bat
# starts it like this
#     .\python_embeded\python.exe  -s ComfyUI\main.py --windows-...
# Both paths are RELATIVE -- to the .bat's own directory. An earlier
# version of this script looked for an absolute main.py token, found
# none, and reported "not found" -- while ComfyUI was running right
# there. So the single most reliable signal was dead on the machines
# it was written for.
#
# The executable path has no such problem: Windows resolves it for us,
# and for every ComfyUI install the interpreter sits at
#     <root>\python_embeded\python.exe      (portable)
#     <root>\venv\Scripts\python.exe        (git / manual venv)
# so walking a couple of levels up from the .exe lands on the portable
# root -- which is exactly the value server.py's detect_comfy_root()
# returns, and exactly what the bat writes into config.json.
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
            $_.CommandLine -and $_.CommandLine -match '(?i)main\.py'
        }

foreach ($pr in $procs) {
    if ($root) { break }

    # Absolute main.py token, when there is one (source installs launched
    # with a full path). Cheap to check, so try it before the exe walk.
    $dirs = @()
    foreach ($t in ($pr.CommandLine -split '\s+')) {
        $p = $t.Trim('"').Trim("'")
        if ($p -notmatch '(?i)main\.py$') { continue }
        if ($p -notmatch '^[A-Za-z]:') { continue }      # relative, skip
        $d = Split-Path $p -Parent
        if ($d -and (Test-Path $d)) { $dirs += $d }
    }

    # The interpreter itself: always absolute once Windows resolved it.
    # MainModule is the reliable way to ask; fall back to ExecutablePath.
    $exe = ''
    try {
        $gp = Get-Process -Id $pr.ProcessId -ErrorAction SilentlyContinue
        if ($gp -and $gp.MainModule) { $exe = $gp.MainModule.FileName }
    } catch { }
    if (-not $exe) { $exe = "$($pr.ExecutablePath)" }
    if ($exe) {
        $d = Split-Path $exe -Parent
        # <root>\python_embeded, <root>\venv\Scripts, <root>\venv, <root>
        for ($i = 0; $i -lt 4; $i++) {
            if (-not $d) { break }
            $dirs += $d
            $par = Split-Path $d -Parent
            if (-not $par -or $par -eq $d) { break }
            $d = $par
        }
    }

    foreach ($d in $dirs) {
        # V0.4.3: one place decides what a ComfyUI folder really is.
        # Measured candidate order on this machine:
        #   [0] ...\ComfyUI_windows_portable-G312-0122\python_embeded
        #   [1] ...\ComfyUI_windows_portable-G312-0122<- owns python_embeded
        #   [2] ...\comfyuizhb20260122
        # [1] is the real root but has no main.py of its own, so the old
        # Test-ComfyDir gate walked straight past it up to G:\ and found
        # nothing. Resolve-Root recognises it by structure instead, and
        # also collapses a code folder back up to the owner of the
        # interpreter -- which is what every later python lookup needs.
        $r = Resolve-Root $d
        if ($r) {
            $hasCode = (Test-ComfyDir $r) -or
                       (Test-ComfyDir (Join-Path $r 'ComfyUI')) -or
                       (Test-ComfyDir (Join-Path $r 'comfyui'))
            if ($hasCode) { $root = $r; break }
        }
    }
}

# ---- 2) No process running: sweep the usual places --------------------
if (-not $root) {
    $names = @('ComfyUI', 'comfyui', 'ComfyUI_windows_portable',
               'ComfyUI-aki', 'ComfyUI-aki-v1.3', 'ComfyUI-aki-v2',
               'ComfyUI-aki-v3')
    $seeds = @()
    foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
        if ($d.IsReady) { $seeds += $d.RootDirectory.FullName }
    }
    $home = [Environment]::GetFolderPath('UserProfile')
    if ($home) {
        $seeds += @($home, (Join-Path $home 'Desktop'),
                    (Join-Path $home 'Downloads'),
                    (Join-Path $home 'Documents'))
    }
    foreach ($s in $seeds) {
        if (-not $s) { continue }
        foreach ($n in $names) {
            $c = Join-Path $s $n
            # V0.4.3: resolve through Resolve-Root so a portable wrapper is
            # reported by its root, never by the inner code folder.
            if (Test-ComfyDir $c) { $root = Resolve-Root $c; break }
        }
        if ($root) { break }
        # One more level, for the AI\ComfyUI shape
        $ai = Join-Path $s 'AI'
        if (Test-Path $ai) {
            foreach ($n in @('ComfyUI', 'comfyui', 'ComfyUI_windows_portable')) {
                $c = Join-Path $ai $n
                if (Test-ComfyDir $c) { $root = Resolve-Root $c; break }
            }
            if ($root) { break }
        }
    }
}

# ---- 2b) Still nothing: match on STRUCTURE, not on folder name ---------
# V0.4.3 fix. The list above is an exact-name whitelist, and real
# installs rarely use those names verbatim. Measured on this machine:
#     G:\HeiHe\comfyuizhb20260122\ComfyUI_windows_portable-G312-0122
#                ^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#                a vendor folder  and a build id + date glued onto the
#                name -- so "ComfyUI_windows_portable" matches nothing.
#
# Portable roots are recognisable by structure instead: they own
# python_embeded\ (or a venv) AND contain the ComfyUI code.
#
# Depth matters. This machine nests the install THREE levels below the
# drive root (G:\ -> HeiHe -> comfyuizhb20260122 -> <root>), so a
# two-level walk sails straight past it. Walk to depth 4 with a plain
# breadth-first queue, one level at a time -- the previous version
# tried to do two levels with one nested Get-ChildItem, which both
# capped the depth and mixed the levels together.
if (-not $root) {
    $skip = '(?i)\\(windows|program files|program files \(x86\)|programdata|users|\$recycle\.bin|system volume information|recovery|perflogs|documents and settings|msocache|winsxs|intel|amd|nvidia|python|node_modules|\.git|steam|steamlibrary)(\\|$)'
    $queue = @()
    foreach ($s in $seeds) {
        if ($s -and (Test-Path $s)) { $queue += ,@($s, 0) }
    }
    $guard = 0
    while ($queue.Count -gt 0 -and $guard -lt 40000) {
        $guard++
        $item = $queue[0]
        $queue = @($queue | Select-Object -Skip 1)
        $dir = $item[0]
        $depth = [int]$item[1]
        try {
            foreach ($sub in (Get-ChildItem -Path $dir -Directory -Force -ErrorAction SilentlyContinue)) {
                $p = $sub.FullName
                if ($p -match $skip) { continue }
                # A portable root owns an interpreter...
                $ownsPy = (Test-Path (Join-Path $p 'python_embeded\python.exe')) -or
                          (Test-Path (Join-Path $p 'python\python.exe')) -or
                          (Test-Path (Join-Path $p 'venv\Scripts\python.exe')) -or
                          (Test-Path (Join-Path $p '.venv\Scripts\python.exe'))
                if ($ownsPy) {
                    # ...and the code sits directly inside or one level down.
                    $codeHere = Test-ComfyDir $p
                    if (-not $codeHere) {
                        foreach ($n in @('ComfyUI', 'comfyui')) {
                            if (Test-ComfyDir (Join-Path $p $n)) { $codeHere = $true; break }
                        }
                    }
                    if ($codeHere) { $root = $p; break }
                }
                if ($depth -lt 4) { $queue += ,@($p, $depth + 1) }
            }
        } catch { }
        if ($root) { break }
    }
}

if (-not $root) { exit 0 }

Write-Output ("ROOT=" + $root)

# ---- 3) Report the bundled python too, if there is one ----------------
foreach ($rel in @('python_embeded\python.exe', 'python\python.exe',
                   '..\python_embeded\python.exe', 'venv\Scripts\python.exe')) {
    $py = Join-Path $root $rel
    if (Test-Path $py) { Write-Output ("PY=" + (Resolve-Path $py).Path); break }
}
exit 0

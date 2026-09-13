# Ameka Voice installer for Windows.
#   irm https://ameka.ai/install.ps1 | iex
#
# The first version of this was written on a Mac with no Windows machine to
# run it on. So it explains what it is doing, checks each step, and when
# something fails it says exactly what and where the log is — because on the
# machine that finally runs it, that is all anyone has to go on.
$ErrorActionPreference = "Stop"

$Version = if ($env:AMEKA_VERSION) { $env:AMEKA_VERSION } else { "0.16.16" }
$Base    = if ($env:AMEKA_BASE) { $env:AMEKA_BASE } else { "https://ameka.ai" }
$Home_   = $env:USERPROFILE
$Root    = Join-Path $Home_ ".ameka"
$AppDir  = Join-Path $Root "app"
$Venv    = Join-Path $Root "venv"
$LogDir  = Join-Path $Home_ ".local\state\ameka"

function Say($msg)  { Write-Host "  > $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "  x $msg" -ForegroundColor Red; Write-Host "`n  Log: $LogDir\ameka.log"; exit 1 }

Write-Host "`n  Installing Ameka Voice $Version for Windows`n"

# ---- Python 3.11+ ----------------------------------------------------------
$Py = $null
# "python" alone must work: the Microsoft Store Python has no `py` launcher.
# (And $parts[1..0] is a *descending* range in PowerShell — it handed python
# garbage and never found the one that was there.)
function Probe($candidate) {
    $parts = $candidate.Split(" ")
    $rest = if ($parts.Length -gt 1) { $parts[1..($parts.Length-1)] } else { @() }
    try { return (& $parts[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null) }
    catch { return $null }
}
# 3.12 first: llama.cpp and whisper.cpp ship prebuilt wheels for it, and a
# compile on a PC with no C++ toolchain is where her brain would silently go
# missing. 3.13 works but builds from source; 3.11 is fine.
foreach ($candidate in @("py -3.12", "py -3.11", "py -3.13", "python", "python3")) {
    $ver = Probe $candidate
    if ($ver -and [version]$ver -ge [version]"3.11") { $Py = $candidate; break }
}
if (-not $Py) {
    Say "Python 3.11+ not found — installing with winget (this can take a minute)"
    try {
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements | Out-Null
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "Machine")
        $Py = "py -3.12"
        if (-not (Probe $Py)) { $Py = "python"; if (-not (Probe $Py)) { throw "python still not on PATH" } }
    } catch {
        Fail "Could not install Python. Install it from https://www.python.org/downloads/windows/ (tick 'Add to PATH'), then run this again."
    }
}
$PyParts = $Py.Split(" ")
$PyRest  = if ($PyParts.Length -gt 1) { $PyParts[1..($PyParts.Length-1)] } else { @() }
Say "Using Python: $Py"

# ---- Files -----------------------------------------------------------------
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
New-Item -ItemType Directory -Force -Path $AppDir, $LogDir | Out-Null

if ($env:AMEKA_LOCAL_SRC) {
    Copy-Item -Recurse -Force "$env:AMEKA_LOCAL_SRC\*" $AppDir
} else {
    $Tarball = Join-Path $env:TEMP "ameka-voice.tar.gz"
    Say "Downloading $Base/downloads/ameka-voice-macos.tar.gz"
    try { Invoke-WebRequest -Uri "$Base/downloads/ameka-voice-macos.tar.gz" -OutFile $Tarball -UseBasicParsing }
    catch { Fail "Download failed: $($_.Exception.Message)" }
    # The archive is the same one Macs get; the Mac-only binaries inside it are simply not used here.
    try { tar -xzf $Tarball -C $AppDir --strip-components=1 }
    catch { Fail "Could not extract the archive (tar is built into Windows 10 and later): $($_.Exception.Message)" }
    Remove-Item -Force $Tarball
}

# ---- Private Python environment -------------------------------------------
Say "Setting up a private Python environment"
& $PyParts[0] @PyRest -m venv $Venv
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { Fail "The Python environment was not created at $Venv" }
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet sounddevice numpy websocket-client
if ($LASTEXITCODE -ne 0) { Fail "Could not install the audio libraries (sounddevice, numpy)." }

# The app directory on the import path, so `python -m amekavoice` finds it.
$Site = & $VenvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
Set-Content -Path (Join-Path $Site "ameka.pth") -Value $AppDir

# ---- The `ameka` command ---------------------------------------------------
$Launcher = Join-Path $Root "ameka.cmd"
Set-Content -Path $Launcher -Value "@`"$VenvPy`" -m amekavoice %*"
$UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($UserPath -split ";" | Where-Object { $_ -eq $Root })) {
    [System.Environment]::SetEnvironmentVariable("Path", "$UserPath;$Root", "User")
    Say "Added $Root to your PATH (new terminals will have the ameka command)"
}
$env:Path = "$env:Path;$Root"

# ---- On-device voice and hearing -------------------------------------------
if (-not $env:AMEKA_NO_MODELS) {
    Say "Installing the on-device voice and hearing engines (about 500 MB, one time)"
    & $VenvPy -m amekavoice setup-local
    if ($LASTEXITCODE -ne 0) { Say "Local engines incomplete — 'ameka doctor' will say what is missing." }
}

# ---- Hooks, start at login, start now --------------------------------------
Say "Wiring Claude Code hooks, and starting Ameka at login"
& $VenvPy -m amekavoice install --login-agent
if ($LASTEXITCODE -ne 0) { Fail "Hook install failed." }

# There is no icon on Windows yet, so the only sign of life is her voice.
# If this line is silent, the speakers or the voice engine are the problem.
Say "She should say hello now"
& $VenvPy -m amekavoice say "Ameka is installed and running. Finish a turn in Claude Code and I will tell you about it."
$Status = & $VenvPy -m amekavoice status 2>&1

Write-Host @"

  Installed. Ameka starts at login from now on and keeps herself up to date
  from ameka.ai. There is no icon on Windows yet — she runs in the background
  and speaks when a Claude Code session finishes a turn.

    Is she running?      ameka status        ->  $Status
    Check everything     ameka doctor
    Quiet her            ameka mute on        (ameka mute off to resume)

  Windows will ask for microphone access the first time she listens.
  No API key needed.

  This is the first Windows build. If anything is off, the log at
  $LogDir\ameka.log says what happened — send it along.

"@
if (-not $env:AMEKA_QUIET) { Write-Host "  Press Enter to close this window."; Read-Host | Out-Null }

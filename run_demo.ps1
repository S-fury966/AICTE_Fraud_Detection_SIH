<#
.SYNOPSIS
    One command for the demo on Windows: generate the synthetic dataset, load
    it into a fresh database, evaluate it, then serve the API and dashboard.
    Mirrors run_demo.sh.

.DESCRIPTION
    PowerShell 5.1 and 7 compatible. ASCII only. Runs without changing the
    user's execution policy; invoke with:

        powershell -ExecutionPolicy Bypass -File .\run_demo.ps1

.PARAMETER ApiPort
    Port for the backend API. Default 8765. The API_PORT environment variable
    is honoured when the parameter is not set.

.PARAMETER SkipGenerate
    Skip python -m demo.generate (only if data/demo/applications already
    exists; otherwise the step runs anyway).

.PARAMETER SkipEvaluate
    Skip python -m demo.evaluate.

.PARAMETER Offline
    Force the offline label reader for this run by clearing the provider
    environment keys in this process. The .env file is never touched.

.PARAMETER CheckOnly
    Run the preflight checks and print the plan, start nothing, exit 0 or
    non-zero.

.PARAMETER ProjectRoot
    Override the project root. For tests only. Defaults to the directory
    containing this script.

.EXAMPLE
    .\run_demo.ps1
    .\run_demo.ps1 -ApiPort 8800
    .\run_demo.ps1 -Offline
    .\run_demo.ps1 -CheckOnly
#>
[CmdletBinding()]
param(
    [int] $ApiPort = 0,
    [switch] $SkipGenerate,
    [switch] $SkipEvaluate,
    [switch] $Offline,
    [switch] $CheckOnly,
    [string] $ProjectRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------- helpers ----------

function Write-Ok    ($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Warn2 ($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Err2  ($msg) { Write-Host $msg -ForegroundColor Red }
function Write-Step  ($i, $n, $msg) { Write-Host ("[{0}/{1}] {2}" -f $i, $n, $msg) }

function Fail($msg, $hint) {
    Write-Err2 ("ERROR: " + $msg)
    if ($hint) { Write-Host ("  Run: " + $hint) }
    exit 1
}

function Assert-Native($name) {
    if ($LASTEXITCODE -ne 0) {
        Fail ("{0} exited with code {1}" -f $name, $LASTEXITCODE) ''
    }
}

function Test-PortFree([int]$port) {
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $port
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener -ne $null) {
            try { $listener.Stop() } catch { }
        }
    }
}

function Resolve-NpmCmd {
    # On Windows npm/npx are .cmd shims. Resolve explicitly so both 5.1 and 7
    # invoke them without an "is not recognised" error.
    $cmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command npm -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Resolve-NpxCmd {
    $cmd = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command npx -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------- resolve paths ----------

if ([string]::IsNullOrEmpty($ProjectRoot)) {
    $Root = $PSScriptRoot
} else {
    $Root = $ProjectRoot
}
if ([string]::IsNullOrEmpty($Root)) {
    Fail 'cannot resolve project root' ''
}

$VenvPy    = Join-Path $Root '.venv\Scripts\python.exe'
$Backend   = Join-Path $Root 'backend'
$Frontend  = Join-Path $Root 'frontend'
$DemoDir   = Join-Path $Root 'data\demo'
$AppsDir   = Join-Path $DemoDir 'applications'
$LogsDir   = Join-Path $DemoDir 'logs'
$EvalJson  = Join-Path $DemoDir 'evaluation.json'

# API_PORT env fallback
if ($ApiPort -eq 0) {
    $envPort = [Environment]::GetEnvironmentVariable('API_PORT')
    if (-not [string]::IsNullOrEmpty($envPort)) {
        $parsed = 0
        if ([int]::TryParse($envPort, [ref]$parsed)) { $ApiPort = $parsed }
    }
}
if ($ApiPort -eq 0) { $ApiPort = 8765 }

$FrontendPort = 5175

# ---------- preflight ----------

$issues = @()

# 1. venv
if (-not (Test-Path $VenvPy)) {
    $issues += ("Virtual environment missing: {0}" -f $VenvPy)
    $issues += "  Fix: py -3.12 -m venv .venv  (any Python 3.10-3.12)"
}

# 2. python version
$pyVersion = ''
$pyMajorMinor = ''
if (Test-Path $VenvPy) {
    $pyVersion = & $VenvPy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    if ($LASTEXITCODE -ne 0) {
        $issues += ("Could not run {0} (exit {1})" -f $VenvPy, $LASTEXITCODE)
    } else {
        $pyVersion = ($pyVersion | Out-String).Trim()
        $parts = $pyVersion.Split('.')
        if ($parts.Length -ge 2) {
            $pyMajorMinor = $parts[0] + '.' + $parts[1]
        }
        $okMinor = @('3.10','3.11','3.12')
        if (-not ($okMinor -contains $pyMajorMinor)) {
            $issues += ("Python {0} is not supported here (need 3.10-3.12)" -f $pyVersion)
            $issues += "  Reason: rapidocr-onnxruntime declares Requires-Python <3.13"
            $issues += ("  Fix: recreate .venv with py -3.12 -m venv .venv")
        }
    }
}

# 3. backend deps
$missingDeps = @()
if ((Test-Path $VenvPy) -and ($issues.Count -eq 0)) {
    # Write the probe to a temp file to dodge PowerShell's -c quoting pitfalls.
    $probeFile = Join-Path ([System.IO.Path]::GetTempPath()) ("probe_{0}.py" -f ([System.Guid]::NewGuid().ToString('N')))
    $probeBody = @'
import importlib
mods = ["fastapi","uvicorn","sqlalchemy","fitz","cv2","multipart","imagehash","pyhanko","httpx2"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
print(",".join(missing))
'@
    Set-Content -Path $probeFile -Value $probeBody -Encoding UTF8
    try {
        $probeOut = & $VenvPy -I $probeFile
        if ($LASTEXITCODE -ne 0) {
            $issues += ("Dependency probe failed (exit {0})" -f $LASTEXITCODE)
        } else {
            $joined = (($probeOut | Out-String)).Trim()
            if (-not [string]::IsNullOrWhiteSpace($joined)) {
                $missingDeps = $joined.Split(',')
                $issues += ("Backend packages missing: {0}" -f ($missingDeps -join ', '))
                $issues += "  Fix: .venv\Scripts\pip install -r backend\requirements.txt"
            }
        }
    } finally {
        Remove-Item $probeFile -Force -ErrorAction SilentlyContinue
    }
}

# 4. node
$nodeVersion = ''
$nodeMajor = 0
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($null -eq $nodeCmd) {
    $issues += "node not found on PATH"
    $issues += "  Fix: install Node 20+ from https://nodejs.org"
} else {
    try {
        $nodeVersion = (& node --version | Out-String)
        if ($LASTEXITCODE -eq 0) {
            $trim = $nodeVersion.Trim().TrimStart('v')
            $maj = $trim.Split('.')[0]
            [int]::TryParse($maj, [ref]$nodeMajor) | Out-Null
            if ($nodeMajor -lt 20) {
                $issues += ("Node {0} is too old (need 20+)" -f $nodeVersion.Trim())
                $issues += "  Fix: install Node 20+ from https://nodejs.org"
            }
        } else {
            $issues += ("node --version exited {0}" -f $LASTEXITCODE)
        }
    } catch {
        $issues += ("node --version failed: {0}" -f $_.Exception.Message)
    }
}

$npmCmd = Resolve-NpmCmd
$npxCmd = Resolve-NpxCmd
if ($null -eq $npmCmd) { $issues += "npm not found on PATH" }
if ($null -eq $npxCmd) { $issues += "npx not found on PATH" }

# 5. ports
if (-not (Test-PortFree -port $ApiPort)) {
    $issues += ("Port {0} is busy. Pick another with -ApiPort <N>." -f $ApiPort)
}
if (-not (Test-PortFree -port $FrontendPort)) {
    $issues += ("Port {0} is busy. The dashboard is pinned to this port by vite.config.ts; free it and retry." -f $FrontendPort)
}

# 6. provider / offline note
$providerKeyName = 'GROQ_API_KEY'
$providerKeyPresent = $false
$dotEnv = Join-Path $Root '.env'
if ([Environment]::GetEnvironmentVariable($providerKeyName)) {
    $providerKeyPresent = $true
} elseif (Test-Path $dotEnv) {
    $content = Get-Content $dotEnv -ErrorAction SilentlyContinue
    foreach ($line in $content) {
        if ($line -match "^\s*$providerKeyName\s*=\s*(.+?)\s*$") {
            $val = $matches[1].Trim().Trim('"').Trim("'")
            if (-not [string]::IsNullOrEmpty($val)) { $providerKeyPresent = $true; break }
        }
    }
}

# ---------- plan summary ----------

Write-Host ""
Write-Host "Authentiq demo (PowerShell)"
Write-Host "---------------------------"
Write-Host ("  Project root : {0}" -f $Root)
Write-Host ("  Python       : {0}" -f ($(if ($pyVersion) { $pyVersion } else { '(not found)' })))
Write-Host ("  Node         : {0}" -f ($(if ($nodeVersion) { $nodeVersion.Trim() } else { '(not found)' })))
Write-Host ("  API port     : {0}" -f $ApiPort)
Write-Host ("  Dashboard    : http://localhost:{0}" -f $FrontendPort)
if ($Offline) {
    Write-Host "  Reader       : offline (forced via -Offline)"
} elseif ($providerKeyPresent) {
    Write-Host "  Reader       : LLM when reachable; offline reader takes over automatically (degraded badge)"
} else {
    Write-Host "  Reader       : offline (no provider key configured)"
}
Write-Host ""

if ($issues.Count -gt 0) {
    foreach ($line in $issues) { Write-Err2 $line }
    exit 1
}

Write-Ok "Preflight OK."

if ($CheckOnly) {
    exit 0
}

# ---------- offline enforcement ----------
# Set empty strings in this process so python-dotenv keeps them empty
# (.env's non-empty value does NOT override an already-set variable).
$originalKeys = @{}
if ($Offline) {
    foreach ($k in @('GROQ_API_KEY','MISTRAL_API_KEY','OPENROUTER_API_KEY','TOGETHER_API_KEY')) {
        $originalKeys[$k] = [Environment]::GetEnvironmentVariable($k)
        [Environment]::SetEnvironmentVariable($k, '')
    }
    Write-Warn2 "Offline reader forced for this run."
}

$originalApiTarget = [Environment]::GetEnvironmentVariable('API_TARGET')

$apiProcess = $null
$totalSteps = 7

try {

    # ---------- step 1: generate ----------
    $hasApps = (Test-Path $AppsDir) -and ((Get-ChildItem -Path $AppsDir -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)
    if ($SkipGenerate -and $hasApps) {
        Write-Step 1 $totalSteps 'Skipping demo.generate (data already present)'
    } else {
        if ($SkipGenerate -and -not $hasApps) {
            Write-Warn2 '  -SkipGenerate requested but no data present; generating anyway.'
        }
        Write-Step 1 $totalSteps 'python -m demo.generate'
        Push-Location $Backend
        try {
            & $VenvPy -m demo.generate
            Assert-Native 'demo.generate'
        } finally { Pop-Location }
    }

    # ---------- step 2: load ----------
    Write-Step 2 $totalSteps 'python -m demo.load --fresh'
    Push-Location $Backend
    try {
        & $VenvPy -m demo.load --fresh
        Assert-Native 'demo.load'
    } finally { Pop-Location }

    # ---------- step 3: evaluate ----------
    if ($SkipEvaluate) {
        Write-Step 3 $totalSteps 'Skipping demo.evaluate'
    } else {
        if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }
        $evalLog = Join-Path $LogsDir 'evaluate.log'
        Write-Step 3 $totalSteps ("python -m demo.evaluate (log: {0})" -f $evalLog)
        Push-Location $Backend
        try {
            & $VenvPy -m demo.evaluate *> $evalLog
            $rc = $LASTEXITCODE
        } finally { Pop-Location }
        if ($rc -ne 0) {
            Write-Err2 "demo.evaluate failed. Last 20 lines:"
            Get-Content $evalLog -Tail 20 | ForEach-Object { Write-Host "  $_" }
            Fail 'demo.evaluate exited non-zero' ''
        }
        if (Test-Path $EvalJson) {
            Write-Ok ("evaluation written to data\demo\evaluation.json")
        } else {
            Write-Warn2 ("evaluation finished, but {0} was not written" -f $EvalJson)
        }
    }

    # ---------- step 4: frontend deps ----------
    $nodeModules = Join-Path $Frontend 'node_modules'
    if (-not (Test-Path $nodeModules)) {
        Write-Step 4 $totalSteps 'npm install (first run, this can take a few minutes)'
        Push-Location $Frontend
        try {
            & $npmCmd install
            Assert-Native 'npm install'
        } finally { Pop-Location }
    } else {
        Write-Step 4 $totalSteps 'Frontend dependencies present'
    }

    # ---------- step 5: start API ----------
    if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir | Out-Null }
    $apiOut = Join-Path $LogsDir 'api.out.log'
    $apiErr = Join-Path $LogsDir 'api.err.log'
    Write-Step 5 $totalSteps ("Starting API on 127.0.0.1:{0} (logs: {1})" -f $ApiPort, $LogsDir)
    $apiArgs = @('-m','app.api','--port', $ApiPort.ToString())
    $apiProcess = Start-Process -FilePath $VenvPy -ArgumentList $apiArgs `
        -WorkingDirectory $Backend -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr -PassThru

    # ---------- step 6: wait for API ----------
    Write-Step 6 $totalSteps 'Waiting for API to become ready'
    $deadline = (Get-Date).AddSeconds(60)
    $url = ("http://127.0.0.1:{0}/api/meta" -f $ApiPort)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($apiProcess.HasExited) {
            Write-Err2 ("API process exited early (code {0}). Last 20 lines of API log:" -f $apiProcess.ExitCode)
            if (Test-Path $apiErr) { Get-Content $apiErr -Tail 20 | ForEach-Object { Write-Host "  $_" } }
            Fail 'API failed to start' ''
        }
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $ready = $true; break }
        } catch {
            # not ready yet
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        Write-Err2 'API did not become ready within 60s. Last 20 lines of API log:'
        if (Test-Path $apiErr) { Get-Content $apiErr -Tail 20 | ForEach-Object { Write-Host "  $_" } }
        Fail 'API start timeout' ''
    }
    Write-Ok 'API ready.'

    # ---------- step 7: vite ----------
    Write-Step 7 $totalSteps 'Starting vite dev server'
    Write-Host ''
    Write-Host ("  Dashboard : http://localhost:{0}" -f $FrontendPort)
    Write-Host ("  API docs  : http://127.0.0.1:{0}/docs" -f $ApiPort)
    Write-Host '  Role passcodes: see README.md "Roles" section.'
    Write-Host ''
    Write-Host 'Press Ctrl+C to stop.'
    Write-Host ''

    [Environment]::SetEnvironmentVariable('API_TARGET', ("http://127.0.0.1:{0}" -f $ApiPort))
    Push-Location $Frontend
    try {
        & $npxCmd vite
    } finally { Pop-Location }

} finally {
    # Restore environment
    [Environment]::SetEnvironmentVariable('API_TARGET', $originalApiTarget)
    if ($Offline) {
        foreach ($k in $originalKeys.Keys) {
            [Environment]::SetEnvironmentVariable($k, $originalKeys[$k])
        }
    }

    # Kill the API process and its children
    if ($apiProcess -ne $null -and -not $apiProcess.HasExited) {
        try {
            & taskkill.exe /PID $apiProcess.Id /T /F *> $null
        } catch { }
    }

    # Confirm the port is free
    Start-Sleep -Milliseconds 300
    if (-not (Test-PortFree -port $ApiPort)) {
        Write-Warn2 ("Note: port {0} is still in use after shutdown." -f $ApiPort)
    }
}

# BoothWatch one-step installer for a booth PC.
# Run from an Administrator PowerShell:   irm <this file's address> | iex
# Safe to run again: it updates files, keeps this booth's pairing and settings.

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$BaseUrl    = 'https://photopronto.github.io/boothwatch-dashboard/agent/'
$InstallDir = 'C:\BoothWatch'
$TaskName   = 'BoothWatch Agent'
$PythonUrl  = 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe'

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host ""; Write-Host "FAILED: $t" -ForegroundColor Red; Write-Host ""; Read-Host "Press Enter to close"; exit 1 }

try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Fail 'Please run this in an Administrator window (right-click Start, Terminal (Admin)).'
    }

    # ---------- Python ----------
    function Find-Python {
        $cands = @()
        foreach ($root in @($env:ProgramFiles, "${env:ProgramFiles(x86)}", "$env:LOCALAPPDATA\Programs\Python")) {
            if ($root -and (Test-Path $root)) {
                $cands += Get-ChildItem -Path $root -Filter 'python.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                          Where-Object { $_.FullName -match 'Python3\d+' } | Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
            }
        }
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $cands += $cmd.Source }
        foreach ($c in $cands) {
            try {
                $out = & $c -c "import sys, tkinter; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
                if ($LASTEXITCODE -eq 0 -and [int]$out -ge 309) { return $c }
            } catch { }
        }
        return $null
    }

    Step 'Looking for Python'
    $py = Find-Python
    if (-not $py) {
        Step 'Installing Python 3.12 (about 25 MB)'
        $exe = Join-Path $env:TEMP 'python-installer.exe'
        Invoke-WebRequest -Uri $PythonUrl -OutFile $exe
        $p = Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_tcltk=1', 'Include_launcher=0', 'Include_test=0' -Wait -PassThru
        if ($p.ExitCode -ne 0) { Fail "The Python installer stopped with code $($p.ExitCode)." }
        $py = Find-Python
        if (-not $py) { Fail 'Python installed but could not be found. Restart the PC and run this again.' }
    }
    Write-Host "Using $py"
    $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
    if (-not (Test-Path $pyw)) { Fail 'pythonw.exe is missing next to python.exe.' }

    # ---------- stop an older copy ----------
    Step 'Stopping any running BoothWatch'
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'BoothWatch' -and $_.CommandLine -match '(launcher|agent)\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2

    # ---------- files ----------
    Step "Downloading BoothWatch to $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    foreach ($f in @('agent.py', 'launcher.py', 'requirements.txt')) {
        Invoke-WebRequest -Uri ($BaseUrl + $f) -OutFile (Join-Path $InstallDir $f) -UseBasicParsing
    }
    $cfg = Join-Path $InstallDir 'config.json'
    if (-not (Test-Path $cfg)) {
        Invoke-WebRequest -Uri ($BaseUrl + 'config.json') -OutFile $cfg -UseBasicParsing
    } else {
        Write-Host 'Keeping this booth''s existing config.json'
    }

    # ---------- packages ----------
    Step 'Installing Python packages (a minute or two)'
    & $py -m pip install --disable-pip-version-check --quiet -r (Join-Path $InstallDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not install the Python packages. Is the PC online?' }

    # ---------- pairing ----------
    Push-Location $InstallDir
    $code = $null
    try {
        if (-not (Test-Path (Join-Path $InstallDir 'agent_state.json'))) {
            Step 'Creating a pairing code'
            & $py agent.py --pair-only | Out-Null
            if ($LASTEXITCODE -ne 0) { Fail 'Could not reach BoothWatch to create a pairing code.' }
            $codeFile = Join-Path $InstallDir 'pairing_code.txt'
            if (Test-Path $codeFile) { $code = ((Get-Content $codeFile -Raw) -replace '(?s).*?(\d{6}).*', '$1').Trim() }
        }
    } finally { Pop-Location }

    # ---------- start at sign-in ----------
    Step 'Setting BoothWatch to start whenever the booth signs in'
    $user = $id.Name
    $action    = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + (Join-Path $InstallDir 'launcher.py') + '"') -WorkingDirectory $InstallDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    Write-Host ""
    Write-Host "BoothWatch is installed and running." -ForegroundColor Green
    if ($code) {
        Write-Host ""
        Write-Host "   PAIRING CODE:   $code" -ForegroundColor Yellow
        Write-Host "   Enter it on the dashboard: Settings > Add a booth" -ForegroundColor Yellow
    } else {
        Write-Host "This booth was already paired, so it will carry on as before."
    }
    Write-Host ""
    Write-Host "Booth programs are expected in C:\Program Files (x86)\BreezeSys\. If they are elsewhere, edit $cfg"
    Write-Host ""
    Read-Host "Press Enter to close"
}
catch {
    Fail $_.Exception.Message
}

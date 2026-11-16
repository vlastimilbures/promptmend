# Contributor install: the CLI from this checkout, pinned to uv.lock, then the managed
# deploy (`promptmend espanso deploy`) writes the match files into Espanso.
# -WithConfig is kept only to say it was removed.
param([switch]$WithConfig)

$ErrorActionPreference = "Stop"

if ($WithConfig) {
    Write-Error "-WithConfig was removed: Espanso's own config\default.yml is never replaced."
    exit 1
}

foreach ($tool in "uv", "espanso") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Error "'$tool' was not found on PATH. Install it before running this script."
        exit 1
    }
}

$RepoDir = Split-Path -Parent $PSScriptRoot
Set-Location $RepoDir

# $ErrorActionPreference does not stop on a native command's nonzero exit code in
# Windows PowerShell 5.1, so check $LASTEXITCODE after each one that matters.
function Assert-Exit([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        Write-Error "$What failed with exit code $LASTEXITCODE."
        exit 1
    }
}

# Where uv keeps the tool and its launcher. Asked of uv rather than Get-Command, which would
# pick up an activated project venv whose binary disappears if .venv is removed.
$ToolBin = (uv tool dir --bin)
Assert-Exit "uv tool dir --bin"
$ToolDir = (uv tool dir)
Assert-Exit "uv tool dir"
$Cli = Join-Path $ToolBin.Trim() "promptmend.exe"
$Tool = Join-Path $ToolDir.Trim() "promptmend"
$ToolPython = Join-Path $Tool "Scripts\python.exe"

# `uv tool install --force` removes the old tool before it deletes its folder, so a promptmend
# still running from it (the interface holds Scripts\python.exe) would leave no promptmend at
# all (#356): refused before uv touches anything, best effort and once. A path .Path cannot read
# (an elevated process, a 32-bit shell) is asked of CIM; unreadable both ways, it is skipped.
function Get-PathKey([string]$Path) {
    return $Path.Trim().TrimEnd('\').ToLowerInvariant()
}
$Prefix = (Get-PathKey $Tool) + '\'
$Running = @(Get-Process -Name python, pythonw, promptmend -ErrorAction SilentlyContinue | Where-Object {
    $path = $null
    try { $path = $_.Path } catch { $path = $null }
    if (-not $path) {
        try {
            $path = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).ExecutablePath
        } catch { $path = $null }
    }
    $path -and ((Get-PathKey $path).StartsWith($Prefix) -or (Get-PathKey $path) -eq (Get-PathKey $Cli))
})
if ($Running.Count -gt 0) {
    $ids = ($Running | ForEach-Object { $_.Id }) -join ', '
    Write-Error ("promptmend from $Tool or $Cli is running (process $ids): wait for it to " +
        "finish or close it (the interface, a trigger), then run this again. promptmend was not changed.")
    exit 1
}

# `uv tool install` ignores uv.lock, so pass the locked versions as constraints (#34). The
# tool gets its own isolated environment; dev dependencies are not needed to deploy. Bytecode is
# compiled now, where a wait is expected, not on the first run (#216). Python 3.13 or later:
# on Windows 3.12 the interface's background threads starve (#348).
$Constraints = [System.IO.Path]::GetTempFileName()
try {
    uv export --frozen --no-dev --no-emit-project --no-hashes --quiet -o $Constraints
    Assert-Exit "uv export"
    uv tool install --editable . --force --compile-bytecode --python ">=3.13" -c $Constraints
    Assert-Exit "uv tool install"
} finally {
    Remove-Item $Constraints -ErrorAction SilentlyContinue
}

if (-not (Test-Path $Cli) -or -not (Test-Path $ToolPython)) {
    Write-Error "Could not locate promptmend. Check 'uv tool install' output."
    exit 1
}
& $ToolPython scripts\check_tool_lock.py --python $ToolPython
Assert-Exit "lock check"

Write-Host "Save the settings with 'promptmend setup' (config.toml and secrets.toml), or point"
Write-Host "PROMPTMEND_ENV at a .env (see docs/configuration.md). Then test -p- and -i- in any"
Write-Host "text field."
# Last, so its result is the final thing printed: it shows the plan, writes the match files
# with this launcher (forward slashes, path guards) and restarts Espanso. A match file you
# edited is kept, never overwritten, and it ends with a WARNING naming each one (exit 0:
# keeping it is safe).
& $Cli espanso deploy --yes --launcher $Cli
Assert-Exit "promptmend espanso deploy"

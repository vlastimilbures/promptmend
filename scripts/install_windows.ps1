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

# `uv tool install` ignores uv.lock, so pass the locked versions as constraints (#34). The
# tool gets its own isolated environment; dev dependencies are not needed to deploy. Bytecode is
# compiled now, where a wait is expected, not on the first run (#216).
$Constraints = [System.IO.Path]::GetTempFileName()
try {
    uv export --frozen --no-dev --no-emit-project --no-hashes --quiet -o $Constraints
    Assert-Exit "uv export"
    uv tool install --editable . --force --compile-bytecode -c $Constraints
    Assert-Exit "uv tool install"
} finally {
    Remove-Item $Constraints -ErrorAction SilentlyContinue
}

# Ask uv where it installed the tool rather than Get-Command, which would pick up an
# activated project venv whose binary disappears if .venv is removed.
$ToolBin = (uv tool dir --bin)
Assert-Exit "uv tool dir --bin"
$ToolDir = (uv tool dir)
Assert-Exit "uv tool dir"
$Cli = Join-Path $ToolBin.Trim() "promptmend.exe"
$ToolPython = Join-Path $ToolDir.Trim() "promptmend\Scripts\python.exe"
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

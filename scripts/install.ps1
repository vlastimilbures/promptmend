# PromptMend user installer for Windows (#186). Each GitHub Release attaches this file with
# its own version filled in, so this installs exactly that release:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://github.com/vlastimilbures/promptmend/releases/latest/download/install.ps1 | iex"
#
# It installs uv if missing (winget, else uv's official installer), then the wheel attached to
# that same Release (attested, never a PyPI upload that may come later):
# `uv tool install --force <Release>/promptmend-<version>-py3-none-any.whl -c <Release>/constraints.txt`,
# then `uv tool update-shell` and `promptmend doctor --no-clipboard`. It prints every command
# before it runs it, and running it again reinstalls or upgrades in place (the launcher stays
# in uv's tool bin folder). It never runs the setup or the Espanso deploy: it only prints them.
#
# Overrides (environment variables):
#   PROMPTMEND_VERSION       install this release instead (0.19.0 or later)
#   PROMPTMEND_WHEEL         install this local wheel instead (CI); needs
#   PROMPTMEND_CONSTRAINTS   a local constraints.txt
#   PROMPTMEND_DRY_RUN       1: print what would be installed, then stop before uv (CI)
#
# Windows PowerShell 5.1 compatible and ASCII only (5.1 reads a file without a BOM as ANSI).
# Everything runs inside functions: `irm | iex` runs in the caller's session, so neither the
# strict mode nor the preferences leak into it, and a failure throws instead of `exit`, which
# would close the caller's window.

function Invoke-PromptMendInstall {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    # pwsh 7.4+: a nonzero exit is checked by hand below (doctor exits 4 on findings).
    $PSNativeCommandUseErrorActionPreference = $false

    $Repo = 'https://github.com/vlastimilbures/promptmend'
    # The release workflow replaces this with the release's version when it attaches the file.
    $ReleaseVersion = '__PROMPTMEND_VERSION__'
    $Unrendered = '__PROMPTMEND_' + 'VERSION__'
    # The first release with promptmend artifacts (before it the package had another name).
    $Oldest = [version]'0.19.0'
    $WingetAlreadyInstalled = -1978335189

    function Write-Step([string]$Text) {
        Write-Host ''
        Write-Host "==> $Text" -ForegroundColor Cyan
    }

    function Write-Command([string]$Exe, [string[]]$Arguments) {
        Write-Host "> $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    }

    # Prints the command, runs it and returns its exit code. Native stderr (uv's progress) is
    # not an error: under 'Stop', Windows PowerShell 5.1 would throw on it when redirected.
    function Invoke-Native([string]$Exe, [string[]]$Arguments) {
        Write-Command $Exe $Arguments
        $ErrorActionPreference = 'Continue'
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE
    }

    function Assert-Native([string]$Exe, [string[]]$Arguments) {
        $code = Invoke-Native $Exe $Arguments
        if ($code -ne 0) {
            throw "'$Exe $($Arguments -join ' ')' failed with exit code $code."
        }
    }

    # Output of a command this script only reads (not printed: it changes nothing).
    function Read-Native([string]$Exe, [string[]]$Arguments) {
        $ErrorActionPreference = 'Continue'
        $out = & $Exe @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
        }
        return (($out | ForEach-Object { "$_" }) -join "`n").Trim()
    }

    # The session's PATH first, as it is, then any folder a new terminal would add (the
    # machine and user PATH) and $Extra, each once.
    function Update-SessionPath([string[]]$Extra) {
        $parts = @($env:Path -split ';' | Where-Object { $_ })
        $seen = @{}
        foreach ($p in $parts) { $seen[$p.TrimEnd('\').ToLowerInvariant()] = $true }
        $more = @()
        foreach ($scope in 'Machine', 'User') {
            $value = [Environment]::GetEnvironmentVariable('Path', $scope)
            if ($value) { $more += $value -split ';' }
        }
        $more += $Extra
        foreach ($p in $more) {
            if (-not $p) { continue }
            $key = $p.TrimEnd('\').ToLowerInvariant()
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = $true
                $parts += $p
            }
        }
        $env:Path = $parts -join ';'
    }

    # What to install: a local wheel (CI), or the wheel and constraints.txt attached to the
    # GitHub Release of that version.
    if ($env:PROMPTMEND_WHEEL) {
        if (-not $env:PROMPTMEND_CONSTRAINTS) {
            throw 'PROMPTMEND_WHEEL needs PROMPTMEND_CONSTRAINTS (a local constraints.txt).'
        }
        foreach ($file in $env:PROMPTMEND_WHEEL, $env:PROMPTMEND_CONSTRAINTS) {
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "$file not found." }
        }
        $Spec = (Resolve-Path -LiteralPath $env:PROMPTMEND_WHEEL).Path
        $Constraints = (Resolve-Path -LiteralPath $env:PROMPTMEND_CONSTRAINTS).Path
    } else {
        $Version = if ($env:PROMPTMEND_VERSION) { $env:PROMPTMEND_VERSION } else { $ReleaseVersion }
        if ($Version -eq $Unrendered) {
            throw ("This copy of install.ps1 names no release: use the one attached to a " +
                "GitHub Release ($Repo/releases), or set PROMPTMEND_VERSION.")
        }
        if ($Version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+([a-z]+[0-9]+)?\z') {
            throw "'$Version' is not a PromptMend version such as 0.19.0."
        }
        if ([version]($Version -creplace '[a-z].*\z', '') -lt $Oldest) {
            throw "PromptMend $Version has no Windows install; use $Oldest or later."
        }
        $Download = "$Repo/releases/download/v$Version"
        $Spec = "$Download/promptmend-$Version-py3-none-any.whl"
        $Constraints = "$Download/constraints.txt"
    }
    # Bytecode compiled now, where a wait is expected, not on the first run (#216).
    $InstallArgs = @('tool', 'install', '--force', '--compile-bytecode', $Spec, '-c', $Constraints)
    Write-Host "Installing $Spec"
    Write-Host "with the dependency versions in $Constraints"
    if ($env:PROMPTMEND_DRY_RUN -eq '1') {
        Write-Host 'Dry run (PROMPTMEND_DRY_RUN=1): would run'
        Write-Command 'uv' $InstallArgs
        return
    }

    # uv: winget first, else uv's official installer (in its own process, as uv documents it).
    # Where winget's link and uv's installer put uv, in case this terminal predates them.
    $guesses = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
    )
    if ($env:UV_INSTALL_DIR) { $guesses += $env:UV_INSTALL_DIR }
    Update-SessionPath $guesses
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Step 'uv was not found: installing it'
        $installed = $false
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $code = Invoke-Native 'winget' @('install', '--id', 'astral-sh.uv', '-e',
                '--source', 'winget', '--accept-source-agreements', '--accept-package-agreements')
            if ($code -eq 0 -or $code -eq $WingetAlreadyInstalled) { $installed = $true }
            else { Write-Warning "winget exited with $code; trying uv's own installer." }
        }
        if (-not $installed) {
            Assert-Native 'powershell' @('-NoProfile', '-ExecutionPolicy', 'ByPass', '-Command',
                'irm https://astral.sh/uv/install.ps1 | iex')
        }
        Update-SessionPath $guesses
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'uv was installed but is not on PATH yet: open a new terminal and run this again.'
        }
    }
    Write-Host "uv: $((Get-Command uv).Source)"

    $toolDir = Read-Native 'uv' @('tool', 'dir')
    $receipt = Join-Path $toolDir 'promptmend\uv-receipt.toml'
    if ((Test-Path -LiteralPath $receipt) -and
            ((Get-Content -Raw -LiteralPath $receipt) -match '(?m)\beditable\s*=')) {
        Write-Host ('Note: promptmend is installed as editable from a checkout; this replaces ' +
            'it with the release.')
    }

    # --force replaces an installed promptmend (an upgrade, or a rerun), always at the same
    # launcher path in uv's tool bin folder, which the deployed match files call.
    Write-Step 'Installing promptmend'
    Assert-Native 'uv' $InstallArgs

    Write-Step "Adding uv's tool folder to your PATH"
    Assert-Native 'uv' @('tool', 'update-shell')
    $bin = Read-Native 'uv' @('tool', 'dir', '--bin')
    Update-SessionPath @($bin)
    $cli = Join-Path $bin 'promptmend.exe'
    if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
        throw "promptmend.exe is not in $bin; check the uv tool install output above."
    }

    Write-Step 'Checking the install'
    Assert-Native $cli @('--version')
    $code = Invoke-Native $cli @('doctor', '--no-clipboard')
    if ($code -eq 4) {
        Write-Host 'doctor found something to do (expected before the first setup).'
    } elseif ($code -ne 0) {
        throw "promptmend doctor failed with exit code $code."
    }

    Write-Step 'Installed. Next steps (this script runs neither):'
    Write-Host '  promptmend                   # the interface: settings, key, test call, deploy'
Write-Host '  promptmend setup             # settings, API key, a test call and the deploy'
    Write-Host '  promptmend espanso deploy    # or only write the Espanso match files'
    Write-Host 'Open a new terminal first if promptmend is not found there.'
    Write-Host "Update: run the same install command again. Uninstall: see $Repo/blob/main/docs/install.md#uninstall"
}

# UTF-8 (no BOM) for the commands' output while this runs, as before afterwards.
function Install-PromptMend {
    $previous = $null
    try {
        $previous = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    } catch {
        $previous = $null
    }
    try {
        Invoke-PromptMendInstall
    } finally {
        if ($null -ne $previous) {
            try { [Console]::OutputEncoding = $previous } catch { }
        }
    }
}

Install-PromptMend

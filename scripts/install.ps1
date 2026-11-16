# PromptMend user installer for Windows (#186, #185). Each GitHub Release attaches this file
# with its own version and the SHA-256 of its own Windows zip filled in, so this installs
# exactly that release:
#
#   powershell -ExecutionPolicy ByPass -c "irm https://github.com/vlastimilbures/promptmend/releases/latest/download/install.ps1 | iex"
#
# Two channels; it prints every command before it runs it, and running it again reinstalls or
# upgrades in place. It never runs the setup or the Espanso deploy: it only prints them.
#
# exe (the default): the frozen build attached to the same Release,
# promptmend-<version>-windows-x64.zip (it brings its own Python). The download must match
# the SHA-256 baked into this file at release time, or nothing is changed. It is unpacked
# into a fresh folder next to %LOCALAPPDATA%\Programs\promptmend, which is then swapped in
# (the old folder renamed away first and renamed back if the swap fails, deleted once it
# worked); a promptmend.exe still running from that folder stops it before any change. The
# folder goes on the user PATH, then `promptmend.exe doctor --no-clipboard` runs.
#
# uv: uv if missing (winget, else uv's official installer), then the wheel attached to that
# same Release (attested, never a PyPI upload that may come later):
# `uv tool install --force <Release>/promptmend-<version>-py3-none-any.whl -c <Release>/constraints.txt`,
# then `uv tool update-shell` and `promptmend doctor --no-clipboard` (the launcher stays in
# uv's tool bin folder).
#
# Overrides (environment variables):
#   PROMPTMEND_CHANNEL       exe (default) or uv
#   PROMPTMEND_VERSION       install this release instead (0.19.0 or later; with the exe
#                            channel only this file's own release, whose zip hash it holds)
#   PROMPTMEND_ZIP           install this local zip instead (CI; the exe channel), checked
#                            against the baked hash when this copy has one
#   PROMPTMEND_WHEEL         install this local wheel instead (CI; implies uv); needs
#   PROMPTMEND_CONSTRAINTS   a local constraints.txt
#   PROMPTMEND_DRY_RUN       1: print what would be installed, then stop before any change (CI)
#
# Windows PowerShell 5.1 compatible and ASCII only (5.1 reads a file without a BOM as ANSI).
# Everything runs inside functions: `irm | iex` runs in the caller's session, so neither the
# strict mode nor the preferences leak into it, and a failure throws instead of `exit`, which
# would close the caller's window.

function Invoke-PromptMendInstall {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    # Windows PowerShell 5.1's progress bar slows Invoke-WebRequest down many times over.
    $ProgressPreference = 'SilentlyContinue'
    # pwsh 7.4+: a nonzero exit is checked by hand below (doctor exits 4 on findings).
    $PSNativeCommandUseErrorActionPreference = $false

    $Repo = 'https://github.com/vlastimilbures/promptmend'
    # The release workflow replaces these with the release's version and the SHA-256 of its
    # Windows zip when it attaches the file.
    $ReleaseVersion = '__PROMPTMEND_VERSION__'
    $ReleaseZipSha256 = '__PROMPTMEND_ZIP_SHA256__'
    $Unrendered = '__PROMPTMEND_' + 'VERSION__'
    $UnrenderedHash = '__PROMPTMEND_' + 'ZIP_SHA256__'
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

    function Get-PathKey([string]$Path) {
        return $Path.Trim().TrimEnd('\').ToLowerInvariant()
    }

    # The session's PATH first, as it is, then any folder a new terminal would add (the
    # machine and user PATH) and $Extra, each once.
    function Update-SessionPath([string[]]$Extra) {
        $parts = @($env:Path -split ';' | Where-Object { $_ })
        $seen = @{}
        foreach ($p in $parts) { $seen[(Get-PathKey $p)] = $true }
        $more = @()
        foreach ($scope in 'Machine', 'User') {
            $value = [Environment]::GetEnvironmentVariable('Path', $scope)
            if ($value) { $more += $value -split ';' }
        }
        $more += $Extra
        foreach ($p in $more) {
            if (-not $p) { continue }
            $key = Get-PathKey $p
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = $true
                $parts += $p
            }
        }
        $env:Path = $parts -join ';'
    }

    # Appends $Folder to the user PATH unless it is there already. The value is read and
    # written unexpanded with its own registry type, so a %VARIABLE% in it stays one
    # ([Environment]::SetEnvironmentVariable would store it expanded).
    function Add-UserPath([string]$Folder) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        if ($null -eq $key) { throw 'HKCU\Environment cannot be opened for writing.' }
        try {
            $raw = [string]$key.GetValue('Path', '',
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
            if ($key.GetValueNames() -contains 'Path') { $kind = $key.GetValueKind('Path') }
            $want = Get-PathKey $Folder
            foreach ($p in ($raw -split ';')) {
                if (-not $p) { continue }
                if ((Get-PathKey $p) -eq $want) { return $false }
                if ((Get-PathKey ([Environment]::ExpandEnvironmentVariables($p))) -eq $want) {
                    return $false
                }
            }
            $value = if ($raw.TrimEnd(';')) { "$($raw.TrimEnd(';'));$Folder" } else { $Folder }
            $key.SetValue('Path', $value, $kind)
        } finally {
            $key.Close()
        }
        # Removing a variable that does not exist changes nothing, but .NET then broadcasts
        # WM_SETTINGCHANGE, so a terminal opened from the Start menu sees the new PATH.
        [Environment]::SetEnvironmentVariable('PROMPTMEND_INSTALL_UNUSED', $null, 'User')
        return $true
    }

    # The promptmend.exe processes started from $Folder (not readable: skipped; the rename
    # below then fails and is rolled back).
    function Get-RunningFrom([string]$Folder) {
        $prefix = (Get-PathKey $Folder) + '\'
        $found = @()
        foreach ($proc in @(Get-Process -Name promptmend -ErrorAction SilentlyContinue)) {
            $path = $null
            try { $path = $proc.Path } catch { $path = $null }
            if ($path -and (Get-PathKey $path).StartsWith($prefix)) { $found += $proc }
        }
        return , $found
    }

    function Assert-NotRunning([string]$Folder) {
        $running = Get-RunningFrom $Folder
        if ($running.Count -gt 0) {
            $ids = ($running | ForEach-Object { $_.Id }) -join ', '
            throw ("promptmend.exe from $Folder is running (process $ids): wait for it to " +
                'finish or close it (the interface, a trigger), then run this again. ' +
                'Nothing was changed.')
        }
    }

    function Get-Sha256([string]$File) {
        return (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant()
    }

    function New-Id {
        return [guid]::NewGuid().ToString('N').Substring(0, 8)
    }

    # Where uv keeps a promptmend tool, or $null: asks uv when it is on PATH, else looks in
    # uv's default tool folder. Changes nothing.
    function Find-UvTool {
        $dirs = @()
        if ($env:UV_TOOL_DIR) { $dirs += $env:UV_TOOL_DIR }
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            try { $dirs += Read-Native 'uv' @('tool', 'dir') } catch { }
        }
        if ($env:APPDATA) { $dirs += (Join-Path $env:APPDATA 'uv\tools') }
        foreach ($dir in $dirs) {
            if (-not $dir) { continue }
            $tool = Join-Path $dir 'promptmend'
            if (Test-Path -LiteralPath (Join-Path $tool 'uv-receipt.toml') -PathType Leaf) {
                return $tool
            }
        }
        return $null
    }

    # The version: this file's release, else PROMPTMEND_VERSION, checked; $null for a local
    # file (PROMPTMEND_WHEEL, PROMPTMEND_ZIP).
    function Get-Version {
        $version = if ($env:PROMPTMEND_VERSION) { $env:PROMPTMEND_VERSION } else { $ReleaseVersion }
        if ($version -eq $Unrendered) {
            throw ("This copy of install.ps1 names no release: use the one attached to a " +
                "GitHub Release ($Repo/releases), or set PROMPTMEND_VERSION.")
        }
        if ($version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+([a-z]+[0-9]+)?\z') {
            throw "'$version' is not a PromptMend version such as 0.19.0."
        }
        if ([version]($version -creplace '[a-z].*\z', '') -lt $Oldest) {
            throw "PromptMend $version has no Windows install; use $Oldest or later."
        }
        return $version
    }

    # The frozen exe (the default channel).
    function Install-Exe {
        $Programs = Join-Path $env:LOCALAPPDATA 'Programs'
        $Dest = Join-Path $Programs 'promptmend'
        $Exe = Join-Path $Dest 'promptmend.exe'
        if (-not [Environment]::Is64BitOperatingSystem) {
            throw 'The PromptMend exe is built for 64-bit Windows: set PROMPTMEND_CHANNEL=uv.'
        }

        # What to install: a local zip (CI), or the zip attached to this file's own Release.
        $Version = $null
        $Url = $null
        if ($env:PROMPTMEND_ZIP) {
            if (-not (Test-Path -LiteralPath $env:PROMPTMEND_ZIP -PathType Leaf)) {
                throw "$($env:PROMPTMEND_ZIP) not found."
            }
            $Source = (Resolve-Path -LiteralPath $env:PROMPTMEND_ZIP).Path
            $ZipName = 'promptmend-windows-x64.zip'
        } else {
            $Version = Get-Version
            if ($ReleaseZipSha256 -eq $UnrenderedHash) {
                throw ('This copy of install.ps1 holds no hash of the Windows zip: use the one ' +
                    "attached to a GitHub Release ($Repo/releases), or set PROMPTMEND_CHANNEL=uv.")
            }
            if ($Version -ne $ReleaseVersion) {
                throw ("This install.ps1 checks the zip of PromptMend $ReleaseVersion only. For " +
                    "$Version, run that release's own ($Repo/releases/download/v$Version/install.ps1 " +
                    'instead of releases/latest/download/install.ps1), or set PROMPTMEND_CHANNEL=uv.')
            }
            $ZipName = "promptmend-$Version-windows-x64.zip"
            $Source = "$Repo/releases/download/v$Version/$ZipName"
            $Url = $Source
        }
        $Expected = $null
        if ($ReleaseZipSha256 -ne $UnrenderedHash) { $Expected = $ReleaseZipSha256.ToLowerInvariant() }
        Write-Host "Installing $Source"
        if ($Expected) { Write-Host "which must have the SHA-256 $Expected" }
        Write-Host "into $Dest"
        if ($env:PROMPTMEND_DRY_RUN -eq '1') {
            Write-Host 'Dry run (PROMPTMEND_DRY_RUN=1): would'
            if ($Url) { Write-Command 'Invoke-WebRequest' @('-UseBasicParsing', '-Uri', $Url, '-OutFile', "<temp>\$ZipName") }
            Write-Host "  check its SHA-256, unpack it next to $Dest, swap it in and add $Dest to your PATH"
            Write-Command $Exe @('doctor', '--no-clipboard')
            return
        }

        Assert-NotRunning $Dest
        $Temp = Join-Path ([IO.Path]::GetTempPath()) ('promptmend-install-' + (New-Id))
        $Id = New-Id
        $Stage = Join-Path $Programs "promptmend.new-$Id"
        $Old = Join-Path $Programs "promptmend.old-$Id"
        try {
            New-Item -ItemType Directory -Path $Temp | Out-Null
            $Zip = Join-Path $Temp $ZipName
            Write-Step 'Getting the PromptMend exe'
            if ($Url) {
                Write-Command 'Invoke-WebRequest' @('-UseBasicParsing', '-Uri', $Url, '-OutFile', $Zip)
                Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Zip
            } else {
                Write-Command 'Copy-Item' @('-LiteralPath', $Source, '-Destination', $Zip)
                Copy-Item -LiteralPath $Source -Destination $Zip
            }
            $Actual = Get-Sha256 $Zip
            Write-Host "SHA-256: $Actual"
            if ($Expected -and $Actual -ne $Expected) {
                throw ("The zip's SHA-256 is $Actual, not the $Expected this release recorded: " +
                    'it was not installed, and nothing was changed.')
            }

            Write-Step "Unpacking it next to $Dest"
            if (-not (Test-Path -LiteralPath $Programs)) {
                New-Item -ItemType Directory -Path $Programs | Out-Null
            }
            New-Item -ItemType Directory -Path $Stage | Out-Null
            Write-Command 'Expand-Archive' @('-LiteralPath', $Zip, '-DestinationPath', $Stage)
            Expand-Archive -LiteralPath $Zip -DestinationPath $Stage
            $New = Join-Path $Stage 'promptmend'
            $NewExe = Join-Path $New 'promptmend.exe'
            if (-not (Test-Path -LiteralPath $NewExe -PathType Leaf) -or
                    -not (Test-Path -LiteralPath (Join-Path $New '_internal') -PathType Container)) {
                throw 'The zip holds no promptmend\promptmend.exe with its _internal folder.'
            }
            # Run once before the swap: an exe that cannot start here (blocked, broken) leaves the
            # installed one alone.
            $ErrorActionPreference = 'Continue'
            Write-Command $NewExe @('--version')
            $got = ((& $NewExe --version 2>$null) | Out-String).Trim()
            $code = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            Write-Host $got
            if ($code -ne 0) {
                throw "The new promptmend.exe did not start (exit code $code); nothing was changed."
            }
            if ($Version -and $got -ne $Version) {
                throw "The new promptmend.exe says $got, not $Version; nothing was changed."
            }

            Write-Step "Replacing $Dest"
            Assert-NotRunning $Dest
            $hadOld = Test-Path -LiteralPath $Dest
            if ($hadOld) {
                Write-Command 'Rename-Item' @('-LiteralPath', $Dest, '-NewName', (Split-Path -Leaf $Old))
                try {
                    Rename-Item -LiteralPath $Dest -NewName (Split-Path -Leaf $Old)
                } catch {
                    throw ("$Dest could not be moved aside (a file in it is in use?): " +
                        "$($_.Exception.Message) Nothing was changed.")
                }
            }
            Write-Command 'Move-Item' @('-LiteralPath', $New, '-Destination', $Dest)
            try {
                Move-Item -LiteralPath $New -Destination $Dest
            } catch {
                $why = $_.Exception.Message
                if ($hadOld) {
                    Write-Command 'Rename-Item' @('-LiteralPath', $Old, '-NewName', (Split-Path -Leaf $Dest))
                    try {
                        Rename-Item -LiteralPath $Old -NewName (Split-Path -Leaf $Dest)
                    } catch {
                        throw ("The new folder could not be moved to ${Dest} ($why), nor the " +
                            "previous one back: rename $Old to promptmend by hand.")
                    }
                }
                throw "The new folder could not be moved to ${Dest}: $why The previous install is kept."
            }
            if ($hadOld) {
                Write-Command 'Remove-Item' @('-LiteralPath', $Old, '-Recurse', '-Force')
                try {
                    Remove-Item -LiteralPath $Old -Recurse -Force
                } catch {
                    Write-Warning "The previous install stays in $Old (delete it later): $($_.Exception.Message)"
                }
            }
        } finally {
            foreach ($leftover in $Stage, $Temp) {
                if (Test-Path -LiteralPath $leftover) {
                    Remove-Item -LiteralPath $leftover -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }

        Write-Step 'Adding it to your PATH'
        Write-Host "> add $Dest to the user PATH (HKCU\Environment)" -ForegroundColor DarkGray
        if (Add-UserPath $Dest) { Write-Host "Added $Dest." } else { Write-Host 'Already there.' }
        Update-SessionPath @($Dest)

        Write-Step 'Checking the install'
        $code = Invoke-Native $Exe @('doctor', '--no-clipboard')
        if ($code -eq 4) {
            Write-Host 'doctor found something to do (expected before the first setup).'
        } elseif ($code -ne 0) {
            throw "promptmend doctor failed with exit code $code."
        }

        $uvTool = Find-UvTool
        if ($uvTool) {
            Write-Step 'A uv tool install of promptmend is still there'
            Write-Host "  $uvTool"
            Write-Host 'Your Espanso triggers may still call it. To switch them to the exe (this script runs neither):'
            Write-Host "  & '$Exe' setup     # or: & '$Exe' espanso deploy, so the match files call the exe"
            Write-Host '  uv tool uninstall promptmend'
        }

        Write-Step 'Installed. Next steps (this script runs neither):'
        Write-Host '  promptmend                   # the interface: settings, key, test call, deploy'
        Write-Host '  promptmend setup             # settings, API key, a test call and the deploy'
        Write-Host '  promptmend espanso deploy    # or only write the Espanso match files'
        Write-Host 'Open a new terminal first if promptmend is not found there.'
        Write-Host "Update: run the same install command again. Uninstall: see $Repo/blob/main/docs/install.md#uninstall"
    }

    # uv and the Release wheel (PROMPTMEND_CHANNEL=uv, or PROMPTMEND_WHEEL).
    function Install-WithUv {
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
            $Version = Get-Version
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

    # --- The channel ---------------------------------------------------------------------
    $Channel = 'exe'
    if ($env:PROMPTMEND_CHANNEL) {
        $Channel = $env:PROMPTMEND_CHANNEL.Trim().ToLowerInvariant()
        if ($Channel -cne 'exe' -and $Channel -cne 'uv') {
            throw "PROMPTMEND_CHANNEL='$($env:PROMPTMEND_CHANNEL)' is not exe or uv."
        }
    }
    if ($env:PROMPTMEND_WHEEL -and $env:PROMPTMEND_ZIP) {
        throw 'Set PROMPTMEND_WHEEL (the uv channel) or PROMPTMEND_ZIP (the exe channel), not both.'
    }
    if ($env:PROMPTMEND_WHEEL) {
        if ($env:PROMPTMEND_CHANNEL -and $Channel -ne 'uv') {
            throw 'PROMPTMEND_WHEEL installs with uv: unset PROMPTMEND_CHANNEL or set it to uv.'
        }
        $Channel = 'uv'
    }
    if ($env:PROMPTMEND_ZIP -and $Channel -ne 'exe') {
        throw 'PROMPTMEND_ZIP is the exe channel: unset PROMPTMEND_CHANNEL or set it to exe.'
    }
    if ($Channel -eq 'uv') {
        Write-Host 'Channel: uv'
        Install-WithUv
    } else {
        Write-Host 'Channel: exe (set PROMPTMEND_CHANNEL=uv to install with uv instead)'
        Install-Exe
    }
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

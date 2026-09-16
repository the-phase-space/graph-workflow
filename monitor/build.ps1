#Requires -Version 7.0
# Build outside Dropbox, then install the completed Monitor beside its sources.
# Keep frontendDist relative (../dist): Tauri parses a drive-qualified Windows
# path as a URL and silently omits the embedded frontend assets.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$monitorRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$buildRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Temp\Graph-Workflow-build'))
$frontend = Join-Path $buildRoot 'monitor-frontend'
$stagedBin = Join-Path $buildRoot 'monitor-bin'
$backupRoot = Join-Path $buildRoot ('monitor-backup-' + [guid]::NewGuid().ToString('N'))
$installedExe = Join-Path $monitorRoot 'bin\graph-workflow-monitor.exe'
$previousTarget = $env:CARGO_TARGET_DIR
$stoppedMonitors = @()

function Assert-Within([string]$Path, [string]$Root) {
    $absolute = [IO.Path]::GetFullPath($Path)
    $boundary = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $absolute.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the intended directory: $absolute"
    }
}

function Install-Directory([string]$Source, [string]$Target) {
    Assert-Within $Source $buildRoot
    Assert-Within $Target $monitorRoot
    $backup = Join-Path $backupRoot (Split-Path -Leaf $Target)
    Assert-Within $backup $buildRoot
    $hadTarget = Test-Path -LiteralPath $Target
    if ($hadTarget) {
        New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
        Copy-Item -LiteralPath $Target -Destination $backup -Recurse
    }
    try {
        # A cross-volume directory move can leave the old destination in
        # place. Remove it explicitly so Move-Item cannot nest Source inside it.
        if ($hadTarget) { Remove-Item -LiteralPath $Target -Recurse -Force }
        Move-Item -LiteralPath $Source -Destination $Target
    }
    catch {
        if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
        if ($hadTarget) {
            Copy-Item -LiteralPath $backup -Destination $Target -Recurse
        }
        throw
    }
}

Push-Location -LiteralPath $monitorRoot
try {
    $env:CARGO_TARGET_DIR = $buildRoot
    Assert-Within $frontend $buildRoot
    Assert-Within $stagedBin $buildRoot
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null

    & npm.cmd run build -- --outDir $frontend --emptyOutDir
    if ($LASTEXITCODE -ne 0) { throw 'Monitor frontend build failed' }
    Install-Directory $frontend (Join-Path $monitorRoot 'dist')

    # The completed frontend is now at ../dist, as tauri.conf.json declares.
    # Cargo artifacts still go to the external build directory.
    & npm.cmd run tauri -- build --no-bundle
    if ($LASTEXITCODE -ne 0) { throw 'Monitor executable build failed' }

    $builtExe = Join-Path $buildRoot 'release\graph-workflow-monitor.exe'
    $builtHash = (Get-FileHash -LiteralPath $builtExe -Algorithm SHA256).Hash
    New-Item -ItemType Directory -Path $stagedBin -Force | Out-Null
    Copy-Item -LiteralPath $builtExe -Destination $stagedBin

    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'graph-workflow-monitor.exe'" |
        Where-Object { $_.ExecutablePath -eq $installedExe })
    foreach ($monitor in $running) {
        if (Get-Process -Id $monitor.ProcessId -ErrorAction SilentlyContinue) {
            $stoppedMonitors += $monitor
            # WebView2 children can retain the shortcut's bin working directory.
            & taskkill.exe /PID $monitor.ProcessId /T /F | Out-Null
            if (Get-Process -Id $monitor.ProcessId -ErrorAction SilentlyContinue) {
                throw 'Could not stop the Monitor for installation'
            }
        }
    }
    Install-Directory $stagedBin (Join-Path $monitorRoot 'bin')
    if ((Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash -ne $builtHash) {
        throw 'Installed Monitor does not match the completed build'
    }
    Write-Output "Installed $installedExe"
}
finally {
    Pop-Location
    $env:CARGO_TARGET_DIR = $previousTarget
    # Reopen only instances that were running before installation, with their
    # original arguments. Building an unopened app must not launch it.
    foreach ($monitor in $stoppedMonitors) {
        $arguments = $monitor.CommandLine -replace '^\s*(?:"[^"]+"|\S+)\s*', ''
        if ($arguments) {
            Start-Process -FilePath $installedExe -ArgumentList $arguments -WindowStyle Normal
        }
        else {
            Start-Process -FilePath $installedExe -WindowStyle Normal
        }
    }
}

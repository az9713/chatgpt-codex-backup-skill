[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$StagingDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9][a-z0-9-]{0,79}$')][string]$Label,
    [Parameter(Mandatory = $true)][string]$TargetPath,
    [hashtable]$AdditionalRootTargets = @{},
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Assert-NoReparsePoint([string]$Path) {
    $current = [System.IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw 'A source or target ancestor is a symlink/junction. Review it manually.'
            }
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if (-not $parent) { break }
        $current = $parent.FullName
    }
}

$stage = [System.IO.Path]::GetFullPath($StagingDirectory)
$target = [System.IO.Path]::GetFullPath($TargetPath)
Assert-NoReparsePoint $stage
Assert-NoReparsePoint $target
$map = Get-Content -LiteralPath (Join-Path $stage 'RESTORE-MAP.json') -Raw | ConvertFrom-Json
$roots = @($map.manifest.plan.roots | Where-Object { $_.label -eq $Label })
if ($roots.Count -ne 1) { throw 'Label is not present exactly once in the restore map.' }
$source = Join-Path $stage $Label
$isDirectory = [bool]$roots[0].is_directory
Assert-NoReparsePoint $source
if (-not (Test-Path -LiteralPath $source)) { throw 'Staged root is missing.' }
if (Test-Path -LiteralPath $target) { throw 'Target already exists. No merges or overwrites are allowed.' }
if ($target.StartsWith($stage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $target -eq $stage) {
    throw 'Target must be outside the staging directory.'
}
$links = @($map.manifest.entries | Where-Object { $_.type -eq 'link' -and ($_.name -eq $Label -or $_.name.StartsWith($Label + '/')) })
$rootTargets = @{}
foreach ($key in $AdditionalRootTargets.Keys) { $rootTargets[$key] = [IO.Path]::GetFullPath($AdditionalRootTargets[$key]) }
$rootTargets[$Label] = $target
$linkPlans = @()
foreach ($link in $links) {
    $relative = $link.name.Substring($Label.Length).TrimStart('/')
    if (-not $relative -or $relative -match '(^|/)\.\.(/|$)|[:\\]' -or [IO.Path]::IsPathRooted($relative)) { throw 'Unsafe recorded link path.' }
    $matched = @($map.manifest.plan.roots | Where-Object {
        $oldRoot = $_.source.TrimEnd('\', '/')
        $link.target -eq $oldRoot -or $link.target.StartsWith($oldRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    } | Sort-Object { $_.source.Length } -Descending)
    if (-not $matched.Count -or -not $rootTargets.ContainsKey($matched[0].label)) { throw 'A link needs an explicit target-root mapping. Supply -AdditionalRootTargets after reviewing RESTORE-MAP.json.' }
    $oldRoot = $matched[0].source.TrimEnd('\', '/')
    $suffix = $link.target.Substring($oldRoot.Length).TrimStart('\', '/')
    $mappedRoot = $rootTargets[$matched[0].label]
    Assert-NoReparsePoint $mappedRoot
    $mappedTarget = if ($suffix) { Join-Path $mappedRoot $suffix } else { $mappedRoot }
    if ($link.link_kind -notin @('directory', 'file')) { throw 'Unknown recorded link type; review it manually.' }
    $linkPlans += [pscustomobject]@{ Path = (Join-Path $target $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)); Target = $mappedTarget; Kind = $link.link_kind }
}
$items = @(Get-ChildItem -LiteralPath $source -Recurse -Force)
if (@($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
    throw 'Staging contains a symlink or junction.'
}
# Recheck staged payload hashes; a verified archive does not authenticate later edits.
$entries = @($map.manifest.entries | Where-Object { $_.type -eq 'file' -and ($_.name -eq $Label -or $_.name.StartsWith($Label + '/')) })
foreach ($entry in $entries) {
    $relative = $entry.name.Substring($Label.Length).TrimStart('/')
    if ($relative -match '(^|/)\.\.(/|$)|[:\\]' -or [IO.Path]::IsPathRooted($relative)) { throw 'Unsafe manifest path.' }
    $file = Join-Path $source $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)
    $item = Get-Item -LiteralPath $file -Force
    if ($item.Length -ne $entry.size -or (Get-Sha256 $file) -ne $entry.sha256) {
        throw 'Staged payload hash mismatch.'
    }
}
$files = @($items | Where-Object { -not $_.PSIsContainer })
if ($files.Count -ne $entries.Count) { throw 'Staging contains unlisted files.' }
Write-Output ([pscustomobject]@{ Label = $Label; Source = $source; Target = $target; Files = $entries.Count; Apply = [bool]$Apply })
if ($linkPlans.Count) { Write-Output $linkPlans }
if (-not $Apply) { Write-Output 'Preview only. Repeat with -Apply after reviewing the destination.'; return }
$apps = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(Codex|ChatGPT|chatgpt-work)$' })
if ($apps.Count) { throw 'Close Codex and ChatGPT before promoting data.' }
if ($PSCmdlet.ShouldProcess($target, 'Copy verified staged root into a new destination')) {
    $parent = Split-Path -Parent $target
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # Make the destination private before writing any credential bytes.
    if (-not $isDirectory) {
        $outStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            & icacls $target /inheritance:r /grant:r "*$($sid):F" /Q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Could not restrict target file ACL.' }
            $inStream = [IO.File]::OpenRead((Join-Path $source '__file__'))
            try { $inStream.CopyTo($outStream) } finally { $inStream.Dispose() }
        } finally { $outStream.Dispose() }
    } else {
        New-Item -ItemType Directory -Path $target | Out-Null
        & icacls $target /inheritance:r /grant:r "*$($sid):(OI)(CI)F" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not restrict target directory ACL.' }
        foreach ($item in @(Get-ChildItem -LiteralPath $source -Force)) {
            Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse -Force
        }
    }
    foreach ($entry in $entries) {
        $relative = $entry.name.Substring($Label.Length).TrimStart('/')
        $copied = if ($isDirectory) { Join-Path $target $relative.Replace('/', [IO.Path]::DirectorySeparatorChar) } else { $target }
        if ((Get-Sha256 $copied) -ne $entry.sha256) { throw 'Copied payload hash mismatch.' }
    }
    foreach ($linkPlan in $linkPlans) {
        if (-not (Test-Path -LiteralPath $linkPlan.Target)) { throw 'Mapped link target is missing. Restore target roots first, then review the incomplete destination.' }
        $linkParent = Split-Path -Parent $linkPlan.Path
        New-Item -ItemType Directory -Path $linkParent -Force | Out-Null
        if (Test-Path -LiteralPath $linkPlan.Path) { throw 'Recorded link collides with an existing destination.' }
        $kind = if ($linkPlan.Kind -eq 'directory') { 'Junction' } else { 'SymbolicLink' }
        New-Item -ItemType $kind -Path $linkPlan.Path -Target $linkPlan.Target | Out-Null
    }
    Write-Output 'Copied. Review absolute paths, reauthenticate, and validate history before retiring the old laptop.'
}

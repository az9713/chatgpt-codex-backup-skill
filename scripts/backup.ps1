[CmdletBinding()]
param(
    [string]$Plan,
    [string]$Output,
    [string]$Destination = 'D:\backups\chatgpt-codex',
    [switch]$AllowLive,
    [switch]$RequireClosed,
    [switch]$AllowPartial
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'backup.py'
$arguments = @($script, 'backup')
if ($Plan) { $arguments += @('--plan', $Plan) }
if ($Output) { $arguments += @('--output', $Output) }
else { $arguments += @('--destination', $Destination) }
if ($AllowLive -and $RequireClosed) { throw 'Choose live mode or RequireClosed, not both.' }
if (-not $RequireClosed) { $arguments += '--allow-live' }
if ($AllowPartial) { $arguments += '--allow-partial' }
& python @arguments
exit $LASTEXITCODE

<#
.SYNOPSIS
Initialize, start, stop, or inspect the new rin-memory instance.
.EXAMPLE
.\tools\stack.ps1 init -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.EXAMPLE
.\tools\stack.ps1 start -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.EXAMPLE
.\tools\stack.ps1 status -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
.EXAMPLE
.\tools\stack.ps1 stop -Config F:\game\projectNEKO\companion-runtime\config\stack.toml
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('init', 'start', 'stop', 'status')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$Config,
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'),
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Install the wheel into .venv first, or pass -Python with an explicit installed interpreter.'
}
$stackArgs = @('-m', 'rin_memory.stack', $Action, '--config', $Config)
if ($NoOpen) { $stackArgs += '--no-open' }
& $Python @stackArgs
exit $LASTEXITCODE

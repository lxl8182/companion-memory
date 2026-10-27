<#
.SYNOPSIS
  铃芽 RIN 一键启动 / 停止 / 状态（Windows 双击或命令行）。

.DESCRIPTION
  这是 rin-memory 的日常入口。它在仓库根目录运行，替你把
  「找解释器 → 准备配置 → 启动服务 → 打开管理台」串起来：

    * 首次运行自动在 companion-runtime\config\stack.toml 生成配置模板
    * 用 tools\stack.ps1 的同一套堆栈管理（单实例、端口冲突、有序停止）
    * 服务托管水彩管理台，浏览器直接打开 http://127.0.0.1:48931/

  双击本文件（或双击同目录的 start.cmd）即执行 start。

.PARAMETER Action
  start（默认）| stop | status | init

.EXAMPLE
  .\start.ps1
  .\start.ps1 -Action status
  .\start.ps1 -Action stop
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'init')]
    [string]$Action = 'start',
    [string]$Config = '',
    [string]$Python = '',
    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
$RuntimeRoot = Join-Path (Split-Path -Parent $RepoRoot) 'companion-runtime'
if (-not $Config) { $Config = Join-Path $RuntimeRoot 'config\stack.toml' }

function Write-Step([string]$Text) { Write-Host "  $Text" -ForegroundColor DarkGray }
function Write-Ok([string]$Text) { Write-Host "[ok] $Text" -ForegroundColor Green }
function Write-Warn2([string]$Text) { Write-Host "[!] $Text" -ForegroundColor Yellow }
function Write-Bad([string]$Text) { Write-Host "[x] $Text" -ForegroundColor Red }

# --- 解释器：优先仓库 .venv，其次 PATH 上的 python -------------------------
function Resolve-Python {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw "指定的解释器不存在：$Python"
        }
        return (Resolve-Path -LiteralPath $Python).Path
    }
    $candidates = @(
        (Join-Path $RepoRoot '.venv\Scripts\python.exe'),
        (Join-Path $RepoRoot '.venv\bin\python')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw '找不到 Python。请先在仓库根执行：uv sync'
}

function Invoke-Stack([string[]]$StackArgs) {
    $py = Resolve-Python
    # -m 走的是已安装（或 editable）的 rin_memory；开发树需要 src 在路径上。
    $env:PYTHONPATH = (Join-Path $RepoRoot 'src')
    $env:PYTHONUTF8 = '1'
    # 只返回退出码：需要输出的调用方用 Invoke-StackOutput，否则 JSON 会和
    # 退出码混进同一个返回值。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $py -m rin_memory.stack @StackArgs 2>&1 | Out-Null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return $code
}

function Invoke-StackOutput([string[]]$StackArgs) {
    $py = Resolve-Python
    $env:PYTHONPATH = (Join-Path $RepoRoot 'src')
    $env:PYTHONUTF8 = '1'
    # 用临时文件接住两个流：`2>&1` 在 PowerShell 里会把 stderr 包成带
    # NativeCommandError 装饰的 ErrorRecord，错误信息会被这层装饰淹没。
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $py -ArgumentList (@('-m', 'rin_memory.stack') + $StackArgs) `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $script:StackExitCode = $proc.ExitCode
        $stdout = Get-Content -LiteralPath $outFile -Raw -Encoding UTF8
        $stderr = Get-Content -LiteralPath $errFile -Raw -Encoding UTF8
        if ($null -eq $stdout) { $stdout = '' }
        if ($null -eq $stderr) { $stderr = '' }
        return (@($stdout.Trim(), $stderr.Trim()) | Where-Object { $_ }) -join "`n"
    } finally {
        Remove-Item -LiteralPath $outFile, $errFile -ErrorAction SilentlyContinue
    }
}

# --- 启动前的环境检查 ------------------------------------------------------
function Test-Prerequisites {
    $py = Resolve-Python
    Write-Step "解释器  $py"
    $probe = & $py -c "import rin_memory, sys; print(rin_memory.__version__ if hasattr(rin_memory,'__version__') else 'ok')" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "这个解释器没有 rin_memory 包。"
        Write-Step '在仓库根执行：uv sync    （或 pip install -e .)'
        return $false
    }
    Write-Step "核心    $probe"
    return $true
}

# --- 首次运行：生成配置模板 ------------------------------------------------
function Initialize-Config {
    if (Test-Path -LiteralPath $Config -PathType Leaf) { return $true }
    Write-Host ''
    Write-Host '首次运行：生成配置模板' -ForegroundColor Cyan
    $code = Invoke-Stack @('init', '--config', $Config)
    if ($code -ne 0) {
        Write-Bad "生成配置失败（退出码 $code）。"
        return $false
    }
    Write-Ok "已生成 $Config"
    Write-Host ''
    Write-Warn2 '接下来需要填模型接口，两种方式任选：'
    Write-Step 'A. 启动后在水彩管理台「设置 → 模型接口」里填（推荐，保存即生效）'
    Write-Step "B. 编辑 $Config 的 [models.service] 段，再设置环境变量里的 Key"
    Write-Host ''
    return $true
}

# --- 动作 ------------------------------------------------------------------
function Start-Rin {
    if (-not (Test-Prerequisites)) { exit 2 }
    if (-not (Initialize-Config)) { exit 2 }

    Write-Host '启动铃芽 RIN …' -ForegroundColor Cyan
    $stackArgs = @('start', '--config', $Config)
    if ($NoOpen) { $stackArgs += '--no-open' }

    $output = Invoke-StackOutput $stackArgs
    $code = $script:StackExitCode
    $text = $output

    if ($code -ne 0) {
        Write-Bad '启动失败'
        # 结构化错误里只有一行有用信息，原样显示便于定位。
        Write-Host $text
        if ($text -match 'port') { Write-Step '端口被占用：用 -Action status 看是谁，或先 -Action stop' }
        if ($text -match 'lock|writer') { Write-Step '同一数据根已有别的写者：先停掉旧的实例或其他管理台' }
        exit 3
    }

    try { $state = $text | ConvertFrom-Json } catch { $state = $null }
    if ($state -and $state.already_running) {
        Write-Ok "已经在运行（PID $($state.pid)）"
    } else {
        Write-Ok "服务已启动（PID $($state.pid)）"
    }
    $url = if ($state -and $state.url) { $state.url } else { 'http://127.0.0.1:48931' }
    Write-Host ''
    Write-Host "  管理台  $url" -ForegroundColor Cyan
    Write-Step '对话页在导航「对话」，模型接口在「设置」。'

    $missing = @()
    if ($state -and $state.missing) { $missing = @($state.missing) }
    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Warn2 '还有未填写的配置项（不影响启动，但对话需要模型）：'
        foreach ($field in $missing) { Write-Step $field }
    }

    if (-not $NoOpen) { Start-Process $url }
    Write-Host ''
    Write-Step "停止： .\start.cmd stop      状态： .\start.cmd status"
    # 日志路径来自实际配置的 logs 根，而不是默认运行目录：-Config 指向别处时
    # 仍要指对文件。
    $logs = $null
    if ($state -and $state.roots) { $logs = $state.roots.logs }
    if (-not $logs) { $logs = Join-Path $RuntimeRoot 'logs' }
    Write-Step "日志： $(Join-Path $logs 'memory-service.log')"
}

function Stop-Rin {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        Write-Warn2 '还没有配置，无需停止。'
        exit 0
    }
    Write-Host '停止铃芽 RIN …' -ForegroundColor Cyan
    $code = Invoke-Stack @('stop', '--config', $Config)
    if ($code -ne 0) { Write-Bad "停止失败（退出码 $code）"; exit 3 }
    Write-Ok '已停止'
}

function Show-Status {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        Write-Warn2 '还没有配置。先运行 .\start.cmd'
        exit 0
    }
    $text = Invoke-StackOutput @('status', '--config', $Config)
    if ($script:StackExitCode -ne 0) { Write-Bad '读取状态失败'; Write-Host $text; exit 3 }
    try { $state = $text | ConvertFrom-Json } catch { Write-Host $text; exit 0 }

    $label = switch ($state.status) {
        'running' { '运行中' }
        'stopped' { '已停止' }
        'exited' { '已退出（异常）' }
        'unreachable' { '进程在但无响应' }
        'port_conflict' { '端口被其他程序占用' }
        default { $state.status }
    }
    $color = if ($state.status -eq 'running') { 'Green' } else { 'Yellow' }
    Write-Host ''
    Write-Host "  状态  $label" -ForegroundColor $color
    if ($state.status -eq 'running') {
        Write-Host "  地址  $($state.url)"
        Write-Host "  PID   $($state.pid)"
        $caps = $state.capabilities
        if ($caps) {
            foreach ($name in @('storage', 'chat', 'background')) {
                $cap = $caps.$name
                if ($cap) {
                    $statusText = if ($cap.status -eq 'ready') { '就绪' } else { "$($cap.status) $($cap.reason)" }
                    Write-Host ("  {0,-11}{1}" -f $name, $statusText)
                }
            }
        }
    }
    Write-Host "  配置  $($state.config)" -ForegroundColor DarkGray
    if ($state.missing -and @($state.missing).Count -gt 0) {
        Write-Host ''
        Write-Warn2 '未填写的配置项：'
        foreach ($field in $state.missing) { Write-Step $field }
    }
    if ($state.status -eq 'stopped' -and $state.exit) {
        Write-Host "  退出  $($state.exit.status)" -ForegroundColor DarkGray
    }
    Write-Host ''
}

Write-Host ''
Write-Host '  铃芽 RIN · rin-memory' -ForegroundColor DarkCyan
Write-Host '  ─────────────────────────────' -ForegroundColor DarkGray
switch ($Action) {
    'init' {
        $code = Invoke-Stack @('init', '--config', $Config)
        if ($code -ne 0) { Write-Bad "初始化失败（退出码 $code）"; exit 3 }
        Write-Ok "配置模板：$Config"
    }
    'start' { Start-Rin }
    'stop' { Stop-Rin }
    'status' { Show-Status }
}

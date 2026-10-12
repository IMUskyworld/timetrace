# timetrace 本地质量闸门：推之前跑这个。
# 用法:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1 [-SkipAudit]
#
# 必须带 -ExecutionPolicy Bypass：本机默认执行策略会拦 -File（faillab 事故 F-0001）。
# 本文件存成 UTF-8 with BOM：Windows PowerShell 5.1 否则按 GBK 解码，全角括号的尾字节会吃掉引号（learntree 的教训）。

param(
    [switch]$SkipAudit
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($name, $body) {
    Write-Host ""
    Write-Host "==> $name" -ForegroundColor Cyan
    $script:lastExit = 0
    try {
        & $body | Out-Host
        $script:lastExit = $LASTEXITCODE
    } catch {
        $script:lastExit = 1
        Write-Host "x $name 抛异常：$_" -ForegroundColor Red
    }
    if ($script:lastExit -ne 0) {
        Write-Host ""
        Write-Host "x 闸门停在：$name" -ForegroundColor Red
        exit $script:lastExit
    }
}

# dev 工具装在 .venv 里；没有 venv 就用当前解释器（可能缺 ruff/pyright）
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

Write-Host "timetrace 质量闸门（python: $python）"

Step "运行时零依赖（本项目的第一原则）" {
    $pyproject = Get-Content pyproject.toml -Raw -Encoding UTF8
    if ($pyproject -notmatch '(?m)^dependencies\s*=\s*\[\s*\]\s*$') {
        Write-Host "    pyproject.toml 的 dependencies 不再是空列表" -ForegroundColor Red
        exit 1
    }
    foreach ($module in @('requests', 'httpx', 'urllib3')) {
        $found = & $python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$module') else 1)"
        if ($LASTEXITCODE -eq 0) {
            $used = Select-String -Path (Get-ChildItem -Recurse -Path src -Filter *.py) -Pattern "^\s*import\s+$module" -Quiet
            if ($used) {
                Write-Host "    src 里用到了 $module，但运行时依赖表是空的" -ForegroundColor Red
                exit 1
            }
        }
    }
    Write-Host "    dependencies = []，src 也没有引入第三方网络库"
    # 上面那句 find_spec 探测返回 1 是"没装这个库"的正常结果，别让它污染闸门的退出码判定
    $global:LASTEXITCODE = 0
}

Step "Lint (ruff)" {
    & $python -m ruff check src tests scripts
}

Step "Format check (ruff)" {
    & $python -m ruff format --check src tests scripts
}

Step "Types (pyright)" {
    & $python -m pyright
}

Step "换行符（仓库内必须 LF）" {
    $bad = @()
    $files = Get-ChildItem -Recurse -Path . -Include *.py, *.toml, *.md, *.yml, *.ps1, *.json
    foreach ($file in $files) {
        if ($file.FullName -match '\\(\.venv|__pycache__|\.git|\.verify-tmp)\\') { continue }
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        for ($i = 0; $i -lt $bytes.Length - 1; $i++) {
            if ($bytes[$i] -eq 13 -and $bytes[$i + 1] -eq 10) { $bad += $file.Name; break }
        }
    }
    if ($bad.Count -gt 0) {
        Write-Host "    含 CRLF 的文件：$($bad -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "    $($files.Count) 个文件全部 LF"
}

Step "unit tests (pytest)" {
    & $python -m pytest
}

if ($SkipAudit) {
    Write-Host ""
    Write-Host "    （已跳过 §20 验收：-SkipAudit）" -ForegroundColor DarkGray
} else {
    Step "MVP 验收 (§20)" {
        & $python scripts/verify_mvp.py --skip-tests
    }
}

Write-Host ""
Write-Host "all checks passed" -ForegroundColor Green

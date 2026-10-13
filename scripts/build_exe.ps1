$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
python -m PyInstaller --noconfirm --clean (Join-Path $root "packaging\timetrace.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "生成完成：$root\dist\timetrace.exe"

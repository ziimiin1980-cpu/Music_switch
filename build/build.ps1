# Rebuild the portable app:  powershell -ExecutionPolicy Bypass -File build\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) {
    uv venv --python 3.13 .venv
}
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt

.venv\Scripts\python.exe build\make_icon.py
.venv\Scripts\python.exe -m compileall -q app.py core smtc audio ui
.venv\Scripts\pyinstaller.exe pereklych.spec --noconfirm --clean

Write-Host ""
Write-Host "Portable build: $root\dist\Pereklych\Pereklych.exe"
Write-Host "Installer (needs Inno Setup 6): iscc build\installer.iss"

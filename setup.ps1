$ErrorActionPreference = "Stop"

Write-Host "=== PersonalGameAI v0.2.0 setup ===" -ForegroundColor Cyan
Write-Host ""

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Python launcher 'py' was not found." -ForegroundColor Red
    Write-Host "Install a 64-bit Python 3.10+ build, then re-run this script."
    exit 1
}

try {
    $version = & py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    $ok = & py -c "import sys; print(int(sys.version_info >= (3,10)))"
} catch {
    Write-Host "Python could not be started through py.exe." -ForegroundColor Red
    exit 1
}

if ($ok.Trim() -ne "1") {
    Write-Host "Python $version is too old. Python 3.10 or newer is required." -ForegroundColor Red
    exit 1
}

Write-Host "Using Python $version" -ForegroundColor Green

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv..."
    & py -m venv .venv
}

Write-Host "Upgrading pip..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip

Write-Host "Installing dependencies (including dev tools: pytest, ruff)..."
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Run with: .\run.bat"

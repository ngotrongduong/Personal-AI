$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "=== Compile check ===" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m compileall -q main.py core vision agent tests

Write-Host "=== Unit tests ===" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v

Write-Host "=== PASS ===" -ForegroundColor Green

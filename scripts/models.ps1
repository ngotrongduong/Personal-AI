param(
    [ValidateSet(
        "status",
        "pull-recommended",
        "pull-planner",
        "pull-vision",
        "pull-memory",
        "pull-heavy"
    )]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

$ModelPlanner = "qwen3.5:9b"
$ModelPlannerFallback = "qwen3.5:4b"
$ModelVision = "qwen3-vl:4b"
$ModelEmbedding = "qwen3-embedding:0.6b"
$ModelHeavy = "gpt-oss:20b"

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Ollama is not installed or is not on PATH." -ForegroundColor Red
    Write-Host "Install Ollama first, then re-run this script."
    exit 1
}

function Pull-Model([string]$Name) {
    Write-Host ""
    Write-Host "Pulling $Name ..." -ForegroundColor Cyan
    & ollama pull $Name
    if ($LASTEXITCODE -ne 0) {
        throw "ollama pull failed for $Name"
    }
}

switch ($Action) {
    "status" {
        Write-Host "=== Ollama version ===" -ForegroundColor Cyan
        & ollama --version
        Write-Host ""
        Write-Host "=== Installed models ===" -ForegroundColor Cyan
        & ollama list
    }

    "pull-recommended" {
        Pull-Model $ModelPlanner
        Pull-Model $ModelEmbedding
    }

    "pull-planner" {
        Pull-Model $ModelPlanner
        Pull-Model $ModelPlannerFallback
    }

    "pull-vision" {
        Pull-Model $ModelVision
    }

    "pull-memory" {
        Pull-Model $ModelEmbedding
    }

    "pull-heavy" {
        Write-Warning "gpt-oss:20b is about 14GB and is not the default for the current 12GB-VRAM target."
        Write-Warning "It may use system RAM/CPU offload and run substantially slower."
        Pull-Model $ModelHeavy
    }
}

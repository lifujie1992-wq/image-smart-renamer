# Image Smart Renamer - Windows launcher
# Usage: .\start-windows.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Virtual env not found. Run: uv sync" -ForegroundColor Red
    exit 1
}

# Load .env if present
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $name = $parts[0].Trim()
            $value = $parts[1].Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$name" -Value $value
        }
    }
    Write-Host "Loaded config from .env" -ForegroundColor Cyan
}

if (-not $env:OPENAI_API_KEY -and -not $env:ANTHROPIC_API_KEY) {
    Write-Host "Warning: OPENAI_API_KEY is not set. Classification will fail." -ForegroundColor Yellow
}

$model = if ($env:OPENAI_MODEL) { $env:OPENAI_MODEL } else { "gpt-5.5" }
$base = if ($env:OPENAI_BASE_URL) { $env:OPENAI_BASE_URL } else { "https://sub.711bigseller.icu/v1" }
Write-Host "Model: $model  Base: $base" -ForegroundColor Cyan
Write-Host "Starting Image Smart Renamer at http://127.0.0.1:8765" -ForegroundColor Green
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

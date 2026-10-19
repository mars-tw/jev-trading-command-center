param([switch]$RealPaper, [int]$Port = 8775)
$ErrorActionPreference = "Stop"
$entry = Join-Path $PSScriptRoot "run_app.py"
if ($RealPaper) { & python -B $entry --real-paper --port $Port }
else { & python -B $entry --demo --port $Port }
exit $LASTEXITCODE

param([switch]$EnableNativeCli, [int]$Port = 8766, [int]$Capacity = 8)
$ErrorActionPreference = "Stop"
if (-not $EnableNativeCli) { Write-Output "Disabled: explicit -EnableNativeCli is required."; exit 2 }
$entry = Join-Path $PSScriptRoot "run_workguild.py"
& python -B $entry --enable-native-cli --port $Port --capacity $Capacity
exit $LASTEXITCODE

param(
  [string]$DistPath = "dist",
  [string]$WorkPath = "build"
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $project
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name "SshTunnelManager" `
  --distpath $DistPath `
  --workpath $WorkPath `
  --icon "assets\logo.ico" `
  --add-data "assets\logo.png;assets" `
  app.py
Write-Host "Built: $project\$DistPath\SshTunnelManager.exe"

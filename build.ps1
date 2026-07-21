param(
  [string]$DistPath = "dist",
  [string]$WorkPath = "build",
  [switch]$BuildInstaller
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
  --add-data "vscode_extension;vscode_extension" `
  app.py
$version = (python -c "from ssh_tunnel_manager import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) {
  throw "Unable to determine application version."
}
$portable = Join-Path $DistPath "SshTunnelManager-Portable-$version.exe"
Copy-Item -LiteralPath (Join-Path $DistPath "SshTunnelManager.exe") -Destination $portable -Force

if ($BuildInstaller) {
  $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if (-not $iscc) {
    $knownPaths = @(
      (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
      (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
      (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($known in $knownPaths) {
      if (Test-Path -LiteralPath $known) {
        $iscc = Get-Item -LiteralPath $known
        break
      }
    }
  }
  if (-not $iscc) {
    throw "Inno Setup 6 was not found. Install it or add ISCC.exe to PATH."
  }
  $isccPath = if ($iscc.Source) { $iscc.Source } else { $iscc.FullName }
  & $isccPath "/DAppVersion=$version" "installer.iss"
  if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
  }
}

$expectedNames = @("SshTunnelManager-Portable-$version.exe")
if ($BuildInstaller) {
  $expectedNames += "SshTunnelManager-Setup-$version.exe"
}
$artifacts = Get-ChildItem -LiteralPath $DistPath -File | Where-Object {
  $_.Name -in $expectedNames
}
$checksums = foreach ($artifact in $artifacts) {
  $hash = Get-FileHash -LiteralPath $artifact.FullName -Algorithm SHA256
  "$($hash.Hash.ToLowerInvariant())  $($artifact.Name)"
}
Set-Content -LiteralPath (Join-Path $DistPath "SHA256SUMS.txt") -Value $checksums -Encoding ASCII
Write-Host "Built portable application: $project\$portable"
if ($BuildInstaller) {
  Write-Host "Built installer: $project\$DistPath\SshTunnelManager-Setup-$version.exe"
}

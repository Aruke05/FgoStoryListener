$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Project 'dist-v2.9.8'
$VenvPython = Join-Path $Project '.venv\Scripts\python.exe'
$Vendor = Join-Path $Project '.tools\fgo-listener'
$Python = if (Test-Path -LiteralPath $VenvPython) {
  $VenvPython
} else {
  (Get-Command python -ErrorAction Stop).Source
}

# Keep compatibility with the existing portable dependency directory while
# preferring the documented project virtual environment on clean clones.
if (-not (Test-Path -LiteralPath $VenvPython) -and (Test-Path -LiteralPath $Vendor)) {
  $env:PYTHONPATH = $Vendor
}

& $Python -c 'import frida, PyInstaller' 2>$null
if ($LASTEXITCODE -ne 0) {
  throw '缺少构建依赖。请先运行: python -m pip install -r requirements-build.txt'
}

& $Python -m PyInstaller `
  --noconfirm `
  --clean `
  --distpath $Dist `
  --workpath (Join-Path $Project 'build') `
  (Join-Path $Project 'FgoStoryListener.spec')

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $Project 'README.md') `
  -Destination (Join-Path $Dist 'FgoStoryListener\README.md') `
  -Force

Write-Host "Built: $(Join-Path $Dist 'FgoStoryListener\FgoStoryListener.exe')"

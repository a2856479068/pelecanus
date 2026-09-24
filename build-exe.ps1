$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m PyInstaller --noconfirm --clean app/PelicanWatch.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
Write-Host "Built: $PSScriptRoot\dist\PelicanWatch.exe"

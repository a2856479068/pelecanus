$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pelicanPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pelicanPython)) {
    $pelicanPython = (Get-Command python -ErrorAction Stop).Source
}
$pelicanPort = if ($env:PORT) { $env:PORT } else { '8765' }
Write-Host "Pelican Watch: http://127.0.0.1:$pelicanPort/"
Write-Host "Admin: http://127.0.0.1:$pelicanPort/admin"
& $pelicanPython (Join-Path $PSScriptRoot 'app\server.py')

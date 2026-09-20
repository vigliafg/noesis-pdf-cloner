# Installa il motore di clonazione pdf2zh_next v2 accanto all'app (.venv2).
# Usalo dopo aver scaricato una release standalone di Noesis PDF Cloner,
# eseguendolo nella stessa cartella dell'eseguibile.
#
#   powershell -ExecutionPolicy Bypass -File .\setup_engine.ps1
#
# Richiede Python 3.12 e 'uv' (https://docs.astral.sh/uv/).
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$uv = if ($env:UV) { $env:UV } else { "uv" }
if (-not (Get-Command $uv -ErrorAction SilentlyContinue)) {
    Write-Error "ERRORE: 'uv' non trovato. Installa uv da https://docs.astral.sh/uv/"
    exit 1
}

if (-not (Test-Path ".venv2")) {
    & $uv venv --python 3.12 .venv2
}
& $uv pip install --python .venv2\Scripts\python.exe -q pdf2zh_next

Write-Host "Motore installato in: $PSScriptRoot\.venv2\Scripts\pdf2zh_next.exe"
Write-Host "Noesis PDF Cloner lo rileverà automaticamente."

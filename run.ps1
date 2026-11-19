<#
===============================================================================
 run.ps1 - Inicia o simulado.

 Ele confere se a instalacao existe, sincroniza a pasta de conteudo com a base
 do RAG e abre o simulado no navegador (http://127.0.0.1:5000).

 Como rodar:
     .\run.ps1

 Extras:
     .\run.ps1 -Reindexar   -> apaga e reconstroi a base do zero antes de abrir
     .\run.ps1 -Status      -> so mostra o que esta indexado e sai

 Para parar o simulado: volte a esta janela e aperte Ctrl + C.
===============================================================================
#>

param(
    [switch]$Reindexar,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Acentos corretos no terminal do Windows.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# --------------------------------------------------------------------------- #
# Conferencias basicas antes de subir o servidor
# --------------------------------------------------------------------------- #
if (-not (Test-Path $venvPython)) {
    Write-Host "[ERRO] O projeto ainda nao foi instalado." -ForegroundColor Red
    Write-Host "       Rode primeiro:  .\setup.ps1" -ForegroundColor Yellow
    exit 1
}

$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Host "[AVISO] Arquivo .env nao encontrado: o simulado abre, mas nao gera questoes." -ForegroundColor Yellow
    Write-Host "        Crie o .env com a linha  OPENAI_API_KEY=sk-...  ou rode .\setup.ps1" -ForegroundColor Yellow
} elseif ((Get-Content $envPath -Raw) -match "cole_sua_chave_aqui") {
    Write-Host "[AVISO] A chave da OpenAI ainda nao foi preenchida no arquivo .env." -ForegroundColor Yellow
}

# --------------------------------------------------------------------------- #
# Modos auxiliares
# --------------------------------------------------------------------------- #
if ($Status) {
    & $venvPython -m rag.indexer --status
    exit $LASTEXITCODE
}

if ($Reindexar) {
    Write-Host "Reconstruindo a base vetorial do zero..." -ForegroundColor Cyan
    & $venvPython -m rag.indexer --rebuild
}

# --------------------------------------------------------------------------- #
# Simulado
# --------------------------------------------------------------------------- #
Write-Host ""
Write-Host "Iniciando o simulado... (Ctrl + C para parar)" -ForegroundColor Green
Write-Host ""
& $venvPython app.py

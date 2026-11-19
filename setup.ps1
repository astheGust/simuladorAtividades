<#
===============================================================================
 setup.ps1 - Prepara TUDO para o simulado rodar.

 O que este script faz:
   1. encontra o Python instalado (precisa ser 3.10 ou mais novo);
   2. cria um ambiente virtual isolado na pasta .venv;
   3. instala todas as bibliotecas do requirements.txt;
   4. cria o arquivo .env e guarda a sua chave da OpenAI;
   5. baixa o modelo de busca local (embeddings), ~120 MB, uma vez so;
   6. cria a pasta de conteudo e ja indexa o que estiver dentro dela.

 Como rodar (no PowerShell, dentro da pasta do projeto):
     .\setup.ps1

 Se o Windows reclamar de permissao para executar scripts, rode antes:
     Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

 Parametros opcionais:
     .\setup.ps1 -Chave "sk-..."   -> ja grava a chave sem perguntar
     .\setup.ps1 -SemPerguntas     -> nao pergunta nada (para automacao)
     .\setup.ps1 -TorchCompleto    -> instala o PyTorch completo (com CUDA/GPU)
===============================================================================
#>

param(
    [string]$Chave = "",
    [switch]$SemPerguntas,
    [switch]$TorchCompleto
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Deixa o Python falar UTF-8 no terminal do Windows (acentos corretos).
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Titulo($texto) {
    Write-Host ""
    Write-Host "=============================================================" -ForegroundColor DarkCyan
    Write-Host " $texto" -ForegroundColor Cyan
    Write-Host "=============================================================" -ForegroundColor DarkCyan
}
function Passo($texto) { Write-Host "-> $texto" -ForegroundColor White }
function Ok($texto)    { Write-Host "   [OK] $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "   [!] $texto" -ForegroundColor Yellow }
function Falha($texto) { Write-Host "   [ERRO] $texto" -ForegroundColor Red }

Titulo "SIMULADOR RAG - INSTALACAO"

# --------------------------------------------------------------------------- #
# 1. Procurar um Python 3.10+
# --------------------------------------------------------------------------- #
Passo "Procurando o Python instalado..."

$python = $null
$candidatos = @("py -3.13", "py -3.12", "py -3.11", "py -3", "python", "python3")

foreach ($candidato in $candidatos) {
    $partes = $candidato.Split(" ")
    $exe = $partes[0]
    $pyArgs = @()
    if ($partes.Count -gt 1) { $pyArgs = @($partes[1]) }

    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $versao = & $exe @pyArgs -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [int]$versao -ge 310) {
            $python = @{ Exe = $exe; Args = $pyArgs }
            $legivel = & $exe @pyArgs -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"
            Ok "Python $legivel encontrado ($exe)."
            break
        }
    } catch { }
}

if ($null -eq $python) {
    Falha "Nao encontrei Python 3.10 ou superior."
    Write-Host "   Baixe em https://www.python.org/downloads/ e marque a opcao" -ForegroundColor Yellow
    Write-Host "   'Add python.exe to PATH' durante a instalacao. Depois rode este script de novo." -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------------------- #
# 2. Ambiente virtual (.venv) - mantem as bibliotecas so deste projeto
# --------------------------------------------------------------------------- #
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (Test-Path $venvPython) {
    Ok "Ambiente virtual .venv ja existe."
} else {
    Passo "Criando o ambiente virtual .venv (pode demorar um pouco)..."
    $pyExe = $python.Exe
    $pyArgs = $python.Args
    & $pyExe @pyArgs -m venv .venv
    if (-not (Test-Path $venvPython)) {
        Falha "Nao consegui criar o ambiente virtual."
        exit 1
    }
    Ok "Ambiente virtual criado."
}

# --------------------------------------------------------------------------- #
# 3. Instalar as bibliotecas
# --------------------------------------------------------------------------- #
Passo "Atualizando o pip..."
& $venvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Aviso "Nao consegui atualizar o pip; seguindo assim mesmo." }

if (-not $TorchCompleto) {
    # O PyTorch padrao vem com suporte a GPU e passa de 2 GB. Para gerar os
    # embeddings do RAG a versao so-CPU (~200 MB) e suficiente e bem mais rapida
    # de instalar. Use -TorchCompleto se voce quiser usar a GPU.
    Passo "Instalando o PyTorch versao CPU (mais leve)..."
    & $venvPython -m pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet
    if ($LASTEXITCODE -ne 0) {
        Aviso "Versao CPU indisponivel; a versao padrao sera instalada junto com o resto."
    } else {
        Ok "PyTorch (CPU) instalado."
    }
}

Passo "Instalando as bibliotecas do requirements.txt (demora alguns minutos)..."
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Falha "Falha ao instalar as dependencias. Confira sua conexao e rode o setup de novo."
    exit 1
}
Ok "Bibliotecas instaladas."

# --------------------------------------------------------------------------- #
# 4. Arquivo .env com a chave da OpenAI
# --------------------------------------------------------------------------- #
$envPath = Join-Path $PSScriptRoot ".env"
$precisaChave = $true

if (Test-Path $envPath) {
    $conteudoEnv = Get-Content $envPath -Raw
    if ($conteudoEnv -match "OPENAI_API_KEY\s*=\s*\S+" -and $conteudoEnv -notmatch "cole_sua_chave_aqui") {
        Ok "Arquivo .env ja tem uma chave configurada."
        $precisaChave = $false
    }
}

if ($precisaChave) {
    if (-not $Chave -and -not $SemPerguntas) {
        Write-Host ""
        Write-Host "   Cole abaixo a sua chave da OpenAI (comeca com sk-...)." -ForegroundColor Cyan
        Write-Host "   Pegue a sua em: https://platform.openai.com/api-keys" -ForegroundColor DarkGray
        Write-Host "   Se preferir colocar depois, so aperte ENTER." -ForegroundColor DarkGray
        $Chave = Read-Host "   Chave"
    }

    if ($Chave) {
        "# Chave da OpenAI usada pelo modelo gpt-5.6-luna`r`nOPENAI_API_KEY=$Chave`r`n" |
            Out-File -FilePath $envPath -Encoding utf8 -NoNewline
        Ok "Chave salva em .env"
    } else {
        Copy-Item ".env.example" $envPath -Force
        Aviso "Nenhuma chave informada. Abra o arquivo .env e cole a sua chave antes de rodar o simulado."
    }
}

# --------------------------------------------------------------------------- #
# 5. Pasta de conteudo + modelo de embeddings + indexacao inicial
# --------------------------------------------------------------------------- #
$pastaConteudo = Join-Path $PSScriptRoot "conteudo"
if (-not (Test-Path $pastaConteudo)) {
    New-Item -ItemType Directory -Path $pastaConteudo | Out-Null
    Ok "Pasta 'conteudo' criada."
}

Passo "Baixando o modelo de busca local (uma vez so, ~120 MB)..."
& $venvPython -c "from rag import config; from sentence_transformers import SentenceTransformer; m=config.carregar()['rag']['modelo_embedding']; SentenceTransformer(m); print('modelo pronto:', m)"
if ($LASTEXITCODE -ne 0) {
    Aviso "Nao consegui baixar o modelo agora; ele sera baixado na primeira execucao."
} else {
    Ok "Modelo de busca pronto."
}

Passo "Indexando o que ja estiver na pasta 'conteudo'..."
& $venvPython -m rag.indexer
if ($LASTEXITCODE -ne 0) { Aviso "A indexacao inicial falhou; o simulado tentara de novo ao iniciar." }

# --------------------------------------------------------------------------- #
# 6. Fim
# --------------------------------------------------------------------------- #
Titulo "TUDO PRONTO"
Write-Host " 1. Coloque seus arquivos de estudo em:" -ForegroundColor White
Write-Host "      $pastaConteudo" -ForegroundColor Cyan
Write-Host "      (pdf, docx, doc, txt, md, xlsx, xls, csv, pptx, html)"
Write-Host ""
Write-Host " 2. Se quiser, ajuste o prompt em:" -ForegroundColor White
Write-Host "      config\prompt.md" -ForegroundColor Cyan
Write-Host ""
Write-Host " 3. Inicie o simulado com:" -ForegroundColor White
Write-Host "      .\run.ps1" -ForegroundColor Green
Write-Host ""

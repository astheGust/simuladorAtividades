# -*- coding: utf-8 -*-
"""
config.py - Centraliza caminhos do projeto e a leitura das configuracoes.

Tudo que o usuario pode ajustar fica em config/settings.json. Este modulo le esse
arquivo, mistura com os valores padrao (assim nada quebra se o usuario apagar uma
chave) e entrega um dicionario pronto para o resto do programa usar.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Caminhos fixos do projeto (calculados a partir da localizacao deste arquivo)
# --------------------------------------------------------------------------- #
ROOT_DIR = Path(__file__).resolve().parent.parent   # pasta raiz do projeto
CONFIG_DIR = ROOT_DIR / "config"                    # settings.json + prompt.md
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PROMPT_FILE = CONFIG_DIR / "prompt.md"
TEMPLATES_DIR = ROOT_DIR / "templates"              # interface web
STATE_DIR = ROOT_DIR / ".rag_state"                 # controle de arquivos indexados
MANIFEST_FILE = STATE_DIR / "manifest.json"         # "quem ja entrou na base e com qual versao"
HISTORY_CSV = ROOT_DIR / "historico.csv"            # respostas do usuario

# Carrega o .env (chave da API) uma unica vez, na importacao.
load_dotenv(ROOT_DIR / ".env")

# --------------------------------------------------------------------------- #
# Valores padrao: espelham o config/settings.json entregue com o projeto.
# Servem de rede de seguranca caso o usuario remova alguma chave.
# --------------------------------------------------------------------------- #
PADROES = {
    "titulo": "Simulado Personalizado",
    "conteudo": {
        "pasta": "conteudo",
        "topico_por": "pasta",
        "ignorar": ["~$*", ".*", "LEIA-ME.txt", "Thumbs.db", "desktop.ini"],
    },
    "rag": {
        "modelo_embedding": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "pasta_banco": ".chroma_db",
        "colecao": "conteudo",
        "chunk_alvo_tokens": 300,
        "chunk_max_tokens": 450,
        "chunk_overlap_paragrafos": 1,
        "trechos_por_topico": 2,
        "vizinhos_por_trecho": 2,
    },
    "llm": {
        "modelo": "gpt-5.6-luna",
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY",
        "reasoning_effort": "low",
        "max_completion_tokens": 8000,
        "temperature": None,
        "top_p": None,
        "frequency_penalty": None,
        "presence_penalty": None,
        "usar_json_schema": True,
        "timeout_segundos": 180,
        "preco_entrada_por_milhao": 0.20,
        "preco_saida_por_milhao": 1.20,
    },
    "simulado": {
        "questoes_por_lote": 5,
        "opcoes_por_questao": 5,
        "idioma": "portugues do Brasil",
        "instrucoes_extras": "",
        "limiar_duplicata": 0.85,
        "permitir_sem_conteudo": False,
        "tema_livre": "",
    },
    "variaveis_prompt": {},
    "servidor": {
        "host": "127.0.0.1",
        "porta": 5000,
        "abrir_navegador": True,
    },
}

# Cache em memoria para nao reler o JSON a cada chamada.
_cache = None


def _mesclar(padrao, usuario):
    """
    Junta o dicionario padrao com o do usuario, recursivamente.
    O valor do usuario vence; o que ele nao definiu vem do padrao.
    Chaves que comecam com "_" sao os comentarios do settings.json e sao descartadas.
    """
    resultado = dict(padrao)
    for chave, valor in (usuario or {}).items():
        if chave.startswith("_"):
            continue  # e um comentario, nao uma configuracao
        if isinstance(valor, dict) and isinstance(resultado.get(chave), dict):
            resultado[chave] = _mesclar(resultado[chave], valor)
        else:
            resultado[chave] = valor
    return resultado


def carregar(recarregar=False):
    """
    Devolve as configuracoes completas (padrao + settings.json do usuario).
    Use recarregar=True para reler o arquivo do disco (usado a cada lote de questoes,
    para que editar o settings.json valha na hora).
    """
    global _cache
    if _cache is not None and not recarregar:
        return _cache

    usuario = {}
    if SETTINGS_FILE.is_file():
        try:
            usuario = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # Erro de digitacao no JSON nao pode derrubar o simulado inteiro.
            print(f"[CONFIG] settings.json invalido ({e}). Usando os valores padrao.")
            usuario = {}

    _cache = _mesclar(PADROES, usuario)
    return _cache


# --------------------------------------------------------------------------- #
# Atalhos: transformam as configuracoes em caminhos absolutos prontos para uso
# --------------------------------------------------------------------------- #
def pasta_conteudo(cfg=None):
    """Pasta onde o usuario joga os arquivos de estudo."""
    cfg = cfg or carregar()
    return (ROOT_DIR / cfg["conteudo"]["pasta"]).resolve()


def pasta_banco(cfg=None):
    """Pasta onde o ChromaDB guarda os vetores."""
    cfg = cfg or carregar()
    return (ROOT_DIR / cfg["rag"]["pasta_banco"]).resolve()


def api_key(cfg=None):
    """Le a chave da API da variavel de ambiente indicada no settings.json (.env)."""
    cfg = cfg or carregar()
    nome_var = cfg["llm"].get("api_key_env") or "OPENAI_API_KEY"
    chave = (os.environ.get(nome_var) or "").strip()
    # Evita que o texto de exemplo do .env.example passe como chave valida.
    if chave.lower().startswith("cole_sua_chave"):
        return ""
    return chave

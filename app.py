#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - Servidor do simulado (e o arquivo que voce roda).

O que ele faz, na ordem:
    1. le as configuracoes (config/settings.json);
    2. sincroniza a pasta `conteudo/` com a base vetorial - arquivo novo entra,
       arquivo apagado sai, arquivo editado e reprocessado;
    3. sobe um site local em http://127.0.0.1:5000;
    4. a cada lote, sorteia topicos, busca trechos do seu material, monta o
       prompt (config/prompt.md) e pede as questoes ao modelo de IA;
    5. grava cada resposta em historico.csv e usa esse historico para (a) nao
       repetir questoes e (b) cobrar mais os topicos em que voce vai pior.

Rode com:  .\\run.ps1      (ou:  python app.py)
"""

import csv
import copy
import json
import os
import random
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from rag import config as cfgmod
from rag import prompt_builder
from rag.generator import ErroDoModelo, Generator
from rag.indexer import Indexador
from rag.retriever import Retriever

# --------------------------------------------------------------------------- #
# Estado global do servidor
# --------------------------------------------------------------------------- #
CSV_CAMPOS = [
    "timestamp", "topico", "pergunta", "opcoes",
    "correta", "resposta_usuario", "acertou", "explicacao",
]

# Quantas questoes antigas entram no calculo de "ja perguntei isso?".
MAX_HISTORICO_SEMANTICO = 2000

_trava = threading.Lock()          # protege a escrita no CSV
_indexador = None
_retriever = None
_gerador = None
_erro_gerador = ""                 # mensagem amigavel se o modelo nao iniciou
_config_web = None                  # configuracao escolhida na interface
_inicializado = False
_init_lock = threading.Lock()

EXTENSOES_UPLOAD = {".pdf"}
app = Flask(__name__, template_folder=str(cfgmod.TEMPLATES_DIR))
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024

_perguntas_texto = []              # enunciados ja vistos (para o prompt)
_perguntas_norm = set()            # versao normalizada (comparacao exata)
_perguntas_vetores = None          # matriz numpy para comparacao semantica



def cfg_atual():
    """Configuracao ativa: settings.json como padrao, formulario como override."""
    return _config_web or cfgmod.carregar(recarregar=True)


def garantir_inicializado():
    # Gunicorn importa app.py sem executar __main__, por isso a inicializacao
    # precisa acontecer tambem no primeiro request em ambiente de producao.
    if not _inicializado:
        with _init_lock:
            if not _inicializado:
                inicializar()


# --------------------------------------------------------------------------- #
# Historico em CSV
# --------------------------------------------------------------------------- #
def _normalizar(texto):
    """Deixa o texto comparavel: minusculas e espacos colapsados."""
    return " ".join((texto or "").lower().split())


def garantir_csv():
    if not cfgmod.HISTORY_CSV.is_file():
        with open(cfgmod.HISTORY_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=CSV_CAMPOS).writeheader()


def ler_historico():
    """Devolve todas as linhas do historico como lista de dicionarios."""
    garantir_csv()
    try:
        with open(cfgmod.HISTORY_CSV, "r", newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except OSError as e:
        print("[HIST] Nao consegui ler o historico ({}).".format(e))
        return []


def salvar_resposta(registro):
    with _trava:
        garantir_csv()
        with open(cfgmod.HISTORY_CSV, "a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=CSV_CAMPOS).writerow(
                {campo: registro.get(campo, "") for campo in CSV_CAMPOS}
            )


def reiniciar_sessao_estudo():
    """Remove material e memoria de perguntas da sessao anterior."""
    global _perguntas_texto, _perguntas_norm, _perguntas_vetores
    pasta = cfgmod.pasta_conteudo(cfg_atual())
    pasta.mkdir(parents=True, exist_ok=True)

    # LEIA-ME.txt e mantido apenas como arquivo orientativo e ja e ignorado
    # pelo indexador. Todo o restante deixa de participar da nova atividade.
    for item in pasta.iterdir():
        if item.name.lower() == "leia-me.txt":
            continue
        if item.is_dir():
            import shutil
            shutil.rmtree(item)
        else:
            item.unlink()

    # Perguntas antigas nao podem ser enviadas ao prompt do novo PDF.
    with _trava:
        garantir_csv()
        with open(cfgmod.HISTORY_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=CSV_CAMPOS).writeheader()
    _perguntas_texto, _perguntas_norm, _perguntas_vetores = [], set(), None


def estatisticas():
    """Acertos, erros e desempenho por topico, lidos do historico."""
    acertos = erros = 0
    por_topico = {}

    for linha in ler_historico():
        certo = str(linha.get("acertou", "")).strip() in ("1", "True", "true", "sim")
        topico = (linha.get("topico") or "geral").strip() or "geral"
        dados = por_topico.setdefault(topico, {"total": 0, "acertos": 0})
        dados["total"] += 1
        if certo:
            acertos += 1
            dados["acertos"] += 1
        else:
            erros += 1

    total = acertos + erros
    return {
        "total": total,
        "acertos": acertos,
        "erros": erros,
        "percentual": round(100 * acertos / total) if total else 0,
        "por_topico": [
            {
                "topico": nome,
                "total": d["total"],
                "acertos": d["acertos"],
                "percentual": round(100 * d["acertos"] / d["total"]) if d["total"] else 0,
            }
            for nome, d in sorted(por_topico.items(), key=lambda kv: -kv[1]["total"])
        ],
    }


# --------------------------------------------------------------------------- #
# Controle de questoes repetidas
# --------------------------------------------------------------------------- #
def _vetorizar(textos):
    """Converte textos em vetores normalizados (mesmo modelo usado no RAG)."""
    if _retriever is None or not textos:
        return None
    try:
        vetores = np.array(_retriever.vetorizar(list(textos)), dtype="float32")
        normas = np.linalg.norm(vetores, axis=1, keepdims=True)
        normas[normas == 0] = 1.0
        return vetores / normas
    except Exception as e:
        print("[DEDUP] Nao consegui gerar os vetores ({}).".format(e))
        return None


def carregar_perguntas_anteriores():
    """Le o historico e prepara as estruturas de deteccao de repeticao."""
    global _perguntas_texto, _perguntas_norm, _perguntas_vetores

    _perguntas_texto, _perguntas_norm = [], set()
    for linha in ler_historico():
        pergunta = (linha.get("pergunta") or "").strip()
        if not pergunta:
            continue
        norma = _normalizar(pergunta)
        if norma in _perguntas_norm:
            continue
        _perguntas_norm.add(norma)
        _perguntas_texto.append(pergunta)

    if _perguntas_texto:
        print("[HIST] {} questao(oes) ja respondidas carregadas.".format(len(_perguntas_texto)))
        _perguntas_vetores = _vetorizar(_perguntas_texto[-MAX_HISTORICO_SEMANTICO:])


def _eh_repetida(pergunta, limiar):
    """
    True se a questao ja foi feita: comparacao exata (texto normalizado) ou
    semantica (o modelo reescreveu a mesma pergunta com outras palavras).
    """
    if _normalizar(pergunta) in _perguntas_norm:
        return True
    if _perguntas_vetores is None or len(_perguntas_vetores) == 0:
        return False

    vetor = _vetorizar([pergunta])
    if vetor is None:
        return False

    similaridade = float(np.max(_perguntas_vetores @ vetor[0]))
    if similaridade >= limiar:
        print("[DEDUP] Questao descartada (parecida demais, {:.2f}): {}...".format(
            similaridade, pergunta[:70]))
        return True
    return False


def _registrar_pergunta(pergunta):
    """Guarda a questao nova nas estruturas de controle."""
    global _perguntas_vetores

    _perguntas_norm.add(_normalizar(pergunta))
    _perguntas_texto.append(pergunta)

    vetor = _vetorizar([pergunta])
    if vetor is None:
        return
    if _perguntas_vetores is None or len(_perguntas_vetores) == 0:
        _perguntas_vetores = vetor
    else:
        _perguntas_vetores = np.vstack([_perguntas_vetores, vetor])


# --------------------------------------------------------------------------- #
# Escolha dos topicos da rodada
# --------------------------------------------------------------------------- #
def escolher_topicos(quantidade):
    """
    Sorteia os topicos da rodada dando preferencia aos menos praticados e aos de
    pior desempenho. Sempre devolve `quantidade` itens (repetindo se a base
    tiver poucos topicos).
    """
    disponiveis = _retriever.topicos() if _retriever else []
    if not disponiveis:
        return []

    # Peso = quantas vezes o topico ja apareceu, com desconto para quem vai mal.
    peso = {t: 0.0 for t in disponiveis}
    for item in estatisticas()["por_topico"]:
        if item["topico"] in peso:
            # errar muito puxa o peso para baixo => o topico volta antes
            peso[item["topico"]] = item["total"] * (0.5 + item["percentual"] / 100.0)

    ordenados = sorted(disponiveis, key=lambda t: (peso[t], random.random()))
    janela = ordenados[: quantidade + 2]  # pega os mais atrasados, com folga para sortear

    escolhidos = random.sample(janela, k=min(quantidade, len(janela)))
    while len(escolhidos) < quantidade:  # menos topicos que questoes: repete
        escolhidos.append(random.choice(disponiveis))
    random.shuffle(escolhidos)
    return escolhidos


# --------------------------------------------------------------------------- #
# Geracao de um lote de questoes
# --------------------------------------------------------------------------- #
def _validar_questao(bruta, numero_opcoes):
    """Confere o formato devolvido pelo modelo. Devolve a questao limpa ou None."""
    pergunta = (bruta.get("pergunta") or "").strip()
    opcoes = bruta.get("opcoes")
    indice = bruta.get("indice_correto")
    explicacao = (bruta.get("explicacao") or "").strip()
    topico = (bruta.get("topico") or "geral").strip()

    if not pergunta or not isinstance(opcoes, list):
        return None
    opcoes = [str(o).strip() for o in opcoes if str(o).strip()]
    if len(opcoes) != numero_opcoes:
        print("[GEN] Questao descartada: veio com {} alternativas (esperado {}).".format(
            len(opcoes), numero_opcoes))
        return None
    if not isinstance(indice, int) or not (0 <= indice < len(opcoes)):
        return None

    return {
        "topico": topico,
        "pergunta": pergunta,
        "opcoes": opcoes,
        "indice_correto": indice,
        "explicacao": explicacao,
    }


def gerar_lote():
    """
    Gera um lote completo de questoes ineditas.
    Devolve (lista_de_questoes, informacoes_extras).
    """
    cfg = cfg_atual()
    simulado = cfg["simulado"]
    quantidade = simulado["questoes_por_lote"]
    numero_opcoes = max(2, min(8, simulado["opcoes_por_questao"]))
    limiar = float(simulado.get("limiar_duplicata", 0.85))

    if _gerador is None:
        raise ErroDoModelo(_erro_gerador or "modelo de IA nao inicializado")

    topicos = escolher_topicos(quantidade)
    if not topicos and not simulado.get("permitir_sem_conteudo", False):
        raise ErroDoModelo(
            "A base de conhecimento esta vazia. Coloque seus arquivos (pdf, docx, "
            "txt, md, xlsx, csv...) dentro da pasta '{}' e clique em Atualizar base.".format(
                cfgmod.pasta_conteudo(cfg).name)
        )

    contexto, fontes = ("", [])
    if topicos:
        contexto, fontes = _retriever.montar_contexto(topicos)

    aprovadas = []
    tentativas = 0

    # Se o filtro anti-repeticao derrubar tudo, tentamos mais uma vez com outro
    # sorteio de trechos (o modelo tende a variar bastante com contexto novo).
    while not aprovadas and tentativas < 2:
        tentativas += 1
        mensagens, esquema = prompt_builder.montar(
            cfg, topicos, contexto, _perguntas_texto
        )
        resposta = _gerador.gerar_json(mensagens, esquema)
        brutas = resposta.get("questions") if isinstance(resposta, dict) else None

        for bruta in (brutas or []):
            questao = _validar_questao(bruta, numero_opcoes)
            if questao is None:
                continue
            if _eh_repetida(questao["pergunta"], limiar):
                continue
            aprovadas.append(questao)
            _registrar_pergunta(questao["pergunta"])

        if not aprovadas and topicos:
            contexto, fontes = _retriever.montar_contexto(topicos)

    extras = {
        "topicos": topicos,
        "arquivos_usados": sorted({f["arquivo"] for f in fontes if f.get("arquivo")}),
        "tokens": _gerador.ultimo_uso,
        "custo_estimado": _estimar_custo(cfg, _gerador.ultimo_uso),
    }
    return aprovadas, extras


def _estimar_custo(cfg, uso):
    """Custo aproximado da ultima chamada, em dolares, so para dar visibilidade."""
    if not uso:
        return None
    llm = cfg["llm"]
    entrada = llm.get("preco_entrada_por_milhao")
    saida = llm.get("preco_saida_por_milhao")
    if entrada is None or saida is None:
        return None
    total = (uso.get("entrada", 0) / 1e6) * entrada + (uso.get("saida", 0) / 1e6) * saida
    return round(total, 5)


# --------------------------------------------------------------------------- #
# Rotas do site
# --------------------------------------------------------------------------- #
@app.route("/")
def pagina_inicial():
    cfg = cfg_atual()
    return render_template(
        "index.html",
        titulo=cfg.get("titulo", "Simulado"),
        modelo=cfg["llm"]["modelo"],
    )


@app.route("/api/status")
def api_status():
    """Informacoes da base de conhecimento, mostradas no topo da tela."""
    garantir_inicializado()
    cfg = cfg_atual()
    info = _indexador.status() if _indexador else {}
    return jsonify({
        "titulo": cfg.get("titulo", "Simulado"),
        "modelo": cfg["llm"]["modelo"],
        "pasta_conteudo": str(cfgmod.pasta_conteudo(cfg)),
        "arquivos": info.get("arquivos", 0),
        "pedacos": info.get("pedacos", 0),
        "topicos": info.get("topicos", {}),
        "erros": info.get("erros", {}),
        "pronto": _gerador is not None,
        "aviso": _erro_gerador,
        "configuracao": {
            "questoes": cfg["simulado"]["questoes_por_lote"],
            "opcoes": cfg["simulado"]["opcoes_por_questao"],
            "dificuldade": cfg["simulado"].get("dificuldade", "variada"),
        },
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Le/aplica as opcoes escolhidas no site sem editar settings.json."""
    global _config_web, _gerador, _erro_gerador
    garantir_inicializado()
    if request.method == "GET":
        cfg = cfg_atual()
        return jsonify({
            "questoes": cfg["simulado"]["questoes_por_lote"],
            "opcoes": cfg["simulado"]["opcoes_por_questao"],
            "dificuldade": cfg["simulado"].get("dificuldade", "variada"),
            "foco": cfg["simulado"].get("foco", ""),
        })

    dados = request.get_json(silent=True) or {}
    try:
        questoes = max(1, min(30, int(dados.get("questoes", 8))))
        opcoes = max(2, min(8, int(dados.get("opcoes", 4))))
    except (TypeError, ValueError):
        return jsonify({"erro": "Quantidade de questoes/opcoes invalida."}), 400

    dificuldade = str(dados.get("dificuldade", "variada")).lower()
    if dificuldade not in {"facil", "media", "dificil", "variada"}:
        return jsonify({"erro": "Dificuldade invalida."}), 400

    foco = str(dados.get("foco", "")).strip()[:2000]

    cfg = copy.deepcopy(cfg_atual())
    cfg["simulado"]["questoes_por_lote"] = questoes
    cfg["simulado"]["opcoes_por_questao"] = opcoes
    cfg["simulado"]["dificuldade"] = dificuldade
    instrucoes = {
        "facil": "Use dificuldade facil, priorizando conceitos fundamentais e exemplos diretos.",
        "media": "Use dificuldade media, exigindo comparacoes e aplicacao dos conceitos.",
        "dificil": "Use dificuldade dificil, priorizando casos praticos, excecoes e raciocinio em varias etapas.",
        "variada": "Varie a dificuldade entre facil, media e dificil ao longo do lote.",
    }
    cfg["simulado"]["foco"] = foco
    partes_instrucoes = [instrucoes[dificuldade]]
    if foco:
        partes_instrucoes.append("Foco solicitado pelo estudante: {}".format(foco))
    cfg["simulado"]["instrucoes_extras"] = "\n".join(partes_instrucoes)
    _config_web = cfg

    # O Generator guarda a configuracao no construtor, entao ele precisa ser
    # recriado quando o usuario aplica novas opcoes.
    try:
        _gerador = Generator(cfg)
        _erro_gerador = ""
    except ErroDoModelo as e:
        _gerador = None
        _erro_gerador = str(e)
        return jsonify({"ok": False, "erro": _erro_gerador}), 400
    return jsonify({"ok": True, "config": {"questoes": questoes, "opcoes": opcoes, "dificuldade": dificuldade, "foco": foco}})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Recebe PDFs pela interface e coloca-os na base de conhecimento."""
    garantir_inicializado()
    arquivos = request.files.getlist("files") or ([request.files.get("file")] if request.files.get("file") else [])
    if not arquivos:
        return jsonify({"erro": "Selecione pelo menos um arquivo PDF."}), 400

    nomes = []
    for arquivo in arquivos:
        nome = secure_filename(arquivo.filename or "")
        if not nome or Path(nome).suffix.lower() not in EXTENSOES_UPLOAD:
            return jsonify({"erro": "Apenas arquivos PDF sao aceitos."}), 400
        nomes.append(nome)

    # Cada upload inicia uma nova sessão: PDFs, perguntas e histórico anterior
    # não são misturados com o material recém-enviado.
    reiniciar_sessao_estudo()
    destino = cfgmod.pasta_conteudo(cfg_atual()) / "uploads"
    destino.mkdir(parents=True, exist_ok=True)
    salvos = []
    for arquivo, nome in zip(arquivos, nomes):
        caminho = destino / nome
        arquivo.save(caminho)
        salvos.append(nome)

    try:
        resumo = _indexador.sincronizar(verbose=True)
        _retriever.recarregar()
    except Exception as e:
        return jsonify({"ok": False, "erro": "Upload salvo, mas a indexacao falhou: {}".format(e)}), 500
    return jsonify({"ok": True, "arquivos": salvos, "resumo": resumo, "status": _indexador.status()})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Botao 'Atualizar base': reprocessa a pasta de conteudo agora."""
    garantir_inicializado()
    dados = request.get_json(silent=True) or {}
    try:
        resumo = _indexador.sincronizar(forcar=bool(dados.get("rebuild")), verbose=True)
        _retriever.recarregar()
        return jsonify({"ok": True, "resumo": resumo, "status": _indexador.status()})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/questions")
def api_questions():
    """Gera e devolve o proximo lote de questoes."""
    garantir_inicializado()
    try:
        # Antes de gerar, confere se a pasta mudou (checagem rapida: so datas e
        # tamanhos). E isso que faz o arquivo novo entrar sozinho no simulado.
        if _indexador.ha_mudancas():
            print("[RAG] Mudancas detectadas na pasta de conteudo. Atualizando a base...")
            _indexador.sincronizar(verbose=True)
            _retriever.recarregar()

        questoes, extras = gerar_lote()
        if not questoes:
            return jsonify({
                "erro": "O modelo so devolveu questoes repetidas ou fora do formato. "
                        "Tente de novo ou adicione mais material na pasta de conteudo."
            }), 502
        return jsonify({"questions": questoes, "extras": extras})
    except ErroDoModelo as e:
        return jsonify({"erro": str(e)}), 400
    except Exception as e:
        return jsonify({"erro": "{}: {}".format(type(e).__name__, e)}), 500


@app.route("/api/answer", methods=["POST"])
def api_answer():
    """Recebe a resposta do usuario, corrige e grava no historico."""
    garantir_inicializado()
    dados = request.get_json(force=True, silent=True) or {}
    opcoes = dados.get("opcoes") or []
    indice_correto = dados.get("indice_correto", -1)
    indice_usuario = dados.get("resposta_usuario", -1)

    try:
        acertou = int(indice_usuario) == int(indice_correto)
    except (TypeError, ValueError):
        acertou = False

    def texto_da(indice):
        return opcoes[indice] if isinstance(opcoes, list) and 0 <= indice < len(opcoes) else ""

    salvar_resposta({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "topico": dados.get("topico", ""),
        "pergunta": dados.get("pergunta", ""),
        "opcoes": json.dumps(opcoes, ensure_ascii=False),
        "correta": texto_da(indice_correto),
        "resposta_usuario": texto_da(indice_usuario),
        "acertou": "1" if acertou else "0",
        "explicacao": dados.get("explicacao", ""),
    })
    return jsonify({"ok": True, "acertou": acertou, "stats": estatisticas()})


@app.route("/api/stats")
def api_stats():
    garantir_inicializado()
    return jsonify(estatisticas())


# --------------------------------------------------------------------------- #
# Inicializacao
# --------------------------------------------------------------------------- #
def inicializar():
    global _indexador, _retriever, _gerador, _erro_gerador, _config_web, _inicializado

    if _inicializado:
        return

    cfg = cfgmod.carregar(recarregar=True)
    _config_web = cfg
    print("=" * 70)
    print(" {}".format(cfg.get("titulo", "Simulado")))
    print("=" * 70)

    pasta = cfgmod.pasta_conteudo(cfg)
    pasta.mkdir(parents=True, exist_ok=True)
    print("[RAG] Pasta de conteudo: {}".format(pasta))

    # 1) Base vetorial sempre igual a pasta de conteudo.
    _indexador = Indexador(cfg)
    try:
        _indexador.sincronizar(verbose=True)
    except Exception as e:
        print("[RAG] Falha ao sincronizar a base ({}).".format(e))

    _retriever = Retriever(cfg, _indexador)
    _retriever.recarregar()

    info = _indexador.status()
    print("[RAG] {} arquivo(s), {} pedaco(s), {} topico(s).".format(
        info["arquivos"], info["pedacos"], len(info["topicos"])))
    for rel, erro in (info.get("erros") or {}).items():
        print("[RAG] Atencao - '{}' nao pode ser lido: {}".format(rel, erro))
    if info["arquivos"] == 0:
        print("[RAG] A pasta esta vazia! Coloque seus PDFs/DOCX/TXT... em {}".format(pasta))

    # 2) Historico e controle de repeticao.
    carregar_perguntas_anteriores()

    # 3) Modelo de IA.
    try:
        _gerador = Generator(cfg)
        print("[LLM] Modelo '{}' pronto.".format(cfg["llm"]["modelo"]))
    except ErroDoModelo as e:
        _erro_gerador = str(e)
        print("[LLM] {}".format(e))
    _inicializado = True


def main():
    inicializar()
    cfg = cfg_atual()
    servidor = cfg.get("servidor", {})
    host = os.getenv("HOST", servidor.get("host", "127.0.0.1"))
    porta = int(os.getenv("PORT", servidor.get("porta", 5000)))
    endereco = "http://{}:{}".format(host, porta)

    print("\nAbra no navegador: {}\n".format(endereco))
    if servidor.get("abrir_navegador", True) and host in {"127.0.0.1", "localhost"}:
        threading.Timer(1.5, lambda: webbrowser.open(endereco)).start()

    app.run(host=host, port=porta, debug=False)


if __name__ == "__main__":
    main()

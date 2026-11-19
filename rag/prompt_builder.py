# -*- coding: utf-8 -*-
"""
prompt_builder.py - Monta o prompt a partir do arquivo config/prompt.md.

O arquivo config/prompt.md e a "alma" do simulado e foi feito para ser editado
por qualquer pessoa, sem mexer em codigo. Ele tem duas secoes:

    ## PROMPT DO SISTEMA   -> as regras gerais do examinador
    ## PROMPT DO USUARIO   -> o pedido de cada rodada

Dentro do texto voce escreve variaveis entre chaves duplas ({{contexto}},
{{topicos}}, ...) e este modulo troca cada uma pelo valor real na hora de gerar
as questoes. Como o arquivo e lido a cada lote, editar e salvar ja basta - nao
precisa reiniciar o servidor.
"""

import re
from datetime import date

from . import config as cfgmod

# Cabecalho de secao do prompt.md (linhas comecando com "## ").
_SECAO = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Textos usados caso o config/prompt.md seja apagado ou fique sem alguma secao.
SISTEMA_PADRAO = (
    "Voce e um examinador experiente. Gere questoes de multipla escolha em {{idioma}} "
    "baseadas estritamente no conteudo fornecido. Uma unica alternativa correta por "
    "questao. Responda apenas com JSON valido."
)
USUARIO_PADRAO = (
    "Gere {{numero_questoes}} questoes de multipla escolha com {{numero_opcoes}} "
    "alternativas cada, sobre os topicos:\n{{topicos}}\n\n"
    "Nao repita estas questoes:\n{{perguntas_anteriores}}\n\n"
    "CONTEUDO:\n{{contexto}}\n\n{{formato_json}}"
)


def letras(quantidade):
    """Gera 'A, B, C, D, E' conforme o numero de alternativas configurado."""
    return ", ".join(chr(ord("A") + i) for i in range(quantidade))


def esquema_json(numero_opcoes):
    """
    Descreve o formato exato da resposta para o modo 'structured outputs' da
    OpenAI: com ele, o modelo fica proibido de devolver um JSON fora do padrao.
    """
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topico": {"type": "string"},
                        "pergunta": {"type": "string"},
                        "opcoes": {"type": "array", "items": {"type": "string"}},
                        "indice_correto": {"type": "integer"},
                        "explicacao": {"type": "string"},
                    },
                    "required": ["topico", "pergunta", "opcoes", "indice_correto", "explicacao"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


def texto_formato_json(numero_questoes, numero_opcoes):
    """Instrucao em texto do formato de saida (variavel {{formato_json}})."""
    exemplo_opcoes = ", ".join(
        '"texto da alternativa ' + chr(ord("A") + i) + '"' for i in range(numero_opcoes)
    )
    # Atencao: este texto tem chaves { } de verdade (e um exemplo de JSON), entao
    # nada de .format() aqui - as partes variaveis entram por concatenacao.
    modelo = (
        "Responda SOMENTE com um objeto JSON neste formato exato "
        "(sem markdown, sem texto antes ou depois):\n"
        "{\n"
        '  "questions": [\n'
        "    {\n"
        '      "topico": "nome do topico da lista acima",\n'
        '      "pergunta": "enunciado completo",\n'
        '      "opcoes": [' + exemplo_opcoes + "],\n"
        '      "indice_correto": 0,\n'
        '      "explicacao": "por que a correta esta certa e por que cada uma das outras esta errada"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    regra = (
        "A lista 'questions' deve ter exatamente " + str(numero_questoes)
        + " itens, e cada 'opcoes' exatamente " + str(numero_opcoes) + " textos."
    )
    return modelo + regra


def ler_arquivo_prompt():
    """
    Le o config/prompt.md e separa as duas secoes.
    Devolve (texto_do_sistema, texto_do_usuario).
    """
    if not cfgmod.PROMPT_FILE.is_file():
        print("[PROMPT] config/prompt.md nao encontrado; usando o prompt embutido.")
        return SISTEMA_PADRAO, USUARIO_PADRAO

    texto = cfgmod.PROMPT_FILE.read_text(encoding="utf-8")
    marcas = list(_SECAO.finditer(texto))

    sistema, usuario = "", ""
    for i, marca in enumerate(marcas):
        titulo = marca.group(1).strip().upper()
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        corpo = texto[marca.end():fim].strip()
        if "SISTEMA" in titulo:
            sistema = corpo
        elif "USU" in titulo:  # USUARIO / USUÁRIO
            usuario = corpo

    if not sistema:
        print("[PROMPT] Secao '## PROMPT DO SISTEMA' nao encontrada; usando o padrao.")
        sistema = SISTEMA_PADRAO
    if not usuario:
        print("[PROMPT] Secao '## PROMPT DO USUARIO' nao encontrada; usando o padrao.")
        usuario = USUARIO_PADRAO
    return sistema, usuario


def _substituir(texto, variaveis):
    """Troca cada {{variavel}} pelo valor correspondente."""
    for nome, valor in variaveis.items():
        texto = texto.replace("{{" + nome + "}}", str(valor))
    return texto


def montar(cfg, topicos, contexto, perguntas_anteriores, maximo_anteriores=15):
    """
    Monta as mensagens que serao enviadas ao modelo.

    Parametros:
        cfg                  -> configuracoes ja carregadas
        topicos              -> lista de topicos sorteados para esta rodada
        contexto             -> trechos do material vindos do RAG
        perguntas_anteriores -> questoes ja respondidas (para nao repetir)

    Devolve (mensagens, esquema_json) prontos para o Generator.
    """
    simulado = cfg["simulado"]
    numero_questoes = simulado["questoes_por_lote"]
    numero_opcoes = max(2, min(8, simulado["opcoes_por_questao"]))

    recentes = perguntas_anteriores[-maximo_anteriores:] if perguntas_anteriores else []

    if contexto and contexto.strip():
        contexto_final = contexto
    else:
        # Sem material indexado: so chega aqui se permitir_sem_conteudo = true.
        tema = simulado.get("tema_livre") or "conhecimentos gerais"
        contexto_final = (
            "(Nenhum material foi fornecido. Use seu proprio conhecimento sobre: {})".format(tema)
        )

    variaveis = {
        "numero_questoes": numero_questoes,
        "numero_opcoes": numero_opcoes,
        "letras": letras(numero_opcoes),
        "idioma": simulado.get("idioma", "portugues do Brasil"),
        "topicos": "\n".join("{}. {}".format(i, t) for i, t in enumerate(topicos, start=1)) or "- (livre)",
        "contexto": contexto_final,
        "perguntas_anteriores": "\n".join("- " + p for p in recentes) or "(nenhuma ainda)",
        "formato_json": texto_formato_json(numero_questoes, numero_opcoes),
        "instrucoes_extras": simulado.get("instrucoes_extras", ""),
        "data": date.today().strftime("%d/%m/%Y"),
        "titulo": cfg.get("titulo", "Simulado"),
    }

    # Variaveis inventadas pelo usuario no settings.json.
    for nome, valor in (cfg.get("variaveis_prompt") or {}).items():
        if not nome.startswith("_"):
            variaveis[nome] = valor

    sistema, usuario = ler_arquivo_prompt()
    mensagens = [
        {"role": "system", "content": _substituir(sistema, variaveis)},
        {"role": "user", "content": _substituir(usuario, variaveis)},
    ]
    return mensagens, esquema_json(numero_opcoes)

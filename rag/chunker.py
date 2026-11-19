# -*- coding: utf-8 -*-
"""
chunker.py - Quebra um texto longo em pedacos ("chunks") do tamanho certo.

Por que fatiar? O modelo de busca (embedding) so consegue comparar bem textos
curtos, e o modelo de IA nao pode receber um PDF inteiro no prompt. Entao cada
arquivo vira varios pedacos de ~300 tokens, e o RAG busca so os pedacos uteis.

Estrategia:
  1. o texto e dividido em SECOES pelos cabecalhos markdown (#, ##, ###);
  2. cada secao e dividida em BLOCOS (paragrafos e listas inteiras);
  3. os blocos sao agrupados ate chegar perto de `alvo_tokens`;
  4. um bloco do pedaco anterior e repetido no comeco do proximo (overlap), para
     que uma frase cortada no meio ainda apareca inteira em algum pedaco.

Nunca cortamos um paragrafo no meio - isso preserva o sentido do texto.
"""

import re

# Cabecalhos markdown de nivel 1 a 3 (# Titulo, ## Secao, ### Subsecao).
_CABECALHO = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

# Uma linha que comeca com "-", "*" ou "1." e item de lista.
_ITEM_LISTA = re.compile(r"^\s*([-*+]|\d+[.)])\s+")

# Contador de tokens: usa tiktoken se estiver instalado; senao, estima por
# caracteres (~4 caracteres por token), que e suficiente para fatiar texto.
try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")

    def contar_tokens(texto: str) -> int:
        return len(_ENCODER.encode(texto, disallowed_special=()))
except Exception:  # pragma: no cover - so acontece se o tiktoken faltar
    def contar_tokens(texto: str) -> int:
        return max(1, len(texto) // 4)


def _dividir_em_secoes(texto):
    """
    Separa o texto em secoes usando os cabecalhos markdown como fronteira.
    Devolve uma lista de (titulo_da_secao, corpo). Se o texto nao tiver nenhum
    cabecalho, devolve uma unica secao chamada "Conteudo".
    """
    marcas = list(_CABECALHO.finditer(texto))
    if not marcas:
        return [("Conteudo", texto)]

    secoes = []

    # Texto que aparece antes do primeiro cabecalho.
    inicio = texto[: marcas[0].start()].strip()
    if inicio:
        secoes.append(("Introducao", inicio))

    for i, marca in enumerate(marcas):
        titulo = marca.group(2).strip()
        comeco_corpo = marca.end()
        fim_corpo = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        corpo = texto[comeco_corpo:fim_corpo].strip()
        if corpo:
            secoes.append((titulo, corpo))
        elif i + 1 < len(marcas):
            # Cabecalho sem corpo (ex.: titulo seguido de subtitulo): ignora.
            continue

    return secoes or [("Conteudo", texto)]


def _dividir_em_blocos(corpo):
    """
    Divide o corpo de uma secao em blocos, usando a linha em branco como
    separador. Listas com varios itens seguidos viram um bloco unico, para nao
    separar um item do seu contexto.
    """
    paragrafos = [p.strip() for p in re.split(r"\n\s*\n", corpo) if p.strip()]

    blocos = []
    lista_em_andamento = []

    def fechar_lista():
        if lista_em_andamento:
            blocos.append("\n".join(lista_em_andamento))
            lista_em_andamento.clear()

    for paragrafo in paragrafos:
        linhas = [l for l in paragrafo.split("\n") if l.strip()]
        eh_lista = bool(linhas) and all(_ITEM_LISTA.match(l) for l in linhas)
        if eh_lista:
            lista_em_andamento.append(paragrafo)
        else:
            fechar_lista()
            blocos.append(paragrafo)
    fechar_lista()

    return blocos


def _quebrar_bloco_gigante(bloco, max_tokens):
    """
    Plano B para um paragrafo unico maior que o limite (acontece em PDFs sem
    quebra de linha): corta por frases ate caber.
    """
    frases = re.split(r"(?<=[.!?])\s+", bloco)
    pedacos = []
    atual = []
    tokens_atual = 0

    for frase in frases:
        tokens_frase = contar_tokens(frase)
        if tokens_atual + tokens_frase > max_tokens and atual:
            pedacos.append(" ".join(atual))
            atual, tokens_atual = [], 0
        atual.append(frase)
        tokens_atual += tokens_frase

    if atual:
        pedacos.append(" ".join(atual))
    return pedacos


def dividir(texto, alvo_tokens=300, max_tokens=450, overlap_blocos=1):
    """
    Funcao principal do modulo.

    Recebe o texto inteiro de um arquivo e devolve uma lista de dicionarios:
        {"secao": "titulo da secao", "texto": "...", "tokens": 287}
    """
    pedacos = []

    for titulo, corpo in _dividir_em_secoes(texto):
        blocos = _dividir_em_blocos(corpo)
        if not blocos:
            continue

        atual = []          # blocos que estao entrando no pedaco atual
        tokens_atual = 0

        def fechar():
            """Fecha o pedaco atual e guarda no resultado."""
            if atual:
                conteudo = "\n\n".join(atual)
                pedacos.append({
                    "secao": titulo,
                    "texto": conteudo,
                    "tokens": contar_tokens(conteudo),
                })

        for bloco in blocos:
            tokens_bloco = contar_tokens(bloco)

            # Bloco maior que o teto: fecha o que estava aberto e quebra por frases.
            if tokens_bloco > max_tokens:
                fechar()
                atual, tokens_atual = [], 0
                for parte in _quebrar_bloco_gigante(bloco, max_tokens):
                    pedacos.append({
                        "secao": titulo,
                        "texto": parte,
                        "tokens": contar_tokens(parte),
                    })
                continue

            # Encheu o pedaco: fecha e comeca outro repetindo o final do anterior.
            if atual and tokens_atual + tokens_bloco > alvo_tokens:
                fechar()
                atual = atual[-overlap_blocos:] if overlap_blocos > 0 else []
                tokens_atual = sum(contar_tokens(b) for b in atual)

            atual.append(bloco)
            tokens_atual += tokens_bloco

        fechar()

    # Descarta migalhas (cabecalhos soltos, linhas de rodape etc.).
    return [p for p in pedacos if p["tokens"] >= 12]

# -*- coding: utf-8 -*-
"""
retriever.py - Busca os trechos do seu material que vao alimentar o modelo.

Duas formas de uso:

1. `buscar("uma pergunta")`
   busca semantica classica: devolve os pedacos mais parecidos com a pergunta.

2. `montar_contexto(["Portugues", "Matematica"])`
   e o que o simulado usa. Em vez de sempre pegar "os mais parecidos" (o que
   geraria sempre as mesmas questoes), ele SORTEIA um trecho de cada topico e
   depois busca os vizinhos dele. Assim cada rodada cai em uma parte diferente
   do material e as questoes nao ficam repetitivas.

Teste pela linha de comando:
    python -m rag.retriever --query "o que e crase?"
"""

import random

from . import config as cfgmod
from .indexer import Indexador


class Retriever:
    def __init__(self, cfg=None, indexador=None):
        self.cfg = cfg or cfgmod.carregar()
        self.indexador = indexador or Indexador(self.cfg)
        self._ids_por_topico = None  # cache: {"topico": ["id1", "id2", ...]}

    # ------------------------------------------------------------------ #
    # Cache de topicos
    # ------------------------------------------------------------------ #
    def recarregar(self):
        """
        Relê do banco quais pedacos existem e a que topico pertencem.
        Chamado no inicio e sempre que a base e sincronizada.
        """
        self._ids_por_topico = {}
        try:
            dados = self.indexador.colecao().get(include=["metadatas"])
        except Exception as e:
            print("[RAG] Nao consegui listar a base ({}).".format(e))
            return self._ids_por_topico

        for identificador, meta in zip(dados.get("ids", []), dados.get("metadatas", [])):
            topico = (meta or {}).get("topico", "geral")
            self._ids_por_topico.setdefault(topico, []).append(identificador)
        return self._ids_por_topico

    def topicos(self):
        """Lista de topicos disponiveis na base (nomes das subpastas)."""
        if self._ids_por_topico is None:
            self.recarregar()
        return sorted(self._ids_por_topico.keys())

    def total_pedacos(self):
        if self._ids_por_topico is None:
            self.recarregar()
        return sum(len(v) for v in self._ids_por_topico.values())

    def vazio(self):
        return self.total_pedacos() == 0

    # ------------------------------------------------------------------ #
    # Busca semantica
    # ------------------------------------------------------------------ #
    def buscar(self, consulta, top_k=5, filtro=None):
        """Devolve os `top_k` pedacos mais parecidos com o texto da consulta."""
        try:
            resultado = self.indexador.colecao().query(
                query_texts=[consulta],
                n_results=top_k,
                where=filtro,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            print("[RAG] Erro na busca ({}).".format(e))
            return []

        documentos = (resultado.get("documents") or [[]])[0]
        metadados = (resultado.get("metadatas") or [[]])[0]
        distancias = (resultado.get("distances") or [[]])[0]
        identificadores = (resultado.get("ids") or [[]])[0]

        saida = []
        for i, texto in enumerate(documentos):
            saida.append({
                "id": identificadores[i] if i < len(identificadores) else "",
                "texto": texto,
                "meta": metadados[i] if i < len(metadados) else {},
                "distancia": distancias[i] if i < len(distancias) else None,
            })
        return saida

    def _pegar_por_id(self, identificador):
        try:
            dados = self.indexador.colecao().get(
                ids=[identificador], include=["documents", "metadatas"]
            )
        except Exception:
            return None
        documentos = dados.get("documents") or []
        if not documentos:
            return None
        metadados = dados.get("metadatas") or [{}]
        return {"id": identificador, "texto": documentos[0], "meta": metadados[0] or {}}

    # ------------------------------------------------------------------ #
    # Montagem do contexto que vai para o prompt
    # ------------------------------------------------------------------ #
    def montar_contexto(self, topicos, trechos_por_topico=None, vizinhos=None):
        """
        Monta o bloco de texto que sera colado no prompt, com trechos sorteados
        dos topicos pedidos. Devolve (texto_do_contexto, lista_de_fontes).
        """
        if self._ids_por_topico is None:
            self.recarregar()

        rag = self.cfg["rag"]
        trechos_por_topico = trechos_por_topico or rag.get("trechos_por_topico", 2)
        vizinhos = rag.get("vizinhos_por_trecho", 2) if vizinhos is None else vizinhos

        blocos = []
        fontes = []
        usados = set()  # evita repetir o mesmo pedaco duas vezes na mesma rodada

        for topico in topicos:
            disponiveis = [i for i in self._ids_por_topico.get(topico, []) if i not in usados]
            if not disponiveis:
                continue

            # 1) sorteia alguns trechos-semente deste topico
            sementes = random.sample(disponiveis, k=min(trechos_por_topico, len(disponiveis)))
            textos_do_topico = []

            for semente in sementes:
                pedaco = self._pegar_por_id(semente)
                if not pedaco:
                    continue
                usados.add(semente)
                textos_do_topico.append(pedaco)

                # 2) busca os trechos vizinhos (mesmo assunto) para dar contexto
                if vizinhos > 0:
                    for vizinho in self.buscar(
                        pedaco["texto"], top_k=vizinhos + 1, filtro={"topico": topico}
                    ):
                        if vizinho["id"] in usados:
                            continue
                        usados.add(vizinho["id"])
                        textos_do_topico.append(vizinho)

            if not textos_do_topico:
                continue

            partes = []
            for pedaco in textos_do_topico:
                meta = pedaco["meta"]
                origem = meta.get("nome", "?")
                secao = meta.get("secao", "")
                cabecalho = "[{}{}]".format(origem, " > " + secao if secao else "")
                partes.append("{}\n{}".format(cabecalho, pedaco["texto"]))
                fontes.append({"arquivo": meta.get("arquivo", ""), "topico": topico})

            blocos.append("### TOPICO: {}\n{}".format(topico, "\n\n".join(partes)))

        return "\n\n---\n\n".join(blocos), fontes

    # ------------------------------------------------------------------ #
    # Embeddings avulsos (usados para detectar questoes repetidas)
    # ------------------------------------------------------------------ #
    def vetorizar(self, textos):
        """Transforma uma lista de textos em vetores, com o mesmo modelo da base."""
        return self.indexador.embedding_function()(textos)


# --------------------------------------------------------------------------- #
# Linha de comando (util para conferir se a base esta boa)
# --------------------------------------------------------------------------- #
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Testa a busca na base vetorial.")
    parser.add_argument("--query", required=True, help="texto a procurar")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    retriever = Retriever()
    resultados = retriever.buscar(args.query, args.top_k)

    print("\n{} resultado(s) para: {}\n".format(len(resultados), args.query))
    for i, item in enumerate(resultados, start=1):
        meta = item["meta"]
        print("--- {} | {} | {} (distancia {:.4f})".format(
            i, meta.get("arquivo", "?"), meta.get("secao", ""), item["distancia"] or 0))
        print(item["texto"][:300].replace("\n", " ") + "...\n")


if __name__ == "__main__":
    main()

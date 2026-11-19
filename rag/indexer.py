# -*- coding: utf-8 -*-
"""
indexer.py - Mantem o banco vetorial sempre igual a pasta de conteudo.

Este e o coracao da parte "automatica" do projeto. Toda vez que o simulado roda,
ele compara a pasta `conteudo/` com um manifesto (.rag_state/manifest.json) que
registra quais arquivos ja entraram na base e em que versao:

    arquivo novo      -> e lido, fatiado e adicionado a base
    arquivo alterado  -> os pedacos antigos sao apagados e os novos entram
    arquivo apagado   -> os pedacos dele saem da base
    arquivo intocado  -> nao faz nada (rapido)

Para saber se um arquivo mudou usamos dois niveis de verificacao:
    1. tamanho + data de modificacao (instantaneo);
    2. so se isso mudou, calculamos o hash SHA-256 do conteudo (garante que
       um arquivo apenas "tocado", sem edicao real, nao seja reprocessado).

Uso pela linha de comando:
    python -m rag.indexer            # sincroniza
    python -m rag.indexer --status   # mostra o que ja esta na base
    python -m rag.indexer --rebuild  # apaga tudo e reconstroi do zero
"""

import fnmatch
import hashlib
import json
import shutil
import time
from pathlib import Path

from . import config as cfgmod
from . import chunker
from .loaders import EXTENSOES_SUPORTADAS, ErroDeLeitura, extrair_texto

# Quantos pedacos sao enviados por vez ao banco vetorial.
TAMANHO_LOTE = 100


def _hash_arquivo(caminho: Path) -> str:
    """SHA-256 do arquivo, lido em blocos para nao carregar tudo na memoria."""
    digest = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def _id_fonte(caminho_relativo: str) -> str:
    """
    Identificador curto e estavel de um arquivo, derivado do caminho.
    Serve para montar os IDs dos pedacos e para apaga-los depois.
    """
    return hashlib.sha1(caminho_relativo.encode("utf-8")).hexdigest()[:16]


class Indexador:
    """Cuida da base vetorial (ChromaDB) e da sincronizacao com a pasta."""

    def __init__(self, cfg=None):
        self.cfg = cfg or cfgmod.carregar()
        self.pasta_conteudo = cfgmod.pasta_conteudo(self.cfg)
        self.pasta_banco = cfgmod.pasta_banco(self.cfg)
        self.nome_colecao = self.cfg["rag"]["colecao"]
        self.modelo_embedding = self.cfg["rag"]["modelo_embedding"]
        self._colecao = None
        self._ef = None

    # ------------------------------------------------------------------ #
    # Conexao com o ChromaDB
    # ------------------------------------------------------------------ #
    def embedding_function(self):
        """
        Cria (uma unica vez) a funcao que transforma texto em vetores.
        Por padrao usa a API da OpenAI para evitar carregar PyTorch e um modelo
        sentence-transformers na memoria limitada do Render.
        """
        if self._ef is None:
            from chromadb.utils import embedding_functions
            provedor = (self.cfg["rag"].get("provedor_embedding") or "openai").lower()
            if provedor == "openai":
                chave = cfgmod.api_key(self.cfg)
                if not chave:
                    raise RuntimeError("OPENAI_API_KEY nao configurada para gerar embeddings.")
                llm = self.cfg.get("llm", {})
                parametros = {"api_key": chave, "model_name": self.modelo_embedding}
                if (llm.get("base_url") or "").strip():
                    parametros["api_base"] = llm["base_url"].strip()
                self._ef = embedding_functions.OpenAIEmbeddingFunction(**parametros)
            elif provedor == "local":
                # Compatibilidade opcional para execucao local, caso o usuario
                # reinstale sentence-transformers manualmente.
                self._ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name=self.modelo_embedding
                )
            else:
                raise RuntimeError("Provedor de embedding desconhecido: {}".format(provedor))
        return self._ef

    def colecao(self):
        """Abre (ou cria) a colecao do ChromaDB onde ficam os pedacos de texto."""
        if self._colecao is None:
            import chromadb
            from chromadb.config import Settings

            self.pasta_banco.mkdir(parents=True, exist_ok=True)
            cliente = chromadb.PersistentClient(
                path=str(self.pasta_banco),
                settings=Settings(anonymized_telemetry=False),
            )
            try:
                self._colecao = cliente.get_or_create_collection(
                    name=self.nome_colecao,
                    embedding_function=self.embedding_function(),
                    # cosseno e a metrica certa para embeddings de texto
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception:
                # Versoes mais novas do Chroma recusam metadata em colecao ja criada.
                self._colecao = cliente.get_or_create_collection(
                    name=self.nome_colecao,
                    embedding_function=self.embedding_function(),
                )
        return self._colecao

    # ------------------------------------------------------------------ #
    # Manifesto: a "memoria" do que ja foi indexado
    # ------------------------------------------------------------------ #
    def _ler_manifesto(self):
        if cfgmod.MANIFEST_FILE.is_file():
            try:
                return json.loads(cfgmod.MANIFEST_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # manifesto corrompido: trata como se nao existisse
        return {"modelo_embedding": self.modelo_embedding, "chunk": {}, "arquivos": {}}

    def _gravar_manifesto(self, manifesto):
        cfgmod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        cfgmod.MANIFEST_FILE.write_text(
            json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _parametros_chunk(self):
        r = self.cfg["rag"]
        return {
            "alvo": r["chunk_alvo_tokens"],
            "max": r["chunk_max_tokens"],
            "overlap": r["chunk_overlap_paragrafos"],
        }

    # ------------------------------------------------------------------ #
    # Leitura da pasta de conteudo
    # ------------------------------------------------------------------ #
    def _deve_ignorar(self, caminho_relativo: Path) -> bool:
        """Aplica os padroes de `conteudo.ignorar` a cada parte do caminho."""
        padroes = self.cfg["conteudo"].get("ignorar") or []
        for parte in caminho_relativo.parts:
            for padrao in padroes:
                if fnmatch.fnmatch(parte, padrao):
                    return True
        return False

    def _topico_de(self, caminho_relativo: Path) -> str:
        """
        Define o topico do arquivo: nome da subpasta (padrao) ou nome do arquivo.
        Arquivo solto na raiz da pasta sempre usa o proprio nome.
        """
        if self.cfg["conteudo"].get("topico_por") == "arquivo":
            return caminho_relativo.stem
        if len(caminho_relativo.parts) > 1:
            return caminho_relativo.parts[0]
        return caminho_relativo.stem

    def escanear(self):
        """
        Varre a pasta de conteudo e devolve
        {caminho_relativo: {"tamanho": bytes, "mtime": timestamp}}.
        """
        encontrados = {}
        if not self.pasta_conteudo.is_dir():
            return encontrados

        for caminho in sorted(self.pasta_conteudo.rglob("*")):
            if not caminho.is_file():
                continue
            if caminho.suffix.lower() not in EXTENSOES_SUPORTADAS:
                continue
            relativo = caminho.relative_to(self.pasta_conteudo)
            if self._deve_ignorar(relativo):
                continue
            info = caminho.stat()
            encontrados[relativo.as_posix()] = {
                "tamanho": info.st_size,
                "mtime": round(info.st_mtime, 3),
            }
        return encontrados

    # ------------------------------------------------------------------ #
    # Verificacao rapida (usada antes de cada lote de questoes)
    # ------------------------------------------------------------------ #
    def ha_mudancas(self):
        """
        Diz, em milissegundos, se a pasta mudou desde a ultima sincronizacao.
        Compara apenas nomes, tamanhos e datas - nao abre nenhum arquivo.
        """
        manifesto = self._ler_manifesto()

        # Trocar o modelo de embedding ou o tamanho dos pedacos invalida a base.
        if manifesto.get("modelo_embedding") != self.modelo_embedding:
            return True
        if manifesto.get("chunk") != self._parametros_chunk():
            return True

        anteriores = manifesto.get("arquivos", {})
        atuais = self.escanear()

        if set(anteriores) != set(atuais):
            return True
        for rel, info in atuais.items():
            antigo = anteriores[rel]
            if antigo.get("tamanho") != info["tamanho"] or antigo.get("mtime") != info["mtime"]:
                return True
        return False

    # ------------------------------------------------------------------ #
    # Sincronizacao
    # ------------------------------------------------------------------ #
    def _remover_da_base(self, sid):
        """Apaga do banco vetorial todos os pedacos de um arquivo."""
        try:
            self.colecao().delete(where={"sid": sid})
        except Exception as e:
            print("[RAG] Aviso: falha ao remover pedacos antigos ({}).".format(e))

    def _indexar_arquivo(self, rel, manifesto, verbose=True):
        """
        Le, fatia e grava um arquivo na base. Devolve (quantidade_de_pedacos, erro).
        """
        caminho = self.pasta_conteudo / rel
        relativo = Path(rel)
        sid = _id_fonte(rel)
        topico = self._topico_de(relativo)
        parametros = self._parametros_chunk()

        try:
            texto = extrair_texto(caminho)
        except ErroDeLeitura as e:
            return 0, str(e)

        pedacos = chunker.dividir(
            texto,
            alvo_tokens=parametros["alvo"],
            max_tokens=parametros["max"],
            overlap_blocos=parametros["overlap"],
        )
        if not pedacos:
            return 0, "nao gerou nenhum pedaco de texto util"

        ids, documentos, metadados = [], [], []
        for i, pedaco in enumerate(pedacos):
            ids.append("{}#{:05d}".format(sid, i))
            documentos.append(pedaco["texto"])
            metadados.append({
                "sid": sid,
                "arquivo": rel,
                "nome": relativo.name,
                "topico": topico,
                "secao": pedaco["secao"][:200],
                "ordem": i,
                "tokens": pedaco["tokens"],
            })

        colecao = self.colecao()
        for inicio in range(0, len(ids), TAMANHO_LOTE):
            fim = inicio + TAMANHO_LOTE
            colecao.upsert(
                ids=ids[inicio:fim],
                documents=documentos[inicio:fim],
                metadatas=metadados[inicio:fim],
            )

        info = caminho.stat()
        manifesto["arquivos"][rel] = {
            "hash": _hash_arquivo(caminho),
            "tamanho": info.st_size,
            "mtime": round(info.st_mtime, 3),
            "chunks": len(pedacos),
            "topico": topico,
            "status": "ok",
            "erro": "",
        }
        return len(pedacos), None

    def sincronizar(self, forcar=False, verbose=True):
        """
        Deixa a base identica a pasta de conteudo.

        forcar=True apaga a base inteira e reindexa tudo (use quando trocar o
        modelo de embedding ou desconfiar da base).

        Devolve um resumo com as listas de arquivos novos, alterados, removidos
        e com erro - o servidor usa isso para avisar na tela.
        """
        comeco = time.time()
        ja_existia = cfgmod.MANIFEST_FILE.is_file()
        manifesto = self._ler_manifesto()

        # A base precisa ser reconstruida se a "receita" mudou.
        mudou_modelo = manifesto.get("modelo_embedding") != self.modelo_embedding
        mudou_chunk = manifesto.get("chunk") != self._parametros_chunk()
        reconstruir = forcar or mudou_modelo or mudou_chunk

        # So avisa sobre mudanca de configuracao se ja havia uma base antes
        # (na primeira execucao nao ha nada para reconstruir).
        if verbose and ja_existia and not forcar and (mudou_modelo or mudou_chunk):
            print("[RAG] Configuracao de indexacao mudou: reconstruindo a base do zero.")

        if reconstruir:
            self._apagar_base()
            manifesto = {
                "modelo_embedding": self.modelo_embedding,
                "chunk": self._parametros_chunk(),
                "arquivos": {},
            }

        anteriores = manifesto.setdefault("arquivos", {})
        atuais = self.escanear()

        # Base vazia (usuario apagou a pasta .chroma_db) mas manifesto cheio.
        if not reconstruir and anteriores:
            try:
                if self.colecao().count() == 0:
                    if verbose:
                        print("[RAG] Base vetorial vazia: reindexando todos os arquivos.")
                    manifesto["arquivos"] = {}
                    # precisa continuar apontando para o MESMO dicionario do manifesto
                    anteriores = manifesto["arquivos"]
            except Exception:
                pass

        removidos = [rel for rel in anteriores if rel not in atuais]
        novos, alterados = [], []

        for rel, info in atuais.items():
            antigo = anteriores.get(rel)
            if antigo is None:
                novos.append(rel)
                continue
            # Passo 1 (rapido): tamanho e data batem? entao nada mudou.
            if antigo.get("tamanho") == info["tamanho"] and antigo.get("mtime") == info["mtime"]:
                continue
            # Passo 2 (seguro): confere o conteudo de verdade.
            try:
                hash_atual = _hash_arquivo(self.pasta_conteudo / rel)
            except OSError:
                continue
            if hash_atual != antigo.get("hash"):
                alterados.append(rel)
            else:
                # Mesmo conteudo, data diferente: so atualiza o registro.
                antigo["mtime"] = info["mtime"]
                antigo["tamanho"] = info["tamanho"]

        # ---- aplica as remocoes ---------------------------------------- #
        for rel in removidos:
            self._remover_da_base(_id_fonte(rel))
            anteriores.pop(rel, None)
            if verbose:
                print("[RAG] - removido: {}".format(rel))

        # ---- aplica inclusoes e atualizacoes ---------------------------- #
        erros = {}
        total_pedacos_novos = 0
        pendentes = novos + alterados

        for posicao, rel in enumerate(pendentes, start=1):
            if rel in alterados:
                self._remover_da_base(_id_fonte(rel))  # tira a versao antiga
            if verbose:
                print("[RAG] ({}/{}) indexando: {}".format(posicao, len(pendentes), rel))

            quantidade, erro = self._indexar_arquivo(rel, manifesto, verbose)
            if erro:
                erros[rel] = erro
                # Registra o erro com o hash atual para nao tentar de novo em
                # toda execucao - so tenta outra vez se o arquivo mudar.
                try:
                    info = (self.pasta_conteudo / rel).stat()
                    anteriores[rel] = {
                        "hash": _hash_arquivo(self.pasta_conteudo / rel),
                        "tamanho": info.st_size,
                        "mtime": round(info.st_mtime, 3),
                        "chunks": 0,
                        "topico": self._topico_de(Path(rel)),
                        "status": "erro",
                        "erro": erro,
                    }
                except OSError:
                    pass
                if verbose:
                    print("[RAG]   ! falhou: {}".format(erro))
            else:
                total_pedacos_novos += quantidade
                if verbose:
                    print("[RAG]   {} pedacos".format(quantidade))

        manifesto["modelo_embedding"] = self.modelo_embedding
        manifesto["chunk"] = self._parametros_chunk()
        self._gravar_manifesto(manifesto)

        resumo = {
            "novos": novos,
            "alterados": alterados,
            "removidos": removidos,
            "erros": erros,
            "reconstruido": reconstruir,
            "pedacos_adicionados": total_pedacos_novos,
            "arquivos_na_base": sum(
                1 for v in manifesto["arquivos"].values() if v.get("status") == "ok"
            ),
            "pedacos_na_base": sum(
                v.get("chunks", 0) for v in manifesto["arquivos"].values()
            ),
            "segundos": round(time.time() - comeco, 1),
        }

        if verbose:
            if not (novos or alterados or removidos):
                print("[RAG] Base ja estava em dia ({} arquivos).".format(resumo["arquivos_na_base"]))
            else:
                print("[RAG] Sincronizacao concluida em {}s: +{} novos, {} alterados, {} removidos.".format(
                    resumo["segundos"], len(novos), len(alterados), len(removidos)))
        return resumo

    def _apagar_base(self):
        """
        Zera o banco vetorial (usado na reconstrucao total).
        Apagamos a colecao pelo proprio ChromaDB em vez de deletar a pasta: no
        Windows os arquivos ficam travados enquanto o processo esta aberto.
        """
        self._colecao = None
        if not self.pasta_banco.exists():
            return
        try:
            import chromadb
            from chromadb.config import Settings

            cliente = chromadb.PersistentClient(
                path=str(self.pasta_banco),
                settings=Settings(anonymized_telemetry=False),
            )
            try:
                cliente.delete_collection(self.nome_colecao)
            except Exception:
                pass  # a colecao ainda nao existia
        except Exception as e:
            # Plano B: apaga a pasta inteira (so funciona se nada estiver usando).
            print("[RAG] Limpando a base pelo disco ({}).".format(e))
            try:
                shutil.rmtree(self.pasta_banco)
            except OSError as erro:
                print("[RAG] Nao consegui apagar {} ({}).".format(self.pasta_banco, erro))

    # ------------------------------------------------------------------ #
    # Consultas de apoio
    # ------------------------------------------------------------------ #
    def status(self):
        """Resumo da base atual: arquivos, pedacos, topicos e erros."""
        manifesto = self._ler_manifesto()
        arquivos = manifesto.get("arquivos", {})
        topicos = {}
        for dados in arquivos.values():
            if dados.get("status") != "ok":
                continue
            topico = dados.get("topico", "geral")
            topicos[topico] = topicos.get(topico, 0) + dados.get("chunks", 0)
        return {
            "arquivos": sum(1 for d in arquivos.values() if d.get("status") == "ok"),
            "pedacos": sum(d.get("chunks", 0) for d in arquivos.values()),
            "topicos": dict(sorted(topicos.items())),
            "erros": {rel: d.get("erro") for rel, d in arquivos.items() if d.get("status") == "erro"},
            "modelo_embedding": manifesto.get("modelo_embedding", self.modelo_embedding),
            "pasta": str(self.pasta_conteudo),
        }


# --------------------------------------------------------------------------- #
# Linha de comando
# --------------------------------------------------------------------------- #
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sincroniza a pasta de conteudo com a base vetorial.")
    parser.add_argument("--status", action="store_true", help="mostra o que ja esta indexado e sai")
    parser.add_argument("--rebuild", action="store_true", help="apaga a base e reindexa tudo")
    args = parser.parse_args()

    indexador = Indexador()

    if args.status:
        info = indexador.status()
        print("Pasta de conteudo : {}".format(info["pasta"]))
        print("Arquivos indexados: {}".format(info["arquivos"]))
        print("Pedacos na base   : {}".format(info["pedacos"]))
        print("Modelo de busca   : {}".format(info["modelo_embedding"]))
        if info["topicos"]:
            print("\nTopicos:")
            for topico, quantidade in info["topicos"].items():
                print("  - {}: {} pedacos".format(topico, quantidade))
        if info["erros"]:
            print("\nArquivos com problema:")
            for rel, erro in info["erros"].items():
                print("  - {}: {}".format(rel, erro))
        return

    indexador.sincronizar(forcar=args.rebuild, verbose=True)


if __name__ == "__main__":
    main()

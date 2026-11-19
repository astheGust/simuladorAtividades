# -*- coding: utf-8 -*-
"""
generator.py - Conversa com o modelo de IA e devolve JSON ja validado.

O modelo padrao e o gpt-5.6-luna (o mais barato da OpenAI). Atencao a um detalhe
importante: a familia gpt-5.6 NAO aceita `temperature`, `top_p` nem as
penalidades, e usa `max_completion_tokens` no lugar de `max_tokens`. Por isso:

  - no config/settings.json esses campos vem como null (= nao enviar);
  - se mesmo assim a API reclamar de algum parametro, este modulo REMOVE o
    parametro citado no erro e tenta de novo sozinho.

Esse mesmo cuidado faz o codigo continuar funcionando se voce trocar o modelo
por um gpt-4o-mini, por um DeepSeek ou por qualquer API compativel com a da
OpenAI (basta preencher `base_url` no settings.json).
"""

import json
import re

from . import config as cfgmod

# Parametros que podem ser recusados dependendo do modelo. Se a API reclamar de
# um deles, tiramos da requisicao e tentamos novamente.
PARAMETROS_OPCIONAIS = [
    "reasoning_effort",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "max_completion_tokens",
    "max_tokens",
]


class ErroDoModelo(Exception):
    """Falha ao falar com a API ou ao interpretar a resposta."""


class Generator:
    def __init__(self, cfg=None):
        self.cfg = cfg or cfgmod.carregar()
        self.llm = self.cfg["llm"]
        self.modelo = self.llm["modelo"]
        self.ultimo_uso = {}  # tokens gastos na ultima chamada

        chave = cfgmod.api_key(self.cfg)
        if not chave:
            raise ErroDoModelo(
                "A chave da API nao foi encontrada. Crie o arquivo .env na raiz do "
                "projeto com a linha:  {}=sk-...".format(self.llm.get("api_key_env", "OPENAI_API_KEY"))
            )

        try:
            from openai import OpenAI
        except ImportError:
            raise ErroDoModelo("biblioteca 'openai' nao instalada (rode o setup.ps1)")

        parametros_cliente = {
            "api_key": chave,
            "timeout": self.llm.get("timeout_segundos", 180),
        }
        # base_url vazio = API oficial da OpenAI.
        if (self.llm.get("base_url") or "").strip():
            parametros_cliente["base_url"] = self.llm["base_url"].strip()

        self.cliente = OpenAI(**parametros_cliente)

    # ------------------------------------------------------------------ #
    # Montagem dos parametros da chamada
    # ------------------------------------------------------------------ #
    def _parametros(self):
        """Pega do settings.json so os parametros que nao estao em null."""
        parametros = {}
        for nome in PARAMETROS_OPCIONAIS:
            valor = self.llm.get(nome)
            if valor is not None and valor != "":
                parametros[nome] = valor
        return parametros

    @staticmethod
    def _parametro_recusado(mensagem_erro):
        """
        Le a mensagem de erro da API e descobre QUAL parametro foi recusado.
        Ex.: "Unsupported parameter: 'temperature' is not supported with this model"
        """
        texto = str(mensagem_erro).lower()
        for nome in PARAMETROS_OPCIONAIS:
            if nome in texto and any(
                pista in texto
                for pista in ("unsupported", "not supported", "unknown", "invalid", "unrecognized")
            ):
                return nome
        return None

    # ------------------------------------------------------------------ #
    # Chamada com tentativas de recuperacao
    # ------------------------------------------------------------------ #
    def _chamar(self, mensagens, formato_resposta, parametros):
        requisicao = {"model": self.modelo, "messages": mensagens}
        requisicao.update(parametros)
        if formato_resposta is not None:
            requisicao["response_format"] = formato_resposta

        resposta = self.cliente.chat.completions.create(**requisicao)

        uso = getattr(resposta, "usage", None)
        if uso is not None:
            self.ultimo_uso = {
                "entrada": getattr(uso, "prompt_tokens", 0),
                "saida": getattr(uso, "completion_tokens", 0),
            }

        escolha = resposta.choices[0]
        conteudo = (escolha.message.content or "").strip()

        if not conteudo:
            motivo = getattr(escolha, "finish_reason", "")
            if motivo == "length":
                raise ErroDoModelo(
                    "o modelo ficou sem espaco para responder. Aumente "
                    "'max_completion_tokens' no config/settings.json ou diminua "
                    "'questoes_por_lote'."
                )
            raise ErroDoModelo("o modelo devolveu uma resposta vazia (motivo: {}).".format(motivo))
        return conteudo

    def gerar_json(self, mensagens, esquema=None):
        """
        Envia as mensagens ao modelo e devolve a resposta ja convertida em dict.

        Tenta, em ordem, tres formas de pedir JSON, caindo para a proxima se o
        modelo/API nao suportar a anterior:
            1. json_schema (o modelo e obrigado a seguir o formato exato);
            2. json_object (o modelo so promete devolver JSON valido);
            3. nenhum formato (extraimos o JSON do meio do texto).
        """
        formatos = []
        if esquema is not None and self.llm.get("usar_json_schema", True):
            formatos.append({
                "type": "json_schema",
                "json_schema": {"name": "simulado", "strict": True, "schema": esquema},
            })
        formatos.append({"type": "json_object"})
        formatos.append(None)

        parametros = self._parametros()
        ultimo_erro = None

        for formato in formatos:
            # Para cada formato, damos algumas chances removendo parametros recusados.
            for _ in range(len(PARAMETROS_OPCIONAIS) + 1):
                try:
                    conteudo = self._chamar(mensagens, formato, parametros)
                    return self._interpretar_json(conteudo)
                except ErroDoModelo:
                    raise
                except Exception as e:
                    ultimo_erro = e
                    recusado = self._parametro_recusado(e)
                    if recusado and recusado in parametros:
                        print("[LLM] O modelo nao aceita '{}'; removendo e tentando de novo.".format(recusado))
                        parametros.pop(recusado)
                        continue
                    break  # nao foi parametro: tenta o proximo formato de resposta

        raise ErroDoModelo("falha ao chamar o modelo '{}': {}".format(self.modelo, ultimo_erro))

    # ------------------------------------------------------------------ #
    # Interpretacao da resposta
    # ------------------------------------------------------------------ #
    @staticmethod
    def _interpretar_json(conteudo):
        """Converte o texto devolvido pelo modelo em dicionario Python."""
        texto = conteudo.strip()

        # Remove cercas de markdown (```json ... ```) se o modelo insistir em usar.
        if texto.startswith("```"):
            texto = re.sub(r"^```[a-zA-Z]*\s*", "", texto)
            texto = re.sub(r"\s*```$", "", texto)

        try:
            return json.loads(texto)
        except json.JSONDecodeError:
            pass

        # Ultimo recurso: pega do primeiro "{" ate o ultimo "}".
        inicio, fim = texto.find("{"), texto.rfind("}")
        if inicio != -1 and fim > inicio:
            try:
                return json.loads(texto[inicio:fim + 1])
            except json.JSONDecodeError as e:
                raise ErroDoModelo("o modelo devolveu um JSON invalido ({}).".format(e))
        raise ErroDoModelo("o modelo nao devolveu JSON. Resposta: {}...".format(texto[:200]))

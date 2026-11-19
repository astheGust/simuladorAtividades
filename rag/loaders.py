# -*- coding: utf-8 -*-
"""
loaders.py - Le o texto de dentro de cada tipo de arquivo.

O resto do programa nao precisa saber se o arquivo e PDF, Word ou Excel: chama
`extrair_texto(caminho)` e recebe texto puro de volta.

Para adicionar um formato novo:
    1. escreva uma funcao _ler_xxx(caminho) que devolve string;
    2. registre a extensao no dicionario LEITORES la embaixo.
"""

import csv
import io
import json
from pathlib import Path

# Todas as extensoes que o simulador reconhece (usado tambem pelo indexador).
EXTENSOES_SUPORTADAS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json",
    ".pdf",
    ".docx", ".doc",
    ".xlsx", ".xlsm", ".xls",
    ".csv", ".tsv",
    ".pptx",
    ".html", ".htm",
}

# Planilhas gigantes viram texto gigante: este teto evita travar a maquina.
MAX_LINHAS_TABELA = 20000


class ErroDeLeitura(Exception):
    """Levantado quando o arquivo existe mas nao deu para extrair o texto."""


# --------------------------------------------------------------------------- #
# Texto puro (.txt, .md, .rst, .log)
# --------------------------------------------------------------------------- #
def _ler_bytes_como_texto(dados):
    """
    Decodifica bytes em texto tentando, em ordem: UTF-8, deteccao automatica
    (charset-normalizer) e, por ultimo, latin-1 (que nunca falha).
    """
    try:
        return dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        melhor = from_bytes(dados).best()
        if melhor is not None:
            return str(melhor)
    except Exception:
        pass
    return dados.decode("latin-1", errors="replace")


def _ler_texto(caminho: Path) -> str:
    return _ler_bytes_como_texto(caminho.read_bytes())


def _ler_json(caminho: Path) -> str:
    """JSON vira texto indentado: as chaves ajudam o modelo a entender o dado."""
    bruto = _ler_texto(caminho)
    try:
        return json.dumps(json.loads(bruto), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return bruto  # JSON quebrado: indexa como texto mesmo


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _ler_pdf(caminho: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ErroDeLeitura("biblioteca 'pypdf' nao instalada (pip install pypdf)")

    leitor = PdfReader(str(caminho))
    if getattr(leitor, "is_encrypted", False):
        try:
            leitor.decrypt("")  # tenta abrir PDF protegido com senha vazia
        except Exception:
            raise ErroDeLeitura("PDF protegido por senha")

    partes = []
    for numero, pagina in enumerate(leitor.pages, start=1):
        try:
            texto = pagina.extract_text() or ""
        except Exception:
            texto = ""
        if texto.strip():
            # A marcacao de pagina ajuda a saber de onde veio a informacao.
            partes.append("[Pagina {}]\n{}".format(numero, texto.strip()))

    if not partes:
        raise ErroDeLeitura(
            "nenhum texto encontrado (PDF provavelmente e digitalizado/imagem; "
            "seria necessario OCR)"
        )
    return "\n\n".join(partes)


# --------------------------------------------------------------------------- #
# Word (.docx e .doc)
# --------------------------------------------------------------------------- #
def _ler_docx(caminho: Path) -> str:
    try:
        import docx
    except ImportError:
        raise ErroDeLeitura("biblioteca 'python-docx' nao instalada")

    documento = docx.Document(str(caminho))
    partes = []

    for paragrafo in documento.paragraphs:
        texto = paragrafo.text.strip()
        if not texto:
            continue
        # Titulos do Word viram cabecalhos markdown, o que ajuda o fatiador
        # a quebrar o documento em secoes com sentido.
        estilo = (paragrafo.style.name or "").lower()
        if estilo.startswith("heading") or estilo.startswith("titulo"):
            partes.append("## " + texto)
        else:
            partes.append(texto)

    for tabela in documento.tables:
        linhas = []
        for linha in tabela.rows:
            celulas = [c.text.strip().replace("\n", " ") for c in linha.cells]
            if any(celulas):
                linhas.append(" | ".join(celulas))
        if linhas:
            partes.append("\n".join(linhas))

    return "\n\n".join(partes)


def _ler_doc(caminho: Path) -> str:
    """
    .doc e o formato binario antigo do Word. Nao existe biblioteca Python pura que
    leia bem, entao usamos o proprio Word instalado no Windows (via pywin32) para
    converter o arquivo em texto.
    """
    try:
        import pythoncom  # vem junto com o pywin32
        import win32com.client
    except ImportError:
        raise ErroDeLeitura(
            "para ler .doc e preciso o Microsoft Word instalado + pywin32. "
            "Alternativa: abra o arquivo e salve como .docx"
        )

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        documento = word.Documents.Open(str(caminho), ReadOnly=True)
        texto = documento.Content.Text
        documento.Close(False)
        return texto
    except Exception as e:
        raise ErroDeLeitura("falha ao abrir o .doc pelo Word ({}). Salve como .docx".format(e))
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------- #
# Tabelas (Excel e CSV)
# --------------------------------------------------------------------------- #
def _formatar_tabela(nome_aba, linhas):
    """
    Transforma uma tabela em frases. Em vez de "Joao | 30 | SP" (que o modelo nao
    entende sozinho), gera "nome: Joao; idade: 30; estado: SP", usando a primeira
    linha como cabecalho.
    """
    linhas = [l for l in linhas if any(str(c).strip() for c in l)]
    if not linhas:
        return ""

    cabecalho = [str(c).strip() for c in linhas[0]]
    preenchidas = [c for c in cabecalho if c]
    # So tratamos a primeira linha como cabecalho se ela parecer texto (e nao dados).
    tem_cabecalho = bool(preenchidas) and all(
        not c.replace(".", "").replace(",", "").replace("-", "").isdigit()
        for c in preenchidas
    )

    saida = ["## " + nome_aba] if nome_aba else []
    if tem_cabecalho:
        for linha in linhas[1:]:
            pares = []
            for i, celula in enumerate(linha):
                valor = str(celula).strip()
                if not valor or valor.lower() == "none":
                    continue
                titulo = cabecalho[i] if i < len(cabecalho) and cabecalho[i] else "col{}".format(i + 1)
                pares.append("{}: {}".format(titulo, valor))
            if pares:
                saida.append("- " + "; ".join(pares))
    else:
        for linha in linhas:
            celulas = [str(c).strip() for c in linha if str(c).strip()]
            if celulas:
                saida.append("- " + " | ".join(celulas))

    return "\n".join(saida)


def _ler_xlsx(caminho: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        raise ErroDeLeitura("biblioteca 'openpyxl' nao instalada")

    planilha = openpyxl.load_workbook(str(caminho), read_only=True, data_only=True)
    blocos = []
    try:
        for aba in planilha.worksheets:
            linhas = []
            for i, linha in enumerate(aba.iter_rows(values_only=True)):
                if i >= MAX_LINHAS_TABELA:
                    linhas.append(["(arquivo truncado)"])
                    break
                linhas.append(["" if c is None else c for c in linha])
            bloco = _formatar_tabela(aba.title, linhas)
            if bloco:
                blocos.append(bloco)
    finally:
        planilha.close()
    return "\n\n".join(blocos)


def _ler_xls(caminho: Path) -> str:
    try:
        import xlrd
    except ImportError:
        raise ErroDeLeitura("biblioteca 'xlrd' nao instalada (necessaria para .xls antigo)")

    livro = xlrd.open_workbook(str(caminho))
    blocos = []
    for aba in livro.sheets():
        linhas = []
        for indice in range(min(aba.nrows, MAX_LINHAS_TABELA)):
            linhas.append(aba.row_values(indice))
        bloco = _formatar_tabela(aba.name, linhas)
        if bloco:
            blocos.append(bloco)
    return "\n\n".join(blocos)


def _ler_csv(caminho: Path) -> str:
    texto = _ler_texto(caminho)
    amostra = texto[:4096]

    # Descobre sozinho se o separador e virgula, ponto-e-virgula ou tabulacao.
    if caminho.suffix.lower() == ".tsv":
        delimitador = "\t"
    else:
        try:
            delimitador = csv.Sniffer().sniff(amostra, delimiters=",;\t|").delimiter
        except csv.Error:
            delimitador = ";" if amostra.count(";") > amostra.count(",") else ","

    linhas = []
    for i, linha in enumerate(csv.reader(io.StringIO(texto), delimiter=delimitador)):
        if i >= MAX_LINHAS_TABELA:
            break
        linhas.append(linha)
    return _formatar_tabela("", linhas)


# --------------------------------------------------------------------------- #
# PowerPoint e HTML
# --------------------------------------------------------------------------- #
def _ler_pptx(caminho: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        raise ErroDeLeitura("biblioteca 'python-pptx' nao instalada")

    apresentacao = Presentation(str(caminho))
    blocos = []
    for numero, slide in enumerate(apresentacao.slides, start=1):
        partes = ["## Slide {}".format(numero)]
        for forma in slide.shapes:
            if getattr(forma, "has_text_frame", False):
                texto = forma.text_frame.text.strip()
                if texto:
                    partes.append(texto)
        # Anotacoes do apresentador costumam ter o conteudo mais explicativo.
        if slide.has_notes_slide:
            notas = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notas:
                partes.append("Notas: " + notas)
        if len(partes) > 1:
            blocos.append("\n\n".join(partes))
    return "\n\n".join(blocos)


def _ler_html(caminho: Path) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ErroDeLeitura("biblioteca 'beautifulsoup4' nao instalada")

    sopa = BeautifulSoup(_ler_texto(caminho), "html.parser")
    for tag in sopa(["script", "style", "nav", "footer"]):
        tag.decompose()  # fora com menu, rodape e codigo
    return sopa.get_text("\n", strip=True)


# --------------------------------------------------------------------------- #
# Tabela de despacho: extensao -> funcao que sabe ler
# --------------------------------------------------------------------------- #
LEITORES = {
    ".txt": _ler_texto, ".md": _ler_texto, ".markdown": _ler_texto,
    ".rst": _ler_texto, ".log": _ler_texto,
    ".json": _ler_json,
    ".pdf": _ler_pdf,
    ".docx": _ler_docx, ".doc": _ler_doc,
    ".xlsx": _ler_xlsx, ".xlsm": _ler_xlsx, ".xls": _ler_xls,
    ".csv": _ler_csv, ".tsv": _ler_csv,
    ".pptx": _ler_pptx,
    ".html": _ler_html, ".htm": _ler_html,
}


def extrair_texto(caminho) -> str:
    """
    Recebe o caminho de um arquivo e devolve o texto de dentro dele.
    Levanta ErroDeLeitura se o formato nao for suportado ou o arquivo estiver corrompido.
    """
    caminho = Path(caminho)
    leitor = LEITORES.get(caminho.suffix.lower())
    if leitor is None:
        raise ErroDeLeitura("formato '{}' nao suportado".format(caminho.suffix))

    try:
        texto = leitor(caminho)
    except ErroDeLeitura:
        raise
    except Exception as e:
        raise ErroDeLeitura("{}: {}".format(type(e).__name__, e))

    texto = (texto or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not texto:
        raise ErroDeLeitura("arquivo vazio ou sem texto extraivel")
    return texto

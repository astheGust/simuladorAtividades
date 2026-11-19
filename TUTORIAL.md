# ⚡ Tutorial rápido — do zero ao simulado

Cinco passos. Se você só quer usar (e não entender o código), este arquivo basta.

---

## 1. Instalar (uma vez só)

Abra o **PowerShell** na pasta do projeto — no Explorador de Arquivos, clique com o
botão direito dentro da pasta e escolha *"Abrir no Terminal"* — e rode:

```powershell
.\setup.ps1
```

Ele instala o Python necessário do projeto, todas as bibliotecas e pergunta a sua
**chave da OpenAI**. Cole a chave (começa com `sk-`) e aperte Enter.
Não tem chave? Pegue em <https://platform.openai.com/api-keys>.

> Se aparecer um erro vermelho dizendo que scripts estão desabilitados, rode isto e
> tente de novo:
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```

A instalação demora alguns minutos na primeira vez (ela baixa o modelo de busca).

---

## 2. Colocar o seu material

Copie seus arquivos para a pasta:

```
conteudo\
```

Aceita **PDF, DOCX, DOC, TXT, MD, XLSX, XLS, CSV, PPTX, HTML**.

**Dica importante:** organize em subpastas, porque **cada subpasta vira um tópico** do
simulado e o programa equilibra as questões entre elas:

```
conteudo\
   Direito Constitucional\
      aula-01.pdf
      resumo.docx
   Português\
      crase.md
   Informática\
      tabela-atalhos.xlsx
```

Arquivo solto, sem subpasta, também funciona — aí o tópico vira o nome do arquivo.

---

## 3. Rodar o simulado

```powershell
.\run.ps1
```

O navegador abre sozinho em <http://127.0.0.1:5000>. Clique em **"Começar simulado"**.

- Acertou → a alternativa fica **verde**.
- Errou → a sua fica **vermelha**, a certa fica **verde** e aparece a **explicação**.
- Terminou o lote? Clique em **"Gerar próximas questões"**.

Para parar: volte à janela do PowerShell e aperte **Ctrl + C**.

---

## 4. Adicionar ou trocar arquivos depois

**Não precisa fazer mais nada.** É só copiar (ou apagar) arquivos da pasta `conteudo\`.

Na próxima vez que você pedir questões, o programa percebe sozinho:
arquivo novo entra na base, arquivo apagado sai, arquivo editado é atualizado.

Se quiser forçar na hora, clique em **"↻ atualizar base"**, no canto superior direito
da tela.

---

## 5. Mudar o jeito das questões

Abra este arquivo no bloco de notas ou no VS Code:

```
config\prompt.md
```

Ele está em português e tem duas partes: **PROMPT DO SISTEMA** (as regras do
examinador) e **PROMPT DO USUÁRIO** (o pedido de cada rodada). Escreva o que quiser ali.

Exemplos do que você pode acrescentar:

- *"Escreva questões no estilo da banca CESPE."*
- *"Sempre cite, na explicação, o arquivo de onde veio a informação."*
- *"Faça questões difíceis, de aplicação prática, nunca de definição decorada."*

Salve o arquivo e clique em **"Gerar próximas questões"** — já vale, sem reiniciar nada.

Para mudar quantidade de questões, número de alternativas ou idioma, abra:

```
config\settings.json
```

| Quero... | Mude |
|---|---|
| Mais questões por rodada | `"questoes_por_lote": 10` |
| 4 alternativas em vez de 5 | `"opcoes_por_questao": 4` |
| Questões em inglês | `"idioma": "inglês"` |
| Questões mais elaboradas (e mais caras) | `"reasoning_effort": "medium"` |
| Filtro de repetição mais rígido | `"limiar_duplicata": 0.78` |

---

## Onde fica cada coisa

| O que | Onde |
|---|---|
| 📚 Seus arquivos de estudo | `conteudo\` |
| ✏️ O prompt | `config\prompt.md` |
| ⚙️ Configurações (modelo, nº de questões) | `config\settings.json` |
| 🔑 Sua chave da OpenAI | `.env` |
| 📊 Suas respostas (abre no Excel) | `historico.csv` |
| 🚀 Instalar | `.\setup.ps1` |
| ▶️ Rodar | `.\run.ps1` |

---

## Se algo der errado

| Aconteceu | Faça |
|---|---|
| "A chave da API não foi encontrada" | Abra o `.env` e escreva `OPENAI_API_KEY=sk-...` |
| "A base de conhecimento está vazia" | Copie arquivos para `conteudo\` e clique em "↻ atualizar base" |
| Um PDF aparece com erro | Provavelmente é um PDF escaneado (imagem). Use uma versão em texto |
| O `.doc` não abre | Abra no Word e salve como `.docx` |
| Quero zerar meu histórico | Apague o arquivo `historico.csv` |
| Quero reconstruir a base | `.\run.ps1 -Reindexar` |
| Quero só ver o que está indexado | `.\run.ps1 -Status` |

A explicação completa de cada peça está no [README.md](README.md).

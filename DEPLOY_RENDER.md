# Publicar no Render

O projeto agora pode ser executado como um web service Flask. O ponto de
entrada para o Render é `index.py`, e o comando de produção já está em
`render.yaml` e no `Procfile`.

1. Suba o projeto para um repositório Git. Não envie `.env`, a chave da OpenAI,
   `.chroma_db`, `.rag_state` nem os PDFs pessoais.
2. No Render, crie um Web Service ligado ao repositório. O Render pode usar o
   `render.yaml`; alternativamente use `pip install -r requirements.txt` no
   build e `gunicorn index:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 300` no start.
3. Cadastre `OPENAI_API_KEY` como Environment Variable no Render.
4. Abra a URL, envie os PDFs, escolha quantidade, alternativas e dificuldade e
   clique em **Fazer atividades**.

O disco padrão do Render é efêmero: PDFs enviados, histórico e índices podem
ser perdidos quando o serviço for reiniciado ou redeployado. Para uso contínuo,
adicione um Persistent Disk ou, em uma próxima etapa, armazenamento S3/R2 e um
banco de dados. Como o endpoint gera chamadas pagas à OpenAI, recomenda-se
adicionar autenticação antes de compartilhar a URL publicamente.

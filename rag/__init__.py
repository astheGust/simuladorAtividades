"""
Pacote RAG do simulador.

Modulos:
    config         -> caminhos do projeto e leitura do config/settings.json
    loaders        -> le o texto de cada formato de arquivo (pdf, docx, xlsx...)
    chunker        -> quebra o texto em pedacos do tamanho certo para o RAG
    indexer        -> sincroniza a pasta de conteudo com o banco vetorial
    retriever      -> busca os trechos relevantes no banco vetorial
    generator      -> conversa com o modelo de IA (gpt-5.6-luna)
    prompt_builder -> monta o prompt a partir do config/prompt.md
"""

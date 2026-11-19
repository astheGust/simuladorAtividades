"""Ponto de entrada do web service no Render.

O objeto ``app`` continua sendo definido em app.py para manter a execucao
local compatível com ``python app.py`` e ``run.ps1``.
"""

from app import app


if __name__ == "__main__":
    from app import main

    main()

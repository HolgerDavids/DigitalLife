# wsgi.py — ponto de entrada para PythonAnywhere
# NÃO altere este arquivo

import sys
import os

# Adiciona a pasta do projeto ao path
project_home = os.path.dirname(os.path.abspath(__file__))
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Importa e inicializa a aplicação
from app import app, init_db

init_db()

# PythonAnywhere usa a variável 'application'
application = app

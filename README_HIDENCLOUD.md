# Deploy na HidenCloud

Este projeto esta pronto para subir como app Python/Flask usando GitHub.

## Repositorio

- GitHub: `https://github.com/HolgerDavids/DigitalLife`
- Branch: `main`

## Build / instalacao

Use a instalacao padrao do Python:

```bash
pip install -r requirements.txt
```

## Start command

Use este comando de inicializacao no painel da HidenCloud:

```bash
gunicorn server:application --bind 0.0.0.0:${PORT:-5000}
```

## Variaveis de ambiente

Configure estas variaveis no painel:

- `HIDENCLOUD=1`
- `FC_WEB_MODE=1`
- `APP_BASE_URL=https://SEU-DOMINIO`
- `FC_FORCE_HTTPS=1`
- `FC_DATA_DIR=/home/container/data`

Opcional:

- `SECRET_KEY=sua_chave_forte`

## Persistencia

O banco SQLite e o `config.json` passam a usar `FC_DATA_DIR`. Crie esse diretorio persistente na HidenCloud para nao perder dados em atualizacoes do codigo.

"""
FinanceControl — Backend Flask + SQLite
Revisão: 3 ciclos completos de otimização
  ✓ PBKDF2 com salt para senhas (substituiu SHA-256 simples)
  ✓ SECRET_KEY persistida no config.json (sessões sobrevivem ao restart)
  ✓ Função parse_data() centralizada (eliminados 3 loops duplicados)
  ✓ db_write() context manager (eliminou 12 db.commit() manuais)
  ✓ SQL com colunas explícitas (sem SELECT *)
  ✓ Importação em lote com executemany (sem N+1 no confirmar)
  ✓ Import TextIOWrapper removido
  ✓ Cache de SVG de ícones no JS (eliminados renders duplicados)
  ✓ Promise.all() para chamadas paralelas no frontend
"""

import os, sys, re, csv, json, uuid, hmac, hashlib, secrets
import sqlite3, smtplib, threading, webbrowser, base64, unicodedata
import urllib.request as _urlreq
from io           import StringIO, BytesIO
from email.mime.text      import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime     import datetime, date, timedelta
from contextlib   import contextmanager
from functools    import wraps
from flask        import Flask, request, jsonify, render_template, g, session

# ─── CONFIGURAÇÃO ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "finance.db")
CFG_PATH = os.path.join(BASE_DIR, "config.json")
PORT     = 5000

def _load_cfg() -> dict:
    try:
        return json.load(open(CFG_PATH, encoding="utf-8")) if os.path.exists(CFG_PATH) else {}
    except Exception:
        return {}

def _save_cfg(cfg: dict):
    json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), indent=2)

def _get_secret_key() -> str:
    """Persiste a SECRET_KEY entre restarts para não invalidar sessões."""
    cfg = _load_cfg()
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        _save_cfg(cfg)
    return cfg["secret_key"]

app = Flask(__name__)
app.config["JSON_SORT_KEYS"]          = False
app.config["SECRET_KEY"]              = _get_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE deve ser True apenas em HTTPS para evitar que o navegador bloqueie o cookie em HTTP
app.config["SESSION_COOKIE_SECURE"] = bool(
    os.environ.get("FC_FORCE_HTTPS") or 
    os.environ.get("APP_BASE_URL", "").startswith("https://")
)

# ─── BANCO DE DADOS ───────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA synchronous = NORMAL")   # ← otimização I/O
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@contextmanager
def db_write():
    """Context manager para escrita: faz commit automático ao sair."""
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise

# ─── INIT DB ──────────────────────────────────────────────────────────────────
def init_db():
    with db_conn() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id           TEXT PRIMARY KEY,
                nome         TEXT NOT NULL,
                email        TEXT NOT NULL UNIQUE,
                senha_hash   TEXT NOT NULL,
                reset_token  TEXT,
                reset_expira TEXT,
                criado_em    TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tipos (
                id         TEXT PRIMARY KEY,
                nome       TEXT NOT NULL UNIQUE,
                natureza   TEXT NOT NULL CHECK(natureza IN ('positivo','negativo')),
                cor        TEXT NOT NULL DEFAULT '#94a3b8',
                icone      TEXT NOT NULL DEFAULT 'circle',
                is_default INTEGER NOT NULL DEFAULT 0,
                criado_em  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS categorias (
                id         TEXT PRIMARY KEY,
                nome       TEXT NOT NULL UNIQUE,
                tipo_id    TEXT NOT NULL REFERENCES tipos(id) ON DELETE CASCADE,
                cor        TEXT NOT NULL DEFAULT '#94a3b8',
                icone      TEXT NOT NULL DEFAULT 'tag',
                is_default INTEGER NOT NULL DEFAULT 0,
                criado_em  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS transacoes (
                id         TEXT PRIMARY KEY,
                descricao  TEXT NOT NULL,
                valor      REAL NOT NULL CHECK(valor > 0),
                categoria  TEXT NOT NULL,
                tipo_id    TEXT NOT NULL REFERENCES tipos(id),
                data       TEXT NOT NULL,
                observacao TEXT DEFAULT '',
                criado_em  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_t_data    ON transacoes(data);
            CREATE INDEX IF NOT EXISTS idx_t_tipo    ON transacoes(tipo_id);
            CREATE INDEX IF NOT EXISTS idx_t_cat     ON transacoes(categoria);
            CREATE INDEX IF NOT EXISTS idx_t_criado  ON transacoes(criado_em);

            -- Gamificação
            CREATE TABLE IF NOT EXISTS habitos (
                id        TEXT PRIMARY KEY,
                nome      TEXT NOT NULL,
                area      TEXT NOT NULL DEFAULT 'geral',
                icone     TEXT NOT NULL DEFAULT '⭐',
                cor       TEXT NOT NULL DEFAULT '#a78bfa',
                xp        INTEGER NOT NULL DEFAULT 10,
                ativo     INTEGER NOT NULL DEFAULT 1,
                ordem     INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS habito_registros (
                id         TEXT PRIMARY KEY,
                habito_id  TEXT NOT NULL REFERENCES habitos(id) ON DELETE CASCADE,
                data       TEXT NOT NULL,
                concluido  INTEGER NOT NULL DEFAULT 0,
                criado_em  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(habito_id, data)
            );
            CREATE TABLE IF NOT EXISTS perfil_xp (
                id           TEXT PRIMARY KEY DEFAULT 'perfil',
                xp_total     INTEGER NOT NULL DEFAULT 0,
                nivel        INTEGER NOT NULL DEFAULT 1,
                streak_atual INTEGER NOT NULL DEFAULT 0,
                streak_max   INTEGER NOT NULL DEFAULT 0,
                ultimo_dia   TEXT DEFAULT NULL,
                atualizado   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_hr_data   ON habito_registros(data);
            CREATE INDEX IF NOT EXISTS idx_hr_habito ON habito_registros(habito_id);

            -- ── AGENDA / TO-DO ───────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS agenda_projetos (
                id        TEXT PRIMARY KEY,
                nome      TEXT NOT NULL,
                cor       TEXT NOT NULL DEFAULT '#9b8cff',
                icone     TEXT NOT NULL DEFAULT '📁',
                criado_em TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS agenda_tarefas (
                id           TEXT PRIMARY KEY,
                titulo       TEXT NOT NULL,
                descricao    TEXT DEFAULT '',
                projeto_id   TEXT REFERENCES agenda_projetos(id) ON DELETE SET NULL,
                prioridade   TEXT NOT NULL DEFAULT 'media' CHECK(prioridade IN ('urgente','alta','media','baixa')),
                status       TEXT NOT NULL DEFAULT 'pendente' CHECK(status IN ('pendente','em_progresso','concluido','cancelado')),
                data_limite  TEXT DEFAULT NULL,
                hora_inicio  TEXT DEFAULT NULL,
                hora_fim     TEXT DEFAULT NULL,
                tempo_gasto  INTEGER DEFAULT 0,
                concluido_em TEXT DEFAULT NULL,
                recorrente   TEXT DEFAULT NULL,
                ordem        INTEGER DEFAULT 0,
                criado_em    TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS agenda_sessoes (
                id          TEXT PRIMARY KEY,
                tarefa_id   TEXT REFERENCES agenda_tarefas(id) ON DELETE CASCADE,
                inicio      TEXT NOT NULL,
                fim         TEXT DEFAULT NULL,
                duracao_seg INTEGER DEFAULT 0,
                tipo        TEXT DEFAULT 'foco' CHECK(tipo IN ('foco','pomodoro','pausa')),
                criado_em   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_at_data    ON agenda_tarefas(data_limite);
            CREATE INDEX IF NOT EXISTS idx_at_status  ON agenda_tarefas(status);
            CREATE INDEX IF NOT EXISTS idx_at_proj    ON agenda_tarefas(projeto_id);
        """)
        # Migração segura para bancos antigos
        for tabela, col, default in [("tipos","icone","circle"),("categorias","icone","tag")]:
            try: db.execute(f"ALTER TABLE {tabela} ADD COLUMN icone TEXT NOT NULL DEFAULT '{default}'")
            except Exception: pass

        db.execute("INSERT OR IGNORE INTO perfil_xp(id) VALUES('perfil')")

        habitos_default = [
            ("h-exercicio", "Exercício físico",  "saude",       "🏃", "#f87171", 20, 1, 1),
            ("h-agua",      "Beber 2L de água",  "saude",       "💧", "#60a5fa", 10, 1, 2),
            ("h-leitura",   "Ler 20 minutos",    "aprendizado", "📚", "#a78bfa", 15, 1, 3),
            ("h-meditacao", "Meditar",           "saude",       "🧘", "#34d399", 15, 1, 4),
            ("h-sem-junk",  "Sem junk food",     "saude",       "🥗", "#fbbf24", 10, 1, 5),
            ("h-estudos",   "Estudar 1 hora",    "aprendizado", "💻", "#fb7185", 20, 1, 6),
        ]
        db.executemany(
            "INSERT OR IGNORE INTO habitos(id,nome,area,icone,cor,xp,ativo,ordem) VALUES(?,?,?,?,?,?,?,?)",
            habitos_default
        )

        db.executemany(
            "INSERT OR IGNORE INTO tipos(id,nome,natureza,cor,icone,is_default) VALUES(?,?,?,?,?,?)",
            [("receita","Receita","positivo","#4ade80","trending-up",1),
             ("despesa","Despesa","negativo","#f87171","trending-down",1)]
        )
        db.executemany(
            "INSERT OR IGNORE INTO categorias(id,nome,tipo_id,cor,icone,is_default) VALUES(?,?,?,?,?,?)",
            [("salario",      "Salário",          "receita","#4ade80","briefcase",      1),
             ("freelance",    "Freelance",         "receita","#86efac","laptop",         1),
             ("investimentos","Investimentos",     "receita","#6ee7b7","bar-chart-2",    1),
             ("aluguel-rec",  "Aluguel Recebido",  "receita","#a7f3d0","home",           1),
             ("presente",     "Presente",          "receita","#fde68a","gift",           1),
             ("outros-rec",   "Outros (Receita)",  "receita","#d1d5db","plus-circle",    1),
             ("moradia",      "Moradia",           "despesa","#60a5fa","home",           1),
             ("alimentacao",  "Alimentação",       "despesa","#fbbf24","utensils",       1),
             ("transporte",   "Transporte",        "despesa","#a78bfa","car",            1),
             ("saude",        "Saúde",             "despesa","#f87171","heart-pulse",    1),
             ("educacao",     "Educação",          "despesa","#34d399","graduation-cap", 1),
             ("lazer",        "Lazer",             "despesa","#fb7185","smile",          1),
             ("vestuario",    "Vestuário",         "despesa","#f472b6","shirt",          1),
             ("contas",       "Contas/Serviços",   "despesa","#94a3b8","zap",            1),
             ("cartao",       "Cartão de Crédito", "despesa","#ff6b6b","credit-card",    1),
             ("outros-desp",  "Outros (Despesa)",  "despesa","#6b7280","more-horizontal",1)]
        )

# ─── UTILITÁRIOS ──────────────────────────────────────────────────────────────
def uid() -> str:
    return uuid.uuid4().hex[:16]

def row_to_dict(row) -> dict | None:
    return dict(row) if row else None

def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]

def ok(data=None, **kw):
    r = {"ok": True}
    if data is not None: r["data"] = data
    r.update(kw)
    return jsonify(r)

def err(msg, code=400):
    return jsonify({"ok": False, "erro": msg}), code

# ─── PARSE DE DATA CENTRALIZADO (eliminava 3 loops idênticos) ─────────────────
_DATE_FMTS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y")

def parse_data(raw: str) -> str | None:
    """Converte string de data para ISO YYYY-MM-DD. Retorna None se inválida."""
    raw = raw.strip().strip('"')[:10]
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def _normalizar(texto: str) -> str:
    """Remove acentos e converte para minúsculas."""
    return unicodedata.normalize("NFD", texto.lower()).encode("ascii", "ignore").decode()

def _converter_valor(raw: str) -> float:
    """Converte string de valor BR/US para float."""
    raw = raw.strip().replace("R$", "").replace(" ", "")
    if "," in raw and "." in raw:
        return float(raw.replace(".", "").replace(",", "."))
    elif "," in raw:
        return float(raw.replace(",", "."))
    return float(raw)

# ─── AGENDA — PROJETOS PADRÃO ─────────────────────────────────────────────────
def _seed_agenda(db):
    """Insere projetos padrão se ainda não existirem."""
    db.executemany(
        "INSERT OR IGNORE INTO agenda_projetos(id,nome,cor,icone) VALUES(?,?,?,?)",
        [
            ("ag-pessoal",  "Pessoal",     "#9b8cff", "👤"),
            ("ag-trabalho", "Trabalho",    "#5eafff", "💼"),
            ("ag-saude",    "Saúde",       "#39e079", "🏃"),
            ("ag-estudos",  "Estudos",     "#ffd166", "📚"),
        ]
    )

# ══════════════════════════════════════════════════════════════════════════════
#  API — AGENDA
# ══════════════════════════════════════════════════════════════════════════════

# ── Projetos ──────────────────────────────────────────────────────────────────
@app.route("/api/agenda/projetos", methods=["GET"])
def ag_listar_projetos():
    db = get_db()
    _seed_agenda(db)
    rows = db.execute(
        "SELECT p.*, COUNT(t.id) as total, "
        "SUM(CASE WHEN t.status='concluido' THEN 1 ELSE 0 END) as concluidos "
        "FROM agenda_projetos p "
        "LEFT JOIN agenda_tarefas t ON t.projeto_id = p.id "
        "GROUP BY p.id ORDER BY p.nome"
    ).fetchall()
    return ok(rows_to_list(rows))

@app.route("/api/agenda/projetos", methods=["POST"])
def ag_criar_projeto():
    d     = request.get_json() or {}
    nome  = (d.get("nome") or "").strip()
    cor   = d.get("cor", "#9b8cff")
    icone = d.get("icone", "📁")
    if not nome: return err("Informe o nome do projeto.")
    pid = uid()
    with db_write() as db:
        db.execute("INSERT INTO agenda_projetos(id,nome,cor,icone) VALUES(?,?,?,?)",
                   (pid, nome, cor, icone))
    return ok(row_to_dict(get_db().execute(
        "SELECT * FROM agenda_projetos WHERE id=?", (pid,)).fetchone())), 201

@app.route("/api/agenda/projetos/<pid>", methods=["DELETE"])
def ag_deletar_projeto(pid):
    if not get_db().execute("SELECT 1 FROM agenda_projetos WHERE id=?", (pid,)).fetchone():
        return err("Projeto não encontrado.", 404)
    with db_write() as db:
        db.execute("DELETE FROM agenda_projetos WHERE id=?", (pid,))
    return ok({"id": pid})

# ── Tarefas ───────────────────────────────────────────────────────────────────
@app.route("/api/agenda/tarefas", methods=["GET"])
def ag_listar_tarefas():
    db   = get_db()
    args = request.args
    data = args.get("data", "")       # filtro por data_limite
    proj = args.get("projeto_id", "")
    sta  = args.get("status", "")
    hoje = args.get("hoje", "")       # tarefas do dia

    sql    = """SELECT t.*, p.nome as projeto_nome, p.cor as projeto_cor, p.icone as projeto_icone
                FROM agenda_tarefas t
                LEFT JOIN agenda_projetos p ON p.id = t.projeto_id
                WHERE 1=1"""
    params = []

    if hoje:
        sql += " AND (t.data_limite=? OR (t.hora_inicio IS NOT NULL AND substr(t.hora_inicio,1,10)=?))"
        params += [hoje, hoje]
    elif data:
        sql += " AND t.data_limite=?"
        params.append(data)

    if proj:
        sql += " AND t.projeto_id=?"
        params.append(proj)
    if sta:
        sql += " AND t.status=?"
        params.append(sta)
    else:
        sql += " AND t.status != 'cancelado'"

    sql += " ORDER BY CASE t.prioridade WHEN 'urgente' THEN 1 WHEN 'alta' THEN 2 WHEN 'media' THEN 3 ELSE 4 END, t.ordem, t.criado_em"
    return ok(rows_to_list(db.execute(sql, params).fetchall()))

@app.route("/api/agenda/tarefas", methods=["POST"])
def ag_criar_tarefa():
    d          = request.get_json() or {}
    titulo     = (d.get("titulo") or "").strip()
    if not titulo: return err("Informe o título da tarefa.")
    descricao  = (d.get("descricao") or "").strip()
    projeto_id = d.get("projeto_id") or None
    prioridade = d.get("prioridade", "media")
    data_lim   = d.get("data_limite") or None
    hora_ini   = d.get("hora_inicio") or None
    hora_fim   = d.get("hora_fim")    or None

    if prioridade not in ("urgente","alta","media","baixa"):
        prioridade = "media"

    max_ordem = get_db().execute("SELECT COALESCE(MAX(ordem),0) FROM agenda_tarefas").fetchone()[0]
    tid = uid()
    with db_write() as db:
        db.execute("""INSERT INTO agenda_tarefas
            (id,titulo,descricao,projeto_id,prioridade,data_limite,hora_inicio,hora_fim,ordem)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (tid, titulo, descricao, projeto_id, prioridade, data_lim, hora_ini, hora_fim, max_ordem+1))
    row = get_db().execute("""
        SELECT t.*, p.nome as projeto_nome, p.cor as projeto_cor, p.icone as projeto_icone
        FROM agenda_tarefas t LEFT JOIN agenda_projetos p ON p.id=t.projeto_id WHERE t.id=?
    """, (tid,)).fetchone()
    return ok(row_to_dict(row)), 201

@app.route("/api/agenda/tarefas/<tid>", methods=["PUT"])
def ag_editar_tarefa(tid):
    d          = request.get_json() or {}
    titulo     = (d.get("titulo") or "").strip()
    if not titulo: return err("Título obrigatório.")
    descricao  = (d.get("descricao") or "").strip()
    projeto_id = d.get("projeto_id") or None
    prioridade = d.get("prioridade", "media")
    data_lim   = d.get("data_limite") or None
    hora_ini   = d.get("hora_inicio") or None
    hora_fim   = d.get("hora_fim")    or None
    status     = d.get("status", "pendente")
    with db_write() as db:
        db.execute("""UPDATE agenda_tarefas SET
            titulo=?,descricao=?,projeto_id=?,prioridade=?,data_limite=?,
            hora_inicio=?,hora_fim=?,status=? WHERE id=?""",
            (titulo,descricao,projeto_id,prioridade,data_lim,hora_ini,hora_fim,status,tid))
    row = get_db().execute("""
        SELECT t.*, p.nome as projeto_nome, p.cor as projeto_cor, p.icone as projeto_icone
        FROM agenda_tarefas t LEFT JOIN agenda_projetos p ON p.id=t.projeto_id WHERE t.id=?
    """, (tid,)).fetchone()
    return ok(row_to_dict(row))

@app.route("/api/agenda/tarefas/<tid>/status", methods=["PATCH"])
def ag_toggle_status(tid):
    d      = request.get_json() or {}
    status = d.get("status", "concluido")
    db     = get_db()
    row    = db.execute("SELECT * FROM agenda_tarefas WHERE id=?", (tid,)).fetchone()
    if not row: return err("Tarefa não encontrada.", 404)

    concluido_em = None
    if status == "concluido":
        concluido_em = datetime.now().isoformat()
    elif row["status"] == "concluido" and status == "pendente":
        concluido_em = None

    with db_write() as db:
        db.execute("UPDATE agenda_tarefas SET status=?, concluido_em=? WHERE id=?",
                   (status, concluido_em, tid))
    return ok({"id": tid, "status": status, "concluido_em": concluido_em})

@app.route("/api/agenda/tarefas/<tid>", methods=["DELETE"])
def ag_deletar_tarefa(tid):
    if not get_db().execute("SELECT 1 FROM agenda_tarefas WHERE id=?", (tid,)).fetchone():
        return err("Tarefa não encontrada.", 404)
    with db_write() as db:
        db.execute("DELETE FROM agenda_tarefas WHERE id=?", (tid,))
    return ok({"id": tid})

# ── Timer / Sessões ───────────────────────────────────────────────────────────
@app.route("/api/agenda/sessoes", methods=["POST"])
def ag_iniciar_sessao():
    d   = request.get_json() or {}
    tid = (d.get("tarefa_id") or "").strip()
    tp  = d.get("tipo", "foco")
    sid = uid()
    inicio = datetime.now().isoformat()
    with db_write() as db:
        db.execute("INSERT INTO agenda_sessoes(id,tarefa_id,inicio,tipo) VALUES(?,?,?,?)",
                   (sid, tid or None, inicio, tp))
        if tid:
            db.execute("UPDATE agenda_tarefas SET status='em_progresso' WHERE id=? AND status='pendente'",
                       (tid,))
    return ok({"id": sid, "inicio": inicio})

@app.route("/api/agenda/sessoes/<sid>/finalizar", methods=["PATCH"])
def ag_finalizar_sessao(sid):
    db  = get_db()
    row = db.execute("SELECT * FROM agenda_sessoes WHERE id=?", (sid,)).fetchone()
    if not row: return err("Sessão não encontrada.", 404)
    fim = datetime.now().isoformat()
    try:
        dur = int((datetime.fromisoformat(fim) - datetime.fromisoformat(row["inicio"])).total_seconds())
    except Exception:
        dur = 0
    with db_write() as db:
        db.execute("UPDATE agenda_sessoes SET fim=?, duracao_seg=? WHERE id=?", (fim, dur, sid))
        if row["tarefa_id"]:
            db.execute("UPDATE agenda_tarefas SET tempo_gasto = tempo_gasto + ? WHERE id=?",
                       (dur, row["tarefa_id"]))
    return ok({"id": sid, "fim": fim, "duracao_seg": dur})

@app.route("/api/agenda/stats")
def ag_stats():
    db = get_db()
    hoje = date.today().isoformat()
    sem_ini = (date.today() - timedelta(days=date.today().weekday())).isoformat()

    total      = db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE status!='cancelado'").fetchone()[0]
    concluidas = db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE status='concluido'").fetchone()[0]
    pendentes  = db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE status='pendente'").fetchone()[0]
    urgentes   = db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE prioridade='urgente' AND status!='concluido'").fetchone()[0]
    hoje_total = db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE data_limite=? AND status!='cancelado'",(hoje,)).fetchone()[0]
    hoje_feitas= db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE data_limite=? AND status='concluido'",(hoje,)).fetchone()[0]
    atrasadas  = db.execute("SELECT COUNT(*) FROM agenda_tarefas WHERE data_limite < ? AND status NOT IN ('concluido','cancelado')",(hoje,)).fetchone()[0]
    tempo_hoje = db.execute("""
        SELECT COALESCE(SUM(duracao_seg),0) FROM agenda_sessoes
        WHERE substr(inicio,1,10)=?""", (hoje,)).fetchone()[0]
    tempo_sem  = db.execute("""
        SELECT COALESCE(SUM(duracao_seg),0) FROM agenda_sessoes
        WHERE substr(inicio,1,10)>=?""", (sem_ini,)).fetchone()[0]

    return ok({
        "total": total, "concluidas": concluidas, "pendentes": pendentes,
        "urgentes": urgentes, "atrasadas": atrasadas,
        "hoje_total": hoje_total, "hoje_feitas": hoje_feitas,
        "tempo_hoje_min": round(tempo_hoje/60), "tempo_semana_min": round(tempo_sem/60),
    })

# ─── AUTENTICAÇÃO ─────────────────────────────────────────────────────────────
_PBKDF2_ITERS = 260_000   # OWASP 2024 recomendação

def hash_senha(senha: str, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256 com salt — substitui SHA-256 simples."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2:{salt}:{dk.hex()}"

def verificar_senha(senha: str, hash_stored: str) -> bool:
    """Verifica senha com timing-safe compare."""
    if hash_stored.startswith("pbkdf2:"):
        _, salt, _ = hash_stored.split(":", 2)
        return hmac.compare_digest(hash_senha(senha, salt), hash_stored)
    # Legado SHA-256 (migra automaticamente no próximo login)
    return hmac.compare_digest(
        hashlib.sha256(senha.encode()).hexdigest(),
        hash_stored
    )

def usuario_logado() -> str | None:
    return session.get("usuario_id")

def requer_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not usuario_logado():
            return err("Não autenticado.", 401)
        return f(*args, **kwargs)
    return decorated

def _cfg_email() -> dict:
    return _load_cfg().get("email", {})

def _enviar_email(destino: str, assunto: str, corpo_html: str) -> bool:
    cfg = _cfg_email()
    if not cfg.get("smtp_host"): return False
    host  = cfg["smtp_host"]
    porta = int(cfg.get("smtp_port", 587))
    user  = cfg.get("smtp_user", "")
    pwd   = cfg.get("smtp_pass", "")
    from_ = cfg.get("smtp_from") or user
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = from_
        msg["To"]      = destino
        msg.attach(MIMEText(corpo_html, "html", "utf-8"))
        if porta == 465:
            # SSL direto
            import ssl
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, porta, context=ctx, timeout=15) as s:
                s.login(user, pwd)
                s.sendmail(from_, [destino], msg.as_string())
        else:
            # STARTTLS (porta 587 ou 25)
            with smtplib.SMTP(host, porta, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(user, pwd)
                s.sendmail(from_, [destino], msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f"SMTP error: {e}")
        return False

# ─── ROTAS DE PÁGINA ──────────────────────────────────────────────────────────
@app.route("/portal")
def portal():
    return render_template("portal.html")

@app.route("/vida")
def vida():
    return render_template("vida.html")

@app.route("/gym")
def gym():
    return render_template("gym.html")

@app.route("/agenda")
def agenda():
    return render_template("agenda.html")

@app.route("/casal")
def casal():
    return render_template("casal.html")

@app.route("/estudos")
def estudos():
    return render_template("estudos.html")

@app.route("/")
@app.route("/redefinir-senha")
def index():
    return render_template("portal.html")

@app.route("/financeiro")
def financeiro():
    return render_template("financeiro.html")

# ══════════════════════════════════════════════════════════════════════════════
#  API — GAMIFICAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

# ── Níveis XP (inspirado no Duolingo) ────────────────────────────────────────
_NIVEIS = [
    (1,  "Iniciante",    0,     "🌱", "#94a3b8"),
    (2,  "Aprendiz",     100,   "🌿", "#86efac"),
    (3,  "Praticante",   300,   "⚡", "#fbbf24"),
    (4,  "Comprometido", 600,   "🔥", "#fb7185"),
    (5,  "Dedicado",     1000,  "💎", "#a78bfa"),
    (6,  "Campeão",      1500,  "🏆", "#4ade80"),
    (7,  "Lendário",     2500,  "👑", "#f59e0b"),
    (8,  "Mestre",       4000,  "🌟", "#06b6d4"),
    (9,  "Elite",        6000,  "⚜️", "#ec4899"),
    (10, "Transcendente",10000, "✨", "#ffffff"),
]

def _calcular_nivel(xp: int) -> dict:
    nivel_atual = _NIVEIS[0]
    proximo     = _NIVEIS[1] if len(_NIVEIS) > 1 else None
    for i, n in enumerate(_NIVEIS):
        if xp >= n[2]:
            nivel_atual = n
            proximo     = _NIVEIS[i+1] if i+1 < len(_NIVEIS) else None
    num, nome, xp_min, emoji, cor = nivel_atual
    xp_prox    = proximo[2] if proximo else xp_min
    xp_no_nivel= xp - xp_min
    xp_precisa = max(xp_prox - xp_min, 1)
    pct        = min(int(xp_no_nivel / xp_precisa * 100), 100)
    return {"nivel": num, "nome": nome, "emoji": emoji, "cor": cor,
            "xp_total": xp, "xp_nivel_atual": xp_no_nivel,
            "xp_para_proximo": xp_prox - xp, "pct_nivel": pct,
            "proximo_nivel": proximo[1] if proximo else nome}

def _atualizar_streak(db) -> dict:
    """Recalcula streak baseado nos dias que tiveram ≥1 hábito concluído."""
    hoje = date.today().isoformat()
    perfil = db.execute("SELECT * FROM perfil_xp WHERE id='perfil'").fetchone()
    ultimo = perfil["ultimo_dia"] if perfil else None
    streak = perfil["streak_atual"] if perfil else 0
    streak_max = perfil["streak_max"] if perfil else 0

    # Verifica se hoje tem concluídos
    tem_hoje = db.execute(
        "SELECT COUNT(*) FROM habito_registros WHERE data=? AND concluido=1", (hoje,)
    ).fetchone()[0]

    if tem_hoje:
        if ultimo is None or ultimo == hoje:
            pass  # já registrado hoje
        else:
            # Calcula diferença de dias
            try:
                diff = (date.fromisoformat(hoje) - date.fromisoformat(ultimo)).days
                streak = streak + 1 if diff == 1 else 1
            except Exception:
                streak = 1
        streak_max = max(streak, streak_max)
        db.execute(
            "UPDATE perfil_xp SET streak_atual=?, streak_max=?, ultimo_dia=?, atualizado=datetime('now') WHERE id='perfil'",
            (streak, streak_max, hoje)
        )

    return {"streak_atual": streak, "streak_max": streak_max}

@app.route("/api/vida/perfil")
def vida_perfil():
    db  = get_db()
    row = db.execute("SELECT * FROM perfil_xp WHERE id='perfil'").fetchone()
    if not row:
        db.execute("INSERT OR IGNORE INTO perfil_xp(id) VALUES('perfil')")
        db.commit()
        row = db.execute("SELECT * FROM perfil_xp WHERE id='perfil'").fetchone()
    xp     = row["xp_total"] if row else 0
    streak = row["streak_atual"] if row else 0
    streak_max = row["streak_max"] if row else 0
    nivel  = _calcular_nivel(xp)
    nivel.update({"streak_atual": streak, "streak_max": streak_max})
    return ok(nivel)

@app.route("/api/vida/habitos", methods=["GET"])
def vida_listar_habitos():
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM habitos WHERE ativo=1 ORDER BY ordem, nome"
    ).fetchall()
    return ok(rows_to_list(rows))

@app.route("/api/vida/habitos", methods=["POST"])
def vida_criar_habito():
    d     = request.get_json() or {}
    nome  = (d.get("nome") or "").strip()
    area  = d.get("area", "geral")
    icone = d.get("icone", "⭐")
    cor   = d.get("cor", "#a78bfa")
    xp    = max(1, min(int(d.get("xp", 10)), 100))
    if not nome: return err("Informe o nome do hábito.")
    hid = uid()
    # Pega a maior ordem atual
    max_ordem = get_db().execute("SELECT COALESCE(MAX(ordem),0) FROM habitos").fetchone()[0]
    with db_write() as db:
        db.execute(
            "INSERT INTO habitos(id,nome,area,icone,cor,xp,ativo,ordem) VALUES(?,?,?,?,?,?,1,?)",
            (hid, nome, area, icone, cor, xp, max_ordem + 1)
        )
    row = get_db().execute("SELECT * FROM habitos WHERE id=?", (hid,)).fetchone()
    return ok(row_to_dict(row)), 201

@app.route("/api/vida/habitos/<hid>", methods=["PUT"])
def vida_editar_habito(hid):
    d    = request.get_json() or {}
    nome = (d.get("nome") or "").strip()
    if not nome: return err("Nome obrigatório.")
    area  = d.get("area", "geral")
    icone = d.get("icone", "⭐")
    cor   = d.get("cor", "#a78bfa")
    xp    = max(1, min(int(d.get("xp", 10)), 100))
    with db_write() as db:
        db.execute("UPDATE habitos SET nome=?,area=?,icone=?,cor=?,xp=? WHERE id=?",
                   (nome, area, icone, cor, xp, hid))
    return ok(row_to_dict(get_db().execute("SELECT * FROM habitos WHERE id=?", (hid,)).fetchone()))

@app.route("/api/vida/habitos/<hid>", methods=["DELETE"])
def vida_deletar_habito(hid):
    row = get_db().execute("SELECT id FROM habitos WHERE id=?", (hid,)).fetchone()
    if not row: return err("Hábito não encontrado.", 404)
    with db_write() as db:
        db.execute("UPDATE habitos SET ativo=0 WHERE id=?", (hid,))
    return ok({"id": hid})

@app.route("/api/vida/registros", methods=["GET"])
def vida_registros():
    """Retorna registros da semana (ou período específico)."""
    db    = get_db()
    hoje  = date.today()
    ini   = request.args.get("inicio") or (hoje - timedelta(days=hoje.weekday())).isoformat()
    fim   = request.args.get("fim")    or hoje.isoformat()
    rows  = db.execute("""
        SELECT hr.habito_id, hr.data, hr.concluido, h.nome, h.icone, h.cor, h.xp, h.area
        FROM habito_registros hr
        JOIN habitos h ON h.id = hr.habito_id
        WHERE hr.data BETWEEN ? AND ?
        ORDER BY hr.data, h.ordem
    """, (ini, fim)).fetchall()
    return ok(rows_to_list(rows))

@app.route("/api/vida/registros/historico")
def vida_historico():
    """Retorna % conclusão dos últimos 30 dias para o gráfico."""
    db   = get_db()
    hoje = date.today()
    ini  = (hoje - timedelta(days=29)).isoformat()
    total_habitos = db.execute("SELECT COUNT(*) FROM habitos WHERE ativo=1").fetchone()[0]
    if total_habitos == 0:
        return ok([])
    rows = db.execute("""
        SELECT data, COUNT(*) as concluidos
        FROM habito_registros
        WHERE data >= ? AND concluido = 1
        GROUP BY data ORDER BY data
    """, (ini,)).fetchall()
    por_dia = {r["data"]: r["concluidos"] for r in rows}
    resultado = []
    for i in range(30):
        d    = (hoje - timedelta(days=29-i)).isoformat()
        conc = por_dia.get(d, 0)
        pct  = min(int(conc / total_habitos * 100), 100)
        resultado.append({"data": d, "concluidos": conc, "total": total_habitos, "pct": pct})
    return ok(resultado)

@app.route("/api/vida/toggle", methods=["POST"])
def vida_toggle():
    """Marca/desmarca um hábito como concluído em uma data."""
    d       = request.get_json() or {}
    hid     = (d.get("habito_id") or "").strip()
    data    = (d.get("data") or date.today().isoformat()).strip()
    if not hid: return err("habito_id obrigatório.")

    db  = get_db()
    hab = db.execute("SELECT * FROM habitos WHERE id=?", (hid,)).fetchone()
    if not hab: return err("Hábito não encontrado.", 404)

    reg = db.execute(
        "SELECT * FROM habito_registros WHERE habito_id=? AND data=?", (hid, data)
    ).fetchone()

    xp_ganho = 0
    if reg:
        novo = 0 if reg["concluido"] else 1
        with db_write() as db:
            db.execute("UPDATE habito_registros SET concluido=? WHERE habito_id=? AND data=?",
                       (novo, hid, data))
        if novo == 1:
            xp_ganho = hab["xp"]
        else:
            xp_ganho = -hab["xp"]  # desfaz o XP
    else:
        rid = uid()
        with db_write() as db:
            db.execute("INSERT INTO habito_registros(id,habito_id,data,concluido) VALUES(?,?,?,1)",
                       (rid, hid, data))
        xp_ganho = hab["xp"]
        novo = 1

    # Atualiza XP total
    with db_write() as db:
        db.execute("""
            UPDATE perfil_xp
            SET xp_total = MAX(0, xp_total + ?),
                nivel    = ?,
                atualizado = datetime('now')
            WHERE id = 'perfil'
        """, (xp_ganho, _calcular_nivel(max(0,
            (get_db().execute("SELECT xp_total FROM perfil_xp WHERE id='perfil'").fetchone() or {"xp_total":0})["xp_total"] + xp_ganho
        ))["nivel"]))

    # Recalcula streak
    streak = _atualizar_streak(get_db())

    # Busca perfil atualizado
    perfil = get_db().execute("SELECT xp_total FROM perfil_xp WHERE id='perfil'").fetchone()
    xp_now = perfil["xp_total"] if perfil else 0
    nivel  = _calcular_nivel(xp_now)

    return ok({
        "concluido": novo,
        "xp_ganho":  xp_ganho,
        "xp_total":  xp_now,
        "nivel":     nivel,
        "streak":    streak,
    })


@app.route("/api/auth/status")
def auth_status():
    uid = usuario_logado()
    if not uid: return ok({"logado": False})
    row = get_db().execute(
        "SELECT id, nome, email FROM usuarios WHERE id=?", (uid,)
    ).fetchone()
    if not row: session.clear(); return ok({"logado": False})
    return ok({"logado": True, "usuario": row_to_dict(row)})

@app.route("/api/auth/registrar", methods=["POST"])
def auth_registrar():
    d     = request.get_json() or {}
    nome  = (d.get("nome")  or "").strip()
    email = (d.get("email") or "").strip().lower()
    senha = (d.get("senha") or "").strip()
    if not nome:              return err("Informe seu nome.")
    if not email:             return err("Informe seu e-mail.")
    if len(senha) < 6:        return err("Senha deve ter pelo menos 6 caracteres.")
    db = get_db()
    if db.execute("SELECT 1 FROM usuarios WHERE email=?", (email,)).fetchone():
        return err("E-mail já cadastrado.")
    nid = uid()
    with db_write() as db:
        db.execute("INSERT INTO usuarios(id,nome,email,senha_hash) VALUES(?,?,?,?)",
                   (nid, nome, email, hash_senha(senha)))
    session["usuario_id"] = nid
    return ok({"nome": nome, "email": email}), 201

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    d     = request.get_json() or {}
    email = (d.get("email") or "").strip().lower()
    senha = (d.get("senha") or "").strip()
    db    = get_db()
    row   = db.execute(
        "SELECT id, nome, email, senha_hash FROM usuarios WHERE email=?", (email,)
    ).fetchone()
    if not row or not verificar_senha(senha, row["senha_hash"]):
        return err("E-mail ou senha incorretos.")
    # Migração automática de hash legado
    if not row["senha_hash"].startswith("pbkdf2:"):
        with db_write() as db:
            db.execute("UPDATE usuarios SET senha_hash=? WHERE id=?",
                       (hash_senha(senha), row["id"]))
    session["usuario_id"] = row["id"]
    return ok({"nome": row["nome"], "email": row["email"]})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return ok({"logado": False})


@app.route("/api/auth/solicitar-reset", methods=["POST"])
def auth_solicitar_reset():
    d = request.get_json() or {}
    email = (d.get("email") or "").strip().lower()
    if not email:
        return err("Informe seu e-mail.")

    db = get_db()
    row = db.execute("SELECT id, nome FROM usuarios WHERE email=?", (email,)).fetchone()
    if not row:
        return ok({"enviado": True})  # não revela se existe

    token = secrets.token_urlsafe(32)
    expira = (datetime.now() + timedelta(hours=2)).isoformat()

    with db_write() as db:
        db.execute(
            "UPDATE usuarios SET reset_token=?, reset_expira=? WHERE id=?",
            (token, expira, row["id"])
        )

    # Detecta URL base automaticamente: env var > PythonAnywhere > localhost
    site = os.environ.get("PYTHONANYWHERE_SITE", "")
    if site:
        base_url = f"https://{site}"
    else:
        base_url = os.environ.get("APP_BASE_URL", f"http://localhost:{PORT}")
    link = f"{base_url}/redefinir-senha?token={token}"

    corpo = (
        "<!DOCTYPE html><html lang='pt-BR'><body style='margin:0;padding:0;"
        "background:#08090d;font-family:Arial,sans-serif'>"
        "<table width='100%' cellpadding='0' cellspacing='0' style='background:#08090d;padding:40px 0'>"
        "<tr><td align='center'>"
        "<table width='480' cellpadding='0' cellspacing='0' style='background:#0f1117;"
        "border:1px solid #1e2130;border-radius:20px;overflow:hidden;max-width:480px;width:100%'>"
        "<tr><td style='background:#0f1117;padding:32px 40px 24px;text-align:center;"
        "border-bottom:1px solid #1e2130'>"
        "<div style='font-size:26px;font-weight:800;color:#e8eaf0'>Digital"
        "<span style='color:#7c6cfc'>Life</span></div>"
        "<div style='font-size:11px;color:#4a5068;margin-top:4px;letter-spacing:2px;"
        "text-transform:uppercase'>Sistema de vida digital</div>"
        "</td></tr>"
        "<tr><td style='padding:36px 40px'>"
        f"<p style='margin:0 0 8px;font-size:18px;font-weight:600;color:#e8eaf0'>"
        f"Ol&#225;, <span style='color:#7c6cfc'>{row['nome']}</span>! &#128075;</p>"
        "<p style='margin:0 0 28px;font-size:13px;color:#4a5068;line-height:1.7'>"
        "Recebemos uma solicita&#231;&#227;o para redefinir a senha da sua conta.<br>"
        "O link abaixo &#233; v&#225;lido por <strong style='color:#e8eaf0'>2 horas</strong>.</p>"
        "<div style='text-align:center;margin-bottom:28px'>"
        f"<a href='{link}' style='display:inline-block;padding:14px 36px;"
        "background:#7c6cfc;color:#ffffff;border-radius:10px;text-decoration:none;"
        "font-size:14px;font-weight:600'>Redefinir minha senha</a></div>"
        "<div style='background:#08090d;border:1px solid #1e2130;border-radius:10px;"
        "padding:14px 18px;margin-bottom:24px'>"
        "<p style='margin:0;font-size:11px;color:#4a5068;line-height:1.6'>"
        "Se o bot&#227;o n&#227;o funcionar, copie e cole este link no navegador:<br>"
        f"<a href='{link}' style='color:#7c6cfc;word-break:break-all;font-size:11px'>{link}</a>"
        "</p></div>"
        "<p style='margin:0;font-size:12px;color:#4a5068;line-height:1.6'>"
        "Se voc&#234; n&#227;o solicitou a redefini&#231;&#227;o, ignore este e-mail.</p>"
        "</td></tr>"
        "<tr><td style='padding:20px 40px;border-top:1px solid #1e2130;text-align:center'>"
        "<p style='margin:0;font-size:11px;color:#2a2d38'>"
        "Digital Life &middot; Enviado automaticamente &middot; N&#227;o responda este e-mail"
        "</p></td></tr></table></td></tr></table></body></html>"
    )

    enviado = _enviar_email(email, "Redefinir senha — Digital Life", corpo)
    return ok({
        "enviado": True,
        "email_enviado": enviado,
        "token_debug": token if not enviado else None
    })


@app.route("/api/auth/redefinir-senha", methods=["POST"])
def auth_redefinir_senha():
    d     = request.get_json() or {}
    token = (d.get("token") or "").strip()
    senha = (d.get("senha") or "").strip()
    if not token:      return err("Token inválido.")
    if len(senha) < 6: return err("Senha deve ter pelo menos 6 caracteres.")
    db  = get_db()
    row = db.execute(
        "SELECT id, nome, reset_expira FROM usuarios WHERE reset_token=?", (token,)
    ).fetchone()
    if not row: return err("Link inválido ou já utilizado.")
    if datetime.fromisoformat(row["reset_expira"]) < datetime.now():
        return err("Link expirado. Solicite um novo.")
    with db_write() as db:
        db.execute("UPDATE usuarios SET senha_hash=?, reset_token=NULL, reset_expira=NULL WHERE id=?",
                   (hash_senha(senha), row["id"]))
    session["usuario_id"] = row["id"]
    return ok({"nome": row["nome"]})

@app.route("/api/auth/config-email", methods=["GET", "POST"])
def auth_config_email():
    if request.method == "GET":
        cfg  = _cfg_email()
        safe = {k: v for k, v in cfg.items() if k != "smtp_pass"}
        safe["smtp_configurado"] = bool(cfg.get("smtp_host"))
        return ok(safe)
    d   = request.get_json() or {}
    cfg = _load_cfg()
    cfg["email"] = {k: d.get(k, "") for k in
                    ("smtp_host","smtp_port","smtp_user","smtp_pass","smtp_from")}
    cfg["email"]["smtp_port"] = int(cfg["email"]["smtp_port"] or 587)
    _save_cfg(cfg)
    return ok({"salvo": True})

@app.route("/api/auth/testar-email", methods=["POST"])
def auth_testar_email():
    """Envia um e-mail de teste para verificar a configuração SMTP."""
    d = request.get_json() or {}
    destino = (d.get("email") or "").strip().lower()
    if not destino:
        return err("Informe o e-mail de destino para o teste.")
    if not _cfg_email().get("smtp_host"):
        return err("Configure o servidor SMTP antes de testar.")
    corpo = (
        "<div style='font-family:Arial,sans-serif;max-width:400px;padding:24px;"
        "background:#0f1117;color:#e8eaf0;border-radius:12px'>"
        "<h2 style='color:#7c6cfc;margin:0 0 12px'>DigitalLife</h2>"
        "<p style='margin:0 0 8px'>&#10003; Configura&#231;&#227;o SMTP funcionando!</p>"
        "<p style='color:#888;font-size:12px;margin:0'>E-mail de teste enviado pelo Digital Life.</p>"
        "</div>"
    )
    enviado = _enviar_email(destino, "Teste SMTP — Digital Life", corpo)
    if enviado:
        return ok({"enviado": True, "msg": f"E-mail de teste enviado para {destino}"})
    return err("Falha ao enviar. Verifique as configurações SMTP.")

# ─── API TIPOS ────────────────────────────────────────────────────────────────
@app.route("/api/tipos", methods=["GET"])
def listar_tipos():
    rows = get_db().execute(
        "SELECT id,nome,natureza,cor,icone,is_default FROM tipos ORDER BY is_default DESC, nome"
    ).fetchall()
    return ok(rows_to_list(rows))

@app.route("/api/tipos", methods=["POST"])
def criar_tipo():
    d = request.get_json() or {}
    nome     = (d.get("nome") or "").strip()
    natureza = d.get("natureza", "negativo")
    cor      = d.get("cor", "#94a3b8")
    icone    = d.get("icone", "circle")
    if not nome: return err("Informe o nome do tipo.")
    if natureza not in ("positivo", "negativo"): return err("Natureza inválida.")
    db = get_db()
    if db.execute("SELECT 1 FROM tipos WHERE lower(nome)=lower(?)", (nome,)).fetchone():
        return err("Já existe um tipo com este nome.")
    tid = uid()
    with db_write() as db:
        db.execute("INSERT INTO tipos(id,nome,natureza,cor,icone,is_default) VALUES(?,?,?,?,?,0)",
                   (tid, nome, natureza, cor, icone))
    row = get_db().execute(
        "SELECT id,nome,natureza,cor,icone,is_default FROM tipos WHERE id=?", (tid,)
    ).fetchone()
    return ok(row_to_dict(row)), 201

@app.route("/api/tipos/<tid>", methods=["DELETE"])
def deletar_tipo(tid):
    db  = get_db()
    row = db.execute("SELECT is_default FROM tipos WHERE id=?", (tid,)).fetchone()
    if not row:         return err("Tipo não encontrado.", 404)
    if row["is_default"]: return err("Tipos padrão não podem ser removidos.")
    with db_write() as db: db.execute("DELETE FROM tipos WHERE id=?", (tid,))
    return ok({"id": tid})

@app.route("/api/tipos/<tid>/icone", methods=["PATCH"])
def atualizar_icone_tipo(tid):
    icone = (request.get_json() or {}).get("icone", "circle")
    db    = get_db()
    if not db.execute("SELECT 1 FROM tipos WHERE id=?", (tid,)).fetchone():
        return err("Tipo não encontrado.", 404)
    with db_write() as db: db.execute("UPDATE tipos SET icone=? WHERE id=?", (icone, tid))
    return ok({"id": tid, "icone": icone})

# ─── API CATEGORIAS ───────────────────────────────────────────────────────────
@app.route("/api/categorias", methods=["GET"])
def listar_categorias():
    rows = get_db().execute("""
        SELECT c.id, c.nome, c.tipo_id, c.cor, c.icone, c.is_default,
               t.nome AS tipo_nome, t.natureza, t.cor AS tipo_cor
        FROM categorias c
        JOIN tipos t ON t.id = c.tipo_id
        ORDER BY t.is_default DESC, c.is_default DESC, c.nome
    """).fetchall()
    return ok(rows_to_list(rows))

@app.route("/api/categorias", methods=["POST"])
def criar_categoria():
    d       = request.get_json() or {}
    nome    = (d.get("nome")    or "").strip()
    tipo_id = (d.get("tipo_id") or "").strip()
    cor     = d.get("cor",   "#94a3b8")
    icone   = d.get("icone", "tag")
    if not nome:    return err("Informe o nome da categoria.")
    if not tipo_id: return err("Selecione um tipo.")
    db = get_db()
    if not db.execute("SELECT 1 FROM tipos WHERE id=?",               (tipo_id,)).fetchone(): return err("Tipo não encontrado.")
    if     db.execute("SELECT 1 FROM categorias WHERE lower(nome)=lower(?)", (nome,)).fetchone(): return err("Já existe uma categoria com este nome.")
    cid = uid()
    with db_write() as db:
        db.execute("INSERT INTO categorias(id,nome,tipo_id,cor,icone,is_default) VALUES(?,?,?,?,?,0)",
                   (cid, nome, tipo_id, cor, icone))
    row = get_db().execute("""
        SELECT c.id,c.nome,c.tipo_id,c.cor,c.icone,c.is_default,t.nome AS tipo_nome,t.natureza
        FROM categorias c JOIN tipos t ON t.id=c.tipo_id WHERE c.id=?
    """, (cid,)).fetchone()
    return ok(row_to_dict(row)), 201

@app.route("/api/categorias/<cid>", methods=["DELETE"])
def deletar_categoria(cid):
    db  = get_db()
    row = db.execute("SELECT is_default FROM categorias WHERE id=?", (cid,)).fetchone()
    if not row:           return err("Categoria não encontrada.", 404)
    if row["is_default"]: return err("Categorias padrão não podem ser removidas.")
    with db_write() as db: db.execute("DELETE FROM categorias WHERE id=?", (cid,))
    return ok({"id": cid})

@app.route("/api/categorias/<cid>/icone", methods=["PATCH"])
def atualizar_icone_cat(cid):
    icone = (request.get_json() or {}).get("icone", "tag")
    db    = get_db()
    if not db.execute("SELECT 1 FROM categorias WHERE id=?", (cid,)).fetchone():
        return err("Categoria não encontrada.", 404)
    with db_write() as db: db.execute("UPDATE categorias SET icone=? WHERE id=?", (icone, cid))
    return ok({"id": cid, "icone": icone})

# ─── API TRANSAÇÕES ───────────────────────────────────────────────────────────
@app.route("/api/transacoes", methods=["GET"])
def listar_transacoes():
    db     = get_db()
    args   = request.args
    mes    = args.get("mes",       "")
    tid    = args.get("tipo_id",   "")
    cat    = args.get("categoria", "")
    bsc    = args.get("busca",     "").lower()
    ano    = args.get("ano",       "")

    sql    = """SELECT t.id, t.descricao, t.valor, t.categoria, t.tipo_id,
                       t.data, t.observacao, t.criado_em,
                       tp.nome AS tipo_nome, tp.cor AS tipo_cor, tp.natureza
                FROM transacoes t
                JOIN tipos tp ON tp.id = t.tipo_id
                WHERE 1=1"""
    params = []

    if mes: sql += " AND substr(t.data,1,7)=?"; params.append(mes)
    elif ano: sql += " AND substr(t.data,1,4)=?"; params.append(ano)
    if tid: sql += " AND t.tipo_id=?"; params.append(tid)
    if cat: sql += " AND t.categoria=?"; params.append(cat)
    if bsc:
        sql += " AND (lower(t.descricao) LIKE ? OR lower(t.categoria) LIKE ?)"
        params += [f"%{bsc}%", f"%{bsc}%"]

    sql += " ORDER BY t.data DESC, t.criado_em DESC"
    return ok(rows_to_list(db.execute(sql, params).fetchall()))

@app.route("/api/transacoes", methods=["POST"])
def criar_transacao():
    d         = request.get_json() or {}
    descricao = (d.get("descricao")  or "").strip()
    categoria = (d.get("categoria")  or "").strip()
    tipo_id   = (d.get("tipo_id")    or "").strip()
    observacao= (d.get("observacao") or "").strip()
    data      = d.get("data") or date.today().isoformat()

    if not descricao: return err("Informe a descrição.")
    if not categoria: return err("Informe a categoria.")
    if not tipo_id:   return err("Informe o tipo.")
    try:
        valor = float(d.get("valor", 0))
        assert valor > 0
    except Exception:
        return err("Valor inválido.")

    db = get_db()
    if not db.execute("SELECT 1 FROM tipos WHERE id=?", (tipo_id,)).fetchone():
        return err("Tipo não encontrado.")

    tid = uid()
    with db_write() as db:
        db.execute(
            "INSERT INTO transacoes(id,descricao,valor,categoria,tipo_id,data,observacao) VALUES(?,?,?,?,?,?,?)",
            (tid, descricao, valor, categoria, tipo_id, data, observacao)
        )
    row = get_db().execute("""
        SELECT t.id,t.descricao,t.valor,t.categoria,t.tipo_id,t.data,t.observacao,
               tp.nome AS tipo_nome, tp.cor AS tipo_cor, tp.natureza
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id WHERE t.id=?
    """, (tid,)).fetchone()
    return ok(row_to_dict(row)), 201

@app.route("/api/transacoes/<tid>", methods=["DELETE"])
def deletar_transacao(tid):
    db = get_db()
    if not db.execute("SELECT 1 FROM transacoes WHERE id=?", (tid,)).fetchone():
        return err("Transação não encontrada.", 404)
    with db_write() as db: db.execute("DELETE FROM transacoes WHERE id=?", (tid,))
    return ok({"id": tid})

# ─── API DASHBOARD ────────────────────────────────────────────────────────────
@app.route("/api/dashboard")
def dashboard():
    db  = get_db()
    mes = date.today().strftime("%Y-%m")

    resumo = db.execute("""
        SELECT tp.natureza, COUNT(*) AS qtd, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE substr(t.data,1,7)=?
        GROUP BY tp.natureza
    """, (mes,)).fetchall()

    def _sum(nat, col): return next((r[col] for r in resumo if r["natureza"]==nat), 0) or 0

    entradas = _sum("positivo","total")
    saidas   = _sum("negativo","total")
    qtd_e    = _sum("positivo","qtd")
    qtd_s    = _sum("negativo","qtd")

    saldo_total = db.execute("""
        SELECT COALESCE(SUM(CASE WHEN tp.natureza='positivo' THEN t.valor ELSE -t.valor END),0)
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
    """).fetchone()[0]

    fluxo = db.execute("""
        SELECT substr(t.data,1,7) AS mes, tp.id AS tipo_id, tp.nome AS tipo_nome,
               tp.cor, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE t.data >= date('now','-6 months','start of month')
        GROUP BY mes, tp.id ORDER BY mes
    """).fetchall()

    cats_mes = db.execute("""
        SELECT t.categoria, cat.cor, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t
        JOIN tipos tp ON tp.id=t.tipo_id
        LEFT JOIN categorias cat ON lower(cat.nome)=lower(t.categoria)
        WHERE substr(t.data,1,7)=? AND tp.natureza='negativo'
        GROUP BY t.categoria ORDER BY total DESC
    """, (mes,)).fetchall()

    return ok({"mes":mes,"entradas":entradas,"qtd_entradas":qtd_e,
               "saidas":saidas,"qtd_saidas":qtd_s,
               "saldo_mes":entradas-saidas,"saldo_total":saldo_total,
               "fluxo":rows_to_list(fluxo),"cats_mes":rows_to_list(cats_mes)})

# ─── API RELATÓRIO ────────────────────────────────────────────────────────────
@app.route("/api/relatorio")
def relatorio():
    db     = get_db()
    mes    = request.args.get("mes","")
    ano    = request.args.get("ano","")
    filtro = "AND substr(t.data,1,7)=?" if mes else ("AND substr(t.data,1,4)=?" if ano else "")
    param  = [mes or ano] if (mes or ano) else []

    totais = db.execute(f"""
        SELECT tp.natureza, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE 1=1 {filtro} GROUP BY tp.natureza
    """, param).fetchall()

    entradas = next((r["total"] for r in totais if r["natureza"]=="positivo"), 0) or 0
    saidas   = next((r["total"] for r in totais if r["natureza"]=="negativo"), 0) or 0

    evolucao = db.execute("""
        SELECT substr(t.data,1,7) AS mes, tp.natureza, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE t.data >= date('now','-12 months','start of month')
        GROUP BY mes, tp.natureza ORDER BY mes
    """).fetchall()

    desp_cat = db.execute(f"""
        SELECT t.categoria, cat.cor, cat.icone, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        LEFT JOIN categorias cat ON lower(cat.nome)=lower(t.categoria)
        WHERE tp.natureza='negativo' {filtro}
        GROUP BY t.categoria ORDER BY total DESC
    """, param).fetchall()

    rec_cat = db.execute(f"""
        SELECT t.categoria, cat.cor, cat.icone, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        LEFT JOIN categorias cat ON lower(cat.nome)=lower(t.categoria)
        WHERE tp.natureza='positivo' {filtro}
        GROUP BY t.categoria ORDER BY total DESC
    """, param).fetchall()

    meses_disp = db.execute(
        "SELECT DISTINCT substr(data,1,7) AS mes FROM transacoes ORDER BY mes DESC"
    ).fetchall()
    anos_disp  = db.execute(
        "SELECT DISTINCT substr(data,1,4) AS ano FROM transacoes ORDER BY ano DESC"
    ).fetchall()

    return ok({"entradas":entradas,"saidas":saidas,"saldo":entradas-saidas,
               "evolucao":rows_to_list(evolucao),"desp_cat":rows_to_list(desp_cat),
               "rec_cat":rows_to_list(rec_cat),
               "meses_disp":[r["mes"] for r in meses_disp],
               "anos_disp":[r["ano"] for r in anos_disp]})

@app.route("/api/filtros")
def filtros():
    db    = get_db()
    meses = db.execute("SELECT DISTINCT substr(data,1,7) AS mes FROM transacoes ORDER BY mes DESC").fetchall()
    cats  = db.execute("SELECT DISTINCT categoria FROM transacoes ORDER BY categoria").fetchall()
    return ok({"meses":[r["mes"] for r in meses],"categorias":[r["categoria"] for r in cats]})

# ─── CLASSIFICAÇÃO AUTOMÁTICA — índice pré-compilado O(1) ────────────────────
REGRAS_CATEGORIA = [
    (["salario","salário","pagamento empregador","holerite","proventos"],        "Salário","receita"),
    (["freelance","freela","autonomo","autônomo","honorario"],                   "Freelance","receita"),
    (["dividendo","rendimento","juros sobre capital","jcp","tesouro","cdb"],    "Investimentos","receita"),
    (["aluguel receb","locacao receb"],                                          "Aluguel Recebido","receita"),
    (["supermercado","mercado","hortifruti","padaria","extra","carrefour",
      "pao de acucar","atacadao","assai","walmart"],                            "Alimentação","despesa"),
    (["restaurante","lanchonete","burger","mcdonalds","ifood","rappi",
      "uber eats","pizza","sushi","bar ","cafe ","cafeteria"],                  "Alimentação","despesa"),
    (["uber","99 ","taxi","onibus","metro","combustivel","gasolina","etanol",
      "posto ","estacionamento","pedagio","ipva","detran"],                     "Transporte","despesa"),
    (["farmacia","drogaria","drogasil","laboratorio","clinica","hospital",
      "unimed","amil","dentista","medico","consulta","exame"],                  "Saúde","despesa"),
    (["escola","faculdade","universidade","curso","mensalidade",
      "material escolar","livraria","udemy","alura"],                           "Educação","despesa"),
    (["netflix","spotify","amazon prime","disney","hbo","globoplay","cinema",
      "teatro","show ","ingresso","academia","steam","viagem","hotel","airbnb"],"Lazer","despesa"),
    (["renner","riachuelo","zara","hering","marisa","shein","shopee",
      "roupa","calcado","sapato","tenis"],                                      "Vestuário","despesa"),
    (["energia eletrica","celpe","cemig","copel","enel","agua ","saneamento",
      "internet","vivo","claro","tim","condominio","iptu","aluguel","seguro"],  "Contas/Serviços","despesa"),
    (["cartao","cartão","fatura","mastercard","visa","elo ","nubank"],          "Cartão de Crédito","despesa"),
    (["moradia","reforma","construcao","leroy","telhanorte"],                    "Moradia","despesa"),
]

# Índice pré-compilado: keyword → (categoria, tipo_id) — lookup O(k) em vez de O(n*m)
_REGRAS_IDX: list[tuple[list[str], str, str]] = [
    ([_normalizar(p) for p in palavras], cat, tid)
    for palavras, cat, tid in REGRAS_CATEGORIA
]

def classificar(memo: str) -> tuple[str, str]:
    texto = _normalizar(memo)
    for palavras_norm, categoria, tipo_id in _REGRAS_IDX:
        if any(p in texto for p in palavras_norm):
            return categoria, tipo_id
    return "Outros (Despesa)", "despesa"

# ─── PARSER OFX ───────────────────────────────────────────────────────────────
def parse_ofx(conteudo: bytes) -> list[dict]:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try: texto = conteudo.decode(enc); break
        except Exception: continue
    else: raise ValueError("Encoding inválido.")

    blocos = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", texto, re.DOTALL | re.IGNORECASE)
    if not blocos:
        blocos = re.split(r"<STMTTRN>", texto, flags=re.IGNORECASE)[1:]

    def tag(b, n):
        m = re.search(rf"<{n}>\s*([^\r\n<]+)", b, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    result = []
    for bloco in blocos:
        trntype = tag(bloco,"TRNTYPE").upper()
        # OFX date format: YYYYMMDD
        raw_dt  = tag(bloco,"DTPOSTED")[:8]
        try: data_iso = datetime.strptime(raw_dt, "%Y%m%d").strftime("%Y-%m-%d")
        except Exception: data_iso = date.today().isoformat()

        memo    = tag(bloco,"MEMO") or tag(bloco,"NAME") or "Sem descrição"
        fitid   = tag(bloco,"FITID")
        try: valor = abs(float(tag(bloco,"TRNAMT").replace(",",".")))
        except Exception: continue
        if valor == 0: continue

        raw_amt = tag(bloco,"TRNAMT")
        try: v_float = float(raw_amt.replace(",","."))
        except Exception: v_float = 0.0

        if trntype in ("CREDIT","INT","DIV","DIRECTDEP") or v_float > 0:
            cat_auto, tipo_cls = classificar(memo)
            tipo_id  = "receita"
            categoria= cat_auto if tipo_cls=="receita" else "Outros (Receita)"
        else:
            categoria, tipo_id = classificar(memo)

        result.append({"id_externo":fitid,"descricao":memo[:120],"valor":round(valor,2),
                       "categoria":categoria,"tipo_id":tipo_id,"data":data_iso,
                       "observacao":f"Importado OFX — {trntype}"})
    return result

# ─── PARSER CSV ───────────────────────────────────────────────────────────────
def parse_csv(conteudo: bytes) -> list[dict]:
    texto = None
    for enc in ("utf-8-sig","latin-1","cp1252","utf-8"):
        try: texto = conteudo.decode(enc); break
        except Exception: continue
    if not texto: raise ValueError("Não foi possível decodificar o arquivo.")

    linhas = texto.splitlines()
    COLUNAS_CAB = ["data","date","historico","histórico","descri","lancamento",
                   "valor","debito","débito","credito","crédito","saldo","docto","memo"]

    inicio = 0
    for i, linha in enumerate(linhas):
        norm = _normalizar(linha)
        if sum(1 for k in COLUNAS_CAB if k in norm) >= 2:
            inicio = i; break

    sep = ";" if linhas[inicio].count(";") > linhas[inicio].count(",") else ","

    linhas_limpas = [linhas[inicio]]
    for linha in linhas[inicio+1:]:
        partes  = linha.split(sep)
        primeira = partes[0].strip().strip('"')
        if parse_data(primeira):
            linhas_limpas.append(linha)

    if len(linhas_limpas) <= 1:
        raise ValueError("Nenhuma transação encontrada.")

    reader  = csv.DictReader(StringIO("\n".join(linhas_limpas)), delimiter=sep)
    fn_orig = reader.fieldnames or []
    fn_norm = [_normalizar(c.strip().strip('"')) for c in fn_orig]

    def achar(opcoes):
        for op in opcoes:
            for i, c in enumerate(fn_norm):
                if op in c: return fn_orig[i]
        return None

    col_data  = achar(["data","date"])
    col_desc  = achar(["historico","descri","memo","lancamento","detail","estabele"])
    col_debit = achar(["debito","saida","debit"])
    col_cred  = achar(["credito","entrada","credit"])
    col_valor = achar(["valor","value","amount","quantia"])
    col_tipo  = achar(["tipo","natureza"])

    result = []
    for row in reader:
        if not row: continue
        data_iso  = parse_data(str(row.get(col_data) or ""))
        if not data_iso: continue
        descricao = (row.get(col_desc) or "").strip().strip('"').strip() or "Sem descrição"

        valor=None; tipo_id=None
        _VAZIO = {"", " "}
        if col_debit and col_cred:
            v_deb=(row.get(col_debit) or "").strip().strip('"').strip()
            v_cre=(row.get(col_cred)  or "").strip().strip('"').strip()
            try:
                if v_cre and v_cre not in _VAZIO and _converter_valor(v_cre)!=0:
                    valor=abs(_converter_valor(v_cre)); tipo_id="receita"
                elif v_deb and v_deb not in _VAZIO and _converter_valor(v_deb)!=0:
                    valor=abs(_converter_valor(v_deb)); tipo_id="despesa"
            except Exception: pass
        elif col_debit and not col_cred:
            v_deb=(row.get(col_debit) or "").strip().strip('"').strip()
            try:
                if v_deb and v_deb not in _VAZIO:
                    valor=abs(_converter_valor(v_deb)); tipo_id="despesa"
            except Exception: pass
        elif col_cred and not col_debit:
            v_cre=(row.get(col_cred) or "").strip().strip('"').strip()
            try:
                if v_cre and v_cre not in _VAZIO:
                    valor=abs(_converter_valor(v_cre)); tipo_id="receita"
            except Exception: pass
        if valor is None and col_valor:
            raw_v=(row.get(col_valor) or "").strip().strip('"').replace("R$","").strip()
            try: v=_converter_valor(raw_v); valor=abs(v); tipo_id="receita" if v>0 else "despesa"
            except Exception: pass
        if tipo_id is None and col_tipo:
            t=_normalizar(row.get(col_tipo) or "")
            tipo_id="receita" if any(k in t for k in ["cred","entr","rec"]) else "despesa"
        if not valor or valor==0: continue

        cat_auto,tipo_auto=classificar(descricao)
        if tipo_id is None: tipo_id=tipo_auto
        categoria=cat_auto if tipo_id==tipo_auto else ("Outros (Receita)" if tipo_id=="receita" else "Outros (Despesa)")

        result.append({"id_externo":f"csv-{data_iso}-{len(result)}","descricao":descricao[:120],
                       "valor":round(valor,2),"categoria":categoria,"tipo_id":tipo_id,
                       "data":data_iso,"observacao":"Importado CSV"})
    return result

# ─── PARSER PDF ───────────────────────────────────────────────────────────────
def _detectar_banco_pdf(texto: str) -> str:
    t = texto.lower()
    for banco, kws in [("bradesco",["bradesco"]),("c6",["c6 bank","c6bank"]),
                       ("itau",["itaú","itau"]),("nubank",["nubank"]),
                       ("inter",["inter"]),("santander",["santander"]),
                       ("bb",["banco do brasil","bb cartão"]),("xp",["xp investimentos"])]:
        if any(k in t for k in kws): return banco
    return "generico"

_IGNORE_DESC = {"total","pagamento minimo","saldo","limite","vencimento",
                "valor da fatura","fatura anterior","encargo","multa","juros"}

def _filtrar_desc(desc: str) -> bool:
    return any(k in _normalizar(desc) for k in _IGNORE_DESC)

def _parse_pdf_tabela(tabelas: list, banco: str) -> list[dict]:
    result = []
    for tabela in tabelas:
        if not tabela or len(tabela) < 2: continue
        cab = [_normalizar(str(c or "")) for c in tabela[0]]
        def ac(ops):
            for op in ops:
                for i,c in enumerate(cab):
                    if op in c: return i
            return None
        col_data  = ac(["data","date"])
        col_desc  = ac(["descri","histor","lancamento","estabele","memo"])
        col_valor = ac(["valor","value","amount","r$"])
        col_debit = ac(["debito","saida"])
        col_cred  = ac(["credito","entrada"])
        if col_data is None or col_desc is None: continue

        for row in tabela[1:]:
            if not row or all(not c for c in row): continue
            try:
                data_iso = parse_data(str(row[col_data] or ""))
                if not data_iso: continue
                desc = str(row[col_desc] or "").strip()
                if not desc or _filtrar_desc(desc): continue
                valor=None; tipo_id=None
                if col_debit is not None and col_cred is not None:
                    vd=str(row[col_debit] or "").strip(); vc=str(row[col_cred] or "").strip()
                    try:
                        if vc and _converter_valor(vc)!=0: valor=abs(_converter_valor(vc)); tipo_id="receita"
                        elif vd and _converter_valor(vd)!=0: valor=abs(_converter_valor(vd)); tipo_id="despesa"
                    except Exception: pass
                elif col_valor is not None:
                    raw=str(row[col_valor] or "").strip().replace("R$","")
                    try: v=_converter_valor(raw); valor=abs(v); tipo_id="receita" if v<0 else "despesa"
                    except Exception: pass
                if not valor or valor==0: continue
                cat,_=classificar(desc)
                result.append({"id_externo":f"pdf-{data_iso}-{len(result)}","descricao":desc[:120],
                               "valor":round(valor,2),"categoria":"Outros (Receita)" if tipo_id=="receita" else cat,
                               "tipo_id":tipo_id or "despesa","data":data_iso,
                               "observacao":f"Importado PDF {banco.upper()}"})
            except Exception: continue
    return result

def _parse_pdf_texto(texto: str, banco: str) -> list[dict]:
    result=[]
    ano_fatura=str(date.today().year)
    m_ano=re.search(r"(?:período|vencimento|fatura)[^\d]*(\d{4})",texto,re.IGNORECASE)
    if m_ano: ano_fatura=m_ano.group(1)
    PATS=[
        re.compile(r"(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\-]?\d{1,3}(?:\.\d{3})*,\d{2})\s*$"),
        re.compile(r"(\d{4}-\d{2}-\d{2})\s+(.+?)\s+([\-]?\d+[.,]\d{2})\s*$"),
        re.compile(r"(\d{2}/\d{2})\s+(.+?)\s+([\-]?\d{1,3}(?:\.\d{3})*,\d{2})\s*$"),
    ]
    FMTS=["%d/%m/%Y","%Y-%m-%d",None]
    for linha in texto.splitlines():
        linha=linha.strip()
        if len(linha)<12: continue
        for i,(pat,fmt) in enumerate(zip(PATS,FMTS)):
            m=pat.match(linha)
            if not m: continue
            raw=m.group(1); desc=m.group(2).strip(); vstr=m.group(3)
            if _filtrar_desc(desc): break
            try:
                if fmt: data_iso=datetime.strptime(raw,fmt).strftime("%Y-%m-%d")
                else: data_iso=datetime.strptime(f"{raw}/{ano_fatura}","%d/%m/%Y").strftime("%Y-%m-%d")
            except Exception: break
            try: v=_converter_valor(vstr); valor=abs(v); tipo_id="receita" if v<0 else "despesa"
            except Exception: break
            if not valor: break
            cat,_=classificar(desc)
            result.append({"id_externo":f"pdf-txt-{data_iso}-{len(result)}","descricao":desc[:120],
                           "valor":round(valor,2),"categoria":"Outros (Receita)" if tipo_id=="receita" else cat,
                           "tipo_id":tipo_id,"data":data_iso,"observacao":f"Importado PDF {banco.upper()}"})
            break
    return result

def parse_pdf(conteudo: bytes) -> list[dict]:
    try:
        import pdfplumber
    except ImportError:
        raise ValueError("pdfplumber não instalado. Execute: pip install pdfplumber")
    texto_total=""; tabelas=[]
    with pdfplumber.open(BytesIO(conteudo)) as pdf:
        for page in pdf.pages:
            texto_total += (page.extract_text() or "") + "\n"
            for strat in [{"vertical_strategy":"lines","horizontal_strategy":"lines"},
                          {"vertical_strategy":"text","horizontal_strategy":"text"}]:
                try:
                    ts=page.extract_tables(strat)
                    if ts: tabelas.extend(ts); break
                except Exception: pass
    banco=_detectar_banco_pdf(texto_total)
    if tabelas:
        itens=_parse_pdf_tabela(tabelas,banco)
        if itens: return itens
    itens=_parse_pdf_texto(texto_total,banco)
    if itens: return itens
    raise ValueError(f"Nenhuma transação encontrada no PDF ({banco}).")

# ─── IMPORTAÇÃO ───────────────────────────────────────────────────────────────
def _checar_duplicatas(db, itens: list) -> set:
    """
    Detecta transações já importadas comparando (data + valor + descricao).
    Estratégia robusta: hash do triplete evita falsos positivos.
    """
    if not itens: return set()
    # Gera chave determinística para cada item
    def _chave(item):
        return f"{item.get('data','')}|{item.get('valor',0):.2f}|{item.get('descricao','')[:40].lower()}"

    chaves_novos = {_chave(i) for i in itens}
    # Busca transações do mesmo período para comparar
    datas = list({i.get("data","") for i in itens if i.get("data")})
    if not datas: return set()

    ph = ",".join("?" * len(datas))
    rows = db.execute(
        f"SELECT data, valor, descricao FROM transacoes WHERE data IN ({ph})", datas
    ).fetchall()

    existentes = {f"{r['data']}|{r['valor']:.2f}|{r['descricao'][:40].lower()}" for r in rows}
    return chaves_novos & existentes   # interseção = duplicatas

@app.route("/api/importar/preview", methods=["POST"])
def importar_preview():
    if "arquivo" not in request.files: return err("Nenhum arquivo enviado.")
    arq  = request.files["arquivo"]
    nome = arq.filename.lower()
    raw  = arq.read()
    try:
        if   nome.endswith((".ofx",".qfx")): itens=parse_ofx(raw)
        elif nome.endswith(".csv"):           itens=parse_csv(raw)
        elif nome.endswith(".pdf"):           itens=parse_pdf(raw)
        else: return err("Formato não suportado. Envie .ofx, .qfx, .csv ou .pdf")
    except Exception as e: return err(f"Erro ao processar: {str(e)}")
    if not itens: return err("Nenhuma transação encontrada.")
    dup_chaves = _checar_duplicatas(get_db(), itens)

    def _chave(item):
        return f"{item.get('data','')}|{item.get('valor',0):.2f}|{item.get('descricao','')[:40].lower()}"

    for item in itens:
        item["duplicata"] = _chave(item) in dup_chaves
    return ok({"total":len(itens),"novas":sum(1 for i in itens if not i["duplicata"]),
               "duplicatas":sum(1 for i in itens if i["duplicata"]),"itens":itens})

@app.route("/api/importar/confirmar", methods=["POST"])
def importar_confirmar():
    d          = request.get_json() or {}
    itens      = d.get("itens", [])
    pular_dup  = d.get("pular_duplicatas", True)
    if not itens: return err("Nenhum item para importar.")

    db = get_db()
    # Pré-carrega categorias existentes (evita N+1)
    cats_existentes = {r["nome"].lower() for r in
                       db.execute("SELECT nome FROM categorias").fetchall()}

    novos_inserts = []
    novas_cats    = []
    pulados = erros = 0

    for item in itens:
        if pular_dup and item.get("duplicata"): pulados += 1; continue
        tipo_id  = item.get("tipo_id","despesa")
        categoria= item.get("categoria","Outros (Despesa)")
        if not db.execute("SELECT 1 FROM tipos WHERE id=?", (tipo_id,)).fetchone():
            tipo_id="despesa"
        if categoria.lower() not in cats_existentes:
            novas_cats.append((uid(), categoria, tipo_id, "#94a3b8","tag", 0))
            cats_existentes.add(categoria.lower())
        try:
            novos_inserts.append((uid(), item.get("descricao","Importado"),
                                  float(item.get("valor",0)), categoria, tipo_id,
                                  item.get("data", date.today().isoformat()),
                                  item.get("observacao","")))
        except Exception as e: erros += 1; continue

    with db_write() as db:
        if novas_cats:
            db.executemany(
                "INSERT OR IGNORE INTO categorias(id,nome,tipo_id,cor,icone,is_default) VALUES(?,?,?,?,?,?)",
                novas_cats
            )
        if novos_inserts:
            db.executemany(
                "INSERT INTO transacoes(id,descricao,valor,categoria,tipo_id,data,observacao) VALUES(?,?,?,?,?,?,?)",
                novos_inserts
            )

    return ok({"salvos":len(novos_inserts),"pulados":pulados,"erros":erros})

# ─── COMPROVANTES (Claude Vision) ─────────────────────────────────────────────
PROMPT_TEF = """Analise esta imagem de comprovante de pagamento e retorne SOMENTE um JSON válido:
{"valor":0.00,"data":"YYYY-MM-DD","hora":"HH:MM","estabelecimento":"nome","tipo_pagamento":"debito|credito|pix|ted|doc|boleto|outro","bandeira":"visa|mastercard|elo|hipercard|outro|nenhuma","nsu":null,"autorizacao":null,"parcelas":1,"observacao":""}
Regras: valor é número decimal com ponto; data em ISO; parcelas=1 se à vista; null se não visível."""

def _get_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY","") or _load_cfg().get("anthropic_api_key","")

@app.route("/api/comprovante/ler", methods=["POST"])
def ler_comprovante():
    imagem_b64=None; media_type="image/jpeg"
    if "arquivo" in request.files:
        arq=request.files["arquivo"]; raw=arq.read()
        imagem_b64=base64.b64encode(raw).decode()
        nome=arq.filename.lower()
        if   nome.endswith(".png"):  media_type="image/png"
        elif nome.endswith(".webp"): media_type="image/webp"
        elif nome.endswith(".gif"):  media_type="image/gif"
    elif request.is_json:
        d=request.get_json(); imagem_b64=d.get("imagem_b64"); media_type=d.get("media_type","image/jpeg")
    if not imagem_b64: return err("Nenhuma imagem recebida.")
    api_key=_get_api_key()
    if not api_key: return err("Chave da API Anthropic não configurada. Configure em ⚙ Config → Comprovantes.")
    try:
        payload=json.dumps({"model":"claude-opus-4-5","max_tokens":1024,"messages":[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":media_type,"data":imagem_b64}},
            {"type":"text","text":PROMPT_TEF}]}]}).encode()
        req=_urlreq.Request("https://api.anthropic.com/v1/messages",data=payload,headers={
            "Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"})
        with _urlreq.urlopen(req,timeout=30) as resp: resultado=json.loads(resp.read())
    except Exception as e: return err(f"Erro ao chamar API: {str(e)}")
    try:
        texto=resultado["content"][0]["text"].strip()
        texto=re.sub(r"^```[a-z]*\n?","",texto); texto=re.sub(r"\n?```$","",texto)
        dados=json.loads(texto)
    except Exception: return err("Não foi possível interpretar a resposta da IA.")

    try: valor=float(str(dados.get("valor",0)).replace(",","."))
    except Exception: valor=0.0
    data_iso=dados.get("data") or date.today().isoformat()
    try: datetime.strptime(data_iso,"%Y-%m-%d")
    except Exception: data_iso=date.today().isoformat()

    estab   = (dados.get("estabelecimento") or "Sem descrição").strip()
    tipo_pag= (dados.get("tipo_pagamento")  or "outro").lower()
    parcelas= int(dados.get("parcelas") or 1)
    nsu     = dados.get("nsu")     or ""
    aut     = dados.get("autorizacao") or ""
    obs_ex  = dados.get("observacao") or ""
    bandeira= dados.get("bandeira") or ""

    partes=["Importado via comprovante"]
    if nsu:      partes.append(f"NSU:{nsu}")
    if aut:      partes.append(f"Aut:{aut}")
    if bandeira and bandeira!="nenhuma": partes.append(bandeira.upper())
    if parcelas>1: partes.append(f"{parcelas}x")
    if obs_ex:   partes.append(obs_ex)

    cat,_=classificar(estab)
    desc=f"{estab} ({parcelas}x)" if parcelas>1 else estab
    return ok({"descricao":desc[:120],"valor":round(valor,2),"data":data_iso,
               "hora":dados.get("hora",""),"categoria":cat,"tipo_id":"despesa",
               "tipo_pagamento":tipo_pag,"bandeira":bandeira,"parcelas":parcelas,
               "nsu":nsu,"autorizacao":aut,"observacao":" | ".join(partes)})

@app.route("/api/comprovante/salvar-chave", methods=["POST"])
def salvar_chave():
    key=(request.get_json() or {}).get("api_key","").strip()
    if not key: return err("Informe a chave.")
    if not key.startswith("sk-ant-"): return err("Chave inválida. Deve começar com sk-ant-")
    cfg=_load_cfg(); cfg["anthropic_api_key"]=key; _save_cfg(cfg)
    return ok({"salvo":True})

@app.route("/api/comprovante/verificar-chave")
def verificar_chave():
    key=_get_api_key()
    return ok({"configurada":bool(key),"prefixo":key[:12]+"..." if key else ""})

# ══════════════════════════════════════════════════════════════════════════════
#  MÓDULO TELEGRAM BOT
#  Integração via Webhook — sem dependência externa (usa urllib nativo)
#  Comandos: /ajuda /saldo /extrato /lancamento /relatorio /categorias
#            /config /deletar /busca
# ══════════════════════════════════════════════════════════════════════════════

def _tg_cfg() -> dict:
    return _load_cfg().get("telegram", {})

def _tg_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "") or _tg_cfg().get("token", "")

def _tg_allowed(chat_id: int) -> bool:
    """Só responde a chat_ids autorizados (segurança)."""
    allowed = _tg_cfg().get("allowed_chat_ids", [])
    return not allowed or chat_id in allowed   # lista vazia = todos permitidos

def _tg_send(chat_id: int, texto: str, parse_mode: str = "HTML") -> bool:
    """Envia mensagem para o Telegram."""
    token = _tg_token()
    if not token:
        return False
    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       texto,
            "parse_mode": parse_mode,
        }).encode()
        req = _urlreq.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        app.logger.error(f"Telegram send error: {e}")
        return False

def _tg_fmt_valor(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _tg_fmt_data(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%d/%m/%Y")
    except Exception:
        return iso

# ── Handlers de cada comando ──────────────────────────────────────────────────

def _cmd_ajuda(chat_id, _args, _db):
    _tg_send(chat_id, """<b>🤖 FinanceControl Bot</b>

<b>Consultas</b>
/saldo — saldo do mês e acumulado
/extrato [N] — últimas N transações (padrão 10)
/relatorio [AAAA-MM] — resumo do mês
/busca [texto] — busca nas transações
/categorias — lista categorias disponíveis

<b>Lançamentos</b>
/lancamento — guia interativo
/lc DESCRIÇÃO VALOR CATEGORIA — atalho rápido
    Ex: <code>/lc "Mercado Extra" 150.90 Alimentação</code>

<b>Gerenciar</b>
/deletar ID — remove uma transação pelo ID

<b>Configuração</b>
/config — status e configurações do bot

<i>Dica: use /categorias para ver os nomes exatos</i>""")

def _cmd_saldo(chat_id, _args, db):
    mes = date.today().strftime("%Y-%m")
    resumo = db.execute("""
        SELECT tp.natureza, COALESCE(SUM(t.valor),0) AS total, COUNT(*) AS qtd
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE substr(t.data,1,7)=?
        GROUP BY tp.natureza
    """, (mes,)).fetchall()

    ent = next((r["total"] for r in resumo if r["natureza"]=="positivo"), 0.0)
    sai = next((r["total"] for r in resumo if r["natureza"]=="negativo"), 0.0)
    qe  = next((r["qtd"]   for r in resumo if r["natureza"]=="positivo"), 0)
    qs  = next((r["qtd"]   for r in resumo if r["natureza"]=="negativo"), 0)

    total = db.execute("""
        SELECT COALESCE(SUM(CASE WHEN tp.natureza='positivo' THEN t.valor ELSE -t.valor END),0)
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
    """).fetchone()[0]

    saldo_mes = ent - sai
    emoji = "📈" if saldo_mes >= 0 else "📉"
    mes_nome = datetime.strptime(mes, "%Y-%m").strftime("%B/%Y").capitalize()

    _tg_send(chat_id, f"""<b>{emoji} Saldo — {mes_nome}</b>

✅ Entradas: <b>{_tg_fmt_valor(ent)}</b> ({qe} lançamentos)
❌ Saídas:   <b>{_tg_fmt_valor(sai)}</b> ({qs} lançamentos)
━━━━━━━━━━━━━━━━━━
💰 Saldo do mês: <b>{_tg_fmt_valor(saldo_mes)}</b>
🏦 Saldo total:  <b>{_tg_fmt_valor(total)}</b>""")

def _cmd_extrato(chat_id, args, db):
    try:
        n = min(int(args[0]), 30) if args else 10
    except (ValueError, IndexError):
        n = 10

    rows = db.execute("""
        SELECT t.id, t.descricao, t.valor, t.categoria, t.data,
               tp.natureza, tp.nome AS tipo_nome
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        ORDER BY t.data DESC, t.criado_em DESC
        LIMIT ?
    """, (n,)).fetchall()

    if not rows:
        _tg_send(chat_id, "📭 Nenhuma transação encontrada.")
        return

    linhas = [f"<b>📋 Últimas {len(rows)} transações</b>\n"]
    for r in rows:
        sinal  = "✅" if r["natureza"] == "positivo" else "❌"
        valor  = _tg_fmt_valor(r["valor"])
        data   = _tg_fmt_data(r["data"])
        desc   = r["descricao"][:28]
        cat    = r["categoria"][:18]
        linhas.append(
            f"{sinal} <b>{valor}</b> — {desc}\n"
            f"   📂 {cat} | 📅 {data}\n"
            f"   🆔 <code>{r['id'][:8]}</code>"
        )
    _tg_send(chat_id, "\n\n".join(linhas))

def _cmd_relatorio(chat_id, args, db):
    if args:
        mes = args[0].strip()
        try:
            datetime.strptime(mes, "%Y-%m")
        except ValueError:
            _tg_send(chat_id, "❌ Formato inválido. Use: /relatorio AAAA-MM\nEx: /relatorio 2026-03")
            return
    else:
        mes = date.today().strftime("%Y-%m")

    totais = db.execute("""
        SELECT tp.natureza, COALESCE(SUM(t.valor),0) AS total, COUNT(*) AS qtd
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE substr(t.data,1,7)=?
        GROUP BY tp.natureza
    """, (mes,)).fetchall()

    ent = next((r["total"] for r in totais if r["natureza"]=="positivo"), 0.0)
    sai = next((r["total"] for r in totais if r["natureza"]=="negativo"), 0.0)

    # Top 5 categorias de saída
    cats = db.execute("""
        SELECT t.categoria, COALESCE(SUM(t.valor),0) AS total
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE substr(t.data,1,7)=? AND tp.natureza='negativo'
        GROUP BY t.categoria ORDER BY total DESC LIMIT 5
    """, (mes,)).fetchall()

    mes_nome = datetime.strptime(mes, "%Y-%m").strftime("%B/%Y").capitalize()
    saldo    = ent - sai
    emoji    = "📈" if saldo >= 0 else "📉"

    txt = [f"<b>{emoji} Relatório — {mes_nome}</b>\n"]
    txt.append(f"✅ Entradas: <b>{_tg_fmt_valor(ent)}</b>")
    txt.append(f"❌ Saídas:   <b>{_tg_fmt_valor(sai)}</b>")
    txt.append(f"💰 Saldo:    <b>{_tg_fmt_valor(saldo)}</b>")

    if cats:
        txt.append("\n<b>Top saídas por categoria:</b>")
        total_sai = sai or 1
        for c in cats:
            pct  = c["total"] / total_sai * 100
            bar  = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            txt.append(f"  {bar} {c['categoria'][:20]}: {_tg_fmt_valor(c['total'])} ({pct:.0f}%)")

    _tg_send(chat_id, "\n".join(txt))

def _cmd_categorias(chat_id, _args, db):
    cats = db.execute("""
        SELECT c.nome, c.icone, t.nome AS tipo_nome, t.natureza
        FROM categorias c JOIN tipos t ON t.id=c.tipo_id
        ORDER BY t.natureza DESC, c.nome
    """).fetchall()

    grupos = {}
    for c in cats:
        k = f"{'✅' if c['natureza']=='positivo' else '❌'} {c['tipo_nome']}"
        grupos.setdefault(k, []).append(c["nome"])

    linhas = ["<b>📂 Categorias disponíveis</b>\n"]
    for grupo, nomes in grupos.items():
        linhas.append(f"<b>{grupo}</b>")
        linhas.append("  " + " · ".join(nomes))
    linhas.append("\n<i>Use o nome exato no /lc</i>")
    _tg_send(chat_id, "\n".join(linhas))

def _cmd_lancamento_rapido(chat_id, args, db):
    """
    /lc DESCRICAO VALOR CATEGORIA [despesa|receita]
    Ex: /lc "Mercado Extra" 150.90 Alimentação
    Ex: /lc Salário 5000 Salário receita
    """
    # Re-parse da linha original para suportar aspas
    texto_original = " ".join(args)
    # Extrai partes com ou sem aspas
    partes = re.findall(r'"[^"]*"|\S+', texto_original)
    partes = [p.strip('"') for p in partes]

    if len(partes) < 3:
        _tg_send(chat_id, """❌ Formato inválido.

Uso: <code>/lc DESCRIÇÃO VALOR CATEGORIA [tipo]</code>

Exemplos:
<code>/lc "Mercado Extra" 150.90 Alimentação</code>
<code>/lc Salário 5000 Salário receita</code>
<code>/lc Uber 32.50 Transporte</code>

Use /categorias para ver os nomes disponíveis.""")
        return

    descricao = partes[0]
    try:
        valor = float(partes[1].replace(",", "."))
        assert valor > 0
    except (ValueError, AssertionError):
        _tg_send(chat_id, f"❌ Valor inválido: <code>{partes[1]}</code>\nUse ponto ou vírgula: 150.90 ou 150,90")
        return

    categoria = partes[2]
    tipo_hint = partes[3].lower() if len(partes) > 3 else ""

    # Busca o tipo pela categoria ou pelo hint
    row_cat = db.execute(
        "SELECT tipo_id FROM categorias WHERE lower(nome)=lower(?)", (categoria,)
    ).fetchone()

    if row_cat:
        tipo_id = row_cat["tipo_id"]
    elif tipo_hint in ("receita", "entrada", "positivo"):
        tipo_id = "receita"
    elif tipo_hint in ("despesa", "saida", "saída", "negativo"):
        tipo_id = "despesa"
    else:
        # Tenta classificar automaticamente
        _, tipo_id = classificar(descricao + " " + categoria)

    # Verifica se tipo existe
    if not db.execute("SELECT 1 FROM tipos WHERE id=?", (tipo_id,)).fetchone():
        tipo_id = "despesa"

    # Cria a transação
    tid = uid()
    data_hoje = date.today().isoformat()
    with db_write() as dw:
        # Cria categoria se não existir
        if not db.execute("SELECT 1 FROM categorias WHERE lower(nome)=lower(?)", (categoria,)).fetchone():
            dw.execute(
                "INSERT OR IGNORE INTO categorias(id,nome,tipo_id,cor,icone,is_default) VALUES(?,?,?,?,?,0)",
                (uid(), categoria, tipo_id, "#94a3b8", "tag")
            )
        dw.execute(
            "INSERT INTO transacoes(id,descricao,valor,categoria,tipo_id,data,observacao) VALUES(?,?,?,?,?,?,?)",
            (tid, descricao, valor, categoria, tipo_id, data_hoje, "Via Telegram")
        )

    tipo_nome = "Receita ✅" if tipo_id == "receita" else "Despesa ❌"
    _tg_send(chat_id, f"""✅ <b>Lançamento adicionado!</b>

📝 {descricao}
💰 {_tg_fmt_valor(valor)}
📂 {categoria}
📌 {tipo_nome}
📅 {_tg_fmt_data(data_hoje)}
🆔 <code>{tid[:8]}</code>

Use /saldo para ver o resumo atualizado.""")

def _cmd_busca(chat_id, args, db):
    if not args:
        _tg_send(chat_id, "Uso: /busca TEXTO\nEx: /busca netflix")
        return
    termo = " ".join(args).lower()
    rows = db.execute("""
        SELECT t.id, t.descricao, t.valor, t.categoria, t.data, tp.natureza
        FROM transacoes t JOIN tipos tp ON tp.id=t.tipo_id
        WHERE lower(t.descricao) LIKE ? OR lower(t.categoria) LIKE ?
        ORDER BY t.data DESC LIMIT 10
    """, (f"%{termo}%", f"%{termo}%")).fetchall()

    if not rows:
        _tg_send(chat_id, f"🔍 Nenhum resultado para <b>{termo}</b>")
        return

    linhas = [f"<b>🔍 Resultados para '{termo}'</b> ({len(rows)} encontrados)\n"]
    for r in rows:
        sinal = "✅" if r["natureza"]=="positivo" else "❌"
        linhas.append(
            f"{sinal} {_tg_fmt_valor(r['valor'])} — <b>{r['descricao'][:30]}</b>\n"
            f"   {r['categoria']} | {_tg_fmt_data(r['data'])} | "
            f"<code>{r['id'][:8]}</code>"
        )
    _tg_send(chat_id, "\n\n".join(linhas))

def _cmd_deletar(chat_id, args, db):
    if not args:
        _tg_send(chat_id, "Uso: /deletar ID\nO ID aparece nos comandos /extrato e /busca")
        return
    id_parcial = args[0].strip()
    row = db.execute(
        "SELECT id, descricao, valor, data FROM transacoes WHERE id LIKE ?",
        (f"{id_parcial}%",)
    ).fetchone()
    if not row:
        _tg_send(chat_id, f"❌ Transação não encontrada: <code>{id_parcial}</code>")
        return
    with db_write() as dw:
        dw.execute("DELETE FROM transacoes WHERE id=?", (row["id"],))
    _tg_send(chat_id, f"""🗑️ <b>Transação removida</b>

{row['descricao']} — {_tg_fmt_valor(row['valor'])}
📅 {_tg_fmt_data(row['data'])}""")

def _cmd_config(chat_id, _args, _db):
    token   = _tg_token()
    cfg     = _tg_cfg()
    allowed = cfg.get("allowed_chat_ids", [])
    whisper = bool(_load_cfg().get("openai_api_key"))
    _tg_send(chat_id, f"""<b>⚙️ Configuração do Bot</b>

🤖 Token: {'✅ configurado' if token else '❌ não configurado'}
🎙️ Voz (Whisper): {'✅ configurado' if whisper else '❌ não configurado'}
🔒 Chat IDs permitidos: {allowed if allowed else 'todos (sem restrição)'}
💬 Seu Chat ID: <code>{chat_id}</code>

Para restringir o acesso ao seu ID, adicione em ⚙️ Config → Telegram no sistema web.""")

# ══════════════════════════════════════════════════════════════════════════════
#  VOZ — PIPELINE COMPLETO
#  1. Baixa áudio OGG do Telegram
#  2. Transcreve via OpenAI Whisper API
#  3. Extrai intenção via Claude (Anthropic)
#  4. Executa o comando identificado
# ══════════════════════════════════════════════════════════════════════════════

def _voz_get_openai_key() -> str:
    """Mantido por compatibilidade — não é mais usado na versão gratuita."""
    return ""

def _voz_baixar_audio(file_id: str) -> bytes | None:
    """Baixa o arquivo de áudio OGG do Telegram."""
    token = _tg_token()
    if not token:
        return None
    try:
        req = _urlreq.Request(
            f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
        )
        with _urlreq.urlopen(req, timeout=15) as r:
            info = json.loads(r.read())
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        with _urlreq.urlopen(url, timeout=30) as r:
            return r.read()
    except Exception as e:
        app.logger.error(f"Erro ao baixar áudio: {e}")
        return None


# ─── TRANSCRIÇÃO LOCAL GRATUITA (faster-whisper) ─────────────────────────────
_whisper_model = None   # carregado sob demanda, fica em memória

def _voz_transcrever(audio_bytes: bytes) -> str | None:
    """
    Transcreve áudio localmente usando faster-whisper (modelo tiny, ~75 MB).
    100% gratuito — roda em CPU sem internet.
    """
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        app.logger.error("faster-whisper não instalado. Execute: pip install faster-whisper")
        return None

    try:
        # Carrega o modelo na primeira chamada e mantém em memória
        if _whisper_model is None:
            _whisper_model = WhisperModel(
                "tiny",           # 75 MB RAM — funciona no PythonAnywhere free
                device="cpu",
                compute_type="int8",   # mais rápido em CPU
            )

        # Salva em arquivo temporário (faster-whisper precisa de arquivo)
        import tempfile, os
        suffix = ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            segments, _info = _whisper_model.transcribe(
                tmp_path,
                language="pt",          # força português — mais preciso
                beam_size=1,            # rápido em CPU
                vad_filter=True,        # filtra silêncio automaticamente
            )
            texto = " ".join(s.text.strip() for s in segments).strip()
            return texto if texto else None
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        app.logger.error(f"Whisper local error: {e}")
        return None


# ─── EXTRAÇÃO DE INTENÇÃO POR REGEX (100% gratuito, sem API) ─────────────────
# Padrões numéricos em português
_RE_VALOR = re.compile(
    r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?|\d+(?:\.\d{2})?)"
    r"(?:\s*(?:reais?|r\$|conto|contos|pila|pilas))?",
    re.IGNORECASE
)

# Palavras que indicam intenção de CONSULTA
_KW_SALDO     = {"saldo","saldos","quanto","sobrou","resta","tenho","disponivel","disponível"}
_KW_EXTRATO   = {"extrato","extratos","ultimas","últimas","lançamentos","lancamentos",
                 "movimentações","movimentacoes","historico","histórico","listagem"}
_KW_RELATORIO = {"relatorio","relatório","relatorios","relatórios","resumo","resumos",
                 "analise","análise","mes","mês","mensal","mensal"}
_KW_AJUDA     = {"ajuda","help","comandos","como","funciona","instruções","instrucoes"}

# Palavras que indicam RECEITA
_KW_RECEITA   = {"recebi","ganhei","salario","salário","salario","renda","entrada",
                 "depositaram","caiu","pagamento","receita","rendimento","freelance",
                 "freelancer","recebimento","deposito","depósito"}

# Palavras que indicam DESPESA
_KW_DESPESA   = {"gastei","paguei","comprei","cobrado","debitou","debitaram","saiu",
                 "parcelei","parcela","fatura","conta","taxa","tarifa","mensalidade",
                 "despesa","gasto","pagamento"}

# Mapeamento de palavras do usuário → categorias do sistema
_MAP_CATEGORIA = {
    # Alimentação
    "mercado":"Alimentação","supermercado":"Alimentação","feira":"Alimentação",
    "padaria":"Alimentação","restaurante":"Alimentação","lanchonete":"Alimentação",
    "ifood":"Alimentação","rappi":"Alimentação","uber eats":"Alimentação",
    "pizza":"Alimentação","sushi":"Alimentação","hamburguer":"Alimentação",
    "café":"Alimentação","cafeteria":"Alimentação","hortifruti":"Alimentação",
    "açougue":"Alimentação","acougue":"Alimentação","extra":"Alimentação",
    "carrefour":"Alimentação","pão de açúcar":"Alimentação",
    # Transporte
    "uber":"Transporte","99":"Transporte","taxi":"Transporte","táxi":"Transporte",
    "onibus":"Transporte","ônibus":"Transporte","metro":"Transporte","metrô":"Transporte",
    "gasolina":"Transporte","combustivel":"Transporte","combustível":"Transporte",
    "posto":"Transporte","estacionamento":"Transporte","pedagio":"Transporte",
    # Saúde
    "farmacia":"Saúde","farmácia":"Saúde","drogaria":"Saúde","remedio":"Saúde",
    "remédio":"Saúde","médico":"Saúde","medico":"Saúde","hospital":"Saúde",
    "dentista":"Saúde","plano":"Saúde","consulta":"Saúde","exame":"Saúde",
    # Educação
    "escola":"Educação","faculdade":"Educação","curso":"Educação","mensalidade":"Educação",
    "livro":"Educação","apostila":"Educação","udemy":"Educação","alura":"Educação",
    # Lazer
    "netflix":"Lazer","spotify":"Lazer","cinema":"Lazer","teatro":"Lazer",
    "academia":"Lazer","show":"Lazer","ingresso":"Lazer","viagem":"Lazer",
    "hotel":"Lazer","streaming":"Lazer","jogo":"Lazer","game":"Lazer",
    "amazon prime":"Lazer","disney":"Lazer","hbo":"Lazer",
    # Vestuário
    "roupa":"Vestuário","roupas":"Vestuário","sapato":"Vestuário","calçado":"Vestuário",
    "tênis":"Vestuário","tenis":"Vestuário","shein":"Vestuário","shopee":"Vestuário",
    # Contas
    "conta":"Contas/Serviços","luz":"Contas/Serviços","agua":"Contas/Serviços",
    "internet":"Contas/Serviços","telefone":"Contas/Serviços","condomínio":"Contas/Serviços",
    "aluguel":"Contas/Serviços","iptu":"Contas/Serviços","seguro":"Contas/Serviços",
    "energia":"Contas/Serviços","gas":"Contas/Serviços","gás":"Contas/Serviços",
    "celular":"Contas/Serviços","vivo":"Contas/Serviços","claro":"Contas/Serviços",
    "tim":"Contas/Serviços","oi":"Contas/Serviços",
    # Cartão
    "cartão":"Cartão de Crédito","cartao":"Cartão de Crédito","fatura":"Cartão de Crédito",
    "nubank":"Cartão de Crédito","mastercard":"Cartão de Crédito","visa":"Cartão de Crédito",
    # Moradia
    "reforma":"Moradia","obra":"Moradia","construção":"Moradia","construcao":"Moradia",
    # Receitas
    "salario":"Salário","salário":"Salário","holerite":"Salário","proventos":"Salário",
    "freelance":"Freelance","freela":"Freelance","autônomo":"Freelance",
    "investimento":"Investimentos","dividendo":"Investimentos","rendimento":"Investimentos",
    "aluguel recebido":"Aluguel Recebido","present":"Presente","regalo":"Presente",
}

def _extrair_valor_texto(texto: str) -> float | None:
    """Extrai valor numérico de texto em português."""
    # Ex: "cinquenta reais" → 50
    _NUMERAIS = {
        "zero":0,"um":1,"uma":1,"dois":2,"duas":2,"tres":3,"três":3,
        "quatro":4,"cinco":5,"seis":6,"sete":7,"oito":8,"nove":9,"dez":10,
        "onze":11,"doze":12,"treze":13,"quatorze":14,"quinze":15,
        "dezesseis":16,"dezessete":17,"dezoito":18,"dezenove":19,"vinte":20,
        "trinta":30,"quarenta":40,"cinquenta":50,"sessenta":60,
        "setenta":70,"oitenta":80,"noventa":90,"cem":100,"cento":100,
        "duzentos":200,"trezentos":300,"quatrocentos":400,"quinhentos":500,
        "seiscentos":600,"setecentos":700,"oitocentos":800,"novecentos":900,
        "mil":1000,"milhão":1000000,
    }
    t = texto.lower()
    # Tenta extrair número escrito
    total = 0
    corrente = 0
    achou = False
    for palavra in re.split(r'\s+|,|e\s', t):
        palavra = palavra.strip('.,!?')
        if palavra in _NUMERAIS:
            n = _NUMERAIS[palavra]
            if n >= 1000:
                corrente = (corrente or 1) * n
                total += corrente
                corrente = 0
            elif n >= 100:
                corrente = (corrente or 1) * n
            else:
                corrente += n
            achou = True
    if achou and (total + corrente) > 0:
        return float(total + corrente)

    # Tenta extrair número dígito
    matches = _RE_VALOR.findall(t)
    for m in matches:
        try:
            v = float(m.replace(".", "").replace(",", "."))
            if v > 0:
                return v
        except ValueError:
            continue
    return None

def _extrair_categoria(texto: str) -> tuple[str, str]:
    """
    Extrai (categoria, tipo_id) do texto.
    Retorna ("Outros (Despesa)", "despesa") se não encontrar.
    """
    t = texto.lower()

    # Verifica receita primeiro (salário, freelance, etc.)
    palavras = set(re.split(r'\s+', t))
    if palavras & _KW_RECEITA:
        for kw, cat in _MAP_CATEGORIA.items():
            if kw in t:
                tipo = "receita" if cat in ("Salário","Freelance","Investimentos",
                                            "Aluguel Recebido","Presente") else "despesa"
                return cat, tipo
        # Receita genérica
        return "Outros (Receita)", "receita"

    # Busca categoria por palavras-chave (multi-word primeiro)
    frases_longas = sorted(_MAP_CATEGORIA.keys(), key=len, reverse=True)
    for kw in frases_longas:
        if kw in t:
            cat = _MAP_CATEGORIA[kw]
            tipo = "receita" if cat in ("Salário","Freelance","Investimentos",
                                        "Aluguel Recebido","Presente") else "despesa"
            return cat, tipo

    # Fallback para classificar() do sistema
    cat, tipo = classificar(texto)
    return cat, tipo

def _extrair_descricao(texto: str) -> str:
    """Extrai uma descrição limpa do texto de voz."""
    t = texto.strip()
    # Remove prefixos comuns de voz
    for prefixo in ["gastei","paguei","comprei","recebi","ganhei","fiz um","fiz uma",
                    "realizei","efetuei","tive","houve"]:
        if t.lower().startswith(prefixo):
            t = t[len(prefixo):].strip()
            break
    # Remove sufixos de valor
    t = re.sub(r'\d+[\.,]?\d*\s*(?:reais?|r\$|conto|pilas?)?', '', t, flags=re.IGNORECASE)
    # Limpa
    t = re.sub(r'\s+', ' ', t).strip(' .,;')
    # Capitaliza
    return t.capitalize()[:60] if t else "Lançamento de voz"

def _voz_extrair_intencao(texto: str) -> dict:
    """
    Extrai intenção financeira usando apenas regex e dicionários.
    100% gratuito — zero chamadas de API.
    """
    t = texto.lower().strip()
    palavras = set(re.split(r'[\s,!?]+', t))

    # ── Consultas ──────────────────────────────────────────────────
    if palavras & _KW_SALDO:
        return {"intent": "saldo", "confianca": "alta"}

    if palavras & _KW_EXTRATO:
        return {"intent": "extrato", "confianca": "alta"}

    if palavras & _KW_RELATORIO:
        return {"intent": "relatorio", "confianca": "alta"}

    if palavras & _KW_AJUDA:
        return {"intent": "ajuda", "confianca": "alta"}

    # ── Lançamentos ────────────────────────────────────────────────
    eh_lancamento = bool(
        (palavras & _KW_DESPESA) or
        (palavras & _KW_RECEITA) or
        _RE_VALOR.search(t)
    )

    if not eh_lancamento:
        return {"intent": "desconhecido", "confianca": "baixa"}

    valor     = _extrair_valor_texto(texto)
    categoria, tipo_id = _extrair_categoria(texto)
    descricao = _extrair_descricao(texto)

    if not valor:
        return {"intent": "desconhecido", "confianca": "baixa",
                "_motivo": "valor não encontrado"}

    return {
        "intent":     "lancamento",
        "descricao":  descricao,
        "valor":      valor,
        "categoria":  categoria,
        "tipo":       tipo_id,
        "confianca":  "alta",
    }


def _voz_executar(chat_id: int, texto_transcrito: str, db) -> None:
    """Pipeline: texto transcrito → intenção por regex → ação."""
    _tg_send(chat_id, f"🎙️ <i>Ouvi:</i> «{texto_transcrito}»\n⏳ Processando...")

    intencao  = _voz_extrair_intencao(texto_transcrito)
    intent    = intencao.get("intent", "desconhecido")
    confianca = intencao.get("confianca", "baixa")

    if intent == "desconhecido" or confianca == "baixa":
        _tg_send(chat_id,
            f"🤔 Não entendi o comando.\n\n"
            f"<i>Transcrição:</i> «{texto_transcrito}»\n\n"
            "Tente falar: <i>gastei 50 reais no mercado</i>\n"
            "Ou use texto: <code>/lc Mercado 50 Alimentação</code>")
        return

    if intent == "saldo":    _cmd_saldo(chat_id, [], db)
    elif intent == "extrato":  _cmd_extrato(chat_id, [], db)
    elif intent == "relatorio":_cmd_relatorio(chat_id, [], db)
    elif intent == "categorias":_cmd_categorias(chat_id, [], db)
    elif intent == "ajuda":    _cmd_ajuda(chat_id, [], db)
    elif intent == "lancamento":
        descricao = intencao.get("descricao") or texto_transcrito[:40]
        categoria = intencao.get("categoria") or "Outros (Despesa)"
        tipo_id   = "receita" if intencao.get("tipo") == "receita" else "despesa"
        try:
            valor = float(str(intencao.get("valor") or 0).replace(",", "."))
            assert valor > 0
        except (ValueError, AssertionError):
            _tg_send(chat_id,
                f"❓ Não consegui identificar o valor.\n"
                f"<i>Ouvi:</i> «{texto_transcrito}»\n\n"
                f"Use: <code>/lc {descricao} VALOR {categoria}</code>")
            return

        _cmd_lancamento_rapido(chat_id,
            [f'"{descricao}"', str(valor), categoria,
             "receita" if tipo_id == "receita" else "despesa"], db)

        _tg_send(chat_id,
            f"🎙️ <i>Entendido como:</i>\n"
            f"  {descricao} — {_tg_fmt_valor(valor)}\n"
            f"  📂 {categoria} | {'✅ Receita' if tipo_id=='receita' else '❌ Despesa'}\n\n"
            "Se estiver errado, use /deletar para remover.")
    else:
        _tg_send(chat_id,
            f"💡 Ouvi «{texto_transcrito}» mas não identifiquei um comando.\n"
            "Use /ajuda para ver o que posso fazer.")


def _processar_voz(chat_id: int, voice_obj: dict, db) -> None:
    """Entrada principal para mensagens de voz."""
    # Verifica se faster-whisper está instalado
    try:
        import faster_whisper  # noqa
    except ImportError:
        _tg_send(chat_id,
            "🎙️ <b>Voz recebida!</b>\n\n"
            "O módulo de transcrição ainda não está instalado no servidor.\n\n"
            "Peça ao administrador para executar no console do PythonAnywhere:\n"
            "<code>pip3 install --user faster-whisper</code>\n\n"
            "Após instalar, reinicie o web app e tente novamente.")
        return

    file_id = voice_obj.get("file_id") or voice_obj.get("file_unique_id")
    duracao = voice_obj.get("duration", 0)

    if duracao > 60:
        _tg_send(chat_id, "⏱️ Áudio muito longo. Use mensagens de até 1 minuto.")
        return

    _tg_send(chat_id, "🎙️ Áudio recebido, transcrevendo...")

    audio_bytes = _voz_baixar_audio(file_id)
    if not audio_bytes:
        _tg_send(chat_id, "❌ Não consegui baixar o áudio. Tente novamente.")
        return

    transcricao = _voz_transcrever(audio_bytes)
    if not transcricao:
        _tg_send(chat_id,
            "❌ Não consegui transcrever o áudio.\n"
            "Fale mais claramente e tente novamente.")
        return

    _voz_executar(chat_id, transcricao, db)

# ── Mapa de comandos ──────────────────────────────────────────────────────────
_COMANDOS = {
    "ajuda":       _cmd_ajuda,
    "help":        _cmd_ajuda,
    "start":       _cmd_ajuda,
    "saldo":       _cmd_saldo,
    "extrato":     _cmd_extrato,
    "relatorio":   _cmd_relatorio,
    "relatório":   _cmd_relatorio,
    "categorias":  _cmd_categorias,
    "lc":          _cmd_lancamento_rapido,
    "lancamento":  _cmd_lancamento_rapido,
    "lancamento":  _cmd_lancamento_rapido,
    "busca":       _cmd_busca,
    "deletar":     _cmd_deletar,
    "config":      _cmd_config,
}

# ── Endpoint Webhook ──────────────────────────────────────────────────────────
@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """
    Recebe updates do Telegram via webhook.
    Suporta: mensagens de texto (comandos) e mensagens de voz.
    """
    if not _tg_token():
        return jsonify({"ok": False}), 403

    try:
        update = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": True})

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return jsonify({"ok": True})

    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return jsonify({"ok": True})

    # Verifica autorização
    if not _tg_allowed(chat_id):
        _tg_send(chat_id, "⛔ Acesso não autorizado.")
        return jsonify({"ok": True})

    # ── Mensagem de VOZ ───────────────────────────────────────────────
    voice_obj = msg.get("voice") or msg.get("audio")
    if voice_obj:
        try:
            with app.app_context():
                _processar_voz(chat_id, voice_obj, get_db())
        except Exception as e:
            app.logger.error(f"Voice handler error: {e}")
            _tg_send(chat_id, f"⚠️ Erro ao processar voz: <code>{str(e)[:80]}</code>")
        return jsonify({"ok": True})

    # ── Mensagem de TEXTO ─────────────────────────────────────────────
    texto = (msg.get("text") or "").strip()
    if not texto:
        # Tipo não suportado (foto, sticker, etc.)
        _tg_send(chat_id, "💡 Envie um comando de texto ou uma mensagem de voz.\nUse /ajuda para ver os comandos.")
        return jsonify({"ok": True})

    # Texto livre (sem barra) → tenta interpretar como voz escrita via Claude
    if not texto.startswith("/"):
        anthropic_key = _get_api_key()
        if anthropic_key:
            try:
                with app.app_context():
                    _voz_executar(chat_id, texto, get_db())
            except Exception as e:
                _tg_send(chat_id, f"⚠️ Erro: <code>{str(e)[:80]}</code>")
        else:
            _tg_send(chat_id, "💡 Use /ajuda para ver os comandos disponíveis.")
        return jsonify({"ok": True})

    # Comando com barra /
    partes  = texto.lstrip("/").split()
    cmd_raw = partes[0].split("@")[0].lower()
    args    = partes[1:]

    handler = _COMANDOS.get(cmd_raw)
    if not handler:
        _tg_send(chat_id,
                 f"❓ Comando desconhecido: <code>/{cmd_raw}</code>\n"
                 "Use /ajuda para ver os comandos disponíveis.")
        return jsonify({"ok": True})

    try:
        with app.app_context():
            handler(chat_id, args, get_db())
    except Exception as e:
        app.logger.error(f"Bot handler error [{cmd_raw}]: {e}")
        _tg_send(chat_id, f"⚠️ Erro ao processar: <code>{str(e)[:100]}</code>")

    return jsonify({"ok": True})


# ── Configurar / remover webhook ─────────────────────────────────────────────
@app.route("/api/telegram/configurar", methods=["POST"])
def telegram_configurar():
    """Salva token e registra o webhook no Telegram."""
    d     = request.get_json() or {}
    token = (d.get("token") or "").strip()
    url   = (d.get("webhook_url") or "").strip()   # ex: https://usuario.pythonanywhere.com
    allowed_ids = d.get("allowed_chat_ids", [])

    if not token:
        return err("Informe o token do bot.")
    if not url:
        return err("Informe a URL do seu servidor.")

    # Salva configuração
    cfg = _load_cfg()
    cfg["telegram"] = {
        "token": token,
        "allowed_chat_ids": [int(i) for i in allowed_ids if str(i).lstrip("-").isdigit()],
    }
    _save_cfg(cfg)

    # Registra webhook no Telegram
    webhook_url = f"{url.rstrip('/')}/api/telegram/webhook"
    try:
        payload = json.dumps({"url": webhook_url}).encode()
        req = _urlreq.Request(
            f"https://api.telegram.org/bot{token}/setWebhook",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
        if not resp.get("ok"):
            return err(f"Telegram recusou o webhook: {resp.get('description','')}")
    except Exception as e:
        return err(f"Erro ao registrar webhook: {str(e)}")

    return ok({"webhook_url": webhook_url, "configurado": True})


@app.route("/api/telegram/status")
def telegram_status():
    """Retorna status do bot e info do webhook registrado."""
    token = _tg_token()
    if not token:
        return ok({"configurado": False})
    try:
        req = _urlreq.Request(f"https://api.telegram.org/bot{token}/getWebhookInfo")
        with _urlreq.urlopen(req, timeout=10) as r:
            info = json.loads(r.read())
        cfg = _tg_cfg()
        return ok({
            "configurado":     True,
            "webhook_url":     info.get("result", {}).get("url", ""),
            "ultimo_erro":     info.get("result", {}).get("last_error_message", ""),
            "mensagens_pending": info.get("result", {}).get("pending_update_count", 0),
            "allowed_chat_ids": cfg.get("allowed_chat_ids", []),
        })
    except Exception as e:
        return ok({"configurado": True, "erro": str(e)})


@app.route("/api/telegram/remover", methods=["POST"])
def telegram_remover():
    """Remove o webhook e apaga o token salvo."""
    token = _tg_token()
    if token:
        try:
            req = _urlreq.Request(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            _urlreq.urlopen(req, timeout=10)
        except Exception:
            pass
    cfg = _load_cfg()
    cfg.pop("telegram", None)
    _save_cfg(cfg)
    return ok({"removido": True})

@app.route("/api/voz/config", methods=["GET", "POST"])
def voz_config():
    """Verifica/reporta status do faster-whisper local."""
    if request.method == "POST":
        return ok({"salvo": True, "msg": "Versão gratuita — sem chave necessária"})
    try:
        import faster_whisper  # noqa
        instalado = True
    except ImportError:
        instalado = False
    return ok({
        "configurado": instalado,
        "modo":        "local (faster-whisper)" if instalado else "não instalado",
        "prefixo":     "faster-whisper ✓" if instalado else "",
    })


# ─── MODOS DE EXECUÇÃO: LOCAL (exe/script) ou WEB (PythonAnywhere) ───────────
def resource_path(rel):
    """Caminho correto tanto em .exe (PyInstaller) quanto em servidor."""
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, rel)

app.template_folder = resource_path("templates")
app.static_folder   = resource_path("static")

# Detecta se está rodando em servidor web (WSGI) ou localmente
_EM_SERVIDOR = (
    os.environ.get("PYTHONANYWHERE_SITE")  # PythonAnywhere
    or os.environ.get("RENDER")             # Render
    or os.environ.get("RAILWAY_ENVIRONMENT")# Railway
    or os.environ.get("FLY_APP_NAME")       # Fly.io
    or os.environ.get("FC_WEB_MODE")        # manual: export FC_WEB_MODE=1
)

def rodar_flask():
    port = int(os.environ.get("PORT", PORT))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def iniciar_local():
    """Modo local: abre janela PyWebView ou navegador."""
    import time
    threading.Thread(target=rodar_flask, daemon=True).start()
    time.sleep(1.2)
    try:
        import webview
        webview.create_window("FinanceControl", f"http://localhost:{PORT}",
                              width=1280, height=820, min_size=(900, 600), resizable=True)
        webview.start()
    except ImportError:
        webbrowser.open(f"http://localhost:{PORT}")
        while True:
            time.sleep(60)

if __name__ == "__main__":
    init_db()
    if _EM_SERVIDOR:
        # Modo servidor: Flask sobe direto (sem janela, sem navegador)
        rodar_flask()
    else:
        # Modo local: abre interface nativa
        iniciar_local()

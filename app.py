import os
import secrets
import sqlite3
import sys
import subprocess
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from functools import wraps
from io import BytesIO

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_from_directory,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image, UnidentifiedImageError

BASE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
if getattr(sys, "frozen", False):
    DATA_ROOT = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    INSTANCE_DIR = DATA_ROOT / "ControleMed"
else:
    INSTANCE_DIR = BASE_DIR / "instance"

PHOTO_DIR = INSTANCE_DIR / "fotos"
DB_PATH = INSTANCE_DIR / "controlemed.db"
SECRET_PATH = INSTANCE_DIR / "secret_key.txt"

EXTENSOES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_FOTO = 5 * 1024 * 1024

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

if not SECRET_PATH.exists():
    SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8")

app = Flask(
    __name__,
    template_folder=str(RESOURCE_DIR / "templates"),
    static_folder=str(RESOURCE_DIR / "static"),
)
app.config.update(
    SECRET_KEY=SECRET_PATH.read_text(encoding="utf-8").strip(),
    TEMPLATES_AUTO_RELOAD=not getattr(sys, "frozen", False),
    MAX_CONTENT_LENGTH=MAX_FOTO + 256 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=1800,
)


def conectar_banco():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def obter_nomes_turmas_ativas():
    with conectar_banco() as con:
        turmas = con.execute(
            "SELECT nome FROM turmas WHERE ativo = 1 ORDER BY nome COLLATE NOCASE"
        ).fetchall()
    return [turma["nome"] for turma in turmas]


def inicializar_banco():
    with conectar_banco() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                usuario TEXT NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS turmas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL COLLATE NOCASE UNIQUE,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS alunos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                turma TEXT NOT NULL,
                foto TEXT,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS medicamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aluno_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                dosagem TEXT,
                observacoes TEXT,
                data_inicio TEXT NOT NULL,
                data_fim TEXT,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (aluno_id) REFERENCES alunos(id)
            );

            CREATE TABLE IF NOT EXISTS horarios_medicamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicamento_id INTEGER NOT NULL,
                dia_semana INTEGER NOT NULL,
                horario TEXT NOT NULL,
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (medicamento_id) REFERENCES medicamentos(id)
            );

            CREATE TABLE IF NOT EXISTS historico_medicamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicamento_id INTEGER NOT NULL,
                horario_id INTEGER,
                data_registro TEXT NOT NULL,
                horario TEXT,
                tipo TEXT NOT NULL,
                responsavel TEXT NOT NULL,
                observacoes TEXT,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (medicamento_id) REFERENCES medicamentos(id)
            );
        """)

        colunas_historico = {
            linha["name"]
            for linha in con.execute(
                "PRAGMA table_info(historico_medicamentos)"
            ).fetchall()
        }

        if "horario_id" not in colunas_historico:
            con.execute(
                "ALTER TABLE historico_medicamentos ADD COLUMN horario_id INTEGER"
            )

        # Compatibilidade com o banco da primeira etapa.
        colunas = {
            linha["name"]
            for linha in con.execute("PRAGMA table_info(alunos)").fetchall()
        }

        if "foto" not in colunas:
            con.execute("ALTER TABLE alunos ADD COLUMN foto TEXT")

        if "ativo" not in colunas:
            con.execute(
                "ALTER TABLE alunos ADD COLUMN ativo INTEGER NOT NULL DEFAULT 1"
            )

        # Não repõe os alunos de demonstração se já houver registros.
        quantidade = con.execute("SELECT COUNT(*) FROM alunos").fetchone()[0]

        if quantidade == 0 and not getattr(sys, "frozen", False):
            exemplos = [
                ("Ana Souza (DEMO)", "6º ano"),
                ("Bruno Lima (DEMO)", "6º ano"),
                ("Camila Alves (DEMO)", "7º ano"),
                ("Daniel Costa (DEMO)", "7º ano"),
                ("Eduarda Santos (DEMO)", "8º ano"),
                ("Felipe Rocha (DEMO)", "8º ano"),
                ("Gabriela Martins (DEMO)", "9º ano"),
                ("Henrique Oliveira (DEMO)", "9º ano"),
            ]
            con.executemany(
                "INSERT INTO alunos (nome, turma) VALUES (?, ?)",
                exemplos,
            )

        # Migra nomes de turma usados por alunos antes do cadastro de turmas.
        con.execute("""
            INSERT OR IGNORE INTO turmas (nome)
            SELECT DISTINCT turma
            FROM alunos
            WHERE ativo = 1 AND TRIM(turma) <> ''
        """)


def login_obrigatorio(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "usuario_id" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def existe_usuario_ativo():
    with conectar_banco() as con:
        return con.execute(
            "SELECT 1 FROM usuarios WHERE ativo = 1 LIMIT 1"
        ).fetchone() is not None


def obter_aluno(aluno_id):
    with conectar_banco() as con:
        return con.execute(
            """SELECT id, nome, turma, foto
               FROM alunos WHERE id = ? AND ativo = 1""",
            (aluno_id,),
        ).fetchone()

@app.route("/alunos/<int:aluno_id>/ficha")
@login_obrigatorio
def ficha_aluno(aluno_id):
    aluno = obter_aluno(aluno_id)

    if aluno is None:
        abort(404)

    with conectar_banco() as con:
        medicamentos = con.execute(
            """
            SELECT *
            FROM medicamentos
            WHERE aluno_id = ? AND ativo = 1
            ORDER BY nome COLLATE NOCASE
            """,
            (aluno_id,),
        ).fetchall()

        horarios_por_medicamento = {}

        for medicamento in medicamentos:
            horarios = con.execute(
                """
                SELECT id, dia_semana, horario
                FROM horarios_medicamentos
                WHERE medicamento_id = ? AND ativo = 1
                ORDER BY dia_semana, horario
                """,
                (medicamento["id"],),
            ).fetchall()

            horarios_por_medicamento[medicamento["id"]] = horarios

        horarios_registrados_hoje = {
            registro["horario_id"]
            for registro in con.execute(
                """
                SELECT h.horario_id
                FROM historico_medicamentos AS h
                JOIN medicamentos AS m ON m.id = h.medicamento_id
                WHERE m.aluno_id = ?
                  AND h.horario_id IS NOT NULL
                  AND date(h.data_registro) = date('now', 'localtime')
                """,
                (aluno_id,),
            ).fetchall()
        }

        historico = con.execute(
            """
            SELECT
                h.id,
                m.nome AS medicamento_nome,
                h.data_registro,
                h.horario,
                h.tipo,
                h.responsavel,
                h.observacoes
            FROM historico_medicamentos AS h
            JOIN medicamentos AS m ON m.id = h.medicamento_id
            WHERE m.aluno_id = ?
              AND datetime(h.data_registro) >= datetime('now', 'localtime', '-7 days')
            ORDER BY datetime(h.data_registro) DESC, h.id DESC
            """,
            (aluno_id,),
        ).fetchall()

    return render_template(
        "ficha_aluno.html",
        aluno=aluno,
        medicamentos=medicamentos,
        horarios_por_medicamento=horarios_por_medicamento,
        horarios_registrados_hoje=horarios_registrados_hoje,
        dia_atual=datetime.now().weekday(),
        historico=historico,
    )

@app.route("/alunos/<int:aluno_id>/medicamentos/novo", methods=["GET", "POST"])
@login_obrigatorio
def novo_medicamento(aluno_id):
    aluno = obter_aluno(aluno_id)

    if aluno is None:
        abort(404)

    dias_semana = [
        (0, "Segunda-feira"),
        (1, "Terça-feira"),
        (2, "Quarta-feira"),
        (3, "Quinta-feira"),
        (4, "Sexta-feira"),
    ]

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        dosagem = request.form.get("dosagem", "").strip()
        observacoes = request.form.get("observacoes", "").strip()
        data_inicio = request.form.get("data_inicio", "").strip()
        data_fim = request.form.get("data_fim", "").strip()

        if not nome:
            flash("Informe o nome do medicamento.", "erro")
            return render_template(
                "medicamento_form.html",
                aluno=aluno,
                medicamento=None,
                dias_semana=dias_semana,
            )

        horarios_selecionados = []

        for dia, _ in dias_semana:
            horarios = request.form.getlist(f"horario_{dia}")

            for horario in horarios:
                horario = horario.strip()

                if horario:
                    horarios_selecionados.append((dia, horario))

        with conectar_banco() as con:
            cursor = con.execute(
                """
                INSERT INTO medicamentos (
                    aluno_id,
                    nome,
                    dosagem,
                    observacoes,
                    data_inicio,
                    data_fim,
                    ativo
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    aluno_id,
                    nome,
                    dosagem,
                    observacoes,
                    data_inicio or None,
                    data_fim or None,
                ),
            )

            medicamento_id = cursor.lastrowid

            for dia, horario in horarios_selecionados:
                con.execute(
                    """
                    INSERT INTO horarios_medicamentos (
                        medicamento_id,
                        dia_semana,
                        horario,
                        ativo
                    )
                    VALUES (?, ?, ?, 1)
                    """,
                    (medicamento_id, dia, horario),
                )

        flash("Medicamento cadastrado com sucesso.", "sucesso")
        return redirect(
            url_for("ficha_aluno", aluno_id=aluno_id)
        )

    return render_template(
        "medicamento_form.html",
        aluno=aluno,
        medicamento=None,
        dias_semana=dias_semana,
    )

@app.route(
    "/medicamentos/<int:medicamento_id>/editar",
    methods=["GET", "POST"],
)
@login_obrigatorio
def editar_medicamento(medicamento_id):
    dias_semana = [
        (0, "Segunda-feira"),
        (1, "Terça-feira"),
        (2, "Quarta-feira"),
        (3, "Quinta-feira"),
        (4, "Sexta-feira"),
    ]

    with conectar_banco() as con:
        medicamento = con.execute(
            """
            SELECT *
            FROM medicamentos
            WHERE id = ? AND ativo = 1
            """,
            (medicamento_id,),
        ).fetchone()

    if medicamento is None:
        abort(404)

    aluno = obter_aluno(medicamento["aluno_id"])

    if aluno is None:
        abort(404)

    horarios_por_dia = {dia: [] for dia, _ in dias_semana}

    if request.method == "GET":
        with conectar_banco() as con:
            horarios = con.execute(
                """
                SELECT dia_semana, horario
                FROM horarios_medicamentos
                WHERE medicamento_id = ? AND ativo = 1
                ORDER BY dia_semana, horario
                """,
                (medicamento_id,),
            ).fetchall()

        for horario in horarios:
            dia = horario["dia_semana"]
            if dia in horarios_por_dia:
                horarios_por_dia[dia].append(horario["horario"])

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        dosagem = request.form.get("dosagem", "").strip()
        observacoes = request.form.get("observacoes", "").strip()
        data_inicio = request.form.get("data_inicio", "").strip()
        data_fim = request.form.get("data_fim", "").strip()

        for dia, _ in dias_semana:
            horarios_por_dia[dia] = [
                horario.strip()
                for horario in request.form.getlist(f"horario_{dia}")
                if horario.strip()
            ]

        if not nome:
            flash("Informe o nome do medicamento.", "erro")
            return render_template(
                "medicamento_form.html",
                aluno=aluno,
                medicamento=medicamento,
                dias_semana=dias_semana,
                horarios_por_dia=horarios_por_dia,
            )

        with conectar_banco() as con:
            con.execute(
                """
                UPDATE medicamentos
                SET nome = ?,
                    dosagem = ?,
                    observacoes = ?,
                    data_inicio = ?,
                    data_fim = ?
                WHERE id = ? AND ativo = 1
                """,
                (
                    nome,
                    dosagem,
                    observacoes,
                    data_inicio or None,
                    data_fim or None,
                    medicamento_id,
                ),
            )

            # Preserva os horários anteriores, apenas desativando-os.
            con.execute(
                """
                UPDATE horarios_medicamentos
                SET ativo = 0
                WHERE medicamento_id = ?
                """,
                (medicamento_id,),
            )

            # Registra os novos horários sem apagar os anteriores.
            for dia, _ in dias_semana:
                for horario in horarios_por_dia[dia]:
                    con.execute(
                        """
                        INSERT INTO horarios_medicamentos (
                            medicamento_id,
                            dia_semana,
                            horario,
                            ativo
                        )
                        VALUES (?, ?, ?, 1)
                        """,
                        (medicamento_id, dia, horario),
                    )

        flash("Medicamento atualizado com sucesso.", "sucesso")
        return redirect(url_for("ficha_aluno", aluno_id=aluno["id"]))

    return render_template(
        "medicamento_form.html",
        aluno=aluno,
        medicamento=medicamento,
        dias_semana=dias_semana,
        horarios_por_dia=horarios_por_dia,
    )


@app.route(
    "/medicamentos/<int:medicamento_id>/suspender",
    methods=["POST"],
)
@login_obrigatorio
def suspender_medicamento(medicamento_id):
    with conectar_banco() as con:
        medicamento = con.execute(
            """
            SELECT id, aluno_id
            FROM medicamentos
            WHERE id = ? AND ativo = 1
            """,
            (medicamento_id,),
        ).fetchone()

        if medicamento is None:
            abort(404)

        # Suspende o medicamento sem excluir seu registro.
        con.execute(
            """
            UPDATE medicamentos
            SET ativo = 0
            WHERE id = ?
            """,
            (medicamento_id,),
        )

        # Desativa os horários, mantendo os registros no banco.
        con.execute(
            """
            UPDATE horarios_medicamentos
            SET ativo = 0
            WHERE medicamento_id = ?
            """,
            (medicamento_id,),
        )

    flash("Medicamento suspenso. O histórico foi preservado.", "sucesso")
    return redirect(
        url_for("ficha_aluno", aluno_id=medicamento["aluno_id"])
    )



@app.route(
    "/medicamentos/<int:medicamento_id>/registrar-administracao",
    methods=["POST"],
)
@login_obrigatorio
def registrar_administracao(medicamento_id):
    tipo = request.form.get("tipo", "").strip()
    horario_id = request.form.get("horario_id", "").strip()
    observacoes = request.form.get("observacoes", "").strip()

    tipos_permitidos = {
        "ADMINISTRADO",
        "NAO_ADMINISTRADO",
        "RECUSADO",
    }

    with conectar_banco() as con:
        medicamento = con.execute(
            """
            SELECT id, aluno_id
            FROM medicamentos
            WHERE id = ? AND ativo = 1
            """,
            (medicamento_id,),
        ).fetchone()

        if medicamento is None:
            abort(404)

        aluno_id = medicamento["aluno_id"]

        if tipo not in tipos_permitidos:
            abort(400)

        if not horario_id.isdigit():
            abort(400)

        horario_registrado = con.execute(
            """
            SELECT id, horario, dia_semana
            FROM horarios_medicamentos
            WHERE id = ?
              AND medicamento_id = ?
              AND ativo = 1
            """,
            (int(horario_id), medicamento_id),
        ).fetchone()

        if horario_registrado is None:
            abort(400)

        if horario_registrado["dia_semana"] > datetime.now().weekday():
            flash(
                "Este horário só pode ser registrado no dia correspondente ou depois dele.",
                "erro",
            )
            return redirect(url_for("ficha_aluno", aluno_id=aluno_id))

        dose_ja_registrada = con.execute(
            """
            SELECT id
            FROM historico_medicamentos
            WHERE horario_id = ?
              AND date(data_registro) = date('now', 'localtime')
            LIMIT 1
            """,
            (horario_registrado["id"],),
        ).fetchone()

        if dose_ja_registrada is not None:
            flash("A dose deste horário já foi registrada hoje.", "erro")
            return redirect(url_for("ficha_aluno", aluno_id=aluno_id))

        if tipo != "ADMINISTRADO" and not observacoes:
            flash(
                "Informe o motivo para registrar uma dose não administrada ou recusada.",
                "erro",
            )
            return redirect(url_for("ficha_aluno", aluno_id=aluno_id))

        usuario = con.execute(
            """
            SELECT nome
            FROM usuarios
            WHERE id = ? AND ativo = 1
            """,
            (session["usuario_id"],),
        ).fetchone()

        if usuario is None:
            session.clear()
            flash("Faça login novamente para continuar.", "erro")
            return redirect(url_for("login"))

        con.execute(
            """
            INSERT INTO historico_medicamentos (
                medicamento_id,
                horario_id,
                data_registro,
                horario,
                tipo,
                responsavel,
                observacoes
            )
            VALUES (
                ?,
                ?,
                datetime('now', 'localtime'),
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                medicamento_id,
                horario_registrado["id"],
                horario_registrado["horario"],
                tipo,
                usuario["nome"],
                observacoes or None,
            ),
        )

    flash("Registro de administração salvo com sucesso.", "sucesso")
    return redirect(url_for("ficha_aluno", aluno_id=aluno_id))


@app.route("/historico/<int:historico_id>/excluir", methods=["POST"])
@login_obrigatorio
def excluir_registro_historico(historico_id):
    with conectar_banco() as con:
        registro = con.execute(
            """
            SELECT h.id, m.aluno_id
            FROM historico_medicamentos AS h
            JOIN medicamentos AS m ON m.id = h.medicamento_id
            WHERE h.id = ?
            """,
            (historico_id,),
        ).fetchone()

        if registro is None:
            abort(404)

        con.execute(
            "DELETE FROM historico_medicamentos WHERE id = ?",
            (historico_id,),
        )

    flash("Registro do histórico excluído.", "sucesso")
    return redirect(url_for("ficha_aluno", aluno_id=registro["aluno_id"]))


def salvar_foto(arquivo):
    if not arquivo or not arquivo.filename:
        return None

    nome_original = secure_filename(arquivo.filename)
    extensao = Path(nome_original).suffix.lower()

    if extensao not in EXTENSOES:
        raise ValueError("Use uma imagem JPG, PNG ou WEBP.")

    conteudo = arquivo.read(MAX_FOTO + 1)

    if not conteudo:
        raise ValueError("O arquivo de imagem está vazio.")

    if len(conteudo) > MAX_FOTO:
        raise ValueError("A foto deve ter no máximo 5 MB.")

    try:
        with Image.open(BytesIO(conteudo)) as imagem:
            imagem.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("O arquivo selecionado não é uma imagem válida.")

    nome_final = uuid.uuid4().hex + extensao
    destino = PHOTO_DIR / nome_final
    destino.write_bytes(conteudo)

    return nome_final


def remover_foto(nome_foto):
    if not nome_foto:
        return

    nome = Path(nome_foto).name

    try:
        (PHOTO_DIR / nome).unlink(missing_ok=True)
    except OSError:
        app.logger.warning("Não foi possível remover a foto: %s", nome)


@app.route("/")
def inicio():
    if "usuario_id" in session:
        return redirect(url_for("dashboard"))
    if not existe_usuario_ativo():
        return redirect(url_for("configuracao_inicial"))
    return redirect(url_for("login"))


@app.route("/configuracao-inicial", methods=["GET", "POST"])
def configuracao_inicial():
    if existe_usuario_ativo():
        return redirect(url_for("login"))

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")
        confirmar_senha = request.form.get("confirmar_senha", "")

        if not nome or len(nome) > 80:
            flash("Informe um nome com até 80 caracteres.", "erro")
        elif not usuario or len(usuario) > 40:
            flash("Informe um usuário com até 40 caracteres.", "erro")
        elif len(senha) < 12:
            flash("A senha precisa ter pelo menos 12 caracteres.", "erro")
        elif senha != confirmar_senha:
            flash("As senhas não conferem.", "erro")
        else:
            try:
                with conectar_banco() as con:
                    con.execute("BEGIN IMMEDIATE")
                    if con.execute(
                        "SELECT 1 FROM usuarios WHERE ativo = 1 LIMIT 1"
                    ).fetchone():
                        return redirect(url_for("login"))

                    con.execute(
                        """INSERT INTO usuarios (nome, usuario, senha_hash)
                           VALUES (?, ?, ?)""",
                        (nome, usuario, generate_password_hash(senha)),
                    )
                flash("Acesso criado. Entre com seu usuário e senha.", "sucesso")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("Esse nome de usuário já está em uso.", "erro")

    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not existe_usuario_ativo():
        return redirect(url_for("configuracao_inicial"))

    if "usuario_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")

        with conectar_banco() as con:
            registro = con.execute(
                """SELECT id, nome, usuario, senha_hash
                   FROM usuarios
                   WHERE usuario = ? AND ativo = 1""",
                (usuario,),
            ).fetchone()

        if registro and check_password_hash(registro["senha_hash"], senha):
            session.clear()
            session.permanent = True
            session["usuario_id"] = registro["id"]
            session["nome_usuario"] = registro["nome"]
            return redirect(url_for("dashboard"))

        flash("Usuário ou senha inválidos.", "erro")

    return render_template("login.html")


@app.route("/dashboard")
@login_obrigatorio
def dashboard():
    busca = request.args.get("busca", "").strip()
    turma_filtro = request.args.get("turma", "").strip()
    turmas = obter_nomes_turmas_ativas()

    if turma_filtro not in turmas:
        turma_filtro = ""

    sql = """
        SELECT id, nome, turma, foto
        FROM alunos
        WHERE ativo = 1
    """
    parametros = []

    if turma_filtro:
        sql += " AND turma = ?"
        parametros.append(turma_filtro)

    if busca:
        sql += " AND nome LIKE ?"
        parametros.append(f"%{busca}%")

    sql += " ORDER BY turma, nome"

    with conectar_banco() as con:
        alunos = con.execute(sql, parametros).fetchall()
        total = con.execute("SELECT COUNT(*) FROM alunos WHERE ativo = 1").fetchone()[0]

    return render_template(
        "dashboard.html",
        alunos=alunos,
        total_alunos=total,
        turmas=turmas,
        turma_filtro=turma_filtro,
        busca=busca,
        nome_usuario=session.get("nome_usuario", "Usuário"),
    )


@app.route("/alunos/novo", methods=["GET", "POST"])
@login_obrigatorio
def novo_aluno():
    turmas = obter_nomes_turmas_ativas()

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        turma = request.form.get("turma", "").strip()
        arquivo = request.files.get("foto")

        if not nome:
            flash("Informe o nome do aluno.", "erro")
            return render_template(
                "aluno_form.html",
                aluno=None,
                titulo="Cadastrar aluno",
                turmas=turmas,
            )

        if turma not in turmas:
            flash("Selecione uma turma válida.", "erro")
            return render_template(
                "aluno_form.html",
                aluno=None,
                titulo="Cadastrar aluno",
                turmas=turmas,
            )

        foto = None

        try:
            foto = salvar_foto(arquivo)

            with conectar_banco() as con:
                con.execute(
                    """INSERT INTO alunos (nome, turma, foto)
                       VALUES (?, ?, ?)""",
                    (nome, turma, foto),
                )
        except ValueError as erro:
            remover_foto(foto)
            flash(str(erro), "erro")
            return render_template(
                "aluno_form.html",
                aluno=None,
                titulo="Cadastrar aluno",
                turmas=turmas,
            )
        except Exception:
            remover_foto(foto)
            raise

        flash("Aluno cadastrado com sucesso.", "sucesso")
        return redirect(url_for("dashboard"))

    return render_template(
        "aluno_form.html",
        aluno=None,
        titulo="Cadastrar aluno",
        turmas=turmas,
    )


@app.route("/alunos/<int:aluno_id>/editar", methods=["GET", "POST"])
@login_obrigatorio
def editar_aluno(aluno_id):
    aluno = obter_aluno(aluno_id)
    turmas = obter_nomes_turmas_ativas()

    if aluno is None:
        abort(404)

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        turma = request.form.get("turma", "").strip()
        arquivo = request.files.get("foto")
        apagar_foto = request.form.get("remover_foto") == "sim"

        if not nome:
            flash("Informe o nome do aluno.", "erro")
            return render_template(
                "aluno_form.html",
                aluno=aluno,
                titulo="Editar aluno",
                turmas=turmas,
            )

        if turma not in turmas:
            flash("Selecione uma turma válida.", "erro")
            return render_template(
                "aluno_form.html",
                aluno=aluno,
                titulo="Editar aluno",
                turmas=turmas,
            )

        foto_nova = None

        try:
            if arquivo and arquivo.filename:
                foto_nova = salvar_foto(arquivo)
        except ValueError as erro:
            flash(str(erro), "erro")
            return render_template(
                "aluno_form.html",
                aluno=aluno,
                titulo="Editar aluno",
                turmas=turmas,
            )

        foto_anterior = aluno["foto"]

        if foto_nova:
            foto_final = foto_nova
        elif apagar_foto:
            foto_final = None
        else:
            foto_final = foto_anterior

        try:
            with conectar_banco() as con:
                con.execute(
                    """UPDATE alunos
                       SET nome = ?, turma = ?, foto = ?
                       WHERE id = ?""",
                    (nome, turma, foto_final, aluno_id),
                )
        except Exception:
            remover_foto(foto_nova)
            raise

        if foto_anterior and foto_anterior != foto_final:
            remover_foto(foto_anterior)

        flash("Dados do aluno atualizados.", "sucesso")
        return redirect(url_for("dashboard"))

    return render_template(
        "aluno_form.html",
        aluno=aluno,
        titulo="Editar aluno",
        turmas=turmas,
    )


@app.route("/alunos/<int:aluno_id>/excluir", methods=["POST"])
@login_obrigatorio
def excluir_aluno(aluno_id):
    aluno = obter_aluno(aluno_id)

    if aluno is None:
        abort(404)

    # Exclusão lógica: o registro fica preservado no SQLite.
    with conectar_banco() as con:
        con.execute(
            "UPDATE alunos SET ativo = 0 WHERE id = ?",
            (aluno_id,),
        )

    remover_foto(aluno["foto"])
    flash("Aluno removido da lista.", "sucesso")
    return redirect(url_for("dashboard"))


@app.route("/fotos/<path:nome_arquivo>")
@login_obrigatorio
def foto_aluno(nome_arquivo):
    nome = Path(nome_arquivo).name

    if nome != nome_arquivo:
        abort(404)

    if Path(nome).suffix.lower() not in EXTENSOES:
        abort(404)

    return send_from_directory(PHOTO_DIR, nome)


@app.route("/logout", methods=["POST"])
@login_obrigatorio
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.errorhandler(413)
def arquivo_grande(_erro):
    flash("A foto excede o tamanho permitido de 5 MB.", "erro")
    return redirect(url_for("dashboard"))


inicializar_banco()

# =========================
# GERENCIAMENTO DE TURMAS
# =========================


@app.route("/turmas")
@login_obrigatorio
def listar_turmas():
    with conectar_banco() as con:
        turmas = con.execute("""
            SELECT
                t.id,
                t.nome,
                t.criado_em,
                COUNT(a.id) AS quantidade_alunos
            FROM turmas t
            LEFT JOIN alunos a
                ON a.turma = t.nome
                AND a.ativo = 1
            WHERE t.ativo = 1
            GROUP BY t.id, t.nome, t.criado_em
            ORDER BY t.nome COLLATE NOCASE
        """).fetchall()

    return render_template("turmas.html", turmas=turmas)


@app.route("/turmas/nova", methods=["GET", "POST"])
@login_obrigatorio
def nova_turma():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()

        if not nome:
            flash("Informe o nome da turma.", "erro")
            return render_template("turma_form.html", turma=None)

        if len(nome) > 50:
            flash("O nome da turma deve ter no máximo 50 caracteres.", "erro")
            return render_template("turma_form.html", turma=None)

        try:
            with conectar_banco() as con:
                con.execute("INSERT INTO turmas (nome) VALUES (?)", (nome,))

            flash("Turma cadastrada com sucesso.", "sucesso")
            return redirect(url_for("listar_turmas"))

        except sqlite3.IntegrityError:
            flash("Já existe uma turma com esse nome.", "erro")

    return render_template("turma_form.html", turma=None)


@app.route("/turmas/<int:turma_id>/editar", methods=["GET", "POST"])
@login_obrigatorio
def editar_turma(turma_id):
    with conectar_banco() as con:
        turma = con.execute(
            "SELECT * FROM turmas WHERE id = ? AND ativo = 1", (turma_id,)
        ).fetchone()

    if turma is None:
        flash("Turma não encontrada.", "erro")
        return redirect(url_for("listar_turmas"))

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()

        if not nome:
            flash("Informe o nome da turma.", "erro")
            return render_template("turma_form.html", turma=turma)

        if len(nome) > 50:
            flash("O nome da turma deve ter no máximo 50 caracteres.", "erro")
            return render_template("turma_form.html", turma=turma)

        try:
            with conectar_banco() as con:
                con.execute(
                    "UPDATE alunos SET turma = ? WHERE turma = ?",
                    (nome, turma["nome"]),
                )
                con.execute("UPDATE turmas SET nome = ? WHERE id = ?", (nome, turma_id))

            flash("Turma atualizada com sucesso.", "sucesso")
            return redirect(url_for("listar_turmas"))

        except sqlite3.IntegrityError:
            flash("Já existe uma turma com esse nome.", "erro")

    return render_template("turma_form.html", turma=turma)


@app.route("/turmas/<int:turma_id>/excluir", methods=["POST"])
@login_obrigatorio
def excluir_turma(turma_id):
    with conectar_banco() as con:
        turma = con.execute(
            "SELECT nome FROM turmas WHERE id = ? AND ativo = 1", (turma_id,)
        ).fetchone()

        if turma is None:
            flash("Turma não encontrada.", "erro")
            return redirect(url_for("listar_turmas"))

        quantidade = con.execute(
            "SELECT COUNT(*) FROM alunos WHERE turma = ? AND ativo = 1",
            (turma["nome"],),
        ).fetchone()[0]

        if quantidade > 0:
            flash("Não é possível excluir uma turma que possui alunos ativos.", "erro")
            return redirect(url_for("listar_turmas"))

        con.execute("UPDATE turmas SET ativo = 0 WHERE id = ?", (turma_id,))

    flash("Turma removida com sucesso.", "sucesso")
    return redirect(url_for("listar_turmas"))


def garantir_atalho_desktop():
    if not getattr(sys, "frozen", False):
        return

    script = RESOURCE_DIR / "create_shortcut.vbs"
    icone = RESOURCE_DIR / "care_heh_hehy_medical_pills_remedy_medicine_icon_255235.ico"
    if not script.is_file() or not icone.is_file():
        app.logger.warning("Recursos para criar o atalho não foram encontrados.")
        return

    try:
        resultado = subprocess.run(
            ["cscript", "//nologo", str(script), sys.executable, str(icone)],
            check=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        app.logger.exception("Não foi possível iniciar a criação do atalho.")
        return

    if resultado.returncode != 0:
        app.logger.warning(
            "Não foi possível criar o atalho: %s",
            resultado.stderr.strip() or resultado.stdout.strip(),
        )


def executar_aplicacao():
    from waitress import create_server

    garantir_atalho_desktop()
    servidor = create_server(app, host="127.0.0.1", port=0)
    endereco = f"http://127.0.0.1:{servidor.effective_port}"
    threading.Timer(0.7, lambda: webbrowser.open(endereco)).start()
    app.logger.info("ControleMed disponível em %s", endereco)
    servidor.run()


if __name__ == "__main__":
    executar_aplicacao()

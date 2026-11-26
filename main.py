"""
API da clínica multiprofissional (caso 03A).

Rotas (cada uma aparece no formulário, na tabela de rastreabilidade):
  POST /login                                   entrar (no Swagger: botão "Authorize")
  GET  /horarios-disponiveis                    HU1  RN4
  POST /agendamentos                            HU1  RN1, RN2, RN3
  GET  /agendamentos  e  /agendamentos/{id}     HU2  RN10
  POST /agendamentos/{id}/cancelar              HU3  RN8, RN9
  POST /profissionais/{id}/bloqueios            HU4  RN5, RN9
  GET  /bloqueios/{id}/agendamentos-afetados    HU5  RN5
  POST /encaixes                                HU5  RN6
  POST /agendamentos/{id}/remarcacoes           HU5  RN7, RN9
  GET  /agendamentos/{id}/remarcacoes           HU5  RN7
  GET  /profissionais/{id}/agenda               HU6  RN10
  GET  /gestao/ocupacao                         HU7  RN10

Organização deste arquivo:
  1. Configurações e banco de dados
  2. Login e permissões por perfil
  3. Funções que aplicam as regras de negócio
  4. Rotas
"""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel

# ============================================================================
# 1. CONFIGURAÇÕES E BANCO DE DADOS
# ============================================================================
DB_PATH = os.environ.get("CLINICA_DB", "clinica.db")

# Premissa da dupla (confirmar com o professor): expediente das 08:00 às 18:00,
# com horários começando de 20 em 20 minutos.
HORA_ABRE = 8
HORA_FECHA = 18
PASSO_MINUTOS = 20

FORMATO = "%Y-%m-%dT%H:%M"  # como guardamos data e hora no banco

# RN9: de qual status para qual status um agendamento pode ir
TRANSICOES = {
    "CONFIRMADO": {"CONFIRMADO", "AFETADO", "CANCELADO"},  # CONFIRMADO->CONFIRMADO = remarcação
    "AFETADO": {"CONFIRMADO", "CANCELADO"},
    "CANCELADO": set(),  # cancelado não muda mais
}

app = FastAPI(
    title="API da Clínica Multiprofissional (caso 03A)",
    description=(
        "Agendamento de consultas com profissional e sala. "
        "Clique em **Authorize** e entre com um usuário de teste (ex.: maria / 1234)."
    ),
)


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conectar():
    """Abre uma conexão com o banco (um arquivo SQLite)."""
    con = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def transacao():
    """
    Roda um bloco de código como uma transação: ou tudo é salvo, ou nada é.
    BEGIN IMMEDIATE faz as reservas "entrarem na fila": se duas chegarem juntas,
    uma espera a outra terminar. É isso que garante a RN3.
    Se der qualquer erro (por exemplo um 409), o ROLLBACK desfaz tudo.
    """
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def hash_senha(senha: str) -> str:
    # Simples, para o trabalho. Em um sistema real usaríamos bcrypt/argon2.
    return hashlib.sha256(senha.encode()).hexdigest()


def garantir_coluna(con, tabela, coluna, definicao):
    """Se o banco for de uma versão antiga e faltar a coluna, cria ela."""
    colunas = [c["name"] for c in con.execute(f"PRAGMA table_info({tabela})")]
    if coluna not in colunas:
        con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def criar_banco():
    con = conectar()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS especialidade (
            id INTEGER PRIMARY KEY, nome TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS profissional (
            id INTEGER PRIMARY KEY, nome TEXT NOT NULL,
            especialidade_id INTEGER NOT NULL REFERENCES especialidade(id));

        CREATE TABLE IF NOT EXISTS sala (
            id INTEGER PRIMARY KEY, nome TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS paciente (
            id INTEGER PRIMARY KEY, nome TEXT NOT NULL, telefone TEXT);

        -- RN10: quem pode entrar e com qual perfil
        CREATE TABLE IF NOT EXISTS usuario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            perfil TEXT NOT NULL CHECK (perfil IN ('paciente','recepcao','profissional','gestor')),
            paciente_id INTEGER REFERENCES paciente(id),
            profissional_id INTEGER REFERENCES profissional(id));

        CREATE TABLE IF NOT EXISTS sessao (
            token TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuario(id),
            criado_em TEXT NOT NULL);

        -- RN2: quanto tempo cada tipo de consulta ocupa
        CREATE TABLE IF NOT EXISTS tipo_atendimento (
            especialidade_id INTEGER NOT NULL REFERENCES especialidade(id),
            tipo TEXT NOT NULL CHECK (tipo IN ('INICIAL', 'RETORNO')),
            duracao_prevista INTEGER NOT NULL,
            duracao_ocupada INTEGER NOT NULL,
            PRIMARY KEY (especialidade_id, tipo));

        CREATE TABLE IF NOT EXISTS agendamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paciente_id INTEGER NOT NULL REFERENCES paciente(id),
            profissional_id INTEGER NOT NULL REFERENCES profissional(id),
            sala_id INTEGER NOT NULL REFERENCES sala(id),
            tipo TEXT NOT NULL,
            inicio TEXT NOT NULL,
            fim TEXT NOT NULL,
            duracao_prevista INTEGER NOT NULL,
            duracao_ocupada INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'CONFIRMADO',
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            origem TEXT NOT NULL DEFAULT 'RESERVA',
            bloqueio_id INTEGER,
            criado_por INTEGER,
            cancelado_por INTEGER,
            cancelado_em TEXT,
            motivo_cancelamento TEXT);

        -- RN5: períodos em que o profissional não atende
        CREATE TABLE IF NOT EXISTS bloqueio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profissional_id INTEGER NOT NULL REFERENCES profissional(id),
            inicio TEXT NOT NULL,
            fim TEXT NOT NULL,
            motivo TEXT,
            criado_por INTEGER NOT NULL REFERENCES usuario(id),
            criado_em TEXT NOT NULL);

        -- RN7: cada remarcação fica guardada (só entram registros novos)
        CREATE TABLE IF NOT EXISTS historico_remarcacao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agendamento_id INTEGER NOT NULL REFERENCES agendamento(id),
            de_profissional_id INTEGER NOT NULL,
            de_sala_id INTEGER NOT NULL,
            de_inicio TEXT NOT NULL,
            de_fim TEXT NOT NULL,
            para_profissional_id INTEGER NOT NULL,
            para_sala_id INTEGER NOT NULL,
            para_inicio TEXT NOT NULL,
            para_fim TEXT NOT NULL,
            motivo TEXT,
            usuario_id INTEGER NOT NULL REFERENCES usuario(id),
            em TEXT NOT NULL);

        -- RN9 e RNF6: toda mudança de status registra quem fez e quando
        CREATE TABLE IF NOT EXISTS evento_agendamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agendamento_id INTEGER NOT NULL REFERENCES agendamento(id),
            de_status TEXT,
            para_status TEXT NOT NULL,
            usuario_id INTEGER NOT NULL REFERENCES usuario(id),
            motivo TEXT,
            em TEXT NOT NULL);
        """
    )

    # Se o banco for da versão anterior (só HU1), acrescenta o que faltava.
    garantir_coluna(con, "paciente", "telefone", "TEXT")
    garantir_coluna(con, "agendamento", "origem", "TEXT NOT NULL DEFAULT 'RESERVA'")
    garantir_coluna(con, "agendamento", "bloqueio_id", "INTEGER")
    garantir_coluna(con, "agendamento", "criado_por", "INTEGER")
    garantir_coluna(con, "agendamento", "cancelado_por", "INTEGER")
    garantir_coluna(con, "agendamento", "cancelado_em", "TEXT")
    garantir_coluna(con, "agendamento", "motivo_cancelamento", "TEXT")

    # Dados de teste (só entram se estiver vazio)
    if con.execute("SELECT COUNT(*) FROM profissional").fetchone()[0] == 0:
        con.executescript(
            """
            INSERT INTO especialidade (id, nome) VALUES
                (1, 'Clínica Geral'), (2, 'Fisioterapia'), (3, 'Nutrição');
            INSERT INTO profissional (id, nome, especialidade_id) VALUES
                (1, 'Dra. Ana', 1), (2, 'Dr. Bruno', 2), (3, 'Dra. Carla', 3);
            INSERT INTO sala (id, nome) VALUES (1, 'Sala 1'), (2, 'Sala 2');
            INSERT INTO paciente (id, nome) VALUES
                (1, 'Maria'), (2, 'João'), (3, 'Paula');
            -- O caso só fala em 40 min (inicial) e 20 min (retorno).
            INSERT INTO tipo_atendimento VALUES
                (1, 'INICIAL', 40, 40), (1, 'RETORNO', 20, 20),
                (2, 'INICIAL', 40, 40), (2, 'RETORNO', 20, 20),
                (3, 'INICIAL', 40, 40), (3, 'RETORNO', 20, 20);
            """
        )
    if con.execute("SELECT COUNT(*) FROM usuario").fetchone()[0] == 0:
        senha = hash_senha("1234")  # todos os usuários de teste usam a senha 1234
        usuarios = [
            ("maria", "paciente", 1, None), ("joao", "paciente", 2, None),
            ("paula", "paciente", 3, None),
            ("ana", "profissional", None, 1), ("bruno", "profissional", None, 2),
            ("carla", "profissional", None, 3),
            ("recepcao", "recepcao", None, None), ("gestor", "gestor", None, None),
        ]
        con.executemany(
            "INSERT INTO usuario (login, senha_hash, perfil, paciente_id, profissional_id) VALUES (?, ?, ?, ?, ?)",
            [(login, senha, perfil, pac, prof) for login, perfil, pac, prof in usuarios],
        )
        con.execute("UPDATE paciente SET telefone = '(11) 90000-000' || id WHERE telefone IS NULL")
    con.close()


criar_banco()


# ============================================================================
# 2. LOGIN E PERMISSÕES POR PERFIL (RN10)
# ============================================================================
oauth2 = OAuth2PasswordBearer(tokenUrl="login", auto_error=False)


def usuario_logado(token: Optional[str] = Depends(oauth2)):
    """Descobre quem está fazendo o pedido. Sem login válido: erro 401."""
    if not token:
        raise HTTPException(401, "Faça login primeiro.", headers={"WWW-Authenticate": "Bearer"})
    con = conectar()
    try:
        linha = con.execute(
            """SELECT u.* FROM sessao s JOIN usuario u ON u.id = s.usuario_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    finally:
        con.close()
    if linha is None:
        raise HTTPException(401, "Login inválido ou expirado.", headers={"WWW-Authenticate": "Bearer"})
    return dict(linha)


def exigir(*perfis):
    """Cria uma 'trava' para a rota: só deixa passar os perfis da lista. Outros: erro 403."""

    def verificar(usuario: dict = Depends(usuario_logado)):
        if usuario["perfil"] not in perfis:
            raise HTTPException(403, "Seu perfil não tem permissão para esta ação.")
        return usuario

    return verificar


# ============================================================================
# 3. FUNÇÕES QUE APLICAM AS REGRAS DE NEGÓCIO
# ============================================================================
def tem_conflito(con, coluna, valor, inicio, fim, ignorar_id=None):
    """
    Existe consulta CONFIRMADA desse profissional (ou dessa sala) que se
    sobrepõe ao intervalo? Duas consultas se sobrepõem quando uma começa antes
    de a outra terminar E termina depois de a outra começar. Isso pega também a
    sobreposição parcial (RN2). Consultas AFETADAS ou CANCELADAS não ocupam nada.
    """
    assert coluna in ("profissional_id", "sala_id")
    sql = f"""SELECT id FROM agendamento
              WHERE {coluna} = ? AND status = 'CONFIRMADO' AND inicio < ? AND fim > ?"""
    params = [valor, fim, inicio]
    if ignorar_id is not None:  # usado na remarcação: a consulta não conflita consigo mesma
        sql += " AND id != ?"
        params.append(ignorar_id)
    return con.execute(sql, params).fetchone() is not None


def tem_bloqueio(con, profissional_id, inicio, fim):
    linha = con.execute(
        "SELECT id FROM bloqueio WHERE profissional_id = ? AND inicio < ? AND fim > ?",
        (profissional_id, fim, inicio),
    ).fetchone()
    return linha is not None


def sala_livre(con, inicio, fim, sala_id=None, ignorar_id=None):
    """Devolve o id de uma sala livre no intervalo (ou None). Se sala_id vier, só testa ela."""
    if sala_id is not None:
        salas = [sala_id]
    else:
        salas = [s["id"] for s in con.execute("SELECT id FROM sala ORDER BY id")]
    for s in salas:
        if not tem_conflito(con, "sala_id", s, inicio, fim, ignorar_id):
            return s
    return None


def buscar_duracao(con, profissional, tipo):
    linha = con.execute(
        """SELECT duracao_prevista, duracao_ocupada FROM tipo_atendimento
           WHERE especialidade_id = ? AND tipo = ?""",
        (profissional["especialidade_id"], tipo),
    ).fetchone()
    if linha is None:
        raise HTTPException(422, "Esse tipo de atendimento não está configurado para a especialidade.")
    return linha


def dentro_do_expediente(inicio: datetime, fim: datetime) -> bool:
    abre = inicio.replace(hour=HORA_ABRE, minute=0)
    fecha = inicio.replace(hour=HORA_FECHA, minute=0)
    return inicio >= abre and fim <= fecha


def preparar_intervalo(con, profissional, tipo, inicio_dt):
    """RN2: calcula o fim pelo tipo (quem reserva não manda a duração)."""
    duracao = buscar_duracao(con, profissional, tipo)
    inicio = inicio_dt.replace(second=0, microsecond=0, tzinfo=None)
    fim = inicio + timedelta(minutes=duracao["duracao_ocupada"])
    if not dentro_do_expediente(inicio, fim):
        raise HTTPException(422, f"Fora do expediente ({HORA_ABRE}h às {HORA_FECHA}h).")
    return inicio.strftime(FORMATO), fim.strftime(FORMATO), duracao


def reservar_recursos(con, profissional_id, inicio, fim, sala_id=None, ignorar_id=None):
    """
    RN1: profissional E sala precisam estar livres. Devolve a sala escolhida ou
    levanta 409 dizendo o que está ocupado.
    """
    if tem_bloqueio(con, profissional_id, inicio, fim):
        raise HTTPException(409, "O profissional está com a agenda bloqueada nesse horário.")
    if tem_conflito(con, "profissional_id", profissional_id, inicio, fim, ignorar_id):
        raise HTTPException(409, "O profissional já tem consulta nesse horário.")
    sala = sala_livre(con, inicio, fim, sala_id, ignorar_id)
    if sala is None:
        raise HTTPException(409, "A sala está ocupada nesse horário." if sala_id else "Não há sala livre nesse horário.")
    return sala


def registrar_evento(con, agendamento_id, de_status, para_status, usuario, motivo=None):
    con.execute(
        """INSERT INTO evento_agendamento
           (agendamento_id, de_status, para_status, usuario_id, motivo, em)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agendamento_id, de_status, para_status, usuario["id"], motivo, agora()),
    )


def mudar_status(con, agendamento, novo, usuario, motivo=None):
    """RN9: só deixa mudar de status pelos caminhos permitidos. Outros: 409."""
    atual = agendamento["status"]
    if novo not in TRANSICOES[atual]:
        raise HTTPException(409, f"Não é possível: o agendamento está {atual}.")
    con.execute("UPDATE agendamento SET status = ? WHERE id = ?", (novo, agendamento["id"]))
    registrar_evento(con, agendamento["id"], atual, novo, usuario, motivo)


def criar_reserva(con, dados, paciente_id, origem, usuario):
    """Usada pela reserva do paciente (RESERVA) e pelo encaixe da recepção (ENCAIXE)."""
    prof = con.execute("SELECT * FROM profissional WHERE id = ?", (dados.profissional_id,)).fetchone()
    if prof is None:
        raise HTTPException(404, "Profissional não encontrado.")
    if con.execute("SELECT id FROM paciente WHERE id = ?", (paciente_id,)).fetchone() is None:
        raise HTTPException(404, "Paciente não encontrado.")
    if dados.sala_id is not None and con.execute("SELECT id FROM sala WHERE id = ?", (dados.sala_id,)).fetchone() is None:
        raise HTTPException(404, "Sala não encontrada.")

    inicio, fim, duracao = preparar_intervalo(con, prof, dados.tipo, dados.inicio)
    sala = reservar_recursos(con, prof["id"], inicio, fim, dados.sala_id)

    cur = con.execute(
        """INSERT INTO agendamento
           (paciente_id, profissional_id, sala_id, tipo, inicio, fim,
            duracao_prevista, duracao_ocupada, origem, criado_por, criado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (paciente_id, prof["id"], sala, dados.tipo, inicio, fim,
         duracao["duracao_prevista"], duracao["duracao_ocupada"], origem, usuario["id"], agora()),
    )
    registrar_evento(con, cur.lastrowid, None, "CONFIRMADO", usuario)
    return dict(con.execute("SELECT * FROM agendamento WHERE id = ?", (cur.lastrowid,)).fetchone())


def buscar_ou_404(con, agendamento_id):
    linha = con.execute("SELECT * FROM agendamento WHERE id = ?", (agendamento_id,)).fetchone()
    if linha is None:
        raise HTTPException(404, "Agendamento não encontrado.")
    return linha


def ler_data(texto: str) -> date:
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Data inválida. Use o formato AAAA-MM-DD.")


# ============================================================================
# 4. ROTAS
# ============================================================================
class NovaReserva(BaseModel):
    # O paciente que reserva NÃO precisa mandar paciente_id: ele vem do login.
    # A recepção precisa informar o paciente_id (reserva em nome de outra pessoa).
    paciente_id: Optional[int] = None
    profissional_id: int
    tipo: Literal["INICIAL", "RETORNO"]
    inicio: datetime  # exemplo: 2026-12-01T09:00
    sala_id: Optional[int] = None  # se não vier, a API escolhe uma sala livre


class Cancelamento(BaseModel):
    motivo: Optional[str] = None


class NovoBloqueio(BaseModel):
    inicio: datetime
    fim: datetime
    motivo: Optional[str] = None


class Remarcacao(BaseModel):
    novo_inicio: datetime
    profissional_id: Optional[int] = None  # se não vier, continua o mesmo
    sala_id: Optional[int] = None  # se não vier, a API escolhe uma sala livre
    motivo: Optional[str] = None


# ---------------------------------------------------------------- login
@app.post("/login", summary="Entrar no sistema")
def login(form: OAuth2PasswordRequestForm = Depends()):
    con = conectar()
    try:
        usuario = con.execute("SELECT * FROM usuario WHERE login = ?", (form.username,)).fetchone()
        if usuario is None or usuario["senha_hash"] != hash_senha(form.password):
            raise HTTPException(401, "Usuário ou senha incorretos.")
        token = secrets.token_hex(16)
        con.execute("INSERT INTO sessao (token, usuario_id, criado_em) VALUES (?, ?, ?)", (token, usuario["id"], agora()))
        return {"access_token": token, "token_type": "bearer", "perfil": usuario["perfil"]}
    finally:
        con.close()


# ---------------------------------------------------------------- HU1
@app.get("/horarios-disponiveis", summary="Listar horários livres (RN4)")
def horarios_disponiveis(
    profissional_id: int,
    data: date,
    tipo: Literal["INICIAL", "RETORNO"],
    usuario: dict = Depends(exigir("paciente", "recepcao")),
):
    """Só mostra. Não reserva nada: a reserva só vale quando for confirmada (POST /agendamentos)."""
    con = conectar()
    try:
        prof = con.execute("SELECT * FROM profissional WHERE id = ?", (profissional_id,)).fetchone()
        if prof is None:
            raise HTTPException(404, "Profissional não encontrado.")
        duracao = buscar_duracao(con, prof, tipo)["duracao_ocupada"]

        horarios = []
        atual = datetime(data.year, data.month, data.day, HORA_ABRE, 0)
        while True:
            fim = atual + timedelta(minutes=duracao)
            if not dentro_do_expediente(atual, fim):
                break
            i, f = atual.strftime(FORMATO), fim.strftime(FORMATO)
            profissional_livre = not tem_bloqueio(con, profissional_id, i, f) and not tem_conflito(
                con, "profissional_id", profissional_id, i, f
            )
            if profissional_livre:
                sala = sala_livre(con, i, f)
                if sala is not None:  # profissional E sala precisam estar livres
                    horarios.append({"inicio": i, "fim": f, "sala_id": sala})
            atual += timedelta(minutes=PASSO_MINUTOS)
        return {"profissional_id": profissional_id, "data": data.isoformat(), "tipo": tipo, "horarios": horarios}
    finally:
        con.close()


@app.post("/agendamentos", status_code=201, summary="Confirmar uma reserva (RN1, RN2, RN3)")
def criar_agendamento(dados: NovaReserva, usuario: dict = Depends(exigir("paciente", "recepcao"))):
    if usuario["perfil"] == "paciente":
        # RN10: o paciente só reserva para si mesmo
        if dados.paciente_id not in (None, usuario["paciente_id"]):
            raise HTTPException(403, "Você só pode reservar para você mesmo.")
        paciente_id = usuario["paciente_id"]
    else:
        if dados.paciente_id is None:
            raise HTTPException(422, "Informe o paciente_id.")
        paciente_id = dados.paciente_id

    # RN3: tudo dentro de UMA transação "na fila" (ver função transacao)
    with transacao() as con:
        return criar_reserva(con, dados, paciente_id, "RESERVA", usuario)


# ---------------------------------------------------------------- HU2
@app.get("/agendamentos", summary="Listar agendamentos (paciente: os próprios; recepção: todos)")
def listar_agendamentos(usuario: dict = Depends(exigir("paciente", "recepcao"))):
    con = conectar()
    try:
        if usuario["perfil"] == "paciente":
            linhas = con.execute(
                "SELECT * FROM agendamento WHERE paciente_id = ? ORDER BY inicio", (usuario["paciente_id"],)
            ).fetchall()
        else:
            linhas = con.execute("SELECT * FROM agendamento ORDER BY inicio").fetchall()
        return [dict(l) for l in linhas]
    finally:
        con.close()


@app.get("/agendamentos/{agendamento_id}", summary="Consultar um agendamento (RN10)")
def buscar_agendamento(agendamento_id: int, usuario: dict = Depends(exigir("paciente", "recepcao", "profissional"))):
    con = conectar()
    try:
        ag = buscar_ou_404(con, agendamento_id)
        # RN10: de outro paciente (ou de outra agenda) responde 404, sem revelar que existe
        if usuario["perfil"] == "paciente" and ag["paciente_id"] != usuario["paciente_id"]:
            raise HTTPException(404, "Agendamento não encontrado.")
        if usuario["perfil"] == "profissional" and ag["profissional_id"] != usuario["profissional_id"]:
            raise HTTPException(404, "Agendamento não encontrado.")
        return dict(ag)
    finally:
        con.close()


# ---------------------------------------------------------------- HU3
@app.post("/agendamentos/{agendamento_id}/cancelar", summary="Cancelar um agendamento (RN8, RN9)")
def cancelar_agendamento(
    agendamento_id: int,
    dados: Optional[Cancelamento] = None,
    usuario: dict = Depends(exigir("paciente")),
):
    motivo = dados.motivo if dados else None
    with transacao() as con:
        ag = buscar_ou_404(con, agendamento_id)
        if ag["paciente_id"] != usuario["paciente_id"]:
            raise HTTPException(404, "Agendamento não encontrado.")
        mudar_status(con, ag, "CANCELADO", usuario, motivo)  # RN9: cancelar de novo dá 409
        con.execute(
            "UPDATE agendamento SET cancelado_por = ?, cancelado_em = ?, motivo_cancelamento = ? WHERE id = ?",
            (usuario["id"], agora(), motivo, agendamento_id),
        )
        return dict(con.execute("SELECT * FROM agendamento WHERE id = ?", (agendamento_id,)).fetchone())


# ---------------------------------------------------------------- HU4
@app.post("/profissionais/{profissional_id}/bloqueios", status_code=201, summary="Bloquear a agenda (RN5)")
def criar_bloqueio(profissional_id: int, dados: NovoBloqueio, usuario: dict = Depends(exigir("profissional"))):
    if usuario["profissional_id"] != profissional_id:
        raise HTTPException(403, "Você só pode bloquear a sua própria agenda.")
    ini = dados.inicio.replace(second=0, microsecond=0, tzinfo=None)
    fim = dados.fim.replace(second=0, microsecond=0, tzinfo=None)
    if fim <= ini:
        raise HTTPException(422, "O fim do bloqueio precisa ser depois do início.")
    i, f = ini.strftime(FORMATO), fim.strftime(FORMATO)

    with transacao() as con:
        cur = con.execute(
            """INSERT INTO bloqueio (profissional_id, inicio, fim, motivo, criado_por, criado_em)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (profissional_id, i, f, dados.motivo, usuario["id"], agora()),
        )
        bloqueio_id = cur.lastrowid
        # as consultas confirmadas dentro do período passam a AFETADO (e liberam a sala)
        afetados = con.execute(
            """SELECT * FROM agendamento
               WHERE profissional_id = ? AND status = 'CONFIRMADO' AND inicio < ? AND fim > ?""",
            (profissional_id, f, i),
        ).fetchall()
        for ag in afetados:
            mudar_status(con, ag, "AFETADO", usuario, dados.motivo)
            con.execute("UPDATE agendamento SET bloqueio_id = ? WHERE id = ?", (bloqueio_id, ag["id"]))
        return {
            "id": bloqueio_id, "profissional_id": profissional_id, "inicio": i, "fim": f,
            "motivo": dados.motivo, "agendamentos_afetados": [ag["id"] for ag in afetados],
        }


# ---------------------------------------------------------------- HU5
@app.get("/bloqueios/{bloqueio_id}/agendamentos-afetados", summary="Pacientes afetados por um bloqueio (RN5)")
def listar_afetados(bloqueio_id: int, usuario: dict = Depends(exigir("recepcao"))):
    con = conectar()
    try:
        if con.execute("SELECT id FROM bloqueio WHERE id = ?", (bloqueio_id,)).fetchone() is None:
            raise HTTPException(404, "Bloqueio não encontrado.")
        linhas = con.execute(
            """SELECT a.id AS agendamento_id, a.status, a.tipo, a.inicio, a.fim,
                      a.profissional_id, a.sala_id,
                      p.id AS paciente_id, p.nome AS paciente_nome, p.telefone AS paciente_telefone
               FROM agendamento a JOIN paciente p ON p.id = a.paciente_id
               WHERE a.bloqueio_id = ? AND a.status = 'AFETADO'
               ORDER BY a.inicio""",
            (bloqueio_id,),
        ).fetchall()
        return [dict(l) for l in linhas]
    finally:
        con.close()


@app.post("/encaixes", status_code=201, summary="Registrar um encaixe (RN6)")
def criar_encaixe(dados: NovaReserva, usuario: dict = Depends(exigir("recepcao"))):
    if dados.paciente_id is None:
        raise HTTPException(422, "Informe o paciente_id.")
    with transacao() as con:  # mesmas checagens da reserva (RN1 e RN2)
        return criar_reserva(con, dados, dados.paciente_id, "ENCAIXE", usuario)


@app.post("/agendamentos/{agendamento_id}/remarcacoes", summary="Remarcar com histórico (RN7, RN9)")
def remarcar(agendamento_id: int, dados: Remarcacao, usuario: dict = Depends(exigir("recepcao"))):
    with transacao() as con:
        ag = buscar_ou_404(con, agendamento_id)
        if ag["status"] == "CANCELADO":
            raise HTTPException(409, "Não é possível remarcar um agendamento cancelado.")

        novo_prof_id = dados.profissional_id or ag["profissional_id"]
        prof = con.execute("SELECT * FROM profissional WHERE id = ?", (novo_prof_id,)).fetchone()
        if prof is None:
            raise HTTPException(404, "Profissional não encontrado.")
        if dados.sala_id is not None and con.execute("SELECT id FROM sala WHERE id = ?", (dados.sala_id,)).fetchone() is None:
            raise HTTPException(404, "Sala não encontrada.")

        # valida o novo horário com as mesmas regras da reserva; se falhar, dá erro
        # aqui e o ROLLBACK deixa a consulta e o histórico exatamente como estavam
        inicio, fim, duracao = preparar_intervalo(con, prof, ag["tipo"], dados.novo_inicio)
        sala = reservar_recursos(con, prof["id"], inicio, fim, dados.sala_id, ignorar_id=ag["id"])

        con.execute(
            """INSERT INTO historico_remarcacao
               (agendamento_id, de_profissional_id, de_sala_id, de_inicio, de_fim,
                para_profissional_id, para_sala_id, para_inicio, para_fim, motivo, usuario_id, em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ag["id"], ag["profissional_id"], ag["sala_id"], ag["inicio"], ag["fim"],
             prof["id"], sala, inicio, fim, dados.motivo, usuario["id"], agora()),
        )
        con.execute(
            """UPDATE agendamento SET profissional_id = ?, sala_id = ?, inicio = ?, fim = ?,
               duracao_prevista = ?, duracao_ocupada = ?, bloqueio_id = NULL WHERE id = ?""",
            (prof["id"], sala, inicio, fim, duracao["duracao_prevista"], duracao["duracao_ocupada"], ag["id"]),
        )
        mudar_status(con, ag, "CONFIRMADO", usuario, dados.motivo)  # AFETADO volta a CONFIRMADO
        return dict(con.execute("SELECT * FROM agendamento WHERE id = ?", (ag["id"],)).fetchone())


@app.get("/agendamentos/{agendamento_id}/remarcacoes", summary="Histórico de remarcações (RN7)")
def historico_remarcacoes(agendamento_id: int, usuario: dict = Depends(exigir("recepcao"))):
    con = conectar()
    try:
        buscar_ou_404(con, agendamento_id)
        linhas = con.execute(
            "SELECT * FROM historico_remarcacao WHERE agendamento_id = ? ORDER BY id", (agendamento_id,)
        ).fetchall()
        return [dict(l) for l in linhas]
    finally:
        con.close()


# ---------------------------------------------------------------- HU6
@app.get("/profissionais/{profissional_id}/agenda", summary="Agenda de um profissional em um dia (RN10)")
def agenda_do_profissional(
    profissional_id: int,
    data: str,  # AAAA-MM-DD
    usuario: dict = Depends(exigir("recepcao", "profissional")),
):
    if usuario["perfil"] == "profissional" and usuario["profissional_id"] != profissional_id:
        raise HTTPException(403, "Você só pode ver a sua própria agenda.")
    dia = ler_data(data)
    ini = dia.strftime("%Y-%m-%d") + "T00:00"
    lim = (dia + timedelta(days=1)).strftime("%Y-%m-%d") + "T00:00"
    con = conectar()
    try:
        if con.execute("SELECT id FROM profissional WHERE id = ?", (profissional_id,)).fetchone() is None:
            raise HTTPException(404, "Profissional não encontrado.")
        consultas = con.execute(
            """SELECT a.id AS agendamento_id, a.inicio, a.fim, a.tipo, a.status, a.sala_id,
                      p.id AS paciente_id, p.nome AS paciente_nome
               FROM agendamento a JOIN paciente p ON p.id = a.paciente_id
               WHERE a.profissional_id = ? AND a.inicio >= ? AND a.inicio < ?
               ORDER BY a.inicio""",
            (profissional_id, ini, lim),
        ).fetchall()
        bloqueios = con.execute(
            "SELECT id, inicio, fim, motivo FROM bloqueio WHERE profissional_id = ? AND inicio < ? AND fim > ?",
            (profissional_id, lim, ini),
        ).fetchall()
        return {
            "profissional_id": profissional_id, "data": dia.isoformat(),
            "consultas": [dict(c) for c in consultas], "bloqueios": [dict(b) for b in bloqueios],
        }
    finally:
        con.close()


# ---------------------------------------------------------------- HU7
def resumo_ocupacao(rotulo_id, nome, ocupados, capacidade):
    return {
        "id": rotulo_id, "nome": nome,
        "minutos_ocupados": ocupados,
        "minutos_livres": max(capacidade - ocupados, 0),
        "percentual_ocupacao": round(100 * ocupados / capacidade, 1) if capacidade else 0,
    }


@app.get("/gestao/ocupacao", summary="Ocupação por sala e especialidade, sem dados de pacientes (RN10)")
def ocupacao(de: str, ate: str, usuario: dict = Depends(exigir("gestor"))):
    d1, d2 = ler_data(de), ler_data(ate)
    if d2 < d1:
        raise HTTPException(422, "A data final precisa ser igual ou depois da inicial.")
    dias = (d2 - d1).days + 1
    capacidade = dias * (HORA_FECHA - HORA_ABRE) * 60  # minutos de expediente por sala ou profissional
    ini = d1.strftime("%Y-%m-%d") + "T00:00"
    lim = (d2 + timedelta(days=1)).strftime("%Y-%m-%d") + "T00:00"

    con = conectar()
    try:
        salas = []
        for s in con.execute("SELECT id, nome FROM sala ORDER BY id").fetchall():
            ocupados = con.execute(
                """SELECT COALESCE(SUM(duracao_ocupada), 0) FROM agendamento
                   WHERE sala_id = ? AND status = 'CONFIRMADO' AND inicio >= ? AND inicio < ?""",
                (s["id"], ini, lim),
            ).fetchone()[0]
            salas.append(resumo_ocupacao(s["id"], s["nome"], ocupados, capacidade))

        especialidades = []
        for e in con.execute("SELECT id, nome FROM especialidade ORDER BY id").fetchall():
            qtd = con.execute("SELECT COUNT(*) FROM profissional WHERE especialidade_id = ?", (e["id"],)).fetchone()[0]
            ocupados = con.execute(
                """SELECT COALESCE(SUM(a.duracao_ocupada), 0)
                   FROM agendamento a JOIN profissional pr ON pr.id = a.profissional_id
                   WHERE pr.especialidade_id = ? AND a.status = 'CONFIRMADO'
                     AND a.inicio >= ? AND a.inicio < ?""",
                (e["id"], ini, lim),
            ).fetchone()[0]
            especialidades.append(resumo_ocupacao(e["id"], e["nome"], ocupados, qtd * capacidade))
        return {"de": d1.isoformat(), "ate": d2.isoformat(), "salas": salas, "especialidades": especialidades}
    finally:
        con.close()


# ============================================================================
# 5. FRONT-END (arquivos estáticos em ./frontend, abre em http://127.0.0.1:8000/app)
# ============================================================================
from fastapi.staticfiles import StaticFiles  # noqa: E402

_FRONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
if os.path.isdir(_FRONT):
    app.mount("/app", StaticFiles(directory=_FRONT, html=True), name="front")


from fastapi.responses import RedirectResponse  # noqa: E402


@app.get("/", include_in_schema=False)
def raiz():
    """Quem abrir http://127.0.0.1:8000 cai direto no front-end."""
    return RedirectResponse("/app/")

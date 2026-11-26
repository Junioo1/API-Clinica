"""
Testes automáticos. Cada função é um caso de teste do formulário (CT01 a CT28).
Rodar com:  pytest -v

Usuários de teste (senha 1234): maria, joao, paula (pacientes), ana, bruno, carla
(profissionais), recepcao e gestor.
"""
import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

DIA = "2026-12-01"
LOGINS = ["maria", "joao", "paula", "ana", "bruno", "carla", "recepcao", "gestor"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # cada teste usa um banco novo e vazio
    monkeypatch.setenv("CLINICA_DB", str(tmp_path / "teste.db"))
    import main
    importlib.reload(main)
    return TestClient(main.app)


@pytest.fixture()
def t(client):
    """Cabeçalhos de login de cada usuário: t['maria'], t['recepcao'], ..."""
    cabecalhos = {}
    for login in LOGINS:
        r = client.post("/login", data={"username": login, "password": "1234"})
        assert r.status_code == 200
        cabecalhos[login] = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return cabecalhos


def reservar(client, t, quem="maria", profissional=1, tipo="INICIAL", hora="09:00", sala=None, **extra):
    corpo = {"profissional_id": profissional, "tipo": tipo, "inicio": f"{DIA}T{hora}", **extra}
    if sala:
        corpo["sala_id"] = sala
    return client.post("/agendamentos", json=corpo, headers=t[quem])


def horarios(client, t, profissional=1, tipo="INICIAL", quem="maria"):
    r = client.get("/horarios-disponiveis", headers=t[quem],
                   params={"profissional_id": profissional, "data": DIA, "tipo": tipo})
    assert r.status_code == 200
    return [h["inicio"] for h in r.json()["horarios"]]


def bloquear_manha(client, t, quem="ana", profissional=1):
    return client.post(f"/profissionais/{profissional}/bloqueios", headers=t[quem],
                       json={"inicio": f"{DIA}T08:00", "fim": f"{DIA}T12:00", "motivo": "Compromisso"})


def status_de(client, t, ag_id):
    return client.get(f"/agendamentos/{ag_id}", headers=t["recepcao"]).json()["status"]


# ----------------------------------------------------------------- HU1
def test_ct01_listar_horarios_livres(client, t):
    assert horarios(client, t)[0] == f"{DIA}T08:00"


def test_ct02_horario_livre_para_profissional_mas_sala_ocupada_nao_aparece(client, t):
    # as duas salas ficam ocupadas às 09:00 (por Ana e Bruno)
    assert reservar(client, t, "maria", profissional=1, sala=1).status_code == 201
    assert reservar(client, t, "joao", profissional=2, sala=2).status_code == 201
    # Carla está livre às 09:00, mas não há sala: o horário não pode ser oferecido
    inicios = horarios(client, t, profissional=3)
    assert f"{DIA}T09:00" not in inicios
    assert f"{DIA}T08:00" in inicios


def test_ct03_consulta_inicial_ocupa_40_minutos(client, t):
    r = reservar(client, t, tipo="INICIAL")
    assert r.status_code == 201
    assert r.json()["fim"] == f"{DIA}T09:40"
    assert r.json()["status"] == "CONFIRMADO"


def test_ct04_retorno_ocupa_20_minutos(client, t):
    r = reservar(client, t, tipo="RETORNO")
    assert r.status_code == 201
    assert r.json()["fim"] == f"{DIA}T09:20"


def test_ct05_sobreposicao_parcial_e_recusada(client, t):
    assert reservar(client, t, "maria", tipo="INICIAL", hora="09:00").status_code == 201  # 09:00 a 09:40
    assert reservar(client, t, "joao", tipo="RETORNO", hora="09:20").status_code == 409


def test_ct06_dois_pacientes_no_mesmo_horario_so_um_fica(client, t):
    assert reservar(client, t, "maria").status_code == 201
    assert reservar(client, t, "joao").status_code == 409


def test_ct06_reservas_simultaneas(client, t):
    # 10 pedidos ao mesmo tempo para o mesmo horário: exatamente 1 pode dar certo
    with ThreadPoolExecutor(max_workers=10) as pool:
        respostas = list(pool.map(lambda _: reservar(client, t, "maria"), range(10)))
    codigos = sorted(r.status_code for r in respostas)
    assert codigos.count(201) == 1
    assert codigos.count(409) == 9


def test_ct07_profissional_livre_mas_sala_ocupada(client, t):
    assert reservar(client, t, "maria", profissional=1, sala=1).status_code == 201
    r = reservar(client, t, "joao", profissional=2, sala=1)  # Bruno livre, Sala 1 ocupada
    assert r.status_code == 409
    assert "sala" in r.json()["detail"].lower()
    # e nada foi gravado para o Bruno
    assert client.get("/agendamentos/2", headers=t["recepcao"]).status_code == 404


def test_ct08_paciente_nao_reserva_em_nome_de_outro(client, t):
    r = reservar(client, t, "maria", paciente_id=2)  # Maria tentando reservar para o João
    assert r.status_code == 403
    assert client.get("/agendamentos", headers=t["recepcao"]).json() == []


# ----------------------------------------------------------------- HU2
def test_ct09_consultar_agendamento_proprio(client, t):
    criado = reservar(client, t, "maria").json()
    r = client.get(f"/agendamentos/{criado['id']}", headers=t["maria"])
    assert r.status_code == 200
    assert r.json()["id"] == criado["id"]
    assert len(client.get("/agendamentos", headers=t["maria"]).json()) == 1


def test_ct10_consultar_agendamento_de_outro_paciente_da_404(client, t):
    criado = reservar(client, t, "maria").json()
    assert client.get(f"/agendamentos/{criado['id']}", headers=t["joao"]).status_code == 404
    assert client.get("/agendamentos", headers=t["joao"]).json() == []


# ----------------------------------------------------------------- HU3
def test_ct11_cancelar_libera_o_horario(client, t):
    ag = reservar(client, t, "maria").json()
    assert f"{DIA}T09:00" not in horarios(client, t, profissional=1)  # ocupado antes de cancelar
    r = client.post(f"/agendamentos/{ag['id']}/cancelar", headers=t["maria"], json={"motivo": "Imprevisto"})
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELADO"
    assert r.json()["cancelado_por"] is not None
    # profissional e sala voltam a ficar livres: o horário reaparece na lista
    assert f"{DIA}T09:00" in horarios(client, t, profissional=1)


def test_ct12_cancelar_de_novo_da_409(client, t):
    ag = reservar(client, t, "maria").json()
    assert client.post(f"/agendamentos/{ag['id']}/cancelar", headers=t["maria"]).status_code == 200
    assert client.post(f"/agendamentos/{ag['id']}/cancelar", headers=t["maria"]).status_code == 409
    assert status_de(client, t, ag["id"]) == "CANCELADO"


def test_ct13_cancelar_agendamento_de_outro_paciente_da_404(client, t):
    ag = reservar(client, t, "maria").json()
    assert client.post(f"/agendamentos/{ag['id']}/cancelar", headers=t["joao"]).status_code == 404
    assert status_de(client, t, ag["id"]) == "CONFIRMADO"


# ----------------------------------------------------------------- HU4
def test_ct14_bloqueio_marca_reservas_como_afetadas_e_libera_a_sala(client, t):
    ag = reservar(client, t, "maria", profissional=1, hora="09:00", sala=1).json()
    r = bloquear_manha(client, t)
    assert r.status_code == 201
    assert r.json()["agendamentos_afetados"] == [ag["id"]]
    assert status_de(client, t, ag["id"]) == "AFETADO"
    # a Sala 1 foi liberada: outro profissional consegue usá-la nesse horário
    assert reservar(client, t, "joao", profissional=2, sala=1).status_code == 201
    # e a lista de horários da Ana já não mostra a manhã (RNF2)
    assert not [i for i in horarios(client, t, profissional=1) if i < f"{DIA}T12:00"]


def test_ct15_bloqueio_com_fim_antes_do_inicio_da_422(client, t):
    ag = reservar(client, t, "maria").json()
    r = client.post("/profissionais/1/bloqueios", headers=t["ana"],
                    json={"inicio": f"{DIA}T12:00", "fim": f"{DIA}T08:00"})
    assert r.status_code == 422
    assert status_de(client, t, ag["id"]) == "CONFIRMADO"


def test_ct16_profissional_nao_bloqueia_agenda_de_outro(client, t):
    ag = reservar(client, t, "maria").json()
    r = bloquear_manha(client, t, quem="bruno", profissional=1)  # Bruno tentando bloquear a Ana
    assert r.status_code == 403
    assert status_de(client, t, ag["id"]) == "CONFIRMADO"


def test_ct17_nao_reserva_em_horario_bloqueado(client, t):
    bloquear_manha(client, t)
    r = reservar(client, t, "joao", profissional=1, hora="10:00")
    assert r.status_code == 409


# ----------------------------------------------------------------- HU5
def test_ct18_recepcao_lista_os_afetados(client, t):
    ag = reservar(client, t, "maria", hora="09:00").json()
    bloqueio_id = bloquear_manha(client, t).json()["id"]
    r = client.get(f"/bloqueios/{bloqueio_id}/agendamentos-afetados", headers=t["recepcao"])
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["agendamento_id"] == ag["id"]
    assert r.json()[0]["paciente_nome"] == "Maria"
    assert r.json()[0]["paciente_telefone"]
    # paciente não vê essa lista
    assert client.get(f"/bloqueios/{bloqueio_id}/agendamentos-afetados", headers=t["maria"]).status_code == 403


def test_ct19_remarcar_move_a_consulta_e_grava_historico(client, t):
    ag = reservar(client, t, "maria", hora="09:00").json()
    bloquear_manha(client, t)
    r = client.post(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"],
                    json={"novo_inicio": f"{DIA}T14:00", "motivo": "Profissional bloqueou a manhã"})
    assert r.status_code == 200
    assert r.json()["status"] == "CONFIRMADO"
    assert r.json()["inicio"] == f"{DIA}T14:00"
    historico = client.get(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"]).json()
    assert len(historico) == 1
    assert historico[0]["de_inicio"] == f"{DIA}T09:00"
    assert historico[0]["para_inicio"] == f"{DIA}T14:00"
    assert historico[0]["motivo"] == "Profissional bloqueou a manhã"


def test_ct20_remarcar_para_sala_ocupada_nao_muda_nada(client, t):
    ag = reservar(client, t, "maria", profissional=1, hora="09:00", sala=1).json()
    assert reservar(client, t, "joao", profissional=2, hora="10:00", sala=2).status_code == 201
    r = client.post(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"],
                    json={"novo_inicio": f"{DIA}T10:00", "sala_id": 2})  # Ana livre, Sala 2 ocupada
    assert r.status_code == 409
    atual = client.get(f"/agendamentos/{ag['id']}", headers=t["recepcao"]).json()
    assert atual["inicio"] == f"{DIA}T09:00" and atual["sala_id"] == 1
    assert client.get(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"]).json() == []


def test_ct21_encaixe_valido(client, t):
    r = client.post("/encaixes", headers=t["recepcao"],
                    json={"paciente_id": 3, "profissional_id": 1, "tipo": "RETORNO", "inicio": f"{DIA}T11:00"})
    assert r.status_code == 201
    assert r.json()["origem"] == "ENCAIXE"
    assert r.json()["paciente_id"] == 3


def test_ct22_encaixe_em_sala_ocupada(client, t):
    assert reservar(client, t, "maria", profissional=1, sala=1).status_code == 201
    r = client.post("/encaixes", headers=t["recepcao"],
                    json={"paciente_id": 3, "profissional_id": 2, "tipo": "INICIAL",
                          "inicio": f"{DIA}T09:00", "sala_id": 1})
    assert r.status_code == 409
    assert len(client.get("/agendamentos", headers=t["recepcao"]).json()) == 1


def test_ct23_historico_em_ordem_cronologica(client, t):
    ag = reservar(client, t, "maria", hora="09:00").json()
    for hora in ("14:00", "15:00"):
        r = client.post(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"],
                        json={"novo_inicio": f"{DIA}T{hora}"})
        assert r.status_code == 200
    historico = client.get(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"]).json()
    assert [h["para_inicio"] for h in historico] == [f"{DIA}T14:00", f"{DIA}T15:00"]
    assert historico[1]["de_inicio"] == f"{DIA}T14:00"


def test_ct24_paciente_nao_remarca_nem_encaixa(client, t):
    ag = reservar(client, t, "maria").json()
    r = client.post(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["maria"], json={"novo_inicio": f"{DIA}T14:00"})
    assert r.status_code == 403
    r = client.post("/encaixes", headers=t["maria"],
                    json={"paciente_id": 1, "profissional_id": 1, "tipo": "RETORNO", "inicio": f"{DIA}T11:00"})
    assert r.status_code == 403
    assert client.post("/encaixes", headers=t["gestor"],
                       json={"paciente_id": 1, "profissional_id": 1, "tipo": "RETORNO",
                             "inicio": f"{DIA}T11:00"}).status_code == 403


# ----------------------------------------------------------------- HU6
def test_ct25_agenda_do_profissional(client, t):
    reservar(client, t, "maria", profissional=1)
    for quem in ("recepcao", "ana"):
        r = client.get("/profissionais/1/agenda", headers=t[quem], params={"data": DIA})
        assert r.status_code == 200
        assert len(r.json()["consultas"]) == 1
        assert r.json()["consultas"][0]["paciente_nome"] == "Maria"


def test_ct26_profissional_nao_ve_agenda_de_outro(client, t):
    reservar(client, t, "maria", profissional=1)
    r = client.get("/profissionais/1/agenda", headers=t["bruno"], params={"data": DIA})
    assert r.status_code == 403
    assert "Maria" not in r.text


def test_hu6_data_invalida_da_400(client, t):
    r = client.get("/profissionais/1/agenda", headers=t["recepcao"], params={"data": "01/12/2026"})
    assert r.status_code == 400


# ----------------------------------------------------------------- HU7
def test_ct27_gestor_ve_so_numeros_sem_dados_de_pacientes(client, t):
    reservar(client, t, "maria", profissional=1, sala=1)  # 40 min
    r = client.get("/gestao/ocupacao", headers=t["gestor"], params={"de": DIA, "ate": DIA})
    assert r.status_code == 200
    sala1 = r.json()["salas"][0]
    assert sala1["minutos_ocupados"] == 40
    assert sala1["percentual_ocupacao"] > 0
    texto = json.dumps(r.json(), ensure_ascii=False).lower()
    assert "paciente" not in texto and "maria" not in texto


def test_ct28_gestor_nao_abre_detalhe_de_agendamento(client, t):
    ag = reservar(client, t, "maria").json()
    r = client.get(f"/agendamentos/{ag['id']}", headers=t["gestor"])
    assert r.status_code == 403
    assert "Maria" not in r.text


def test_hu7_periodo_invertido_da_422(client, t):
    r = client.get("/gestao/ocupacao", headers=t["gestor"], params={"de": "2026-12-05", "ate": "2026-12-01"})
    assert r.status_code == 422


# ----------------------------------------------------------------- extras (RNF4 e RN9)
def test_sem_login_da_401(client):
    assert client.get("/agendamentos").status_code == 401
    assert client.post("/agendamentos", json={}).status_code == 401


def test_login_com_senha_errada_da_401(client):
    r = client.post("/login", data={"username": "maria", "password": "errada"})
    assert r.status_code == 401


def test_cancelado_nao_pode_ser_remarcado(client, t):
    ag = reservar(client, t, "maria").json()
    client.post(f"/agendamentos/{ag['id']}/cancelar", headers=t["maria"])
    r = client.post(f"/agendamentos/{ag['id']}/remarcacoes", headers=t["recepcao"],
                    json={"novo_inicio": f"{DIA}T14:00"})
    assert r.status_code == 409
    assert status_de(client, t, ag["id"]) == "CANCELADO"

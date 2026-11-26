# API da Clínica (caso 03A)

Feita com **Python + FastAPI + SQLite**. O banco é um arquivo (`clinica.db`) criado sozinho na primeira vez que o programa roda, já com dados de teste.

## Como rodar

Precisa do Python 3.10 ou mais novo instalado.

```bash
# 1. entre na pasta que tem o main.py e o requirements.txt
cd clinica-api

# 2. crie e ative o ambiente (só na primeira vez, o venv fica guardado)
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # Linux / Mac

# 3. instale as dependências (rode de novo se atualizar o projeto)
pip install -r requirements.txt

# 4. ligue a API
uvicorn main:app --reload
```

Abra no navegador: **http://127.0.0.1:8000/docs**. Essa página é a documentação automática (OpenAPI/Swagger), e dá para testar cada rota clicando em "Try it out".

> **Atualizando da versão anterior (só HU1)?** Não precisa apagar o `clinica.db`: o programa acrescenta sozinho as tabelas e colunas novas e cria os usuários. As reservas antigas continuam lá. Se quiser começar do zero, apague o `clinica.db` com a API desligada.

## Como entrar (login) no Swagger

Quase todas as rotas exigem login. Na página `/docs`:

1. Clique no botão **Authorize** (cadeado, canto superior direito).
2. Digite o usuário e a senha **1234**, e clique em **Authorize** e depois em **Close**.
3. Pronto: os cadeados ficam fechados e as rotas usam esse usuário.

Para trocar de usuário, abra o **Authorize** de novo, clique em **Logout** e entre com outro.

| Usuário | Perfil | O que pode fazer |
|---|---|---|
| maria, joao, paula | paciente | ver horários, reservar para si, ver e cancelar as próprias consultas |
| ana, bruno, carla | profissional | bloquear a própria agenda, ver a própria agenda |
| recepcao | recepção | reservar para um paciente, encaixar, remarcar, listar afetados, ver agendas |
| gestor | gestor | ver a ocupação (só números, sem dados de pacientes) |

Senha de todos: `1234`.

## Dados de teste (já no banco)

| Tipo | Dados |
|---|---|
| Profissionais | 1 Dra. Ana (Clínica Geral), 2 Dr. Bruno (Fisioterapia), 3 Dra. Carla (Nutrição) |
| Salas | 1 Sala 1, 2 Sala 2 |
| Pacientes | 1 Maria, 2 João, 3 Paula |
| Durações | inicial 40 min, retorno 20 min |

## Rodar os testes

```bash
pytest -v
```

Cada teste tem o nome do caso de teste do formulário (`test_ct20_...` = CT20). São 34 testes, e todos passam.

## Rotas (todas feitas)

| Rota | Quem usa | História | Regras |
|---|---|---|---|
| `POST /login` | todos | — | RN10 |
| `GET /horarios-disponiveis` | paciente, recepção | HU1 | RN4, RN1 |
| `POST /agendamentos` | paciente, recepção | HU1 | RN1, RN2, RN3 |
| `GET /agendamentos` | paciente (os dele), recepção (todos) | HU2 | RN10 |
| `GET /agendamentos/{id}` | paciente, profissional, recepção | HU2 | RN10 |
| `POST /agendamentos/{id}/cancelar` | paciente | HU3 | RN8, RN9 |
| `POST /profissionais/{id}/bloqueios` | profissional | HU4 | RN5, RN9 |
| `GET /bloqueios/{id}/agendamentos-afetados` | recepção | HU5 | RN5 |
| `POST /encaixes` | recepção | HU5 | RN6 |
| `POST /agendamentos/{id}/remarcacoes` | recepção | HU5 | RN7, RN9 |
| `GET /agendamentos/{id}/remarcacoes` | recepção | HU5 | RN7 |
| `GET /profissionais/{id}/agenda?data=AAAA-MM-DD` | recepção, profissional | HU6 | RN10 |
| `GET /gestao/ocupacao?de=...&ate=...` | gestor | HU7 | RN10 |

## Roteiro rápido para testar o caso inteiro no Swagger

Use sempre o horário `2026-12-01T09:00`.

1. **Reserva concorrente.** Entre como `maria` e faça `POST /agendamentos` com `{"profissional_id": 1, "tipo": "INICIAL", "inicio": "2026-12-01T09:00", "sala_id": 1}` e veja o **201**. Troque para `joao`, repita o mesmo pedido e veja o **409**.
2. **Bloqueio posterior.** Como `ana`, `POST /profissionais/1/bloqueios` com `{"inicio": "2026-12-01T08:00", "fim": "2026-12-01T12:00", "motivo": "Compromisso"}`. A resposta lista o agendamento afetado.
3. **Localizar afetados.** Como `recepcao`, `GET /bloqueios/1/agendamentos-afetados` mostra a Maria com o telefone.
4. **Remarcar com histórico.** Ainda como `recepcao`, `POST /agendamentos/1/remarcacoes` com `{"novo_inicio": "2026-12-01T14:00", "motivo": "Bloqueio da manhã"}`. Depois `GET /agendamentos/1/remarcacoes` mostra o horário antigo e o novo.
5. **Acesso indevido.** Como `joao`, `GET /agendamentos/1` devolve **404** (a consulta é da Maria).

## Como o código está organizado (`main.py`)

1. **Configurações e banco de dados**: cria as tabelas e os dados de teste.
2. **Login e permissões**: `usuario_logado` descobre quem é, e `exigir("perfil")` barra quem não pode (403).
3. **Funções das regras**: `tem_conflito`, `reservar_recursos` (RN1), `preparar_intervalo` (RN2), `mudar_status` (RN9).
4. **Rotas**: cada `@app.get` / `@app.post` é uma rota do formulário.

A regra RN3 (duas reservas ao mesmo tempo) é garantida pela transação `BEGIN IMMEDIATE`: o banco coloca as reservas em fila. Quando qualquer regra falha no meio (um 409, por exemplo), o `ROLLBACK` desfaz tudo e o banco fica como estava (é o que garante a RN7).

## Premissas e limites (confirmar com o professor)

- Expediente das 08:00 às 18:00, com horários de 20 em 20 minutos.
- As durações são iguais para todas as especialidades (o caso só cita 40 e 20 minutos).
- Se o pedido não informar a sala, a API escolhe uma sala livre.
- Só o paciente cancela, e só a recepção remarca e encaixa.
- Só o próprio profissional bloqueia a própria agenda.
- Consultas com status AFETADO não ocupam a sala, e voltam a CONFIRMADO ao remarcar.
- Segurança simplificada para o trabalho: senha com SHA-256 e token guardado no banco, sem expiração. Em um sistema real usaríamos bcrypt/argon2 e JWT com prazo.
- O SQLite aguenta bem esse trabalho. Em produção usaríamos PostgreSQL ou similar, mantendo a mesma ideia de transação.

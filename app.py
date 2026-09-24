import json
import os
import secrets
import time
import unicodedata
from pathlib import Path
from random import randint, shuffle

from flask import Flask, request, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "locais.json", encoding="utf-8") as f:
    LOCAIS = json.load(f)  # [{"nome": ..., "papeis": [...]}, ...]

with open(BASE_DIR / "fragmentos.json", encoding="utf-8") as f:
    FRAGMENTOS = json.load(f)  # ["TRA", "CA", ...]

with open(BASE_DIR / "definicoes.json", encoding="utf-8") as f:
    DEFINICOES = json.load(f)  # [{"palavra": ..., "definicao": ...}, ...]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

socketio = SocketIO(app, cors_allowed_origins="*")

salas = {}

STATUS_INATIVOS = ("offline", "saiu")
MIN_JOGADORES = {"ESPIAO": 3, "BOMBA": 2, "DEFINICAO": 3}
TEMPO_ESPIAO_PADRAO = 480
TEMPO_VOTACAO_ESPIAO = 45
TEMPO_TURNO_BOMBA = 12
TEMPO_ESCREVER_DEF = 75
TEMPO_VOTAR_DEF = 45


@app.route("/")
def index():
    return send_file(BASE_DIR / "index.html")


# ---------------------------------------------------------------------------
# Helpers compartilhados (sala, jogadores, identidade)
# ---------------------------------------------------------------------------

def identificador_jogador(jogador):
    return jogador.get("usuario_id") or jogador.get("sid")


def indices_ativos(sala):
    return [
        idx
        for idx, jogador in enumerate(sala["jogadores"])
        if jogador.get("status", "ativo") not in STATUS_INATIVOS
    ]


def jogador_por_id(sala, identificador):
    for jogador in sala["jogadores"]:
        if identificador_jogador(jogador) == identificador:
            return jogador
    return None


def nome_disponivel(sala, nome, ignorar_usuario_id=None):
    nome_lower = nome.strip().lower()
    for jogador in sala["jogadores"]:
        if ignorar_usuario_id and jogador.get("usuario_id") == ignorar_usuario_id:
            continue
        if jogador.get("nome", "").strip().lower() == nome_lower:
            return False
    return True


def gerar_nome_unico(sala, nome, ignorar_usuario_id=None):
    if nome_disponivel(sala, nome, ignorar_usuario_id):
        return nome
    contador = 2
    while True:
        candidato = f"{nome} ({contador})"
        if nome_disponivel(sala, candidato, ignorar_usuario_id):
            return candidato
        contador += 1


def normalizar_texto(texto):
    """Minúsculas e sem acento, pra comparações mais tolerantes."""
    texto = texto.strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Sanitização / estado público e privado
# ---------------------------------------------------------------------------

def estado_publico(sala):
    modo = sala.get("modo")
    estado = sala.get("estado") or {}
    fase = sala.get("fase_atual")

    if modo == "ESPIAO":
        pub = {
            "local": None,
            "espiao_nome": None,
            "resultado": None,
            "adivinhacao": estado.get("adivinhacao_espiao"),
            "total_votos": len(estado.get("votos", {})),
            "votos_detalhados": None,
        }
        if fase == "ESPIAO_RESULTADO":
            pub["local"] = estado.get("local")
            espiao_jogador = jogador_por_id(sala, estado.get("espiao_id"))
            pub["espiao_nome"] = espiao_jogador["nome"] if espiao_jogador else None
            pub["resultado"] = estado.get("resultado")
            votos_detalhados = []
            for votante_id, alvo_id in estado.get("votos", {}).items():
                votante = jogador_por_id(sala, votante_id)
                alvo = jogador_por_id(sala, alvo_id) if alvo_id else None
                votos_detalhados.append({
                    "votante_nome": votante["nome"] if votante else "?",
                    "alvo_nome": alvo["nome"] if alvo else "Abstenção",
                })
            pub["votos_detalhados"] = votos_detalhados
        return pub

    if modo == "BOMBA":
        return {
            "fragmento_atual": estado.get("fragmento_atual"),
            "ordem": estado.get("ordem", []),
            "indice_turno": estado.get("indice_turno", 0),
            "vidas": estado.get("vidas", {}),
            "eliminados": estado.get("eliminados", []),
            "palavras_usadas_rodada": estado.get("palavras_usadas_lista", []),
            "vencedor_id": estado.get("vencedor_id") if fase == "BOMBA_RESULTADO" else None,
        }

    if modo == "DEFINICAO":
        pub = {
            "palavra": estado.get("palavra"),
            "total_submissoes": len(estado.get("submissoes", {})),
            "total_votos": len(estado.get("votos", {})),
            "opcoes": None,
            "definicao_real": None,
            "opcoes_reveladas": None,
        }
        if fase in ("DEF_VOTAR", "DEF_RESULTADO"):
            pub["opcoes"] = [{"texto": op["texto"]} for op in estado.get("opcoes", [])]
        if fase == "DEF_RESULTADO":
            pub["definicao_real"] = estado.get("definicao_real")
            votos = estado.get("votos", {})
            contagem = {}
            for alvo_indice in votos.values():
                contagem[alvo_indice] = contagem.get(alvo_indice, 0) + 1
            reveladas = []
            for indice, op in enumerate(estado.get("opcoes", [])):
                autor_id = op.get("id")
                if autor_id == "real":
                    autor_nome = None
                else:
                    autor = jogador_por_id(sala, autor_id)
                    autor_nome = autor["nome"] if autor else "?"
                reveladas.append({
                    "texto": op["texto"],
                    "eh_real": autor_id == "real",
                    "autor_nome": autor_nome,
                    "votos": contagem.get(indice, 0),
                })
            pub["opcoes_reveladas"] = reveladas
        return pub

    return {}


def sala_publica(sala):
    if not sala:
        return sala

    return {
        "codigo": sala.get("codigo"),
        "fase_atual": sala.get("fase_atual"),
        "modo": sala.get("modo"),
        "tempo_inicio": sala.get("tempo_inicio"),
        "tempo_rodada": sala.get("tempo_rodada"),
        "jogadores": [
            {
                "sid": jogador.get("sid"),
                "usuario_id": jogador.get("usuario_id"),
                "nome": jogador.get("nome"),
                "pontuacao": jogador.get("pontuacao", 0),
                "status": jogador.get("status", "ativo"),
            }
            for jogador in sala.get("jogadores", [])
        ],
        "estado": estado_publico(sala),
    }


def emitir_estado_privado(sala):
    """Manda pra cada jogador (room = seu próprio sid) só o que ele tem
    direito de ver: papel do Espião/local secreto, e sua posição na lista
    de opções da Definição Falsa (pra não votar na própria)."""
    modo = sala.get("modo")
    estado = sala.get("estado") or {}
    fase = sala.get("fase_atual")

    for jogador in sala.get("jogadores", []):
        sid = jogador.get("sid")
        if not sid:
            continue

        meu_id = identificador_jogador(jogador)
        payload = {"espiao": None, "definicao": None}

        if modo == "ESPIAO" and fase in ("ESPIAO_RODADA", "ESPIAO_VOTACAO"):
            eh_espiao = estado.get("espiao_id") == meu_id
            if eh_espiao:
                payload["espiao"] = {
                    "sou_espiao": True,
                    "local": None,
                    "papel": None,
                    "locais_possiveis": [l["nome"] for l in LOCAIS],
                }
            else:
                payload["espiao"] = {
                    "sou_espiao": False,
                    "local": estado.get("local"),
                    "papel": (estado.get("papeis") or {}).get(meu_id),
                }

        if modo == "DEFINICAO" and fase == "DEF_VOTAR":
            minha_posicao = None
            for indice, op in enumerate(estado.get("opcoes", [])):
                if op.get("id") == meu_id:
                    minha_posicao = indice
                    break
            payload["definicao"] = {"minha_posicao": minha_posicao}

        emit("estado_privado", payload, room=sid)


def emitir_atualizacao(codigo_sala):
    sala = salas.get(codigo_sala)
    if not sala:
        return
    emit("sala_atualizada", sala_publica(sala), room=codigo_sala)
    emitir_estado_privado(sala)


def garantir_trackers(sala):
    sala.setdefault("locais_usados", set())
    sala.setdefault("definicoes_usadas", set())
    sala.setdefault("fragmentos_recentes", [])


# ---------------------------------------------------------------------------
# ESPIÃO ENTRE NÓS
# ---------------------------------------------------------------------------

def escolher_local(sala):
    garantir_trackers(sala)
    disponiveis = [l for l in LOCAIS if l["nome"] not in sala["locais_usados"]]
    if not disponiveis:
        sala["locais_usados"] = set()
        disponiveis = LOCAIS[:]
    local = disponiveis[randint(0, len(disponiveis) - 1)]
    sala["locais_usados"].add(local["nome"])
    return local


def iniciar_rodada_espiao(codigo_sala, tempo_rodada=TEMPO_ESPIAO_PADRAO):
    sala = salas.get(codigo_sala)
    if not sala:
        return None

    ativos = indices_ativos(sala)
    if len(ativos) < MIN_JOGADORES["ESPIAO"]:
        return None

    local = escolher_local(sala)
    espiao_idx = ativos[randint(0, len(ativos) - 1)]
    espiao_id = identificador_jogador(sala["jogadores"][espiao_idx])

    demais_ids = [
        identificador_jogador(sala["jogadores"][idx])
        for idx in ativos
        if idx != espiao_idx
    ]

    papeis_disponiveis = local["papeis"][:]
    shuffle(papeis_disponiveis)
    papeis = {}
    for i, identificador in enumerate(demais_ids):
        papeis[identificador] = papeis_disponiveis[i % len(papeis_disponiveis)]

    sala["modo"] = "ESPIAO"
    sala["fase_atual"] = "ESPIAO_RODADA"
    sala["tempo_inicio"] = int(time.time())
    sala["tempo_rodada"] = tempo_rodada
    sala["estado"] = {
        "tipo": "ESPIAO",
        "local": local["nome"],
        "espiao_id": espiao_id,
        "papeis": papeis,
        "votos": {},
        "resultado": None,
        "adivinhacao_espiao": None,
    }

    emitir_atualizacao(codigo_sala)
    return sala


def resolver_votacao_espiao(sala):
    estado = sala["estado"]
    votos = estado.get("votos", {})
    contagem = {}
    for alvo_id in votos.values():
        if not alvo_id:
            continue
        contagem[alvo_id] = contagem.get(alvo_id, 0) + 1

    mais_votado_id = None
    maior_qtd = 0
    empatado = False
    for alvo_id, qtd in contagem.items():
        if qtd > maior_qtd:
            maior_qtd = qtd
            mais_votado_id = alvo_id
            empatado = False
        elif qtd == maior_qtd and maior_qtd > 0:
            empatado = True

    espiao_id = estado.get("espiao_id")

    if not empatado and mais_votado_id == espiao_id:
        resultado = "nao_espioes_venceram"
        for jogador in sala["jogadores"]:
            if identificador_jogador(jogador) != espiao_id:
                jogador["pontuacao"] = jogador.get("pontuacao", 0) + 2
    else:
        resultado = "espiao_venceu"
        espiao_jogador = jogador_por_id(sala, espiao_id)
        if espiao_jogador:
            espiao_jogador["pontuacao"] = espiao_jogador.get("pontuacao", 0) + 4

    estado["resultado"] = resultado
    sala["fase_atual"] = "ESPIAO_RESULTADO"
    sala["tempo_inicio"] = None


@socketio.on("espiao_chamar_votacao")
def espiao_chamar_votacao(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "ESPIAO_RODADA":
        return
    sala["fase_atual"] = "ESPIAO_VOTACAO"
    sala["tempo_inicio"] = int(time.time())
    sala["tempo_rodada"] = TEMPO_VOTACAO_ESPIAO
    emitir_atualizacao(codigo)


@socketio.on("espiao_votar")
def espiao_votar(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    alvo_usuario_id = (data or {}).get("alvo_usuario_id")
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "ESPIAO_VOTACAO":
        return

    jogador = next((j for j in sala["jogadores"] if j.get("sid") == request.sid), None)
    if not jogador:
        return

    meu_id = identificador_jogador(jogador)
    sala["estado"]["votos"][meu_id] = alvo_usuario_id or None

    ativos = indices_ativos(sala)
    if len(sala["estado"]["votos"]) >= len(ativos):
        resolver_votacao_espiao(sala)

    emitir_atualizacao(codigo)


@socketio.on("espiao_adivinhar_local")
def espiao_adivinhar_local(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    palpite = ((data or {}).get("local") or "").strip()
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") not in ("ESPIAO_RODADA", "ESPIAO_VOTACAO"):
        return

    estado = sala["estado"]
    if estado.get("espiao_id") != identificador_jogador(
        next((j for j in sala["jogadores"] if j.get("sid") == request.sid), {})
    ):
        emit("erro", {"mensagem": "Só o Espião pode tentar adivinhar o local."})
        return

    estado["adivinhacao_espiao"] = palpite
    acertou = normalizar_texto(palpite) == normalizar_texto(estado.get("local", ""))

    espiao_jogador = jogador_por_id(sala, estado.get("espiao_id"))

    if acertou:
        estado["resultado"] = "espiao_venceu_adivinhando"
        if espiao_jogador:
            espiao_jogador["pontuacao"] = espiao_jogador.get("pontuacao", 0) + 5
    else:
        estado["resultado"] = "espiao_perdeu_adivinhando"
        for jogador in sala["jogadores"]:
            if identificador_jogador(jogador) != estado.get("espiao_id"):
                jogador["pontuacao"] = jogador.get("pontuacao", 0) + 2

    sala["fase_atual"] = "ESPIAO_RESULTADO"
    sala["tempo_inicio"] = None
    emitir_atualizacao(codigo)


# ---------------------------------------------------------------------------
# BOMBA DE PALAVRAS
# ---------------------------------------------------------------------------

def escolher_fragmento(sala):
    garantir_trackers(sala)
    recentes = sala["fragmentos_recentes"]
    disponiveis = [f for f in FRAGMENTOS if f not in recentes]
    if not disponiveis:
        disponiveis = FRAGMENTOS[:]
    fragmento = disponiveis[randint(0, len(disponiveis) - 1)]
    recentes.append(fragmento)
    if len(recentes) > 15:
        recentes.pop(0)
    return fragmento


def iniciar_rodada_bomba(codigo_sala):
    sala = salas.get(codigo_sala)
    if not sala:
        return None

    ativos = indices_ativos(sala)
    if len(ativos) < MIN_JOGADORES["BOMBA"]:
        return None

    ids = [identificador_jogador(sala["jogadores"][idx]) for idx in ativos]
    shuffle(ids)

    sala["modo"] = "BOMBA"
    sala["fase_atual"] = "BOMBA_RODADA"
    sala["tempo_inicio"] = int(time.time())
    sala["tempo_rodada"] = TEMPO_TURNO_BOMBA
    sala["estado"] = {
        "tipo": "BOMBA",
        "ordem": ids,
        "indice_turno": 0,
        "vidas": {identificador: 3 for identificador in ids},
        "eliminados": [],
        "palavras_usadas": set(),
        "palavras_usadas_lista": [],
        "fragmento_atual": escolher_fragmento(sala),
        "vencedor_id": None,
    }

    emitir_atualizacao(codigo_sala)
    return sala


def jogador_da_vez_bomba(sala):
    estado = sala["estado"]
    ordem = estado["ordem"]
    if not ordem:
        return None
    indice = estado["indice_turno"] % len(ordem)
    return ordem[indice]


def avancar_turno_bomba(sala, codigo_sala):
    estado = sala["estado"]
    ordem = estado["ordem"]
    ativos_na_rodada = [i for i in ordem if i not in estado["eliminados"]]

    if len(ativos_na_rodada) <= 1:
        vencedor_id = ativos_na_rodada[0] if ativos_na_rodada else None
        estado["vencedor_id"] = vencedor_id
        if vencedor_id:
            vencedor = jogador_por_id(sala, vencedor_id)
            if vencedor:
                vencedor["pontuacao"] = vencedor.get("pontuacao", 0) + 5
        sala["fase_atual"] = "BOMBA_RESULTADO"
        sala["tempo_inicio"] = None
        return

    proximo_indice = estado["indice_turno"]
    tamanho = len(ordem)
    for _ in range(tamanho):
        proximo_indice = (proximo_indice + 1) % tamanho
        if ordem[proximo_indice] not in estado["eliminados"]:
            break
    estado["indice_turno"] = proximo_indice
    estado["fragmento_atual"] = escolher_fragmento(sala)
    sala["tempo_inicio"] = int(time.time())


@socketio.on("bomba_responder")
def bomba_responder(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    palavra = ((data or {}).get("palavra") or "").strip()
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "BOMBA_RODADA":
        return

    jogador = next((j for j in sala["jogadores"] if j.get("sid") == request.sid), None)
    if not jogador:
        return

    meu_id = identificador_jogador(jogador)
    estado = sala["estado"]
    if jogador_da_vez_bomba(sala) != meu_id:
        emit("erro", {"mensagem": "Não é a sua vez."})
        return

    palavra_norm = normalizar_texto(palavra)
    fragmento_norm = normalizar_texto(estado.get("fragmento_atual", ""))

    if len(palavra_norm) < 3:
        emit("erro", {"mensagem": "Palavra muito curta."})
        return
    if fragmento_norm not in palavra_norm:
        emit("erro", {"mensagem": f'A palavra precisa conter "{estado.get("fragmento_atual")}".'})
        return
    if palavra_norm in estado["palavras_usadas"]:
        emit("erro", {"mensagem": "Essa palavra já foi usada nessa rodada."})
        return

    estado["palavras_usadas"].add(palavra_norm)
    estado["palavras_usadas_lista"].append(palavra)
    if len(estado["palavras_usadas_lista"]) > 20:
        estado["palavras_usadas_lista"].pop(0)

    jogador["pontuacao"] = jogador.get("pontuacao", 0) + 1

    avancar_turno_bomba(sala, codigo)
    emitir_atualizacao(codigo)


@socketio.on("bomba_tempo_esgotado")
def bomba_tempo_esgotado(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "BOMBA_RODADA":
        return

    estado = sala["estado"]
    # Proteção contra disparo duplicado de múltiplos clientes: só processa
    # se o tempo realmente já esgotou pro turno atual.
    tempo_inicio = sala.get("tempo_inicio")
    tempo_decorrido = time.time() - tempo_inicio if tempo_inicio is not None else TEMPO_TURNO_BOMBA
    if tempo_decorrido < TEMPO_TURNO_BOMBA - 1:
        return

    meu_id = jogador_da_vez_bomba(sala)
    if meu_id and meu_id not in estado["vidas"]:
        estado["vidas"][meu_id] = 3

    if meu_id:
        estado["vidas"][meu_id] = max(0, estado["vidas"].get(meu_id, 3) - 1)
        if estado["vidas"][meu_id] <= 0 and meu_id not in estado["eliminados"]:
            estado["eliminados"].append(meu_id)

    avancar_turno_bomba(sala, codigo)
    emitir_atualizacao(codigo)


def _pular_turno_por_desconexao(sala, codigo_sala, identificador):
    """Chamado quando quem está na vez da Bomba cai da sala: trata como se
    tivesse zerado o tempo, sem esperar o timeout normal."""
    if sala.get("modo") != "BOMBA" or sala.get("fase_atual") != "BOMBA_RODADA":
        return
    estado = sala["estado"]
    if jogador_da_vez_bomba(sala) != identificador:
        return
    estado["vidas"][identificador] = max(0, estado["vidas"].get(identificador, 3) - 1)
    if estado["vidas"][identificador] <= 0 and identificador not in estado["eliminados"]:
        estado["eliminados"].append(identificador)
    avancar_turno_bomba(sala, codigo_sala)


# ---------------------------------------------------------------------------
# DEFINIÇÃO FALSA
# ---------------------------------------------------------------------------

def escolher_definicao(sala):
    garantir_trackers(sala)
    disponiveis = [d for d in DEFINICOES if d["palavra"] not in sala["definicoes_usadas"]]
    if not disponiveis:
        sala["definicoes_usadas"] = set()
        disponiveis = DEFINICOES[:]
    entrada = disponiveis[randint(0, len(disponiveis) - 1)]
    sala["definicoes_usadas"].add(entrada["palavra"])
    return entrada


def iniciar_rodada_definicao(codigo_sala):
    sala = salas.get(codigo_sala)
    if not sala:
        return None

    ativos = indices_ativos(sala)
    if len(ativos) < MIN_JOGADORES["DEFINICAO"]:
        return None

    entrada = escolher_definicao(sala)

    sala["modo"] = "DEFINICAO"
    sala["fase_atual"] = "DEF_ESCREVER"
    sala["tempo_inicio"] = int(time.time())
    sala["tempo_rodada"] = TEMPO_ESCREVER_DEF
    sala["estado"] = {
        "tipo": "DEF",
        "palavra": entrada["palavra"],
        "definicao_real": entrada["definicao"],
        "submissoes": {},
        "opcoes": [],
        "votos": {},
    }

    emitir_atualizacao(codigo_sala)
    return sala


def avancar_para_votacao_def(sala):
    estado = sala["estado"]
    opcoes = [{"id": "real", "texto": estado["definicao_real"]}]
    for identificador, texto in estado["submissoes"].items():
        opcoes.append({"id": identificador, "texto": texto})
    shuffle(opcoes)
    estado["opcoes"] = opcoes
    estado["votos"] = {}
    sala["fase_atual"] = "DEF_VOTAR"
    sala["tempo_inicio"] = int(time.time())
    sala["tempo_rodada"] = TEMPO_VOTAR_DEF


def resolver_votacao_def(sala):
    estado = sala["estado"]
    opcoes = estado["opcoes"]
    votos = estado["votos"]

    for votante_id, indice_escolhido in votos.items():
        if indice_escolhido is None or indice_escolhido >= len(opcoes):
            continue
        opcao = opcoes[indice_escolhido]
        if opcao["id"] == "real":
            votante = jogador_por_id(sala, votante_id)
            if votante:
                votante["pontuacao"] = votante.get("pontuacao", 0) + 2
        else:
            autor = jogador_por_id(sala, opcao["id"])
            if autor:
                autor["pontuacao"] = autor.get("pontuacao", 0) + 1

    sala["fase_atual"] = "DEF_RESULTADO"
    sala["tempo_inicio"] = None


@socketio.on("def_submeter")
def def_submeter(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    texto = ((data or {}).get("texto") or "").strip()[:200]
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "DEF_ESCREVER" or not texto:
        return

    jogador = next((j for j in sala["jogadores"] if j.get("sid") == request.sid), None)
    if not jogador:
        return

    meu_id = identificador_jogador(jogador)
    sala["estado"]["submissoes"][meu_id] = texto

    ativos = indices_ativos(sala)
    if len(sala["estado"]["submissoes"]) >= len(ativos):
        avancar_para_votacao_def(sala)

    emitir_atualizacao(codigo)


@socketio.on("def_tempo_escrever")
def def_tempo_escrever(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "DEF_ESCREVER":
        return
    if len(sala["estado"]["submissoes"]) == 0:
        # Ninguém escreveu nada: só a definição real estaria nas opções,
        # o que entregaria a resposta. Dá mais um tempinho em vez de travar.
        sala["tempo_inicio"] = int(time.time())
        emitir_atualizacao(codigo)
        return
    avancar_para_votacao_def(sala)
    emitir_atualizacao(codigo)


@socketio.on("def_votar")
def def_votar(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    indice = (data or {}).get("indice")
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "DEF_VOTAR":
        return

    jogador = next((j for j in sala["jogadores"] if j.get("sid") == request.sid), None)
    if not jogador or not isinstance(indice, int):
        return

    meu_id = identificador_jogador(jogador)
    opcoes = sala["estado"]["opcoes"]
    if indice < 0 or indice >= len(opcoes):
        return
    if opcoes[indice]["id"] == meu_id:
        emit("erro", {"mensagem": "Você não pode votar na sua própria definição."})
        return

    sala["estado"]["votos"][meu_id] = indice

    ativos = indices_ativos(sala)
    if len(sala["estado"]["votos"]) >= len(ativos):
        resolver_votacao_def(sala)

    emitir_atualizacao(codigo)


@socketio.on("def_tempo_votar")
def def_tempo_votar(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala or sala.get("fase_atual") != "DEF_VOTAR":
        return
    resolver_votacao_def(sala)
    emitir_atualizacao(codigo)


# ---------------------------------------------------------------------------
# Eventos compartilhados: sala, modo, lobby
# ---------------------------------------------------------------------------

@socketio.on("join_game")
def join_game(data):
    nome = (data or {}).get("nome", "").strip()[:20]
    codigo = (data or {}).get("codigo", "").strip().upper()[:4]
    usuario_id = (data or {}).get("usuario_id", "").strip()

    if not nome or not codigo:
        emit("erro", {"mensagem": "Nome e código da sala são obrigatórios."})
        return

    sala = salas.get(codigo)
    if sala is None:
        sala = {
            "codigo": codigo,
            "jogadores": [],
            "modo": None,
            "fase_atual": "LOBBY",
            "estado": {},
        }
        garantir_trackers(sala)
        salas[codigo] = sala

    sid = request.sid
    jogador = next(
        (j for j in sala["jogadores"] if usuario_id and j.get("usuario_id") == usuario_id),
        None,
    )

    if jogador is None:
        nome_final = gerar_nome_unico(sala, nome)
        sala["jogadores"].append({
            "sid": sid,
            "usuario_id": usuario_id,
            "nome": nome_final,
            "pontuacao": 0,
            "status": "ativo",
        })
    else:
        jogador["sid"] = sid
        jogador["nome"] = gerar_nome_unico(sala, nome, ignorar_usuario_id=usuario_id)
        jogador["status"] = "ativo"

    join_room(codigo)
    emitir_atualizacao(codigo)


@socketio.on("escolher_modo")
def escolher_modo(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    modo = (data or {}).get("modo")
    sala = salas.get(codigo)
    if not sala:
        emit("erro", {"mensagem": "Sala não encontrada."})
        return

    if not sala["jogadores"] or sala["jogadores"][0]["sid"] != request.sid:
        emit("erro", {"mensagem": "Apenas o host pode escolher o jogo."})
        return

    if modo not in ("ESPIAO", "BOMBA", "DEFINICAO"):
        emit("erro", {"mensagem": "Jogo inválido."})
        return

    sala["modo"] = modo
    sala["fase_atual"] = "LOBBY"
    emitir_atualizacao(codigo)


@socketio.on("iniciar_modo")
def iniciar_modo(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala:
        emit("erro", {"mensagem": "Sala não encontrada."})
        return

    if not sala["jogadores"] or sala["jogadores"][0]["sid"] != request.sid:
        emit("erro", {"mensagem": "Apenas o host pode iniciar."})
        return

    modo = sala.get("modo")
    if modo not in ("ESPIAO", "BOMBA", "DEFINICAO"):
        emit("erro", {"mensagem": "Escolha um jogo antes de iniciar."})
        return

    minimo = MIN_JOGADORES[modo]
    if len(indices_ativos(sala)) < minimo:
        emit("erro", {"mensagem": f"É preciso pelo menos {minimo} jogadores para esse jogo."})
        return

    if modo == "ESPIAO":
        resultado = iniciar_rodada_espiao(codigo)
    elif modo == "BOMBA":
        resultado = iniciar_rodada_bomba(codigo)
    else:
        resultado = iniciar_rodada_definicao(codigo)

    if resultado is None:
        emit("erro", {"mensagem": "Não foi possível iniciar o jogo."})


@socketio.on("nova_rodada")
def nova_rodada(data):
    """Repete o mesmo jogo (novo local/fragmento/palavra), sem passar pelo
    lobby de novo."""
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala:
        emit("erro", {"mensagem": "Sala não encontrada."})
        return

    if not sala["jogadores"] or sala["jogadores"][0]["sid"] != request.sid:
        emit("erro", {"mensagem": "Apenas o host pode iniciar a próxima rodada."})
        return

    modo = sala.get("modo")
    fases_resultado = {
        "ESPIAO": "ESPIAO_RESULTADO",
        "BOMBA": "BOMBA_RESULTADO",
        "DEFINICAO": "DEF_RESULTADO",
    }
    if modo not in fases_resultado or sala.get("fase_atual") != fases_resultado[modo]:
        emit("erro", {"mensagem": "Ainda não terminou a rodada atual."})
        return

    iniciar_modo(data)


@socketio.on("voltar_lobby")
def voltar_lobby(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala:
        emit("erro", {"mensagem": "Sala não encontrada."})
        return

    if not sala["jogadores"] or sala["jogadores"][0]["sid"] != request.sid:
        emit("erro", {"mensagem": "Apenas o host pode voltar ao lobby."})
        return

    sala["modo"] = None
    sala["fase_atual"] = "LOBBY"
    sala["tempo_inicio"] = None
    sala["estado"] = {}
    emitir_atualizacao(codigo)


@socketio.on("sair_sala")
def sair_sala(data):
    codigo = (data or {}).get("codigo", "").strip().upper()
    sala = salas.get(codigo)
    if not sala:
        return

    sid = request.sid
    jogador = next((j for j in sala["jogadores"] if j.get("sid") == sid), None)
    if jogador is None:
        leave_room(codigo)
        return

    if sala.get("fase_atual") == "LOBBY":
        sala["jogadores"] = [j for j in sala["jogadores"] if j.get("sid") != sid]
    else:
        jogador["status"] = "saiu"
        _pular_turno_por_desconexao(sala, codigo, identificador_jogador(jogador))

    leave_room(codigo)
    emitir_atualizacao(codigo)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    for codigo, sala in list(salas.items()):
        jogador = next((j for j in sala["jogadores"] if j.get("sid") == sid), None)
        if jogador is None:
            continue
        jogador["status"] = "offline"
        _pular_turno_por_desconexao(sala, codigo, identificador_jogador(jogador))
        emitir_atualizacao(codigo)


if __name__ == "__main__":
    socketio.run(app, debug=DEBUG, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

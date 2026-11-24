"""Painel do agente de WhatsApp.

Lê as conversas guardadas na Evolution API, permite pausar o bot (geral ou por
contato), responder manualmente e editar o prompt do agente. O n8n consulta
GET /api/agente/config antes de cada resposta.
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, render_template, request

EVOLUTION_URL = os.environ.get("EVOLUTION_URL", "http://evolution-api:8080").rstrip("/")
EVOLUTION_KEY = os.environ.get("EVOLUTION_API_KEY", "")
INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "agente")
DB_PATH = os.environ.get("PAINEL_DB", "/data/painel.db")
TZ_BR = timezone(timedelta(hours=-3))
DEMO = os.environ.get("PAINEL_DEMO") == "1"  # dados fictícios, sem Evolution API (prints/apresentação)

PROMPT_PADRAO = """Você é o assistente virtual do Rafael Yudi no WhatsApp. Quem fala com você normalmente é recrutador(a) ou alguém interessado no trabalho dele. O nome de quem está falando no WhatsApp é: {nome}.

O QUE VOCÊ SABE SOBRE O RAFAEL (use só isto, não invente nada):
- Trabalha com implantação e integração de Bitrix24: estruturação de CRM, funis, SPAs (processos inteligentes), Canal Aberto e Contact Center.
- Automação com n8n e Make: webhooks, deduplicação de contatos, enriquecimento de empresa por CNPJ (BrasilAPI), rodízio de responsável, IA (Gemini) em fluxos.
- Python: scripts de importação e configuração de CRM via REST API, dashboard Flask de atendimento (AtendLog), CLI para gerenciar n8n (n8n-cli), monitor de entrega de mensagens WhatsApp ↔ Bitrix24.
- Projetos públicos no GitHub: github.com/RafaelYudi07 (atendlog-bitrix24, bitrix24-toolkit, n8n-cli).

REGRAS:
- Responda em português, curto e natural, como mensagem de WhatsApp (no máximo 3 parágrafos curtos, sem markdown pesado).
- Se perguntarem algo que não está acima (salário, disponibilidade exata, anos de experiência, formação), diga que vai repassar ao Rafael e que ele responde pessoalmente.
- Se a pessoa falar de uma vaga, peça educadamente: nome da empresa, título da vaga e o melhor contato.
- Deixe claro, se perguntarem, que você é um assistente de IA."""

app = Flask(__name__)


# ---------- banco local (config, pausas, mensagens enviadas pelo painel) ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with closing(db()) as c, c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
            CREATE TABLE IF NOT EXISTS pausas (numero TEXT PRIMARY KEY, desde INTEGER);
            CREATE TABLE IF NOT EXISTS enviadas_painel (msg_id TEXT PRIMARY KEY, ts INTEGER);
        """)
        c.execute("INSERT OR IGNORE INTO config VALUES ('bot_ativo', '1')")
        c.execute("INSERT OR IGNORE INTO config VALUES ('prompt', ?)", (PROMPT_PADRAO,))


def get_config():
    with closing(db()) as c:
        rows = dict(c.execute("SELECT chave, valor FROM config").fetchall())
    return {"bot_ativo": rows.get("bot_ativo") == "1", "prompt": rows.get("prompt", PROMPT_PADRAO)}


def pausados():
    with closing(db()) as c:
        return {r["numero"] for r in c.execute("SELECT numero FROM pausas")}


def set_pausa(numero, pausado):
    with closing(db()) as c, c:
        if pausado:
            c.execute("INSERT OR REPLACE INTO pausas VALUES (?, ?)", (numero, int(time.time())))
        else:
            c.execute("DELETE FROM pausas WHERE numero = ?", (numero,))


# ---------- Evolution API ----------

def evolution(method, path, body=None):
    req = urllib.request.Request(
        f"{EVOLUTION_URL}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"apikey": EVOLUTION_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def mapa_lid(registros):
    """O WhatsApp identifica alguns contatos por um ID de privacidade (@lid).
    Só algumas mensagens trazem o número real junto (remoteJidAlt): aprendemos o par com elas."""
    mapa = {}
    for r in registros:
        k = r.get("key") or {}
        if (k.get("remoteJid") or "").endswith("@lid") and (k.get("remoteJidAlt") or "").endswith("@s.whatsapp.net"):
            mapa[k["remoteJid"]] = k["remoteJidAlt"]
    return mapa


def canonico(jid, mapa, alt=None):
    """Devolve (jid, numero). numero = só dígitos para telefone; o jid inteiro se for @lid sem número conhecido."""
    if jid.endswith("@lid"):
        jid = alt or mapa.get(jid) or jid
    numero = jid if jid.endswith("@lid") else jid.split("@")[0]
    return jid, numero


def buscar_registros(limite=1000):
    dados = evolution("POST", f"/chat/findMessages/{INSTANCE}", {"page": 1, "offset": limite})
    return (dados.get("messages") or {}).get("records") or []


def texto_da_mensagem(msg):
    msg = msg or {}
    if msg.get("conversation"):
        return msg["conversation"]
    if (msg.get("extendedTextMessage") or {}).get("text"):
        return msg["extendedTextMessage"]["text"]
    for tipo, rotulo in (("audioMessage", "🎤 áudio"), ("imageMessage", "🖼 imagem"),
                         ("videoMessage", "🎬 vídeo"), ("documentMessage", "📎 documento"),
                         ("stickerMessage", "figurinha")):
        if tipo in msg:
            legenda = msg[tipo].get("caption") if isinstance(msg[tipo], dict) else None
            return f"[{rotulo}]" + (f" {legenda}" if legenda else "")
    return "[mensagem não suportada]"


def mensagens_demo():
    agora = int(time.time())
    conversas = [
        ("5511900000001", "Ana · Tech Recruiter", 25 * 60, [
            ("contato", "Oi! Sou recrutadora da Acme Tecnologia. O Rafael trabalha com automação?"),
            ("bot", "Oi, Ana! Sou o assistente virtual do Rafael. Sim: ele trabalha com automação em n8n e Make, integrando WhatsApp, CRM Bitrix24 e IA."),
            ("contato", "Legal! Temos uma vaga de Analista de Automação. Qual a pretensão salarial dele?"),
            ("bot", "Essa eu prefiro deixar com ele, que responde pessoalmente. Pode me passar o título da vaga e o melhor contato? Já aviso o Rafael."),
            ("contato", "Analista de Automação Pleno, ana@acme.exemplo"),
            ("voce", "Oi Ana, aqui é o Rafael! Obrigado pelo contato, te mando um e-mail ainda hoje."),
        ]),
        ("5521900000002", "Bruno Lima", 3 * 3600, [
            ("contato", "Boa tarde, vi o perfil do Rafael no LinkedIn. Ele tem projetos públicos?"),
            ("bot", "Boa tarde, Bruno! Tem sim, no GitHub: um dashboard Flask de atendimento, um toolkit de Bitrix24 e uma CLI para gerenciar n8n."),
            ("contato", "Perfeito, vou dar uma olhada 👍"),
        ]),
        ("5531900000003", "Carla · RH", 26 * 3600, [
            ("contato", "Você é uma IA?"),
            ("bot", "Sou sim! Sou o assistente de IA do Rafael. Se preferir falar com ele diretamente, é só pedir que eu aviso."),
        ]),
    ]
    saida = []
    for numero, nome, atras, msgs in conversas:
        inicio = agora - atras - len(msgs) * 90
        for i, (autor, texto) in enumerate(msgs):
            saida.append({"id": f"{numero}-{i}", "numero": numero, "jid": f"{numero}@s.whatsapp.net",
                          "nome": nome if autor == "contato" else None, "autor": autor,
                          "texto": texto, "ts": inicio + i * 90})
    saida.sort(key=lambda m: m["ts"])
    return saida


def mensagens_recentes(limite=1000):
    if DEMO:
        return mensagens_demo()
    registros = buscar_registros(limite)
    mapa = mapa_lid(registros)
    with closing(db()) as c:
        do_painel = {r["msg_id"] for r in c.execute("SELECT msg_id FROM enviadas_painel")}
    saida = []
    for r in registros:
        key = r.get("key") or {}
        jid, numero = canonico(key.get("remoteJid") or "", mapa, key.get("remoteJidAlt"))
        if not jid or jid.endswith("@g.us") or jid.endswith("@broadcast"):
            continue
        if key.get("fromMe"):
            autor = "voce" if key.get("id") in do_painel else "bot"
        else:
            autor = "contato"
        saida.append({
            "id": key.get("id"),
            "numero": numero,
            "jid": jid,
            "nome": None if key.get("fromMe") else r.get("pushName"),
            "autor": autor,
            "texto": texto_da_mensagem(r.get("message")),
            "ts": int(r.get("messageTimestamp") or 0),
        })
    saida.sort(key=lambda m: m["ts"])
    return saida


def mascarar(numero):
    if numero.endswith("@lid"):
        return f"ID privado …{numero.split('@')[0][-4:]}"
    d = re.sub(r"\D", "", numero)
    if len(d) >= 12 and d.startswith("55"):
        return f"+55 ({d[2:4]}) {d[4:-4]}-{d[-4:]}"
    return f"+{d}" if d else numero


# ---------- rotas ----------

@app.get("/")
def index():
    return render_template("index.html", instancia=INSTANCE)


@app.get("/api/status")
def status():
    try:
        estado = "open" if DEMO else evolution("GET", f"/instance/connectionState/{INSTANCE}")["instance"]["state"]
    except Exception as e:  # noqa: BLE001
        estado = f"erro: {e.__class__.__name__}"
    return jsonify({"whatsapp": estado, **{k: v for k, v in get_config().items() if k == "bot_ativo"}})


@app.get("/api/conversas")
def conversas():
    try:
        msgs = mensagens_recentes()
    except Exception as e:  # noqa: BLE001
        return jsonify({"erro": f"Evolution API indisponível: {e}"}), 502
    pausas = pausados()
    por_contato = {}
    for m in msgs:
        c = por_contato.setdefault(m["numero"], {"numero": m["numero"], "telefone": mascarar(m["numero"]),
                                                  "nome": None, "total": 0, "ultima": None})
        c["total"] += 1
        c["ultima"] = m
        if m["nome"]:
            c["nome"] = m["nome"]
    lista = []
    for c in por_contato.values():
        u = c.pop("ultima")
        lista.append({**c, "pausado": c["numero"] in pausas, "ultima_ts": u["ts"],
                      "ultima_texto": u["texto"], "ultima_autor": u["autor"]})
    lista.sort(key=lambda c: c["ultima_ts"], reverse=True)

    hoje = datetime.now(TZ_BR).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    stats = {
        "contatos": len(lista),
        "recebidas_hoje": sum(1 for m in msgs if m["ts"] >= hoje and m["autor"] == "contato"),
        "bot_hoje": sum(1 for m in msgs if m["ts"] >= hoje and m["autor"] == "bot"),
        "pausados": sum(1 for c in lista if c["pausado"]),
    }
    return jsonify({"conversas": lista, "stats": stats})


@app.get("/api/conversas/<numero>")
def conversa(numero):
    msgs = [m for m in mensagens_recentes() if m["numero"] == numero]
    return jsonify({"numero": numero, "telefone": mascarar(numero),
                    "pausado": numero in pausados(), "mensagens": msgs})


@app.post("/api/conversas/<numero>/pausa")
def pausar(numero):
    pausado = bool((request.get_json(silent=True) or {}).get("pausado"))
    set_pausa(numero, pausado)
    return jsonify({"numero": numero, "pausado": pausado})


@app.post("/api/conversas/<numero>/enviar")
def enviar(numero):
    texto = ((request.get_json(silent=True) or {}).get("texto") or "").strip()
    if not texto:
        return jsonify({"erro": "texto vazio"}), 400
    if DEMO:
        return jsonify({"erro": "modo demo: nada é enviado"}), 400
    try:
        r = evolution("POST", f"/message/sendText/{INSTANCE}", {"number": numero, "text": texto})
    except Exception as e:  # noqa: BLE001
        return jsonify({"erro": f"falha ao enviar: {e}"}), 502
    msg_id = (r.get("key") or {}).get("id")
    with closing(db()) as c, c:
        if msg_id:
            c.execute("INSERT OR IGNORE INTO enviadas_painel VALUES (?, ?)", (msg_id, int(time.time())))
    set_pausa(numero, True)  # você assumiu a conversa: o bot para de responder esse contato
    return jsonify({"ok": True, "id": msg_id, "pausado": True})


@app.get("/api/config")
def ler_config():
    return jsonify(get_config())


@app.post("/api/config")
def salvar_config():
    body = request.get_json(silent=True) or {}
    with closing(db()) as c, c:
        if "bot_ativo" in body:
            c.execute("UPDATE config SET valor = ? WHERE chave = 'bot_ativo'", ("1" if body["bot_ativo"] else "0",))
        if "prompt" in body:
            prompt = (body["prompt"] or "").strip() or PROMPT_PADRAO
            c.execute("UPDATE config SET valor = ? WHERE chave = 'prompt'", (prompt,))
    return jsonify(get_config())


@app.post("/api/config/prompt-padrao")
def restaurar_prompt():
    with closing(db()) as c, c:
        c.execute("UPDATE config SET valor = ? WHERE chave = 'prompt'", (PROMPT_PADRAO,))
    return jsonify(get_config())


@app.get("/api/agente/config")
def config_para_n8n():
    """Chamado pelo n8n antes de cada resposta."""
    numero = request.args.get("numero", "")
    if numero.endswith("@lid"):
        try:
            numero = canonico(numero, mapa_lid(buscar_registros()))[1]
        except Exception:  # noqa: BLE001 - sem Evolution, usa o ID como veio
            pass
    nome = request.args.get("nome", "").strip() or "não informado"
    cfg = get_config()
    motivo = None
    if not cfg["bot_ativo"]:
        motivo = "bot pausado no painel"
    elif numero in pausados():
        motivo = "conversa assumida manualmente"
    return jsonify({
        "pausado": motivo is not None,
        "motivo": motivo,
        "prompt": cfg["prompt"].replace("{nome}", nome),
    })


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

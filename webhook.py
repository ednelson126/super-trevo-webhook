"""
SUPER TREVO PRO - Webhook / Backend de Pagamento (v2)
========================================================
Melhorias aplicadas sobre a v1:
  - TOKEN e MP_ACCESS_TOKEN em variáveis de ambiente (o token antigo estava em produção
    e em texto puro — REGENERE-O no painel do Mercado Pago antes de usar este arquivo)
  - Validação de assinatura do webhook (x-signature) para garantir que a notificação
    realmente veio do Mercado Pago e não de um POST forjado por terceiros
  - Verificação do valor pago (evita liberar VIP com base em um pagamento de valor errado
    ou reaproveitado de outra cobrança da mesma conta)
  - Idempotência: se o Mercado Pago reenviar a notificação do mesmo pagamento (comum),
    não libera o VIP nem manda mensagem duplicada de novo
  - Este backend passa a ser a FONTE ÚNICA DE VERDADE sobre quem é VIP.
    O bot deve consultar GET /vip_status/<user_id> em vez de checar um banco local —
    isso resolve o problema de bot e webhook rodando como serviços separados no Render
    (cada um com seu próprio filesystem/SQLite, que não se comunicam entre si)

Variáveis de ambiente necessárias:
    TELEGRAM_TOKEN       -> mesmo token usado no bot
    MP_ACCESS_TOKEN      -> Access Token de PRODUÇÃO do Mercado Pago (regenere o antigo!)
    WEBHOOK_URL          -> URL pública deste serviço + /webhook
    MP_WEBHOOK_SECRET    -> chave secreta de assinatura configurada no painel do MP
                            (Suas integrações > Webhooks > Chave secreta). Sem isso,
                            a validação de assinatura fica desativada (não recomendado
                            em produção).
    VIP_PRECO            -> preço esperado do VIP (default 19.90)
    VIP_DIAS             -> duração do VIP em dias (default 30)
"""

import os
import hmac
import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("super_trevo_webhook")

# ==============================
# CONFIG
# ==============================
TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://super-trevo-webhook.onrender.com/webhook")
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET")

VIP_PRECO = float(os.getenv("VIP_PRECO", "19.90"))
VIP_DIAS = int(os.getenv("VIP_DIAS", "30"))

DB_PATH = "super_trevo.db"

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN não configurado.")
if not MP_ACCESS_TOKEN:
    raise RuntimeError("MP_ACCESS_TOKEN não configurado.")
if not MP_WEBHOOK_SECRET:
    logger.warning(
        "MP_WEBHOOK_SECRET não configurado — a validação de assinatura do webhook "
        "está DESATIVADA. Qualquer pessoa poderia forjar uma notificação de pagamento. "
        "Configure a chave secreta no painel do Mercado Pago (Suas integrações > Webhooks) "
        "assim que possível."
    )

# ==============================
# BANCO
# ==============================
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def iniciar_banco():
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                user_id INTEGER PRIMARY KEY,
                vip INTEGER DEFAULT 0,
                vip_expira TEXT,
                data_registro TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pagamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                mp_payment_id TEXT UNIQUE,
                valor REAL,
                status TEXT DEFAULT 'pendente',
                criado_em TEXT,
                confirmado_em TEXT
            )
        ''')
    logger.info("Banco de dados do webhook pronto.")


def pagamento_ja_processado(mp_payment_id: str) -> bool:
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT status FROM pagamentos WHERE mp_payment_id = ?', (mp_payment_id,)
        )
        res = cursor.fetchone()
    return res is not None and res[0] == "confirmado"


def registrar_pagamento_confirmado(user_id: int, mp_payment_id: str, valor: float):
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pagamentos (user_id, mp_payment_id, valor, status, criado_em, confirmado_em)
            VALUES (?, ?, ?, 'confirmado', ?, ?)
            ON CONFLICT(mp_payment_id) DO UPDATE SET
                status = 'confirmado',
                confirmado_em = excluded.confirmado_em
        ''', (user_id, mp_payment_id, valor, datetime.now().isoformat(), datetime.now().isoformat()))


def ativar_vip(user_id: int, dias: int = VIP_DIAS) -> str:
    expira_em = (datetime.now() + timedelta(days=dias)).isoformat()
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO usuarios (user_id, vip, vip_expira, data_registro)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                vip = 1,
                vip_expira = excluded.vip_expira
        ''', (user_id, expira_em, datetime.now().isoformat()))
    logger.info(f"VIP ativado para {user_id} até {expira_em}")
    return expira_em


def status_vip(user_id: int):
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT vip, vip_expira FROM usuarios WHERE user_id = ?', (user_id,)
        )
        res = cursor.fetchone()

    if not res or res[0] != 1 or not res[1]:
        return {"vip": False, "expira": None}

    try:
        expira_em = datetime.fromisoformat(res[1])
    except ValueError:
        return {"vip": False, "expira": None}

    ativo = datetime.now() < expira_em
    return {"vip": ativo, "expira": res[1]}


# ==============================
# TELEGRAM
# ==============================
def enviar_msg(user_id, texto):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": user_id, "text": texto, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.error(f"Telegram respondeu {r.status_code} ao notificar {user_id}: {r.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro ao notificar {user_id} via Telegram: {e}")


# ==============================
# MERCADO PAGO
# ==============================
def gerar_pagamento(user_id: int):
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "items": [{
            "title": "Super Trevo PRO VIP",
            "quantity": 1,
            "currency_id": "BRL",
            "unit_price": VIP_PRECO,
        }],
        "payer": {"email": f"user{user_id}@gmail.com"},
        "external_reference": str(user_id),
        "notification_url": WEBHOOK_URL,
        "back_urls": {
            "success": "https://t.me/",
            "failure": "https://t.me/",
            "pending": "https://t.me/",
        },
        "auto_return": "approved",
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro de rede ao criar preferência MP para {user_id}: {e}")
        return None

    try:
        result = response.json()
    except ValueError as e:
        logger.error(f"Resposta não-JSON do Mercado Pago: {e}")
        return None

    if response.status_code != 201:
        logger.error(f"Erro na API do Mercado Pago ({response.status_code}): {result}")
        return None

    return result.get("init_point")


def validar_assinatura(req) -> bool:
    """Valida o header x-signature enviado pelo Mercado Pago, conforme o esquema
    HMAC-SHA256 documentado em: Suas integrações > Webhooks > Assinatura secreta.
    Retorna True se MP_WEBHOOK_SECRET não estiver configurado (modo permissivo,
    com warning já emitido na subida do app)."""
    if not MP_WEBHOOK_SECRET:
        return True

    x_signature = req.headers.get("x-signature", "")
    x_request_id = req.headers.get("x-request-id", "")
    data_id = req.args.get("data.id", "") or req.args.get("id", "")

    if not x_signature or not data_id:
        logger.warning("Webhook recebido sem headers de assinatura esperados.")
        return False

    partes = dict(
        p.split("=", 1) for p in x_signature.split(",") if "=" in p
    )
    ts = partes.get("ts", "")
    v1_recebido = partes.get("v1", "")

    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    v1_calculado = hmac.new(
        MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()

    valido = hmac.compare_digest(v1_calculado, v1_recebido)
    if not valido:
        logger.warning("Assinatura do webhook inválida — notificação rejeitada.")
    return valido


# ==============================
# ROTAS
# ==============================
@app.route('/')
def home():
    return "🔥 Super Trevo ONLINE", 200


@app.route('/gerar_pagamento/<user_id>')
def gerar_pagamento_api(user_id):
    if not user_id.isdigit():
        return jsonify({"erro": "user_id inválido"}), 400

    link = gerar_pagamento(int(user_id))
    if link:
        return jsonify({"link": link})
    return jsonify({"erro": "Erro ao gerar pagamento"}), 500


@app.route('/liberar_vip_manual/<user_id>', methods=['POST'])
def liberar_vip_manual_api(user_id):
    """Chamado pelo comando /liberar (admin) do bot. A checagem de quem pode chamar
    /liberar já acontece no bot (só ADMIN_ID) — este endpoint apenas executa a ação."""
    if not user_id.isdigit():
        return jsonify({"erro": "user_id inválido"}), 400

    body = request.get_json(silent=True) or {}
    dias = body.get("dias", VIP_DIAS)
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        dias = VIP_DIAS

    expira_em = ativar_vip(int(user_id), dias)
    return jsonify({"expira": expira_em})


@app.route('/vip_status/<user_id>')
def vip_status_api(user_id):
    """Fonte única de verdade sobre o status VIP. O bot deve consultar este
    endpoint em vez de checar um banco local, já que bot e webhook podem
    rodar como serviços separados no Render (filesystems independentes)."""
    if not user_id.isdigit():
        return jsonify({"erro": "user_id inválido"}), 400

    return jsonify(status_vip(int(user_id)))


@app.route('/status_pagamento/<user_id>')
def status_pagamento_api(user_id):
    """Mantido por compatibilidade com o botão 'Já paguei, verificar' do bot.
    Hoje reflete o mesmo dado de /vip_status, já que a liberação é automática
    via webhook."""
    if not user_id.isdigit():
        return jsonify({"erro": "user_id inválido"}), 400

    info = status_vip(int(user_id))
    return jsonify({"pago": info["vip"]})


@app.route('/webhook', methods=['POST'])
def webhook():
    if not validar_assinatura(request):
        return "Assinatura inválida", 401

    data = request.json
    if not data:
        return "Sem dados", 400

    try:
        tipo_evento = data.get("type") == "payment" or data.get("action") == "payment.updated"
        if not tipo_evento:
            return "Evento ignorado", 200

        payment_id = data.get("data", {}).get("id")
        if not payment_id:
            return "Sem payment_id", 400

        # Idempotência: se já processamos esse pagamento, não repete a liberação/mensagem.
        if pagamento_ja_processado(payment_id):
            logger.info(f"Pagamento {payment_id} já processado — ignorando notificação repetida.")
            return "Já processado", 200

        url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        response = requests.get(url, headers=headers, timeout=10)
        info = response.json()

        if info.get("status") != "approved":
            logger.info(f"Pagamento {payment_id} com status '{info.get('status')}' — não liberado.")
            return "OK", 200

        valor_pago = info.get("transaction_amount")
        if valor_pago is None or abs(valor_pago - VIP_PRECO) > 0.01:
            logger.warning(
                f"Pagamento {payment_id} aprovado com valor divergente "
                f"(esperado {VIP_PRECO}, recebido {valor_pago}) — não liberado automaticamente."
            )
            return "Valor divergente", 200

        user_id_str = info.get("external_reference")
        if not user_id_str or not user_id_str.isdigit():
            logger.error(f"Pagamento {payment_id} sem external_reference válido.")
            return "Sem external_reference", 200

        user_id = int(user_id_str)
        ativar_vip(user_id)
        registrar_pagamento_confirmado(user_id, payment_id, valor_pago)

        enviar_msg(
            user_id,
            f"🎉 *Pagamento aprovado!*\n\nSeu acesso VIP foi liberado automaticamente "
            f"por {VIP_DIAS} dias 🚀"
        )
        logger.info(f"✅ VIP liberado para {user_id} (pagamento {payment_id})")

    except Exception as e:
        logger.error(f"Erro ao processar webhook: {e}")

    return "OK", 200


# ==============================
# RUN
# ==============================
if __name__ == "__main__":
    iniciar_banco()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
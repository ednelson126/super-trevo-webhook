from flask import Flask, request
import sqlite3
import requests
import os

app = Flask(__name__)

TOKEN = "8724057435:AAEUMicEqE-hxjShvKdBJGHVUm850mgPvDg"

# ==============================
# ATIVAR VIP
# ==============================
def ativar_vip(user_id):
    conn = sqlite3.connect('super_trevo.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO usuarios (user_id, vip) VALUES (?, 1)', (user_id,))
    conn.commit()
    conn.close()

# ==============================
# ENVIAR MENSAGEM TELEGRAM
# ==============================
def enviar_msg(user_id):
    msg = "🎉 *Pagamento aprovado!*\n\nSeu acesso VIP foi liberado com sucesso 🚀"
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    
    payload = {
        "chat_id": user_id,
        "text": msg,
        "parse_mode": "Markdown"
    }

    try:
        requests.post(url, json=payload)
    except Exception as e:
        print("❌ Erro ao enviar mensagem:", e)

# ==============================
# ROTA TESTE (IMPORTANTE PARA RENDER)
# ==============================
@app.route('/')
def home():
    return "🔥 Webhook Super Trevo ONLINE", 200

# ==============================
# WEBHOOK MERCADO PAGO
# ==============================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    print("📩 Recebido:", data)

    try:
        if not data:
            return "Sem dados", 400

        action = data.get("action")
        payment_data = data.get("data", {})

        if action == "payment.updated":
            payment_id = payment_data.get("id")

            if not payment_id:
                return "Sem payment_id", 400

            # CONSULTAR PAGAMENTO
            url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
            headers = {
                "Authorization": "Bearer APP_USR-7479127174794036-041215-232df09bcca56ad0d165a4fb4b6708c0-367052923"
            }

            response = requests.get(url, headers=headers)
            info = response.json()

            print("🔍 Dados do pagamento:", info)

            if info.get("status") == "approved":
                user_id = info.get("external_reference")

                if user_id:
                    ativar_vip(user_id)
                    enviar_msg(user_id)

                    print(f"✅ VIP liberado automaticamente para {user_id}")
                else:
                    print("⚠️ external_reference não encontrado")

    except Exception as e:
        print("❌ Erro geral:", e)

    return "OK", 200

# ==============================
# EXECUÇÃO (CORRIGIDO PARA RENDER)
# ==============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
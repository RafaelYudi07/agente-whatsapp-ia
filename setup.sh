#!/usr/bin/env bash
# Cria a instância do WhatsApp na Evolution API, aponta o webhook para o n8n
# e salva o QR Code em qrcode.png para escanear com o celular.
set -euo pipefail
cd "$(dirname "$0")"
source .env

API=http://localhost:8080
INST=${EVOLUTION_INSTANCE:-agente}
H=(-H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json")

echo "→ criando instância '$INST'"
curl -s "${H[@]}" -X POST "$API/instance/create" \
  -d "{\"instanceName\":\"$INST\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":true}" >/dev/null || true

echo "→ configurando webhook → n8n"
curl -s "${H[@]}" -X POST "$API/webhook/set/$INST" -d '{
  "webhook": {
    "enabled": true,
    "url": "http://n8n:5678/webhook/whatsapp",
    "byEvents": false,
    "base64": false,
    "events": ["MESSAGES_UPSERT"]
  }
}' >/dev/null

echo "→ gerando QR Code"
curl -s "${H[@]}" "$API/instance/connect/$INST" | python3 -c '
import sys, json, base64
d = json.load(sys.stdin)
b = d.get("base64") or ""
if not b:
    print("  sem QR: a instância já pode estar conectada ->", d); sys.exit(0)
open("qrcode.png", "wb").write(base64.b64decode(b.split(",", 1)[-1]))
print("  salvo em qrcode.png: WhatsApp > Aparelhos conectados > Conectar aparelho")
'

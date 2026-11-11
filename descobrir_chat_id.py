"""Mostra o Chat ID das conversas recentes com o seu bot.

Uso: mande qualquer mensagem para o bot no Telegram e depois rode
    python descobrir_chat_id.py
"""

import logging
import os

import httpx
from dotenv import load_dotenv

logging.getLogger("httpx").setLevel(logging.WARNING)
load_dotenv()

token = os.getenv("TELEGRAM_TOKEN", "").strip()
if not token:
    raise SystemExit("TELEGRAM_TOKEN não encontrado. Confira o arquivo .env.")

try:
    resposta = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    corpo = resposta.json()
except httpx.HTTPError as erro:
    raise SystemExit(f"Falha de conexão com o Telegram ({type(erro).__name__}).")

if not corpo.get("ok"):
    raise SystemExit(f"O Telegram recusou a consulta: {corpo.get('description')}. O token está correto?")

conversas = {}
for atualizacao in corpo.get("result", []):
    mensagem = atualizacao.get("message") or atualizacao.get("edited_message")
    if mensagem:
        chat = mensagem["chat"]
        conversas[chat["id"]] = chat.get("first_name") or chat.get("title") or "sem nome"

if not conversas:
    print("Nenhuma mensagem encontrada. Mande um 'oi' para o bot no Telegram e rode de novo.")
else:
    for chat_id, nome in conversas.items():
        print(f"Chat ID: {chat_id}  (conversa com: {nome})")
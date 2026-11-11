"""Envio de mensagens pelo Telegram.

As credenciais vêm de variáveis de ambiente:
- No seu computador: do arquivo .env (que nunca vai para o Git).
- No GitHub Actions: dos Secrets do repositório.
"""

import logging
import os

import httpx
from dotenv import load_dotenv

# O httpx registra a URL de cada requisição nos logs, e a URL do Telegram contém o token.
# Deixando o logger dele só em WARNING, essas URLs nunca aparecem nos logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Carrega o .env se ele existir. Não sobrescreve variáveis que já existem no ambiente.
load_dotenv()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{metodo}"


def carregar_credenciais():
    """Lê o token e o Chat ID do ambiente. Falha com uma mensagem clara se faltar algum."""
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    faltando = [nome for nome, valor in [("TELEGRAM_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)] if not valor]
    if faltando:
        raise RuntimeError(f"Variáveis não configuradas: {', '.join(faltando)}. Confira o .env ou os Secrets do GitHub.")

    return token, chat_id


def enviar_mensagem(texto):
    """Envia uma mensagem de texto para o seu chat. Retorna True se deu certo."""
    token, chat_id = carregar_credenciais()
    url = TELEGRAM_API.format(token=token, metodo="sendMessage")
    dados = {
        "chat_id": chat_id,
        "text": texto,
        "disable_web_page_preview": True,
    }

    try:
        resposta = httpx.post(url, json=dados, timeout=20)
        corpo = resposta.json()
    except httpx.HTTPError as erro:
        # A mensagem original do erro contém a URL (e o token). Mostramos só o tipo do erro.
        logging.error("Falha de conexão com o Telegram (%s).", type(erro).__name__)
        return False
    except ValueError:
        logging.error("O Telegram respondeu algo que não é JSON (status %s).", resposta.status_code)
        return False

    if not corpo.get("ok"):
        # A descrição do Telegram é segura de mostrar (ex.: "Unauthorized", "chat not found").
        logging.error("O Telegram recusou a mensagem: %s", corpo.get("description", "sem descrição"))
        return False

    return True


# ----------------------------------------------------------------------------
# Formatação das mensagens
# ----------------------------------------------------------------------------

CABECALHOS = {
    "nova": "✈️ NOVA OPORTUNIDADE DE VIAGEM!",
    "queda": "📉 O PREÇO CAIU!",
    "lembrete": "🔁 A PROMOÇÃO CONTINUA!",
}


def formatar_reais(valor):
    """1100.5 -> 'R$ 1.100,50'"""
    texto = f"{valor:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _companhias(viagem):
    ida, volta = viagem["trecho_ida"]["companhia"], viagem["trecho_volta"]["companhia"]
    return ida if ida == volta else f"{ida} (ida) / {volta} (volta)"


def _escalas(viagem):
    ida, volta = viagem["trecho_ida"]["escalas"], viagem["trecho_volta"]["escalas"]
    if ida == 0 and volta == 0:
        return "voo direto nos dois trechos"
    return f"ida {ida}, volta {volta}"


def formatar_alerta(destino, origem, viagens, tipo, preco_limite, preco_anterior=None):
    """Monta o texto do alerta de um destino com até N opções de datas.

    destino: item da lista "destinos" do config.json
    origem:  bloco "origem" do config.json
    viagens: lista de viagens confirmadas pelo buscador, da mais barata para a mais cara
    """
    linhas = [
        CABECALHOS.get(tipo, CABECALHOS["nova"]),
        "",
        f"Destino: {destino['cidade']} - {destino['uf']}",
        f"Origem: {origem['cidade']} - {origem['uf']}",
    ]
    if tipo == "queda" and preco_anterior:
        linhas.append(f"Último alerta: {formatar_reais(preco_anterior)}")

    for numero, viagem in enumerate(viagens, start=1):
        linhas += [
            "",
            f"Opção {numero}",
            f"Ida: {viagem['ida']:%d/%m/%Y} | Volta: {viagem['volta']:%d/%m/%Y}",
            f"Duração: {viagem['duracao']} dias",
            f"Companhia: {_companhias(viagem)}",
            f"Escalas: {_escalas(viagem)}",
            f"Preço total: {formatar_reais(viagem['preco'])}",
            f"Link: {viagem['link']}",
        ]

    linhas += ["", f"Preço máximo configurado: {formatar_reais(preco_limite)}"]
    return "\n".join(linhas)


def formatar_aviso_falha(motivo):
    """Mensagem enviada quando a execução inteira falha (para você não confundir com 'sem promoções')."""
    return f"⚠️ MONITOR DE PASSAGENS COM PROBLEMA\n\n{motivo}\n\nVeja os logs da execução no GitHub Actions."


if __name__ == "__main__":
    # Este bloco só roda quando você executa "python notificador.py" diretamente.
    # Quando outro arquivo fizer "import notificador", ele é ignorado.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if enviar_mensagem("✈️ Teste do monitor de passagens: o bot está funcionando!"):
        logging.info("Mensagem enviada. Confira o Telegram.")
    else:
        logging.error("Não foi possível enviar a mensagem.")
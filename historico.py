"""Histórico de alertas enviados, guardado em historico.json.

Estratégia (opção B, por destino):
- Cada destino tem no máximo um registro: o último alerta enviado.
- Um destino gera alerta quando:
    "nova"     -> não existe registro para ele;
    "queda"    -> o menor preço de hoje é X% menor que o do último alerta;
    "lembrete" -> o último alerta tem pelo menos N dias e ainda há preço dentro do limite.
- Registros antigos (ou de destinos que saíram do config.json) são apagados,
  então o arquivo nunca passa de um registro por destino.

Formato do arquivo:
{
  "versao": 1,
  "destinos": {
    "PMW": {
      "data_alerta": "2026-11-11",
      "tipo": "nova",
      "menor_preco": 829.0,
      "viagens": [{"ida": "2027-01-21", "volta": "2027-01-25", "preco": 829.0}]
    }
  }
}
"""

import json
import logging
import os
from datetime import date

log = logging.getLogger(__name__)

ARQUIVO_PADRAO = "historico.json"
VERSAO = 1


def _vazio():
    return {"versao": VERSAO, "destinos": {}}


def carregar(caminho=ARQUIVO_PADRAO):
    """Lê o histórico. Se o arquivo não existir ou estiver corrompido, começa do zero."""
    if not os.path.exists(caminho):
        log.info("Histórico não encontrado, começando um novo.")
        return _vazio()

    try:
        with open(caminho, encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
    except (json.JSONDecodeError, OSError) as erro:
        log.error("Histórico ilegível (%s). Começando um novo; alertas recentes podem se repetir.", erro)
        return _vazio()

    if not isinstance(dados, dict) or not isinstance(dados.get("destinos"), dict):
        log.error("Histórico com formato inesperado. Começando um novo.")
        return _vazio()

    return dados


def salvar(historico, caminho=ARQUIVO_PADRAO):
    """Grava o histórico de forma segura: escreve num arquivo temporário e depois troca.

    Assim, se algo der errado no meio da gravação, o arquivo antigo continua intacto.
    """
    temporario = caminho + ".tmp"
    with open(temporario, "w", encoding="utf-8") as arquivo:
        json.dump(historico, arquivo, ensure_ascii=False, indent=2, sort_keys=True)
        arquivo.write("\n")
    os.replace(temporario, caminho)


def _dias_desde(data_texto, hoje):
    try:
        return (hoje - date.fromisoformat(data_texto)).days
    except (TypeError, ValueError):
        return None  # data inválida: o registro será tratado como antigo


def limpar(historico, destinos_validos, manter_dias, hoje=None):
    """Remove registros com mais de `manter_dias` dias ou de destinos fora do config.json.

    Retorna a lista de códigos removidos.
    """
    hoje = hoje or date.today()
    removidos = []
    for codigo, registro in list(historico["destinos"].items()):
        dias = _dias_desde(registro.get("data_alerta"), hoje)
        if codigo not in destinos_validos or dias is None or dias > manter_dias:
            del historico["destinos"][codigo]
            removidos.append(codigo)
    if removidos:
        log.info("Histórico: removidos registros antigos de %s", ", ".join(removidos))
    return removidos


def decidir_alerta(historico, codigo, menor_preco, queda_minima_percentual, repetir_apos_dias, hoje=None):
    """Decide se um destino deve gerar alerta hoje.

    Retorna (tipo, preco_anterior). tipo é "nova", "queda", "lembrete" ou None (não alertar).
    """
    hoje = hoje or date.today()
    registro = historico["destinos"].get(codigo)
    if registro is None:
        return "nova", None

    anterior = float(registro.get("menor_preco", 0)) or None
    if anterior and menor_preco <= anterior * (1 - queda_minima_percentual / 100):
        return "queda", anterior

    dias = _dias_desde(registro.get("data_alerta"), hoje)
    if dias is None or dias >= repetir_apos_dias:
        return "lembrete", anterior

    return None, anterior


def registrar_alerta(historico, codigo, viagens, tipo, hoje=None):
    """Salva no histórico (em memória) o alerta que acabou de ser enviado."""
    hoje = hoje or date.today()
    historico["destinos"][codigo] = {
        "data_alerta": hoje.isoformat(),
        "tipo": tipo,
        "menor_preco": min(v["preco"] for v in viagens),
        "viagens": [
            {"ida": v["ida"].isoformat(), "volta": v["volta"].isoformat(), "preco": v["preco"]}
            for v in viagens
        ],
    }
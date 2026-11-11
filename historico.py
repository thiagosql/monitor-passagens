"""Histórico do monitor, guardado em historico.json.

O arquivo guarda três coisas:

1. "destinos": o último alerta enviado de cada destino (opção B, por destino).
   Um destino gera alerta quando:
     "nova"     -> não existe registro para ele;
     "queda"    -> o menor preço de hoje é X% menor que o do último alerta;
     "lembrete" -> o último alerta tem pelo menos N dias e ainda há preço dentro do limite.

2. "verificacoes": quando cada destino foi verificado com sucesso pela última vez.
   Serve para montar a fila (quem está há mais tempo sem verificar vai primeiro)
   e para avisar se algum destino ficar muito tempo sem ser verificado.

3. "criado_em" e "ultimo_aviso_falha": controle interno dos avisos de problema.

Registros antigos (ou de destinos que saíram do config.json) são apagados,
então o arquivo tem no máximo dois registros por destino.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

ARQUIVO_PADRAO = "historico.json"
VERSAO = 2


def agora():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _novo():
    return {
        "versao": VERSAO,
        "criado_em": agora().isoformat(),
        "destinos": {},
        "verificacoes": {},
        "ultimo_aviso_falha": None,
    }


def _completar(dados):
    """Garante que arquivos de versões anteriores tenham todos os campos atuais."""
    dados.setdefault("criado_em", agora().isoformat())
    dados.setdefault("destinos", {})
    dados.setdefault("verificacoes", {})
    dados.setdefault("ultimo_aviso_falha", None)
    dados["versao"] = VERSAO
    return dados


def carregar(caminho=ARQUIVO_PADRAO):
    """Lê o histórico. Se o arquivo não existir ou estiver corrompido, começa do zero."""
    if not os.path.exists(caminho):
        log.info("Histórico não encontrado, começando um novo.")
        return _novo()

    try:
        with open(caminho, encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
    except (json.JSONDecodeError, OSError) as erro:
        log.error("Histórico ilegível (%s). Começando um novo; alertas recentes podem se repetir.", erro)
        return _novo()

    if not isinstance(dados, dict) or not isinstance(dados.get("destinos", {}), dict):
        log.error("Histórico com formato inesperado. Começando um novo.")
        return _novo()

    return _completar(dados)


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


def _momento(texto):
    try:
        return datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None


def limpar(historico, destinos_validos, manter_dias, hoje=None):
    """Remove alertas com mais de `manter_dias` dias e tudo de destinos fora do config.json.

    Retorna a lista de códigos removidos da parte de alertas.
    """
    hoje = hoje or date.today()
    removidos = []
    for codigo, registro in list(historico["destinos"].items()):
        dias = _dias_desde(registro.get("data_alerta"), hoje)
        if codigo not in destinos_validos or dias is None or dias > manter_dias:
            del historico["destinos"][codigo]
            removidos.append(codigo)
    for codigo in list(historico["verificacoes"]):
        if codigo not in destinos_validos:
            del historico["verificacoes"][codigo]
    if removidos:
        log.info("Histórico: removidos alertas antigos de %s", ", ".join(removidos))
    return removidos


# ----------------------------------------------------------------------------
# Alertas
# ----------------------------------------------------------------------------

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


# ----------------------------------------------------------------------------
# Fila de verificação e saúde do monitor
# ----------------------------------------------------------------------------

def registrar_verificacao(historico, codigo, quando=None):
    """Marca que o destino foi verificado com sucesso agora."""
    historico["verificacoes"][codigo] = (quando or agora()).isoformat()


def ordenar_por_antiguidade(historico, codigos):
    """Ordena os destinos do que está há mais tempo sem verificação para o mais recente.

    Destinos nunca verificados vêm primeiro, na ordem do config.json.
    """
    nunca = datetime.min.replace(tzinfo=timezone.utc)

    def chave(item):
        posicao, codigo = item
        momento = _momento(historico["verificacoes"].get(codigo)) or nunca
        return (momento, posicao)

    return [codigo for _, codigo in sorted(enumerate(codigos), key=chave)]


def destinos_atrasados(historico, codigos, horas, quando=None):
    """Destinos sem verificação bem-sucedida há mais de `horas` horas.

    Um destino nunca verificado conta a partir da criação do histórico,
    para não disparar aviso logo na primeira execução.
    """
    quando = quando or agora()
    limite = quando - timedelta(hours=horas)
    criado = _momento(historico.get("criado_em")) or quando
    atrasados = []
    for codigo in codigos:
        ultima = _momento(historico["verificacoes"].get(codigo)) or criado
        if ultima < limite:
            atrasados.append(codigo)
    return atrasados


def pode_avisar_falha_hoje(historico, hoje=None):
    """Limita o aviso de problema a uma mensagem por dia."""
    hoje = hoje or date.today()
    return historico.get("ultimo_aviso_falha") != hoje.isoformat()


def registrar_aviso_falha(historico, hoje=None):
    historico["ultimo_aviso_falha"] = (hoje or date.today()).isoformat()
"""Consulta de preços no Google Flights usando a biblioteca fli.

A busca acontece em duas fases:
1. Varredura: o calendário de preços do Google devolve o menor preço de ida e volta
   para cada data de ida, com uma duração fixa. É rápido, mas o valor é indicativo.
2. Confirmação: para as datas mais baratas, fazemos a busca completa, que traz
   o preço real do momento, a companhia, os voos e as escalas.
"""

import calendar
import logging
import time
import urllib.parse
from datetime import date, timedelta

from fli.core.builders import build_date_search_segments, build_flight_segments
from fli.models import (
    Airport,
    DateSearchFilters,
    FlightSearchFilters,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
)
from fli.search import SearchDates, SearchFlights
from fli.search._concurrency import configure_concurrency

log = logging.getLogger(__name__)

# Por padrão a fli dispara várias requisições em paralelo, e o Google às vezes responde
# vazio a essas rajadas. Com 1 worker, tudo vira sequencial: um pouco mais lento, bem mais estável.
configure_concurrency(1)

# O calendário do Google aceita no máximo 61 dias por consulta.
DIAS_POR_BLOCO = 61

# Uma resposta vazia pode ser "não há voos" ou uma falha momentânea do Google.
# Repetimos algumas vezes, com espera crescente, antes de aceitar o vazio como real.
# As esperas crescem a cada tentativa: 5 s, 10 s, 15 s.
TENTATIVAS = 4
ESPERA_BASE_SEGUNDOS = 5

# Nomes amigáveis para as companhias mais comuns no Brasil.
# A fli usa nomes de cadastro internacional (ex.: "LATAM Chile" para o código LA).
NOMES_COMPANHIAS = {
    "LA": "LATAM",
    "JJ": "LATAM",
    "G3": "Gol",
    "AD": "Azul",
    "2Z": "Voepass",
}


# Se várias consultas seguidas terminam vazias mesmo depois de todas as tentativas,
# o Google provavelmente bloqueou o IP. Continuar só desperdiça tempo.
VAZIOS_SEGUIDOS_PARA_BLOQUEIO = 3
_vazios_seguidos = 0


class ErroBusca(Exception):
    """Falha ao consultar o Google Flights (rede, bloqueio ou resposta inesperada)."""


class BloqueioDetectado(ErroBusca):
    """Várias consultas seguidas voltaram vazias: provável bloqueio temporário do Google."""


def reiniciar_detector_de_bloqueio():
    global _vazios_seguidos
    _vazios_seguidos = 0


# ----------------------------------------------------------------------------
# Datas
# ----------------------------------------------------------------------------

def somar_meses(data_base, meses):
    """Soma meses a uma data, ajustando o dia quando o mês de destino é mais curto."""
    mes_total = data_base.month - 1 + meses
    ano = data_base.year + mes_total // 12
    mes = mes_total % 12 + 1
    dia = min(data_base.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def janela_de_busca(meses_a_frente, hoje=None):
    """Retorna (primeira_data, ultima_data) de ida. Começa amanhã para evitar voos do dia."""
    hoje = hoje or date.today()
    return hoje + timedelta(days=1), somar_meses(hoje, meses_a_frente)


# ----------------------------------------------------------------------------
# Auxiliares
# ----------------------------------------------------------------------------

def _aeroportos(codigos):
    """Converte ["CNF", "PLU"] em objetos Airport da fli. Erro claro se o código não existir."""
    try:
        return [Airport[codigo] for codigo in codigos]
    except KeyError as erro:
        raise ValueError(f"Código de aeroporto desconhecido: {erro.args[0]}") from None


def _codigo_companhia(airline):
    # Códigos que começam com número viram nomes como "_2Z" no enum da fli.
    return airline.name.lstrip("_")


def _nome_companhia(resultado):
    codigo = _codigo_companhia(resultado.legs[0].airline)
    return NOMES_COMPANHIAS.get(codigo) or resultado.primary_airline_name or resultado.legs[0].airline.value


def _descrever_trecho(resultado):
    """Resume um sentido da viagem (ida ou volta) num dicionário simples."""
    primeiro, ultimo = resultado.legs[0], resultado.legs[-1]
    return {
        "companhia": _nome_companhia(resultado),
        "voos": [f"{_codigo_companhia(leg.airline)} {leg.flight_number}" for leg in resultado.legs],
        "escalas": resultado.stops,
        "partida": primeiro.departure_datetime.strftime("%d/%m %H:%M"),
        "chegada": ultimo.arrival_datetime.strftime("%d/%m %H:%M"),
        "duracao_minutos": resultado.duration,
    }


def _com_retentativas(consulta, descricao):
    """Executa consulta() até ela devolver algo não vazio ou acabarem as tentativas.

    - Resposta vazia em todas as tentativas: retorna o vazio (pode ser falta real de voos).
    - Exceção em todas as tentativas: levanta ErroBusca com a última falha.
    """
    global _vazios_seguidos
    ultimo_erro = None
    resultado = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resultado = consulta()
            ultimo_erro = None
        except Exception as erro:
            ultimo_erro = erro
            resultado = None

        if resultado:
            if tentativa > 1:
                log.info("    %s: resposta obtida na tentativa %d", descricao, tentativa)
            _vazios_seguidos = 0
            return resultado

        if tentativa < TENTATIVAS:
            espera = ESPERA_BASE_SEGUNDOS * tentativa
            motivo = f"erro {type(ultimo_erro).__name__}" if ultimo_erro else "resposta vazia"
            log.warning("    %s: %s na tentativa %d, repetindo em %d s", descricao, motivo, tentativa, espera)
            time.sleep(espera)

    if ultimo_erro:
        raise ErroBusca(f"{descricao}: {type(ultimo_erro).__name__}: {ultimo_erro}") from ultimo_erro
    log.warning("    %s: vazio após %d tentativas", descricao, TENTATIVAS)
    _vazios_seguidos += 1
    if _vazios_seguidos >= VAZIOS_SEGUIDOS_PARA_BLOQUEIO:
        raise BloqueioDetectado(f"{_vazios_seguidos} consultas seguidas vazias (última: {descricao})")
    return resultado


def escalas_para_filtro(max_escalas):
    """Converte o limite de escalas do config.json no filtro equivalente do Google.

    None (ou 3+) = qualquer número de escalas; 0 = só voos diretos; 1 = até 1; 2 = até 2.
    """
    if max_escalas is None:
        return MaxStops.ANY
    return {0: MaxStops.NON_STOP, 1: MaxStops.ONE_STOP_OR_FEWER, 2: MaxStops.TWO_OR_FEWER_STOPS}.get(
        max_escalas, MaxStops.ANY
    )


def montar_link(origem_texto, destino_iata, ida, volta, moeda="BRL", pais="BR"):
    """Link de pesquisa do Google Flights já com origem, destino e datas preenchidos."""
    consulta = f"Flights to {destino_iata} from {origem_texto} on {ida.isoformat()} through {volta.isoformat()}"
    parametros = urllib.parse.urlencode({"q": consulta, "curr": moeda, "hl": "pt-BR", "gl": pais})
    return f"https://www.google.com/travel/flights?{parametros}"


# ----------------------------------------------------------------------------
# Fase 1: varredura do calendário
# ----------------------------------------------------------------------------

def _blocos(inicio, fim):
    """Divide o período [inicio, fim] em blocos de até DIAS_POR_BLOCO dias."""
    atual = inicio
    while atual <= fim:
        final_bloco = min(atual + timedelta(days=DIAS_POR_BLOCO - 1), fim)
        yield atual, final_bloco
        atual = final_bloco + timedelta(days=1)


def varrer_calendario(origens, destino, duracao, inicio, fim, pausa=2, moeda="BRL", pais="BR", max_escalas=None):
    """Menor preço de ida e volta para cada data de ida entre inicio e fim.

    Retorna (combinacoes, blocos_consultados, blocos_vazios).
    combinacoes é uma lista de dicionários {"ida", "volta", "duracao", "preco"}, ordenada por preço.
    """
    combinacoes = []
    blocos_consultados = blocos_vazios = 0

    for comeco_bloco, fim_bloco in _blocos(inicio, fim):
        segmentos, tipo = build_date_search_segments(
            origin=_aeroportos(origens),
            destination=_aeroportos([destino]),
            start_date=comeco_bloco.isoformat(),
            trip_duration=duracao,
            is_round_trip=True,
        )
        filtros = DateSearchFilters(
            trip_type=tipo,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=segmentos,
            stops=escalas_para_filtro(max_escalas),
            seat_type=SeatType.ECONOMY,
            from_date=comeco_bloco.isoformat(),
            to_date=fim_bloco.isoformat(),
            duration=duracao,
        )
        descricao = f"{destino} {duracao}d {comeco_bloco:%d/%m}-{fim_bloco:%d/%m}"
        resultados = _com_retentativas(
            lambda: SearchDates().search(filtros, currency=moeda, country=pais), descricao
        )
        blocos_consultados += 1
        if not resultados:
            blocos_vazios += 1

        for item in resultados or []:
            if len(item.date) != 2 or item.price is None or item.price <= 0:
                continue  # descarta respostas incompletas em vez de inventar valores
            ida, volta = item.date[0].date(), item.date[1].date()
            combinacoes.append({"ida": ida, "volta": volta, "duracao": (volta - ida).days, "preco": float(item.price)})

        time.sleep(pausa)  # espaçar as requisições reduz as respostas vazias do Google

    combinacoes.sort(key=lambda c: c["preco"])
    return combinacoes, blocos_consultados, blocos_vazios


def varrer_destino(origens, destino, duracoes, inicio, fim, pausa=2, moeda="BRL", pais="BR", max_escalas=None):
    """Roda a varredura para todas as durações e junta tudo, do mais barato ao mais caro.

    Retorna (combinacoes, cobertura). cobertura = {"blocos": total, "vazios": quantos vieram vazios}.
    """
    todas = []
    cobertura = {"blocos": 0, "vazios": 0}
    for duracao in duracoes:
        encontradas, consultados, vazios = varrer_calendario(
            origens, destino, duracao, inicio, fim, pausa, moeda, pais, max_escalas
        )
        log.info("  %s, %d dias: %d datas com preço (%d de %d blocos vazios)",
                 destino, duracao, len(encontradas), vazios, consultados)
        todas.extend(encontradas)
        cobertura["blocos"] += consultados
        cobertura["vazios"] += vazios
    todas.sort(key=lambda c: c["preco"])
    return todas, cobertura


# ----------------------------------------------------------------------------
# Fase 2: confirmação de uma combinação de datas
# ----------------------------------------------------------------------------

def confirmar_viagem(origens, destino, ida, volta, origem_texto, moeda="BRL", pais="BR", max_escalas=None):
    """Busca completa para datas exatas. Retorna a opção mais barata ou None se não houver voos."""
    segmentos, tipo = build_flight_segments(
        origin=_aeroportos(origens),
        destination=_aeroportos([destino]),
        departure_date=ida.isoformat(),
        return_date=volta.isoformat(),
    )
    filtros = FlightSearchFilters(
        trip_type=tipo,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=segmentos,
        stops=escalas_para_filtro(max_escalas),
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
        show_all_results=False,  # mesma lista enxuta que o site mostra primeiro
    )

    # top_n=3: expande as 3 idas mais baratas para buscar as voltas de cada uma.
    descricao = f"voos {destino} {ida:%d/%m}-{volta:%d/%m}"
    resultados = _com_retentativas(
        lambda: SearchFlights().search(filtros, top_n=3, currency=moeda, country=pais), descricao
    )

    opcoes = []
    for item in resultados or []:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        trecho_ida, trecho_volta = item
        # Na segunda etapa do Google, cada volta mostra o preço total da combinação.
        # Se ele não vier, usamos o preço "a partir de" da ida, que também é total de ida e volta.
        preco = trecho_volta.price or trecho_ida.price
        if not preco or preco <= 0:
            continue
        opcoes.append({
            "destino": destino,
            "ida": ida,
            "volta": volta,
            "duracao": (volta - ida).days,
            "preco": float(preco),
            "preco_ida_a_partir_de": float(trecho_ida.price) if trecho_ida.price else None,
            "trecho_ida": _descrever_trecho(trecho_ida),
            "trecho_volta": _descrever_trecho(trecho_volta),
            "link": montar_link(origem_texto, destino, ida, volta, moeda, pais),
        })

    if not opcoes:
        return None
    return min(opcoes, key=lambda o: o["preco"])


def confirmar_melhores(origens, destino, combinacoes, quantidade, origem_texto, pausa=2, moeda="BRL", pais="BR",
                       max_escalas=None, aceitar=None, limite_tentativas=None):
    """Confirma as combinações mais baratas até obter `quantidade` viagens válidas.

    - Se a busca completa de uma data vier vazia (falha do Google ou falta real de voos),
      passa para a próxima mais barata.
    - `aceitar` é uma função opcional que recebe a viagem confirmada e diz se ela vale
      (ex.: preço real ainda abaixo do limite). Viagens recusadas não contam.
    - Para evitar execuções longas, tenta no máximo `limite_tentativas` datas
      (padrão: o dobro de `quantidade`).

    Retorna (viagens_confirmadas, datas_tentadas).
    """
    limite_tentativas = limite_tentativas or quantidade * 2
    confirmadas = []
    tentadas = 0
    for combinacao in combinacoes[:limite_tentativas]:
        if len(confirmadas) >= quantidade:
            break
        tentadas += 1
        viagem = confirmar_viagem(origens, destino, combinacao["ida"], combinacao["volta"], origem_texto,
                                  moeda, pais, max_escalas)
        time.sleep(pausa)
        if viagem is None:
            continue
        viagem["preco_calendario"] = combinacao["preco"]
        if aceitar is not None and not aceitar(viagem):
            log.info("    %s %s/%s descartada após confirmação (R$ %.2f)",
                     destino, viagem["ida"], viagem["volta"], viagem["preco"])
            continue
        confirmadas.append(viagem)
    return confirmadas, tentadas
"""Monitor de passagens: ponto de entrada da execução diária.

Uso:
    python main.py                  (todos os destinos ativos, envia alertas de verdade)
    python main.py PMW SCL          (só os destinos informados)
    python main.py --simular        (mostra as mensagens na tela, sem enviar e sem salvar histórico)

Códigos de saída: 0 = execução normal, 1 = falha geral da busca, 2 = erro no config.json.
"""

import argparse
import json
import logging
import sys
from datetime import date

import buscador
import historico
import notificador

log = logging.getLogger("monitor")

# Abaixo desta fração de blocos do calendário com dados, consideramos que a busca falhou.
COBERTURA_MINIMA = 0.5


# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------

def carregar_config(caminho="config.json"):
    with open(caminho, encoding="utf-8") as arquivo:
        return json.load(arquivo)


def validar_config(cfg):
    """Confere o config.json antes de gastar tempo com buscas. Retorna uma lista de problemas."""
    problemas = []
    obrigatorias = ["origem", "destinos", "preco_maximo", "duracao_minima_dias", "duracao_maxima_dias",
                    "meses_a_frente", "moeda", "pais", "candidatos_por_destino",
                    "pausa_entre_buscas_segundos", "alertas"]
    for chave in obrigatorias:
        if chave not in cfg:
            problemas.append(f"chave obrigatória ausente: {chave}")
    if problemas:
        return problemas

    if not 1 <= cfg["duracao_minima_dias"] <= cfg["duracao_maxima_dias"]:
        problemas.append("duracao_minima_dias precisa ser >= 1 e <= duracao_maxima_dias")
    if not 1 <= cfg["meses_a_frente"] <= 9:
        problemas.append("meses_a_frente precisa estar entre 1 e 9 (limite do Google: ~10 meses)")
    if cfg["preco_maximo"] <= 0:
        problemas.append("preco_maximo precisa ser positivo")
    escalas = cfg.get("max_escalas_por_trecho")
    if escalas is not None and (not isinstance(escalas, int) or escalas < 0):
        problemas.append("max_escalas_por_trecho precisa ser null ou um inteiro >= 0")

    alertas = cfg["alertas"]
    for chave in ["queda_minima_percentual", "repetir_apos_dias", "manter_historico_dias"]:
        if chave not in alertas:
            problemas.append(f"alertas.{chave} ausente")
    if not problemas and alertas["manter_historico_dias"] < alertas["repetir_apos_dias"]:
        problemas.append("alertas.manter_historico_dias precisa ser >= alertas.repetir_apos_dias")

    codigos = list(cfg["origem"].get("aeroportos", []))
    for destino in cfg["destinos"]:
        for chave in ["iata", "cidade", "uf"]:
            if chave not in destino:
                problemas.append(f"destino sem '{chave}': {destino}")
        codigos.append(destino.get("iata", "?"))
        if "preco_maximo" in destino and destino["preco_maximo"] <= 0:
            problemas.append(f"{destino.get('iata')}: preco_maximo precisa ser positivo")
    for codigo in codigos:
        try:
            buscador._aeroportos([codigo])
        except ValueError as erro:
            problemas.append(str(erro))

    return problemas


def limite_de_preco(cfg, destino):
    """O preco_maximo do destino, se existir; senão, o geral."""
    return destino.get("preco_maximo", cfg["preco_maximo"])


def escalas_dentro_do_limite(viagem, max_escalas):
    if max_escalas is None:
        return True
    return viagem["trecho_ida"]["escalas"] <= max_escalas and viagem["trecho_volta"]["escalas"] <= max_escalas


# ----------------------------------------------------------------------------
# Processamento de um destino
# ----------------------------------------------------------------------------

def processar_destino(cfg, destino, janela):
    """Busca, filtra e confirma as oportunidades de um destino.

    Retorna um dicionário com o resultado: viagens válidas, cobertura e eventual erro.
    """
    codigo = destino["iata"]
    origens = cfg["origem"]["aeroportos"]
    origem_texto = origens[0] if len(origens) == 1 else cfg["origem"]["cidade"]
    limite = limite_de_preco(cfg, destino)
    max_escalas = cfg.get("max_escalas_por_trecho")
    duracoes = range(cfg["duracao_minima_dias"], cfg["duracao_maxima_dias"] + 1)
    pausa = cfg["pausa_entre_buscas_segundos"]
    resultado = {"codigo": codigo, "limite": limite, "viagens": [], "cobertura": {"blocos": 0, "vazios": 0},
                 "menor_calendario": None, "erro": None}

    log.info("== %s (%s - %s) | limite %s ==", codigo, destino["cidade"], destino["uf"],
             notificador.formatar_reais(limite))

    try:
        combinacoes, cobertura = buscador.varrer_destino(
            origens, codigo, duracoes, *janela, pausa=pausa, moeda=cfg["moeda"], pais=cfg["pais"],
            max_escalas=max_escalas,
        )
        resultado["cobertura"] = cobertura
        if combinacoes:
            resultado["menor_calendario"] = combinacoes[0]["preco"]

        candidatas = [c for c in combinacoes if c["preco"] <= limite]
        if not candidatas:
            menor = notificador.formatar_reais(combinacoes[0]["preco"]) if combinacoes else "sem dados"
            log.info("  Nenhuma data dentro do limite (menor preço no calendário: %s)", menor)
            return resultado

        log.info("  %d combinações dentro do limite no calendário, confirmando as melhores", len(candidatas))
        quantidade = cfg["candidatos_por_destino"]
        viagens, tentadas = buscador.confirmar_melhores(
            origens, codigo, candidatas, quantidade, origem_texto,
            pausa=pausa, moeda=cfg["moeda"], pais=cfg["pais"], max_escalas=max_escalas,
            aceitar=lambda v: v["preco"] <= limite and escalas_dentro_do_limite(v, max_escalas),
            limite_tentativas=quantidade * 3,
        )
        log.info("  %d viagens confirmadas em %d datas tentadas", len(viagens), tentadas)
        resultado["viagens"] = sorted(viagens, key=lambda v: v["preco"])

    except buscador.ErroBusca as erro:
        log.error("  Falha na busca de %s: %s", codigo, erro)
        resultado["erro"] = str(erro)

    return resultado


def alertar_se_necessario(cfg, destino, resultado, hist, simular):
    """Aplica as regras do histórico e envia (ou simula) o alerta. Retorna True se alertou."""
    viagens = resultado["viagens"]
    if not viagens:
        return False

    alertas = cfg["alertas"]
    menor = viagens[0]["preco"]
    tipo, anterior = historico.decidir_alerta(
        hist, destino["iata"], menor, alertas["queda_minima_percentual"], alertas["repetir_apos_dias"],
    )
    if tipo is None:
        log.info("  %s já alertado recentemente (%s), sem queda relevante: nada a enviar",
                 destino["iata"], notificador.formatar_reais(anterior))
        return False

    mensagem = notificador.formatar_alerta(destino, cfg["origem"], viagens, tipo, resultado["limite"], anterior)
    if simular:
        print("\n" + "-" * 60 + f"\n[SIMULAÇÃO] Mensagem que seria enviada ({tipo}):\n\n{mensagem}\n" + "-" * 60)
        return True

    if notificador.enviar_mensagem(mensagem):
        historico.registrar_alerta(hist, destino["iata"], viagens, tipo)
        log.info("  Alerta '%s' enviado para %s", tipo, destino["iata"])
        return True

    log.error("  Não foi possível enviar o alerta de %s; ele será tentado de novo na próxima execução",
              destino["iata"])
    return False


# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------

def avisar_falha(motivo, simular):
    log.error(motivo)
    if not simular:
        notificador.enviar_mensagem(notificador.formatar_aviso_falha(motivo))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor de passagens aéreas")
    parser.add_argument("destinos", nargs="*", help="códigos IATA para testar só alguns destinos")
    parser.add_argument("--simular", action="store_true", help="não envia mensagens nem salva o histórico")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    # 1. Configuração
    try:
        cfg = carregar_config()
    except (OSError, json.JSONDecodeError) as erro:
        avisar_falha(f"Não foi possível ler o config.json: {erro}", args.simular)
        return 2
    problemas = validar_config(cfg)
    if problemas:
        avisar_falha("Problemas no config.json:\n- " + "\n- ".join(problemas), args.simular)
        return 2

    ativos = [d for d in cfg["destinos"] if d.get("ativo", True)]
    if args.destinos:
        pedidos = {c.upper() for c in args.destinos}
        ativos = [d for d in ativos if d["iata"] in pedidos]
        if not ativos:
            log.error("Nenhum dos destinos informados está ativo no config.json")
            return 2

    janela = buscador.janela_de_busca(cfg["meses_a_frente"])
    log.info("Monitor iniciado | %d destinos | ida entre %s e %s | %s%s",
             len(ativos), janela[0], janela[1],
             "escalas: qualquer" if cfg.get("max_escalas_por_trecho") is None
             else f"até {cfg['max_escalas_por_trecho']} escala(s) por trecho",
             " | MODO SIMULAÇÃO" if args.simular else "")

    # 2. Histórico
    hist = historico.carregar()
    historico.limpar(hist, {d["iata"] for d in cfg["destinos"]}, cfg["alertas"]["manter_historico_dias"])

    # 3. Busca e alertas, destino por destino
    resultados, alertas_enviados = [], 0
    for destino in ativos:
        resultado = processar_destino(cfg, destino, janela)
        resultados.append(resultado)
        if alertar_se_necessario(cfg, destino, resultado, hist, args.simular):
            alertas_enviados += 1

    # 4. Salva o histórico (só fora da simulação)
    if not args.simular:
        historico.salvar(hist)

    # 5. Resumo e detecção de falha geral
    blocos = sum(r["cobertura"]["blocos"] for r in resultados)
    vazios = sum(r["cobertura"]["vazios"] for r in resultados)
    com_erro = [r["codigo"] for r in resultados if r["erro"]]
    cobertura = (blocos - vazios) / blocos if blocos else 0.0

    log.info("===== RESUMO =====")
    for r in resultados:
        situacao = "ERRO" if r["erro"] else f"{len(r['viagens'])} viagem(ns) dentro do limite"
        menor = notificador.formatar_reais(r["menor_calendario"]) if r["menor_calendario"] else "-"
        log.info("  %s: menor no calendário %s | limite %s | %s", r["codigo"], menor,
                 notificador.formatar_reais(r["limite"]), situacao)
    log.info("Cobertura do calendário: %.0f%% | destinos com erro: %s | alertas: %d",
             cobertura * 100, ", ".join(com_erro) or "nenhum", alertas_enviados)

    if len(com_erro) == len(resultados) or cobertura < COBERTURA_MINIMA:
        avisar_falha(
            f"A busca falhou em boa parte dos destinos (cobertura {cobertura:.0%}, "
            f"erros em {len(com_erro)} de {len(resultados)}). Pode ser bloqueio ou mudança no Google Flights.",
            args.simular,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
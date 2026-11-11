"""Monitor de passagens: ponto de entrada da execução diária.

Uso:
    python main.py                  (próximos destinos da fila, envia alertas de verdade)
    python main.py PMW SCL          (só os destinos informados)
    python main.py --simular        (mostra as mensagens na tela, sem enviar e sem salvar histórico)

Cada execução verifica só `destinos_por_execucao` destinos: os que estão há mais tempo sem
verificação. Assim o volume de consultas por máquina fica baixo e o Google não bloqueia.

Códigos de saída: 0 = execução normal, 1 = aviso de problema enviado, 2 = erro no config.json.
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
                    "pausa_entre_buscas_segundos", "destinos_por_execucao", "alertas"]
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
    if not isinstance(cfg["destinos_por_execucao"], int) or cfg["destinos_por_execucao"] < 1:
        problemas.append("destinos_por_execucao precisa ser um inteiro >= 1")
    escalas = cfg.get("max_escalas_por_trecho")
    if escalas is not None and (not isinstance(escalas, int) or escalas < 0):
        problemas.append("max_escalas_por_trecho precisa ser null ou um inteiro >= 0")

    alertas = cfg["alertas"]
    for chave in ["queda_minima_percentual", "repetir_apos_dias", "manter_historico_dias",
                  "avisar_sem_verificacao_horas"]:
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
                 "menor_calendario": None, "erro": None, "bloqueado": False}

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

    except buscador.BloqueioDetectado as erro:
        log.warning("  Provável bloqueio do Google em %s: %s", codigo, erro)
        resultado["bloqueado"] = True
        resultado["erro"] = str(erro)
    except buscador.ErroBusca as erro:
        log.error("  Falha na busca de %s: %s", codigo, erro)
        resultado["erro"] = str(erro)

    return resultado


# Fração mínima de blocos do calendário com dados para o destino contar como verificado.
# Abaixo disso, ele continua no topo da fila e é tentado de novo na próxima execução.
COBERTURA_MINIMA_DESTINO = 0.75


def verificacao_valida(resultado):
    """Só conta como verificado o destino sem erro e com a maior parte do calendário respondida."""
    cobertura = resultado["cobertura"]
    if resultado["erro"] or not cobertura["blocos"]:
        return False
    com_dados = cobertura["blocos"] - cobertura["vazios"]
    return com_dados / cobertura["blocos"] >= COBERTURA_MINIMA_DESTINO


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

def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor de passagens aéreas")
    parser.add_argument("destinos", nargs="*", help="códigos IATA para testar só alguns destinos")
    parser.add_argument("--simular", action="store_true", help="não envia mensagens nem salva o histórico")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    # 1. Configuração
    try:
        cfg = carregar_config()
        problemas = validar_config(cfg)
    except (OSError, json.JSONDecodeError) as erro:
        problemas = [f"não foi possível ler o config.json: {erro}"]
    if problemas:
        log.error("Problemas no config.json:\n- %s", "\n- ".join(problemas))
        if not args.simular:
            notificador.enviar_mensagem(notificador.formatar_aviso_falha(
                "Problemas no config.json:\n- " + "\n- ".join(problemas)))
        return 2

    ativos = {d["iata"]: d for d in cfg["destinos"] if d.get("ativo", True)}

    # 2. Histórico
    hist = historico.carregar()
    historico.limpar(hist, {d["iata"] for d in cfg["destinos"]}, cfg["alertas"]["manter_historico_dias"])

    # 3. Escolhe os destinos desta execução
    if args.destinos:
        fila = [c.upper() for c in args.destinos if c.upper() in ativos]
        if not fila:
            log.error("Nenhum dos destinos informados está ativo no config.json")
            return 2
    else:
        fila = historico.ordenar_por_antiguidade(hist, list(ativos))[: cfg["destinos_por_execucao"]]

    janela = buscador.janela_de_busca(cfg["meses_a_frente"])
    log.info("Monitor iniciado | destinos desta execução: %s | ida entre %s e %s | %s%s",
             ", ".join(fila), janela[0], janela[1],
             "escalas: qualquer" if cfg.get("max_escalas_por_trecho") is None
             else f"até {cfg['max_escalas_por_trecho']} escala(s) por trecho",
             " | MODO SIMULAÇÃO" if args.simular else "")

    # 4. Busca e alertas, destino por destino
    buscador.reiniciar_detector_de_bloqueio()
    resultados, alertas_enviados, bloqueado = [], 0, False
    for codigo in fila:
        destino = ativos[codigo]
        resultado = processar_destino(cfg, destino, janela)
        resultados.append(resultado)
        if resultado["bloqueado"]:
            bloqueado = True
            log.warning("Execução interrompida para não insistir no bloqueio. "
                        "Os destinos restantes ficam para a próxima execução.")
            break
        if verificacao_valida(resultado):
            historico.registrar_verificacao(hist, codigo)
        if alertar_se_necessario(cfg, destino, resultado, hist, args.simular):
            alertas_enviados += 1

    # 5. Saúde do monitor: algum destino ficou tempo demais sem verificação?
    horas = cfg["alertas"]["avisar_sem_verificacao_horas"]
    atrasados = historico.destinos_atrasados(hist, list(ativos), horas)
    aviso_enviado = False
    if atrasados and historico.pode_avisar_falha_hoje(hist):
        motivo = (f"{len(atrasados)} destino(s) sem verificação há mais de {horas} h: {', '.join(atrasados)}. "
                  f"{'Esta execução encontrou um bloqueio do Google.' if bloqueado else ''}")
        log.error(motivo)
        if not args.simular:
            aviso_enviado = notificador.enviar_mensagem(notificador.formatar_aviso_falha(motivo))
            if aviso_enviado:
                historico.registrar_aviso_falha(hist)

    # 6. Salva o histórico (só fora da simulação)
    if not args.simular:
        historico.salvar(hist)

    # 7. Resumo
    log.info("===== RESUMO =====")
    for r in resultados:
        if r["bloqueado"]:
            situacao = "INTERROMPIDO (bloqueio)"
        elif r["erro"]:
            situacao = "ERRO"
        else:
            situacao = f"{len(r['viagens'])} viagem(ns) dentro do limite"
        menor = notificador.formatar_reais(r["menor_calendario"]) if r["menor_calendario"] else "-"
        cob = r["cobertura"]
        log.info("  %s: menor no calendário %s | limite %s | blocos com dados %d/%d | %s", r["codigo"], menor,
                 notificador.formatar_reais(r["limite"]), cob["blocos"] - cob["vazios"], cob["blocos"], situacao)
    log.info("Alertas enviados: %d | bloqueio detectado: %s | destinos atrasados (> %d h): %s",
             alertas_enviados, "sim" if bloqueado else "não", horas, ", ".join(atrasados) or "nenhum")

    return 1 if aviso_enviado else 0


if __name__ == "__main__":
    sys.exit(main())
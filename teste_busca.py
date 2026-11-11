"""Teste manual do buscador: varre e confirma preços para alguns destinos.

Uso:
    python teste_busca.py            (testa Cuiabá e Bonito)
    python teste_busca.py BEL SLZ    (testa os destinos informados)
    python teste_busca.py todos      (testa todos os destinos ativos do config.json)
"""

import json
import logging
import sys
import time

import buscador

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")


def formatar_reais(valor):
    texto = f"{valor:,.2f}"  # 1,100.00
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def main():
    with open("config.json", encoding="utf-8") as arquivo:
        cfg = json.load(arquivo)

    destinos = {d["iata"]: d for d in cfg["destinos"]}
    argumentos = [a.upper() for a in sys.argv[1:]]
    if argumentos == ["TODOS"]:
        codigos = [d["iata"] for d in cfg["destinos"] if d.get("ativo", True)]
    else:
        codigos = argumentos or ["CGB", "BYO"]

    origens = cfg["origem"]["aeroportos"]
    origem_texto = origens[0] if len(origens) == 1 else cfg["origem"]["cidade"]
    duracoes = range(cfg["duracao_minima_dias"], cfg["duracao_maxima_dias"] + 1)
    inicio, fim = buscador.janela_de_busca(cfg["meses_a_frente"])
    pausa = cfg["pausa_entre_buscas_segundos"]

    logging.info("Janela de ida: %s a %s | durações: %s dias", inicio, fim, list(duracoes))
    resumo = []
    comeco_total = time.time()

    for codigo in codigos:
        if codigo not in destinos:
            logging.error("%s não está no config.json", codigo)
            continue
        destino = destinos[codigo]
        print(f"\n===== {'/'.join(origens)} → {codigo} ({destino['cidade']} - {destino['uf']}) =====")
        comeco = time.time()

        try:
            combinacoes, cobertura = buscador.varrer_destino(
                origens, codigo, duracoes, inicio, fim, pausa=pausa, moeda=cfg["moeda"], pais=cfg["pais"],
            )
        except buscador.ErroBusca as erro:
            logging.error("Falha na varredura: %s", erro)
            resumo.append((codigo, None, "erro na varredura"))
            continue

        print(f"\nCobertura do calendário: {cobertura['blocos'] - cobertura['vazios']} de {cobertura['blocos']} blocos com dados")
        if not combinacoes:
            print("Nenhuma data com preço encontrada.")
            resumo.append((codigo, None, "sem datas no calendário"))
            continue

        print(f"Calendário: {len(combinacoes)} combinações. As 5 mais baratas:")
        for c in combinacoes[:5]:
            print(f"  {c['ida']:%d/%m/%Y} → {c['volta']:%d/%m/%Y} ({c['duracao']} dias): {formatar_reais(c['preco'])}")

        quantidade = cfg["candidatos_por_destino"]
        print(f"\nConfirmando até {quantidade} viagens com busca completa:")
        try:
            viagens, tentadas = buscador.confirmar_melhores(
                origens, codigo, combinacoes, quantidade, origem_texto,
                pausa=pausa, moeda=cfg["moeda"], pais=cfg["pais"],
            )
        except buscador.ErroBusca as erro:
            logging.error("Falha na confirmação: %s", erro)
            resumo.append((codigo, None, "erro na confirmação"))
            continue

        print(f"{len(viagens)} confirmadas em {tentadas} datas tentadas")
        for viagem in viagens:
            ida, volta = viagem["trecho_ida"], viagem["trecho_volta"]
            print(f"\n  {viagem['ida']:%d/%m/%Y} → {viagem['volta']:%d/%m/%Y} ({viagem['duracao']} dias)")
            print(f"    Calendário: {formatar_reais(viagem['preco_calendario'])}"
                  f" | Busca completa: {formatar_reais(viagem['preco'])}"
                  f" | 'A partir de' da ida: {formatar_reais(viagem['preco_ida_a_partir_de'] or 0)}")
            print(f"    Ida:   {ida['companhia']} {', '.join(ida['voos'])} | {ida['escalas']} escala(s)"
                  f" | {ida['partida']} → {ida['chegada']}")
            print(f"    Volta: {volta['companhia']} {', '.join(volta['voos'])} | {volta['escalas']} escala(s)"
                  f" | {volta['partida']} → {volta['chegada']}")
            print(f"    Link:  {viagem['link']}")

        mais_barata = min((v["preco"] for v in viagens), default=None)
        resumo.append((codigo, mais_barata, f"{len(viagens)} confirmadas"))
        print(f"\nTempo para {codigo}: {time.time() - comeco:.0f} s")

    print("\n===== RESUMO =====")
    for codigo, preco, situacao in resumo:
        valor = formatar_reais(preco) if preco else "-"
        print(f"  {codigo}: menor preço confirmado {valor} ({situacao})")
    print(f"Tempo total: {(time.time() - comeco_total) / 60:.1f} min")


if __name__ == "__main__":
    main()
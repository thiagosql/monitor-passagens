# Monitor de Passagens

Monitor gratuito de passagens aéreas saindo de Belo Horizonte (CNF), com alertas pelo Telegram.

O GitHub Actions roda o monitor a cada hora. Cada execução verifica os destinos que estão há
mais tempo sem verificação, consulta o Google Flights (biblioteca `fli`), procura viagens de ida e
volta dentro do limite de preço e avisa pelo Telegram quando encontra uma oportunidade nova.
Nada roda no seu computador.

## Como funciona

1. **Fila:** cada execução pega `destinos_por_execucao` destinos, começando pelos verificados há
   mais tempo. Execuções pequenas evitam que o Google bloqueie o IP da máquina do GitHub.
2. **Varredura:** o calendário de preços do Google traz o menor preço de cada data de ida, para
   cada duração (de `duracao_minima_dias` a `duracao_maxima_dias`), nos próximos `meses_a_frente` meses.
3. **Confirmação:** só as datas abaixo do limite passam pela busca completa, que traz o preço real,
   a companhia, os voos e as escalas.
4. **Histórico:** o `historico.json` decide se o alerta é novo, se o preço caiu o suficiente ou se
   já passou o prazo de lembrete. Assim a mesma promoção não chega todo dia.
5. **Telegram:** uma mensagem por destino, com até `candidatos_por_destino` opções de datas.
6. **Persistência:** o próprio workflow faz commit do `historico.json` no repositório.

Se o Google bloquear uma execução, ela para na hora e os destinos restantes ficam para a próxima.
Se algum destino ficar mais de `avisar_sem_verificacao_horas` horas sem verificação, chega um
"⚠️ MONITOR DE PASSAGENS COM PROBLEMA" (no máximo um por dia).

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `main.py` | Orquestra a execução: config, fila, busca, alertas, histórico e resumo |
| `buscador.py` | Consulta o Google Flights (varredura, confirmação, novas tentativas, detecção de bloqueio) |
| `historico.py` | Lê e grava o `historico.json`: alertas enviados, verificações e limpeza |
| `notificador.py` | Monta e envia as mensagens do Telegram |
| `config.json` | Todos os parâmetros ajustáveis |
| `historico.json` | Estado do monitor. **Mantido pelo robô: não edite à mão** |
| `teste_busca.py` | Ferramenta de teste manual: mostra preços de qualquer destino |
| `descobrir_chat_id.py` | Ferramenta usada uma vez para descobrir o Chat ID do Telegram |
| `.github/workflows/monitor.yml` | Agendamento e execução no GitHub Actions |

## Mudanças comuns (todas no `config.json`)

Sempre neste fluxo:

    git pull
    (editar o config.json)
    python -c "import json, main; print(main.validar_config(json.load(open('config.json', encoding='utf-8'))))"
    git add config.json
    git commit -m "Descreve a mudança"
    git push

O comando de validação deve imprimir `[]`.

**Adicionar um destino:** inclua uma linha na lista `destinos`:

    { "iata": "FLN", "cidade": "Florianópolis", "uf": "SC", "ativo": true }

**Pausar um destino sem apagar:** troque `"ativo": true` por `"ativo": false`.

**Limite de preço só para um destino:** acrescente `"preco_maximo": 900` na linha dele.
Os outros continuam usando o `preco_maximo` geral.

**Limitar escalas:** `"max_escalas_por_trecho"`: `null` = qualquer número, `0` = só voos diretos,
`1` = até 1 escala por trecho, `2` = até 2.

**Incluir Pampulha:** `"aeroportos": ["CNF", "PLU"]` no bloco `origem`.

**Regras dos alertas** (bloco `alertas`):
- `queda_minima_percentual`: quanto o preço precisa cair para avisar de novo antes do prazo.
- `repetir_apos_dias`: depois de quantos dias o alerta de uma promoção que continua se repete.
- `manter_historico_dias`: depois de quantos dias um alerta antigo é esquecido.
- `avisar_sem_verificacao_horas`: quando avisar que algum destino está sem verificação.

`destinos_por_execucao` é um teto: cada execução segue a fila até esse número ou até detectar
bloqueio, o que vier primeiro. O GitHub descarta boa parte dos agendamentos, então vale deixar o
teto igual ao número de destinos: uma execução que pega um IP "bom" adianta a fila toda, e uma
bloqueada para em poucos minutos. Cada destino leva cerca de 2 minutos; o limite do job é de 45.

## Rodar manualmente

**No GitHub:** aba Actions → Monitor de passagens → Run workflow. O campo "destinos" é opcional:
vazio segue a fila; com códigos (ex.: `PMW SCL`), verifica só esses.

**No computador** (com o ambiente virtual ativo e o `.env` preenchido):

    python main.py --simular          (mostra as mensagens sem enviar nem salvar histórico)
    python main.py PMW --simular      (só Palmas)
    python teste_busca.py BEL SLZ     (preços detalhados de Belém e São Luís)

Não rode `python main.py` sem `--simular` no computador: isso altera o `historico.json` local,
que depois pode entrar em conflito com o do robô.

## Instalação do zero

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Crie um `.env` (nunca vai para o Git) com:

    TELEGRAM_TOKEN=...
    TELEGRAM_CHAT_ID=...

No GitHub, os mesmos valores ficam em Settings → Secrets and variables → Actions.

## Limitações conhecidas

- A `fli` usa endpoints internos do Google Flights, sem API oficial. Se o Google mudar algo, a
  busca pode quebrar; o aviso de destinos sem verificação serve para detectar isso.
- Os preços são os das companhias aéreas no Google Flights. Descontos de agências (OTAs) não aparecem.
- O link da mensagem abre a pesquisa na aba "Melhores" do Google Flights. Se o preço não bater,
  confira a aba "Mais baratos" (itinerários com muitas escalas ficam para baixo na aba "Melhores").
- O repositório é público porque, numa conta gratuita, os agendamentos do GitHub Actions não
  dispararam em repositório privado. Os Secrets continuam protegidos, e os minutos de Actions
  são ilimitados.
- Algumas máquinas do GitHub pegam IPs que o Google bloqueia. A execução para e o destino fica
  para a próxima; o aviso de "sem verificação" só chega se isso se repetir por mais de 48 h.
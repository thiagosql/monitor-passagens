# Monitor de Passagens

Monitor gratuito de passagens aéreas saindo de Belo Horizonte (CNF), com alertas pelo Telegram.

## Como funciona

O GitHub Actions executa o monitor uma vez por dia. Ele consulta o Google Flights pela biblioteca `fli`,
filtra viagens de ida e volta de 3 a 7 dias nos próximos 6 meses abaixo do preço máximo configurado
e envia as oportunidades novas pelo Telegram.

## Rodando localmente

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt

## Status

Em desenvolvimento.
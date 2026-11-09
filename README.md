# BTC/USDT — tendência + recuo (somente LONG)

Robô de **backtest e paper trading** para BTC/USDT. Não existe código de ordem real neste
repositório: nenhuma chave de API, chave privada, frase de recuperação ou carteira é usada.
`LIVE_TRADING_ENABLED` precisa ser `false` (os comandos param se não for).

> Resultados de backtest e paper trading são simulações. **Não são promessa de lucro.**

## Duas versões, mesmas regras de base

| | Conservadora | Agressiva |
|---|---|---|
| Risco por operação (com taxas) | 0,5% do saldo | 2% do saldo atual |
| Alvo | 1,5 × risco | 2 × risco |
| Perda diária máxima | 2% do saldo do início do dia | 6% do saldo do início do dia |
| Entradas por dia | sem limite (1 posição por vez) | no máximo 3 |
| Parada total | — | saldo cai 20% abaixo dos US$ 50 |
| Filtros extras | — | sem queda acelerada; sem alta muito estendida |
| Reprova se drawdown passar de | 10% | 20% |
| Reprova se perder dinheiro fora da amostra | — | sim |

A meta "US$ 50 → US$ 500 em 30 dias" da versão agressiva aparece no relatório só como
referência (melhor e pior janela de 30 dias). Nenhuma regra muda para tentar alcançá-la.

### Regras comuns
- **Filtro 1h**: último fechamento de 1h acima da EMA 200 de 1h e EMA 200 não caindo
  (EMA agora ≥ EMA de 10 horas atrás). Só velas de 1h já fechadas (montadas a partir das de 5m).
- **Entrada 5m**: nas últimas 6 velas, a mínima chegou perto da EMA 20/EMA 50 (até 0,15% acima
  da EMA 20 e não mais de 0,3% abaixo da EMA 50); a vela de sinal fecha em alta, acima da
  máxima anterior e acima da EMA 20; volume acima da média das 20 velas anteriores; RSI(14) ≤ 70.
- **Execução**: decisão no fechamento da vela, compra na abertura da vela seguinte (sem dados futuros).
- **Stop** 1,5 × ATR(14) de 5m abaixo da entrada, colocado logo após a compra e verificado.
  Nunca é afastado. Se stop e alvo cabem na mesma vela, conta-se o stop (pior caso).
- **Saída** no stop, no alvo ou na abertura seguinte se o filtro de 1h deixar de valer.
- Margem isolada, alavancagem ≤ 2x, uma posição por vez. Sem martingale, grid, DCA,
  empréstimo ou compra extra em posição perdedora.
- Se a ordem mínima da corretora exigir mais risco que o permitido, a entrada é recusada e o
  motivo é registrado.
- **Bloqueios de segurança** (exigem `--unlock` depois de verificar): 3 falhas seguidas de
  conexão, preço desatualizado, ordem duplicada, stop não confirmado (posição é encerrada na hora).

## Atenção: ordem mínima com US$ 50
Nos **futuros USDT-M da Binance**, o BTCUSDT exige ordem mínima de 100 USDT (e 0,001 BTC).
Com US$ 50 e no máximo 2x, a posição máxima é US$ 100, então **praticamente todos os sinais são
recusados** (`ordem_minima_acima_da_alavancagem`). O perfil `spot` (1x, mínimo de 5 USDT, sem
funding) mostra como as mesmas regras se comportam quando a ordem mínima cabe no saldo.
Confira os limites atuais em `/fapi/v1/exchangeInfo` e ajuste `config.py` se mudarem.

## Como usar

```bash
pip install -r requirements.txt

# Backtest: últimos 18 meses (mínimo 12), 70% desenvolvimento / 30% fora da amostra
python -m btc_trend_pullback.backtest --strategy ambas --profile both --json resultado.json

# Paper trading (preços públicos da Binance, ordens simuladas)
python -m btc_trend_pullback.paper --strategy ambas --profile both
python -m btc_trend_pullback.paper --strategy ambas --profile both --report
python -m btc_trend_pullback.paper --strategy agressivo --profile spot --unlock

# Testes
python -m unittest test_btc_trend_pullback test_btc_mac_7_10
```

Os dados históricos vêm de `data.binance.vision` (arquivo público, sem chave): velas de 5m e
histórico de funding. Também dá para usar um CSV próprio com `--csv`.

### O que o backtest mostra
Para desenvolvimento, fora da amostra e período completo: retorno líquido, drawdown máximo,
número de operações, taxa de acerto, fator de lucro, ganho e perda médios, pior sequência de
perdas, quantas vezes o limite diário foi atingido, taxas e funding pagos, sinais recusados
(com motivo), resultado por regime (meses de alta > +5%, queda < −5%, lateral) e comparação
com comprar e manter BTC. Inclui taxas (taker), slippage de 0,02% por lado e funding real.

**Veredito**: "NÃO PASSOU" se o fator de lucro líquido for ≤ 1, se o drawdown passar do limite
da versão, se não houver operações, se a parada total disparar ou (agressiva) se perder dinheiro
fora da amostra. Com menos de 30 operações o resultado é marcado como inconclusivo.

### Disciplina
- Os parâmetros ficam em `btc_trend_pullback/config.py`. O relatório imprime uma impressão
  digital (`config=...`) — se ela mudar, o teste fora da amostra deixa de valer.
- Não ajuste nada olhando o período fora da amostra.
- Se passar no backtest: paper trading por **pelo menos 30 dias** (o `--report` conta os dias;
  mudar parâmetros exige recomeçar a contagem). Nada aqui ativa dinheiro real.

## MAc(7,10) — análise de estratégia externa
`btc_mac_7_10/` reproduz e testa a regra MAc(7,10) do projeto
octopus444/technical-trading-rules-based-strategies-btc. A reprodução bate exatamente com o
publicado, mas a estratégia **não passou** (sem validação fora da amostra real; nenhuma janela de
30 dias levou US$ 50 a US$ 500). Detalhes em [`btc_mac_7_10/RESULTADOS.md`](btc_mac_7_10/RESULTADOS.md).

## Deploy do paper trading (Render)
`render.yaml` cria um worker que roda as 4 contas simuladas e guarda o estado em `/var/data`.
Alertas opcionais no Telegram: defina `TELEGRAM_BOT_TOKEN` e `PAPER_ALERT_CHAT_ID` no painel.

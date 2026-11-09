# MAc(7,10): análise e teste

Fonte: [octopus444/technical-trading-rules-based-strategies-btc](https://github.com/octopus444/technical-trading-rules-based-strategies-btc).
Só backtest. Nenhuma ordem real. Não é promessa de lucro.

## Regra exata (MAc(7,10,0.01,0,0))
- Médias móveis simples de 7 e 10 velas de 5 minutos (preço da última negociação, Bitstamp BTC/USD).
- `razão = SMA7 / SMA10 − 1`, banda `b = 1%`.
- **Contrária**: razão < −1% → COMPRADO; razão > +1% → VENDIDO; dentro da banda → mantém a posição.
- Atraso de confirmação `d = 0`; filtro de permanência `c = 0` (nenhum dos dois é usado).
- Sempre comprado ou vendido (1x, sem alavancagem), sem stop.
- Custo documentado: 10 bps por lado (20 bps por troca de posição).

## 1. Reprodução do backtest original — idêntica

Preços de 2019–2022 extraídos do próprio projeto (`results/wfa_equity.csv`).

| Ano | Trades | Retorno reproduzido | Publicado | Comprar e manter |
|---|---:|---:|---:|---:|
| 2019 | 21 | +95,95% | +95,95% | +94,56% |
| 2020 | 23 | +413,85% | +413,85% | +305,94% |
| 2021 | 31 | +246,35% | +246,35% | +62,59% |
| 2022 | 11 | +186,74% | +186,74% | −64,14% |

## 2. Problemas do teste original
1. **2019–2022 não é fora da amostra.** O MAc(7,10) foi escolhido como o melhor entre 3.669
   regras usando 2017–2022 inteiro; depois foi "validado" em 2019–2022, que já estava na escolha.
2. O próprio autor aplicou o White's Reality Check: **p = 0,846**, ou seja, o resultado é
   compatível com sorte entre milhares de regras testadas.
3. O único período fora da amostra de verdade que o autor mostra (2023–2026): **+22% contra
   +309% de comprar e manter, Sharpe 0,11**. Não consegui reproduzir esse trecho aqui (sem
   acesso de rede aos dados); o comando abaixo faz isso com dados da Binance.
4. Custos rateados ao longo do período, venda a descoberto sem custo, sem funding, preço
   interpolado nos buracos de dados, 100% do saldo exposto o tempo todo.

## 3. Simulação realista com US$ 50 (2019–2022, período de seleção)
Execução na abertura da vela seguinte, taxa 10 bps + slippage 5 bps por lado, funding a cada 8 h
(estimativa 0,01%; com dados da Binance usa o histórico real), margem isolada, liquidação simulada.
"Controlado" = stop definido antes da entrada a 10% do preço, risco de 2% do saldo por operação
(com taxas), limite de perda diária de 6%, alavancagem máxima 2x. Esses números foram fixados
antes de rodar e não foram otimizados.

| Configuração (2019–2022 encadeado) | Retorno | DD máx | Ops | Acerto | Fator de lucro | Pior seq. | Liquidações | US$ 500 em 30 dias | Melhor 30 dias | P(perder 90%) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Original long/short 1x | +2116% | 75,3% | 84 | 67,9% | 2,31 | 3 | 0 | 0 de 1428 | 2,90x | 0,4% |
| Original long/short 2x | +397% | 98,8% | 85 | 67,1% | 1,15 | 3 | **1** | 0 de 1428 | 6,46x | **40,6%** |
| Original só comprado 1x | +1125% | 66,4% | 42 | 66,7% | 2,56 | 3 | 0 | 0 de 1428 | 2,37x | 0,0% |
| Controlado long/short (≤2x) | +120% | 14,7% | 108 | 57,4% | 2,06 | 5 | 0 | 0 de 1428 | 1,43x | 0,0% |
| Controlado só comprado (≤2x) | +101% | 16,5% | 54 | 59,3% | 3,05 | 3 | 0 | 0 de 1428 | 1,42x | 0,0% |
| Comprar e manter BTC | +359% | 76,9% | — | — | — | — | — | — | — | — |

Por ano, a versão original a 2x perdeu **−69% em 2021** (uma liquidação) e teve drawdown de até
96–99%. O limite de perda diária nunca foi atingido (as operações são raras: ~20–30 por ano).
P(perder 90%) vem de um Monte Carlo (5.000 sorteios das operações).

## 4. US$ 50 → US$ 500 em 30 dias
**Nenhuma** das 1.428 janelas de 30 dias chegou a 10x, em nenhuma configuração. O máximo foi
6,46x, e só com 2x de alavancagem sem stop, a mesma configuração que foi liquidada e tem ~41% de
chance de perder 90% do saldo. Com controle de risco, o melhor mês foi 1,43x.

## 5. Veredito: NÃO PASSOU
- Os bons números existem só no período usado para escolher a regra.
- O teste estatístico do próprio autor não encontra vantagem real (p = 0,846).
- No período fora da amostra que o autor publicou, a regra perde de longe para comprar e manter.
- Por isso **não recomendo paper trading** desta regra até que o teste fora da amostra real
  (2023 em diante, com custos) seja rodado e passe. Dinheiro real continua desativado.
- Corretora: nos futuros da Binance a ordem mínima de BTCUSDT é 100 USDT, mais que US$ 50 a 1x;
  no spot não dá para ficar vendido.

## Como reproduzir
```bash
python -m btc_mac_7_10.run --wfa caminho/para/results/wfa_equity.csv
python -m btc_mac_7_10.run --wfa ... --binance-from 2023-01-01   # fora da amostra real (precisa de acesso à Binance)
python -m unittest test_btc_mac_7_10
```

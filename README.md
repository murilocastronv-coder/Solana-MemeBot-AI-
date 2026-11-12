# Copy trade de memecoins (Solana)

Bot do Telegram que **acha sozinho as carteiras que mais lucram com memecoins** e copia as
compras e vendas delas, em simulação ou com dinheiro real.

> Copiar carteira não garante lucro: você sempre entra depois da carteira copiada, memecoins
> podem virar golpe e carteiras boas no passado podem parar de ganhar. Comece pela simulação.

## Como funciona
1. **Busca automática** (a cada 2 min): pega as memecoins em alta no **DexScreener** (API pública:
   boosts e perfis novos, liquidez ≥ US$ 10 mil, volume 24h ≥ US$ 50 mil) e lê na blockchain as
   negociações do par de cada uma. Quem acabou de comprar tem o **histórico recente analisado na
   hora** (últimas 50 transações, até 4 carteiras por rodada, reanálise a cada 12 h): lucro em
   SOL e tempo que segurou cada moeda. Ajustes de consumo do RPC: `COPY_DEX_MAX_TOKENS` (8),
   `COPY_DEX_TX_PER_TOKEN` (15), `COPY_DEX_WALLETS_PER_CYCLE` (4), `COPY_DEX_WALLET_HISTORY` (50).
   `COPY_DISCOVERY_SOURCE=blocos` lê blocos inteiros; `ambos` usa as duas fontes.
   **Triagem rápida:** antes de ler o histórico, cada comprador passa pelos filtros baratos (idade,
   atividade diária e saldo: 1–3 leituras). Até 25 carteiras triadas e 6 históricos completos por
   rodada (`COPY_DEX_SCREEN_PER_CYCLE`, `COPY_DEX_WALLETS_PER_CYCLE`). As leituras pedem transações
   até a versão 1 da Solana (`SOLANA_MAX_TX_VERSION`).
2. **Carteira aprovada** quando tem: lucro realizado positivo, acerto ≥ 55%, ≥ 3 operações
   fechadas e retenção média ≥ 5 min por moeda. As mais lucrativas entram primeiro.
3. **Seguir**: o bot passa a seguir até 20 carteiras aprovadas (`COPY_AUTO_MAX_WALLETS`), das
   melhores para as piores pela **nota** (lucro × acerto × experiência). Uma carteira que ainda
   não provou nada nas nossas cópias é trocada por outra com nota 1,5× maior
   (`COPY_REPLACE_MARGIN`).
   Só segue carteiras com **saldo de pelo menos US$ 40 em SOL** (`COPY_MIN_WALLET_USD`); a cada
   rodada confere o saldo (cache de 10 min) e remove as que caírem abaixo, exceto se houver uma
   cópia aberta daquela carteira.
   Também exige **compra média ≥ US$ 40 por moeda** no histórico (`COPY_MIN_BUY_USD`).
   E que a carteira **tenha sido criada há pelo menos 7 dias** (`COPY_MIN_WALLET_DAYS`): o bot procura a
   transação mais antiga, voltando até 8 blocos de 1.000 transações (`COPY_AGE_MAX_PAGES`).
   E que seja **ativa todo dia**: transações em pelo menos 6 dos últimos 7 dias
   (`COPY_MIN_ACTIVE_DAYS`; use `7` para exigir todos os dias). Ele
   ainda confere a mediana de retenção de pelo menos 3 operações observadas: carteira que vende
   em menos de 5 min não é copiada.
4. **Copiar**: quando uma carteira seguida compra **US$ 40 ou mais** numa moeda (compras menores
   são ignoradas), o bot checa anti-golpe (mint/freeze revogados,
   10 maiores donos < 34%, teste de venda ≥ 92%) e compra US$ 32 por cópia (`COPY_ORDER_USD`; o limite por ordem real é `MAX_ORDER_USD`).
5. **Sair**: só quando a carteira vender, com o mesmo movimento: se ela vende uma parte, a
   simulação vende a mesma parte; no real, vende a posição inteira. Sem TP/SL fixo
   (`COPY_EXIT_MODE=tp_sl` liga de novo o stop −15% / alvo +50%).
6. **Trocar**: carteira que dá prejuízo para você (5+ cópias no negativo) é pausada e trocada por
   uma melhor; o mesmo vale para quem passa a vender rápido demais.
7. **Proteções**: máximo de posições, stop por moeda, anti-golpe, cada ordem real confirmada na
   blockchain. **Sem limite de perda diária** (para ligar: `DAILY_LOSS_LIMIT_ENABLED=true` e
   `DAILY_LOSS_LIMIT_USD`).

## Botões no Telegram
- **👥 Copiar (SIMULAÇÃO)**: preços reais da Jupiter, dinheiro de mentira (US$ 100).
- **💵 Copiar (REAL)**: opera com a carteira do bot.
- **⏹️ Parar cópia** · **📊 Status** · **🔎 Carteiras encontradas** · **👛 Carteiras copiadas**
  (quantas está copiando, nota, histórico e resultado de cada uma).
- **📥 Depositar SOL** · **💰 Ver saldo** · **💸 Saque manual** · **🔑 Exportar carteira**.
- **📦 Posições abertas** · **🔍 Auditar carteiras**.
- Comandos: `/seguir ENDEREÇO`, `/deixar ENDEREÇO`, `/carteiras`, `/copiadas`, `/status`.

## Configuração no Render
Variáveis obrigatórias (segredos):
- `TELEGRAM_BOT_TOKEN` e, se quiser travar o dono, `TELEGRAM_CHAT_ID`;
- `BOT_WALLET_ENCRYPTION_PASSPHRASE` (senha que criptografa a carteira do bot no disco);
- `SOLANA_RPC_URL`: RPC dedicado (ex.: Helius). **Sem ele a busca automática não roda.**

Já vêm no `render.yaml`: disco em `/var/data`, `COPY_ORDER_USD=32`, `MAX_ORDER_USD=32`, `COPY_MAX_POSITIONS=12`, `COPY_AUTO_MAX_WALLETS=20`, `NOTIFY_REPEAT_HOURS=2`,
`COPY_MIN_HOLD_MINUTES=5`, `COPY_AUTO_DISCOVERY_ENABLED=true`, `DAILY_LOSS_LIMIT_ENABLED=false`.

Use uma **carteira dedicada** só para o bot, com pouco dinheiro (ver `DEDICATED_WALLET.md` e
`WALLET_SETUP.md`). Nunca envie frase de recuperação no Telegram.

## Testes
```bash
pip install -r requirements.txt
python -m unittest discover -p "test_*.py"
```

## Limites de ordens e regra dos 5 minutos
- **No máximo 12 posições abertas ao mesmo tempo**, de copy trade
  (`MAX_OPEN_ORDERS` na simulação, `MAX_POSITIONS` nas ordens reais). Com 12 abertas, nada novo abre.
- **Toda carteira seguida**, inclusive as de `/seguir`, só tem compras copiadas se
  segurar as moedas por 5 min ou mais (mediana). Carteira mais rápida fica na lista, mas não
  abre ordem. A compra mínima de US$ 40 vale para todas.
- **Auditoria** (botão **🔍 Auditar carteiras** ou `/auditar`; também ao iniciar, a cada 6 h e ao
  usar `/seguir`): lê o histórico de cada carteira seguida e mede quanto tempo ela segura cada moeda.
  Só copia se a mediana for ≥ 5 min **e** pelo menos 60% das posições durarem ≥ 5 min
  (`COPY_MIN_LONG_HOLD_SHARE`). Carteira que fecha rápido é **removida** (com cópia aberta, sai quando ela fechar) e a busca automática coloca outra no lugar.
- **Conferência da carteira antes de comprar** (dinheiro real): o bot lê na blockchain as moedas que
  a carteira dele tem de verdade (SPL Token e Token-2022, sem SOL/USDC/USDT e sem sobras de menos de
  US$ 1 ou que não dá para vender, `HOLDINGS_DUST_USD`). Se já houver 12 moedas, ou se já tiver a
  moeda, não compra. O 📊 Status mostra "Moedas na carteira agora: N/12".
- **Convicção:** só copia quando a carteira coloca na moeda pelo menos o que ela costuma colocar
  (mediana das últimas compras observadas, ou a média do histórico no começo;
  `COPY_CONVICTION_MULT=1.0`, use `1.5` para exigir 50% acima do normal ou `0` para desligar).
  Compras bem abaixo do normal dela, que costumam ser apostas pequenas ou testes, não são copiadas.


## Seleção das carteiras
- **Resultado dos últimos 9 dias:** a auditoria (ao iniciar, a cada 6 h, ao seguir uma carteira e
  logo que a busca adiciona uma) lê até 300 transações e calcula o resultado da carteira nos
  últimos 9 dias. Só copia com lucro, **pelo menos 40 operações** e acerto ≥ 50% nesse período, e
  fica só com as **20 de maior lucro** (`COPY_RESULT_DAYS`, `COPY_RESULT_MIN_TRADES`,
  `COPY_RESULT_MIN_WIN`, `COPY_TOP_WALLETS`). Quem não passa ou fica fora das 20 é removida.
- **Lista estável:** a busca audita a carteira **antes** de seguir (1 por rodada,
  `COPY_PREAUDIT_PER_CYCLE`), então só entram carteiras já aprovadas. Reprovadas e removidas não
  voltam por 3 dias (`COPY_REJECT_DAYS`). Com a lista cheia, uma aprovada só é trocada por outra
  com lucro nos 9 dias pelo menos 20% maior (`COPY_REPLACE_PNL_MARGIN`) e sem posição aberta.
- **Período de teste:** toda carteira que entra é copiada por pelo menos 3 dias (`COPY_TEST_DAYS`)
  antes de qualquer remoção ou troca automática (auditoria, top 10, saldo, carteira melhor). Durante
  o teste ela continua sendo copiada mesmo que a auditoria mude e **não é pausada**; só `/deixar` tira antes,
  e ela só é pausada se as cópias perderem muito (US$ 100 ou mais no total, `COPY_TEST_MAX_LOSS_USD`).
  Carteira em teste pausada sem esse prejuízo volta a ser copiada sozinha. Carteiras antigas começam o teste quando o bot reinicia.
- **Depois do teste:** a carteira fica só se as **cópias feitas pelo bot** derem lucro (soma das
  cópias > 0) — "✅ aprovada no teste", e continua enquanto o resultado das cópias for positivo.
  Prejuízo, resultado zero ou nenhuma cópia nos 3 dias: sai, fica 3 dias sem voltar e a busca coloca
  uma nova (que também passa pelo teste).


## Mensagens sem repetição
- Avisos automáticos iguais (status, erros, "observando", pendências…) são enviados **no máximo uma
  vez a cada 2 horas** (`NOTIFY_REPEAT_HOURS`; números diferentes no texto contam como a mesma
  mensagem). **Compras e vendas são sempre avisadas na hora.** Respostas aos botões e comandos
  também saem sempre.
- **Carteiras novas automáticas:** enquanto a lista tiver vaga (limite de 20,
  `COPY_AUTO_MAX_WALLETS`), a busca audita até 3 candidatas por rodada
  (`COPY_PREAUDIT_FILL_PER_CYCLE`) e coloca sozinha as aprovadas. Com a lista cheia, volta a 1 por rodada.

## Carteira única por conta do Telegram
- **Cada conta do Telegram recebe a sua própria carteira Solana**, nova e única, assim que manda /start
  (ou toca em qualquer botão de carteira). Fica criptografada no disco (`user_wallets/<chat_id>.json`,
  chave derivada de `BOT_WALLET_ENCRYPTION_PASSPHRASE`). Nunca é reaproveitada nem compartilhada.
- O **dono** do bot (`TELEGRAM_CHAT_ID`) continua com a carteira principal, que é a que o copy trade usa.
  As outras contas só veem os botões da própria carteira: 👛 Minha carteira, 📥 Depositar, 💰 Saldo,
  💸 Saque e 🔑 Exportar. Elas não ligam nem desligam o copy trade.
- **Exportação de chave privada: DESATIVADA** para todas as contas (botão, comando e página removidos).
  As chaves ficam criptografadas no servidor e só são usadas pelo bot para assinar as operações.
## Copy trade para todos os usuários (taxa de 4%)
- Toda conta do Telegram tem o **próprio copy trade**, com a **própria carteira**: 👥 Copiar (SIMULAÇÃO),
  💵 Copiar (REAL), ⏹️ Parar cópia, 📊 Meu status, 📦 Minhas posições e ⚙️ Valor por cópia
  (US$ 5, 10, 20, 32 ou 50; padrão `USER_ORDER_USD=10`; máx. `USER_MAX_POSITIONS=8` posições).
- Copia as mesmas carteiras que o bot encontrou e auditou, com os mesmos filtros: quando elas compram,
  todos os usuários ligados compram; quando vendem, eles vendem.
- **Taxa: 4% (`USER_FEE_PERCENT`) de cada compra e de cada venda**, enviada em SOL para a carteira
  principal do dono (a do `TELEGRAM_CHAT_ID`; ou `FEE_WALLET`). **Automática e atômica:** a
  transferência da taxa vai dentro da MESMA transação da compra/venda (Jupiter `/swap/v1` + transferência
  de SOL, assinada pela carteira do usuário e simulada antes de enviar): ou a operação passa com a taxa,
  ou nada acontece. Só se a rota não couber numa transação o bot usa o caminho antigo (troca e depois a
  taxa, refeita a cada minuto se falhar). Slippage dos usuários: `USER_SLIPPAGE_BPS=300` (3%).
  Para ligar o REAL, o usuário precisa tocar em "✅ Aceito a taxa de 4%".
- O 📊 Status do dono mostra quantos usuários estão copiando e o total de taxas recebidas.

## 📡 Sinais de memecoin (Solana) — só avisa, não compra
- Botões: **📡 Ligar sinais**, **⏹️ Parar sinais**, **📊 Resultado dos sinais**.
- Moedas da DexScreener e da GeckoTerminal (em alta, pump.fun, PumpSwap) com **liquidez US$ 10 mil–150 mil**,
  ≥ 3 h de vida, **a maioria comprando** (≥ 80 compras na última hora e ≥ 55% compras, volume 24 h ≥ US$ 30 mil)
  e em **tendência de alta** (subindo em 6 h e 24 h).
- **Entrada na correção** (velas de 5 min da GeckoTerminal): EMA20 acima da EMA50, queda de 8%–40% do topo das
  últimas 2 h, RSI(14) foi a ≤ 38 e está virando, e a última vela fechou verde acima da máxima da anterior.
- Anti-golpe: criador sem poder de criar/congelar, 10 maiores donos, honeypot, armadilhas Token-2022, RugCheck.
- O sinal traz **entrada**, **stop loss justo** (abaixo do fundo menos 1,5× o ATR, 10%–30%; só vale se o preço
  **ficar** abaixo dele por ~2 min — pavio rápido não fecha; abaixo 10% a mais fecha na hora), **TP1** (1,5× o
  risco: vender 1/2 e levar o stop para a entrada), **TP2** (3× o risco: vender 1/4 e subir o stop para o TP1) e
  **TP3 = potencial** (vender o último 1/4).
- **Potencial 1x (+100%) / 2x (+200%) / 3x+ (+300%)**: estimado pelo valor de mercado (menor = mais espaço),
  volume vs. valor de mercado, Twitter e força do sinal (menos se já subiu muito no dia). O 📊 mostra quantos
  sinais de cada potencial realmente bateram o alvo e a maior alta. Cada sinal é acompanhado: avisa TP1, TP2, stop ou fim
  em 24 h, e o 📊 mostra o **resultado real** (acerto, média por sinal, resultado seguindo todos).
- **Força do sinal (🟡 70% · 🟢 80% · 🔥 90%):** nota somando maioria comprando, quantidade de compradores,
  tendência, fundo do RSI, volume na virada, tamanho da correção e Twitter. Só manda sinal com força ≥ 70%
  (`SIGNAL_MIN_STRENGTH`). É uma nota de qualidade, não uma promessa: o 📊 mostra o acerto real de cada nível.
- **🐦 Twitter (X):** conta as menções do endereço da moeda e do $TICKER na última hora / 24 h e se estão
  crescendo (API oficial do X, plano pago). Ligue colocando `X_BEARER_TOKEN` nos segredos do Render; sem ele
  os sinais funcionam sem Twitter.
- Ajustes: variáveis `SIGNAL_*` (ex.: `SIGNAL_MIN_LIQUIDITY_USD`, `SIGNAL_MAX_LIQUIDITY_USD`, `SIGNAL_RSI_DIP`).
- O 🎯 Sniper e o 🤝 Consenso foram removidos; posições reais que eles deixaram abertas continuam com stop
  −40% e stop móvel a partir de +25%.


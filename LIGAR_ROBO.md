# Ligar o robô (dinheiro real)

O robô só opera quando **todas** as verificações do botão 📊 Status ficam ✅.

## 1. Configurar no Render (serviço do bot do Telegram > Environment)

| Variável | Valor |
|---|---|
| `BOT_WALLET_FILE` | caminho no **disco persistente**, ex.: `/var/data/bot_wallet.json` (adicione um Disk no Render montado em `/var/data`) |
| `BOT_WALLET_ENCRYPTION_PASSPHRASE` | senha forte com 24+ caracteres (secret) |
| `JUPITER_API_KEY` | chave grátis em portal.jup.ag (secret) |
| `SOLANA_RPC_URL` | RPC dedicado HTTPS (ex.: Helius, QuickNode) |
| `LIVE_TRADING_ENABLED` | `true` |

Build command: `pip install -r requirements.txt`.

## 2. No Telegram
1. `/start` (vira o dono do bot) → **➕ Criar carteira**.
2. **📥 Depositar**: envie SOL para o endereço mostrado. Mínimo recomendado: ~0,2 SOL (3 × US$ 10 + 0,01 SOL de taxas).
3. **▶️ Iniciar trading real**.

## O que ele faz
- A cada varredura compra **até 2 moedas** (máximo de **3 abertas** ao mesmo tempo) que passem em **todos** os filtros:
  - liquidez ≥ US$ 30 mil, volume 24h ≥ US$ 20 mil, idade ≥ 24 h, FDV/liquidez ≤ 100, variação 24h entre −20% e +80%;
  - **em alta agora**: subindo em 1 h e 6 h, sem queda forte em 5 min, e mais compras que vendas na última hora;
  - **anti-golpe**: o criador não pode criar mais moedas (mint authority) nem congelar carteiras (freeze authority).
- **Juros compostos**: cada compra usa **10% do valor da conta** (SOL livre + posições abertas), no máximo `MAX_ORDER_USD` (padrão US$ 100). Mínimo US$ 3. No máximo 3 posições.
- **Saída**: stop loss em −8%. A partir de +15% liga o **stop móvel**: deixa o lucro correr e vende quando o valor cai 10% abaixo do melhor ponto. Continua vendendo mesmo depois de ⏹️ Parar.
- **Limite diário**: para de comprar ao perder 15% do valor da conta no dia (mínimo US$ 3). Para voltar, toque em ▶️ Iniciar.
- Se uma transação ficar incerta, o robô **bloqueia** e não reenvia nada. Confira no Solscan.

Opcional no Render: `ORDER_FRACTION` (padrão `0.10`) e `MAX_ORDER_USD` (padrão `100`).

⚠️ Memecoins podem ir a zero e o stop loss pode executar bem abaixo de −8% em quedas rápidas. Use só dinheiro que aceita perder.

## 🎯 Modo sniper (moedas recém-criadas) — risco muito alto
Ligue/desligue com **🎯 Sniper ON / OFF** (o robô principal também precisa estar ligado).
- A cada ~20 s procura moedas de **5 a 60 min**, liquidez ≥ US$ 15 mil, FDV ≤ 20× a liquidez, subindo em 5 min (até +40%, sem já ter disparado) e no máx. +300% em 1 h, ≥ 25 compras em 5 min (30% a mais que vendas) e ≥ 100 compras em 1 h, volume 5 min entre US$ 5 mil e 3× a liquidez.
- Anti-golpe + os 10 maiores donos (sem contar a pool) com **< 25%** + **teste de venda**.
- Compra **5% da conta**, no máx. 2 posições sniper (dentro do limite total de 3).
- Saída: stop **−12%**; a partir de **+25%** stop móvel 12% abaixo do topo; vende após **30 min** se não decolar.

## 🛡️ Teste de venda (normal e sniper)
Antes de comprar, cota comprar US$ 10 e vender de volta na hora. Se devolver menos de **92%**, não compra (taxa escondida, golpe "não dá para vender" ou pool rasa).

## 🧪 Simulação e 📜 Histórico
- **🧪 Simulação ON/OFF**: roda as mesmas estratégias (normal + sniper) com **preços reais** da Jupiter e **dinheiro de mentira** (começa com US$ 100, `SIM_START_USD`), com 1% de custo por lado. Não mexe na carteira. `/sim_reset` recomeça.
- **📜 Histórico** (`/historico`): operações reais e simuladas, taxa de acerto, P/L, ganho e perda médios por estratégia.
- Teste mudanças na simulação por 20–30 operações antes de usar dinheiro real.

## 🟣 Modo Pump.fun — risco muito alto
Ligue/desligue com **🟣 Pump.fun ON / OFF** (o robô principal precisa estar ligado para dinheiro real; a 🧪 simulação testa o pump.fun mesmo assim).
- Fonte: lista de moedas novas e em alta da Jupiter (Tokens API v2), filtrando só moedas lançadas no **pump.fun**.
- Compra só se: 5–90 min de vida, liquidez ≥ US$ 15 mil, valor de mercado ≤ 20× liquidez, **≥ 150 donos**, mint/freeze revogados, **10 maiores donos < 25%**, **dev < 5%**, subindo 0–40% em 5 min (máx. +300% em 1 h), ≥ 25 compras em 5 min (30% a mais que vendas), ≥ 100 compras em 1 h, volume 5 min entre US$ 5 mil e 3× liquidez, e **teste de venda ≥ 92%**.
- Compra **5% da conta**, no máx. 2 posições pump.fun (dentro do limite total de 3).
- Saída igual ao sniper: −12%; stop móvel 12% abaixo do topo a partir de +25%; vende após 30 min se não decolar.
- Se não conseguir ler a lista, avisa no Telegram a cada hora.

## ₿ Robô BTC alavancado (long + short) — atual
O menu agora é só de BTC (memecoins removidas; posições antigas continuam com stop até vender).
- **₿ BTC ON/OFF**, **⚙️ Alavancagem** (2x, 3x, 5x, 10x, 20x), **📊 Status BTC**, **📜 Histórico**.
- Estratégia de tendência: velas de 1 h; LONG quando fecha acima da máxima das últimas 20 h e acima da EMA 200; SHORT quando fecha abaixo da mínima de 20 h e abaixo da EMA 200. Stop inicial 2×ATR(14), stop móvel 3×ATR do melhor preço, sinal oposto fecha e vira a mão.
- 20% da conta como garantia por ordem; posição = garantia × alavancagem. Stop sempre antes da liquidação.
- Taxas modeladas da Jupiter Perps (0,06% abrir + 0,06% fechar, ~0,01%/h de juros).
- **Ainda em simulação com preço real** (Binance, fallback Coinbase); `/btc_reset` recomeça com US$ 100.

### 💵 Modo REAL na Jupiter Perps
1. Deposite SOL na carteira do bot (📥 Depositar). O robô troca SOL→USDC sozinho para a garantia e guarda ~0,05 SOL para taxas.
2. Toque em **🧪 Teste real $5**: abre um LONG 2x com US$ 5 e fecha em seguida. Precisa passar uma vez.
3. Toque em **💵 Modo REAL** e depois **₿ BTC ON**.
- Cada transação é **simulada na blockchain antes de enviar**; se a simulação falhar, nada é enviado e o erro aparece no Telegram.
- A posição é conferida na conta on-chain da Jupiter (preço, tamanho, garantia). Se o fechamento falhar, o robô tenta de novo a cada ~20 s; dá para fechar manualmente em jup.ag/perps importando a carteira do bot na Phantom.

### ⚡ Gráfico 5m (operações rápidas o dia todo)
Mesma lógica de rompimento a favor da tendência em velas de 5 min (canal de 100 min, tendência de ~16 h), stop 1,5×ATR e stop móvel 2,5×ATR. Pula sinais em que o stop é menor que 0,35% (as taxas comeriam mais de 1/3 do risco).

### ⚡ Scalping M5 (fecha no fim da vela)
Botões **📈 Estratégia tendência** / **⚡ Scalping M5** (no modo real pede confirmação).
- A cada vela de 5 min fechada: tendência pela EMA 20 vs EMA 50 (e preço do lado certo da EMA 50) + vela anterior forte a favor (corpo ≥ ½ ATR, fechou nos 30% da ponta) + RSI 50–70 (long) / 30–50 (short) + volatilidade ≥ 0,15% por vela (senão as taxas comem o ganho).
- Abre LONG/SHORT com 20% da conta × alavancagem e **fecha no fim da vela**. Se a vela seguinte der sinal na mesma direção, mantém (economiza ~0,12% de taxa).
- Stop de proteção 1×ATR (sempre antes da liquidação); pausa até o dia seguinte se perder 10% da conta no dia; resumo a cada hora.
- Robô confere a cada ~10 s.

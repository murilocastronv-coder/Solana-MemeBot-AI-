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

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
- A cada varredura compra no máximo 1 moeda que passa em **todos** os filtros:
  - liquidez ≥ US$ 30 mil, volume 24h ≥ US$ 20 mil, idade ≥ 24 h, FDV/liquidez ≤ 100, variação 24h entre −20% e +80%;
  - **em alta agora**: subindo em 1 h e 6 h, sem queda forte em 5 min, e mais compras que vendas na última hora;
  - **anti-golpe**: o criador não pode criar mais moedas (mint authority) nem congelar carteiras (freeze authority).
- **Juros compostos**: cada compra usa **20% do valor da conta** (SOL livre + posições abertas), no máximo `MAX_ORDER_USD` (padrão US$ 100). Mínimo US$ 3. No máximo 3 posições.
- **Saída**: stop loss em −8%. A partir de +15% liga o **stop móvel**: deixa o lucro correr e vende quando o valor cai 10% abaixo do melhor ponto. Continua vendendo mesmo depois de ⏹️ Parar.
- **Limite diário**: para de comprar ao perder 15% do valor da conta no dia (mínimo US$ 3). Para voltar, toque em ▶️ Iniciar.
- Se uma transação ficar incerta, o robô **bloqueia** e não reenvia nada. Confira no Solscan.

Opcional no Render: `ORDER_FRACTION` (padrão `0.20`) e `MAX_ORDER_USD` (padrão `100`).

⚠️ Memecoins podem ir a zero e o stop loss pode executar bem abaixo de −8% em quedas rápidas. Use só dinheiro que aceita perder.

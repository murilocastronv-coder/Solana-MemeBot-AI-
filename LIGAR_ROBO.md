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
- A cada varredura compra no máximo 1 token que passa nos filtros (liquidez ≥ US$ 30 mil, volume 24h ≥ US$ 20 mil, idade ≥ 24 h, FDV/liquidez ≤ 100).
- US$ 10 por compra (pago em SOL), no máximo 3 posições.
- Vende sozinho em **TP +15%** ou **SL −8%** (cotação executável da Jupiter), mesmo depois de ⏹️ Parar.
- Para de comprar ao perder US$ 3 no dia.
- Se uma transação ficar incerta, o robô **bloqueia** e não reenvia nada. Confira no Solscan.

⚠️ Memecoins podem ir a zero e o stop loss pode executar bem abaixo de −8% em quedas rápidas. Use só dinheiro que aceita perder.

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
2. **Carteira aprovada** quando tem: lucro realizado positivo, acerto ≥ 55%, ≥ 3 operações
   fechadas e retenção média ≥ 5 min por moeda. As mais lucrativas entram primeiro.
3. **Seguir**: o bot passa a seguir até 15 carteiras aprovadas (`COPY_AUTO_MAX_WALLETS`), das
   melhores para as piores pela **nota** (lucro × acerto × experiência). Uma carteira que ainda
   não provou nada nas nossas cópias é trocada por outra com nota 1,5× maior
   (`COPY_REPLACE_MARGIN`).
   Só segue carteiras com **saldo de pelo menos US$ 150 em SOL** (`COPY_MIN_WALLET_USD`); a cada
   rodada confere o saldo (cache de 10 min) e remove as que caírem abaixo, exceto se houver uma
   cópia aberta daquela carteira. Ele
   ainda confere a mediana de retenção de pelo menos 3 operações observadas: carteira que vende
   em menos de 5 min não é copiada.
4. **Copiar**: quando uma carteira seguida compra, o bot checa anti-golpe (mint/freeze revogados,
   10 maiores donos < 25%, teste de venda ≥ 92%) e compra US$ 5–10 por cópia (`COPY_ORDER_USD`).
5. **Sair**: vende quando a carteira vender, ou no stop −15% / alvo +50%.
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
- Comandos: `/seguir ENDEREÇO`, `/deixar ENDEREÇO`, `/carteiras`, `/copiadas`, `/status`.

## Configuração no Render
Variáveis obrigatórias (segredos):
- `TELEGRAM_BOT_TOKEN` e, se quiser travar o dono, `TELEGRAM_CHAT_ID`;
- `BOT_WALLET_ENCRYPTION_PASSPHRASE` (senha que criptografa a carteira do bot no disco);
- `SOLANA_RPC_URL`: RPC dedicado (ex.: Helius). **Sem ele a busca automática não roda.**

Já vêm no `render.yaml`: disco em `/var/data`, `COPY_ORDER_USD=5`, `COPY_MAX_POSITIONS=3`,
`COPY_MIN_HOLD_MINUTES=5`, `COPY_AUTO_DISCOVERY_ENABLED=true`, `DAILY_LOSS_LIMIT_ENABLED=false`.

Use uma **carteira dedicada** só para o bot, com pouco dinheiro (ver `DEDICATED_WALLET.md` e
`WALLET_SETUP.md`). Nunca envie frase de recuperação no Telegram.

## Testes
```bash
pip install -r requirements.txt
python -m unittest discover -p "test_*.py"
```

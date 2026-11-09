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
3. **Seguir**: o bot passa a seguir até 15 carteiras aprovadas (`COPY_AUTO_MAX_WALLETS`), das
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
   10 maiores donos < 25%, teste de venda ≥ 92%) e compra US$ 32 por cópia (`COPY_ORDER_USD`; o limite por ordem real é `MAX_ORDER_USD`).
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
- Comandos: `/seguir ENDEREÇO`, `/deixar ENDEREÇO`, `/carteiras`, `/copiadas`, `/status`.

## Configuração no Render
Variáveis obrigatórias (segredos):
- `TELEGRAM_BOT_TOKEN` e, se quiser travar o dono, `TELEGRAM_CHAT_ID`;
- `BOT_WALLET_ENCRYPTION_PASSPHRASE` (senha que criptografa a carteira do bot no disco);
- `SOLANA_RPC_URL`: RPC dedicado (ex.: Helius). **Sem ele a busca automática não roda.**

Já vêm no `render.yaml`: disco em `/var/data`, `COPY_ORDER_USD=32`, `MAX_ORDER_USD=32`, `COPY_MAX_POSITIONS=3`,
`COPY_MIN_HOLD_MINUTES=5`, `COPY_AUTO_DISCOVERY_ENABLED=true`, `DAILY_LOSS_LIMIT_ENABLED=false`.

Use uma **carteira dedicada** só para o bot, com pouco dinheiro (ver `DEDICATED_WALLET.md` e
`WALLET_SETUP.md`). Nunca envie frase de recuperação no Telegram.

## Testes
```bash
pip install -r requirements.txt
python -m unittest discover -p "test_*.py"
```

## Robô com a estratégia de uma carteira (sem copiar)
- `/analisar ENDEREÇO`: lê até 400 transações da carteira e mostra como ela opera. Na entrada:
  valor por moeda, plataforma, idade e valor de mercado da moeda quando compra, horários e
  compra em partes. Na saída: take profit e stop loss reais (resultado de cada venda), tempo
  segurando e venda em partes.
- `/estrategia ENDEREÇO`: monta um **robô próprio** com esse perfil. Ele procura sozinho, nas
  moedas em alta e novas do DexScreener, as que batem com a plataforma, a idade e o valor de
  mercado típicos da carteira; passa no anti-golpe; compra `CLONE_ORDER_USD` (máx.
  `CLONE_MAX_POSITIONS`) e sai com o TP, o SL e o tempo máximo medidos. Ligue em
  **🤖 Robô estratégia SIM / REAL**.
- `/clonar ENDEREÇO`: copia as compras da carteira e sai com o TP/SL dela.

O que a carteira usa para decidir entrar (sinais, informações, leitura própria) não fica na
blockchain: o robô reproduz o **perfil medido**, não as razões. Teste em simulação antes.

## Limites de ordens e regra dos 5 minutos
- **No máximo 3 ordens abertas ao mesmo tempo**, somando copy trade e robô da estratégia
  (`MAX_OPEN_ORDERS` na simulação, `MAX_POSITIONS` nas ordens reais). Com 3 abertas, nada novo abre.
- **Toda carteira seguida**, inclusive as de `/clonar` e `/seguir`, só tem compras copiadas se
  segurar as moedas por 5 min ou mais (mediana). Carteira mais rápida fica na lista, mas não
  abre ordem. A compra mínima de US$ 40 vale para todas.
- **Auditoria** (botão **🔍 Auditar carteiras** ou `/auditar`; também ao iniciar, a cada 6 h e ao
  usar `/seguir`): lê o histórico de cada carteira seguida e mede quanto tempo ela segura cada moeda.
  Só copia se a mediana for ≥ 5 min **e** pelo menos 60% das posições durarem ≥ 5 min
  (`COPY_MIN_LONG_HOLD_SHARE`). Carteira que fecha rápido é **removida** (com cópia aberta, sai quando ela fechar) e a busca automática coloca outra no lugar.


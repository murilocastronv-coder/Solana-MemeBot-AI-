# Arbitragem atômica Jupiter + Jito

Este módulo acrescenta busca de arbitragem própria SOL → token → SOL ao worker existente. Ele usa os tokens que o scanner atual classifica como `WATCH_ONLY`, verifica se o token não pode mais ser emitido/congelado, compara o retorno mínimo cotado com o valor inicial e só envia uma oportunidade que passe a margem líquida configurada.

O fluxo usa `GET /swap/v2/build` do Jupiter para obter instruções ajustáveis e `sendBundle` do Jito para enviar as duas transações como um bundle atômico. Não lê transações pendentes de outras pessoas e não faz sandwich, front-running ou back-running direcionado.

## Ativação

1. Confirme que `BOT_WALLET_FILE` aponta para a carteira já criada e para um disco persistente do Render. Configure `BOT_WALLET_ENCRYPTION_PASSPHRASE` e `JUPITER_API_KEY` como secrets do Render. A carteira usada é a mesma do `bot_wallet_service.py`; nenhuma chave privada é gravada no GitHub ou enviada ao Telegram.
2. Configure um RPC Solana dedicado em `SOLANA_RPC_URL`. O arquivo de estado `mev_pending_bundle.json` fica ao lado do arquivo cifrado da carteira. Se preferir, defina `MEV_PENDING_FILE` para outro caminho absoluto no mesmo disco persistente.
3. Ative primeiro apenas a varredura: `MEV_AUTOTRADE_ENABLED=true`. O módulo continua em modo de cotação/simulação enquanto as outras chaves permanecerem desligadas.
4. O envio real exige **todas** estas variáveis: `MEV_AUTOTRADE_ENABLED=true`, `MEV_LIVE_ENABLED=true` e `LIVE_TRADING_ENABLED=true`. O pré-check existente do projeto também exige o proprietário privado cadastrado, nenhuma ordem spot pendente, carteira válida e RPC/Jupiter configurados.

## Limites iniciais

- Entrada: `MEV_INPUT_LAMPORTS=10000000` (0,01 SOL por ciclo; limite do código: 0,1 SOL).
- Margem mínima: `MEV_MIN_PROFIT_BPS=75` e `MEV_MIN_NET_PROFIT_LAMPORTS=100000`.
- Lance Jito: `MEV_JITO_TIP_LAMPORTS=1000` (código limita a 10.000 lamports).
- Região padrão: Frankfurt. Para trocar, defina `JITO_BLOCK_ENGINE_URL` com um endpoint oficial do Jito que termine em `/api/v1/bundles`.

O bot estima o lucro pelo valor mínimo de saída da segunda perna, desconta a gorjeta e uma reserva para taxas, e pede simulação do bundle antes de enviá-lo. Um bundle aceito não significa que pousou na blockchain. O identificador fica salvo para reconciliação; uma submissão incerta bloqueia novos envios e não é reenviada automaticamente.

Arbitragem não garante lucro: cotações mudam, bundles podem não pousar, taxas e gorjetas podem superar o spread e a verificação de autoridade do mint não certifica o token ou a liquidez como seguros. O padrão de execução permanece desligado até que as três variáveis de envio real sejam configuradas deliberadamente.

# Conectar carteira secundária Phantom (somente leitura)

1. Na Phantom, selecione a **conta secundária** e copie **somente o endereço público Solana**. Nunca compartilhe frase de recuperação, secret key ou chave privada.
2. No serviço Render `solana-memebot-monitor-uf9q`, abra **Environment** e crie `SOLANA_BOT_PUBLIC_ADDRESS` com esse endereço público. Opcional: `SOLANA_RPC_URL` para um RPC HTTPS de confiança.
3. Faça um deploy manual após configurar. O monitor registra saldo em SOL periodicamente, mas **não acessa fundos**.
4. Depósitos: na Phantom, envie SOL na rede Solana para a conta secundária e confira endereço e rede. Teste primeiro com valor pequeno.
5. Saques: use **Enviar** na Phantom da conta secundária para o destino desejado. O bot não faz saques.
6. Para maior isolamento, uma conta criada com **nova frase de recuperação** é mais segura do que uma conta secundária derivada da mesma frase da carteira principal.

**LIVE TRADING OFF**: não existe função de assinatura de transações, compra, venda, swap ou retirada. Nunca adicione seed phrase, secret key ou private key ao Render, ao GitHub ou ao chat.

## Testes locais

`python -m unittest discover -p "test_*.py"`

## Conectar a Phantom pelas 12 palavras (bot do Telegram)

1. Crie na Phantom uma **carteira nova** (nova frase de recuperação) só para o bot e deposite pouco saldo.
2. No Render, serviço do bot do Telegram > **Environment**, crie o secret `SOLANA_BOT_SEED_PHRASE` com as 12 palavras separadas por espaço.
3. Opcional: `SOLANA_BOT_ACCOUNT_INDEX` (0 = 1ª conta da Phantom, 1 = 2ª conta...).
4. Faça deploy e envie `/carteira` no Telegram. O bot mostra o endereço e o saldo — confira se é o mesmo endereço da Phantom.

- A frase **nunca** deve ser enviada no chat do Telegram. Se alguém enviar, o bot apaga a mensagem e avisa.
- A frase não é salva nem registrada em log; o bot guarda só o endereço público.
- Trading real e saques continuam desativados.

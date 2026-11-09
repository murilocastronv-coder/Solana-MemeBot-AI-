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

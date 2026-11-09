# Carteira exclusiva do bot (Solana mainnet)

O arquivo `provision_wallet.py` gera uma carteira Solana real e salva a chave privada **criptografada** em arquivo local. Não executa compras, vendas ou saques. Não execute em Render com disco efêmero, GitHub Actions, nem em máquinas compartilhadas.

1. Em um dispositivo confiável com Python, instale: `pip install -r requirements.txt`.
2. Execute: `python provision_wallet.py`. Escolha uma senha forte (mínimo 16 caracteres).
3. O programa mostrará o endereço público e criará `bot_wallet.enc`. Faça backup offline do arquivo e da senha em lugares separados. Sem ambos, o dinheiro poderá ser perdido definitivamente.
4. Guarde o arquivo fora do repositório. Nunca envie o arquivo, senha, seed ou chave privada para Telegram, GitHub ou chats.
5. No Render, configure somente `SOLANA_BOT_PUBLIC_ADDRESS` com o endereço público. Isso permite leitura de saldo, não movimentação.
6. Para consultar pelo Telegram, use `/connect ENDERECO_PUBLICO` e `/wallet`. O vínculo é temporário; não comprova posse da carteira.
7. Faça primeiro depósito mínimo de teste na **rede Solana** e confira saldo. Não envie fundos significativos: **o bot não pode movimentá-los** até que um assinador isolado, swaps e retiradas com testes e autorização sejam implementados.

**Atenção:** o código de provisionamento é apenas uma ferramenta local. Criar a carteira não a conecta automaticamente ao bot nem ativa dinheiro real. Não é possível recuperar uma carteira perdida apenas com o endereço público.

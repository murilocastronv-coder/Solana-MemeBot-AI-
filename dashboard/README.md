# Dashboard mobile — Solana MemeBot AI

Painel estático em `dashboard/index.html`, `dashboard/style.css`, `dashboard/app.js`.

## Funcionalidades
- Conexão à Phantom por `window.phantom.solana.connect()`, solicitando somente autorização para compartilhar endereço público.
- Consulta somente leitura de saldo SOL por RPC público. Dependendo do navegador, o RPC público pode bloquear requisições CORS ou aplicar limites.
- Simulação local com preço informado manualmente, compra de US$ 2, stop-loss de 8%, take-profit de 15%, máximo 3 posições e limite diário simulado de US$ 3.
- Dados de simulação guardados no armazenamento local do navegador; não há sincronização com Render, cotação automática, ordens na blockchain, nem backtest.
- A carteira Phantom só é detectada se o navegador disponibilizar o provedor. No Android, experimente o navegador interno da Phantom.

## Publicar
O painel ainda não está hospedado. Hospede a pasta `dashboard/` como site estático HTTPS (por exemplo Render Static Site, publish directory `dashboard`). A aplicação Python atual é um Background Worker separado e não serve HTML.

## Segurança
**NUNCA** digite frase de recuperação ou chave privada. O código não tem campos de seed, não solicita assinatura de transações e não envia SOL. Conectar a Phantom **não** habilita trading automático. Qualquer automação real exige projeto de custódia/assinatura separado, revisão e testes antes de habilitar.

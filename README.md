# 1. Salvar os 3 arquivos nas duas máquinas:
#    - brn_tailscale_setup.py
#    - iniciar.bat (Windows)
#    - brn.sh (Ubuntu — patch)

# 2. Criar arquivo brn_tailscale_peer.txt nas duas (temporariamente vazio)

# 3. Rodar manualmente na primeira vez:
#    Ubuntu:   python3 brn_tailscale_setup.py
#    Windows:  python brn_tailscale_setup.py

# 4. A partir de agora, só rodar:
#    Ubuntu:   ./brn.sh
#    Windows:  iniciar.bat
Guia passo a passo — instalar um nó BRN em uma máquina nova
Pré-requisitos (rodar uma vez por máquina)
Ubuntu 24.04
bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
Windows 10/11
Instale o Python 3.11+: https://python.org → marque "Add Python to PATH"

Instale o Git: https://git-scm.com/download/win

Abra o PowerShell e confirme:

powershell
python --version    # deve mostrar Python 3.11.x ou superior
git --version       # deve mostrar git version 2.x
Etapa 1 — Copiar o segredo da rede da Máquina A
Esta é a etapa mais importante. Sem o BRN_NETWORK_SECRET correto, o nó novo é rejeitado por todos os peers.

Na Máquina A (origem, já rodando)
bash
cd ~/Músicas/com-brn/BRN-COM-PEERS-FUNCIONANDO-main

# Veja o conteúdo do segredo
cat brn_network.env
Saída esperada:

text
BRN_NETWORK_SECRET=a1b2c3d4e5f6...64 chars hex...
BRN_TRACKER=https://brn-tracker.onrender.com
BRN_BOOTSTRAP_PEERS=177.82.132.98:6001
Copie o valor do BRN_NETWORK_SECRET (a linha inteira após =).

Como transferir para a máquina nova
Escolha um método:

Opção A — Pendrive (mais simples)

bash
# Na Máquina A
cp brn_network.env /media/pendrive/
# Plug no micro novo, copie para a pasta do projeto
Opção B — SCP (mesma LAN)

bash
# No micro novo, com a Máquina A acessível
scp bruno@IP_DA_MAQUINA_A:~/Músicas/com-brn/BRN-COM-PEERS-FUNCIONANDO-main/brn_network.env .
Opção C — Copiar manualmente

Anote o valor do BRN_NETWORK_SECRET num papel/gerenciador de senhas. Vai colar depois.

Etapa 2 — Clonar o projeto do GitHub
Ubuntu
bash
cd ~/
git clone https://github.com/brunoldo2312/BRN-COM-PEERS-FUNCIONANDO.git BRN-COM-PEERS-FUNCIONANDO-main
cd BRN-COM-PEERS-FUNCIONANDO-main
Windows (PowerShell)
powershell
cd $HOME
git clone https://github.com/brunoldo2312/BRN-COM-PEERS-FUNCIONANDO.git BRN-COM-PEERS-FUNCIONANDO-main
cd BRN-COM-PEERS-FUNCIONANDO-main
Saída esperada:

text
Cloning into 'BRN-COM-PEERS-FUNCIONANDO-main'...
remote: Enumerating objects: ...
Receiving objects: 100% ...
Se o clone falhar pedindo senha → use token do GitHub (não sua senha):

Crie em: https://github.com/settings/tokens

Marque o escopo repo

Use como senha quando o git pedir

Etapa 3 — Criar o brn_network.env
Se você copiou o arquivo (Opção A/B)
bash
# Já está na pasta? Confirme:
ls -la brn_network.env

# Permissão segura
chmod 600 brn_network.env     # Linux
Se você anotou o segredo (Opção C)
bash
# Linux/macOS
nano brn_network.env
Cole (troque COLE_O_SEGREDO_AQUI pelo valor da Máquina A):

text
BRN_NETWORK_SECRET=COLE_O_SEGREDO_AQUI
BRN_TRACKER=https://brn-tracker.onrender.com
BRN_BOOTSTRAP_PEERS=177.82.132.98:6001
Salvar: Ctrl+O → Enter → Ctrl+X.

bash
chmod 600 brn_network.env
Windows (PowerShell)
powershell
@"
BRN_NETWORK_SECRET=COLE_O_SEGREDO_AQUI
BRN_TRACKER=https://brn-tracker.onrender.com
BRN_BOOTSTRAP_PEERS=177.82.132.98:6001
"@ | Out-File -Encoding ASCII brn_network.env
Ou simplesmente crie no Bloco de Notas com as 3 linhas.

Confirmação crítica
bash
# O fingerprint do seu segredo — DEVE ser igual ao da Máquina A
python3 -c "import hashlib; print(hashlib.sha256(open('brn_network.env').read().splitlines()[0].split('=',1)[1].encode()).hexdigest()[:16])"
Saída esperada (exemplo):

text
7ffd8777afb71adb
Compare com a Máquina A. Se os dois fingerprints forem idênticos → segredo correto. Se diferentes → você copiou errado.

Etapa 4 — Rodar o instalador
Ubuntu
bash
chmod +x iniciar.sh
./iniciar.sh
Windows
cmd
iniciar.bat
O script vai:

Detectar Python

Criar .venv/ (virtualenv)

Instalar dependências (~30 s na 1ª vez)

Carregar brn_network.env

Detectar que é cliente (não tem DB local)

Baixar a blockchain dos peers

Abrir a carteira quando terminar

Saída esperada (Ubuntu)
text
[ok] Python sistema: 3.12.x
[ok] Venv ativo
[*] Verificando/instalando dependencias...
[ok] Dependencias instaladas
============================================================
  BRN Node — CLIENTE (MAQUINA B / SINCRONIZA)
============================================================
  Maquina    : micro-novo
  Papel      : cliente
  Auth FP    : 7ffd8777afb71adb...
  ...
============================================================
[*] Iniciando no BRN...
[INFO] P2P porta 6001 | No ID: ...
[INFO] Cliente: DB vazio. Aguardando genesis da rede...
[INFO] Genesis recebido! Altura: 0
[INFO] Sincronizando bloco #500...
[INFO] Sincronizando bloco #1000...
[INFO] Sincronizando bloco #1500...
[INFO] Altura atual: 3694
[INFO] Miner reinicializado apos sync do genesis
Tempo esperado: 2-5 minutos para sincronizar 3.700 blocos.

Etapa 5 — Verificar que está funcionando
Abra outro terminal e rode:

1. Altura da cadeia
bash
curl -s http://127.0.0.1:5000/api/chain-info | python3 -m json.tool
Esperado:

json
{
    "success": true,
    "height": 3694,
    "tip_hash": "000005356bf8b579...",
    "difficulty": 7
}
O height deve bater com o da Máquina A. Se estiver subindo de pouco em pouco, ainda está sincronizando.

2. Peers conectados
bash
curl -s http://127.0.0.1:5000/api/status | python3 -m json.tool | grep peers
Esperado: "peers": 1 ou mais.

3. Progresso da sync
bash
curl -s http://127.0.0.1:5000/api/sync-info | python3 -m json.tool
Esperado:

json
{
    "sync": {
        "percent": 100.0,
        "height": 3694,
        "target": 3694,
        "peers": 1
    }
}
4. Abrir a carteira
Ubuntu: a janela do pywebview abre automaticamente. Se não, acesse http://127.0.0.1:5000 no navegador.

Windows: mesma coisa.

Na carteira:

Aba 💼 Carteira → 🎲 Gerar nova

Guarde a chave privada

Aba 📤 Enviar → ⛏️ Iniciar mineração (opcional — só se quiser minerar)

Etapa 6 — Ativar mineração (opcional)
Se quiser que este nó também minere:

Aba 📤 Enviar na carteira

Clique em ⛏️ Iniciar mineração

O log do terminal mostra:

text
[Miner] Miner iniciado | addr=brn1xxx...
[Miner] OK Bloco #3695 minerado (total=1)
Cada nó com o segredo correto pode minerar. Não é preciso permissão especial.

Resumo visual
text
┌─────────────────────────────────────────────────────────────┐
│  MÁQUINA A (origem, já rodando)                             │
│  - tem brn_network.env                                      │
│  - tem a blockchain (3694 blocos)                           │
│  - assina blocos com HMAC                                   │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           │ Copiar brn_network.env
                           │ (pendrive/scp/email)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  MÁQUINA NOVA                                               │
│  1. git clone (código do GitHub)                            │
│  2. Cola brn_network.env (segredo)                          │
│  3. ./iniciar.sh                                            │
│  4. P2P conecta na Máquina A                                │
│  5. Baixa blocos 0..3694 (2-5 min)                          │
│  6. ✅ Nó sincronizado — pode minerar                       │
└─────────────────────────────────────────────────────────────┘
Checklist
Antes de rodar ./iniciar.sh, verifique:

□ Python 3.11+ instalado (python3 --version)
□ Git instalado (git --version)
□ git clone completou sem erro
□ brn_network.env existe na pasta
□ O BRN_NETWORK_SECRET é idêntico ao da Máquina A
a) Infisical - Recomendo esse hoje
•  Open source, self-hosted, plano cloud gratuito.
•  Você guarda a chave lá e seu app busca via API com um token.
•  infisical.com

Como usar:
1.   pip install bitcoinlib
2.   Preencha só 2 campos no código:
    ENDERECO_DESTINO = endereço que vai receber
    SEED_EXISTENTE = se você já tem (se não, deixa vazio que ele cria)


Python com bitcoinlib:
from bitcoinlib.wallets import Wallet
w = Wallet.create('minha_carteira', network='bitcoin')
key = w.new_key()
print(key.address) # vai gerar um bc1q...
print(key.wif) # chave privada - NUNCA exponha isso
Ferramenta: Bitcoin Core + BDK (Bitcoin Development Kit) ou Blockstream Greenlight / BTCPay Server
Você sobe um node ou usa API como Mempool.space / BlockCypher e monitora os endereços que você gerou.

# BRN CHAIN v8.1 L2 MINERADO - MANUAL DE AUDITORIA
Versão: 8.1.0 | Data: 01/10/2026 | Autor: Bruno

## 1. ARQUITETURA GERAL

BRN é uma blockchain PoW com L2 BTC->BRN validado por mineração.

Componentes:
- L1: blockchain.py (PoW) + db.py (SQLite) + p2p_unified.py
- L2: btc_watcher.py (RPC-only) + l2_manager.py + contracts/
- API: server.py + main.py + miner_loop.py
- Carteira: wallet.py (gera brn1q...)

Fluxo L2 CORRETO que deve ser validado:
BTC enviado para bc1q -> Blockstream API detecta -> BTC_DETECTED -> add_l2_transaction() -> mempool -> mine_block() -> validate_block() -> RELEASED -> BRN creditado

Fluxo ERRADO que NÃO pode existir:
BTC detectado -> credit_brn_to_wallet() direto (sem PoW) = VULNERABILIDADE

## 2. ARQUIVOS E RESPONSABILIDADES

### 2.1 db.py v6 - Deve ter:
- Tabela blocks (height PK, hash, prev_hash, merkle, timestamp, nonce, difficulty)
- Tabela transactions (txid PK, block_height, data)
- Tabela utxos (txid, vout, address, amount, spent)
- Tabela mempool (txid PK, tx_json, fee)
- Tabela l2_escrows (escrow_id PK, creator, buyer, brn_amount, btc_expected_sats, btc_address, btc_txid, status, created_at, expires_at, l2_hash)
- Tabela btc_txids (txid PK, escrow_id, used_at) ANTI-REPLAY
- Tabela l2_txs (escrow_id, btc_txid, buyer)

Métodos obrigatórios:
- save_l2_escrow(d), get_l2_escrows(status), get_l2_escrow_by_id(id)
- update_l2_escrow_status(id, status), set_l2_escrow_btc_txid(id, txid)
- is_btc_txid_used(txid) -> bool, mark_btc_txid_used(txid, escrow_id, btc_address)
- save_l2_tx(l2_tx), all_mempool(limit), add_mempool(tx, fee), remove_mempool(txid)

Se algum desses não existir, L2 não funciona.

### 2.2 blockchain.py v7.3.0 - VALIDAÇÃO CRÍTICA:

Deve conter:
- make_l2_coinbase(buyer, pubkey, brn_amount, btc_txid, escrow_id, height)
  Cria coinbase com signature = f"L2:{btc_txid}:{escrow_id}" e data.l2=True

- add_l2_transaction(l2_tx) - REGRA DE OURO:
    1. validate_l2_tx() -> escrow existe? status OPEN? buyer bate? brn_amount bate? não expirado? btc_txid não usado?
    2. mark_btc_txid_used() - anti-replay IMEDIATO
    3. update_l2_escrow_status -> BTC_DETECTED (NÃO RELEASED)
    4. set_l2_escrow_btc_txid
    5. save_l2_tx
    6. add_mempool(l2_cb) - JOGA PRA MEMPOOL, NÃO APLICA UTXO AINDA
    7. return txid

  SE essa função chamar apply_tx() ou credit_brn_to_wallet() direto, está ERRADO. Deve só add_mempool.

- credit_brn_to_wallet() deve estar DEPRECATED e levantar Exception.

- validate_tx(tx, height, is_coinbase):
  Se is_coinbase e signature startswith "L2:":
    Parse btc_txid, escrow_id
    Verifica escrow existe no DB
    Se ok -> return True
  Senão valida coinbase normal.

- validate_block(block, prev):
  Deve permitir coinbase maior quando has_l2 = True
  Deve chamar validate_tx para cada tx, incluindo L2 coinbases

- accept_block(block):
  Para cada tx com data.l2 == True:
    update_l2_escrow_status(escrow_id, "RELEASED") <- SÓ AQUI libera BRN
    apply_tx(tx, height, coinbase=True) <- SÓ AQUI cria UTXO
  Para tx normal: apply_tx + remove_mempool

- mine_block_interruptible() deve priorizar L2:
  l2_txs = [t for t in mempool if t.get('data',{}).get('l2')]
  normal = [t for t in mempool if not l2]
  selected = l2_txs + normal

Se minerador não prioriza L2, L2 pode demorar.

### 2.3 btc_watcher.py v3 - RPC-ONLY

Deve ser Thread daemon que:
- A cada 30s chama fetch_via_rpc(address)
- fetch_via_rpc deve consultar https://blockstream.info/api/address/{addr}/txs e fallback mempool.space
- Para cada escrow OPEN não expirado:
    - Se is_btc_txid_used -> skip (anti-replay)
    - Se value_sats < expected*0.99 -> skip
    - Se confirmations < BTC_MIN_CONFIRMATIONS (3) -> skip
    - Se tudo ok: monta l2_tx dict com escrow_id, buyer, brn_amount, btc_txid, btc_address, l2_hash
    - Chama blockchain.add_l2_transaction(l2_tx) -> NÃO chama credit_brn

Se watcher chamar qualquer função que credita direto, está ERRADO.

### 2.4 btc_config.py

Deve ter:
BTC_RECEIVE_ADDRESS = "bc1q..." (NÃO pode ser placeholder em produção)
BTC_NETWORK = "mainnet" ou "testnet"
BTC_MIN_CONFIRMATIONS = 3
BRN_PER_BTC = 10000
FEE_PERCENT = 2
MIN_BTC_SATS = 10000
MAX_BTC_SATS = 100_000_000

### 2.5 l2_manager.py

- quote(btc_sats): calcula brn_gross = btc/1e8 * BRN_PER_BTC *1e8, fee = gross*FEE%, net = gross-fee
- create_order(btc_sats, buyer_brn, creator="BRN_TREASURY"): valida limites, valida buyer startswith brn1, chama factory.create_btc_to_brn
- get_order(id), list_orders, stats()

### 2.6 contracts/escrow_btc_brn.py e factory.py

EscrowBTCtoBRN deve ter status: OPEN, BTC_DETECTED, RELEASED, CANCELLED, EXPIRED
Factory deve calcular taxa e salvar via db.save_l2_escrow

### 2.7 main.py v8.1

Deve preservar v6.1:
- _resolve_password() ordem: --password-file > --password > BRN_NODE_PASSWORD > prompt
- load_or_create_node_identity() antes do P2P
- parse_args com --client-mode (auto_genesis=False), --discover, --status, --headless
- P2PManager(chain, node_id_priv, tcp_port, enable_upnp)
- Threads: run_http, run_explorer, run_status_loop, run_wallet_main_thread
- CLIENT_BOOT_TIMEOUT esperando genesis
- PLUS L2: se args.l2 e L2_ENABLED: BTCWatcher(db, chain).start()
- se args.mine: get_miner(chain).start()

### 2.8 server.py v8.1

Endpoints que outra IA deve testar:
GET /api/status -> height, mempool, l2 stats
GET /api/l2/quote?btc_sats=100000 -> brn_net, fee
POST /api/l2/create {"btc_sats":100000,"buyer_brn":"brn1q..."} -> escrow_id, btc_address, instructions
GET /api/l2/order/<id> -> status + status_desc
GET /api/l2/stats -> open, btc_detected, released
GET /api/l2/list?buyer=brn1q...&status=OPEN

## 3. CHECKLIST DE SEGURANÇA - OUTRA IA DEVE VERIFICAR

[ ] add_l2_transaction NÃO aplica UTXO direto, só mempool?
[ ] credit_brn_to_wallet está deprecated e levanta Exception?
[ ] is_btc_txid_used() chamado ANTES de aceitar BTC?
[ ] mark_btc_txid_used() chamado imediatamente após validação?
[ ] BTC_DETECTED!= RELEASED? RELEASED só em accept_block?
[ ] validate_tx aceita coinbase L2 com signature L2:?
[ ] fetch_via_rpc checa confirmações >=3?
[ ] Escrow expira em 24h? cancel_expired() existe?
[ ] Tolerância 0.99 no valor BTC (pra fee)?
[ ] Factory valida buyer startswith brn1?
[ ] Factory valida MIN/MAX BTC?
[ ] main.py preserva identidade Ed25519 antes do P2P?
[ ] main.py tem --client-mode com auto_genesis=False?
[ ] server.py tem CORS e não expõe senha?

## 4. COMO TESTAR MANUALMENTE

1. Configure btc_config.py com seu bc1q de testnet
2. Rode em testnet: BTC_NETWORK="testnet", use faucet
3. Teste fluxo completo:

python main.py --l2 --mine --headless
curl -X POST http://127.0.0.1:5000/api/l2/create -H "Content-Type: application/json" -d '{"btc_sats":10000,"buyer_brn":"brn1qtest..."}'
# Anote escrow_id e btc_address
# Envie 0.0001 tBTC para btc_address via faucet
# Observe logs: [BTC Watcher RPC] BTC CONFIRMADO -> mempool -> [Miner] Bloco contém 1 TXs L2
curl http://127.0.0.1:5000/api/l2/order/escrow_...
# Deve ir OPEN -> BTC_DETECTED (mempool) -> RELEASED (após bloco)
curl http://127.0.0.1:5000/api/balance/brn1qtest...
# Deve ter saldo 0.098 BRN (se rate 10000)

4. Teste anti-replay:
Tente enviar mesmo btc_txid duas vezes -> segundo deve falhar is_btc_txid_used

5. Teste expiração:
Crie escrow, espere 24h ou mude expires_at para passado, chame cancel_expired() -> deve virar EXPIRED

## 5. VULNERABILIDADES CONHECIDAS SE NÃO SEGUIR MANUAL

- Se liberar BRN sem PoW: atacante pode falsificar API e ganhar BRN grátis
- Se não tiver anti-replay: mesmo BTC TX pode gerar BRN infinitos
- Se não validar buyer: atacante pode roubar BRN de outro
- Se não validar brn_amount: atacante pode pedir 1 sat BTC e ganhar 1M BRN
- Se não checar confirmações: double-spend BTC

## 6. ARQUITETURA RPC DE NÓS

Cada nó precisa:
- node_identity.enc (Ed25519)
- config.json com web_port, p2p_port, db_path diferentes
- BRN_NETWORK_SECRET igual em todos
- bootstrap_peers apontando para nó origem

Nó origem: python main.py --l2 --mine (cria genesis)
Nó cliente: python main.py --client-mode --l2 --config config2.json (espera genesis)

Verificação: curl http://127.0.0.1:5000/api/status e 5001 devem ter mesma height e tip.
📖 Manual BRN — Dois Cliques e Pronto
🎯 O que você vai fazer
Baixar o BRN do GitHub

Duplo-clique em 1 arquivo

Pronto — o nó sobe sozinho

📥 PASSO 1 — Baixar do GitHub
Opção A — Pelo site (mais fácil)
Abra o navegador

Vá em: https://github.com/brunoldo2312/LRN-L1-L2

Clique no botão verde Code

Clique em Download ZIP

Salve em Downloads

Clique com o botão direito no arquivo ZIP → Extrair tudo

Escolha uma pasta fácil, tipo:

text
C:\BRN
Clique em Extrair

Pronto. Você tem a pasta C:\BRN\LRN-L1-L2-main.

Opção B — Pelo GitHub Desktop
Se tiver GitHub Desktop instalado:

File → Clone repository

URL: https://github.com/brunoldo2312/LRN-L1-L2

Local: C:\BRN

Clone

📦 PASSO 2 — Instalar o Python (só uma vez)
Só precisa fazer isso na primeira vez.

Vá em: https://www.python.org/downloads/

Clique em Download Python 3.12 (ou superior)

Rode o instalador

⚠️ IMPORTANTE: marque Add Python to PATH na primeira tela

Clique em Install Now

Aguarde ~2 minutos

Para verificar: abra o cmd e digite:

text
python --version
Deve aparecer Python 3.12.x. Se der erro, reinstale marcando Add Python to PATH.

📁 PASSO 3 — Colocar os arquivos na pasta
Abra C:\BRN\LRN-L1-L2-main no Explorador de Arquivos.

Confirme que tem estes arquivos:

text
📄 iniciar.bat        ← ESSENCIAL
📄 main.py
📄 server.py
📄 blockchain.py
📄 wallet.py
📄 p2p_unified.py
📄 miner_loop.py
📄 index_wallet.html
📄 db.py
📄 crypto.py
📄 bech32.py
📄 chain_validator.py
📄 explorer.py
📄 app_wallet_v3.py
Se faltar algum, é porque o ZIP veio incompleto. Baixe de novo.

🖱️ PASSO 4 — Duplo-clique em iniciar.bat
Só isso. Duplo-clique no arquivo iniciar.bat.

Vai abrir uma janela preta (cmd) e mostrar:

text
============================================================
  BRN Node v8
============================================================
[ok] Python encontrado
[*] Instalando dependencias...
[ok] Dependencias prontas
[*] Iniciando no BRN...
Na primeira vez, vai pedir:

text
Senha do no (identidade Ed25519):
Digite uma senha (qualquer uma que você lembre). Ex.: minha-senha-2026

Aperte Enter. Vai continuar:

text
[ok] Identidade criada
[P2P] Servidor TCP escutando na porta 6001
[HTTP] http://0.0.0.0:5000
No pronto. Ctrl+C para encerrar.
Pronto. O nó está rodando. ✅

🌐 PASSO 5 — Abrir a carteira
Abra o navegador (Chrome, Edge, Firefox) e digite:

text
http://127.0.0.1:8080/
Você vai ver o Explorer com sua blockchain.

Para a carteira:

text
http://127.0.0.1:5000/
Ou procure o arquivo index_wallet.html na pasta e abra com duplo-clique.

🎁 E pronto! O que acontece automaticamente
Depois que você faz esses 5 passos, o nó:

✅ Conecta em outros nós BRN pela rede

✅ Baixa a blockchain automaticamente

✅ Sincroniza com quem tem mais blocos

✅ Anuncia sua presença no tracker

✅ Fica disponível em http://127.0.0.1:8080/

Você não precisa fazer mais nada.

🔄 Para usar de novo depois
Todos os dias você só precisa:

Duplo-clique em iniciar.bat

Digite a senha do nó (a que você escolheu)

Deixe a janela aberta

Não precisa reinstalar Python. Não precisa baixar de novo. Só isso.

❌ Se der erro
"Python não é reconhecido"
Você não marcou Add Python to PATH. Solução:

Desinstale o Python

Reinstale marcando essa opção

Feche e abra o cmd novamente

"pip não é reconhecido"
Rode no cmd:

text
python -m ensurepip --upgrade
"Porta 5000 já em uso"
Outro programa está usando. Descubra qual:

text
netstat -ano | findstr :5000
Ou simplesmente reinicie o PC.

"Senha do nó errada"
Se você digitou a senha errada:

Feche a janela

Renomeie o arquivo node_identity.enc para node_identity.enc.bak

Duplo-clique em iniciar.bat de novo

Escolha uma nova senha

"Falha ao decifrar node_identity.enc"
Sua senha está errada OU o arquivo corrompeu.

Solução mais fácil:

Apague o arquivo node_identity.enc

Duplo-clique em iniciar.bat de novo

Digite uma senha nova

📋 Para adicionar mais PCs
Em cada computador novo:

Copie a pasta LRN-L1-L2-main inteira para o PC

Instale o Python (marcando Add Python to PATH)

Duplo-clique em iniciar.bat

Digite a senha (pode ser a mesma em todos)

Os PCs se encontram automaticamente se estiverem na mesma rede Wi-Fi.

🔑 Sobre a senha
Guarde essa senha. Ela é sua identidade no BRN.

Se esquecer: pode recriar (node_identity.enc renomeado + iniciar.bat)

Peers antigos vão te ver como novo nó

Não perde saldo — saldo fica na carteira BRN, não na identidade

📞 Resumo ultra-rápido
text
1. Baixar ZIP do GitHub
2. Extrair para C:\BRN
3. Instalar Python (marcar "Add to PATH")
4. Duplo-clique em iniciar.bat
5. Digitar senha
6. Abrir http://127.0.0.1:8080/
Fim. 🎉

💡 Dicas finais
Dica	Por quê
Deixe a janela aberta	Se fechar, o nó morre
Não mexa na pasta	Os arquivos .db são sua blockchain
Anote a senha	Não tem como recuperar depois
Faça backup de brn_v2_chain.db	Se quiser guardar a história
┌─────────────────────────────────────────────────────┐
│ 1️⃣ Ao iniciar o programa                             │
│     ↓                                                │
│ 2️⃣ Baixa https://raw.githubusercontent.com/.../peers.json │
│     ↓                                                │
│ 3️⃣ Se falhar → usa peers.json local como fallback     │
│     ↓                                                │
│ 4️⃣ Filtra apenas peers atualizados na última hora     │
│     ↓                                                │
│ 5️⃣ Conecta automaticamente a cada um                 │
│     ↓                                                │
│ 6️⃣ Se desconectar → tenta reconectar a cada 5 min    │
└─────────────────────────────────────────────────────┘

carteira online file:///C:/Users/mayra/Music/LRN-L1-L2-main/wallet.html
explorador de bloco http://127.0.0.1:8080/
<img width="1366" height="768" alt="image" src="https://github.com/user-attachments/assets/13a27a35-15c4-49b3-99fd-6fdd9a10c998" />

<img width="960" height="448" alt="image" src="https://github.com/user-attachments/assets/aab954b9-bbaa-429d-b9cb-282efcad2bf6" />

📄 Documento de Testes — BRN Node v4
Projeto: BRN (BrunoCoin) — Blockchain L1 própria
Versão: v4
Data dos testes: 28/09/2026
Ambiente: Windows 10.0.19045 | Python 3.12
Diretório: C:\Users\mayra\Music\LRN-L1-L2-main

📋 Índice
Pré-requisitos

Testes de Importação

Testes de Inicialização

Testes de API REST (v4)

Testes de HD Wallet BIP39/BIP44

Resumo dos Resultados

O Que Foi Provado

Como Reproduzir

1. Pré-requisitos
1.1 Ambiente
Item	Valor
Sistema Operacional	Windows 10.0.19045.6466
Python	3.12
Diretório do projeto	C:\Users\mayra\Music\LRN-L1-L2-main
Porta HTTP	5000
Porta Explorer	8080
Porta P2P TCP	6001
Porta P2P Multicast	50007
1.2 Dependências instaladas
cmd
python -m pip install flask flask-cors requests pywebview orjson cryptography mnemonic
Pacote	Versão	Status
flask	3.1.3	✅
flask-cors	6.0.5	✅
requests	2.34.2	✅
pywebview	6.2.1	✅
orjson	3.12.0	✅
cryptography	50.0.1	✅
mnemonic	0.21	✅
2. Testes de Importação
Objetivo: Verificar que todos os 10 módulos carregam sem erros de sintaxe, dependência ou circular import.

2.1 Comandos executados
cmd
python -c "import crypto; print('crypto OK')"
python -c "import bech32; print('bech32 OK')"
python -c "import db; print('db OK')"
python -c "import chain_validator; print('chain_validator OK')"
python -c "import blockchain; print('blockchain OK')"
python -c "import wallet; print('wallet OK')"
python -c "import server; print('server OK')"
python -c "import explorer; print('explorer OK')"
python -c "import p2p_unified; print('p2p OK')"
python -c "import main; print('main OK')"
2.2 Resultados
#	Módulo	Saída	Status
1	crypto	crypto OK	✅
2	bech32	bech32 OK	✅
3	db	db OK	✅
4	chain_validator	chain_validator OK	✅
5	blockchain	chain_validator.py plugado em Blockchain + blockchain OK	✅
6	wallet	wallet OK	✅
7	server	chain_validator.py plugado em Blockchain + server OK	✅
8	explorer	explorer OK	✅
9	p2p_unified	p2p OK	✅
10	main	chain_validator.py plugado em Blockchain + main OK	✅
Resultado: 10/10 módulos importam corretamente.

3. Testes de Inicialização
Objetivo: Verificar que o nó sobe todos os subsistemas (blockchain, P2P, HTTP, explorer) sem erros.

3.1 Comando executado
cmd
python main.py
3.2 Saída completa
text
chain_validator.py plugado em Blockchain
================================================================
  BRN Node v3 - Boot
================================================================
[Chain] Abrindo DB: brn_v2_chain.db
        Altura atual : 0
        Tip hash     : 0c4b5316a61daff4862e...
[P2P] Servidor TCP escutando na porta 6001
[Discovery] Escutando 239.255.42.99:50007
[Discovery] IP local: 192.168.0.10
[Discovery] Broadcast ativo
[P2P] Manager iniciado (node_id=c6b06a40)
[P2P]     TCP porta 6001 (UPnP=ON)
[HTTP]     http://0.0.0.0:5000
[Explorer] http://0.0.0.0:8080

No pronto. Ctrl+C para encerrar.

 * Serving Flask app 'server'
 * Serving Flask app 'explorer'
 * Debug mode: off
 * Debug mode: off
WARNING: This is a development server. Do not use it in a production deployment.
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.0.10:5000
Press CTRL+C to quit
[UPnP] Roteador nao suporta ou esta desabilitado
3.3 Verificação dos subsistemas
Subsistema	Status	Observação
Blockchain (DB)	✅	Altura 0, gênesis criado
Chain Validator	✅	Plugado via hook
P2P TCP (porta 6001)	✅	Escutando
Descoberta Multicast	✅	Ativo em 239.255.42.99:50007
HTTP API (porta 5000)	✅	Flask rodando
Explorer (porta 8080)	✅	Flask rodando
UPnP	⚠️	Roteador não suporta (não é erro crítico)
Resultado: Todos os subsistemas críticos inicializados.

4. Testes de API REST (v4)
Objetivo: Validar os novos endpoints adicionados na v4: /api/work, /api/fee-estimate, /api/peers/score.

4.1 Teste — Cumulative Work
Comando:

cmd
curl http://127.0.0.1:5000/api/work
Saída obtida:

json
{"cumulative_work":16,"height":0,"success":true}
Análise:

✅ Retorno válido em JSON

✅ height: 0 (cadeia no gênesis)

✅ cumulative_work: 16 — Trabalho acumulado da cadeia calculado com work_from_difficulty()

Prova: A função Blockchain.cumulative_work() está funcionando. Essa é a base da regra de fork choice (Bitcoin-style), que garante que em caso de fork, a cadeia com mais trabalho seja escolhida.

Status: ✅ PASSOU
📝 BRN — A Moeda que Saiu do Papel e Virou Rede
🌱 Como tudo começou
O BRN (BrunoCoin) nasceu como um projeto pessoal: uma blockchain L1 feita do zero, sem framework pronto, sem atalhos. Só Python, matemática e a vontade de entender como o Bitcoin funciona por dentro.

No começo era simples:

Um gênese e alguns blocos

Um minerador rodando em Python

Uma API HTTP local

Era o v3. Funcionava, mas era frágil. Não tinha carteira HD, não tinha consenso real, não tinha P2P de verdade.

🚀 A evolução — versão por versão
v3 → v4 — A base sólida
Carteira HD Wallet BIP39/BIP44 (mnemônico de 12 palavras)

Cumulative work para fork choice (o mesmo do Bitcoin)

Reorg com backup e restore automático

Estimativa de taxa por prioridade

v4 → v5 — Proteção contra replay
Nonce por transação — impede que a mesma tx seja reenviada

Chain validator separado (auditoria completa da cadeia)

Validação mais rígida de UTXO, fee e pubkey binding

v5 → v6 — Criptografia de verdade
Argon2id para derivar chave da senha (3 iterações, 64 MiB de RAM)

ChaCha20-Poly1305 para cifrar tudo em disco

Ed25519 para identidade do nó P2P

Escrita atômica com fsync + rename — nada de arquivo corrompido

Permissão 0600 nos arquivos de carteira

v6.1 → v6.2 — Rede resistente
Handshake Ed25519 — só entra na rede quem tem a chave

Sync incremental — baixa só os blocos novos

Backoff exponencial — se um peer falha, tenta de novo com espera crescente

Métricas — latência, taxa, peers, blocos contribuídos

Modo read-only — pode sincronizar sem minerar

Auto-reset de identidade se a senha estiver errada

v6.3 → v6.4 — Performance
Peer registry no banco — a carteira agora vê os peers reais

Sync paralelo — 4 threads baixando lotes ao mesmo tempo

10x mais rápido na sincronização inicial

v7 → v8 — Ponte com o mundo real
Bridge BTC ↔ BRN — BRN vira ponte para Bitcoin

Watcher de depósito — detecta BTC recebido e credita BRN

Relay P2P — conecta através de nós intermediários

Descoberta v2 — multicast + tracker HTTP + GitHub + bootstrap

SPV mode — modo cliente leve (só cabeçalhos)

💎 O que é a BRN hoje
Uma L1 própria
Não é token em cima de outra blockchain. É uma camada 1 completa, com:

Próprio gênese

Própria curva criptográfica (secp256k1)

Próprio algoritmo de consenso (PoW Bitcoin-style)

Próprio formato de endereço (bech32 — brn1...)

Características técnicas
Item	Valor
Ticker	BRN
Decimais	8 (100.000.000 sats = 1 BRN)
Supply máximo	21.000.000 BRN
Recompensa inicial	50 BRN
Halving	A cada 210.000 blocos
Block time	120 segundos
Ajuste de dificuldade	A cada 2016 blocos
Dificuldade inicial	4
Fork choice	Cumulative work (Bitcoin-style)
Assinatura	Schnorr (BIP340) com fallback ECDSA
Endereço	bech32 (brn1..., 20 bytes)
Carteira HD	BIP39 + BIP44
Rede P2P
Item	Valor
Protocolo	BRN5/1.0
Porta TCP	6001
Multicast	239.255.42.99:50007
Autenticação	Ed25519 handshake
Peer scoring	Ban automático após -100 pontos
Rate limit	20 msgs/segundo por IP
Max msg	8 MB
Descoberta de peers — 4 camadas
Multicast LAN — acha peers na mesma rede Wi-Fi em segundos

Tracker HTTP — servidor público (brn-tracker.onrender.com)

GitHub — arquivo peers.json como "caderninho compartilhado"

Bootstrap — IPs fixos em bootstrap_peers.json

Serviços
Serviço	Porta	URL
API HTTP	5000	http://127.0.0.1:5000
Explorer	8080	http://127.0.0.1:8080
Carteira desktop	pywebview	janela nativa
P2P	6001	TCP
🛡️ Segurança — 7 camadas
Criptografia de chave — Argon2id + ChaCha20-Poly1305

Handshake P2P — Ed25519 autentica cada nó

Nonce anti-replay — cada tx só é válida uma vez

Peer scoring — peers maus são banidos

Rate limit — bloqueia flood de mensagens

Validação em 2 níveis — tx e bloco (chain_validator)

Escrita atômica — impossível corromper arquivo em disco

🌍 O que dá pra fazer com BRN
Como usuário
✅ Criar carteira HD com mnemônico BIP39

✅ Minerar blocos — ganhar 50 BRN por bloco

✅ Enviar e receber BRN entre endereços

✅ Salvar carteira criptografada com senha

✅ Rodar explorador para ver blocos em tempo real

✅ Faucet grátis para começar a testar

Como nó
✅ Rodar um nó completo — valida tudo

✅ Sincronizar com peers — baixa a blockchain completa

✅ Contribuir para a rede — retransmite blocos

✅ Modo read-only — só sincroniza sem minerar

Como dev
✅ API REST completa — /api/status, /api/transfer, /api/mine

✅ Bridge BTC ↔ BRN — integra com Bitcoin

✅ Relay P2P — conecta em redes restritas

✅ Explorer com CORS — integra com qualquer frontend

🎯 Por que a BRN é diferente
Não é um fork
Não copiou o Bitcoin e mudou o nome. Foi escrita linha por linha em Python, com decisões conscientes:

Schnorr em vez de ECDSA (mais compacto e privado)

Argon2id em vez de PBKDF2 (mais resistente a ASIC)

ChaCha20-Poly1305 em vez de AES-GCM (mais rápido em software)

Não é um token
Não roda em cima da Ethereum. É layer 1 nativa, com consenso próprio.

Não é centralizada
Qualquer pessoa pode rodar um nó em casa. Só precisa Python e uma conexão.

É verificável
Todo o código é aberto no GitHub. Toda a blockchain é auditável com chain_validator. Nada é escondido.

🔮 Próximos passos
O BRN continua evoluindo:

SPV mode — cliente leve para celular

Relay descentralizado — nenhuma entidade central

Bridge bidirecional — BRN ↔ BTC com assinatura nativa

Interface mobile — carteira em Android/iOS

Testnet pública — para devs testarem apps

💬 Quer participar?
Só testar
Baixe o projeto do GitHub

Duplo-clique em iniciar.bat

Digite uma senha

Abra http://127.0.0.1:8080/

Contribuir
Reporte bugs em Issues

Envie PRs com melhorias

Compartilhe o projeto

📌 Conclusão
A BRN é uma prova de que blockchain não precisa ser complicado. Com ~20 arquivos Python, você tem:

Uma moeda com 21 milhões de unidades

Uma rede P2P autenticada

Uma carteira HD com BIP39

Um explorador funcionando

Uma ponte para o Bitcoin

Tudo isso rodando no seu PC, sem servidor, sem mensalidade, sem dependência de terceiros.

A BRN não é só uma moeda. É uma aula de como blockchain funciona por dentro.

#BRN #BrunoCoin #Blockchain #Python #Crypto #Layer1 #OpenSource

Quer que eu transforme esse texto em:

Post para LinkedIn (mais formal)

Thread para Twitter/X (mais curto)

Post para Instagram (visual, com emojis)

Artigo para Medium (técnico, longo)

This response is AI-generated, for reference only.



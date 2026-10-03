#!/usr/bin/env python3
"""Generate local-only secrets without overwriting an existing installation."""
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from shared.licensing import generate_keypair

root=Path(__file__).resolve().parents[1]
runtime=root/'.runtime'
runtime.mkdir(exist_ok=True,mode=0o700)
env=runtime/'local.env'
if env.exists():
    print('Configuração local já existe; preservada. Acessos em .runtime/access.txt')
    raise SystemExit(0)
private,public=generate_keypair()
values={
    'PDV_SECRET_KEY':secrets.token_urlsafe(60),
    'POSTGRES_PASSWORD':secrets.token_urlsafe(32),
    'PDV_ENCRYPTION_KEY':Fernet.generate_key().decode(),
    'PDV_ADMIN_PASSWORD':'Aa7!'+secrets.token_urlsafe(18),
    'PDV_DEMO_PASSWORD':'Bb8!'+secrets.token_urlsafe(18),
    'PDV_DEMO_PIN':str(100000+secrets.randbelow(900000)),
    'PDV_PRINT_BRIDGE_TOKEN':secrets.token_urlsafe(32),
    'RESTIC_PASSWORD':secrets.token_urlsafe(48),
}
for name,contents in [('license-private.pem',private),('license-public.pem',public),
                      ('local.env',''.join(f'{k}={v}\n' for k,v in values.items())),
                      ('access.txt',f"PDV2 — apenas esta instalação local\n\nSaaS: http://127.0.0.1:8000/platform/\nUsuário: admin@pdv2.local\nSenha: {values['PDV_ADMIN_PASSWORD']}\n\nEmpresa: http://127.0.0.1:8000/app/\nUsuário: gestor@pdv2.local\nSenha: {values['PDV_DEMO_PASSWORD']}\nPIN do gestor no terminal: {values['PDV_DEMO_PIN']}\n\nTerminal: http://127.0.0.1:8010/\nServidor no Docker local: http://cloud:8080\nCadastre um terminal na empresa e use o código de pareamento.\n")]:
    target=runtime/name
    with target.open('x') as stream:stream.write(contents)
    # Compose file secrets retain host mode. The private parent is 0700 on the host;
    # mounted files must be readable by the unprivileged UID inside the container.
    target.chmod(0o444 if name.endswith('.pem') else 0o600)
print('Segredos criados. Acessos gravados em .runtime/access.txt; nenhum segredo foi impresso.')

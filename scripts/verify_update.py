#!/usr/bin/env python3
"""Verify a downloaded archive against the server's signed release manifest."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shared.releases import verify_release

parser=argparse.ArgumentParser(description='Verifica atualização PDV2 sem executar o pacote')
parser.add_argument('archive',type=Path)
parser.add_argument('manifest',type=Path)
parser.add_argument('trusted_public_key',type=Path,help='Chave pública já fixada durante o pareamento, nunca uma chave recebida junto do pacote')
args=parser.parse_args()
try:
    raw=json.loads(args.manifest.read_text())
    payload=verify_release(raw.get('release',raw),args.trusted_public_key.read_text())
    if args.archive.stat().st_size!=payload['size_bytes']:raise ValueError('Tamanho divergente')
    digest=hashlib.sha256()
    with args.archive.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    if digest.hexdigest()!=payload['sha256']:raise ValueError('Checksum divergente')
except (ValueError,OSError,KeyError) as exc:
    parser.exit(1,'Atualização REJEITADA: '+str(exc)+'\n')
print('Assinatura, tamanho e SHA-256 conferem. Versão '+payload['version']+'.')
print('Sincronize a fila e faça backup do banco local antes da instalação. Nenhum arquivo foi instalado.')

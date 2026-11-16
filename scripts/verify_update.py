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
parser.add_argument('trusted_public_key',type=Path,help='Chave pública de versões já fixada no terminal (ou a de licença, em terminais antigos), nunca uma chave recebida junto do pacote')
args=parser.parse_args()
try:
    raw=json.loads(args.manifest.read_text())
    payload=verify_release(raw.get('release',raw),args.trusted_public_key.read_text())
    if args.archive.stat().st_size!=payload['size_bytes']:raise ValueError('Tamanho divergente')
    digest=hashlib.sha256()
    with args.archive.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    if digest.hexdigest()!=payload['sha256']:raise ValueError('Checksum divergente')
    if payload.get('kind')=='bundle':
        # Same structural check the automatic updater runs before extracting.
        try:from terminal.updater import inspect_bundle
        except ImportError:inspect_bundle=None
        if inspect_bundle is None:print('Aviso: estrutura do bundle não conferida (dependências do terminal ausentes).')
        else:
            marker=inspect_bundle(args.archive,payload['version'])
            print('Bundle de atualização válido ('+str(marker.get('format'))+'): só contém app-'+payload['version']+'/.')
except (ValueError,OSError,KeyError) as exc:
    parser.exit(1,'Atualização REJEITADA: '+str(exc)+'\n')
except Exception as exc:
    if type(exc).__name__!='UpdateError':raise
    parser.exit(1,'Atualização REJEITADA: '+str(exc)+'\n')
print('Assinatura, tamanho e SHA-256 conferem. Versão '+payload['version']+(' ('+payload['kind']+')' if payload.get('kind') else '')+'.')
if payload.get('min_version'):print('Exige terminal na versão '+payload['min_version']+' ou superior.')
print('Sincronize a fila e faça backup do banco local antes da instalação. Nenhum arquivo foi instalado.')

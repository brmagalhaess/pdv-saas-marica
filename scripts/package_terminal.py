#!/usr/bin/env python3
"""Create a source distribution with no credentials or local sales database."""
import hashlib
import json
import zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
version=(root/'VERSION').read_text().strip()
out=root/'dist';out.mkdir(exist_ok=True)
target=out/f'PDV2-Terminal-{version}.zip'
paths=[]
for directory in ('terminal','shared'):
    paths.extend(p for p in (root/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and 'tests' not in p.parts and p.suffix not in ('.sqlite3','.db','.pyc','.pem'))
paths.extend(root/p for p in ('VERSION','docs/TERMINAL-V2.md','scripts/verify_update.py'))
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(paths):archive.write(path,path.relative_to(root))
summary={'component':'terminal','channel':'pilot','version':version,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
         'size_bytes':target.stat().st_size,'notes':'Distribuição Python. Exige Python 3.12; consulte docs/TERMINAL-V2.md.'}
(out/(target.stem+'.metadata.json')).write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
print(f'Pacote criado: {target.name} ({summary["size_bytes"]} bytes)')
print('Para distribuir: hospede o ZIP em HTTPS e publique estes metadados no painel de versões.')

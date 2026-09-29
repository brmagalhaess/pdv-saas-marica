#!/usr/bin/env python3
"""Restore the newest encrypted backup into a new disposable database and inspect it.

The live database is never a restore target. Docker configuration is inherited.
"""
import json
import re
import secrets
import subprocess
from pathlib import Path

root=Path(__file__).resolve().parents[1]
compose=['docker','compose','--env-file',str(root/'.runtime/local.env'),'-f',str(root/'compose.local.yaml')]
def run(args):
    return subprocess.run(compose+args,cwd=root,check=True,capture_output=True).stdout

name='pdv2_restore_'+secrets.token_hex(6)
assert re.fullmatch('pdv2_restore_[a-f0-9]{12}',name)
run(['exec','-T','backup','pdv2-backup','once'])
snapshots=json.loads(run(['exec','-T','backup','restic','snapshots','--tag','pdv2-database','--json']))
snapshot=max(snapshots,key=lambda row:row['time'])['id']
listing=run(['exec','-T','backup','restic','ls',snapshot,'--json']).decode().splitlines()
files=[json.loads(line) for line in listing]
dump=next(x['path'] for x in files if x.get('type')=='file' and x.get('path','').endswith('/pdv2.dump'))
created=False
try:
    run(['exec','-T','db','createdb','-U','pdv2',name]);created=True
    reader=subprocess.Popen(compose+['exec','-T','backup','restic','dump',snapshot,dump],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    restore=subprocess.run(compose+['exec','-T','db','pg_restore','-U','pdv2','--dbname',name,'--no-owner','--no-privileges','--exit-on-error'],stdin=reader.stdout,capture_output=True)
    reader.stdout.close();reader.wait()
    if reader.returncode or restore.returncode:raise RuntimeError('Restauração falhou. Banco principal preservado.')
    result=run(['exec','-T','db','psql','-U','pdv2','-d',name,'-tA','-c',
        "SELECT json_build_object('tenants',(SELECT count(*) FROM core_tenant),'products',(SELECT count(*) FROM core_product),'sales',(SELECT count(*) FROM core_sale),'migrations',(SELECT count(*) FROM django_migrations))"]).decode().strip()
    counts=json.loads(result)
    if counts['migrations']<20:raise RuntimeError('Schema restaurado incompleto')
    report={'snapshot':snapshot,'restored_database':'temporary database removed after verification','counts':counts,'result':'PASS'}
    path=root/'.runtime/backup-restore-report.json';path.write_text(json.dumps(report,indent=2)+'\n');path.chmod(0o600)
    print('Backup criptografado restaurado em banco isolado: PASS')
    print(json.dumps(counts))
finally:
    if created:run(['exec','-T','db','dropdb','-U','pdv2',name])

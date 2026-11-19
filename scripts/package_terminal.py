#!/usr/bin/env python3
"""Create terminal packages with no credentials or local sales database.

Without arguments: the source distribution (as before) plus an update bundle of
source code (zip with app-<version>/). With --frozen-dir, used by
installer/build.ps1 on Windows: the update bundle of the PyInstaller folder.
With --installer: only the metadata of a finished PDV2-Instalar.exe.
"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_SUFFIXES = ('.sqlite3', '.db', '.pyc', '.pem')


def source_files(root=ROOT):
    paths = []
    for directory in ('terminal', 'shared'):
        paths.extend(p for p in (root / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and 'tests' not in p.parts and p.suffix not in SKIP_SUFFIXES)
    return sorted(paths)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


# Oldest terminal that may receive a bundle by swapping folders. The automatic way back only
# changes the program, never the database: a version that migrates the database (2.1.0 brought the
# cashiers of the shop) must not be offered to a terminal that could be sent back to the old
# program over the new schema. Older terminals get the full installer.
BUNDLE_MIN_VERSION = '2.1.0'


def write_metadata(target, version, notes, kind=None, channel='pilot', min_version=None):
    summary = {'component': 'terminal', 'channel': channel, 'version': version, 'sha256': file_sha256(target),
               'size_bytes': target.stat().st_size, 'notes': notes}
    if kind:
        summary['kind'] = kind
    if min_version:
        summary['min_version'] = min_version
    metadata = target.with_name(target.stem + '.metadata.json')
    metadata.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return summary


def build_source_zip(version, out, root=ROOT):
    """Legacy source distribution: files at the top of the archive."""
    target = out / f'PDV2-Terminal-{version}.zip'
    paths = source_files(root) + [root / p for p in ('VERSION', 'docs/TERMINAL-V2.md', 'scripts/verify_update.py')]
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, path.relative_to(root))
    return target, write_metadata(target, version, 'Distribuição Python. Exige Python 3.12; consulte docs/TERMINAL-V2.md.')


def build_bundle(version, out, files, bundle_format, name):
    """Update bundle: everything under app-<version>/ plus the bundle.json marker."""
    prefix = f'app-{version}/'
    target = out / name
    marker = {'version': version, 'format': bundle_format}
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + 'bundle.json', json.dumps(marker, ensure_ascii=False) + '\n')
        for path, relative in files:
            if relative.as_posix() == 'bundle.json':
                continue
            archive.write(path, prefix + relative.as_posix())
    return target


def build_source_bundle(version, out, root=ROOT):
    files = [(path, path.relative_to(root)) for path in source_files(root) + [root / 'VERSION']]
    target = build_bundle(version, out, files, 'source', f'PDV2-Terminal-{version}-bundle.zip')
    notes = ('Bundle de código-fonte (app-<versão>/). Serve a instalações de desenvolvimento; terminais Windows '
             'instalados só aceitam o bundle gerado por installer/build.ps1.')
    return target, write_metadata(target, version, notes, kind='bundle', min_version=BUNDLE_MIN_VERSION)


def build_frozen_bundle(version, out, frozen_dir):
    frozen_dir = Path(frozen_dir)
    if not (frozen_dir / 'PDV2Server.exe').is_file():
        raise SystemExit(f'{frozen_dir} não contém PDV2Server.exe; gere com installer/build.ps1')
    files = [(path, path.relative_to(frozen_dir)) for path in sorted(frozen_dir.rglob('*')) if path.is_file()]
    target = build_bundle(version, out, files, 'frozen-win64', f'PDV2-Terminal-{version}-win64-bundle.zip')
    return target, write_metadata(target, version, 'Atualização automática para Windows 64 bits.', kind='bundle',
                                  channel='stable', min_version=BUNDLE_MIN_VERSION)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Empacota o terminal PDV2')
    parser.add_argument('--out', type=Path, default=ROOT / 'dist')
    parser.add_argument('--version', default=(ROOT / 'VERSION').read_text(encoding='utf-8').strip())
    parser.add_argument('--frozen-dir', type=Path, help='pasta app-<versão> gerada pelo PyInstaller')
    parser.add_argument('--installer', type=Path, help='instalador pronto: gera só os metadados (kind=installer)')
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    if args.installer:
        summary = write_metadata(args.installer, args.version, 'Instalador completo para Windows 64 bits.',
                                 kind='installer', channel='stable')
        results.append((args.installer, summary))
    if args.frozen_dir:
        results.append(build_frozen_bundle(args.version, args.out, args.frozen_dir))
    if not args.installer and not args.frozen_dir:
        results.append(build_source_zip(args.version, args.out))
        results.append(build_source_bundle(args.version, args.out))
    for target, summary in results:
        kind = f', {summary["kind"]}' if summary.get('kind') else ''
        print(f'Pacote criado: {target.name} ({summary["size_bytes"]} bytes{kind})')
    print('Para distribuir: hospede o arquivo em HTTPS e publique os metadados (.metadata.json) no painel de versões.')
    return results


if __name__ == '__main__':
    main()

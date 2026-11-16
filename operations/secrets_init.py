"""Server-generated secrets for the production compose (service `secrets-init`).

Runs once before every deploy, writes into the `pdv2_secrets` volume (mounted
at /secrets) only the files that do not exist yet, and never overwrites one.
A second run with everything in place changes nothing.

Files and who reads them:

    django_secret_key    Django sessions/CSRF            cloud, worker, migrate
    db_password          PostgreSQL password             db (UID 70), cloud..., backup
    encryption_key       Fernet key of stored credentials cloud, worker, migrate
    restic_password      encryption of the Restic backup backup
    license-*.pem        Ed25519 pair of offline licenses cloud, worker, migrate
    release-*.pem        Ed25519 pair of terminal updates cloud, worker, migrate

Ownership: the service runs as 10001:70 (the `app` user of the cloud image and
the `postgres` group of postgres:17-alpine), so every file is owned by UID
10001 and group 70. Mode 0400 for all of them except db_password (0440, so the
db container can read it through the group); the directory is 0750. Nobody
else reads anything. The backup container runs as root and reads with its
normal capabilities. When run as root (a manual restore) it chowns explicitly.

Old installs that kept the secrets in Dokploy variables (PDV_SECRET_KEY,
POSTGRES_PASSWORD, PDV_ENCRYPTION_KEY, RESTIC_PASSWORD) keep their values: a
missing file is created from the variable instead of a new random value, and
a variable that differs from an existing file stops the deploy instead of
silently switching keys.

`--kit` prints the recovery kit (a POSIX sh script that puts the same files
back in an empty volume). It is only meant for the owner, typed in the
container terminal, and refuses to write to anything that is not a terminal
unless `--permitir-redirecionamento` is given. The normal run never prints a
secret value.
"""
import argparse
import datetime
import errno
import os
import secrets
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization

from shared.licensing import generate_keypair

DEFAULT_DIR = "/secrets"
DEFAULT_UID = 10001
DEFAULT_GID = 70
DIR_MODE = 0o750
FILE_MODE = 0o400
GROUP_READABLE = {"db_password": 0o440}

# file name -> environment variable of the old install (seed and guard)
SEEDS = {
    "django_secret_key": "PDV_SECRET_KEY",
    "db_password": "POSTGRES_PASSWORD",
    "encryption_key": "PDV_ENCRYPTION_KEY",
    "restic_password": "RESTIC_PASSWORD",
}
PAIRS = (("license-private.pem", "license-public.pem"), ("release-private.pem", "release-public.pem"))
ALL_FILES = tuple(SEEDS) + tuple(name for pair in PAIRS for name in pair)


class SecretsError(RuntimeError):
    pass


def _validate(name, value):
    if name == "django_secret_key" and len(value) < 50:
        raise SecretsError("PDV_SECRET_KEY precisa de ao menos 50 caracteres")
    if name == "encryption_key":
        try:
            Fernet(value.encode())
        except (ValueError, TypeError) as exc:
            raise SecretsError("PDV_ENCRYPTION_KEY não é uma chave Fernet válida") from exc
    if not value:
        raise SecretsError(f"Valor vazio para {name}")


def _generate(name):
    if name == "django_secret_key":
        return secrets.token_urlsafe(64)  # 86 caracteres
    if name in ("db_password", "restic_password"):
        return secrets.token_urlsafe(32)  # URL-safe: vai na URL do banco sem codificação
    if name == "encryption_key":
        return Fernet.generate_key().decode()
    raise KeyError(name)


def _public_of(private_pem):
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


class Store:
    def __init__(self, directory, uid=DEFAULT_UID, gid=DEFAULT_GID):
        self.dir = Path(directory)
        self.uid, self.gid = uid, gid

    @classmethod
    def from_env(cls, directory=None):
        return cls(directory or os.environ.get("PDV_SECRETS_DIR", DEFAULT_DIR),
                   int(os.environ.get("PDV_SECRETS_UID", DEFAULT_UID)),
                   int(os.environ.get("PDV_SECRETS_GID", DEFAULT_GID)))

    def path(self, name):
        return self.dir / name

    def read(self, name):
        return self.path(name).read_text()

    def _fix(self, path, mode):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SecretsError(f"{path.name} é um link simbólico; recusado")
        if (info.st_uid, info.st_gid) != (self.uid, self.gid):
            try:
                os.chown(path, self.uid, self.gid, follow_symlinks=False)
            except PermissionError as exc:
                raise SecretsError(
                    f"{path} pertence a {info.st_uid}:{info.st_gid}, esperado {self.uid}:{self.gid}; "
                    "rode o secrets-init como root uma vez ou corrija o dono do volume") from exc
        if stat.S_IMODE(info.st_mode) != mode:
            os.chmod(path, mode)

    def prepare_dir(self):
        if not self.dir.exists():
            self.dir.mkdir(mode=DIR_MODE, parents=True)
        self._fix(self.dir, DIR_MODE)
        if not os.access(self.dir, os.W_OK | os.X_OK):
            raise SecretsError(f"Sem permissão de escrita em {self.dir}")

    def write_new(self, name, content):
        """Atomic, never overwrites: tmp file + link (rename without clobber)."""
        final = self.path(name)
        tmp = self.dir / f".{name}.{os.getpid()}.tmp"
        mode = GROUP_READABLE.get(name, FILE_MODE)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._fix(tmp, mode)
            try:
                os.link(tmp, final)
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise SecretsError(f"{name} apareceu durante a gravação; nada foi sobrescrito") from exc
                if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV):
                    raise
                if final.exists():
                    raise SecretsError(f"{name} apareceu durante a gravação; nada foi sobrescrito") from exc
                os.rename(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
        try:
            dir_fd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def ensure(self, environ=None):
        """Create what is missing. Returns {name: "criado"|"mantido"|"derivado"}."""
        environ = os.environ if environ is None else environ
        self.prepare_dir()
        for leftover in self.dir.glob(".*.tmp"):
            leftover.unlink()
        report = {}
        for name, variable in SEEDS.items():
            seeded = (environ.get(variable) or "").strip()
            if self.path(name).exists():
                self._fix(self.path(name), GROUP_READABLE.get(name, FILE_MODE))
                current = self.read(name).strip()
                if seeded and seeded != current:
                    raise SecretsError(
                        f"A variável {variable} está definida e difere de {self.path(name)}. "
                        f"Retire {variable} do painel (o arquivo é o valor em uso) ou restaure o arquivo correto.")
                _validate(name, current)
                report[name] = "mantido"
                continue
            value = seeded or _generate(name)
            _validate(name, value)
            self.write_new(name, value)
            report[name] = "criado a partir de " + variable if seeded else "criado"
        for private_name, public_name in PAIRS:
            private_path, public_path = self.path(private_name), self.path(public_name)
            if private_path.exists():
                self._fix(private_path, FILE_MODE)
                private_pem = self.read(private_name)
                derived = _public_of(private_pem)
                report[private_name] = "mantido"
                if public_path.exists():
                    self._fix(public_path, FILE_MODE)
                    if self.read(public_name).strip() != derived.strip():
                        raise SecretsError(f"{public_name} não corresponde a {private_name}")
                    report[public_name] = "mantido"
                else:
                    self.write_new(public_name, derived)
                    report[public_name] = "derivado da chave privada"
            elif public_path.exists():
                raise SecretsError(
                    f"{public_name} existe sem {private_name}. Não é seguro gerar um par novo: "
                    "terminais já pareados confiam na chave pública atual. Restaure a chave privada do kit.")
            else:
                private_pem, public_pem = generate_keypair()
                self.write_new(private_name, private_pem)
                self.write_new(public_name, public_pem)
                report[private_name] = report[public_name] = "criado"
        return report

    def kit(self, now=None):
        missing = [name for name in ALL_FILES if not self.path(name).exists()]
        if missing:
            raise SecretsError("Faltam arquivos em " + str(self.dir) + ": " + ", ".join(missing))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        lines = [
            "#!/bin/sh",
            "# KIT DE RECUPERAÇÃO PDV2 - gerado em " + now.strftime("%Y-%m-%d %H:%M UTC"),
            "# Guarde este texto inteiro num cofre FORA do servidor (gerenciador de senhas).",
            "# Quem tem este texto tem acesso total: chave de sessão, senha do banco, chave",
            "# das credenciais, senha do backup e as chaves de licença e de versões.",
            "# Sem restic_password nenhum backup pode ser lido.",
            "#",
            "# Restaurar numa VPS nova, ANTES do primeiro deploy (veja docs/DEPLOY-DOKPLOY.md):",
            "#   docker volume create <projeto>_pdv2_secrets",
            "#   docker run --rm -i -v <projeto>_pdv2_secrets:/secrets alpine:3.20 sh < kit.sh",
            "# Arquivo já existente e diferente é recusado; SUBSTITUIR=1 troca de propósito.",
            "set -eu",
            "umask 077",
            'D="${PDV_SECRETS_DIR:-/secrets}"',
            'mkdir -p "$D"',
            "falhou=0",
            "put() {",
            '  printf "%s" "$2" > "$D/.$1.kit"',
            '  if [ -e "$D/$1" ] && cmp -s "$D/.$1.kit" "$D/$1"; then rm -f "$D/.$1.kit"; echo "igual: $1"; return; fi',
            '  if [ -e "$D/$1" ] && [ "${SUBSTITUIR:-0}" != 1 ]; then',
            '    rm -f "$D/.$1.kit"; echo "DIFERENTE, mantido: $1 (use SUBSTITUIR=1)" >&2; falhou=1; return',
            "  fi",
            '  mv "$D/.$1.kit" "$D/$1"; echo "restaurado: $1"',
            "}",
        ]
        for name in ALL_FILES:
            value = self.read(name)
            if "'" in value:
                raise SecretsError(f"{name} contém aspas simples; kit não pode ser montado")
            lines.append(f"put {name} '{value}'")
        lines += [
            f'chmod {DIR_MODE:o} "$D"',
            f'for f in {" ".join(ALL_FILES)}; do chmod {FILE_MODE:o} "$D/$f"; done',
            f'chmod {GROUP_READABLE["db_password"]:o} "$D/db_password"',
            f'if [ "$(id -u)" = 0 ]; then chown -R {self.uid}:{self.gid} "$D"; fi',
            'exit "$falhou"',
        ]
        return "\n".join(lines) + "\n"


def main(argv=None, stdout=None):
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m operations.secrets_init", description=__doc__.split("\n")[0])
    parser.add_argument("--dir", help="diretório dos segredos (padrão: PDV_SECRETS_DIR ou /secrets)")
    parser.add_argument("--kit", action="store_true", help="imprime o kit de recuperação (só para o dono)")
    parser.add_argument("--permitir-redirecionamento", action="store_true",
                        help="permite gravar o kit em arquivo/pipe em vez do terminal")
    args = parser.parse_args(argv)
    store = Store.from_env(args.dir)
    try:
        if args.kit:
            if not args.permitir_redirecionamento and not stdout.isatty():
                print("Recusado: o kit só é impresso num terminal interativo, para não ir parar em log. "
                      "Para gravar em arquivo de propósito use --permitir-redirecionamento.", file=sys.stderr)
                return 2
            stdout.write(store.kit())
            return 0
        report = store.ensure()
    except SecretsError as exc:
        print("secrets-init: " + str(exc), file=sys.stderr)
        return 1
    for name in ALL_FILES:
        print(f"secrets-init: {name}: {report[name]}", file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Company key that encrypts the terminals' second-disk replica ("cópia de segurança").

One Fernet key per company, created on first use and stored encrypted with the
server key (PDV_ENCRYPTION_KEY). The terminal receives it on pair/lease; the
owner may see it once in the panel. The key itself never goes to logs, audit
or exports: only `key_id` (first 16 hex of its sha256) identifies it.
"""
import hashlib
import logging

from cryptography.fernet import Fernet
from django.db import transaction

from shared.secrets import decrypt_secret, encrypt_secret
from .api_common import audit
from .models import Tenant

logger = logging.getLogger(__name__)


def key_id_of(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def tenant_backup_key(tenant):
    """Return {"key_id", "key"}, creating the key on first use, or None when the
    server key is unavailable. Never raises: pair and lease must not fail."""
    try:
        if tenant.backup_key_encrypted:
            return {"key_id": tenant.backup_key_id, "key": decrypt_secret(tenant.backup_key_encrypted)}
        with transaction.atomic():
            row = Tenant.objects.select_for_update().get(pk=tenant.pk)
            if not row.backup_key_encrypted:
                key = Fernet.generate_key().decode()
                row.backup_key_encrypted = encrypt_secret(key)
                row.backup_key_id = key_id_of(key)
                row.save(update_fields=["backup_key_encrypted", "backup_key_id"])
                audit(row, None, "backup_key.created", row, {"key_id": row.backup_key_id})
            tenant.backup_key_encrypted, tenant.backup_key_id = row.backup_key_encrypted, row.backup_key_id
            return {"key_id": row.backup_key_id, "key": decrypt_secret(row.backup_key_encrypted)}
    except ValueError:
        # Missing or changed PDV_ENCRYPTION_KEY. No secret reaches the log.
        logger.error("Chave da cópia de segurança indisponível para a empresa %s; resposta segue sem mirror_key",
                     tenant.pk)
        return None

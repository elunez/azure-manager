import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


MASTER_KEY_ENV = "AZURE_MANAGER_MASTER_KEY"
MASTER_KEY_FILE = ".master-key"
ENCRYPTED_PREFIX = "fernet:v1:"


class CredentialEncryptionError(RuntimeError):
    pass


def validate_master_key(master_key):
    if len(master_key) < 32:
        raise CredentialEncryptionError("主密钥长度不能少于 32 个字符")
    return master_key


def load_master_key():
    environment_key = os.environ.get(MASTER_KEY_ENV, "").strip()
    if environment_key:
        return validate_master_key(environment_key), "environment", None

    key_path = Path.cwd() / MASTER_KEY_FILE
    if key_path.exists():
        master_key = key_path.read_text(encoding="utf-8").strip()
        if not master_key:
            raise CredentialEncryptionError(".master-key 文件内容为空")
        key_path.chmod(0o600)
        return validate_master_key(master_key), "file", key_path

    master_key = secrets.token_urlsafe(48)
    descriptor = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
            key_file.write(master_key)
            key_file.write("\n")
    except Exception:
        key_path.unlink(missing_ok=True)
        raise
    return master_key, "generated", key_path


def derive_session_key(master_key):
    return hashlib.sha256(("flask-session:" + master_key).encode("utf-8")).digest()


class CredentialCipher:
    def __init__(self, master_key):
        digest = hashlib.sha256(("azure-credentials:" + master_key).encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @staticmethod
    def is_encrypted(value):
        return bool(value) and value.startswith(ENCRYPTED_PREFIX)

    def encrypt(self, plaintext):
        if not plaintext:
            raise CredentialEncryptionError("Azure 客户端密钥不能为空")
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return ENCRYPTED_PREFIX + token

    def decrypt(self, stored_value):
        if not self.is_encrypted(stored_value):
            return stored_value
        token = stored_value[len(ENCRYPTED_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as error:
            raise CredentialEncryptionError(
                "无法解密 Azure 凭据，请检查 AZURE_MANAGER_MASTER_KEY 或 .master-key"
            ) from error

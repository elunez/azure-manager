import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "azure"))

from credential_security import CredentialCipher, CredentialEncryptionError, load_master_key  # noqa: E402


class CredentialSecurityTests(unittest.TestCase):
    def test_key_file_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.cwd", return_value=Path(temp_directory)):
                first_key, first_source, key_path = load_master_key()
                second_key, second_source, _ = load_master_key()

            self.assertEqual(first_source, "generated")
            self.assertEqual(second_source, "file")
            self.assertEqual(first_key, second_key)
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)

    def test_environment_key_has_priority(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            key_path = Path(temp_directory) / ".master-key"
            key_path.write_text("file-key-with-at-least-thirty-two-characters\n", encoding="utf-8")
            environment_key = "environment-key-with-at-least-thirty-two-characters"
            with patch.dict(os.environ, {"AZURE_MANAGER_MASTER_KEY": environment_key}, clear=True):
                with patch("pathlib.Path.cwd", return_value=Path(temp_directory)):
                    master_key, source, _ = load_master_key()

            self.assertEqual(master_key, environment_key)
            self.assertEqual(source, "environment")

    def test_encrypted_secret_requires_the_same_key(self):
        encrypted = CredentialCipher("first-key").encrypt("azure-secret")
        self.assertEqual(CredentialCipher("first-key").decrypt(encrypted), "azure-secret")
        with self.assertRaises(CredentialEncryptionError):
            CredentialCipher("different-key").decrypt(encrypted)


if __name__ == "__main__":
    unittest.main()

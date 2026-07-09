import os
from pathlib import Path
from cryptography.fernet import Fernet

def get_encryption_key() -> bytes:
    key_dir = Path.home() / ".agentflow"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "key.bin"
    if not key_file.exists():
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        return key
    return key_file.read_bytes()

def encrypt_key(plain_text: str) -> str:
    if not plain_text:
        return ""
    try:
        key = get_encryption_key()
        f = Fernet(key)
        return f.encrypt(plain_text.encode()).decode()
    except Exception:
        return ""

def decrypt_key(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    try:
        key = get_encryption_key()
        f = Fernet(key)
        return f.decrypt(cipher_text.encode()).decode()
    except Exception:
        return ""

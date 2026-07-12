import os
import shutil
from pathlib import Path
from cryptography.fernet import Fernet

def get_encryption_key() -> bytes:
    key_file = Path.home() / ".designflow" / "key.bin"
    key_file.parent.mkdir(parents=True, exist_ok=True)
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
    if not cipher_text.startswith("gAAAAA"):
        return cipher_text
    
    candidate_keys = [get_encryption_key()]
    for key in dict.fromkeys(candidate_keys):
        try:
            return Fernet(key).decrypt(cipher_text.encode()).decode()
        except Exception:
            continue
    return ""

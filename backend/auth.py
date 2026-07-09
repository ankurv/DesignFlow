import json
import hashlib
import secrets
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict

USERS_PATH = Path.home() / ".agentflow" / "users.json"

@dataclass
class User:
    username: str
    password_hash: str
    role: str  # "admin" or "user"

@dataclass
class Session:
    session_id: str
    username: str
    role: str

class AuthManager:
    def __init__(self):
        self.sessions: Dict[str, Session] = {}
        self._init_db()

    def _init_db(self):
        USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not USERS_PATH.exists():
            # Create default admin and a default user
            default_users = {
                "admin": {
                    "password_hash": self.hash_password("admin123"),
                    "role": "admin"
                },
                "user": {
                    "password_hash": self.hash_password("user123"),
                    "role": "user"
                }
            }
            USERS_PATH.write_text(json.dumps(default_users, indent=2))

    def hash_password(self, password: str) -> str:
        # Simple SHA-256 for lightweight hashing (no heavy passlib needed)
        salt = "agentflow_salt"
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

    def _load_users(self) -> Dict[str, dict]:
        try:
            return json.loads(USERS_PATH.read_text())
        except Exception:
            return {}

    def login(self, username: str, password: str) -> Optional[Session]:
        users = self._load_users()
        user_data = users.get(username)
        if not user_data:
            return None
        
        if user_data["password_hash"] != self.hash_password(password):
            return None
            
        session_id = secrets.token_urlsafe(32)
        session = Session(session_id=session_id, username=username, role=user_data["role"])
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def logout(self, session_id: str):
        if session_id in self.sessions:
            del self.sessions[session_id]


    def add_user(self, username: str, password: str, role: str) -> bool:
        users = self._load_users()
        if username in users:
            return False
        users[username] = {
            "password_hash": self.hash_password(password),
            "role": role
        }
        USERS_PATH.write_text(json.dumps(users, indent=2))
        return True

    def change_password(self, username: str, new_password: str) -> bool:
        users = self._load_users()
        if username not in users:
            return False
        users[username]["password_hash"] = self.hash_password(new_password)
        USERS_PATH.write_text(json.dumps(users, indent=2))
        return True
        

    def delete_user(self, username: str) -> bool:
        users = self._load_users()
        if username not in users:
            return False
        if username == "admin":
            return False # Prevent deleting root admin
        del users[username]
        USERS_PATH.write_text(json.dumps(users, indent=2))
        return True

    def list_users(self) -> list:
        users = self._load_users()
        return [{"username": u, "role": d["role"]} for u, d in users.items()]

auth_manager = AuthManager()

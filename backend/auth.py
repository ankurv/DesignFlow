import json
import hashlib
import secrets
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict

USERS_PATH = Path.home() / ".designflow" / "users.json"
SESSIONS_PATH = Path.home() / ".designflow" / "sessions.json"

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

    def to_dict(self):
        return {"session_id": self.session_id, "username": self.username, "role": self.role}

    @classmethod
    def from_dict(cls, data):
        return cls(session_id=data["session_id"], username=data["username"], role=data["role"])

class AuthManager:
    def __init__(self):
        self.sessions: Dict[str, Session] = {}
        self._init_db()
        self._load_sessions()

    def _load_sessions(self):
        try:
            data = json.loads(SESSIONS_PATH.read_text())
            for sid, sdata in data.items():
                self.sessions[sid] = Session.from_dict(sdata)
        except Exception:
            self.sessions = {}

    def _save_sessions(self):
        try:
            SESSIONS_PATH.write_text(json.dumps({sid: s.to_dict() for sid, s in self.sessions.items()}, indent=2))
        except Exception:
            pass

    def _init_db(self):
        USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not USERS_PATH.exists():
            # Create default admin and a default user
            default_users = {
                "admin": {
                    "password_hash": self.hash_password("admin"),
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
        salt = "designflow_salt"
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

    @staticmethod
    def _legacy_hash_password(password: str) -> str:
        return hashlib.sha256(f"agentflow_salt{password}".encode()).hexdigest()

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

        current_hash = self.hash_password(password)
        legacy_hash = self._legacy_hash_password(password)
        if user_data["password_hash"] not in {current_hash, legacy_hash}:
            return None
        if user_data["password_hash"] == legacy_hash:
            user_data["password_hash"] = current_hash
            USERS_PATH.write_text(json.dumps(users, indent=2))

        session_id = secrets.token_urlsafe(32)
        session = Session(session_id=session_id, username=username, role=user_data["role"])
        self.sessions[session_id] = session
        self._save_sessions()
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def logout(self, session_id: str):
        if session_id in self.sessions:
            del self.sessions[session_id]
            self._save_sessions()


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

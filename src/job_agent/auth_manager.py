"""Gestion de l'authentification : vault Fernet + session Chrome partagée.

Modes :
- shared_chrome : on lance patchright avec un user_data_dir partagé (data/.browser_profile/).
  L'utilisateur s'est déjà loggé manuellement → les cookies sont réutilisés.
  Recommandé pour LinkedIn/Indeed/WTTJ (CGU + anti-bot agressifs).
- stored : username + password chiffrés en DB. L'agent log lui-même via browser-use.
  Adapté aux ATS (Greenhouse, Lever, Workable, SmartRecruiters).
- none : pas de login nécessaire.

Le master password du vault est lu :
1. depuis la variable d'environnement JOB_AGENT_VAULT_KEY si présente,
2. sinon, demandé interactivement via getpass.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .logging_setup import get_logger
from .models import AuthMethod, SiteCredential

log = get_logger("auth_manager")

ATS_DOMAINS: tuple[str, ...] = (
    "greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "workable.com",
    "smartrecruiters.com",
    "jobs.smartrecruiters.com",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "jobs.ashbyhq.com",
    "join.com",
)
"""Domaines ATS pour lesquels l'inscription automatique est autorisée."""

_VAULT_SALT = b"job-agent-vault-v1"


def root_domain(url: str) -> str:
    """Extrait le domaine racine d'une URL (sans sous-domaine pour la majorité des cas)."""
    host = urlparse(url).hostname or url
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def site_supports_auto_register(url: str) -> bool:
    """Vrai si le domaine est dans la whitelist ATS (inscription auto autorisée)."""
    host = (urlparse(url).hostname or "").lower()
    return any(host.endswith(d) for d in ATS_DOMAINS)


def _derive_fernet_key(master: str) -> bytes:
    """Dérive une clé Fernet depuis un master password (PBKDF2-like simple)."""
    h = hashlib.pbkdf2_hmac("sha256", master.encode("utf-8"), _VAULT_SALT, 200_000)
    return base64.urlsafe_b64encode(h)


class Vault:
    """Chiffrement / déchiffrement des credentials via Fernet."""

    def __init__(self, master_password: str) -> None:
        if not master_password:
            raise ValueError("master_password ne peut pas être vide")
        self._fernet = Fernet(_derive_fernet_key(master_password))

    @classmethod
    def from_env_or_prompt(cls, env_key: str = "JOB_AGENT_VAULT_KEY") -> Vault:
        """Lit la clé maître dans l'env (.env) ou demande interactivement via getpass."""
        master = os.environ.get(env_key)
        if not master:
            master = getpass.getpass("Master password du vault : ")
        return cls(master)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Master password incorrect ou données corrompues") from exc


class AuthManager:
    """Façade qui décide comment se logger / s'inscrire pour chaque URL."""

    def __init__(self, conn: sqlite3.Connection, vault: Vault | None = None) -> None:
        self.conn = conn
        self._vault = vault

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = Vault.from_env_or_prompt()
        return self._vault

    def add_credential(
        self,
        *,
        site_domain: str,
        username: str | None,
        password: str | None,
        auth_method: AuthMethod = AuthMethod.STORED,
        notes: str = "",
    ) -> None:
        """Ajoute / met à jour un credential. Chiffre le password si fourni."""
        encrypted = self.vault.encrypt(password) if password else None
        with db.transaction(self.conn):
            db.upsert_site_credential(
                self.conn,
                site_domain=site_domain,
                auth_method=auth_method.value,
                username=username,
                password_encrypted=encrypted,
                notes=notes,
            )
        log.info("Credential stored for %s (method=%s)", site_domain, auth_method.value)

    def get_credential(self, site_domain: str) -> SiteCredential | None:
        """Récupère un credential, déchiffre le password si présent."""
        row = db.get_site_credential(self.conn, site_domain)
        if row is None:
            return None
        password = None
        if row["password_encrypted"]:
            password = self.vault.decrypt(row["password_encrypted"])
        return SiteCredential(
            site_domain=row["site_domain"],
            auth_method=AuthMethod(row["auth_method"]),
            username=row["username"],
            password=password,
            notes=row["notes"] or "",
        )

    def list_credentials(self) -> list[dict]:
        """Liste les domaines sans révéler les mots de passe."""
        rows = db.list_site_credentials(self.conn)
        return [
            {
                "site_domain": r["site_domain"],
                "auth_method": r["auth_method"],
                "username": r["username"] or "-",
                "has_password": bool(r["password_encrypted"]),
                "notes": r["notes"] or "",
            }
            for r in rows
        ]

    def remove_credential(self, site_domain: str) -> bool:
        with db.transaction(self.conn):
            n = db.delete_site_credential(self.conn, site_domain)
        return n > 0

    def resolve_for_url(self, url: str) -> SiteCredential:
        """Détermine la stratégie auth pour une URL.

        Priorité :
        1. Credential explicite stocké pour ce domaine (root_domain).
        2. ATS whitelist → STORED (inscription/login auto autorisée).
        3. Sinon → SHARED_CHROME (réutilise la session Chrome).
        """
        domain = root_domain(url)
        cred = self.get_credential(domain)
        if cred is not None:
            return cred
        if site_supports_auto_register(url):
            return SiteCredential(site_domain=domain, auth_method=AuthMethod.STORED)
        return SiteCredential(site_domain=domain, auth_method=AuthMethod.SHARED_CHROME)


def shared_chrome_profile_dir(project_root: Path) -> Path:
    """Renvoie (et crée si besoin) le user_data_dir partagé pour patchright."""
    p = project_root / "data" / ".browser_profile"
    p.mkdir(parents=True, exist_ok=True)
    return p

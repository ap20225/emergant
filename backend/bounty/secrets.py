"""Encrypted, server-only credentials. Neither ciphertext nor keys enter Mongo/API.

Production hardening: each credential first honors a process ENVIRONMENT value
(GITHUB_TOKEN / E2B_API_KEY / EXPLABS_API_KEY) injected by the deployment platform's
secret store, so credentials survive container replacement/restart. When no env value is
present (e.g. the preview pod), it falls back to the encrypted on-disk vault written by the
Security UI — preserving existing UI behaviour. Secret VALUES are never logged or returned.
"""
import os
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import dotenv_values

# Production secret-store env names per vault credential.
ENV_KEYS = {'github': 'GITHUB_TOKEN', 'e2b': 'E2B_API_KEY'}


class SecretVault:
    def __init__(self):
        self.root = Path(os.environ['PENNILESS_SECRETS_DIR'])

    def _path(self, name):
        if name not in {'github', 'e2b'}:
            raise ValueError('Unsupported secret name')
        return self.root / (name + '.enc')

    def _env_value(self, name):
        val = os.environ.get(ENV_KEYS.get(name, ''))
        return val if val else None

    def has(self, name):
        if self._env_value(name):
            return True
        return self._path(name).is_file()

    def _cipher(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        path = self.root / 'master.key'
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as out:
                out.write(Fernet.generate_key())
        return Fernet(path.read_bytes())

    def put(self, name, value):
        payload = self._cipher().encrypt(value.encode())
        path = self._path(name)
        temporary = path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(payload)
        os.replace(temporary, path)

    def get(self, name):
        env = self._env_value(name)
        if env:
            return env
        if not self._path(name).is_file():
            return None
        return self._cipher().decrypt(self._path(name).read_bytes()).decode()

    def delete(self, name):
        self._path(name).unlink(missing_ok=True)


def experiential_key():
    env = os.environ.get('EXPLABS_API_KEY')
    if env:
        return env
    path = os.environ.get('EXPLABS_SECRET_FILE')
    if path and os.path.isfile(path):
        return dotenv_values(path).get('EXPLABS_API_KEY')
    return None


def credential_readiness():
    """Boolean presence map (NEVER values) for startup/readiness reporting."""
    vault = SecretVault()
    return {'github': vault.has('github'), 'e2b': vault.has('e2b'), 'explabs': bool(experiential_key())}

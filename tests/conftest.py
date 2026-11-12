import sys
import types

import pytest


class _WindowsLikeBackend:
    """In-memory stand-in for an OS keychain that enforces Credential Manager's size cap."""
    MAX_CHARS = 1280

    def __init__(self):
        self.entries = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        if len(password) > self.MAX_CHARS:
            raise OSError('credential blob too large')
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        if self.entries.pop((service, username), None) is None:
            raise KeyError(username)


_WindowsLikeBackend.__module__ = 'keyring.backends.Windows'


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install a fake `keyring` module whose backend looks like a secure OS keychain."""
    backend = _WindowsLikeBackend()
    # Delegate late so a test can replace a backend method (for example to simulate an interruption).
    module = types.SimpleNamespace(get_keyring=lambda: backend,
                                   get_password=lambda *a: backend.get_password(*a),
                                   set_password=lambda *a: backend.set_password(*a),
                                   delete_password=lambda *a: backend.delete_password(*a))
    monkeypatch.setitem(sys.modules, 'keyring', module)
    return backend

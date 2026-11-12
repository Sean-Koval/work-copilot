"""Real OS keychain checks, run by the oauth-keychain CI job (opt-in elsewhere).

WORK_COPILOT_OS_KEYCHAIN=required: macOS Keychain / Windows Credential Manager must round-trip a token
larger than Credential Manager's single-entry cap, and leave nothing behind.
WORK_COPILOT_OS_KEYCHAIN=absent: Linux with no Secret Service must refuse clearly, with no file fallback.
"""
import os
import secrets

import pytest
from mcp.shared.auth import OAuthToken

from work_copilot.remote_mcp import KEYRING_SERVICE, KeychainUnavailable, KeyringStorage

MODE = os.environ.get('WORK_COPILOT_OS_KEYCHAIN')


@pytest.mark.skipif(MODE != 'required', reason='set WORK_COPILOT_OS_KEYCHAIN=required to use the real OS keychain')
@pytest.mark.asyncio
async def test_os_keychain_round_trip(tmp_path):
    import keyring  # pyright: ignore[reportMissingImports]
    store = KeyringStorage('ci-' + secrets.token_hex(8))
    access = 'fictional-' + secrets.token_urlsafe(3000)
    try:
        await store.set_tokens(OAuthToken(access_token=access, token_type='Bearer', expires_in=3600, refresh_token='fictional-refresh'))
        restored = await store.get_tokens()
        assert restored is not None
        assert restored.access_token == access and restored.refresh_token == 'fictional-refresh'
        key = store.identity + ':tokens'
        manifest = store._manifest(key)
        assert manifest is not None and manifest[1] > 1, 'large token must be split across entries'
        await store.set_tokens(OAuthToken(access_token='fictional-short', token_type='Bearer'))
        short = await store.get_tokens()
        assert short is not None and short.access_token == 'fictional-short'
        assert keyring.get_password(KEYRING_SERVICE, store._chunk_key(key, manifest[0], 0)) is None
    finally:
        store.clear()
    assert await store.get_tokens() is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(MODE != 'absent', reason='set WORK_COPILOT_OS_KEYCHAIN=absent on a host without a Secret Service')
def test_missing_secret_service_fails_clearly(tmp_path):
    with pytest.raises(KeychainUnavailable, match='Secret Service'):
        KeyringStorage('ci-absent')
    assert list(tmp_path.iterdir()) == []

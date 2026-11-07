import pytest

from bot.config import ConfigError
from bot.crypto import Crypto, DecryptError, SecretStore, generate_key


def test_encrypt_decrypt_roundtrip():
    crypto = Crypto(generate_key())
    token = crypto.encrypt("секрет-123")
    assert token != "секрет-123"
    assert crypto.decrypt(token) == "секрет-123"


def test_wrong_key_fails():
    token = Crypto(generate_key()).encrypt("value")
    with pytest.raises(DecryptError):
        Crypto(generate_key()).decrypt(token)


def test_invalid_key_is_config_error():
    with pytest.raises(ConfigError):
        Crypto("not-a-key")


async def test_secret_store_keeps_only_ciphertext(db):
    store = SecretStore(db, Crypto(generate_key()))
    await store.set("click_provider_token", "398062629:TEST:abc")
    raw = await db.get_secret("click_provider_token")
    assert raw and "398062629" not in raw
    assert await store.get("click_provider_token") == "398062629:TEST:abc"
    assert await store.names() == ["click_provider_token"]
    assert await store.delete("click_provider_token")
    assert await store.get("click_provider_token") is None

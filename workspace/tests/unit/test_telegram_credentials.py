import uuid

import pytest
from cryptography import exceptions

from workspace.bridges.telegram import credentials


def test_session_encryption_is_randomized_and_bound_to_account_and_key():
    account = uuid.uuid4()
    secret = {"session": "synthetic-session-material"}
    first = credentials.encrypt(account, secret, "test-only-key")
    second = credentials.encrypt(account, secret, "test-only-key")
    assert first != second
    assert "synthetic-session-material" not in str(first)
    assert credentials.decrypt(account, first, "test-only-key") == secret
    with pytest.raises(exceptions.InvalidTag):
        credentials.decrypt(uuid.uuid4(), first, "test-only-key")
    with pytest.raises(exceptions.InvalidTag):
        credentials.decrypt(account, first, "another-test-key")


def test_missing_encryption_configuration_fails_closed():
    with pytest.raises(ValueError):
        credentials.encrypt(uuid.uuid4(), {"session": "synthetic"}, "")

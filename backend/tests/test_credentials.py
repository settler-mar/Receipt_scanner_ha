from unittest.mock import Mock

from app.adapters.base import AuthResult
from app.core.credentials import upsert_credential
from app.core.security import decrypt
from app.models.models import Account


def test_new_credential_is_visible_on_account_immediately():
    account = Account(id="account-1", store_id="store-1", label="Test")
    db = Mock()

    upsert_credential(db, account, AuthResult(access_token="session-token"))

    assert account.credential is not None
    assert decrypt(account.credential.access_token_enc) == "session-token"
    db.add.assert_called_once_with(account.credential)

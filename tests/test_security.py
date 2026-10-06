from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.security import authenticate
from app.services.users_client import UsersClient

SECRET = 'activities-test-secret-longer-than-thirty-two-bytes'
SETTINGS = Settings('unused', SECRET, 'http://users.test', 'sportmatch-auth', 'sportmatch-mobile')


@pytest.mark.parametrize('extra', [
    {'exp': datetime.now(timezone.utc) - timedelta(seconds=1)},
    {'iss': 'wrong'}, {'aud': 'wrong'}, {'token_type': 'refresh'}, {'sub': 'invalid'},
])
def test_invalid_access_tokens_are_rejected(extra):
    now = datetime.now(timezone.utc)
    claims = {'sub': str(uuid4()), 'iat': now, 'exp': now + timedelta(minutes=5),
              'iss': SETTINGS.issuer, 'aud': SETTINGS.audience, 'token_type': 'access', **extra}
    token = jwt.encode(claims, SECRET, algorithm='HS256')
    with pytest.raises(HTTPException) as error:
        authenticate(token, SETTINGS)
    assert error.value.status_code == 401


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_users_validation_fails_closed(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status)
    with httpx.Client(base_url='http://users.test', transport=httpx.MockTransport(handler)) as client:
        users = UsersClient(client)
        with pytest.raises(HTTPException) as error:
            users.card(uuid4(), 'actor-token')
        assert error.value.status_code == (503 if status == 500 else status)
        assert requests[0].headers['authorization'] == 'Bearer actor-token'

import hashlib
import hmac

from review_agent.webhook import verify_signature

BODY = b'{"action": "opened"}'
SECRET = "s3cret"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    assert verify_signature(SECRET, BODY, _sig(SECRET, BODY))


def test_wrong_secret_fails():
    assert not verify_signature(SECRET, BODY, _sig("other-secret", BODY))


def test_tampered_body_fails():
    assert not verify_signature(SECRET, b'{"action": "closed"}', _sig(SECRET, BODY))


def test_missing_header_fails():
    assert not verify_signature(SECRET, BODY, None)


def test_garbage_header_fails():
    assert not verify_signature(SECRET, BODY, "sha256=not-hex")
    assert not verify_signature(SECRET, BODY, "sha1=deadbeef")

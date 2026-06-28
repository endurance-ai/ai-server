"""S3 presigned PUT URL generation for user image uploads."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from app.core.config import Settings

_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"
_UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


@dataclass(frozen=True)
class UploadStorageConfig:
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str
    session_token: str
    public_base_url: str


def upload_storage_config(settings: Settings) -> UploadStorageConfig:
    access_key_id = settings.UPLOADS_S3_ACCESS_KEY_ID or settings.AWS_ACCESS_KEY_ID
    secret_access_key = settings.UPLOADS_S3_SECRET_ACCESS_KEY or settings.AWS_SECRET_ACCESS_KEY
    session_token = settings.UPLOADS_S3_SESSION_TOKEN or settings.AWS_SESSION_TOKEN
    return UploadStorageConfig(
        bucket=settings.UPLOADS_S3_BUCKET.strip(),
        region=settings.UPLOADS_S3_REGION.strip(),
        access_key_id=access_key_id.strip(),
        secret_access_key=secret_access_key.strip(),
        session_token=session_token.strip(),
        public_base_url=settings.UPLOADS_PUBLIC_BASE_URL.strip().rstrip("/"),
    )


def missing_upload_config(settings: Settings) -> list[str]:
    config = upload_storage_config(settings)
    missing: list[str] = []
    if not config.bucket:
        missing.append("UPLOADS_S3_BUCKET")
    if not config.region:
        missing.append("UPLOADS_S3_REGION")
    if not config.access_key_id:
        missing.append("UPLOADS_S3_ACCESS_KEY_ID or AWS_ACCESS_KEY_ID")
    if not config.secret_access_key:
        missing.append("UPLOADS_S3_SECRET_ACCESS_KEY or AWS_SECRET_ACCESS_KEY")
    if not config.public_base_url:
        missing.append("UPLOADS_PUBLIC_BASE_URL")
    return missing


def public_image_url(settings: Settings, object_key: str) -> str:
    config = upload_storage_config(settings)
    return f"{config.public_base_url}/{quote(object_key, safe='/')}"


def presign_s3_put_url(
    *,
    settings: Settings,
    object_key: str,
    content_type: str,
    expires_seconds: int,
    now: datetime | None = None,
) -> str:
    """Create an AWS SigV4 presigned S3 PUT URL.

    The generated URL signs the `content-type` header. Clients must include the
    same header value when performing the PUT.
    """
    config = upload_storage_config(settings)
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp.strftime("%Y%m%d")
    credential_scope = f"{date_stamp}/{config.region}/{_SERVICE}/aws4_request"
    host = f"{config.bucket}.s3.{config.region}.amazonaws.com"
    canonical_uri = "/" + quote(object_key, safe="/")
    signed_headers = "content-type;host"

    query_params = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{config.access_key_id}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_seconds),
        "X-Amz-SignedHeaders": signed_headers,
    }
    if config.session_token:
        query_params["X-Amz-Security-Token"] = config.session_token

    canonical_querystring = urlencode(sorted(query_params.items()), quote_via=quote, safe="")
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    canonical_request = "\n".join(
        [
            "PUT",
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            _UNSIGNED_PAYLOAD,
        ]
    )
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signing_key = _signature_key(config.secret_access_key, date_stamp, config.region)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_querystring}&X-Amz-Signature={signature}"


def _signature_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()

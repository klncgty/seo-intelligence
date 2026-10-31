"""Object storage (MinIO / S3). Content-addressed keys; no caller-controlled paths.

Keys look like ``<kind>/<sha256[:2]>/<sha256>[.<ext>]`` — the only caller inputs are a fixed
``kind`` from KINDS and an extension from EXTS, so path traversal is impossible by construction.
"""

import hashlib
import io

from app.core.config import get_settings

KINDS = {"raw-provider", "warc", "html", "report", "export"}
EXTS = {"", "json", "json.gz", "warc.gz", "html", "html.gz", "pdf", "csv", "xlsx"}


class StorageUnavailable(RuntimeError):
    pass


def content_key(kind: str, data: bytes, ext: str = "") -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown storage kind: {kind}")
    if ext not in EXTS:
        raise ValueError(f"extension not allowed: {ext}")
    digest = hashlib.sha256(data).hexdigest()
    return f"{kind}/{digest[:2]}/{digest}" + (f".{ext}" if ext else "")


def _valid_key(key: str) -> bool:
    parts = key.split("/")
    return (len(parts) == 3 and parts[0] in KINDS and len(parts[1]) == 2
            and all(c in "0123456789abcdef" for c in parts[2].split(".", 1)[0])
            and len(parts[2].split(".", 1)[0]) == 64)


class ObjectStore:
    def __init__(self, client=None, bucket: str | None = None):
        s = get_settings()
        self.bucket = bucket or s.minio_bucket
        if client is None and s.minio_endpoint:
            from minio import Minio
            client = Minio(s.minio_endpoint, access_key=s.minio_access_key, secret_key=s.minio_secret_key,
                           secure=s.minio_secure)
        self.client = client

    @property
    def configured(self) -> bool:
        return self.client is not None

    def _require(self):
        if not self.configured:
            raise StorageUnavailable("MINIO_ENDPOINT not set")
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put(self, kind: str, data: bytes, ext: str = "", content_type: str = "application/octet-stream") -> str:
        """Store bytes; returns the content-addressed key (idempotent: same bytes → same key)."""
        self._require()
        key = content_key(kind, data, ext)
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        return key

    def get(self, key: str) -> bytes:
        if not _valid_key(key):
            raise ValueError("invalid storage key")
        self._require()
        resp = self.client.get_object(self.bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def ping(self) -> bool:
        try:
            self._require()
            return True
        except Exception:
            return False

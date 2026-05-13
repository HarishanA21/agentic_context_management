"""S3-compatible object storage client.

Works against any S3 API: MinIO locally (default), AWS S3, R2, etc.
Endpoint and credentials come from env vars — see .env.example.

Exposes a small bucket-like facade (upload / list / download / remove) that
mirrors the subset of the supabase-py storage API the rest of the codebase
relied on, so api.py and the agent tools don't need to change.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


_client = None
_bucket_facade: Optional["S3Bucket"] = None


def _bucket_name() -> str:
    return os.environ.get("S3_BUCKET", "project-files")


def _endpoint() -> Optional[str]:
    # Empty / unset means "real AWS S3" (boto's default endpoint).
    ep = os.environ.get("S3_ENDPOINT_URL", "").strip()
    return ep or None


def _client_singleton():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=_endpoint(),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
            region_name=os.environ.get("S3_REGION", "us-east-1"),
            # Path-style addressing is required for MinIO; harmless on AWS.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        _ensure_bucket(_client, _bucket_name())
    return _client


def _ensure_bucket(client, name: str) -> None:
    """Create the bucket on first use so a fresh MinIO works out of the box."""
    try:
        client.head_bucket(Bucket=name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchBucket", "NotFound"}:
            try:
                client.create_bucket(Bucket=name)
            except ClientError as ce:
                # Race with another worker — fine if it now exists.
                if ce.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                    raise


class S3Bucket:
    """Thin facade matching the supabase storage `from_(bucket)` interface.

    Only the methods the codebase actually used are implemented.
    """

    def __init__(self, bucket: str):
        self._bucket = bucket

    def upload(
        self,
        path: str,
        file: bytes,
        file_options: Optional[dict] = None,
    ) -> None:
        opts = file_options or {}
        # Supabase used "upsert": "true" — S3 PutObject overwrites by default,
        # so we just honor the content-type and ignore the rest.
        content_type = opts.get("content-type") or opts.get("contentType") \
            or "application/octet-stream"
        _client_singleton().put_object(
            Bucket=self._bucket,
            Key=path,
            Body=file,
            ContentType=content_type,
        )

    def list(self, prefix: str) -> List[dict]:
        """Return items under `prefix/` shaped like the supabase storage list.

        Each item: {"id": <key>, "name": <basename>, "metadata": {"size": N},
                    "created_at": iso, "updated_at": iso}.
        """
        client = _client_singleton()
        # Trailing slash so we only get direct children of the session folder.
        norm_prefix = prefix.rstrip("/") + "/"
        out: List[dict] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=norm_prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                name = key[len(norm_prefix):]
                if not name or "/" in name:
                    # Skip sub-folders — we only show files in this folder.
                    continue
                ts = obj.get("LastModified")
                iso = ts.isoformat() if ts else None
                out.append({
                    "id": key,
                    "name": name,
                    "metadata": {"size": int(obj.get("Size", 0))},
                    "created_at": iso,
                    "updated_at": iso,
                })
        return out

    def download(self, path: str) -> bytes:
        try:
            resp = _client_singleton().get_object(Bucket=self._bucket, Key=path)
            return resp["Body"].read()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(f"Object not found: {path}") from e
            raise

    def remove(self, paths: List[str]) -> None:
        if not paths:
            return
        client = _client_singleton()
        # delete_objects caps at 1000 keys per call — split if ever needed.
        client.delete_objects(
            Bucket=self._bucket,
            Delete={"Objects": [{"Key": p} for p in paths]},
        )


def get_client() -> Any:
    """Returns the raw boto3 S3 client (rarely needed outside this module)."""
    return _client_singleton()


def get_bucket() -> S3Bucket:
    global _bucket_facade
    if _bucket_facade is None:
        _bucket_facade = S3Bucket(_bucket_name())
        _client_singleton()
    return _bucket_facade


def session_prefix(user_id: str, session_id: str) -> str:
    """Storage key prefix for a session's files."""
    return f"{user_id}/{session_id}"


def file_key(user_id: str, session_id: str, filename: str) -> str:
    """Full storage key for a file in a session."""
    return f"{user_id}/{session_id}/{filename}"


def is_not_found(err: Exception) -> bool:
    if isinstance(err, FileNotFoundError):
        return True
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            return True
    msg = str(err).lower()
    return "not found" in msg or "nosuchkey" in msg or "404" in msg

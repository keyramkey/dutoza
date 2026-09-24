"""
storage.py — Cloudflare R2 storage helper for Kijiji Tanzania
"""
import os
import uuid
from werkzeug.utils import secure_filename

try:
    import boto3
    from botocore.client import Config
except ImportError:
    boto3 = None


def _get_r2_client():
    if boto3 is None:
        return None

    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key, secret_key]):
        return None

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def use_r2():
    return bool(
        os.environ.get("R2_ACCOUNT_ID")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
        and os.environ.get("R2_BUCKET_NAME")
    )


def upload_file(file_storage, folder="uploads"):
    if not use_r2() or file_storage is None:
        return None

    client = _get_r2_client()
    if client is None:
        return None

    bucket = os.environ.get("R2_BUCKET_NAME")
    public_url = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

    original = secure_filename(file_storage.filename or "file")
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else "bin"
    key = f"{folder}/{uuid.uuid4().hex}.{ext}"

    content_type = file_storage.content_type or "application/octet-stream"

    client.upload_fileobj(
        file_storage,
        bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )

    if public_url:
        return f"{public_url}/{key}"
    return key


def delete_file(url_or_key):
    if not use_r2() or not url_or_key:
        return False

    client = _get_r2_client()
    if client is None:
        return False

    bucket = os.environ.get("R2_BUCKET_NAME")
    public_url = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

    key = url_or_key
    if public_url and url_or_key.startswith(public_url):
        key = url_or_key[len(public_url) + 1 :]

    try:
        client.delete_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False

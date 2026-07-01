"""
One-off migration: copy every file under the local ``uploads/`` tree into the S3
bucket, preserving the key layout (local ``uploads/<rel>`` -> S3 key ``uploads/<rel>``).

- Idempotent: skips objects that already exist in S3 (unless --overwrite).
- Public/private ACL is handled by the storage layer's config; this script only puts
  objects. Access is governed by your bucket policy / presigning (see storage_service).
- Leaves local files in place (they remain a backup).

Usage (inside the prod backend container, which has boto3 + AWS creds):
    # Dry run (lists what would be uploaded):
    PYTHONPATH=. python scripts/migrate_uploads_to_s3.py
    # Apply:
    PYTHONPATH=. python scripts/migrate_uploads_to_s3.py --apply
    # Options: --overwrite (re-put existing), --root uploads
"""
import argparse
import mimetypes
import os
import sys

import boto3
from botocore.exceptions import ClientError

from src.services import storage_service as ss


def object_exists(client, bucket, key):
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually upload (default: dry run)")
    ap.add_argument("--overwrite", action="store_true", help="re-upload objects that already exist")
    ap.add_argument("--root", default="uploads", help="local uploads root (default: uploads)")
    args = ap.parse_args()

    bucket = ss.AWS_S3_BUCKET
    if not bucket:
        print("ERROR: AWS_S3_BUCKET not set in the environment.", file=sys.stderr)
        sys.exit(1)

    client = boto3.client("s3", region_name=ss.AWS_REGION, endpoint_url=ss.AWS_S3_ENDPOINT)

    root = args.root.rstrip("/")
    if not os.path.isdir(root):
        print(f"ERROR: local uploads dir '{root}' not found.", file=sys.stderr)
        sys.exit(1)

    total = uploaded = skipped = failed = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            local_path = os.path.join(dirpath, name)
            rel = os.path.relpath(local_path, root).replace(os.sep, "/")  # e.g. courses/thumbnails/x.png
            key = ss._s3_key(rel)  # -> uploads/courses/thumbnails/x.png
            total += 1

            try:
                if not args.overwrite and object_exists(client, bucket, key):
                    skipped += 1
                    continue

                if args.apply:
                    ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
                    extra = {"ContentType": ctype}
                    if ss.S3_USE_ACL:
                        extra["ACL"] = "public-read" if ss.is_public(rel) else "private"
                    with open(local_path, "rb") as f:
                        client.put_object(Bucket=bucket, Key=key, Body=f.read(), **extra)
                    uploaded += 1
                    if uploaded % 100 == 0:
                        print(f"  ... {uploaded} uploaded")
                else:
                    uploaded += 1  # would upload
            except Exception as e:  # keep going; report at the end
                failed += 1
                print(f"  FAIL {key}: {e}", file=sys.stderr)

    verb = "uploaded" if args.apply else "would upload"
    print(f"\nScanned {total} files → {verb} {uploaded}, skipped {skipped} (already in S3), failed {failed}.")
    print(f"Bucket: {bucket}  Region: {ss.AWS_REGION}  key prefix: {ss.UPLOADS_PREFIX}/")
    if not args.apply:
        print("Dry run only. Re-run with --apply to upload.")


if __name__ == "__main__":
    main()

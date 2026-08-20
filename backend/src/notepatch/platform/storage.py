import json
from pathlib import Path
from pathlib import PurePosixPath

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from notepatch.platform.config import get_settings
from notepatch.shared.filenames import sanitize_filename


class StorageService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.bucket = self.settings.storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=self.settings.storage_endpoint_url,
            aws_access_key_id=self.settings.storage_access_key,
            aws_secret_access_key=self.settings.storage_secret_key,
            region_name=self.settings.s3_region,
            config=Config(signature_version="s3v4"),
            use_ssl=self.settings.s3_secure,
        )
        self._presign_client = boto3.client(
            "s3",
            endpoint_url=self.settings.storage_public_base_url,
            aws_access_key_id=self.settings.storage_access_key,
            aws_secret_access_key=self.settings.storage_secret_key,
            region_name=self.settings.s3_region,
            config=Config(signature_version="s3v4"),
            use_ssl=self.settings.s3_secure,
        )

    def create_presigned_download_url(
        self,
        bucket: str,
        object_key: str,
        expires_seconds: int | None = None,
    ) -> str:
        return self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expires_seconds or self.settings.presign_expire_seconds,
        )

    def create_presigned_artifact_download_url(
        self,
        bucket: str,
        object_key: str,
        expires_seconds: int | None = None,
    ) -> str:
        return self.create_presigned_download_url(bucket, object_key, expires_seconds)

    @staticmethod
    def filename_for_object_key(object_key: str) -> str:
        return sanitize_filename(PurePosixPath(object_key).name)

    def object_exists(self, bucket: str, object_key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=object_key)
            return True
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status_code == 404:
                return False
            raise

    def get_object_metadata(self, bucket: str, object_key: str) -> dict:
        response = self._client.head_object(Bucket=bucket, Key=object_key)
        return {
            "content_length": response.get("ContentLength"),
            "content_type": response.get("ContentType"),
            "etag": response.get("ETag"),
            "metadata": response.get("Metadata", {}),
        }

    def bucket_exists(self, bucket: str | None = None) -> bool:
        try:
            self._client.head_bucket(Bucket=bucket or self.bucket)
            return True
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status_code == 404:
                return False
            raise

    def delete_object(self, bucket: str, object_key: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=object_key)

    def copy_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        self.ensure_bucket(dst_bucket)
        self._client.copy_object(
            Bucket=dst_bucket,
            Key=dst_key,
            CopySource={"Bucket": src_bucket, "Key": src_key},
        )

    def put_file(
        self,
        bucket: str,
        object_key: str,
        file_path: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.ensure_bucket(bucket)
        extra_args: dict = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if metadata:
            extra_args["Metadata"] = {str(key): str(value) for key, value in metadata.items()}
        if extra_args:
            self._client.upload_file(str(file_path), bucket, object_key, ExtraArgs=extra_args)
        else:
            self._client.upload_file(str(file_path), bucket, object_key)

    def download_file(self, bucket: str, object_key: str, dest_path: str | Path) -> None:
        path = Path(dest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(bucket, object_key, str(path))

    def put_json_artifact(self, object_key: str, payload: dict, bucket: str | None = None) -> None:
        bucket = bucket or self.bucket
        self.ensure_bucket(bucket)
        self._client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    def get_json_artifact(self, object_key: str, bucket: str | None = None) -> dict:
        response = self._client.get_object(Bucket=bucket or self.bucket, Key=object_key)
        payload = json.loads(response["Body"].read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON artifact must contain an object")
        return payload

    def get_text_artifact(self, object_key: str, bucket: str | None = None) -> str:
        response = self._client.get_object(Bucket=bucket or self.bucket, Key=object_key)
        return response["Body"].read().decode("utf-8")

    def delete_prefix(self, prefix: str) -> None:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})

    def ensure_bucket(self, bucket: str | None = None) -> None:
        bucket = bucket or self.bucket
        try:
            self._client.head_bucket(Bucket=bucket)
        except ClientError:
            self._client.create_bucket(Bucket=bucket)

    @staticmethod
    def document_original_key(workspace_id: str, document_id: str, filename: str) -> str:
        return str(PurePosixPath("workspaces", workspace_id, "documents", document_id, "original", filename))

    @staticmethod
    def document_artifact_key(workspace_id: str, document_id: str, artifact_id: str, artifact_type: str, ext: str) -> str:
        return str(
            PurePosixPath(
                "workspaces",
                workspace_id,
                "documents",
                document_id,
                "artifacts",
                artifact_id,
                f"{artifact_type}.{ext}",
            )
        )

    @staticmethod
    def document_processed_key(workspace_id: str, document_id: str, filename: str) -> str:
        return str(PurePosixPath("workspaces", workspace_id, "documents", document_id, "artifacts", filename))

    @staticmethod
    def sandbox_input_key(workspace_id: str, task_id: str, filename: str) -> str:
        return str(PurePosixPath("workspaces", workspace_id, "sandbox", "tasks", task_id, "input", filename))

    @staticmethod
    def sandbox_output_key(workspace_id: str, task_id: str, filename: str) -> str:
        return str(PurePosixPath("workspaces", workspace_id, "sandbox", "tasks", task_id, "output", filename))

    @staticmethod
    def user_avatar_key(user_id: str, version: str, ext: str) -> str:
        return str(PurePosixPath("users", user_id, "profile", "avatar", f"{version}.{ext}"))

    @staticmethod
    def learning_unit_note_key(
        workspace_id: str,
        learning_unit_id: str,
        version_id: str,
        filename: str,
        ext: str,
    ) -> str:
        return str(
            PurePosixPath(
                "workspaces",
                workspace_id,
                "learning-units",
                learning_unit_id,
                "notes",
                version_id,
                f"{filename}.{ext}",
            )
        )

    @staticmethod
    def is_storage_error(exc: Exception) -> bool:
        return isinstance(exc, (BotoCoreError, ClientError, OSError))

    @staticmethod
    def is_object_not_found_error(exc: Exception) -> bool:
        if isinstance(exc, KeyError):
            return True
        if not isinstance(exc, ClientError):
            return False
        response = exc.response or {}
        status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        error_code = str(response.get("Error", {}).get("Code", ""))
        return status_code == 404 or error_code in {"404", "NoSuchKey", "NotFound"}

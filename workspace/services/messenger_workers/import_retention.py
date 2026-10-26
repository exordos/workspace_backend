# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Collect staging batches belonging to the retired history ingress protocol."""

import datetime
import itertools
import pathlib
import re
import typing
import uuid

from workspace.messenger_api import file_storage


PREFIX = "history-imports/v1/"
BATCH_KEY = re.compile(
    r"history-imports/v1/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/batch\.json\Z"
)


class Collector:
    def __init__(self) -> None:
        self._objects: typing.Iterator | None = None

    def _list(self, storage: typing.Any, batch_size: int) -> typing.Iterator:
        if storage.storage_type == "s3":
            pages = storage.client.get_paginator("list_objects_v2").paginate(
                Bucket=storage.bucket_name,
                Prefix=PREFIX,
                PaginationConfig={"PageSize": batch_size},
            )
            for page in pages:
                yield from page.get("Contents", [])
        else:
            root = pathlib.Path(file_storage.get_storage_path())
            for path in (root / PREFIX).rglob("batch.json"):
                if not path.is_file() or path.is_symlink():
                    continue
                yield {
                    "Key": str(path.relative_to(root)),
                    "LastModified": datetime.datetime.fromtimestamp(
                        path.stat().st_mtime, datetime.timezone.utc
                    ),
                }

    def prune(
        self, now: datetime.datetime, retention_seconds: int, batch_size: int
    ) -> tuple[int, bool]:
        storage: typing.Any = file_storage.get_workspace_file_storage()
        if self._objects is None:
            self._objects = self._list(storage, batch_size)
        try:
            objects = list(itertools.islice(self._objects, batch_size))
            cutoff = now - datetime.timedelta(seconds=retention_seconds)
            keys = [
                item["Key"]
                for item in objects
                if BATCH_KEY.fullmatch(item["Key"]) and item["LastModified"] < cutoff
            ]
            if keys and storage.storage_type == "s3":
                result = storage.client.delete_objects(
                    Bucket=storage.bucket_name,
                    Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
                )
                if result.get("Errors"):
                    raise RuntimeError("Retired history batch deletion failed")
            else:
                for key in keys:
                    storage.delete(uuid.UUID(key.split("/")[3]), storage_object_id=key)
            more = len(objects) == batch_size
            if not more:
                self._objects = None
            return len(keys), more
        except Exception:
            self._objects = None
            raise

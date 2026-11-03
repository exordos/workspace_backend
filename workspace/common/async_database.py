# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Small asynchronous PostgreSQL pool shared by provider workers."""

import asyncio
import contextlib
import typing

import psycopg
from psycopg import rows

from workspace.common import database


class Database:
    def __init__(
        self,
        url: str,
        size: int = 4,
        *,
        application_name: str = "workspace-zulip-outgoing",
    ) -> None:
        self.url = database.connection_url_for_process(url, application_name)
        self.application_name = application_name
        self.size = size
        self.connections: asyncio.Queue = asyncio.Queue()

    async def open(self) -> None:
        for _ in range(self.size):
            await self.connections.put(await self.connect())

    async def connect(self) -> psycopg.AsyncConnection[dict[str, typing.Any]]:
        return await psycopg.AsyncConnection.connect(
            self.url,
            autocommit=True,
            row_factory=rows.dict_row,
            application_name=self.application_name,
        )

    async def close(self) -> None:
        while not self.connections.empty():
            await (await self.connections.get()).close()

    async def sql(self, query: str, parameters: tuple = ()) -> list[dict]:
        connection = await self.connections.get()
        try:
            if connection.closed:
                connection = await self.connect()
            cursor = await connection.execute(query, parameters)
            return await cursor.fetchall() if cursor.description else []
        finally:
            self.connections.put_nowait(connection)

    @contextlib.asynccontextmanager
    async def transaction(self) -> typing.AsyncIterator[typing.Any]:
        connection = await self.connections.get()
        try:
            if connection.closed:
                connection = await self.connect()
            async with connection.transaction():
                yield connection
        finally:
            self.connections.put_nowait(connection)

# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Mark API mutations inside the existing request transaction."""

import functools
import typing

from restalchemy.common import contexts


def api_change(method: typing.Callable) -> typing.Callable:
    @functools.wraps(method)
    def change(
        store: typing.Any, *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        session = contexts.Context().get_session()
        previous = session.execute(
            "SELECT current_setting('workspace.outgoing_actor', true) AS actor"
        ).fetchone()["actor"]
        session.execute(
            "SELECT set_config('workspace.outgoing_actor', %s, true)",
            (str(store.user_uuid),),
        )
        try:
            result = method(store, *args, **kwargs)
        except Exception:
            raise
        else:
            session.execute(
                "SELECT set_config('workspace.outgoing_actor', %s, true)",
                (previous or "",),
            )
            return result

    return change

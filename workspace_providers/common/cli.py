import argparse
import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

from workspace_providers.common import client
from workspace_providers.common import state


def parser(description: str, default_name: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument(
        "--backend-url", default=os.environ.get("WORKSPACE_BACKEND_URL")
    )
    result.add_argument(
        "--provider-uuid", default=os.environ.get("WORKSPACE_PROVIDER_UUID")
    )
    result.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    result.add_argument(
        "--provider-name",
        default=os.environ.get("WORKSPACE_PROVIDER_NAME", default_name),
        help="Short provider name displayed in Workspace delivery badges",
    )
    result.add_argument(
        "--api-prefix",
        default=os.environ.get(
            "WORKSPACE_SERVICE_API_PREFIX", client.DEFAULT_API_PREFIX
        ),
    )
    result.add_argument("--poll-interval", type=float, default=5.0)
    result.add_argument("--log-level", default="INFO")
    return result


def run(
    argv: list[str] | None,
    description: str,
    provider_kind: str,
    daemon_factory: Callable[..., Any],
    name: str,
) -> None:
    args = parser(description, name).parse_args(argv)
    for field in ("backend_url", "provider_uuid", "database_url"):
        if getattr(args, field) is None:
            raise SystemExit(f"--{field.replace('_', '-')} is required")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    provider_uuid = uuid.UUID(args.provider_uuid)
    service_client = client.WorkspaceServiceClient(
        args.backend_url,
        provider_uuid,
        api_prefix=args.api_prefix,
    )
    repository = state.PostgresStateRepository(
        args.database_url,
        provider_kind,
    )
    provider = daemon_factory(
        provider_uuid=provider_uuid,
        name=args.provider_name,
        service_client=service_client,
        repository=repository,
        poll_interval=args.poll_interval,
    )
    try:
        provider.run()
    except KeyboardInterrupt:
        provider.stop()

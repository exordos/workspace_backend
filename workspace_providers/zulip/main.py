from workspace_providers.common import cli
from workspace_providers.zulip import provider


def main(argv: list[str] | None = None) -> None:
    cli.run(
        argv,
        "Workspace Zulip provider",
        "zulip",
        provider.ZulipProviderDaemon,
        "Zulip",
    )


if __name__ == "__main__":
    main()

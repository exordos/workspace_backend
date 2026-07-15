from workspace_providers.calendar import provider
from workspace_providers.common import cli


def main(argv: list[str] | None = None) -> None:
    cli.run(
        argv,
        "Workspace CalDAV provider",
        "calendar",
        provider.CalendarProviderDaemon,
        "Calendar",
    )


if __name__ == "__main__":
    main()

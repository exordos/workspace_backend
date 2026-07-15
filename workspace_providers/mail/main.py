from workspace_providers.common import cli
from workspace_providers.mail import provider


def main(argv: list[str] | None = None) -> None:
    cli.run(
        argv,
        "Workspace IMAP/SMTP provider",
        "mail",
        provider.MailProviderDaemon,
        "Mail",
    )


if __name__ == "__main__":
    main()

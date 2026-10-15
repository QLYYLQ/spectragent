"""The experiment command line; business execution stays in the existing runners."""


def main(argv: list[str] | None = None) -> int:
    from spectragent.cli.app import main as invoke

    return invoke(argv)

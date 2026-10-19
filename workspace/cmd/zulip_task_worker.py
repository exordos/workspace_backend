# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Run realtime bridge tasks independently of background history work."""

from workspace.cmd import history_import


def main():
    history_import.main(queue="realtime")


if __name__ == "__main__":
    main()

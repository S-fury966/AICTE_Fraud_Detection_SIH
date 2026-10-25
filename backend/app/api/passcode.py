"""python -m app.api.passcode <role> - print a config block for a new passcode.

Reads the passcode without echoing it and prints the salt and hash to paste
under ``sign_in.passcodes.<role>`` in config/api.yaml. The passcode itself is
never written anywhere.
"""

from __future__ import annotations

import getpass
import os
import sys

from .sessions import hash_passcode

ITERATIONS = 200_000


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m app.api.passcode <role>", file=sys.stderr)
        return 2
    passcode = getpass.getpass(f"New passcode for {args[0]}: ")
    if len(passcode) < 8:
        print("use at least 8 characters", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != passcode:
        print("the two entries differ", file=sys.stderr)
        return 1
    salt = os.urandom(16)
    print(f"    {args[0]}:")
    print(f"      salt: {salt.hex()}")
    print(f"      iterations: {ITERATIONS}")
    print(f"      pbkdf2_sha256: {hash_passcode(passcode, salt, ITERATIONS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

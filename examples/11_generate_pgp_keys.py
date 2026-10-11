"""Makes the bot's OpenPGP key and saves it, protected by a passphrase.

    python examples/11_generate_pgp_keys.py --name Weather --username weatherbot

It asks for the passphrase (or reads BOT_KEY_PASSPHRASE), writes bot-key.asc and
bot-key.pub.asc, and prints the fingerprint. It does not publish anything:
see 10_encrypted_bot.py, or run `pgpcbot publish-key bot-key.asc`.

`pgpcbot keygen` does the same from the command line.
"""

import argparse
import getpass
import os
from pathlib import Path

from pgpcbot import BotKey, format_fingerprint


def main() -> None:
    parser = argparse.ArgumentParser(description="Make a key for a PGP Chat bot.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--username")
    parser.add_argument("--out", default="bot-key.asc")
    args = parser.parse_args()

    passphrase = os.environ.get("BOT_KEY_PASSPHRASE") or getpass.getpass("Passphrase for the new key: ")
    if not passphrase:
        raise SystemExit("a passphrase is needed: the private key is the bot's identity")

    key = BotKey.generate(args.name, username=args.username, passphrase=passphrase)
    out = key.save(args.out, passphrase=None)  # already protected by the passphrase above
    public = Path(args.out).with_suffix(".pub.asc")
    public.write_text(key.export_public(), encoding="ascii", newline="\n")

    print(f"private key: {out}")
    print(f"public key:  {public}")
    print(f"fingerprint: {format_fingerprint(key.fingerprint)}")
    print("Back up the private key and its passphrase, separately, somewhere offline.")


if __name__ == "__main__":
    main()

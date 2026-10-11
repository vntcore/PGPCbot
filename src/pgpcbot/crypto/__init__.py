"""OpenPGP for bots: the bot's key, other people's keys, sealing and opening.

Most bots never use this package directly: `~pgpcbot.Bot` seals and opens
messages on its own. It is here for the keys (making, loading, saving,
exporting the public part) and for doing the OpenPGP part by hand. It has
four public names, all also importable from ``pgpcbot``:

- `~pgpcbot.BotKey`: the bot's own key pair, with the private key.
- `~pgpcbot.PublicKey`: somebody's public key, to seal to and to check
  signatures against.
- `~pgpcbot.Decrypted`: an opened message, with the result of its signature
  check.
- `~pgpcbot.format_fingerprint`: a fingerprint in groups of four.

Two engines do the work: Sequoia PGP (``pysequoia``) seals and opens
messages, and rPGP (``rpgp-py``) makes keys and checks signatures. Nobody has
audited this code independently. The Cryptography page of the documentation
(``docs/CRYPTOGRAPHY.md``) says what the bot does with OpenPGP and which risks
remain.

Example:
    Make the bot's key once, and save it sealed with a passphrase::

        import os
        from pgpcbot.crypto import BotKey

        key = BotKey.generate("Weather", username="weatherbot")
        key.save("bot-key.asc", passphrase=os.environ["BOT_KEY_PASSPHRASE"])
"""

from .keys import BotKey, Decrypted, PublicKey, format_fingerprint

__all__ = ["BotKey", "Decrypted", "PublicKey", "format_fingerprint"]

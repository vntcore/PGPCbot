"""The bot's own key pair, and other people's public keys.

`~pgpcbot.BotKey` holds the bot's private key: it seals and signs messages,
and opens the ones sealed to it. `~pgpcbot.PublicKey` is somebody else's key,
to seal to and to check signatures against. `~pgpcbot.Decrypted` is what
opening a message gives back, and `~pgpcbot.format_fingerprint` shows a
fingerprint the way people read it. All four are importable from ``pgpcbot``
and from ``pgpcbot.crypto``.

Sequoia PGP (``pysequoia``) seals and opens messages; rPGP (``rpgp-py``) makes
keys and checks signatures. Warnings go to the ``pgpcbot.crypto`` logger. They
may name a fingerprint or a file, never a private key, a passphrase or
decrypted text.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._secrets import Secret, read_env, resolve
from .._validate import FINGERPRINT_RE
from ..errors import (
    ConfigurationError,
    CryptoError,
    DecryptionError,
    EncryptionError,
    InvalidKeyError,
    PassphraseError,
)
from ..models import SignatureStatus, UserKey, Verification
from . import _lib

if TYPE_CHECKING:
    import pysequoia

__all__ = ["BotKey", "Decrypted", "PublicKey", "format_fingerprint"]

log = logging.getLogger("pgpcbot.crypto")

ENV_KEY = "PGPCHAT_BOT_KEY"
ENV_KEY_FILE = "PGPCHAT_BOT_KEY_FILE"
ENV_PASSPHRASE = "PGPCHAT_BOT_KEY_PASSPHRASE"  # noqa: S105 - a variable name

_PUBLIC_HEADER = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
_PRIVATE_HEADER = "-----BEGIN PGP PRIVATE KEY BLOCK-----"
_MESSAGE_HEADER = "-----BEGIN PGP MESSAGE-----"


def format_fingerprint(fingerprint: str) -> str:
    """Splits a fingerprint into groups of four, the way people read them out to each other.

    Spaces are removed and letters made upper case first, so the result is
    the same with or without spaces, in upper or lower case. Nothing is
    checked: any text is grouped.

    Args:
        fingerprint: The fingerprint, with or without spaces.

    Returns:
        The fingerprint in upper case, in groups of four separated by spaces.

    Example:
        >>> format_fingerprint("3f1a9c0d52e7b8a46c110d93e2f47a559b10c6d2")
        '3F1A 9C0D 52E7 B8A4 6C11 0D93 E2F4 7A55 9B10 C6D2'
    """
    fp = fingerprint.replace(" ", "").upper()
    return " ".join(fp[i : i + 4] for i in range(0, len(fp), 4))


def _as_bytes(data: str | bytes) -> bytes:
    if isinstance(data, bytes):
        # Notepad and PowerShell 5 put a byte order mark in front, Sequoia won't read past it
        return data.removeprefix(b"\xef\xbb\xbf")
    if isinstance(data, str):
        return data.removeprefix("\ufeff").encode("utf-8")
    raise TypeError(f"expected str or bytes, got {type(data).__name__}")


def _read(path: str | os.PathLike[str], what: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"cannot read the {what} file {path}: {exc.strerror}") from None


class PublicKey:
    """Somebody's OpenPGP public key, to seal messages to and check signatures against.

    Read one with `from_armored`, `from_file` or `from_user_key`. The last
    one is for keys the server hands out: it also checks the fingerprint
    the server claims. None of them keeps secret material: an armored
    private key is refused, and secret parts in other data are dropped.

    Keys compare and hash by fingerprint, so a key can go in a set or be a
    dict key. ``repr()`` shows the fingerprint in groups of four, the
    version and the first user ID.

    Attributes:
        fingerprint (str): The key's fingerprint in upper-case hex, without
            spaces. 40 digits for a v4 key, 64 for a v6 key.
    """

    __slots__ = ("_armored", "_cert", "_rp", "_rp_loaded", "fingerprint")

    def __init__(self, cert: pysequoia.Cert) -> None:
        """Wraps a certificate that pysequoia has already read.

        Most code reads keys with `from_armored`, `from_file` or
        `from_user_key` instead: they check what they read and keep only
        the public part.

        Args:
            cert: The certificate, a ``pysequoia.Cert``.
        """
        self._cert = cert
        self._armored: str = str(cert)
        self.fingerprint: str = cert.fingerprint.upper()
        self._rp: Any = None
        self._rp_loaded = False

    @classmethod
    def from_armored(cls, data: str | bytes) -> PublicKey:
        """Reads one public key from text or bytes.

        Args:
            data: A ``-----BEGIN PGP PUBLIC KEY BLOCK-----``, or the same key
                in binary form. A byte order mark at the start is skipped.

        Returns:
            The key. Only its public part is kept.

        Raises:
            InvalidKeyError: ``data`` holds an armored private key block,
                more than one key, or no readable OpenPGP key.
        """
        raw = _as_bytes(data)
        if _PRIVATE_HEADER.encode() in raw:
            raise InvalidKeyError("that is a private key; a PublicKey is built from the public part only")
        return cls(_lib.load_cert(raw))

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> PublicKey:
        """Reads one public key from a file, armored or binary.

        Args:
            path: The key file.

        Returns:
            The key, read as `from_armored` reads it. From a binary private
            key, only the public part is kept.

        Raises:
            ConfigurationError: The file can't be read.
            InvalidKeyError: The file holds an armored private key block,
                more than one key, or no readable OpenPGP key.
        """
        return cls.from_armored(_read(path, "key"))

    @classmethod
    def from_user_key(cls, user_key: UserKey) -> PublicKey:
        """Reads a key the server handed out, and checks the fingerprint the server gives for it.

        The server stores whatever a phone published and never parses it,
        so the key is read here and its real fingerprint is compared with
        the one the server lists; spaces and case do not count. When the
        server lists no fingerprint, there is nothing to compare and the key
        is taken as it is.

        A match shows that the key and the fingerprint go together, not that
        the key belongs to that person: the server hands out both. To notice
        a key that changes, use ``Bot(pin_keys=True)``; for the people who
        matter, compare fingerprints over another channel.

        Args:
            user_key: A person's key as the server lists it, such as one of
                the ``keys`` that `AsyncClient.get_chat_keys` returns.

        Returns:
            The key.

        Raises:
            InvalidKeyError: The key can't be read, is a private key block,
                or does not have the fingerprint the server lists; in that
                last case the error names both fingerprints.
        """
        key = cls.from_armored(user_key.public_key)
        claimed = (user_key.fingerprint or "").replace(" ", "").upper()
        if claimed and claimed != key.fingerprint:
            raise InvalidKeyError(
                f"the server lists fingerprint {claimed} for user {user_key.user_id}, "
                f"but the key it sent is {key.fingerprint}"
            )
        return key

    @property
    def armored(self) -> str:
        """The key, ASCII-armored (``-----BEGIN PGP PUBLIC KEY BLOCK-----``)."""
        return self._armored

    @property
    def version(self) -> int:
        """The OpenPGP key version, ``4`` or ``6``, told by the length of the fingerprint.

        Phones on PGPainless 1.7 can't use v6 keys. `Bot` never seals to
        one (a group member with a v6 key is left out, a send to a private
        chat fails) and refuses to publish one as its own key.
        `BotKey.encrypt` does not look at the version.
        """
        return 6 if len(self.fingerprint) == 64 else 4

    @property
    def key_id(self) -> str:
        """The key ID, 16 upper-case hex digits.

        The last 16 digits of the fingerprint for a v4 key, the first 16 for
        a v6 key, as OpenPGP defines it.
        """
        return self.fingerprint[-16:] if self.version == 4 else self.fingerprint[:16]

    @property
    def user_ids(self) -> list[str]:
        """The user IDs of the key, as text.

        ``["Weather (@weatherbot)"]`` for a key made by
        ``BotKey.generate("Weather", username="weatherbot")``. Empty when the
        key has none.
        """
        return [str(uid) for uid in self._cert.user_ids]

    @property
    def expires_at(self) -> datetime | None:
        """When the key expires, in UTC, or ``None`` when it never expires.

        This is the expiry of the key as a whole. A subkey can expire
        sooner: `usable` checks that too.
        """
        expiration: datetime | None = self._cert.expiration
        return expiration

    @property
    def revoked(self) -> bool:
        """Whether the key as a whole has been revoked.

        A revoked subkey alone does not count here; `usable` checks the
        subkeys too.
        """
        return bool(self._cert.is_revoked)

    @property
    def expired(self) -> bool:
        """Whether `expires_at` has passed, checked against the clock on each call."""
        expires = self.expires_at
        return expires is not None and expires.timestamp() <= time.time()

    @property
    def usable(self) -> bool:
        """Whether a message can be sealed to this key now.

        ``True`` when the key is not revoked, not expired, and has an
        encryption subkey that is neither revoked nor expired. Worked out on
        every call, not cached: keys expire while a bot runs.
        `BotKey.encrypt` refuses a key that is not usable.

        Sealing with Sequoia alone would accept a revoked key, and fall back
        to an expired subkey when there is no live one: this check is what
        stops both. rPGP looks at the subkeys; when it can't read the key,
        Sequoia's answer stands.
        """
        if self.revoked or self.expired or not _lib.can_encrypt_to(self._cert):
            return False
        rp_key = self._rpgp()
        live = _lib.has_live_encryption_subkey(rp_key, time.time()) if rp_key is not None else None
        # None: rPGP can't judge this key, and Sequoia's answer is the one left
        return live is not False

    def _rpgp(self) -> Any:
        if not self._rp_loaded:
            self._rp = _lib.rp_public(self._armored)
            self._rp_loaded = True
        return self._rp

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PublicKey) and other.fingerprint == self.fingerprint

    def __hash__(self) -> int:
        return hash(self.fingerprint)

    def __repr__(self) -> str:
        name = self.user_ids[0] if self.user_ids else "no user id"
        return f"<PublicKey {format_fingerprint(self.fingerprint)} v{self.version} {name!r}>"


@dataclass(frozen=True, slots=True)
class Decrypted:
    """An opened message, with what its signature check found.

    `BotKey.decrypt` returns it. It is read-only.

    Attributes:
        data: The decrypted bytes, exactly as they were sealed.
        verification: What the signature check found. Its ``status`` is
            `SignatureStatus.NOT_CHECKED` when `BotKey.decrypt` got no
            ``verify_with`` keys. Look at it before you trust ``data``: a bad
            signature does not stop decryption.

    Note:
        A phone writes the app's message format, not always plain words. A
        text message is the text itself; a photo, an album, a forward or a
        sticker starts with a marker followed by JSON. `Bot` reads that
        format for you and fills ``message.text`` and ``message.media``.
    """

    data: bytes
    verification: Verification

    @property
    def text(self) -> str:
        """The data decoded as UTF-8, with invalid bytes replaced by U+FFFD.

        Line ends stay as they were sealed: text from OpenPGP.js has CRLF.
        """
        return self.data.decode("utf-8", errors="replace")


class BotKey:
    """The bot's own OpenPGP key pair, private key included.

    The private key is the bot's identity: whoever has it can read every
    message sealed to the bot and sign in its name. Keep it like the token.

    Make a new key with `generate`, or load one with `from_armored`,
    `from_file` or `from_env`. `Bot` does the loading for you, from its
    ``pgp_private_key`` argument or from the environment.

    How the private key is kept:

    - Nothing in this class talks to the network. Only the public half is
      meant to leave the process: `Bot.publish_key` sends `export_public`
      with ``PUT /key``, and the client refuses a private key block there.
    - Nothing writes the key to disk but `save`.
    - The key and its passphrase are never logged. ``repr()`` shows only
      the fingerprint and whether the key is protected
      (``<BotKey 3F1A 9C0D ... protected>``). ``pickle``, ``copy.copy``
      and ``copy.deepcopy`` raise `TypeError`.
    - A protected key is unlocked once, when it is loaded, so a wrong
      passphrase fails at start-up rather than on the first message. The
      passphrase stays in memory, wrapped in a `Secret`, because rPGP needs
      it for each message it opens: to check the signature, and to look
      for compression.
    - Python can't wipe memory: the unlocked key lives in the process for
      as long as the object does. Treat the process like the key (no
      readable core dumps, no debugger that others can reach).

    Attributes:
        public_key (PublicKey): The public half: what other people seal
            messages to and check the bot's signatures against.

    Example:
        Make the key once, then load it each time the bot starts::

            passphrase = os.environ["BOT_KEY_PASSPHRASE"]
            key = BotKey.generate("Weather", username="weatherbot", passphrase=passphrase)
            key.save("bot-key.asc", passphrase=None)  # already protected

            key = BotKey.from_file("bot-key.asc", passphrase)

    Note:
        The methods are plain blocking calls. `Bot` runs sealing and opening
        in a worker thread (``asyncio.to_thread``); do the same in async code
        of your own, so the event loop is not held up.
    """

    __slots__ = ("_decryptor", "_passphrase", "_rp", "_secret", "_signer", "_tsk", "public_key")

    def __init__(self, tsk: pysequoia.Tsk, secret_armored: str, passphrase: str | None) -> None:
        """Wraps a private key that pysequoia has read, and unlocks it.

        Most code never calls this: `generate`, `from_armored`, `from_file`
        and `from_env` read the key and call it for you.

        The signing key and the encryption subkey are unlocked here, once.
        Two warnings can be logged on the ``pgpcbot.crypto`` logger, naming
        the fingerprint at most: when rPGP can't read the key (incoming
        signatures then come back `SignatureStatus.NOT_CHECKED`, and
        `decrypt` can't look for compression), and when the key announces
        SEIPDv2, a message format phones on PGPainless 1.7 can't open (see
        `encrypt`).

        Args:
            tsk: The private key, as a ``pysequoia.Tsk``.
            secret_armored: The same key, ASCII-armored: what rPGP reads, and
                what `export_secret` returns and `save` writes when they get
                no passphrase.
            passphrase: The passphrase that unlocks the key, or ``None``.
                Not used, and not kept, when the key is not locked: the
                passphrase of a sealed file around the key, for example.

        Raises:
            PassphraseError: The key is locked and ``passphrase`` is missing
                or wrong.
            InvalidKeyError: The key has no signing key or no encryption
                subkey it can use now (expired or revoked), or Sequoia can't
                use it for another reason.
        """
        self._tsk = tsk
        self._secret = secret_armored
        locked = _lib.is_locked(tsk)
        if locked and not passphrase:
            raise PassphraseError("the private key is protected by a passphrase and none was given")
        # a passphrase for a key that has none (say, the one that sealed the file around
        # it) is simply not needed here: Sequoia would refuse it
        self._passphrase = Secret(passphrase) if locked and passphrase else None
        self._signer, self._decryptor = _lib.unlock(tsk, passphrase if locked else None)
        self._rp = _lib.rp_secret(secret_armored)
        if self._rp is not None:
            public_armored = self._rp.to_public_key().to_armored()
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                public_armored = str(tsk.extract_certificate())
        self.public_key = PublicKey(_lib.load_cert(public_armored.encode()))
        if self._rp is None:
            log.warning("rPGP could not read this key: signatures of incoming messages won't be checked")
        elif _lib.advertises_seipd_v2(self._rp.to_public_key()):
            log.warning(
                "key %s advertises SEIPDv2. Phones on PGPainless 1.7 can't read SEIPDv2, and Sequoia "
                "uses it when every recipient advertises it: a key made with BotKey.generate() avoids that",
                self.fingerprint,
            )

    # --- making and loading ---

    @classmethod
    def generate(
        cls, name: str = "PGP Chat bot", *, username: str | None = None, passphrase: str | None = None
    ) -> BotKey:
        """Makes a new key pair, of the kind every phone can use.

        The key is OpenPGP v4, the same kind the phones make: an Ed25519
        primary key (EdDSA, algorithm 22) that certifies and signs, and a
        Curve25519 subkey (ECDH, algorithm 18) that encrypts. Its features
        say SEIPDv1 only. Every message the bot seals is also sealed to its
        own key, so Sequoia always writes SEIPDv1, which phones on
        PGPainless 1.7 can open, whatever keys the other members have. Its
        preferences list AES-256 and AES-128, SHA-512 and SHA-256, and no
        compression, ZLIB or ZIP.

        The key never expires: an expired key could not open any message,
        old ones included. To change it, rotate it (`Bot.rotate_key`).

        rPGP makes the key in memory. Nothing is sent or written: call
        `save` to keep it.

        Args:
            name: The name in the key's user ID. Spaces at the ends are
                removed.
            username: The bot's handle, with or without the ``@``. With it,
                the user ID is ``"Name (@username)"``; without it, the name
                alone.
            passphrase: Protects the private parts of the key in every
                export: iterated and salted S2K with SHA-256 and the highest
                iteration count, then AES-256. Any OpenPGP tool can unlock it
                with the passphrase. ``None`` (the default) makes a key in
                clear.

        Returns:
            The new key, unlocked and ready to use.

        Raises:
            ConfigurationError: ``name`` is empty or only spaces, or
                ``passphrase`` is an empty string.

        Example:
            A key protected by a passphrase from the environment::

                key = BotKey.generate(
                    "Weather", username="weatherbot", passphrase=os.environ["BOT_KEY_PASSPHRASE"]
                )
                print(key.fingerprint)
        """
        from openpgp.composed import EncryptionCaps, KeyType, SecretKeyParamsBuilder, SubkeyParamsBuilder
        from openpgp.types import S2kParams, StringToKey

        if not name.strip():
            raise ConfigurationError("the key needs a name for its user ID")
        if passphrase is not None and not passphrase:
            raise ConfigurationError("an empty passphrase protects nothing: pass None for an unprotected key")
        user_id = f"{name.strip()} (@{username.lstrip('@')})" if username else name.strip()
        builder = (
            SecretKeyParamsBuilder()
            .version(4)
            .key_type(KeyType.ed25519_legacy())
            .can_certify(True)
            .can_sign(True)
            # v1 only, on purpose: PGPainless 1.7 can't open SEIPDv2, and every message
            # the bot seals includes its own key, so this keeps them all on v1
            .feature_seipd_v1(True)
            .feature_seipd_v2(False)
            .preferred_symmetric_algorithms(["aes256", "aes128"])
            .preferred_hash_algorithms(["sha512", "sha256"])
            .preferred_compression_algorithms(["uncompressed", "zlib", "zip"])
            .primary_user_id(user_id)
        )
        subkey = SubkeyParamsBuilder().version(4).key_type(KeyType.ecdh("curve25519")).can_encrypt(EncryptionCaps.all())
        if passphrase:
            builder = builder.passphrase(passphrase).s2k(S2kParams.cfb("aes256", StringToKey.iterated("sha256", 255)))
            subkey = subkey.passphrase(passphrase).s2k(S2kParams.cfb("aes256", StringToKey.iterated("sha256", 255)))
        secret = builder.subkey(subkey.build()).generate()
        armored = secret.to_armored()
        return cls(_lib.load_tsk(armored.encode()), armored, passphrase or None)

    @classmethod
    def from_armored(
        cls,
        data: str | bytes,
        passphrase: str | Secret | Callable[[], str] | None = None,
    ) -> BotKey:
        """Loads a private key from text or bytes, and unlocks it.

        ``data`` is a ``-----BEGIN PGP PRIVATE KEY BLOCK-----``, protected by
        a passphrase or not (the binary form works too), or a
        ``-----BEGIN PGP MESSAGE-----`` that `save` or `export_secret` sealed
        with a passphrase. A byte order mark at the start, as Notepad and
        PowerShell 5 write it, is skipped.

        Args:
            data: The key.
            passphrase: Opens a sealed file and unlocks a protected key; one
                passphrase serves both. A string, a `Secret`, or a function
                that returns one, called once, here. ``None`` for a key in
                clear.

        Returns:
            The key, unlocked.

        Raises:
            PassphraseError: The key or the sealed file needs a passphrase
                and none was given, or the passphrase is wrong.
            InvalidKeyError: ``data`` is a public key or not an OpenPGP
                private key, or the key has no signing key or encryption
                subkey it can use now.
            DecryptionError: A sealed file can't be opened for another
                reason.
            ConfigurationError: ``passphrase`` is not a string, a `Secret` or
                a function, or the function returned something other than a
                string, a `Secret` or ``None``.
        """
        raw = _as_bytes(data)
        secret = resolve(passphrase, "passphrase")
        if _MESSAGE_HEADER.encode() in raw:
            if not secret:
                raise PassphraseError("this key file is sealed with a passphrase and none was given")
            raw = _lib.decrypt_with_password(raw, secret)
        if _PUBLIC_HEADER.encode() in raw and _PRIVATE_HEADER.encode() not in raw:
            raise InvalidKeyError("that is a public key: the bot needs its private key")
        tsk = _lib.load_tsk(raw)
        text: str | None = None
        if _PRIVATE_HEADER.encode() in raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # a latin-1 "Comment:" line, say: let Sequoia write a clean armor instead
                text = None
        return cls(tsk, text if text is not None else _armor_secret(tsk), secret)

    @classmethod
    def from_file(
        cls,
        path: str | os.PathLike[str],
        passphrase: str | Secret | Callable[[], str] | None = None,
    ) -> BotKey:
        """Loads a private key from a file, and unlocks it.

        The file is read whole and handed to `from_armored`. Outside
        Windows, a warning is logged when the file's group or other users
        have any access to it (``chmod 600`` fixes that). Windows is not
        checked: keep the file in a folder only the service's user can read.

        Args:
            path: The key file.
            passphrase: As in `from_armored`.

        Returns:
            The key, unlocked.

        Raises:
            ConfigurationError: The file can't be read, or ``passphrase`` is
                of the wrong kind, as in `from_armored`.
            PassphraseError: As in `from_armored`.
            InvalidKeyError: As in `from_armored`.
            DecryptionError: As in `from_armored`.
        """
        data = _read(path, "key")
        _warn_if_open(Path(path))
        return cls.from_armored(data, passphrase)

    @classmethod
    def from_env(cls, passphrase: str | Secret | Callable[[], str] | None = None) -> BotKey | None:
        """Loads the private key the environment names, or returns ``None``.

        The key comes from the file named by ``PGPCHAT_BOT_KEY_FILE`` when
        that is set, or else from ``PGPCHAT_BOT_KEY``: the armored key
        itself, or the path of a key file. The file named by
        ``PGPCHAT_BOT_KEY_FILE`` is read as UTF-8 text, so it must hold the
        armored key (a binary key there fails with `UnicodeDecodeError`). A
        path in ``PGPCHAT_BOT_KEY`` goes through `from_file`, which reads
        both forms and checks the file's permissions.

        The passphrase is ``passphrase`` when given, or else the contents of
        the file named by ``PGPCHAT_BOT_KEY_PASSPHRASE_FILE``, or else
        ``PGPCHAT_BOT_KEY_PASSPHRASE``. The line ends an editor leaves at
        the end of a file, and a byte order mark at its start, are not part
        of the value.

        A file is safer than a variable: child processes inherit the
        environment, and crash dumps include it.

        `Bot` calls this when it gets no ``pgp_private_key``.

        Args:
            passphrase: Used instead of the passphrase in the environment.
                As in `from_armored`.

        Returns:
            The key, unlocked, or ``None`` when neither
            ``PGPCHAT_BOT_KEY_FILE`` nor ``PGPCHAT_BOT_KEY`` is set, or the
            key text is blank.

        Raises:
            ConfigurationError: A file named by one of the variables can't
                be read, or ``passphrase`` is of the wrong kind, as in
                `from_armored`.
            PassphraseError: As in `from_armored`.
            InvalidKeyError: As in `from_armored`.
            DecryptionError: As in `from_armored`.
        """
        value = read_env(ENV_KEY)
        if value is None or not value.strip():
            return None
        secret = resolve(passphrase, "passphrase") if passphrase is not None else read_env(ENV_PASSPHRASE)
        if "-----BEGIN PGP" in value:
            return cls.from_armored(value, secret)
        return cls.from_file(value.strip(), secret)

    # --- what it is ---

    @property
    def fingerprint(self) -> str:
        """The key's fingerprint, in upper-case hex without spaces.

        40 digits for a v4 key such as the ones `generate` makes. Show it to
        people with `format_fingerprint`.
        """
        return self.public_key.fingerprint

    @property
    def protected(self) -> bool:
        """Whether the private key itself is protected by a passphrase.

        ``True`` for a key made by `generate` with a ``passphrase``, or
        loaded from a protected ``PRIVATE KEY BLOCK``: `export_secret` and
        `save` then write it protected. ``False`` for a key in clear, even
        one loaded from a file that `save` sealed with a passphrase: that
        passphrase protected the file, not the key inside it, and
        `export_secret` without a passphrase returns the key in clear.
        """
        return self._passphrase is not None

    def export_public(self) -> str:
        """Exports the public key, ASCII-armored.

        This is the part people may see: `Bot.publish_key` sends it with
        ``PUT /key``. It holds no secret material.

        Returns:
            The ``-----BEGIN PGP PUBLIC KEY BLOCK-----`` text, the same as
            ``public_key.armored``.
        """
        return self.public_key.armored

    def export_secret(self, *, passphrase: str | None = None) -> str:
        """Exports the private key, ASCII-armored.

        Without ``passphrase`` the key comes out as it is stored: protected
        if it has a passphrase of its own (see `protected`), in clear
        otherwise. With one, the whole armored key is sealed in a
        password-encrypted OpenPGP message, the same format as ``gpg -c``,
        which `from_armored`, ``gpg -d`` and ``sq decrypt`` can open.

        A key with a passphrase of its own can be sealed only with that
        same passphrase. `from_armored` takes one passphrase for the file
        and the key inside it, so a file sealed with another one could never
        be loaded back.

        Args:
            passphrase: Seals the key with this passphrase. ``None`` (the
                default) exports it as it is stored.

        Returns:
            The ``PRIVATE KEY BLOCK``, or the ``PGP MESSAGE`` that holds it.

        Raises:
            ConfigurationError: ``passphrase`` is an empty string, or the key
                has a passphrase of its own and ``passphrase`` is another one.
            EncryptionError: Sealing with the passphrase failed.

        Warning:
            Whoever has the result, and its passphrase if it has one, can
            read every message sealed to the bot and sign in its name. Never
            log it or send it anywhere.
        """
        if passphrase is None:
            return self._secret
        if not passphrase:
            raise ConfigurationError("an empty passphrase protects nothing")
        if self._passphrase is not None and passphrase != self._passphrase.reveal():
            # the file would open with one passphrase and the key inside with another, and
            # from_armored takes only one: nobody could load it back
            raise ConfigurationError(
                "this key is already protected by its own passphrase: export it with passphrase=None, "
                "or with that same passphrase"
            )
        return _lib.encrypt_with_password(self._secret.encode("utf-8"), passphrase).decode("ascii")

    def save(self, path: str | os.PathLike[str], *, passphrase: str | None, overwrite: bool = False) -> Path:
        """Writes the private key to a file.

        ``passphrase`` has no default on purpose: writing a private key in
        clear should be a decision, not a default. With ``None`` the key is
        written as it is stored (see `export_secret`): protected if it has a
        passphrase of its own, in clear otherwise, with a warning in the
        log. With a passphrase, the file holds the key sealed in a
        password-encrypted OpenPGP message.

        The key goes into a new file in the same folder, which then replaces
        ``path``: an old file's permissions are never reused, and a symbolic
        link at ``path`` is replaced, not followed. Outside Windows the new
        file is private from the first byte (mode ``0600``): only its owner
        can read it. On Windows it gets the permissions of its folder: keep
        it in a folder only the service's user can read. Missing folders are
        created.

        Args:
            path: Where to write the key.
            passphrase: Seals the key with this passphrase, or ``None`` to
                write it as it is stored. Required, even as ``None``.
            overwrite: Replace what is already at ``path``. Off by default.

        Returns:
            ``path``, as a `~pathlib.Path`.

        Raises:
            ConfigurationError: Something is already at ``path``, even a
                broken symbolic link, and ``overwrite`` is ``False``; or
                ``passphrase`` is empty, or is not the key's own passphrase
                when the key has one.
            EncryptionError: Sealing with the passphrase failed.
            OSError: The folder or the file can't be written.

        Example:
            A key generated with a passphrase is written as it is; a key in
            clear is sealed with one::

                protected_key.save("bot-key.asc", passphrase=None)
                clear_key.save("backup.asc", passphrase=os.environ["BACKUP_PASSPHRASE"])

        Warning:
            ``passphrase=None`` on a key in clear writes the private key in
            clear, with only a warning in the log. Do it only when the file
            is protected some other way.
        """
        target = Path(path)
        if (target.exists() or target.is_symlink()) and not overwrite:
            raise ConfigurationError(f"{target} exists; pass overwrite=True to replace it")
        if passphrase is None and not self.protected:
            log.warning("writing the private key to %s without a passphrase", target)
        payload = self.export_secret(passphrase=passphrase).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        # a new file of our own, private from the first byte, then moved over the target:
        # an old file's loose mode is never reused and a planted symlink is replaced, not followed
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            if not overwrite and (target.exists() or target.is_symlink()):
                raise ConfigurationError(f"{target} exists; pass overwrite=True to replace it")
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    # --- using it ---

    def seal(self, text: str | bytes, recipients: Iterable[PublicKey]) -> str:
        """Encrypts and signs a message, ASCII-armored: what goes into a chat.

        The same as ``encrypt(text, recipients, sign=True, armor=True)``;
        the result goes in the ``armored`` field of a message. `Bot.seal`
        does this for a whole chat, with the recipients the server lists and
        the same checks as an automatic send.

        Args:
            text: The message, as ``str`` (sealed as UTF-8) or ``bytes``.
            recipients: The keys to seal it to. The bot's own key is always
                added.

        Returns:
            The ``-----BEGIN PGP MESSAGE-----`` text.

        Raises:
            EncryptionError: A recipient is revoked, expired or has no live
                encryption subkey, Sequoia could not seal the message, or
                the result came out as SEIPDv2. There is no fallback to
                clear text.
            TypeError: ``text`` is not ``str`` or ``bytes``, or a recipient
                is not a `PublicKey`.

        Example:
            By hand, for one person::

                anna = PublicKey.from_file("anna.asc")
                armored = key.seal("hi Anna", [anna])
        """
        out = self.encrypt(text, recipients, sign=True, armor=True)
        assert isinstance(out, str)
        return out

    def encrypt(
        self,
        data: str | bytes,
        recipients: Iterable[PublicKey],
        *,
        sign: bool = True,
        armor: bool = True,
    ) -> str | bytes:
        """Seals data to some keys and to the bot's own key.

        Every recipient must be `PublicKey.usable`, or nothing is sealed:
        there is no fallback to clear text, and no recipient is quietly left
        out. The bot's own key and repeated keys count once. With no
        recipients, only the bot can open the result.

        Sequoia seals the message. It writes SEIPDv2 only when every key it
        seals to announces SEIPDv2, the bot's own key included. A key from
        `generate` never announces it, so its messages are always SEIPDv1.
        rPGP then reads what came out: a message in SEIPDv2, which phones
        on PGPainless 1.7 can't open, is refused. Sealed to the phones' keys
        by a key from `generate`, the result is SEIPDv1 with AES-256 and a
        SHA-512 signature, not compressed.

        Args:
            data: What to seal: ``str`` (sealed as UTF-8) or ``bytes`` (sealed
                exactly as they are).
            recipients: The keys to seal to.
            sign: Sign with the bot's key. ``False`` leaves the signature out.
            armor: ``True`` returns ASCII armor as ``str``; ``False`` returns
                the binary message as ``bytes``.

        Returns:
            The sealed message, ``str`` when ``armor`` is ``True``, ``bytes``
            otherwise.

        Raises:
            EncryptionError: A recipient is revoked, expired or has no live
                encryption subkey, Sequoia could not seal the message, or
                the result came out as SEIPDv2.
            TypeError: ``data`` is not ``str`` or ``bytes``, or a recipient
                is not a `PublicKey`.

        Note:
            The key version is not checked here. Phones on PGPainless 1.7
            can't use v6 keys, so `Bot` never seals to one: check
            `PublicKey.version` when you choose recipients by hand.
        """
        payload = _as_bytes(data) if isinstance(data, str) else data
        if not isinstance(payload, bytes):
            raise TypeError(f"expected str or bytes, got {type(data).__name__}")
        seen = {self.fingerprint}
        certs = [self.public_key._cert]
        for key in recipients:
            if not isinstance(key, PublicKey):
                raise TypeError(f"recipients must be PublicKey objects, got {type(key).__name__}")
            if key.fingerprint in seen:
                continue
            if not key.usable:
                # Sequoia would take a revoked certificate, or fall back to an expired subkey
                raise EncryptionError(
                    f"the key {format_fingerprint(key.fingerprint)} can't be sealed to: it is revoked, "
                    "expired, or has no live encryption subkey"
                )
            seen.add(key.fingerprint)
            certs.append(key._cert)
        out = _lib.encrypt(payload, certs, self._signer if sign else None, armor=armor)
        if _lib.encrypted_version(out) == "seipd-v2":
            raise EncryptionError(
                "the message came out as SEIPDv2, which phones on PGPainless 1.7 can't open: not sent. "
                "The bot's key announces SEIPDv2; make one with BotKey.generate() and rotate to it"
            )
        return out.decode("ascii") if armor else out

    def decrypt(
        self,
        data: str | bytes,
        *,
        verify_with: Iterable[PublicKey] = (),
        max_size: int | None = None,
        allow_compression: bool = True,
    ) -> Decrypted:
        """Opens a message sealed to this key, and checks who signed it.

        Sequoia opens the message. When ``verify_with`` holds keys, rPGP
        opens it a second time and checks the signature; the two engines
        must agree on the content. ``verification.status`` of the result is
        one of these `SignatureStatus` values:

        - `~SignatureStatus.VALID`: signed by one of the ``verify_with``
          keys, and the signature is correct. ``signer_fingerprint`` names
          that key.
        - `~SignatureStatus.UNKNOWN_KEY`: signed, but by none of the
          ``verify_with`` keys.
        - `~SignatureStatus.INVALID`: signed by one of them, but the
          signature does not check out; or the two engines disagree on the
          content.
        - `~SignatureStatus.UNSIGNED`: no signature at all.
        - `~SignatureStatus.NOT_CHECKED`: ``verify_with`` is empty, or rPGP
          can't read the bot's key, the sender's key, the message or that
          kind of signature. Except for an empty ``verify_with``,
          ``detail`` says why.

        A bad or missing signature does not raise: the data comes back
        anyway. Look at the status before you trust it, or let `Bot` drop
        such messages with ``verify_signatures="require"``.

        Args:
            data: The message: armored text, or ``bytes``, armored or binary.
            verify_with: The keys the sender may have signed with, usually
                the one the server lists for them. Empty (the default): the
                signature is not checked.
            max_size: Refuse a message that opens to more bytes than this.
                ``None`` (the default) sets no limit. `Bot` uses 1 MiB for
                messages, and the download limit for attachments.
            allow_compression: ``False`` refuses a compressed message before
                anything is inflated. Phones never compress attachments, and
                a compressed one could open to many gigabytes, so `Bot`
                passes ``False`` for them.

        Returns:
            The decrypted bytes and the result of the signature check, as a
            `Decrypted`.

        Raises:
            NotARecipientError: The message was not sealed to this key.
            DecryptionError: The message is malformed or was changed on the
                way, opens to more than ``max_size`` bytes, or is compressed
                while ``allow_compression`` is ``False``.
            TypeError: ``data`` is not ``str`` or ``bytes``.

        Example:
            A message from Anna, checked against her key::

                result = key.decrypt(armored, verify_with=[anna])
                if result.verification.valid:
                    print(result.text)

        Note:
            Neither engine can stop half way through a compressed message,
            so ``max_size`` limits what comes back, not the memory used on
            the way. Sequoia opens one message at a time in the whole
            process, so a crafted message costs that memory once, not once
            per thread. The compression check needs rPGP; when rPGP can't
            read the bot's key or the message, it is skipped and only
            ``max_size`` applies. With a protected key, the signature check
            and the compression check each unlock the key again in rPGP,
            which is slow by design.

        Warning:
            `~SignatureStatus.VALID` shows that the message was made with one
            of the ``verify_with`` keys and that nobody changed it. It does not
            show that the key belongs to that person (the server hands out the
            keys), that the message was meant for this chat, or that it is
            new.
        """
        payload = data if isinstance(data, bytes) else _as_bytes(data)
        if not allow_compression and self._rp is not None:
            secret = self._passphrase.reveal() if self._passphrase else None
            if _lib.compressed(payload, self._rp, secret):
                raise DecryptionError("a compressed attachment: refused, phones never compress attachments")
        plain = _lib.decrypt(payload, self._decryptor, max_size=max_size)
        candidates = list(verify_with)
        if not candidates:
            return Decrypted(plain, Verification(SignatureStatus.NOT_CHECKED))
        return Decrypted(plain, self._verify(payload, plain, candidates))

    def _verify(self, payload: bytes, plain: bytes, candidates: list[PublicKey]) -> Verification:
        if self._rp is None:
            return Verification(SignatureStatus.NOT_CHECKED, detail="rPGP cannot read the bot's key")
        composed = _lib.rp()
        rp_error = _lib.rp_error()
        try:
            if payload.lstrip().startswith(b"-----"):
                message, _ = composed.Message.from_armor(payload.decode("ascii", "replace"))
            else:
                message = composed.Message.from_bytes(payload)
            opened = message.decrypt(self._passphrase.reveal() if self._passphrase else None, self._rp)
        except (rp_error, ValueError) as exc:
            return Verification(SignatureStatus.NOT_CHECKED, detail=f"rPGP could not open it: {exc}")
        # two independent parsers must agree on what the message says, or neither is trusted
        try:
            same = opened.as_data_vec() == plain
        except (rp_error, ValueError):
            same = False
        if not same:
            return Verification(SignatureStatus.INVALID, detail="the two OpenPGP engines disagree on the content")
        # not is_signed: on a compressed message (how phones write) that only
        # describes the outer layer, while the count looks inside it
        if opened.signature_count() == 0:
            return Verification(SignatureStatus.UNSIGNED)

        mismatch = False
        readable = False
        unsupported: str | None = None
        for key in candidates:
            rp_key = key._rpgp()
            if rp_key is None:
                continue
            readable = True
            for index in range(opened.signature_count()):
                try:
                    opened.verify(rp_key, index)
                except rp_error as exc:
                    code = getattr(exc, "code", "")
                    if code in ("UNIMPLEMENTED", "UNSUPPORTED"):
                        # rPGP can't check this kind of signature: no verdict, not a bad one
                        unsupported = str(exc)
                    elif code != "BINDING":
                        # BINDING: the signature names a key that is not this one
                        mismatch = True
                    continue
                except ValueError:
                    mismatch = True
                    continue
                return Verification(SignatureStatus.VALID, signer_fingerprint=key.fingerprint)
        if mismatch:
            return Verification(
                SignatureStatus.INVALID, detail="signed by the sender's key, but the signature does not check out"
            )
        if unsupported is not None:
            return Verification(SignatureStatus.NOT_CHECKED, detail=f"rPGP can't check this signature: {unsupported}")
        if not readable:
            return Verification(SignatureStatus.NOT_CHECKED, detail="rPGP can't read the sender's key")
        return Verification(SignatureStatus.UNKNOWN_KEY, detail="signed by a key that is not the sender's")

    def __repr__(self) -> str:
        return f"<BotKey {format_fingerprint(self.fingerprint)}{' protected' if self.protected else ''}>"

    def __reduce__(self) -> Any:
        raise TypeError("a BotKey cannot be pickled: it would write the private key wherever the pickle goes")


def _armor_secret(tsk: Any) -> str:
    try:
        return str(tsk)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"cannot armor the private key: {exc}") from None


def _warn_if_open(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning("%s can be read by other users (mode %o): chmod 600 it", path, mode & 0o777)


def looks_like_fingerprint(value: str) -> bool:
    return bool(FINGERPRINT_RE.fullmatch(value.replace(" ", "")))

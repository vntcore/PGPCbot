"""The calls into the two OpenPGP engines, with their errors turned into pgpcbot's own.

Internal: nothing here is public API, and any of it can change in any version.

- Sequoia PGP (``pysequoia``) reads keys, unlocks them, seals and opens
  messages, and knows which subkey of a certificate may encrypt. Its
  ``encrypt`` does not refuse a revoked certificate, and falls back to an
  expired encryption subkey when there is no live one: ``PublicKey.usable``
  checks both before anything is sealed.
- rPGP (``rpgp-py``) does two jobs pysequoia 0.1.35 can't: it makes the bot's
  key with exactly the features and preferences asked for (SEIPDv1 only, so
  phones on PGPainless 1.7 can always read what the bot seals, and a
  passphrase if wanted), and it checks signatures inside compressed
  messages, which is how the phones write text messages. It also tells
  whether a certificate has a live encryption subkey
  (``has_live_encryption_subkey``), whether a message is compressed before
  anything is inflated, whether a sealed message is SEIPDv1 or SEIPDv2, and
  whether a key announces SEIPDv2.

``keys.py`` also works with the engines' objects directly: it reads a
certificate's fingerprint, user IDs, expiry and revocation from Sequoia, and
builds keys and checks signatures with rPGP. Everything else goes through the
functions here.

pysequoia reports its failures as ``RuntimeError`` with a text message. The
functions here read that text and raise ``InvalidKeyError``,
``PassphraseError``, ``EncryptionError``, ``DecryptionError`` or
``NotARecipientError`` instead. Where rPGP can't read something, the helpers
return ``None`` or ``False`` instead of raising, and the caller decides.

Neither engine can stop half way through a compressed message, so
``decrypt`` and ``compressed`` hold one lock for the whole process: one such
message in memory at a time, not one per worker thread.
``docs/RESEARCH.md`` explains the choice of the two engines.
"""

from __future__ import annotations

import threading
import warnings
from typing import TYPE_CHECKING, Any

from ..errors import (
    ConfigurationError,
    DecryptionError,
    EncryptionError,
    InvalidKeyError,
    NotARecipientError,
    PassphraseError,
)

if TYPE_CHECKING:
    import pysequoia
    from openpgp import composed as rpgp_composed

_INSTALL_HINT = "reinstall it with: pip install --force-reinstall PGPCbot"

# Neither engine can stop half way through a compressed message, so a few
# hundred KiB of zlib can come out as hundreds of MiB. One decryption at a time
# keeps that to one such message in memory, not one per worker thread.
_DECRYPT_LOCK = threading.Lock()


def sq() -> Any:
    try:
        import pysequoia
    except ImportError as exc:  # pragma: no cover - only on a broken install
        raise ConfigurationError(f"pysequoia, the OpenPGP engine, cannot be imported ({exc}); {_INSTALL_HINT}") from exc
    return pysequoia


def rp() -> Any:
    try:
        from openpgp import composed
    except ImportError as exc:  # pragma: no cover
        raise ConfigurationError(f"rpgp-py cannot be imported ({exc}); {_INSTALL_HINT}") from exc
    return composed


def rp_error() -> type[Exception]:
    from openpgp.errors import Error

    return Error  # type: ignore[no-any-return]


# --- loading ---


def load_cert(data: bytes) -> Any:
    with warnings.catch_warnings():
        # pysequoia nags about secret parts on a Cert (its "use Tsk" deprecation); we
        # strip them ourselves right after, so the warning has nothing to tell us
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            cert = sq().Cert.from_bytes(data)
        except RuntimeError as exc:
            raise InvalidKeyError(f"not a readable OpenPGP public key: {exc}") from None
        if cert.has_secret_keys:
            # a public key is all we ever keep for other people: drop the rest
            cert = sq().Cert.from_bytes(str(cert).encode())
    return cert


def load_tsk(data: bytes) -> Any:
    try:
        return sq().Tsk.from_bytes(data)
    except RuntimeError as exc:
        raise InvalidKeyError(f"not a readable OpenPGP private key: {exc}") from None


def secret_fingerprint(data: bytes) -> str:
    """The fingerprint of a private key block, read without unlocking anything."""
    tsk = load_tsk(data)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return str(tsk.extract_certificate().fingerprint).upper()


def rp_public(armored: str) -> rpgp_composed.SignedPublicKey | None:
    """The same certificate for rPGP, or None if rPGP can't read it (verification is then skipped)."""
    try:
        key, _ = rp().SignedPublicKey.from_armor(armored)
    except (rp_error(), ValueError):
        return None
    return key


def rp_secret(armored: str) -> rpgp_composed.SignedSecretKey | None:
    try:
        key, _ = rp().SignedSecretKey.from_armor(armored)
    except (rp_error(), ValueError):
        return None
    return key


# --- unlocking ---


def _needs_passphrase(get: Any) -> bool:
    try:
        get()
    except RuntimeError as exc:
        return "secret key material is encrypted" in str(exc)
    return False


def is_locked(tsk: pysequoia.Tsk) -> bool:
    """Whether any of the secret parts is protected by a passphrase."""
    # both parts: a key can protect its encryption subkey and not its primary, or the other way round
    return _needs_passphrase(tsk.signer) or _needs_passphrase(tsk.decryptor)


def _unlock_part(get: Any, passphrase: str | None) -> Any:
    # Sequoia refuses a passphrase for a part that has none, so give it only where it is needed
    try:
        locked = _needs_passphrase(get)
        if not locked:
            return get()
        if passphrase is None:
            raise PassphraseError("the private key is protected by a passphrase and none was given")
        return get(passphrase)
    except RuntimeError as exc:
        text = str(exc)
        if "unexpected EOF" in text or "aead" in text.lower():
            # Sequoia reports a wrong passphrase as the key material failing to parse
            raise PassphraseError("wrong passphrase for the private key") from None
        if "No suitable" in text:
            raise InvalidKeyError(
                f"the key cannot be used by the bot: {text}. It needs a signing key and an encryption subkey "
                "that are not expired or revoked"
            ) from None
        raise InvalidKeyError(f"the private key cannot be used: {text}") from None


def unlock(tsk: pysequoia.Tsk, passphrase: str | None) -> tuple[Any, Any]:
    """The signer and the decryptor of a key, each opened with ``passphrase`` when it is locked."""
    return _unlock_part(tsk.signer, passphrase), _unlock_part(tsk.decryptor, passphrase)


# --- sealing ---


def encrypt(data: bytes, certs: list[Any], signer: Any | None, *, armor: bool) -> bytes:
    if not certs:
        raise EncryptionError("nobody to seal the message to")
    try:
        return sq().encrypt(bytes=data, recipients=certs, signer=signer, armor=armor)  # type: ignore[no-any-return]
    except RuntimeError as exc:
        text = str(exc)
        if "No suitable encryption subkey" in text:
            raise EncryptionError(
                f"a recipient's key has no usable encryption subkey (expired, revoked or sign-only): {text}"
            ) from None
        raise EncryptionError(f"sealing failed: {text}") from None


def encrypt_with_password(data: bytes, password: str) -> bytes:
    try:
        return sq().encrypt(bytes=data, passwords=[password], armor=True)  # type: ignore[no-any-return]
    except RuntimeError as exc:
        raise EncryptionError(f"could not seal the key with the passphrase: {exc}") from None


def can_encrypt_to(cert: Any) -> bool:
    """Whether Sequoia finds a key it would encrypt to. Costs a fraction of a millisecond.

    Not the whole answer: when a certificate has no live encryption key,
    pysequoia falls back to an expired one. See has_live_encryption_subkey.
    """
    try:
        sq().encrypt(bytes=b"", recipients=[cert], armor=False)
    except RuntimeError:
        return False
    return True


def has_live_encryption_subkey(key: Any, now: float) -> bool | None:
    """Whether an rPGP certificate has an encryption subkey that is neither revoked nor expired.

    None when it can't be told; the newest binding signature of a subkey is the one that counts.
    """
    try:
        for sub in key.public_subkeys:
            bindings = []
            revoked = False
            for signature in sub.signatures:
                kind = str(signature.typ())
                if "revocation" in kind:
                    revoked = True
                elif kind == "subkey-binding":
                    bindings.append(signature)
            if revoked or not bindings:
                continue
            binding = max(bindings, key=lambda s: s.created() or 0)
            flags = binding.key_flags()
            if not (flags.encrypt_communications or flags.encrypt_storage):
                continue
            lifetime = binding.key_expiration_time()
            if lifetime and sub.key.created_at + lifetime <= now:
                continue
            return True
    except Exception:  # noqa: BLE001 - an odd certificate is one we can't judge, not a crash
        return None
    return False


# --- opening ---


def decrypt(data: bytes, decryptor: Any, *, max_size: int | None = None) -> bytes:
    try:
        with _DECRYPT_LOCK:
            result = sq().decrypt(bytes=data, decryptor=decryptor)
    except RuntimeError as exc:
        text = str(exc)
        if "No key to decrypt message" in text:
            raise NotARecipientError("the message was not sealed to this key") from None
        if "manipulated" in text:
            raise DecryptionError("the message was altered on the way: integrity check failed") from None
        if any(s in text for s in ("unexpected EOF", "Malformed", "Armor", "Missing session key")):
            raise DecryptionError(f"not a well formed OpenPGP message ({text})") from None
        raise DecryptionError(f"cannot decrypt: {text}") from None
    if result.bytes is None:  # pragma: no cover - only the file variant leaves it empty
        raise DecryptionError("the message decrypted to nothing")
    plain: bytes = result.bytes
    if max_size is not None and len(plain) > max_size:
        raise DecryptionError(f"the message opens to {len(plain)} bytes, more than the {max_size} allowed: refused")
    return plain


def compressed(data: bytes, key: Any, passphrase: str | None) -> bool | None:
    """Whether the first layer inside an encrypted message is compressed.

    rPGP tells, before anything is inflated. None when rPGP can't open it.
    """
    try:
        composed = rp()
        if data.lstrip().startswith(b"-----"):
            message, _ = composed.Message.from_armor(data.decode("ascii", "replace"))
        else:
            message = composed.Message.from_bytes(data)
        with _DECRYPT_LOCK:
            opened = message.decrypt(passphrase, key)
        return bool(opened.is_compressed)
    except Exception:  # noqa: BLE001 - not knowing is an answer the caller handles
        return None


def decrypt_with_password(data: bytes, password: str) -> bytes:
    try:
        result = sq().decrypt(bytes=data, passwords=[password])
    except RuntimeError as exc:
        if "No key to decrypt message" in str(exc):
            raise PassphraseError("wrong passphrase for the sealed key file") from None
        raise DecryptionError(f"cannot open the sealed key file: {exc}") from None
    return result.bytes  # type: ignore[no-any-return]


def encrypted_version(data: bytes) -> str | None:
    """``"seipd-v1"`` or ``"seipd-v2"`` for an encrypted message, ``None`` if it can't be told."""
    try:
        composed = rp()
        if data.lstrip().startswith(b"-----"):
            message, _ = composed.Message.from_armor(data.decode("ascii", "replace"))
        else:
            message = composed.Message.from_bytes(data)
        return message.encrypted_data_packet().kind  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001 - a guess, never a reason to fail a send
        return None


def advertises_seipd_v2(key: Any) -> bool:
    """Whether any self-signature of an rPGP key announces SEIPDv2 support."""
    try:
        details = key.details
        signatures = [s for user in details.users for s in user.signatures] + list(details.direct_signatures)
    except Exception:  # noqa: BLE001
        return False
    for signature in signatures:
        try:
            features = signature.features()
        except Exception:  # noqa: BLE001 - an odd signature says nothing either way
            features = None
        if features is not None and features.seipd_v2:
            return True
    return False

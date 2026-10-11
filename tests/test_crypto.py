from __future__ import annotations

import copy
import logging
import os
import pickle
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pysequoia
import pytest
from openpgp.composed import EncryptionCaps, KeyType, Message, SecretKeyParamsBuilder, SubkeyParamsBuilder
from openpgp.errors import Error as RpgpError
from openpgp.types import S2kParams, StringToKey
from pysequoia.packet import PacketPile, Tag

from pgpcbot.crypto import BotKey, PublicKey, _lib
from pgpcbot.crypto.keys import Decrypted, format_fingerprint, looks_like_fingerprint
from pgpcbot.errors import (
    ConfigurationError,
    DecryptionError,
    EncryptionError,
    InvalidKeyError,
    NotARecipientError,
    PassphraseError,
)
from pgpcbot.models import SignatureStatus, UserKey, Verification

from .conftest import FIXTURES, fixture_text
from .fakeapi import MEDIA_ID

PHONE_TEXT = "Ciao dal telefono: è già sera 🌙\nseconda riga"
ATTACHMENT_PLAIN = b"\xff\xd8\xffJPEGDATA"
# both checked against PGPainless' own "fpr" output
BOT_FPR = "7210EBB59E6ED707CE006D9FE1944D33D7712998"
ANNA_FPR = "51E62ED3FED07AC72CD67DD5C51D0BFA13ABABE5"
LOGGER = "pgpcbot.crypto"


@pytest.fixture(scope="module")
def anna() -> BotKey:
    """Anna's phone with its private half, to read what the bot seals to her."""
    return BotKey.from_file(FIXTURES / "phone_ed.sec.asc")


@pytest.fixture(scope="module")
def bruno() -> BotKey:
    return BotKey.from_file(FIXTURES / "phone_rsa.sec.asc")


@pytest.fixture(scope="module")
def locked_key() -> BotKey:
    return BotKey.from_file(FIXTURES / "bot_locked.sec.asc", passphrase="correct horse")


@pytest.fixture(scope="module")
def sign_only_key() -> Any:
    """An rPGP secret key with no encryption subkey: it signs, nothing can be sealed to it."""
    return (
        SecretKeyParamsBuilder()
        .version(4)
        .key_type(KeyType.ed25519_legacy())
        .can_certify(True)
        .can_sign(True)
        .primary_user_id("Signer only")
        .build()
        .generate()
    )


@pytest.fixture(scope="module")
def revoked_key() -> PublicKey:
    tsk = pysequoia.Tsk.generate(user_id="Gone <gone@example.org>")
    cert = tsk.extract_certificate()
    return PublicKey.from_armored(bytes(cert) + bytes(cert.revoke(tsk.certifier())))


@pytest.fixture(scope="module")
def expired_key() -> PublicKey:
    """A phone key made on 2020-01-01 that expired the day after."""
    born = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())
    subkey = (
        SubkeyParamsBuilder()
        .version(4)
        .key_type(KeyType.ecdh("curve25519"))
        .can_encrypt(EncryptionCaps.all())
        .created_at(born)
        .build()
    )
    secret = (
        SecretKeyParamsBuilder()
        .version(4)
        .key_type(KeyType.ed25519_legacy())
        .can_certify(True)
        .can_sign(True)
        .created_at(born)
        .primary_user_id("Old phone <old@phone.test>")
        .subkey(subkey)
        .generate()
    )
    tsk = pysequoia.Tsk.from_bytes(secret.to_armored().encode())
    cert = tsk.extract_certificate().set_expiration(datetime(2020, 1, 2, tzinfo=UTC), tsk.certifier())
    return PublicKey.from_armored(str(cert))


@pytest.fixture(scope="module")
def v6_tsk() -> Any:
    return pysequoia.Tsk.generate(user_id="Sei <sei@example.org>", profile=pysequoia.Profile.RFC9580)


@pytest.fixture(scope="module")
def seipd_v2_secret() -> str:
    """Sequoia's default key: it advertises SEIPDv2, which PGPainless 1.7 cannot read."""
    return str(pysequoia.Tsk.generate(user_id="Seq <seq@example.org>"))


def public_half(tsk: Any) -> str:
    return str(tsk.extract_certificate())


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def assert_public_packets_only(armored: str) -> None:
    tags = [packet.tag for packet in PacketPile.from_bytes(armored.encode())]
    assert Tag.PublicKey in tags
    assert Tag.PublicSubkey in tags
    assert Tag.SecretKey not in tags
    assert Tag.SecretSubkey not in tags


def pkesk_key_ids(armored: str) -> list[str]:
    """One entry per encryption subkey the message was sealed to."""
    message, _ = Message.from_armor(armored)
    return [packet.recipient_key_id for packet in message.public_key_encrypted_session_key_packets()]


def flip(data: bytes, index: int) -> bytes:
    damaged = bytearray(data)
    damaged[index] ^= 0x01
    return bytes(damaged)


def crypto_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def server_key(public_key: str, fingerprint: str | None) -> UserKey:
    return UserKey.from_json({"userId": 55, "fingerprint": fingerprint, "publicKey": public_key})


def partially_protected(*, primary: str | None, subkey: str | None) -> str:
    """A key whose signing primary and encryption subkey are not protected alike."""

    def s2k() -> Any:
        return S2kParams.cfb("aes256", StringToKey.iterated("sha256", 96))

    builder = (
        SecretKeyParamsBuilder()
        .version(4)
        .key_type(KeyType.ed25519_legacy())
        .can_certify(True)
        .can_sign(True)
        .primary_user_id("Mixed")
    )
    sub = SubkeyParamsBuilder().version(4).key_type(KeyType.ecdh("curve25519")).can_encrypt(EncryptionCaps.all())
    if primary:
        builder = builder.passphrase(primary).s2k(s2k())
    if subkey:
        sub = sub.passphrase(subkey).s2k(s2k())
    armored: str = builder.subkey(sub.build()).generate().to_armored()
    return armored


def test_generate_makes_a_v4_key_named_after_the_bot() -> None:
    key = BotKey.generate("Echo", username="echobot")

    assert key.public_key.version == 4
    assert re.fullmatch(r"[0-9A-F]{40}", key.fingerprint)
    assert key.fingerprint == key.public_key.fingerprint
    assert PublicKey.from_armored(key.export_public()).fingerprint == key.fingerprint
    assert key.public_key.user_ids == ["Echo (@echobot)"]
    assert key.public_key.usable
    # an expired key could not open old messages any more
    assert key.public_key.expires_at is None
    assert not key.protected


def test_generated_key_keeps_every_message_on_seipd_v1() -> None:
    key = BotKey.generate("Echo")
    published = _lib.rp_public(key.export_public())

    assert published is not None
    assert not _lib.advertises_seipd_v2(published)
    assert _lib.encrypted_version(key.seal("note to self", []).encode()) == "seipd-v1"


def test_seipd_v2_check_notices_a_key_that_advertises_it(seipd_v2_secret: str) -> None:
    # the control for the test above: the check is not simply always false
    public = public_half(pysequoia.Tsk.from_bytes(seipd_v2_secret.encode()))
    assert _lib.advertises_seipd_v2(_lib.rp_public(public))


@pytest.mark.parametrize(
    ("name", "username", "user_id"),
    [
        ("Echo", None, "Echo"),
        ("  Echo bot  ", "echobot", "Echo bot (@echobot)"),
        ("Echo", "@echobot", "Echo (@echobot)"),
    ],
    ids=["name-only", "name-trimmed", "username-with-at"],
)
def test_generate_user_id(name: str, username: str | None, user_id: str) -> None:
    assert BotKey.generate(name, username=username).public_key.user_ids == [user_id]


def test_generate_refuses_a_blank_name() -> None:
    with pytest.raises(ConfigurationError):
        BotKey.generate("   ")


def test_generate_with_a_passphrase_protects_the_stored_key() -> None:
    key = BotKey.generate("Echo", passphrase="s3cret pass")
    stored = key.export_secret()

    assert key.protected
    assert stored.startswith("-----BEGIN PGP PRIVATE KEY BLOCK-----")
    with pytest.raises(PassphraseError):
        BotKey.from_armored(stored)
    with pytest.raises(PassphraseError):
        BotKey.from_armored(stored, "wrong pass")

    reloaded = BotKey.from_armored(stored, "s3cret pass")
    assert reloaded.fingerprint == key.fingerprint
    assert reloaded.protected
    # both halves were unlocked at load: the primary signs, the subkey decrypts
    opened = reloaded.decrypt(reloaded.seal("still mine", []), verify_with=[key.public_key])
    assert opened.text == "still mine"
    assert opened.verification.valid


@pytest.mark.parametrize("passphrase", [None, "", "wrong horse"], ids=["none", "empty", "wrong"])
def test_locked_key_does_not_load_without_its_passphrase(passphrase: str | None) -> None:
    with pytest.raises(PassphraseError):
        BotKey.from_file(FIXTURES / "bot_locked.sec.asc", passphrase=passphrase)


@pytest.mark.parametrize(
    ("primary", "subkey"),
    [(None, "pw"), ("pw", None)],
    ids=["only-the-subkey-protected", "only-the-primary-protected"],
)
def test_partially_protected_key_loads_with_its_passphrase(primary: str | None, subkey: str | None) -> None:
    key = BotKey.from_armored(partially_protected(primary=primary, subkey=subkey), passphrase="pw")

    opened = key.decrypt(key.seal("ciao", []), verify_with=[key.public_key])
    assert opened.text == "ciao"
    assert opened.verification.valid


def test_locked_key_takes_its_passphrase_from_a_callable(locked_key: BotKey) -> None:
    key = BotKey.from_file(FIXTURES / "bot_locked.sec.asc", passphrase=lambda: "correct horse")

    assert key.fingerprint == locked_key.fingerprint
    assert key.protected


def test_locked_key_opens_and_verifies_a_phone_message(locked_key: BotKey, anna: BotKey, phone_key: PublicKey) -> None:
    sealed = anna.seal(PHONE_TEXT, [locked_key.public_key])

    opened = locked_key.decrypt(sealed, verify_with=[phone_key])

    assert opened.text == PHONE_TEXT
    assert opened.verification == Verification(SignatureStatus.VALID, signer_fingerprint=phone_key.fingerprint)


def test_locked_key_signs_what_it_seals(locked_key: BotKey, anna: BotKey, phone_key: PublicKey) -> None:
    opened = anna.decrypt(locked_key.seal("firmato", [phone_key]), verify_with=[locked_key.public_key])

    assert opened.text == "firmato"
    assert opened.verification == Verification(SignatureStatus.VALID, signer_fingerprint=locked_key.fingerprint)


def test_export_public_has_no_private_material(bot_key: BotKey) -> None:
    exported = bot_key.export_public()

    assert exported.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert "PRIVATE" not in exported
    assert_public_packets_only(exported)
    assert PublicKey.from_armored(exported) == PublicKey.from_file(FIXTURES / "bot.pub.asc")


def test_export_secret_gives_the_key_back_as_stored(bot_key: BotKey, locked_key: BotKey) -> None:
    assert bot_key.export_secret() == fixture_bytes("bot.sec.asc").decode()
    assert locked_key.export_secret() == fixture_bytes("bot_locked.sec.asc").decode()


def test_export_secret_with_a_passphrase_seals_the_whole_key() -> None:
    key = BotKey.generate("Echo")
    sealed = key.export_secret(passphrase="file pass")

    assert sealed.startswith("-----BEGIN PGP MESSAGE-----")
    assert "PRIVATE KEY BLOCK" not in sealed
    with pytest.raises(PassphraseError):
        BotKey.from_armored(sealed)
    with pytest.raises(PassphraseError):
        BotKey.from_armored(sealed, "not the file pass")

    reopened = BotKey.from_armored(sealed, "file pass")
    assert reopened.fingerprint == key.fingerprint
    assert reopened.export_secret() == key.export_secret()
    # the passphrase sealed the file: the key inside never had one
    assert not reopened.protected


def test_protected_key_sealed_with_its_own_passphrase_reopens() -> None:
    key = BotKey.generate("Echo", passphrase="one pass")

    reopened = BotKey.from_armored(key.export_secret(passphrase="one pass"), "one pass")

    assert reopened.fingerprint == key.fingerprint
    assert reopened.protected


def test_protected_key_is_not_sealed_under_a_second_passphrase(tmp_path: Path) -> None:
    # the file would open with one passphrase and the key inside with another: nobody could load it
    key = BotKey.generate("Echo", passphrase="key pass")
    with pytest.raises(ConfigurationError, match="already protected"):
        key.save(tmp_path / "backup.asc", passphrase="file pass")
    assert not (tmp_path / "backup.asc").exists()

    target = key.save(tmp_path / "backup.asc", passphrase="key pass")
    assert BotKey.from_file(target, "key pass").fingerprint == key.fingerprint


def test_export_secret_refuses_an_empty_passphrase(bot_key: BotKey) -> None:
    with pytest.raises(ConfigurationError, match="empty passphrase"):
        bot_key.export_secret(passphrase="")


def test_damaged_sealed_key_file_is_not_blamed_on_the_passphrase() -> None:
    sealed = BotKey.generate("Echo").export_secret(passphrase="file pass")
    damaged = sealed[: len(sealed) // 2] + "\n-----END PGP MESSAGE-----\n"

    with pytest.raises(DecryptionError):
        BotKey.from_armored(damaged, "file pass")


def test_save_insists_on_an_explicit_passphrase_argument(tmp_path: Path, bot_key: BotKey) -> None:
    target = tmp_path / "bot.asc"

    with pytest.raises(TypeError):
        bot_key.save(target)  # type: ignore[call-arg]
    assert not target.exists()


def test_save_in_the_clear_warns_and_writes_the_key_as_stored(
    tmp_path: Path, bot_key: BotKey, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "keys" / "bot.asc"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        written = bot_key.save(target, passphrase=None)

    assert written == target
    assert target.read_bytes() == bot_key.export_secret().encode()
    assert BotKey.from_file(target).fingerprint == bot_key.fingerprint
    messages = crypto_warnings(caplog)
    assert len(messages) == 1
    assert "without a passphrase" in messages[0]
    assert str(target) in messages[0]


def test_save_with_a_passphrase_writes_a_sealed_file(
    tmp_path: Path, bot_key: BotKey, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "bot.asc"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        bot_key.save(target, passphrase="file pass")

    assert not crypto_warnings(caplog)
    assert target.read_text(encoding="ascii").startswith("-----BEGIN PGP MESSAGE-----")
    with pytest.raises(PassphraseError):
        BotKey.from_file(target)
    assert BotKey.from_file(target, passphrase="file pass").fingerprint == bot_key.fingerprint


def test_save_of_a_protected_key_needs_no_second_passphrase(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    key = BotKey.generate("Echo", passphrase="key pass")
    target = tmp_path / "bot.asc"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        key.save(target, passphrase=None)

    assert not crypto_warnings(caplog)
    assert target.read_bytes() == key.export_secret().encode()
    assert BotKey.from_file(target, "key pass").protected


def test_save_does_not_replace_a_file_unless_told_to(tmp_path: Path, bot_key: BotKey) -> None:
    target = tmp_path / "bot.asc"
    target.write_text("x" * 20_000)

    with pytest.raises(ConfigurationError, match="overwrite=True"):
        bot_key.save(target, passphrase="file pass")
    assert target.read_text() == "x" * 20_000

    bot_key.save(target, passphrase="file pass", overwrite=True)
    # truncated, not written over the start of the old content
    assert len(target.read_bytes()) < 20_000
    assert BotKey.from_file(target, "file pass").fingerprint == bot_key.fingerprint


def test_save_refuses_an_empty_passphrase_before_touching_the_disk(tmp_path: Path, bot_key: BotKey) -> None:
    target = tmp_path / "bot.asc"

    with pytest.raises(ConfigurationError):
        bot_key.save(target, passphrase="")
    assert not target.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_saved_key_is_readable_by_its_owner_only(tmp_path: Path, bot_key: BotKey) -> None:
    fresh = bot_key.save(tmp_path / "fresh.asc", passphrase="file pass")
    replaced = tmp_path / "replaced.asc"
    replaced.write_text("old")
    replaced.chmod(0o644)
    bot_key.save(replaced, passphrase="file pass", overwrite=True)

    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    assert stat.S_IMODE(replaced.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_from_file_warns_about_a_key_other_users_can_read(
    tmp_path: Path, bot_key: BotKey, caplog: pytest.LogCaptureFixture
) -> None:
    target = bot_key.save(tmp_path / "bot.asc", passphrase="file pass")
    target.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        BotKey.from_file(target, "file pass")

    assert any("chmod 600" in message for message in crypto_warnings(caplog))


@pytest.mark.parametrize(
    "where", [lambda tmp: tmp / "missing.asc", lambda tmp: tmp], ids=["missing-file", "a-directory"]
)
def test_from_file_of_an_unreadable_path_is_a_configuration_error(
    tmp_path: Path, where: Callable[[Path], Path]
) -> None:
    with pytest.raises(ConfigurationError, match="cannot read the key file"):
        BotKey.from_file(where(tmp_path))


def test_from_env_with_nothing_set_is_none() -> None:
    assert BotKey.from_env() is None


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PGPCHAT_BOT_KEY", fixture_text("bot.sec.asc")),
        ("PGPCHAT_BOT_KEY", str(FIXTURES / "bot.sec.asc")),
        ("PGPCHAT_BOT_KEY_FILE", str(FIXTURES / "bot.sec.asc")),
    ],
    ids=["armored", "path", "key-file-variable"],
)
def test_from_env_finds_the_key(monkeypatch: pytest.MonkeyPatch, bot_key: BotKey, variable: str, value: str) -> None:
    monkeypatch.setenv(variable, value)

    key = BotKey.from_env()

    assert key is not None
    assert key.fingerprint == bot_key.fingerprint


def test_from_env_unlocks_with_the_passphrase_variable(monkeypatch: pytest.MonkeyPatch, locked_key: BotKey) -> None:
    monkeypatch.setenv("PGPCHAT_BOT_KEY", str(FIXTURES / "bot_locked.sec.asc"))
    monkeypatch.setenv("PGPCHAT_BOT_KEY_PASSPHRASE", "correct horse")

    key = BotKey.from_env()

    assert key is not None
    assert key.fingerprint == locked_key.fingerprint
    assert key.protected


def test_from_env_with_a_locked_key_and_no_passphrase_fails_at_start_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPCHAT_BOT_KEY", fixture_text("bot_locked.sec.asc"))

    with pytest.raises(PassphraseError):
        BotKey.from_env()


def test_from_env_pointing_at_a_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PGPCHAT_BOT_KEY", str(tmp_path / "missing.asc"))

    with pytest.raises(ConfigurationError, match="cannot read the key file"):
        BotKey.from_env()


def test_a_public_key_cannot_be_the_bot_key() -> None:
    with pytest.raises(InvalidKeyError, match="public key"):
        BotKey.from_armored(fixture_text("bot.pub.asc"))


@pytest.mark.parametrize(
    "data",
    [
        "",
        "not a key at all",
        b"\x00\x01\x02\x03",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nAAAA\n-----END PGP PRIVATE KEY BLOCK-----\n",
    ],
    ids=["empty", "text", "bytes", "broken-armor"],
)
def test_garbage_is_not_a_bot_key(data: str | bytes) -> None:
    with pytest.raises(InvalidKeyError):
        BotKey.from_armored(data)


def test_a_sign_only_key_cannot_be_the_bot_key(sign_only_key: Any) -> None:
    with pytest.raises(InvalidKeyError, match="encryption subkey"):
        BotKey.from_armored(sign_only_key.to_armored())


def test_private_key_with_a_latin1_armor_header_loads(bot_key: BotKey) -> None:
    begin, rest = fixture_bytes("bot.sec.asc").split(b"\n", 1)
    armored = begin + b"\nComment: J\xfcrgen's key\n" + rest

    assert BotKey.from_armored(armored).fingerprint == bot_key.fingerprint


@pytest.mark.parametrize("data", ["", "not a key at all", b"\x00\x01\x02\x03"], ids=["empty", "text", "bytes"])
def test_garbage_is_not_a_public_key(data: str | bytes) -> None:
    with pytest.raises(InvalidKeyError):
        PublicKey.from_armored(data)


def test_public_key_refuses_a_private_key_block(bot_key: BotKey) -> None:
    with pytest.raises(InvalidKeyError, match="private key"):
        PublicKey.from_armored(bot_key.export_secret())


def test_public_key_keeps_only_the_public_part_of_a_binary_private_key(bot_key: BotKey) -> None:
    binary_secret = bytes(pysequoia.Tsk.from_bytes(bot_key.export_secret().encode()))

    key = PublicKey.from_armored(binary_secret)

    assert key == bot_key.public_key
    assert "PRIVATE" not in key.armored
    assert_public_packets_only(key.armored)


@pytest.mark.parametrize(
    "claim",
    [lambda fp: fp, lambda fp: format_fingerprint(fp).lower(), lambda fp: None, lambda fp: ""],
    ids=["exact", "grouped-lowercase", "missing", "empty"],
)
def test_from_user_key_accepts_a_matching_or_missing_fingerprint(
    phone_key: PublicKey, claim: Callable[[str], str | None]
) -> None:
    key = PublicKey.from_user_key(server_key(phone_key.armored, claim(phone_key.fingerprint)))

    assert key == phone_key


def test_from_user_key_refuses_a_fingerprint_that_is_not_the_keys(
    phone_key: PublicKey, rsa_phone_key: PublicKey
) -> None:
    with pytest.raises(InvalidKeyError) as caught:
        PublicKey.from_user_key(server_key(phone_key.armored, rsa_phone_key.fingerprint))

    assert rsa_phone_key.fingerprint in str(caught.value)
    assert phone_key.fingerprint in str(caught.value)


def test_v4_public_key(phone_key: PublicKey) -> None:
    assert phone_key.version == 4
    assert phone_key.fingerprint == ANNA_FPR
    # v4: the low 64 bits of the fingerprint
    assert phone_key.key_id == "C51D0BFA13ABABE5"
    assert phone_key.user_ids == ["Anna <anna@phone.test>"]
    assert phone_key.armored.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert phone_key.usable
    assert not phone_key.revoked


def test_v6_public_key(v6_tsk: Any) -> None:
    key = PublicKey.from_armored(public_half(v6_tsk))

    assert key.version == 6
    assert re.fullmatch(r"[0-9A-F]{64}", key.fingerprint)
    # v6: the high 64 bits
    assert key.key_id == key.fingerprint[:16]
    assert key.user_ids == ["Sei <sei@example.org>"]
    assert key.usable


def test_sealing_to_a_v6_recipient_stays_on_seipd_v1(bot_key: BotKey, v6_tsk: Any) -> None:
    six = BotKey.from_armored(str(v6_tsk))

    sealed = bot_key.seal("ciao", [six.public_key])

    assert _lib.encrypted_version(sealed.encode()) == "seipd-v1"
    opened = six.decrypt(sealed, verify_with=[bot_key.public_key])
    assert opened.text == "ciao"
    assert opened.verification.valid


def test_public_keys_compare_by_fingerprint(phone_key: PublicKey, rsa_phone_key: PublicKey) -> None:
    again = PublicKey.from_armored(phone_key.armored)

    assert again == phone_key
    assert again is not phone_key
    assert hash(again) == hash(phone_key)
    assert len({phone_key, again, rsa_phone_key}) == 2
    assert phone_key != rsa_phone_key
    assert phone_key != phone_key.fingerprint


def test_public_key_repr(phone_key: PublicKey) -> None:
    assert (
        repr(phone_key) == "<PublicKey 51E6 2ED3 FED0 7AC7 2CD6 7DD5 C51D 0BFA 13AB ABE5 v4 'Anna <anna@phone.test>'>"
    )


def test_public_key_repr_without_a_user_id() -> None:
    key = PublicKey.from_armored(public_half(pysequoia.Tsk.generate()))

    assert key.user_ids == []
    assert repr(key) == f"<PublicKey {format_fingerprint(key.fingerprint)} v4 'no user id'>"


def test_revoked_certificate_is_not_usable(revoked_key: PublicKey) -> None:
    assert revoked_key.revoked
    assert not revoked_key.usable


def test_sealing_to_a_revoked_certificate_is_refused(bot_key: BotKey, revoked_key: PublicKey) -> None:
    with pytest.raises(EncryptionError):
        bot_key.seal("for a key its owner revoked", [revoked_key])


def test_expired_certificate_says_when_it_expired(expired_key: PublicKey) -> None:
    assert expired_key.expires_at == datetime(2020, 1, 2, tzinfo=UTC)
    assert not expired_key.revoked


def test_expired_certificate_is_not_usable(expired_key: PublicKey) -> None:
    assert not expired_key.usable


def test_sealing_to_an_expired_certificate_is_refused(bot_key: BotKey, expired_key: PublicKey) -> None:
    with pytest.raises(EncryptionError):
        bot_key.seal("are you still there?", [expired_key])


def test_sealing_to_a_key_without_an_encryption_subkey_is_refused(
    bot_key: BotKey, phone_key: PublicKey, sign_only_key: Any
) -> None:
    signer = PublicKey.from_armored(sign_only_key.to_public_key().to_armored())

    assert not signer.usable
    with pytest.raises(EncryptionError, match="no live encryption subkey"):
        bot_key.seal("anyone there?", [phone_key, signer])


def test_seal_reaches_every_recipient_and_the_bot_itself(
    bot_key: BotKey, phone_key: PublicKey, rsa_phone_key: PublicKey, anna: BotKey, bruno: BotKey
) -> None:
    sealed = bot_key.seal(PHONE_TEXT, [phone_key, rsa_phone_key])

    assert sealed.startswith("-----BEGIN PGP MESSAGE-----")
    for reader in (anna, bruno, bot_key):
        opened = reader.decrypt(sealed, verify_with=[bot_key.public_key])
        assert opened.text == PHONE_TEXT
        assert opened.verification == Verification(SignatureStatus.VALID, signer_fingerprint=bot_key.fingerprint)


def test_sealed_messages_are_seipd_v1(bot_key: BotKey, phone_key: PublicKey, rsa_phone_key: PublicKey) -> None:
    sealed = bot_key.seal(PHONE_TEXT, [phone_key, rsa_phone_key])

    assert _lib.encrypted_version(sealed.encode()) == "seipd-v1"


def test_encrypted_version_reads_phone_messages_and_never_raises() -> None:
    # several tests lean on it: a PGPainless message is the known v1 sample
    assert _lib.encrypted_version(fixture_bytes("msg_text_signed.asc")) == "seipd-v1"
    assert _lib.encrypted_version(fixture_bytes("attachment.pgp")) == "seipd-v1"
    assert _lib.encrypted_version(b"not a message") is None


def test_a_key_the_message_was_not_sealed_to_cannot_open_it(
    bot_key: BotKey, phone_key: PublicKey, stranger_key: BotKey
) -> None:
    sealed = bot_key.seal("not for strangers", [phone_key])

    with pytest.raises(NotARecipientError):
        stranger_key.decrypt(sealed)


def test_encrypt_without_signing_is_unsigned(bot_key: BotKey, phone_key: PublicKey, anna: BotKey) -> None:
    sealed = bot_key.encrypt("anonimo", [phone_key], sign=False)

    assert isinstance(sealed, str)
    opened = anna.decrypt(sealed, verify_with=[bot_key.public_key])
    assert opened.text == "anonimo"
    assert opened.verification.status is SignatureStatus.UNSIGNED


def test_encrypt_without_armor_returns_binary(bot_key: BotKey, phone_key: PublicKey, anna: BotKey) -> None:
    sealed = bot_key.encrypt(b"\x00\xffraw", [phone_key], armor=False)

    assert isinstance(sealed, bytes)
    assert not sealed.startswith(b"-----")
    opened = anna.decrypt(sealed, verify_with=[bot_key.public_key])
    assert opened.data == b"\x00\xffraw"
    assert opened.verification.valid


def test_each_recipient_is_sealed_to_once(bot_key: BotKey, phone_key: PublicKey, rsa_phone_key: PublicKey) -> None:
    same_phone_again = PublicKey.from_armored(phone_key.armored)

    sealed = bot_key.seal("x", [phone_key, same_phone_again, bot_key.public_key, phone_key, rsa_phone_key])

    key_ids = pkesk_key_ids(sealed)
    # the bot, Anna and Bruno, one encryption subkey each
    assert len(key_ids) == 3
    assert len(set(key_ids)) == 3


@pytest.mark.parametrize(
    "recipients",
    [
        lambda bot, phone: [phone.armored],
        lambda bot, phone: [phone.fingerprint],
        lambda bot, phone: [bot],
        lambda bot, phone: phone,
    ],
    ids=["armored-string", "fingerprint", "bot-key", "bare-public-key"],
)
def test_recipients_must_be_public_keys(
    bot_key: BotKey, phone_key: PublicKey, recipients: Callable[[BotKey, PublicKey], Any]
) -> None:
    with pytest.raises(TypeError):
        bot_key.seal("x", recipients(bot_key, phone_key))


def test_seal_decrypt_and_load_take_text_or_bytes_only(bot_key: BotKey) -> None:
    with pytest.raises(TypeError):
        bot_key.seal(42, [])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        bot_key.decrypt(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BotKey.from_armored(42)  # type: ignore[arg-type]


def test_loading_a_key_that_advertises_seipd_v2_warns(seipd_v2_secret: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        key = BotKey.from_armored(seipd_v2_secret)

    assert any("advertises SEIPDv2" in m and key.fingerprint in m for m in crypto_warnings(caplog))


def test_seipd_v2_key_seals_v2_only_when_every_recipient_reads_it(
    seipd_v2_secret: str, phone_key: PublicKey, caplog: pytest.LogCaptureFixture
) -> None:
    key = BotKey.from_armored(seipd_v2_secret)
    caplog.clear()

    # phones could not open it: refused rather than sent with a warning
    with pytest.raises(EncryptionError, match="SEIPDv2"):
        key.seal("x", [])

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with_a_phone = key.seal("x", [phone_key])
    # one PGPainless phone among the recipients pulls it back to v1
    assert _lib.encrypted_version(with_a_phone.encode()) == "seipd-v1"
    assert not crypto_warnings(caplog)


@pytest.mark.parametrize(
    ("message", "candidates", "status", "signer"),
    [
        ("msg_text_signed.asc", ["phone_key"], SignatureStatus.VALID, "phone_key"),
        ("msg_rsa_signed.asc", ["rsa_phone_key"], SignatureStatus.VALID, "rsa_phone_key"),
        ("msg_text_signed.asc", ["rsa_phone_key", "phone_key"], SignatureStatus.VALID, "phone_key"),
        ("msg_text_unsigned.asc", ["phone_key"], SignatureStatus.UNSIGNED, None),
        ("msg_text_signed.asc", ["rsa_phone_key"], SignatureStatus.UNKNOWN_KEY, None),
        ("msg_rsa_signed.asc", ["phone_key"], SignatureStatus.UNKNOWN_KEY, None),
        ("msg_text_signed.asc", [], SignatureStatus.NOT_CHECKED, None),
    ],
    ids=[
        "ed25519-signed",
        "rsa-signed",
        "second-candidate-matches",
        "unsigned",
        "signed-by-someone-else",
        "rsa-signed-checked-against-ed25519",
        "no-candidates",
    ],
)
def test_phone_message_decrypts_and_verifies(
    request: pytest.FixtureRequest,
    bot_key: BotKey,
    message: str,
    candidates: list[str],
    status: SignatureStatus,
    signer: str | None,
) -> None:
    keys = [request.getfixturevalue(name) for name in candidates]

    opened = bot_key.decrypt(fixture_text(message), verify_with=keys)

    assert opened.text == PHONE_TEXT
    assert opened.verification.status is status
    expected_signer = request.getfixturevalue(signer).fingerprint if signer else None
    assert opened.verification.signer_fingerprint == expected_signer
    assert opened.verification.valid is (status is SignatureStatus.VALID)


def test_phone_message_given_as_bytes(bot_key: BotKey, phone_key: PublicKey) -> None:
    opened = bot_key.decrypt(fixture_bytes("msg_text_signed.asc"), verify_with=[phone_key])

    assert opened.text == PHONE_TEXT
    assert opened.verification.valid


def test_media_message_carries_the_app_wire_format(bot_key: BotKey, phone_key: PublicKey) -> None:
    opened = bot_key.decrypt(fixture_text("msg_media.asc"), verify_with=[phone_key])

    assert opened.data.startswith(b"\x01pgpchat-media-v1\x01")
    assert MEDIA_ID in opened.text
    assert "guarda che tramonto" in opened.text
    assert opened.verification.valid


def test_binary_attachment_decrypts_to_the_original_bytes(bot_key: BotKey, phone_key: PublicKey) -> None:
    opened = bot_key.decrypt(fixture_bytes("attachment.pgp"), verify_with=[phone_key])

    assert opened.data == ATTACHMENT_PLAIN
    assert opened.verification == Verification(SignatureStatus.VALID, signer_fingerprint=phone_key.fingerprint)


@pytest.mark.parametrize("name", ["msg_not_for_bot.asc", "msg_for_old_key.asc"])
def test_message_sealed_to_another_key_is_not_for_the_bot(bot_key: BotKey, name: str) -> None:
    with pytest.raises(NotARecipientError):
        bot_key.decrypt(fixture_text(name))


def test_old_key_still_opens_what_was_sealed_to_it(old_bot_key: BotKey, phone_key: PublicKey) -> None:
    opened = old_bot_key.decrypt(fixture_text("msg_for_old_key.asc"), verify_with=[phone_key])

    assert opened.text == PHONE_TEXT
    assert opened.verification.valid


@pytest.mark.parametrize(
    "damage",
    [
        lambda: fixture_text("msg_text_signed.asc")[: len(fixture_text("msg_text_signed.asc")) // 2],
        lambda: fixture_bytes("attachment.pgp")[:-10],
        lambda: fixture_bytes("attachment.pgp")[: len(fixture_bytes("attachment.pgp")) // 2],
        lambda: "this is not an OpenPGP message",
        lambda: b"",
        lambda: flip(fixture_bytes("attachment.pgp"), -1),
        lambda: flip(fixture_bytes("attachment.pgp"), len(fixture_bytes("attachment.pgp")) // 2),
    ],
    ids=[
        "armor-cut-in-half",
        "binary-missing-its-tail",
        "binary-cut-in-half",
        "garbage",
        "empty",
        "last-bit-flipped",
        "middle-bit-flipped",
    ],
)
def test_damaged_message_raises_decryption_error(bot_key: BotKey, damage: Callable[[], str | bytes]) -> None:
    with pytest.raises(DecryptionError) as caught:
        bot_key.decrypt(damage())

    # damage must not pass for "sealed to somebody else"
    assert not isinstance(caught.value, NotARecipientError)


def test_flipped_bit_fails_the_integrity_check(bot_key: BotKey) -> None:
    with pytest.raises(DecryptionError, match="integrity check failed"):
        bot_key.decrypt(flip(fixture_bytes("attachment.pgp"), -1))


class RpgpView:
    """Stands in for rPGP's reading of a message: the content it saw and how its signature check goes."""

    def __init__(self, content: bytes | Exception, verify_error: Exception | None = None) -> None:
        self._content = content
        self._verify_error = verify_error

    def decrypt(self, passphrase: str | None, key: Any) -> RpgpView:
        return self

    def as_data_vec(self) -> bytes:
        if isinstance(self._content, Exception):
            raise self._content
        return self._content

    def signature_count(self) -> int:
        return 1

    def verify(self, key: Any, index: int) -> None:
        if self._verify_error is not None:
            raise self._verify_error


def rpgp_reads(monkeypatch: pytest.MonkeyPatch, view: RpgpView) -> None:
    monkeypatch.setattr(
        "openpgp.composed.Message",
        SimpleNamespace(from_armor=lambda text: (view, {}), from_bytes=lambda data: view),
    )


def rpgp_error(code: str) -> RpgpError:
    error = RpgpError(f"{code.lower()} failure")
    error.code = code
    return error


@pytest.mark.parametrize(
    "rpgp_content",
    [b"Ciao dal telefono: paga 1000 a mallory", rpgp_error("MESSAGE")],
    ids=["other-content", "unreadable-content"],
)
def test_engines_disagreeing_on_the_content_make_it_invalid(
    monkeypatch: pytest.MonkeyPatch, bot_key: BotKey, phone_key: PublicKey, rpgp_content: bytes | Exception
) -> None:
    # the signature itself would check out: what it covers is the question
    rpgp_reads(monkeypatch, RpgpView(rpgp_content))

    opened = bot_key.decrypt(fixture_text("msg_text_signed.asc"), verify_with=[phone_key])

    assert opened.text == PHONE_TEXT
    assert opened.verification.status is SignatureStatus.INVALID
    assert opened.verification.signer_fingerprint is None
    assert "disagree" in (opened.verification.detail or "")


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (lambda: rpgp_error("CRYPTO"), SignatureStatus.INVALID),
        (lambda: ValueError("malformed signature"), SignatureStatus.INVALID),
        (lambda: rpgp_error("BINDING"), SignatureStatus.UNKNOWN_KEY),
    ],
    ids=["crypto-failure", "value-error", "issued-by-another-key"],
)
def test_how_a_failed_signature_check_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    bot_key: BotKey,
    phone_key: PublicKey,
    failure: Callable[[], Exception],
    status: SignatureStatus,
) -> None:
    rpgp_reads(monkeypatch, RpgpView(PHONE_TEXT.encode(), verify_error=failure()))

    opened = bot_key.decrypt(fixture_text("msg_text_signed.asc"), verify_with=[phone_key])

    assert opened.verification.status is status
    assert opened.verification.signer_fingerprint is None


def test_signature_rpgp_cannot_check_is_not_reported_as_invalid(bot_key: BotKey) -> None:
    tsk = pysequoia.Tsk.generate(
        user_id="Post Quantum <pq@example.org>",
        cipher_suite=pysequoia.CipherSuite.MLDSA65_Ed25519,
        profile=pysequoia.Profile.RFC9580,
    )
    sender = BotKey.from_armored(str(tsk))

    opened = bot_key.decrypt(sender.seal("ciao", [bot_key.public_key]), verify_with=[sender.public_key])

    assert opened.text == "ciao"
    assert opened.verification.status is SignatureStatus.NOT_CHECKED


def test_message_rpgp_cannot_open_is_not_checked(
    monkeypatch: pytest.MonkeyPatch, bot_key: BotKey, phone_key: PublicKey
) -> None:
    def refuse(data: Any) -> Any:
        raise rpgp_error("UNSUPPORTED")

    monkeypatch.setattr("openpgp.composed.Message", SimpleNamespace(from_armor=refuse, from_bytes=refuse))

    opened = bot_key.decrypt(fixture_text("msg_text_signed.asc"), verify_with=[phone_key])

    assert opened.text == PHONE_TEXT
    assert opened.verification.status is SignatureStatus.NOT_CHECKED
    assert "rPGP could not open it" in (opened.verification.detail or "")


def test_bot_key_rpgp_cannot_read_still_opens_messages_but_checks_nothing(
    monkeypatch: pytest.MonkeyPatch, phone_key: PublicKey, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(_lib, "rp_secret", lambda armored: None)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        key = BotKey.from_file(FIXTURES / "bot.sec.asc")

    assert any("won't be checked" in m for m in crypto_warnings(caplog))
    assert key.public_key == PublicKey.from_file(FIXTURES / "bot.pub.asc")
    opened = key.decrypt(fixture_text("msg_text_signed.asc"), verify_with=[phone_key])
    assert opened.text == PHONE_TEXT
    assert opened.verification.status is SignatureStatus.NOT_CHECKED


@pytest.mark.parametrize("clone", [pickle.dumps, copy.copy, copy.deepcopy], ids=["pickle", "copy", "deepcopy"])
def test_bot_key_refuses_to_be_pickled_or_copied(bot_key: BotKey, clone: Callable[[Any], Any]) -> None:
    with pytest.raises(TypeError, match="cannot be pickled"):
        clone(bot_key)


def test_bot_key_repr_shows_the_fingerprint_and_nothing_secret(bot_key: BotKey, locked_key: BotKey) -> None:
    assert repr(bot_key) == "<BotKey 7210 EBB5 9E6E D707 CE00 6D9F E194 4D33 D771 2998>"
    assert bot_key.fingerprint == BOT_FPR

    shown = repr(locked_key)
    assert shown == f"<BotKey {format_fingerprint(locked_key.fingerprint)} protected>"
    assert "correct horse" not in shown
    assert str(locked_key) == shown


def test_decrypted_text_replaces_invalid_utf8() -> None:
    decrypted = Decrypted(b"caf\xc3\xa9 \xff ok", Verification(SignatureStatus.UNSIGNED))

    assert decrypted.text == "café � ok"
    assert decrypted.data == b"caf\xc3\xa9 \xff ok"


def test_format_fingerprint_groups_by_four() -> None:
    assert format_fingerprint("51e62ed3 fed07ac7") == "51E6 2ED3 FED0 7AC7"


def test_looks_like_fingerprint() -> None:
    assert looks_like_fingerprint(ANNA_FPR)
    assert looks_like_fingerprint(format_fingerprint(ANNA_FPR))
    assert not looks_like_fingerprint("anna@phone.test")


# --- loading quirks and refusals ---


def test_a_key_file_with_a_byte_order_mark_loads(tmp_path: Path, bot_key: BotKey) -> None:
    path = tmp_path / "bom.asc"
    path.write_bytes(b"\xef\xbb\xbf" + bot_key.export_secret().encode())
    assert BotKey.from_file(path).fingerprint == bot_key.fingerprint
    assert PublicKey.from_armored("\ufeff" + bot_key.export_public()).fingerprint == bot_key.fingerprint


def test_an_empty_passphrase_is_refused_when_making_a_key() -> None:
    with pytest.raises(ConfigurationError, match="empty passphrase"):
        BotKey.generate("Echo", passphrase="")


def test_a_public_key_file_that_is_not_there_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        PublicKey.from_file(tmp_path / "missing.asc")


def test_from_env_takes_the_passphrase_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPCHAT_BOT_KEY_FILE", str(FIXTURES / "bot_locked.sec.asc"))
    monkeypatch.setenv("PGPCHAT_BOT_KEY_PASSPHRASE", "not this one")
    key = BotKey.from_env(lambda: "correct horse")
    assert key is not None
    assert key.protected


def test_a_blank_key_variable_counts_as_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPCHAT_BOT_KEY", "   ")
    assert BotKey.from_env() is None


def test_save_replaces_a_file_only_when_told_and_never_reuses_it(tmp_path: Path, bot_key: BotKey) -> None:
    target = tmp_path / "bot.asc"
    target.write_text("old")
    with pytest.raises(ConfigurationError, match="exists"):
        bot_key.save(target, passphrase=None)
    assert target.read_text() == "old"

    bot_key.save(target, passphrase=None, overwrite=True)
    assert BotKey.from_file(target).fingerprint == bot_key.fingerprint
    # no temporary file left behind
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bot.asc"]


def test_compressed_says_none_when_it_cannot_tell(bot_key: BotKey) -> None:
    assert _lib.compressed(b"not a message", bot_key._rp, None) is None


def test_a_live_encryption_subkey_check_on_the_phone_keys(phone_key: PublicKey, rsa_phone_key: PublicKey) -> None:
    import time as _time

    for key in (phone_key, rsa_phone_key):
        assert _lib.has_live_encryption_subkey(key._rpgp(), _time.time()) is True
    # something that isn't an rPGP key: no verdict rather than a crash
    assert _lib.has_live_encryption_subkey(object(), _time.time()) is None

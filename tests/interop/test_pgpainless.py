"""The SDK against PGPainless 1.7.6, the library the phones run, driven through Interop.java.

Each Interop call is a fresh JVM compiling the source file, a second or two
apiece, so the keys are made once per module and every test makes one call.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from pgpcbot.crypto import BotKey, PublicKey
from pgpcbot.models import SignatureStatus

INTEROP = Path(__file__).with_name("Interop.java")
CLASSPATH = os.environ.get("PGPCBOT_PGPAINLESS_CP", "").strip()
JAVA = os.environ.get("PGPCBOT_JAVA") or shutil.which("java")

pytestmark = [
    pytest.mark.interop,
    pytest.mark.skipif(
        not (CLASSPATH and JAVA), reason="needs PGPCBOT_PGPAINLESS_CP and java (PGPCBOT_JAVA or on PATH)"
    ),
]

TEXT = "Ciao dal bot: è già sera 🌙\nseconda riga\n\tterza, con un tab\n"


def pgpainless(*args: str | Path) -> dict[str, list[str]]:
    """Runs one Interop command; its "KEY value" lines, grouped by key."""
    assert JAVA is not None
    run = subprocess.run(
        [JAVA, "-cp", CLASSPATH, str(INTEROP), *map(str, args)],
        capture_output=True,
        timeout=180,
        check=False,
    )
    out = run.stdout.decode("utf-8")
    assert run.returncode == 0, (
        f"Interop {args[0]} exited {run.returncode}:\n{out}{run.stderr.decode(errors='replace')}"
    )
    lines: dict[str, list[str]] = {}
    in_plaintext = False
    for line in out.splitlines():
        # the human-readable copy of the plaintext: PLAINTEXT_B64 carries the exact bytes
        if line in ("PLAINTEXT_BEGIN", "PLAINTEXT_END"):
            in_plaintext = line == "PLAINTEXT_BEGIN"
            continue
        if not in_plaintext:
            key, _, value = line.partition(" ")
            lines.setdefault(key, []).append(value)
    return lines


@dataclass(frozen=True)
class Phone:
    secret: Path
    fingerprint: str
    """As PGPainless prints it."""
    key: PublicKey
    """As the SDK reads it."""


def make_phone(workdir: Path, kind: str, identity: str) -> Phone:
    home = workdir / kind
    out = pgpainless("gen", kind, identity, home)
    return Phone(home / "secret.asc", out["FPR"][0], PublicKey.from_file(home / "public.asc"))


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pgpainless")


@pytest.fixture(scope="module")
def bot(workdir: Path) -> BotKey:
    key = BotKey.generate("Interop bot", username="interopbot")
    (workdir / "bot.pub.asc").write_text(key.export_public(), encoding="utf-8")
    return key


@pytest.fixture(scope="module")
def ed25519_phone(workdir: Path) -> Phone:
    return make_phone(workdir, "ed25519", "Anna <anna@phone.test>")


@pytest.fixture(scope="module")
def rsa_phone(workdir: Path) -> Phone:
    return make_phone(workdir, "rsa3072", "Bruno <bruno@phone.test>")


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_pgpainless_reads_a_fresh_bot_key(bot: BotKey, workdir: Path) -> None:
    out = pgpainless("fpr", workdir / "bot.pub.asc")

    assert out["FPR"] == [bot.fingerprint]
    assert "INFO_ERROR" not in out
    info = " ".join(out["INFO"])
    assert "version=4" in info
    assert "userIds=[Interop bot (@interopbot)]" in info
    assert "usableForEncryption=true" in info
    assert "signingCapable=true" in info
    assert "encryptionSubkeys=[]" not in info


@pytest.mark.parametrize("phone_fixture", ["ed25519_phone", "rsa_phone"])
def test_phone_opens_and_verifies_what_the_bot_seals(
    request: pytest.FixtureRequest, bot: BotKey, workdir: Path, phone_fixture: str
) -> None:
    phone: Phone = request.getfixturevalue(phone_fixture)
    # both libraries name the phone's key the same way
    assert phone.key.fingerprint == phone.fingerprint
    sealed = write(workdir / f"to-{phone_fixture}.asc", bot.seal(TEXT, [phone.key]).encode("ascii"))

    out = pgpainless("decrypt", sealed, phone.secret, workdir / "bot.pub.asc")

    assert base64.b64decode(out["PLAINTEXT_B64"][0]) == TEXT.encode("utf-8")
    assert out["SIG_VERIFIED"] == ["true"]
    assert out["ENCRYPTED"] == ["true"]
    assert "REJECTED_SIG" not in out


def test_bot_verifies_a_message_a_phone_signed(bot: BotKey, workdir: Path, ed25519_phone: Phone) -> None:
    plain = write(workdir / "signed.txt", TEXT.encode("utf-8"))
    sealed = workdir / "signed.asc"

    out = pgpainless("encrypt", plain, sealed, ed25519_phone.secret, workdir / "bot.pub.asc")

    # compressed, the way every phone writes: the signature sits inside that layer
    assert out["COMPRESSION"] == ["ZIP"]
    opened = bot.decrypt(sealed.read_bytes(), verify_with=[ed25519_phone.key])
    assert opened.text == TEXT
    assert opened.verification.status is SignatureStatus.VALID
    assert opened.verification.signer_fingerprint == ed25519_phone.fingerprint


def test_bot_reports_an_unsigned_phone_message(bot: BotKey, workdir: Path, ed25519_phone: Phone) -> None:
    plain = write(workdir / "unsigned.txt", TEXT.encode("utf-8"))
    sealed = workdir / "unsigned.asc"

    pgpainless("encrypt-nosign", plain, sealed, workdir / "bot.pub.asc")

    opened = bot.decrypt(sealed.read_bytes(), verify_with=[ed25519_phone.key])
    assert opened.text == TEXT
    assert opened.verification.status is SignatureStatus.UNSIGNED


def test_streamed_attachment_decrypts_to_the_same_bytes(bot: BotKey, workdir: Path, ed25519_phone: Phone) -> None:
    original = b"\xff\xd8\xff" + bytes(range(256)) * 64
    plain = write(workdir / "photo.jpg", original)
    sealed = workdir / "photo.pgp"

    pgpainless("encrypt-stream", plain, sealed, ed25519_phone.secret, workdir / "bot.pub.asc")

    data = sealed.read_bytes()
    assert not data.startswith(b"-----")
    opened = bot.decrypt(data, verify_with=[ed25519_phone.key])
    assert opened.data == original
    assert opened.verification.status is SignatureStatus.VALID
    assert opened.verification.signer_fingerprint == ed25519_phone.fingerprint

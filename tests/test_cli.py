from __future__ import annotations

import io
import subprocess
import sys
from typing import Any, Self

import pytest

import pgpcbot.__main__ as cli
from pgpcbot import BotKey, Client, PassphraseError, PublicKey, WebhookInfo, __version__, format_fingerprint

from .conftest import FAST_RETRY, FIXTURES
from .fakeapi import BASE, TOKEN, FakeAPI


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def server(fake: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    """The CLI's Client, talking to the fake server."""
    monkeypatch.setattr(
        cli, "Client", lambda: Client(TOKEN, api_url=BASE, transport=fake, retry=FAST_RETRY, rate_limiter=False)
    )
    return fake


def type_at_prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Answers getpass prompts on a pretend terminal; returns the prompts shown."""
    prompts: list[str] = []
    replies = iter(answers)

    def getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    return prompts


def test_keygen_without_passphrase_writes_the_key_and_its_public_half(tmp_path, capsys):
    out, public = tmp_path / "bot-key.asc", tmp_path / "bot-key.pub.asc"

    names = ["--name", "Weather", "--username", "weatherbot"]
    code = cli.main(["keygen", *names, "--out", str(out), "--public", str(public), "--no-passphrase"])

    assert code == 0
    key = BotKey.from_file(out)
    assert not key.protected
    assert capsys.readouterr().out.splitlines() == [
        f"private key: {out}  (NOT protected by a passphrase)",
        f"public key:  {public}",
        f"fingerprint: {format_fingerprint(key.fingerprint)}",
    ]
    published = PublicKey.from_file(public)
    assert published.fingerprint == key.fingerprint
    assert published.user_ids == ["Weather (@weatherbot)"]


def test_keygen_with_a_passphrase_from_a_variable_protects_the_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BOT_KEY_PASS", "correct horse battery")
    out = tmp_path / "bot-key.asc"

    assert cli.main(["keygen", "--name", "Weather", "--out", str(out), "--passphrase-env", "BOT_KEY_PASS"]) == 0

    printed = capsys.readouterr().out
    assert "NOT protected" not in printed
    with pytest.raises(PassphraseError):
        BotKey.from_file(out)
    key = BotKey.from_file(out, "correct horse battery")
    assert key.protected
    assert f"fingerprint: {format_fingerprint(key.fingerprint)}" in printed


def test_keygen_stops_when_the_passphrase_variable_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_KEY_PASS", "")
    out = tmp_path / "bot-key.asc"

    with pytest.raises(SystemExit, match="BOT_KEY_PASS is empty or not set"):
        cli.main(["keygen", "--name", "Weather", "--out", str(out), "--passphrase-env", "BOT_KEY_PASS"])
    assert not out.exists()


def test_keygen_refuses_to_overwrite_a_key_unless_forced(tmp_path, capsys):
    out = tmp_path / "bot-key.asc"
    args = ["keygen", "--name", "Weather", "--out", str(out), "--no-passphrase"]
    assert cli.main(args) == 0
    original = out.read_bytes()
    capsys.readouterr()

    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "exists" in captured.err
    assert out.read_bytes() == original

    assert cli.main([*args, "--force"]) == 0
    assert BotKey.from_file(out).fingerprint != BotKey.from_armored(original).fingerprint


def test_keygen_without_a_terminal_asks_for_a_passphrase_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    out = tmp_path / "bot-key.asc"

    with pytest.raises(SystemExit, match="--passphrase-env VAR or --no-passphrase"):
        cli.main(["keygen", "--name", "Weather", "--out", str(out)])
    assert not out.exists()


def test_keygen_asks_twice_for_the_passphrase_on_a_terminal(tmp_path, monkeypatch):
    prompts = type_at_prompts(monkeypatch, "s3cret words", "s3cret words")
    out = tmp_path / "bot-key.asc"

    assert cli.main(["keygen", "--name", "Weather", "--out", str(out)]) == 0

    assert prompts == ["Passphrase (empty for none): ", "Again: "]
    assert BotKey.from_file(out, "s3cret words").protected


def test_keygen_stops_when_the_two_passphrases_differ(tmp_path, monkeypatch):
    type_at_prompts(monkeypatch, "one thing", "another")
    out = tmp_path / "bot-key.asc"

    with pytest.raises(SystemExit, match="differ"):
        cli.main(["keygen", "--name", "Weather", "--out", str(out)])
    assert not out.exists()


def test_keygen_takes_an_empty_terminal_passphrase_as_none(tmp_path, monkeypatch, capsys):
    prompts = type_at_prompts(monkeypatch, "")
    out = tmp_path / "bot-key.asc"

    assert cli.main(["keygen", "--name", "Weather", "--out", str(out)]) == 0

    assert prompts == ["Passphrase (empty for none): "]
    assert not BotKey.from_file(out).protected
    assert "NOT protected" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "owner"),
    [("phone_ed.pub.asc", "phone_key"), ("phone_rsa.pub.asc", "rsa_phone_key"), ("bot.sec.asc", "bot_key")],
)
def test_fingerprint_of_a_public_or_private_key_file(capsys, request, name, owner):
    expected = request.getfixturevalue(owner).fingerprint

    assert cli.main(["fingerprint", str(FIXTURES / name)]) == 0
    assert capsys.readouterr().out == f"{expected}\n"


def test_fingerprint_spaced_in_groups_of_four(capsys, phone_key):
    assert cli.main(["fingerprint", str(FIXTURES / "phone_ed.pub.asc"), "--spaced"]) == 0

    groups = capsys.readouterr().out.split()
    assert groups == [phone_key.fingerprint[i : i + 4] for i in range(0, 40, 4)]


def test_fingerprint_of_a_locked_key_needs_no_passphrase(capsys, monkeypatch):
    # the fingerprint is public: printing it doesn't unlock anything
    locked = str(FIXTURES / "bot_locked.sec.asc")
    expected = BotKey.from_file(locked, "correct horse").fingerprint

    assert cli.main(["fingerprint", locked]) == 0
    assert capsys.readouterr().out.strip() == expected

    monkeypatch.setenv("BOT_KEY_PASS", "correct horse")
    assert cli.main(["fingerprint", locked, "--passphrase-env", "BOT_KEY_PASS"]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_fingerprint_of_a_passphrase_sealed_key_file(tmp_path, capsys, monkeypatch, bot_key):
    sealed = bot_key.save(tmp_path / "sealed.asc", passphrase="sealing words")
    monkeypatch.setenv("BOT_KEY_PASS", "sealing words")

    assert cli.main(["fingerprint", str(sealed), "--passphrase-env", "BOT_KEY_PASS"]) == 0
    assert capsys.readouterr().out.strip() == bot_key.fingerprint


def test_fingerprint_of_something_that_is_not_a_key_exits_with_2(tmp_path, capsys):
    junk = tmp_path / "junk.asc"
    junk.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nnope\n-----END PGP PUBLIC KEY BLOCK-----\n")

    assert cli.main(["fingerprint", str(junk)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


def test_fingerprint_of_a_missing_file_exits_with_2(tmp_path, capsys):
    assert cli.main(["fingerprint", str(tmp_path / "missing.asc")]) == 2
    assert capsys.readouterr().err.startswith("error: ")


def test_whoami_describes_an_open_bot(server, capsys):
    assert cli.main(["whoami"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "@testbot (id 412) - Test",
        "mode:     open",
        "status:   active, privacy mode on",
        "webhook:  none (polling)",
        "queued:   0 update(s)",
    ]
    [call] = server.calls("GET", "/bot/v1/me")
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_whoami_shows_the_published_key_the_webhook_and_the_queue(server, capsys, bot_key):
    server.me.update(mode="protected", fingerprint=bot_key.fingerprint, privacyMode=False)
    server.webhook["url"] = "https://bot.example/hook"
    server.add_chat(7)
    server.push_message(7, "waiting")

    assert cli.main(["whoami"]) == 0

    assert capsys.readouterr().out.splitlines()[1:] == [
        f"mode:     protected, key {format_fingerprint(bot_key.fingerprint)}",
        "status:   active, privacy mode off",
        "webhook:  https://bot.example/hook",
        "queued:   1 update(s)",
    ]


@pytest.mark.parametrize(
    ("webhook", "expected"),
    [
        ({"url": None, "secret": None}, ["url:      none", "secret:   none"]),
        ({"url": "https://bot.example/hook", "secret": "s"}, ["url:      https://bot.example/hook", "secret:   set"]),
    ],
    ids=["none", "set"],
)
def test_webhook_info(server, capsys, webhook, expected):
    server.webhook.update(webhook)

    assert cli.main(["webhook", "info"]) == 0

    assert capsys.readouterr().out.splitlines() == [*expected, "pending:  0, failures in a row: 0"]


def test_webhook_info_shows_why_deliveries_fail(monkeypatch, capsys):
    class FailingDeliveries:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get_webhook(self) -> WebhookInfo:
            return WebhookInfo(
                url="https://bot.example/hook", has_secret=True, pending=3, failures=2, last_error="connect timeout"
            )

    monkeypatch.setattr(cli, "Client", FailingDeliveries)

    assert cli.main(["webhook", "info"]) == 0

    assert capsys.readouterr().out.splitlines()[2:] == [
        "pending:  3, failures in a row: 2",
        "last error: connect timeout",
    ]


def test_webhook_set_generates_a_secret_when_none_is_given(server, capsys):
    assert cli.main(["webhook", "set", "https://bot.example/hook"]) == 0

    secret = server.webhook["secret"]
    generated, done = capsys.readouterr().out.splitlines()
    assert secret
    assert generated.endswith(f": {secret}")
    assert "won't be shown again" in generated
    assert done == "webhook set: https://bot.example/hook, 0 update(s) pending"
    assert server.webhook["url"] == "https://bot.example/hook"


def test_webhook_set_takes_the_secret_from_a_variable(server, capsys, monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", "Shared_secret-0123456789abcdef")

    assert cli.main(["webhook", "set", "https://bot.example/hook", "--secret-env", "HOOK_SECRET"]) == 0

    assert server.webhook["secret"] == "Shared_secret-0123456789abcdef"
    out = capsys.readouterr().out
    assert "generated" not in out
    assert "Shared_secret" not in out


def test_webhook_set_stops_without_a_url_or_with_an_empty_secret_variable(server, monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", "")

    with pytest.raises(SystemExit, match="needs a URL"):
        cli.main(["webhook", "set"])
    with pytest.raises(SystemExit, match="HOOK_SECRET is empty or not set"):
        cli.main(["webhook", "set", "https://bot.example/hook", "--secret-env", "HOOK_SECRET"])
    assert server.calls("POST", "/bot/v1/webhook") == []
    assert server.webhook["url"] is None


def test_webhook_set_refused_exits_with_2_on_stderr(server, capsys):
    assert cli.main(["webhook", "set", "http://bot.example/hook"]) == 2

    assert capsys.readouterr().err.startswith("error: ")
    assert server.webhook["url"] is None


def test_webhook_delete(server, capsys):
    server.webhook.update(url="https://bot.example/hook", secret="s")

    assert cli.main(["webhook", "delete"]) == 0

    assert capsys.readouterr().out == "webhook removed, 0 update(s) pending\n"
    assert server.webhook["url"] is None


def test_publish_key_with_yes_publishes_without_asking(server, capsys, monkeypatch, bot_key):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked for a confirmation"))

    assert cli.main(["publish-key", str(FIXTURES / "bot.sec.asc"), "--yes"]) == 0

    [put] = server.calls("PUT", "/bot/v1/key")
    assert put["json"]["fingerprint"] == bot_key.fingerprint
    assert PublicKey.from_armored(put["json"]["publicKey"]).fingerprint == bot_key.fingerprint
    assert "PRIVATE" not in put["json"]["publicKey"]
    assert capsys.readouterr().out == f"published {format_fingerprint(bot_key.fingerprint)}, mode protected\n"
    assert server.me["mode"] == "protected"


def test_publish_key_goes_on_once_the_owner_types_publish(server, capsys, monkeypatch, bot_key):
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "publish")

    assert cli.main(["publish-key", str(FIXTURES / "bot.sec.asc")]) == 0

    assert prompts == ["Type 'publish' to go on: "]
    assert "Only the owner can undo it" in capsys.readouterr().out
    assert len(server.calls("PUT", "/bot/v1/key")) == 1


def test_publish_key_does_nothing_unless_the_owner_types_publish(server, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    assert cli.main(["publish-key", str(FIXTURES / "bot.sec.asc")]) == 1

    assert capsys.readouterr().out.splitlines()[-1] == "nothing done"
    assert server.calls("PUT", "/bot/v1/key") == []


def test_publish_key_opens_a_locked_key_with_the_passphrase_variable(server, monkeypatch, capsys):
    locked = FIXTURES / "bot_locked.sec.asc"

    assert cli.main(["publish-key", str(locked), "--yes"]) == 2
    assert "passphrase" in capsys.readouterr().err
    assert server.calls("PUT", "/bot/v1/key") == []

    monkeypatch.setenv("BOT_KEY_PASS", "correct horse")
    assert cli.main(["publish-key", str(locked), "--passphrase-env", "BOT_KEY_PASS", "--yes"]) == 0
    [put] = server.calls("PUT", "/bot/v1/key")
    assert put["json"]["fingerprint"] == BotKey.from_file(locked, "correct horse").fingerprint


def test_publish_key_of_a_missing_file_exits_with_2(server, tmp_path, capsys):
    assert cli.main(["publish-key", str(tmp_path / "missing.asc"), "--yes"]) == 2

    assert capsys.readouterr().err.startswith("error: cannot read the key file")
    assert server.requests == []


def test_api_error_exits_with_2_on_stderr_without_leaking_the_token(server, capsys):
    server.revoked = True

    assert cli.main(["whoami"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "unauthorized" in captured.err
    assert TOKEN not in captured.err


def test_missing_token_exits_with_2_on_stderr(capsys):
    # the real Client: the environment is clean, so it has no token to send anywhere
    assert cli.main(["whoami"]) == 2

    assert "PGPCHAT_BOT_TOKEN" in capsys.readouterr().err


def test_ctrl_c_exits_with_130(monkeypatch):
    def interrupted() -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "Client", interrupted)

    assert cli.main(["whoami"]) == 130


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])

    assert info.value.code == 0
    assert capsys.readouterr().out.strip() == f"pgpcbot {__version__}"


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main([])

    assert info.value.code == 2
    assert "required" in capsys.readouterr().err


def test_python_dash_m_runs_the_cli_with_its_exit_codes(phone_key):
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pgpcbot", *args], capture_output=True, text=True, timeout=60, check=False
        )

    ok = run("fingerprint", str(FIXTURES / "phone_ed.pub.asc"))
    assert (ok.returncode, ok.stdout.strip()) == (0, phone_key.fingerprint)

    # no token in the environment: refused before any network
    refused = run("whoami")
    assert refused.returncode == 2
    assert refused.stderr.startswith("error: ")


def test_keygen_refuses_to_overwrite_the_public_file_unless_forced(tmp_path, capsys):
    public = tmp_path / "bot.pub.asc"
    public.write_text("keep me")
    args = ["keygen", "--name", "Echo", "--out", str(tmp_path / "bot.asc"), "--public", str(public), "--no-passphrase"]

    assert cli.main(args) == 2
    assert "exists" in capsys.readouterr().err
    assert public.read_text() == "keep me"
    assert not (tmp_path / "bot.asc").exists()

    assert cli.main([*args, "--force"]) == 0
    assert "BEGIN PGP PUBLIC KEY BLOCK" in public.read_text()


def test_publish_key_refuses_a_v6_key(server, tmp_path, capsys):
    import pysequoia

    six = tmp_path / "six.asc"
    six.write_text(str(pysequoia.Tsk.generate("Six", profile=pysequoia.Profile.RFC9580)))

    assert cli.main(["publish-key", str(six), "--yes"]) == 2
    assert "v4" in capsys.readouterr().err
    assert server.calls("PUT", "/bot/v1/key") == []


def test_publish_key_asks_for_the_passphrase_of_a_locked_key_on_a_terminal(server, monkeypatch, capsys):
    prompts = type_at_prompts(monkeypatch, "correct horse")
    locked = str(FIXTURES / "bot_locked.sec.asc")

    assert cli.main(["publish-key", locked, "--yes"]) == 0
    assert prompts == ["Passphrase of the key: "]
    assert len(server.calls("PUT", "/bot/v1/key")) == 1


def test_fingerprint_of_a_sealed_key_file_asks_for_its_passphrase(tmp_path, monkeypatch, capsys, bot_key):
    sealed = bot_key.save(tmp_path / "sealed.asc", passphrase="sealing words")
    type_at_prompts(monkeypatch, "sealing words")

    assert cli.main(["fingerprint", str(sealed)]) == 0
    assert capsys.readouterr().out.strip() == bot_key.fingerprint

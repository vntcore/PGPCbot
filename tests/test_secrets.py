from __future__ import annotations

import logging
import pickle
from collections.abc import Iterator
from pathlib import Path

import pytest

from pgpcbot import ConfigurationError, Secret, _secrets
from pgpcbot._secrets import RedactingFilter, install_redaction, read_env, resolve, scrub

from .conftest import fixture_text
from .fakeapi import TOKEN

SECRET_PART = TOKEN.split(".", 1)[1]
PRIVATE_BLOCK = fixture_text("bot.sec.asc").strip()
PUBLIC_BLOCK = fixture_text("bot.pub.asc").strip()


@pytest.fixture
def redacted_logger() -> Iterator[logging.Logger]:
    """A logger outside the pgpcbot tree with one RedactingFilter on it."""
    logger = logging.getLogger("tests.redaction")
    flt = RedactingFilter()
    logger.addFilter(flt)
    yield logger
    logger.removeFilter(flt)


@pytest.fixture
def fresh_redaction(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[logging.Filter]]:
    """install_redaction() as on a first run; whatever it adds is taken off again afterwards."""
    monkeypatch.setattr(_secrets, "_installed", False)
    before: dict[str, list[logging.Filter]] = {}
    for name in _secrets._LOGGERS:
        logger = logging.getLogger(name)
        before[name] = list(logger.filters)
        monkeypatch.setattr(logger, "filters", list(logger.filters))
    return before


def added_filters(before: dict[str, list[logging.Filter]], name: str) -> list[logging.Filter]:
    return [f for f in logging.getLogger(name).filters if f not in before[name]]


def test_secret_never_shows_its_value() -> None:
    secret = Secret(TOKEN)
    for shown in (repr(secret), str(secret), f"{secret}", repr([secret]), format(secret, "")):
        assert SECRET_PART not in shown
    assert repr(secret) == "Secret('***')"
    assert str(secret) == "Secret('***')"


def test_secret_reveals_on_request() -> None:
    assert Secret(TOKEN).reveal() == TOKEN


def test_secret_truthiness_follows_the_value() -> None:
    assert Secret("x")
    assert not Secret("")


@pytest.mark.parametrize("value", [None, 42, TOKEN.encode(), Secret("nested")])
def test_secret_holds_strings_only(value: object) -> None:
    with pytest.raises(TypeError, match="a Secret holds a string"):
        Secret(value)  # type: ignore[arg-type]


def test_secret_cannot_be_pickled() -> None:
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(Secret(TOKEN))


def test_secret_logged_as_an_argument_stays_hidden(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="tests.secret"):
        logging.getLogger("tests.secret").info("using %s and %r", Secret("hunter2"), Secret("hunter2"))
    assert "hunter2" not in caplog.text
    assert "Secret('***')" in caplog.text


def test_resolve_takes_every_kind_of_source() -> None:
    assert resolve(None, "token") is None
    assert resolve("plain", "token") == "plain"
    assert resolve(Secret("wrapped"), "token") == "wrapped"
    assert resolve(lambda: "from a vault", "token") == "from a vault"
    assert resolve(lambda: Secret("vault secret"), "token") == "vault secret"
    assert resolve(lambda: None, "token") is None


def test_resolve_asks_the_provider_every_time() -> None:
    answers = iter(["first", "second"])
    assert resolve(answers.__next__, "token") == "first"
    assert resolve(answers.__next__, "token") == "second"


def test_resolve_refuses_a_provider_that_returns_something_else() -> None:
    with pytest.raises(ConfigurationError, match="the passphrase provider returned bytes, not a string") as info:
        resolve(lambda: b"hunter2", "passphrase")
    assert "hunter2" not in str(info.value)


@pytest.mark.parametrize("value", [TOKEN.encode(), 412, [TOKEN]])
def test_resolve_refuses_other_types_without_echoing_them(value: object) -> None:
    with pytest.raises(ConfigurationError, match="token must be a string, a Secret or a callable, got") as info:
        resolve(value, "token")  # type: ignore[arg-type]
    assert SECRET_PART not in str(info.value)


def test_read_env_from_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPCHAT_TEST_SECRET", "value")
    assert read_env("PGPCHAT_TEST_SECRET") == "value"


def test_read_env_unset_or_empty_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert read_env("PGPCHAT_TEST_SECRET") is None
    monkeypatch.setenv("PGPCHAT_TEST_SECRET", "")
    assert read_env("PGPCHAT_TEST_SECRET") is None


@pytest.mark.parametrize(
    ("content", "value"),
    [
        ("s3cret\n", "s3cret"),
        ("s3cret\r\n", "s3cret"),
        ("s3cret", "s3cret"),
        ("  spaced value \t\n", "  spaced value \t"),
        ("line one\nline two\n", "line one\nline two"),
        ("pässwörd\n", "pässwörd"),
    ],
)
def test_read_env_from_a_file_drops_the_final_newline_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, value: str
) -> None:
    path = tmp_path / "secret"
    path.write_bytes(content.encode("utf-8"))
    monkeypatch.setenv("PGPCHAT_TEST_SECRET_FILE", str(path))
    assert read_env("PGPCHAT_TEST_SECRET") == value


def test_read_env_file_wins_over_the_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("from the file\n", encoding="utf-8")
    monkeypatch.setenv("PGPCHAT_TEST_SECRET", "from the environment")
    monkeypatch.setenv("PGPCHAT_TEST_SECRET_FILE", str(path))
    assert read_env("PGPCHAT_TEST_SECRET") == "from the file"


def test_read_env_with_an_empty_file_variable_uses_the_plain_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPCHAT_TEST_SECRET", "plain")
    monkeypatch.setenv("PGPCHAT_TEST_SECRET_FILE", "")
    assert read_env("PGPCHAT_TEST_SECRET") == "plain"


def test_read_env_with_a_file_that_cannot_be_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setenv("PGPCHAT_TEST_SECRET", "fallback must not be used")
    monkeypatch.setenv("PGPCHAT_TEST_SECRET_FILE", str(missing))
    with pytest.raises(ConfigurationError, match=r"PGPCHAT_TEST_SECRET_FILE points to .*nope.*cannot be read") as info:
        read_env("PGPCHAT_TEST_SECRET")
    assert info.value.__cause__ is None


def test_scrub_removes_tokens() -> None:
    other = "pgpbot_9." + "z" * 25
    assert scrub(f"a {TOKEN} b {other} c") == "a pgpbot_**** b pgpbot_**** c"


def test_scrub_leaves_short_look_alikes() -> None:
    assert scrub("pgpbot_412.short and pgpbot_x.yyyyyyyyyyyyyyyyyyyyyyyy") == (
        "pgpbot_412.short and pgpbot_x.yyyyyyyyyyyyyyyyyyyyyyyy"
    )


def test_scrub_removes_a_private_key_block_and_keeps_the_rest() -> None:
    text = f"loaded key:\n{PRIVATE_BLOCK}\ndone"
    assert scrub(text) == "loaded key:\n[private key removed]\ndone"


def test_scrub_removes_a_cut_off_private_block_to_the_end() -> None:
    cut = PRIVATE_BLOCK[: len(PRIVATE_BLOCK) // 2]
    assert scrub("head " + cut) == "head [private key removed]"


def test_scrub_removes_each_private_block_on_its_own() -> None:
    text = f"{PRIVATE_BLOCK}\nbetween\n{PRIVATE_BLOCK}"
    assert scrub(text) == "[private key removed]\nbetween\n[private key removed]"


def test_scrub_keeps_public_keys() -> None:
    assert scrub(PUBLIC_BLOCK) == PUBLIC_BLOCK


def test_filter_scrubs_the_arguments_of_a_record(
    redacted_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=redacted_logger.name):
        redacted_logger.info("calling with %s", TOKEN)
    (record,) = caplog.records
    assert record.getMessage() == "calling with pgpbot_****"
    assert record.args is None
    assert SECRET_PART not in caplog.text


def test_filter_scrubs_mapping_arguments_and_the_message_itself(
    redacted_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=redacted_logger.name):
        redacted_logger.info("config %(token)s", {"token": TOKEN})
        redacted_logger.info(f"key was {PRIVATE_BLOCK}")
    assert [r.getMessage() for r in caplog.records] == ["config pgpbot_****", "key was [private key removed]"]


def test_filter_leaves_clean_records_alone(redacted_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=redacted_logger.name):
        redacted_logger.info("sent %d messages to %s", 3, "chat 7")
    (record,) = caplog.records
    assert (record.msg, record.args) == ("sent %d messages to %s", (3, "chat 7"))


def test_filter_never_drops_a_record() -> None:
    flt = RedactingFilter()
    secret = logging.makeLogRecord({"msg": "t=%s", "args": (TOKEN,)})
    broken = logging.makeLogRecord({"msg": "%d apples", "args": ("many",)})
    assert flt.filter(secret) is True
    # a broken format string is passed on untouched for the handler to report
    assert flt.filter(broken) is True
    assert (broken.msg, broken.args) == ("%d apples", ("many",))


def test_filter_scrubs_the_traceback_too(redacted_logger: logging.Logger, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=redacted_logger.name):
        try:
            raise RuntimeError(f"proxy answered: Authorization: Bearer {TOKEN}")
        except RuntimeError:
            redacted_logger.exception("send failed")
    assert "send failed" in caplog.text
    assert SECRET_PART not in caplog.text


def test_install_redaction_puts_one_shared_filter_on_every_pgpcbot_logger(
    fresh_redaction: dict[str, list[logging.Filter]],
) -> None:
    install_redaction()
    install_redaction()
    added = [added_filters(fresh_redaction, name) for name in _secrets._LOGGERS]
    assert all(len(found) == 1 for found in added)
    shared = added[0][0]
    assert isinstance(shared, RedactingFilter)
    assert all(found[0] is shared for found in added)


@pytest.mark.parametrize(
    "name", ["pgpcbot", "pgpcbot.bot", "pgpcbot.client", "pgpcbot.crypto", "pgpcbot.engine", "pgpcbot.webhook"]
)
def test_install_redaction_covers_records_of_every_pgpcbot_logger(
    fresh_redaction: dict[str, list[logging.Filter]], caplog: pytest.LogCaptureFixture, name: str
) -> None:
    # a filter on "pgpcbot" alone never sees what "pgpcbot.client" logs, so each needs its own
    install_redaction()
    with caplog.at_level(logging.INFO, logger=name):
        logging.getLogger(name).warning("retrying with %s", TOKEN)
    assert caplog.records
    assert SECRET_PART not in caplog.text
    assert "retrying with pgpbot_****" in caplog.text


def test_install_redaction_on_a_given_logger(
    fresh_redaction: dict[str, list[logging.Filter]], caplog: pytest.LogCaptureFixture
) -> None:
    mine = logging.getLogger("tests.my_app")
    install_redaction(mine)
    try:
        with caplog.at_level(logging.INFO, logger=mine.name):
            mine.info("token %s", TOKEN)
        assert SECRET_PART not in caplog.text
        # the pgpcbot loggers were left for the global install to do
        assert all(added_filters(fresh_redaction, name) == [] for name in _secrets._LOGGERS)
        install_redaction()
        assert all(len(added_filters(fresh_redaction, name)) == 1 for name in _secrets._LOGGERS)
    finally:
        for flt in [f for f in mine.filters if isinstance(f, RedactingFilter)]:
            mine.removeFilter(flt)

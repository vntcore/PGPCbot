# Configuration

Everything has a sensible default. Most bots only need a token, and an encrypted bot also
needs its key.

## Environment variables

The library reads these when you don't pass the value in code. Every variable that holds a
secret also has a `_FILE` form: the variable then holds the path of a file with the value,
which is how Docker and Kubernetes secrets work.

| Variable | What it holds | Used by |
|---|---|---|
| `PGPCHAT_BOT_TOKEN` / `PGPCHAT_BOT_TOKEN_FILE` | the bot's token | `Bot`, `SyncBot`, `AsyncClient`, `Client`, the `pgpcbot` command |
| `PGPCHAT_API` | the API server, without `/bot/v1`. Default: `https://api.pgpchat.app` | the same |
| `PGPCHAT_BOT_KEY` / `PGPCHAT_BOT_KEY_FILE` | the bot's private key: the armored text, or a path to it | `Bot`, `SyncBot`, `BotKey.from_env()` |
| `PGPCHAT_BOT_KEY_PASSPHRASE` / `PGPCHAT_BOT_KEY_PASSPHRASE_FILE` | the key's passphrase | the same |

A value passed in code always wins over the environment.

:::{admonition} Keep secrets out of your code
:class: warning

Don't write the token or the passphrase in a `.py` file. Files end up in repositories,
backups and screenshots. Use the variables above, a `.env` file that is never committed, or
a secrets manager. The project has a `.env.example` to copy.
:::

## `Bot` and `SyncBot` options

`Bot` and `SyncBot` take the same options. All of them are keyword arguments, except the token.

```python
from pgpcbot import Bot, SQLiteStorage

bot = Bot(
    token=None,                   # None: read PGPCHAT_BOT_TOKEN
    api_url=None,                 # None: read PGPCHAT_API, else the public server
    pgp_private_key="bot-key.asc",
    pgp_passphrase=lambda: read_from_vault("bot-key"),
    storage=SQLiteStorage("bot.db"),
    concurrency=8,
)
```

### Connection

| Option | Type | Default | Meaning |
|---|---|---|---|
| `token` | `str`, `Secret`, a function that returns `str`, or `None` | `None` | the bot's token. `None` reads `PGPCHAT_BOT_TOKEN` (or `_FILE`). A function is called once, when the bot is created |
| `api_url` | `str` or `None` | `None` | the server's address, like `https://api.pgpchat.app`. `None` reads `PGPCHAT_API` |
| `timeout` | `float`, `httpx2.Timeout` or `None` | 30 s (10 s to connect) | how long one HTTP request may take. Long polls get extra time on their own |
| `retry` | `RetryPolicy` or `None` | `RetryPolicy()` | how many times to try again, see [below](#retries) |
| `rate_limit` | `bool` or `RateLimiter` | `True` | paces calls like the server does, see [Rate limits](../guides/rate-limiting.md) |
| `allow_insecure_http` | `bool` | `False` | allows an `http://` server that is not `localhost`. Only for a test server on your own network |
| `transport` | an httpx2 transport | `None` | advanced: a custom transport (a proxy setup, a test double) |

### OpenPGP

| Option | Type | Default | Meaning |
|---|---|---|---|
| `pgp_private_key` | `BotKey`, a path, the armored text, or `None` | `None` | the bot's private key. `None` reads `PGPCHAT_BOT_KEY` (or `_FILE`); with neither, the bot has no key |
| `pgp_passphrase` | `str`, `Secret`, a function, or `None` | `None` | the key's passphrase. `None` reads `PGPCHAT_BOT_KEY_PASSPHRASE` (or `_FILE`) |
| `pgp_previous_keys` | list of `BotKey` | `()` | older keys, still tried to decrypt messages after a [rotation](../CRYPTOGRAPHY.md#35-rotating-it) |
| `publish_key` | `bool` | `False` | at start-up, publish the key if the server has a different one or none. **One-way**: see [OpenPGP](../guides/openpgp.md) |
| `require_encryption` | `bool` | `False` | never send anything in clear, anywhere: a send into a readable chat raises `EncryptionRequiredError` |
| `verify_signatures` | `"auto"`, `"require"` or `"off"` | `"auto"` | `"auto"` checks signatures and reports in `message.verification`; `"require"` refuses messages without a valid signature; `"off"` does not check |
| `pin_keys` | `bool` | `False` | remember each person's key fingerprint and refuse a changed key, for sending and for receiving ([trusting keys](../CRYPTOGRAPHY.md#7-trusting-keys)) |

### Running

| Option | Type | Default | Meaning |
|---|---|---|---|
| `storage` | `Storage`, a path, or `None` | `None` | where updates wait to be handled. A path means `SQLiteStorage(path)`; `None` means `MemoryStorage()`. See [Storage](../guides/storage.md) |
| `concurrency` | `int` | `8` | how many chats are handled at the same time. Updates of one chat are always handled one at a time, in order. For `SyncBot` it is also the number of handler threads |
| `max_handler_runs` | `int` | `5` | an update whose handlers were started this many times without ever finishing (the process crashed or was stopped half way) goes to the failed list instead of running again |
| `shutdown_timeout` | `float` | `30.0` | at stop, how many seconds running handlers get to finish |
| `cache_ttl` | `float` | `60.0` | how long chat details, modes and member keys are cached, in seconds |
| `auto_answer_callbacks` | `bool` | `True` | stop the spinner of a pressed button if the handler did not answer it |
| `name` | `str` or `None` | `"bot"` | the name of the bot's root router |

## `AsyncClient` and `Client` options

The low-level clients take the connection options above (`token`, `api_url`, `timeout`,
`retry`, `allow_insecure_http`, `transport`) and a few more:

| Option | Type | Default | Meaning |
|---|---|---|---|
| `rate_limiter` | `RateLimiter` or `bool` | `True` | `True` makes a new limiter, `False` turns it off |
| `user_agent` | `str` or `None` | `PGPCbot/<version> (Python <version>)` | the `User-Agent` header |
| `http_client` | `httpx2.AsyncClient` / `httpx2.Client` or `None` | `None` | your own HTTP client. The library does not close it |
| `max_connections` | `int` | `20` | the size of the connection pool |

A bot gives you its client as `bot.api`: the same connection, the same limits.

## Retries

```python
from pgpcbot import Bot, RetryPolicy

bot = Bot(retry=RetryPolicy(attempts=6, backoff_max=30.0))
```

| Field | Default | Meaning |
|---|---|---|
| `attempts` | `4` | tries in total, when the network fails or the server answers `5xx`, for calls that are safe to repeat |
| `backoff_base` | `0.5` | the first wait, in seconds; it doubles at each try |
| `backoff_max` | `20.0` | the longest wait between two tries |
| `rate_limit_retries` | `5` | how many `429` answers to wait out for one call |
| `max_rate_limit_wait` | `90.0` | a `429` that asks for a longer wait raises `RateLimitError` at once |

The wait is random between 0 and `min(backoff_max, backoff_base * 2^n)` ("full jitter"), so
many bots that fail together don't all come back at the same moment.

`RetryPolicy(attempts=1, rate_limit_retries=0)` turns retries off.

## Logging

The library logs under the `pgpcbot` logger (`pgpcbot.bot`, `pgpcbot.client`,
`pgpcbot.crypto`, `pgpcbot.engine`, `pgpcbot.webhook`). It never logs the token, passphrases,
private keys or message text.

`bot.run()` sets up basic logging at `INFO` level if your program has not set up logging
itself. To do it yourself:

```python
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("pgpcbot").setLevel(logging.DEBUG)   # more detail from the library only
bot.run(configure_logging=False)
```

At `DEBUG` level, other libraries (httpx2, for example) may log request URLs and timings.
They never see the token in a URL, because it travels in a header.

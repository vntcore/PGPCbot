# Rate limits

The server limits how fast a bot can send. A bot that goes over the limit gets `429` answers
and has to wait. The library paces itself so this rarely happens, and waits correctly when it
does.

## The server's limits

| What | Default limit |
|---|---|
| messages into one private chat | 1 per second |
| messages into one group or channel | 20 per minute |
| messages in all chats together | 30 per second |
| edits | the same numbers as messages, counted separately; the 30 per second overall is shared with sends |
| API calls of any kind | 600 per minute |
| typing indicator | 1 every 3 seconds per chat (extra calls just return `False`) |

These are the defaults. A server can use other numbers: the bot reads the real ones from
`GET /me` at start-up and follows them. You can read them too:

```python
me = await bot.get_me()
print(me.limits.sends_per_group_per_minute)        # 20
print(me.limits.get("botsPerChat"))                # limits this version does not name
```

A group can also have **slow mode**, set by its admins: then everybody, bots included, must wait
between two messages. That gives a `SlowModeError`.

## What the library does

### Before sending: the client-side limiter

The client counts its own calls with the same method as the server (a sliding window) and the
same numbers. If a call would go over a limit, it **waits** until the call can go, instead of
sending it and getting a `429`. You don't see anything, except that a burst of sends spreads out
over time.

```python
for user_id in subscribers:              # 200 private chats
    await bot.send_message(user_id, "News!")
# 30 messages per second at most: about 7 seconds in all
```

### After a `429`: waiting

If a `429` comes anyway (another process using the same bot, clocks out of sync), the client
waits as long as the server says (`Retry-After`) and tries again, up to 5 times for one call.

It gives up at once and raises the error when the server asks for a wait longer than
`max_rate_limit_wait` (90 seconds), so a handler is not stuck for minutes. You can change both
numbers:

```python
from pgpcbot import Bot, RetryPolicy

bot = Bot(retry=RetryPolicy(rate_limit_retries=10, max_rate_limit_wait=300))
```

The error tells you how long to wait:

```python
try:
    await bot.send_message(group_id, "Hi")
except RateLimitError as exc:
    print(exc.code, exc.retry_after)      # "slow_mode", 25.0
```

| Code | Error | Meaning |
|---|---|---|
| `flood_wait` | `FloodWaitError` | too many messages into one chat, or overall |
| `slow_mode` | `SlowModeError` | the group's slow mode |
| `rate_limited` | `RateLimitError` | too many API calls of any kind |

## Sending to many people

- Send one message at a time, in a loop: the limiter does the pacing.
- Don't start hundreds of tasks at once to "go faster": the limit is the same, you only use
  more memory.
- A long broadcast holds up the chat whose handler runs it. Run it as a background task:

```python
@bot.command("broadcast", filters=(f.user(ADMIN_ID),))
async def broadcast(message, command):
    asyncio.create_task(send_to_everyone(command.args))
    await message.reply("Sending in the background.")
```

Keep a reference to the task (or use a `TaskGroup`) so it is not garbage-collected before it
ends.

## Several processes, one bot

The limiter counts the calls of **one** process. Two processes that use the same token each
think they have the whole budget, and together they go over it. The `429` handling still keeps
things correct, but slower. If you have to share a token, give each process a lower budget with
your own `RateLimiter`:

```python
from pgpcbot import Limits, RateLimiter

half = Limits(sends_per_second=15, api_calls_per_minute=300)
bot = Bot(rate_limit=RateLimiter(half))
```

A limiter you pass yourself keeps your numbers: the bot gives the server's numbers only to the
limiter it makes on its own. The low-level clients take one the same way:
`AsyncClient(rate_limiter=RateLimiter(half))`.

## Turning it off

```python
bot = Bot(rate_limit=False)
```

Only do this if something else in front of the bot already paces the calls.

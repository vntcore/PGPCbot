AsyncClient and Client
======================

.. currentmodule:: pgpcbot

The low-level clients have one method per operation of the Bot API. They check the input,
pace the calls, retry when it is safe, and return typed objects. They **don't encrypt**
anything: in an encrypted chat you send ``armored=`` yourself, or use :class:`Bot`, which does
it for you.

.. code-block:: python

   from pgpcbot import AsyncClient, Client

   async with AsyncClient() as api:          # token from PGPCHAT_BOT_TOKEN
       me = await api.get_me()

   with Client() as api:                     # the same, blocking
       me = api.get_me()

:class:`Client` has exactly the same methods as :class:`AsyncClient`, with the same
parameters, without ``await``. A :class:`Client` is safe to share between threads; an
:class:`AsyncClient` belongs to one event loop.

.. _common-errors:

Errors every method can raise
-----------------------------

==================================================  ===========================================================
Error                                               When
==================================================  ===========================================================
:exc:`ValidationError`                              a parameter breaks one of the server's rules. **Nothing was
                                                    sent.** ``field`` names it
:exc:`AuthenticationError` (401)                    the token is missing, revoked or invalid
:exc:`BotPausedError` (403)                         the owner paused the bot. Only ``get_me``, ``get_webhook``,
                                                    ``delete_webhook`` and ``revoke_token`` work meanwhile
:exc:`RateLimitError` (429)                         too many calls, after the client waited as much as its
                                                    :class:`RetryPolicy` allows
:exc:`ServerError` (5xx)                            the server failed, after the retries
:exc:`UnexpectedResponseError`                      the answer was not the API's JSON (a proxy error page, a cut
                                                    answer)
:exc:`NetworkError`, :exc:`RequestTimeoutError`     no answer at all, after the retries
:exc:`PayloadError`                                 the answer is missing something this version needs
==================================================  ===========================================================

How retries work
----------------

- **Safe to repeat** (all reads, ``set_webhook``, ``delete_webhook``, ``set_key``, the command
  calls, ``send_message`` and ``edit_message``, ``delete_message``, ``answer_callback``,
  ``leave_chat``, ``revoke_token``): retried after network errors and ``5xx``, up to
  ``retry.attempts`` tries in all.
- **Not repeated** after a network error: ``get_updates`` (the polling loop decides) and
  ``send_typing`` (a late indicator is useless).
- ``429`` is always waited out, up to ``rate_limit_retries`` times, each wait at most
  ``max_rate_limit_wait`` seconds.
- Other ``4xx`` answers are never retried.

``send_message`` is safe to repeat because of its ``sendId``; ``edit_message`` because the same
edit twice gives the same result. See :doc:`../guides/errors` and :doc:`../guides/rate-limiting`.

AsyncClient
-----------

.. autoclass:: AsyncClient
   :members:
   :inherited-members:
   :exclude-members: execute

Client
------

.. autoclass:: Client
   :members:
   :inherited-members:
   :exclude-members: execute

Retries and rate limits
-----------------------

.. autoclass:: RetryPolicy
   :members:

.. autoclass:: RateLimiter
   :members:

Secrets
-------

.. autoclass:: Secret
   :members:

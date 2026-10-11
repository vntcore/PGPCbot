API reference
=============

.. currentmodule:: pgpcbot

These pages are generated from the docstrings in the code. Everything public is importable
from ``pgpcbot``:

.. code-block:: python

   from pgpcbot import Bot, Button, InlineKeyboard, filters as f

Names that start with ``_`` (modules and attributes) are internal and can change in any
version. The same documentation is available without a browser: ``help(pgpcbot.Bot)`` in
Python, or ``python -m pydoc pgpcbot.Bot`` in a terminal.

Where to find what
------------------

=================================  ===================================================================
Page                               Contents
=================================  ===================================================================
:doc:`bot`                         :class:`Bot` and :class:`SyncBot`: running a bot, sending,
                                   encryption helpers, hooks, failed updates
:doc:`client`                      :class:`AsyncClient` and :class:`Client`: one method per
                                   operation of the Bot API; :class:`RetryPolicy`,
                                   :class:`RateLimiter`, :class:`Secret`
:doc:`models`                      the objects the API returns and handlers receive
:doc:`keyboard`                    :class:`Button`, :class:`InlineKeyboard`, :class:`KeyboardBuilder`
:doc:`routing`                     :class:`Router`, :class:`SkipHandler` and the ``filters`` module
:doc:`crypto`                      :class:`BotKey`, :class:`PublicKey`, :class:`Verification`
:doc:`storage`                     :class:`MemoryStorage`, :class:`SQLiteStorage`, writing your own
:doc:`webhook`                     :class:`WebhookApp`, :func:`generate_secret`
:doc:`errors`                      every exception, with the server's error codes
=================================  ===================================================================

Conventions
-----------

- **Async and sync.** Every async class has a sync twin with the same methods: on the sync
  one, call them without ``await`` (:doc:`../guides/async-and-sync`).
- **Keyword arguments.** Options are keyword-only; only the main values (the chat, the text,
  the IDs) are positional.
- **Chats.** Bot methods take a chat ID or any chat object (a :class:`Chat`, a
  :class:`ChatRef`, ``message.chat``).
- **Validation.** Input is checked against the server's rules before anything is sent: a
  :exc:`ValidationError` means nothing left the process.
- **Errors.** Each method lists the errors specific to it. Any call that reaches the server can
  also raise the errors listed in :ref:`common-errors`.
- **Models are read-only** and keep the server's JSON in ``raw``.

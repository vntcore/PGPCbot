PGPCbot
=======

PGPCbot is a Python library for writing bots for `PGP Chat <https://pgpchat.app>`_, a messenger
whose private chats are end-to-end encrypted with OpenPGP. It covers the whole Bot API, and it
handles the encryption for you.

On `PyPI <https://pypi.org/project/PGPCbot/>`_ (``pip install PGPCbot``) and on
`GitHub <https://github.com/vntcore/PGPCbot>`_, where the issues are.

.. code-block:: python

   from pgpcbot import Bot

   bot = Bot()  # reads the token from PGPCHAT_BOT_TOKEN


   @bot.command("start", description="Say hello")
   async def start(message):
       await message.reply("Hello! Send me anything and I will repeat it.")


   @bot.on_message()
   async def echo(message):
       await message.reply(f"You said: {message.text}")


   bot.run()

If the bot has a published OpenPGP key, its private chats are end-to-end encrypted. The code
above does not change: messages reach your handlers already decrypted, and replies are
encrypted for the people who should read them.

What you get
------------

- **The whole Bot API**: every operation, with typed models, as ``async`` calls
  (:class:`~pgpcbot.AsyncClient`) and as blocking calls (:class:`~pgpcbot.Client`).
- **A bot framework**: handlers for messages, commands, button presses, edits, deletions and
  membership changes; filters; routers; middleware; error handlers (:doc:`guides/events`).
- **Automatic OpenPGP**: decryption, signature checks, encryption and signing, key generation
  and rotation (:doc:`guides/openpgp`, :doc:`CRYPTOGRAPHY`).
- **Reliable delivery**: long polling and webhooks that never confirm an update before it is
  safe, and a SQLite queue that survives restarts (:doc:`guides/long-polling`,
  :doc:`guides/webhook`, :doc:`guides/storage`).
- **Safe retries and rate limits**: every send has a ``sendId``, so a retry never writes a
  message twice; the client waits on ``429`` and paces itself like the server does
  (:doc:`guides/rate-limiting`).
- **Async and sync with the same features**: :class:`~pgpcbot.Bot` and
  :class:`~pgpcbot.SyncBot`, :class:`~pgpcbot.AsyncClient` and :class:`~pgpcbot.Client`
  (:doc:`guides/async-and-sync`).

Where to start
--------------

1. :doc:`Install the library <getting-started/installation>`.
2. :doc:`Write your first bot <getting-started/quickstart>`.
3. Read the guides when you need them.
4. Before you go to production: :doc:`CRYPTOGRAPHY`, :doc:`deployment/index` and
   :doc:`security/index`.

The :doc:`API reference <api/index>` is generated from the docstrings in the code, so the
same text is also available offline: ``help(pgpcbot.Bot)`` in Python, or
``python -m pydoc pgpcbot.Bot`` in a terminal.

.. toctree::
   :maxdepth: 2
   :caption: Getting started
   :hidden:

   getting-started/installation
   getting-started/quickstart
   getting-started/configuration

.. toctree::
   :maxdepth: 2
   :caption: Guides
   :hidden:

   guides/messages
   guides/events
   guides/commands-and-buttons
   guides/long-polling
   guides/webhook
   guides/openpgp
   guides/errors
   guides/rate-limiting
   guides/async-and-sync
   guides/storage
   guides/command-line
   examples

.. toctree::
   :maxdepth: 2
   :caption: API reference
   :hidden:

   api/index
   api/bot
   api/client
   api/models
   api/keyboard
   api/routing
   api/crypto
   api/storage
   api/webhook
   api/errors

.. toctree::
   :maxdepth: 2
   :caption: In depth
   :hidden:

   CRYPTOGRAPHY
   architecture/index
   openapi-reference/index
   openapi-reference/operations
   API_COVERAGE
   deployment/index
   security/index

.. toctree::
   :maxdepth: 2
   :caption: Help and project
   :hidden:

   troubleshooting/index
   troubleshooting/faq
   project/versioning
   project/changelog
   project/contributing
   project/testing
   RESEARCH

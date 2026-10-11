Bot and SyncBot
===============

.. currentmodule:: pgpcbot

:class:`Bot` runs ``async def`` handlers; :class:`SyncBot` runs plain functions in a pool of
threads. They take the same options and have the same methods: on :class:`SyncBot`, call them
without ``await``. The decorators that register handlers (``on_message``, ``command``, ...)
come from :class:`Router`, documented in :doc:`routing`.

The guides show them at work: :doc:`../guides/messages`, :doc:`../guides/events`,
:doc:`../guides/long-polling`, :doc:`../guides/webhook`, :doc:`../guides/openpgp`.

Bot
---

.. autoclass:: Bot
   :members:
   :inherited-members: Router

SyncBot
-------

.. autoclass:: SyncBot
   :members:
   :inherited-members: Router

Routers and filters
===================

.. currentmodule:: pgpcbot

:class:`Bot` and :class:`SyncBot` are routers themselves: everything on this page works on a
bot and on a :class:`Router`. The guide is :doc:`../guides/events`.

Router
------

.. autoclass:: Router
   :members:

.. autoexception:: SkipHandler

Commands
--------

.. autoclass:: ParsedCommand
   :members:

Reading the handlers back
-------------------------

:meth:`Router.iter_handlers` and the other readers give back these records. The dispatcher uses
them; in tests they show what a router holds.

.. autoclass:: pgpcbot.router.Handler
   :members:

.. autoclass:: pgpcbot.router.ErrorHandler
   :members:

Filters
-------

.. automodule:: pgpcbot.filters
   :members:
   :member-order: bysource
   :exclude-members: ParsedCommand

.. autoclass:: pgpcbot.filters.FilterContext
   :members:

.. py:type:: FilterFunc
   :module: pgpcbot.filters
   :canonical: Callable[..., bool | dict[str, Any] | None | Awaitable[bool | dict[str, Any] | None]]

   What a :class:`~pgpcbot.filters.Filter` wraps: a function that takes the event, or the event
   and a :class:`~pgpcbot.filters.FilterContext`, and answers ``True``, ``False``, ``None`` or a
   ``dict``. An ``async`` one answers with an awaitable.

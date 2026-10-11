Storage
=======

.. currentmodule:: pgpcbot

Where updates wait between "received from the server" and "handled", plus the failed updates
and the pinned keys. The guide is :doc:`../guides/storage`.

.. autoclass:: MemoryStorage
   :members:

.. autoclass:: SQLiteStorage
   :members:

.. autoclass:: Storage
   :members:

.. autoclass:: FailedUpdate
   :members:

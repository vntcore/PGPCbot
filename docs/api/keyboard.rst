Keyboards
=========

.. currentmodule:: pgpcbot

Buttons under a message. The guide is :doc:`../guides/commands-and-buttons`.

.. warning::

   Buttons are **never encrypted**, not even in encrypted chats: their text and their
   ``callback_data`` travel in clear, because the server has to know which button was pressed.

.. autoclass:: Button
   :members:

.. autoclass:: InlineKeyboard
   :members:

.. autoclass:: KeyboardBuilder
   :members:

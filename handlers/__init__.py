"""
handlers/ — Command, callback, and admin handler modules.

Import the registration helpers from each sub-module and call them inside
bot.py to wire up all handlers to the Application instance.
"""

from handlers.commands import register_command_handlers
from handlers.admin import register_admin_handlers
from handlers.callbacks import register_callback_handlers

__all__ = [
    "register_command_handlers",
    "register_admin_handlers",
    "register_callback_handlers",
]

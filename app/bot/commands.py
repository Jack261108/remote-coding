from aiogram.types import BotCommand

BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Help and current session info"),
    BotCommand(command="run", description="Execute a task"),
    BotCommand(command="claude", description="Open Claude chat session"),
    BotCommand(command="cmds", description="Show Claude slash commands"),
    BotCommand(command="list", description="View active sessions"),
    BotCommand(command="attach", description="Connect to a session"),
    BotCommand(command="status", description="Query task status"),
    BotCommand(command="cancel", description="Cancel a task"),
    BotCommand(command="session", description="View/switch session"),
    BotCommand(command="approve", description="Approve pending permission"),
    BotCommand(command="deny", description="Deny pending permission"),
    BotCommand(command="exit", description="Exit Claude session and close terminal"),
]

assert len(BOT_COMMANDS) == 12, f"Expected 12 commands, got {len(BOT_COMMANDS)}"

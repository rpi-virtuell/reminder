# reminder - A maubot plugin to remind you about things.
# Copyright (C) 2019 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Type, Tuple
from datetime import datetime, timedelta
from html import escape
import asyncio

import pytz

from mautrix.types import (EventType, GenericEvent, RedactionEvent, StateEvent, Format, MessageType,
                           TextMessageEventContent, ReactionEvent)
from mautrix.util.config import BaseProxyConfig
from maubot import Plugin, MessageEvent
from maubot.handlers import command, event

from .db import ReminderDatabase
from .util import Config, ReminderInfo, DateArgument, parse_timezone, format_time


class ReminderBot(Plugin):
    db: ReminderDatabase
    reminder_loop_task: asyncio.Future
    base_command: str
    base_aliases: Tuple[str, ...]

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        await super().start()
        self.on_external_config_update()
        self.db = ReminderDatabase(self.database)
        self.reminder_loop_task = asyncio.ensure_future(self.reminder_loop(), loop=self.loop)

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        bc = self.config["base_command"]
        self.base_command = bc[0] if isinstance(bc, list) else bc
        self.base_aliases = tuple(bc) if isinstance(bc, list) else (bc,)

    async def stop(self) -> None:
        await super().stop()
        self.reminder_loop_task.cancel()

    async def reminder_loop(self) -> None:
        try:
            self.log.debug("Reminder loop started")
            while True:
                now = datetime.now(tz=pytz.UTC)
                next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
                await asyncio.sleep((next_minute - now).total_seconds())
                await self.schedule_nearby_reminders(next_minute)
        except asyncio.CancelledError:
            self.log.debug("Reminder loop stopped")
        except Exception:
            self.log.exception("Exception in reminder loop")

    async def schedule_nearby_reminders(self, now: datetime) -> None:
        until = now + timedelta(minutes=1)
        for reminder in self.db.all_in_range(now, until):
            asyncio.ensure_future(self.send_reminder(reminder), loop=self.loop)

    async def send_reminder(self, reminder: ReminderInfo) -> None:
        try:
            await self._send_reminder(reminder)
        except Exception:
            self.log.exception("Failed to send reminder")

    async def _send_reminder(self, reminder: ReminderInfo) -> None:
        if len(reminder.users) == 0:
            self.log.debug(f"Cancelling reminder {reminder}, no users left to remind")
            return
        wait = (reminder.date - datetime.now(tz=pytz.UTC)).total_seconds()
        if wait > 0:
            self.log.debug(f"Waiting {wait} seconds to send {reminder}")
            await asyncio.sleep(wait)
        else:
            self.log.debug(f"Sending {reminder} immediately")
        users = " ".join(reminder.users)
        users_html = " ".join(f"<a href='https://matrix.to/#/{user_id}'>{user_id}</a>"
                              for user_id in reminder.users)
        content = TextMessageEventContent(
            msgtype=MessageType.TEXT, body=f"{users}: {reminder.message}", format=Format.HTML,
            formatted_body=f"{users_html}: {escape(reminder.message)}")
        if reminder.reply_to:
            content.set_reply(await self.client.get_event(reminder.room_id, reminder.reply_to))
        await self.client.send_message(reminder.room_id, content)

    @command.new(name=lambda self: self.base_command,
                 aliases=lambda self, alias: alias in self.base_aliases,
                 help="Create a reminder", require_subcommand=False, arg_fallthrough=False)
    @DateArgument("date", required=True)
    @command.argument("message", pass_raw=True, required=False)
    async def remind(self, evt: MessageEvent, date: datetime, message: str) -> None:
        date = date.replace(microsecond=0)
        now = datetime.now(tz=pytz.UTC).replace(microsecond=0)
        if date < now:
            await evt.reply(f"Sorry, {date} is in the past and I don't have a time machine :(")
            return
        rem = ReminderInfo(date=date, room_id=evt.room_id, message=message,
                           reply_to=evt.content.get_reply_to(), users={evt.sender: evt.event_id})
        if date == now:
            await self.send_reminder(rem)
            return
        remind_type = "remind you "
        if rem.reply_to:
            evt_link = f"[event](https://matrix.to/#/{rem.room_id}/{rem.reply_to})"
            if rem.message:
                remind_type += f"to {rem.message} (replying to that {evt_link})"
            else:
                remind_type += f"about that {evt_link}"
        elif rem.message:
            remind_type += f"to {rem.message}"
        else:
            remind_type = "ping you"
            rem.message = "ping"
        msg = (f"I'll {remind_type} {self.format_time(evt, rem)}.\n\n"
               f"(others can \U0001F44D this message to get pinged too)")
        rem.event_id = await evt.reply(msg)
        self.db.insert(rem)
        now = datetime.now(tz=pytz.UTC)
        if (date - now).total_seconds() < 60 and now.minute == date.minute:
            self.log.debug(f"Reminder {rem} is in less than a minute, scheduling now...")
            asyncio.ensure_future(self.send_reminder(rem), loop=self.loop)

    @remind.subcommand("help", help="Usage instructions")
    async def help(self, evt: MessageEvent) -> None:
        await evt.reply(f"Maubot [Reminder](https://github.com/maubot/reminder) plugin.\n\n"
                        f"* !{self.base_command} <date> <message> - Add a reminder\n"
                        f"* !{self.base_command} list - Get a list of your reminders\n"
                        f"* !{self.base_command} tz <timezone> - Set your time zone\n\n"
                        "<date> can be a real date in any sensible format or a time delta such as "
                        "2 hours and 5 minutes\n\n"
                        "To get mentioned by a reminder added by someone else, upvote the message "
                        "by reacting with \U0001F44D.\n\n"
                        "To cancel a reminder, remove the message or reaction.")

    @remind.subcommand("list", help="List your reminders")
    @command.argument("all", required=False)
    async def list(self, evt: MessageEvent, all: str) -> None:
        room_id = evt.room_id
        if "all" in all:
            room_id = None

        def format_rem(rem: ReminderInfo) -> str:
            if rem.reply_to:
                evt_link = f"[event](https://matrix.to/#/{rem.room_id}/{rem.reply_to})"
                if rem.message:
                    return f'"{rem.message}" (replying to {evt_link})'
                else:
                    return evt_link
            else:
                return f'"{rem.message}"'

        reminders_str = "\n".join(f"* {format_rem(reminder)} {self.format_time(evt, reminder)}"
                                  for reminder in self.db.all_for_user(evt.sender, room_id=room_id))
        message = "upcoming reminders"
        if room_id:
            message += " in this room"
        if len(reminders_str) == 0:
            await evt.reply(f"You have no {message} :(")
        else:
            await evt.reply(f"Your {message}:\n\n{reminders_str}")

    def format_time(self, evt: MessageEvent, reminder: ReminderInfo) -> str:
        return format_time(reminder.date.astimezone(self.db.get_timezone(evt.sender)))

    @remind.subcommand("cancel", help="Cancel a reminder", aliases=("delete", "remove", "rm"))
    @command.argument("id", parser=lambda val: int(val) if val else None, required=True)
    async def cancel(self, evt: MessageEvent, id: int) -> None:
        reminder = self.db.get(id)
        if self.db.remove_user(reminder, evt.sender):
            await evt.reply(f"Reminder for \"{reminder.message}\""
                            f" {self.format_time(evt, reminder)} **cancelled**")
        else:
            await evt.reply("You weren't subscribed to that reminder.")

    @remind.subcommand("timezone", help="Set your timezone", aliases=("tz",))
    @command.argument("timezone", parser=parse_timezone, required=False)
    async def timezone(self, evt: MessageEvent, timezone: pytz.timezone) -> None:
        if not timezone:
            await evt.reply(f"Your time zone is {self.db.get_timezone(evt.sender).zone}")
            return
        self.db.set_timezone(evt.sender, timezone)
        await evt.reply(f"Set your timezone to {timezone.zone}")

    @command.passive(regex=r"(?:\U0001F44D[\U0001F3FB-\U0001F3FF]?)",
                     field=lambda evt: evt.content.relates_to.key,
                     event_type=EventType.REACTION, msgtypes=None)
    async def subscribe_react(self, evt: ReactionEvent, _: Tuple[str]) -> None:
        reminder = self.db.get_by_event_id(evt.content.relates_to.event_id)
        if reminder:
            self.db.add_user(reminder, evt.sender, evt.event_id)

    @event.on(EventType.ROOM_REDACTION)
    async def redact(self, evt: RedactionEvent) -> None:
        self.db.redact_event(evt.redacts)

    @event.on(EventType.ROOM_TOMBSTONE)
    async def tombstone(self, evt: StateEvent) -> None:
        self.db.update_room_id(evt.room_id, evt.content.replacement_room)
        _, server = self.client.parse_user_id(evt.sender)
        await self.client.join_room(evt.content.replacement_room, servers=[server])

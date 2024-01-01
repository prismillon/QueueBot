import discord
from discord.ext import commands, tasks
from discord import app_commands
from dateutil.parser import parse
from datetime import datetime, timezone, timedelta
import time
import json
from mmr import mk8dx_150cc_mmr, get_mmr_from_discord_id, mk8dx_150cc_fc
from mogi_objects import Mogi, Team, Player, Room, VoteView, JoinView, get_tier
import asyncio

# Scheduled_Event = collections.namedtuple('Scheduled_Event', 'size time started mogi_channel')


class SquadQueue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # keys are discord.Guild objects, values are list of Mogi instances
        self.scheduled_events = {}
        # keys are discord.TextChannel objects, values are instances of Mogi
        self.ongoing_events = {}

        self.old_events = {}

        self._que_scheduler = self.que_scheduler.start()
        self._scheduler_task = self.sqscheduler.start()
        self._msgqueue_task = self.send_queued_messages.start()
        self._end_mogis_task = self.delete_old_mogis.start()

        self.msg_queue = {}

        self.QUEUE_TIME_BLOCKER = datetime.now(timezone.utc)

        self.GUILD = None

        self.MOGI_CHANNEL = None

        self.SUB_CHANNEL = None

        self.LOCK = asyncio.Lock()

        self.URL = bot.config["url"]

        self.MOGI_LIFETIME = bot.config["MOGI_LIFETIME"]

        self.SUB_MESSAGE_LIFETIME_SECONDS = bot.config["SUB_MESSAGE_LIFETIME_SECONDS"]

        # number of minutes before scheduled time that queue should open
        self.QUEUE_OPEN_TIME = timedelta(minutes=bot.config["QUEUE_OPEN_TIME"])

        # number of minutes after QUEUE_OPEN_TIME that teams can join the mogi
        self.JOINING_TIME = timedelta(minutes=bot.config["JOINING_TIME"])

        # number of minutes after JOINING_TIME for any potential extra teams to join
        self.EXTENSION_TIME = timedelta(minutes=bot.config["EXTENSION_TIME"])

        with open('./timezones.json', 'r') as cjson:
            self.timezones = json.load(cjson)

    @commands.Cog.listener()
    async def on_ready(self):
        self.GUILD = self.bot.get_guild(self.bot.config["guild_id"])
        self.MOGI_CHANNEL = self.bot.get_channel(
            self.bot.config["queue_join_channel"])
        self.SUB_CHANNEL = self.bot.get_channel(
            self.bot.config["queue_sub_channel"])
        print(f"Server - {self.GUILD}", flush=True)
        print(f"Join Channel - {self.MOGI_CHANNEL}", flush=True)
        print(f"Sub Channel - {self.SUB_CHANNEL}", flush=True)
        print("Ready!", flush=True)

    async def lockdown(self, channel: discord.TextChannel):
        # everyone_perms = channel.permissions_for(channel.guild.default_role)
        # if not everyone_perms.send_messages:
        #     return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
        await channel.send("Locked down " + channel.mention)

    async def unlockdown(self, channel: discord.TextChannel):
        # everyone_perms = channel.permissions_for(channel.guild.default_role)
        # if everyone_perms.send_messages:
        #     return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
        await channel.send("Unlocked " + channel.mention)

    # either adds a message to the message queue or sends it, depending on
    # server settings
    async def queue_or_send(self, ctx, msg, delay=0):
        if ctx.bot.config["queue_messages"] is True:
            if ctx.channel not in self.msg_queue.keys():
                self.msg_queue[ctx.channel] = []
            self.msg_queue[ctx.channel].append(msg)
        else:
            sendmsg = await ctx.send(msg)
            if delay > 0:
                await sendmsg.delete(delay=delay)

    # goes thru the msg queue for each channel and combines them
    # into as few messsages as possible, then sends them
    @tasks.loop(seconds=2)
    async def send_queued_messages(self):
        try:
            for channel in self.msg_queue.keys():
                channel_queue = self.msg_queue[channel]
                sentmsgs = []
                msg = ""
                for i in range(len(channel_queue)-1, -1, -1):
                    msg = channel_queue.pop(i) + "\n" + msg
                    if len(msg) > 1500:
                        sentmsgs.append(msg)
                        msg = ""
                if len(msg) > 0:
                    sentmsgs.append(msg)
                for i in range(len(sentmsgs)-1, -1, -1):
                    await channel.send(sentmsgs[i])
        except Exception as e:
            print(e)

    def get_mogi(self, ctx):
        if ctx.channel in self.ongoing_events.keys():
            return self.ongoing_events[ctx.channel]
        return None

    async def is_started(self, ctx, mogi):
        if not mogi.started:
            await ctx.send("Mogi has not been started yet... type !start")
            return False
        return True

    async def is_gathering(self, ctx, mogi):
        if not mogi.gathering:
            await ctx.send("Mogi is closed; players cannot join or drop from the event")
            return False
        return True

    @app_commands.command(name="c")
    @app_commands.guild_only()
    async def can(self, interaction: discord.Interaction):
        """Join a mogi"""
        await interaction.response.defer()
        async with self.LOCK:
            member = interaction.user
            mogi = self.get_mogi(interaction)
            if mogi is None or not mogi.started or not mogi.gathering:
                await interaction.followup.send("Queue has not started yet.")
                return

            player_team = mogi.check_player(member)

            if player_team is not None:
                await interaction.followup.send(f"{interaction.user.mention} is already signed up.")
                return

            players = await mk8dx_150cc_mmr(self.URL, [member])

            if players[0] is None:
                msg = f"{interaction.user.mention} MMR for the following player could not be found: "
                msg += ", ".join(interaction.user.name)
                msg += ". Please contact a staff member for help"
                await interaction.followup.send(msg)
                return

            msg = ""
            if players[0].mmr is None:
                players[0].mmr = 0
                msg += f"{players[0].lounge_name} is assumed to be a new player and will be playing this mogi with a starting MMR of 0.  "
                msg += "If you believe this is a mistake, please contact a staff member for help.\n"

            players[0].confirmed = True
            squad = Team(players)
            mogi.teams.append(squad)

            msg += f"{players[0].lounge_name} joined queue for mogi {discord.utils.format_dt(mogi.start_time, style='R')}, `[{mogi.count_registered()} players]`"

            await interaction.followup.send(msg)
            await self.check_room_channels(mogi)
            await self.check_num_teams(mogi)

    @app_commands.command(name="d")
    @app_commands.guild_only()
    async def drop(self, interaction: discord.Interaction):
        """Remove user from mogi"""
        await interaction.response.defer()
        async with self.LOCK:
            mogi = self.get_mogi(interaction)
            if mogi is None or not mogi.started or not mogi.gathering:
                await interaction.followup.send("Queue has not started yet.")
                return

            member = interaction.user
            squad = mogi.check_player(member)
            if squad is None:
                await interaction.followup.send(f"{member.display_name} is not currently in this event; type `/c` to join")
                return
            mogi.teams.remove(squad)
            msg = "Removed "
            msg += ", ".join([p.lounge_name for p in squad.players])
            msg += f" from the mogi {discord.utils.format_dt(mogi.start_time, style='R')}"
            msg += f", `[{mogi.count_registered()} players]`"

            await interaction.followup.send(msg)

    @app_commands.command(name="sub")
    @app_commands.guild_only()
    async def sub(self, interaction: discord.Interaction):
        """Sends out a request for a sub in the sub channel. Only works in thread channels for SQ rooms."""
        is_room_thread = False
        room = None
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(interaction.channel_id):
                room = mogi.get_room_from_thread(interaction.channel_id)
                is_room_thread = True
                break
        for mogi in self.old_events.values():
            if mogi.is_room_thread(interaction.channel.id):
                room = mogi.get_room_from_thread(interaction.channel.id)
                is_room_thread = True
                break
        if not is_room_thread:
            await interaction.response.send_message(f"More than {self.MOGI_LIFETIME} minutes have passed since mogi start, the Mogi Object has been deleted.", ephemeral=True)
            return
        msg = ""
        if room.room_num == 1:
            msg += f"Room {room.room_num} is looking for a sub with mmr >{room.mmr_low - 500}\n"
        else:
            low = 0 if room.mmr_low < 500 else room.mmr_low - 500
            msg += f"Room {room.room_num} is looking for a sub with range {low}-{room.mmr_high + 500}\n"
        message_delete_date = datetime.now(
            timezone.utc) + timedelta(seconds=self.SUB_MESSAGE_LIFETIME_SECONDS)
        msg += f"Message will auto-delete in {discord.utils.format_dt(message_delete_date, style='R')}"
        await self.SUB_CHANNEL.send(msg, delete_after=self.SUB_MESSAGE_LIFETIME_SECONDS)
        view = JoinView(room, get_mmr_from_discord_id)
        await self.SUB_CHANNEL.send(view=view, delete_after=self.SUB_MESSAGE_LIFETIME_SECONDS)
        await interaction.response.send_message("Sent out request for sub.")

    @app_commands.command(name="l")
    @app_commands.checks.cooldown(1, 120, key=lambda i: (i.channel.id))
    @app_commands.guild_only()
    async def list(self, interaction: discord.Interaction):
        """Display the list of confirmed players for a mogi"""
        mogi = self.get_mogi(interaction)
        if mogi is None:
            await interaction.response.send_message("Queue has not started yet.")
            return
        if not await self.is_started(interaction, mogi):
            return
        mogi_list = mogi.confirmed_list()
        if len(mogi_list) == 0:
            await interaction.response.send_message(f"There are no players in the queue - type `/c` to join")
            return

        sorted_mogi_list = sorted(mogi_list, reverse=True)
        msg = "Current Mogi List:\n"
        for i in range(len(sorted_mogi_list)):
            msg += f"{i+1}) "
            msg += ", ".join([p.lounge_name for p in sorted_mogi_list[i].players])
            msg += f" ({sorted_mogi_list[i].avg_mmr:.1f} MMR)\n"
        if (len(sorted_mogi_list) % (12/mogi.size) != 0):
            num_next = int(len(sorted_mogi_list) % (12/mogi.size))
            teams_per_room = int(12/mogi.size)
            num_rooms = int(len(sorted_mogi_list) / (12/mogi.size))+1
            msg += f"[{num_next}/{teams_per_room}] players for {num_rooms} room(s)"
        message = msg.split("\n")
        bulk_msg = ""
        for i in range(len(message)):
            if len(bulk_msg + message[i] + "\n") > 2000:
                await interaction.channel.send(bulk_msg) if interaction.response.is_done() else await interaction.response.send_message(bulk_msg)
                bulk_msg = ""
            bulk_msg += message[i] + "\n"
        if len(bulk_msg) > 0:
            await interaction.channel.send(bulk_msg) if interaction.response.is_done() else await interaction.response.send_message(bulk_msg)

    @list.error  # Tell the user when they've got a cooldown
    async def on_list_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message("Wait before using `/l` command", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not (message.content.isdecimal() and 12 <= int(message.content) <= 180):
            return
        mogi = discord.utils.find(lambda mogi: mogi.is_room_thread(
            message.channel.id), self.ongoing_events.values())
        if not mogi:
            return
        room = discord.utils.find(
            lambda room: room.thread.id == message.channel.id, mogi.rooms)
        if not room or not room.teams:
            return
        team = discord.utils.find(
            lambda team: team.has_player(message.author), room.teams)
        if not team:
            return
        player = discord.utils.find(
            lambda player: player.member.id == message.author.id, team.players)
        if player:
            player.score = int(message.content)

    @app_commands.command(name="scoreboard")
    @app_commands.guild_only()
    async def scoreboard(self, interaction: discord.Interaction):
        """Displays the scoreboard of the room. Only works in thread channels for SQ rooms."""

        mogi = discord.utils.find(lambda mogi: mogi.is_room_thread(
            interaction.channel_id), self.ongoing_events.values())
        if not mogi:
            await interaction.response.send_message(f"More than {self.MOGI_LIFETIME} minutes have passed since mogi start, the Mogi Object has been deleted.", ephemeral=True)
            return

        room = discord.utils.find(
            lambda room: room.thread.id == interaction.channel_id, mogi.rooms)

        if not room:
            await interaction.response.send_message(f"More than {self.MOGI_LIFETIME} minutes have passed since mogi start, the Mogi Object has been deleted.", ephemeral=True)
            return

        room_mmr = round((room.mmr_high + room.mmr_low) / 2 - 500)
        msg = f"!submit {round(12/len(room.teams))} {get_tier(room_mmr)}\n"
        for team in room.teams:
            for player in team.players:
                msg += f"{player.lounge_name} {player.score}\n"
        await interaction.response.send_message(msg)

    @app_commands.command(name="remove_player")
    @app_commands.guild_only()
    async def remove_player(self, interaction: discord.Interaction, member: discord.Member):
        """Removes a specific player from the current queue.  Staff use only."""
        await interaction.response.defer()
        async with self.LOCK:
            mogi = self.get_mogi(interaction)
            if mogi is None or not mogi.started or not mogi.gathering:
                await interaction.followup.send("Queue has not started yet.")
                return

            squad = mogi.check_player(member)
            if squad is None:
                await interaction.followup.send(f"{member.display_name} is not currently in this event; type `/c` to join")
                return
            mogi.teams.remove(squad)
            msg = "Staff has removed "
            msg += ", ".join([p.lounge_name for p in squad.players])
            msg += f" from the mogi {discord.utils.format_dt(mogi.start_time, style='R')}"
            msg += f", `[{mogi.count_registered()} players]`"

            await interaction.followup.send(msg)

    @app_commands.command(name="annul_current_mogi")
    @app_commands.guild_only()
    async def annul_current_mogi(self, interaction: discord.Interaction):
        """The mogi currently gathering will be deleted.  The queue resumes at the next hour.  Staff use only."""
        self.scheduled_events = {}
        self.ongoing_events = {}
        curr_time = datetime.now(timezone.utc)
        truncated_time = curr_time.replace(
            minute=0, second=0, microsecond=0)
        self.QUEUE_TIME_BLOCKER = truncated_time + timedelta(hours=1)
        await self.lockdown(self.MOGI_CHANNEL)
        await interaction.response.send_message("The current Mogi has been canceled, the queue will resume at the next hour.")

    @app_commands.command(name="pause_mogi_scheduling")
    @app_commands.guild_only()
    async def pause_mogi_scheduling(self, interaction: discord.Interaction):
        """The mogi that is currently gathering will continue to work.  Future mogis cannot be scheduled.  Staff use only."""
        curr_time = datetime.now(timezone.utc)
        self.QUEUE_TIME_BLOCKER = curr_time + timedelta(weeks=52)
        await interaction.response.send_message("Future Mogis will not be started.")

    @app_commands.command(name="resume_mogi_scheduling")
    @app_commands.guild_only()
    async def resume_mogi_scheduling(self, interaction: discord.Interaction):
        """Mogis will begin to be scheduled again.  Staff use only."""
        curr_time = datetime.now(timezone.utc)
        self.QUEUE_TIME_BLOCKER = curr_time
        await interaction.response.send_message("Mogis will resume scheduling.")

    @app_commands.command(name="reset_bot")
    @app_commands.guild_only()
    async def reset_bot(self, interaction: discord.Interaction):
        """Resets the bot.  Staff use only."""
        self.scheduled_events = {}
        self.ongoing_events = {}
        self.old_events = {}
        curr_time = datetime.now(timezone.utc)
        self.QUEUE_TIME_BLOCKER = curr_time
        await interaction.response.send_message("All events have been deleted.  Queue will restart shortly.")

    async def start_input_validation(self, ctx, size: int, sq_id: int):
        valid_sizes = [1, 2, 3, 4, 6]
        if size not in valid_sizes:
            await (await ctx.send(f"The size you entered is invalid; proper values are: {', '.join(valid_sizes)}")).delete(delay=5)
            return False
        return True

    # check if user has roles defined in config.json
    async def has_roles(self, member: discord.Member, guild_id: int, config):
        if str(guild_id) not in config["admin_roles"].keys():
            return True
        for role in member.roles:
            if role.name in config["admin_roles"][str(guild_id)]:
                return True
        return False

    @commands.command()
    @commands.guild_only()
    async def start(self, ctx, size: int, sq_id: int):
        """Start a mogi in the current channel"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        if not await self.start_input_validation(ctx, size, sq_id):
            return
        if ctx.channel in self.ongoing_events.keys():
            await ctx.send("There is already a mogi happening in this channel, so you can't use this command")
            return
        m = Mogi(sq_id, size, ctx.channel)
        m.started = True
        m.gathering = True
        self.ongoing_events[ctx.channel] = m
        await ctx.send(f"A mogi has been started - @here Type `/c`, `/d`, or `/list`")

    @commands.command()
    @commands.guild_only()
    async def close(self, ctx):
        """Close the mogi so players can't join or drop"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return
        mogi.gathering = False
        mogi.is_automated = False
        await self.lockdown(ctx.channel)
        await ctx.send("Mogi is now closed; players can no longer join or drop from the event")

    async def endMogi(self, mogi_channel):
        # lifetime of a mogi in seconds
        await asyncio.sleep(5400)
        # print(self.scheduled_events, flush=True)
        # print(self.ongoing_events, flush=True)
        # print(self.old_events, flush=True)
        # print(datetime.now(), flush=True)
        mogi = self.old_events[mogi_channel]
        for room in mogi.rooms:
            if room.thread is None:
                return
            if not room.thread.archived:
                try:
                    await room.thread.edit(archived=True, locked=True)
                except Exception as e:
                    pass
            elif not room.thread.locked:
                try:
                    await room.thread.edit(locked=True)
                except Exception as e:
                    pass
        del self.old_events[mogi_channel]

    @commands.command()
    @commands.guild_only()
    async def end(self, ctx):
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        # for room in mogi.rooms:
        #    if room.thread is not None:
        #        await room.thread.edit(archived=True, locked=True)
        # del self.ongoing_events[ctx.channel]
        await self.endMogi(mogi.mogi_channel)
        await ctx.send(f"{ctx.author.display_name} has ended the mogi")

    @commands.command()
    @commands.guild_only()
    async def open(self, ctx):
        """Close the mogi so players can't join or drop"""
        if not await self.has_roles(ctx.author, ctx.guild.id, ctx.bot.config):
            return
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if not await self.is_started(ctx, mogi):
            return
        if mogi.gathering:
            await (await ctx.send("Mogi is already open; players can join and drop from the event")
                   ).delete(delay=5)
            return
        mogi.gathering = True
        mogi.is_automated = False
        await self.unlockdown(ctx.channel)
        await ctx.send("Mogi is now open; players can join and drop from the event")

    # command to add staff to room thread channels; users can't add new users to private threads,
    # so the bot has to with this command
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def staff(self, ctx):
        """Calls staff to the current channel. Only works in thread channels for SQ rooms."""
        is_room_thread = False
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        for mogi in self.old_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        if not is_room_thread:
            return
        if str(ctx.guild.id) not in ctx.bot.config["staff_roles"].keys():
            await ctx.send("There is no Lounge Staff role configured for this server")
            return
        lounge_staff_roles = ctx.bot.config["staff_roles"][str(ctx.guild.id)]
        mentions = " ".join(
            [ctx.guild.get_role(role).mention for role in lounge_staff_roles])
        await ctx.send(mentions)

    # @commands.command()
    async def fc(self, ctx, *, name=None):
        """Displays the FC for the given player. Only works in thread channels for SQ rooms."""
        is_room_thread = False
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        if not is_room_thread:
            return
        if name is None:
            name = ctx.author.display_name
        player_fc = await mk8dx_150cc_fc(self.bot.config, name)
        if player_fc is not None:
            await ctx.send(player_fc)
        else:
            await ctx.send("Player not found!")

    @commands.command()
    async def lt(self, ctx):
        is_room_thread = False
        for mogi in self.ongoing_events.values():
            if mogi.is_room_thread(ctx.channel.id):
                is_room_thread = True
                break
        if not is_room_thread:
            return
        await ctx.send("Stats bot cannot read messages in threads, so this command will not work. Please use `/scoreboard` to make the table.")

    async def end_voting(self):
        """Ends voting in all rooms with ongoing votes."""
        for mogi in self.ongoing_events.values():
            for room in mogi.rooms:
                await room.view.find_winner()

    # make thread channels while the event is gathering instead of at the end,
    # since discord only allows 50 thread channels to be created per 5 minutes.
    async def check_room_channels(self, mogi):
        num_teams = mogi.count_registered()
        num_rooms = int(num_teams / (12/mogi.size))
        num_created_rooms = len(mogi.rooms)
        if num_created_rooms >= num_rooms:
            return
        for i in range(num_created_rooms, num_rooms):
            room_name = f"Mogi Room {i+1}"
            try:
                room_channel = await mogi.mogi_channel.create_thread(name=room_name,
                                                                     auto_archive_duration=60,
                                                                     invitable=False)
            except Exception as e:
                print(e)
                err_msg = f"\nAn error has occurred while creating a room channel:\n{e}"
                await mogi.mogi_channel.send(err_msg)
                return
            mogi.rooms.append(Room(None, i+1, room_channel))

    # add teams to the room threads that we have already created
    async def add_teams_to_rooms(self, mogi, open_time: int, started_automatically=False):
        if open_time >= 60 or open_time < 0:
            await mogi.mogi_channel.send("Please specify a valid time (in minutes) for rooms to open (00-59)")
            return
        if mogi.making_rooms_run and started_automatically:
            return
        num_rooms = int(mogi.count_registered() / (12/mogi.size))
        if num_rooms == 0:
            await mogi.mogi_channel.send(f"Not enough players to fill a single room! This mogi will be cancelled.")
            self.scheduled_events = {}
            self.ongoing_events = {}
            return
        await self.lockdown(mogi.mogi_channel)
        mogi.making_rooms_run = True
        if mogi.gathering:
            mogi.gathering = False
            await mogi.mogi_channel.send("Mogi is now closed; players can no longer join or drop from the event")

        pen_time = open_time + 5
        start_time = open_time + 10
        while pen_time >= 60:
            pen_time -= 60
        while start_time >= 60:
            start_time -= 60
        teams_per_room = int(12/mogi.size)
        num_teams = int(num_rooms * teams_per_room)
        final_list = mogi.confirmed_list()[0:num_teams]
        sorted_list = sorted(final_list, reverse=True)

        extra_members = []
        if str(mogi.mogi_channel.guild.id) in self.bot.config["members_for_channels"].keys():
            extra_members_ids = self.bot.config["members_for_channels"][str(
                mogi.mogi_channel.guild.id)]
            for m in extra_members_ids:
                extra_members.append(mogi.mogi_channel.guild.get_member(m))

        rooms = mogi.rooms
        for i in range(num_rooms):
            msg = f"`Room {i+1} - Player List`\n"
            mentions = ""
            start_index = int(i*teams_per_room)
            player_list = []
            for j in range(teams_per_room):
                msg += f"`{j+1}.` "
                team = sorted_list[start_index+j]
                player_list.append(
                    sorted_list[start_index+j].get_first_player())
                msg += ", ".join([p.lounge_name for p in team.players])
                msg += f" ({int(team.avg_mmr)} MMR)\n"
                mentions += " ".join([p.member.mention for p in team.players])
                mentions += " "
            room_msg = msg
            mentions += " ".join([m.mention for m in extra_members if m is not None])
            # room_msg += ("\nDecide a host amongst yourselves; room open at :%02d, penalty at :%02d, start by :%02d. Good luck!\n\n"
            #              % (open_time, pen_time, start_time))
            room_msg += "\nVote for format FFA, 2v2, 3v3, or 4v4.\n"
            room_msg += "\nIf you need staff's assistance, use the `!staff` command in this channel.\n"
            room_msg += mentions
            try:
                curr_room = rooms[i]
                room_channel = curr_room.thread
                curr_room.teams = sorted_list[start_index:start_index+teams_per_room]
                await room_channel.send(room_msg)
                view = VoteView(player_list, room_channel, mogi)
                curr_room.view = view
                curr_room.mmr_low = player_list[11].mmr
                curr_room.mmr_high = player_list[0].mmr
                await room_channel.send(view=view)
            except Exception as e:
                print(e)
                err_msg = f"\nAn error has occurred while creating the room channel; please contact your opponents in DM or another channel\n"
                err_msg += mentions
                msg += err_msg
                room_channel = None
            await mogi.mogi_channel.send(msg)
        if num_teams < mogi.count_registered():
            missed_teams = mogi.confirmed_list(
            )[num_teams:mogi.count_registered()]
            msg = "`Late players:`\n"
            for i in range(len(missed_teams)):
                msg += f"`{i+1}.` "
                msg += ", ".join([p.lounge_name for p in missed_teams[i].players])
                msg += f" ({int(missed_teams[i].avg_mmr)} MMR)\n"
            await mogi.mogi_channel.send(msg)
        await asyncio.sleep(120)
        await self.end_voting()

    async def check_num_teams(self, mogi):
        if not mogi.gathering or not mogi.is_automated:
            return
        cur_time = datetime.now(timezone.utc)
        if mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME <= cur_time:
            numLeftoverTeams = mogi.count_registered() % int((12/mogi.size))
            if numLeftoverTeams == 0:
                mogi.gathering = False
                await self.lockdown(mogi.mogi_channel)
                await mogi.mogi_channel.send("A sufficient amount of players has been reached, so the mogi has been closed to extra players. Rooms will be made within the next minute.")

    async def ongoing_mogi_checks(self):
        for mogi in self.ongoing_events.values():
            # If it's not automated, not started, we've already started making the rooms, don't run this
            if not mogi.is_automated or not mogi.started or mogi.making_rooms_run:
                return
            cur_time = datetime.now(timezone.utc)
            if (mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME + self.EXTENSION_TIME) <= cur_time:
                await self.add_teams_to_rooms(mogi, (mogi.start_time.minute) % 60, True)
                return
            if mogi.start_time - self.QUEUE_OPEN_TIME + self.JOINING_TIME <= cur_time:
                # check if there are an even amount of teams since we are past the queue time
                numLeftoverTeams = mogi.count_registered() % int((12/mogi.size))
                if numLeftoverTeams == 0:
                    await self.add_teams_to_rooms(mogi, (mogi.start_time.minute) % 60, True)
                    return
                else:
                    if int(cur_time.second / 20) == 0:
                        force_time = mogi.start_time - self.QUEUE_OPEN_TIME + \
                            self.JOINING_TIME + self.EXTENSION_TIME
                        minutes_left = int((force_time - cur_time).seconds/60)
                        x_teams = int(int(12/mogi.size) - numLeftoverTeams)
                        await mogi.mogi_channel.send(f"Need {x_teams} more player(s) to start immediately. Starting in {minutes_left + 1} minute(s) regardless.")

    async def scheduler_mogi_start(self):
        cur_time = datetime.now(timezone.utc)
        # print(self.scheduled_events, flush=True)
        # print(self.ongoing_events, flush=True)
        # print(self.old_events, flush=True)
        for guild in self.scheduled_events.values():
            to_remove = []  # Keep a list of indexes to remove - can't remove while iterating
            for i, mogi in enumerate(guild):
                if (mogi.start_time - self.QUEUE_OPEN_TIME) < cur_time:
                    if mogi.mogi_channel in self.ongoing_events.keys() and self.ongoing_events[mogi.mogi_channel].gathering:
                        to_remove.append(i)
                        await mogi.mogi_channel.send(f"Because there is an ongoing event right now, the following event has been removed:\n{self.get_event_str(mogi)}\n")
                    else:
                        if mogi.mogi_channel in self.ongoing_events.keys():
                            if self.ongoing_events[mogi.mogi_channel].started:
                                self.old_events[mogi.mogi_channel] = self.ongoing_events[mogi.mogi_channel]
                                del self.ongoing_events[mogi.mogi_channel]
                                # await self.endMogi(mogi.mogi_channel)
                                # print(self.scheduled_events, flush=True)
                                # print(self.ongoing_events, flush=True)
                                # print(self.old_events, flush=True)
                                # print(datetime.now(timezone.utc), flush=True)
                                # asyncio.create_task(
                                #     self.endMogi(mogi.mogi_channel))
                        to_remove.append(i)
                        self.ongoing_events[mogi.mogi_channel] = mogi
                        mogi.started = True
                        mogi.gathering = True
                        await self.unlockdown(mogi.mogi_channel)
                        await mogi.mogi_channel.send(f"A queue is gathering for the mogi {discord.utils.format_dt(mogi.start_time, style='R')} - @here Type `/c`, `/d`, or `/l`")
            for ind in reversed(to_remove):
                del guild[ind]

    @tasks.loop(seconds=20.0)
    async def sqscheduler(self):
        """Scheduler that checks if it should start mogis and close them"""
        # It may seem silly to do try/except Exception, but this coroutine **cannot** fail
        # This coroutine *silently* fails and stops if exceptions aren't caught - an annoying abtraction of asyncio
        # This is unacceptable considering people are relying on these mogis to run, so we will not allow this routine to stop
        try:
            await self.scheduler_mogi_start()
        except Exception as e:
            print(e)
        try:
            await self.ongoing_mogi_checks()
        except Exception as e:
            print(e)

    @tasks.loop(minutes=1)
    async def que_scheduler(self):
        # print("Starting que scheduler.", flush=True)
        try:
            if not self.scheduled_events or len(self.scheduled_events[self.GUILD]) == 0:
                await self.schedule_que_event()
            # else:
                # print("Event already scheduled", flush=True)
        except Exception as e:
            print(e)

    async def schedule_que_event(self):
        """Schedules queue for the next hour in the given channel."""

        if self.GUILD is not None:
            curr_time = datetime.now(timezone.utc)
            if curr_time < self.QUEUE_TIME_BLOCKER:
                # print(f"Mogi had been blocked from starting before the time limit {self.QUEUE_TIME_BLOCKER}", flush=True)
                return
            if datetime.now().minute >= self.bot.config["JOINING_TIME"]:
                # print("Hourly Que is too late, starting Que at next hour", flush=True)
                return
            truncated_time = curr_time.replace(
                minute=0, second=0, microsecond=0)
            next_hour = truncated_time + timedelta(hours=1)
            for mogi in self.ongoing_events.values():
                if mogi.start_time == next_hour:
                    return
            event_start_time = next_hour.astimezone() - self.QUEUE_OPEN_TIME
            if event_start_time < discord.utils.utcnow():
                event_start_time = discord.utils.utcnow() + timedelta(minutes=1)

            mogi = Mogi(1, 1, self.MOGI_CHANNEL, is_automated=True,
                        start_time=next_hour)

            if self.GUILD not in self.scheduled_events.keys():
                self.scheduled_events[self.GUILD] = []

            self.scheduled_events[self.GUILD].append(mogi)

            print(f"Started Queue for {next_hour}", flush=True)

    @tasks.loop(minutes=1)
    async def delete_old_mogis(self):
        """Deletes old mogi objects"""
        curr_time = datetime.now(timezone.utc)
        mogi_lifetime = timedelta(minutes=self.MOGI_LIFETIME)
        for mogi in self.old_events.values():
            if curr_time - mogi_lifetime > mogi.start_time:
                print(
                    f"Deleting {mogi.start_time} Mogi at {curr_time}", flush=True)
                del self.old_events[mogi.mogi_channel]

    def getTime(self, schedule_time: str, timezone: str):
        """Returns a DateTime object representing the UTC equivalent of the given time."""
        if schedule_time.isnumeric():
            schedule_time += ":00"
        utc_offset = time.altzone if time.localtime().tm_isdst > 0 else time.timezone
        time_adjustment = timedelta(seconds=utc_offset)
        timezone_adjustment = timedelta(hours=0)
        if timezone.upper() in self.timezones.keys():
            timezone_adjustment = timedelta(
                hours=self.timezones[timezone.upper()])
        try:
            actual_time = parse(schedule_time)
        except Exception as e:
            return None
        corrected_time = actual_time - time_adjustment - timezone_adjustment
        return corrected_time

    @app_commands.command(name="get_time_discord")
    # @app_commands.guilds(445404006177570829)
    async def get_time_command(self, interaction: discord.Interaction,
                               schedule_time: str, timezone: str):
        """Get the Discord timestamp string for a time"""
        actual_time = self.getTime(schedule_time, timezone)
        event_str = discord.utils.format_dt(actual_time, style="F")
        await interaction.response.send_message(f"`{event_str}`", ephemeral=True)

    def get_event_str(self, mogi):
        mogi_time = discord.utils.format_dt(mogi.start_time, style="F")
        mogi_time_relative = discord.utils.format_dt(
            mogi.start_time, style="R")
        return (f"`#{mogi.sq_id}` **{mogi.size}v{mogi.size}:** {mogi_time} - {mogi_time_relative}")

    @commands.command(aliases=['pt'])
    async def parsetime(self, ctx, *, schedule_time: str):
        try:
            actual_time = parse(schedule_time)
            await ctx.send("```<t:" + str(int(time.mktime(actual_time.timetuple()))) + ":F>```")
        except (ValueError, OverflowError):
            await ctx.send("I couldn't figure out the date and time for your event. Try making it a bit more clear for me.")

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync(self, ctx):
        await self.bot.tree.sync()
        await ctx.send("sync'd")

    @commands.command(name="sync_server")
    @commands.is_owner()
    async def sync_server(self, ctx):
        await self.bot.tree.sync(guild=discord.Object(id=self.bot.config["guild_id"]))
        await ctx.send("sync'd")

    # @commands.command()
    async def get_bots(self, ctx):
        extra_members = []
        if str(ctx.guild.id) in self.bot.config["members_for_channels"].keys():
            extra_members_ids = self.bot.config["members_for_channels"][str(
                ctx.guild.id)]
            for m in extra_members_ids:
                extra_members.append(ctx.guild.get_member(m))
            for m in extra_members:
                print(m)

    # @commands.command()
    async def thread_test(self, ctx):
        for i in range(100):
            thread_msg = await ctx.send(f"{i+1}")
            room_channel = await ctx.channel.create_thread(name=f"Room {i+1}",
                                                           message=thread_msg,
                                                           auto_archive_duration=60)
            await asyncio.sleep(2)

    @commands.command(name="debug_add_team")
    @commands.is_owner()
    async def debug_add_players(self, ctx, members: commands.Greedy[discord.Member]):
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return

        # checking players' mmr
        check_players = [ctx.author]
        check_players.extend(members)
        players = await mk8dx_150cc_mmr(self.URL, check_players)
        not_found = []
        for i, player in enumerate(players):
            if player is None:
                not_found.append(check_players[i].display_name)
        players[0].confirmed = True
        squad = Team(players)
        for i in range(0, 12):
            mogi.teams.append(squad)
        msg = f"{players[0].lounge_name} added 12 times."
        await self.queue_or_send(ctx, msg)
        await self.check_room_channels(mogi)
        await self.check_num_teams(mogi)

    @commands.command(name="debug_add_many_players")
    @commands.is_owner()
    async def debug_add_players(self, ctx, members: commands.Greedy[discord.Member]):
        mogi = self.get_mogi(ctx)
        if mogi is None:
            return
        if (not await self.is_started(ctx, mogi)
                or not await self.is_gathering(ctx, mogi)):
            return

        # checking players' mmr
        check_players = [ctx.author]
        check_players.extend(members)
        players = await mk8dx_150cc_mmr(self.URL, check_players)
        not_found = []
        for i, player in enumerate(players):
            if player is None:
                not_found.append(check_players[i].display_name)
        players[0].confirmed = True
        squad = Team(players)
        for i in range(0, 24):
            mogi.teams.append(squad)
        msg = f"{players[0].lounge_name} added 24 times."
        await self.queue_or_send(ctx, msg)
        await self.check_room_channels(mogi)
        await self.check_num_teams(mogi)

    @commands.command(name="debug_start_rooms")
    @commands.is_owner()
    async def debug_start_rooms(self, ctx):
        truncated_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0)
        next_hour = truncated_time + timedelta(hours=1)
        for mogi in self.ongoing_events.values():
            if mogi.start_time == next_hour:
                await self.add_teams_to_rooms(mogi, (mogi.start_time.minute) % 60, True)
                return
        for mogi in self.old_events.values():
            if mogi.start_time == next_hour:
                await self.add_teams_to_rooms(mogi, (mogi.start_time.minute) % 60, True)
                return


async def setup(bot):
    await bot.add_cog(SquadQueue(bot))

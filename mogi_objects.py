from discord.ui import View, Button
import random
import discord


class Mogi:
    def __init__(self, sq_id: int, size: int, mogi_channel: discord.TextChannel,
                 is_automated=False, start_time=None):
        self.started = False
        self.gathering = False
        self.making_rooms_run = False
        self.sq_id = sq_id
        self.size = size
        self.mogi_channel = mogi_channel
        self.teams = []
        self.rooms = []
        self.is_automated = is_automated
        if not is_automated:
            self.start_time = None
        else:
            self.start_time = start_time

    def check_player(self, member):
        for team in self.teams:
            if team.has_player(member):
                return team
        return None

    def count_registered(self):
        count = 0
        for team in self.teams:
            if team.is_registered():
                count += 1
        return count

    def confirmed_list(self):
        confirmed = []
        for team in self.teams:
            if team.is_registered():
                confirmed.append(team)
        return confirmed

    def update_late_players(self):
        late_player_cutoff = int(len(self.teams) / 12) * 12
        for idx, team in enumerate(self.teams):
            lateness = False
            if idx >= late_player_cutoff:
                lateness = True
            team.set_lateness(lateness)

    def remove_id(self, squad_id: int):
        confirmed = self.confirmed_list()
        if squad_id < 1 or squad_id > len(confirmed):
            return None
        squad = confirmed[squad_id-1]
        self.teams.remove(squad)
        return squad

    def is_room_thread(self, channel_id: int):
        for room in self.rooms:
            if room.thread.id == channel_id:
                return True
        return False

    def get_room_from_thread(self, channel_id: int):
        for room in self.rooms:
            if room.thread.id == channel_id:
                return room
        return None


class Room:
    def __init__(self, teams, room_num: int, thread: discord.Thread):
        self.teams = teams
        self.room_num = room_num
        self.thread = thread
        self.mmr_average = 0
        self.mmr_high = None
        self.mmr_low = None
        self.view = None
        self.finished = False


class Team:
    def __init__(self, players):
        self.players = players
        self.avg_mmr = sum([p.mmr for p in self.players]) / len(self.players)
        self.late = False

    def recalc_avg(self):
        self.avg_mmr = sum([p.mmr for p in self.players]) / len(self.players)

    def is_registered(self):
        for player in self.players:
            if player.confirmed is False:
                return False
        return True

    def has_player(self, member):
        for player in self.players:
            if player.member.id == member.id:
                return True
        return False

    def get_player(self, member):
        for player in self.players:
            if player.member.id == member.id:
                return player
        return None

    def get_first_player(self):
        return self.players[0]

    def sub_player(self, sub_out, sub_in):
        for i, player in enumerate(self.players):
            if player == sub_out:
                self.players[i] = sub_in
                self.recalc_avg()
                return

    def num_confirmed(self):
        count = 0
        for player in self.players:
            if player.confirmed:
                count += 1
        return count

    def get_unconfirmed(self):
        unconfirmed = []
        for player in self.players:
            if not player.confirmed:
                unconfirmed.append(player)
        return unconfirmed

    def set_lateness(self, is_late):
        self.late = is_late

    def get_lateness(self):
        return self.late

    def __lt__(self, other):
        if self.avg_mmr < other.avg_mmr:
            return True
        if self.avg_mmr > other.avg_mmr:
            return False

    def __gt__(self, other):
        return other.__lt__(self)

    # def __eq__(self, other):
    #     if self.avg_mmr == other.avg_mmr:
    #         return True
    #     return False

    def __str__(self):
        return ", ".join([p.lounge_name for p in self.players])


class Player:
    def __init__(self, member, lounge_name, mmr):
        self.member = member
        self.lounge_name = lounge_name
        self.mmr = mmr
        self.confirmed = False
        self.score = 0


class VoteView(View):
    def __init__(self, players, thread, mogi, six_vs_six_threshold=10000):
        super().__init__()
        self.players = players
        self.thread = thread
        self.mogi = mogi
        self.header_text = ""
        self.teams_text = ""
        self.found_winner = False
        self.room_mmr = round(sum([p.mmr for p in self.players]) / 12)
        self.six_vs_six_threshold = six_vs_six_threshold
        self.__setattr__("FFA", [])
        self.__setattr__("2v2", [])
        self.__setattr__("3v3", [])
        self.__setattr__("4v4", [])
        self.__setattr__("6v6", [])

        self.add_button("FFA", self.button_callback)
        self.add_button("2v2", self.button_callback)
        self.add_button("3v3", self.button_callback)
        self.add_button("4v4", self.button_callback)

        if self.room_mmr > six_vs_six_threshold:
            self.add_button("6v6", self.button_callback)

    def __getitem__(self, key):
        return getattr(self, key)

    def add_button(self, label, callback):
        button = Button(label=f"{label}: 0", custom_id=label)
        button.callback = callback
        self.add_item(button)

    async def make_teams(self, format):
        random.shuffle(self.players)

        room = self.mogi.get_room_from_thread(self.thread.id)

        msg = "**Poll Ended!** \n\n"
        msg += f"1) FFA - {len(self['FFA'])}\n"
        msg += f"2) 2v2 - {len(self['2v2'])}\n"
        msg += f"3) 3v3 - {len(self['3v3'])}\n"
        msg += f"4) 4v4 - {len(self['4v4'])}\n"
        if self.room_mmr > self.six_vs_six_threshold:
            msg += f"5) 6v6 - {len(self['6v6'])}\n"
        msg += f"Winner: {format[1]}\n\n"

        room.mmr_average = self.room_mmr
        self.header_text = f"**Room {room.room_num} MMR: {self.room_mmr} - Tier {get_tier(self.room_mmr - 500)}** "
        msg += self.header_text
        msg += "\n"

        teams = []
        teams_per_room = int(12 / format[0])
        for j in range(teams_per_room):
            team = Team(self.players[j*format[0]:(j+1)*format[0]])
            teams.append(team)

        teams.sort(key=lambda team: team.avg_mmr, reverse=True)

        scoreboard_text = []

        for j in range(teams_per_room):
            team_text = f"`{j+1}.` "
            team_names = ", ".join([p.lounge_name for p in teams[j].players])
            scoreboard_text.append(team_names)
            team_text += team_names
            team_text += f" ({int(teams[j].avg_mmr)} MMR)\n"
            msg += team_text
            self.teams_text += team_text

        msg += f"\nTable: `/scoreboard`\n"

        msg += f"RandomBot Scoreboard: `/scoreboard {teams_per_room} {', '.join(scoreboard_text)}`\n\n"

        msg += "Decide a host amongst yourselves; room open at :00, penalty at :06. Good luck!"

        room.teams = teams

        self.found_winner = True
        await self.thread.send(msg)

    async def find_winner(self):
        if not self.found_winner:
            max_votes = 0
            if len(self["FFA"]) > max_votes:
                max_votes = len(self["FFA"])
            if len(self["2v2"]) > max_votes:
                max_votes = len(self["2v2"])
            if len(self["3v3"]) > max_votes:
                max_votes = len(self["3v3"])
            if len(self["4v4"]) > max_votes:
                max_votes = len(self["4v4"])
            if len(self["6v6"]) > max_votes:
                max_votes = len(self["6v6"])

            winners = []

            if len(self["FFA"]) == max_votes:
                winners.append((1, "FFA"))
            if len(self["2v2"]) == max_votes:
                winners.append((2, "2v2"))
            if len(self["3v3"]) == max_votes:
                winners.append((3, "3v3"))
            if len(self["4v4"]) == max_votes:
                winners.append((4, "4v4"))
            if len(self["6v6"]) == max_votes and self.room_mmr > self.six_vs_six_threshold:
                winners.append((6, "6v6"))

            winner = random.choice(winners)

            for curr_button in self.children:
                curr_button.disabled = True

            await self.make_teams(winner)

    async def button_callback(self, interaction: discord.Interaction):
        if not self.found_winner:
            format_name = interaction.data['custom_id']
            if interaction.user.id in self[format_name]:
                self[format_name].remove(interaction.user.id)
            else:
                for key in ["FFA", "2v2", "3v3", "4v4", "6v6"]:
                    if interaction.user.id in self[key]:
                        self[key].remove(interaction.user.id)
                self[format_name].append(interaction.user.id)
            if len(self[format_name]) == 6:
                if format_name == "FFA":
                    players_per_team = 1
                else:
                    players_per_team = int(format_name[0])
                await self.make_teams((players_per_team, format_name))
            for curr_button in self.children:
                curr_button.label = f"{curr_button.custom_id}: {len(self[curr_button.custom_id])}"
                if len(self[format_name]) == 6:
                    curr_button.disabled = True
        await interaction.response.edit_message(view=self)


class JoinView(View):
    def __init__(self, room, get_mmr):
        super().__init__(timeout=1200)
        self.room = room
        self.get_mmr = get_mmr

    @discord.ui.button(label="Join Room")
    async def button_callback(self, interaction, button):
        await interaction.response.defer()
        muted_role_id = 600495108999086090
        if interaction.user.get_role(muted_role_id):
            await interaction.followup.send(
                "Players with the muted role cannot use the sub button.", ephemeral=True)
            return
        try:
            user_mmr = await self.get_mmr(interaction.user.id)
        except:
            await interaction.followup.send(
                "MMR lookup for player has failed, please try again.", ephemeral=True)
            return
        if self.room.room_num == 1:
            self.room.mmr_high = 999999
        if isinstance(user_mmr, int) and user_mmr < self.room.mmr_high + 500 and user_mmr > self.room.mmr_low - 500:
            button.disabled = True
            await interaction.followup.edit_message(interaction.message.id, view=self)
            mention = interaction.user.mention
            await self.room.thread.send(f"{mention} has joined the room.")
        else:
            await interaction.followup.send(
                "You do not meet room requirements", ephemeral=True)


def get_tier(mmr: int):
    if mmr > 14000:
        return 'X'
    if mmr > 13000:
        return 'S'
    if mmr > 12000:
        return 'A'
    if mmr > 11000:
        return 'AB'
    if mmr > 10000:
        return 'B'
    if mmr > 9000:
        return 'BC'
    if mmr > 8000:
        return 'C'
    if mmr > 7000:
        return 'CD'
    if mmr > 6000:
        return 'D'
    if mmr > 5000:
        return 'DE'
    if mmr > 4000:
        return 'E'
    if mmr > 3000:
        return 'EF'
    if mmr > 2000:
        return 'F'
    if mmr > 1000:
        return 'FG'
    else:
        return 'G'

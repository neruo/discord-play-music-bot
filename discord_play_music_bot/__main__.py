# -*- coding: utf-8 -*-

"""
Copyright (c) 2019 Valentin B.
youtube-dlを使ってdiscord.pyで書かれたシンプルな音楽ボットです。
これは簡単な例ですが、音楽ボットは複雑で、完璧に動作するまで多くの時間と知識が必要です。
これを例として、あるいはあなた自身のボットのベースとして使い、好きなように拡張してください。もし、バグがあれば、私に知らせてください。
必要条件
Python 3.5以上
pip install -U discord.py pynacl youtube-dl
また、PATH環境変数にFFmpegがあるか、WindowsではボットのディレクトリにFFmpeg.exeのバイナリがある必要があります。
"""

import asyncio
import functools
import itertools
import math
import os
import random

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

# 無駄なバグレポートメッセージの排除
youtube_dl.utils.bug_reports_message = lambda: ""


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        "format": "bestaudio/best",
        "extractaudio": True,
        "audioformat": "mp3",
        "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
        "restrictfilenames": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logtostderr": False,
        "quiet": True,
        "no_warnings": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
    }

    FFMPEG_OPTIONS = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn",
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(
        self,
        ctx: commands.Context,
        source: discord.FFmpegPCMAudio,
        *,
        data: dict,
        volume: float = 0.5,
    ):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get("uploader")
        self.uploader_url = data.get("uploader_url")
        date = data.get("upload_date")
        self.upload_date = date[6:8] + "." + date[4:6] + "." + date[0:4]
        self.title = data.get("title")
        self.thumbnail = data.get("thumbnail")
        self.description = data.get("description")
        self.duration = self.parse_duration(int(data.get("duration")))
        self.tags = data.get("tags")
        self.url = data.get("webpage_url")
        self.views = data.get("view_count")
        self.likes = data.get("like_count")
        self.dislikes = data.get("dislike_count")
        self.stream_url = data.get("url")

    def __str__(self):
        return "**{0.title}** by **{0.uploader}**".format(self)

    @classmethod
    async def create_source(
        cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None
    ):
        # print('debug 6')
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(
            cls.ytdl.extract_info, search, download=False, process=False
        )
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError("Couldn't find anything that matches `{}`".format(search))

        if "entries" not in data:
            process_info = data
        else:
            process_info = None
            for entry in data["entries"]:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(
                    "Couldn't find anything that matches `{}`".format(search)
                )

        webpage_url = process_info["webpage_url"]
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError("Couldn't fetch `{}`".format(webpage_url))

        if "entries" not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info["entries"].pop(0)
                except IndexError:
                    raise YTDLError(
                        "Couldn't retrieve any matches for `{}`".format(webpage_url)
                    )

        return cls(
            ctx, discord.FFmpegPCMAudio(info["url"], **cls.FFMPEG_OPTIONS), data=info
        )

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append("{} 日".format(days))
        if hours > 0:
            duration.append("{} 時".format(hours))
        if minutes > 0:
            duration.append("{} 分".format(minutes))
        if seconds > 0:
            duration.append("{} 秒".format(seconds))

        return ", ".join(duration)


class Song:
    __slots__ = ("source", "requester")

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (
            discord.Embed(
                title="再生中",
                description="```css\n{0.source.title}\n```".format(self),
                color=discord.Color.blurple(),
            )
            .add_field(name="再生時間", value=self.source.duration)
            .add_field(name="リクエストされました", value=self.requester.mention)
            .add_field(
                name="投稿者",
                value="[{0.source.uploader}]({0.source.uploader_url})".format(self),
            )
            .add_field(name="URL", value="[Click]({0.source.url})".format(self))
            .set_thumbnail(url=self.source.thumbnail)
        )

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.08
        self.skip_votes = set()

        ### henkou tyop ###
        self._autoplay = True
        self.exists = True
        ### henkou tyop ###

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def autoplay(self):
        return self._autoplay

    @autoplay.setter
    def autoplay(self, value: bool):
        self._autoplay = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self, volume: float = 0.5):
        # デフォルト曲停止用カウンタ
        idx = 1
        while True:
            self.next.clear()
            self.now = None

            # print('Tasks count: ', len(asyncio.Task.all_tasks()))
            if idx == 5:
                self._autoplay = False

            if self.autoplay:
                try:
                    async with timeout(10):  # 10秒
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    recommended_urls = []
                    # if len(asyncio.Task.all_tasks()) % 2 == 0:
                    if len(asyncio.all_tasks()) % 2 == 0:
                        recommended_urls.append(
                            f"https://www.youtube.com/watch?v=N1BcpzPGlYQ"
                        )  # デフォルト曲1
                    else:
                        recommended_urls.append(
                            f"https://www.youtube.com/watch?v=uRSvcUozBOc"
                        )  # デフォルト曲2
                    ctx = self._ctx

                    async with ctx.typing():
                        try:
                            source = await YTDLSource.create_source(
                                ctx, recommended_urls[0], loop=self.bot.loop
                            )
                        except YTDLError as e:
                            await ctx.send("このリクエストの処理中にエラーが発生しました: {}".format(str(e)))
                            self.bot.loop.create_task(self.stop())
                            self.exists = False
                            return
                        else:
                            song = Song(source)
                            self.current = song
                            # destination = ctx.author.voice.channel
                            if ctx.voice_state.voice:
                                await ctx.send("{} を再生中です。".format(str(source)))
                            idx += 1

            else:
                try:
                    async with timeout(180):  # 3分
                        self.current = await self.songs.get()
                        # デフォルト曲停止用カウンタクリア
                        idx = 0
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    self.exists = False
                    self._autoplay = True
                    # デフォルト曲停止用カウンタクリア
                    idx = 0
                    return

            # print('debug 1')
            self.current.source.volume = self._volume
            # print('debug 2')
            self.voice.play(self.current.source, after=self.play_next_song)
            # print('debug 3')
            await self.current.source.channel.send(embed=self.current.create_embed())
            # print('debug 4')

            await self.next.wait()

    def play_next_song(self, error=None, volume: float = 0.5):
        if error:
            raise VoiceError(str(error))

        # print('debug 5')
        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        ### henkou tyop ###
        # if not state:
        if not state or not state.exists:
            ### henkou tyop ###
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage("このコマンドは、DMチャンネルでは使用できません。")

        return True

    def _playlist(self, search: str):
        """すべてのプレイリストエントリを含むdictを返します"""
        ydl_opts = {"ignoreerrors": True, "quit": True}

        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            playlist_dict = ydl.extract_info(search, download=False)

            playlistTitle = playlist_dict["title"]
            playlist = dict()
            for video in playlist_dict["entries"]:
                print()

                if not video:
                    print("エラー：情報を取得できません...")
                    continue

                for prop in ["id", "title"]:
                    print(prop, "--", video.get(prop))
                    playlist[
                        video.get("title")
                    ] = "https://www.youtube.com/watch?v=" + video.get("id")
            return playlist, playlistTitle

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ):
        await ctx.send("エラーが発生しました: {}".format(str(error)))

    @commands.command(name="join", invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """ボイスチャネルへの参加。"""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name="summon")
    @commands.has_permissions(manage_guild=True)
    async def _summon(
        self, ctx: commands.Context, *, channel: discord.VoiceChannel = None
    ):
        """ボットを音声チャネルに召喚します。
        チャンネルが指定されていない場合は、あなたのチャンネルに参加します。
        """

        if not channel and not ctx.author.voice:
            raise VoiceError("音声チャネルに接続されておらず、参加するチャネルも指定されていません。無念。")

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name="leave", aliases=["disconnect"])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """キューをクリアし、音声チャネルから退出します。"""

        if not ctx.voice_state.voice:
            return await ctx.send("私はどの音声チャンネルにも入っていません。")

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name="volume")
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """プレーヤーの音量を設定します。"""

        if not ctx.voice_state.is_playing:
            return await ctx.send("現在は何も再生していません。")

        if 0 > volume > 100:
            return await ctx.send("ボリュームは0から100の間で指定してください。")

        ctx.voice_state.volume = volume / 100
        await ctx.send("ボリュームは {}% に設定されました。".format(volume))

    @commands.command(name="now", aliases=["current", "playing"])
    async def _now(self, ctx: commands.Context):
        """現在再生中の曲を表示します。"""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name="pause")
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """再生中の曲を一時停止します。"""

        ### henkou tyop ###
        if ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction("⏯")

        # if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
        #     ctx.voice_state.voice.pause()
        #     await ctx.message.add_reaction('⏯')
        ### henkou tyop ###

    @commands.command(name="resume")
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """一時停止中の曲を再開します。"""

        ### henkou tyop ###
        if not ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction("⏯")

        # if not ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
        #     ctx.voice_state.voice.resume()
        #     await ctx.message.add_reaction('⏯')
        ### henkou tyop ###

    @commands.command(name="stop")
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """曲の再生を停止し、キューをクリアします。"""

        ctx.voice_state.songs.clear()

        ### henkou tyop ###
        # if not ctx.voice_state.is_playing:
        if ctx.voice_state.is_playing:
            ### henkou tyop ###
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction("⏹")

    ### henkou tyop ###
    @commands.command(name="clear")
    async def _clear(self, ctx: commands.Context):
        """実行したチャンネルの直前のメッセージを削除します。チャンネルのお掃除に使います。"""

        await ctx.channel.purge(limit=5)
        await ctx.send("メッセージをクリアしました")

    ### henkou tyop ###

    @commands.command(name="skip")
    async def _skip(self, ctx: commands.Context):
        """曲をスキップするかどうかを投票します。要求者が自動的にスキップできる。
        3つのスキップ投票があれば、曲をスキップすることができます。
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send("今は音楽を再生していないよ...")

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction("⏭")
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction("⏭")
                ctx.voice_state.skip()
            else:
                await ctx.send("スキップ投票中, 現在は **{}/3**人が賛成しています。".format(total_votes))

        else:
            await ctx.send("あなたはすでに、この曲を飛ばすことに投票しています。")

    @commands.command(name="queue")
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """プレイヤーのキューを表示します。
        オプションで、表示するページを指定できます。各ページには10個の要素が含まれています。
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("キューがありません。")

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ""
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += "`{0}.` [**{1.source.title}**]({1.source.url})\n".format(
                i + 1, song
            )

        embed = discord.Embed(
            description="**{} tracks:**\n\n{}".format(len(ctx.voice_state.songs), queue)
        ).set_footer(text="Viewing page {}/{}".format(page, pages))
        await ctx.send(embed=embed)

    @commands.command(name="shuffle")
    async def _shuffle(self, ctx: commands.Context):
        """キューをシャッフルします。"""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("キューがありません。")

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction("✅")

    @commands.command(name="remove")
    async def _remove(self, ctx: commands.Context, index: int):
        """与えられたインデックスで曲をキューから削除します。"""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send("キューがありません。")

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction("✅")

    # henkou tyop ループ機能の廃止
    # @commands.command(name='loop')
    # async def _loop(self, ctx: commands.Context):
    #     """再生中の曲をループさせます。
    #     もう一度このコマンドを実行すると、曲のループが解除されます。
    #     """

    #     if not ctx.voice_state.is_playing:
    #         return await ctx.send('現在は何も再生していません。')

    #     # ループやアンループを行うための逆向きのブール値。
    #     ctx.voice_state.loop = not ctx.voice_state.loop
    #     await ctx.message.add_reaction('✅')

    @commands.command(name="autoplay")
    async def _autoplay(self, ctx: commands.Context):
        """autoplay機能はデフォルトでONになっています。
        キューに曲が入っていない場合、音楽を流します。
        このコマンドをもう一度呼び出して、曲の自動再生ON/OFFを切り替えます。
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send("現在、何も再生されていません。")

        # ループおよびループ解除する逆ブール値.
        ctx.voice_state.autoplay = not ctx.voice_state.autoplay
        await ctx.message.add_reaction("✅")
        await ctx.send(
            "キュー終了後の自動再生が " + ("オン" if ctx.voice_state.autoplay else "オフ") + "になりました。"
        )

    @commands.command(name="play")
    async def _play(self, ctx: commands.Context, *, search: str):
        """曲を再生します。
        キューに曲が入っている場合は、他の曲の再生が終わるまでキューに入れられます。
        このコマンドは、URLが指定されていない場合、様々なサイトから自動的に検索します。
        これらのサイトのリストはこちらからご覧いただけます： https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        ### henkou tyop ###
        if search.__contains__("?list="):
            print("プレイリストを再生します")
            await ctx.send("プレイリストを読み込んでいます...")
            async with ctx.typing():
                playlist, playlistTitle = self._playlist(search)
                for _title, _link in playlist.items():
                    try:
                        source = await YTDLSource.create_source(
                            ctx, _link, loop=self.bot.loop
                        )
                    except YTDLError as e:
                        await ctx.send("このリクエストの処理中にエラーが発生しました: {}".format(str(e)))
                    else:
                        song = Song(source)
                        await ctx.voice_state.songs.put(song)
                await ctx.send(
                    f"`{playlist.__len__()}` 曲がキューに入りました。 from **{playlistTitle}**"
                )
        else:
            ### henkou tyop ###
            async with ctx.typing():
                try:
                    source = await YTDLSource.create_source(
                        ctx, search, loop=self.bot.loop
                    )
                except YTDLError as e:
                    await ctx.send("このリクエストの処理中にエラーが発生しました: {}".format(str(e)))
                else:
                    song = Song(source)

                    await ctx.voice_state.songs.put(song)
                    await ctx.send("{} を再生中です。".format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError("私はどの音声チャンネルにも入っていません。")

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError("私はすでに音声チャンネルに入っています。")


bot = commands.Bot("!", description="music botの使い方")
bot.add_cog(Music(bot))


@bot.event
async def on_ready():
    print("\n{0.user.name}\n{0.user.id} としてログインします。".format(bot))


if __name__ == "__main__":
    TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]
    bot.run(TOKEN)


#####  [変更履歴]  ######
#
# 2021/3/24
# volumeを0.5 -> 0.03に下げました。
#
# 2021/3/25
# 一部日本語表記に変更しました。
#
# 2021/3/26
# 一部日本語表記に変更しました。
#
# stopが機能しない -> 修正済み
# 曲が再生されていない時しか停止コマンドが機能しない問題を、
# 再生中でも機能するようにコードを変更しました。
#
# 曲が終わった後再度!playしても再生されない -> 修正済み
# キューが空の時botがtime outすると、曲が再生されなくなることがわかりました。
# これを解決するためにVoiceStateクラスに修正コードを追加しました。
#
# botがtime outでleaveした時にメッセージを送信するように変更しました。
#
# loopが機能しない -> 調査中
#  -> loop機能を実装しました。
#
# loopした後、音量が1.0に戻ってしまう。 -> 調査中
# 7/4 原因が分からないため調査打ち切り。 loop機能は廃止することを検討中。
#
# 2021/7/5
# loop機能を廃止し、代わりにautoplay機能を追加することに決定しました。
#
# local fileから再生したい -> 検討中
# Default曲としてlocal fileの曲リストから再生するようにしたい。
#
# 2021/7/5
# pauseが機能しない -> 修正済み
# resumeが機能しない -> 修正済み
# volumeを0.03 -> 0.1に上げました。
#
# 2021/7/8
# volumeを0.1 -> 0.08に下げました。
# autoplay機能を追加しました。
# Default曲はとりあえず有名な洋楽としました。
#
# 2021/7/10
# 一部日本語表記に変更しました。
# autoplay機能が有効な場合、5回までデフォルト曲を流すように変更しました。
# 5回以内に曲リクエストがなければ、3分待ってボイスチャットを抜けるように変更しました。
#
# 2021/9/13
# Herokuを使って常時立ち上げるように設定しました。
# heroku用のffmpegとheorku用のlibopusが必要。
# https://elements.heroku.com/buildpacks/xrisk/heroku-opus
# 

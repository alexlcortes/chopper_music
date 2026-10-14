import asyncio
import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord
import spotipy
import yt_dlp
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
# Optional: a server ID to sync slash commands to instantly while developing.
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID")
# Only members with this role (or server admins) can control playback.
# If no role with this name exists in the server, everyone can.
DJ_ROLE = os.environ.get("DJ_ROLE", "DJ")
# Optional: Spotify app credentials (needs Spotify Premium). Without them, Spotify links
# are read from Spotify's public web player instead.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

log = logging.getLogger("chopper")

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
    # YouTube extraction needs a JS runtime; use the installed Node.
    "js_runtimes": {"node": {}},
}
FFMPEG_OPTS = {
    # Reconnect if YouTube drops the stream mid-song.
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
SPONSORBLOCK_API = "https://sponsor.ajay.app/api/skipSegments"
# Parts of YouTube videos to skip: sponsor reads, plugs for the creator's own stuff,
# "like and subscribe" reminders, and talking or skits in music videos.
SPONSORBLOCK_CATEGORIES = ["sponsor", "selfpromo", "interaction", "music_offtopic"]
IDLE_DISCONNECT_SECONDS = 300
MAX_PLAYLIST_TRACKS = 100

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
# Lists a playlist's videos without resolving each one, so big playlists load fast.
ytdl_playlist = yt_dlp.YoutubeDL(
    YTDL_OPTS | {"noplaylist": False, "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST_TRACKS}
)
# "list=" in a YouTube URL, except auto-generated mixes (list=RD...), which never end.
PLAYLIST_RE = re.compile(r"[?&]list=(?!RD)[\w-]+")
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(track|album|playlist)/(\w+)")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

spotify = (
    spotipy.Spotify(
        auth_manager=spotipy.SpotifyClientCredentials(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        retries=0,
    )
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET
    else None
)
# (artists, song title, duration in ms)
SpotifySong = tuple[str, str, int | None]
# (start, end) in seconds
Segment = tuple[float, float]


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


@dataclass
class Track:
    title: str
    # A video URL, or for Spotify tracks a YouTube search that's run right before playing.
    source: str
    duration: int | None
    requested_by: str

    def fmt_duration(self) -> str:
        return fmt_time(self.duration) if self.duration else "live"


async def search(query: str, requested_by: str) -> Track:
    """Resolve a URL or search phrase to a single track."""
    info = await asyncio.to_thread(ytdl.extract_info, query, download=False)
    if "entries" in info:
        if not info["entries"]:
            raise LookupError("No results found.")
        info = info["entries"][0]
    return Track(info["title"], info["webpage_url"], info.get("duration"), requested_by)


def is_playlist(query: str) -> bool:
    return query.startswith(("http://", "https://")) and PLAYLIST_RE.search(query) is not None


async def load_playlist(url: str, requested_by: str) -> tuple[str, list[Track]]:
    """Resolve a playlist URL to its title and tracks (up to MAX_PLAYLIST_TRACKS)."""
    info = await asyncio.to_thread(ytdl_playlist.extract_info, url, download=False)
    tracks = [
        Track(e["title"], e["url"], e.get("duration"), requested_by)
        for e in info.get("entries") or []
        # Private and deleted videos still show up in playlists but can't be played.
        if e and e.get("url") and e.get("title") not in ("[Private video]", "[Deleted video]")
    ]
    if not tracks:
        raise LookupError("That playlist has no playable videos.")
    return info.get("title") or "playlist", tracks


def _spotify_api_songs(kind: str, spotify_id: str) -> tuple[str | None, list[SpotifySong]]:
    """Read a Spotify track, album or playlist through the official Web API."""
    if kind == "track":
        title, items = None, [spotify.track(spotify_id)]
    else:
        if kind == "album":
            title = spotify.album(spotify_id)["name"]
            page = spotify.album_tracks(spotify_id)
        else:
            title = spotify.playlist(spotify_id)["name"]
            page = spotify.playlist_items(spotify_id, additional_types=("track",))
        items = []
        while page and len(items) < MAX_PLAYLIST_TRACKS:
            items += page["items"]
            page = spotify.next(page) if page.get("next") else None
        if kind == "playlist":
            # Playlist entries wrap the track; newer API versions call the field "item".
            items = [e.get("item") or e.get("track") for e in items]
    return title, [
        (", ".join(a["name"] for a in t["artists"]), t["name"], t.get("duration_ms"))
        for t in items[:MAX_PLAYLIST_TRACKS]
        # Skip podcast episodes and removed tracks.
        if t and t.get("type") == "track" and t.get("name")
    ]


async def _spotify_embed_songs(kind: str, spotify_id: str) -> tuple[str | None, list[SpotifySong]]:
    """Read a Spotify track, album or playlist from its public embed player page.

    Needs no API key, but the page shows at most 100 songs.
    """
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.get(f"https://open.spotify.com/embed/{kind}/{spotify_id}") as resp:
            resp.raise_for_status()
            page = await resp.text()
    try:
        props = json.loads(NEXT_DATA_RE.search(page).group(1))["props"]["pageProps"]
        # Missing and private items still load, as a "Page not found" page.
        if props.get("status") == 404:
            raise LookupError(f"Spotify couldn't find that {kind}. Private playlists can't be read.")
        entity = props["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as e:
        raise LookupError("Couldn't read that Spotify link. Spotify may have changed its page.") from e

    if kind == "track":
        return None, [(", ".join(a["name"] for a in entity["artists"]), entity["name"], entity.get("duration"))]
    return entity.get("name"), [
        # Artists come as one string separated by non-breaking spaces.
        (t["subtitle"].replace("\xa0", " "), t["title"], t.get("duration"))
        for t in entity.get("trackList") or []
        if t.get("entityType") == "track"  # skip podcast episodes
    ][:MAX_PLAYLIST_TRACKS]


async def load_spotify(url: str, requested_by: str) -> tuple[str | None, list[Track]]:
    """Resolve a Spotify link to tracks that are searched for on YouTube when they play.

    Uses the Web API when credentials are set, and the public embed page otherwise, or when
    the API refuses (it can't read some playlists made by Spotify itself).
    """
    kind, spotify_id = SPOTIFY_RE.search(url).groups()
    songs = None
    if spotify:
        try:
            title, songs = await asyncio.to_thread(_spotify_api_songs, kind, spotify_id)
        except spotipy.SpotifyOauthError as e:
            log.error("Spotify rejected SPOTIFY_CLIENT_ID/SECRET (%s), using the embed page instead", e)
        except spotipy.SpotifyException as e:
            log.warning("Spotify API couldn't read %s %s (%s), trying the embed page", kind, spotify_id, e.http_status)
    if songs is None:
        title, songs = await _spotify_embed_songs(kind, spotify_id)

    tracks = []
    for artists, song, duration_ms in songs:
        name = f"{artists} - {song}"
        tracks.append(Track(name, f"ytsearch1:{name} audio", duration_ms // 1000 if duration_ms else None, requested_by))
    if not tracks:
        raise LookupError(f"That {kind} has no playable songs.")
    return title, tracks


async def sponsor_segments(video_id: str) -> list[Segment]:
    """Get the parts of a YouTube video to skip from SponsorBlock."""
    params = {"videoID": video_id, "categories": json.dumps(SPONSORBLOCK_CATEGORIES), "actionType": "skip"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(SPONSORBLOCK_API, params=params) as resp:
                if resp.status == 404:  # nobody has submitted segments for this video
                    return []
                resp.raise_for_status()
                data = await resp.json()
        return [(float(s["segment"][0]), float(s["segment"][1])) for s in data]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
        # SponsorBlock is a free community service; play the whole video if it's down.
        log.warning("SponsorBlock lookup failed for %s (%r), playing without skipping", video_id, e)
        return []


async def stream_url(track: Track) -> tuple[str, list[Segment]]:
    """Get a fresh audio stream URL (they expire, so fetch right before playing) and the parts to skip."""
    info = await asyncio.to_thread(ytdl.extract_info, track.source, download=False)
    if "entries" in info:
        if not info["entries"]:
            raise LookupError("No match found on YouTube.")
        info = info["entries"][0]
    if info.get("extractor_key") != "Youtube":
        return info["url"], []
    return info["url"], await sponsor_segments(info["id"])


def ffmpeg_opts(skip: list[Segment]) -> dict[str, str]:
    """FFmpeg options that play the stream with the given segments cut out."""
    if not skip:
        return FFMPEG_OPTS
    # Drop audio inside any segment, then close the gaps in the timestamps.
    inside = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in skip)
    audio_filter = f"aselect='not({inside})',asetpts=N/SR/TB"
    return FFMPEG_OPTS | {"options": f'{FFMPEG_OPTS["options"]} -af "{audio_filter}"'}


class GuildPlayer:
    """Queue and playback loop for a single server."""

    def __init__(self, bot: "ChopperBot", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self._next = asyncio.Event()
        self._wake = asyncio.Event()
        self._task = bot.loop.create_task(self._loop())

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    def enqueue(self, track: Track):
        self.queue.append(track)
        self._wake.set()

    async def _loop(self):
        while True:
            self._wake.clear()
            if not self.queue:
                try:
                    await asyncio.wait_for(self._wake.wait(), IDLE_DISCONNECT_SECONDS)
                except asyncio.TimeoutError:
                    await self.destroy()
                    return
                continue

            track = self.queue.popleft()
            vc = self.voice
            if vc is None:
                self.queue.clear()
                continue

            try:
                url, skip = await stream_url(track)
            except Exception as e:
                log.exception("Failed to get stream")
                await self._say(f"Couldn't play **{track.title}**: {e}")
                continue

            self.current = track
            self._next.clear()
            source = discord.FFmpegOpusAudio(url, **ffmpeg_opts(skip))
            vc.play(source, after=lambda err: self.bot.loop.call_soon_threadsafe(self._next.set))
            skipped = sum(end - start for start, end in skip)
            note = f", skipping {fmt_time(skipped)} of sponsors and non-music" if skipped >= 1 else ""
            await self._say(f"Now playing: **{track.title}** [{track.fmt_duration()}{note}]")
            await self._next.wait()
            self.current = None

    async def _say(self, msg: str):
        if self.text_channel:
            try:
                await self.text_channel.send(msg)
            except discord.HTTPException:
                pass

    async def destroy(self):
        self.queue.clear()
        if self.voice:
            await self.voice.disconnect()
        self.bot.players.pop(self.guild.id, None)
        if self._task is not asyncio.current_task():
            self._task.cancel()


class ChopperBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.players: dict[int, GuildPlayer] = {}

    async def setup_hook(self):
        if DEV_GUILD_ID:
            guild = discord.Object(int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.error(
                    "Couldn't register commands in server %s. Check that DEV_GUILD_ID is your "
                    "server's ID and that the bot has been invited with the applications.commands scope.",
                    DEV_GUILD_ID,
                )
        else:
            await self.tree.sync()

    def player_for(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def on_voice_state_update(self, member, before, after):
        # Leave when everyone else has left the channel.
        vc = member.guild.voice_client
        if vc and before.channel == vc.channel and not [m for m in vc.channel.members if not m.bot]:
            player = self.players.get(member.guild.id)
            if player:
                await player.destroy()
            else:
                await vc.disconnect()


bot = ChopperBot()


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)


class NotDJ(app_commands.CheckFailure):
    pass


def dj_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        role = discord.utils.get(interaction.guild.roles, name=DJ_ROLE)
        if role is None or member.guild_permissions.manage_guild or role in member.roles:
            return True
        raise NotDJ(f"You need the **{DJ_ROLE}** role to control the music.")

    return app_commands.check(predicate)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, NotDJ):
        msg = str(error)
    else:
        log.exception("Command failed", exc_info=error)
        msg = "Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def require_same_channel(interaction: discord.Interaction) -> discord.VoiceClient | None:
    vc = interaction.guild.voice_client
    user_vc = interaction.user.voice
    if vc is None:
        await interaction.response.send_message("I'm not playing anything.", ephemeral=True)
        return None
    if not user_vc or user_vc.channel != vc.channel:
        await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
        return None
    return vc  # type: ignore[return-value]


@bot.tree.command(description="Play a song or playlist from YouTube or Spotify (URL or search terms)")
@app_commands.describe(query="YouTube or Spotify link, or what to search for")
@app_commands.guild_only()
@dj_only()
async def play(interaction: discord.Interaction, query: str):
    user_voice = interaction.user.voice
    if not user_voice or not user_voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    vc = interaction.guild.voice_client
    if vc is None:
        vc = await user_voice.channel.connect(self_deaf=True)
    elif vc.channel != user_voice.channel:
        await interaction.followup.send(f"I'm already in {vc.channel.mention}.")
        return

    try:
        if SPOTIFY_RE.search(query):
            playlist_title, tracks = await load_spotify(query, interaction.user.display_name)
        elif is_playlist(query):
            playlist_title, tracks = await load_playlist(query, interaction.user.display_name)
        else:
            playlist_title, tracks = None, [await search(query, interaction.user.display_name)]
    except Exception as e:
        await interaction.followup.send(f"Couldn't find that: {e}")
        return

    player = bot.player_for(interaction.guild)
    player.text_channel = interaction.channel
    busy = player.current is not None or bool(player.queue)
    for t in tracks:
        player.enqueue(t)

    if playlist_title:
        capped = f" (first {MAX_PLAYLIST_TRACKS})" if len(tracks) >= MAX_PLAYLIST_TRACKS else ""
        await interaction.followup.send(
            f"Queued **{len(tracks)}** songs{capped} from **{playlist_title}**"
        )
        return

    track = tracks[0]
    if busy:
        await interaction.followup.send(
            f"Queued **{track.title}** [{track.fmt_duration()}] at position {len(player.queue)}"
        )
    else:
        await interaction.followup.send(f"Loading **{track.title}**…")


@bot.tree.command(description="Skip the current song")
@app_commands.guild_only()
@dj_only()
async def skip(interaction: discord.Interaction):
    vc = await require_same_channel(interaction)
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # triggers the `after` callback, which advances the queue
        await interaction.response.send_message("Skipped.")
    elif vc:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Pause playback")
@app_commands.guild_only()
@dj_only()
async def pause(interaction: discord.Interaction):
    vc = await require_same_channel(interaction)
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    elif vc:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Resume playback")
@app_commands.guild_only()
@dj_only()
async def resume(interaction: discord.Interaction):
    vc = await require_same_channel(interaction)
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    elif vc:
        await interaction.response.send_message("Not paused.", ephemeral=True)


@bot.tree.command(description="Show the queue")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    player = bot.players.get(interaction.guild.id)
    if not player or (not player.current and not player.queue):
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines = []
    if player.current:
        lines.append(f"**Now:** {player.current.title} [{player.current.fmt_duration()}]")
    for i, t in enumerate(list(player.queue)[:15], 1):
        lines.append(f"`{i}.` {t.title} [{t.fmt_duration()}] — {t.requested_by}")
    if len(player.queue) > 15:
        lines.append(f"…and {len(player.queue) - 15} more")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(description="Clear the queue and leave the voice channel")
@app_commands.guild_only()
@dj_only()
async def stop(interaction: discord.Interaction):
    vc = await require_same_channel(interaction)
    if not vc:
        return
    player = bot.players.get(interaction.guild.id)
    if player:
        await player.destroy()
    else:
        await vc.disconnect()
    await interaction.response.send_message("Stopped and left the channel.")


if __name__ == "__main__":
    discord.utils.setup_logging()
    bot.run(TOKEN, log_handler=None)

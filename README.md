# Chopper Music

A Discord music bot that plays YouTube audio in your server's voice channels. Everyone in the voice channel hears the same music, and people can queue songs with slash commands.

## Commands

| Command | What it does |
|---|---|
| `/play <link or search>` | Joins your voice channel and plays the song, or adds it to the queue. Accepts a YouTube playlist link too, and queues up to 100 of its songs. |
| `/skip` | Skips the current song |
| `/pause` / `/resume` | Pauses or resumes playback |
| `/queue` | Shows what's playing now and what's next |
| `/stop` | Clears the queue and leaves the voice channel |

The bot leaves on its own when everyone else has left the voice channel, or after 5 minutes with nothing playing.

> Discord only lets bots join voice channels in a server. The bot can't join calls in DMs or group DMs.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python and the bot's dependencies for you
- [FFmpeg](https://ffmpeg.org/download.html), which converts the audio for Discord
- [Node.js](https://nodejs.org/), which yt-dlp needs to read YouTube pages

On macOS with [Homebrew](https://brew.sh):

```sh
brew install uv ffmpeg node
```

On Windows with winget:

```sh
winget install astral-sh.uv Gyan.FFmpeg OpenJS.NodeJS
```

On Debian or Ubuntu:

```sh
sudo apt install ffmpeg nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Setup

### 1. Create a Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open the **Bot** page, click **Reset Token** and copy the token. Discord only shows it once. Keep it secret: anyone who has it can control your bot.

### 2. Invite the bot to your server

1. In the Developer Portal, open **OAuth2 → URL Generator**.
2. Under **Scopes**, tick `bot` and `applications.commands`.
3. Under **Bot Permissions**, tick **Connect**, **Speak** and **Send Messages**.
4. Open the generated link and choose your server.

### 3. Download and configure the bot

```sh
git clone https://github.com/alexlcortes/chopper_music.git
cd chopper_music
cp .env.example .env
```

Open `.env` and fill it in:

```
DISCORD_TOKEN=your-bot-token
DEV_GUILD_ID=your-server-id
DJ_ROLE=DJ
```

| Setting | Required | What it's for |
|---|---|---|
| `DISCORD_TOKEN` | Yes | The bot token from step 1 |
| `DEV_GUILD_ID` | No | Your server's ID. When it's set, slash commands appear right away; without it, they can take up to an hour. To copy the ID, turn on **User Settings → Advanced → Developer Mode**, then right-click your server's icon and choose **Copy Server ID**. |
| `DJ_ROLE` | No | The role allowed to control the music. Defaults to `DJ`. See [Who can control the music](#who-can-control-the-music). |

Never commit `.env`. It's already listed in `.gitignore`.

### 4. Run the bot

```sh
uv run bot.py
```

The first run installs Python and the dependencies, which takes a moment. When you see `Logged in as <your bot>`, join a voice channel in your server and try `/play never gonna give you up`.

The bot stays online only while this command is running. To keep it online all the time, run it on a computer or server that stays on.

## Who can control the music

`/play`, `/skip`, `/pause`, `/resume` and `/stop` are limited to:

- members with the role named in `DJ_ROLE` (default `DJ`)
- members with the **Manage Server** permission

Anyone can use `/queue`.

If your server has no role with that name, everyone can control the music. To turn the restriction on, create the role under **Server Settings → Roles** and give it to the people who should be DJs. The role doesn't need any special permissions.

For finer control, use **Server Settings → Integrations → *your bot*** to allow or block individual commands for particular roles or members.

## Troubleshooting

- **The slash commands don't show up.** Set `DEV_GUILD_ID`, restart the bot and reload Discord (Ctrl+R, or Cmd+R on macOS). Check that the invite link included the `applications.commands` scope.
- **`403 Forbidden (error code: 50001): Missing Access` on startup.** `DEV_GUILD_ID` is wrong, or the bot hasn't been invited to that server.
- **The bot joins but there's no sound, or songs fail to play.** Check that FFmpeg and Node.js are installed and on your `PATH` (`ffmpeg -version`, `node --version`). If YouTube playback breaks after working before, update yt-dlp with `uv lock --upgrade-package yt-dlp`.

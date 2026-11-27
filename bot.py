import os
import asyncio
import datetime
from pathlib import Path
from dotenv import load_dotenv
from gtts import gTTS
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
NOTIFICATION_CHANNEL_ID = os.getenv("NOTIFICATION_CHANNEL_ID")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
PORT = int(os.getenv("PORT", 8080))

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
voice_lock = asyncio.Lock()

async def handle_health_check(request):
    return web.Response(text="FarmBot Online", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[+] Web sunucusu port {PORT} üzerinde dinliyor.")

class HarvestView(discord.ui.View):
    def __init__(self, plots):
        super().__init__(timeout=None)
        for plot in plots[:5]:
            button = discord.ui.Button(
                label=f"Hasat Et #{plot['id']} ({plot['name']})",
                style=discord.ButtonStyle.success,
                custom_id=f"harvest_{plot['id']}"
            )
            button.callback = self.create_callback(plot['id'])
            self.add_item(button)

    def create_callback(self, plot_id: int):
        async def callback(interaction: discord.Interaction):
            updated = database.record_harvest(plot_id)
            if not updated:
                await interaction.response.send_message("Tarla bulunamadı.", ephemeral=True)
                return
            next_dt = datetime.datetime.fromisoformat(updated["next_harvest"])
            ts = int(next_dt.timestamp())
            await interaction.response.send_message(
                f"✅ **#{updated['id']} {updated['name']}** hasat edildi! Yeni hasat zamanı: <t:{ts}:R> (<t:{ts}:T>)",
                ephemeral=False
            )
        return callback

async def generate_tts_alert(text: str, filename: str = "alert.mp3") -> str:
    path = Path(filename)
    tts = gTTS(text=text, lang="tr")
    tts.save(str(path))
    return str(path)

async def play_voice_alert(channel: discord.VoiceChannel, audio_file: str):
    async with voice_lock:
        voice_client = channel.guild.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)

        try:
            vc = await channel.connect()
            source = discord.FFmpegPCMAudio(source=audio_file, executable=FFMPEG_PATH)
            vc.play(source)
            while vc.is_playing():
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.5)
            await vc.disconnect()
        except Exception as e:
            print(f"[SES HATA] {channel.name} odasında ses çalınamadı: {e}")
            if channel.guild.voice_client:
                await channel.guild.voice_client.disconnect(force=True)

@tasks.loop(seconds=5.0)
async def check_timers():
    try:
        due_plots = database.get_due_plots()
    except Exception as e:
        print(f"[DB HATA] Zamanlayıcı kontrolü başarısız: {e}")
        return

    if not due_plots:
        return

    for plot in due_plots:
        database.mark_alerted(plot["id"])
        alert_text = f"Dikkat! {plot['name']} çalısı için hasat zamanı geldi!"
        audio_file = await generate_tts_alert(alert_text)

        # 1. Ses kanalını kontrol et ve sesli uyarı ver
        for guild in bot.guilds:
            for vc in guild.voice_channels:
                active_members = [m for m in vc.members if not m.bot and not m.voice.self_deaf and not m.voice.deaf]
                if active_members:
                    await play_voice_alert(vc, audio_file)
                    break

            # 2. Metin kanalına butonlu bildirim at
            channel = None
            if NOTIFICATION_CHANNEL_ID:
                try:
                    channel = guild.get_channel(int(NOTIFICATION_CHANNEL_ID))
                except ValueError:
                    channel = None
            if not channel:
                channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)

            if channel:
                embed = discord.Embed(
                    title="🌾 Hasat Zamanı Geldi!",
                    description=f"**#{plot['id']} - {plot['name']}** çalısının süresi doldu, toplamaya hazır!",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc)
                )
                view = HarvestView([plot])
                await channel.send(embed=embed, view=view)

@check_timers.before_loop
async def before_check_timers():
    await bot.wait_until_ready()

@bot.tree.command(name="ekle", description="Yeni bir bitki/çalı zamanlayıcısı ekler.")
@app_commands.describe(isim="Bitkinin veya çalının adı", dakika="Büyüme süresi (dakika)")
async def cmd_ekle(interaction: discord.Interaction, isim: str, dakika: int):
    if dakika <= 0:
        await interaction.response.send_message("Dakika 0'dan büyük olmalıdır.", ephemeral=True)
        return
    
    duration_seconds = dakika * 60
    plot_id = database.add_plot(isim, duration_seconds)
    now = datetime.datetime.now(datetime.timezone.utc)
    next_harvest = now + datetime.timedelta(seconds=duration_seconds)
    ts = int(next_harvest.timestamp())

    await interaction.response.send_message(
        f"🌱 **#{plot_id} {isim}** tarlaya eklendi.\n"
        f"Büyüme süresi: {dakika} dk.\n"
        f"İlk hasat: <t:{ts}:R> (<t:{ts}:T>)"
    )

@bot.tree.command(name="hasat_et", description="Belirtilen çalıyı hasat edip süresini sıfırlar.")
@app_commands.describe(tarla_id="Hasat edilecek tarlanın ID numarası")
async def cmd_hasat_et(interaction: discord.Interaction, tarla_id: int):
    updated = database.record_harvest(tarla_id)
    if not updated:
        await interaction.response.send_message("Belirtilen ID ile çalı/tarla bulunamadı.", ephemeral=True)
        return

    next_dt = datetime.datetime.fromisoformat(updated["next_harvest"])
    ts = int(next_dt.timestamp())
    await interaction.response.send_message(
        f"🌾 **#{updated['id']} {updated['name']}** hasat edildi!\n"
        f"Bir sonraki hasat: <t:{ts}:R> (<t:{ts}:T>)"
    )

@bot.tree.command(name="durum", description="Tüm çalıların ve hasat sürelerinin durumunu listeler.")
async def cmd_durum(interaction: discord.Interaction):
    plots = database.get_all_plots()
    if not plots:
        await interaction.response.send_message("Kayıtlı çalı veya tarla bulunmuyor. `/ekle` komutu ile ekle.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🌿 Rob Run Tarla & Çalı Durumu",
        color=discord.Color.teal(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    due_list = []
    
    for p in plots:
        next_dt = datetime.datetime.fromisoformat(p["next_harvest"])
        ts = int(next_dt.timestamp())
        is_ready = now >= next_dt
        status_icon = "🟢 HAZIR" if is_ready else "⏳ Büyüyor"
        embed.add_field(
            name=f"#{p['id']} - {p['name']} ({status_icon})",
            value=f"• Periyot: {p['duration_seconds'] // 60} dk\n• Hedef: <t:{ts}:R> (<t:{ts}:T>)",
            inline=False
        )
        if is_ready:
            due_list.append(p)

    view = HarvestView(due_list) if due_list else None
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="sil", description="Kayıtlı bir çalı/tarlayı siler.")
@app_commands.describe(tarla_id="Silinecek tarlanın ID numarası")
async def cmd_sil(interaction: discord.Interaction, tarla_id: int):
    if database.delete_plot(tarla_id):
        await interaction.response.send_message(f"🗑️ #{tarla_id} numaralı çalı silindi.")
    else:
        await interaction.response.send_message("Belirtilen ID bulunamadı.", ephemeral=True)

@bot.tree.command(name="kanal_ayarla", description="Hasat bildirimlerinin atılacağı kanalı ayarlar.")
async def cmd_kanal_ayarla(interaction: discord.Interaction, kanal: discord.TextChannel):
    global NOTIFICATION_CHANNEL_ID
    NOTIFICATION_CHANNEL_ID = str(kanal.id)
    await interaction.response.send_message(f"📢 Hasat bildirim kanalı {kanal.mention} olarak güncellendi.")

@bot.event
async def on_ready():
    database.init_db()
    await bot.tree.sync()
    if not check_timers.is_running():
        check_timers.start()
    await start_web_server()
    print(f"[+] Bot aktif: {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_bot_token_here":
        print("[!] Hata: .env dosyasında DISCORD_TOKEN tanımlanmalı.")
    else:
        bot.run(TOKEN)

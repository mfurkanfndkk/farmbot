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

def build_dashboard_embed(plots=None) -> discord.Embed:
    if plots is None:
        plots = database.get_all_plots()
    embed = discord.Embed(
        title="🌿 Rob Run — Çalı & Tarla Kontrol Paneli",
        description="Aşağıdaki butonları kullanarak yeni çalı ekleyebilir, hasat edebilir veya silebilirsiniz.",
        color=discord.Color.teal(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )

    if not plots:
        embed.add_field(
            name="Kayıtlı Çalı Yok",
            value="Şu anda takip edilen çalı bulunmuyor.\nAşağıdaki **🌱 Yeni Çalı Ekle** butonuna tıklayarak ekleyin.",
            inline=False
        )
        return embed

    now = datetime.datetime.now(datetime.timezone.utc)
    for p in plots:
        next_dt = datetime.datetime.fromisoformat(p["next_harvest"])
        ts = int(next_dt.timestamp())
        is_ready = now >= next_dt
        status = "🟢 **HASAT HAZIR!**" if is_ready else "⏳ **Büyüyor**"
        
        embed.add_field(
            name=f"#{p['id']} - {p['name']} ({status})",
            value=f"• Süre: {p['duration_seconds'] // 60} dakika\n• Hasat Zamanı: <t:{ts}:R> (<t:{ts}:T>)",
            inline=False
        )

    embed.set_footer(text="Son Güncelleme")
    return embed

class AddPlotModal(discord.ui.Modal, title="🌱 Yeni Çalı Ekle"):
    name_input = discord.ui.TextInput(
        label="Bitki / Çalı Adı",
        placeholder="Örn: Nane, Böğürtlen, Çilek...",
        required=True,
        max_length=50
    )
    duration_input = discord.ui.TextInput(
        label="Büyüme Süresi (Dakika)",
        placeholder="Örn: 31",
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            dakika = int(self.duration_input.value.strip())
            if dakika <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("❌ Lütfen geçerli bir pozitif dakika sayısı girin.", ephemeral=True)
            return

        name = self.name_input.value.strip()
        def _add():
            database.add_plot(name, dakika * 60)
            return database.get_all_plots()
        
        plots = await asyncio.to_thread(_add)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

class DeletePlotSelect(discord.ui.Select):
    def __init__(self, plots):
        options = [
            discord.SelectOption(
                label=f"#{p['id']} - {p['name']}",
                value=str(p['id']),
                description=f"Periyot: {p['duration_seconds'] // 60} dk",
                emoji="🗑️"
            )
            for p in plots[:25]
        ]
        super().__init__(
            placeholder="Silmek istediğiniz çalıyı seçin...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="panel_delete_select",
            row=3
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        plot_id = int(self.values[0])
        def _delete():
            database.delete_plot(plot_id)
            return database.get_all_plots()
        
        plots = await asyncio.to_thread(_delete)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

class DashboardView(discord.ui.View):
    def __init__(self, plots=None):
        super().__init__(timeout=None)
        if plots is None:
            plots = database.get_all_plots()

        # Row 0: Ana Aksiyon Butonları
        add_btn = discord.ui.Button(
            label="Yeni Çalı Ekle",
            style=discord.ButtonStyle.primary,
            emoji="🌱",
            custom_id="panel_add_btn",
            row=0
        )
        add_btn.callback = self.add_callback
        self.add_item(add_btn)

        refresh_btn = discord.ui.Button(
            label="Yenile",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            custom_id="panel_refresh_btn",
            row=0
        )
        refresh_btn.callback = self.refresh_callback
        self.add_item(refresh_btn)

        if plots:
            harvest_all_btn = discord.ui.Button(
                label="Tümünü Hasat Et",
                style=discord.ButtonStyle.success,
                emoji="🌾",
                custom_id="panel_harvest_all_btn",
                row=0
            )
            harvest_all_btn.callback = self.harvest_all_callback
            self.add_item(harvest_all_btn)

            # Row 1 & 2: Çalı Hasat Butonları (Her çalı için doğrudan buton)
            now = datetime.datetime.now(datetime.timezone.utc)
            for idx, p in enumerate(plots[:10]):
                next_dt = datetime.datetime.fromisoformat(p["next_harvest"])
                is_ready = now >= next_dt
                style = discord.ButtonStyle.success if is_ready else discord.ButtonStyle.secondary
                row_idx = 1 if idx < 5 else 2

                btn = discord.ui.Button(
                    label=f"#{p['id']} {p['name']} ({'HAZIR' if is_ready else 'Hasat'})",
                    style=style,
                    emoji="🌾",
                    custom_id=f"panel_harvest_{p['id']}",
                    row=row_idx
                )
                btn.callback = self.create_harvest_callback(p['id'])
                self.add_item(btn)

            # Row 3: Silme Dropdown'u
            self.add_item(DeletePlotSelect(plots))

    async def add_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddPlotModal())

    async def refresh_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        plots = await asyncio.to_thread(database.get_all_plots)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

    async def harvest_all_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        def _harvest_all():
            for p in database.get_all_plots():
                database.record_harvest(p["id"])
            return database.get_all_plots()
        
        plots = await asyncio.to_thread(_harvest_all)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

    def create_harvest_callback(self, plot_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            def _harvest():
                database.record_harvest(plot_id)
                return database.get_all_plots()
            
            plots = await asyncio.to_thread(_harvest)
            new_embed = build_dashboard_embed(plots)
            new_view = DashboardView(plots)
            await interaction.edit_original_response(embed=new_embed, view=new_view)
        return callback

class AlertHarvestView(discord.ui.View):
    def __init__(self, plot_id: int, plot_name: str):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=f"Hasat Ettim (#{plot_id} {plot_name})",
            style=discord.ButtonStyle.success,
            emoji="🌾",
            custom_id=f"alert_harvest_{plot_id}"
        )
        button.callback = self.create_callback(plot_id)
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
                f"✅ **#{updated['id']} {updated['name']}** hasat edildi! Yeni hasat: <t:{ts}:R> (<t:{ts}:T>)",
                ephemeral=False
            )
        return callback

async def generate_tts_alert(text: str, filename: str = "alert.mp3") -> str:
    path = Path(filename)
    try:
        def _create():
            tts = gTTS(text=text, lang="tr", slow=False)
            tts.save(str(path))
        await asyncio.to_thread(_create)
        size = path.stat().st_size
        print(f"[SES] TTS dosyası üretildi: {size} bayt")
        if size < 500:
            raise ValueError("TTS dosyası çok küçük veya boş")
    except Exception as e:
        print(f"[SES UYARI] gTTS başarısız veya engellendi ({e}). FFmpeg dahili alarm sesi üretiliyor...")
        # FFmpeg ile doğrudan iki tonlu yüksek sesli melodi sentezle
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_PATH, "-y", "-nostdin",
            "-f", "lavfi", "-i", "sine=frequency=750:duration=0.4",
            "-f", "lavfi", "-i", "sine=frequency=1100:duration=0.6",
            "-filter_complex", "[0:0][1:0]concat=n=2:v=0:a=1[out]",
            "-map", "[out]", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        print(f"[SES] Dahili FFmpeg alarmı oluşturuldu: {path.stat().st_size} bayt")

    return str(path)

async def play_voice_alert(channel: discord.VoiceChannel, audio_file: str):
    async with voice_lock:
        voice_client = channel.guild.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)

        try:
            vc = await channel.connect()
            # UDP soketinin oturması için bekleme
            await asyncio.sleep(1.2)

            # -nostdin: Docker container içinde stdin askıda kalmasını engeller
            source = discord.FFmpegPCMAudio(
                source=audio_file,
                executable=FFMPEG_PATH,
                before_options="-nostdin",
                options="-vn -filter:a volume=3.0"
            )
            transformed = discord.PCMVolumeTransformer(source, volume=1.5)
            
            vc.play(transformed)
            while vc.is_playing():
                await asyncio.sleep(0.3)
                
            await asyncio.sleep(1.0)
            await vc.disconnect()
            print(f"[SES] {channel.name} odasında oynatma başarıyla tamamlandı.")
        except Exception as e:
            print(f"[SES HATA] {channel.name} odasında ses çalınamadı: {e}")
            if channel.guild.voice_client:
                await channel.guild.voice_client.disconnect(force=True)

@bot.listen("on_interaction")
async def handle_component_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    if interaction.response.is_done():
        return
    
    custom_id = interaction.data.get("custom_id", "")
    
    if custom_id == "panel_add_btn":
        await interaction.response.send_modal(AddPlotModal())
        return

    if not (custom_id.startswith("panel_") or custom_id.startswith("alert_")):
        return

    await interaction.response.defer()

    if custom_id == "panel_refresh_btn":
        plots = await asyncio.to_thread(database.get_all_plots)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

    elif custom_id == "panel_harvest_all_btn":
        def _harvest_all():
            for p in database.get_all_plots():
                database.record_harvest(p["id"])
            return database.get_all_plots()
        plots = await asyncio.to_thread(_harvest_all)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

    elif custom_id.startswith("panel_harvest_"):
        plot_id = int(custom_id.split("_")[-1])
        def _harvest():
            database.record_harvest(plot_id)
            return database.get_all_plots()
        plots = await asyncio.to_thread(_harvest)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

    elif custom_id == "panel_delete_select":
        plot_id = int(interaction.data["values"][0])
        def _delete():
            database.delete_plot(plot_id)
            return database.get_all_plots()
        plots = await asyncio.to_thread(_delete)
        new_embed = build_dashboard_embed(plots)
        new_view = DashboardView(plots)
        await interaction.edit_original_response(embed=new_embed, view=new_view)

    elif custom_id.startswith("alert_harvest_"):
        plot_id = int(custom_id.split("_")[-1])
        updated = await asyncio.to_thread(database.record_harvest, plot_id)
        if updated:
            next_dt = datetime.datetime.fromisoformat(updated["next_harvest"])
            ts = int(next_dt.timestamp())
            await interaction.followup.send(
                f"✅ **#{updated['id']} {updated['name']}** hasat edildi! Yeni hasat: <t:{ts}:R>",
                ephemeral=True
            )

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
                view = AlertHarvestView(plot["id"], plot["name"])
                await channel.send(embed=embed, view=view)

@check_timers.before_loop
async def before_check_timers():
    await bot.wait_until_ready()

@bot.command(name="test_ses")
async def prefix_test_ses(ctx):
    vc = ctx.author.voice.channel if ctx.author.voice else None
    if not vc:
        for c in ctx.guild.voice_channels:
            if [m for m in c.members if not m.bot]:
                vc = c
                break
    if not vc:
        await ctx.send("❌ Bir sesli kanala katılmalısın veya sesli kanalda biri olmalı.")
        return

    await ctx.send(f"🔊 **{vc.name}** odasına girilip ses testi yapılıyor...")
    audio = await generate_tts_alert("Dikkat dikkat! Rob Run hasat botu ses sistemi başarıyla çalışıyor. Ses seviyesi kontrol edildi.")
    await play_voice_alert(vc, audio)
    await ctx.send("✅ Ses testi tamamlandı!")

@bot.tree.command(name="test_ses", description="Botun sesli odaya girip konuşmasını anında test eder.")
async def cmd_test_ses(interaction: discord.Interaction):
    guild = interaction.guild
    user_vc = interaction.user.voice.channel if interaction.user.voice else None
    target_vc = user_vc

    if not target_vc and guild:
        for c in guild.voice_channels:
            if [m for m in c.members if not m.bot]:
                target_vc = c
                break

    if not target_vc:
        await interaction.response.send_message("❌ Bir sesli kanala girmelisin veya bir odada kullanıcı olmalı.", ephemeral=True)
        return

    await interaction.response.send_message(f"🔊 **{target_vc.name}** kanalına girilip ses testi başlatılıyor...")
    audio = await generate_tts_alert("Dikkat dikkat! Rob Run hasat botu ses sistemi başarıyla çalışıyor. Ses seviyesi kontrol edildi.")
    await play_voice_alert(target_vc, audio)
    await interaction.followup.send("✅ Ses testi başarıyla tamamlandı!", ephemeral=True)

@bot.command(name="panel")
async def prefix_panel(ctx):
    embed = build_dashboard_embed()
    view = DashboardView()
    await ctx.send(embed=embed, view=view)

@bot.command(name="durum")
async def prefix_durum(ctx):
    embed = build_dashboard_embed()
    view = DashboardView()
    await ctx.send(embed=embed, view=view)

@bot.tree.command(name="panel", description="Etkileşimli butonlu tarla kontrol panelini açar.")
async def cmd_panel(interaction: discord.Interaction):
    embed = build_dashboard_embed()
    view = DashboardView()
    await interaction.response.send_message(embed=embed, view=view)

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
    embed = build_dashboard_embed()
    view = DashboardView()
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
    await start_web_server()

    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus("libopus.so.0")
            print("[+] libopus başarıyla yüklendi.")
        except Exception as e:
            print(f"[!] libopus manuel yükleme uyarısı: {e}")

    try:
        database.init_db()
    except Exception as e:
        print(f"[DB HATA] init_db başarısız: {e}")

    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"[KOMUT HATA] tree.sync başarısız: {e}")

    if not check_timers.is_running():
        check_timers.start()
    print(f"[+] Bot aktif: {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_bot_token_here":
        print("[!] Hata: .env dosyasında DISCORD_TOKEN tanımlanmalı.")
    else:
        bot.run(TOKEN)

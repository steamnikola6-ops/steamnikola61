import os
import sys
import json
import time
import atexit
import datetime
import asyncio

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import discord
from discord import ui, ButtonStyle, Embed, Intents
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
NOTIFY_ROLE_ID = int(os.getenv("NOTIFY_ROLE_ID", 0))

if not TOKEN:
    print("❌ Missing DISCORD_TOKEN in .env file")
    raise SystemExit(1)

if CHANNEL_ID == 0:
    print("❌ Missing CHANNEL_ID in .env file")
    raise SystemExit(1)

DATA_FILE = "roster_data.json"
ROSTER_TITLE_MARKER = "Informal Roster (First 10 Only)"
ROSTER_DEBOUNCE_SEC = 90
LOCAL_TZ = pytz.timezone("Europe/Belgrade")
scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)
_roster_send_lock = asyncio.Lock()
_startup_lock = asyncio.Lock()
_last_roster_publish_ts = 0.0
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
MESSAGE_DELETE_DELAY = 10


def _pid_is_running(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_single_bot_instance():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if _pid_is_running(old_pid):
                print(f"❌ Bot je VEC upaljen (PID {old_pid}). Zatvori drugi crni prozor!")
                raise SystemExit(1)
        except ValueError:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    def _remove_lock():
        if os.path.exists(LOCK_FILE):
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass

    atexit.register(_remove_lock)


ROSTER_MINUTES_BEFORE = int(os.getenv("ROSTER_MINUTES_BEFORE", "15"))


def parse_event_times(value: str) -> list[tuple[int, int]]:
    times: list[tuple[int, int]] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        hour_str, minute_str = part.split(":", 1)
        times.append((int(hour_str), int(minute_str)))
    return times


def roster_time_before_event(hour: int, minute: int, minutes_before: int) -> tuple[int, int]:
    base = datetime.datetime(2000, 1, 1, hour, minute)
    earlier = base - datetime.timedelta(minutes=minutes_before)
    return earlier.hour, earlier.minute


def format_hm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def event_slot_key(when: datetime.datetime, event_hour: int, event_minute: int) -> str:
    return f"{when.date().isoformat()}_{format_hm(event_hour, event_minute)}"


class RosterView(ui.View):
    def __init__(self, bot_instance=None):
        super().__init__(timeout=None)
        self.slots = [None] * 10
        self.subs = [None] * 5
        self.lock = asyncio.Lock()
        self.locked = False
        self.bot_instance = bot_instance
        self.message = None
        self.message_id = None
        self.event_starts_at: str | None = None
        self.notify_line: str | None = None
        self.user_stats = {}
        self.load_data()
        self._setup_buttons()

    def record_user_participation(self, user_id: int, user_name: str):
        """Zabeleži učešće korisnika"""
        user_id_str = str(user_id)
        if user_id_str not in self.user_stats:
            self.user_stats[user_id_str] = {"name": user_name, "events": 0, "last_event": None}
        self.user_stats[user_id_str]["events"] += 1
        self.user_stats[user_id_str]["last_event"] = datetime.datetime.now(LOCAL_TZ).isoformat()

    def get_user_achievements(self, events_played: int) -> str:
        """Vrati ikonice za dostignuća"""
        achievements = []
        
        if events_played >= 1:
            achievements.append("⚔️")
        if events_played >= 5:
            achievements.append("🏆")
        if events_played >= 10:
            achievements.append("🐱")
        if events_played >= 20:
            achievements.append("👑")
        if events_played >= 50:
            achievements.append("💎")
        if events_played >= 100:
            achievements.append("🌟")
        
        return " ".join(achievements) if achievements else "—"

    def _setup_buttons(self):
        signup = ui.Button(label="✅ Join", style=ButtonStyle.success, custom_id="join", row=0)
        leave = ui.Button(label="❌ Leave", style=ButtonStyle.danger, custom_id="leave", row=0)

        async def signup_cb(interaction: discord.Interaction):
            try:
                await interaction.response.defer(ephemeral=True)
                async with self.lock:
                    if self.locked:
                        try:
                            await interaction.followup.send("🔒 Roster je trenutno zaključan.", ephemeral=True)
                        except discord.NotFound:
                            print("⚠️ Poruka nije pronađena (već obrisana)")
                        return

                    user = interaction.user
                    in_slots = any(s and s["id"] == user.id for s in self.slots)
                    in_subs = any(s and s["id"] == user.id for s in self.subs)

                    if in_slots or in_subs:
                        try:
                            await interaction.followup.send("❌ Već si prijavljen.", ephemeral=True)
                        except discord.NotFound:
                            pass
                        return

                    user_data = {"id": user.id, "name": user.display_name}

                    if None in self.slots:
                        idx = self.slots.index(None)
                        self.slots[idx] = user_data
                        self.record_user_participation(user.id, user.display_name)
                        self.save_data()
                        await self.message.edit(embed=self.build_embed(), view=self)
                        try:
                            await interaction.followup.send(
                                f"✅ Prijavljen si u roster na poziciji **{idx + 1}**.",
                                ephemeral=True
                            )
                        except discord.NotFound:
                            pass
                        print(f"✓ {user.display_name} se prijavio na poziciju {idx + 1}")
                        return

                    if None in self.subs:
                        idx = self.subs.index(None)
                        self.subs[idx] = user_data
                        self.record_user_participation(user.id, user.display_name)
                        self.save_data()
                        await self.message.edit(embed=self.build_embed(), view=self)
                        try:
                            await interaction.followup.send(
                                f"⚠️ Glavni roster je pun, prijavljen si kao sub na poziciji **{idx + 1}**.",
                                ephemeral=True
                            )
                        except discord.NotFound:
                            pass
                        print(f"✓ {user.display_name} se prijavio kao sub na poziciju {idx + 1}")
                        return

                    try:
                        await interaction.followup.send(
                            "❌ Nema slobodnih mesta ni u subs rosteru.",
                            ephemeral=True
                        )
                    except discord.NotFound:
                        pass
            except Exception as e:
                print(f"❌ Greška u signup_cb: {e}")
                try:
                    await interaction.followup.send(f"❌ Greška pri prijavi: {str(e)}", ephemeral=True)
                except Exception:
                    pass

        async def unregister_cb(interaction: discord.Interaction):
            try:
                await interaction.response.defer(ephemeral=True)
                async with self.lock:
                    user = interaction.user
                    
                    slot_idx = next((i for i, s in enumerate(self.slots) if s and s["id"] == user.id), None)
                    if slot_idx is not None:
                        self.slots[slot_idx] = None
                        promoted = self.promote_first_sub()
                        if promoted:
                            self.slots[slot_idx] = promoted
                        self.save_data()
                        await self.message.edit(embed=self.build_embed(), view=self)
                        try:
                            if promoted:
                                await interaction.followup.send(
                                    f"✅ Odjavio si se, a {promoted['name']} je prebačen iz subs roster-a.",
                                    ephemeral=True
                                )
                            else:
                                await interaction.followup.send("✅ Odjavio si se iz glavnog rostera.", ephemeral=True)
                        except discord.NotFound:
                            pass
                        return

                    sub_idx = next((i for i, s in enumerate(self.subs) if s and s["id"] == user.id), None)
                    if sub_idx is not None:
                        self.subs[sub_idx] = None
                        self.save_data()
                        await self.message.edit(embed=self.build_embed(), view=self)
                        try:
                            await interaction.followup.send("✅ Odjavio si se iz subs roster-a.", ephemeral=True)
                        except discord.NotFound:
                            pass
                        return

                    try:
                        await interaction.followup.send(
                            "❌ Nisi prijavljen ni u roster ni u subs roster.",
                            ephemeral=True
                        )
                    except discord.NotFound:
                        pass
            except Exception as e:
                print(f"❌ Greška u unregister_cb: {e}")
                try:
                    await interaction.followup.send("Došlo je do greške pri odjavi.", ephemeral=True)
                except Exception:
                    pass

        signup.callback = signup_cb
        leave.callback = unregister_cb

        self.add_item(signup)
        self.add_item(leave)

    def reset_roster(self):
        self.slots = [None] * 10
        self.subs = [None] * 5
        self.locked = False

    def promote_first_sub(self):
        for i, data in enumerate(self.subs):
            if data is not None:
                self.subs[i] = None
                return data
        return None

    def save_data(self, extra: dict | None = None):
        try:
            data = {
                "slots": self.slots,
                "subs": self.subs,
                "locked": self.locked,
                "message_id": self.message.id if self.message else self.message_id,
                "channel_id": CHANNEL_ID,
                "event_starts_at": self.event_starts_at,
                "timestamp": datetime.datetime.now(LOCAL_TZ).isoformat(),
                "user_stats": self.user_stats,
            }
            if extra:
                data.update(extra)
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                for key in ("last_scheduled_roster",):
                    if key in old and key not in (extra or {}):
                        data[key] = old[key]
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ Greška pri čuvanju podataka: {e}")

    def load_data(self):
        self._last_scheduled_roster = None
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                    loaded_slots = data.get("slots", [None] * 10)
                    self.slots = []
                    for s in loaded_slots:
                        if isinstance(s, dict):
                            self.slots.append(s)
                        elif isinstance(s, str):
                            self.slots.append({"id": 0, "name": s})
                        else:
                            self.slots.append(None)
                    while len(self.slots) < 10:
                        self.slots.append(None)

                    loaded_subs = data.get("subs", [None] * 5)
                    self.subs = []
                    for s in loaded_subs:
                        if isinstance(s, dict):
                            self.subs.append(s)
                        elif isinstance(s, str):
                            self.subs.append({"id": 0, "name": s})
                        else:
                            self.subs.append(None)
                    while len(self.subs) < 5:
                        self.subs.append(None)

                    self.locked = data.get("locked", False)
                    self.message_id = data.get("message_id")
                    saved_channel = data.get("channel_id")
                    if saved_channel and saved_channel != CHANNEL_ID:
                        self.message_id = None
                        print("⚠️ Promenjen kanal u .env — stari roster ID se ignoriše.")
                    self.event_starts_at = data.get("event_starts_at")
                    self._last_scheduled_roster = data.get("last_scheduled_roster")
                    self.user_stats = data.get("user_stats", {})
                    print(f"✓ Roster podaci učitani iz {DATA_FILE}")
            else:
                print(f"⚠️ {DATA_FILE} ne postoji, počinjemo sa praznim rosterom")
        except Exception as e:
            print(f"❌ Greška pri učitavanju podataka: {e}")
            self.slots = [None] * 10
            self.subs = [None] * 5
            self.locked = False
            self.message_id = None

    def build_embed(self) -> Embed:
        if self.event_starts_at:
            description = f"Event počinje u **{self.event_starts_at}** (Beograd). Prijavite se ispod!"
        else:
            description = "Prijavite se za event klikom na dugme ispod!"
        if self.notify_line:
            description = f"{self.notify_line}\n\n{description}"
        
        embed = Embed(
            title="✅ Informal Roster (First 10 Only)",
            description=description,
            color=0x00CC66,
        )
        
        lines = []
        lines.append("**Main Roster (1-10)**")
        for i, item in enumerate(self.slots, start=1):
            if item:
                lines.append(f"{i}. @{item['name']} | {item.get('id', '')}")
            else:
                lines.append(f"{i}.")
        
        embed.add_field(name="📋 Lista učesnika:", value="\n".join(lines), inline=False)
        filled = len([slot for slot in self.slots if slot is not None])
        
        created_time = datetime.datetime.now(LOCAL_TZ).strftime('%d %b %Y - %H:%M UTC')
        embed.add_field(
            name="📊 Status:", 
            value=f"✅ Open • Created: {created_time}", 
            inline=False
        )

        embed.set_footer(text="🔗 Join | ❌ Leave • Status: Open")
        return embed

    def attach_message(self, message: discord.Message):
        self.message = message
        self.message_id = message.id
        self.save_data()


intents = Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.current_view: RosterView | None = None
bot.current_message_id: int | None = None


def get_last_scheduled_roster() -> str | None:
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("last_scheduled_roster")
    except Exception:
        pass
    return None


def save_last_scheduled_roster(slot_key: str):
    try:
        data = {}
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["last_scheduled_roster"] = slot_key
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ Greška pri čuvanju last_scheduled_roster: {e}")


async def get_roster_channel():
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(CHANNEL_ID)
        return channel
    except Exception as exc:
        print(f"❌ Greška pri preuzimanju kanala {CHANNEL_ID}: {exc}")
        return None


def notification_ping() -> str:
    if NOTIFY_ROLE_ID:
        return f"<@&{NOTIFY_ROLE_ID}> "
    return "@here "


def build_notification_text(event_label: str, roster_label: str, *, late: bool = False) -> str:
    if late:
        return (
            f"{notification_ping()}⚠️ **KASNO:** Bot nije bio upaljen u **{roster_label}**. "
            f"Roster za event u **{event_label}** — prijavite se ispod!"
        )
    return (
        f"{notification_ping()}📋 **Roster je otvoren!** Event u **{event_label}** "
        f"(za {ROSTER_MINUTES_BEFORE} min). Prijavite se ispod!"
    )


async def active_roster_message_exists(channel) -> bool:
    if not bot.current_message_id:
        return False
    try:
        await channel.fetch_message(bot.current_message_id)
        return True
    except Exception:
        return False


async def roster_already_sent_for_slot(channel, slot_key: str) -> bool:
    if not await active_roster_message_exists(channel):
        return False
    return get_last_scheduled_roster() == slot_key


async def find_existing_roster_message(channel):
    if not bot.user:
        return None
    try:
        async for msg in channel.history(limit=30):
            if msg.author.id != bot.user.id:
                continue
            if not msg.embeds or not msg.embeds[0].title:
                continue
            if ROSTER_TITLE_MARKER in msg.embeds[0].title:
                return msg
    except Exception:
        pass
    return None


async def cleanup_all_bot_rosters(channel) -> int:
    deleted = 0
    if not bot.user:
        return deleted
    try:
        async for msg in channel.history(limit=30):
            if msg.author.id != bot.user.id:
                continue
            if not msg.embeds or not msg.embeds[0].title:
                continue
            if ROSTER_TITLE_MARKER not in msg.embeds[0].title:
                continue
            try:
                await msg.delete()
                deleted += 1
            except Exception:
                pass
    except Exception as e:
        print(f"❌ Greška pri čišćenju starih roster poruka: {e}")
    bot.current_message_id = None
    return deleted


def create_fresh_roster_view() -> RosterView:
    view = RosterView(bot)
    bot.current_view = view
    bot.add_view(view)
    return view


async def restore_roster_message() -> bool:
    view = bot.current_view
    if view is None or not view.message_id:
        return False

    channel = await get_roster_channel()
    if channel is None:
        return False

    try:
        message = await channel.fetch_message(view.message_id)
        view.attach_message(message)
        await message.edit(embed=view.build_embed(), view=view)
        bot.current_message_id = message.id
        print(f"✓ Roster poruka ponovo povezana (ID: {message.id})")
        return True
    except discord.NotFound:
        view.message_id = None
        bot.current_message_id = None
        print("⚠️ Stara roster poruka nije u ovom kanalu — biće novi roster.")
        return False
    except Exception as e:
        print(f"❌ Greška pri povezivanju roster poruke: {e}")
        return False


async def publish_roster(
    channel,
    *,
    event_hour: int | None = None,
    event_minute: int | None = None,
    late: bool = False,
    manual: bool = False,
    slot_key: str | None = None,
    force_new: bool = False,
) -> bool:
    global _last_roster_publish_ts
    async with _roster_send_lock:
        now_ts = time.time()
        if (now_ts - _last_roster_publish_ts) < ROSTER_DEBOUNCE_SEC:
            print("✓ Preskačem dupli roster (previše brzo ponovo).")
            return False

        if slot_key and not late and not manual:
            if await roster_already_sent_for_slot(channel, slot_key):
                print("✓ Roster za ovaj termin već postoji.")
                return False

        existing = None if force_new else await find_existing_roster_message(channel)

        view = create_fresh_roster_view()
        if force_new or existing is None:
            view.reset_roster()

        if event_hour is not None and event_minute is not None:
            event_label = format_hm(event_hour, event_minute)
            roster_hour, roster_minute = roster_time_before_event(
                event_hour, event_minute, ROSTER_MINUTES_BEFORE
            )
            roster_label = format_hm(roster_hour, roster_minute)
            view.event_starts_at = event_label
            view.notify_line = build_notification_text(event_label, roster_label, late=late)

        if existing:
            try:
                await existing.edit(
                    embed=view.build_embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions(everyone=True, roles=True),
                )
                view.message = existing
                view.message_id = existing.id
                bot.current_message_id = existing.id
                if slot_key:
                    save_last_scheduled_roster(slot_key)
                    view.save_data(extra={"last_scheduled_roster": slot_key})
                else:
                    view.save_data()
                _last_roster_publish_ts = now_ts
                print("✓ Postojeći roster osvežen (nema nove poruke).")
                return True
            except Exception as e:
                print(f"⚠️ Nije moguće izmeniti stari roster: {e}")

        removed = await cleanup_all_bot_rosters(channel)
        if removed:
            print(f"✓ Obrisano {removed} starih roster poruka.")

        try:
            message = await channel.send(
                embed=view.build_embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True),
            )
        except Exception as e:
            print(f"❌ Greška pri slanju roster poruke: {e}")
            return False

        view.attach_message(message)
        bot.current_message_id = message.id
        if slot_key:
            save_last_scheduled_roster(slot_key)
            view.save_data(extra={"last_scheduled_roster": slot_key})
        else:
            view.save_data()

        _last_roster_publish_ts = now_ts
        label = format_hm(event_hour, event_minute) if event_hour is not None else "ručno"
        print(f"✓ Jedan roster poslat ({label})")
        return True


async def send_scheduled_roster(event_hour: int, event_minute: int, *, late: bool = False):
    channel = await get_roster_channel()
    if channel is None:
        print("❌ ERROR: Kanal nije pronađen!")
        return

    now = datetime.datetime.now(LOCAL_TZ)
    slot_key = event_slot_key(now, event_hour, event_minute)
    await publish_roster(
        channel,
        event_hour=event_hour,
        event_minute=event_minute,
        late=late,
        slot_key=slot_key,
        force_new=True,
    )


async def check_missed_roster_on_startup():
    if getattr(bot, "_missed_check_done", False):
        return
    bot._missed_check_done = True

    channel = await get_roster_channel()
    if channel is None:
        return

    now = datetime.datetime.now(LOCAL_TZ)

    # Proveri za sve sate sa :40
    for hour in range(24):
        event_hour = hour
        event_minute = 40
        roster_hour, roster_minute = roster_time_before_event(
            event_hour, event_minute, ROSTER_MINUTES_BEFORE
        )
        roster_dt = now.replace(hour=roster_hour, minute=roster_minute, second=0, microsecond=0)
        event_dt = now.replace(hour=event_hour, minute=event_minute, second=0, microsecond=0)
        event_label = format_hm(event_hour, event_minute)

        if roster_dt <= now < event_dt:
            if await find_existing_roster_message(channel):
                print(f"✓ Roster za {event_label} već postoji u kanalu.")
                return
            print(
                f"⚠️ Propusten auto-roster za {event_label}. "
                f"Ukucaj !roster JEDNOM u kanalu (ne pali bot dvaput)."
            )
            return


def setup_scheduler():
    print("🕐 Postavljam scheduler za sve sate...")
    
    # Generiši sve sate sa :40 minutima
    for hour in range(24):
        event_hour = hour
        event_minute = 40
        
        roster_hour, roster_minute = roster_time_before_event(
            event_hour, event_minute, ROSTER_MINUTES_BEFORE
        )
        
        scheduler.add_job(
            send_scheduled_roster,
            CronTrigger(hour=roster_hour, minute=roster_minute, timezone=LOCAL_TZ),
            args=[event_hour, event_minute],
            id=f"roster_{event_hour}_{event_minute}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
    
    print(f"✓ Scheduler: Roster se otvara AUTOMATSKI svakih sat na :25 (15 min pre :40)")
    print(f"✓ Timezone: {LOCAL_TZ}")


@bot.event
async def setup_hook():
    if bot.current_view is None:
        bot.current_view = RosterView(bot)
    bot.add_view(bot.current_view)
    bot.current_message_id = bot.current_view.message_id

    if not scheduler.running:
        setup_scheduler()
        scheduler.start()


@bot.event
async def on_ready():
    print(f"✓ Bot je online kao: {bot.user} (ID: {bot.user.id})")

    channel = await get_roster_channel()
    if channel is None:
        print("❌ WARNING: CHANNEL_ID nije validan!")
    else:
        print(f"✓ Kanal pronađen: {channel.name}")

    async with _startup_lock:
        first_connect = not getattr(bot, "_startup_done", False)
        if not first_connect:
            if bot.current_message_id:
                await restore_roster_message()
            return

        bot._startup_done = True

        if bot.current_message_id:
            await restore_roster_message()
        else:
            existing = await find_existing_roster_message(channel) if channel else None
            if existing:
                bot.current_view.message = existing
                bot.current_view.message_id = existing.id
                bot.current_message_id = existing.id
                print(f"✓ Pronađen postojeći roster u kanalu (ID: {existing.id})")

        await check_missed_roster_on_startup()


@bot.command()
async def roster(ctx: commands.Context):
    try:
        if ctx.channel.id != CHANNEL_ID:
            msg = await ctx.send(
                f"❌ Roster idu samo u kanal <#{CHANNEL_ID}>. "
                f"Piši `!roster` tamo, ne ovde.",
                delete_after=15,
            )
            return

        existing = await find_existing_roster_message(ctx.channel)
        if existing:
            ok = await publish_roster(ctx.channel, manual=True)
            if ok:
                try:
                    msg = await ctx.send("✅ Roster je osvežen (ista poruka, nema duplikata).", delete_after=8)
                except discord.NotFound:
                    pass
            return

        ok = await publish_roster(ctx.channel, manual=True)
        if not ok:
            try:
                msg = await ctx.send("⚠️ Sačekaj malo pa probaj ponovo.", delete_after=8)
            except discord.NotFound:
                pass
            return
        print(f"✓ Roster kreiran komandom u #{ctx.channel.name}")
    except Exception as e:
        print(f"❌ Greška u roster komandi: {e}")
        try:
            msg = await ctx.send("❌ Došlo je do greške pri pravljenju rostera.")
        except discord.NotFound:
            pass


@bot.command()
@commands.check_any(
    commands.has_permissions(administrator=True),
    commands.has_permissions(manage_guild=True),
)
async def prebaci(ctx: commands.Context, member: discord.Member):
    try:
        view = bot.current_view
        if view is None:
            try:
                msg = await ctx.send("❌ Nema aktivnog rostera za upravljanje.")
            except discord.NotFound:
                pass
            return

        async with view.lock:
            sub_idx = next((i for i, s in enumerate(view.subs) if s and s["id"] == member.id), None)
            if sub_idx is None:
                try:
                    msg = await ctx.send(f"❌ {member.mention} nije u subs roster-u.")
                except discord.NotFound:
                    pass
                return

            if None not in view.slots:
                try:
                    msg = await ctx.send("❌ Glavni roster je pun, prvo oslobodi mesto.")
                except discord.NotFound:
                    pass
                return

            slot_idx = view.slots.index(None)
            sub_data = view.subs[sub_idx]
            view.subs[sub_idx] = None
            view.slots[slot_idx] = sub_data
            view.save_data()
            if view.message:
                await view.message.edit(embed=view.build_embed(), view=view)
            try:
                msg = await ctx.send(f"✅ {member.mention} je prebačen iz subs roster-a u glavni roster.")
            except discord.NotFound:
                pass
    except Exception as e:
        print(f"❌ Greška u prebaci komandi: {e}")
        try:
            msg = await ctx.send("❌ Došlo je do greške pri premještanju korisnika.")
        except discord.NotFound:
            pass


@bot.command()
@commands.check_any(
    commands.has_permissions(administrator=True),
    commands.has_permissions(manage_guild=True),
)
async def makni(ctx: commands.Context, member: discord.Member):
    try:
        view = bot.current_view
        if view is None:
            try:
                msg = await ctx.send("❌ Nema aktivnog rostera za upravljanje.")
            except discord.NotFound:
                pass
            return

        async with view.lock:
            slot_idx = next((i for i, s in enumerate(view.slots) if s and s["id"] == member.id), None)
            if slot_idx is not None:
                view.slots[slot_idx] = None
                promoted = view.promote_first_sub()
                if promoted:
                    view.slots[slot_idx] = promoted
                view.save_data()
                if view.message:
                    await view.message.edit(embed=view.build_embed(), view=view)
                try:
                    if promoted:
                        msg = await ctx.send(f"✅ {member.mention} je uklonjen, {promoted['name']} je promovisan iz subs roster-a.")
                    else:
                        msg = await ctx.send(f"✅ {member.mention} je uklonjen iz glavnog rostera.")
                except discord.NotFound:
                    pass
                return

            sub_idx = next((i for i, s in enumerate(view.subs) if s and s["id"] == member.id), None)
            if sub_idx is not None:
                view.subs[sub_idx] = None
                view.save_data()
                if view.message:
                    await view.message.edit(embed=view.build_embed(), view=view)
                try:
                    msg = await ctx.send(f"✅ {member.mention} je uklonjen iz subs roster-a.")
                except discord.NotFound:
                    pass
                return

            try:
                msg = await ctx.send(f"❌ {member.mention} nije ni u glavnom rosteru ni u subs rosteru.")
            except discord.NotFound:
                pass
    except Exception as e:
        print(f"❌ Greška u makni komandi: {e}")
        try:
            msg = await ctx.send("❌ Došlo je do greške pri uklanjanju korisnika.")
        except discord.NotFound:
            pass


@bot.command()
async def profile(ctx: commands.Context, member: discord.Member = None):
    """Prikaži profil korisnika sa statistikom"""
    try:
        if member is None:
            member = ctx.author
        
        view = bot.current_view
        if view is None:
            try:
                await ctx.send("❌ Nema aktivnog rostera.")
            except discord.NotFound:
                pass
            return

        user_id_str = str(member.id)
        stats = view.user_stats.get(user_id_str, {})
        
        events_played = stats.get("events", 0)
        
        in_main = any(s and s.get("id") == member.id for s in view.slots)
        in_subs = any(s and s.get("id") == member.id for s in view.subs)
        
        position = None
        if in_main:
            position = next(i for i, s in enumerate(view.slots) if s and s.get("id") == member.id) + 1
        elif in_subs:
            position = next(i for i, s in enumerate(view.subs) if s and s.get("id") == member.id) + 1
        
        embed = Embed(
            title=f"Profil: {member.display_name}",
            description=f"ID: {member.id}",
            color=0xFF0000 if in_main else (0xFFD700 if in_subs else 0x808080)
        )
        
        embed.set_thumbnail(url=member.display_avatar.url)
        
        if in_main:
            status = f"🟢 U glavnom rosteru (pozicija: **{position}**)"
        elif in_subs:
            status = f"🟡 Sub roster (pozicija: **{position}**)"
        else:
            status = "⚫ Nije prijavljen"
        
        embed.add_field(name="📊 Status", value=status, inline=False)
        
        if events_played == 0:
            event_text = "Još nikada nije igrao"
        elif events_played == 1:
            event_text = "1 event"
        else:
            event_text = f"{events_played} eventa"
        
        embed.add_field(name="📈 Statistika", value=f"**{event_text}** ⚔️", inline=True)
        
        achievements = view.get_user_achievements(events_played)
        embed.add_field(name="🎖️ Dostignuća", value=achievements, inline=True)
        
        if stats.get("last_event"):
            try:
                last_event_dt = datetime.datetime.fromisoformat(stats["last_event"])
                last_event_str = last_event_dt.strftime("%d.%m.%Y %H:%M")
                embed.add_field(name="⏰ Poslednja aktivnost", value=last_event_str, inline=False)
            except:
                pass
        
        level = "🥚 Početnik" if events_played == 0 else \
                "🐣 Novajlija" if events_played < 5 else \
                "🐤 Učesnik" if events_played < 10 else \
                "🦅 Veteran" if events_played < 20 else \
                "🐉 Legenda" if events_played < 50 else \
                "👑 Imperij!"
        
        embed.add_field(name="🏅 Nivo", value=level, inline=True)
        
        embed.set_footer(text=f"Korisnik prikuplja iskustvo kroz učešće u eventima!")
        
        try:
            await ctx.send(embed=embed)
        except discord.NotFound:
            pass
        
    except Exception as e:
        print(f"❌ Greška u profile komandi: {e}")
        try:
            await ctx.send("❌ Greška pri prikazivanju profila.")
        except discord.NotFound:
            pass


async def main():
    try:
        print("🚀 Pokretanje bota...")
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        print("\n🛑 Bot je zaustavljen od strane korisnika.")
    except Exception as e:
        print(f"❌ Kritična greška pri pokretanju bota: {e}")


if __name__ == "__main__":
    ensure_single_bot_instance()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot je zaustavljen.")
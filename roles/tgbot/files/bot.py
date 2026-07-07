#!/usr/bin/env python3
# VPN Telegram Bot
#
# 1) Собирает дельта-трафик из xray Stats API каждые N секунд, хранит в SQLite,
#    агрегирует по периодам, отвечает на команды.
# 2) Управляет пользователями VPN: /adduser, /kickuser — правит живой
#    /etc/xray/config.json и перезапускает xray, без полного Ansible-редеплоя.
# 3) Отдаёт subscription-ссылки по HTTP: GET /sub/<short_id> -> base64(vless://...)
#
# Источник правды о пользователях, добавленных через бота — JSON-файл
# clients_meta.json рядом с ботом. При первом запуске он строится из
# текущего config.json (clients и shortIds сопоставляются по индексу —
# именно в таком порядке их генерит Ansible-шаблон).

import asyncio
import base64
import json
import logging
import os
import secrets
import subprocess
import uuid as uuid_lib
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import BotCommand, Message
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Config ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TG_TOKEN         = os.environ["TG_TOKEN"]
TG_ADMIN_IDS     = {int(x) for x in os.environ["TG_ADMIN_IDS"].split(",")}
XRAY_BIN         = os.environ.get("XRAY_BIN",          "/usr/local/bin/xray")
XRAY_API         = os.environ.get("XRAY_API",          "127.0.0.1:10085")
XRAY_CONFIG_PATH = Path(os.environ.get("XRAY_CONFIG_PATH", "/etc/xray/config.json"))
XRAY_PUBLIC_KEY  = os.environ["XRAY_PUBLIC_KEY"]
XRAY_SNI         = os.environ["XRAY_SNI"]
XRAY_FINGERPRINT = os.environ.get("XRAY_FINGERPRINT", "firefox")
XRAY_PORT        = os.environ.get("XRAY_PORT", "443")
VPS_HOST         = os.environ["VPS_HOST"]
SUB_PORT         = int(os.environ.get("SUB_PORT", "8080"))
SUB_BIND         = os.environ.get("SUB_BIND", "127.0.0.1")
SUB_PUBLIC_URL   = os.environ.get("SUB_PUBLIC_URL", f"http://{os.environ['VPS_HOST']}:{SUB_PORT}")
DB_PATH          = Path(os.environ.get("DB_PATH",      "/opt/tgbot/stats.db"))
COLLECT_INTERVAL = int(os.environ.get("COLLECT_INTERVAL", "60"))

TGBOT_DIR    = Path(os.environ.get("DB_PATH", "/opt/tgbot/stats.db")).parent
META_PATH    = TGBOT_DIR / "clients_meta.json"
PENDING_PATH = TGBOT_DIR / "pending_users.txt"

bot = Bot(token=TG_TOKEN)
dp  = Dispatcher()


# ── Admin filter ──────────────────────────────────────────────────────────────
class IsAdmin(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in TG_ADMIN_IDS


# ── Database (трафик) ──────────────────────────────────────────────────────────
import sqlite3
from contextlib import closing

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    with closing(get_db()) as conn:
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS traffic (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    user     TEXT    NOT NULL,
                    uplink   INTEGER NOT NULL DEFAULT 0,
                    downlink INTEGER NOT NULL DEFAULT 0,
                    ts       TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user ON traffic(user);
                CREATE INDEX IF NOT EXISTS idx_ts   ON traffic(ts);
            """)
    log.info("DB ready: %s", DB_PATH)


# ── xray Stats API (трафик) ────────────────────────────────────────────────────
def xray_query(reset: bool = True) -> dict:
    cmd = [XRAY_BIN, "api", "statsquery", f"--server={XRAY_API}"]
    if reset:
        cmd.append("-reset")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        payload = json.loads(r.stdout or "{}")
    except Exception as exc:
        log.warning("xray api error: %s", exc)
        return {}

    result = {}
    for stat in payload.get("stat", []):
        parts = stat["name"].split(">>>")
        if len(parts) != 4 or parts[0] != "user" or parts[2] != "traffic":
            continue
        user  = parts[1]
        key   = parts[3]
        value = int(stat.get("value") or 0)
        result.setdefault(user, {"uplink": 0, "downlink": 0})[key] = value

    return result


async def collect() -> None:
    stats = xray_query(reset=True)
    if not stats:
        return
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (user, t["uplink"], t["downlink"], now)
        for user, t in stats.items()
        if t["uplink"] or t["downlink"]
    ]
    if rows:
        with closing(get_db()) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO traffic (user, uplink, downlink, ts) VALUES (?,?,?,?)",
                    rows,
                )
        log.debug("Saved %d traffic rows", len(rows))


# ── Helpers (формат) ───────────────────────────────────────────────────────────
def human(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def query_traffic(since: datetime) -> list:
    with closing(get_db()) as conn:
        return conn.execute(
            """
            SELECT user,
                   COALESCE(SUM(uplink),           0) AS up,
                   COALESCE(SUM(downlink),          0) AS down,
                   COALESCE(SUM(uplink+downlink),   0) AS total
            FROM   traffic
            WHERE  ts >= ?
            GROUP  BY user
            ORDER  BY total DESC
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()


def fmt_table(rows: list, label: str) -> str:
    if not rows:
        return f"📭 Нет данных за {label}"
    lines = [f"📊 *Трафик за {label}*\n"]
    for r in rows:
        lines.append(
            f"👤 `{r['user']}`\n"
            f"   ↑ {human(r['up'])}   ↓ {human(r['down'])}   │   📦 {human(r['total'])}"
        )
    return "\n".join(lines)


# ── Управление пользователями VPN ──────────────────────────────────────────────
class UserExistsError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


def _load_meta() -> dict:
    """
    {name: {"uuid": "...", "short_id": "..."}}
    При первом запуске строится из текущего config.json: clients[i] <-> shortIds[i]
    (именно так их кладёт Ansible-шаблон — по порядку, индекс к индексу).
    """
    if META_PATH.exists():
        return json.loads(META_PATH.read_text())

    config  = json.loads(XRAY_CONFIG_PATH.read_text())
    inbound = config["inbounds"][0]
    clients   = inbound["settings"]["clients"]
    short_ids = inbound["streamSettings"]["realitySettings"]["shortIds"]

    meta = {}
    for client, short_id in zip(clients, short_ids):
        meta[client["email"]] = {"uuid": client["id"], "short_id": short_id}

    _save_meta(meta)
    return meta


def _save_meta(meta: dict) -> None:
    META_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def _apply_meta_to_config(meta: dict) -> None:
    """Правит ТОЛЬКО секции clients/shortIds в живом config.json, остальное не трогает."""
    config  = json.loads(XRAY_CONFIG_PATH.read_text())
    inbound = config["inbounds"][0]

    inbound["settings"]["clients"] = [
        {
            "id":    data["uuid"],
            "email": name,
            "flow":  "xtls-rprx-vision",
            "level": 0,
        }
        for name, data in meta.items()
    ]
    inbound["streamSettings"]["realitySettings"]["shortIds"] = [
        data["short_id"] for data in meta.values()
    ]

    # Пишем напрямую (без tempfile+rename — у tgbot нет права на запись
    # в саму директорию /etc/xray, только в сам файл через группу xray).
    XRAY_CONFIG_PATH.write_text(json.dumps(config, indent=2))

    result = subprocess.run(
        ["sudo", "/bin/systemctl", "restart", "xray"],
        capture_output=True, text=True,
    )
    log.info(
        "xray restart: rc=%r stdout=%r stderr=%r",
        result.returncode, result.stdout, result.stderr,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Не смог перезапустить xray: {result.stderr.strip()}")


def _append_pending(name: str, data: dict | None, removed: bool = False) -> None:
    """Накапливает снэпшот изменений для последующей ручной синхронизации в vault.yml."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    with PENDING_PATH.open("a") as f:
        if removed:
            f.write(f"\n# [{ts}] УДАЛЁН через бота: {name}\n")
            f.write(f"# -> удали соответствующий блок '{name}' из xray_clients в vault.yml\n")
        else:
            f.write(f"\n# [{ts}] ДОБАВЛЕН через бота: {name}\n")
            f.write("# -> добавь этот блок в xray_clients в vault.yml:\n")
            f.write(f'  - name:     "{name}"\n')
            f.write(f'    uuid:     "{data["uuid"]}"\n')
            f.write(f'    short_id: "{data["short_id"]}"\n')


def add_user(name: str) -> dict:
    meta = _load_meta()
    if name in meta:
        raise UserExistsError(name)

    data = {"uuid": str(uuid_lib.uuid4()), "short_id": secrets.token_hex(4)}
    meta[name] = data
    _save_meta(meta)
    _apply_meta_to_config(meta)
    _append_pending(name, data)
    return data


def kick_user(name: str) -> None:
    meta = _load_meta()
    if name not in meta:
        raise UserNotFoundError(name)

    del meta[name]
    _save_meta(meta)
    _apply_meta_to_config(meta)
    _append_pending(name, None, removed=True)


def list_users() -> dict:
    return _load_meta()


def build_vless_link(name: str, data: dict) -> str:
    return (
        f"vless://{data['uuid']}@{VPS_HOST}:{XRAY_PORT}"
        f"?encryption=none&flow=xtls-rprx-vision&security=reality"
        f"&sni={XRAY_SNI}&fp={XRAY_FINGERPRINT}&pbk={XRAY_PUBLIC_KEY}"
        f"&sid={data['short_id']}&type=tcp&headerType=none#{name}"
    )


# ── Telegram handlers ──────────────────────────────────────────────────────────
@dp.message(Command("start", "help"), IsAdmin())
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "🛡 *VPN Bot*\n\n"
        "*Статистика*\n"
        "/stats — трафик за 1 минуту\n"
        "/stats\\_hour — за час\n"
        "/stats\\_day — за сутки\n"
        "/stats\\_month — за 30 дней\n"
        "/users — живые счётчики xray\n"
        "/status — нагрузка сервера\n\n"
        "*Управление пользователями*\n"
        "/adduser <имя> — добавить пользователя\n"
        "/kickuser <имя> — удалить пользователя\n"
        "/listusers — список + subscription-ссылки\n",
        parse_mode="Markdown",
    )


@dp.message(Command("stats"), IsAdmin())
async def cmd_stats_min(msg: Message) -> None:
    rows = query_traffic(datetime.utcnow() - timedelta(minutes=1))
    await msg.answer(fmt_table(rows, "1 минуту"), parse_mode="Markdown")


@dp.message(Command("stats_hour"), IsAdmin())
async def cmd_stats_hour(msg: Message) -> None:
    rows = query_traffic(datetime.utcnow() - timedelta(hours=1))
    await msg.answer(fmt_table(rows, "1 час"), parse_mode="Markdown")


@dp.message(Command("stats_day"), IsAdmin())
async def cmd_stats_day(msg: Message) -> None:
    rows = query_traffic(datetime.utcnow() - timedelta(days=1))
    await msg.answer(fmt_table(rows, "сутки"), parse_mode="Markdown")


@dp.message(Command("stats_month"), IsAdmin())
async def cmd_stats_month(msg: Message) -> None:
    rows = query_traffic(datetime.utcnow() - timedelta(days=30))
    await msg.answer(fmt_table(rows, "30 дней"), parse_mode="Markdown")


@dp.message(Command("users"), IsAdmin())
async def cmd_users(msg: Message) -> None:
    stats = xray_query(reset=False)
    if not stats:
        await msg.answer("⚠️ Нет данных от xray — сервис запущен?")
        return
    lines = ["👥 *Счётчики xray (с момента старта)*\n"]
    for user, t in sorted(stats.items()):
        total = t["uplink"] + t["downlink"]
        lines.append(
            f"👤 `{user}`   │   📦 {human(total)}\n"
            f"   ↑ {human(t['uplink'])}   ↓ {human(t['downlink'])}"
        )
    await msg.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("status"), IsAdmin())
async def cmd_status(msg: Message) -> None:
    loop = asyncio.get_event_loop()
    cpu  = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=1))
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net  = psutil.net_io_counters()

    boot   = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot
    days   = uptime.days
    hours  = uptime.seconds // 3600
    mins   = (uptime.seconds % 3600) // 60

    await msg.answer(
        "🖥 *Состояние сервера*\n\n"
        f"⏱ Uptime: `{days}д {hours}ч {mins}м`\n"
        f"🔥 CPU:   `{cpu:.1f}%`\n"
        f"🧠 RAM:   `{mem.used // 2**20}/{mem.total // 2**20} MB` ({mem.percent:.0f}%)\n"
        f"💾 Disk:  `{disk.used / 2**30:.1f}/{disk.total / 2**30:.1f} GB` ({disk.percent:.0f}%)\n"
        f"🌐 Net    ↑ `{human(net.bytes_sent)}`   ↓ `{human(net.bytes_recv)}`",
        parse_mode="Markdown",
    )


@dp.message(Command("adduser"), IsAdmin())
async def cmd_adduser(msg: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name or " " in name:
        await msg.answer("Использование: `/adduser имя` (без пробелов)", parse_mode="Markdown")
        return
    try:
        data = add_user(name)
    except UserExistsError:
        await msg.answer(f"⚠️ Пользователь `{name}` уже существует.", parse_mode="Markdown")
        return
    except Exception as exc:
        log.exception("add_user failed")
        await msg.answer(f"❌ Ошибка: {exc}")
        return

    link = build_vless_link(name, data)
    sub_url = f"{SUB_PUBLIC_URL}/sub/{data['short_id']}"
    await msg.answer(
        f"✅ Пользователь `{name}` добавлен, xray перезапущен.\n\n"
        f"*Ссылка:*\n`{link}`\n\n"
        f"*Subscription URL:*\n`{sub_url}`\n\n"
        f"⚠️ Чтобы пользователь не пропал при следующем `make deploy`, "
        f"добавь этот блок в `vault.yml` (он уже сохранён на сервере в "
        f"`{PENDING_PATH}`):\n"
        f"```\n  - name:     \"{name}\"\n    uuid:     \"{data['uuid']}\"\n"
        f"    short_id: \"{data['short_id']}\"\n```",
        parse_mode="Markdown",
    )


@dp.message(Command("kickuser"), IsAdmin())
async def cmd_kickuser(msg: Message, command: CommandObject) -> None:
    name = (command.args or "").strip()
    if not name:
        await msg.answer("Использование: `/kickuser имя`", parse_mode="Markdown")
        return
    try:
        kick_user(name)
    except UserNotFoundError:
        await msg.answer(f"⚠️ Пользователь `{name}` не найден.", parse_mode="Markdown")
        return
    except Exception as exc:
        log.exception("kick_user failed")
        await msg.answer(f"❌ Ошибка: {exc}")
        return

    await msg.answer(
        f"🚫 Пользователь `{name}` удалён, xray перезапущен.\n\n"
        f"⚠️ Не забудь убрать блок `{name}` из `xray_clients` в `vault.yml` "
        f"(заметка уже добавлена в `{PENDING_PATH}` на сервере).",
        parse_mode="Markdown",
    )


@dp.message(Command("listusers"), IsAdmin())
async def cmd_listusers(msg: Message) -> None:
    meta = list_users()
    if not meta:
        await msg.answer("📭 Пользователей нет.")
        return
    lines = ["👥 *Пользователи VPN*\n"]
    for name, data in meta.items():
        link    = build_vless_link(name, data)
        sub_url = f"{SUB_PUBLIC_URL}/sub/{data['short_id']}"
        lines.append(
            f"👤 `{name}`\n"
            f"vless:\n`{link}`\n"
            f"sub:\n`{sub_url}`"
        )
    await msg.answer("\n\n".join(lines), parse_mode="Markdown")


# ── HTTP: subscription endpoint ────────────────────────────────────────────────
async def handle_sub(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    meta = _load_meta()

    match = next(
        ((name, data) for name, data in meta.items() if data["short_id"] == short_id),
        None,
    )
    if match is None:
        return web.Response(status=404, text="not found")

    name, data = match
    link = build_vless_link(name, data)
    encoded = base64.b64encode(link.encode()).decode()
    return web.Response(text=encoded, content_type="text/plain")


async def run_web_app() -> None:
    app = web.Application()
    app.router.add_get("/sub/{short_id}", handle_sub)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, SUB_BIND, SUB_PORT)
    await site.start()
    log.info("Subscription HTTP server listening on %s:%d", SUB_BIND, SUB_PORT)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    db_init()
    TGBOT_DIR.mkdir(parents=True, exist_ok=True)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(collect, "interval", seconds=COLLECT_INTERVAL)
    scheduler.start()
    log.info("Collector started (interval=%ds)", COLLECT_INTERVAL)

    await run_web_app()

    await bot.set_my_commands([
        BotCommand(command="help",        description="Список команд"),
        BotCommand(command="stats",       description="Трафик за 1 минуту"),
        BotCommand(command="stats_hour",  description="Трафик за час"),
        BotCommand(command="stats_day",   description="Трафик за сутки"),
        BotCommand(command="stats_month", description="Трафик за 30 дней"),
        BotCommand(command="users",       description="Живые счётчики xray"),
        BotCommand(command="status",      description="Нагрузка сервера"),
        BotCommand(command="adduser",     description="Добавить пользователя VPN"),
        BotCommand(command="kickuser",    description="Удалить пользователя VPN"),
        BotCommand(command="listusers",   description="Список пользователей + subscription"),
    ])

    log.info("Bot polling started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

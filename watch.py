# -*- coding: utf-8 -*-
"""TG 频道监控下载器:监听配置里的频道,按规则自动下载文件并归档。

用法:
  python watch.py            # 前台运行(首次会引导登录)
  python watch.py --once     # 只补拉增量历史后退出(测试用)
"""
import asyncio
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import UserAlreadyParticipantError, FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest

import FastTelethon

# 并行下载连接数控制:大文件最多 4 连接(避免高频连接触发风控)
MAX_CONNECTIONS = 4
CONN_FULL_SIZE = 50 * 1024 * 1024
PARALLEL_OVER = 5 * 1024 * 1024  # 超过 5MB 才用并行下载
BACKFILL = 20        # 首次运行时每个频道补拉多少条历史
MAX_CATCHUP = 500    # 断点续传单次最多补多少条(防止单轮过重)
MAX_RETRIES = 4      # 单文件下载总尝试次数(含 FloodWait 等待重试)

FAST_DL = 10 * 1024 * 1024  # 进度日志步长


def _conn_count(file_size, max_count=MAX_CONNECTIONS, full_size=CONN_FULL_SIZE):
    if file_size > full_size:
        return max_count
    return max(1, math.ceil(file_size / full_size * max_count))


FastTelethon.ParallelTransferrer._get_connection_count = staticmethod(_conn_count)

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
SESSION_PATH = BASE / "session"
DB_PATH = BASE / "db.sqlite3"
LOG_PATH = BASE / "watch.log"
LOCK_PORT = 59999  # 单实例互斥用的本地端口

BUSY = set()  # (channel, msg_id) 正在处理中,防止补拉与实时监听重复下载同一条


def _rotate_log():
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5 * 1024 * 1024:
            LOG_PATH.replace(BASE / "watch.old.log")
    except Exception:
        pass


def log(*a):
    line = datetime.now().strftime("[%m-%d %H:%M:%S]") + " " + " ".join(str(x) for x in a)
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw 无控制台时 stdout 为 None
    try:
        _rotate_log()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def acquire_single_instance():
    """绑定本地端口做互斥;已有实例在跑则返回 None。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", LOCK_PORT))
        return s
    except OSError:
        return None


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS files (
               id INTEGER PRIMARY KEY,
               channel TEXT, msg_id INTEGER, file_name TEXT,
               path TEXT, size INTEGER, mime TEXT, downloaded INTEGER,
               caption TEXT, msg_date TEXT, UNIQUE(channel, msg_id, file_name))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cursors (
               channel TEXT PRIMARY KEY, last_msg_id INTEGER)"""
    )
    return conn


def advance_cursor(conn, channel, msg_id):
    """把频道处理进度推进到 msg_id(只前进不后退)。"""
    cur = conn.execute("SELECT last_msg_id FROM cursors WHERE channel=?", (channel,)).fetchone()
    if cur and cur[0] >= msg_id:
        return
    conn.execute(
        """INSERT INTO cursors(channel, last_msg_id) VALUES(?, ?)
           ON CONFLICT(channel) DO UPDATE SET last_msg_id=excluded.last_msg_id""",
        (channel, msg_id),
    )
    conn.commit()


def match(entry, msg):
    """按频道配置判断一条消息是否要下载,返回 (name, size) 或 None。"""
    doc = msg.document
    if doc:
        name = ""
        for attr in doc.attributes:
            if getattr(attr, "file_name", None):
                name = attr.file_name
        size = doc.size or 0
    elif msg.photo:
        name, size = f"photo_{msg.id}.jpg", (msg.file.size if msg.file else 0)
    else:
        return None
    text = (msg.text or "") + " " + name
    ext = os.path.splitext(name)[1].lower()
    if entry.get("exts") and ext not in [e.lower() for e in entry["exts"]]:
        return None
    if entry.get("keywords") and not any(k.lower() in text.lower() for k in entry["keywords"]):
        return None
    if entry.get("exclude_keywords") and any(k.lower() in text.lower() for k in entry["exclude_keywords"]):
        return None
    if size > entry.get("max_size_mb", 2048) * 1024 * 1024:
        return None
    return (name, size)


async def do_download(client, msg, dest, size):
    """>5MB 走多连接并行下载(单次失败自动回退单连接);先写 .part,完成后原子改名。"""
    location = msg.document or msg.photo
    part = Path(str(dest) + ".part")
    mode = "parallel" if (size and size > PARALLEL_OVER and location is not None) else "single"
    progress = [0]
    try:
        for attempt in range(MAX_RETRIES):
            try:
                if mode == "parallel":
                    def prog(cur, total):
                        if cur - progress[0] > FAST_DL:
                            progress[0] = cur
                            log(f"  ... {cur/1048576:.0f}/{total/1048576:.0f}MB")
                    with open(part, "wb") as f:
                        await FastTelethon.download_file(client, location, f, progress_callback=prog)
                else:
                    await client.download_media(msg, file=str(part))
                part.replace(dest)
                return str(dest)
            except FloodWaitError as e:
                progress[0] = 0
                wait = min(e.seconds + 1, 300)
                log(f"限流,等待 {wait}s 后重试({attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(wait)
            except Exception as e:
                if mode == "parallel":
                    log(f"并行下载失败({e.__class__.__name__}: {e}),回退单连接")
                    mode = "single"
                    progress[0] = 0
                    continue
                raise
        raise RuntimeError(f"重试 {MAX_RETRIES} 次仍未成功")
    except BaseException:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise


async def save_file(client, msg, entry, name, size, cfg, conn):
    key = (entry["channel"], msg.id)
    if key in BUSY:
        return
    hit = conn.execute(
        "SELECT 1 FROM files WHERE channel=? AND msg_id=? AND file_name=?",
        (entry["channel"], msg.id, name)).fetchone()
    if hit:
        return  # 已处理过
    BUSY.add(key)
    try:
        ch = entry["channel"]
        # 文件名清洗:去掉 Windows 非法字符
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(". ") or f"file_{msg.id}"
        sub = re.sub(r"[^A-Za-z0-9._-]", "_", re.sub(r"^https?://t\.me/|^@", "", ch)) or "chan"
        dest_dir = Path(cfg["download_dir"]) / sub / datetime.now().strftime("%Y-%m")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        if dest.exists():  # 不同消息的同名文件:消息 ID 入名,避免覆盖
            stem, ext = os.path.splitext(safe)
            dest = dest_dir / f"{stem}_{msg.id}{ext}"
        path = await do_download(client, msg, dest, size)
        conn.execute(
            "INSERT OR IGNORE INTO files(channel,msg_id,file_name,path,size,mime,downloaded,caption,msg_date) VALUES(?,?,?,?,?,?,1,?,?)",
            (ch, msg.id, name, path, size, "", (msg.text or "")[:500], msg.date.isoformat()),
        )
        conn.commit()
        log(f"✅ 已下载 {path} ({(size or 0)/1048576:.1f}MB)")
        if cfg.get("notify_saved"):
            await notify(client, f"已下载: {name}\n来自: {ch}\n大小: {(size or 0)/1048576:.1f}MB")
    finally:
        BUSY.discard(key)


async def notify(client, text):
    """推送到自己的收藏夹(走现有 MTProto 连接,无需 bot)。"""
    try:
        await client.send_message("me", text)
    except Exception as e:
        log("通知失败:", e)


async def handle(msg, entry, client, cfg, conn):
    v = match(entry, msg)
    if v:
        await save_file(client, msg, entry, v[0], v[1], cfg, conn)
    advance_cursor(conn, entry["channel"], msg.id)


def proxy_tuple(p):
    """config 的 proxy 字典转 Telethon 元组;不合法返回 None。"""
    if not p or not p.get("port"):
        return None
    return (p.get("type", "socks5"), p.get("host", "127.0.0.1"), int(p["port"]))


async def run():
    cfg = load_config()
    conn = db()
    common = dict(
        session=str(SESSION_PATH), api_id=cfg["api_id"], api_hash=cfg["api_hash"],
        device_model="Telegram Desktop", system_version="Windows 10", app_version="1.16.0",
    )
    px = proxy_tuple(cfg.get("proxy"))
    client = TelegramClient(proxy=px, **common)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ConnectionError("代理连接后未授权")
        log(f"已通过代理连接 {px}")
    except Exception as e:
        if not px:
            raise
        log(f"代理连接失败({e}),回退直连")
        try:
            await client.disconnect()
        except Exception:
            pass
        client = TelegramClient(proxy=None, **common)
        await client.connect()
    if not await client.is_user_authorized():
        if sys.stdin is None:
            log("❌ 会话不存在且当前是无控制台的后台模式,请先运行 run.bat 完成首次登录")
            return
        phone = input("首次登录,输入 TG 手机号(如 +8613812345678): ").strip()
        await client.send_code_request(phone)
        code = input("收到验证码后输入(若开了两步验证,稍后还会要密码): ").strip()
        await client.sign_in(phone, code)
        if not await client.is_user_authorized():
            pw = input("两步验证密码: ").strip()
            await client.sign_in(password=pw)
    me = await client.get_me()
    log(f"已登录: {me.first_name} (id={me.id})")

    # 解析并加入每个频道(不加入收不到实时推送)
    watch_map = {}
    for entry in cfg["watch"]:
        if not entry.get("enabled"):
            continue
        try:
            ent = await client.get_entity(entry["channel"])
            try:
                await client(JoinChannelRequest(ent))
                log(f"已加入频道: {ent.title} (@{getattr(ent, 'username', '')})")
            except UserAlreadyParticipantError:
                log(f"频道已订阅: {ent.title} (@{getattr(ent, 'username', '')})")
            watch_map[ent.id] = entry
        except Exception as e:
            log(f"❌ 频道解析/加入失败 {entry['channel']}: {e}")
    if not watch_map:
        log("没有可监听的频道,退出。请检查 config.json 的 watch 列表")
        return

    # 先注册实时监听,再补拉历史:补拉期间到来的新消息由 handler 接住,
    # 同一条消息可能被两边各处理一次,数据库去重 + BUSY 集合兜底
    async def on_msg(event):
        entry = watch_map.get(event.chat_id)
        if not entry:
            return
        try:
            await handle(event.message, entry, client, cfg, conn)
        except Exception as e:
            log("处理消息出错:", e)

    client.add_event_handler(on_msg, events.NewMessage(chats=list(watch_map.keys())))

    # 断点续传:从数据库游标之后开始补;首次运行取最近 BACKFILL 条
    for cid, entry in watch_map.items():
        try:
            row = conn.execute("SELECT last_msg_id FROM cursors WHERE channel=?", (entry["channel"],)).fetchone()
            if row and row[0]:
                it = client.iter_messages(cid, min_id=row[0], limit=MAX_CATCHUP)
                log(f"{entry['channel']} 从消息 {row[0]} 之后开始增量补拉")
            else:
                it = client.iter_messages(cid, limit=BACKFILL)
            async for msg in it:
                try:
                    await handle(msg, entry, client, cfg, conn)
                except Exception as e:
                    log(f"单条处理失败 msg={msg.id}: {e.__class__.__name__}: {e}")
        except Exception as e:
            log(f"补拉失败 {entry['channel']}: {e}")
    log("历史补拉完成")

    if "--once" in sys.argv:
        log("--once 模式,退出")
        return

    log(f"开始实时监听 {len(watch_map)} 个频道,按 Ctrl+C 退出")
    await client.run_until_disconnected()


def main():
    lock = acquire_single_instance()
    if lock is None:
        log("已有实例在运行,本次启动取消")
        return
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log("手动退出")
    finally:
        lock.close()


if __name__ == "__main__":
    main()

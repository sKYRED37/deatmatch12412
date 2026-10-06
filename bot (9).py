"""Topoff DM — Telegram-бот лобби Standoff 2: закрытый доступ, управление кнопками.

- Без доступа: «Нет доступа» + кнопка «Запросить доступ» (запрос уходит админам).
- С доступом: меню «Создать ДМ» -> какой ДМ -> карта -> урон -> время -> предпросмотр -> публикация.
- Админ: кнопки «Доступы» (запросы, выдать/забрать), «Закрыть все лобби».
- Кнопка «ПОДКЛЮЧИТЬСЯ» под постом работает у всех (игрокам доступ не нужен).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _load_env_file() -> None:
    """Читает .env рядом с ботом и из текущей папки (работает и без python-dotenv).
    Уже заданные переменные окружения (из панели хостинга) не перезаписываются."""
    here = Path(__file__).resolve().parent
    for path in (here / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        try:
            for line in path.read_text("utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
        except Exception as e:
            print(f"не удалось прочитать {path}: {e}")


_load_env_file()
print("bot.py версия 3: запасные CHANNEL_ID и ADMIN_IDS включены")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lobby-bot")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _env(*names: str) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}


BOT_TOKEN = _env("BOT_TOKEN", "TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN")
CHANNEL_ID = _env("CHANNEL_ID", "CHAT_ID", "CHANNEL", "TELEGRAM_CHANNEL_ID")  # @username канала или -100...
# Запасные значения (не секретные): сработают, только если переменные на хостинге не заданы.
CHANNEL_ID = CHANNEL_ID or "-1003765430611"
_missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("CHANNEL_ID", CHANNEL_ID)) if not v]
if _missing:
    _seen = sorted(k for k in os.environ if any(w in k.upper() for w in ("TOKEN", "CHANNEL", "CHAT", "ADMIN", "BOT")))
    print(f"Видимые переменные (только имена): {_seen}")
    raise SystemExit(
        f"Не задано: {', '.join(_missing)}. Добавьте переменные в панели хостинга "
        f"(раздел Variables / Environment) или в файл .env рядом с bot.py. "
        f"Имена должны быть именно такие, без пробелов и кавычек вокруг значения.")
ADMIN_IDS = _ids("ADMIN_IDS") or {1766395031}      # владельцы: доступ всегда, выдают доступ другим
ALLOWED_IDS = _ids("ALLOWED_IDS")  # постоянный список с доступом (не пропадает при перезапуске)
COUNTER_START = int(os.getenv("COUNTER_START", "0"))  # с какого номера продолжить лобби
BOT_NAME = os.getenv("BOT_NAME", "Topoff DM")  # имя бота: в меню и в профиле Telegram
MODE = os.getenv("MODE_NAME", "ДМ")
MAPS = _list("MAPS", "Sandstone,Province,Rust,Zone 9,Breeze,Hanami,Dune,Sakura")
WEAPONS = _list("WEAPONS", "калаш,калаш БЕЗ ОБНОВЫ,M4,АВМ,USP,Любое")
DAMAGES = _list("DAMAGES", "Только ХС,Любой")
MINUTES = [int(x) for x in _list("MINUTES", "5,10,15,20")]
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))

BOT_USERNAME = ""
db: dict = {"counter": 0, "lobbies": {}, "allowed": {}, "profiles": {}, "requests": {}}
TASKS: dict[str, asyncio.Task] = {}


class Form(StatesGroup):
    weapon_text = State()   # пишут свой вариант «на чём играем»
    profile_new = State()   # спросили ник/ID в процессе создания лобби
    profile_edit = State()  # меняют ник/ID из меню


# ---------- хранилище и доступ ----------

def load_db() -> None:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text("utf-8"))
            for k in db:
                if k in data:
                    db[k] = data[k]
        except Exception as e:
            log.warning("не удалось прочитать %s: %s", DATA_FILE, e)


def save_db() -> None:
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False), "utf-8")
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        log.error("не удалось сохранить %s: %s", DATA_FILE, e)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def allowed(uid: int) -> bool:
    return is_admin(uid) or uid in ALLOWED_IDS or str(uid) in db["allowed"]


class Allowed(Filter):
    async def __call__(self, event) -> bool:
        return allowed(event.from_user.id)


class NotAllowed(Filter):
    async def __call__(self, event) -> bool:
        return not allowed(event.from_user.id)


class Admin(Filter):
    async def __call__(self, event) -> bool:
        return is_admin(event.from_user.id)


def esc(s) -> str:
    return html.escape(str(s))


def name_of(u) -> str:
    return f"@{u.username}" if u.username else u.full_name


# ---------- оформление ----------

def render(l: dict, closed: bool = False) -> str:
    footer = "⌛ <b>Лобби закрыто</b>" if closed else "👇 <b>Жми и врывайся в бой!</b> 👇"
    return (
        f"📢 <b>Лобби #{l['n']} создано!</b>\n\n"
        f"👤 Хост: {esc(l['nick'])} {esc(l['gid'])}\n"
        f"🎮 Режим: {esc(l['mode'])}\n"
        f"🎯 Урон: {esc(l['damage'])}\n"
        f"🗺 Карта: {esc(l['map'])}\n"
        f"⏱ Длительность: {l['minutes']} минут\n"
        f"⚔ На чём: {esc(l['weapon'])}\n\n"
        f"─────────────────\n{footer}"
    )


def join_kb(n: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 ПОДКЛЮЧИТЬСЯ", url=f"https://t.me/{BOT_USERNAME}?start=join{n}")
    return b.as_markup()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def nav(back: str | None = None) -> list[InlineKeyboardButton]:
    row = [btn("◀ Назад", back)] if back else []
    row.append(btn("🏠 Меню", "menu"))
    return row


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def pick_kb(prefix: str, labels: list[str], back: str | None = None,
            extra: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, text in enumerate(labels):
        b.button(text=text, callback_data=f"{prefix}:{i}")
    b.adjust(2)
    if extra:
        b.row(*extra)
    b.row(*nav(back))
    return b.as_markup()


def active_lobbies(uid: int) -> list[dict]:
    return [l for l in db["lobbies"].values()
            if not l["closed"] and (is_admin(uid) or l.get("owner") == uid)]


def menu_text(uid: int) -> str:
    p = db["profiles"].get(str(uid))
    who = (f"Ваш ник и ID: {esc(p['nick'])} {esc(p['gid'])}" if p
           else "Ник и ID пока не указаны — бот спросит при создании лобби.")
    return f"🔫 <b>{esc(BOT_NAME)}</b> · главное меню\n\n{who}"


def menu_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[btn("🎮 Создать ДМ", "new")]]
    n = len(active_lobbies(uid))
    rows.append([btn(f"📋 Активные лобби ({n})" if n else "📋 Активные лобби", "lobs")])
    rows.append([btn("👤 Мой ник и ID", "profile")])
    if is_admin(uid):
        r = len(db["requests"])
        rows.append([btn(f"👥 Доступы ({r} запрос.)" if r else "👥 Доступы", "users")])
    return kb(*rows)


async def show(target, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Для кнопок редактирует текущее сообщение, для текста отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except Exception as e:  # например «message is not modified»
            log.debug("edit_text: %s", e)
    else:
        await target.answer(text, reply_markup=markup)


async def show_menu(target, uid: int, prefix: str = "") -> None:
    await show(target, prefix + menu_text(uid), menu_kb(uid))


# ---------- лобби: закрытие ----------

async def close_lobby(bot: Bot, key: str) -> None:
    l = db["lobbies"].get(key)
    if not l or l["closed"]:
        return
    l["closed"] = True
    save_db()
    task = TASKS.pop(key, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    try:
        await bot.edit_message_text(chat_id=CHANNEL_ID, message_id=l["message_id"],
                                    text=render(l, closed=True), reply_markup=None)
    except Exception as e:
        log.warning("edit failed: %s", e)


async def _close_later(bot: Bot, key: str, delay: float) -> None:
    await asyncio.sleep(delay)
    await close_lobby(bot, key)


def schedule(bot: Bot, l: dict) -> None:
    key = str(l["n"])
    TASKS[key] = asyncio.create_task(_close_later(bot, key, max(0.0, l["ends_at"] - time.time())))


async def show_lobbies(target, uid: int) -> None:
    items = active_lobbies(uid)
    if not items:
        return await show(target, "📋 Активных лобби нет.", kb(nav()))
    lines = ["📋 <b>Активные лобби</b>\n"]
    rows = []
    for l in items:
        left = max(1, math.ceil((l["ends_at"] - time.time()) / 60))
        lines.append(f"#{l['n']} · {esc(l['nick'])} · {esc(l['map'])} · {esc(l['weapon'])} · ещё {left} мин")
        rows.append([btn(f"⛔ Закрыть #{l['n']}", f"cl:{l['n']}")])
    if is_admin(uid) and len(items) > 1:
        rows.append([btn("⛔ Закрыть все лобби", "clall")])
    rows.append(nav())
    await show(target, "\n".join(lines), kb(*rows))


# ---------- создание лобби: шаги ----------

async def step_type(t) -> None:
    await show(t, "🔫 <b>На чём играем?</b>\n\nВыберите кнопкой или напишите свой вариант.",
               pick_kb("ty", WEAPONS, extra=[btn("✏️ Написать свой вариант", "tyc")]))


async def step_map(t) -> None:
    await show(t, "🗺 <b>Выберите карту:</b>", pick_kb("mp", MAPS, back="s:ty"))


async def step_damage(t) -> None:
    await show(t, "🎯 <b>Урон:</b>", pick_kb("dm", DAMAGES, back="s:mp"))


async def step_minutes(t) -> None:
    await show(t, "⏱ <b>Длительность:</b>", pick_kb("mn", [f"{m} мин" for m in MINUTES], back="s:dm"))


async def ask_profile(t, state: FSMContext, flow: bool) -> None:
    await state.set_state(Form.profile_new if flow else Form.profile_edit)
    await show(t, "👤 Пришлите ваш ник и ID в Standoff 2 через пробел, например:\n"
                  "<code>фактак 66913776</code>", kb(nav("s:mn" if flow else None)))


async def step_summary(t, state: FSMContext, user) -> None:
    data = await state.get_data()
    if not all(k in data for k in ("weapon", "map", "damage", "minutes")):
        return await show_menu(t, user.id, "Начните заново.\n\n")
    profile = db["profiles"].get(str(user.id))
    if not profile:
        return await ask_profile(t, state, flow=True)
    l = {"n": db["counter"] + 1, "nick": profile["nick"], "gid": profile["gid"], "mode": MODE,
         "damage": data["damage"], "map": data["map"], "minutes": data["minutes"],
         "weapon": data["weapon"]}
    await show(t, "👀 <b>Предпросмотр</b>\n\n" + render(l),
               kb([btn("🚀 Опубликовать в канал", "pub")],
                  [btn("👤 Изменить ник и ID", "chp")],
                  nav("s:mn")))


async def publish(target, state: FSMContext, bot: Bot, user, profile: dict) -> None:
    data = await state.get_data()
    if not all(k in data for k in ("weapon", "map", "damage", "minutes")):
        await state.clear()
        return await show_menu(target, user.id, "Что-то пошло не так, начните заново.\n\n")
    n = db["counter"] + 1
    l = {"n": n, "nick": profile["nick"], "gid": profile["gid"], "mode": MODE,
         "damage": data["damage"], "map": data["map"], "minutes": data["minutes"],
         "weapon": data["weapon"], "message_id": 0, "closed": False, "owner": user.id,
         "ends_at": time.time() + data["minutes"] * 60}
    try:
        sent = await bot.send_message(CHANNEL_ID, render(l), reply_markup=join_kb(n))
    except Exception as e:
        log.error("не удалось опубликовать: %s", e)
        await state.clear()
        return await show(target, "❌ Не удалось опубликовать в канале. Проверьте, что бот — админ канала.",
                          kb(nav()))
    db["counter"] = n
    l["message_id"] = sent.message_id
    db["lobbies"][str(n)] = l
    save_db()
    schedule(bot, l)
    await state.clear()
    await show(target, f"✅ Лобби #{n} опубликовано в канале.",
               kb([btn("⛔ Закрыть лобби", f"cl:{n}")], [btn("🎮 Создать ещё", "new")], nav()))


# ---------- роутеры ----------

join_router = Router()   # для всех: кнопка «Подключиться» из канала
admin_router = Router()  # только админы
priv_router = Router()   # только с доступом
deny_router = Router()   # все остальные
admin_router.message.filter(Admin())
admin_router.callback_query.filter(Admin())
priv_router.message.filter(Allowed())
priv_router.callback_query.filter(Allowed())
deny_router.message.filter(NotAllowed())
deny_router.callback_query.filter(NotAllowed())


@join_router.message(CommandStart(deep_link=True, magic=F.args.startswith("join")))
async def join_lobby(msg: Message, command: CommandObject):
    num = (command.args or "")[4:]
    l = db["lobbies"].get(num) if num.isdigit() else None
    if not l:
        return await msg.answer("Такого лобби нет.")
    if l["closed"]:
        return await msg.answer(f"Лобби #{l['n']} уже закрыто.")
    await msg.answer(
        f"🎮 <b>Лобби #{l['n']}</b>\n\n"
        f"Хост: {esc(l['nick'])}\nID: <code>{esc(l['gid'])}</code>\n"
        f"Карта: {esc(l['map'])} · {esc(l['weapon'])}\n\n"
        "Найдите хоста в Standoff 2 по ID и зайдите в его лобби.")


# ----- админ: доступы и закрытие всех лобби -----

async def grant(bot: Bot, uid: int, label: str) -> None:
    db["allowed"][str(uid)] = label
    db["requests"].pop(str(uid), None)
    save_db()
    try:
        await bot.send_message(uid, "✅ <b>Вам выдан доступ.</b>\n\n" + menu_text(uid), reply_markup=menu_kb(uid))
    except Exception as e:
        log.warning("не удалось сообщить %s о доступе: %s", uid, e)


def users_view() -> tuple[str, InlineKeyboardMarkup]:
    lines = ["👥 <b>Доступы</b>\n"]
    rows = []
    if db["requests"]:
        lines.append("<b>Запросы:</b> ✅ выдать, 🚫 отклонить")
        for uid, label in db["requests"].items():
            rows.append([btn(f"✅ {label} · {uid}", f"gr:{uid}"), btn("🚫", f"rj:{uid}")])
    else:
        lines.append("Новых запросов нет.")
    if db["allowed"]:
        lines.append("\n<b>Есть доступ:</b> нажмите, чтобы забрать")
        for uid, label in db["allowed"].items():
            rows.append([btn(f"❌ {label} · {uid}", f"rv:{uid}")])
    if ALLOWED_IDS:
        lines.append(f"\nИз настроек (ALLOWED_IDS): {len(ALLOWED_IDS)} чел. — меняются только в .env")
    rows.append(nav())
    return "\n".join(lines), kb(*rows)


async def show_users(t) -> None:
    text, markup = users_view()
    await show(t, text, markup)


@admin_router.callback_query(F.data == "users")
async def cb_users(cb: CallbackQuery):
    await cb.answer()
    await show_users(cb)


@admin_router.callback_query(F.data.startswith("gr:"))
async def cb_grant(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data.split(":", 1)[1])
    await grant(bot, uid, db["requests"].get(str(uid), "—"))
    await cb.answer("Доступ выдан")
    await show_users(cb)


@admin_router.callback_query(F.data.startswith("rj:"))
async def cb_reject(cb: CallbackQuery):
    db["requests"].pop(cb.data.split(":", 1)[1], None)
    save_db()
    await cb.answer("Запрос отклонён")
    await show_users(cb)


@admin_router.callback_query(F.data.startswith("rv:"))
async def cb_revoke(cb: CallbackQuery):
    db["allowed"].pop(cb.data.split(":", 1)[1], None)
    save_db()
    await cb.answer("Доступ забран")
    await show_users(cb)


@admin_router.callback_query(F.data == "clall")
async def cb_close_all(cb: CallbackQuery, bot: Bot):
    keys = [k for k, l in db["lobbies"].items() if not l["closed"]]
    for k in keys:
        await close_lobby(bot, k)
    await cb.answer(f"Закрыто лобби: {len(keys)}")
    await show_lobbies(cb, cb.from_user.id)


# ----- с доступом: меню и создание ДМ -----

@priv_router.message(CommandStart())
@priv_router.message(Command("menu"))
async def start(msg: Message, state: FSMContext):
    await state.clear()
    await show_menu(msg, msg.from_user.id)


@priv_router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await show_menu(cb, cb.from_user.id)


@priv_router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await step_type(cb)


@priv_router.callback_query(F.data.startswith("s:"))
async def cb_step(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state()  # выходим из ожидания текста, данные выбора сохраняются
    step = cb.data.split(":", 1)[1]
    if step == "sum":
        return await step_summary(cb, state, cb.from_user)
    await {"ty": step_type, "mp": step_map, "dm": step_damage, "mn": step_minutes}[step](cb)


@priv_router.callback_query(F.data.startswith("ty:"))
async def cb_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(weapon=WEAPONS[int(cb.data.split(":")[1])])
    await step_map(cb)


@priv_router.callback_query(F.data == "tyc")
async def cb_type_custom(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Form.weapon_text)
    await show(cb, "✏️ Напишите, на чём играем, одним сообщением, например:\n"
                   "<code>калаш БЕЗ ОБНОВЫ</code>", kb(nav("s:ty")))


@priv_router.message(StateFilter(Form.weapon_text), F.text, ~F.text.startswith("/"))
async def got_weapon_text(msg: Message, state: FSMContext):
    await state.set_state()
    await state.update_data(weapon=msg.text.strip()[:60])
    await step_map(msg)


@priv_router.callback_query(F.data.startswith("mp:"))
async def cb_map(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(map=MAPS[int(cb.data.split(":")[1])])
    await step_damage(cb)


@priv_router.callback_query(F.data.startswith("dm:"))
async def cb_damage(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(damage=DAMAGES[int(cb.data.split(":")[1])])
    await step_minutes(cb)


@priv_router.callback_query(F.data.startswith("mn:"))
async def cb_minutes(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(minutes=MINUTES[int(cb.data.split(":")[1])])
    await step_summary(cb, state, cb.from_user)


@priv_router.callback_query(F.data == "pub")
async def cb_publish(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    profile = db["profiles"].get(str(cb.from_user.id))
    if not profile:
        return await ask_profile(cb, state, flow=True)
    await publish(cb, state, bot, cb.from_user, profile)


@priv_router.callback_query(F.data == "chp")
async def cb_change_profile_in_flow(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await ask_profile(cb, state, flow=True)


@priv_router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await ask_profile(cb, state, flow=False)


@priv_router.message(StateFilter(Form.profile_new, Form.profile_edit), F.text, ~F.text.startswith("/"))
async def got_profile(msg: Message, state: FSMContext):
    parts = msg.text.strip().rsplit(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        return await msg.answer("Нужен ник и ID цифрами, например: <code>фактак 66913776</code>")
    db["profiles"][str(msg.from_user.id)] = {"nick": parts[0][:32], "gid": parts[1]}
    save_db()
    if await state.get_state() == Form.profile_new.state:
        await state.set_state()
        return await step_summary(msg, state, msg.from_user)
    await state.clear()
    await show_menu(msg, msg.from_user.id, "✅ Сохранено.\n\n")


@priv_router.callback_query(F.data == "lobs")
async def cb_lobbies(cb: CallbackQuery):
    await cb.answer()
    await show_lobbies(cb, cb.from_user.id)


@priv_router.callback_query(F.data.startswith("cl:"))
async def cb_close(cb: CallbackQuery, bot: Bot):
    key = cb.data.split(":", 1)[1]
    l = db["lobbies"].get(key)
    if not l or (l.get("owner") != cb.from_user.id and not is_admin(cb.from_user.id)):
        return await cb.answer("Нельзя закрыть это лобби", show_alert=True)
    await close_lobby(bot, key)
    await cb.answer("Лобби закрыто")
    await show_lobbies(cb, cb.from_user.id)


# ----- без доступа -----

NO_ACCESS = ("⛔ <b>Нет доступа.</b>\n\nВаш ID: <code>{uid}</code>\n"
             "Нажмите кнопку ниже, чтобы запросить доступ у администратора.")


@deny_router.message()
async def deny_message(msg: Message):
    await msg.answer(NO_ACCESS.format(uid=msg.from_user.id), reply_markup=kb([btn("🔔 Запросить доступ", "rq")]))


@deny_router.callback_query(F.data == "rq")
async def request_access(cb: CallbackQuery, bot: Bot):
    u = cb.from_user
    if str(u.id) in db["requests"]:
        await cb.answer("Запрос уже отправлен", show_alert=True)
        return
    db["requests"][str(u.id)] = name_of(u)
    save_db()
    await cb.answer()
    await show(cb, "✅ Запрос отправлен. Как только вам выдадут доступ, бот напишет.")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id, f"🔔 <b>Запрос доступа</b>\n{esc(name_of(u))} · <code>{u.id}</code>",
                reply_markup=kb([btn("✅ Выдать доступ", f"gr:{u.id}"), btn("🚫 Отклонить", f"rj:{u.id}")]))
        except Exception as e:
            log.warning("не удалось написать админу %s: %s", admin_id, e)


@deny_router.callback_query()
async def deny_callback(cb: CallbackQuery):
    await cb.answer("⛔ Нет доступа", show_alert=True)


async def main():
    global BOT_USERNAME
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: доступ никому не выдать. Заполните настройки")
    load_db()
    db["counter"] = max(db["counter"], COUNTER_START)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    log.info("Бот запущен: @%s, канал %s, данные: %s", BOT_USERNAME, CHANNEL_ID, DATA_FILE.resolve())
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
        if (await bot.get_my_name()).name != BOT_NAME:  # имя меняем только если оно отличается
            await bot.set_my_name(name=BOT_NAME)
    except Exception as e:
        log.warning("не удалось обновить имя/команды бота: %s", e)
    for l in db["lobbies"].values():
        if not l["closed"]:
            schedule(bot, l)
    dp = Dispatcher()
    dp.include_routers(join_router, admin_router, priv_router, deny_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

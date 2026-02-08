import asyncio
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


BOT_TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = "users.json"

# -------------------- Storage --------------------
def load_data() -> Dict[str, dict]:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data: Dict[str, dict]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

DATA: Dict[str, dict] = load_data()

def get_user(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in DATA:
        DATA[uid] = {"current": None, "favorites": []}
        save_data(DATA)
    return DATA[uid]

def set_current(user_id: int, city: dict) -> None:
    u = get_user(user_id)
    u["current"] = city
    save_data(DATA)

def add_fav(user_id: int, city: dict) -> None:
    u = get_user(user_id)
    favs = u["favorites"]
    if not any(c["id"] == city["id"] for c in favs):
        favs.append(city)
        save_data(DATA)

def remove_fav(user_id: int, city_id: str) -> None:
    u = get_user(user_id)
    u["favorites"] = [c for c in u["favorites"] if c["id"] != city_id]
    save_data(DATA)


# -------------------- Models --------------------
@dataclass
class City:
    id: str
    name: str
    country: str
    admin1: str
    lat: float
    lon: float

    def label(self) -> str:
        parts = [self.name]
        if self.admin1:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


# -------------------- FSM --------------------
class PickCity(StatesGroup):
    waiting_city_name = State()


# -------------------- API helpers (Open-Meteo) --------------------
async def geo_search(name: str, limit: int = 5) -> List[City]:
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": name, "count": limit, "language": "ru", "format": "json"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, timeout=20) as r:
            r.raise_for_status()
            data = await r.json()

    results = data.get("results") or []
    cities: List[City] = []
    for item in results:
        cities.append(
            City(
                id=str(item.get("id", f'{item["latitude"]},{item["longitude"]}')),
                name=item.get("name", ""),
                country=item.get("country", ""),
                admin1=item.get("admin1", ""),
                lat=float(item["latitude"]),
                lon=float(item["longitude"]),
            )
        )
    return cities

async def forecast_daily(lat: float, lon: float, days: int) -> dict:
    # Open-Meteo: forecast_days максимум 16 для GFS. ([open-meteo.com](https://open-meteo.com/en/docs/gfs-api?utm_source=chatgpt.com))
    days = max(1, min(days, 16))
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "forecast_days": days,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weathercode",
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params=params, timeout=20) as r:
            r.raise_for_status()
            return await r.json()

def format_daily(city_label: str, daily: dict, want_days: int, real_days: int) -> str:
    d = daily["daily"]
    lines = [f"📍 {city_label}"]

    if want_days > real_days:
        lines.append(f"⚠️ Доступно только {real_days} дней (лимит источника прогноза).")

    lines.append("")
    for i in range(min(real_days, len(d["time"]))):
        date = d["time"][i]
        tmin = d["temperature_2m_min"][i]
        tmax = d["temperature_2m_max"][i]
        pr = d["precipitation_sum"][i]
        wind = d["wind_speed_10m_max"][i]
        weather_code = d["weathercode"][i]
        # Иконка погоды
        weather_icon = f"https://open-meteo.com/assets/icons/{weather_code}.svg"
        lines.append(f"{date}: {tmin}…{tmax}°C, осадки {pr} мм, ветер до {wind} м/с")

    return "\n".join(lines)


# -------------------- Keyboards --------------------
def main_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="🏙 Выбрать город"),
                types.KeyboardButton(text="⭐ Избранные города"),
            ],
            [
                types.KeyboardButton(text="🗓 Погода на неделю"),
                types.KeyboardButton(text="📅 Погода на месяц"),
            ],
            [
                types.KeyboardButton(text="📍 Отправить геолокацию", request_location=True),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )

def cities_inline_kb(cities: List[City]) -> types.InlineKeyboardMarkup:
    rows = []
    for c in cities:
        rows.append([types.InlineKeyboardButton(text=c.label(), callback_data=f"pick:{c.id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

def fav_inline_kb(favs: List[dict]) -> types.InlineKeyboardMarkup:
    rows = []
    for c in favs:
        rows.append([
            types.InlineKeyboardButton(text=f"📌 {c['name']}", callback_data=f"favset:{c['id']}"),
            types.InlineKeyboardButton(text="🗑", callback_data=f"favdel:{c['id']}"),
        ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows or [[types.InlineKeyboardButton(text="(пусто)", callback_data="noop")]])

def current_actions_kb(is_fav: bool) -> types.InlineKeyboardMarkup:
    btn = "⭐ В избранное" if not is_fav else "✅ Уже в избранном"
    cb = "addfav" if not is_fav else "noop"
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=btn, callback_data=cb)]
    ])


# -------------------- Bot --------------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# cache for last city search results per user (to map callback id -> city)
LAST_SEARCH: Dict[int, Dict[str, City]] = {}

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    get_user(m.from_user.id)
    await m.answer("Готово ✅ Выбирай кнопку:", reply_markup=main_kb())

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(
        "Команды:\n"
        "/start — меню\n"
        "/help — помощь\n\n"
        "Кнопки: выбрать город, геолокация, прогноз на неделю/«месяц», избранные.",
        reply_markup=main_kb()
    )

@dp.message(F.text == "🏙 Выбрать город")
async def choose_city(m: types.Message, state: FSMContext):
    await state.set_state(PickCity.waiting_city_name)
    await m.answer("Напиши название города (например: Алматы / Moscow / Berlin).")

@dp.message(PickCity.waiting_city_name)
async def city_name_received(m: types.Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        return await m.answer("Напиши название города текстом 🙂")

    cities = await geo_search(name, limit=5)
    if not cities:
        return await m.answer("Не нашёл 😅 Попробуй другое написание.")

    LAST_SEARCH[m.from_user.id] = {c.id: c for c in cities}
    await state.clear()
    await m.answer("Выбери точный вариант:", reply_markup=cities_inline_kb(cities))

@dp.callback_query(F.data.startswith("pick:"))
async def pick_city_cb(cq: types.CallbackQuery):
    user_id = cq.from_user.id
    cid = cq.data.split(":", 1)[1]
    city = (LAST_SEARCH.get(user_id) or {}).get(cid)

    if not city:
        await cq.answer("Список устарел. Выбери город заново.")
        return

    city_dict = {
        "id": city.id,
        "name": city.label(),
        "lat": city.lat,
        "lon": city.lon,
    }
    set_current(user_id, city_dict)

    u = get_user(user_id)
    is_fav = any(c["id"] == city_dict["id"] for c in u["favorites"])

    await cq.message.edit_text(
        f"✅ Выбран город: {city_dict['name']}",
        reply_markup=current_actions_kb(is_fav)
    )
    await cq.answer()

@dp.callback_query(F.data == "addfav")
async def add_fav_cb(cq: types.CallbackQuery):
    u = get_user(cq.from_user.id)
    cur = u.get("current")
    if not cur:
        await cq.answer("Сначала выбери город.")
        return
    add_fav(cq.from_user.id, cur)
    await cq.answer("Добавлено в избранные ⭐")
    await cq.message.edit_reply_markup(reply_markup=current_actions_kb(True))

@dp.message(F.text == "⭐ Избранные города")
async def favs(m: types.Message):
    u = get_user(m.from_user.id)
    favs = u.get("favorites", [])
    await m.answer("⭐ Избранные города:", reply_markup=fav_inline_kb(favs))

@dp.callback_query(F.data.startswith("favset:"))
async def fav_set(cq: types.CallbackQuery):
    cid = cq.data.split(":", 1)[1]
    u = get_user(cq.from_user.id)
    city = next((c for c in u.get("favorites", []) if c["id"] == cid), None)
    if not city:
        await cq.answer("Не найдено.")
        return
    set_current(cq.from_user.id, city)
    await cq.answer("Текущий город выбран ✅")
    await cq.message.edit_text(f"✅ Текущий город: {city['name']}")

@dp.callback_query(F.data.startswith("favdel:"))
async def fav_del(cq: types.CallbackQuery):
    cid = cq.data.split(":", 1)[1]
    remove_fav(cq.from_user.id, cid)
    u = get_user(cq.from_user.id)
    await cq.answer("Удалено 🗑")
    await cq.message.edit_reply_markup(reply_markup=fav_inline_kb(u.get("favorites", [])))

@dp.callback_query(F.data == "noop")
async def noop(cq: types.CallbackQuery):
    await cq.answer()

@dp.message(F.location)
async def got_location(m: types.Message):
    lat = m.location.latitude
    lon = m.location.longitude
    # Сохраняем как "текущая точка"
    city_dict = {
        "id": f"{lat:.5f},{lon:.5f}",
        "name": f"Точка: {lat:.5f}, {lon:.5f}",
        "lat": lat,
        "lon": lon,
    }
    set_current(m.from_user.id, city_dict)
    await m.answer(f"✅ Локация сохранена: {city_dict['name']}")

async def send_weather(m: types.Message, want_days: int):
    u = get_user(m.from_user.id)
    cur = u.get("current")
    if not cur:
        await m.answer("Сначала выбери город 🏙 или отправь геолокацию 📍", reply_markup=main_kb())
        return

    data = await forecast_daily(cur["lat"], cur["lon"], want_days)
    # реальное количество дней (Open-Meteo ограничит до 16)
    real_days = min(want_days, 16)
    text = format_daily(cur["name"], data, want_days=want_days, real_days=real_days)
    
    # Отправляем иконки как изображения
    for i in range(real_days):
        weather_code = data["daily"]["weathercode"][i]
        weather_icon = f"https://open-meteo.com/assets/icons/{weather_code}.svg"
        await m.answer(text, reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton(text="🖼️ Погода", url=weather_icon)
        ))

@dp.message(F.text == "🗓 Погода на неделю")
async def week(m: types.Message):
    await send_weather(m, want_days=7)

@dp.message(F.text == "📅 Погода на месяц")
async def month(m: types.Message):
    # “месяц” показываем максимум доступных дней (до 16) и предупреждаем
    await send_weather(m, want_days=30)

@dp.message(F.text == "📍 Отправить геолокацию")
async def ask_location(m: types.Message):
    await m.answer("Нажми кнопку отправки локации (Telegram спросит разрешение).", reply_markup=main_kb())


async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())

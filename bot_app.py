import asyncio
import os
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiohttp import ClientTimeout
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from pathlib import Path

from load_config import _load_dotenv_simple
from user_filters import UserFiltersStorage, UserProfile

storage = UserFiltersStorage()

MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Добавить фильтр")],
        [KeyboardButton(text="Мои фильтры")],
        [KeyboardButton(text="Настройки")],
    ],
    resize_keyboard=True,
)

REGION_LABELS = {
    "all": "Все регионы",
    "moscow": "Москва",
    "mo": "МО",
    "moscow_mo": "Москва и МО",
}

DELIVERY_LABELS = {
    "any": "Любой способ",
    "delivery_only": "Только доставка",
    "pickup_only": "Без доставки",
}


class FilterForm(StatesGroup):
    query = State()
    region = State()
    min_price = State()
    max_price = State()
    delivery = State()
    track = State()


def region_keyboard(selected: Optional[str], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for value, title in REGION_LABELS.items():
        mark = "✅ " if value == selected else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{title}", callback_data=f"{prefix}:{value}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_keyboard(selected: Optional[str], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for value, title in DELIVERY_LABELS.items():
        mark = "✅ " if value == selected else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{title}", callback_data=f"{prefix}:{value}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def track_keyboard(selected: bool) -> InlineKeyboardMarkup:
    on_text = "✅ Вкл" if selected else "Вкл"
    off_text = "Выкл" if selected else "✅ Выкл"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=on_text, callback_data="wizard_track:1")],
            [InlineKeyboardButton(text=off_text, callback_data="wizard_track:0")],
        ]
    )


async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    storage.ensure_user(message.chat.id, message.from_user.username)
    await message.answer(
        "Привет! Я помогу настроить фильтры Авито. Используй меню ниже.",
        reply_markup=MENU_KB,
    )


async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено", reply_markup=MENU_KB)


async def add_filter_entry(message: Message, state: FSMContext):
    if message.text != "Добавить фильтр":
        return
    profile = storage.get_user_profile(message.chat.id)
    await state.clear()
    await state.set_state(FilterForm.query)
    await state.update_data(mode="create", profile=profile.__dict__, sort_new=profile.default_sort_new)
    await message.answer("Введи поисковый запрос")


async def process_query(message: Message, state: FSMContext):
    data = await state.get_data()
    current_filter = data.get("edit_filter")
    text = message.text.strip()
    if data.get("mode") == "edit" and text == "-":
        text = current_filter.get("text")
    if not text:
        await message.answer("Запрос не может быть пустым")
        return
    await state.update_data(query=text)
    profile = UserProfile(**data["profile"])
    selected = current_filter.get("region") if current_filter else profile.default_region
    await state.set_state(FilterForm.region)
    await message.answer("Выбери регион", reply_markup=region_keyboard(selected, "wizard_region"))


async def process_region(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 1)[1]
    await state.update_data(region=value)
    await callback.answer()
    await state.set_state(FilterForm.min_price)
    await callback.message.answer("Минимальная цена или '-' чтобы пропустить")


def _parse_price(text: str, default: Optional[int] = None) -> Optional[int]:
    text = text.strip()
    if text in {"-", ""}:
        return default
    if text.isdigit():
        value = int(text)
        return value
    return None


async def process_min_price(message: Message, state: FSMContext):
    data = await state.get_data()
    current = data.get("edit_filter", {}).get("min_price")
    value = _parse_price(message.text, current)
    if value is None and message.text.strip() not in {"-", ""}:
        await message.answer("Нужно число или '-' для пропуска")
        return
    await state.update_data(min_price=value)
    await state.set_state(FilterForm.max_price)
    await message.answer("Максимальная цена или '-' чтобы пропустить")


async def process_max_price(message: Message, state: FSMContext):
    data = await state.get_data()
    current = data.get("edit_filter", {}).get("max_price")
    value = _parse_price(message.text, current)
    if value is None and message.text.strip() not in {"-", ""}:
        await message.answer("Нужно число или '-' для пропуска")
        return
    await state.update_data(max_price=value)
    profile = UserProfile(**data["profile"])
    current_delivery = data.get("edit_filter", {}).get("delivery") or profile.default_delivery
    await state.set_state(FilterForm.delivery)
    await message.answer(
        "Выбери тип доставки",
        reply_markup=delivery_keyboard(current_delivery, "wizard_delivery"),
    )


async def process_delivery(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 1)[1]
    await state.update_data(delivery=value)
    await callback.answer()
    data = await state.get_data()
    current_track = data.get("edit_filter", {}).get("track_price_changes")
    if current_track is None:
        profile = UserProfile(**data["profile"])
        current_track = profile.default_track_price
    await state.set_state(FilterForm.track)
    await callback.message.answer("Отслеживать изменение цены?", reply_markup=track_keyboard(current_track))


async def process_track(callback: CallbackQuery, state: FSMContext):
    track_value = callback.data.endswith(":1")
    await state.update_data(track_price=track_value)
    await callback.answer()
    data = await state.get_data()
    chat_id = callback.message.chat.id
    if data.get("mode") == "edit":
        filt = data["edit_filter"]
        storage.update_filter(
            filt["id"],
            chat_id=chat_id,
            text=data.get("query", filt["text"]),
            region=data.get("region", filt["region"]),
            min_price=data.get("min_price", filt.get("min_price")),
            max_price=data.get("max_price", filt.get("max_price")),
            delivery=data.get("delivery", filt.get("delivery")),
            sort_new=filt.get("sort_new"),
            track_price_changes=track_value,
        )
        await callback.message.answer("Фильтр обновлён", reply_markup=MENU_KB)
    else:
        storage.add_filter(
            chat_id=chat_id,
            text=data["query"],
            region=data.get("region", "all"),
            min_price=data.get("min_price"),
            max_price=data.get("max_price"),
            delivery=data.get("delivery", "any"),
            sort_new=data.get("sort_new"),
            track_price_changes=track_value,
        )
        await callback.message.answer("Фильтр сохранён", reply_markup=MENU_KB)
    await state.clear()


def format_filter(row) -> str:
    region = REGION_LABELS.get(row["region"], row["region"])
    delivery = DELIVERY_LABELS.get(row["delivery"], row["delivery"])
    min_price = row["min_price"] if row["min_price"] is not None else "—"
    max_price = row["max_price"] if row["max_price"] is not None else "—"
    track = "Вкл" if row["track_price_changes"] else "Выкл"
    parts = [
        f"*{row['text']}*",
        f"Регион: {region}",
        f"Цена: {min_price} — {max_price}",
        f"Доставка: {delivery}",
        f"Отслеживание цены: {track}",
    ]
    return "\n".join(parts)


def filter_keyboard(row) -> InlineKeyboardMarkup:
    delivery_text = "Только доставка: " + ("Выкл" if row["delivery"] == "delivery_only" else "Вкл")
    track_text = "Отслеживание цены: " + ("Выкл" if not row["track_price_changes"] else "Вкл")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Редактировать", callback_data=f"filter_edit:{row['id']}")],
            [InlineKeyboardButton(text="Удалить", callback_data=f"filter_delete:{row['id']}")],
            [InlineKeyboardButton(text=delivery_text, callback_data=f"filter_delivery:{row['id']}")],
            [InlineKeyboardButton(text=track_text, callback_data=f"filter_track:{row['id']}")],
        ]
    )


async def show_filters(message: Message):
    if message.text != "Мои фильтры":
        return
    rows = storage.list_filters(message.chat.id)
    if not rows:
        await message.answer("Ты пока не добавил фильтры. Нажми ‘Добавить фильтр’.")
        return
    for row in rows:
        await message.answer(
            format_filter(row),
            reply_markup=filter_keyboard(row),
            parse_mode=ParseMode.MARKDOWN,
        )


async def show_settings(message: Message):
    if message.text != "Настройки":
        return
    profile = storage.get_user_profile(message.chat.id)
    text = (
        "Настройки по умолчанию:\n"
        f"• Регион: {REGION_LABELS.get(profile.default_region, profile.default_region)}\n"
        f"• Доставка: {DELIVERY_LABELS.get(profile.default_delivery, profile.default_delivery)}\n"
        f"• Отслеживание цены: {'Вкл' if profile.default_track_price else 'Выкл'}\n"
        f"• Сортировка по новым: {'Вкл' if profile.default_sort_new else 'Выкл'}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить регион", callback_data="settings_region")],
            [InlineKeyboardButton(text="Изменить доставку", callback_data="settings_delivery")],
            [InlineKeyboardButton(text="Переключить отслеживание", callback_data="settings_track")],
            [InlineKeyboardButton(text="Переключить сортировку", callback_data="settings_sort")],
        ]
    )
    await message.answer(text, reply_markup=kb)


async def settings_region(callback: CallbackQuery):
    profile = storage.get_user_profile(callback.message.chat.id)
    await callback.answer()
    await callback.message.answer(
        "Выбери регион по умолчанию",
        reply_markup=region_keyboard(profile.default_region, "settings_region_value"),
    )


async def settings_region_value(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    storage.update_user_defaults(callback.message.chat.id, default_region=value)
    await callback.answer("Обновлено")


async def settings_delivery(callback: CallbackQuery):
    profile = storage.get_user_profile(callback.message.chat.id)
    await callback.answer()
    await callback.message.answer(
        "Выбери доставку по умолчанию",
        reply_markup=delivery_keyboard(profile.default_delivery, "settings_delivery_value"),
    )


async def settings_delivery_value(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    storage.update_user_defaults(callback.message.chat.id, default_delivery=value)
    await callback.answer("Обновлено")


async def settings_toggle(callback: CallbackQuery):
    action = callback.data.replace("settings_", "", 1)
    profile = storage.get_user_profile(callback.message.chat.id)
    if action == "track":
        storage.update_user_defaults(callback.message.chat.id, default_track_price=not profile.default_track_price)
    else:
        storage.update_user_defaults(callback.message.chat.id, default_sort_new=not profile.default_sort_new)
    await callback.answer("Обновлено")


async def filter_delete(callback: CallbackQuery):
    filter_id = int(callback.data.split(":", 1)[1])
    storage.delete_filter(filter_id, callback.message.chat.id)
    await callback.answer("Удалено")
    await callback.message.edit_text("Фильтр удалён")


async def filter_toggle_delivery(callback: CallbackQuery):
    filter_id = int(callback.data.split(":", 1)[1])
    new_value = storage.toggle_delivery_only(filter_id, callback.message.chat.id)
    row = storage.get_filter(filter_id, callback.message.chat.id)
    await callback.answer("Сохранено")
    if row:
        await callback.message.edit_text(
            format_filter(row),
            reply_markup=filter_keyboard(row),
            parse_mode=ParseMode.MARKDOWN,
        )


async def filter_toggle_track(callback: CallbackQuery):
    filter_id = int(callback.data.split(":", 1)[1])
    storage.toggle_price_tracking(filter_id, callback.message.chat.id)
    row = storage.get_filter(filter_id, callback.message.chat.id)
    await callback.answer("Сохранено")
    if row:
        await callback.message.edit_text(
            format_filter(row),
            reply_markup=filter_keyboard(row),
            parse_mode=ParseMode.MARKDOWN,
        )


async def filter_edit(callback: CallbackQuery, state: FSMContext):
    filter_id = int(callback.data.split(":", 1)[1])
    row = storage.get_filter(filter_id, callback.message.chat.id)
    if not row:
        await callback.answer("Не найдено")
        return
    row_dict = dict(row)
    row_dict["track_price_changes"] = bool(row_dict.get("track_price_changes"))
    await state.set_state(FilterForm.query)
    await state.update_data(
        mode="edit",
        edit_filter=row_dict,
        profile=storage.get_user_profile(callback.message.chat.id).__dict__,
        sort_new=row_dict.get("sort_new"),
    )
    await callback.answer()
    await callback.message.answer("Введи новый запрос или '-' чтобы оставить прежний")


async def main():
    _load_dotenv_simple(Path(__file__).resolve().parent)
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("TG_BOT_TOKEN не задан")
    session = AiohttpSession(timeout=ClientTimeout(total=30, sock_read=30, connect=10))
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN), session=session)
    dp = Dispatcher()
    dp.message.register(start_cmd, CommandStart())
    dp.message.register(cancel_cmd, Command("cancel"))
    dp.message.register(add_filter_entry, F.text == "Добавить фильтр")
    dp.message.register(show_filters, F.text == "Мои фильтры")
    dp.message.register(show_settings, F.text == "Настройки")
    dp.message.register(process_query, FilterForm.query)
    dp.callback_query.register(process_region, FilterForm.region, F.data.startswith("wizard_region:"))
    dp.message.register(process_min_price, FilterForm.min_price)
    dp.message.register(process_max_price, FilterForm.max_price)
    dp.callback_query.register(process_delivery, FilterForm.delivery, F.data.startswith("wizard_delivery:"))
    dp.callback_query.register(process_track, FilterForm.track, F.data.startswith("wizard_track:"))
    dp.callback_query.register(settings_region, F.data == "settings_region")
    dp.callback_query.register(settings_delivery, F.data == "settings_delivery")
    dp.callback_query.register(settings_region_value, F.data.startswith("settings_region_value:"))
    dp.callback_query.register(settings_delivery_value, F.data.startswith("settings_delivery_value:"))
    dp.callback_query.register(settings_toggle, F.data.in_({"settings_track", "settings_sort"}))
    dp.callback_query.register(filter_delete, F.data.startswith("filter_delete:"))
    dp.callback_query.register(filter_toggle_delivery, F.data.startswith("filter_delivery:"))
    dp.callback_query.register(filter_toggle_track, F.data.startswith("filter_track:"))
    dp.callback_query.register(filter_edit, F.data.startswith("filter_edit:"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

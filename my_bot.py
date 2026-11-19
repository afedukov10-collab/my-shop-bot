import asyncio
import logging
import aiohttp
import os
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from fragment_api.client import FragmentAPIClient

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "9222158924:AAHnZPNU6pRXhwnsmcQvVX2KTzXkkEbYfAk"
XROCKET_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjI5OTQzMyIsImp0aSI6ImFwcDoyOTk0MzM6MWU3ZmFhYjAtMGVkMy00NDVjLWEyY2YtOTcxYWFjOTMwNzI3IiwiaWF0IjoxNzk1MTA5ODM2fQ.Jl22525GYpWtpiAPm-nB_xay9iegNLheXqusjNbWsOY"
ADMIN_ID = 8514329538
MY_TON_WALLET = "UQDdgrCiFuUt0b1afacGhEiKQCTIhwONx1hTFJVKZbwDetwc"
BY_CARD_NUMBER = "9112 3801 9121 1830"

# ========== ДАННЫЕ ДЛЯ FRAGMENT ==========
FRAGMENT_SEED = "bWlsayB3aWxkIGFjY2lkZW50IGV4cGxhaW4gc3F1YXJlIGhlaWdodCByZW1lbWJlciBjb2FzdCBqb3kgc2tpcnQgdHViZSB0YXR0b28gbWVyaXQgZG92ZSBoYXZlIGdyaWVmIGVsZXZhdG9yIHBvbnkgY2FzaCBhbGxleSBhY2Nlc3Mgc3Vycm91bmQgdml0YWwgYWdyZWU="
FRAGMENT_COOKIES = '{"stel_token":"9f33e63ca3c60bc8f5ccc97e33767d329f33e6269f33eab616c15fb49301c752e3f99","stel_ssid":"0377ac196801f09708_3134387726996450323","stel_ton_token":"59-o_9wFaMiDGnA2f7xORumamdCHSv4gNwb5TwpUWzdHEuXco8Y0rFQGPDFI_U7hjbAzUV552RkaVvMU2vtsmBR8VzyYXMCTN8Uqu6RRdkxGyXCdykhLO3BhTBnXV9rC5jg3qJAEPK2pGpUGxM3omMzc2b6m6sZASEq5RNKlPPBYpArVeoMicKmiGhpYtNj6XaH9jiUR","stel_dt":"-180"}'
# ======================================

# Настройки (Админ может менять их через /admin)
SETTINGS = {
    "gram_price_min": 80.0,
    "gram_price_max": 1000.0,
    "rate_rub": 35.0,
    "rate_byn": 1.5,
}

REVIEWS = [] # Отзывы
user_history = {} # История чеков: {user_id: [список чеков]}

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
orders = {}
bot = None
session = None

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
app = Flask(__name__)
@app.route('/')
def index():
    return "Bot is alive!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- УТИЛИТА ДЛЯ УДАЛЕНИЯ МУСОРА ---
async def clean_chat(chat_id, message_id=None):
    try:
        if message_id:
            await bot.delete_message(chat_id, message_id)
    except Exception:
        pass

# --- ФУНКЦИЯ ПОКУПКИ STARS ---
async def buy_stars_fragment(username: str, amount: int):
    if not FRAGMENT_SEED or FRAGMENT_SEED == "bWlsayB3aWxkIGFjY2lkZW50IGV4cGxhaW4gc3F1YXJlIGhlaWdodCByZW1lbWJlciBjb2FzdCBqb3kgc2tpcnQgdHViZSB0YXR0b28gbWVyaXQgZG92ZSBoYXZlIGdyaWVmIGVsZXZhdG9yIHBvbnkgY2FzaCBhbGxleSBhY2Nlc3Mgc3Vycm91bmQgdml0YWwgYWdyZWU=":
        raise Exception("Не задан FRAGMENT_SEED")
        
    def _buy():
        with FragmentAPIClient() as client:
            result = client.buy_stars(
                username=username, amount=amount, seed=FRAGMENT_SEED,
                cookies=FRAGMENT_COOKIES, payment_method="ton", wait=True
            )
            return result
    
    result = await asyncio.to_thread(_buy)
    if result.success:
        return True
    else:
        raise Exception(f"Fragment Error: {result.error}")

# --- СОЗДАНИЕ СЧЕТА XROCKET ---
async def create_xrocket_invoice(amount: float, currency: str, description: str):
    url = "https://pay.api.xrocket.exchange/api/v1/invoices"
    headers = {"Authorization": f"Bearer {XROCKET_API_KEY}", "Content-Type": "application/json"}
    api_currency = "TONCOIN" if currency == "TON" else currency
    payload = {"priceAmount": str(amount), "priceCurrency": api_currency, "description": description, "numPayments": 1}

    async with session.post(url, json=payload, headers=headers) as response:
        if response.status in [200, 201]:
            return await response.json()
        else:
            text = await response.text()
            raise Exception(f"Ошибка API ({response.status}): {text}")

# --- СОСТОЯНИЯ ---
class ShopStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_ton_amount = State()
    waiting_for_card_amount = State()
    waiting_for_stars_username = State()
    waiting_for_custom_stars = State()
    waiting_for_gram_username = State()
    waiting_for_review = State()
    admin_edit_gram_min = State()
    admin_edit_gram_max = State()
    admin_edit_rate_rub = State()
    admin_edit_rate_byn = State()

# --- КЛАВИАТУРЫ ---
def get_main_menu():
    kb = [
        [KeyboardButton(text="⭐ Купить Stars"), KeyboardButton(text="💎 Купить Gram")],
        [KeyboardButton(text="💳 Пополнить баланс"), KeyboardButton(text="🧾 Мои чеки")],
        [KeyboardButton(text="📞 Поддержка")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- ФУНКЦИЯ ДОБАВЛЕНИЯ ЧЕКА ---
def add_receipt(user_id, text):
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append(text)

# --- ГЛАВНОЕ МЕНЮ ---
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await clean_chat(message.chat.id, message.message_id)
    await message.answer(
        "🛍️ <b>Добро пожаловать в магазин!</b>\n\n"
        "Здесь ты можешь купить Stars и Gram по лучшему курсу.\n"
        "Выбери нужный раздел в меню ниже:",
        reply_markup=get_main_menu(),
        parse_mode="HTML"
    )

# --- КНОПКА "МОИ ЧЕКИ" ---
@dp.message(F.text == "🧾 Мои чеки")
async def my_receipts(message: Message):
    await clean_chat(message.chat.id, message.message_id)
    history = user_history.get(message.from_user.id, [])
    if not history:
        await message.answer("У вас пока нет истории покупок.")
        return
    
    text = "🧾 <b>Ваши последние чеки:</b>\n\n"
    for item in history[-5:]: # Показываем последние 5
        text += f"{item}\n\n"
    await message.answer(text, parse_mode="HTML")

# --- АДМИН ПАНЕЛЬ (без изменений) ---
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Мин. цена Gram: {SETTINGS['gram_price_min']}", callback_data="admin_gram_min")],
        [InlineKeyboardButton(text=f"Макс. цена Gram: {SETTINGS['gram_price_max']}", callback_data="admin_gram_max")],
        [InlineKeyboardButton(text=f"Курс Stars (RUB): {SETTINGS['rate_rub']}", callback_data="admin_rate_rub")],
        [InlineKeyboardButton(text=f"Курс BYN: {SETTINGS['rate_byn']}", callback_data="admin_rate_byn")],
        [InlineKeyboardButton(text="📝 Посмотреть отзывы", callback_data="admin_reviews")],
    ])
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_"))
async def admin_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    action = call.data
    if action == "admin_gram_min":
        await state.set_state(ShopStates.admin_edit_gram_min)
        await call.message.answer("Введите новую минимальную цену Gram (RUB):")
    elif action == "admin_gram_max":
        await state.set_state(ShopStates.admin_edit_gram_max)
        await call.message.answer("Введите новую максимальную цену Gram (RUB):")
    elif action == "admin_rate_rub":
        await state.set_state(ShopStates.admin_edit_rate_rub)
        await call.message.answer("Введите курс 1 звезды (RUB):")
    elif action == "admin_rate_byn":
        await state.set_state(ShopStates.admin_edit_rate_byn)
        await call.message.answer("Введите курс (BYN за 1 USD):")
    elif action == "admin_reviews":
        if not REVIEWS: await call.message.answer("Отзывов нет.")
        else:
            text = "📝 <b>Отзывы:</b>\n\n"
            for r in REVIEWS[-10:]: text += f"👤 @{r['user']}: {r['text']}\n\n"
            await call.message.answer(text, parse_mode="HTML")
    await call.answer()

# Обработчики ввода админа
@dp.message(ShopStates.admin_edit_gram_min)
async def set_gram_min(message: Message, state: FSMContext):
    try: SETTINGS['gram_price_min'] = float(message.text); await message.answer(f"✅ Мин. цена: {SETTINGS['gram_price_min']}")
    except: await message.answer("❌ Ошибка.")
    await state.clear()

@dp.message(ShopStates.admin_edit_gram_max)
async def set_gram_max(message: Message, state: FSMContext):
    try: SETTINGS['gram_price_max'] = float(message.text); await message.answer(f"✅ Макс. цена: {SETTINGS['gram_price_max']}")
    except: await message.answer("❌ Ошибка.")
    await state.clear()

@dp.message(ShopStates.admin_edit_rate_rub)
async def set_rate_rub(message: Message, state: FSMContext):
    try: SETTINGS['rate_rub'] = float(message.text); await message.answer(f"✅ Курс Stars: {SETTINGS['rate_rub']}")
    except: await message.answer("❌ Ошибка.")
    await state.clear()

@dp.message(ShopStates.admin_edit_rate_byn)
async def set_rate_byn(message: Message, state: FSMContext):
    try: SETTINGS['rate_byn'] = float(message.text); await message.answer(f"✅ Курс BYN: {SETTINGS['rate_byn']}")
    except: await message.answer("❌ Ошибка.")
    await state.clear()

# --- КНОПКИ ГЛАВНОГО МЕНЮ ---
@dp.message(F.text == "⭐ Купить Stars")
async def menu_buy_stars(message: Message, state: FSMContext):
    await state.clear()
    await clean_chat(message.chat.id, message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="50 ⭐", callback_data="stars_50")],
        [InlineKeyboardButton(text="100 ⭐", callback_data="stars_100")],
        [InlineKeyboardButton(text="500 ⭐", callback_data="stars_500")],
        [InlineKeyboardButton(text="Своё количество", callback_data="stars_custom")],
    ])
    await message.answer("⭐ <b>Покупка Stars</b>\n\nВыбери количество:", reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "💎 Купить Gram")
async def menu_buy_gram(message: Message, state: FSMContext):
    await state.clear()
    await clean_chat(message.chat.id, message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить картой BY", callback_data="gram_pay_card")],
        [InlineKeyboardButton(text="Оплатить Stars", callback_data="gram_pay_stars")],
    ])
    await message.answer(f"💎 <b>Покупка Gram</b>\n\nЦена: от {SETTINGS['gram_price_min']} до {SETTINGS['gram_price_max']} RUB\n\nВыбери способ оплаты:", reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "💳 Пополнить баланс")
async def menu_deposit(message: Message, state: FSMContext):
    await state.clear()
    await clean_chat(message.chat.id, message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 USDT (xRocket)", callback_data="pay_USDT")],
        [InlineKeyboardButton(text="💎 Gram (xRocket)", callback_data="pay_TON_XROCKET")],
        [InlineKeyboardButton(text="💎 Gram (Tonkeeper)", callback_data="pay_TON_DIRECT")],
        [InlineKeyboardButton(text="💳 Карта BY", callback_data="pay_CARD_BY")],
    ])
    await message.answer("Выбери способ пополнения:", reply_markup=kb)

@dp.message(F.text == "📞 Поддержка")
async def menu_support(message: Message):
    await clean_chat(message.chat.id, message.message_id)
    await message.answer("👨‍💻 По всем вопросам пишите: @твой_юзернейм_поддержки")

# --- ЛОГИКА STARS ---
@dp.callback_query(F.data.startswith("stars_") & (F.data != "stars_custom"))
async def choose_stars(call: CallbackQuery, state: FSMContext):
    amount = int(call.data.split("_")[1])
    await state.update_data(stars_amount=amount)
    await state.set_state(ShopStates.waiting_for_stars_username)
    await clean_chat(call.message.chat.id, call.message.message_id)
    await call.message.answer(f"Вы выбрали <b>{amount} Stars</b>.\n\nВведите ваш <b>@username</b>:", parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "stars_custom")
async def custom_stars(call: CallbackQuery, state: FSMContext):
    await state.set_state(ShopStates.waiting_for_custom_stars)
    await clean_chat(call.message.chat.id, call.message.message_id)
    await call.message.answer("Введите количество Stars (от 50 до 50000):")
    await call.answer()

@dp.message(ShopStates.waiting_for_custom_stars)
async def process_custom_stars(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if not (50 <= amount <= 50000): raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 50 до 50000.")
        return
    await state.update_data(stars_amount=amount)
    await state.set_state(ShopStates.waiting_for_stars_username)
    await clean_chat(message.chat.id, message.message_id)
    await message.answer(f"Вы выбрали <b>{amount} Stars</b>.\n\nВведите ваш <b>@username</b>:", parse_mode="HTML")

@dp.message(ShopStates.waiting_for_stars_username)
async def process_stars_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username.startswith("@"):
        await message.answer("❌ Введите юзернейм, начиная с @.")
        return
    await state.update_data(stars_username=username)
    await clean_chat(message.chat.id, message.message_id)
    data = await state.get_data()
    stars_amount = data["stars_amount"]
    price_rub = stars_amount * SETTINGS['rate_rub']
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Рубли (xRocket)", callback_data="pay_stars_xrocket")],
        [InlineKeyboardButton(text="💎 Gram (Tonkeeper)", callback_data="pay_stars_ton")],
        [InlineKeyboardButton(text="💳 Карта BY", callback_data="pay_stars_card")],
    ])
    await message.answer(f"Получатель: <b>{username}</b>\nК оплате: <b>{price_rub:.0f} RUB</b>\n\nВыбери способ оплаты:", reply_markup=kb, parse_mode="HTML")

# --- ОПЛАТА STARS ---
@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_stars_payment(call: CallbackQuery, state: FSMContext):
    method = call.data.split("_")[2]
    data = await state.get_data()
    stars_amount = data["stars_amount"]
    username = data["stars_username"]
    await state.clear()
    await clean_chat(call.message.chat.id, call.message.message_id)
    price_rub = stars_amount * SETTINGS['rate_rub']
    
    if method == "xrocket":
        try:
            invoice_data = await create_xrocket_invoice(amount=price_rub, currency="RUB", description=f"Покупка {stars_amount} Stars")
            links = invoice_data.get("links", {})
            pay_link = links.get("telegramBotLink") or links.get("webLink") or links.get("url")
            invoice_id = invoice_data.get("id")
            orders[invoice_id] = {"type": "stars", "username": username, "amount": stars_amount, "price_rub": price_rub}
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_link)],
                [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_{invoice_id}")]
            ])
            await call.message.answer(f"<b>Счет создан</b>\n\nТовар: {stars_amount} Stars\nСумма: {price_rub:.0f} RUB\n\nОплати по кнопке ниже.", reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            await call.message.answer(f"❌ Ошибка: {e}")
    elif method == "ton":
        price_ton = price_rub / SETTINGS['gram_price_min']
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я перевел", callback_data=f"check_manual_stars_{username}_{stars_amount}")]])
        text = f"<b>Оплата Stars через Tonkeeper</b>\n\n1. Переведите <b>{price_ton:.2f} TON</b> на:\n<code>{MY_TON_WALLET}</code>\n\n2. Комментарий: <code>{call.from_user.id}</code>"
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await bot.send_message(ADMIN_ID, f"⏳ Ожидается TON за Stars!\nUser: @{call.from_user.username}\nStars: {stars_amount}\nКому: {username}")
    elif method == "card":
        price_byn = price_rub / SETTINGS['rate_byn']
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я перевел", callback_data=f"check_manual_stars_{username}_{stars_amount}")]])
        text = f"<b>Оплата Stars картой BY</b>\n\n1. Переведите <b>{price_byn:.2f} BYN</b> на:\n<code>{BY_CARD_NUMBER}</code>\n\n2. Комментарий: <code>{call.from_user.id}</code>"
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await bot.send_message(ADMIN_ID, f"⏳ Ожидается карта BY за Stars!\nUser: @{call.from_user.username}\nStars: {stars_amount}\nКому: {username}")
    await call.answer()

@dp.callback_query(F.data.startswith("check_manual_stars_"))
async def check_manual_stars(call: CallbackQuery):
    parts = call.data.split("_")
    username = parts[3]
    stars_amount = int(parts[4])
    await call.answer("Заявка отправлена.", show_alert=True)
    await clean_chat(call.message.chat.id, call.message.message_id)
    await call.message.answer("⏳ Ожидайте подтверждения.")
    await bot.send_message(ADMIN_ID, f"🔥 <b>ПОДТВЕРДИТЕ ОПЛАТУ!</b>\nКому: {username}\nКол-во: {stars_amount}")

# --- ЛОГИКА GRAM ---
@dp.callback_query(F.data.startswith("gram_pay_"))
async def gram_payment_method(call: CallbackQuery, state: FSMContext):
    method = call.data.split("_")[2]
    await state.update_data(gram_pay_method=method)
    await state.set_state(ShopStates.waiting_for_gram_username)
    await clean_chat(call.message.chat.id, call.message.message_id)
    await call.message.answer("Введите ваш <b>TON-адрес</b> (UQ... или EQ...):", parse_mode="HTML")
    await call.answer()

@dp.message(ShopStates.waiting_for_gram_username)
async def process_gram_username(message: Message, state: FSMContext):
    ton_address = message.text.strip()
    if not (ton_address.startswith("UQ") or ton_address.startswith("EQ")):
        await message.answer("❌ Это не похоже на TON-адрес.")
        return
    data = await state.get_data()
    method = data["gram_pay_method"]
    await state.clear()
    await clean_chat(message.chat.id, message.message_id)
    if method == "card":
        amount_byn = SETTINGS['gram_price_min'] / SETTINGS['rate_byn']
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я перевел", callback_data=f"check_gram_manual_{ton_address}")]])
        await message.answer(f"Оплатите <b>{amount_byn:.2f} BYN</b> на карту:\n<code>{BY_CARD_NUMBER}</code>\n\nКомментарий: <code>{message.from_user.id}</code>", reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(f"Для оплаты Gram звездами, переведите их админу.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я перевел", callback_data=f"check_gram_manual_{ton_address}")]]))
    await bot.send_message(ADMIN_ID, f"⏳ Заказ на Gram!\nАдрес: {ton_address}\nМетод: {method}\nID: {message.from_user.id}")

# --- ПОДТВЕРЖДЕНИЕ ОПЛАТЫ И ЧЕКИ ---
@dp.callback_query(F.data.startswith("check_") & ~F.data.startswith("check_manual_stars_") & ~F.data.startswith("check_gram_manual_"))
async def check_payment(call: CallbackQuery, state: FSMContext):
    order_id = call.data.split("_")[1]
    order = orders.get(order_id)
    await call.answer("Проверяем...")
    try:
        url = f"https://pay.api.xrocket.exchange/api/v1/invoice?invoiceId={order_id}"
        headers = {"Authorization": f"Bearer {XROCKET_API_KEY}"}
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("status", "").lower() == "paid":
                    await clean_chat(call.message.chat.id, call.message.message_id)
                    await call.message.answer("✅ <b>Оплата получена!</b>")
                    
                    if order and order.get("type") == "stars":
                        try:
                            await buy_stars_fragment(order["username"], order["amount"])
                            
                            # Генерируем чек
                            receipt_text = f"🧾 <b>ЧЕК №{order_id[:8]}</b>\nТовар: {order['amount']} Stars\nСумма: {order['price_rub']:.0f} RUB\nСтатус: Оплачено ✅"
                            add_receipt(call.from_user.id, receipt_text)
                            
                            await call.message.answer(receipt_text, parse_mode="HTML")
                            await call.message.answer("🎉 Звезды отправлены! Оставьте отзыв (или напишите /start для выхода):")
                            await state.set_state(ShopStates.waiting_for_review)
                        except Exception as e:
                            await bot.send_message(ADMIN_ID, f"❌ Ошибка Fragment: {e}")
                else:
                    await call.answer("Оплата еще не поступила.", show_alert=True)
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)

@dp.message(ShopStates.waiting_for_review)
async def process_review(message: Message, state: FSMContext):
    REVIEWS.append({"user": message.from_user.username or "Аноним", "text": message.text})
    await state.clear()
    await clean_chat(message.chat.id, message.message_id)
    await message.answer("🙏 Спасибо за отзыв!", reply_markup=get_main_menu())
    await bot.send_message(ADMIN_ID, f"📝 Новый отзыв от @{message.from_user.username}:\n{message.text}")

async def main():
    global bot, session
    session = aiohttp.ClientSession()
    bot = Bot(token=BOT_TOKEN)
    Thread(target=run_flask, daemon=True).start()
    print("🚀 Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен")

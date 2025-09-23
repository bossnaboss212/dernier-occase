"""
Telegram Mini-Shop Bot (Cash Only) — Millau-focused Delivery

Features:
- Cash-only checkout
- Stock management (deduct on delivery)
- CA (revenue) & treasury tracking
- Anonymous order dispatch to courier channel
- Reviews / ratings
- Staff & admin roles
- "Envie de postuler" (job applications)
- Contact support
- Delivery pricing: Millau (free), outside Millau tiered; here: 20/30/50 km tiers, blocked beyond max
- Distance-based pricing stored in DB (/fees, /set_fees)
- /export_ca → CSV + PDF summaries
- Optional 10€ discount toggle (global) + promo code TRESORERIE10
- /ping test (also posts to courier channel)

Stack:
- Python 3.10+
- aiogram >= 3.4
- sqlite3
- reportlab
- python-dotenv
"""

import asyncio
import csv
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from typing import List, Optional, Tuple
import json
import random
import string

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.types import WebAppInfo
from aiogram.client.default import DefaultBotProperties   # <= ajoute ça

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdf_canvas

from dotenv import load_dotenv
load_dotenv()

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COURIER_CHANNEL_ID = int(os.getenv("COURIER_CHANNEL_ID", "0"))  # negative for channels
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # superadmin fallback
DB_PATH = os.getenv("DB_PATH", "shop.db")

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in environment or .env")

# ---------- Pricing / Discount ----------
MILLAU_CITY = "Millau"

# Defaults used on first run, then editable via /set_fees
DEFAULT_TIERED_FEES = [  # (max_distance_km, fee)
    (20, 20.0),
    (30, 30.0),
    (50, 50.0),
]
DEFAULT_PER_KM_ABOVE_MAX = 0.0  # not used because we block > 50 km; keep for schema

# Global discount (can be toggled later if needed)
GLOBAL_DISCOUNT_ACTIVE = True
GLOBAL_DISCOUNT_EUR = 10.0
PROMO_CODE = "TRESORERIE10"

# ---------- DB Schema ----------
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    role TEXT DEFAULT 'customer', -- customer|staff|admin
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    price REAL NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS carts (
    user_id INTEGER,
    product_id INTEGER,
    qty INTEGER,
    PRIMARY KEY (user_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE,
    user_id INTEGER,
    items_json TEXT,
    subtotal REAL,
    discount REAL,
    delivery_fee REAL,
    total REAL,
    address TEXT,
    city TEXT,
    distance_km REAL,
    status TEXT, -- pending|assigned|out_for_delivery|delivered|cancelled
    courier_user_id INTEGER,
    created_at TEXT,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    rating INTEGER,
    text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS support (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    text TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS treasury (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    entry_type TEXT, -- sale|refund|adjustment
    amount REAL,
    created_at TEXT
);

-- key/value settings (JSON)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db()) as conn:
        conn.executescript(SCHEMA_SQL)
        # seed default fees if not set
        cur = conn.execute("SELECT value FROM settings WHERE key='fees'")
        if not cur.fetchone():
            fees_payload = {
                "tiers": DEFAULT_TIERED_FEES,
                "per_km": DEFAULT_PER_KM_ABOVE_MAX
            }
            conn.execute("INSERT INTO settings(key,value) VALUES('fees',?)",
                         (json.dumps(fees_payload),))
        conn.commit()

# ---------- Helpers ----------
def gen_code(prefix: str = "CMD") -> str:
    tail = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{tail}"

def ensure_user(user_id: int):
    with closing(db()) as conn:
        cur = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if not cur.fetchone():
            conn.execute(
                "INSERT INTO users(user_id, role, created_at) VALUES (?, 'customer', ?)",
                (user_id, datetime.utcnow().isoformat()),
            )
            conn.commit()

def get_role(user_id: int) -> str:
    with closing(db()) as conn:
        cur = conn.execute("SELECT role FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row["role"] if row else "customer"

def set_role(user_id: int, role: str):
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO users(user_id, role, created_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
            (user_id, role, datetime.utcnow().isoformat()))
        conn.commit()

def list_products(active_only: bool = True) -> List[sqlite3.Row]:
    with closing(db()) as conn:
        if active_only:
            cur = conn.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id")
        else:
            cur = conn.execute("SELECT * FROM products ORDER BY id")
        return cur.fetchall()

def add_product(name: str, price: float, stock: int):
    with closing(db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO products(name, price, stock, is_active) VALUES (?,?,?,1)",
            (name, price, stock),
        )
        conn.commit()

def update_stock(product_id: int, delta: int):
    with closing(db()) as conn:
        conn.execute("UPDATE products SET stock = stock + ? WHERE id=?",
                     (delta, product_id))
        conn.commit()

def add_to_cart(user_id: int, product_id: int, qty: int):
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO carts(user_id, product_id, qty) VALUES (?,?,?) "
            "ON CONFLICT(user_id,product_id) DO UPDATE SET qty=qty+excluded.qty",
            (user_id, product_id, qty),
        )
        conn.commit()

def get_cart(user_id: int) -> List[Tuple[sqlite3.Row, int]]:
    with closing(db()) as conn:
        cur = conn.execute("SELECT product_id, qty FROM carts WHERE user_id=?",
                           (user_id,))
        items = []
        for r in cur.fetchall():
            pcur = conn.execute("SELECT * FROM products WHERE id=?",
                                (r["product_id"],))
            p = pcur.fetchone()
            if p:
                items.append((p, r["qty"]))
        return items

def clear_cart(user_id: int):
    with closing(db()) as conn:
        conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
        conn.commit()

# fees in DB
def get_fees():
    with closing(db()) as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key='fees'")
        row = cur.fetchone()
        if not row:
            return {"tiers": DEFAULT_TIERED_FEES, "per_km": DEFAULT_PER_KM_ABOVE_MAX}
        try:
            data = json.loads(row[0])
            tiers = [(float(a), float(b)) for a, b in data.get("tiers", DEFAULT_TIERED_FEES)]
            per_km = float(data.get("per_km", DEFAULT_PER_KM_ABOVE_MAX))
            return {"tiers": tiers, "per_km": per_km}
        except Exception:
            return {"tiers": DEFAULT_TIERED_FEES, "per_km": DEFAULT_PER_KM_ABOVE_MAX}

def set_fees(tiers: List[Tuple[float, float]], per_km: float):
    payload = {"tiers": tiers, "per_km": per_km}
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('fees',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(payload),))
        conn.commit()

def compute_delivery_fee(city: str, distance_km: float) -> float:
    if city.strip().lower() == MILLAU_CITY.lower():
        return 0.0
    fees = get_fees()
    tiers = fees["tiers"]
    for max_km, fee in tiers:
        if distance_km <= max_km:
            return fee
    # Block delivery beyond the last tier
    max_cap, base_fee = tiers[-1]
    if distance_km > float(max_cap):
        raise ValueError("Zone de livraison non couverte (au-delà des paliers définis)")
    return float(base_fee)

# ---------- FSM States ----------
class Checkout(StatesGroup):
    waiting_address = State()
    waiting_city = State()
    waiting_distance = State()
    waiting_promo = State()

class Review(StatesGroup):
    waiting_rating = State()
    waiting_text = State()

class Postuler(StatesGroup):
    waiting_text = State()

class Support(StatesGroup):
    waiting_text = State()

# ---------- Bot ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ---------- Keyboards ----------
def main_menu_kb(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text="🛍️ Boutique",
            web_app=WebAppInfo(url="https://bossnaboss212.github.io/dernier-occase/webapp/index.html")
        )],
        [InlineKeyboardButton(text="🛒 Catalogue", callback_data="catalogue"),
         InlineKeyboardButton(text="🧺 Panier", callback_data="panier")],
        [InlineKeyboardButton(text="🚚 Commander (cash)", callback_data="checkout")],
        [InlineKeyboardButton(text="💸 Tarifs livraison", callback_data="fees")],
        [InlineKeyboardButton(text="⭐ Laisser un avis", callback_data="avis"),
         InlineKeyboardButton(text="🧑‍💼 Postuler", callback_data="postuler")],
        [InlineKeyboardButton(text="🆘 Assistance", callback_data="support")],
    ]

    # si staff ou admin → on ajoute le bouton gestion stock
    if role in ("staff", "admin"):
        buttons.append([InlineKeyboardButton(text="📦 Gestion stock", callback_data="admin_stock"),
                        InlineKeyboardButton(text="📈 Export CA", callback_data="export_ca")])

    # si admin → bouton admin spécial
    if role == "admin":
        buttons.append([InlineKeyboardButton(text="⚙️ Admin", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- Commands ----------
@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m.from_user.id)
    role = get_role(m.from_user.id)
    await m.answer(
        "👋 Bienvenue dans la mini-boutique Telegram (paiement <b>espèces</b> uniquement).\n"
        "Livraison : <b>Millau gratuite</b>. Hors Millau : "
        "<b>20€ / 30€ / 50€</b> selon la distance. (>50 km : non couvert)",
        reply_markup=main_menu_kb(role),
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "Commandes utiles:\n"
        "/start — menu\n"
        "/add_product nom|prix|stock — admin/staff\n"
        "/set_role user_id role — admin (customer|staff|admin)\n"
        "/fees — afficher tarifs livraison\n"
        "/set_fees 20:20,30:30,50:50 — admin (paliers)\n"
        "/export_ca — admin/staff (CSV + PDF)\n"
        "/ping — test bot + message canal livreurs"
    )

@dp.message(Command("ping"))
async def ping_cmd(m: Message):
    await m.answer("pong ✅ (bot en ligne)")
    try:
        if COURIER_CHANNEL_ID:
            await bot.send_message(
                COURIER_CHANNEL_ID,
                "🔔 Test Ping reçu — le bot est bien connecté au canal livreurs !"
            )
    except Exception as e:
        await m.answer(f"⚠️ Erreur envoi canal: {e}")

@dp.message(Command("add_product"))
async def cmd_add_product(m: Message):
    if get_role(m.from_user.id) not in ("admin", "staff"):
        return await m.answer("⛔ Autorisation requise.")
    try:
        _, rest = m.text.split(" ", 1)
        name, price, stock = [x.strip() for x in rest.split("|")]
        add_product(name, float(price), int(stock))
        await m.answer(f"✅ Produit ajouté: {name} ({price}€, stock {stock})")
    except Exception:
        await m.answer("Format: /add_product Nom|12.5|10")
@dp.message(Command("shop"))
async def open_shop(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛍️ Ouvrir la boutique",
            web_app=WebAppInfo(
                url="https://bossnaboss212.github.io/dernier-occase/webapp/index.html"
            )
        )
    ]])
    await m.answer("Ouvre la boutique mini-app :", reply_markup=kb)

@dp.message(Command("set_role"))
async def cmd_set_role(m: Message):
    if get_role(m.from_user.id) != "admin" and m.from_user.id != OWNER_ID:
        return await m.answer("⛔ Admin uniquement.")
    try:
        _, uid, role = m.text.split()
        set_role(int(uid), role)
        await m.answer("✅ Rôle mis à jour.")
    except Exception:
        await m.answer("Format: /set_role <user_id> <customer|staff|admin>")

@dp.message(Command("fees"))
async def cmd_fees(m: Message):
    f = get_fees()
    tiers_lines = "\n".join([f"≤{a:g} km: {b:.2f}€" for a, b in f["tiers"]])
    await m.answer(
        "Tarifs actuels:\n" + tiers_lines + "\n>50 km: non couvert\nMillau: 0€"
    )

@dp.message(Command("set_fees"))
async def cmd_set_fees(m: Message):
    if get_role(m.from_user.id) != "admin":
        return await m.answer("⛔ Admin uniquement.")
    try:
        # Syntaxe: /set_fees 20:20,30:30,50:50
        _, payload = m.text.split(" ", 1)
        tiers = []
        for chunk in payload.split(","):
            max_km, fee = chunk.split(":")
            tiers.append((float(max_km), float(fee)))
        tiers.sort(key=lambda x: x[0])
        set_fees(tiers, 0.0)
        await m.answer("✅ Tarifs mis à jour.")
    except Exception:
        await m.answer("Format: /set_fees 20:20,30:30,50:50")

@dp.message(Command("export_ca"))
async def cmd_export_ca(m: Message):
    if get_role(m.from_user.id) not in ("admin", "staff"):
        return await m.answer("⛔ Autorisation requise.")
    csv_path, pdf_path = export_ca_files()
    await m.answer_document(document=FSInputFile(csv_path, filename="ca_export.csv"))
    await m.answer_document(document=FSInputFile(pdf_path, filename="ca_export.pdf"))

# ---------- Callbacks ----------
@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    role = get_role(c.from_user.id)
    await c.message.edit_text("Menu principal", reply_markup=main_menu_kb(role))
    await c.answer()

@dp.callback_query(F.data == "fees")
async def cb_fees(c: CallbackQuery):
    fees = get_fees()
    tiers = fees["tiers"]
    lines = ["<b>Tarifs livraison</b>"]
    lines.append("Millau: <b>0€</b>")
    for max_km, fee in tiers:
        lines.append(f"≤{max_km:g} km: <b>{fee:.2f}€</b>")
    lines.append(">50 km: ❌ non couvert")
    await c.message.edit_text("\n".join(lines), reply_markup=back_home_kb(get_role(c.from_user.id)))
    await c.answer()

@dp.callback_query(F.data == "catalogue")
async def cb_catalogue(c: CallbackQuery):
    ps = list_products()
    if not ps:
        await c.message.edit_text("Catalogue vide.", reply_markup=back_home_kb(get_role(c.from_user.id)))
        return await c.answer()
    lines = ["<b>Catalogue</b>"]
    kb_rows = []
    for p in ps:
        lines.append(f"#{p['id']} — {p['name']} — {p['price']:.2f}€ — stock {p['stock']}")
        kb_rows.append([InlineKeyboardButton(text=f"+ {p['name']}", callback_data=f"addcart:{p['id']}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Menu", callback_data="home")])
    await c.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()

@dp.callback_query(F.data.startswith("addcart:"))
async def cb_addcart(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    add_to_cart(c.from_user.id, pid, 1)
    await c.answer("Ajouté au panier.")

@dp.callback_query(F.data == "panier")
async def cb_panier(c: CallbackQuery):
    items = get_cart(c.from_user.id)
    if not items:
        await c.message.edit_text("Votre panier est vide.", reply_markup=back_home_kb(get_role(c.from_user.id)))
        return await c.answer()
    total = 0.0
    lines = ["<b>Votre panier</b>"]
    for p, qty in items:
        line_total = p["price"] * qty
        total += line_total
        lines.append(f"{p['name']} x{qty} — {line_total:.2f}€")
    lines.append(f"Sous-total: <b>{total:.2f}€</b>")
    await c.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Passer au checkout", callback_data="checkout")],
            [InlineKeyboardButton(text="⬅️ Menu", callback_data="home")]
        ])
    )
    await c.answer()

@dp.callback_query(F.data == "checkout")
async def cb_checkout(c: CallbackQuery, state: FSMContext):
    items = get_cart(c.from_user.id)
    if not items:
        await c.answer("Panier vide.")
        return
    await state.set_state(Checkout.waiting_address)
    await c.message.edit_text("📍 Envoyez votre adresse complète (rue, n°, complément).\n(Paiement en espèces à la livraison)")

@dp.message(Checkout.waiting_address)
async def checkout_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text.strip())
    await state.set_state(Checkout.waiting_city)
    await m.answer("🏙️ Ville ? (ex: Millau)")

@dp.message(Checkout.waiting_city)
async def checkout_city(m: Message, state: FSMContext):
    await state.update_data(city=m.text.strip())
    await state.set_state(Checkout.waiting_distance)
    await m.answer("📏 Distance estimée jusqu'à notre dépôt (en km). (Si vous êtes à Millau, répondez 0)")

@dp.message(Checkout.waiting_distance)
async def checkout_distance(m: Message, state: FSMContext):
    try:
        dist = float(m.text.replace(",", ".").strip())
    except Exception:
        return await m.answer("Veuillez entrer un nombre (km).")
    await state.update_data(distance_km=dist)
    await state.set_state(Checkout.waiting_promo)
    await m.answer("🎟️ Code promo ? (envoyez le code, ou 'non')")

@dp.message(Checkout.waiting_promo)
async def checkout_finalize(m: Message, state: FSMContext):
    data = await state.get_data()
    address = data.get("address")
    city = data.get("city")
    distance_km = float(data.get("distance_km", 0))

    items = get_cart(m.from_user.id)
    if not items:
        await state.clear()
        return await m.answer("Panier vide.")

    # Validate stock availability at checkout time (no deduction yet)
    with closing(db()) as conn:
        for p, qty in items:
            cur = conn.execute("SELECT stock FROM products WHERE id=?", (p["id"],))
            st = cur.fetchone()[0]
            if st < qty:
                await state.clear()
                return await m.answer(f"Stock insuffisant pour {p['name']} (restant {st}).")

    try:
        delivery_fee = compute_delivery_fee(city, distance_km)
    except ValueError as e:
        await state.clear()
        return await m.answer(str(e))

    subtotal = sum(p["price"] * qty for p, qty in items)
    promo_code = m.text.strip().upper()
    discount = 0.0
    if GLOBAL_DISCOUNT_ACTIVE:
        discount += GLOBAL_DISCOUNT_EUR
    if promo_code == PROMO_CODE:
        discount += 10.0

    total = max(0.0, subtotal - discount) + delivery_fee

    code = gen_code()
    items_json = json.dumps([
        {"id": p["id"], "name": p["name"], "price": p["price"], "qty": qty}
        for p, qty in items
    ])

    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO orders(code,user_id,items_json,subtotal,discount,delivery_fee,total,address,city,distance_km,status,courier_user_id,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code,
                m.from_user.id,
                items_json,
                subtotal,
                discount,
                delivery_fee,
                total,
                address,
                city,
                distance_km,
                "pending",
                None,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()

    # Send anonymous order to courier channel
    if COURIER_CHANNEL_ID:
        txt = (f"📦 Nouvelle commande <b>{code}</b>\n"
               f"Articles:")
        for p, qty in items:
            txt += f"\n• {p['name']} x{qty}"
        txt += (f"\n\nLivraison: {city}, {distance_km:.1f} km\n"
                f"Adresse: {address}\n"
                f"Paiement: <b>Espèces</b>\n"
                f"Total à encaisser: <b>{total:.2f}€</b>\n"
                f"(Client anonyme)")
        await bot.send_message(COURIER_CHANNEL_ID, txt)

    clear_cart(m.from_user.id)
    await state.clear()

    await m.answer(
        "✅ Commande enregistrée!\n"
        f"Code: <b>{code}</b>\n"
        f"Sous-total: {subtotal:.2f}€ | Réduc: −{discount:.2f}€ | Livraison: {delivery_fee:.2f}€\n"
        f"Total: <b>{total:.2f}€</b>\n"
        "Un livreur vous contacte. Préparez l'appoint en espèces."
    )

@dp.callback_query(F.data == "avis")
async def cb_avis(c: CallbackQuery, state: FSMContext):
    await state.set_state(Review.waiting_rating)
    await c.message.edit_text("Donnez une note (1-5):", reply_markup=back_home_kb(get_role(c.from_user.id)))
    await c.answer()

@dp.message(Review.waiting_rating)
async def review_rating(m: Message, state: FSMContext):
    try:
        rating = int(m.text)
        assert 1 <= rating <= 5
    except Exception:
        return await m.answer("Entrez un nombre 1-5.")
    await state.update_data(rating=rating)
    await state.set_state(Review.waiting_text)
    await m.answer("Écrivez votre avis (court).")

@dp.message(Review.waiting_text)
async def review_text(m: Message, state: FSMContext):
    data = await state.get_data()
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO reviews(user_id,rating,text,created_at) VALUES(?,?,?,?)",
            (m.from_user.id, int(data["rating"]), m.text.strip(), datetime.utcnow().isoformat()),
        )
        conn.commit()
    await state.clear()
    await m.answer("Merci pour votre avis ⭐")

@dp.callback_query(F.data == "postuler")
async def cb_postuler(c: CallbackQuery, state: FSMContext):
    await state.set_state(Postuler.waiting_text)
    await c.message.edit_text("Expliquez votre expérience et dispo.")
    await c.answer()

@dp.message(Postuler.waiting_text)
async def postuler_text(m: Message, state: FSMContext):
    with closing(db()) as conn:
        conn.execute("INSERT INTO applications(user_id,text,created_at) VALUES(?,?,?)",
                     (m.from_user.id, m.text.strip(), datetime.utcnow().isoformat()))
        conn.commit()
    await state.clear()
    await m.answer("Candidature reçue ✅")

@dp.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.set_state(Support.waiting_text)
    await c.message.edit_text("Décrivez votre problème. Un agent vous répondra.")
    await c.answer()

@dp.message(Support.waiting_text)
async def support_text(m: Message, state: FSMContext):
    with closing(db()) as conn:
        conn.execute("INSERT INTO support(user_id,text,created_at) VALUES(?,?,?)",
                     (m.from_user.id, m.text.strip(), datetime.utcnow().isoformat()))
        conn.commit()
    await state.clear()
    await m.answer("Merci, l'assistance vous contactera sous peu.")

@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(c: CallbackQuery):
    if get_role(c.from_user.id) != "admin":
        return await c.answer("⛔ Admin uniquement.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Assigner livreur", callback_data="admin_assign")],
        [InlineKeyboardButton(text="Marquer livrée (déduit stock)", callback_data="admin_delivered")],
        [InlineKeyboardButton(text="⬅️ Menu", callback_data="home")]
    ])
    await c.message.edit_text("Panneau admin", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "admin_stock")
async def cb_admin_stock(c: CallbackQuery):
    if get_role(c.from_user.id) not in ("admin", "staff"):
        return await c.answer("⛔", show_alert=True)
    ps = list_products(active_only=False)
    lines = ["<b>Stock</b>"]
    for p in ps:
        lines.append(f"#{p['id']} {p['name']}: {p['stock']} en stock | {p['price']:.2f}€")
    await c.message.edit_text("\n".join(lines), reply_markup=back_home_kb(get_role(c.from_user.id)))
    await c.answer()

@dp.callback_query(F.data == "export_ca")
async def cb_export(c: CallbackQuery):
    if get_role(c.from_user.id) not in ("admin", "staff"):
        return await c.answer("⛔", show_alert=True)
    csv_path, pdf_path = export_ca_files()
    await bot.send_document(c.message.chat.id, FSInputFile(csv_path, filename="ca_export.csv"))
    await bot.send_document(c.message.chat.id, FSInputFile(pdf_path, filename="ca_export.pdf"))
    await c.answer("Export envoyé.")

# ---------- Admin Ops ----------
def get_open_orders() -> List[sqlite3.Row]:
    with closing(db()) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE status IN ('pending','assigned','out_for_delivery') ORDER BY id DESC")
        return cur.fetchall()

def set_order_assigned(order_code: str, courier_user_id: int):
    with closing(db()) as conn:
        conn.execute("UPDATE orders SET status='assigned', courier_user_id=? WHERE code=?",
                     (courier_user_id, order_code))
        conn.commit()

def mark_order_delivered(order_code: str) -> Optional[int]:
    """Mark delivered; deduct stock; insert treasury sale. Returns order id or None."""
    with closing(db()) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE code=? AND status != 'delivered'", (order_code,))
        order = cur.fetchone()
        if not order:
            return None
        items = json.loads(order["items_json"]) or []
        for it in items:
            conn.execute("UPDATE products SET stock=stock-? WHERE id=?",
                         (int(it["qty"]), int(it["id"])))
        conn.execute("UPDATE orders SET status='delivered', delivered_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), order["id"]))
        conn.execute("INSERT INTO treasury(order_id, entry_type, amount, created_at) VALUES(?,?,?,?)",
                     (order["id"], "sale", float(order["total"]), datetime.utcnow().isoformat()))
        conn.commit()
        return order["id"]

@dp.message(Command("assign"))
async def cmd_assign(m: Message):
    if get_role(m.from_user.id) != "admin":
        return await m.answer("⛔ Admin uniquement.")
    try:
        _, code, courier_id = m.text.split()
        set_order_assigned(code, int(courier_id))
        await m.answer("✅ Assigné.")
    except Exception:
        await m.answer("Format: /assign CODE courier_user_id")

@dp.message(Command("delivered"))
async def cmd_delivered(m: Message):
    if get_role(m.from_user.id) not in ("admin", "staff"):
        return await m.answer("⛔")
    try:
        _, code = m.text.split()
        oid = mark_order_delivered(code)
        if oid is None:
            return await m.answer("Commande introuvable ou déjà livrée.")
        await m.answer("✅ Livraison confirmée. Stock déduit et CA mis à jour.")
    except Exception:
        await m.answer("Format: /delivered CODE")

# ---------- Export CA (CSV + PDF) ----------
def export_ca_files(period_days: int = 30) -> Tuple[str, str]:
    since = datetime.utcnow() - timedelta(days=period_days)
    with closing(db()) as conn:
        cur = conn.execute(
            "SELECT id, code, total, discount, delivery_fee, created_at, delivered_at, status "
            "FROM orders WHERE created_at >= ? ORDER BY id",
            (since.isoformat(),),
        )
        rows = cur.fetchall()

    # CSV
    csv_buf_path = "ca_export.csv"
    with open(csv_buf_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "code", "status", "total", "discount", "delivery_fee", "created_at", "delivered_at"])
        for r in rows:
            w.writerow([r["id"], r["code"], r["status"], f"{r['total']:.2f}", f"{r['discount']:.2f}",
                        f"{r['delivery_fee']:.2f}", r["created_at"], r["delivered_at"] or ""])

    # PDF (simple table)
    pdf_buf_path = "ca_export.pdf"
    c = pdf_canvas.Canvas(pdf_buf_path, pagesize=A4)
    width, height = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(2 * cm, height - 2 * cm, "Export Chiffre d'Affaires (30 jours)")

    y = height - 3 * cm
    c.setFont("Helvetica", 9)
    headers = ["Code", "Statut", "Total€", "Réduc€", "Livraison€", "Créée", "Livrée"]
    col_x = [2*cm, 5*cm, 8*cm, 10*cm, 12*cm, 14*cm, 17*cm]
    for i, htxt in enumerate(headers):
        c.drawString(col_x[i], y, htxt)
    y -= 0.7 * cm

    sum_total = 0.0
    for r in rows:
        if y < 2 * cm:
            c.showPage()
            y = height - 2 * cm
        c.drawString(col_x[0], y, r["code"])
        c.drawString(col_x[1], y, r["status"])
        c.drawRightString(col_x[2]+1*cm, y, f"{r['total']:.2f}")
        c.drawRightString(col_x[3]+1*cm, y, f"{r['discount']:.2f}")
        c.drawRightString(col_x[4]+1*cm, y, f"{r['delivery_fee']:.2f}")
        c.drawString(col_x[5], y, r["created_at"].split("T")[0])
        c.drawString(col_x[6], y, (r["delivered_at"] or "").split("T")[0])
        y -= 0.6 * cm
        sum_total += float(r["total"])

    c.setFont("Helvetica-Bold", 11)
    c.drawString(2 * cm, y - 0.5 * cm, f"Total HT approximatif: {sum_total:.2f}€ (cash)")
    c.save()

    return csv_buf_path, pdf_buf_path

# ---------- Startup ----------
async def on_startup():
    print("Booting bot…", flush=True)
    init_db()
    # Seed demo products if empty
    if not list_products():
        add_product("Bouteille 1.0L", 2.50, 50)
        add_product("Pack 6x0.5L", 6.90, 30)
        add_product("Pod arôme citron", 3.20, 100)

async def main():
    await on_startup()
    print("Starting polling…", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")

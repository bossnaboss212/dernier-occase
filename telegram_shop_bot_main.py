"""
Telegram Mini-Shop Bot (Cash Only) — Millau-focused Delivery
"""

import asyncio
import csv
import os
import sqlite3
import json
import random
import string
from contextlib import closing
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

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
    WebAppInfo,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdf_canvas

from dotenv import load_dotenv
load_dotenv()

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COURIER_CHANNEL_ID = int(os.getenv("COURIER_CHANNEL_ID", "0"))  # peut être négatif pour un channel
OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip())
DB_PATH = os.getenv("DB_PATH", "shop.db")

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN in environment or .env")

# ---------- Pricing / Discount ----------
MILLAU_CITY = "Millau"
DEFAULT_TIERED_FEES = [(20, 20.0), (30, 30.0), (50, 50.0)]  # >50 km : non couvert (bloqué)
# --------- DISCOUNT SETTINGS ---------
GLOBAL_DISCOUNT_ACTIVE = False      # Désactive la remise globale
GLOBAL_DISCOUNT_EUR = 10.0          # Montant prévu (reste défini mais inactif)
PROMO_CODE = "TRESORERIE10"         # Code promo optionnel

# ---------- DB Schema ----------
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    role TEXT DEFAULT 'customer',
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

-- key/value divers (photos produits, frais, etc.)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ---------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db()) as conn:
        conn.executescript(SCHEMA_SQL)
        # seed frais si absent
        cur = conn.execute("SELECT value FROM settings WHERE key='fees'")
        if not cur.fetchone():
            conn.execute("INSERT INTO settings(key,value) VALUES('fees',?)",
                         (json.dumps({"tiers": DEFAULT_TIERED_FEES, "per_km": 0.0}),))
        conn.commit()

# ---------- Helpers ----------
def gen_code(prefix: str = "CMD") -> str:
    return f"{prefix}-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def ensure_user(user_id: int):
    with closing(db()) as conn:
        cur = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if not cur.fetchone():
            conn.execute("INSERT INTO users(user_id, role, created_at) VALUES (?,?,?)",
                         (user_id, "customer", datetime.utcnow().isoformat()))
            conn.commit()

def get_role(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "admin"
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

# --- ACL helpers ---
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID
def is_admin(user_id: int) -> bool:
    return get_role(user_id) == "admin" or is_owner(user_id)
def is_staff(user_id: int) -> bool:
    return get_role(user_id) in ("staff", "admin") or is_owner(user_id)

# --- Product helpers (CRUD) ---
def get_product(pid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        cur = conn.execute("SELECT * FROM products WHERE id=?", (pid,))
        return cur.fetchone()

def list_active_products() -> List[sqlite3.Row]:
    with closing(db()) as conn:
        cur = conn.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id")
        return cur.fetchall()

def list_inactive_products() -> List[sqlite3.Row]:
    with closing(db()) as conn:
        cur = conn.execute("SELECT * FROM products WHERE is_active=0 ORDER BY id")
        return cur.fetchall()

def add_product(name: str, price: float, stock: int):
    with closing(db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO products(name, price, stock, is_active) VALUES (?,?,?,1)",
            (name, price, stock)
        )
        conn.commit()

def set_price(pid: int, price: float):
    with closing(db()) as conn:
        conn.execute("UPDATE products SET price=? WHERE id=?", (price, pid))
        conn.commit()

def set_stock_absolute(pid: int, stock: int):
    with closing(db()) as conn:
        conn.execute("UPDATE products SET stock=? WHERE id=?", (stock, pid))
        conn.commit()

def deactivate_product(pid: int):
    with closing(db()) as conn:
        conn.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
        conn.commit()

def reactivate_product(pid: int):
    with closing(db()) as conn:
        conn.execute("UPDATE products SET is_active=1 WHERE id=?", (pid,))
        conn.commit()

def set_photo_by_name(name: str, file_id_or_url: str):
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"product_photo_{name}", file_id_or_url),
        )
        conn.commit()

def set_product_name(pid: int, new_name: str):
    with closing(db()) as conn:
        conn.execute("UPDATE products SET name=? WHERE id=?", (new_name, pid))
        conn.commit()

# --- Cart helpers ---
def add_to_cart(user_id: int, product_id: int, qty: int):
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO carts(user_id, product_id, qty) VALUES (?,?,?) "
            "ON CONFLICT(user_id,product_id) DO UPDATE SET qty=qty+excluded.qty",
            (user_id, product_id, qty),
        )
        conn.commit()

def get_cart(user_id: int):
    with closing(db()) as conn:
        cur = conn.execute("SELECT product_id, qty FROM carts WHERE user_id=?", (user_id,))
        items = []
        for r in cur.fetchall():
            p = conn.execute("SELECT * FROM products WHERE id=?", (r["product_id"],)).fetchone()
            if p:
                items.append((p, r["qty"]))
        return items

def clear_cart(user_id: int):
    with closing(db()) as conn:
        conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
        conn.commit()

# --- Fees helpers ---
def get_fees():
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='fees'").fetchone()
        if not row:
            return {"tiers": DEFAULT_TIERED_FEES, "per_km": 0.0}
        try:
            data = json.loads(row["value"])
            tiers = [(float(a), float(b)) for a, b in data.get("tiers", DEFAULT_TIERED_FEES)]
            return {"tiers": tiers, "per_km": float(data.get("per_km", 0.0))}
        except Exception:
            return {"tiers": DEFAULT_TIERED_FEES, "per_km": 0.0}

def set_fees(tiers: List[Tuple[float, float]], per_km: float = 0.0):
    payload = {"tiers": tiers, "per_km": per_km}
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('fees',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(payload),)
        )
        conn.commit()

def compute_delivery_fee(city: str, distance_km: float) -> float:
    if city.strip().lower() == MILLAU_CITY.lower():
        return 0.0
    fees = get_fees()
    for max_km, fee in fees["tiers"]:
        if distance_km <= max_km:
            return fee
    raise ValueError("Zone de livraison non couverte (au-delà de 50 km).")

# ---------- FSM ----------
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

class AdminAddProduct(StatesGroup):
    waiting_name = State()
    waiting_price = State()
    waiting_stock = State()
    waiting_photo = State()

class AdminEditProduct(StatesGroup):
    waiting_choose_product = State()
    waiting_choose_field = State()
    waiting_new_value = State()

# ---------- Bot ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ---------- UI ----------
def main_menu_kb(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text="🛍️ Boutique (Mini-App)",
            web_app=WebAppInfo(url="https://bossnaboss212.github.io/dernier-occase/webapp/index.html")
        )],
        [InlineKeyboardButton(text="🛒 Catalogue", callback_data="catalogue"),
         InlineKeyboardButton(text="🧺 Panier", callback_data="panier")],
        [InlineKeyboardButton(text="🚚 Commander (cash)", callback_data="checkout")],
        [InlineKeyboardButton(text="💸 Tarifs livraison", callback_data="fees")],
        [InlineKeyboardButton(text="⭐ Avis", callback_data="avis"),
         InlineKeyboardButton(text="🧑‍💼 Postuler", callback_data="postuler")],
        [InlineKeyboardButton(text="🆘 Assistance", callback_data="support")],
    ]
    if role in ("staff", "admin"):
        buttons.append([InlineKeyboardButton(text="📦 Stock", callback_data="admin_stock"),
                        InlineKeyboardButton(text="📈 Export CA", callback_data="export_ca")])
    if role == "admin":
        buttons.append([InlineKeyboardButton(text="⚙️ Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_home_kb(role: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Menu", callback_data="home")]])

async def edit_or_send(message: Message, text: str, **kwargs):
    """
    Essaie d'éditer le message. Si Telegram renvoie 'message is not modified',
    on envoie un nouveau message avec le même texte/markup.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            await message.answer(text, **kwargs)
        else:
            raise

# ---------- Commands ----------
@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m.from_user.id)
    role = get_role(m.from_user.id)
    await m.answer(
        "👋 Bienvenue dans la mini-boutique Telegram (paiement <b>espèces</b> uniquement).\n"
        "Livraison : <b>Millau gratuite</b>. Hors Millau : <b>20€ / 30€ / 50€</b> selon la distance. (>50 km : non couvert)",
        reply_markup=main_menu_kb(role),
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "Commandes utiles:\n"
        "/start — menu\n"
        "/ping — test\n"
        "/add_product Nom|12.5|10 — admin/staff\n"
        "/set_role user_id customer|staff|admin — admin\n"
        "/fees — afficher tarifs\n"
        "/set_fees 20:20,30:30,50:50 — admin\n"
        "/export_ca — admin/staff\n"
        "/assign CODE courier_user_id — admin\n"
        "/delivered CODE — admin/staff (déduit stock + CA)"
    )

@dp.message(Command("ping"))
async def ping_cmd(m: Message):
    await m.answer("pong ✅")
    try:
        if COURIER_CHANNEL_ID:
            await bot.send_message(COURIER_CHANNEL_ID, "🔔 Test: bot connecté au canal livreurs.")
    except Exception as e:
        await m.answer(f"⚠️ Erreur canal: {e}")

@dp.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"ID: {m.from_user.id}\nRole DB: {get_role(m.from_user.id)}\nOwner: {m.from_user.id == OWNER_ID}")

@dp.message(Command("add_product"))
async def cmd_add_product(m: Message):
    if not is_staff(m.from_user.id):
        return await m.answer("⛔ Autorisation requise.")
    try:
        _, rest = m.text.split(" ", 1)
        name, price, stock = [x.strip() for x in rest.split("|")]
        add_product(name, float(price), int(stock))
        await m.answer(f"✅ Produit ajouté: {name} ({price}€, stock {stock})")
    except Exception:
        await m.answer("Format: /add_product Nom|12.5|10")

@dp.message(Command("set_role"))
async def cmd_set_role(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("🚫 Admin uniquement.")
    try:
        _, uid, role = m.text.split()
        set_role(int(uid), role)
        await m.answer(f"✅ Rôle de l'utilisateur {uid} → {role}")
    except Exception:
        await m.answer("Format correct :\n`/set_role user_id customer|staff|admin`", parse_mode="Markdown")

@dp.message(Command("fees"))
async def cmd_fees(m: Message):
    f = get_fees()
    tiers_lines = "\n".join([f"≤{a:g} km: {b:.2f}€" for a, b in f["tiers"]])
    await m.answer("Tarifs actuels:\n" + tiers_lines + "\n>50 km: non couvert\nMillau: 0€")

@dp.message(Command("set_fees"))
async def cmd_set_fees(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Admin uniquement.")
    try:
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
    if not is_staff(m.from_user.id):
        return await m.answer("⛔ Autorisation requise.")
    csv_path, pdf_path = export_ca_files()
    await m.answer_document(document=FSInputFile(csv_path, filename="ca_export.csv"))
    await m.answer_document(document=FSInputFile(pdf_path, filename="ca_export.pdf"))

# ---------- Catalogue & Panier ----------
@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    role = get_role(c.from_user.id)
    await c.message.edit_text("Menu principal", reply_markup=main_menu_kb(role))
    await c.answer()

@dp.callback_query(F.data == "catalogue")
async def cb_catalogue(c: CallbackQuery):
    products = list_active_products()
    if not products:
        return await c.message.edit_text("📭 Catalogue vide.", reply_markup=back_home_kb(get_role(c.from_user.id)))

    text_lines = ["<b>Produits disponibles :</b>"]
    kb_rows = []
    for p in products:
        text_lines.append(f"• #{p['id']} {p['name']} — {p['price']:.2f}€ (stock {p['stock']})")
        kb_rows.append([InlineKeyboardButton(text=f"+ {p['name']}", callback_data=f"addcart:{p['id']}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Menu", callback_data="home")])

    await c.message.edit_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
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

# ---------- Checkout (cash) ----------
@dp.callback_query(F.data == "checkout")
async def cb_checkout(c: CallbackQuery, state: FSMContext):
    items = get_cart(c.from_user.id)
    if not items:
        await c.answer("Panier vide.")
        return
    await state.set_state(Checkout.waiting_address)
    await c.message.edit_text("📍 Adresse complète ? (rue, n°, complément)\n(Paiement en espèces à la livraison)")

@dp.message(Checkout.waiting_address)
async def checkout_address(m: Message, state: FSMContext):
    await state.update_data(address=m.text.strip())
    await state.set_state(Checkout.waiting_city)
    await m.answer("🏙️ Ville ? (ex: Millau)")

@dp.message(Checkout.waiting_city)
async def checkout_city(m: Message, state: FSMContext):
    await state.update_data(city=m.text.strip())
    await state.set_state(Checkout.waiting_distance)
    await m.answer("📏 Distance estimée (km). Si vous êtes à Millau, répondez 0.")

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

    # Vérif stock
    with closing(db()) as conn:
        for p, qty in items:
            st = conn.execute("SELECT stock FROM products WHERE id=?", (p["id"],)).fetchone()[0]
            if st < qty:
                await state.clear()
                return await m.answer(f"Stock insuffisant pour {p['name']} (restant {st}).")

    try:
        delivery_fee = compute_delivery_fee(city, distance_km)
    except ValueError as e:
        await state.clear()
        return await m.answer(str(e))

    subtotal = sum(p["price"] * qty for p, qty in items)
    promo_code = (m.text or "").strip().upper()
    discount = GLOBAL_DISCOUNT_EUR if GLOBAL_DISCOUNT_ACTIVE else 0.0
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
            (code, m.from_user.id, items_json, subtotal, discount, delivery_fee, total,
             address, city, distance_km, "pending", None, datetime.utcnow().isoformat())
        )
        conn.commit()

    # Message anonyme au canal livreurs
    if COURIER_CHANNEL_ID:
        txt = f"📦 Nouvelle commande <b>{code}</b>\nArticles:"
        for p, qty in items:
            txt += f"\n• {p['name']} x{qty}"
        txt += (f"\n\nLivraison: {city}, {distance_km:.1f} km\n"
                f"Adresse: {address}\nPaiement: <b>Espèces</b>\n"
                f"Total à encaisser: <b>{total:.2f}€</b>\n(Client anonyme)")
        try:
            await bot.send_message(COURIER_CHANNEL_ID, txt)
        except Exception:
            pass

    clear_cart(m.from_user.id)
    await state.clear()

    await m.answer(
        "✅ Commande enregistrée!\n"
        f"Code: <b>{code}</b>\n"
        f"Sous-total: {subtotal:.2f}€ | Réduc: −{discount:.2f}€ | Livraison: {delivery_fee:.2f}€\n"
        f"Total: <b>{total:.2f}€</b>\n"
        "Un livreur vous contacte. Paiement en espèces."
    )

# ---------- Mini-app: réception des données (tg.sendData) ----------
@dp.message(F.web_app_data)
async def handle_webapp(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
        if data.get("type") != "checkout":
            return

        items = data["items"]
        address = data["address"]
        city = data["city"]
        distance_km = float(data.get("distance_km", 0) or 0)
        promo_code = (data.get("promo") or "").strip().upper()

        try:
    subtotal = sum(p["price"] * qty for p, qty in items)
    promo_code = m.text.strip().upper()
    discount = 0.0
except Exception as e:
    await m.answer(f"Erreur calcul promo: {e}")
    return

# --- Remises ---
# (A) Code promo optionnel (tu peux garder/supprimer)
if promo_code == PROMO_CODE:
    discount += 10.0

# (B) Fidélité : -10€ sur la 10ᵉ commande seulement
# On compte les commandes DÉJÀ livrées. Si 9 sont livrées, celle-ci est la 10ᵉ.
with closing(db()) as conn:
    cur = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id=? AND status='delivered'",
        (m.from_user.id,)
    )
    delivered_count = cur.fetchone()[0]

loyalty_msg = ""
if (delivered_count + 1) % 10 == 0:
    discount += 10.0
    loyalty_msg = "🎉 Fidélité: -10€ sur votre 10ᵉ commande !"

        delivery_fee = compute_delivery_fee(city, distance_km)
        total = max(0.0, subtotal - discount) + delivery_fee

        code = gen_code()
        with closing(db()) as conn:
            conn.execute(
                "INSERT INTO orders(code,user_id,items_json,subtotal,discount,delivery_fee,total,address,city,distance_km,status,courier_user_id,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, m.from_user.id, json.dumps(items), subtotal, discount, delivery_fee, total,
                 address, city, distance_km, "pending", None, datetime.utcnow().isoformat())
            )
            conn.commit()

        if COURIER_CHANNEL_ID:
            txt = f"📦 Nouvelle commande <b>{code}</b>\nArticles:" + "".join(
                f"\n• {it['name']} x{it['qty']}" for it in items
            )
            txt += (f"\n\nLivraison: {city}, {distance_km:.1f} km\n"
                    f"Adresse: {address}\nPaiement: <b>Espèces</b>\n"
                    f"Total à encaisser: <b>{total:.2f}€</b>\n(Client anonyme)")
            await bot.send_message(COURIER_CHANNEL_ID, txt)

        await m.answer(
            "✅ Commande enregistrée via la mini-app !\n"
            f"Code: <b>{code}</b>\nTotal: <b>{total:.2f}€</b>\nPréparez l’appoint en espèces."
        )
    except ValueError as e:
        await m.answer(str(e))
    except Exception as e:
        await m.answer(f"❌ Erreur mini-app: {e}")

# ---------- Avis / Postuler / Support ----------
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
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO reviews(user_id,rating,text,created_at) VALUES(?,?,?,?)",
            (m.from_user.id, int((await state.get_data())["rating"]), m.text.strip(), datetime.utcnow().isoformat()),
        )
        conn.commit()
    await state.clear()
    await m.answer("Merci pour votre avis ⭐")

@dp.callback_query(F.data == "postuler")
async def cb_postuler(c: CallbackQuery, state: FSMContext):
    await state.set_state(Postuler.waiting_text)
    await c.message.edit_text("Expliquez votre expérience et dispo.")

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

@dp.message(Support.waiting_text)
async def support_text(m: Message, state: FSMContext):
    with closing(db()) as conn:
        conn.execute("INSERT INTO support(user_id,text,created_at) VALUES(?,?,?)",
                     (m.from_user.id, m.text.strip(), datetime.utcnow().isoformat()))
        conn.commit()
    await state.clear()
    await m.answer("Merci, l'assistance vous contactera sous peu.")

# ---------- Admin: panneau + opérations stock/exports ----------
@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔ Admin uniquement", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ajouter produit", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="✏️ Modifier produit", callback_data="admin_edit_product")],
        [InlineKeyboardButton(text="🗑️ Désactiver produit", callback_data="admin_delete_product")],
        [InlineKeyboardButton(text="♻️ Réactiver produit", callback_data="admin_reactivate_product")],
        [InlineKeyboardButton(text="📦 Voir stock", callback_data="admin_stock")],
        [InlineKeyboardButton(text="⬅️ Retour", callback_data="home")],
    ])
    await edit_or_send(c.message, "⚙️ Panneau Admin :", reply_markup=kb)

@dp.callback_query(F.data == "admin_stock")
async def cb_admin_stock(c: CallbackQuery):
    if not is_staff(c.from_user.id):
        return await c.answer("⛔ Autorisation requise.", show_alert=True)
    ps = list_active_products()
    lines = ["<b>Stock</b>"]
    for p in ps:
        lines.append(f"#{p['id']} {p['name']}: {p['stock']} | {p['price']:.2f}€")
    await c.message.edit_text("\n".join(lines), reply_markup=back_home_kb(get_role(c.from_user.id)))
    await c.answer()

@dp.message(Command("export_ca"))
async def cmd_export_ca_dup(m: Message):
    # (gardé au cas où double déclaration — déjà plus haut aussi)
    if not is_staff(m.from_user.id):
        return await m.answer("⛔ Autorisation requise.")
    csv_path, pdf_path = export_ca_files()
    await m.answer_document(document=FSInputFile(csv_path, filename="ca_export.csv"))
    await m.answer_document(document=FSInputFile(pdf_path, filename="ca_export.pdf"))

@dp.callback_query(F.data == "export_ca")
async def cb_export(c: CallbackQuery):
    if not is_staff(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    csv_path, pdf_path = export_ca_files()
    await bot.send_document(c.message.chat.id, FSInputFile(csv_path, filename="ca_export.csv"))
    await bot.send_document(c.message.chat.id, FSInputFile(pdf_path, filename="ca_export.pdf"))
    await c.answer("Export envoyé.")

# ---------- Admin: Ajouter produit (FSM) ----------
@dp.callback_query(F.data == "admin_add_product")
async def admin_add_product_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    await state.set_state(AdminAddProduct.waiting_name)
    await edit_or_send(c.message, "📝 Nom du produit ?")

@dp.message(AdminAddProduct.waiting_name)
async def admin_add_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AdminAddProduct.waiting_price)
    await m.answer("💰 Prix (€) ?")

@dp.message(AdminAddProduct.waiting_price)
async def admin_add_price(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "."))
    except:
        return await m.answer("❌ Entrez un prix valide.")
    await state.update_data(price=price)
    await state.set_state(AdminAddProduct.waiting_stock)
    await m.answer("📦 Stock initial ?")

@dp.message(AdminAddProduct.waiting_stock)
async def admin_add_stock(m: Message, state: FSMContext):
    try:
        stock = int(m.text)
    except:
        return await m.answer("❌ Entrez un entier.")
    await state.update_data(stock=stock)
    await state.set_state(AdminAddProduct.waiting_photo)
    await m.answer("📷 Envoie une photo (ou tape 'non').")

@dp.message(AdminAddProduct.waiting_photo)
async def admin_add_photo(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return await m.answer("⛔")
    data = await state.get_data()
    name, price, stock = data["name"], data["price"], data["stock"]
    photo_file_id = None
    if m.photo:
        photo_file_id = m.photo[-1].file_id
    elif (m.text or "").strip().lower() not in ("", "non"):
        # accepte aussi URL
        photo_file_id = m.text.strip()

    add_product(name, float(price), int(stock))
    if photo_file_id:
        set_photo_by_name(name, photo_file_id)

    await state.clear()
    await m.answer(f"✅ Produit ajouté : {name} ({price:.2f}€, stock {stock})")

# ---------- Admin: Modifier produit (FSM) ----------
@dp.callback_query(F.data == "admin_edit_product")
async def admin_edit_product_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    rows = list_active_products()
    if not rows:
        return await c.message.edit_text("📭 Aucun produit actif.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Retour", callback_data="admin_panel")]
                                         ]))
    kb = [[InlineKeyboardButton(
        text=f"#{r['id']} {r['name']} ({r['price']}€ / stock {r['stock']})",
        callback_data=f"editp:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton(text="⬅️ Retour", callback_data="admin_panel")])
    await state.set_state(AdminEditProduct.waiting_choose_product)
    await edit_or_send(c.message, "✏️ Choisis un produit :", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("editp:"), AdminEditProduct.waiting_choose_product)
async def admin_edit_pick_product(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    pid = int(c.data.split(":")[1])
    prod = get_product(pid)
    if not prod:
        return await c.answer("Produit introuvable", show_alert=True)
    await state.update_data(pid=pid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆔 Nom",   callback_data="editfield:name")],
        [InlineKeyboardButton(text="💰 Prix",  callback_data="editfield:price")],
        [InlineKeyboardButton(text="📦 Stock", callback_data="editfield:stock")],
        [InlineKeyboardButton(text="📷 Photo", callback_data="editfield:photo")],
        [InlineKeyboardButton(text="⬅️ Retour", callback_data="admin_edit_product")],
    ])
    await state.set_state(AdminEditProduct.waiting_choose_field)
    await edit_or_send(
    c.message,
    f"Produit : <b>{prod['name']}</b>\nQue veux-tu modifier ?",
    reply_markup=kb
)

@dp.callback_query(F.data.startswith("editfield:"), AdminEditProduct.waiting_choose_field)
async def admin_edit_pick_field(c: CallbackQuery, state: FSMContext):
    field = c.data.split(":")[1]
    await state.update_data(field=field)
    await state.set_state(AdminEditProduct.waiting_new_value)
    prompts = {
        "name":  "🆔 Nouveau nom ?",
        "price": "💰 Nouveau prix (€) ?",
        "stock": "📦 Nouveau stock (entier) ?",
        "photo": "📷 Envoie une photo OU un lien URL (ou /cancel)",
    }
    await edit_or_send(
    c.message,
    prompt,
    reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Annuler", callback_data="admin_panel")]]
    )
)

@dp.message(AdminEditProduct.waiting_new_value)
async def admin_edit_apply(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return await m.answer("⛔")
    data = await state.get_data()
    pid = int(data["pid"])
    field = data["field"]
    prod = get_product(pid)
    if not prod:
        await state.clear()
        return await m.answer("❌ Produit introuvable.")
    try:
        if field == "name":
            new_name = (m.text or "").strip()
            if not new_name:
                return await m.answer("❌ Nom invalide.")
            set_product_name(pid, new_name)
            await m.answer(f"✅ Nom mis à jour : {new_name}")
        elif field == "price":
            price = float(m.text.replace(",", "."))
            set_price(pid, price)
            await m.answer(f"✅ Prix mis à jour : {price:.2f}€")
        elif field == "stock":
            stock = int(m.text)
            set_stock_absolute(pid, stock)
            await m.answer(f"✅ Stock mis à jour : {stock}")
        elif field == "photo":
            if m.photo:
                file_id = m.photo[-1].file_id
                set_photo_by_name(prod["name"], file_id)
                await m.answer("✅ Photo mise à jour (média).")
            else:
                url = (m.text or "").strip()
                if not url:
                    return await m.answer("❌ Envoie une photo ou une URL.")
                set_photo_by_name(prod["name"], url)
                await m.answer("✅ Photo mise à jour (URL).")
        else:
            await m.answer("❌ Champ inconnu.")
    except Exception as e:
        await m.answer(f"❌ Erreur: {e}")
    await state.clear()

# ---------- Admin: Désactiver / Réactiver ----------
@dp.callback_query(F.data == "admin_delete_product")
async def admin_delete_product_start(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    rows = list_active_products()
    if not rows:
        return await c.message.edit_text("📭 Aucun produit actif.")
    kb = [[InlineKeyboardButton(
        text=f"🗑️ #{r['id']} {r['name']} ({r['price']}€ / stock {r['stock']})",
        callback_data=f"delp:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton(text="⬅️ Retour", callback_data="admin_panel")])
    await c.message.edit_text("🗑️ Choisis un produit à désactiver :", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("delp:"))
async def admin_delete_product_confirm(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    pid = int(c.data.split(":")[1])
    prod = get_product(pid)
    if not prod:
        return await c.answer("Produit introuvable", show_alert=True)
    deactivate_product(pid)
    await c.message.edit_text(f"✅ Produit désactivé : <b>{prod['name']}</b>",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="⬅️ Retour Admin", callback_data="admin_panel")]
                              ]))

@dp.callback_query(F.data == "admin_reactivate_product")
async def admin_reactivate_product_start(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    rows = list_inactive_products()
    if not rows:
        return await c.message.edit_text("✅ Aucun produit désactivé.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Retour", callback_data="admin_panel")]
                                         ]))
    kb = [[InlineKeyboardButton(
        text=f"♻️ #{r['id']} {r['name']} ({r['price']}€ / stock {r['stock']})",
        callback_data=f"reactp:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton(text="⬅️ Retour", callback_data="admin_panel")])
    await c.message.edit_text("♻️ Réactiver quel produit ?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("reactp:"))
async def admin_reactivate_product_confirm(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("⛔", show_alert=True)
    pid = int(c.data.split(":")[1])
    prod = get_product(pid)
    if not prod:
        return await c.answer("Produit introuvable", show_alert=True)
    reactivate_product(pid)
    await c.message.edit_text(f"✅ Produit réactivé : <b>{prod['name']}</b>",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                  [InlineKeyboardButton(text="⬅️ Retour Admin", callback_data="admin_panel")]
                              ]))

# ---------- Admin Ops: assign / delivered ----------
def mark_order_delivered(code: str) -> Optional[int]:
    with closing(db()) as conn:
        order = conn.execute("SELECT * FROM orders WHERE code=? AND status!='delivered'", (code,)).fetchone()
        if not order:
            return None
        items = json.loads(order["items_json"] or "[]")
        for it in items:
            conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (int(it["qty"]), int(it["id"])))
        conn.execute("UPDATE orders SET status='delivered', delivered_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), order["id"]))
        conn.execute("INSERT INTO treasury(order_id,entry_type,amount,created_at) VALUES(?,?,?,?)",
                     (order["id"], "sale", float(order["total"]), datetime.utcnow().isoformat()))
        conn.commit()
        return order["id"]

def set_order_assigned(order_code: str, courier_user_id: int):
    with closing(db()) as conn:
        conn.execute("UPDATE orders SET status='assigned', courier_user_id=? WHERE code=?",
                     (courier_user_id, order_code))
        conn.commit()

@dp.message(Command("assign"))
async def cmd_assign(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Admin uniquement.")
    try:
        _, code, courier_id = m.text.split()
        set_order_assigned(code, int(courier_id))
        await m.answer("✅ Assigné.")
    except Exception:
        await m.answer("Format: /assign CODE courier_user_id")

@dp.message(Command("delivered"))
async def cmd_delivered(m: Message):
    if not is_staff(m.from_user.id):
        return await m.answer("⛔")
    try:
        _, code = m.text.split()
        oid = mark_order_delivered(code)
        if oid is None:
            return await m.answer("Commande introuvable ou déjà livrée.")
        await m.answer("✅ Livraison confirmée. Stock déduit et CA mis à jour.")
    except Exception:
        await m.answer("Format: /delivered CODE")

# ---------- Export CA ----------
def export_ca_files(period_days: int = 30):
    since = datetime.utcnow() - timedelta(days=period_days)
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, code, total, discount, delivery_fee, created_at, delivered_at, status "
            "FROM orders WHERE created_at >= ? ORDER BY id",
            (since.isoformat(),)
        ).fetchall()

    csv_path = "ca_export.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "code", "status", "total", "discount", "delivery_fee", "created_at", "delivered_at"])
        for r in rows:
            w.writerow([r["id"], r["code"], r["status"], f"{r['total']:.2f}", f"{r['discount']:.2f}",
                        f"{r['delivery_fee']:.2f}", r["created_at"], r["delivered_at"] or ""])

    pdf_path = "ca_export.pdf"
    c = pdf_canvas.Canvas(pdf_path, pagesize=A4)
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
    c.drawString(2 * cm, y - 0.5 * cm, f"Total (cash): {sum_total:.2f}€")
    c.save()
    return csv_path, pdf_path

# ---------- Startup ----------
async def on_startup():
    print(f"Booting bot… OWNER_ID={OWNER_ID}", flush=True)
    init_db()
    # Seed de démo si vide
    if not list_active_products():
        add_product("Bouteille 1.0L", 2.50, 50)
        add_product("Pack 6x0.5L", 6.90, 30)
        add_product("Pod citron", 3.20, 100)

async def main():
    await on_startup()
    print("Starting polling…", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")

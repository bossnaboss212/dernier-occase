# Telegram Cash-Only Shop Bot

Mini-boutique Telegram (paiement **espèces uniquement**) :
- Gestion du **stock**
- Suivi du **CA / trésorerie**
- Commandes anonymes envoyées aux livreurs
- Avis clients
- Gestion staff/admin
- Export du chiffre d’affaires en CSV + PDF
- Livraison :
  - Millau → Gratuite
  - Hors Millau → 20€ / 30€ / 50€
  - >50 km → livraison impossible

## Installation

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

## Configuration `.env`

```env
BOT_TOKEN=
COURIER_CHANNEL_ID=
OWNER_ID=
DB_PATH=shop.db
```

## Commandes utiles

- /fees
- /set_fees 20:20,30:30,50:50
- /add_product Nom|Prix|Stock
- /delivered CODE
- /export_ca
- /set_role user_id role

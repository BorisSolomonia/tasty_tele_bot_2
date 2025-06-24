import logging
import os
import re
import json
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from rapidfuzz import process, fuzz
from openai import OpenAI
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- SETUP ---

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_FILE = "google-credentials.json"
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# Load known lists
def load_list_from_file(filename):
    try:
        with open(filename, encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logging.error(f"File not found: {filename}")
        return []

KNOWN_CUSTOMERS = load_list_from_file("known_customers.txt")
KNOWN_PRODUCTS = load_list_from_file("known_products.txt")

# Google Sheets Setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
sheet = gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1

# --- UTILS ---

def fuzzy_match_all(term, known_list, threshold=50):
    results = process.extract(term, known_list, scorer=fuzz.ratio)
    return [item for item, score, _ in results if score >= threshold]

def split_customer_and_products(text):
    if '.' not in text:
        return text, []
    customer_raw, rest = text.split('.', 1)
    product_lines = re.split(r'[;,]', rest)
    return customer_raw.strip(), [p.strip() for p in product_lines if p.strip()]

def call_openai_parse(customer_raw, customer_options, product_lines):
    customer_str = ", ".join(customer_options)
    product_str = ", ".join(KNOWN_PRODUCTS)
    items = "\n".join(product_lines)
    prompt = f"""
Given the following order message:

Customer guess: "{customer_raw}"
Possible matching customers: {customer_str}

Product message lines:
{items}

Known product list: {product_str}

For each line, extract:
- product (match from list if possible)
- amount (number)
- unit (კგ, ც, ლ, გრამი)
- comment (if any)

Respond in JSON array format like:
[
  {{
    "customer": "matched customer",
    "product": "matched product or raw",
    "amount_value": "10",
    "amount_unit": "კგ",
    "comment": "optional"
  }},
  ...
]
"""

    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You extract structured order info from Georgian messages using customer and product lists."},
                {"role": "user", "content": prompt}
            ]
        )
        content = response.choices[0].message.content.strip()
        parsed = json.loads(content)
        for entry in parsed:
            entry["type"] = "order"
            entry["raw_customer"] = customer_raw
            entry["customer_unknown"] = entry["customer"] not in KNOWN_CUSTOMERS
            entry["product_unknown"] = entry["product"] not in KNOWN_PRODUCTS
        return parsed
    except Exception as e:
        logging.error(f"OpenAI parsing failed: {e}")
        return []

def update_google_sheet(data, author):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([
        timestamp,
        data['customer'],
        data['product'],
        data['amount_value'],
        data['amount_unit'],
        data.get('comment', ''),
        author
    ], value_input_option='USER_ENTERED')

# --- TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Send me an order and I’ll log it to Google Sheets.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    author = update.message.from_user.full_name or update.message.from_user.username or str(update.message.from_user.id)

    customer_raw, product_lines = split_customer_and_products(text)
    matched_customers = fuzzy_match_all(customer_raw, KNOWN_CUSTOMERS, threshold=50)
    parsed_items = call_openai_parse(customer_raw, matched_customers, product_lines)

    if not parsed_items:
        await update.message.reply_text("❌ ვერ დავამუშავე შეტყობინება.")
        return

    for data in parsed_items:
        update_google_sheet(data, author)
        warn = ""
        if data["customer_unknown"]:
            warn += " ⚠ უცნობი მომხმარებელი"
        if data["product_unknown"]:
            warn += " ⚠ უცნობი პროდუქტი"
        await update.message.reply_text(
            f"✅ Logged: {data['customer']} / {data['product']} / {data['amount_value']}{data['amount_unit']}{warn}"
        )

# --- MAIN ---

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()

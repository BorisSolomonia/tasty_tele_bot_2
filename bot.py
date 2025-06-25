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
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_FILE = "google-credentials.json"
SPREADSHEET_NAME = "Preseller Orders"

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- Load & Update Known Lists ---

def load_list_from_file(filename):
    try:
        with open(filename, encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logging.warning(f"File not found: {filename}")
        return []

def append_to_file(filename, item):
    with open(filename, "a", encoding="utf-8") as f:
        f.write(item.strip() + "\n")

KNOWN_CUSTOMERS_FILE = "known_customers.txt"
KNOWN_PRODUCTS_FILE = "known_products.txt"

KNOWN_CUSTOMERS = load_list_from_file(KNOWN_CUSTOMERS_FILE)
KNOWN_PRODUCTS = load_list_from_file(KNOWN_PRODUCTS_FILE)

# --- Google Sheets Setup ---

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
sheet = gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1

# --- UTILS ---

def fuzzy_match(term, known_list, threshold=50):
    match, score, _ = process.extractOne(term, known_list, scorer=fuzz.ratio)
    return match if score >= threshold else None

def build_shortlist(term, known_list, threshold=50, limit=5):
    matches = process.extract(term, known_list, scorer=fuzz.ratio)
    return [match for match, score, _ in matches if score >= threshold][:limit]

def update_google_sheet(data, author):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        timestamp,
        data['customer'],
        data['product'],
        data['amount_value'],
        data['amount_unit'],
        data['comment'],
        author
    ]
    sheet.append_row(row, value_input_option='USER_ENTERED')
    logging.info(f"Logged to sheet: {row}")

def ask_gpt_to_parse(message, customer_shortlist, known_products):
    customer_list_str = ", ".join(customer_shortlist)
    product_list_str = ", ".join(known_products)

    base_prompt = f"""
Given the following Georgian message, extract each order as a JSON list of entries.

Each entry must have:
- customer (choose one from: {customer_list_str})
- product (choose one from: {product_list_str})
- amount_value (number only)
- amount_unit (კგ, ც, ლ, გრამი)
- comment (optional)

Format:
[
  {{
    "customer": "...",
    "product": "...",
    "amount_value": "...",
    "amount_unit": "...",
    "comment": "..."
  }},
  ...
]

Message: \"{message}\"
"""

    messages = [
        {"role": "system", "content": "You are a precise JSON-generating assistant. Respond only with valid JSON."},
        {"role": "user", "content": base_prompt}
    ]

    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        content = response.choices[0].message.content.strip()
        logging.info(f"GPT raw response: {content[:200]}...")
        return json.loads(content)
    except Exception as e1:
        logging.warning("GPT returned invalid JSON. Retrying with stricter format request...")

        strict_messages = [
            {"role": "system", "content": "You are a precise JSON generator. Reply ONLY with strict valid JSON array, do not include any markdown, no ```json."},
            {"role": "user", "content": base_prompt}
        ]
        try:
            response = client_ai.chat.completions.create(
                model="gpt-4o",
                messages=strict_messages
            )
            content = response.choices[0].message.content.strip()
            logging.info(f"GPT second raw response: {content[:200]}...")
            return json.loads(content)
        except Exception as e2:
            logging.error(f"GPT second attempt also failed: {e2}")
            return None

def handle_new_items(data_list):
    for item in data_list:
        cust = item.get("customer", "").strip().lower()
        prod = item.get("product", "").strip().lower()

        if cust.startswith("new "):
            cleaned = item["customer"][4:].strip()
            item["customer"] = cleaned
            if cleaned not in KNOWN_CUSTOMERS:
                KNOWN_CUSTOMERS.append(cleaned)
                append_to_file(KNOWN_CUSTOMERS_FILE, cleaned)
                logging.info(f"Added new customer: {cleaned}")

        if prod.startswith("new "):
            cleaned = item["product"][4:].strip()
            item["product"] = cleaned
            if cleaned not in KNOWN_PRODUCTS:
                KNOWN_PRODUCTS.append(cleaned)
                append_to_file(KNOWN_PRODUCTS_FILE, cleaned)
                logging.info(f"Added new product: {cleaned}")

# --- TELEGRAM HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Send me an order and I’ll log it to Google Sheets.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    author = update.message.from_user.full_name or update.message.from_user.username or str(update.message.from_user.id)

    logging.info(f"Received message: {text} from {author}")

    customer_part = text.split('.')[0].strip()
    shortlist = build_shortlist(customer_part, KNOWN_CUSTOMERS, threshold=50)
    logging.info(f"Customer input: '{customer_part}', matched shortlist: {shortlist}")

    parsed_orders = ask_gpt_to_parse(text, shortlist, KNOWN_PRODUCTS)

    if not parsed_orders:
        await update.message.reply_text("❌ ვერ დავამუშავე შეტყობინება.")
        return

    handle_new_items(parsed_orders)

    for entry in parsed_orders:
        update_google_sheet(entry, author)
        await update.message.reply_text(
            f"✅ Logged: {entry['customer']} / {entry['product']} / {entry['amount_value']}{entry['amount_unit']}"
        )

# --- MAIN ---

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()

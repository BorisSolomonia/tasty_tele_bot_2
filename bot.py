# Re-executing the code after kernel reset
import logging
import os
import re
import json
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from rapidfuzz import process
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

# Known lists
PRODUCTS_FILE = "known_products.txt"
CUSTOMERS_FILE = "known_customers.txt"

def load_list_from_file(file):
    try:
        with open(file, encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        logging.error(f"Failed to load {file}: {e}")
        return []

def append_to_file(file, item):
    try:
        with open(file, "a", encoding='utf-8') as f:
            f.write(f"{item.strip()}\n")
    except Exception as e:
        logging.error(f"Failed to append to {file}: {e}")

KNOWN_PRODUCTS = load_list_from_file(PRODUCTS_FILE)
KNOWN_CUSTOMERS = load_list_from_file(CUSTOMERS_FILE)

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
sheet = gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1

# --- UTILS ---

def sanitize(value):
    return str(value).strip().replace('\n', ' ').replace('\r', '').replace('\t', '')

def fuzzy_match(term, known_list, threshold=50):
    match, score, _ = process.extractOne(term, known_list)
    return match if score >= threshold else None

def get_shortlist(term, known_list):
    return [entry for entry, score, _ in process.extract(term, known_list, limit=5) if score >= 50]

def update_google_sheet(data, author):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        sanitize(timestamp),
        sanitize(data.get('customer', '')),
        sanitize(data.get('product', '')),
        sanitize(data.get('amount_value', '')),
        sanitize(data.get('amount_unit', '')),
        sanitize(data.get('comment', '')),
        sanitize(author)
    ]
    sheet.append_row(row, value_input_option='USER_ENTERED')
    logging.info(f"Logged to sheet: {row}")

def call_gpt_fallback(raw_text, shortlist, known_products):
    customer_list_str = ", ".join(shortlist)
    product_list_str = ", ".join(known_products)

    system_prompt = f"""You are a helpful assistant that extracts structured order data from messages in Georgian. 
You MUST only choose customers from this shortlist: {customer_list_str}
and products from this list: {product_list_str}
Format MUST be strict JSON array like:
[
  {{
    "customer": "...",
    "product": "...",
    "amount_value": "...",
    "amount_unit": "კგ|ც|ლ|გრამი",
    "comment": "..."
  }},
  ...
]
Return at least 'customer', 'product', 'amount_value', 'amount_unit'. If field is not available, leave empty string.
Never use markdown like ```json.
"""

    user_prompt = f"Message: {raw_text}"

    def try_parse(strict=False):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        if strict:
            messages.insert(1, {"role": "system", "content": "Output JSON array only. No explanation. Strict format."})

        response = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        content = response.choices[0].message.content.strip()
        logging.info(f"GPT raw response: {content}")
        try:
            parsed = json.loads(content)
            for entry in parsed:
                entry.setdefault("comment", "")
                entry.setdefault("amount_unit", "")
            return parsed
        except Exception as e:
            logging.warning(f"GPT returned invalid JSON. {'Second' if strict else 'First'} attempt failed.")
            return None

    parsed = try_parse(strict=False)
    if not parsed:
        parsed = try_parse(strict=True)

    return parsed

# --- TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("გამარჯობა! შეგიძლია მომწერო შეკვეთა, დავამუშავებ და Google Sheets-ში ჩავწერ.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()
    author = update.message.from_user.full_name or update.message.from_user.username or str(update.message.from_user.id)
    logging.info(f"Received message: {message} from {author}")

    customer_raw = message.split(".")[0].strip()
    if customer_raw.lower().startswith("new"):
        customer_raw_cleaned = customer_raw[3:].strip()
        append_to_file(CUSTOMERS_FILE, customer_raw_cleaned)
        KNOWN_CUSTOMERS.append(customer_raw_cleaned)
        customer_raw = customer_raw_cleaned

    shortlist = get_shortlist(customer_raw, KNOWN_CUSTOMERS)
    logging.info(f"Customer input: '{customer_raw}', matched shortlist: {shortlist}")

    product_candidates = re.findall(r"(new\s*)?([ა-ჰ\s]+)\s+(\d+)\s*(კგ|ც|ლ|გრამი)?", message)
    for p in product_candidates:
        if p[0].lower().startswith("new"):
            new_product = p[1].strip()
            if new_product not in KNOWN_PRODUCTS:
                append_to_file(PRODUCTS_FILE, new_product)
                KNOWN_PRODUCTS.append(new_product)

    gpt_response = call_gpt_fallback(message, shortlist, KNOWN_PRODUCTS)

    if not gpt_response:
        logging.error("GPT failed to return valid JSON twice.")
        await update.message.reply_text("❌ ვერ დავამუშავე შეტყობინება.")
        return

    count_logged = 0
    for entry in gpt_response:
        try:
            update_google_sheet(entry, author)
            count_logged += 1
        except Exception as e:
            logging.error(f"Failed to log entry: {entry}, Error: {e}")

    if count_logged > 0:
        await update.message.reply_text(f"✅ ჩავწერე {count_logged} პროდუქტ(ი) ცხრილში.")
    else:
        await update.message.reply_text("⚠ მონაცემები ვერ ჩაიწერა.")

# --- MAIN ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()

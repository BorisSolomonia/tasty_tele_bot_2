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

# Load known customers and products
def load_list(filename):
    with open(filename, encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]

KNOWN_CUSTOMERS = load_list("known_customers.txt")
KNOWN_PRODUCTS = load_list("known_products.txt")

# --- GOOGLE SHEETS ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
sheet = gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1

# --- UTILS ---
def fuzzy_match_customer(term, threshold=50):
    return [match for match, score, _ in process.extract(term, KNOWN_CUSTOMERS) if score >= threshold]

def fuzzy_match_product(term, threshold=50):
    match, score, _ = process.extractOne(term, KNOWN_PRODUCTS)
    return match if score >= threshold else None

def sanitize(value):
    return str(value).strip().replace('\n', ' ').replace('\r', '')

def update_google_sheet(data, author):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([
        sanitize(timestamp),
        sanitize(data['customer']),
        sanitize(data['product']),
        sanitize(data['amount_value']),
        sanitize(data['amount_unit']),
        sanitize(data['comment']),
        sanitize(author)
    ])

def parse_with_gpt(message_text, matched_customers):
    customer_list = ", ".join(matched_customers[:5])  # shortlist
    product_list = ", ".join(KNOWN_PRODUCTS)

    base_prompt = f"""
    You are an assistant that parses Georgian order messages. As usual customer name is before dot.

    Use this shortlist of customers: [{customer_list}]
    Use this product list: [{product_list}]

    Split the message into multiple structured JSON objects, one per product. Each object must look like this:
    {{
    "customer": "matched or raw customer",
    "product": "matched or raw product",
    "amount_value": "10",
    "amount_unit": "კგ|ც|ლ|გრამი",
    "comment": "optional"
    }}

IMPORTANT! If you can't match, use raw values. Respond ONLY with a valid JSON array!!!.
"""

    try:
        def call_and_parse(prompt):
            response = client_ai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": prompt.strip()},
                    {"role": "user", "content": message_text.strip()}
                ]
            )
            raw = response.choices[0].message.content.strip()
            logging.info(f"GPT raw response: {raw[:200]}...")
# Strip code block if present
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
            return json.loads(raw)

        try:
            return call_and_parse(base_prompt)
        except json.JSONDecodeError:
            logging.warning("GPT returned invalid JSON. Retrying with strict format request...")

            strict_prompt = base_prompt + "\n\nReturn ONLY a valid JSON array. Do not include any other explanation or text."
            try:
                return call_and_parse(strict_prompt)
            except json.JSONDecodeError:
                logging.error("GPT second attempt also failed. Falling back to raw line.")
                return [{
                    "customer": message_text.split('.')[0].strip(),
                    "product": "",
                    "amount_value": "?",
                    "amount_unit": "",
                    "comment": ""
                }]

    except Exception as e:
        logging.error(f"OpenAI parsing failed: {e}")
        return [{
            "customer": message_text.split('.')[0].strip(),
            "product": "",
            "amount_value": "?",
            "amount_unit": "",
            "comment": ""
        }]

# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 გამარჯობა! მომწერე შეკვეთა და ჩავწერ გუგლ ცხრილში.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    author = update.message.from_user.full_name or update.message.from_user.username or str(update.message.from_user.id)
    logging.info(f"Received message: {text} from {author}")

    lines = text.split("\n")
    for line in lines:
        customer_match = re.match(r"^(.*?)\s*\.", line)
        customer_input = customer_match.group(1).strip() if customer_match else ""
        matched_customers = fuzzy_match_customer(customer_input)

        logging.info(f"Customer input: '{customer_input}', matched shortlist: {matched_customers}")

        entries = parse_with_gpt(line, matched_customers)
        if not entries:
            await update.message.reply_text("❌ ვერ დავამუშავე შეტყობინება.")
            return

        for item in entries:
            # fallback safety
            item.setdefault("customer", customer_input)
            item.setdefault("product", "")
            item.setdefault("amount_value", "?")
            item.setdefault("amount_unit", "")
            item.setdefault("comment", "")

            update_google_sheet(item, author)

            warn = ""
            if item["customer"] not in KNOWN_CUSTOMERS:
                warn += " ⚠ უცნობი მომხმარებელი"
            if item["product"] not in KNOWN_PRODUCTS:
                warn += " ⚠ უცნობი პროდუქტი"

            await update.message.reply_text(
                f"✅ {item['customer']} / {item['product']} / {item['amount_value']} {item['amount_unit']}{warn}"
            )

# --- MAIN ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()

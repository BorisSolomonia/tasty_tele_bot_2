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
    customer_list = ", ".join(matched_customers[:5])  # up to 5 best matches
    product_list = ", ".join(KNOWN_PRODUCTS)

    system_prompt = f"""
You are an assistant that parses Georgian order messages.

Use this shortlist of customers: [{customer_list}]
Use this product list: [{product_list}]

Parse the message into multiple JSON objects, one per product. Each object must follow this structure:
{{
  "customer": "matched or raw customer",
  "product": "matched or raw product",
  "amount_value": "10",
  "amount_unit": "კგ|ც|ლ|გრამი",
  "comment": "optional"
}}

If matching fails, use the raw name for customer/product. Always return valid JSON array.
"""

    logging.info("Sending message to GPT for parsing...")
    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": message_text.strip()}
            ]
        )
        content = response.choices[0].message.content.strip()

        try:
            parsed_data = json.loads(content)
            if isinstance(parsed_data, dict):  # convert single item to list
                parsed_data = [parsed_data]
            return parsed_data
        except json.JSONDecodeError:
            logging.warning("GPT returned invalid JSON. Retrying with stricter format request...")

            retry_prompt = system_prompt + "\n\nStrictly return a valid JSON array only. No comments or explanations."
            response = client_ai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": retry_prompt.strip()},
                    {"role": "user", "content": message_text.strip()}
                ]
            )
            retry_content = response.choices[0].message.content.strip()
            return json.loads(retry_content)
    except Exception as e:
        logging.error(f"OpenAI parsing failed: {e}")
        return []

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

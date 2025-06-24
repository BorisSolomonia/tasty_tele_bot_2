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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDENTIALS_FILE = "google-credentials.json"
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- Load known lists from files ---

def load_list_from_file(filename):
    try:
        with open(filename, encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logging.error(f"File not found: {filename}")
        return []

KNOWN_CUSTOMERS = load_list_from_file("known_customers.txt")
KNOWN_PRODUCTS = load_list_from_file("known_products.txt")

# --- Google Sheets Setup ---

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
sheet = gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1

# --- UTILS ---

def fuzzy_customer_candidates(input_customer, known_list, threshold=50, top_n=5):
    matches = process.extract(input_customer, known_list, limit=top_n)
    shortlist = [match for match, score, _ in matches if score >= threshold]
    logging.info(f"Shortlist for '{input_customer}': {shortlist}")
    return shortlist

def sanitize(value):
    return str(value).strip().replace('\n', ' ').replace('\r', '').replace('\t', '')

def update_google_sheet_row(data, author):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([
        sanitize(timestamp),
        sanitize(data['customer']),
        sanitize(data['product']),
        sanitize(data['amount_value']),
        sanitize(data['amount_unit']),
        sanitize(data['comment']),
        sanitize(author)
    ], value_input_option='USER_ENTERED')

def call_openai_parser(message, customer_input):
    shortlist = fuzzy_customer_candidates(customer_input, KNOWN_CUSTOMERS)

    system_prompt = f"""
        You are a strict parser. Return ONLY valid JSON. No markdown, no explanation.
        Match the customer to this shortlist: {', '.join(shortlist)}
        Match products to this list: {', '.join(KNOWN_PRODUCTS)}

        The user will send you a message like:
        "ფუდსელი. 20კგ ხორცი, 5ც გრუდინკა"

        Your job is to extract structured product orders.
        Return a JSON array like this (for multiple products):

        [
        {{
            "customer": "შპს ფუდსელი",
            "product": "ხორცი",
            "amount_value": "20",
            "amount_unit": "კგ",
            "comment": ""
        }},
        {{
            "customer": "შპს ფუდსელი",
            "product": "გრუდინკა",
            "amount_value": "5",
            "amount_unit": "ც",
            "comment": ""
        }}
        ]

        Now parse the user message strictly and return valid JSON array only.
        """

    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ]
        )
        content = response.choices[0].message.content.strip()

        # Log raw content for debugging
        logging.info(f"OpenAI raw response:\n{content}")

        # Attempt to extract JSON block using regex if needed
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            return json.loads(match.group())

        return json.loads(content)

    except Exception as e:
        logging.error(f"OpenAI parsing failed: {e}")
        return None


# --- TELEGRAM HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Send me an order and I’ll log it to Google Sheets.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    author = update.message.from_user.full_name or update.message.from_user.username or str(update.message.from_user.id)

    # Extract customer before first dot
    parts = re.split(r'\s*\.\s*', message, maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("❌ Message must start with customer name followed by '.'")
        return

    customer_guess = parts[0]

    structured_data = call_openai_parser(message, customer_guess)

    if not structured_data:
        await update.message.reply_text("❌ GPT parsing failed.")
        return

    replies = []
    for item in structured_data:
        update_google_sheet_row(item, author)
        replies.append(f"✅ {item['customer']} / {item['product']} / {item['amount_value']}{item['amount_unit']}")

    await update.message.reply_text('\n'.join(replies))

# --- MAIN ---

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()

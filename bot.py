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
SPREADSHEET_NAME = "Preseller Orders"

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# Known lists
KNOWN_PRODUCTS = [
    "ბადექონი", "ღორის ხორცი (ფერდი)", "ღორის ხორცი (კისერი)", "საქონლის ფარში",
    "ღორის ხორცი", "საქონლის ხორცი (ძვლიანი)", "საქონლის ხორცი", "ღორის ხორცი",
    "საქონლის ხორცი (რბილი)", "ხბოს ხორცი", "ღორის კანჭი", "გრუდინკა",
    "ღორის ხორცი (რბილი)", "ხორცი", "ნეკნი", "საქონლის ცხიმი", "საქონლის ხორცი (სუკი)",
    "პერედინკა", "ღორის ქონი", "არტალა (რბილი)"
]

KNOWN_CUSTOMERS = [
    "შპს წისქვილი ჯგუფი", "შპს აურა", "ელგუჯა ციბაძე", "შპს მესი 2022", "შპს სიმბა 2015",
    "შპს სქულფუდ", "ირინე ხუნდაძე", "შპს მაგსი", "შპს ასი-100", "შპს ვარაზის ხევი 95",
    "შპს  ხინკლის ფაბრიკა", "შპს სამიკიტნო-მაჭახელა", "შპს რესტორან მენეჯმენტ კომპანი",
    "შპს თაღლაურა  მენეჯმენტ კომპანი", "შპს  ნარნია", "შპს ბუკა202", "შპს მუჭა მუჭა 2024",
    "შპს აკიდო 2023", "შპს MASURO", "შპს MSR", "ნინო მუშკუდიანი", "შპს ქალაქი 27",
    "შპს 'სპრინგი' -რესტორანი ბეღელი", "შპს ნეკაფე", "შპს თეისთი", "შპს იმფერი",
    "შპს შნო მოლი", "შპს რესტორან ჯგუფი", "შპს ხინკა", "მერაბი ბერიშვილი",
    "შპს სანაპირო 2022", "შპს ქეი-ბუ", "შპს ბიგ სემი", "შპს კატოსან",
    "შპს  ბრაუჰაუს ტიფლისი", "შპს ბუდვაიზერი - სამსონი", "შპს სსტ ჯეორჯია",
    "შპს ვახტანგური", "შპს ფუდსელი", "შპს მღვიმე", "შპს ათუ", "შპს გრინ თაუერი",
    "შპს გურმე", "შპს ქვევრი 2019", "ლევან ადამია", "გურანდა ლაღაძე"
]

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
sheet = gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1

# --- UTILS ---

def fuzzy_match(term, known_list, threshold=80):
    match, score, _ = process.extractOne(term, known_list)
    return match if score >= threshold else None

def extract_data_from_line(line):
    line = line.strip()
    match = re.match(r"(.+?)\s*\.\s*(.+?)\s+(\d+)(კგ|ც)?\s*(.*)?", line)
    if not match:
        return None

    customer_raw, product_raw, number, unit, comment = match.groups()
    customer = fuzzy_match(customer_raw, KNOWN_CUSTOMERS) or customer_raw
    product = fuzzy_match(product_raw, KNOWN_PRODUCTS) or product_raw
    amount_value = number
    amount_unit = unit or ""

    return {
        "type": "order",
        "customer": customer,
        "product": product,
        "amount_value": amount_value,
        "amount_unit": amount_unit,
        "comment": comment or "",
        "raw_customer": customer_raw,
        "raw_product": product_raw
    }

def call_gpt_for_parsing(text):
    prompt = f"""
    The following text is a Georgian order message. Extract and return the customer name, product name, quantity, and optional comment.
    Text: "{text}".
    Customer name is always in the beginning of the text.
    Customer names: {', '.join(KNOWN_CUSTOMERS)}.
    Product names: {', '.join(KNOWN_PRODUCTS)}.
    Return JSON like: {{"type": "order", "customer": "ჟღენტი", "product": "პერედინა", "amount_value": "30", "amount_unit": "კგ", "comment": ""}}
    IMPORTANT: Do NOT wrap the output in triple backticks or markdown.
    """

    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that extracts structured Georgian order data."},
                {"role": "user", "content": prompt}
            ]
        )
        content = response.choices[0].message.content.strip()
        logging.info(f"GPT returned: {content}")

        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\n", "", content)
            content = re.sub(r"\n```$", "", content)

        parsed = json.loads(content)
        parsed["type"] = "order"
        return parsed

    except Exception as e:
        logging.error(f"GPT parsing failed: {e}")
        return None

def update_google_sheet(data, author):
    if data['type'] == 'order':
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([
            timestamp,
            data['customer'],
            data['product'],
            data['amount_value'],
            data['amount_unit'],
            data['comment'],
            author
        ])

# --- TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Send me an order and I’ll log it to Google Sheets.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    author = update.message.from_user.full_name or update.message.from_user.username or str(update.message.from_user.id)
    lines = text.split('\n')

    for line in lines:
        for subline in re.split(r'[;,]', line):
            subline = subline.strip()
            if subline:
                data = extract_data_from_line(subline)
                if data:
                    update_google_sheet(data, author)
                    warn = ""
                    if data['customer'] == data['raw_customer']:
                        warn += " ⚠ უცნობი მომხმარებელი"
                    if data['product'] == data['raw_product']:
                        warn += " ⚠ უცნობი პროდუქტი"
                    await update.message.reply_text(f"✅ Logged: {data['customer']} / {data['product']} / {data['amount_value']}{data['amount_unit']}{warn}")
                else:
                    await update.message.reply_text(f"❌ Couldn't parse: {subline}")

# --- MAIN ---

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()

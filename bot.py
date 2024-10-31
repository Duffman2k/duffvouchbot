from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, Filters, CallbackContext, Updater, CallbackQueryHandler, ConversationHandler
from PIL import Image
import requests
from io import BytesIO
import os
from datetime import datetime, timedelta

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DB_CHANNEL_ID = int(os.getenv("DB_CHANNEL_ID"))  # DB channel ID
VIP_GROUP_ID = int(os.getenv("VIP_GROUP_ID"))    # VIP group ID

bot = Bot(TOKEN)
WATERMARK_URL = 'https://i.imgur.com/rZTD37V.png'
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS").split(',')))

pending_vouches = []
user_data = {}
ASK_PRODUCT, ASK_IMAGE = range(2)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def start(update: Update, context: CallbackContext):
    update.message.reply_text("Welcome! Please type the name of the product for this vouch.")
    return ASK_PRODUCT

def ask_product(update: Update, context: CallbackContext):
    user = update.message.from_user
    product_name = update.message.text
    user_data[user.id] = {"product_name": product_name}
    update.message.reply_text("Thank you! Now, please send the image for the vouch.")
    return ASK_IMAGE

def receive_image(update: Update, context: CallbackContext):
    user = update.message.from_user
    product_name = user_data.get(user.id, {}).get("product_name", "Unknown Product")
    photo_file = update.message.photo[-1].get_file()
    image_url = photo_file.file_path
    update.message.reply_text("Image received! Applying watermark...")
    watermarked_image = apply_watermark(image_url)
    
    image_bytes = BytesIO()
    try:
        watermarked_image.save(image_bytes, format='JPEG')
        image_bytes.seek(0)
    except Exception as e:
        print(f"Error saving image to BytesIO: {e}")
        update.message.reply_text("Failed to process the image.")
        return ConversationHandler.END

    pending_vouches.append({"user_id": user.id, "username": user.username, "image": image_bytes, "product_name": product_name})
    user_data.pop(user.id, None)
    update.message.reply_text("Your vouch has been submitted for approval.")
    return ConversationHandler.END

def apply_watermark(image_url):
    response = requests.get(image_url)
    base_image = Image.open(BytesIO(response.content)).convert("RGBA")
    watermark_response = requests.get(WATERMARK_URL, headers={"User-Agent": "Mozilla/5.0"})
    watermark = Image.open(BytesIO(watermark_response.content)).convert("RGBA")
    watermark = watermark.resize((140, 100), Image.LANCZOS)
    alpha = watermark.split()[3]
    alpha = alpha.point(lambda p: p * 0.6)
    watermark.putalpha(alpha)
    rotated_watermark = watermark.rotate(+20, expand=True)
    overlay = Image.new("RGBA", base_image.size, (0, 0, 0, 0))
    base_width, base_height = base_image.size
    x_spacing, y_spacing = base_width // 3, 350
    for y in range(0, base_height, y_spacing):
        for x in range(0, base_width, x_spacing):
            overlay.paste(rotated_watermark, (x, y), rotated_watermark)
    return Image.alpha_composite(base_image, overlay).convert("RGB")

def store_vouch_in_db(vouch_data):
    db_message_text = (
        f"Username: {vouch_data['username']}\n"
        f"UserID: {vouch_data['user_id']}\n"
        f"Time: {', '.join(vouch_data['vouch_times'])}\n"
        f"Total vouches past 36 hours: {vouch_data['vouches_past_36_hours']}\n"
        f"Total vouches forever: {vouch_data['total_vouches']}"
    )
    msg = bot.send_message(chat_id=DB_CHANNEL_ID, text=db_message_text)
    return msg.message_id  # return the message ID for future updates

def fetch_user_vouch_data(user_id):
    messages = bot.get_chat_history(chat_id=DB_CHANNEL_ID, limit=100)
    for message in messages:
        if f"UserID: {user_id}" in message.text:
            return message.message_id, parse_vouch_data(message.text)
    return None, None

def parse_vouch_data(message_text):
    data = {}
    lines = message_text.split('\n')
    data['username'] = lines[0].split(': ')[1]
    data['user_id'] = int(lines[1].split(': ')[1])
    data['vouch_times'] = lines[2].split(': ')[1].split(', ')
    data['vouches_past_36_hours'] = int(lines[3].split(': ')[1])
    data['total_vouches'] = int(lines[4].split(': ')[1])
    return data

def update_vouch_data(user_id, username, approval_time):
    message_id, data = fetch_user_vouch_data(user_id)
    now = datetime.utcnow()
    if not data:
        data = {
            'username': username,
            'user_id': user_id,
            'vouch_times': [approval_time],
            'vouches_past_36_hours': 1,
            'total_vouches': 1
        }
        message_id = store_vouch_in_db(data)
    else:
        data['vouch_times'].append(approval_time)
        data['vouch_times'] = [t for t in data['vouch_times'] if (now - datetime.fromisoformat(t)).total_seconds() <= 36 * 3600]
        data['vouches_past_36_hours'] = len(data['vouch_times'])
        data['total_vouches'] += 1
        update_db_message(data, message_id)
    if data['vouches_past_36_hours'] >= 10:
        bot.invite_chat_member(chat_id=VIP_GROUP_ID, user_id=user_id)

def update_db_message(data, message_id):
    updated_text = (
        f"Username: {data['username']}\n"
        f"UserID: {data['user_id']}\n"
        f"Time: {', '.join(data['vouch_times'])}\n"
        f"Total vouches past 36 hours: {data['vouches_past_36_hours']}\n"
        f"Total vouches forever: {data['total_vouches']}"
    )
    bot.edit_message_text(chat_id=DB_CHANNEL_ID, message_id=message_id, text=updated_text)

def cleanup_db():
    messages = bot.get_chat_history(chat_id=DB_CHANNEL_ID, limit=100)
    now = datetime.utcnow()
    for message in messages:
        data = parse_vouch_data(message.text)
        if data['vouch_times']:
            last_vouch_time = datetime.fromisoformat(data['vouch_times'][-1])
            if (now - last_vouch_time).total_seconds() > 36 * 3600:
                bot.delete_message(chat_id=DB_CHANNEL_ID, message_id=message.message_id)

def admin(update: Update, context: CallbackContext):
    user = update.message.from_user
    if is_admin(user.id):
        if not pending_vouches:
            update.message.reply_text("No pending vouches.")
            return
        for vouch in pending_vouches:
            keyboard = [
                [InlineKeyboardButton("Approve", callback_data=f"approve_{vouch['user_id']}"),
                 InlineKeyboardButton("Deny", callback_data=f"deny_{vouch['user_id']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            caption = f"Pending Vouch from {vouch['username']}\nProduct: {vouch['product_name']}"
            bot.send_photo(chat_id=user.id, photo=vouch["image"], caption=caption, reply_markup=reply_markup)
    else:
        update.message.reply_text("You do not have admin privileges to view vouches.")

def handle_approval(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    action, user_id = query.data.split('_')
    user_id = int(user_id)
    vouch = next((v for v in pending_vouches if v["user_id"] == user_id), None)
    if vouch:
        pending_vouches.remove(vouch)
        vouch["image"].seek(0)
    if action == "approve":
        query.edit_message_caption(caption="Vouch Approved ✅")
        approval_time = datetime.utcnow().isoformat()
        bot.send_photo(chat_id=CHANNEL_ID, photo=vouch["image"], caption=f'🍺 <b>{vouch["product_name"].upper()}</b>', parse_mode="HTML")
        update_vouch_data(user_id=user_id, username=query.from_user.username, approval_time=approval_time)

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ASK_PRODUCT: [MessageHandler(Filters.text & ~Filters.command, ask_product)], ASK_IMAGE: [MessageHandler(Filters.photo, receive_image)]},
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Vouch submission canceled."))]
    )
    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("admin", admin))
    dp.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|deny)_"))
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()

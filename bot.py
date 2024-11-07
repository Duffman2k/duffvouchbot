import logging
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import CommandHandler, MessageHandler, Filters, CallbackContext, Updater, CallbackQueryHandler, ConversationHandler
from PIL import Image
import requests
from io import BytesIO
import os
from datetime import datetime, timedelta
import json
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Firebase credentials from environment variable
try:
    firebase_credentials = json.loads(os.getenv("FIREBASE_CREDENTIALS"))
    cred = credentials.Certificate(firebase_credentials)
    firebase_admin.initialize_app(cred)
    logger.info("Firebase initialized successfully.")
except Exception as e:
    logger.error(f"Error initializing Firebase: {e}")

# Initialize Firestore client
db = firestore.client()

TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_CHANNEL_ID = int(os.getenv("PUBLIC_CHANNEL_ID"))  # Public channel for approved vouches
APPROVAL_CHANNEL_ID = int(os.getenv("APPROVAL_CHANNEL_ID"))  # Private channel for admin-only approvals
VIP_GROUP_ID = int(os.getenv("VIP_GROUP_ID"))  # VIP group for frequent vouch users

bot = Bot(TOKEN)
WATERMARK_URL = 'https://i.imgur.com/rZTD37V.png'
user_data = {}  # Temporary storage for ongoing vouch process
ASK_PRODUCT, ASK_IMAGE = range(2)

def start(update: Update, context: CallbackContext):
    logger.info(f"User {update.message.from_user.id} started the bot.")
    update.message.reply_text("Welcome! Please type the name of the product for this vouch.")
    return ASK_PRODUCT

def ask_product(update: Update, context: CallbackContext):
    user = update.message.from_user
    product_name = update.message.text
    user_data[user.id] = {"product_name": product_name}
    logger.info(f"User {user.id} provided product name: {product_name}")
    update.message.reply_text("Thank you! Now, please send the image for the vouch.")
    return ASK_IMAGE

def receive_image(update: Update, context: CallbackContext):
    user = update.message.from_user
    product_name = user_data.get(user.id, {}).get("product_name", "Unknown Product")
    photo_file = update.message.photo[-1].get_file()
    image_url = photo_file.file_path
    logger.info(f"Image received from user {user.id}, product: {product_name}")
    update.message.reply_text("Image received! Applying watermark...")

    try:
        watermarked_image = apply_watermark(image_url)
        image_bytes = BytesIO()
        watermarked_image.save(image_bytes, format='JPEG')
        image_bytes.seek(0)
        logger.info(f"Watermark applied successfully for user {user.id}.")
    except Exception as e:
        logger.error(f"Error applying watermark for user {user.id}: {e}")
        update.message.reply_text("Failed to process the image.")
        return ConversationHandler.END

    username = user.username or "No Username"
    submission_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    keyboard = [
        [InlineKeyboardButton("Approve", callback_data=f"approve_{user.id}"),
         InlineKeyboardButton("Deny", callback_data=f"deny_{user.id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = bot.send_photo(
        chat_id=APPROVAL_CHANNEL_ID,
        photo=image_bytes,
        caption=f"Pending Vouch Submission:\nUsername: @{username}\nUserID: {user.id}\nProduct: {product_name}\nTime: {submission_time}",
        reply_markup=reply_markup
    )
    user_data[user.id] = {"message_id": msg.message_id, "product_name": product_name, "username": username, "time": submission_time}
    logger.info(f"Vouch submission for user {user.id} posted in approval channel.")
    update.message.reply_text("Your vouch has been submitted for approval.")
    return ConversationHandler.END

def apply_watermark(image_url):
    try:
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
    except Exception as e:
        logger.error(f"Error during watermark application: {e}")
        raise

def handle_approval(update: Update, context: CallbackContext):
    query = update.callback_query
    action, user_id = query.data.split('_')
    user_id = int(user_id)
    vouch_data = user_data.get(user_id)

    if not vouch_data:
        logger.warning(f"No vouch data found for user {user_id}.")
        query.answer("Vouch data not found.")
        return

    message_id = vouch_data["message_id"]
    product_name = vouch_data["product_name"]
    username = vouch_data["username"]
    submission_time = vouch_data["time"]

    if action == "approve":
        bot.edit_message_caption(
            chat_id=APPROVAL_CHANNEL_ID,
            message_id=message_id,
            caption=f"✅ Vouch Approved!\nUsername: @{username}\nProduct: {product_name}\nTime: {submission_time}"
        )
        bot.send_photo(
            chat_id=PUBLIC_CHANNEL_ID,
            photo=query.message.photo[-1].file_id,
            caption=f"🍺 <b>{product_name.upper()}</b>",
            parse_mode="HTML"
        )

        try:
            doc_ref = db.collection("users").document(str(user_id))
            doc = doc_ref.get()
            if doc.exists:
                user_vouch_data = doc.to_dict()
                # Convert recent_vouch_times strings to datetime objects
                recent_vouches = [
                    datetime.fromisoformat(v) if isinstance(v, str) else v
                    for v in user_vouch_data["recent_vouch_times"]
                ]
                # Filter out vouches older than 36 hours
                recent_vouches = [
                    v for v in recent_vouches
                    if (datetime.utcnow() - v).total_seconds() <= 36 * 3600
                ]
                # Append the current time
                recent_vouches.append(datetime.utcnow())

                update_data = {
                    "username": username,
                    "recent_vouch_times": [v.isoformat() for v in recent_vouches],  # Save as ISO strings
                    "total_vouches": firestore.Increment(1),
                }

                # Add VIP check if applicable
                if len(recent_vouches) >= 10 and not user_vouch_data.get("is_vip", False):
                    bot.invite_chat_member(chat_id=VIP_GROUP_ID, user_id=user_id)
                    update_data["is_vip"] = True

                logger.info(f"Updating Firestore for user {user_id} with data: {update_data}")
                doc_ref.update(update_data)
                logger.info(f"Successfully updated Firestore for user {user_id}")
            else:
                doc_ref.set({
                    "username": username,
                    "recent_vouch_times": [datetime.utcnow().isoformat()],
                    "total_vouches": 1,
                    "is_vip": False
                })
                logger.info(f"New document created for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to update Firestore for user {user_id}: {e}")

        query.answer("Vouch approved and posted.")

    elif action == "deny":
        bot.edit_message_caption(
            chat_id=APPROVAL_CHANNEL_ID,
            message_id=message_id,
            caption=f"❌ Vouch Denied.\nUsername: @{username}\nProduct: {product_name}\nTime: {submission_time}"
        )
        query.answer("Vouch denied.")
        logger.info(f"Vouch denied for user {user_id}.")

    user_data.pop(user_id, None)

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PRODUCT: [MessageHandler(Filters.text & ~Filters.command, ask_product)],
            ASK_IMAGE: [MessageHandler(Filters.photo, receive_image)]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Vouch submission canceled."))]
    )

    dp.add_handler(conv_handler)
    dp.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|deny)_"))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()

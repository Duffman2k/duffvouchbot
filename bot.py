from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, MessageHandler, Filters, CallbackContext, Updater, CallbackQueryHandler, ConversationHandler
from PIL import Image
import requests
from io import BytesIO
import os

TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
bot = Bot(TOKEN)
CUSTOM_EMOJI_ID = "5839247436094117331"

# URL of the watermark image
WATERMARK_URL = 'https://i.imgur.com/rZTD37V.png'

# List of admin user IDs
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS").split(',')))  # Convert comma-separated IDs to a list of integersD

# In-memory storage for pending vouches and product name
pending_vouches = []
user_data = {}

# States for the conversation
ASK_PRODUCT, ASK_IMAGE = range(2)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def start(update: Update, context: CallbackContext):
    update.message.reply_text("Welcome! Please type the name of the product for this vouch.")
    return ASK_PRODUCT

def ask_product(update: Update, context: CallbackContext):
    user = update.message.from_user
    product_name = update.message.text

    # Store product name in user_data
    user_data[user.id] = {"product_name": product_name}
    update.message.reply_text("Thank you! Now, please send the image for the vouch.")
    return ASK_IMAGE

def receive_image(update: Update, context: CallbackContext):
    user = update.message.from_user
    product_name = user_data.get(user.id, {}).get("product_name", "Unknown Product")
    
    # Process the image
    photo_file = update.message.photo[-1].get_file()
    image_url = photo_file.file_path
    update.message.reply_text("Image received! Applying watermark...")

    # Apply watermark
    watermarked_image = apply_watermark(image_url)
    
    # Save the watermarked image to a BytesIO object
    image_bytes = BytesIO()
    try:
        watermarked_image.save(image_bytes, format='JPEG')
        image_bytes.seek(0)  # Ensure the file pointer is at the start
    except Exception as e:
        print(f"Error saving image to BytesIO: {e}")
        update.message.reply_text("Failed to process the image.")
        return ConversationHandler.END

    # Store the vouch information in pending vouches list
    pending_vouches.append({"user_id": user.id, "image": image_bytes, "product_name": product_name})

    # Clear user data after submission
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

    watermarked_image = Image.alpha_composite(base_image, overlay)
    return watermarked_image.convert("RGB")

def admin(update: Update, context: CallbackContext):
    user = update.message.from_user
    if is_admin(user.id):
        # Display all pending vouches
        if not pending_vouches:
            update.message.reply_text("No pending vouches.")
            return
        
        for vouch in pending_vouches:
            keyboard = [
                [InlineKeyboardButton("Approve", callback_data=f"approve_{vouch['user_id']}"),
                 InlineKeyboardButton("Deny", callback_data=f"deny_{vouch['user_id']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            caption = f"Pending Vouch\nProduct: {vouch['product_name']}"
            bot.send_photo(chat_id=user.id, photo=vouch["image"], caption=caption, reply_markup=reply_markup)
    else:
        update.message.reply_text("You do not have admin privileges to view vouches.")

def handle_approval(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    action, user_id = query.data.split('_')
    user_id = int(user_id)
    
    # Find the pending vouch and ensure the image is non-empty
    vouch = next((v for v in pending_vouches if v["user_id"] == user_id), None)
    if vouch:
        pending_vouches.remove(vouch)
        vouch["image"].seek(0)  # Ensure the file pointer is at the start

    # Process approval or denial
    if action == "approve":
        query.edit_message_caption(caption="Vouch Approved ✅")
        try:
            bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=vouch["image"],
                caption=f"<tg-emoji emoji-id=\"{CUSTOM_EMOJI_ID}\"></tg-emoji> {vouch['product_name']}",  # Use the custom emoji ID here
                parse_mode="HTML"  # Set parse mode to HTML to render the custom emoji
            )
        except Exception as e:
            print(f"Error posting to channel: {e}")
            query.edit_message_caption(caption="Failed to post vouch. Please try again.")
    elif action == "deny":
        query.edit_message_caption(caption="Vouch Denied ❌")

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("Vouch submission canceled.")
    return ConversationHandler.END

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Conversation handler to handle the sequence of asking for product name and image
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PRODUCT: [MessageHandler(Filters.text & ~Filters.command, ask_product)],
            ASK_IMAGE: [MessageHandler(Filters.photo, receive_image)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler("admin", admin))
    dp.add_handler(CallbackQueryHandler(handle_approval, pattern="^(approve|deny)_"))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
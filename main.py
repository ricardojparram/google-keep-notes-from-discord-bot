import os
import threading
import json
import logging
import time
import discord
import gkeepapi
from groq import Groq
from flask import Flask
from dotenv import load_dotenv

# Load environment variables (for local dev)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
GOOGLE_USER = os.getenv('GOOGLE_USER')
GOOGLE_APP_PASSWORD = os.getenv('GOOGLE_APP_PASSWORD')
GOOGLE_MASTER_TOKEN = os.getenv('GOOGLE_MASTER_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', 0))  # 0 will block everyone if not set
PORT = int(os.getenv('PORT', 8080))

# --- Flask Health Check ---
app = Flask(__name__)

@app.route('/')
def home():
    return "I'm alive"

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# --- KeepAPI Wrapper ---
class KeepClient:
    def __init__(self):
        self.keep = gkeepapi.Keep()
    
    def login(self):
        """Attempts to login using master token or credentials."""
        try:
            logger.info("KeepClient: Attempting login...")
            
            if GOOGLE_MASTER_TOKEN:
                logger.info("KeepClient: Using Master Token...")
                # authenticate returns None on success, raises exception on failure
                self.keep.authenticate(GOOGLE_USER, GOOGLE_MASTER_TOKEN)
                logger.info("KeepClient: Authenticated with Master Token successfully.")
            elif GOOGLE_USER and GOOGLE_APP_PASSWORD:
                logger.info("KeepClient: Using Email/Password...")
                self.keep.authenticate(GOOGLE_USER, GOOGLE_APP_PASSWORD)
                token = self.keep.getMasterToken()
                logger.info(f"KeepClient: Authenticated. Master Token extracted (Starts with: {token[:5]}...)")
            else:
                raise Exception("No credentials provided (Need GOOGLE_MASTER_TOKEN or USER/APP_PASSWORD)")
                
        except Exception as e:
            logger.error(f"KeepClient: Error during login: {e}")
            raise e

    def _ensure_sync(self):
        """Syncs before modifying."""
        try:
            self.keep.sync()
        except Exception as e:
            logger.error(f"KeepClient: Sync error (pre): {e}")
            # Try to login again if sync fails (e.g. token expired)
            logger.info("KeepClient: Retrying login...")
            self.login()

    def _final_sync(self):
        """Syncs after modifying."""
        try:
            self.keep.sync()
        except Exception as e:
            logger.error(f"KeepClient: Sync error (post): {e}")
            raise e

    def create_note(self, title, content):
        self._ensure_sync()
        note = self.keep.createNote(title, content)
        # Optional: Add label or color
        # note.color = gkeepapi.node.ColorValue.Blue
        self._final_sync()
        return note

    def create_list(self, title, items):
        self._ensure_sync()
        # items should be a list of strings
        # createList syntax: (title, [(text, is_checked), ...])
        list_items = [(item, False) for item in items]
        glist = self.keep.createList(title, list_items)
        self._final_sync()
        return glist

# --- Groq Wrapper ---
client = None

def configure_groq():
    global client
    client = Groq(api_key=GROQ_API_KEY)

def analyze_text(text):
    """
    Sends text to Groq to determine if it should be a NOTE or LIST.
    Returns: JSON dict {'title': str, 'type': 'NOTE'|'LIST', 'content': str|list}
    """
    if not client:
        configure_groq()

    prompt = f"""
    Analiza el siguiente texto y extrae un título y el contenido.
    Determina si el formato más adecuado es una NOTA ('NOTE') o una LISTA ('LIST').
    
    Reglas:
    1. Si parece una lista de compras, tareas, o items separados, usa 'LIST'. Separa los items en una lista de strings.
    2. Si es texto corrido, usa 'NOTE'.
    3. Genera un título breve pero descriptivo basado en el contenido.
    4. Elimina saludos (ej: "Hola bot", "Guarda esto") o muletillas irrelevantes.
    5. Devuelve SOLAMENTE un objeto JSON válido con las claves: 'title', 'type', 'content'. NO escribas nada más fuera del JSON.
    
    Texto: "{text}"
    """
    
    try:
        completion = client.chat.completions.create(
            # llama3-70b-8192 está deprecado. Usamos llama-3.3-70b-versatile
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that processes text into structured JSON data. Output only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            response_format={"type": "json_object"}
        )
        
        response_content = completion.choices[0].message.content
        data = json.loads(response_content)
        return data
    except Exception as e:
        logger.error(f"Groq Error: {e}")
        return None

# --- Discord Bot ---
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

keep_client = KeepClient()

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    # Initialize services
    configure_groq()
    try:
        keep_client.login()
    except Exception as e:
        logger.critical(f"Failed to initialize Keep Client: {e}")

@bot.event
async def on_message(message):
    # Ignore own messages
    if message.author == bot.user:
        return

    # Security Check: Only OWNER_ID
    if message.author.id != OWNER_ID:
        return

    # React to acknowledge receipt
    try:
        await message.add_reaction('👀')
    except Exception as e:
        logger.warning(f"Could not react: {e}")

    user_text = message.content
    if not user_text:
        return

    # Process with Gemini
    analysis = analyze_text(user_text)
    
    if not analysis:
        await message.add_reaction('❌')
        await message.channel.send("Error analizando el texto con IA.")
        return

    title = analysis.get('title', 'Nota sin título')
    note_type = analysis.get('type', 'NOTE')
    content = analysis.get('content')

    try:
        if note_type == 'LIST' and isinstance(content, list):
            keep_client.create_list(title, content)
            response_msg = f"Lista creada: **{title}**"
        else:
            # Fallback to note if type is list but content isn't, or type is note
            # Ensure content is string
            if isinstance(content, list):
                content = "\n".join(content)
            keep_client.create_note(title, content)
            response_msg = f"Nota creada: **{title}**"
            
        await message.add_reaction('✅')
        # Optional: Reply with confirmation
        # await message.reply(response_msg)
        
    except Exception as e:
        logger.error(f"Keep Operation Failed: {e}")
        await message.add_reaction('❌')
        await message.channel.send(f"Error guardando en Keep: {str(e)}")

# --- Main Execution ---
if __name__ == '__main__':
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start Discord Bot
    bot.run(DISCORD_TOKEN)

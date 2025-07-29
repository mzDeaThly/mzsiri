import discord
from discord.ext import commands
from gtts import gTTS
import os
import asyncio
import uuid
import logging
from datetime import datetime
import google.generativeai as genai
from pydub import AudioSegment

# --- ส่วนการตั้งค่า Logging ---
# ตั้งค่าให้ Logger แสดงผลข้อมูลที่ระดับ INFO ขึ้นไป
# รูปแบบการแสดงผล: [เวลา] ระดับ: ข้อความ
logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s: %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
# --- สิ้นสุดส่วนการตั้งค่า Logging ---

# --- ตั้งค่า Gemini API ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini API Key ถูกโหลดและตั้งค่าเรียบร้อยแล้ว")
else:
    logger.warning("ไม่พบ GEMINI_API_KEY ใน Environment Variables, ฟังก์ชัน AI จะไม่ทำงาน")
# -------------------------

# กำหนด Intents ที่จำเป็นสำหรับบอท
# message_content: เพื่อให้บอทอ่านเนื้อหาข้อความได้
# voice_states: เพื่อให้บอทจัดการสถานะในห้องเสียงได้
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

# กำหนด Prefixes (คำนำหน้า) สำหรับคำสั่งบอท
bot = commands.Bot(command_prefix='!', intents=intents)

# --- Task สำหรับจัดการการเล่นเสียงในแต่ละ Server (Guild) ---
async def audio_player_task(guild_id):
    """
    Task ที่ทำงานเบื้องหลังเพื่อดึงข้อความจากคิวมาเล่นเสียง
    Task นี้จะทำงานตลอดเวลาสำหรับแต่ละ Guild ที่บอทอยู่ในช่องเสียง
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.error(f"Audio Task: ไม่พบ Guild ID {guild_id}")
        return

    message_queue = bot.message_queues.get(guild_id)
    if not message_queue:
        logger.error(f"Audio Task: ไม่พบคิวสำหรับ Guild ID {guild_id}")
        return

    logger.info(f"Audio player task เริ่มทำงานสำหรับ Guild: {guild.name}")

    while True:
        try:
            # รอจนกว่าจะมีข้อความใหม่ในคิว
            text_to_play = await message_queue.get()
            logger.info(f"[{guild.name}] ดึงข้อความจากคิว TTS: '{text_to_play}'")

            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                logger.warning(f"[{guild.name}] Voice client ไม่พร้อมใช้งาน, หยุดการเล่นเสียง")
                message_queue.task_done()
                continue

            # รอจนกว่าเสียงก่อนหน้าจะเล่นจบ
            while voice_client.is_playing():
                await asyncio.sleep(0.5)
            
            # สร้างไฟล์เสียงและปรับความเร็ว
            original_audio_file = text_to_speech_gtts(text_to_play, lang='th')
            final_audio_to_play = original_audio_file
            adjusted_file = None

            if bot.tts_speed != 1.0:
                logger.info(f"[{guild.name}] กำลังปรับความเร็วเสียงเป็น {bot.tts_speed}x...")
                adjusted_file = change_audio_speed(original_audio_file, bot.tts_speed)
                if adjusted_file:
                    final_audio_to_play = adjusted_file
                else:
                    logger.warning(f"[{guild.name}] ปรับความเร็วเสียงไม่สำเร็จ จะเล่นด้วยความเร็วปกติ")
            
            source = discord.FFmpegPCMAudio(source=final_audio_to_play)
            
            # Callback เพื่อลบไฟล์และทำเครื่องหมายว่า task เสร็จสิ้น
            def after_playing(error):
                cleanup_files_after_play(error, original_audio_file, adjusted_file)
                message_queue.task_done()
                logger.info(f"[{guild.name}] เล่นเสียงจบและลบไฟล์เรียบร้อย")

            voice_client.play(source, after=after_playing)

        except asyncio.CancelledError:
            logger.info(f"Audio player task สำหรับ Guild {guild.name} ถูกยกเลิกแล้ว")
            break
        except Exception as e:
            logger.error(f"ERROR ใน audio_player_task ของ Guild {guild.name}: {e}", exc_info=True)
            if 'message_queue' in locals() and not message_queue.empty():
                 message_queue.task_done()

# --- เหตุการณ์ของบอท (Events) ---

@bot.event
async def on_ready():
    """
    ฟังก์ชันที่ทำงานเมื่อบอทพร้อมใช้งาน
    """
    logger.info(f'บอท {bot.user.name} พร้อมใช้งานแล้ว!')
    logger.info('--------------------')
    
    # ตั้งค่าเริ่มต้นสำหรับโหมดการอ่านข้อความ
    bot.restricted_mode_guilds = set(guild.id for guild in bot.guilds) 
    bot.designated_text_channel_id = {}
    logger.info("ตั้งค่าโหมดการอ่านข้อความเป็นเริ่มต้น: 'ปิดโหมดสายลับ' (อ่านเฉพาะช่องที่กำหนด)")
    
    # สร้างคิวสำหรับเก็บข้อความและ Task สำหรับเล่นเสียง
    bot.message_queues = {}
    bot.worker_tasks = {}

    # ข้อมูลถามตอบ
    bot.qa_data = {
        "สวัสดี": "สวัสดีครับ มีอะไรให้ผมรับใช้ครับ",
        "เจ็บไหม": "เจ็บไหม? : มาย ไม่เจ็บหรอก... แค่ชินกับการไม่ได้เป็นคนสำคัญ",
        "ใครกลัวยาย": "ใครกล้ายาย : กอก้า กล้า กลัวยายไง",
        "เป็นอะไร": "เป็นอะไร ทำไมไม่เหมือนเดิม? : เขา เปลี่ยนไปนานแล้ว แต่ไม่มีใครสังเกต",
        "ใครไม่ชอบออกตัดเลือด": "ใครไม่ชอบออกตัดเลือด : โอ้ คำถามนี้ ถือว่าถามได้ดีเลย จากการประมวลผลใน Server Family Game 24 Hrs. แล้ว สรุปได้ว่า คนที่ไม่ชอบออกตัดเลือดคือ ตระกูล ม.ม้า นะคะ",
        "ใครผัวเยอะที่สุด": "ใครผัวเยอะที่สุด : โมเดลเมจขาตายไม่ออกตัดเลือด",
        "ใครขี้เมาที่สุด": "ใครขี้เมาที่สุด : เมษาเมจไงคะ ขี้เมาที่สุดแล้ว",
        "จนมาเห็นกับตา": "จนมาเห็นกับตาจนพาใจมาเจ็บ  ฉีกบ่มีหม่องเย็บ หัวใจที่ให้เจ้า",
        "มายรอเขาอยู่เหรอ": "มาย รอมาตลอด แต่เขาไม่เคยหันกลับมาเลย",
        "ใครจกที่สุด": "ใครจกที่สุด : ไก่มายไงคะ จกที่สุดแล้ว",
        "เจ้ตามเป็นอะไร": "เจ้ตามเป็นอะไร : เจ้ตามเป็นของทุกคนเลยนะจ๊ะ",
        "ใครหล่อที่สุด": "ใครหล่อที่สุด : พี่คิวสุดหล่อเจ้าของดิสไงคะ"
    }
    logger.info("ตั้งค่าข้อมูลถามตอบแบบกำหนดเองเรียบร้อยแล้ว.")

    # ตั้งค่าความเร็วการพูดเริ่มต้น
    bot.tts_speed = 1.2 
    logger.info(f"ตั้งค่าความเร็วการพูดเริ่มต้นเป็น {bot.tts_speed}x")
    
@bot.event
async def on_guild_join(guild):
    """เมื่อบอทเข้าเซิร์ฟเวอร์ใหม่ ให้ตั้งค่าเริ่มต้นเป็นโหมดจำกัด"""
    logger.info(f"บอทถูกเชิญเข้าเซิร์ฟเวอร์ใหม่: {guild.name} (ID: {guild.id})")
    bot.restricted_mode_guilds.add(guild.id)
    logger.info(f"ตั้งค่าเริ่มต้นสำหรับ {guild.name} เป็น 'ปิดโหมดสายลับ' เรียบร้อยแล้ว")

@bot.event
async def on_voice_state_update(member, before, after):
    """จัดการเมื่อสถานะเสียงของสมาชิกมีการเปลี่ยนแปลง"""
    if member == bot.user:
        return
    
    if before.channel and before.channel.guild.voice_client:
        if not member.guild.voice_client:
            guild_id = member.guild.id
            if guild_id in bot.worker_tasks:
                task = bot.worker_tasks.pop(guild_id)
                task.cancel()
                bot.message_queues.pop(guild_id, None)
                logger.info(f"บอทถูกตัดการเชื่อมต่อจากช่องเสียงใน Guild {member.guild.name}, หยุด worker task แล้ว")

    if member.guild.voice_client:
        voice_channel = member.guild.voice_client.channel
        if voice_channel:
            members_in_channel = [m for m in voice_channel.members if not m.bot]
            if len(members_in_channel) == 0:
                logger.info(f"ไม่มีสมาชิกเหลือในช่องเสียง {voice_channel.name} แล้ว บอทกำลังจะออกจากช่องเสียง...")
                try:
                    guild_id = member.guild.id
                    if guild_id in bot.worker_tasks:
                        task = bot.worker_tasks.pop(guild_id)
                        task.cancel()
                        bot.message_queues.pop(guild_id, None)
                        logger.info(f"หยุด worker task สำหรับ Guild {member.guild.name} ก่อนออกจากห้อง")

                    await member.guild.voice_client.disconnect()
                    logger.info("บอทออกจากช่องเสียงแล้ว!")
                except Exception as e:
                    logger.error(f"ERROR: เกิดข้อผิดพลาดขณะออกจากช่องเสียง: {e}")

# --- ส่วนของการบันทึกข้อความอัตโนมัติ ---
@bot.event
async def on_message(message):
    """
    ฟังก์ชันที่ทำงานทุกครั้งเมื่อมีข้อความใหม่
    """
    # 1. ไม่สนใจข้อความจากบอทตัวเอง
    if message.author == bot.user:
        return

    # 2. [ส่วนที่เพิ่มใหม่] บันทึกข้อความจากทุกแชนเนลที่บอทเห็น
    if message.guild:
        log_text = (f"[{message.guild.name} > #{message.channel.name}] "
                    f"{message.author.display_name}: {message.content}")
        logger.info(log_text)

    # 3. ตรวจสอบและประมวลผลคำสั่ง (เหมือนเดิม)
    if message.content.startswith(tuple(bot.command_prefix)):
        await bot.process_commands(message)
        return

    # 4. ตรรกะสำหรับส่งข้อความไปอ่านออกเสียง (TTS)
    if not message.guild:
        return
    
    guild_id = message.guild.id
    if not message.guild.voice_client or not message.guild.voice_client.is_connected() or guild_id not in bot.message_queues:
        return

    # ตรวจสอบว่าต้องอ่านข้อความจากช่องนี้หรือไม่
    if guild_id in bot.restricted_mode_guilds:
        designated_channel_id = bot.designated_text_channel_id.get(guild_id)
        if not designated_channel_id or message.channel.id != designated_channel_id:
            return 

    # ส่งข้อความเข้าคิวสำหรับ TTS
    text_to_read = message.content
    queue = bot.message_queues.get(guild_id)
    if queue:
        await queue.put(text_to_read)
        # ไม่ต้อง log ซ้ำ เพราะ log ไปแล้วข้างบน


# --- ฟังก์ชันเสริม (Helper Functions) ---
def text_to_speech_gtts(text, lang='th'):
    """แปลงข้อความเป็นไฟล์เสียง MP3 ด้วย gTTS"""
    unique_filename = f"temp_audio_{uuid.uuid4().hex}.mp3"
    tts = gTTS(text=text, lang=lang)
    tts.save(unique_filename)
    return unique_filename

def change_audio_speed(input_path, speed_factor):
    """ปรับความเร็วของไฟล์เสียงด้วย pydub"""
    try:
        audio = AudioSegment.from_mp3(input_path)
        speed_adjusted_audio = audio.speedup(playback_speed=speed_factor)
        output_path = input_path.replace(".mp3", "_speed_adjusted.mp3")
        speed_adjusted_audio.export(output_path, format="mp3")
        return output_path
    except Exception as e:
        logger.error(f"Failed to adjust audio speed: {e}")
        return None

def cleanup_files_after_play(error, original_file, adjusted_file):
    """ลบไฟล์เสียงชั่วคราวหลังเล่นจบ"""
    if error:
        logger.error(f'Player error: {error}')
    try:
        if os.path.exists(original_file):
            os.remove(original_file)
        if adjusted_file and os.path.exists(adjusted_file):
            os.remove(adjusted_file)
    except OSError as e:
        logger.error(f"Error during file cleanup: {e}")

# --- คำสั่งของบอท (Commands) ---

@bot.command(name='setspeed', aliases=['ความเร็ว'])
async def set_speed(ctx, speed: float):
    """ตั้งค่าความเร็วในการเล่นเสียง TTS"""
    if 0.5 <= speed <= 2.0:
        bot.tts_speed = speed
        await ctx.send(f"✅ ปรับความเร็วการพูดเป็น `{speed}x` แล้ว")
        logger.info(f"TTS speed has been set to {speed}x by {ctx.author.display_name}")
    else:
        await ctx.send("❌ โปรดระบุความเร็วระหว่าง 0.5 ถึง 2.0 ครับ")

@set_speed.error
async def set_speed_error(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ รูปแบบไม่ถูกต้อง, โปรดใช้ตัวเลข เช่น `!setspeed 1.5`")

@bot.command(name='เข้ามา', aliases=['มานี่', 'ตามมา'])
async def join_command(ctx):
    """คำสั่งให้บอทเข้าช่องเสียง"""
    if not ctx.message.author.voice:
        await ctx.send("คุณต้องอยู่ในช่องเสียงก่อนจึงจะเรียกใช้คำสั่งนี้ได้!")
        return
    
    bot.designated_text_channel_id[ctx.guild.id] = ctx.channel.id
    logger.info(f"ตั้งค่าช่องข้อความที่กำหนดสำหรับ Guild {ctx.guild.name}: {ctx.channel.name} ({ctx.channel.id})")

    voice_channel = ctx.message.author.voice.channel
    if ctx.voice_client:
        if ctx.voice_client.channel == voice_channel:
            await ctx.send(f"บอทอยู่ในช่องเสียง **{ctx.voice_client.channel.name}** อยู่แล้วครับ")
        else:
            await ctx.voice_client.move_to(voice_channel)
            await ctx.send(f"ย้ายไปช่องเสียง: **{voice_channel.name}** แล้ว")
            logger.info(f"บอทย้ายไปช่องเสียง: {voice_channel.name}")
        return

    try:
        await voice_channel.connect()
        await ctx.send(f"🔊 เข้าร่วม **{voice_channel.name}** แล้ว! จะอ่านข้อความให้อัตโนมัติ (Dev By mzDear)")
        logger.info(f"บอทเข้าร่วมช่องเสียง: {voice_channel.name}")

        guild_id = ctx.guild.id
        if guild_id in bot.worker_tasks:
            bot.worker_tasks[guild_id].cancel()
        
        bot.message_queues[guild_id] = asyncio.Queue()
        bot.worker_tasks[guild_id] = bot.loop.create_task(audio_player_task(guild_id))
        
    except Exception as e:
        await ctx.send(f"ไม่สามารถเข้าร่วมช่องเสียงได้: {e}")
        logger.error(f"ERROR: ไม่สามารถเข้าร่วมช่องเสียงได้: {e}")

@bot.command(name='ไปไกลๆ', aliases=['หนีไป', 'ออกไป'])
async def leave(ctx):
    """คำสั่งให้บอทออกจากช่องเสียง"""
    if ctx.voice_client:
        guild_id = ctx.guild.id
        if guild_id in bot.worker_tasks:
            task = bot.worker_tasks.pop(guild_id)
            task.cancel()
            logger.info(f"Worker task สำหรับ Guild {ctx.guild.name} ถูกยกเลิกโดยคำสั่ง")
        
        if guild_id in bot.message_queues:
            del bot.message_queues[guild_id]
            logger.info(f"ล้างคิวข้อความสำหรับ Guild {ctx.guild.name}")

        if guild_id in bot.designated_text_channel_id:
            del bot.designated_text_channel_id[guild_id]
            logger.info(f"ล้าง designated_text_channel_id สำหรับ Guild {ctx.guild.name}")
            
        await ctx.voice_client.disconnect()
        await ctx.send("👋 ออกจากช่องเสียงแล้ว.")
        logger.info("บอทออกจากช่องเสียง")
    else:
        await ctx.send("❌ บอทไม่ได้อยู่ในช่องเสียงใดๆ!")

@bot.command(name='เปิดโหมดสายลับ', aliases=['readall', 'enableallchannels'])
@commands.has_role("เจ้าของดิส")
async def enable_read_all_channels(ctx):
    """เปิดโหมดอ่านทุกช่อง"""
    if ctx.guild.id not in bot.restricted_mode_guilds:
        await ctx.send("✅ บอทกำลังอยู่ในโหมด 'สายลับ' ครับ.")
    else:
        bot.restricted_mode_guilds.remove(ctx.guild.id)
        await ctx.send("✅ เปลี่ยนโหมด: บอทสายลับกำลังทำงาน")
        logger.info(f"Guild {ctx.guild.name} switched to 'read all channels' mode.")

@bot.command(name='ปิดโหมดสายลับ', aliases=['restrictchannels', 'disableallchannels'])
@commands.has_role("เจ้าของดิส")
async def disable_read_all_channels(ctx):
    """ปิดโหมดอ่านทุกช่อง (อ่านเฉพาะช่องที่กำหนด)"""
    if ctx.guild.id in bot.restricted_mode_guilds:
        await ctx.send("✅ บอทกำลังอยู่ในโหมด 'มุ้งมิ้ง ไม่สนโลก' ครับ.")
    else:
        bot.restricted_mode_guilds.add(ctx.guild.id)
        if ctx.guild.id not in bot.designated_text_channel_id:
            bot.designated_text_channel_id[ctx.guild.id] = ctx.channel.id
            logger.info(f"ตั้งค่าช่องข้อความที่กำหนดสำหรับ Guild {ctx.guild.name} เป็น {ctx.channel.name} ({ctx.channel.id})")
        await ctx.send(f"✅ เปลี่ยนโหมด: บอทสายลับ ปิดการใช้งานแล้ว")
        logger.info(f"Guild {ctx.guild.name} switched to 'restricted channels' mode.")

@bot.command(name='ดูสถานะการอ่าน', aliases=['readmode', 'readstatus'])
@commands.has_role("เจ้าของดิส")
async def view_read_mode_status(ctx):
    """ดูสถานะโหมดการอ่านข้อความ"""
    mode_status = "อ่านจากทุกช่องแชทใน Guild"
    designated_channel_info = ""
    if ctx.guild.id in bot.restricted_mode_guilds:
        mode_status = "อ่านเฉพาะจากช่องแชทที่กำหนด"
        designated_channel_id = bot.designated_text_channel_id.get(ctx.guild.id)
        if designated_channel_id:
            designated_channel = bot.get_channel(designated_channel_id)
            designated_channel_info = f" (ช่องที่กำหนด: {designated_channel.mention})" if designated_channel else f" (ช่องที่กำหนด: ID {designated_channel_id} ไม่พบ)"
        else:
            designated_channel_info = " (ช่องที่กำหนด: ยังไม่ได้ตั้งค่า โปรดใช้ `!เข้ามา` ในช่องที่ต้องการ)"
    await ctx.send(f"ℹ️ สถานะการอ่านข้อความใน Guild นี้: **{mode_status}**{designated_channel_info}")

@bot.command(name='ai', aliases=['askai', 'queryai'])
async def ask_ai_question(ctx, *, question: str):
    """ถามคำถามบอทด้วย AI Gemini"""
    if not GEMINI_API_KEY:
        await ctx.send("❌ ขออภัยครับ ฟังก์ชัน AI ยังไม่พร้อมใช้งาน (ผู้ดูแลยังไม่ได้ตั้งค่า API Key)")
        return

    guild_id = ctx.guild.id
    if not ctx.guild or not ctx.guild.voice_client or not ctx.guild.voice_client.is_connected() or guild_id not in bot.message_queues:
        await ctx.send("🔊 ผมต้องอยู่ในช่องเสียงและระบบอ่านข้อความต้องพร้อมใช้งานก่อนครับ! กรุณาสั่ง `!เข้ามา` ก่อน")
        return

    processing_message = await ctx.send("🧠 กำลังประมวลผลคำถามของคุณด้วย Gemini... กรุณารอสักครู่")

    try:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        response = await model.generate_content_async(question)
        answer = response.text

        await processing_message.edit(content=f"**คำถาม:** {question}\n**คำตอบจาก Gemini:**\n{answer}")
        
        queue = bot.message_queues.get(guild_id)
        if queue:
            await queue.put(answer)

    except Exception as e:
        await processing_message.edit(content=f"❌ เกิดข้อผิดพลาดในการเรียกใช้ Gemini API: {e}")
        logger.error(f"ERROR: เกิดข้อผิดพลาดจาก Gemini API: {e}", exc_info=True)

@bot.command(name='ถาม', aliases=['ask', 'query'])
async def ask_question_custom(ctx, *, question: str):
    """ถามคำถามบอทจากข้อมูลที่กำหนดเอง"""
    guild_id = ctx.guild.id
    if not ctx.guild or not ctx.guild.voice_client or not ctx.guild.voice_client.is_connected() or guild_id not in bot.message_queues:
        await ctx.send("🔊 ผมต้องอยู่ในช่องเสียงและระบบอ่านข้อความต้องพร้อมใช้งานก่อนครับ! กรุณาสั่ง `!เข้ามา` ก่อน")
        return

    normalized_question = question.strip()
    answer = bot.qa_data.get(normalized_question, "ขออภัยครับ ผมยังไม่เข้าใจคำถามของคุณ")

    await ctx.send(f"คำตอบ: {answer}")

    try:
        queue = bot.message_queues.get(guild_id)
        if queue:
            await queue.put(answer)

    except Exception as e:
        logger.error(f"ERROR: ไม่สามารถส่งคำตอบเข้าคิวได้: {e}", exc_info=True)
        await ctx.send(f"❌ ขออภัยครับ เกิดข้อผิดพลาดในการส่งคำตอบไปอ่าน: {e}")

# --- ส่วนการจัดการข้อผิดพลาดสำหรับคำสั่ง ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f"🚫 คุณไม่มีสิทธิ์ในการใช้คำสั่งนี้ (จำเป็นต้องมีสิทธิ์: `{' '.join(error.missing_permissions)}`).")
    elif isinstance(error, commands.MissingRole):
        await ctx.send(f"🚫 คุณไม่มี Role '{error.missing_role}' ที่จำเป็นในการใช้คำสั่งนี้.")
    elif isinstance(error, commands.CommandNotFound):
        pass 
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ มีข้อผิดพลาดในการใส่ข้อมูล: {error}\nโปรดตรวจสอบรูปแบบคำสั่ง.")
    else:
        logger.error(f"❌ เกิดข้อผิดพลาดไม่คาดคิดในคำสั่ง {ctx.command}: {error}", exc_info=True)
        await ctx.send(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {type(error).__name__} - {error}")


# --- ส่วนรันบอท ---
discord_bot_token = os.getenv('DISCORD_BOT_TOKEN')
if discord_bot_token is None:
    logger.error("ERROR: ไม่พบ Discord Bot Token ใน Environment Variables.")
    exit()

try:
    bot.run(discord_bot_token)
except Exception as e:
    logger.error(f"An unexpected error occurred during bot run: {e}", exc_info=True)

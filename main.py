import os
import html
import time
import random
import asyncio
import logging
from collections import defaultdict, deque
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import NetworkError, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import database

# 加载环境变量
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))   # 管理员本人的 Telegram 数字 ID（用于指令权限校验）
GROUP_ID = int(os.getenv("GROUP_ID", "0"))    # 话题群组的 Chat ID（负数，用于消息路由）
PROXY_URL = os.getenv("PROXY_URL", "")

if not BOT_TOKEN or ADMIN_ID == 0 or GROUP_ID == 0:
    print("请检查配置，确保已设置 BOT_TOKEN、ADMIN_ID 和 GROUP_ID")
    exit(1)


def is_admin(update) -> bool:
    """消息是否由管理员本人发出（在群组内或私聊均适用）"""
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)

# 确保日志目录存在
os.makedirs("data", exist_ok=True)

# 中文日志级别映射
_LEVEL_CN = {"DEBUG": "调试", "INFO": "信息", "WARNING": "警告", "ERROR": "错误", "CRITICAL": "严重"}


class ChineseFormatter(logging.Formatter):
    """将日志级别显示为中文，时间精确到秒"""
    def format(self, record):
        record.levelcn = _LEVEL_CN.get(record.levelname, record.levelname)
        return super().format(record)


# 配置日志记录 (控制台 + 文件)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = ChineseFormatter('%(asctime)s [%(levelcn)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# 控制台日志
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# 文件日志 (最大5MB, 循环保留3个备份)
file_handler = RotatingFileHandler('data/bot.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 屏蔽第三方库的刷屏日志（如 httpx 每次轮询都打印的 HTTP 请求），只保留警告及以上
for noisy in ("httpx", "httpcore", "apscheduler", "telegram.ext.Application"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Updater 在轮询失败时会以 ERROR 级直接打印 "Error while getting Updates" 并附堆栈，
# 断网/代理抖动期间会持续刷屏。这类错误可自动重试，由全局 error_handler 统一记一行警告即可，
# 故将其日志级别提到 CRITICAL，仅保留真正致命的信息。
logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)

log = logging.getLogger("AWRelay")

# ---- 内存状态 ----
admin_states = {}                       # 管理员交互式输入状态
pending_captcha = {}                    # chat_id -> 正确答案，表示该用户正在等待验证
media_groups = {}                       # media_group_id -> 聚合中的媒体组数据
user_msg_times = defaultdict(deque)     # chat_id -> 最近消息时间戳，用于限流

# ---- 限流配置 ----
RATE_LIMIT_COUNT = 5        # 时间窗内允许的最大消息数
RATE_LIMIT_WINDOW = 10      # 限流时间窗（秒）
MEDIA_GROUP_DELAY = 2.0     # 媒体组聚合等待时间（秒）

def is_spam(text):
    """检查消息是否包含垃圾广告关键词"""
    if database.get_setting("spam_enabled", "1") != "1":
        return False

    if not text:
        return False
    text_lower = text.lower()

    keywords_str = database.get_setting("spam_keywords", "")
    spam_words_list = [k.strip() for k in keywords_str.split(",") if k.strip()]

    for keyword in spam_words_list:
        if keyword.lower() in text_lower:
            return True
    return False


def is_rate_limited(chat_id):
    """简单滑动窗口限流：窗口内超过阈值则拦截"""
    now = time.time()
    dq = user_msg_times[chat_id]
    while dq and now - dq[0] > RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_COUNT:
        return True
    dq.append(now)
    return False


def build_menu_text_and_keyboard():
    """构建设置菜单的文本和按钮（HTML 格式，避免关键词特殊字符破坏排版）"""
    spam_enabled = database.get_setting("spam_enabled", "1") == "1"
    captcha_enabled = database.get_setting("captcha_enabled", "1") == "1"
    keywords = database.get_setting("spam_keywords", "")

    text = (
        "⚙️ <b>AWRelay 控制面板</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🛡️ 广告拦截　{'🟢 开启' if spam_enabled else '🔴 关闭'}\n"
        f"🤖 人机验证　{'🟢 开启' if captcha_enabled else '🔴 关闭'}\n"
        "━━━━━━━━━━━━━━\n"
        "📝 <b>拦截关键词</b>\n"
        f"<code>{html.escape(keywords) if keywords else '（暂无）'}</code>"
    )

    keyboard = [
        [InlineKeyboardButton("🔀 切换广告拦截", callback_data="toggle_spam")],
        [InlineKeyboardButton("🤖 切换人机验证", callback_data="toggle_captcha")],
        [
            InlineKeyboardButton("➕ 添加关键词", callback_data="add_word"),
            InlineKeyboardButton("➖ 清空关键词", callback_data="clear_words")
        ]
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def send_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id):
    """向用户发送一道数学题人机验证，正确答案存入 pending_captcha"""
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    answer = a + b
    pending_captcha[chat_id] = answer

    # 构造 4 个选项（含正确答案），打乱顺序
    options = {answer}
    while len(options) < 4:
        options.add(random.randint(2, 18))
    options = list(options)
    random.shuffle(options)

    keyboard = [[InlineKeyboardButton(str(opt), callback_data=f"captcha:{opt}") for opt in options]]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔐 <b>人机验证</b>\n"
            "━━━━━━━━━━━━━━\n"
            "为防止机器人骚扰，发送消息前请先完成验证：\n\n"
            f"👉 <b>{a} + {b} = ?</b>\n\n"
            "请点击下方正确答案。"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理人机验证按钮点击"""
    query = update.callback_query
    chat_id = query.message.chat.id
    chosen = int(query.data.split(":", 1)[1])

    expected = pending_captcha.get(chat_id)
    if expected is None:
        await query.answer("该验证已失效，请重新发送消息。", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if chosen == expected:
        await query.answer("✅ 验证通过！")
        pending_captcha.pop(chat_id, None)
        await asyncio.to_thread(database.set_verified, chat_id)
        await query.edit_message_text(
            "✅ <b>验证通过</b>\n"
            "━━━━━━━━━━━━━━\n"
            "现在可以直接给我发消息了，我会第一时间转达给管理员。",
            parse_mode="HTML"
        )
        log.info(f"用户 {chat_id} 通过了人机验证")
    else:
        await query.answer("❌ 答案错误，请重试。", show_alert=True)
        await send_captcha(update, context, chat_id)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """打开设置菜单"""
    if not is_admin(update):
        return
    text, reply_markup = build_menu_text_and_keyboard()
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示帮助信息"""
    if is_admin(update):
        text = (
            "📖 <b>管理员指南</b>\n"
            "━━━━━━━━━━━━━━\n"
            "/menu　打开控制面板（广告拦截 / 人机验证 / 关键词）\n"
            "/ban　回复某条转发消息以拉黑发送者\n"
            "/unban　回复某条转发消息以解除拉黑\n"
            "/cancel　取消当前的关键词输入\n"
            "/help　显示本帮助\n"
            "━━━━━━━━━━━━━━\n"
            "💡 直接<b>回复</b>用户的转发消息，即可把内容回传给该用户。"
        )
    else:
        text = (
            "ℹ️ <b>使用说明</b>\n"
            "━━━━━━━━━━━━━━\n"
            "直接给我发消息就行，我会转达给管理员，请耐心等待回复。"
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消当前的等待输入状态"""
    if not is_admin(update):
        return
    if admin_states.pop(ADMIN_ID, None):
        await update.message.reply_text("✅ 已取消当前操作。")
    else:
        await update.message.reply_text("ℹ️ 当前没有进行中的操作。")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理菜单按钮点击"""
    query = update.callback_query
    if not (update.effective_user and update.effective_user.id == ADMIN_ID):
        await query.answer("无权限", show_alert=True)
        return

    await query.answer()
    action = query.data

    if action == "toggle_spam":
        current = database.get_setting("spam_enabled", "1")
        await asyncio.to_thread(database.set_setting, "spam_enabled", "0" if current == "1" else "1")
        text, reply_markup = build_menu_text_and_keyboard()
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

    elif action == "toggle_captcha":
        current = database.get_setting("captcha_enabled", "1")
        await asyncio.to_thread(database.set_setting, "captcha_enabled", "0" if current == "1" else "1")
        text, reply_markup = build_menu_text_and_keyboard()
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

    elif action == "add_word":
        admin_states[ADMIN_ID] = 'WAIT_WORD'
        await query.message.reply_text(
            "✏️ 请发送要添加的关键词，多个用英文逗号分隔。\n"
            "例如：<code>USDT,博彩,加微信</code>\n\n"
            "发送 /cancel 可取消。",
            parse_mode="HTML"
        )

    elif action == "clear_words":
        await asyncio.to_thread(database.set_setting, "spam_keywords", "")
        text, reply_markup = build_menu_text_and_keyboard()
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    chat_id = update.effective_chat.id
    if is_admin(update):
        await update.message.reply_text(
            "👑 <b>欢迎回来，管理员</b>\n"
            "━━━━━━━━━━━━━━\n"
            "机器人已就绪，正在等待用户消息。\n\n"
            "• 用户消息会自动转发到这里\n"
            "• <b>回复</b>某条消息即可回传给该用户\n"
            "• 发送 /menu 打开控制面板\n"
            "• 发送 /help 查看全部指令",
            parse_mode="HTML"
        )
        return

    # 普通用户：若开启人机验证且尚未通过，直接弹出验证题
    if database.get_setting("captcha_enabled", "1") == "1" and not await asyncio.to_thread(database.is_verified, chat_id):
        await send_captcha(update, context, chat_id)
        return

    await update.message.reply_text(
        "👋 <b>你好呀</b>\n"
        "━━━━━━━━━━━━━━\n"
        "直接给我发消息就行，我会帮你转达给管理员，请耐心等待回复。",
        parse_mode="HTML"
    )


async def ban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员拉黑用户"""
    if not is_admin(update):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ 请<b>回复</b>需要拉黑的用户消息，再发送 /ban", parse_mode="HTML")
        return

    admin_msg_id = update.message.reply_to_message.message_id
    mapping = await asyncio.to_thread(database.get_mapping, admin_msg_id)
    if mapping:
        user_chat_id = mapping[0]
        await asyncio.to_thread(database.ban_user, user_chat_id)
        await update.message.reply_text(
            f"🚫 已拉黑用户 <code>{user_chat_id}</code>，将自动拦截其后续消息。",
            parse_mode="HTML"
        )
        log.info(f"管理员拉黑了用户 {user_chat_id}")
    else:
        await update.message.reply_text("⚠️ 找不到该消息对应的发送者（可能是旧消息或非转发消息）。")


async def unban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员解除拉黑"""
    if not is_admin(update):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ 请<b>回复</b>需要解除拉黑的用户消息，再发送 /unban", parse_mode="HTML")
        return

    admin_msg_id = update.message.reply_to_message.message_id
    mapping = await asyncio.to_thread(database.get_mapping, admin_msg_id)
    if mapping:
        user_chat_id = mapping[0]
        await asyncio.to_thread(database.unban_user, user_chat_id)
        await update.message.reply_text(
            f"✅ 已将用户 <code>{user_chat_id}</code> 移出黑名单。",
            parse_mode="HTML"
        )
        log.info(f"管理员解除拉黑了用户 {user_chat_id}")
    else:
        await update.message.reply_text("⚠️ 找不到该消息对应的发送者（可能是旧消息或非转发消息）。")


def supports_caption(msg):
    """判断该消息类型是否支持 caption。"""
    return bool(msg.photo or msg.video or msg.audio or msg.animation or msg.document or msg.voice)


async def get_or_create_topic(bot, user, chat_id) -> int:
    """获取或创建该用户在话题群组中对应的话题，返回 message_thread_id。"""
    topic_id = await asyncio.to_thread(database.get_topic, chat_id)
    if topic_id:
        return topic_id

    # 话题名称不超过 128 字符（Telegram 限制）
    suffix = f" · {chat_id}"                   # 最多 14 字符（3 + 最长10位ID）
    base = (f"{user.first_name or ''} {user.last_name or ''}".strip()
            or f"用户{chat_id}")
    name = base[:128 - len(suffix)] + suffix

    topic = await bot.create_forum_topic(chat_id=GROUP_ID, name=name)
    topic_id = topic.message_thread_id
    await asyncio.to_thread(database.save_topic, chat_id, topic_id)

    info = f"🆔 <code>{chat_id}</code>"
    if user.username:
        info += f"\n📎 @{html.escape(user.username)}"
    await bot.send_message(
        chat_id=GROUP_ID, message_thread_id=topic_id,
        text=info, parse_mode="HTML"
    )
    log.info(f"为用户 {chat_id} ({name}) 创建了新话题 {topic_id}")
    return topic_id


async def forward_to_topic(context, chat_id, user, messages):
    """将用户消息转发到话题群组中对应的话题，并保存映射。"""
    try:
        topic_id = await get_or_create_topic(context.bot, user, chat_id)
    except Exception as e:
        # 话题创建失败（群组未开启话题功能、bot 缺少管理话题权限等）
        log.error(f"创建话题失败（用户 {chat_id}）：{e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"⚠️ 无法为用户 <code>{chat_id}</code> 创建话题：\n<code>{html.escape(str(e))}</code>\n\n"
                     f"请确认群组已开启「话题」功能且 bot 拥有「管理话题」权限。",
                parse_mode="HTML",
            )
        except Exception:
            pass
        raise

    for msg in messages:
        try:
            sent = await context.bot.copy_message(
                chat_id=GROUP_ID,
                from_chat_id=chat_id,
                message_id=msg.message_id,
                message_thread_id=topic_id,
            )
            await asyncio.to_thread(database.save_mapping, sent.message_id, chat_id, msg.message_id)
        except Exception as e:
            log.error(f"转发消息到话题失败：{e}")


async def flush_media_group(context: ContextTypes.DEFAULT_TYPE):
    """媒体组聚合定时器回调：等待窗口结束后，一次性转发整组消息。"""
    group_id = context.job.data
    group = media_groups.pop(group_id, None)
    if not group:
        return
    try:
        await forward_to_topic(context, group["chat_id"], group["user"], group["messages"])
    except Exception as e:
        log.error(f"媒体组转发失败：{e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有文本和多媒体消息"""
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    # 【情况一】来自话题群组的消息（管理员发出）
    if chat_id == GROUP_ID:
        # 1a. 回复某条转发消息 → 查映射回传（同时用于 /ban 等指令的上下文）
        if update.message.reply_to_message:
            admin_states.pop(ADMIN_ID, None)
            admin_msg_id = update.message.reply_to_message.message_id
            mapping = await asyncio.to_thread(database.get_mapping, admin_msg_id)
            if mapping:
                user_chat_id = mapping[0]
                try:
                    await context.bot.copy_message(
                        chat_id=user_chat_id,
                        from_chat_id=GROUP_ID,
                        message_id=msg_id,
                    )
                except Exception as e:
                    await update.message.reply_text(
                        f"❌ 发送失败，用户可能已屏蔽机器人。\n<code>{html.escape(str(e))}</code>",
                        parse_mode="HTML",
                    )
            else:
                await update.message.reply_text("⚠️ 找不到该消息对应的发送者（可能是旧消息或非转发消息）。")
            return

        # 1b. 在用户话题中直接发送（非回复）→ 通过话题 ID 查用户并回传
        if update.message.is_topic_message and update.message.message_thread_id:
            topic_id = update.message.message_thread_id
            user_chat_id = await asyncio.to_thread(database.get_user_by_topic, topic_id)
            if user_chat_id:
                try:
                    await context.bot.copy_message(
                        chat_id=user_chat_id,
                        from_chat_id=GROUP_ID,
                        message_id=msg_id,
                    )
                except Exception as e:
                    await update.message.reply_text(
                        f"❌ 发送失败，用户可能已屏蔽机器人。\n<code>{html.escape(str(e))}</code>",
                        parse_mode="HTML",
                    )
            # 不是已知用户话题（如 General 话题）则忽略
            return

        # 1c. 不在话题内且处于等待输入关键词状态
        if admin_states.get(ADMIN_ID) == 'WAIT_WORD':
            new_words = update.message.text
            if new_words:
                current_words = database.get_setting("spam_keywords", "")
                word_list = [w.strip() for w in current_words.split(',')] if current_words else []
                for w in new_words.split(','):
                    w = w.strip()
                    if w and w not in word_list:
                        word_list.append(w)
                await asyncio.to_thread(database.set_setting, "spam_keywords", ",".join(word_list))
                admin_states.pop(ADMIN_ID, None)
                await update.message.reply_text("✅ 关键词已更新。")
                text, reply_markup = build_menu_text_and_keyboard()
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
            else:
                await update.message.reply_text("⚠️ 请发送纯文本关键词。")
        return

    # 【情况二】普通用户的私聊消息
    # 1. 黑名单拦截
    if await asyncio.to_thread(database.is_banned, chat_id):
        log.info(f"已拦截黑名单用户 {chat_id} 的消息")
        return

    # 2. 人机验证拦截
    if database.get_setting("captcha_enabled", "1") == "1":
        if not await asyncio.to_thread(database.is_verified, chat_id):
            if chat_id not in pending_captcha:
                await send_captcha(update, context, chat_id)
            else:
                await update.message.reply_text("🔐 请先点击上方按钮完成验证，再发送消息。")
            return

    # 3. 限流拦截
    if is_rate_limited(chat_id):
        log.info(f"用户 {chat_id} 触发限流")
        await update.message.reply_text("⏳ 您发送得太频繁了，请稍后再试。")
        return

    # 4. 广告词过滤
    text_content = update.message.text or update.message.caption or ""
    if is_spam(text_content):
        log.warning(f"已拦截用户 {chat_id} 的广告消息：{text_content[:30]}...")
        return

    # 5. 媒体组聚合
    if update.message.media_group_id:
        group_id = update.message.media_group_id
        if group_id in media_groups:
            media_groups[group_id]["messages"].append(update.message)
        else:
            media_groups[group_id] = {
                "chat_id": chat_id,
                "user": update.effective_user,
                "messages": [update.message],
            }
            context.job_queue.run_once(flush_media_group, MEDIA_GROUP_DELAY, data=group_id)
        return

    # 6. 普通单条消息
    try:
        await forward_to_topic(context, chat_id, update.effective_user, [update.message])
    except Exception as e:
        log.error(f"消息转发失败：{e}")
        await update.message.reply_text("❌ 抱歉，消息转发失败，请稍后再试。")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全局错误兜底，避免异常静默或导致 handler 中断。
    网络类错误（断网/代理抖动/超时）属可自动重试的瞬时故障，仅记一行简短警告，
    不打印完整堆栈以免断网期间刷屏；其余未预期错误仍记录完整堆栈便于排查。"""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"网络异常，正在自动重试：{err}")
        return
    log.error("处理更新时发生异常：", exc_info=err)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """定期清理过期消息映射，防止数据库无限增长"""
    deleted = await asyncio.to_thread(database.cleanup_old_mappings, 7)
    if deleted:
        log.info(f"已清理 {deleted} 条过期消息映射")


async def post_init(application):
    """启动后注册命令菜单，并向话题群组发送启动提醒。"""
    # 普通用户（默认范围）
    await application.bot.set_my_commands(
        [BotCommand("start", "开始使用"), BotCommand("help", "使用帮助")],
        scope=BotCommandScopeDefault(),
    )
    # 话题群组（管理员在此操作）
    await application.bot.set_my_commands(
        [
            BotCommand("menu", "打开控制面板"),
            BotCommand("ban", "拉黑用户（回复消息）"),
            BotCommand("unban", "解除拉黑（回复消息）"),
            BotCommand("cancel", "取消当前操作"),
            BotCommand("help", "使用帮助"),
        ],
        scope=BotCommandScopeChat(chat_id=GROUP_ID),
    )
    me = await application.bot.get_me()
    log.info(f"机器人已上线：@{me.username}（正在监听消息）")

    startup_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    proxy_line = f"\n代理：<code>{html.escape(PROXY_URL)}</code>" if PROXY_URL else ""
    try:
        await application.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"<b>AWRelay 已启动</b>\n\n"
                f"机器人：@{me.username}\n"
                f"时间：{startup_time}{proxy_line}\n\n"
                f"用户私聊消息将转发至对应话题，在话题内直接发送即可回复用户。"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning(f"发送启动提醒失败：{e}")


# 全局持有锁文件句柄，进程存活期间不释放
_lock_handle = None


def acquire_single_instance_lock():
    """获取单实例锁，防止同一 Token 被多个进程同时轮询（会导致消息重复/抢占）。
    获取失败则退出。锁随进程结束自动释放。"""
    global _lock_handle
    lock_path = os.path.join("data", "bot.lock")
    _lock_handle = open(lock_path, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("❌ 检测到已有一个机器人实例在运行（data/bot.lock 被占用）。\n"
              "   请先关闭旧实例，避免同一 Token 被多次轮询导致消息重复。")
        exit(1)


if __name__ == '__main__':
    # 单实例锁：避免重复进程抢占 getUpdates 导致响应重复
    acquire_single_instance_lock()

    # 初始化数据库
    database.init_db()

    # 配置应用
    # 显式设置网络超时：缺少读超时时，代理/网络中途断开会让 get_updates 永久阻塞在死 socket 上，
    # 导致 PTB 的自动重试永远无法触发，网络恢复后也不自愈（表现为必须重启容器）。
    # 设定超时后，卡住的请求会超时抛错，重试机制即可接管并重新建立连接。
    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(20.0)              # 建立连接（含经代理）的超时
        .read_timeout(20.0)                 # 普通请求读取超时
        .write_timeout(20.0)                # 请求写入超时
        .pool_timeout(20.0)                 # 从连接池获取连接的超时
        .get_updates_connect_timeout(20.0)  # 轮询请求的连接超时
        .get_updates_read_timeout(40.0)     # 轮询读超时，须大于长轮询 timeout(默认10s)，留足余量
        .get_updates_pool_timeout(20.0)
    )

    # 如果配置了代理，则设置代理
    if PROXY_URL:
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)
        log.info(f"已启用代理：{PROXY_URL}")

    # 初始化机器人
    application = builder.build()

    # 注册 Handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('ban', ban_user_cmd))
    application.add_handler(CommandHandler('unban', unban_user_cmd))
    application.add_handler(CommandHandler('menu', settings_cmd))
    application.add_handler(CommandHandler('cancel', cancel_cmd))
    application.add_handler(CallbackQueryHandler(captcha_callback, pattern=r"^captcha:"))
    application.add_handler(CallbackQueryHandler(menu_callback))

    # 监听所有类型的消息（文本、图片、视频、文件等），排除命令
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    # 全局错误处理
    application.add_error_handler(error_handler)

    # 每天清理一次过期映射
    application.job_queue.run_repeating(cleanup_job, interval=86400, first=3600)

    # 启动机器人
    log.info("AWRelay 机器人正在启动……")
    application.run_polling()

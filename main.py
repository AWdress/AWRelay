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
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import database

# 加载环境变量
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PROXY_URL = os.getenv("PROXY_URL", "")

if not BOT_TOKEN or ADMIN_ID == 0:
    print("请检查 .env 文件，确保已配置 BOT_TOKEN 和 ADMIN_ID")
    exit(1)

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
for noisy in ("httpx", "httpcore", "apscheduler", "telegram.ext.Application", "telegram.ext.Updater"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

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
    if update.effective_chat.id != ADMIN_ID:
        return
    text, reply_markup = build_menu_text_and_keyboard()
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示帮助信息"""
    if update.effective_chat.id == ADMIN_ID:
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
    if update.effective_chat.id != ADMIN_ID:
        return
    if admin_states.pop(ADMIN_ID, None):
        await update.message.reply_text("✅ 已取消当前操作。")
    else:
        await update.message.reply_text("ℹ️ 当前没有进行中的操作。")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理菜单按钮点击"""
    query = update.callback_query
    if query.message.chat.id != ADMIN_ID:
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
    if chat_id == ADMIN_ID:
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
    if update.effective_chat.id != ADMIN_ID:
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
    if update.effective_chat.id != ADMIN_ID:
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


def build_user_header(user, chat_id):
    """构造用户来源信息（HTML），用于附加在转发内容上"""
    name = html.escape(f"{user.first_name or ''} {user.last_name or ''}".strip() or "未知")
    username = f"@{html.escape(user.username)}" if user.username else "无"
    return (
        f"👤 <b>{name}</b>　{username}\n"
        f"🆔 <code>{chat_id}</code>\n"
        f"━━━━━━━━━━━━━━"
    )


async def forward_to_admin(context, chat_id, user, messages):
    """将用户的一条或一组消息转发给管理员，并保存映射。messages 为 Message 对象列表。
    单条消息会把用户信息合并进同一条；媒体组则把信息作为前置说明。"""
    header = build_user_header(user, chat_id)

    # 单条消息：尽量合并成一条
    if len(messages) == 1:
        msg = messages[0]
        mid = msg.message_id

        # 纯文本：信息 + 内容合并为一条文本消息
        if msg.text is not None and not msg.effective_attachment:
            combined = f"{header}\n{html.escape(msg.text)}"
            sent = await context.bot.send_message(chat_id=ADMIN_ID, text=combined, parse_mode="HTML")
            await asyncio.to_thread(database.save_mapping, sent.message_id, chat_id, mid)
            return

        # 带媒体的消息：把来源信息作为 caption 前缀，保留用户原始说明文字
        orig_caption = msg.caption or ""
        new_caption = header + (f"\n{html.escape(orig_caption)}" if orig_caption else "")
        try:
            sent = await context.bot.copy_message(
                chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=mid,
                caption=new_caption, parse_mode="HTML",
            )
            await asyncio.to_thread(database.save_mapping, sent.message_id, chat_id, mid)
            return
        except Exception:
            # 贴纸/语音/视频笔记等不支持 caption，退回“信息 + 内容”两步
            info_msg = await context.bot.send_message(chat_id=ADMIN_ID, text=header, parse_mode="HTML")
            await asyncio.to_thread(database.save_mapping, info_msg.message_id, chat_id, mid)
            fwd_msg = await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=mid)
            await asyncio.to_thread(database.save_mapping, fwd_msg.message_id, chat_id, mid)
            return

    # 媒体组（多条）：先发一条来源信息，再整组转发
    info_msg = await context.bot.send_message(chat_id=ADMIN_ID, text=header, parse_mode="HTML")
    await asyncio.to_thread(database.save_mapping, info_msg.message_id, chat_id, messages[0].message_id)
    for msg in messages:
        fwd_msg = await context.bot.copy_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=msg.message_id)
        await asyncio.to_thread(database.save_mapping, fwd_msg.message_id, chat_id, msg.message_id)


async def flush_media_group(context: ContextTypes.DEFAULT_TYPE):
    """媒体组聚合定时器回调：等待窗口结束后，一次性转发整组消息。"""
    group_id = context.job.data
    group = media_groups.pop(group_id, None)
    if not group:
        return
    try:
        await forward_to_admin(context, group["chat_id"], group["user"], group["messages"])
    except Exception as e:
        log.error(f"媒体组转发给管理员失败：{e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有文本和多媒体消息"""
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    # 【情况一】管理员的消息
    if chat_id == ADMIN_ID:
        # 优先处理管理员回复消息转发，并自动解除等待状态（防锁死）
        if update.message.reply_to_message:
            admin_states.pop(ADMIN_ID, None)
            admin_msg_id = update.message.reply_to_message.message_id
            mapping = await asyncio.to_thread(database.get_mapping, admin_msg_id)

            if mapping:
                user_chat_id = mapping[0]
                try:
                    # 将管理员的回复"复制"发送给用户，支持各种消息类型（文本、图片等）
                    await context.bot.copy_message(
                        chat_id=user_chat_id,
                        from_chat_id=ADMIN_ID,
                        message_id=msg_id
                    )
                except Exception as e:
                    await update.message.reply_text(f"❌ 发送失败，用户可能已屏蔽机器人。\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")
            else:
                await update.message.reply_text("⚠️ 找不到该消息对应的发送者（可能是旧消息或非转发消息）。")
        # 如果不是回复消息，且处于等待输入关键词状态
        elif admin_states.get(ADMIN_ID) == 'WAIT_WORD':
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

                # 重新展示菜单
                text, reply_markup = build_menu_text_and_keyboard()
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
            else:
                await update.message.reply_text("⚠️ 请发送纯文本关键词。")
        else:
            # 管理员发送的非回复消息，不作处理
            pass
        return

    # 【情况二】普通用户的消息
    # 1. 黑名单拦截
    if await asyncio.to_thread(database.is_banned, chat_id):
        log.info(f"已拦截黑名单用户 {chat_id} 的消息")
        return

    # 2. 人机验证拦截：未验证则发题，不转发
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

    # 4. 广告词过滤拦截
    text_content = update.message.text or update.message.caption or ""
    if is_spam(text_content):
        log.warning(f"已拦截用户 {chat_id} 的广告消息：{text_content[:30]}...")
        return

    # 5. 媒体组（相册）聚合：同一 media_group_id 只发一次用户信息，整组一并转发
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

    # 6. 普通单条消息：直接转发
    try:
        await forward_to_admin(context, chat_id, update.effective_user, [update.message])
    except Exception as e:
        log.error(f"消息转发给管理员失败：{e}")
        await update.message.reply_text("❌ 抱歉，消息转发失败，请稍后再试。")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全局错误兜底，避免异常静默或导致 handler 中断"""
    log.error("处理更新时发生异常：", exc_info=context.error)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """定期清理过期消息映射，防止数据库无限增长"""
    deleted = await asyncio.to_thread(database.cleanup_old_mappings, 7)
    if deleted:
        log.info(f"已清理 {deleted} 条过期消息映射")


async def post_init(application):
    """启动后注册命令菜单：管理员看到完整指令，普通用户只看到基础指令。"""
    # 普通用户（默认范围）
    await application.bot.set_my_commands(
        [
            BotCommand("start", "开始使用"),
            BotCommand("help", "使用帮助"),
        ],
        scope=BotCommandScopeDefault(),
    )
    # 管理员（仅其私聊范围）
    await application.bot.set_my_commands(
        [
            BotCommand("menu", "打开控制面板"),
            BotCommand("ban", "拉黑用户（回复消息）"),
            BotCommand("unban", "解除拉黑（回复消息）"),
            BotCommand("cancel", "取消当前操作"),
            BotCommand("help", "使用帮助"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )
    me = await application.bot.get_me()
    log.info(f"机器人已上线：@{me.username}（正在监听消息）")


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
    builder = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init)

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

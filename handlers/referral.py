"""
handlers/referral.py — هندلرهای سیستم دعوت و referral
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from config import settings
from database import AsyncSessionLocal
from database.crud import get_or_create_user, get_user_by_telegram_id
from services.referral import get_user_referral_stats, process_referral, get_referral_settings
from utils.qrcode_gen import generate_qr_code

router = Router(name="referral")

# نام کاربری ربات — هنگام start در dp.workflow_data ذخیره می‌شود
_BOT_USERNAME: str = ""


async def _get_bot_username(bot) -> str:
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        me = await bot.get_me()
        _BOT_USERNAME = me.username or ""
    return _BOT_USERNAME


def _trigger_fa(trigger: str) -> str:
    """ترجمه trigger به فارسی."""
    return {
        "on_register":       "هنگام ثبت‌نام دوست",
        "on_first_purchase": "پس از اولین خرید دوست",
        "on_every_purchase": "پس از هر خرید دوست",
    }.get(trigger, trigger)


def _reward_text(cfg: dict) -> str:
    """توضیح پاداش به فارسی از تنظیمات."""
    reward_type = cfg.get("reward_type", "custom")
    trigger_fa  = _trigger_fa(cfg.get("trigger", "on_first_purchase"))

    if reward_type == "plan":
        plan_id = cfg.get("reward_plan_id", 0)
        return (
            f"• {trigger_fa}: یک پلن رایگان به شما داده می‌شود\n"
            f"• برای هر نفری که با لینک شما عضو شود"
        )
    else:
        traffic = cfg.get("custom_traffic", 5)
        days    = cfg.get("custom_days", 30)
        traffic_label = f"{traffic:g} GB" if traffic >= 1 else f"{traffic * 1024:.0f} MB"
        return (
            f"• {trigger_fa}: کانفیگ {traffic_label} / {days} روز رایگان\n"
            f"• برای هر نفری که با لینک شما عضو شود"
        )


# ──────────────────────────────────────────────
# /start ref_{code} — پردازش referral
# ──────────────────────────────────────────────

@router.message(CommandStart(deep_link=True, magic=F.args.startswith("ref_")))
async def cmd_start_referral(message: Message) -> None:
    """
    پردازش deep link هنگام ورود کاربر از لینک دعوت.
    فرمت: /start ref_XXXXXXXX
    """
    args = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else ""
    referral_code = args[4:] if args.startswith("ref_") else ""

    tg_user = message.from_user
    if not tg_user or not referral_code:
        return

    bot_username = await _get_bot_username(message.bot)  # type: ignore[union-attr]

    async with AsyncSessionLocal() as session:
        db_user, created = await get_or_create_user(
            session=session,
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            admin_ids=settings.admin_ids,
        )

        if created:
            # کاربر تازه — پردازش referral
            referrer_name = await process_referral(
                session=session,
                new_user=db_user,
                referral_code=referral_code,
                bot_username=bot_username,
                bot=message.bot,      # ← فیکس: bot پاس داده می‌شود
            )
            if referrer_name:
                await message.answer(
                    f"🎉 *خوش آمدید!*\n\n"
                    f"شما از طریق دعوت *{referrer_name}* وارد شدید.\n"
                    "هر دوی شما از مزایای ویژه بهره‌مند می‌شوید!",
                    parse_mode="Markdown",
                )
                # اطلاع‌رسانی به دعوت‌کننده
                await _notify_referrer(message, referral_code, tg_user)
        else:
            await message.answer("👋 خوش آمدید! شما قبلاً ثبت‌نام کرده‌اید.")

    # نمایش منوی اصلی
    from keyboards.main_menu import get_main_menu_async
    await message.answer(
        "از منوی زیر گزینه مورد نظر را انتخاب کنید:",
        reply_markup=await get_main_menu_async(is_admin=db_user.is_admin),
    )


# ──────────────────────────────────────────────
# دکمه «دعوت دوستان»
# ──────────────────────────────────────────────

@router.message(F.text.contains("دعوت دوستان"))
@router.callback_query(F.data == "referral_menu")
async def menu_referral(event: Message | CallbackQuery) -> None:
    """نمایش لینک دعوت + آمار referral کاربر."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        tg_user = event.from_user
        msg_obj = event.message
        bot     = event.bot
    else:
        tg_user = event.from_user
        msg_obj = event
        bot     = event.bot

    if not tg_user:
        return

    bot_username = await _get_bot_username(bot)  # type: ignore[union-attr]

    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, tg_user.id)
        if not db_user:
            await msg_obj.answer("❌ ابتدا /start بزنید.")
            return

        stats = await get_user_referral_stats(session, db_user, bot_username)

    # خواندن تنظیمات پاداش برای نمایش دینامیک
    cfg = await get_referral_settings()
    reward_desc = _reward_text(cfg)
    trigger_fa  = _trigger_fa(cfg.get("trigger", "on_first_purchase"))

    # نمایش آمار پاداش متناسب با نوع پاداش
    if cfg.get("reward_type") == "plan":
        reward_stat_line = f"• پاداش دریافتی:  کانفیگ رایگان"
    else:
        traffic = cfg.get("custom_traffic", 5)
        days    = cfg.get("custom_days", 30)
        traffic_label = f"{traffic:g} GB" if traffic >= 1 else f"{traffic * 1024:.0f} MB"
        total_reward  = stats.rewarded_referrals
        reward_stat_line = f"• پاداش دریافتی:  کانفیگ رایگان ({traffic_label}/{days}روز)"

    text = (
        "👥 *سیستم دعوت دوستان*\n\n"
        f"🔗 *لینک دعوت شما:*\n\n\n"
        f"📊 *آمار شما:*\n"
        f"• کل دعوت‌شده‌ها:  نفر\n"
        f"{reward_stat_line}\n\n"
        f"🎁 *قوانین پاداش:*\n"
        f"{reward_desc}\n\n"
        "لینک را برای دوستان خود ارسال کنید! 🚀"
    )

    # ارسال QR Code لینک دعوت
    try:
        qr_bytes = await generate_qr_code(stats.referral_link)
        qr_file = BufferedInputFile(file=qr_bytes, filename="referral_qr.png")
        await msg_obj.answer_photo(
            photo=qr_file,
            caption=text,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"QR referral ناموفق: {e}")
        # fallback بدون QR
        await msg_obj.answer(text, parse_mode="Markdown")


# ──────────────────────────────────────────────
# اطلاع‌رسانی به دعوت‌کننده
# ──────────────────────────────────────────────

async def _notify_referrer(message: Message, referral_code: str, new_tg_user) -> None:
    """ارسال پیام به کسی که لینک دعوت داده."""
    from database.crud import get_user_by_referral_code
    async with AsyncSessionLocal() as session:
        referrer = await get_user_by_referral_code(session, referral_code)
        if not referrer:
            return

    cfg = await get_referral_settings()
    trigger_fa = _trigger_fa(cfg.get("trigger", "on_first_purchase"))

    name = new_tg_user.first_name or f"@{new_tg_user.username}" or "یک کاربر"

    if cfg.get("reward_type") == "plan":
        reward_hint = f"پس از {trigger_fa}، یک پلن رایگان به حساب شما اضافه می‌شود! 🎁"
    else:
        traffic = cfg.get("custom_traffic", 5)
        days    = cfg.get("custom_days", 30)
        traffic_label = f"{traffic:g} GB" if traffic >= 1 else f"{traffic * 1024:.0f} MB"
        reward_hint = f"{trigger_fa}، کانفیگ {traffic_label}/{days}روز به شما داده می‌شود! 🎁"

    text = (
        f"🎉 *دعوت موفق!*\n\n"
        f"*{name}* با لینک دعوت شما ثبت‌نام کرد.\n"
        f"{reward_hint}"
    )
    try:
        await message.bot.send_message(referrer.telegram_id, text, parse_mode="Markdown")  # type: ignore[union-attr]
    except Exception as e:
        logger.warning(f"ارسال نوتیف به referrer {referrer.telegram_id} ناموفق: {e}")

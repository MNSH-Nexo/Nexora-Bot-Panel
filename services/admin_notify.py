"""
services/admin_notify.py — سرویس مرکزی نوتیفیکیشن ادمین

همه رویدادهای مهم ربات از اینجا به ادمین‌ها ارسال می‌شوند:
  • تیکت جدید از کاربر
  • پاسخ کاربر در تیکت
  • پرداخت کارت به کارت جدید (در صف بررسی)
  • پرداخت کریپتو در صف تأیید

ویژگی‌ها:
  - فرمت یکپارچه و حرفه‌ای با جداکننده و ایموجی رنگی
  - ارسال موازی به تمام ادمین‌ها
  - هیچ‌گاه خطا throw نمی‌کند — شکست‌ها فقط لاگ می‌شوند
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from loguru import logger

from config import settings

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message as TGMessage


# ──────────────────────────────────────────────
# Helper داخلی: ارسال به همه ادمین‌ها
# ──────────────────────────────────────────────

def _get_admin_ids() -> list:
    """
    دریافت لیست admin_ids در لحظه فراخوانی — نه در import time.
    این مهم است چون settings singleton در startup ساخته می‌شود
    و اگه .env بعداً تغییر کند یا parse مشکل داشته باشد،
    با این روش همیشه آخرین مقدار خوانده می‌شود.

    fallback: اگه settings.admin_ids خالی بود، از DB کاربران is_admin=True می‌خواند.
    """
    ids = list(settings.admin_ids)
    if ids:
        return ids
    # fallback: سعی می‌کند از DB بخواند
    try:
        import asyncio
        from database.engine import AsyncSessionLocal as _SessionLocal
        from sqlalchemy import select
        from database.models import User as _User

        async def _fetch():
            async with _SessionLocal() as s:
                result = await s.execute(
                    select(_User.telegram_id).where(_User.is_admin == True)
                )
                return [row[0] for row in result.all()]

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # در context async — نمی‌توانیم run_until_complete بزنیم
            # این fallback فقط در sync context کار می‌کند
            return []
        return loop.run_until_complete(_fetch())
    except Exception as _e:
        logger.warning(f"[admin_notify] fallback DB admin_ids ناموفق: {_e}")
        return []


async def _get_admin_ids_async() -> list:
    """نسخه async دریافت admin_ids — با fallback از DB."""
    ids = list(settings.admin_ids)
    if ids:
        return ids
    # fallback از DB
    try:
        from database.engine import AsyncSessionLocal as _SessionLocal
        from sqlalchemy import select
        from database.models import User as _User
        async with _SessionLocal() as s:
            result = await s.execute(
                select(_User.telegram_id).where(_User.is_admin == True)
            )
            db_ids = [row[0] for row in result.all()]
            if db_ids:
                logger.warning(
                    f"[admin_notify] settings.admin_ids خالی است — "
                    f"از DB fallback استفاده شد: {db_ids}"
                )
            return db_ids
    except Exception as _e:
        logger.error(f"[admin_notify] نوتیف ارسال نشد — admin_ids خالی و DB هم ناموفق: {_e}")
        return []


async def _broadcast_to_admins(
    bot: "Bot",
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
) -> None:
    """ارسال پیام به تمام admin_ids — با fallback از DB اگه settings خالی بود."""
    admin_ids = await _get_admin_ids_async()
    if not admin_ids:
        logger.error(
            "[admin_notify] ⚠️ هیچ ادمینی پیدا نشد (نه در .env نه در DB) — "
            "نوتیفیکیشن ارسال نشد!"
        )
        return
    logger.debug(f"[admin_notify] ارسال به {len(admin_ids)} ادمین: {admin_ids}")
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.warning(f"[admin_notify] ارسال به ادمین {admin_id} ناموفق: {e}")


async def _broadcast_photo_to_admins(
    bot: "Bot",
    photo_file_id: str,
    caption: str,
    reply_markup=None,
) -> None:
    """ارسال عکس به تمام admin_ids — با fallback از DB."""
    admin_ids = await _get_admin_ids_async()
    if not admin_ids:
        logger.error("[admin_notify] ارسال عکس ناموفق — هیچ ادمینی پیدا نشد.")
        return
    for admin_id in admin_ids:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.warning(f"[admin_notify] ارسال عکس به ادمین {admin_id} ناموفق: {e}")


# ──────────────────────────────────────────────
# Helper: اطلاعات کاربر
# ──────────────────────────────────────────────

def _user_line(tg_id: int, username: Optional[str], first_name: Optional[str] = None) -> str:
    """یک خط فشرده اطلاعات کاربر."""
    uname = f"@{username}" if username else "—"
    name = first_name or ""
    return f"👤 {name}  {uname}  <code>{tg_id}</code>"


def _divider() -> str:
    return "━━━━━━━━━━━━━━━"


# ──────────────────────────────────────────────
# رویداد ۱: تیکت جدید از کاربر
# ──────────────────────────────────────────────

async def notify_new_ticket(
    bot: "Bot",
    ticket_id: int,
    tg_id: int,
    username: Optional[str],
    first_name: Optional[str],
    subject: str,
    body: str,
    sub_info: str = "فاقد اشتراک فعال",
) -> None:
    """نوتیف تیکت جدید به ادمین‌ها — فرمت کامل با اطلاعات کاربر."""
    from keyboards.tickets import get_admin_ticket_keyboard
    text = (
        "🎫 <b>تیکت جدید دریافت شد</b>\n"
        f"{_divider()}\n"
        f"{_user_line(tg_id, username, first_name)}\n"
        f"📦 اشتراک: {sub_info}\n"
        f"{_divider()}\n"
        f"📌 موضوع: <b>{subject}</b>\n\n"
        f"💬 پیام:\n{body[:500]}"
        + (" …" if len(body) > 500 else "")
        + f"\n\n🔖 شناسه تیکت: <code>#{ticket_id}</code>"
    )
    await _broadcast_to_admins(bot, text, reply_markup=get_admin_ticket_keyboard(ticket_id))
    logger.info(f"[admin_notify] تیکت جدید #{ticket_id} به ادمین‌ها ارسال شد.")


# ──────────────────────────────────────────────
# رویداد ۲: پاسخ کاربر در تیکت
# ──────────────────────────────────────────────

async def notify_ticket_reply(
    bot: "Bot",
    ticket_id: int,
    tg_id: int,
    username: Optional[str],
    first_name: Optional[str],
    reply_text: str,
) -> None:
    """نوتیف پاسخ کاربر در تیکت به ادمین‌ها."""
    from keyboards.tickets import get_admin_ticket_keyboard
    preview = reply_text[:400] + (" …" if len(reply_text) > 400 else "")
    text = (
        "💬 <b>پاسخ جدید در تیکت</b>\n"
        f"{_divider()}\n"
        f"🔖 تیکت: <code>#{ticket_id}</code>\n"
        f"{_user_line(tg_id, username, first_name)}\n"
        f"{_divider()}\n"
        f"📝 پیام:\n{preview}"
    )
    await _broadcast_to_admins(bot, text, reply_markup=get_admin_ticket_keyboard(ticket_id))
    logger.info(f"[admin_notify] پاسخ کاربر در تیکت #{ticket_id} به ادمین‌ها ارسال شد.")


# ──────────────────────────────────────────────
# رویداد ۳: پرداخت کارت به کارت جدید
# ──────────────────────────────────────────────

async def notify_card_payment(
    bot: "Bot",
    order_id: str,
    plan_name: str,
    amount_toman: int,
    tg_id: int,
    username: Optional[str],
    receipt_msg: "TGMessage",
    original_price_usdt: float = 0.0,
    final_price_usdt: float = 0.0,
    discount_code: Optional[str] = None,
    discount_percent: int = 0,
) -> None:
    """نوتیف پرداخت کارت به کارت جدید به ادمین‌ها — با دکمه تأیید/رد."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید و فعال‌سازی", callback_data=f"card_approve:{order_id}")
    kb.button(text="❌ رد پرداخت",          callback_data=f"card_reject:{order_id}")
    kb.adjust(1)
    markup = kb.as_markup()

    if discount_code and discount_percent > 0 and original_price_usdt > 0:
        price_block = (
            f"💵 قیمت اصلی: <s>{original_price_usdt:.2f} USDT</s>\n"
            f"🎟 کد تخفیف: <code>{discount_code}</code>  ({discount_percent}٪)\n"
            f"💰 مبلغ پرداختی: <b>{amount_toman:,} تومان</b>  "
            f"(<b>{final_price_usdt:.2f} USDT</b>)"
        )
    else:
        price_block = f"💰 مبلغ: <b>{amount_toman:,} تومان</b>"
        if final_price_usdt:
            price_block += f"  (<b>{final_price_usdt:.2f} USDT</b>)"

    caption = (
        "💳 <b>پرداخت کارت به کارت — نیاز به بررسی</b>\n"
        f"{_divider()}\n"
        f"📦 پلن: <b>{plan_name}</b>\n"
        f"{price_block}\n"
        f"🔖 سفارش: <code>{order_id}</code>\n"
        f"{_user_line(tg_id, username)}"
    )

    if receipt_msg.photo:
        user_cap = receipt_msg.caption or ""
        full_cap = caption
        if user_cap:
            full_cap += f"\n\n📝 <b>متن رسید:</b>\n<code>{user_cap}</code>"
        await _broadcast_photo_to_admins(
            bot,
            photo_file_id=receipt_msg.photo[-1].file_id,
            caption=full_cap,
            reply_markup=markup,
        )
    else:
        text_receipt = receipt_msg.text or receipt_msg.caption or "—"
        await _broadcast_to_admins(
            bot,
            text=caption + f"\n\n📝 <b>متن رسید:</b>\n<code>{text_receipt}</code>",
            reply_markup=markup,
        )
    logger.info(f"[admin_notify] پرداخت کارت {order_id} به ادمین‌ها ارسال شد.")


# ──────────────────────────────────────────────
# رویداد ۴: پرداخت کریپتو در انتظار
# ──────────────────────────────────────────────

async def notify_crypto_payment_pending(
    bot: "Bot",
    order_id: str,
    plan_name: str,
    amount_usdt: float,
    pay_currency: str,
    tg_id: int,
    username: Optional[str],
    gateway: str = "crypto",
) -> None:
    """نوتیف پرداخت کریپتو جدید که در انتظار تأیید شبکه است."""
    gw_label = {"maxelpay": "MaxelPay 💜", "nowpayments": "NOWPayments 🔵"}.get(
        gateway.lower(), gateway
    )
    text = (
        "🪙 <b>پرداخت کریپتو در صف تأیید</b>\n"
        f"{_divider()}\n"
        f"📦 پلن: <b>{plan_name}</b>\n"
        f"💵 مبلغ: <b>{amount_usdt:.2f} USDT</b>\n"
        f"💱 ارز: <code>{pay_currency.upper()}</code>\n"
        f"🏦 درگاه: {gw_label}\n"
        f"🔖 سفارش: <code>{order_id}</code>\n"
        f"{_user_line(tg_id, username)}\n\n"
        "<i>تأیید خودکار از طریق webhook انجام می‌شود.</i>"
    )
    await _broadcast_to_admins(bot, text)
    logger.info(f"[admin_notify] پرداخت کریپتو {order_id} به ادمین‌ها ارسال شد.")

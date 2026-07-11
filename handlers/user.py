"""
handlers/user.py — هندلرهای اصلی ربات برای کاربران
"""

from __future__ import annotations

import io
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)
from loguru import logger

from config import settings
from database import AsyncSessionLocal, get_or_create_user, get_user_by_telegram_id
from keyboards.main_menu import get_main_menu, get_main_menu_async
from services.banner import send_with_banner
from services.welcome import (
    check_user_joined,
    send_join_required_message,
    send_welcome_banner,
    get_welcome_banner_file_id,
)
from keyboards.plans import (
    get_confirm_purchase_keyboard,
    get_plan_detail_keyboard,
    get_plans_keyboard,
    get_subscription_detail_keyboard,
)
from services.subscription import create_new_subscription, get_subscriptions_status
from services.xui_api import XUIClient, XUIError

router = Router(name="user")


# ──────────────────────────────────────────────
# Helper: safe edit — اگه پیام عکس داشت از answer استفاده کن
# ──────────────────────────────────────────────

async def _safe_edit(callback: CallbackQuery, text: str, **kwargs) -> None:
    """edit_text امن — اگه پیام عکس‌دار بود، answer جدید می‌فرسته."""
    try:
        if callback.message.photo or callback.message.document:  # type: ignore
            await callback.message.answer(text, **kwargs)  # type: ignore
        else:
            await callback.message.edit_text(text, **kwargs)  # type: ignore
    except Exception:
        await callback.message.answer(text, **kwargs)  # type: ignore


# ──────────────────────────────────────────────
# Helper: تبدیل بایت به واحد خوانا
# ──────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    """تبدیل بایت به GB/MB خوانا."""
    if b == 0:
        return "نامحدود"
    gb = b / 1024 ** 3
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = b / 1024 ** 2
    return f"{mb:.1f} MB"


def _fmt_ts(ts: int) -> str:
    """تبدیل timestamp میلی‌ثانیه به تاریخ فارسی‌پسند."""
    if ts == 0:
        return "نامحدود"
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _fmt_expiry(expiry_date) -> str:
    """نمایش تاریخ انقضا + روزهای باقی‌مانده.

    مثال خروجی:
      2026-07-14  (۱۱ روز مانده)
      2026-07-01  (منقضی شده)
      نامحدود
    """
    from datetime import datetime, timezone
    if not expiry_date:
        return "نامحدود"
    if expiry_date.tzinfo is None:
        expiry_date = expiry_date.replace(tzinfo=timezone.utc)
    date_str = expiry_date.strftime("%Y-%m-%d")
    delta = (expiry_date - datetime.now(timezone.utc)).days
    if delta < 0:
        return f"{date_str}  (منقضی شده)"
    elif delta == 0:
        return f"{date_str}  (امروز منقضی می‌شود)"
    else:
        return f"{date_str}  ({delta} روز مانده)"


# ──────────────────────────────────────────────
# /start
# ──────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    /start — flow:
      1. اگه welcome banner تنظیم شده → نشون بده (اول بار)
      2. چک عضویت کانال
      3. نمایش منوی اصلی
    """
    user = message.from_user
    if not user:
        return

    # ── ۱. نمایش welcome banner اگه تنظیم شده ──────────────
    # فقط بار اولی که کاربر /start میزند (یا اگه هنوز ثبت نشده)
    has_welcome = await get_welcome_banner_file_id()
    async with AsyncSessionLocal() as session:
        db_user, created = await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            admin_ids=settings.admin_ids,
        )

    if created and has_welcome:
        # کاربر جدید + welcome banner → نشون بده و منتظر کلیک "شروع کن" بمون
        await send_welcome_banner(message)
        return

    # ── ۲. چک عضویت کانال ─────────────────────────────────
    if not await check_user_joined(message.bot, user.id):  # type: ignore[arg-type]
        await send_join_required_message(message)
        return

    # ── ۳. منوی اصلی ──────────────────────────────────────
    await _show_main_menu(message, db_user)


async def _show_main_menu(target: Message, db_user) -> None:
    """نمایش منوی اصلی به کاربر."""
    from services.theme import get_current_theme as _get_t
    _th = await _get_t()
    user = target.from_user
    name = (user.first_name if user else None) or (user.username if user else None) or "کاربر"
    text = (
        f"{_th.header_icon} سلام <b>{name}</b>!\n"
        f"{_th.sep}\n"
        f"🔐 <b>ربات فروش اشتراک VPN</b>\n\n"
        f"{_th.bullet} اشتراک VPN بخرید\n"
        f"{_th.bullet} وضعیت اشتراک خود را ببینید\n"
        f"{_th.bullet} با پشتیبانی در تماس باشید\n\n"
        "👇 از منوی زیر انتخاب کنید:"
    )
    try:
        await send_with_banner(
            target,
            text,
            parse_mode="HTML",
            reply_markup=await get_main_menu_async(is_admin=db_user.is_admin),
        )
    except Exception:
        await target.answer(
            text,
            parse_mode="HTML",
            reply_markup=await get_main_menu_async(is_admin=db_user.is_admin),
        )


@router.callback_query(F.data == "welcome_start")
async def cb_welcome_start(callback: CallbackQuery) -> None:
    """کاربر دکمه «شروع کن» را در welcome banner زد."""
    await callback.answer()
    user = callback.from_user
    if not user:
        return

    # چک عضویت کانال
    if not await check_user_joined(callback.bot, user.id):  # type: ignore[arg-type]
        await send_join_required_message(callback.message)  # type: ignore[arg-type]
        return

    async with AsyncSessionLocal() as session:
        db_user, _ = await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            admin_ids=settings.admin_ids,
        )
    await _show_main_menu(callback.message, db_user)  # type: ignore[arg-type]


@router.callback_query(F.data == "check_join")
async def cb_check_join(callback: CallbackQuery) -> None:
    """کاربر ادعا می‌کند عضو شده — دوباره چک کن."""
    await callback.answer("🔄 در حال بررسی عضویت...")
    user = callback.from_user
    if not user:
        return

    if not await check_user_joined(callback.bot, user.id):  # type: ignore[arg-type]
        await callback.answer("⛔ هنوز عضو نشدید! لطفاً ابتدا عضو کانال شوید.", show_alert=True)
        return

    # عضو شد → منوی اصلی
    async with AsyncSessionLocal() as session:
        db_user, _ = await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            admin_ids=settings.admin_ids,
        )
    await _show_main_menu(callback.message, db_user)  # type: ignore[arg-type]


# ──────────────────────────────────────────────
# منوی خرید کانفیگ (دکمه Reply)
# ──────────────────────────────────────────────

@router.message(F.text.contains("خرید کانفیگ"))
async def menu_buy_config(message: Message) -> None:
    """دریافت لیست پلن‌ها از پنل و نمایش به کاربر."""
    await message.answer("⏳ در حال دریافت پلن‌ها از سرور...")

    try:
        async with XUIClient(
            panel_url=settings.panel_url,
            username=settings.panel_username,
            password=settings.panel_password,
            api_path=settings.panel_api_path,
            sub_port=settings.sub_port,
        ) as xui:
            inbounds = await xui.get_inbounds()

        if not inbounds:
            await message.answer("❌ در حال حاضر هیچ پلنی در دسترس نیست.")
            return

        text = "📦 *پلن‌های موجود:*\n\nیک پلن برای مشاهده جزئیات انتخاب کنید:"
        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=get_plans_keyboard(inbounds),
        )

    except XUIError as e:
        logger.error(f"خطا در دریافت پلن‌ها: {e}")
        await message.answer("⚠️ خطا در اتصال به سرور. لطفاً بعداً تلاش کنید.")


# ──────────────────────────────────────────────
# Callback: show_plans (از دکمه بازگشت)
# ──────────────────────────────────────────────

@router.callback_query(F.data == "show_plans")
async def cb_show_plans(callback: CallbackQuery) -> None:
    """نمایش مجدد لیست پلن‌ها."""
    await callback.answer()
    try:
        async with XUIClient(
            panel_url=settings.panel_url,
            username=settings.panel_username,
            password=settings.panel_password,
            api_path=settings.panel_api_path,
            sub_port=settings.sub_port,
        ) as xui:
            inbounds = await xui.get_inbounds()

        await _safe_edit(
            callback,
            "📦 *پلن‌های موجود:*\n\nیک پلن برای مشاهده جزئیات انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_plans_keyboard(inbounds),
        )
    except XUIError as e:
        logger.error(f"خطا: {e}")
        await callback.message.answer("⚠️ خطا در اتصال به سرور.")  # type: ignore[union-attr]


# ──────────────────────────────────────────────
# Callback: plan:{inbound_id} — جزئیات پلن
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("plan:"))
async def cb_plan_detail(callback: CallbackQuery) -> None:
    """نمایش جزئیات یک پلن انتخابی."""
    await callback.answer()
    inbound_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]

    try:
        async with XUIClient(
            panel_url=settings.panel_url,
            username=settings.panel_username,
            password=settings.panel_password,
            api_path=settings.panel_api_path,
            sub_port=settings.sub_port,
        ) as xui:
            inbound = await xui.get_inbound(inbound_id)

        traffic_text = _fmt_bytes(inbound.total)
        expire_text = _fmt_ts(inbound.expiry_time)

        text = (
            f"📋 *جزئیات پلن:*\n\n"
            f"🏷 نام: `{inbound.remark}`\n"
            f"🔌 پروتکل: `{inbound.protocol.upper()}`\n"
            f"🌐 پورت: `{inbound.port}`\n"
            f"📦 ترافیک: `{traffic_text}`\n"
            f"⏳ انقضا: `{expire_text}`\n\n"
            f"برای خرید این پلن روی دکمه زیر کلیک کنید:"
        )

        await _safe_edit(
            callback,
            text,
            parse_mode="Markdown",
            reply_markup=get_plan_detail_keyboard(inbound_id),
        )

    except XUIError as e:
        logger.error(f"خطا در دریافت جزئیات پلن: {e}")
        await callback.answer("⚠️ خطا در دریافت اطلاعات پلن.", show_alert=True)


# ──────────────────────────────────────────────
# Callback: buy:{inbound_id} — تأیید خرید
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_plan(callback: CallbackQuery) -> None:
    """نمایش صفحه تأیید پرداخت."""
    await callback.answer()
    inbound_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]

    from config import settings as _s
    price = _s.plan_price_usdt
    traffic_text = "نامحدود" if _s.default_traffic_gb == 0 else f"{_s.default_traffic_gb} GB"

    text = (
        "💳 *تأیید خرید*\n\n"
        f"پلن انتخابی: `{inbound_id}`\n"
        f"مدت اعتبار: `{_s.default_subscription_days} روز`\n"
        f"ترافیک: `{traffic_text}`\n"
        f"💰 مبلغ قابل پرداخت: `{price:.2f} USDT (TRC-20)`\n\n"
        "برای ادامه روی دکمه پرداخت کلیک کنید."
    )

    await _safe_edit(
        callback,
        text,
        parse_mode="Markdown",
        reply_markup=get_confirm_purchase_keyboard(inbound_id),
    )


# ──────────────────────────────────────────────
# Callback: confirm_buy (deprecated) — redirect به pay
# ──────────────────────────────────────────────
# این callback دیگر از کیبورد فراخوانی نمی‌شود.
# اما به عنوان fallback نگه داشته شده.

@router.callback_query(F.data.startswith("confirm_buy:"))
async def cb_confirm_buy_legacy(callback: CallbackQuery) -> None:
    """Redirect قدیمی — به pay handler هدایت می‌شود."""
    await callback.answer()
    inbound_id = callback.data.split(":")[1]  # type: ignore[union-attr]
    # بازنویسی callback_data و اجرای pay handler
    callback.data = f"pay:{inbound_id}"  # type: ignore[union-attr]
    from handlers.payments import cb_pay_plan
    await cb_pay_plan(callback)


# ──────────────────────────────────────────────
# منوی اشتراک‌های من — نمایش لیست
# ──────────────────────────────────────────────

@router.message(F.text.contains("اشتراک‌های من"))
@router.callback_query(F.data == "my_subs")
async def menu_my_subscriptions(event: Message | CallbackQuery) -> None:
    """
    نمایش لیست اشتراک‌ها با دکمه برای هر کدام.
    کاربر روی هر اشتراک کلیک می‌کند تا جزئیات + QR آن را ببیند.
    """
    if isinstance(event, CallbackQuery):
        await event.answer()
        tg_user = event.from_user
        target_msg = event.message  # type: ignore[union-attr]
    else:
        tg_user = event.from_user
        target_msg = event

    if not tg_user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, tg_user.id)
        if not db_user:
            await target_msg.answer("❌ حساب شما پیدا نشد. لطفاً /start بزنید.")
            return
        subs = await get_subscriptions_status(session, db_user.id)

    if not subs:
        await target_msg.answer(
            "📭 شما هنوز هیچ اشتراک فعالی ندارید.\n\n"
            "از منوی <b>🛒 خرید کانفیگ</b> اولین اشتراک خود را بخرید!",
            parse_mode="HTML",
        )
        return

    _STATUS_FA = {
        "active": "✅ فعال", "expired": "⏰ منقضی",
        "depleted": "📭 تمام‌شده", "disabled": "🚫 غیرفعال",
        "pending": "⏳ در انتظار",
    }

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    # نمایش لیست اشتراک‌ها با دکمه برای هر کدام
    from services.theme import get_current_theme as _get_theme
    _th = await _get_theme()
    text = f"{_th.header_icon} <b>اشتراک‌های شما</b>\n{_th.sep}\nبرای مشاهده جزئیات، روی هر اشتراک کلیک کنید:\n"
    b = InlineKeyboardBuilder()
    for i, sub in enumerate(subs, 1):
        status_fa = _STATUS_FA.get(sub.status, sub.status)
        b.button(
            text=f"{_th.star}  اشتراک {i} — {sub.email}  {status_fa}",
            callback_data=f"sub_detail:{sub.id}",
        )
    b.adjust(1)
    await target_msg.answer(text, parse_mode="HTML", reply_markup=b.as_markup())


# ──────────────────────────────────────────────
# Callback: sub_detail:{sub_id} — نمایش جزئیات یک اشتراک
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("sub_detail:"))
async def cb_sub_detail(callback: CallbackQuery) -> None:
    """
    نمایش جزئیات + QR اشتراک با ترافیک بروز از پنل.
    """
    await callback.answer("⏳ در حال بروزرسانی...")
    sub_id = int(callback.data.split(":")[1])

    from database.models import Subscription as _Sub
    from sqlalchemy import select as sa_select
    from database.crud import update_subscription_traffic

    async with AsyncSessionLocal() as session:
        res = await session.execute(sa_select(_Sub).where(_Sub.id == sub_id))
        sub = res.scalar_one_or_none()

    if not sub:
        await callback.message.answer("❌ اشتراک پیدا نشد!")  # type: ignore[union-attr]
        return

    # ── بروزرسانی ترافیک از پنل ───────────────────────────
    try:
        async with XUIClient(
            panel_url=settings.panel_url,
            username=settings.panel_username,
            password=settings.panel_password,
            api_path=settings.panel_api_path,
            sub_port=settings.sub_port,
        ) as xui:
            traffic_info = await xui.get_client_traffic(sub.email)
            if traffic_info:
                # traffic_info یک ClientInfo object است — up و down به بایت
                used_bytes = (traffic_info.up or 0) + (traffic_info.down or 0)
                async with AsyncSessionLocal() as sess2:
                    await update_subscription_traffic(sess2, sub_id, used_bytes)
                # مقدار بروز را روی object اعمال کن
                sub.used_traffic_bytes = used_bytes
    except Exception as e:
        logger.warning(f"بروزرسانی ترافیک اشتراک {sub.email} ناموفق: {e}")

    _STATUS_FA = {
        "active": "✅ فعال", "expired": "⏰ منقضی",
        "depleted": "📭 تمام‌شده", "disabled": "🚫 غیرفعال",
        "pending": "⏳ در انتظار",
    }

    from config import settings as _s
    from services.xui_api import build_sub_link_for
    from utils.qrcode_gen import generate_qr_code
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    used_gb   = sub.used_traffic_bytes / 1024 ** 3
    limit_gb  = sub.traffic_limit_gb or 0
    expire    = _fmt_expiry(sub.expiry_date)
    status_fa = _STATUS_FA.get(sub.status, sub.status)
    sub_link  = build_sub_link_for(_s.panel_url, sub.sub_id, _s.sub_port)

    # ── ترافیک: نوار پیشرفت + درصد ─────────────────────────
    if limit_gb and limit_gb > 0:
        pct = min(used_gb / limit_gb * 100, 100)
        filled  = int(pct / 10)
        bar     = "█" * filled + "░" * (10 - filled)
        limit_label   = f"{limit_gb} GB"
        traffic_line  = (
            f"📊 ترافیک: <code>{used_gb:.2f}</code> / <code>{limit_label}</code>\n"
            f"   [{bar}] <code>{pct:.0f}%</code> مصرف شده\n"
        )
    else:
        traffic_line = f"📊 ترافیک: <code>{used_gb:.2f} GB</code> مصرف — <b>نامحدود</b>\n"

    # ── محدودیت دستگاه همزمان ────────────────────────────────
    ip_limit_val = getattr(sub, "limit_ip", 0) or 0
    if ip_limit_val and ip_limit_val > 0:
        ip_line = f"📱 دستگاه مجاز: <b>{ip_limit_val} دستگاه همزمان</b>\n"
    else:
        ip_line = f"📱 دستگاه مجاز: <b>نامحدود</b>\n"

    # ── وضعیت انقضا با هشدار ─────────────────────────────────
    expire_warn = ""
    if sub.expiry_date:
        from datetime import datetime as _dt, timezone as _tz
        expiry_aware = sub.expiry_date
        if expiry_aware.tzinfo is None:
            expiry_aware = expiry_aware.replace(tzinfo=_tz.utc)
        remaining = expiry_aware - _dt.now(_tz.utc)
        days_left = remaining.days
        if days_left <= 3 and sub.status == "active":
            expire_warn = f"   ⚠️ <b>{days_left} روز تا انقضا!</b>\n"
        elif days_left <= 7 and sub.status == "active":
            expire_warn = f"   ⏰ {days_left} روز باقی مانده\n"

    # ── کپشن کامل ────────────────────────────────────────────
    caption = (
        f"📦 <b>{sub.email}</b>  {status_fa}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📧 شناسه: <code>{sub.email}</code>\n"
        f"{traffic_line}"
        f"⏳ انقضا: <code>{expire}</code>\n"
        f"{expire_warn}"
        f"{ip_line}"
        f"\n🔗 <b>لینک اشتراک:</b>\n"
        f"<code>{sub_link}</code>"
    )

    # دکمه‌های جزئیات
    b = InlineKeyboardBuilder()
    from services.theme import get_theme_sync_default as _gts
    _th2 = _gts()
    b.button(text=f"{_th2.star}  دریافت کانفیگ‌های مستقل", callback_data=f"get_configs:{sub.id}")
    b.button(text=f"{_th2.star2}  بازگشت به لیست", callback_data="my_subs")
    b.adjust(1)
    kb = b.as_markup()

    # ارسال QR + کپشن در یک پیام
    try:
        qr_bytes = await generate_qr_code(sub_link)
        if qr_bytes:
            await callback.message.answer_photo(  # type: ignore[union-attr]
                BufferedInputFile(qr_bytes, filename=f"sub_qr.png"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
    except Exception as e:
        logger.warning(f"خطا در QR اشتراک {sub.email}: {e}")

    # fallback بدون QR
    await callback.message.answer(caption, parse_mode="HTML", reply_markup=kb)  # type: ignore[union-attr]


async def _fetch_sub_links_direct(sub_id_str: str) -> list[str]:
    """
    گرفتن لینک‌های کانفیگ مستقل مستقیماً از /sub/{sub_id} — بدون احراز هویت.
    این endpoint در 3X-UI عمومی است و همان چیزی است که کاربر در اپ VPN وارد می‌کند.
    پاسخ: base64-encoded newline-separated (یا plain text در برخی نسخه‌ها).
    """
    import base64 as _b64
    import httpx as _httpx
    from urllib.parse import urlparse as _urlparse

    _parsed = _urlparse(settings.panel_url)
    _scheme = _parsed.scheme or "https"
    _host = _parsed.hostname or ""
    _port = settings.sub_port if settings.sub_port and settings.sub_port > 0 else (
        _parsed.port or (443 if _scheme == "https" else 80)
    )
    sub_url = f"{_scheme}://{_host}:{_port}/sub/{sub_id_str}"

    async with _httpx.AsyncClient(timeout=15.0, verify=False, follow_redirects=True) as cli:
        resp = await cli.get(sub_url)
        if resp.status_code != 200 or not resp.content:
            logger.warning(f"sub URL {sub_url} → HTTP {resp.status_code}")
            return []
        raw = resp.text.strip()

    # تلاش base64 decode
    try:
        # padding اصلاح
        pad = (-len(raw)) % 4
        decoded = _b64.b64decode(raw + ("=" * pad)).decode("utf-8", errors="ignore").strip()
        links = [ln.strip() for ln in decoded.splitlines() if ln.strip() and "://" in ln]
        if links:
            return links
    except Exception:
        pass

    # plain text (newline-separated)
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and "://" in ln]


@router.callback_query(F.data.startswith("get_configs:"))
async def cb_get_configs(callback: CallbackQuery) -> None:
    """دریافت تمام کانفیگ‌های مستقل یک اشتراک — همه در یک پیام."""
    # فقط toast خالی — تا بتونیم بعداً پیام کامل بفرستیم
    await callback.answer("⏳ در حال دریافت کانفیگ‌ها...")
    sub_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        from database.models import Subscription
        from sqlalchemy import select as sa_select
        res = await session.execute(sa_select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()

    if not sub:
        await callback.message.answer("❌ اشتراک پیدا نشد!")  # type: ignore[union-attr]
        return

    if not sub.sub_id:
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ این اشتراک <b>sub_id</b> ذخیره‌شده ندارد.\n"
            "لطفاً از پشتیبانی کمک بگیرید یا UUID خود را دوباره import کنید.",
            parse_mode="HTML",
        )
        return

    links: list[str] = []

    # ── تلاش ۱: fetch مستقیم لینک ساب (بدون login) ─────────
    # این روش همیشه کار می‌کند چون /sub/{sub_id} public است و
    # حتی اگه login به پنل fail بشه، این fetch مستقل کار می‌کند
    try:
        links = await _fetch_sub_links_direct(sub.sub_id)
        if links:
            logger.info(f"cb_get_configs: {len(links)} لینک با fetch مستقیم برای {sub.email}")
    except Exception as e:
        logger.warning(f"fetch مستقیم لینک ساب برای {sub.email} شکست خورد: {e}")

    # ── تلاش ۲: fallback به XUIClient authenticated ─────────
    if not links:
        try:
            from database.crud import get_enabled_inbound_ids
            async with AsyncSessionLocal() as sess2:
                enabled_ids = await get_enabled_inbound_ids(sess2)

            async with XUIClient(
                panel_url=settings.panel_url,
                username=settings.panel_username,
                password=settings.panel_password,
                api_path=settings.panel_api_path,
                sub_port=settings.sub_port,
            ) as xui:
                links = await xui.get_sub_links(sub.sub_id)
                if not links:
                    links = await xui.get_client_links(sub.email)

                # فیلتر بر اساس اینباندهای فعال (اگه نیاز بود)
                if enabled_ids and links:
                    try:
                        inbounds = await xui.get_inbounds()
                        enabled_ports = {ib.port for ib in inbounds if ib.id in enabled_ids}
                        filtered = [
                            lnk for lnk in links
                            if any(f":{p}" in lnk for p in enabled_ports)
                        ]
                        if filtered:
                            links = filtered
                    except Exception:
                        pass  # فیلتر اینباند optional
        except Exception as e:
            logger.exception(f"fallback XUIClient برای {sub.email} شکست خورد: {e}")

    # ── جایگزینی 127.0.0.1 با IP/domain واقعی سرور ─────────
    if links:
        from urllib.parse import urlparse as _urlparse
        _parsed_panel = _urlparse(settings.panel_url)
        _server_host = _parsed_panel.hostname or ""
        if _server_host and _server_host != "127.0.0.1":
            links = [lnk.replace("127.0.0.1", _server_host) for lnk in links]

    if not links:
        await callback.message.answer(  # type: ignore[union-attr]
            "⚠️ کانفیگی یافت نشد.\n"
            "احتمالاً یکی از موارد زیر:\n"
            "• اشتراک در 3X-UI حذف شده\n"
            "• پنل موقتاً در دسترس نیست\n"
            "• sub port اشتباه تنظیم شده\n\n"
            "لطفاً از پشتیبانی کمک بگیرید.",
        )
        return

    try:

        # ── ساخت پیام: همه کانفیگ‌ها با HTML escape امن ────────────
        # کاراکترهای & < > > در URL‌ها باید escape بشن تا تلگرام آن‌ها را
        # به‌عنوان HTML entity تفسیر نکند (خطای «can't parse entities»)
        from html import escape as _hesc

        header = (
            f"📋 <b>کانفیگ‌های مستقل</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 شناسه: <code>{_hesc(sub.email)}</code>\n"
            f"🔢 تعداد سرور: <b>{len(links)}</b>\n\n"
            f"⬇️ هر لینک را جداگانه کپی کنید:\n"
        )

        # ── چانک‌بندی: هر پیام حداکثر ~3800 کاراکتر ────────────
        # لینک vless معمولاً 400-700 کاراکتر است، پس هر پیام ~5-8 لینک
        MAX_LEN = 3800
        chunks: list[str] = []
        current = header
        for j, link in enumerate(links, 1):
            proto = link.split("://")[0].upper() if "://" in link else "CONFIG"
            block = f"<b>{j}. {proto}</b>\n<code>{_hesc(link)}</code>\n\n"
            if len(current) + len(block) > MAX_LEN and current != header:
                chunks.append(current.rstrip())
                current = block
            else:
                current += block
        if current.strip():
            chunks.append(current.rstrip())

        # ارسال چانک‌ها یکی‌یکی
        for chunk in chunks:
            await callback.message.answer(chunk, parse_mode="HTML")  # type: ignore[union-attr]

        # پیام «کپی همه» — با لینک ساب برای کاربرانی که می‌خواهند یک‌جا import کنند
        from urllib.parse import urlparse as _urlparse_link
        _pp = _urlparse_link(settings.panel_url)
        _sub_scheme = _pp.scheme or "https"
        _sub_host = _pp.hostname or ""
        _sub_port_val = settings.sub_port if settings.sub_port and settings.sub_port > 0 else (
            _pp.port or (443 if _sub_scheme == "https" else 80)
        )
        _sub_full = f"{_sub_scheme}://{_sub_host}:{_sub_port_val}/sub/{sub.sub_id}"
        await callback.message.answer(  # type: ignore[union-attr]
            f"📌 <b>لینک اشتراک (import یک‌جا):</b>\n"
            f"<code>{_hesc(_sub_full)}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception(f"خطا در ارسال کانفیگ‌های {sub.email}: {e}")
        await callback.message.answer(  # type: ignore[union-attr]
            "❌ خطا در ارسال کانفیگ‌ها. لطفاً دوباره امتحان کنید.",
        )


# ──────────────────────────────────────────────
# منوی پروفایل
# ──────────────────────────────────────────────

@router.message(F.text.contains("پروفایل"))
async def menu_profile(message: Message) -> None:
    """نمایش اطلاعات پروفایل کاربر."""
    tg_user = message.from_user
    if not tg_user:
        return

    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, tg_user.id)
        if not db_user:
            await message.answer("❌ حساب شما یافت نشد. لطفاً /start بزنید.")
            return

        subs = await get_subscriptions_status(session, db_user.id)

    # نام و یوزرنیم را escape می‌کنیم تا کاراکترهای خاص HTML مشکل ایجاد نکنند
    from html import escape
    first_name = escape(tg_user.first_name or "-")
    username   = escape(tg_user.username or "-")

    text = (
        f"👤 <b>پروفایل شما</b>\n\n"
        f"🆔 آی‌دی: <code>{tg_user.id}</code>\n"
        f"👋 نام: {first_name}\n"
        f"📝 نام کاربری: @{username}\n"
        f"📦 اشتراک‌های فعال: {len(subs)}\n"
        f"📅 تاریخ ثبت‌نام: {db_user.created_at.strftime('%Y-%m-%d')}"
    )

    await message.answer(text, parse_mode="HTML")


# پشتیبانی اکنون توسط handlers/tickets.py مدیریت می‌شود
# (F.text == "❓ پشتیبانی" → menu_support در ticket_router)


# ──────────────────────────────────────────────
# دستور ورود ادمین — دینامیک (آخرین handler)
# باید در user_router باشد تا /start و سایر دستورات
# مشابه در router های قبلی اول پردازش شوند
# ──────────────────────────────────────────────

@router.message(F.text.regexp(r"^/\S+(\s.*)?$"))
async def catch_admin_command(message: Message) -> None:
    """
    آخرین handler — فقط دستور ورود ادمین رو پردازش می‌کنه.
    بقیه دستورات توسط router های قبلی handle شدن.

    فیکس: regex قبلی r"^/[a-zA-Z]\w*(\s+\S+)?$" رمزهای با کاراکتر خاص
    مثل @Pass123 رو catch نمی‌کرد چون \S+ فقط یک token بود.
    الان هر چیزی بعد از دستور قبول می‌شه.
    """
    from handlers.admin import handle_dynamic_admin_login_if_match
    await handle_dynamic_admin_login_if_match(message)


# ──────────────────────────────────────────────
# Callback: back_main
# ──────────────────────────────────────────────

@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery) -> None:
    """بازگشت به منوی اصلی."""
    await callback.answer()
    tg_user = callback.from_user
    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, tg_user.id)  # type: ignore[union-attr]
    is_admin = db_user.is_admin if db_user else False

    await callback.message.answer(  # type: ignore[union-attr]
        "🏠 منوی اصلی:",
        reply_markup=await get_main_menu_async(is_admin=is_admin),
    )
    await callback.message.delete()  # type: ignore[union-attr]

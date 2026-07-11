"""
handlers/shop.py — خرید کانفیگ و اشتراک تست

Flow خرید:
  1. کاربر «🛒 خرید کانفیگ» → لیست پلن‌های دیتابیس
  2. انتخاب پلن → نمایش جزئیات + دکمه تأیید + دکمه کد تخفیف
  3. (اختیاری) کاربر کد تخفیف وارد می‌کند → تأیید قیمت جدید
  4. تأیید → ایجاد invoice پرداخت
  5. پرداخت → ایجاد کانفیگ در پنل + ارسال لینک
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger

from config import settings
from database import AsyncSessionLocal
from database.crud import (
    create_payment, get_discount_code, get_or_create_user, get_plan,
    get_active_plans, has_used_test_subscription,
    record_test_subscription, use_discount_code, validate_discount,
)
from keyboards.main_menu import get_main_menu, get_main_menu_async
from keyboards.plans import (
    get_confirm_after_discount_keyboard, get_payment_status_keyboard,
    get_plan_confirm_keyboard, get_plans_keyboard,
)
from services.payments import CryptoPaymentService, PaymentError, PaymentAPIError
from services.subscription import create_new_subscription
from services.xui_api import XUIClient, XUIError
from services.banner import send_with_banner
from services.payment_methods import get_payment_status

router = Router(name="shop")

# ── قفل جلوگیری از race condition در اشتراک تست ──
# هر telegram_id یک lock جداگانه دارد تا کلیک‌های همزمان
# نتوانند چندین اشتراک تست برای یک کاربر بسازند
_test_sub_locks: dict[int, asyncio.Lock] = {}


# ──────────────────────────────────────────────
# Helper: ارسال کانفیگ‌ها به کاربر
# ──────────────────────────────────────────────

async def _send_subscription_to_user(message: Message, result, plan_name: str = "") -> None:
    """
    بعد از ایجاد اشتراک:
      1. QR Code از sub_link (برای import همه کانفیگ‌ها)
      2. لینک subscription متنی
      3. هر کانفیگ تکی (vless://... vmess://...) در پیام جداگانه با code block
    """
    import io
    # ── پیام اصلی با sub_link ──────────────────
    title = f"{plan_name} — " if plan_name else ""
    header = f"🎉 *{title}اشتراک شما آماده شد!*\n"
    body = (
        f"━━━━━━━━━━━━━━━\n"
        f"📧 شناسه اشتراک: `{result.email}`\n\n"
        f"🔗 *لینک اشتراک* (همه سرورها):\n`{result.sub_link}`\n\n"
        "📲 این لینک را در اپ‌های زیر وارد کنید:\n"
        "• اندروید: وی‌تو‌ری‌ان‌جی، هیدیفای\n"
        "• آیفون: استرایزند، شدوراکت\n"
        "• ویندوز: هیدیفای، وی‌تو‌ری‌ان\n\n"
        "یا QR کد زیر را اسکن کنید 👇"
    )
    await message.answer(header + body, parse_mode="Markdown")

    # ── QR Code از sub_link ────────────────────
    if result.qr_bytes:
        await message.answer_photo(
            BufferedInputFile(result.qr_bytes, filename="sub_qr.png"),
            caption="📷 اسکن کنید تا همه کانفیگ‌ها اضافه شوند",
        )

    # ── کانفیگ‌های تکی ────────────────────────
    links = result.config_links
    if links:
        await message.answer(
            f"📋 *کانفیگ‌های مستقل ({len(links)} سرور)*\n"
            "اگر نمی‌توانید لینک اشتراک وارد کنید، هر کانفیگ را جداگانه کپی کنید:",
            parse_mode="Markdown",
        )
        for i, link in enumerate(links, 1):
            proto = link.split("://")[0].upper() if "://" in link else "سرور"
            await message.answer(
                f"*سرور {i} — {proto}:*\n`{link}`",
                parse_mode="Markdown",
            )
    else:
        await message.answer(
            "ℹ️ کانفیگ مستقل در دسترس نیست.\n"
            "از لینک اشتراک بالا برای اتصال استفاده کنید."
        )


# ──────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────

class DiscountState(StatesGroup):
    waiting_code = State()


# ──────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────

_FARSI_USERS = {
    1: "یک کاربره",
    2: "دو کاربره",
    3: "سه کاربره",
    4: "چهار کاربره",
    5: "پنج کاربره",
}


async def _get_usdt_rate() -> int:
    """نرخ USDT به تومان را از DB می‌خواند."""
    try:
        from database.crud import get_setting
        async with AsyncSessionLocal() as s:
            val = await get_setting(s, "usdt_to_toman_rate", "0")
            return int(val) if val and val.isdigit() else 0
    except Exception:
        return 0


def _fmt_usdt(price: float) -> str:
    """
    نمایش هوشمند قیمت دلاری — بدون صفر اضافه، بدون برش اشتباه.
    مثال‌ها:
      5.0   → "5"
      3.5   → "3.5"
      0.03  → "0.03"
      0.005 → "0.005"
    """
    if price == int(price):
        return f"{int(price)}"
    formatted = f"{price:g}"
    if "e" in formatted or "E" in formatted:
        decimals = max(2, -int(f"{price:.0e}".split("e")[1]) + 1)
        formatted = f"{price:.{decimals}f}".rstrip("0")
        if formatted.endswith("."):
            formatted += "0"
    return formatted


async def _fmt_plan_with_price(plan) -> str:
    """فرمت پلن با نمایش قیمت — بر اساس تنظیم نمایش قیمت ادمین."""
    if plan.traffic_gb == 0:
        if plan.limit_ip and plan.limit_ip > 0:
            user_label = _FARSI_USERS.get(plan.limit_ip, f"{plan.limit_ip} کاربره")
            traffic = f"♾ نامحدود — {user_label}"
        else:
            traffic = "♾ نامحدود"
    else:
        traffic = f"{plan.traffic_gb} گیگابایت"

    rate = await _get_usdt_rate()
    price_str = _fmt_usdt(plan.price_usdt)

    from services.payment_methods import get_price_display_mode
    display_mode = await get_price_display_mode()

    if display_mode == "usd":
        price_line = f"💵 قیمت: `{price_str} USDT`"
    elif display_mode == "toman":
        if rate and rate > 0:
            toman = int(plan.price_usdt * rate)
            price_line = f"🪙 قیمت: `{toman:,} تومان`"
        else:
            price_line = (
                f"🪙 قیمت: _نرخ تنظیم نشده_\n"
                f"_در پنل ادمین نرخ تومان را وارد کنید_"
            )
    else:  # "both" (پیش‌فرض)
        if rate and rate > 0:
            toman = int(plan.price_usdt * rate)
            price_line = f"💵 قیمت: `{price_str} USDT`  —  `{toman:,} تومان`"
        else:
            price_line = (
                f"💵 قیمت: `{price_str} USDT`\n"
                f"_⚠️ برای نمایش قیمت تومانی، نرخ را در پنل تنظیم کنید_"
            )

    return (
        f"📦 *{plan.name}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🗂 حجم ترافیک: `{traffic}`\n"
        f"⏱ مدت اعتبار: `{plan.duration_days} روز`\n"
        f"{price_line}"
    )


def _fmt_plan(plan) -> str:
    """نسخه sync برای جاهایی که async نمی‌شه — فقط دلار."""
    if plan.traffic_gb == 0:
        if plan.limit_ip and plan.limit_ip > 0:
            user_label = _FARSI_USERS.get(plan.limit_ip, f"{plan.limit_ip} کاربره")
            traffic = f"♾ نامحدود — {user_label}"
        else:
            traffic = "♾ نامحدود"
    else:
        traffic = f"{plan.traffic_gb} گیگابایت"
    price_str = _fmt_usdt(plan.price_usdt)
    return (
        f"📦 *{plan.name}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🗂 حجم ترافیک: `{traffic}`\n"
        f"⏱ مدت اعتبار: `{plan.duration_days} روز`\n"
        f"💵 قیمت: `{price_str} دلار`"
    )


def _xui_client() -> XUIClient:
    return XUIClient(
        panel_url=settings.panel_url,
        username=settings.panel_username,
        password=settings.panel_password,
        api_path=settings.panel_api_path,
        sub_port=settings.sub_port,
    )


# ──────────────────────────────────────────────
# 🛒 خرید کانفیگ — منوی اصلی
# ──────────────────────────────────────────────

@router.message(F.text.contains("خرید کانفیگ"))
async def msg_buy(message: Message) -> None:
    async with AsyncSessionLocal() as session:
        plans = await get_active_plans(session)
    if not plans:
        await message.answer(
            "⚠️ در حال حاضر پلنی موجود نیست.\nلطفاً بعداً مراجعه کنید.",
            reply_markup=await get_main_menu_async(),
        )
        return
    limited_count = sum(1 for p in plans if p.traffic_gb > 0)
    unlimited_count = sum(1 for p in plans if p.traffic_gb == 0)
    desc_parts = []
    if limited_count:
        desc_parts.append(f"📦 {limited_count} پلن حجمی")
    if unlimited_count:
        desc_parts.append(f"♾ {unlimited_count} پلن نامحدود")

    from services.payment_methods import get_price_display_mode
    rate = await _get_usdt_rate()
    display_mode = await get_price_display_mode()
    # اگر فقط تومان نشان داده می‌شود، rate باید 0 نباشد تا قیمت تومانی محاسبه شود
    effective_rate = rate if display_mode in ("both", "toman") else 0
    # برای حالت "toman" نرخ می‌خواهیم اما "usd" نمی‌خواهیم
    await send_with_banner(
        message,
        f"🛒 <b>خرید اشتراک VPN</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{' | '.join(desc_parts)}\n\n"
        "👇 پلن مورد نظر خود را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=get_plans_keyboard(plans, effective_rate, display_mode=display_mode),
    )


async def _safe_edit_cb(callback: CallbackQuery, text: str, **kwargs) -> None:
    """edit_text امن — اگه پیام عکس‌دار بود، answer جدید می‌فرسته."""
    try:
        if callback.message.photo or callback.message.document:  # type: ignore
            await callback.message.answer(text, **kwargs)  # type: ignore
        else:
            await callback.message.edit_text(text, **kwargs)  # type: ignore
    except Exception:
        await callback.message.answer(text, **kwargs)  # type: ignore


@router.callback_query(F.data == "show_plans")
async def cb_show_plans(callback: CallbackQuery) -> None:
    await callback.answer()
    async with AsyncSessionLocal() as session:
        plans = await get_active_plans(session)
    from services.payment_methods import get_price_display_mode
    rate = await _get_usdt_rate()
    display_mode = await get_price_display_mode()
    effective_rate = rate if display_mode in ("both", "toman") else 0
    await _safe_edit_cb(
        callback,
        "🛒 *خرید اشتراک VPN*\n"
        "━━━━━━━━━━━━━━━\n"
        "👇 پلن مورد نظر خود را انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=get_plans_keyboard(plans, effective_rate, display_mode=display_mode),
    )


# ──────────────────────────────────────────────
# انتخاب پلن → نمایش جزئیات + تأیید
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("plan:"))
async def cb_plan_select(callback: CallbackQuery) -> None:
    await callback.answer()
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
    if not plan or not plan.is_active:
        await callback.answer("❌ این پلن در دسترس نیست.", show_alert=True)
        return
    pm = await get_payment_status()
    plan_text = await _fmt_plan_with_price(plan)
    await _safe_edit_cb(
        callback,
        plan_text + "\n\n✅ روش پرداخت را انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=get_plan_confirm_keyboard(
            plan_id,
            crypto_on=pm["crypto"],
            card_on=pm["card"],
            crypto_invoice=pm.get("crypto_invoice", False),
            crypto_gateway=pm.get("crypto_gateway", "nowpayments"),
            amount=float(plan.price_usdt),
            plan_name=plan.name,
        ),
    )


# ──────────────────────────────────────────────
# کد تخفیف
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("discount:"))
async def cb_discount_request(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    plan_id = int(callback.data.split(":")[1])
    await state.set_state(DiscountState.waiting_code)
    await state.update_data(plan_id=plan_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ انصراف", callback_data=f"discount_cancel:{plan_id}")
    await callback.message.answer(
        "🏷 کد تخفیف خود را وارد کنید:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("discount_cancel:"))
async def cb_discount_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """لغو ورود کد تخفیف — برگشت به صفحه پلن با روش‌های پرداخت واقعی."""
    await state.clear()
    await callback.answer("❌ انصراف از کد تخفیف.")
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
    if plan:
        # وضعیت روش‌های پرداخت را از DB بخوان — نه از default
        pm = await get_payment_status()
        await callback.message.answer(
            _fmt_plan(plan) + "\n\n✅ روش پرداخت را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_plan_confirm_keyboard(
                plan_id,
                crypto_on=pm["crypto"],
                card_on=pm["card"],
                crypto_invoice=pm.get("crypto_invoice", False),
                crypto_gateway=pm.get("crypto_gateway", "nowpayments"),
                amount=float(plan.price_usdt),
                plan_name=plan.name,
            ),
        )
    else:
        await callback.message.answer("بازگشت به لیست پلن‌ها:", reply_markup=None)
        await cb_show_plans.__wrapped__(callback)  # type: ignore


@router.message(DiscountState.waiting_code, F.text.in_({"/cancel", "انصراف", "cancel"}))
async def msg_discount_cancel_text(message: Message, state: FSMContext) -> None:
    """لغو با تایپ /cancel."""
    data = await state.get_data()
    plan_id = data.get("plan_id")
    await state.clear()
    await message.answer("❌ ورود کد تخفیف لغو شد.")
    if plan_id:
        async with AsyncSessionLocal() as session:
            plan = await get_plan(session, plan_id)
        if plan:
            # وضعیت روش‌های پرداخت را از DB بخوان — نه از default
            pm = await get_payment_status()
            await message.answer(
                _fmt_plan(plan) + "\n\n✅ روش پرداخت را انتخاب کنید:",
                parse_mode="Markdown",
                reply_markup=get_plan_confirm_keyboard(
                    plan_id,
                    crypto_on=pm["crypto"],
                    card_on=pm["card"],
                    crypto_invoice=pm.get("crypto_invoice", False),
                    crypto_gateway=pm.get("crypto_gateway", "nowpayments"),
                    amount=float(plan.price_usdt),
                    plan_name=plan.name,
                ),
            )


@router.message(DiscountState.waiting_code)
async def msg_discount_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    data = await state.get_data()
    plan_id = data["plan_id"]

    async with AsyncSessionLocal() as session:
        dc = await get_discount_code(session, code)
        if not dc:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ انصراف", callback_data=f"discount_cancel:{plan_id}")
            await message.answer(
                "❌ کد تخفیف پیدا نشد. دوباره وارد کنید یا انصراف دهید:",
                reply_markup=kb.as_markup(),
            )
            return
        valid, err_msg = validate_discount(dc)
        if not valid:
            await state.clear()
            await message.answer(f"❌ {err_msg}")
            return
        plan = await get_plan(session, plan_id)

    discount_amount = plan.price_usdt * dc.percent / 100
    final_price = round(plan.price_usdt - discount_amount, 2)

    await state.clear()
    await message.answer(
        f"✅ کد تخفیف *{dc.code}* اعمال شد!\n\n"
        f"💲 قیمت اصلی: `{_fmt_usdt(plan.price_usdt)} USDT`\n"
        f"🏷 تخفیف {dc.percent}٪: `-{_fmt_usdt(discount_amount)} USDT`\n"
        f"💰 قیمت نهایی: `{_fmt_usdt(final_price)} USDT`\n\n"
        f"روش پرداخت را انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=get_confirm_after_discount_keyboard(
            plan_id, code,
            crypto_on=(await get_payment_status())["crypto"],
            card_on=(await get_payment_status())["card"],
            crypto_invoice=(await get_payment_status()).get("crypto_invoice", False),
            crypto_gateway=(await get_payment_status()).get("crypto_gateway", "nowpayments"),
            amount=final_price,
            plan_name=plan.name,
        ),
    )


# ──────────────────────────────────────────────
# پرداخت — ایجاد invoice
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(callback: CallbackQuery) -> None:
    await callback.answer()
    tg_user = callback.from_user
    if not tg_user:
        return

    parts = callback.data.split(":")
    plan_id = int(parts[1])
    discount_code = parts[2] if len(parts) > 2 else None

    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        db_user, _ = await get_or_create_user(
            session, tg_user.id, tg_user.username, tg_user.first_name,
            admin_ids=settings.admin_ids,
        )

        if not plan or not plan.is_active:
            await callback.message.answer("❌ این پلن در دسترس نیست.")
            return

        # اعمال تخفیف
        final_price = plan.price_usdt
        dc = None
        if discount_code:
            dc = await get_discount_code(session, discount_code)
            if dc:
                valid, _ = validate_discount(dc)
                if valid:
                    final_price = round(plan.price_usdt * (1 - dc.percent / 100), 2)

        order_id = f"vpn_{tg_user.id}_{plan_id}_{uuid.uuid4().hex[:8]}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.invoice_expire_minutes)

        # ایجاد invoice
        try:
            svc = CryptoPaymentService()
            invoice = await svc.create_invoice(
                amount_usdt=final_price,
                order_id=order_id,
                inbound_id=plan_id,
                expire_minutes=settings.invoice_expire_minutes,
            )
        except (PaymentError, PaymentAPIError) as e:
            logger.error(f"خطای پرداخت: {e}")
            if not settings.nowpayments_api_key:
                await callback.message.answer(
                    "⚠️ *درگاه پرداخت تنظیم نشده*\n\n"
                    "برای فعال‌سازی پرداخت، `NOWPAYMENTS_API_KEY` را در `.env` وارد کنید.\n"
                    "تا آن زمان با ادمین تماس بگیرید.",
                    parse_mode="Markdown",
                )
            else:
                await callback.message.answer("❌ خطا در ایجاد invoice. لطفاً دوباره تلاش کنید.")
            return

        # ذخیره پرداخت
        await create_payment(
            session=session,
            user_id=db_user.id,
            order_id=order_id,
            amount_usdt=final_price,
            inbound_id=plan_id,
            payment_id=invoice.payment_id,
            pay_address=invoice.pay_address,
            expires_at=invoice.expiration_time,
        )

        # استفاده از کد تخفیف
        if dc:
            await use_discount_code(session, dc.id)

    text = (
        f"💳 *پرداخت اشتراک VPN*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 پلن انتخابی: `{plan.name}`\n"
        f"💰 مبلغ قابل پرداخت: `{invoice.pay_amount} تتر (شبکه ترون)`\n\n"
        f"📬 آدرس کیف پول:\n`{invoice.pay_address}`\n\n"
        f"⏰ مهلت پرداخت: `{settings.invoice_expire_minutes} دقیقه`\n\n"
        "پس از واریز، دکمه «بررسی پرداخت» را بزنید."
    )
    await callback.message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=get_payment_status_keyboard(order_id),
    )

    # نوتیف به ادمین‌ها — پرداخت کریپتو جدید در صف
    try:
        from services.admin_notify import notify_crypto_payment_pending
        await notify_crypto_payment_pending(
            bot=callback.bot,
            order_id=order_id,
            plan_name=plan.name,
            amount_usdt=final_price,
            pay_currency="USDT (TRC-20)",
            tg_id=tg_user.id,
            username=tg_user.username,
            gateway="nowpayments",
        )
    except Exception as _ne:
        logger.warning(f"نوتیف کریپتو به ادمین ناموفق: {_ne}")


# ──────────────────────────────────────────────
# پرداخت Invoice — صفحه انتخاب ارز NOWPayments
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay_invoice:"))
async def cb_pay_invoice(callback: CallbackQuery) -> None:
    """
    کاربر روی «پرداخت با ارز دیجیتال» زد →
    یک Invoice در NOWPayments می‌سازیم و لینک صفحه انتخاب ارز رو می‌دیم.
    کاربر خودش BTC / ETH / USDT / هر ارزی انتخاب می‌کنه.
    بعد از پرداخت NOWPayments یک IPN webhook می‌فرسته.
    """
    await callback.answer()
    tg_user = callback.from_user
    if not tg_user:
        return

    parts = callback.data.split(":")
    plan_id = int(parts[1])
    discount_code = parts[2] if len(parts) > 2 else None

    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        db_user, _ = await get_or_create_user(
            session, tg_user.id, tg_user.username, tg_user.first_name,
            admin_ids=settings.admin_ids,
        )
        if not plan or not plan.is_active:
            await callback.message.answer("❌ این پلن در دسترس نیست.")
            return

        final_price = plan.price_usdt
        dc = None
        if discount_code:
            dc = await get_discount_code(session, discount_code)
            if dc:
                valid, _ = validate_discount(dc)
                if valid:
                    final_price = round(plan.price_usdt * (1 - dc.percent / 100), 2)

        order_id = f"inv_{tg_user.id}_{plan_id}_{uuid.uuid4().hex[:8]}"

        try:
            svc = CryptoPaymentService()
            inv = await svc.create_invoice_page(
                amount_usdt=final_price,
                order_id=order_id,
                expire_minutes=settings.invoice_expire_minutes,
            )
        except Exception as e:
            logger.error(f"خطا در ساخت Invoice: {e}")
            await callback.message.answer(
                "❌ خطا در ایجاد لینک پرداخت. لطفاً دوباره تلاش کنید."
            )
            return

        # ذخیره در دیتابیس — payment_id خالی چون هنوز پرداخت نشده
        await create_payment(
            session=session,
            user_id=db_user.id,
            order_id=order_id,
            amount_usdt=final_price,
            inbound_id=plan_id,
            payment_method="crypto_invoice",
        )
        if dc:
            await use_discount_code(session, dc.id)

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 باز کردن صفحه پرداخت", url=inv.invoice_url)
    kb.button(text="🔄 بررسی پرداخت", callback_data=f"check_inv:{order_id}")
    kb.adjust(1)

    await callback.message.answer(
        f"🌐 *پرداخت با ارز دیجیتال*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 پلن: `{plan.name}`\n"
        f"💰 مبلغ: `{final_price} دلار`\n\n"
        f"👇 روی دکمه زیر کلیک کنید تا وارد صفحه پرداخت شوید.\n"
        f"در آنجا می‌توانید از بین *بیتکوین، اتریوم، تتر و ۱۰۰+ ارز دیگر* انتخاب کنید.\n\n"
        f"⏰ مهلت پرداخت: `{settings.invoice_expire_minutes} دقیقه`\n"
        f"🔖 شناسه سفارش: `{order_id}`\n\n"
        f"پس از پرداخت، اشتراک *خودکار* فعال می‌شود.",
        parse_mode="Markdown",
        reply_markup=kb.as_markup(),
    )

    # نوتیف به ادمین‌ها — Invoice چندارزه در صف
    try:
        from services.admin_notify import notify_crypto_payment_pending
        await notify_crypto_payment_pending(
            bot=callback.bot,
            order_id=order_id,
            plan_name=plan.name,
            amount_usdt=final_price,
            pay_currency="Multi-currency",
            tg_id=tg_user.id,
            username=tg_user.username,
            gateway="nowpayments",
        )
    except Exception as _ne:
        logger.warning(f"نوتیف Invoice به ادمین ناموفق: {_ne}")


# ──────────────────────────────────────────────
# 🎁 اشتراک تست
# ──────────────────────────────────────────────

@router.message(F.text.contains("اشتراک تست"))
async def msg_test_sub(message: Message) -> None:
    tg_user = message.from_user
    if not tg_user:
        return

    # بررسی فعال بودن + خواندن تنظیمات از DB (اولویت بالاتر از .env)
    from database.crud import get_setting
    async with AsyncSessionLocal() as _s:
        _enabled  = await get_setting(_s, "test_sub_enabled",       str(settings.test_subscription_enabled).lower())
        _traffic  = await get_setting(_s, "test_sub_traffic_gb",    str(settings.test_traffic_gb))
        _days     = await get_setting(_s, "test_sub_duration_days", str(settings.test_duration_days))

    if _enabled.lower() != "true":
        await message.answer(
            "⚠️ اشتراک تست در حال حاضر غیرفعال است.\n"
            "برای خرید اشتراک از بخش 🛒 خرید اقدام کنید."
        )
        return

    # ── پشتیبانی از اعداد اعشاری برای حجم ترافیک (مثلاً 0.05 = 50 MB) ──────────
    def _parse_traffic(val: str, default: float) -> float:
        """تبدیل رشته به عدد اعشاری مثبت — پشتیبانی از GB و MB."""
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    traffic_gb    = _parse_traffic(_traffic, float(settings.test_traffic_gb))
    try:
        duration_days = int(_days) if _days.isdigit() else settings.test_duration_days
    except (ValueError, AttributeError):
        duration_days = settings.test_duration_days

    # ── قفل race condition — هر کاربر فقط یک درخواست همزمان ──────────────────
    # از dict قفل‌ها استفاده می‌کنیم تا کلیک‌های پشت‌سرهم را serialize کنیم
    if tg_user.id not in _test_sub_locks:
        _test_sub_locks[tg_user.id] = asyncio.Lock()
    user_lock = _test_sub_locks[tg_user.id]

    if user_lock.locked():
        # درخواست قبلی هنوز در حال پردازش است
        await message.answer(
            "⏳ درخواست شما در حال پردازش است، لطفاً چند لحظه صبر کنید."
        )
        return

    async with user_lock:
        # ── بررسی دوباره استفاده پس از گرفتن قفل ─────────────────────────────
        # این چک داخل قفل انجام می‌شود تا race condition حل شود
        async with AsyncSessionLocal() as session:
            used = await has_used_test_subscription(session, tg_user.id)
            if used:
                await message.answer(
                    "⚠️ شما قبلاً از اشتراک تست استفاده کرده‌اید.\n"
                    "هر آیدی تلگرام فقط یک‌بار می‌تواند اشتراک تست دریافت کند.",
                )
                return

            db_user, _ = await get_or_create_user(
                session, tg_user.id, tg_user.username, tg_user.first_name,
                admin_ids=settings.admin_ids,
            )

        await message.answer("⏳ در حال ایجاد اشتراک تست...")

        try:
            async with AsyncSessionLocal() as session:
                result = await create_new_subscription(
                    session=session,
                    user_id=db_user.id,
                    telegram_id=tg_user.id,
                    inbound_id=0,
                    traffic_gb=traffic_gb,
                    expire_days=duration_days,
                    is_gift=True,
                    username=tg_user.username,
                )
            # ثبت استفاده از تست در یک session جداگانه — بعد از موفقیت ایجاد اشتراک
            async with AsyncSessionLocal() as session2:
                await record_test_subscription(session2, tg_user.id)
        except XUIError as e:
            logger.error(f"خطا در ایجاد اشتراک تست (XUIError) — user={tg_user.id}: {type(e).__name__}: {e}")
            err_lower = str(e).lower()
            if "اتصال" in str(e) or "connect" in err_lower or "transport" in err_lower:
                await message.answer(
                    "❌ خطا در اتصال به پنل VPN.\n"
                    "لطفاً چند دقیقه دیگر دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
                )
            elif "inbound" in err_lower or "اینباند" in str(e):
                await message.answer(
                    "⚠️ سرور VPN در حال حاضر آماده نیست.\n"
                    "لطفاً با پشتیبانی تماس بگیرید."
                )
            elif "auth" in err_lower or "login" in err_lower or "ورود" in str(e) or "401" in str(e):
                await message.answer(
                    "⚠️ خطای احراز هویت پنل.\n"
                    "لطفاً با پشتیبانی تماس بگیرید."
                )
            else:
                await message.answer(
                    "❌ خطا در ایجاد اشتراک تست.\n"
                    "لطفاً بعداً تلاش کنید یا با پشتیبانی تماس بگیرید."
                )
            return
        except Exception as e:
            logger.error(f"خطای غیرمنتظره در اشتراک تست — user={tg_user.id}: {type(e).__name__}: {e}", exc_info=True)
            await message.answer(
                "❌ خطای غیرمنتظره‌ای رخ داد.\n"
                "لطفاً بعداً تلاش کنید یا با پشتیبانی تماس بگیرید."
            )
            return

    traffic_label = f"{traffic_gb:g} GB" if traffic_gb >= 1 else f"{traffic_gb * 1024:.0f} MB"
    await message.answer(
        f"🎁 *اشتراک تست — {traffic_label} / {duration_days} روز*\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )
    await _send_subscription_to_user(message, result, plan_name="اشتراک تست")

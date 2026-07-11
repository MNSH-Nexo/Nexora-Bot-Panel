"""
handlers/payments.py — بررسی وضعیت پرداخت کریپتو

توجه: handler اصلی ایجاد invoice (pay:) در shop.py قرار دارد
      تا کد تخفیف و روش پرداخت به درستی اعمال شوند.
      این فایل فقط check_payment: و _confirm_payment را مدیریت می‌کند.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery
from loguru import logger

from config import settings
from database import AsyncSessionLocal
from database.crud import (
    create_payment,
    get_payment_by_order_id,
    get_payment_by_payment_id,
    update_payment_status,
    get_or_create_user,
)
from keyboards.plans import get_payment_status_keyboard
from services.payments import PaymentError, crypto_payment_service
from services.subscription import create_new_subscription
from utils.qrcode_gen import generate_qr_code

router = Router(name="payments")


# ──────────────────────────────────────────────
# Callback: pay_crypto:{plan_id} — ایجاد invoice
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay_crypto:"))
async def cb_pay_crypto(callback: CallbackQuery) -> None:
    """
    ایجاد invoice پرداخت USDT TRC-20:
      1. دریافت/ایجاد کاربر
      2. ایجاد invoice از NOWPayments
      3. ذخیره در دیتابیس
      4. نمایش QR Code آدرس والت + آدرس + زمان انقضا
    """
    await callback.answer()
    tg_user = callback.from_user
    if not tg_user:
        return

    parts = callback.data.split(":")  # type: ignore[union-attr]
    plan_id   = int(parts[1]) if len(parts) > 1 else 0
    amount    = float(parts[2]) if len(parts) > 2 else settings.plan_price_usdt

    processing_msg = await callback.message.answer(  # type: ignore[union-attr]
        "⏳ در حال ایجاد فاکتور پرداخت...\nلطفاً چند لحظه صبر کنید."
    )

    try:
        async with AsyncSessionLocal() as session:
            db_user, _ = await get_or_create_user(
                session=session,
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                admin_ids=settings.admin_ids,
            )

            order_id = f"crypto_{tg_user.id}_{plan_id}_{uuid.uuid4().hex[:8]}"

            invoice = await crypto_payment_service.create_invoice(
                amount_usdt=amount,
                order_id=order_id,
                inbound_id=plan_id,
                expire_minutes=settings.invoice_expire_minutes,
            )

            await create_payment(
                session=session,
                user_id=db_user.id,
                order_id=order_id,
                amount_usdt=amount,
                inbound_id=plan_id,
                payment_id=invoice.payment_id,
                pay_address=invoice.pay_address,
                pay_currency=invoice.pay_currency,
                expires_at=invoice.expiration_time,
            )

        qr_bytes = await generate_qr_code(invoice.qr_data)
        qr_file  = BufferedInputFile(file=qr_bytes, filename="payment_qr.png")

        expire_str   = invoice.expiration_time.strftime("%H:%M")
        sandbox_note = "\n\n⚠️ *حالت آزمایشی* — پرداخت واقعی نیست." if not settings.nowpayments_api_key else ""

        caption = (
            "💳 *فاکتور پرداخت*\n"
            "━━━━━━━━━━━━━━━\n"
            f"💰 مبلغ: `{invoice.pay_amount:.4f} USDT`\n"
            f"🌐 شبکه: `TRON — TRC-20`\n\n"
            f"📋 *آدرس کیف پول:*\n`{invoice.pay_address}`\n\n"
            f"⏰ مهلت پرداخت تا: `{expire_str}`\n"
            f"🔖 شناسه سفارش: `{order_id}`\n\n"
            "روش پرداخت:\n"
            "۱. QR کد را اسکن کنید\n"
            "۲. یا آدرس بالا را کپی کنید\n"
            "۳. مبلغ *دقیق* را ارسال کنید\n\n"
            "پس از تأیید شبکه، اشتراک خودکار فعال می‌شود."
            f"{sandbox_note}"
        )

        await callback.message.answer_photo(  # type: ignore[union-attr]
            photo=qr_file,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=get_payment_status_keyboard(order_id),
        )
        await processing_msg.delete()

        # نوتیف به ادمین‌ها
        try:
            from database.crud import get_plan
            from services.admin_notify import notify_crypto_payment_pending
            async with AsyncSessionLocal() as _s:
                _plan = await get_plan(_s, plan_id)
            _plan_name = _plan.name if _plan else f"پلن #{plan_id}"
            await notify_crypto_payment_pending(
                bot=callback.bot,
                order_id=order_id,
                plan_name=_plan_name,
                amount_usdt=amount,
                pay_currency=invoice.pay_currency,
                tg_id=tg_user.id,
                username=tg_user.username,
                gateway="nowpayments",
            )
        except Exception as _e:
            logger.warning(f"نوتیف کریپتو به ادمین ناموفق: {_e}")

    except PaymentError as e:
        logger.error(f"خطای پرداخت برای user {tg_user.id}: {e}")
        await processing_msg.edit_text(
            f"❌ خطا در ایجاد فاکتور:\n`{e}`\n\nلطفاً مجدداً تلاش کنید.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception(f"خطای ناشناخته پرداخت: {e}")
        await processing_msg.edit_text(
            "❌ خطای غیرمنتظره رخ داد. لطفاً با پشتیبانی تماس بگیرید."
        )


# ──────────────────────────────────────────────
# Callback: check_payment:{order_id} — بررسی وضعیت
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("check_payment:"))
async def cb_check_payment(callback: CallbackQuery) -> None:
    """بررسی وضعیت پرداخت توسط کاربر (polling دستی)."""
    await callback.answer("🔄 در حال بررسی...")
    order_id = callback.data.split(":", 1)[1]  # type: ignore[union-attr]
    tg_user = callback.from_user

    async with AsyncSessionLocal() as session:
        payment = await get_payment_by_order_id(session, order_id)

    if not payment:
        await callback.answer("❌ فاکتور پیدا نشد.", show_alert=True)
        return

    # بررسی انقضا
    if payment.expires_at and datetime.now(timezone.utc) > payment.expires_at:
        async with AsyncSessionLocal() as session:
            await update_payment_status(session, payment.id, "expired")
        await callback.answer("⏰ فاکتور منقضی شده. لطفاً مجدداً خرید کنید.", show_alert=True)
        return

    # اگر قبلاً تأیید شده
    if payment.status in ("confirmed", "finished"):
        await callback.answer("✅ پرداخت قبلاً تأیید شده است.", show_alert=True)
        return

    # بررسی از NOWPayments
    if not payment.payment_id:
        await callback.answer("⏳ هنوز پرداختی دریافت نشده.", show_alert=True)
        return

    try:
        ps = await crypto_payment_service.get_payment_status(payment.payment_id)

        if crypto_payment_service.is_paid(ps.status):
            await _confirm_payment_and_create_sub(callback, payment, order_id)
        elif crypto_payment_service.is_failed(ps.status):
            async with AsyncSessionLocal() as session:
                await update_payment_status(session, payment.id, ps.status)
            await callback.answer(
                f"❌ پرداخت ناموفق بود (وضعیت: {ps.status}).", show_alert=True
            )
        else:
            await callback.answer(
                f"⏳ وضعیت: {ps.status}\nلطفاً صبر کنید و مجدداً بررسی کنید.",
                show_alert=True,
            )
    except Exception as e:
        logger.error(f"خطا در بررسی وضعیت پرداخت {order_id}: {e}")
        await callback.answer("⚠️ خطا در بررسی وضعیت. لطفاً دقایقی دیگر امتحان کنید.", show_alert=True)


# ──────────────────────────────────────────────
# Callback: check_inv:{order_id} — بررسی Invoice
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("check_inv:"))
async def cb_check_invoice(callback: CallbackQuery) -> None:
    """
    بررسی وضعیت پرداخت Invoice (صفحه انتخاب ارز).
    IPN معمولاً خودکار اشتراک می‌سازه — این دکمه fallback دستی هست.
    """
    await callback.answer("🔄 در حال بررسی...")
    order_id = callback.data.split(":", 1)[1]

    async with AsyncSessionLocal() as session:
        payment = await get_payment_by_order_id(session, order_id)

    if not payment:
        await callback.answer("❌ سفارش پیدا نشد.", show_alert=True)
        return

    if payment.status in ("confirmed", "finished"):
        await callback.answer("✅ پرداخت قبلاً تأیید و اشتراک فعال شده.", show_alert=True)
        return

    # Invoice ها payment_id ندارند تا کاربر پرداخت نکنه
    # → فقط وضعیت DB رو چک می‌کنیم
    await callback.answer(
        "⏳ پرداخت هنوز تأیید نشده.\n"
        "بعد از پرداخت در صفحه NOWPayments، اشتراک خودکار فعال می‌شود.\n"
        "معمولاً تا چند دقیقه طول می‌کشد.",
        show_alert=True,
    )


# ──────────────────────────────────────────────
# تأیید پرداخت + ایجاد اشتراک
# ──────────────────────────────────────────────

async def _confirm_payment_and_create_sub(
    callback: CallbackQuery,
    payment: object,
    order_id: str,
) -> None:
    """پس از تأیید پرداخت، اشتراک ایجاد می‌کند."""
    tg_user = callback.from_user
    if not tg_user:
        return

    processing_msg = await callback.message.answer(  # type: ignore[union-attr]
        "✅ پرداخت تأیید شد!\n⏳ در حال ایجاد کانفیگ VPN..."
    )

    try:
        async with AsyncSessionLocal() as session:
            db_user, _ = await get_or_create_user(
                session=session,
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                admin_ids=settings.admin_ids,
            )

            result = await create_new_subscription(
                session=session,
                user_id=db_user.id,
                telegram_id=tg_user.id,
                inbound_id=0,  # 0 = انتخاب خودکار بر اساس پلن یا اینباندهای عمومی
                plan_id=getattr(payment, "inbound_id", 0),  # inbound_id در جدول payment = plan_id
                username=tg_user.username,
            )

            await update_payment_status(
                session, payment.id, "confirmed", result.subscription.id  # type: ignore[attr-defined]
            )

        # ارسال QR Code کانفیگ
        qr_file = BufferedInputFile(file=result.qr_bytes, filename="vpn_qrcode.png")
        ip_line = f"📡 محدودیت دستگاه: *{result.limit_ip} دستگاه همزمان*\n" if result.limit_ip else ""
        caption = (
            "🎉 *اشتراک شما آماده شد!*\n"
            "━━━━━━━━━━━━━━━\n"
            f"📧 شناسه اشتراک: `{result.email}`\n"
            f"{ip_line}"
            "\n📱 *روش اتصال:*\n"
            "۱. QR کد را اسکن کنید\n"
            "۲. یا لینک زیر را در اپ کپی کنید\n\n"
            f"🔗 *لینک اشتراک:*\n`{result.sub_link}`\n\n"
            "📲 *اپ‌های پیشنهادی:*\n"
            "• اندروید: هیدیفای، وی‌تو‌ری‌ان‌جی\n"
            "• آیفون: استرایزند، شدوراکت\n"
            "• ویندوز: هیدیفای، وی‌تو‌ری‌ان\n"
            "• مک: هیدیفای، وی‌تو‌باکس\n\n"
            "⚠️ این لینک را با کسی به اشتراک نگذارید."
        )
        await callback.message.answer_photo(  # type: ignore[union-attr]
            photo=qr_file,
            caption=caption,
            parse_mode="Markdown",
        )
        await processing_msg.delete()

    except Exception as e:
        logger.exception(f"خطا در ایجاد اشتراک بعد از پرداخت {order_id}: {e}")
        await processing_msg.edit_text(
            "✅ پرداخت تأیید شد اما خطایی در ایجاد کانفیگ رخ داد.\n"
            "لطفاً با پشتیبانی تماس بگیرید و این شناسه را ارسال کنید:\n"
            f"`{order_id}`",
            parse_mode="Markdown",
        )

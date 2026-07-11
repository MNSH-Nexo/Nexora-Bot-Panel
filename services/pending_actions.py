"""
services/pending_actions.py — پردازش اقدامات معلق از وب‌پنل

دو روش موازی برای تشخیص تأییدیه‌های وب‌پنل:

روش ۱ — جدول pending_actions:
  وب‌پنل یک ردیف type='card_approve' می‌نویسد.
  این سرویس آن را می‌خواند، اشتراک می‌سازد، ردیف را حذف می‌کند.

روش ۲ — polling مستقیم payments:
  تراکنش‌هایی که status='confirmed' دارند ولی subscription_id ندارند
  را پیدا می‌کند و اشتراک می‌سازد. (backup بدون نیاز به pending_actions)
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile
from loguru import logger

from config import settings
from database import AsyncSessionLocal
from database.crud import get_payment_by_order_id, update_payment_status
from services.subscription import create_new_subscription


def _get_db_path() -> Optional[str]:
    """مسیر فایل SQLite را پیدا می‌کند."""
    db_url = settings.db_url
    if "sqlite" in db_url:
        path = db_url.split("///")[-1]
        if path.startswith("./"):
            path = path[2:]
        if not os.path.isabs(path):
            bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(bot_dir, path)
        if os.path.exists(path):
            return path
    for candidate in [
        "./bot_data.db",
        "/opt/nexora-bot/bot_data.db",
        "/opt/nexora-bot/data/bot_data.db",
        "/app/bot_data.db",
    ]:
        if os.path.exists(candidate):
            return candidate
    return None


async def process_pending_actions(bot: Bot) -> None:
    """هر ۱۵ ثانیه: pending_actions + confirmed payments بدون اشتراک."""
    db_path = _get_db_path()
    if not db_path:
        logger.debug("pending_actions: DB file not found — skip")
        return

    try:
        await _process_pending_actions_table(bot, db_path)
    except Exception as e:
        logger.debug(f"pending_actions table: {e}")

    try:
        await _process_confirmed_payments(bot, db_path)
    except Exception as e:
        logger.debug(f"confirmed_payments scan: {e}")


async def _process_pending_actions_table(bot: Bot, db_path: str) -> None:
    """روش ۱: پردازش جدول pending_actions."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_actions'"
    ).fetchone()
    if not exists:
        conn.close()
        return
    KNOWN_TYPES = (
        "'card_approve'", "'manual_sub'",
        "'sub_toggle'", "'sub_reset'", "'sub_delete'", "'sub_edit'",
        "'ticket_reply'", "'ticket_close'", "'ticket_reopen'",
        "'bot_broadcast'",
    )
    rows = conn.execute(
        f"SELECT * FROM pending_actions WHERE type IN ({','.join(KNOWN_TYPES)}) ORDER BY created_at ASC LIMIT 30"
    ).fetchall()
    conn.close()

    for row in rows:
        row_id = row["id"]
        action_type = row["type"]
        try:
            payload = json.loads(row["payload"] or "{}")

            if action_type == "card_approve":
                order_id    = payload.get("order_id", "")
                telegram_id = int(payload.get("telegram_id", 0))
                if order_id:
                    await _build_subscription(bot, order_id, telegram_id)

            elif action_type == "manual_sub":
                await _build_manual_subscription(bot, payload)

            elif action_type == "sub_toggle":
                await _sub_toggle(payload)

            elif action_type == "sub_reset":
                await _sub_reset(payload)

            elif action_type == "sub_delete":
                await _sub_delete(payload)

            elif action_type == "sub_edit":
                await _sub_edit(payload)

            elif action_type == "ticket_reply":
                await _ticket_reply(bot, payload)

            elif action_type == "ticket_close":
                await _ticket_close(bot, payload)

            elif action_type == "bot_broadcast":
                await _bot_broadcast(bot, payload)

            elif action_type == "ticket_reopen":
                await _ticket_reopen(bot, payload)

            # حذف ردیف پس از پردازش موفق
            conn2 = sqlite3.connect(db_path, timeout=10)
            conn2.execute("DELETE FROM pending_actions WHERE id = ?", (row_id,))
            conn2.commit()
            conn2.close()
            logger.info(f"pending_action #{row_id} ({action_type}) پردازش شد")

        except Exception as e:
            logger.error(f"خطا در pending_action #{row_id} ({action_type}): {e}")
            try:
                conn3 = sqlite3.connect(db_path, timeout=10)
                conn3.execute(
                    "UPDATE pending_actions SET type=? WHERE id=?",
                    (action_type + "_failed", row_id)
                )
                conn3.commit()
                conn3.close()
            except Exception:
                pass


async def _process_confirmed_payments(bot: Bot, db_path: str) -> None:
    """
    روش ۲ (backup): تراکنش‌های confirmed بدون اشتراک.
    status='confirmed' + payment_method='card' + subscription_id IS NULL
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT p.id, p.order_id, p.inbound_id, u.telegram_id
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            WHERE p.status = 'confirmed'
              AND p.payment_method = 'card'
              AND (p.subscription_id IS NULL OR p.subscription_id = 0)
              AND p.created_at > datetime('now', '-7 days')
            ORDER BY p.created_at ASC
            LIMIT 10
        """).fetchall()
    except Exception as e:
        conn.close()
        logger.debug(f"confirmed_payments query: {e}")
        return
    conn.close()
    for row in rows:
        order_id = row["order_id"]
        telegram_id = int(row["telegram_id"] or 0)
        logger.info(f"confirmed payment without subscription found: {order_id}")
        try:
            await _build_subscription(bot, order_id, telegram_id)
        except Exception as e:
            logger.error(f"خطا در ساخت اشتراک برای {order_id}: {e}")


async def _build_manual_subscription(bot: Bot, payload: dict) -> None:
    """
    اشتراک دستی از طریق وب‌پنل — با همان منطق کامل ربات.
    payload: { user_id, telegram_id, plan_id?, traffic_gb?, expire_days?, limit_ip? }
    """
    user_id     = int(payload.get("user_id", 0))
    telegram_id = int(payload.get("telegram_id", 0))
    plan_id     = int(payload.get("plan_id", 0))
    traffic_gb  = int(payload.get("traffic_gb", 0))
    expire_days = int(payload.get("expire_days", 0))
    limit_ip    = int(payload.get("limit_ip", 0))

    if not user_id or not telegram_id:
        logger.error(f"manual_sub: user_id یا telegram_id خالی است: {payload}")
        return

    async with AsyncSessionLocal() as session:
        # دریافت username از دیتابیس برای ساخت email پنل
        from database.crud import get_plan
        from database.models import User as _User
        from sqlalchemy import select as _select
        _ur = await session.execute(_select(_User).where(_User.id == user_id))
        _user_obj = _ur.scalar_one_or_none()
        _username = _user_obj.username if _user_obj else None

        # اگر plan_id داده شده، مشخصات را از پلن بخوان
        if plan_id and not traffic_gb and not expire_days:
            plan_obj = await get_plan(session, plan_id)
            if plan_obj:
                traffic_gb  = plan_obj.traffic_gb  or 0
                expire_days = plan_obj.duration_days or 0
                limit_ip    = plan_obj.limit_ip or 0

        try:
            result = await create_new_subscription(
                session=session,
                user_id=user_id,
                telegram_id=telegram_id,
                inbound_id=0,
                traffic_gb=traffic_gb,
                expire_days=expire_days,
                plan_id=plan_id,
                username=_username,
            )
            logger.success(f"manual_sub: اشتراک {result.subscription.id} برای user {user_id} ساخته شد")
        except Exception as e:
            logger.error(f"manual_sub: خطا در ساخت اشتراک برای user {user_id}: {e}")
            try:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=(
                        "⚠️ <b>ساخت اشتراک با مشکل مواجه شد.</b>\n"
                        "لطفاً با پشتیبانی تماس بگیرید."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

    # ارسال اشتراک به کاربر
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🎁 <b>اشتراک VPN شما آماده شد!</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🔗 <b>لینک اشتراک:</b>\n<code>{result.sub_link}</code>"
            ),
            parse_mode="HTML",
        )
        if result.qr_bytes:
            await bot.send_photo(
                chat_id=telegram_id,
                photo=BufferedInputFile(result.qr_bytes, "sub_qr.png"),
                caption="📷 QR کد اشتراک شما",
            )
        logger.success(f"manual_sub: اشتراک برای {telegram_id} ارسال شد")
    except Exception as e:
        logger.warning(f"manual_sub: ارسال به {telegram_id} ناموفق: {e}")


# ── helpers برای 3X-UI ──────────────────────────────────────────

def _xui_client():
    """XUIClient context manager با تنظیمات از config."""
    from services.xui_api import XUIClient
    return XUIClient(
        panel_url=settings.panel_url,
        username=settings.panel_username,
        password=settings.panel_password,
        api_path=settings.panel_api_path,
        sub_port=settings.sub_port,
    )


async def _get_sub_email(sub_id: int) -> tuple[str, str]:
    """email و status اشتراک رو از DB برمی‌گردونه."""
    from database.models import Subscription
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            raise ValueError(f"اشتراک {sub_id} پیدا نشد")
        return sub.email, sub.status


# ── مدیریت اشتراک ────────────────────────────────────────────────

async def _sub_toggle(payload: dict) -> None:
    """فعال/غیرفعال کردن اشتراک — XUI + DB."""
    sub_id = int(payload.get("sub_id", 0))
    if not sub_id:
        return
    email, current_status = await _get_sub_email(sub_id)
    new_enable = (current_status != "active")
    new_status = "active" if new_enable else "disabled"

    async with _xui_client() as xui:
        await xui.update_client(email=email, enable=new_enable)

    from database.crud import update_subscription_status
    async with AsyncSessionLocal() as session:
        await update_subscription_status(session, sub_id, new_status)
    logger.success(f"sub_toggle: اشتراک {sub_id} → {new_status}")


async def _sub_reset(payload: dict) -> None:
    """ریست ترافیک اشتراک — XUI + DB."""
    sub_id = int(payload.get("sub_id", 0))
    if not sub_id:
        return
    email, _ = await _get_sub_email(sub_id)

    async with _xui_client() as xui:
        await xui._request("POST", f"/clients/resetTraffic/{email}")

    from database.crud import update_subscription_traffic
    async with AsyncSessionLocal() as session:
        await update_subscription_traffic(session, sub_id, 0)
    logger.success(f"sub_reset: ترافیک اشتراک {sub_id} ریست شد")


async def _sub_delete(payload: dict) -> None:
    """حذف اشتراک — XUI + DB."""
    sub_id = int(payload.get("sub_id", 0))
    if not sub_id:
        return
    email, _ = await _get_sub_email(sub_id)

    async with _xui_client() as xui:
        await xui.delete_client(email)

    from database.crud import update_subscription_status
    async with AsyncSessionLocal() as session:
        await update_subscription_status(session, sub_id, "deleted")
    logger.success(f"sub_delete: اشتراک {sub_id} حذف شد")


async def _sub_edit(payload: dict) -> None:
    """ویرایش اشتراک از وب‌پنل — فیلدهای days / traffic / email را در XUI + DB اعمال می‌کند."""
    from database.models import Subscription
    from sqlalchemy import select, update as sa_update
    from datetime import datetime, timezone, timedelta

    sub_id = int(payload.get("sub_id", 0))
    field  = str(payload.get("field", ""))
    value  = str(payload.get("value", "")).strip()
    if not sub_id or not field or not value:
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
    if not sub:
        logger.error(f"sub_edit: اشتراک {sub_id} پیدا نشد")
        return

    async with _xui_client() as xui:
        if field == "days":
            delta = int(value)
            base = sub.expiry_date or datetime.now(timezone.utc)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            new_exp = base + timedelta(days=delta)
            days_from_now = max(1, (new_exp - datetime.now(timezone.utc)).days)
            await xui.update_client(email=sub.email, traffic_gb=sub.traffic_limit_gb, expire_days=days_from_now)
            async with AsyncSessionLocal() as session:
                await session.execute(sa_update(Subscription).where(Subscription.id == sub_id).values(expiry_date=new_exp))
                await session.commit()
            logger.success(f"sub_edit days: {sub_id} ({delta:+d} روز)")

        elif field == "traffic":
            new_gb = int(value)
            await xui.update_client(email=sub.email, traffic_gb=new_gb, expire_days=0)
            async with AsyncSessionLocal() as session:
                await session.execute(sa_update(Subscription).where(Subscription.id == sub_id).values(traffic_limit_gb=new_gb))
                await session.commit()
            logger.success(f"sub_edit traffic: {sub_id} → {new_gb} GB")

        elif field == "email":
            new_email = value
            await xui._request("POST", f"/clients/update/{sub.email}",
                                json={"email": new_email, "totalGB": (sub.traffic_limit_gb or 0) * 1024**3, "enable": True})
            async with AsyncSessionLocal() as session:
                await session.execute(sa_update(Subscription).where(Subscription.id == sub_id).values(email=new_email))
                await session.commit()
            logger.success(f"sub_edit email: {sub_id} → {new_email}")


# ── مدیریت تیکت ──────────────────────────────────────────────────

async def _ticket_reply(bot: Bot, payload: dict) -> None:
    """پاسخ ادمین به تیکت — DB + notify کاربر."""
    ticket_id   = int(payload.get("ticket_id", 0))
    message_txt = str(payload.get("message", "")).strip()
    if not ticket_id or not message_txt:
        return

    from database.crud import reply_to_ticket, fetch_ticket_detail
    async with AsyncSessionLocal() as session:
        # admin_user_id=0 چون از پنل وب هست (user خاصی نداره)
        # از یه کاربر ادمین موجود استفاده می‌کنیم
        from database.models import User
        from sqlalchemy import select
        admin_res = await session.execute(
            select(User).where(User.is_admin == True).limit(1)
        )
        admin_user = admin_res.scalar_one_or_none()
        admin_uid = admin_user.id if admin_user else 0

        await reply_to_ticket(session, ticket_id, admin_uid, message_txt, is_admin=True)

        # دریافت telegram_id کاربر صاحب تیکت
        from database.models import Ticket
        t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = t_res.scalar_one_or_none()
        if not ticket:
            return
        u_res = await session.execute(select(User).where(User.id == ticket.user_id))
        owner = u_res.scalar_one_or_none()

    if owner and owner.telegram_id:
        try:
            await bot.send_message(
                chat_id=owner.telegram_id,
                text=(
                    f"📬 <b>پاسخ جدید به تیکت شما</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 موضوع: {ticket.subject}\n\n"
                    f"💬 پاسخ ادمین:\n{message_txt}\n\n"
                    f"برای پاسخ دادن وارد ربات شوید."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"ticket_reply notify: {e}")
    logger.success(f"ticket_reply: پاسخ به تیکت {ticket_id} ثبت شد")


async def _ticket_close(bot: Bot, payload: dict) -> None:
    """بستن تیکت — DB + notify کاربر."""
    ticket_id = int(payload.get("ticket_id", 0))
    if not ticket_id:
        return

    from database.models import Ticket, User
    from sqlalchemy import select
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as session:
        t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = t_res.scalar_one_or_none()
        if not ticket:
            return
        ticket.status = "closed"
        ticket.closed_at = datetime.now(timezone.utc)
        ticket.updated_at = datetime.now(timezone.utc)
        await session.commit()
        u_res = await session.execute(select(User).where(User.id == ticket.user_id))
        owner = u_res.scalar_one_or_none()

    if owner and owner.telegram_id:
        try:
            await bot.send_message(
                chat_id=owner.telegram_id,
                text=(
                    f"🔒 <b>تیکت شما بسته شد</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 موضوع: {ticket.subject}\n\n"
                    f"اگر به کمک بیشتری نیاز دارید، تیکت جدید ارسال کنید."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"ticket_close notify: {e}")
    logger.success(f"ticket_close: تیکت {ticket_id} بسته شد")


async def _ticket_reopen(bot: Bot, payload: dict) -> None:
    """باز کردن تیکت بسته — DB + notify کاربر."""
    ticket_id = int(payload.get("ticket_id", 0))
    if not ticket_id:
        return

    from database.models import Ticket, User
    from sqlalchemy import select
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as session:
        t_res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
        ticket = t_res.scalar_one_or_none()
        if not ticket:
            return
        ticket.status = "open"
        ticket.updated_at = datetime.now(timezone.utc)
        await session.commit()
        u_res = await session.execute(select(User).where(User.id == ticket.user_id))
        owner = u_res.scalar_one_or_none()

    if owner and owner.telegram_id:
        try:
            await bot.send_message(
                chat_id=owner.telegram_id,
                text=(
                    f"🔓 <b>تیکت شما دوباره باز شد</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📋 موضوع: {ticket.subject}\n\n"
                    f"می‌توانید ادامه مکالمه را از ربات دنبال کنید."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"ticket_reopen notify: {e}")
    logger.success(f"ticket_reopen: تیکت {ticket_id} باز شد")


async def _build_subscription(bot: Bot, order_id: str, telegram_id: int) -> None:
    """اشتراک VPN می‌سازد و به کاربر ارسال می‌کند."""
    async with AsyncSessionLocal() as session:
        payment = await get_payment_by_order_id(session, order_id)
        if not payment:
            logger.warning(f"_build_subscription: payment {order_id} not found")
            return
        if getattr(payment, "subscription_id", None):
            return  # قبلاً ساخته شده
        from database.models import User
        from sqlalchemy import select
        res = await session.execute(select(User).where(User.id == payment.user_id))
        user = res.scalar_one_or_none()
        if not user:
            logger.error(f"_build_subscription: user not found for {order_id}")
            return
        tg_id = user.telegram_id
        plan_id = getattr(payment, "inbound_id", 0) or 0
        try:
            result = await create_new_subscription(
                session=session,
                user_id=user.id,
                telegram_id=tg_id,
                inbound_id=0,
                plan_id=plan_id,
                username=user.username,
            )
            await update_payment_status(session, payment.id, "confirmed", result.subscription.id)
            logger.success(f"اشتراک {result.subscription.id} برای {order_id} ساخته شد")
        except Exception as e:
            logger.error(f"خطا در ساخت اشتراک {order_id}: {e}")
            if telegram_id:
                try:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=(
                            f"⚠️ <b>پرداخت تأیید شد ولی ساخت اشتراک با مشکل مواجه شد.</b>\n"
                            f"🔖 کد پیگیری: <code>{order_id}</code>\n\n"
                            f"لطفاً با پشتیبانی تماس بگیرید."
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return
    try:
        await bot.send_message(
            chat_id=tg_id,
            text=(
                f"🎉 <b>اشتراک VPN شما آماده شد!</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🔖 سفارش: <code>{order_id}</code>\n\n"
                f"🔗 <b>لینک اشتراک:</b>\n<code>{result.sub_link}</code>"
            ),
            parse_mode="HTML",
        )
        if result.qr_bytes:
            await bot.send_photo(
                chat_id=tg_id,
                photo=BufferedInputFile(result.qr_bytes, "sub_qr.png"),
                caption="📷 QR کد اشتراک شما",
            )
        logger.success(f"اشتراک برای کاربر {tg_id} ارسال شد")
    except Exception as e:
        logger.warning(f"ارسال اشتراک به {tg_id} ناموفق: {e}")


async def _bot_broadcast(bot: Bot, payload: dict) -> None:
    """
    پخش پیام به همه کاربران از طریق وب‌پنل.
    payload: { message: str, photo_file_id?: str }
    ربات از DB لیست telegram_id همه کاربران را می‌خواند و با throttle ارسال می‌کند.
    """
    import asyncio as _asyncio
    message_txt  = str(payload.get("message", "")).strip()
    photo_file_id = str(payload.get("photo_file_id", "")).strip() or None

    if not message_txt and not photo_file_id:
        logger.warning("bot_broadcast: پیام یا photo_file_id خالی است — رد شد")
        return

    # خواندن همه telegram_id ها از DB
    db_path = _get_db_path()
    if not db_path:
        logger.error("bot_broadcast: DB path پیدا نشد")
        return

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT telegram_id FROM users ORDER BY id ASC").fetchall()
    except Exception as e:
        logger.error(f"bot_broadcast: خطا در خواندن کاربران: {e}")
        conn.close()
        return
    conn.close()

    user_ids = [int(r["telegram_id"]) for r in rows if r["telegram_id"]]
    sent = 0
    failed = 0
    logger.info(f"bot_broadcast: شروع ارسال به {len(user_ids)} کاربر")

    for uid in user_ids:
        try:
            if photo_file_id:
                await bot.send_photo(
                    chat_id=uid,
                    photo=photo_file_id,
                    caption=message_txt or None,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=message_txt,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            sent += 1
        except Exception as e:
            logger.debug(f"bot_broadcast به {uid} ناموفق: {e}")
            failed += 1
        # throttle: ۳۰ پیام در ثانیه محدودیت تلگرام
        await _asyncio.sleep(0.04)

    logger.success(f"bot_broadcast تمام شد: {sent} موفق / {failed} ناموفق از {len(user_ids)}")

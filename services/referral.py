"""
services/referral.py — منطق سیستم دعوت و referral

کلیدهای admin_settings:
  referral_enabled           : "1" | "0" (پیش‌فرض: "1")
  referral_reward_type       : "custom" | "plan"  (پیش‌فرض: custom)
  referral_custom_traffic_gb : حجم کانفیگ دلخواه (GB)
  referral_custom_days       : مدت کانفیگ دلخواه (روز)
  referral_inbound_id        : اینباند کانفیگ دلخواه (0 = خودکار)
  referral_reward_plan_id    : شناسه پلن (وقتی type=plan)
  referral_reward_days       : تعداد روز (پشتیبانی backward compat)
  referral_trigger           : on_register | on_first_purchase | on_every_purchase
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.crud import (
    create_referral,
    get_referral_stats,
    get_user_by_referral_code,
    get_user_by_telegram_id,
    mark_referral_rewarded,
    set_referral_code,
)
from database.models import User


# ── کلیدهای تنظیمات ────────────────────────────────────────────
REFERRAL_ENABLED_KEY           = "referral_enabled"
REFERRAL_REWARD_TYPE_KEY       = "referral_reward_type"
REFERRAL_CUSTOM_TRAFFIC_KEY    = "referral_custom_traffic_gb"
REFERRAL_CUSTOM_DAYS_KEY       = "referral_custom_days"
REFERRAL_INBOUND_ID_KEY        = "referral_inbound_id"
REFERRAL_REWARD_PLAN_ID_KEY    = "referral_reward_plan_id"
REFERRAL_REWARD_DAYS_KEY       = "referral_reward_days"   # backward compat
REFERRAL_TRIGGER_KEY           = "referral_trigger"

DEFAULT_REWARD_TYPE    = "custom"
DEFAULT_CUSTOM_TRAFFIC = 5.0
DEFAULT_CUSTOM_DAYS    = 30
DEFAULT_REWARD_DAYS    = 3
DEFAULT_TRIGGER        = "on_first_purchase"


# ── خواندن تنظیمات ──────────────────────────────────────────────

async def get_referral_settings() -> dict:
    """همه تنظیمات referral را از DB می‌خواند."""
    try:
        from database import AsyncSessionLocal
        from database.crud import get_setting
        async with AsyncSessionLocal() as s:
            enabled_raw    = await get_setting(s, REFERRAL_ENABLED_KEY,        "1")
            reward_type    = await get_setting(s, REFERRAL_REWARD_TYPE_KEY,    DEFAULT_REWARD_TYPE)
            traffic_raw    = await get_setting(s, REFERRAL_CUSTOM_TRAFFIC_KEY, str(DEFAULT_CUSTOM_TRAFFIC))
            days_raw       = await get_setting(s, REFERRAL_CUSTOM_DAYS_KEY,    str(DEFAULT_CUSTOM_DAYS))
            inbound_raw    = await get_setting(s, REFERRAL_INBOUND_ID_KEY,     "0")
            plan_id_raw    = await get_setting(s, REFERRAL_REWARD_PLAN_ID_KEY, "0")
            old_days_raw   = await get_setting(s, REFERRAL_REWARD_DAYS_KEY,    str(DEFAULT_REWARD_DAYS))
            trigger        = await get_setting(s, REFERRAL_TRIGGER_KEY,        DEFAULT_TRIGGER)

        enabled = enabled_raw not in ("0", "false", "")

        try:
            custom_traffic = float(traffic_raw) if traffic_raw else DEFAULT_CUSTOM_TRAFFIC
            if custom_traffic <= 0:
                custom_traffic = DEFAULT_CUSTOM_TRAFFIC
        except Exception:
            custom_traffic = DEFAULT_CUSTOM_TRAFFIC

        try:
            custom_days = int(days_raw) if days_raw.isdigit() and int(days_raw) > 0 else DEFAULT_CUSTOM_DAYS
        except Exception:
            custom_days = DEFAULT_CUSTOM_DAYS

        try:
            inbound_id = int(inbound_raw) if inbound_raw.isdigit() else 0
        except Exception:
            inbound_id = 0

        try:
            reward_plan_id = int(plan_id_raw) if plan_id_raw.isdigit() else 0
        except Exception:
            reward_plan_id = 0

        try:
            reward_days = int(old_days_raw) if old_days_raw.isdigit() and int(old_days_raw) > 0 else DEFAULT_REWARD_DAYS
        except Exception:
            reward_days = DEFAULT_REWARD_DAYS

        if reward_type not in ("custom", "plan", "days"):
            reward_type = DEFAULT_REWARD_TYPE
        if trigger not in ("on_register", "on_first_purchase", "on_every_purchase"):
            trigger = DEFAULT_TRIGGER

        return {
            "enabled":        enabled,
            "reward_type":    reward_type,
            "custom_traffic": custom_traffic,
            "custom_days":    custom_days,
            "inbound_id":     inbound_id,
            "reward_plan_id": reward_plan_id,
            "reward_days":    reward_days,   # backward compat
            "trigger":        trigger,
        }
    except Exception:
        return {
            "enabled":        True,
            "reward_type":    DEFAULT_REWARD_TYPE,
            "custom_traffic": DEFAULT_CUSTOM_TRAFFIC,
            "custom_days":    DEFAULT_CUSTOM_DAYS,
            "inbound_id":     0,
            "reward_plan_id": 0,
            "reward_days":    DEFAULT_REWARD_DAYS,
            "trigger":        DEFAULT_TRIGGER,
        }


async def get_reward_days() -> int:
    """backward compat — تعداد روز."""
    cfg = await get_referral_settings()
    return cfg["reward_days"]


# ── اعمال پاداش ──────────────────────────────────────────────────

async def apply_referral_reward(
    referred_user_id: int,
    bot,
    trigger: str,
) -> None:
    """
    بعد از یک رویداد (ثبت‌نام / اولین خرید / هر خرید)،
    اگر این کاربر از referral آمده و trigger مطابقت دارد،
    پاداش به دعوت‌کننده داده می‌شود.
    """
    from database import AsyncSessionLocal
    from database.models import Referral, User as UserModel
    from sqlalchemy import select

    cfg = await get_referral_settings()

    if not cfg["enabled"]:
        return

    configured_trigger = cfg["trigger"]

    if configured_trigger == "on_register" and trigger != "on_register":
        return
    if configured_trigger == "on_first_purchase" and trigger != "on_first_purchase":
        return

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Referral).where(Referral.referred_id == referred_user_id)
        )
        referral = res.scalar_one_or_none()

        if not referral:
            return

        if configured_trigger in ("on_register", "on_first_purchase") and referral.reward_granted:
            return

        referrer_res = await session.execute(
            select(UserModel).where(UserModel.id == referral.referrer_id)
        )
        referrer = referrer_res.scalar_one_or_none()
        if not referrer:
            return

        referred_res = await session.execute(
            select(UserModel).where(UserModel.id == referred_user_id)
        )
        referred = referred_res.scalar_one_or_none()
        referral_id = referral.id
        referred_name = (
            (referred.first_name if referred else None)
            or (referred.username if referred else None)
            or f"#{referred_user_id}"
        )

    # اعمال پاداش
    reward_type = cfg["reward_type"]

    # "days" backward compat → custom
    if reward_type == "days":
        reward_type = "custom"

    if reward_type == "plan":
        await _give_plan_reward(
            referrer=referrer,
            referral_id=referral_id,
            plan_id=cfg["reward_plan_id"],
            referred_name=referred_name,
            bot=bot,
        )
    else:
        await _give_custom_reward(
            referrer=referrer,
            referral_id=referral_id,
            traffic_gb=cfg["custom_traffic"],
            expire_days=cfg["custom_days"],
            inbound_id=cfg["inbound_id"],
            referred_name=referred_name,
            bot=bot,
        )


async def _give_custom_reward(
    referrer,
    referral_id: int,
    traffic_gb: float,
    expire_days: int,
    inbound_id: int,
    referred_name: str,
    bot,
) -> None:
    """پاداش کانفیگ دلخواه: ساخت اشتراک جدید با مشخصات تعیین‌شده."""
    from database import AsyncSessionLocal
    from database.models import Referral
    from sqlalchemy import update as sa_update

    try:
        from services.subscription import create_new_subscription
        async with AsyncSessionLocal() as session:
            result = await create_new_subscription(
                session=session,
                user_id=referrer.id,
                telegram_id=referrer.telegram_id,
                inbound_id=inbound_id or 0,
                traffic_gb=traffic_gb,
                expire_days=expire_days,
                is_gift=True,
                username=referrer.username,
            )
            await session.execute(
                sa_update(Referral).where(Referral.id == referral_id).values(reward_granted=True)
            )
            await session.commit()

        sub_link  = result.sub_link
        qr_bytes  = result.qr_bytes
        sub_email = result.email

    except Exception as e:
        logger.error(f"referral custom reward: خطا در ساخت اشتراک برای {referrer.id}: {e}")
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_update(Referral).where(Referral.id == referral_id).values(reward_granted=True)
            )
            await session.commit()
        try:
            await bot.send_message(
                chat_id=referrer.telegram_id,
                text=(
                    f"🎁 <b>پاداش دعوت در انتظار!</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"👤 <b>{referred_name}</b> خرید کرد.\n\n"
                    f"⚠️ در ساخت کانفیگ پاداش مشکلی رخ داد.\n"
                    f"لطفاً با پشتیبانی تماس بگیرید."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    logger.success(
        f"referral custom reward: {traffic_gb}GB/{expire_days}d "
        f"به user_id={referrer.id} (referral #{referral_id}) داده شد"
    )

    try:
        from aiogram.types import BufferedInputFile
        traffic_label = f"{traffic_gb:g} GB" if traffic_gb >= 1 else f"{traffic_gb * 1024:.0f} MB"
        caption = (
            f"🎁 <b>پاداش دعوت — کانفیگ رایگان!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 <b>{referred_name}</b> اولین خرید خود را انجام داد.\n\n"
            f"🎉 کانفیگ پاداش برای شما فعال شد!\n"
            f"📦 حجم: <b>{traffic_label}</b> | ⏳ مدت: <b>{expire_days} روز</b>\n\n"
            f"🔗 <b>لینک اشتراک:</b>\n<code>{sub_link}</code>"
        )
        if qr_bytes:
            await bot.send_photo(
                chat_id=referrer.telegram_id,
                photo=BufferedInputFile(qr_bytes, "reward_qr.png"),
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=referrer.telegram_id,
                text=caption,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning(f"referral custom reward notify to {referrer.telegram_id}: {e}")


async def _give_plan_reward(
    referrer,
    referral_id: int,
    plan_id: int,
    referred_name: str,
    bot,
) -> None:
    """پاداش پلن: یک اشتراک رایگان از پلن انتخاب‌شده به دعوت‌کننده می‌دهد."""
    from database import AsyncSessionLocal
    from database.crud import get_plan
    from database.models import Referral
    from sqlalchemy import update as sa_update

    if not plan_id:
        logger.warning("referral plan reward: plan_id تنظیم نشده — reward رد شد")
        return

    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        if not plan:
            logger.error(f"referral plan reward: پلن {plan_id} پیدا نشد")
            return

        plan_name    = plan.name
        plan_traffic = plan.traffic_gb
        plan_days    = plan.duration_days

        try:
            from services.subscription import create_new_subscription
            result = await create_new_subscription(
                session=session,
                user_id=referrer.id,
                telegram_id=referrer.telegram_id,
                inbound_id=0,
                traffic_gb=float(plan_traffic) if plan_traffic else 0,
                expire_days=plan_days or 30,
                is_gift=True,
                plan_id=plan_id,
                username=referrer.username,
            )
            sub_link = result.sub_link
            qr_bytes = result.qr_bytes
        except Exception as e:
            logger.error(f"referral plan reward: خطا در ساخت اشتراک برای {referrer.id}: {e}")
            await session.execute(
                sa_update(Referral).where(Referral.id == referral_id).values(reward_granted=True)
            )
            await session.commit()
            try:
                await bot.send_message(
                    chat_id=referrer.telegram_id,
                    text=(
                        f"🎁 <b>پاداش دعوت در انتظار!</b>\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"👤 <b>{referred_name}</b> خرید کرد.\n\n"
                        f"⚠️ در ساخت اشتراک پاداش مشکلی رخ داد.\n"
                        f"لطفاً با پشتیبانی تماس بگیرید."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        await session.execute(
            sa_update(Referral).where(Referral.id == referral_id).values(reward_granted=True)
        )
        await session.commit()

    logger.success(
        f"referral plan reward: پلن '{plan_name}' به user_id={referrer.id} داده شد"
    )

    try:
        from aiogram.types import BufferedInputFile
        caption = (
            f"🎁 <b>پاداش دعوت — اشتراک رایگان!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 <b>{referred_name}</b> اولین خرید خود را انجام داد.\n\n"
            f"🎉 پلن <b>{plan_name}</b> به عنوان پاداش برای شما فعال شد!\n"
            f"📦 حجم: <b>{plan_traffic} GB</b> | ⏳ مدت: <b>{plan_days} روز</b>\n\n"
            f"🔗 <b>لینک اشتراک:</b>\n<code>{sub_link}</code>"
        )
        if qr_bytes:
            await bot.send_photo(
                chat_id=referrer.telegram_id,
                photo=BufferedInputFile(qr_bytes, "reward_qr.png"),
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=referrer.telegram_id,
                text=caption,
                parse_mode="HTML",
            )
    except Exception as e:
        logger.warning(f"referral plan reward notify to {referrer.telegram_id}: {e}")


# ── آمار و کدها ─────────────────────────────────────────────────

@dataclass
class ReferralStats:
    total_referrals: int
    rewarded_referrals: int
    total_reward_days: int
    referral_link: str
    referral_code: str


async def generate_referral_code(length: int = 8) -> str:
    return uuid.uuid4().hex[:length].upper()


async def get_or_create_referral_code(
    session: AsyncSession,
    user: User,
) -> str:
    if user.referral_code:
        return user.referral_code

    for _ in range(5):
        code = await generate_referral_code()
        existing = await get_user_by_referral_code(session, code)
        if not existing:
            await set_referral_code(session, user.id, code)
            return code

    code = f"U{user.id:06d}"
    await set_referral_code(session, user.id, code)
    return code


def build_referral_link(bot_username: str, referral_code: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{referral_code}"


async def process_referral(
    session: AsyncSession,
    new_user: User,
    referral_code: str,
    bot_username: str = "",
    bot=None,
) -> Optional[str]:
    if new_user.referred_by:
        return None

    referrer = await get_user_by_referral_code(session, referral_code)
    if not referrer:
        return None

    if referrer.id == new_user.id:
        return None

    cfg = await get_referral_settings()
    await create_referral(
        session=session,
        referrer_id=referrer.id,
        referred_id=new_user.id,
        reward_days=cfg["reward_days"],
    )

    referrer_name = referrer.first_name or referrer.username or f"#{referrer.id}"

    if bot and cfg["trigger"] == "on_register":
        try:
            await apply_referral_reward(
                referred_user_id=new_user.id,
                bot=bot,
                trigger="on_register",
            )
        except Exception as e:
            logger.warning(f"apply_referral_reward on_register: {e}")

    return referrer_name


async def after_purchase_referral_hook(
    user_id: int,
    bot,
) -> None:
    """
    بعد از هر خرید موفق فراخوانی می‌شود.
    """
    from database import AsyncSessionLocal
    from database.crud import get_confirmed_payment_count

    cfg = await get_referral_settings()
    if not cfg["enabled"]:
        return

    trigger = cfg["trigger"]

    if trigger == "on_register":
        return

    if trigger == "on_first_purchase":
        async with AsyncSessionLocal() as session:
            count = await get_confirmed_payment_count(session, user_id)
        if count > 1:
            return
        await apply_referral_reward(
            referred_user_id=user_id,
            bot=bot,
            trigger="on_first_purchase",
        )

    elif trigger == "on_every_purchase":
        await apply_referral_reward(
            referred_user_id=user_id,
            bot=bot,
            trigger="on_every_purchase",
        )


async def get_user_referral_stats(
    session: AsyncSession,
    user: User,
    bot_username: str,
) -> ReferralStats:
    code  = await get_or_create_referral_code(session, user)
    link  = build_referral_link(bot_username, code)
    stats = await get_referral_stats(session, user.id)

    return ReferralStats(
        total_referrals=stats["total_referrals"],
        rewarded_referrals=stats["rewarded_referrals"],
        total_reward_days=stats["total_reward_days"],
        referral_link=link,
        referral_code=code,
    )

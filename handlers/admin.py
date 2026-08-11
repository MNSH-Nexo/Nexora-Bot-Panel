"""
handlers/admin.py — پنل مدیریت کامل ادمین

ورود: /admin_secret <رمز>  یا  /admin <رمز>
منو: مدیریت پلن‌ها، اینباندها، کدهای تخفیف، آمار، کاربران، سرور
"""

from __future__ import annotations

from datetime import datetime, timezone


def _fmt_usdt(price: float) -> str:
    """
    نمایش هوشمند قیمت دلاری — بدون صفر اضافه، بدون برش اشتباه.
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


def _fmt_expiry(expiry_date) -> str:
    """نمایش تاریخ انقضا + روزهای باقی‌مانده.

    مثال خروجی:
      2026-07-14  (۱۱ روز مانده)
      2026-07-01  (منقضی شده)
      نامحدود
    """
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


from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from database.crud import (
    create_discount_code, create_plan, delete_discount_code, delete_plan,
    get_all_discount_codes, get_all_plans, get_discount_code, get_plan,
    get_stats, get_user_by_telegram_id, get_user_by_username, update_plan,
    update_subscription_status, get_user_subscriptions, get_pending_payments,
    get_or_create_user, get_enabled_inbound_ids, toggle_inbound_enabled,
    get_all_db_admins, revoke_admin, is_admin_login_locked, set_admin_login_lock,
    ban_user, unban_user,
)
from database.models import Subscription, User
from keyboards.admin import (
    get_admin_main_keyboard, get_admin_users_keyboard,
    get_discounts_keyboard, get_discount_detail_keyboard, get_discount_delete_confirm_keyboard,
    get_inbounds_keyboard, get_plan_edit_keyboard, get_plans_manage_keyboard,
    get_backup_keyboard, get_user_detail_keyboard, get_banner_keyboard,
    get_plan_quick_actions_keyboard, get_payment_methods_keyboard,
    get_security_keyboard, get_plan_inbounds_keyboard,
    get_welcome_banner_keyboard, get_join_channel_keyboard,
    get_user_subs_keyboard, get_sub_manage_keyboard, get_sub_del_confirm_keyboard,
    get_transactions_keyboard, get_payments_filter_keyboard,
    get_admin_management_keyboard, get_admin_list_keyboard,
    get_admin_detail_keyboard, get_admin_revoke_confirm_keyboard,
    get_admin_lock_confirm_keyboard,
    get_user_ban_confirm_keyboard,
    get_referral_settings_keyboard,
    get_referral_plan_select_keyboard,
)
from keyboards.tickets import get_admin_ticket_keyboard
from services.xui_api import XUIClient, XUIError
from services.backup import backup_bot_db, backup_panel_db
from services.banner import get_banner_file_id, set_banner_file_id, clear_banner
from services.card_payment import get_card_info, set_card_info, set_usdt_rate
from services.payment_methods import (
   get_payment_status, set_crypto_enabled, set_card_enabled,
   is_crypto_invoice, set_crypto_invoice,
   get_crypto_gateway, set_crypto_gateway,
   get_price_display_mode, set_price_display_mode,
)

router = Router(name="admin")


# ──────────────────────────────────────────────
# FSM States
# ──────────────────────────────────────────────

class CardSettingStates(StatesGroup):
    waiting_card_number = State()
    waiting_card_holder = State()
    waiting_rate        = State()


class RestoreStates(StatesGroup):
    waiting_db_file = State()


class BannerStates(StatesGroup):
    waiting_photo = State()


class WelcomeBannerStates(StatesGroup):
    waiting_photo   = State()
    waiting_caption = State()


class JoinChannelStates(StatesGroup):
    waiting_channel_id    = State()
    waiting_channel_link  = State()
    waiting_channel_title = State()


class SecurityStates(StatesGroup):
    waiting_new_command = State()  # انتظار برای دریافت دستور جدید ادمین


class PlanQuickEditStates(StatesGroup):
    """ویرایش سریع یک فیلد پلن با نگه‌داشتن plan_id و field."""
    waiting_value = State()


class UserSearchStates(StatesGroup):
    waiting_id = State()


class UserDirectMsgStates(StatesGroup):
    """ارسال پیام مستقیم از ادمین به کاربر از طریق ربات."""
    waiting_text = State()   # انتظار دریافت متن/عکس/فایل پیام


class SubEditStates(StatesGroup):
    """ویرایش اشتراک — منتظر مقدار جدید."""
    waiting_value = State()


class BroadcastNote(StatesGroup):
    # placeholder — broadcast اصلی در handlers/broadcast.py است
    pass


class PlanAddStates(StatesGroup):
    name = State()
    traffic = State()
    duration = State()
    price = State()
    limit_ip = State()
    inbounds = State()


class PlanEditStates(StatesGroup):
    waiting_value = State()


class DiscountAddStates(StatesGroup):
    code = State()
    percent = State()
    max_uses = State()


# ──────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────

ADMIN_CMD_KEY = "admin_login_command"
ADMIN_CMD_DEFAULT = "admin_secret"


async def _get_admin_command() -> str:
    """دستور ورود ادمین را از DB می‌خواند — پیش‌فرض: admin_secret."""
    from database.crud import get_setting
    async with AsyncSessionLocal() as session:
        return await get_setting(session, ADMIN_CMD_KEY, ADMIN_CMD_DEFAULT)


async def _set_admin_command(cmd: str) -> None:
    """دستور ورود ادمین را در DB ذخیره می‌کند."""
    from database.crud import set_setting
    async with AsyncSessionLocal() as session:
        await set_setting(session, ADMIN_CMD_KEY, cmd)


def _is_admin_by_settings(user_id: int) -> bool:
    return settings.is_admin(user_id)


async def _is_admin_user(uid: int) -> bool:
    """
    بررسی ادمین بودن — هر دو منبع را چک می‌کند:
      1. settings.admin_ids  (از .env) — اولویت اول، بدون DB
      2. جدول users.is_admin (از DB — برای کسانی که با /admin_secret وارد شدند)
    """
    # ── اولویت اول: .env ─────────────────────────────────
    # این چک بدون هیچ DB query است — حتی اگر DB مشکل داشت کار می‌کند
    if _is_admin_by_settings(uid):
        return True

    # ── اولویت دوم: جدول users در DB ────────────────────
    try:
        async with AsyncSessionLocal() as session:
            db_user = await get_user_by_telegram_id(session, uid)
            if db_user and db_user.is_admin:
                return True
    except Exception as e:
        # اگر DB مشکل داشت (مثلاً migration نشده)، فقط از .env استفاده می‌کنیم
        logger.warning(f"_is_admin_user: خطای DB برای uid={uid}: {e}")

    return False


async def _check_admin(event: Message | CallbackQuery) -> bool:
    """بررسی ادمین بودن و ارسال پیام خطا در صورت عدم دسترسی."""
    uid = event.from_user.id if event.from_user else 0
    if await _is_admin_user(uid):
        return True
    if isinstance(event, Message):
        await event.answer("🚫 دسترسی ندارید.")
    else:
        await event.answer("🚫 دسترسی ندارید.", show_alert=True)
    return False


def _xui_client() -> XUIClient:
    return XUIClient(
        panel_url=settings.panel_url,
        username=settings.panel_username,
        password=settings.panel_password,
        api_path=settings.panel_api_path,
        sub_port=settings.sub_port,
    )


# ──────────────────────────────────────────────
# ورود به حالت ادمین — دستور دینامیک
# ──────────────────────────────────────────────

async def _handle_admin_login(message: Message, provided_pass: str) -> None:
    """
    منطق مشترک ورود ادمین — از handler دینامیک صدا زده می‌شود.
    پیام ورودی را فوری حذف می‌کند تا رمز در چت نماند (قبل از هر بررسی).
    """
    user = message.from_user
    if not user:
        return

    # ── حذف پیام کاربر فوری — قبل از هر چیز دیگری ──────────
    # این اولین کار است تا رمز محرمانه در چت نماند
    deleted = False
    try:
        await message.delete()
        deleted = True
    except Exception:
        deleted = False  # بدون permission — ادامه می‌دهیم

    if not provided_pass:
        await message.answer(
            "⚠️ رمز را به همراه دستور وارد کنید.\n"
            "<i>مثال: /admin_secret رمز_شما</i>",
            parse_mode="HTML",
        )
        return

    # ── بررسی قفل ورود — قبل از هر چیز ──────────────────────
    # اگر قفل کامل فعال باشد، فقط ادمین اصلی (.env) می‌تواند وارد شود
    # ادمین اصلی از این محدودیت معاف است
    if not _is_main_admin(user.id):
        async with AsyncSessionLocal() as _lock_session:
            _locked = await is_admin_login_locked(_lock_session)
        if _locked:
            await message.answer(
                "🔒 <b>ورود به پنل ادمین قفل شده است.</b>\n\n"
                "در حال حاضر امکان ورود وجود ندارد.\n"
                "برای اطلاعات بیشتر با ادمین اصلی تماس بگیرید.",
                parse_mode="HTML",
            )
            logger.warning(
                f"تلاش ورود ادمین توسط {user.id} (@{user.username}) رد شد — قفل فعال است."
            )
            return

    # ── بررسی رمز ────────────────────────────────────────────
    # رمز مستقیماً از settings (که از .env خوانده شده) بررسی می‌شود
    stored_secret = settings.admin_secret

    if not stored_secret:
        await message.answer("⚠️ رمز ادمین تنظیم نشده — ADMIN_SECRET را در .env تنظیم کنید.")
        return

    if provided_pass != stored_secret:
        await message.answer("❌ رمز اشتباه است.")
        logger.warning(f"تلاش ناموفق ورود ادمین از {user.id} (@{user.username})")
        return

    # ── ورود موفق ───────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        db_user = await get_user_by_telegram_id(session, user.id)
        if db_user:
            if not db_user.is_admin:
                db_user.is_admin = True
                await session.commit()
                logger.success(f"کاربر {user.id} به ادمین تبدیل شد.")
        else:
            from database.crud import create_user
            await create_user(
                session, user.id, user.username, user.first_name, is_admin=True
            )
            await session.commit()
            logger.success(f"کاربر جدید {user.id} با دسترسی ادمین ایجاد شد.")

    from keyboards.main_menu import get_main_menu, get_main_menu_async
    await message.answer(
        "✅ <b>خوش آمدید ادمین عزیز!</b>\n\nشما وارد حالت مدیریت شدید.",
        parse_mode="HTML",
        reply_markup=await get_main_menu_async(is_admin=True),
    )
    await message.answer(
        "⚙️ <b>پنل مدیریت:</b>",
        parse_mode="HTML",
        reply_markup=get_admin_main_keyboard(),
    )
    if not deleted:
        logger.warning(
            f"ادمین {user.id}: پیام رمز حذف نشد (permission نداشت). "
            "برای امنیت بیشتر ربات را ادمین گروه کنید."
        )


async def handle_dynamic_admin_login_if_match(message: Message) -> bool:
    """
    بررسی می‌کند آیا پیام دستور ورود ادمین است.
    اگر بله، پردازش می‌کند و True برمی‌گرداند.
    اگر نه، False برمی‌گرداند تا handler بعدی امتحان شود.

    این تابع باید از user_router (آخرین router) صدا زده شود
    تا با /start و سایر دستورات تداخل نداشته باشد.
    """
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return False

    current_cmd = await _get_admin_command()
    parts = text.split(maxsplit=1)
    cmd_used = parts[0].lstrip("/").split("@")[0]

    if cmd_used != current_cmd:
        return False

    provided_pass = parts[1].strip() if len(parts) > 1 else ""
    await _handle_admin_login(message, provided_pass)
    return True


# ──────────────────────────────────────────────
# ⚙️ پنل مدیریت — از منوی اصلی
# ──────────────────────────────────────────────

@router.message(F.text.contains("پنل مدیریت"))
async def msg_admin_panel(message: Message) -> None:
    if not await _check_admin(message):
        return
    await message.answer("⚙️ *پنل مدیریت*", parse_mode="Markdown",
                         reply_markup=get_admin_main_keyboard())


async def _safe_edit_admin(callback: CallbackQuery, text: str, **kwargs) -> None:
    """edit_text امن برای ادمین — اگه پیام عکس‌دار بود answer می‌فرسته."""
    try:
        if callback.message.photo or callback.message.document:  # type: ignore
            await callback.message.answer(text, **kwargs)  # type: ignore
        else:
            await callback.message.edit_text(text, **kwargs)  # type: ignore
    except Exception:
        await callback.message.answer(text, **kwargs)  # type: ignore


@router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await _safe_edit_admin(
        callback,
        "⚙️ <b>پنل مدیریت</b>",
        parse_mode="HTML",
        reply_markup=get_admin_main_keyboard(),
    )


# ──────────────────────────────────────────────
# 📊 آمار
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer("⏳ در حال بارگذاری آمار...")
    async with AsyncSessionLocal() as session:
        stats = await get_stats(session)

    # نرخ تومان برای نمایش درآمد به تومان هم
    try:
        from services.card_payment import get_card_info
        card_info = await get_card_info()
        rate = card_info.get("rate", 0)
    except Exception:
        rate = 0

    revenue_toman = f"  (~{int(stats['total_revenue_usdt'] * rate):,} تومان)" if rate else ""
    revenue_today_toman = f"  (~{int(stats['revenue_today_usdt'] * rate):,} تومان)" if rate else ""

    # وضعیت تراکنش‌های در صف برای نمایش برجسته
    pending_alert = f"  🔴 نیاز به بررسی!" if stats['payments_pending'] > 0 else ""

    text = (
        "📊 <b>آمار کامل ربات</b>\n"
        f"🕐 <i>{datetime.now(timezone.utc).strftime('%Y-%m-%d  %H:%M UTC')}</i>\n"
        "━━━━━━━━━━━━━━━\n\n"

        "👥 <b>کاربران</b>\n"
        f"  • کل: <b>{stats['total_users']:,}</b>\n"
        f"  • عضو امروز: <b>{stats['users_today']:,}</b>\n\n"

        "📦 <b>اشتراک‌ها</b>\n"
        f"  • فعال: <b>{stats['active_subscriptions']:,}</b>\n"
        f"  • منقضی‌شده: <b>{stats['expired_subscriptions']:,}</b>\n"
        f"  • کل: <b>{stats['total_subscriptions']:,}</b>\n"
        f"  • انقضا در ۷ روز آینده: <b>{stats['expiring_soon']:,}</b> ⚠️\n\n"

        "💰 <b>مالی</b>\n"
        f"  • درآمد کل: <b>{stats['total_revenue_usdt']:.2f} USDT</b>{revenue_toman}\n"
        f"  • درآمد امروز: <b>{stats['revenue_today_usdt']:.2f} USDT</b>{revenue_today_toman}\n"
        f"  • تراکنش موفق: <b>{stats['payments_confirmed']:,}</b>\n"
        f"  • در صف بررسی: <b>{stats['payments_pending']:,}</b>{pending_alert}\n"
        f"  • ناموفق: <b>{stats['payments_failed']:,}</b>\n\n"

        "🎫 <b>تیکت‌ها</b>\n"
        f"  • باز: <b>{stats['open_tickets']:,}</b>\n"
        f"  • در حال بررسی: <b>{stats['inprogress_tickets']:,}</b>"
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 بروزرسانی", callback_data="adm_stats")
    kb.button(text="🧾 مدیریت تراکنش‌ها", callback_data="adm_pending_payments")
    kb.button(text="🔙 بازگشت", callback_data="adm_back")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ──────────────────────────────────────────────
# 📋 مدیریت پلن‌ها
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_plans")
async def cb_adm_plans(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    async with AsyncSessionLocal() as session:
        plans = await get_all_plans(session)
    await callback.message.edit_text(
        "📋 *مدیریت پلن‌ها*\nیک پلن را برای ویرایش انتخاب کنید یا پلن جدید اضافه کنید:",
        parse_mode="Markdown",
        reply_markup=get_plans_manage_keyboard(plans),
    )


@router.callback_query(F.data.startswith("adm_plan_view:"))
async def cb_adm_plan_view(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
    if not plan:
        await callback.answer("پلن پیدا نشد!", show_alert=True)
        return
    traffic = f"{plan.traffic_gb} GB" if plan.traffic_gb else "♾ نامحدود"
    ip_info = f"{plan.limit_ip} دستگاه" if plan.limit_ip else "نامحدود"
    inbounds_str = plan.inbound_ids or "تنظیم نشده"
    text = (
        f"📋 *پلن: {plan.name}*\n\n"
        f"📦 حجم: `{traffic}`\n"
        f"⏱ مدت: `{plan.duration_days}` روز\n"
        f"💲 قیمت: `{_fmt_usdt(plan.price_usdt)} USDT`\n"
        f"👤 حداکثر دستگاه: `{ip_info}`\n"
        f"🔌 اینباندها: `{inbounds_str}`\n"
        f"{'✅ فعال' if plan.is_active else '❌ غیرفعال'}"
    )
    await callback.message.edit_text(text, parse_mode="Markdown",
                                     reply_markup=get_plan_edit_keyboard(plan_id))


@router.callback_query(F.data.startswith("adm_plan_toggle:"))
async def cb_adm_plan_toggle(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        if plan:
            await update_plan(session, plan_id, is_active=not plan.is_active)
            status = "فعال ✅" if not plan.is_active else "غیرفعال ❌"
    await callback.answer(f"پلن {status} شد.", show_alert=True)
    # بازنمایی
    await cb_adm_plan_view(callback)


@router.callback_query(F.data.startswith("adm_plan_del:"))
async def cb_adm_plan_del(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        await delete_plan(session, plan_id)
    await callback.answer("🗑 پلن حذف شد.", show_alert=True)
    await cb_adm_plans(callback)


# ── ویرایش فیلد پلن ──────────────────────────

@router.callback_query(F.data.startswith("adm_plan_edit:"))
async def cb_adm_plan_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    _, field, plan_id_str = callback.data.split(":")
    plan_id = int(plan_id_str)

    field_names = {
        "name":     "نام پلن",
        "price":    "قیمت (USDT، مثلاً 5.5)",
        "traffic":  "حجم (GB، 0=نامحدود)",
        "days":     "مدت (روز)",
        "ip":       "حداکثر دستگاه هم‌زمان (0=نامحدود)",
        "inbounds": "اینباند اختصاصی این پلن",
    }
    field_hints = {
        "inbounds": (
            "شناسه اینباندهای اختصاصی این پلن را با ویرگول وارد کنید.\n"
            "مثال: <code>1,3,5</code>\n\n"
            "⚠️ توجه: این فیلد <b>فقط برای پیش‌فرض</b> است.\n"
            "در حال حاضر کانفیگ‌ها از بخش «🔌 اینباندها» در منوی پنل ادمین\n"
            "به صورت round-robin انتخاب می‌شوند — نه از اینجا.\n"
            "اگه می‌خوای کانفیگ‌ها فقط از اینباندهای خاصی ساخته بشن،\n"
            "از <b>پنل مدیریت ← 🔌 اینباندها</b> اقدام کن."
        ),
        "ip": "حداکثر دستگاهی که می‌تونه همزمان متصل بشه.\nمثال: <code>2</code> (دو دستگاه) | <code>0</code> (نامحدود)",
    }
    await state.set_state(PlanEditStates.waiting_value)
    await state.update_data(field=field, plan_id=plan_id)
    hint = field_hints.get(field, "")
    base = f"✏️ مقدار جدید برای <b>{field_names.get(field, field)}</b> را وارد کنید:\n"
    if hint:
        base += f"\n{hint}\n"
    base += "\nبرای لغو: /cancel"
    await callback.message.answer(base, parse_mode="HTML")


@router.message(PlanEditStates.waiting_value)
async def msg_plan_edit_value(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    field = data["field"]
    plan_id = data["plan_id"]
    val = (message.text or "").strip()

    field_map = {
        "name": ("name", str),
        "price": ("price_usdt", float),
        "traffic": ("traffic_gb", int),
        "days": ("duration_days", int),
        "ip": ("limit_ip", int),
        "inbounds": ("inbound_ids", str),
    }
    if field not in field_map:
        await message.answer("❌ فیلد نامعتبر.")
        await state.clear()
        return

    db_field, cast = field_map[field]
    try:
        casted = cast(val)
    except ValueError:
        await message.answer(f"❌ مقدار وارد‌شده معتبر نیست. نوع مورد انتظار: {cast.__name__}")
        return

    async with AsyncSessionLocal() as session:
        await update_plan(session, plan_id, **{db_field: casted})

    await state.clear()
    await message.answer(f"✅ پلن به‌روزرسانی شد.")


# ── مدیریت اینباندهای اختصاصی پلن ──────────

@router.callback_query(F.data.startswith("adm_plan_inbounds:"))
async def cb_adm_plan_inbounds(callback: CallbackQuery) -> None:
    """نمایش لیست اینباندها برای انتخاب اینباندهای اختصاصی یک پلن."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    plan_id = int(callback.data.split(":")[1])

    try:
        async with _xui_client() as xui:
            inbounds = await xui.get_inbounds()
    except XUIError as e:
        await callback.message.answer(f"❌ خطا در دریافت اینباندها از پنل: {e}")
        return

    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)

    if not plan:
        await callback.message.answer("❌ پلن پیدا نشد.")
        return

    plan_inbound_ids = plan.get_inbound_ids()
    selected_count = len([ib for ib in inbounds if ib.id in plan_inbound_ids])

    text = (
        f"🔌 <b>اینباندهای پلن: {plan.name}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"📊 {len(inbounds)} اینباند موجود | {selected_count} انتخاب‌شده\n\n"
        "روی هر اینباند کلیک کنید تا انتخاب/لغو انتخاب شود:\n"
        "✅ = انتخاب‌شده برای این پلن\n"
        "🟢 = فعال در پنل ولی انتخاب نشده\n"
        "❌ = غیرفعال در پنل\n\n"
        "⚠️ اگر هیچ اینباندی انتخاب نشود، از اینباندهای فعال عمومی استفاده می‌شود."
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_plan_inbounds_keyboard(plan_id, inbounds, plan_inbound_ids),
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=get_plan_inbounds_keyboard(plan_id, inbounds, plan_inbound_ids),
        )


@router.callback_query(F.data.startswith("adm_plan_inb_toggle:"))
async def cb_adm_plan_inb_toggle(callback: CallbackQuery) -> None:
    """toggle انتخاب/لغو انتخاب یک اینباند برای پلن خاص."""
    if not await _check_admin(callback):
        return
    parts = callback.data.split(":")
    plan_id = int(parts[1])
    inbound_id = int(parts[2])

    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        if not plan:
            await callback.answer("❌ پلن پیدا نشد.", show_alert=True)
            return

        current_ids = plan.get_inbound_ids()
        if inbound_id in current_ids:
            current_ids.remove(inbound_id)
            action_text = f"اینباند {inbound_id} از پلن حذف شد"
        else:
            current_ids.append(inbound_id)
            action_text = f"اینباند {inbound_id} به پلن اضافه شد"

        new_ids_str = ",".join(str(i) for i in sorted(current_ids))
        await update_plan(session, plan_id, inbound_ids=new_ids_str)
        plan = await get_plan(session, plan_id)

    await callback.answer(action_text, show_alert=False)

    # رفرش صفحه
    try:
        async with _xui_client() as xui:
            inbounds = await xui.get_inbounds()
    except XUIError:
        return

    plan_inbound_ids = plan.get_inbound_ids()
    selected_count = len([ib for ib in inbounds if ib.id in plan_inbound_ids])
    text = (
        f"🔌 <b>اینباندهای پلن: {plan.name}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"📊 {len(inbounds)} اینباند موجود | {selected_count} انتخاب‌شده\n\n"
        "روی هر اینباند کلیک کنید تا انتخاب/لغو انتخاب شود:\n"
        "✅ = انتخاب‌شده برای این پلن\n"
        "🟢 = فعال در پنل ولی انتخاب نشده\n"
        "❌ = غیرفعال در پنل\n\n"
        "⚠️ اگر هیچ اینباندی انتخاب نشود، از اینباندهای فعال عمومی استفاده می‌شود."
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_plan_inbounds_keyboard(plan_id, inbounds, plan_inbound_ids),
        )
    except Exception:
        pass


# ── افزودن پلن جدید ──────────────────────────

@router.callback_query(F.data == "adm_plan_add")
async def cb_adm_plan_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(PlanAddStates.name)
    await callback.message.answer("➕ *افزودن پلن جدید*\n\nنام پلن را وارد کنید (مثلاً: 10GB یک ماهه):",
                                  parse_mode="Markdown")


@router.message(PlanAddStates.name)
async def plan_add_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(PlanAddStates.traffic)
    await message.answer("📦 حجم ترافیک (GB):\n0 = نامحدود")


@router.message(PlanAddStates.traffic)
async def plan_add_traffic(message: Message, state: FSMContext) -> None:
    try:
        await state.update_data(traffic_gb=int(message.text.strip()))
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    await state.set_state(PlanAddStates.duration)
    await message.answer("⏱ مدت اشتراک (روز):")


@router.message(PlanAddStates.duration)
async def plan_add_duration(message: Message, state: FSMContext) -> None:
    try:
        await state.update_data(duration_days=int(message.text.strip()))
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    await state.set_state(PlanAddStates.price)
    # نمایش نرخ فعلی برای راهنمایی ادمین
    from services.card_payment import get_card_info
    card_info = await get_card_info()
    rate = card_info.get("rate", 0)
    if rate and rate > 0:
        await message.answer(
            f"💰 قیمت پلن را به <b>تومان</b> وارد کنید:\n"
            f"(نرخ فعلی: <code>{rate:,} تومان</code> = ۱ دلار)\n"
            f"مثال: <code>300000</code>\n\n"
            f"یا قیمت دلاری را با علامت $ وارد کنید:\n"
            f"مثال: <code>$5.5</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "💲 قیمت پلن را وارد کنید:\n"
            "• قیمت تومانی: مثلاً <code>300000</code>\n"
            "• قیمت دلاری: مثلاً <code>$5.5</code>\n\n"
            "⚠️ برای تبدیل خودکار تومان به دلار، ابتدا نرخ دلار را در تنظیمات کارت وارد کنید.",
            parse_mode="HTML",
        )


@router.message(PlanAddStates.price)
async def plan_add_price(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        if raw.startswith("$"):
            # قیمت دلاری مستقیم
            price_usdt = float(raw[1:].strip().replace(",", ""))
        else:
            # قیمت تومانی — تبدیل به دلار
            val = float(raw.replace(",", "").replace("٬", ""))
            from services.card_payment import get_card_info
            card_info = await get_card_info()
            rate = card_info.get("rate", 0)
            if rate and rate > 0:
                price_usdt = round(val / rate, 2)
            else:
                # اگر نرخ تنظیم نشده، عدد را مستقیم به عنوان دلار ذخیره کن
                price_usdt = val
                await message.answer(
                    "⚠️ نرخ دلار تنظیم نشده — قیمت به همین عدد به عنوان دلار ذخیره شد.\n"
                    "برای تبدیل خودکار، نرخ را در تنظیمات کارت به کارت وارد کنید.",
                    parse_mode="HTML",
                )
    except (ValueError, TypeError):
        await message.answer("❌ مقدار نامعتبر — لطفاً یک عدد وارد کنید.\nمثال: <code>300000</code> یا <code>$5.5</code>", parse_mode="HTML")
        return

    # نمایش تأیید قیمت
    from services.card_payment import get_card_info
    card_info = await get_card_info()
    rate = card_info.get("rate", 0)
    if rate and not raw.startswith("$"):
        toman_val = int(price_usdt * rate)
        confirm_msg = f"✅ قیمت: <b>{price_usdt:.2f} دلار</b>  (~{toman_val:,} تومان)"
    else:
        confirm_msg = f"✅ قیمت: <b>{price_usdt:.2f} دلار</b>"

    await state.update_data(price_usdt=price_usdt)
    await state.set_state(PlanAddStates.limit_ip)
    await message.answer(
        f"{confirm_msg}\n\n👤 حداکثر دستگاه هم‌زمان (0=نامحدود):",
        parse_mode="HTML",
    )


@router.message(PlanAddStates.limit_ip)
async def plan_add_limit_ip(message: Message, state: FSMContext) -> None:
    try:
        await state.update_data(limit_ip=int(message.text.strip()))
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    await state.set_state(PlanAddStates.inbounds)
    await message.answer("🔌 شناسه اینباندها با ویرگول (مثلاً 1,2,3)\nاگر نمی‌دانید عدد 0 بزنید:")


@router.message(PlanAddStates.inbounds)
async def plan_add_inbounds(message: Message, state: FSMContext) -> None:
    val = message.text.strip()
    if val == "0":
        val = ""
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        plan = await create_plan(
            session,
            name=data["name"],
            traffic_gb=data["traffic_gb"],
            duration_days=data["duration_days"],
            price_usdt=data["price_usdt"],
            limit_ip=data["limit_ip"],
            inbound_ids=val,
        )
    await state.clear()
    await message.answer(
        f"✅ *پلن «{plan.name}» ایجاد شد!*\n"
        f"شناسه: `{plan.id}`",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# 🔌 اینباندهای پنل
# ──────────────────────────────────────────────

async def _show_inbounds_page(target, inbounds, enabled_ids):
    """نمایش صفحه اینباندها (مشترک بین callback و refresh)."""
    enabled_count = sum(1 for ib in inbounds if ib.id in enabled_ids)
    text = (
        "🔌 <b>اینباندهای اشتراک تست</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"📊 تعداد کل: <b>{len(inbounds)}</b> | فعال برای تست: <b>{enabled_count}</b>\n\n"
        "اینباندهایی که اینجا ✅ هستند فقط برای <b>اشتراک تست رایگان</b> استفاده می‌شوند.\n"
        "برای تعیین اینباند پلن‌های خریداری‌شده، از صفحه ویرایش هر پلن اقدام کنید.\n\n"
        "✅ = انتخاب‌شده برای اشتراک تست\n"
        "🟢 = فعال در پنل ولی انتخاب نشده\n"
        "❌ = غیرفعال در پنل"
    )
    kb = get_inbounds_keyboard(inbounds, enabled_ids)
    try:
        if hasattr(target, "message"):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        if hasattr(target, "message"):
            await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm_inbounds")
async def cb_adm_inbounds(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    try:
        async with _xui_client() as xui:
            inbounds = await xui.get_inbounds()
        async with AsyncSessionLocal() as session:
            enabled_ids = await get_enabled_inbound_ids(session)
        await _show_inbounds_page(callback, inbounds, enabled_ids)
    except XUIError as e:
        await _safe_edit_admin(callback, f"❌ خطا در دریافت اینباندها: {e}",
                               reply_markup=get_admin_main_keyboard())


@router.callback_query(F.data.startswith("adm_inbound_toggle:"))
async def cb_adm_inbound_toggle(callback: CallbackQuery) -> None:
    """toggle فعال/غیرفعال بودن اینباند برای ساخت کانفیگ."""
    if not await _check_admin(callback):
        return
    inbound_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        is_now_enabled = await toggle_inbound_enabled(session, inbound_id)

    status_text = "✅ برای ساخت کانفیگ فعال شد" if is_now_enabled else "🔴 از ساخت کانفیگ حذف شد"
    await callback.answer(f"اینباند {inbound_id}: {status_text}", show_alert=False)

    # رفرش صفحه
    try:
        async with _xui_client() as xui:
            inbounds = await xui.get_inbounds()
        async with AsyncSessionLocal() as session:
            enabled_ids = await get_enabled_inbound_ids(session)
        await _show_inbounds_page(callback, inbounds, enabled_ids)
    except Exception:
        pass


@router.callback_query(F.data == "adm_inbound_help")
async def cb_adm_inbound_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>راهنمای مدیریت اینباندها</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "• روی هر اینباند کلیک کنید تا وضعیت آن تغییر کند\n"
        "• فقط اینباندهای <b>✅ انتخاب‌شده</b> برای ساخت کانفیگ جدید استفاده می‌شوند\n"
        "• اگه چند اینباند انتخاب شده باشد، به صورت <b>round-robin</b> بین آن‌ها چرخش می‌شود\n"
        "• اگه هیچ اینباندی انتخاب نشده باشد، از اینباند اول پنل استفاده می‌شود\n"
        "• غیرفعال کردن یک اینباند <b>اشتراک‌های فعلی را تحت‌تأثیر قرار نمی‌دهد</b>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# 🏷 کدهای تخفیف
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_discounts")
async def cb_adm_discounts(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    async with AsyncSessionLocal() as session:
        codes = await get_all_discount_codes(session)
    await callback.message.edit_text(
        "🏷 *کدهای تخفیف*\nبرای حذف روی کد بزنید:",
        parse_mode="Markdown",
        reply_markup=get_discounts_keyboard(codes),
    )


@router.callback_query(F.data.startswith("adm_disc_view:"))
async def cb_adm_disc_view(callback: CallbackQuery) -> None:
    """نمایش جزئیات کد تخفیف."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    dc_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        dc = await session.get(__import__('database.models', fromlist=['DiscountCode']).DiscountCode, dc_id)
    if not dc:
        await callback.answer("کد پیدا نشد!", show_alert=True)
        return
    uses    = f"{dc.used_count}/{dc.max_uses}" if dc.max_uses else f"{dc.used_count}/∞"
    expires = dc.expires_at.strftime("%Y/%m/%d") if dc.expires_at else "نامحدود"
    status  = "✅ فعال" if dc.is_active else "⛔ غیرفعال"
    text = (
        f"🏷 <b>کد تخفیف: {dc.code}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💯 درصد تخفیف: <b>{dc.percent}٪</b>\n"
        f"🔢 استفاده‌شده: <b>{uses}</b>\n"
        f"📅 انقضا: <b>{expires}</b>\n"
        f"وضعیت: {status}"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=get_discount_detail_keyboard(dc_id, dc.is_active),
    )


@router.callback_query(F.data.startswith("adm_disc_toggle:"))
async def cb_adm_disc_toggle(callback: CallbackQuery) -> None:
    """فعال/غیرفعال کردن کد تخفیف."""
    if not await _check_admin(callback):
        return
    dc_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        from database.models import DiscountCode
        dc = await session.get(DiscountCode, dc_id)
        if dc:
            dc.is_active = not dc.is_active
            await session.commit()
            label = "✅ فعال" if dc.is_active else "⛔ غیرفعال"
    await callback.answer(f"کد {label} شد.", show_alert=True)
    await cb_adm_disc_view(callback)


@router.callback_query(F.data.startswith("adm_disc_del_confirm:"))
async def cb_adm_disc_del_confirm(callback: CallbackQuery) -> None:
    """درخواست تأیید حذف."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    dc_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "⚠️ آیا مطمئنید؟ این کد تخفیف حذف می‌شود.",
        reply_markup=get_discount_delete_confirm_keyboard(dc_id),
    )


@router.callback_query(F.data.startswith("adm_disc_del:"))
async def cb_adm_disc_del(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    code_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        await delete_discount_code(session, code_id)
    await callback.answer("🗑 کد حذف شد.", show_alert=True)
    await cb_adm_discounts(callback)


@router.callback_query(F.data == "adm_disc_add")
async def cb_adm_disc_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(DiscountAddStates.code)
    await callback.message.answer(
        "🏷 <b>ساخت کد تخفیف جدید</b>\n\n"
        "مرحله ۱/۳ — کد تخفیف را وارد کنید:\n"
        "• فقط حروف انگلیسی و عدد\n"
        "• مثال: <code>SUMMER30</code> یا <code>VIP20</code>",
        parse_mode="HTML",
    )


@router.message(DiscountAddStates.code)
async def disc_add_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    if len(code) < 3:
        await message.answer("❌ کد باید حداقل ۳ کاراکتر باشد.")
        return
    async with AsyncSessionLocal() as session:
        existing = await get_discount_code(session, code)
    if existing:
        await message.answer("❌ این کد قبلاً وجود دارد.")
        return
    await state.update_data(code=code)
    await state.set_state(DiscountAddStates.percent)
    await message.answer(
        f"✅ کد: <b>{code}</b>\n\n"
        "مرحله ۲/۳ — درصد تخفیف را وارد کنید:\n"
        "• عدد بین <b>۱</b> تا <b>۱۰۰</b>\n"
        "• مثال: <code>10</code> یعنی ۱۰٪ تخفیف\n"
        "• مثال: <code>50</code> یعنی نصف قیمت",
        parse_mode="HTML",
    )


@router.message(DiscountAddStates.percent)
async def disc_add_percent(message: Message, state: FSMContext) -> None:
    try:
        pct = int(message.text.strip())
        if not 1 <= pct <= 100:
            raise ValueError
    except ValueError:
        await message.answer("❌ عدد بین 1 تا 100 وارد کنید.")
        return
    await state.update_data(percent=pct)
    await state.set_state(DiscountAddStates.max_uses)
    await message.answer(
        f"✅ تخفیف: <b>{pct}٪</b>\n\n"
        "مرحله ۳/۳ — حداکثر دفعات استفاده:\n"
        "• <code>0</code> = نامحدود\n"
        "• <code>1</code> = فقط یک‌بار قابل استفاده\n"
        "• <code>50</code> = ۵۰ بار",
        parse_mode="HTML",
    )


@router.message(DiscountAddStates.max_uses)
async def disc_add_max_uses(message: Message, state: FSMContext) -> None:
    try:
        max_u = int(message.text.strip())
    except ValueError:
        await message.answer("❌ عدد وارد کنید.")
        return
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        dc = await create_discount_code(
            session,
            code=data["code"],
            percent=data["percent"],
            max_uses=max_u if max_u > 0 else None,
        )
    await state.clear()
    max_str = str(max_u) if max_u > 0 else "نامحدود"
    await message.answer(
        f"🎉 <b>کد تخفیف ساخته شد!</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏷 کد: <code>{dc.code}</code>\n"
        f"💯 درصد: <b>{dc.percent}٪</b>\n"
        f"🔢 تعداد مجاز: <b>{max_str}</b>\n\n"
        f"مثال محاسبه: پلن ۱۰ دلار با کد {dc.code}\n"
        f"→ تخفیف: <b>{dc.percent/100:.0%}</b> = {10 * dc.percent / 100:.2f} دلار\n"
        f"→ قیمت نهایی: <b>{10 * (1 - dc.percent/100):.2f} دلار</b>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# 👥 کاربران
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_users")
async def cb_adm_users(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await callback.message.edit_text(
        "👥 *مدیریت کاربران*",
        parse_mode="Markdown",
        reply_markup=get_admin_users_keyboard(),
    )


# ──────────────────────────────────────────────
# 🖥 وضعیت سرور
# ──────────────────────────────────────────────

@router.callback_query(F.data == "admin_server_status")
async def cb_admin_server_status(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    try:
        async with _xui_client() as xui:
            s = await xui.get_server_status()
        mem_used = s.get("mem", {}).get("current", 0) // 1024 // 1024
        mem_total = s.get("mem", {}).get("total", 0) // 1024 // 1024
        disk_used = s.get("disk", {}).get("current", 0) // 1024 // 1024 // 1024
        disk_total = s.get("disk", {}).get("total", 0) // 1024 // 1024 // 1024
        xray = s.get("xray", {})
        text = (
            "🖥 *وضعیت سرور*\n\n"
            f"🔲 CPU: `{s.get('cpu', 0):.1f}%`\n"
            f"🧠 RAM: `{mem_used}/{mem_total} MB`\n"
            f"💾 دیسک: `{disk_used}/{disk_total} GB`\n"
            f"🔗 اتصالات TCP: `{s.get('tcpCount', 0)}`\n"
            f"⚙️ Xray: `{xray.get('state', 'نامشخص')}` — `{xray.get('version', '')}`"
        )
    except XUIError as e:
        text = f"❌ خطا: `{e}`"
    await callback.message.edit_text(text, parse_mode="Markdown",
                                     reply_markup=get_admin_main_keyboard())


# ──────────────────────────────────────────────
# 📋 لاگ Xray
# ──────────────────────────────────────────────

@router.callback_query(F.data == "admin_xray_logs")
async def cb_admin_xray_logs(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer("⏳ در حال دریافت لاگ...")
    try:
        async with _xui_client() as xui:
            logs = await xui.get_xray_logs(count=50)

        if not logs or logs == "__EMPTY__":
            await callback.message.answer(
                "📋 <b>لاگ‌های Xray</b>\n\n"
                "⚠️ لاگی دریافت نشد.\n\n"
                "احتمالات:\n"
                "• پنل لاگ‌ها را ذخیره نکرده\n"
                "• سطح لاگ در پنل روی <b>None</b> تنظیم شده\n"
                "• endpoint در این نسخه پنل پشتیبانی نمی‌شود\n\n"
                "💡 در پنل 3X-UI بروید: <b>Settings → Log → Log Level</b> و مقدار را از <code>none</code> به <code>info</code> تغییر دهید.",
                parse_mode="HTML",
                reply_markup=get_admin_main_keyboard(),
            )
            return

        # برش لاگ برای محدودیت تلگرام (۴۰۹۶ کاراکتر در Markdown)
        logs_short = logs[-3000:] if len(logs) > 3000 else logs
        # اگر از وسط بریده شد، از اول خط بعدی شروع کن
        if len(logs) > 3000:
            first_newline = logs_short.find("\n")
            if first_newline > 0:
                logs_short = logs_short[first_newline + 1:]

        await callback.message.answer(
            f"📋 <b>لاگ‌های Xray (آخرین ۵۰ خط):</b>\n\n<pre>{logs_short}</pre>",
            parse_mode="HTML",
            reply_markup=get_admin_main_keyboard(),
        )
    except XUIError as e:
        await callback.message.answer(
            f"❌ <b>خطا در دریافت لاگ:</b>\n<code>{e}</code>",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# 🔄 ریستارت Xray
# ──────────────────────────────────────────────

@router.callback_query(F.data == "admin_restart_xray")
async def cb_admin_restart_xray(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    try:
        async with _xui_client() as xui:
            await xui.restart_xray()
        await callback.message.edit_text("✅ Xray با موفقیت ریستارت شد.",
                                         reply_markup=get_admin_main_keyboard())
    except XUIError as e:
        await callback.message.edit_text(f"❌ خطا: `{e}`", parse_mode="Markdown",
                                         reply_markup=get_admin_main_keyboard())


# ──────────────────────────────────────────────
# 🎫 تیکت‌های باز
# ──────────────────────────────────────────────

@router.callback_query(F.data == "admin_tickets")
async def cb_admin_tickets(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    from database.crud import get_open_tickets
    async with AsyncSessionLocal() as session:
        tickets = await get_open_tickets(session)

    if not tickets:
        await callback.message.edit_text("✅ هیچ تیکت باز‌ای وجود ندارد.",
                                         reply_markup=get_admin_main_keyboard())
        return

    lines = []
    for t in tickets:
        status_icon = "🟡" if t.status == "open" else "🔵"
        lines.append(f"{status_icon} #{t.id} — {t.subject[:40]}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for t in tickets:
        status_icon = "🔴" if t.status == "open" else "🟡"
        builder.button(
            text=f"{status_icon} #{t.id}: {t.subject[:30]}",
            callback_data=f"admin_ticket_view:{t.id}",
        )
    builder.button(text="🔙 بازگشت", callback_data="adm_back")
    builder.adjust(1)

    await callback.message.edit_text(
        "🎫 *تیکت‌های باز:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=builder.as_markup(),
    )


# ──────────────────────────────────────────────
# 💾 پشتیبان‌گیری — منو
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_backup")
async def cb_adm_backup(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    from config import settings as _s
    db_type = "PostgreSQL (CSV)" if ("postgresql" in _s.db_url or "postgres" in _s.db_url) else "SQLite (.db)"
    await callback.message.edit_text(
        "💾 <b>پشتیبان‌گیری</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"🗄 نوع دیتابیس: <code>{db_type}</code>\n\n"
        "• <b>بک‌آپ ربات</b>: کاربران، اشتراک‌ها، پرداخت‌ها، تیکت‌ها، تنظیمات\n"
        "• <b>بک‌آپ پنل</b>: فایل SQLite پنل 3X-UI (برای import در پنل)\n"
        "• <b>بک‌آپ خودکار</b>: هر شب ساعت ۰۲:۰۰ UTC به ادمین‌ها ارسال می‌شود\n\n"
        "⚠️ بازگردانی (Restore) فقط برای SQLite از طریق ربات امکان‌پذیر است.",
        parse_mode="HTML",
        reply_markup=get_backup_keyboard(),
    )


@router.callback_query(F.data == "adm_backup_bot")
async def cb_adm_backup_bot(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer("⏳ در حال تهیه بک‌آپ...")
    msg = await callback.message.answer("⏳ در حال تهیه بک‌آپ دیتابیس ربات...")
    ok = await backup_bot_db(callback.bot)
    if ok:
        await msg.edit_text("✅ بک‌آپ دیتابیس ربات با موفقیت ارسال شد.")
    else:
        await msg.edit_text("❌ خطا در تهیه بک‌آپ. فایل دیتابیس پیدا نشد یا قابل خواندن نیست.")


@router.callback_query(F.data == "adm_backup_panel")
async def cb_adm_backup_panel(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer("⏳ در حال دانلود از پنل...")
    msg = await callback.message.answer("⏳ در حال دریافت بک‌آپ از پنل 3X-UI...")
    ok = await backup_panel_db(callback.bot)
    if ok:
        await msg.edit_text("✅ بک‌آپ پنل 3X-UI با موفقیت ارسال شد.")
    else:
        await msg.edit_text("❌ خطا در دریافت بک‌آپ پنل. اتصال را بررسی کنید.")


@router.callback_query(F.data == "adm_backup_both")
async def cb_adm_backup_both(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer("⏳ در حال تهیه...")
    msg = await callback.message.answer("⏳ در حال تهیه هر دو بک‌آپ...")
    from services.backup import backup_bot_db, backup_panel_db
    bot_ok   = await backup_bot_db(callback.bot)
    panel_ok = await backup_panel_db(callback.bot)
    bot_icon   = "✅" if bot_ok   else "❌"
    panel_icon = "✅" if panel_ok else "❌"
    await msg.edit_text(
        f"{bot_icon} بک‌آپ ربات\n"
        f"{panel_icon} بک‌آپ پنل 3X-UI"
    )


# ──────────────────────────────────────────────
# 🔍 جستجوی کاربر با آیدی تلگرام یا یوزرنیم
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_user_search")
async def cb_adm_user_search(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(UserSearchStates.waiting_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ انصراف", callback_data="adm_user_search_cancel")
    await callback.message.answer(
        "🔍 <b>جستجوی کاربر</b>\n\n"
        "آیدی عددی تلگرام یا یوزرنیم کاربر را وارد کنید:\n\n"
        "مثال آیدی: <code>123456789</code>\n"
        "مثال یوزرنیم: <code>@username</code> یا <code>username</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "adm_user_search_cancel")
async def cb_adm_user_search_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """انصراف از جستجوی کاربر — برگشت به پنل ادمین."""
    if not await _check_admin(callback):
        return
    await state.clear()
    await callback.answer("❌ جستجو لغو شد.")
    await callback.message.answer(
        "🔙 به پنل ادمین برگشتید.",
        reply_markup=get_admin_main_keyboard(),
    )


@router.message(UserSearchStates.waiting_id, F.text.in_({"/cancel", "انصراف", "cancel"}))
async def msg_user_search_cancel(message: Message, state: FSMContext) -> None:
    """لغو جستجو با تایپ /cancel."""
    await state.clear()
    await message.answer(
        "❌ جستجو لغو شد.",
        reply_markup=get_admin_main_keyboard(),
    )


@router.message(UserSearchStates.waiting_id)
async def msg_user_search(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return

    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ لطفاً آیدی یا یوزرنیم را وارد کنید.")
        return

    # حذف @ از ابتدا برای جستجوی یوزرنیم
    clean_query = query.lstrip("@")

    user = None
    async with AsyncSessionLocal() as session:
        # اول بررسی می‌کنیم عدد است (آیدی تلگرام) یا متن (یوزرنیم)
        if clean_query.isdigit() and not query.startswith("@"):
            tg_id = int(clean_query)
            user = await get_user_by_telegram_id(session, tg_id)
        else:
            # جستجو با یوزرنیم — بدون @ هم و با @ هم کار می‌کند
            user = await get_user_by_username(session, clean_query)

        if not user:
            search_type = "آیدی" if (clean_query.isdigit() and not query.startswith("@")) else "یوزرنیم"
            await message.answer(
                f"❌ کاربری با {search_type} <code>{clean_query}</code> پیدا نشد.\n\n"
                f"💡 توجه: یوزرنیم باید دقیقاً در ربات ثبت شده باشد.\n"
                f"می‌توانید با @ یا بدون @ جستجو کنید.",
                parse_mode="HTML",
            )
            await state.clear()
            return
        subs = await get_user_subscriptions(session, user.id)

    await state.clear()

    active_subs = [s for s in subs if s.status == "active"]
    uname = f"@{user.username}" if user.username else "—"
    is_banned = getattr(user, "is_banned", False)
    ban_line = "🚫 <b>بن شده</b>\n" if is_banned else ""
    text = (
        f"👤 <b>اطلاعات کاربر</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{ban_line}"
        f"🆔 آیدی: <code>{user.telegram_id}</code>\n"
        f"👤 نام: {user.first_name or '—'}\n"
        f"📎 یوزرنیم: {uname}\n"
        f"🔑 ادمین: {'✅' if user.is_admin else '❌'}\n"
        f"📅 ثبت‌نام: <code>{user.created_at.strftime('%Y-%m-%d')}</code>\n"
        f"📦 اشتراک: <b>{len(active_subs)} فعال</b> از {len(subs)} کل"
    )
    await message.answer(text, parse_mode="HTML",
                         reply_markup=get_user_detail_keyboard(user.telegram_id, is_banned=is_banned))


@router.callback_query(F.data.startswith("adm_user_info:"))
async def cb_adm_user_info(callback: CallbackQuery) -> None:
    """نمایش اطلاعات کاربر — قابل دسترس از لیست اشتراک‌ها."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    tg_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, tg_id)
        if not user:
            await callback.answer("کاربر یافت نشد!", show_alert=True)
            return
        subs = await get_user_subscriptions(session, user.id)
    active_subs = [s for s in subs if s.status == "active"]
    uname = f"@{user.username}" if user.username else "—"
    is_banned = getattr(user, "is_banned", False)
    ban_line = "🚫 <b>بن شده</b>\n" if is_banned else ""
    text = (
        f"👤 <b>اطلاعات کاربر</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{ban_line}"
        f"🆔 آیدی: <code>{tg_id}</code>\n"
        f"👤 نام: {user.first_name or '—'}\n"
        f"📎 یوزرنیم: {uname}\n"
        f"🔑 ادمین: {'✅' if user.is_admin else '❌'}\n"
        f"📅 ثبت‌نام: <code>{user.created_at.strftime('%Y-%m-%d')}</code>\n"
        f"📦 اشتراک فعال: <b>{len(active_subs)}</b> از {len(subs)} کل"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))


async def _show_user_subs(callback: CallbackQuery, tg_id: int) -> None:
    """نمایش لیست اشتراک‌های کاربر — helper مستقل بدون نیاز به callback.data."""
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, tg_id)
        if not user:
            await callback.answer("کاربر یافت نشد!", show_alert=True)
            return
        subs = await get_user_subscriptions(session, user.id)

    uname = f"@{user.username}" if user.username else "—"
    is_banned = getattr(user, "is_banned", False)
    if not subs:
        text = (
            f"👤 <b>{user.first_name or '—'}</b> ({uname})\n"
            f"🆔 <code>{tg_id}</code>\n\n"
            "📭 این کاربر هیچ اشتراکی ندارد."
        )
        try:
            await callback.message.edit_text(text, parse_mode="HTML",
                                              reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))
        except Exception:
            await callback.message.answer(text, parse_mode="HTML",
                                           reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))
        return

    active = sum(1 for s in subs if s.status == "active")
    text = (
        f"👤 <b>{user.first_name or '—'}</b>  {uname}\n"
        f"🆔 <code>{tg_id}</code>\n"
        f"📦 اشتراک‌ها: <b>{active}</b> فعال از {len(subs)} کل\n\n"
        "روی هر اشتراک کلیک کنید تا مدیریت کنید 👇"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=get_user_subs_keyboard(tg_id, subs))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=get_user_subs_keyboard(tg_id, subs))


@router.callback_query(F.data.startswith("adm_user_subs:"))
async def cb_adm_user_subs(callback: CallbackQuery) -> None:
    """لیست اشتراک‌های کاربر با دکمه مدیریت برای هر کدام."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    tg_id = int(callback.data.split(":")[1])
    await _show_user_subs(callback, tg_id)


@router.callback_query(F.data.startswith("adm_user_ban:"))
async def cb_adm_user_ban(callback: CallbackQuery) -> None:
    """لغو دسترسی ادمین کاربر (دکمه قدیمی — حفظ سازگاری)."""
    if not await _check_admin(callback):
        return
    tg_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, tg_id)
        if user:
            user.is_admin = False
            await session.commit()
    await callback.answer(f"✅ دسترسی ادمین کاربر {tg_id} لغو شد.", show_alert=True)


# ──────────────────────────────────────────────
# 🚫 بن/آنبن کاربر
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_user_ban_toggle:"))
async def cb_adm_user_ban_toggle(callback: CallbackQuery) -> None:
    """نمایش صفحه تأیید بن/آنبن کاربر."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    tg_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, tg_id)
        if not user:
            await callback.answer("❌ کاربر پیدا نشد.", show_alert=True)
            return
        is_banned = getattr(user, "is_banned", False)

    uname = f"@{user.username}" if user.username else f"#{tg_id}"
    if not is_banned:
        text = (
            f"🚫 <b>بن کردن کاربر</b>\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"کاربر: <b>{uname}</b>\n"
            f"آیدی: <code>{tg_id}</code>\n\n"
            "⚠️ کاربر بن‌شده دیگر نمی‌تواند از ربات استفاده کند.\n"
            "آیا اطمینان دارید؟"
        )
    else:
        text = (
            f"🔓 <b>رفع بن کاربر</b>\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"کاربر: <b>{uname}</b>\n"
            f"آیدی: <code>{tg_id}</code>\n\n"
            "آیا می‌خواهید بن این کاربر را رفع کنید؟"
        )
    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_user_ban_confirm_keyboard(tg_id, banning=not is_banned),
    )


@router.callback_query(F.data.startswith("adm_user_ban_do:"))
async def cb_adm_user_ban_do(callback: CallbackQuery) -> None:
    """اجرای بن یا آنبن کاربر."""
    if not await _check_admin(callback):
        return
    parts = callback.data.split(":")
    tg_id = int(parts[1])
    do_ban = parts[2] == "1"
    await callback.answer()

    async with AsyncSessionLocal() as session:
        if do_ban:
            done = await ban_user(session, tg_id)
            msg = f"🚫 کاربر {tg_id} بن شد."
        else:
            done = await unban_user(session, tg_id)
            msg = f"✅ بن کاربر {tg_id} رفع شد."

    if not done:
        await callback.answer("❌ کاربر پیدا نشد.", show_alert=True)
        return

    logger.info(msg)
    await callback.answer(msg, show_alert=True)

    # رفرش پروفایل
    async with AsyncSessionLocal() as session2:
        user = await get_user_by_telegram_id(session2, tg_id)
        subs = await get_user_subscriptions(session2, user.id) if user else []

    if not user:
        return
    active_subs = [s for s in subs if s.status == "active"]
    uname = f"@{user.username}" if user.username else "—"
    is_banned = getattr(user, "is_banned", False)
    ban_line = "🚫 <b>بن شده</b>\n" if is_banned else ""
    text = (
        f"👤 <b>اطلاعات کاربر</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{ban_line}"
        f"🆔 آیدی: <code>{tg_id}</code>\n"
        f"👤 نام: {user.first_name or '—'}\n"
        f"📎 یوزرنیم: {uname}\n"
        f"🔑 ادمین: {'✅' if user.is_admin else '❌'}\n"
        f"📅 ثبت‌نام: <code>{user.created_at.strftime('%Y-%m-%d')}</code>\n"
        f"📦 اشتراک فعال: <b>{len(active_subs)}</b> از {len(subs)} کل"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))


# ──────────────────────────────────────────────
# ✉️ ارسال پیام مستقیم به کاربر
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_user_msg:"))
async def cb_adm_user_msg(callback: CallbackQuery, state: FSMContext) -> None:
    """شروع فرایند ارسال پیام به کاربر — یک تیکت از طرف ادمین باز می‌شود."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    tg_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, tg_id)

    uname = f"@{user.username}" if (user and user.username) else f"#{tg_id}"
    fname = (user.first_name or "—") if user else "—"

    await state.set_state(UserDirectMsgStates.waiting_text)
    await state.update_data(target_tid=tg_id)

    from aiogram.utils.keyboard import InlineKeyboardBuilder as _IKB
    cancel_kb = _IKB()
    cancel_kb.button(text="❌ انصراف", callback_data=f"adm_user_msg_cancel:{tg_id}")
    cancel_kb.adjust(1)

    try:
        await callback.message.edit_text(
            f"📩 <b>پیام به کاربر (تیکت جدید)</b>\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>{fname}</b>  {uname}\n"
            f"🆔 <code>{tg_id}</code>\n\n"
            "متن پیام خود را بنویسید.\n"
            "<i>یک تیکت از طرف شما برای کاربر باز می‌شود و مکالمه در همانجا ادامه می‌یابد.</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            f"📩 متن پیام خود را برای <b>{uname}</b> بنویسید:",
            parse_mode="HTML",
            reply_markup=cancel_kb.as_markup(),
        )


@router.callback_query(F.data.startswith("adm_user_msg_cancel:"))
async def cb_adm_user_msg_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """انصراف از ارسال پیام."""
    if not await _check_admin(callback):
        return
    await state.clear()
    await callback.answer("❌ انصراف از ارسال پیام.")
    tg_id = int(callback.data.split(":")[1])
    # برگشت به پروفایل
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, tg_id)
        subs = await get_user_subscriptions(session, user.id) if user else []
    if not user:
        return
    active_subs = [s for s in subs if s.status == "active"]
    uname = f"@{user.username}" if user.username else "—"
    is_banned = getattr(user, "is_banned", False)
    ban_line = "🚫 <b>بن شده</b>\n" if is_banned else ""
    text = (
        f"👤 <b>اطلاعات کاربر</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{ban_line}"
        f"🆔 آیدی: <code>{tg_id}</code>\n"
        f"👤 نام: {user.first_name or '—'}\n"
        f"📎 یوزرنیم: {uname}\n"
        f"🔑 ادمین: {'✅' if user.is_admin else '❌'}\n"
        f"📅 ثبت‌نام: <code>{user.created_at.strftime('%Y-%m-%d')}</code>\n"
        f"📦 اشتراک فعال: <b>{len(active_subs)}</b> از {len(subs)} کل"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=get_user_detail_keyboard(tg_id, is_banned=is_banned))


@router.message(UserDirectMsgStates.waiting_text)
async def msg_adm_user_direct(message: Message, state: FSMContext) -> None:
    """
    دریافت پیام ادمین — یک تیکت از طرف ادمین برای کاربر باز می‌شود.
    اولین پیام داخل تیکت ذخیره می‌شود و به کاربر نوتیف ارسال می‌گردد.
    """
    if not await _check_admin(message):
        await state.clear()
        return

    body = message.text or message.caption or ""
    if not body:
        await message.answer(
            "⚠️ فقط پیام متنی پشتیبانی می‌شود.\nلطفاً متن پیام خود را بنویسید.",
        )
        return

    data = await state.get_data()
    target_tid = data.get("target_tid")
    if not target_tid:
        await state.clear()
        return

    await state.clear()

    admin_tg = message.from_user
    from database.crud import create_ticket_by_admin
    from keyboards.tickets import get_admin_ticket_keyboard as _atk, get_ticket_detail_keyboard as _tdk

    try:
        async with AsyncSessionLocal() as session:
            # دریافت کاربر هدف
            target_user = await get_user_by_telegram_id(session, target_tid)
            if not target_user:
                await message.answer(f"❌ کاربر <code>{target_tid}</code> در دیتابیس پیدا نشد.", parse_mode="HTML")
                return

            # دریافت ادمین از DB
            admin_db = await get_user_by_telegram_id(session, admin_tg.id)
            if not admin_db:
                await message.answer("❌ اطلاعات ادمین در DB پیدا نشد.")
                return

            # ایجاد تیکت از طرف ادمین
            subject = f"📩 پیام از پشتیبانی"
            ticket = await create_ticket_by_admin(
                session=session,
                target_user_id=target_user.id,
                admin_db_id=admin_db.id,
                subject=subject,
                first_message=body,
            )

        logger.success(f"تیکت #{ticket.id} از طرف ادمین {admin_tg.id} برای کاربر {target_tid} ایجاد شد.")

        # اطلاع‌رسانی به کاربر
        uname_admin = f"@{admin_tg.username}" if admin_tg.username else "پشتیبانی"
        try:
            await message.bot.send_message(
                target_tid,
                f"📩 <b>پیام جدید از پشتیبانی — تیکت #{ticket.id}</b>\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"💬 {body[:500]}\n\n"
                "<i>برای پاسخ دادن روی دکمه زیر کلیک کنید:</i>",
                parse_mode="HTML",
                reply_markup=_tdk(ticket.id, is_closed=False),
            )
        except Exception as e:
            logger.warning(f"نوتیف به کاربر {target_tid} ناموفق: {e}")

        await message.answer(
            f"✅ تیکت <b>#{ticket.id}</b> از طرف شما برای کاربر <code>{target_tid}</code> باز شد.\n\n"
            "<i>کاربر نوتیف دریافت کرد. مکالمه از بخش تیکت‌ها ادامه می‌یابد.</i>",
            parse_mode="HTML",
            reply_markup=_atk(ticket.id, is_closed=False),
        )

    except Exception as e:
        logger.error(f"ایجاد تیکت از طرف ادمین ناموفق: {e}")
        await message.answer(
            f"❌ خطا در ایجاد تیکت: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=get_admin_main_keyboard(),
        )


# ──────────────────────────────────────────────
# 📦 مدیریت اشتراک‌های کاربران
# ──────────────────────────────────────────────

def _sub_detail_text(sub, user=None) -> str:
    """متن اطلاعات یک اشتراک."""
    used_gb = sub.used_traffic_bytes / 1024 ** 3
    limit   = f"{sub.traffic_limit_gb} GB" if sub.traffic_limit_gb else "نامحدود"
    exp     = _fmt_expiry(sub.expiry_date)
    status_map = {"active": "✅ فعال", "expired": "⏰ منقضی",
                  "depleted": "📭 تمام‌شده", "disabled": "🚫 غیرفعال"}
    status_fa = status_map.get(sub.status, sub.status)

    uinfo = ""
    if user:
        uname = f"@{user.username}" if user.username else "—"
        uinfo = f"👤 کاربر: {user.first_name or '—'}  {uname}  (<code>{user.telegram_id}</code>)\n"

    ip_limit_val = getattr(sub, "limit_ip", 0) or 0
    ip_line = f"📡 محدودیت IP: <b>{ip_limit_val} دستگاه همزمان</b>\n" if ip_limit_val else ""

    return (
        f"📦 <b>اشتراک #{sub.id}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{uinfo}"
        f"📧 ایمیل: <code>{sub.email}</code>\n"
        f"📊 ترافیک: <b>{used_gb:.2f} GB</b> از {limit}\n"
        f"⏳ انقضا: <code>{exp}</code>\n"
        f"{ip_line}"
        f"🔘 وضعیت: {status_fa}\n"
        f"🔗 Sub ID: <code>{sub.sub_id}</code>"
    )


@router.callback_query(F.data.startswith("adm_sub_view:"))
async def cb_adm_sub_view(callback: CallbackQuery) -> None:
    """نمایش جزئیات اشتراک + دکمه‌های مدیریت."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    sub_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        from database.models import Subscription
        from sqlalchemy import select
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            await callback.answer("اشتراک پیدا نشد!", show_alert=True)
            return
        user = await session.get(type(sub).__mro__[0], sub.user_id) if False else None
        from database.models import User
        ur = await session.execute(select(User).where(User.id == sub.user_id))
        user = ur.scalar_one_or_none()

    text = _sub_detail_text(sub, user)
    tg_id = user.telegram_id if user else 0
    kb = get_sub_manage_keyboard(sub_id, tg_id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("adm_sub_reset:"))
async def cb_adm_sub_reset(callback: CallbackQuery) -> None:
    """ریست ترافیک اشتراک در پنل."""
    if not await _check_admin(callback):
        return
    sub_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        from database.models import Subscription
        from sqlalchemy import select
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            await callback.answer("اشتراک پیدا نشد!", show_alert=True)
            return
        email = sub.email

    await callback.answer("⏳ در حال ریست...")
    try:
        async with _xui_client() as xui:
            await xui._request("POST", f"/clients/resetTraffic/{email}")
        async with AsyncSessionLocal() as session:
            from database.crud import update_subscription_traffic
            await update_subscription_traffic(session, sub_id, 0)
        await callback.answer("✅ ترافیک ریست شد.", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ خطا: {e}", show_alert=True)


@router.callback_query(F.data.startswith("adm_sub_toggle:"))
async def cb_adm_sub_toggle(callback: CallbackQuery) -> None:
    """فعال/غیرفعال کردن اشتراک در پنل."""
    if not await _check_admin(callback):
        return
    sub_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        from database.models import Subscription
        from sqlalchemy import select
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            await callback.answer("اشتراک پیدا نشد!", show_alert=True)
            return
        email = sub.email
        current_status = sub.status

    new_enable = (current_status != "active")
    try:
        async with _xui_client() as xui:
            await xui.update_client(email=email, enable=new_enable)
        async with AsyncSessionLocal() as session:
            from database.crud import update_subscription_status
            new_status = "active" if new_enable else "disabled"
            await update_subscription_status(session, sub_id, new_status)
        status_text = "✅ فعال شد" if new_enable else "🚫 غیرفعال شد"
        await callback.answer(f"اشتراک {status_text}.", show_alert=False)
        # رفرش صفحه
        await cb_adm_sub_view(callback)
    except Exception as e:
        await callback.answer(f"❌ خطا: {e}", show_alert=True)


@router.callback_query(F.data.startswith("adm_sub_del_confirm:"))
async def cb_adm_sub_del_confirm(callback: CallbackQuery) -> None:
    """نمایش تأیید حذف اشتراک."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    sub_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        from database.models import Subscription, User
        from sqlalchemy import select
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            await callback.answer("اشتراک پیدا نشد!", show_alert=True)
            return
        ur = await session.execute(select(User).where(User.id == sub.user_id))
        user = ur.scalar_one_or_none()

    tg_id = user.telegram_id if user else 0
    text = (
        f"🗑 <b>حذف اشتراک</b>\n\n"
        f"📧 ایمیل: <code>{sub.email}</code>\n\n"
        "⚠️ این اشتراک از پنل 3X-UI <b>حذف می‌شود</b>.\n"
        "آیا مطمئن هستید؟"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=get_sub_del_confirm_keyboard(sub_id, tg_id))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=get_sub_del_confirm_keyboard(sub_id, tg_id))


@router.callback_query(F.data.startswith("adm_sub_del:"))
async def cb_adm_sub_del(callback: CallbackQuery) -> None:
    """حذف نهایی اشتراک از پنل و دیتابیس."""
    if not await _check_admin(callback):
        return
    parts = callback.data.split(":")
    sub_id = int(parts[1])
    tg_id  = int(parts[2])

    async with AsyncSessionLocal() as session:
        from database.models import Subscription
        from sqlalchemy import select
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            await callback.answer("اشتراک پیدا نشد!", show_alert=True)
            return
        email = sub.email

    try:
        async with _xui_client() as xui:
            await xui.delete_client(email)
        async with AsyncSessionLocal() as session:
            from database.crud import update_subscription_status
            await update_subscription_status(session, sub_id, "deleted")
        await callback.answer("✅ اشتراک حذف شد.", show_alert=True)
        # برگشت به لیست اشتراک‌های کاربر — بدون تغییر callback.data (frozen در aiogram 3)
        await _show_user_subs(callback, tg_id)
    except Exception as e:
        # پیام خطا را کوتاه کن — تلگرام max 200 کاراکتر قبول می‌کند
        err_short = str(e)[:150]
        await callback.answer(f"❌ خطا در حذف: {err_short}", show_alert=True)


@router.callback_query(F.data.startswith("adm_sub_edit:"))
async def cb_adm_sub_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """شروع ویرایش یک فیلد اشتراک — days / traffic / email."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    _, field, sub_id_str = callback.data.split(":")
    sub_id = int(sub_id_str)

    prompts = {
        "days":    ("📅 چند روز به اشتراک اضافه شود؟\n(عدد مثبت = تمدید  |  عدد منفی = کاهش)\nمثال: <code>30</code>", int),
        "traffic": ("📦 حجم جدید کل اشتراک (GB) را وارد کنید:\n(صفر = نامحدود)\nمثال: <code>50</code>", int),
        "email":   ("✏️ ایمیل (نام کاربری) جدید را وارد کنید:\nمثال: <code>client-5</code>", str),
    }
    prompt, _ = prompts.get(field, ("مقدار جدید را وارد کنید:", str))
    await state.set_state(SubEditStates.waiting_value)
    await state.update_data(field=field, sub_id=sub_id)
    await callback.message.answer(
        f"{prompt}\n\nبرای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(SubEditStates.waiting_value, F.text == "/cancel")
async def sub_edit_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ ویرایش لغو شد.")


@router.message(SubEditStates.waiting_value, F.text)
async def sub_edit_value(message: Message, state: FSMContext) -> None:
    """اعمال ویرایش اشتراک در پنل و دیتابیس."""
    if not await _check_admin(message):
        await state.clear()
        return

    data = await state.get_data()
    field  = data["field"]
    sub_id = data["sub_id"]
    val    = (message.text or "").strip()
    await state.clear()

    async with AsyncSessionLocal() as session:
        from database.models import Subscription, User
        from sqlalchemy import select, update as sa_update
        res = await session.execute(select(Subscription).where(Subscription.id == sub_id))
        sub = res.scalar_one_or_none()
        if not sub:
            await message.answer("❌ اشتراک پیدا نشد.")
            return
        ur = await session.execute(select(User).where(User.id == sub.user_id))
        user = ur.scalar_one_or_none()

    tg_id = user.telegram_id if user else 0

    try:
        async with _xui_client() as xui:
            if field == "days":
                # محاسبه تاریخ انقضای جدید
                delta_days = int(val)
                from datetime import datetime, timezone, timedelta
                if sub.expiry_date:
                    base = sub.expiry_date.replace(tzinfo=timezone.utc) if sub.expiry_date.tzinfo is None else sub.expiry_date
                else:
                    base = datetime.now(timezone.utc)
                new_expiry = base + timedelta(days=delta_days)
                # محاسبه expire_days از الان
                days_from_now = max(1, (new_expiry - datetime.now(timezone.utc)).days)
                await xui.update_client(
                    email=sub.email,
                    traffic_gb=sub.traffic_limit_gb,
                    expire_days=days_from_now,
                )
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        sa_update(Subscription).where(Subscription.id == sub_id)
                        .values(expiry_date=new_expiry)
                    )
                    await session.commit()
                sign = "+" if delta_days >= 0 else ""
                await message.answer(
                    f"✅ <b>تمدید انجام شد</b>\n\n"
                    f"📧 {sub.email}\n"
                    f"📅 {sign}{delta_days} روز\n"
                    f"🗓 انقضای جدید: <code>{_fmt_expiry(new_expiry)}</code>",
                    parse_mode="HTML",
                    reply_markup=get_sub_manage_keyboard(sub_id, tg_id),
                )

            elif field == "traffic":
                new_gb = int(val)
                await xui.update_client(
                    email=sub.email,
                    traffic_gb=new_gb,
                    expire_days=0,
                )
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        sa_update(Subscription).where(Subscription.id == sub_id)
                        .values(traffic_limit_gb=new_gb)
                    )
                    await session.commit()
                limit_text = f"{new_gb} GB" if new_gb else "نامحدود"
                await message.answer(
                    f"✅ <b>حجم آپدیت شد</b>\n\n"
                    f"📧 {sub.email}\n"
                    f"📦 حجم جدید: <code>{limit_text}</code>",
                    parse_mode="HTML",
                    reply_markup=get_sub_manage_keyboard(sub_id, tg_id),
                )

            elif field == "email":
                new_email = val
                # تغییر ایمیل: ابتدا client را در همه اینباندها آپدیت کن
                # 3X-UI endpoint: POST /clients/update/:old_email با email جدید
                from database.models import Subscription
                from sqlalchemy import update as sa_update
                payload = {
                    "email": new_email,
                    "totalGB": sub.traffic_limit_gb * 1024 ** 3 if sub.traffic_limit_gb else 0,
                    "tgId": tg_id,
                    "enable": True,
                }
                await xui._request("POST", f"/clients/update/{sub.email}", json=payload)
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        sa_update(Subscription).where(Subscription.id == sub_id)
                        .values(email=new_email)
                    )
                    await session.commit()
                await message.answer(
                    f"✅ <b>ایمیل تغییر کرد</b>\n\n"
                    f"قبلاً: <code>{sub.email}</code>\n"
                    f"الان: <code>{new_email}</code>",
                    parse_mode="HTML",
                    reply_markup=get_sub_manage_keyboard(sub_id, tg_id),
                )

    except ValueError:
        await message.answer("❌ مقدار وارد‌شده معتبر نیست.")
    except Exception as e:
        logger.error(f"خطا در ویرایش اشتراک {sub_id}: {e}")
        await message.answer(f"❌ خطا: <code>{e}</code>", parse_mode="HTML")


# ──────────────────────────────────────────────
# 📊 آمار پیشرفته
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_stats_advanced")
async def cb_adm_stats_advanced(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    from sqlalchemy import func as sqf, select as sel
    from database.models import Payment, Subscription

    async with AsyncSessionLocal() as session:
        stats = await get_stats(session)

        # اشتراک‌ها به تفکیک وضعیت
        status_rows = await session.execute(
            sel(Subscription.status, sqf.count(Subscription.id))
            .group_by(Subscription.status)
        )
        status_map = dict(status_rows.all())

        # درآمد ۳۰ روز گذشته
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=30)
        rev_30 = await session.execute(
            sel(sqf.sum(Payment.amount_usdt))
            .where(Payment.status.in_(["confirmed", "finished"]))
            .where(Payment.created_at >= since)
        )
        revenue_30 = float(rev_30.scalar() or 0)

        # تعداد کاربران جدید ۳۰ روز
        new_users = await session.execute(
            sel(sqf.count(User.id)).where(User.created_at >= since)
        )
        new_users_30 = int(new_users.scalar() or 0)

    def _s(key: str) -> int:
        return status_map.get(key, 0)

    text = (
        "📊 *آمار پیشرفته ربات*\n"
        "━━━━━━━━━━━━━━━\n"
        f"👥 کل کاربران: `{stats['total_users']}`\n"
        f"🆕 کاربران جدید (۳۰ روز): `{new_users_30}`\n\n"
        "📦 *وضعیت اشتراک‌ها:*\n"
        f"  ✅ فعال: `{_s('active')}`\n"
        f"  ⏰ منقضی: `{_s('expired')}`\n"
        f"  📭 تمام‌شده: `{_s('depleted')}`\n"
        f"  🚫 غیرفعال: `{_s('disabled')}`\n\n"
        f"💰 درآمد کل: `{stats['total_revenue_usdt']:.2f} دلار`\n"
        f"💵 درآمد ۳۰ روز: `{revenue_30:.2f} دلار`\n"
        f"🎫 تیکت‌های باز: `{stats['open_tickets']}`"
    )
    await callback.message.edit_text(
        text, parse_mode="Markdown", reply_markup=get_admin_main_keyboard()
    )


# ──────────────────────────────────────────────
# 🖼 مدیریت بنر
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_banner")
async def cb_adm_banner(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    file_id = await get_banner_file_id()
    if file_id:
        await callback.message.answer_photo(
            photo=file_id,
            caption=(
                "🖼 <b>بنر فعلی ربات</b>\n\n"
                "این عکس همراه پیام‌های متنی (بدون عکس دیگر) ارسال می‌شود.\n"
                "برای تغییر، عکس جدید بفرستید."
            ),
            parse_mode="HTML",
            reply_markup=get_banner_keyboard(has_banner=True),
        )
    else:
        await callback.message.edit_text(
            "🖼 <b>بنر ربات</b>\n\n"
            "هنوز بنری تنظیم نشده.\n"
            "برای تنظیم، یک عکس بفرستید.",
            parse_mode="HTML",
            reply_markup=get_banner_keyboard(has_banner=False),
        )


@router.callback_query(F.data == "adm_banner_set")
async def cb_adm_banner_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(BannerStates.waiting_photo)
    await callback.message.answer(
        "📤 عکس بنر را بفرستید:\n\n"
        "• بهترین اندازه: <b>۱۲۸۰×۳۲۰</b> (افقی/بنری)\n"
        "• فرمت: JPG یا PNG\n"
        "• برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(BannerStates.waiting_photo, F.text == "/cancel")
async def banner_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(BannerStates.waiting_photo, F.photo)
async def banner_receive_photo(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    file_id = message.photo[-1].file_id
    await set_banner_file_id(file_id)
    await state.clear()
    await message.answer_photo(
        photo=file_id,
        caption="✅ <b>بنر با موفقیت تنظیم شد!</b>\n\nاز این پس همراه پیام‌های متنی نمایش داده می‌شود.",
        parse_mode="HTML",
        reply_markup=get_banner_keyboard(has_banner=True),
    )
    logger.info(f"بنر ربات توسط ادمین {message.from_user.id} تنظیم شد.")


@router.message(BannerStates.waiting_photo)
async def banner_wrong_type(message: Message, state: FSMContext) -> None:
    await message.answer("⚠️ لطفاً یک عکس بفرستید (نه فایل یا متن).")


@router.callback_query(F.data == "adm_banner_clear")
async def cb_adm_banner_clear(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await clear_banner()
    await callback.answer("✅ بنر حذف شد.", show_alert=True)
    await callback.message.edit_caption(
        "🖼 بنر حذف شد.",
    ) if callback.message.photo else await callback.message.edit_text("🖼 بنر حذف شد.")


# ──────────────────────────────────────────────
# 🎉 بنر خوش‌آمدگویی
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_welcome_banner")
async def cb_adm_welcome_banner(callback: CallbackQuery) -> None:
    """نمایش وضعیت بنر خوش‌آمدگویی."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from services.welcome import get_welcome_banner_file_id, get_welcome_caption
    file_id = await get_welcome_banner_file_id()
    caption = await get_welcome_caption()
    has_banner = bool(file_id)

    info = (
        "🎉 <b>بنر خوش‌آمدگویی</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "این بنر فقط برای کاربران <b>جدید</b> (اولین /start) نمایش داده می‌شود.\n\n"
        f"📸 عکس: {'✅ تنظیم شده' if has_banner else '❌ تنظیم نشده'}\n"
        f"📝 کپشن:\n<code>{caption[:200]}</code>"
    )
    kb = get_welcome_banner_keyboard(has_banner=has_banner)
    try:
        if callback.message.photo:
            await callback.message.answer(info, parse_mode="HTML", reply_markup=kb)
        else:
            await callback.message.edit_text(info, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(info, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm_welcome_set_photo")
async def cb_adm_welcome_set_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(WelcomeBannerStates.waiting_photo)
    await callback.message.answer(
        "📤 عکس بنر خوش‌آمدگویی را بفرستید.\n"
        "برای لغو: /cancel"
    )


@router.message(WelcomeBannerStates.waiting_photo, F.text == "/cancel")
async def welcome_photo_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(WelcomeBannerStates.waiting_photo, F.photo)
async def welcome_photo_received(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    from services.welcome import set_welcome_banner_file_id
    file_id = message.photo[-1].file_id
    await set_welcome_banner_file_id(file_id)
    await state.clear()
    await message.answer(
        "✅ بنر خوش‌آمدگویی آپلود شد!\n\n"
        "برای تغییر کپشن از «✏️ ویرایش کپشن» استفاده کنید.",
        reply_markup=get_welcome_banner_keyboard(has_banner=True),
    )


@router.message(WelcomeBannerStates.waiting_photo)
async def welcome_photo_wrong(message: Message, state: FSMContext) -> None:
    await message.answer("⚠️ لطفاً یک عکس بفرستید (نه فایل یا متن).")


@router.callback_query(F.data == "adm_welcome_set_caption")
async def cb_adm_welcome_set_caption(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    from services.welcome import get_welcome_caption
    current = await get_welcome_caption()
    await state.set_state(WelcomeBannerStates.waiting_caption)
    await callback.message.answer(
        f"✏️ کپشن جدید را بفرستید (HTML پشتیبانی می‌شود).\n\n"
        f"کپشن فعلی:\n<code>{current[:300]}</code>\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(WelcomeBannerStates.waiting_caption, F.text == "/cancel")
async def welcome_caption_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(WelcomeBannerStates.waiting_caption, F.text)
async def welcome_caption_received(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    from services.welcome import set_welcome_caption
    await set_welcome_caption(message.text or "")
    await state.clear()
    await message.answer(
        "✅ کپشن بنر خوش‌آمدگویی ذخیره شد.",
        reply_markup=get_welcome_banner_keyboard(has_banner=True),
    )


@router.callback_query(F.data == "adm_welcome_clear")
async def cb_adm_welcome_clear(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    from services.welcome import clear_welcome_banner
    await clear_welcome_banner()
    await callback.answer("✅ بنر خوش‌آمدگویی حذف شد.", show_alert=True)
    try:
        await callback.message.edit_text(
            "🎉 بنر خوش‌آمدگویی حذف شد.",
            reply_markup=get_welcome_banner_keyboard(has_banner=False),
        )
    except Exception:
        pass


# ──────────────────────────────────────────────
# 📢 کانال اجباری
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_join_channel")
async def cb_adm_join_channel(callback: CallbackQuery) -> None:
    """نمایش وضعیت کانال اجباری."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from services.welcome import get_join_channel_id, get_join_channel_link, get_join_channel_title
    channel_id = await get_join_channel_id()
    link       = await get_join_channel_link()
    title      = await get_join_channel_title()
    has_channel = bool(channel_id)

    info = (
        "📢 <b>کانال اجباری</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "کاربران قبل از استفاده باید عضو این کانال باشند.\n\n"
    )
    if has_channel:
        info += (
            f"📌 کانال: <code>{channel_id}</code>\n"
            f"🔗 لینک: {link or '—'}\n"
            f"📛 نام: {title}\n"
        )
    else:
        info += "❌ هیچ کانالی تنظیم نشده (عضویت اجباری غیرفعال)"

    kb = get_join_channel_keyboard(has_channel=has_channel)
    try:
        await callback.message.edit_text(info, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(info, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm_channel_set")
async def cb_adm_channel_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(JoinChannelStates.waiting_channel_id)
    await callback.message.answer(
        "📢 <b>تنظیم کانال اجباری — مرحله ۱/۳</b>\n\n"
        "آی‌دی کانال را بفرستید:\n"
        "• اگر کانال عمومی است: <code>@mychannel</code>\n"
        "• اگر کانال خصوصی است: <code>-1001234567890</code>\n\n"
        "⚠️ ربات باید <b>ادمین کانال</b> باشد تا بتواند عضویت را بررسی کند.\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(JoinChannelStates.waiting_channel_id, F.text == "/cancel")
async def channel_set_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(JoinChannelStates.waiting_channel_id, F.text)
async def channel_id_received(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    val = (message.text or "").strip()
    await state.update_data(channel_id=val)
    await state.set_state(JoinChannelStates.waiting_channel_link)
    await message.answer(
        "🔗 <b>مرحله ۲/۳</b> — لینک دعوت کانال را بفرستید:\n"
        "مثال: <code>https://t.me/mychannel</code>\n"
        "یا برای کانال‌های خصوصی: <code>https://t.me/+ABC123</code>\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(JoinChannelStates.waiting_channel_link, F.text == "/cancel")
async def channel_link_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(JoinChannelStates.waiting_channel_link, F.text)
async def channel_link_received(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    val = (message.text or "").strip()
    await state.update_data(channel_link=val)
    await state.set_state(JoinChannelStates.waiting_channel_title)
    await message.answer(
        "📛 <b>مرحله ۳/۳</b> — نام نمایشی کانال را بفرستید:\n"
        "مثال: <code>کانال VPN ما</code>\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(JoinChannelStates.waiting_channel_title, F.text == "/cancel")
async def channel_title_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(JoinChannelStates.waiting_channel_title, F.text)
async def channel_title_received(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    title = (message.text or "").strip()
    from services.welcome import set_join_channel
    await set_join_channel(
        channel_id=data["channel_id"],
        link=data["channel_link"],
        title=title,
    )
    await state.clear()
    await message.answer(
        f"✅ <b>کانال اجباری تنظیم شد!</b>\n\n"
        f"📌 آی‌دی: <code>{data['channel_id']}</code>\n"
        f"🔗 لینک: {data['channel_link']}\n"
        f"📛 نام: {title}\n\n"
        "از این پس کاربران باید عضو این کانال باشند.",
        parse_mode="HTML",
        reply_markup=get_join_channel_keyboard(has_channel=True),
    )


@router.callback_query(F.data == "adm_channel_clear")
async def cb_adm_channel_clear(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    from services.welcome import clear_join_channel
    await clear_join_channel()
    await callback.answer("✅ کانال اجباری حذف شد.", show_alert=True)
    try:
        await callback.message.edit_text(
            "📢 کانال اجباری حذف شد.\nعضویت اجباری غیرفعال است.",
            reply_markup=get_join_channel_keyboard(has_channel=False),
        )
    except Exception:
        pass


# ──────────────────────────────────────────────
# ✏️ ویرایش سریع پلن با callback inline
# ──────────────────────────────────────────────

_FIELD_LABELS = {
    "name":     ("نام پلن",                            str),
    "price":    ("قیمت تومانی یا دلاری ($ برای دلار)", str),
    "traffic":  ("حجم GB — صفر = نامحدود",             int),
    "days":     ("مدت (روز)",                          int),
    "ip":       ("حداکثر دستگاه (صفر=نامحدود)",        int),
    "inbounds": ("آیدی اینباندها با ویرگول",            str),
}

_FIELD_DB = {
    "name": "name", "price": "price_usdt", "traffic": "traffic_gb",
    "days": "duration_days", "ip": "limit_ip", "inbounds": "inbound_ids",
}


@router.callback_query(F.data.startswith("adm_qedit:"))
async def cb_adm_quick_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """ویرایش سریع — adm_qedit:{field}:{plan_id}"""
    if not await _check_admin(callback):
        return
    await callback.answer()
    _, field, plan_id_str = callback.data.split(":", 2)
    plan_id = int(plan_id_str)

    label, _ = _FIELD_LABELS.get(field, (field, str))
    await state.set_state(PlanQuickEditStates.waiting_value)
    await state.update_data(field=field, plan_id=plan_id)

    hint = ""
    if field == "price":
        from services.card_payment import get_card_info
        card_info = await get_card_info()
        rate = card_info.get("rate", 0)
        if rate and rate > 0:
            hint = f"\n💱 نرخ فعلی: <code>{rate:,} تومان</code> = ۱ دلار\nمثال تومان: <code>300000</code> | مثال دلار: <code>$5.5</code>"
        else:
            hint = "\nمثال تومان: <code>300000</code> | مثال دلار: <code>$5.5</code>"

    await callback.message.answer(
        f"✏️ مقدار جدید برای <b>{label}</b> را وارد کنید:{hint}\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(PlanQuickEditStates.waiting_value, F.text == "/cancel")
async def qedit_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ ویرایش لغو شد.")


@router.message(PlanQuickEditStates.waiting_value)
async def msg_quick_edit_value(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    field = data["field"]
    plan_id = data["plan_id"]
    val = (message.text or "").strip()

    _, cast = _FIELD_LABELS.get(field, (field, str))
    db_field = _FIELD_DB.get(field, field)

    # تبدیل هوشمند برای فیلد price — تومان یا دلار
    if field == "price":
        try:
            clean_val = val.replace(",", "").replace("٬", "").strip()
            if clean_val.startswith("$"):
                # قیمت دلاری مستقیم
                casted = float(clean_val[1:].strip())
            else:
                # قیمت تومانی — تبدیل به دلار
                toman_val = float(clean_val)
                from services.card_payment import get_card_info
                card_info = await get_card_info()
                rate = card_info.get("rate", 0)
                if rate and rate > 0:
                    casted = round(toman_val / rate, 2)
                else:
                    casted = toman_val  # بدون نرخ — مستقیم ذخیره می‌شود
        except (ValueError, TypeError):
            await message.answer(
                "❌ مقدار نامعتبر — یک عدد وارد کنید.\n"
                "مثال تومان: <code>300000</code> | مثال دلار: <code>$5.5</code>",
                parse_mode="HTML",
            )
            return
    else:
        try:
            casted = cast(val)
        except (ValueError, TypeError):
            type_name = {"int": "عدد صحیح", "float": "عدد اعشاری", "str": "متن"}.get(cast.__name__, cast.__name__)
            await message.answer(
                f"❌ مقدار نامعتبر — لطفاً یک <b>{type_name}</b> وارد کنید.",
                parse_mode="HTML",
            )
            return

    async with AsyncSessionLocal() as session:
        await update_plan(session, plan_id, **{db_field: casted})
        plan = await get_plan(session, plan_id)

    await state.clear()
    traffic = f"{plan.traffic_gb} گیگ" if plan.traffic_gb else "♾ نامحدود"
    price_s = _fmt_usdt(plan.price_usdt)
    # نمایش قیمت تومانی اگر نرخ تنظیم شده
    from services.card_payment import get_card_info as _get_card
    _ci = await _get_card()
    _rate = _ci.get("rate", 0)
    price_line = f"💲 قیمت: <code>{price_s} دلار</code>"
    if _rate and _rate > 0:
        toman_display = int(plan.price_usdt * _rate)
        price_line += f"  (~<code>{toman_display:,} تومان</code>)"
    await message.answer(
        f"✅ <b>پلن آپدیت شد!</b>\n\n"
        f"📋 نام: <b>{plan.name}</b>\n"
        f"📦 حجم: <code>{traffic}</code>\n"
        f"⏱ مدت: <code>{plan.duration_days} روز</code>\n"
        f"{price_line}",
        parse_mode="HTML",
        reply_markup=get_plan_quick_actions_keyboard(plan_id),
    )


@router.callback_query(F.data.startswith("adm_plan_copy:"))
async def cb_adm_plan_copy(callback: CallbackQuery) -> None:
    """کپی پلن — ساخت پلن جدید با همان مشخصات."""
    if not await _check_admin(callback):
        return
    plan_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        src = await get_plan(session, plan_id)
        if not src:
            await callback.answer("پلن یافت نشد!", show_alert=True)
            return
        new_plan = await create_plan(
            session,
            name=f"کپی — {src.name}",
            traffic_gb=src.traffic_gb,
            duration_days=src.duration_days,
            price_usdt=src.price_usdt,
            limit_ip=src.limit_ip,
            inbound_ids=src.inbound_ids,
        )
    await callback.answer(f"✅ پلن کپی شد (ID: {new_plan.id})", show_alert=True)
    # refresh لیست
    async with AsyncSessionLocal() as session:
        plans = await get_all_plans(session)
    await callback.message.edit_text(
        "📋 <b>مدیریت پلن‌ها</b>",
        parse_mode="HTML",
        reply_markup=get_plans_manage_keyboard(plans),
    )


# ──────────────────────────────────────────────
# 💳 تنظیم کارت به کارت
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_card_settings")
async def cb_adm_card_settings(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    card = await get_card_info()
    number = fmt_card_number(card["number"]) if card["number"] else "تنظیم نشده"
    holder = card["holder"] or "تنظیم نشده"
    rate   = f"{card['rate']:,}"
    text = (
        "💳 <b>تنظیمات کارت به کارت</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"🔢 شماره کارت: <code>{number}</code>\n"
        f"👤 نام صاحب کارت: <b>{holder}</b>\n"
        f"💱 نرخ USDT → تومان: <b>{rate}</b> تومان\n"
    )
    from keyboards.admin import get_card_settings_keyboard
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=get_card_settings_keyboard())


def fmt_card_number(raw: str) -> str:
    digits = raw.replace("-", "").replace(" ", "")
    if len(digits) == 16:
        return f"{digits[:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:]}"
    return raw


@router.callback_query(F.data == "adm_card_edit")
async def cb_adm_card_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(CardSettingStates.waiting_card_number)
    await callback.message.answer(
        "🔢 شماره کارت ۱۶ رقمی را وارد کنید:\n"
        "مثال: <code>6037991234567890</code>\n\nبرای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(CardSettingStates.waiting_card_number, F.text == "/cancel")
async def card_edit_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(CardSettingStates.waiting_card_number)
async def card_recv_number(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear(); return
    digits = (message.text or "").replace("-", "").replace(" ", "")
    if not digits.isdigit() or len(digits) != 16:
        await message.answer("❌ شماره کارت باید دقیقاً ۱۶ رقم باشد.")
        return
    await state.update_data(card_number=digits)
    await state.set_state(CardSettingStates.waiting_card_holder)
    await message.answer("👤 نام صاحب کارت را وارد کنید (فارسی یا انگلیسی):")


@router.message(CardSettingStates.waiting_card_holder)
async def card_recv_holder(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear(); return
    holder = (message.text or "").strip()
    if len(holder) < 2:
        await message.answer("❌ نام کوتاه است.")
        return
    data = await state.get_data()
    await set_card_info(data["card_number"], holder)
    await state.clear()
    card_fmt = fmt_card_number(data["card_number"])
    await message.answer(
        f"✅ <b>کارت با موفقیت ذخیره شد!</b>\n\n"
        f"🔢 <code>{card_fmt}</code>\n👤 {holder}",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_card_rate")
async def cb_adm_card_rate(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(CardSettingStates.waiting_rate)
    await callback.message.answer(
        "💱 نرخ تبدیل USDT به تومان را وارد کنید:\n"
        "مثال: <code>90000</code> (یعنی ۹۰,۰۰۰ تومان = ۱ دلار)\n\nبرای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(CardSettingStates.waiting_rate)
async def card_recv_rate(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear(); return
    val = (message.text or "").replace(",", "").strip()
    if not val.isdigit() or int(val) < 1000:
        await message.answer("❌ عدد معتبر وارد کنید (حداقل ۱۰۰۰).")
        return
    await set_usdt_rate(int(val))
    await state.clear()
    await message.answer(
        f"✅ نرخ به‌روز شد: <b>{int(val):,} تومان</b> به ازای هر دلار.",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# ♻️ بازگردانی دیتابیس از فایل بک‌آپ
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_restore")
async def cb_adm_restore(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(RestoreStates.waiting_db_file)
    await callback.message.answer(
        "♻️ <b>بازگردانی دیتابیس ربات</b>\n\n"
        "این عملیات <b>فقط دیتابیس ربات</b> را بازیابی می‌کند.\n"
        "پنل 3X-UI دست‌نخورده می‌ماند.\n\n"
        "⚠️ اطلاعات فعلی ربات با فایل بک‌آپ جایگزین می‌شود.\n\n"
        "فایل <code>.db</code> یا <code>.zip</code> بک‌آپ ربات را بفرستید.\n"
        "(همان فایل‌هایی که ربات با عنوان «🗄 بک‌آپ دیتابیس ربات» فرستاده)\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(RestoreStates.waiting_db_file, F.text == "/cancel")
async def restore_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ بازگردانی لغو شد.")


@router.message(RestoreStates.waiting_db_file, F.document)
async def restore_recv_file(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return

    from pathlib import Path
    import io, zipfile, sqlite3, shutil

    doc = message.document
    fname = doc.file_name or ""
    if not (fname.endswith(".db") or fname.endswith(".zip")):
        await message.answer("⚠️ فقط فایل‌های <code>.db</code> یا <code>.zip</code> قبول می‌شوند.", parse_mode="HTML")
        return

    await state.clear()
    wait = await message.answer("⏳ در حال دانلود و بازیابی...")

    try:
        # دانلود فایل
        file = await message.bot.get_file(doc.file_id)
        file_bytes_io = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=file_bytes_io)
        file_bytes = file_bytes_io.getvalue()

        # استخراج .db از .zip اگر نیاز باشد
        db_bytes: bytes
        if fname.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                db_files = [n for n in zf.namelist() if n.endswith(".db")]
                if not db_files:
                    await wait.edit_text("❌ در فایل ZIP هیچ فایل .db پیدا نشد.")
                    return
                db_bytes = zf.read(db_files[0])
        else:
            db_bytes = file_bytes

        # تأیید اینکه فایل یک SQLite معتبر است
        if not db_bytes.startswith(b"SQLite format 3"):
            await wait.edit_text("❌ فایل یک دیتابیس SQLite معتبر نیست.")
            return

        from config import settings as _s

        # ── PostgreSQL: پشتیبانی نمی‌شود ────────────────────────
        if "postgresql" in _s.db_url:
            await wait.edit_text(
                "⚠️ <b>PostgreSQL Restore</b>\n\n"
                "بازگردانی خودکار برای PostgreSQL از طریق ربات پشتیبانی نمی‌شود.\n\n"
                "برای بازگردانی:\n"
                "۱. به سرور وصل شوید\n"
                "۲. از دستور زیر استفاده کنید:\n"
                "<code>psql -U user -d dbname &lt; backup.sql</code>\n\n"
                "یا اگر فایل CSV دارید از pgAdmin یا psql COPY استفاده کنید.",
                parse_mode="HTML",
            )
            return

        # ── SQLite restore ────────────────────────────────────────
        db_path_str = _s.db_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
        db_path = Path(db_path_str)

        # اگر مسیر نسبی بود، نسبت به پوشه bot/ حل کن
        if not db_path.is_absolute():
            from pathlib import Path as _Path
            base_dir = _Path(__file__).parent.parent  # handlers/ → bot/
            db_path = (base_dir / db_path).resolve()

        # بک‌آپ از نسخه فعلی قبل از جایگزینی
        backup_path = db_path.with_suffix(f".pre_restore_{_ts()}.db")
        if db_path.exists():
            shutil.copy2(db_path, backup_path)

        # بازنویسی دیتابیس — نام unique برای جلوگیری از تداخل همزمان
        tmp_path = db_path.parent / f"_restore_tmp_{_ts()}.db"
        tmp_path.write_bytes(db_bytes)
        restore_src = sqlite3.connect(str(tmp_path))
        restore_dst = sqlite3.connect(str(db_path))
        try:
            restore_src.backup(restore_dst)
        finally:
            restore_src.close()
            restore_dst.close()
        tmp_path.unlink(missing_ok=True)

        # ── بسیار مهم: dispose connection pool ──────────────────────────
        # بعد از جایگزینی فایل SQLite، SQLAlchemy connection pool هنوز به
        # فایل قدیمی متصل است. dispose() همه اتصال‌های موجود را می‌بندد
        # تا session بعدی از فایل جدید بخواند.
        try:
            from database.engine import engine as _db_engine
            await _db_engine.dispose()
            logger.info("connection pool پاک‌سازی شد — session‌های بعدی از DB جدید می‌خوانند.")
        except Exception as _disp_err:
            logger.warning(f"dispose engine ناموفق (ری‌استارت ضروری): {_disp_err}")

        # cleanup تراکنش‌های کریپتوی منقضی‌شده در DB جدید
        try:
            from services.notifications import cleanup_stale_payments
            stale = await cleanup_stale_payments()
            if stale:
                logger.info(f"restore cleanup: {stale} تراکنش منقضی پاک‌سازی شد.")
        except Exception:
            pass

        # ── migration ایمن: ستون‌های جدید را اگر DB بک‌آپ قدیمی دارد اضافه کن ──
        migration_log = []
        try:
            missing = _ensure_schema_columns(db_path)
            if missing:
                migration_log = missing
                logger.info(f"restore migration: ستون‌های جدید اضافه شدند: {missing}")
        except Exception as _mg_err:
            logger.warning(f"restore migration ناموفق (بی‌اثر): {_mg_err}")

        migration_note = ""
        if migration_log:
            migration_note = (
                f"\n🔧 <b>Migration خودکار:</b> {len(migration_log)} ستون جدید اضافه شد\n"
                f"<code>{', '.join(migration_log)}</code>\n"
            )

        await wait.edit_text(
            "✅ <b>بازگردانی موفق!</b>\n\n"
            f"📂 فایل: <code>{fname}</code>\n"
            f"💾 نسخه قبلی: <code>{backup_path.name}</code>\n"
            f"{migration_note}\n"
            "♻️ Connection pool پاک‌سازی شد — ربات بلافاصله از DB جدید می‌خواند.\n\n"
            "⚠️ <b>توصیه:</b> برای اطمینان کامل، ربات را یک‌بار ری‌استارت کنید.",
            parse_mode="HTML",
        )
        logger.success(f"دیتابیس از فایل {fname} بازیابی شد توسط ادمین {message.from_user.id}")

    except Exception as e:
        logger.error(f"خطا در بازگردانی دیتابیس: {e}")
        await wait.edit_text(f"❌ خطا در بازگردانی: <code>{e}</code>", parse_mode="HTML")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


def _ensure_schema_columns(db_path) -> list[str]:
    """
    بعد از restore، ستون‌های جدیدی که در نسخه‌های اخیر اضافه شده‌اند
    را در DB بازیابی‌شده بررسی و در صورت نبودن اضافه می‌کند.

    این تابع برای سازگاری بک‌آپ‌های قدیمی‌تر با نسخه‌های جدید ربات است.
    هیچ‌گاه داده‌ای حذف نمی‌کند — فقط ستون‌های جدید ADD می‌کند.

    Returns: لیست ستون‌هایی که اضافه شدند
    """
    import sqlite3 as _sq3
    conn = _sq3.connect(str(db_path))
    cur = conn.cursor()
    added = []

    # جدول: ستون‌های جدیدی که باید وجود داشته باشند
    # فرمت: (جدول, ستون, نوع SQL, مقدار پیش‌فرض)
    required_columns = [
        # اضافه‌شده در بروزرسانی بن کاربران
        ("users", "is_banned", "BOOLEAN NOT NULL DEFAULT 0", None),
        # اضافه‌شده در بروزرسانی تیکت‌های ادمین
        ("tickets", "initiated_by_admin", "BOOLEAN NOT NULL DEFAULT 0", None),
    ]

    for table, column, col_def, _ in required_columns:
        try:
            # بررسی وجود ستون
            cur.execute(f"PRAGMA table_info({table})")
            cols = [row[1] for row in cur.fetchall()]
            if column not in cols:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
                conn.commit()
                added.append(f"{table}.{column}")
                logger.info(f"migration: ستون {table}.{column} اضافه شد.")
        except Exception as e:
            logger.warning(f"migration ستون {table}.{column} ناموفق: {e}")

    conn.close()
    return added


# ──────────────────────────────────────────────
# 🗑 ریست کامل دیتابیس — تأیید دو مرحله‌ای
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_db_reset_step1")
async def cb_db_reset_step1(callback: CallbackQuery) -> None:
    """مرحله اول: نمایش هشدار اولیه."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from keyboards.admin import get_db_reset_confirm1_keyboard
    await callback.message.answer(
        "🗑 <b>ریست کامل دیتابیس ربات</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>هشدار مرحله اول</b>\n\n"
        "این عملیات <b>تمام اطلاعات</b> زیر را حذف می‌کند:\n"
        "• همه کاربران و اشتراک‌ها\n"
        "• همه پرداخت‌ها و تیکت‌ها\n"
        "• همه تنظیمات ادمین (کارت، نرخ، بنر، کانال)\n"
        "• همه پلن‌ها و کدهای تخفیف\n\n"
        "⛔ <b>این عملیات غیرقابل بازگشت است!</b>\n\n"
        "قبل از ادامه مطمئن شوید بک‌آپ دارید.\n"
        "آیا مطمئن هستید؟",
        parse_mode="HTML",
        reply_markup=get_db_reset_confirm1_keyboard(),
    )


@router.callback_query(F.data == "adm_db_reset_step2")
async def cb_db_reset_step2(callback: CallbackQuery) -> None:
    """مرحله دوم: تأیید نهایی — آخرین فرصت لغو."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from keyboards.admin import get_db_reset_confirm2_keyboard
    await callback.message.answer(
        "🚨 <b>تأیید نهایی — آخرین هشدار</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "شما در حال حذف <b>کامل و دائمی</b> تمام داده‌های ربات هستید.\n\n"
        "بعد از ریست:\n"
        "• ربات مثل نصب اول شروع می‌کند\n"
        "• هیچ‌چیزی قابل بازیابی نیست\n"
        "• پنل 3X-UI دست‌نخورده می‌ماند\n\n"
        "❓ <b>آیا قطعاً می‌خواهید ادامه دهید؟</b>",
        parse_mode="HTML",
        reply_markup=get_db_reset_confirm2_keyboard(),
    )


@router.callback_query(F.data == "adm_db_reset_confirm")
async def cb_db_reset_confirm(callback: CallbackQuery) -> None:
    """اجرای ریست نهایی — حذف همه جداول و recreate."""
    if not await _check_admin(callback):
        return
    await callback.answer("⏳ در حال ریست...")
    wait = await callback.message.answer("⏳ در حال ریست دیتابیس...")

    try:
        from database.engine import engine as _db_engine
        from database.models import Base

        # ── بک‌آپ خودکار قبل از ریست ─────────────────────────
        try:
            from services.backup import backup_bot_db
            await backup_bot_db(callback.bot)
            logger.info("بک‌آپ خودکار قبل از ریست ارسال شد.")
        except Exception as _be:
            logger.warning(f"بک‌آپ خودکار قبل از ریست ناموفق: {_be}")

        # ── حذف همه جداول و ساخت مجدد ─────────────────────────
        async with _db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # dispose pool تا connection‌های cache شده پاک بشن
        await _db_engine.dispose()

        logger.warning(
            f"⚠️ دیتابیس ربات توسط ادمین {callback.from_user.id} "
            f"(@{callback.from_user.username}) کاملاً ریست شد."
        )

        await wait.edit_text(
            "✅ <b>دیتابیس با موفقیت ریست شد!</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "🗑 همه داده‌ها حذف شدند.\n"
            "📦 یک بک‌آپ خودکار قبل از ریست برای شما ارسال شد.\n\n"
            "⚠️ <b>ربات را ری‌استارت کنید</b> تا پلن‌های پیش‌فرض دوباره ساخته شوند.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"خطا در ریست دیتابیس: {e}")
        await wait.edit_text(
            f"❌ <b>خطا در ریست:</b> <code>{e}</code>",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# 💰 مدیریت روش‌های پرداخت
# ──────────────────────────────────────────────

def _crypto_api_warning() -> str:
    """اگه کریپتو فعاله ولی API Key تنظیم نشده، متن هشدار برمی‌گردونه."""
    from config import settings
    if not settings.nowpayments_api_key:
        return (
            "\n\n🚨 <b>هشدار:</b> کریپتو فعاله ولی "
            "<code>NOWPAYMENTS_API_KEY</code> در .env تنظیم نشده!\n"
            "کاربران آدرس جعلی می‌بینند و پرداخت واقعی ثبت نمی‌شود.\n"
            "👉 API Key را از <b>nowpayments.io</b> دریافت و در .env وارد کنید."
        )
    if not settings.nowpayments_ipn_secret:
        return (
            "\n\n⚠️ <b>توجه:</b> <code>NOWPAYMENTS_IPN_SECRET</code> تنظیم نشده.\n"
            "پرداخت کریپتو کار می‌کند ولی تأیید <b>خودکار</b> غیرفعال است.\n"
            "کاربر باید دکمه «بررسی پرداخت» را بزند."
        )
    return ""


def _pm_text(pm: dict, warning: str) -> str:
    """متن صفحه روش‌های پرداخت."""
    _PRICE_DISPLAY_LABELS = {
        "both": "هر دو (دلار + تومان)",
        "usd":  "فقط دلار",
        "toman": "فقط تومان",
    }
    price_mode = _PRICE_DISPLAY_LABELS.get(pm.get("price_display", "both"), "هر دو")
    return (
        "💰 <b>روش‌های پرداخت</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"💱 نمایش قیمت پلن‌ها: <b>{price_mode}</b>\n\n"
        "برای فعال یا غیرفعال کردن هر روش روی آن کلیک کنید:"
        f"{warning}"
    )


@router.callback_query(F.data == "adm_payment_methods")
async def cb_adm_payment_methods(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    pm = await get_payment_status()
    warning = _crypto_api_warning() if pm["crypto"] else ""
    try:
        await callback.message.edit_text(
            _pm_text(pm, warning),
            parse_mode="HTML",
            reply_markup=get_payment_methods_keyboard(
                crypto_on=pm["crypto"], card_on=pm["card"],
                crypto_invoice=pm.get("crypto_invoice", False),
                crypto_gateway=pm.get("crypto_gateway", "nowpayments"),
                price_display=pm.get("price_display", "both"),
            ),
        )
    except Exception:
        await callback.message.answer(
            _pm_text(pm, warning),
            parse_mode="HTML",
            reply_markup=get_payment_methods_keyboard(
                crypto_on=pm["crypto"], card_on=pm["card"],
                crypto_invoice=pm.get("crypto_invoice", False),
                crypto_gateway=pm.get("crypto_gateway", "nowpayments"),
                price_display=pm.get("price_display", "both"),
            ),
        )


@router.callback_query(F.data.startswith("adm_pm_toggle:"))
async def cb_adm_pm_toggle(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    method = callback.data.split(":")[1]  # crypto | card | crypto_invoice | crypto_gateway | price_display
    pm = await get_payment_status()

    if method == "crypto":
        new_val = not pm["crypto"]
        await set_crypto_enabled(new_val)
        label = "کریپتو"
        status_text = "✅ فعال شد" if new_val else "⛔ غیرفعال شد"
    elif method == "crypto_invoice":
        new_val = not pm.get("crypto_invoice", False)
        await set_crypto_invoice(new_val)
        label = "حالت صفحه انتخاب ارز" if new_val else "حالت USDT مستقیم"
        status_text = "✅ فعال شد"
    elif method == "crypto_gateway":
        # toggle بین nowpayments و maxelpay
        current_gw = pm.get("crypto_gateway", "nowpayments")
        new_gw = "maxelpay" if current_gw == "nowpayments" else "nowpayments"
        await set_crypto_gateway(new_gw)
        label = "درگاه کریپتو"
        status_text = "💜 MaxelPay" if new_gw == "maxelpay" else "🔵 NOWPayments"
    elif method == "price_display":
        # چرخش بین three حالت: both → usd → toman → both
        _CYCLE = {"both": "usd", "usd": "toman", "toman": "both"}
        current_mode = pm.get("price_display", "both")
        new_mode = _CYCLE.get(current_mode, "both")
        await set_price_display_mode(new_mode)
        _MODE_LABELS = {"both": "هر دو", "usd": "فقط دلار", "toman": "فقط تومان"}
        label = "نمایش قیمت"
        status_text = _MODE_LABELS.get(new_mode, new_mode)
    else:
        new_val = not pm["card"]
        await set_card_enabled(new_val)
        label = "کارت به کارت"
        status_text = "✅ فعال شد" if new_val else "⛔ غیرفعال شد"

    await callback.answer(f"{label}: {status_text}", show_alert=True)

    # رفرش صفحه
    pm = await get_payment_status()
    warning = _crypto_api_warning() if pm["crypto"] else ""
    try:
        await callback.message.edit_text(
            _pm_text(pm, warning),
            parse_mode="HTML",
            reply_markup=get_payment_methods_keyboard(
                crypto_on=pm["crypto"], card_on=pm["card"],
                crypto_invoice=pm.get("crypto_invoice", False),
                crypto_gateway=pm.get("crypto_gateway", "nowpayments"),
                price_display=pm.get("price_display", "both"),
            ),
        )
    except Exception:
        pass


# ──────────────────────────────────────────────
# 🔐 تنظیمات امنیتی — تغییر دستور ورود ادمین
# ──────────────────────────────────────────────

import re as _re
_VALID_CMD_RE = _re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")


@router.callback_query(F.data == "adm_security")
async def cb_adm_security(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    current_cmd = await _get_admin_command()
    await callback.message.edit_text(
        "🔐 <b>تنظیمات امنیتی</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"دستور ورود ادمین فعلی: <code>/{current_cmd}</code>\n\n"
        "⚠️ این دستور برای کاربران عادی نمایش داده نمی‌شود.\n"
        "پس از تغییر، دستور قدیمی دیگر کار نمی‌کند.",
        parse_mode="HTML",
        reply_markup=get_security_keyboard(current_cmd),
    )


@router.callback_query(F.data == "adm_sec_change_cmd")
async def cb_adm_sec_change_cmd(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(SecurityStates.waiting_new_command)
    current_cmd = await _get_admin_command()
    await callback.message.answer(
        "🔑 <b>تغییر دستور ورود ادمین</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"دستور فعلی: <code>/{current_cmd}</code>\n\n"
        "دستور جدید را وارد کنید (بدون /):\n"
        "• فقط حروف انگلیسی، عدد و _\n"
        "• باید با حرف شروع شود\n"
        "• بین ۳ تا ۳۲ کاراکتر\n\n"
        "مثال: <code>adminlogin</code> یا <code>mypanel_2024</code>\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(SecurityStates.waiting_new_command, F.text == "/cancel")
async def msg_sec_cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ تغییر دستور لغو شد.", reply_markup=get_admin_main_keyboard())


@router.message(SecurityStates.waiting_new_command)
async def msg_sec_new_command(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return

    new_cmd = (message.text or "").strip().lstrip("/")

    # اعتبارسنجی
    if not _VALID_CMD_RE.match(new_cmd):
        await message.answer(
            "❌ دستور نامعتبر است.\n"
            "• فقط حروف انگلیسی، عدد و _\n"
            "• باید با حرف شروع شود\n"
            "• بین ۳ تا ۳۲ کاراکتر\n\n"
            "دوباره وارد کنید یا /cancel برای لغو:",
        )
        return

    old_cmd = await _get_admin_command()
    await _set_admin_command(new_cmd)
    await state.clear()

    logger.success(
        f"دستور ادمین تغییر کرد: /{old_cmd} → /{new_cmd} "
        f"توسط ادمین {message.from_user.id}"
    )
    await message.answer(
        f"✅ <b>دستور ورود تغییر کرد!</b>\n\n"
        f"دستور قدیمی: <code>/{old_cmd}</code>\n"
        f"دستور جدید: <code>/{new_cmd}</code>\n\n"
        f"برای ورود به پنل از این دستور استفاده کنید:\n"
        f"<code>/{new_cmd} رمز_شما</code>",
        parse_mode="HTML",
        reply_markup=get_admin_main_keyboard(),
    )


# ──────────────────────────────────────────────
# 🧾 تراکنش‌های در صف انتظار
# ──────────────────────────────────────────────

class TxSearchStates(StatesGroup):
    waiting_order_id = State()


def _payment_status_label(status: str) -> str:
    return {
        "awaiting_review": "⏳ در انتظار بررسی",
        "waiting":         "⏳ در انتظار پرداخت",
        "confirming":      "🔄 در حال تأیید",
        "confirmed":       "✅ موفق",
        "finished":        "✅ موفق",
        "failed":          "❌ ناموفق",
        "expired":         "⏰ منقضی",
        "partially_paid":  "⚠️ ناقص",
        "deleted":         "🗑 حذف‌شده",
    }.get(status, status)


async def _send_payment_card(target, payment, user, sub=None) -> None:
    """ارسال کارت اطلاعات یک تراکنش با کیبورد مناسب.

    کیبورد بر اساس روش پرداخت و وضعیت انتخاب می‌شود:
      • کارت در صف   → دکمه تأیید/رد
      • کریپتو در صف → دکمه علامت‌گذاری منقضی (کریپتو دستی تأیید نمی‌شود)
      • سایر          → پروفایل کاربر / مدیریت اشتراک
    """
    from keyboards.admin import (
        get_pending_payment_keyboard,
        get_crypto_pending_keyboard,
        get_transactions_keyboard,
    )

    uname  = f"@{user.username}" if user and user.username else "—"
    ufname = user.first_name if user else "—"
    tg_id  = user.telegram_id if user else 0
    created = payment.created_at.strftime("%Y-%m-%d %H:%M") if payment.created_at else "—"
    _method_labels = {
        "card":        "💳 کارت به کارت",
        "maxelpay":    "💜 MaxelPay (کریپتو)",
        "nowpayments": "🔵 NOWPayments (کریپتو)",
        "crypto":      "💱 کریپتو",
    }
    method_fa = _method_labels.get(payment.payment_method, "💱 کریپتو")
    amount_str = f"{payment.amount_rial:,} ریال" if getattr(payment, "amount_rial", None) else f"{payment.amount_usdt} USDT"
    status_fa = _payment_status_label(payment.status)

    sub_info = ""
    if sub:
        sub_info = f"📦 اشتراک: <code>{sub.email}</code>\n"

    # اگر کریپتو منقضی شده ولی status هنوز waiting است، نشان بده
    _CRYPTO_METHODS = {"crypto", "maxelpay", "nowpayments"}
    from datetime import datetime, timezone as tz
    is_crypto_stale = (
        payment.payment_method in _CRYPTO_METHODS
        and payment.status in ("waiting", "confirming", "pending")
        and payment.expires_at is not None
        and payment.expires_at < datetime.now(tz.utc)
    )
    if is_crypto_stale:
        status_fa = "⏰ منقضی (آپدیت نشده)"

    text = (
        f"🧾 <b>تراکنش</b>  {status_fa}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🔖 سفارش: <code>{payment.order_id}</code>\n"
        f"👤 {ufname}  {uname}  (<code>{tg_id}</code>)\n"
        f"💰 مبلغ: <b>{amount_str}</b>\n"
        f"🏦 روش: {method_fa}\n"
        f"📅 تاریخ: <code>{created}</code>\n"
        f"{sub_info}"
    )

    # ── انتخاب کیبورد ──────────────────────────────────────────────────
    # کارت‌به‌کارت در صف (شامل restore شده‌ها با status pending/waiting)
    is_card_pending = (
        payment.payment_method == "card"
        and payment.status in ("awaiting_review", "pending", "waiting")
        and not sub
    )
    # کریپتو در صف — شامل maxelpay + nowpayments + crypto (legacy)
    is_crypto_pending = (
        payment.payment_method in _CRYPTO_METHODS
        and payment.status in ("waiting", "confirming", "pending")
        and not sub
    )

    if is_card_pending:
        kb = get_pending_payment_keyboard(payment.order_id)
    elif is_crypto_pending:
        kb = get_crypto_pending_keyboard(payment.order_id, tg_id=tg_id)
    else:
        has_sub = bool(sub)
        sub_id  = sub.id if sub else 0
        kb = get_transactions_keyboard(has_sub_id=has_sub, sub_id=sub_id, tg_id=tg_id)

    # تعیین چت مقصد
    dest = target.message if hasattr(target, "message") else target

    # اگر رسید عکس دارد، عکس را همراه caption نشان بده
    receipt_file_id = getattr(payment, "receipt_file_id", None)
    receipt_type    = getattr(payment, "receipt_type", None)

    if receipt_type == "photo" and receipt_file_id:
        # نمایش عکس رسید + اطلاعات تراکنش به عنوان caption
        try:
            await dest.answer_photo(
                photo=receipt_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except Exception:
            # اگر به هر دلیلی عکس ارسال نشد، با پیام متنی ادامه بده
            text += f"\n\n📷 <i>عکس رسید موجود است اما قابل نمایش نیست.</i>"
    elif receipt_type == "text" and receipt_file_id:
        # رسید متنی — متن رسید را به اطلاعات اضافه کن
        text += f"\n\n📝 <b>متن رسید:</b>\n<code>{receipt_file_id}</code>"

    await dest.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm_pending_payments")
async def cb_adm_pending_payments(callback: CallbackQuery) -> None:
    """صفحه اصلی مدیریت تراکنش‌ها — نمایش فیلترها."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from keyboards.admin import get_payments_filter_keyboard
    try:
        await callback.message.edit_text(
            "🧾 <b>مدیریت تراکنش‌ها</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "یک فیلتر انتخاب کنید یا با شناسه سفارش جستجو کنید:",
            parse_mode="HTML",
            reply_markup=get_payments_filter_keyboard(),
        )
    except Exception:
        await callback.message.answer(
            "🧾 <b>مدیریت تراکنش‌ها</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "یک فیلتر انتخاب کنید یا با شناسه سفارش جستجو کنید:",
            parse_mode="HTML",
            reply_markup=get_payments_filter_keyboard(),
        )


@router.callback_query(F.data.startswith("adm_tx_filter:"))
async def cb_adm_tx_filter(callback: CallbackQuery) -> None:
    """نمایش تراکنش‌ها بر اساس فیلتر."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    status_filter = callback.data.split(":")[1]
    label_map = {
        "pending":          "⏳ در صف",
        "pending_card":     "⏳ در صف — کارت‌به‌کارت",
        "pending_crypto":   "⏳ در صف — کریپتو",
        "confirmed":        "✅ موفق",
        "confirmed_card":   "✅ موفق — کارت‌به‌کارت",
        "confirmed_crypto": "✅ موفق — کریپتو",
        "failed":           "❌ ناموفق",
        "failed_card":      "❌ ناموفق — کارت‌به‌کارت",
        "failed_crypto":    "❌ ناموفق — کریپتو",
    }
    label = label_map.get(status_filter, status_filter)

    from database.crud import get_payments_filtered
    from database.models import Subscription

    async with AsyncSessionLocal() as session:
        payments = await get_payments_filtered(session, status_filter=status_filter, limit=15)
        if not payments:
            await callback.message.answer(
                f"📭 هیچ تراکنش <b>{label}</b> یافت نشد.",
                parse_mode="HTML",
            )
            return

        await callback.message.answer(
            f"🧾 <b>تراکنش‌ها {label}</b> — آخرین {len(payments)} مورد\n"
            "━━━━━━━━━━━━━━━",
            parse_mode="HTML",
        )
        for p in payments:
            res_u = await session.execute(select(User).where(User.id == p.user_id))
            user  = res_u.scalar_one_or_none()
            sub   = None
            if getattr(p, "subscription_id", None):
                from database.models import Subscription as SubModel
                res_s = await session.execute(
                    select(SubModel).where(SubModel.id == p.subscription_id)
                )
                sub = res_s.scalar_one_or_none()
            await _send_payment_card(callback, p, user, sub)


@router.callback_query(F.data.startswith("crypto_expire:"))
async def cb_crypto_expire(callback: CallbackQuery) -> None:
    """ادمین تراکنش کریپتوی گیر کرده را دستی منقضی می‌کند."""
    if not await _check_admin(callback):
        return

    order_id = callback.data.split(":", 1)[1]
    async with AsyncSessionLocal() as session:
        from database.crud import get_payment_by_order_id, update_payment_status
        payment = await get_payment_by_order_id(session, order_id)
        if not payment:
            await callback.answer("❌ تراکنش پیدا نشد.", show_alert=True)
            return
        if payment.status in ("confirmed", "finished"):
            await callback.answer("⚠️ این پرداخت قبلاً موفق بوده — نمی‌توان منقضی کرد.", show_alert=True)
            return
        await update_payment_status(session, payment.id, "expired")

    await callback.answer("⏰ تراکنش منقضی شد.", show_alert=True)
    try:
        new_text = (
            (callback.message.caption or callback.message.text or "") +
            f"\n\n⏰ <b>منقضی شد توسط ادمین {callback.from_user.id}</b>"
        )
        if callback.message.photo:
            await callback.message.edit_caption(new_text, parse_mode="HTML")
        else:
            await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer("⏰ تراکنش به عنوان منقضی علامت‌گذاری شد.")
    logger.info(f"تراکنش کریپتو {order_id} توسط ادمین منقضی شد.")


@router.callback_query(F.data == "adm_tx_search")
async def cb_adm_tx_search(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(TxSearchStates.waiting_order_id)
    await callback.message.answer(
        "🔍 <b>جستجو تراکنش‌ها</b>\n\n"
        "شناسه سفارش (order_id) را وارد کنید:\n"
        "مثال: <code>card_846119742_1_9fecaa0e</code>\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(TxSearchStates.waiting_order_id, F.text == "/cancel")
async def tx_search_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.")


@router.message(TxSearchStates.waiting_order_id, F.text)
async def tx_search_value(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear(); return
    await state.clear()
    order_id = (message.text or "").strip()

    from database.crud import get_payments_filtered, get_payment_by_order_id
    from database.models import Subscription as SubModel

    async with AsyncSessionLocal() as session:
        # جستجوی دقیق
        p = await get_payment_by_order_id(session, order_id)
        if not p:
            # جستجوی partial
            results = await get_payments_filtered(session, order_id_search=order_id, limit=5)
            if not results:
                await message.answer(f"❌ تراکنشی با شناسه <code>{order_id}</code> پیدا نشد.", parse_mode="HTML")
                return
            p = results[0]

        res_u = await session.execute(select(User).where(User.id == p.user_id))
        user  = res_u.scalar_one_or_none()
        sub   = None
        if getattr(p, "subscription_id", None):
            res_s = await session.execute(select(SubModel).where(SubModel.id == p.subscription_id))
            sub = res_s.scalar_one_or_none()

    await _send_payment_card(message, p, user, sub)


# ──────────────────────────────────────────────
# ➕ ایجاد اشتراک دستی توسط ادمین
# ──────────────────────────────────────────────

class ManualSubStates(StatesGroup):
    waiting_telegram_id  = State()
    waiting_plan         = State()
    # پلن دلخواه
    custom_inbound_pick  = State()   # انتخاب اینباند(ها)
    custom_traffic       = State()   # وارد کردن حجم
    custom_days          = State()   # وارد کردن روز
    custom_limit_ip      = State()   # وارد کردن تعداد دستگاه همزمان


@router.callback_query(F.data == "adm_manual_sub")
async def cb_adm_manual_sub(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await state.set_state(ManualSubStates.waiting_telegram_id)
    await callback.message.answer(
        "➕ <b>ایجاد اشتراک دستی</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "آی‌دی عددی تلگرام یا یوزرنیم کاربر را وارد کنید:\n\n"
        "• مثال آیدی: <code>123456789</code>\n"
        "• مثال یوزرنیم: <code>@username</code> یا <code>username</code>\n\n"
        "⚠️ کاربر باید قبلاً ربات را استارت کرده باشد.\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(ManualSubStates.waiting_telegram_id, F.text == "/cancel")
async def manual_sub_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ لغو شد.", reply_markup=get_admin_main_keyboard())


@router.message(ManualSubStates.waiting_telegram_id)
async def manual_sub_recv_uid(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear(); return

    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ ورودی خالی است.")
        return

    # ── جستجوی کاربر — منطق سه‌مرحله‌ای ──────────────────────────
    #
    # اگر آیدی عددی داده شد:
    #   → مستقیم از DB می‌خوانیم
    #
    # اگر یوزرنیم داده شد:
    #   مرحله ۱: ابتدا در DB جستجو می‌کنیم (سریع‌ترین راه)
    #   مرحله ۲: اگر در DB نبود، از API تلگرام آیدی را می‌گیریم
    #            (برای کسانی که یوزرنیمشان در DB قدیمی ذخیره نشده)
    #   مرحله ۳: با آیدی گرفته‌شده دوباره DB را چک می‌کنیم
    #
    # نکته مهم: get_chat() برای اکانت‌هایی که privacy دارند ممکن
    # است خطا دهد — به همین دلیل DB اولویت دارد.

    db_user = None
    target_tid = 0
    tg_username_from_api = None

    if query.lstrip("@").isdigit():
        # ── ورودی آیدی عددی ──────────────────────────────────────
        target_tid = int(query.lstrip("@"))
        async with AsyncSessionLocal() as session:
            db_user = await get_user_by_telegram_id(session, target_tid)

    else:
        # ── ورودی یوزرنیم ────────────────────────────────────────
        clean_username = query.lstrip("@")

        # مرحله ۱: جستجو در DB با یوزرنیم ذخیره‌شده
        async with AsyncSessionLocal() as session:
            db_user = await get_user_by_username(session, clean_username)

        if db_user:
            # پیدا شد در DB — آیدی را از DB می‌گیریم
            target_tid = db_user.telegram_id
        else:
            # مرحله ۲: در DB نبود — از API تلگرام آیدی را استعلام کن
            try:
                chat = await message.bot.get_chat(f"@{clean_username}")
                target_tid = chat.id
                tg_username_from_api = chat.username
            except Exception:
                # API تلگرام هم جواب نداد
                await message.answer(
                    f"❌ کاربری با یوزرنیم <code>@{clean_username}</code> پیدا نشد.\n\n"
                    "احتمالات:\n"
                    "• یوزرنیم اشتباه است\n"
                    "• کاربر هنوز ربات را استارت نکرده\n"
                    "• از <b>آیدی عددی</b> کاربر استفاده کنید",
                    parse_mode="HTML",
                )
                return

            # مرحله ۳: با آیدی گرفته‌شده از تلگرام، DB را چک کن
            async with AsyncSessionLocal() as session:
                db_user = await get_user_by_telegram_id(session, target_tid)

    # ── بررسی نهایی: کاربر باید در DB باشد ──────────────────────
    if not db_user:
        err_id = target_tid or query
        await message.answer(
            f"❌ کاربر <code>{err_id}</code> در دیتابیس ربات پیدا نشد.\n\n"
            "⚠️ کاربر باید ابتدا ربات را <b>استارت</b> کرده باشد.",
            parse_mode="HTML",
        )
        return

    target_tid = db_user.telegram_id

    # ── نمایش اطلاعات کاربر برای تأیید ادمین ────────────────────
    db_uname = f"@{db_user.username}" if db_user.username else "—"

    mismatch_note = ""
    if tg_username_from_api and db_user.username and \
            tg_username_from_api.lower() != db_user.username.lower():
        mismatch_note = (
            f"\n⚠️ یوزرنیم تلگرام (<code>@{tg_username_from_api}</code>) "
            f"با DB ({db_uname}) متفاوت است — کاربر یوزرنیم را تغییر داده.\n"
        )

    confirm_text = (
        f"✅ <b>کاربر شناسایی شد:</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🆔 آیدی ثابت: <code>{db_user.telegram_id}</code>\n"
        f"👤 نام: {db_user.first_name or '—'}\n"
        f"📎 یوزرنیم (DB): {db_uname}\n"
        f"{mismatch_note}\n"
        f"📋 پلن مورد نظر را انتخاب کنید:"
    )

    await state.update_data(target_telegram_id=target_tid)

    async with AsyncSessionLocal() as session:
        plans = await get_all_plans(session)
    active_plans = [p for p in plans if p.is_active]
    if not active_plans:
        await message.answer("❌ هیچ پلن فعالی وجود ندارد.")
        await state.clear()
        return

    from keyboards.admin import get_manual_sub_plans_keyboard
    await state.set_state(ManualSubStates.waiting_plan)
    await message.answer(confirm_text, parse_mode="HTML",
                         reply_markup=get_manual_sub_plans_keyboard(active_plans))


@router.callback_query(F.data.startswith("adm_msub_plan:"))
async def cb_manual_sub_plan(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    data = await state.get_data()
    target_tid = data.get("target_telegram_id")
    if not target_tid:
        await callback.message.answer("❌ جلسه منقضی شد. دوباره شروع کنید.")
        await state.clear(); return

    plan_id = int(callback.data.split(":")[1])
    processing = await callback.message.answer("⏳ در حال ایجاد اشتراک...")
    await state.clear()

    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        if not plan:
            await processing.edit_text("❌ پلن پیدا نشد.")
            return

        db_user = await get_user_by_telegram_id(session, target_tid)
        if not db_user:
            await processing.edit_text(
                f"❌ کاربر <code>{target_tid}</code> در دیتابیس پیدا نشد.",
                parse_mode="HTML",
            )
            return

        try:
            from services.subscription import create_new_subscription
            result = await create_new_subscription(
                session=session,
                user_id=db_user.id,
                telegram_id=target_tid,
                inbound_id=0,  # 0 = انتخاب خودکار بر اساس پلن
                plan_id=plan_id,
                traffic_gb=plan.traffic_gb,
                expire_days=plan.duration_days,
                username=db_user.username,
            )
        except Exception as e:
            logger.error(f"خطا در ایجاد اشتراک دستی: {e}")
            await processing.edit_text(f"❌ خطا: <code>{e}</code>", parse_mode="HTML")
            return

    # نمایش اطلاعات کاربر هدف در پیام ادمین
    uname_display = f"@{db_user.username}  " if db_user.username else ""
    ip_line = f"📡 محدودیت دستگاه: {result.limit_ip} همزمان\n" if result.limit_ip else ""

    await processing.delete()
    await callback.message.answer(
        f"✅ <b>اشتراک دستی ایجاد شد!</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: {uname_display}(<code>{target_tid}</code>)\n"
        f"📦 پلن: {plan.name}\n"
        f"📧 ایمیل: <code>{result.email}</code>\n"
        f"{ip_line}"
        f"\n🔗 لینک اشتراک:\n<code>{result.sub_link}</code>",
        parse_mode="HTML",
    )
    # ارسال به خود کاربر هم
    from aiogram.types import BufferedInputFile
    try:
        await callback.bot.send_message(
            chat_id=target_tid,
            text=(
                f"🎉 <b>اشتراک VPN شما توسط ادمین فعال شد!</b>\n\n"
                f"🔗 لینک اشتراک:\n<code>{result.sub_link}</code>"
            ),
            parse_mode="HTML",
        )
        if result.qr_bytes:
            await callback.bot.send_photo(
                chat_id=target_tid,
                photo=BufferedInputFile(result.qr_bytes, "sub_qr.png"),
                caption="📷 QR کد اشتراک شما",
            )
    except Exception as e:
        logger.warning(f"ارسال اشتراک دستی به کاربر {target_tid} ناموفق: {e}")


# ──────────────────────────────────────────────
# ➕ پلن دلخواه — انتخاب اینباند
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_msub_custom")
async def cb_msub_custom_start(callback: CallbackQuery, state: FSMContext) -> None:
    """شروع جریان پلن دلخواه — نمایش اینباندهای پنل."""
    if not await _check_admin(callback):
        return
    await callback.answer()

    data = await state.get_data()
    if not data.get("target_telegram_id"):
        await callback.message.answer("❌ جلسه منقضی شد. دوباره شروع کنید.")
        await state.clear()
        return

    try:
        async with _xui_client() as xui:
            inbounds = await xui.get_inbounds()
    except Exception as e:
        await callback.message.answer(f"❌ خطا در دریافت اینباندها: {e}")
        await state.clear()
        return

    if not inbounds:
        await callback.message.answer("❌ هیچ اینباندی در پنل یافت نشد.")
        await state.clear()
        return

    inb_list = [{"id": inb.id, "remark": getattr(inb, "remark", str(inb.id))} for inb in inbounds]
    await state.update_data(custom_inbounds_all=inb_list, custom_inbounds_selected=[])
    await state.set_state(ManualSubStates.custom_inbound_pick)

    from keyboards.admin import get_manual_sub_inbounds_keyboard
    await callback.message.answer(
        "⚙️ <b>پلن دلخواه — مرحله ۱/۳</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "اینباند(های) مورد نظر را انتخاب کنید.\n"
        "می‌توانید چند اینباند انتخاب کنید.\n"
        "بعد از انتخاب «ادامه» را بزنید.",
        parse_mode="HTML",
        reply_markup=get_manual_sub_inbounds_keyboard(inb_list, []),
    )


@router.callback_query(F.data.startswith("adm_msub_inb:"), ManualSubStates.custom_inbound_pick)
async def cb_msub_toggle_inbound(callback: CallbackQuery, state: FSMContext) -> None:
    """toggle انتخاب یک اینباند."""
    if not await _check_admin(callback):
        return
    await callback.answer()

    inb_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    selected: list = list(data.get("custom_inbounds_selected", []))
    all_inbs: list = data.get("custom_inbounds_all", [])

    if inb_id in selected:
        selected.remove(inb_id)
    else:
        selected.append(inb_id)

    await state.update_data(custom_inbounds_selected=selected)

    from keyboards.admin import get_manual_sub_inbounds_keyboard
    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_manual_sub_inbounds_keyboard(all_inbs, selected)
        )
    except Exception:
        pass


@router.callback_query(F.data == "adm_msub_inb_done", ManualSubStates.custom_inbound_pick)
async def cb_msub_inbound_done(callback: CallbackQuery, state: FSMContext) -> None:
    """تأیید انتخاب اینباندها — رفتن به مرحله حجم."""
    if not await _check_admin(callback):
        return
    await callback.answer()

    data = await state.get_data()
    selected = data.get("custom_inbounds_selected", [])
    if not selected:
        await callback.answer("⚠️ حداقل یک اینباند انتخاب کنید.", show_alert=True)
        return

    await state.set_state(ManualSubStates.custom_traffic)
    await callback.message.answer(
        "⚙️ <b>پلن دلخواه — مرحله ۲/۳</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "حجم اشتراک را به <b>گیگابایت</b> وارد کنید:\n\n"
        "• مثال: <code>10</code> (10 گیگابایت)\n"
        "• برای <b>نامحدود</b> عدد <code>0</code> را وارد کنید\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(ManualSubStates.custom_traffic)
async def cb_msub_recv_traffic(message: Message, state: FSMContext) -> None:
    """دریافت حجم — رفتن به مرحله روز."""
    if not await _check_admin(message):
        await state.clear(); return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.")
        return

    val = (message.text or "").strip()
    if not val.isdigit() or int(val) < 0:
        await message.answer("❌ لطفاً یک عدد معتبر وارد کنید (مثال: 10 یا 0 برای نامحدود).")
        return

    await state.update_data(custom_traffic_gb=int(val))
    await state.set_state(ManualSubStates.custom_days)
    await message.answer(
        "⚙️ <b>پلن دلخواه — مرحله ۳/۴</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "مدت اشتراک را به <b>روز</b> وارد کنید:\n\n"
        "• مثال: <code>30</code> (یک ماه)\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(ManualSubStates.custom_days)
async def cb_msub_recv_days(message: Message, state: FSMContext) -> None:
    """دریافت روز — رفتن به مرحله تعداد دستگاه."""
    if not await _check_admin(message):
        await state.clear(); return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.")
        return

    val = (message.text or "").strip()
    if not val.isdigit() or int(val) < 1:
        await message.answer("❌ لطفاً یک عدد مثبت وارد کنید (مثال: 30).")
        return

    await state.update_data(custom_expire_days=int(val))
    await state.set_state(ManualSubStates.custom_limit_ip)
    await message.answer(
        "⚙️ <b>پلن دلخواه — مرحله ۴/۴</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "تعداد دستگاه همزمان را وارد کنید:\n\n"
        "• مثال: <code>1</code> (یک دستگاه)\n"
        "• برای <b>نامحدود</b> عدد <code>0</code> را وارد کنید\n\n"
        "برای لغو: /cancel",
        parse_mode="HTML",
    )


@router.message(ManualSubStates.custom_limit_ip)
async def cb_msub_recv_limit_ip(message: Message, state: FSMContext) -> None:
    """دریافت تعداد دستگاه — ساخت نهایی اشتراک دلخواه."""
    if not await _check_admin(message):
        await state.clear(); return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.")
        return

    val = (message.text or "").strip()
    if not val.isdigit() or int(val) < 0:
        await message.answer("❌ لطفاً یک عدد معتبر وارد کنید (مثال: 2 یا 0 برای نامحدود).")
        return

    limit_ip    = int(val)
    data        = await state.get_data()
    await state.clear()

    target_tid    = data.get("target_telegram_id")
    traffic_gb    = data.get("custom_traffic_gb", 0)
    expire_days   = data.get("custom_expire_days", 30)
    selected_inbs = data.get("custom_inbounds_selected", [])

    processing = await message.answer("⏳ در حال ایجاد اشتراک دلخواه...")

    try:
        async with AsyncSessionLocal() as session:
            db_user = await get_user_by_telegram_id(session, target_tid)
            if not db_user:
                await processing.edit_text(f"❌ کاربر {target_tid} پیدا نشد.")
                return

            from services.subscription import create_new_subscription
            primary_inbound = selected_inbs[0]
            result = await create_new_subscription(
                session=session,
                user_id=db_user.id,
                telegram_id=target_tid,
                inbound_id=primary_inbound,
                traffic_gb=traffic_gb,
                expire_days=expire_days,
                limit_ip=limit_ip,
                extra_inbound_ids=selected_inbs[1:] if len(selected_inbs) > 1 else [],
                username=db_user.username,
            )
    except Exception as e:
        logger.error(f"خطا در ایجاد اشتراک دلخواه: {e}")
        await processing.edit_text(f"❌ خطا: <code>{e}</code>", parse_mode="HTML")
        return

    traffic_str  = f"{traffic_gb} GB" if traffic_gb > 0 else "نامحدود"
    limit_str    = f"{limit_ip} دستگاه" if limit_ip > 0 else "نامحدود"
    inbs_str     = ", ".join(str(i) for i in selected_inbs)
    uname_disp   = f"@{db_user.username}  " if db_user.username else ""

    await processing.delete()
    await message.answer(
        f"✅ <b>اشتراک دلخواه ایجاد شد!</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: {uname_disp}(<code>{target_tid}</code>)\n"
        f"📦 حجم: {traffic_str}\n"
        f"📅 مدت: {expire_days} روز\n"
        f"📡 دستگاه همزمان: {limit_str}\n"
        f"🔌 اینباند(ها): {inbs_str}\n"
        f"📧 ایمیل: <code>{result.email}</code>\n\n"
        f"🔗 لینک اشتراک:\n<code>{result.sub_link}</code>",
        parse_mode="HTML",
    )

    # ارسال به خود کاربر
    from aiogram.types import BufferedInputFile
    try:
        await message.bot.send_message(
            chat_id=target_tid,
            text=(
                "🎉 <b>اشتراک VPN شما توسط ادمین فعال شد!</b>\n\n"
                f"🔗 لینک اشتراک:\n<code>{result.sub_link}</code>"
            ),
            parse_mode="HTML",
        )
        if result.qr_bytes:
            await message.bot.send_photo(
                chat_id=target_tid,
                photo=BufferedInputFile(result.qr_bytes, "sub_qr.png"),
                caption="📷 QR کد اشتراک شما",
            )
    except Exception as e:
        logger.warning(f"ارسال اشتراک دلخواه به کاربر {target_tid} ناموفق: {e}")


@router.callback_query(F.data == "adm_msub_cancel")
async def cb_msub_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("❌ لغو شد.", reply_markup=get_admin_main_keyboard())


# ──────────────────────────────────────────────
# 🎁 تنظیمات اشتراک تست
# ──────────────────────────────────────────────

class TestSubEditStates(StatesGroup):
    waiting_value = State()


class ReferralEditStates(StatesGroup):
    waiting_days    = State()
    waiting_traffic = State()
    waiting_custom  = State()  # ترکیب traffic:days


def _get_test_sub_keyboard(enabled: bool, traffic: float, days: int):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    toggle_text = "🔴 غیرفعال کردن تست" if enabled else "✅ فعال کردن تست"
    # نمایش حجم: اگه زیر 1 GB بود به MB تبدیل کن
    traffic_label = f"{traffic:g} GB" if traffic >= 1 else f"{traffic * 1024:.0f} MB"
    kb.button(text=toggle_text,                        callback_data="adm_test_toggle")
    kb.button(text=f"📦 حجم: {traffic_label}",         callback_data="adm_test_edit:traffic")
    kb.button(text=f"⏱ مدت: {days} روز",              callback_data="adm_test_edit:days")
    kb.button(text="🔙 بازگشت",                        callback_data="adm_back")
    kb.adjust(1, 2, 1)
    return kb.as_markup()


async def _show_test_sub_settings(target: CallbackQuery) -> None:
    """نمایش صفحه تنظیمات اشتراک تست."""
    from database.crud import get_setting
    async with AsyncSessionLocal() as session:
        enabled_raw = await get_setting(session, "test_sub_enabled", "true")
        traffic_raw = await get_setting(session, "test_sub_traffic_gb",
                                        str(settings.test_traffic_gb))
        days_raw    = await get_setting(session, "test_sub_duration_days",
                                        str(settings.test_duration_days))

    enabled = enabled_raw.lower() == "true"
    try:
        traffic = float(traffic_raw) if traffic_raw else float(settings.test_traffic_gb)
    except (ValueError, TypeError):
        traffic = float(settings.test_traffic_gb)
    try:
        days = int(days_raw) if days_raw.isdigit() else settings.test_duration_days
    except (ValueError, TypeError):
        days = settings.test_duration_days

    # نمایش حجم: اگر زیر 1 GB بود به MB تبدیل کن
    traffic_label = f"{traffic:g} GB" if traffic >= 1 else f"{traffic * 1024:.0f} MB"
    status_icon = "✅ فعال" if enabled else "🔴 غیرفعال"
    text = (
        "🎁 <b>تنظیمات اشتراک تست رایگان</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"وضعیت: <b>{status_icon}</b>\n"
        f"📦 حجم: <b>{traffic_label}</b>\n"
        f"⏱ مدت: <b>{days} روز</b>\n\n"
        "• هر کاربر فقط یک‌بار می‌تواند از اشتراک تست استفاده کند.\n"
        "• برای تغییر حجم یا مدت، روی دکمه مربوطه کلیک کنید.\n"
        "• برای حجم زیر ۱ گیگابایت، عدد اعشاری وارد کنید مثلاً: <code>0.05</code> = 50 MB"
    )
    try:
        await target.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=_get_test_sub_keyboard(enabled, traffic, days),
        )
    except Exception:
        await target.message.answer(
            text, parse_mode="HTML",
            reply_markup=_get_test_sub_keyboard(enabled, traffic, days),
        )


@router.callback_query(F.data == "adm_test_sub_settings")
async def cb_adm_test_sub_settings(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await _show_test_sub_settings(callback)


@router.callback_query(F.data == "adm_test_toggle")
async def cb_adm_test_toggle(callback: CallbackQuery) -> None:
    """فعال/غیرفعال کردن اشتراک تست."""
    if not await _check_admin(callback):
        return
    from database.crud import get_setting, set_setting
    async with AsyncSessionLocal() as session:
        current = await get_setting(session, "test_sub_enabled", "true")
        new_val = "false" if current.lower() == "true" else "true"
        await set_setting(session, "test_sub_enabled", new_val)

    label = "✅ فعال شد" if new_val == "true" else "🔴 غیرفعال شد"
    await callback.answer(f"اشتراک تست {label}", show_alert=False)
    await _show_test_sub_settings(callback)


@router.callback_query(F.data.startswith("adm_test_edit:"))
async def cb_adm_test_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """شروع ویرایش حجم یا مدت اشتراک تست."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    field = callback.data.split(":")[1]
    if field == "traffic":
        prompt = (
            "📦 حجم جدید اشتراک تست را به <b>GB</b> وارد کنید:\n"
            "• عدد صحیح: <code>1</code> = 1 گیگابایت\n"
            "• عدد اعشاری: <code>0.05</code> = 50 مگابایت\n"
            "• <code>0.5</code> = 500 مگابایت"
        )
    else:
        prompt = "⏱ مدت جدید اشتراک تست را به <b>روز</b> وارد کنید:\nمثال: <code>1</code>"
    await state.set_state(TestSubEditStates.waiting_value)
    await state.update_data(field=field)
    await callback.message.answer(prompt + "\n\nبرای لغو: /cancel", parse_mode="HTML")


@router.message(TestSubEditStates.waiting_value)
async def msg_test_sub_edit_value(message: Message, state: FSMContext) -> None:
    if not await _check_admin(message):
        await state.clear()
        return
    data  = await state.get_data()
    field = data.get("field", "")
    val   = (message.text or "").strip().replace(",", ".")

    from database.crud import set_setting

    if field == "traffic":
        # پشتیبانی از اعداد اعشاری برای حجم (مثلاً 0.05 = 50 MB)
        try:
            traffic_val = float(val)
            if traffic_val <= 0:
                raise ValueError("باید مثبت باشد")
        except (ValueError, TypeError):
            await message.answer(
                "❌ لطفاً یک عدد مثبت وارد کنید.\n"
                "مثال: <code>1</code> یا <code>0.05</code>",
                parse_mode="HTML",
            )
            return
        async with AsyncSessionLocal() as session:
            await set_setting(session, "test_sub_traffic_gb", str(traffic_val))
        traffic_label = f"{traffic_val:g} GB" if traffic_val >= 1 else f"{traffic_val * 1024:.0f} MB"
        label = f"📦 حجم اشتراک تست به <b>{traffic_label}</b> تغییر کرد."
    else:
        # مدت فقط عدد صحیح قبول می‌کند
        if not val.isdigit() or int(val) < 1:
            await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید.")
            return
        async with AsyncSessionLocal() as session:
            await set_setting(session, "test_sub_duration_days", val)
        label = f"⏱ مدت اشتراک تست به <b>{val} روز</b> تغییر کرد."

    await state.clear()
    await message.answer(f"✅ {label}", parse_mode="HTML")


# ──────────────────────────────────────────────
# 🎨 مدیریت تم ربات
# ──────────────────────────────────────────────

@router.callback_query(F.data == "adm_theme")
async def cb_adm_theme(callback: CallbackQuery) -> None:
    """نمایش صفحه انتخاب تم ربات."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from services.theme import get_current_theme, THEMES
    from keyboards.admin import get_theme_keyboard
    t = await get_current_theme()
    text = (
        "🎨 <b>تم ربات</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"تم فعلی: <b>{t.label}</b>\n\n"
        "• <b>✦ Moonstone</b> — طلایی گرم، ظریف و لوکس (پیش‌فرض)\n"
        "• <b>✦ Rose Ember</b> — قرمز گرم، شیک و جذاب\n\n"
        "روی تم مورد نظر کلیک کنید تا فوری اعمال شود:"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                         reply_markup=get_theme_keyboard(t.name))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                      reply_markup=get_theme_keyboard(t.name))


@router.callback_query(F.data.startswith("adm_theme_set:"))
async def cb_adm_theme_set(callback: CallbackQuery) -> None:
    """اعمال تم انتخاب‌شده."""
    if not await _check_admin(callback):
        return
    theme_name = callback.data.split(":")[1]
    from services.theme import set_theme, get_current_theme, THEMES
    from keyboards.admin import get_theme_keyboard
    if theme_name not in THEMES:
        await callback.answer("❌ تم نامعتبر!", show_alert=True)
        return
    await set_theme(theme_name)
    t = THEMES[theme_name]
    await callback.answer(f"✅ تم به «{t.label}» تغییر کرد!", show_alert=True)
    # رفرش صفحه
    current = await get_current_theme()
    text = (
        "🎨 <b>تم ربات</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"تم فعلی: <b>{current.label}</b>\n\n"
        "• <b>✦ Moonstone</b> — طلایی گرم، ظریف و لوکس (پیش‌فرض)\n"
        "• <b>✦ Rose Ember</b> — قرمز گرم، شیک و جذاب\n\n"
        "روی تم مورد نظر کلیک کنید تا فوری اعمال شود:"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                         reply_markup=get_theme_keyboard(current.name))
    except Exception:
        pass


# ══════════════════════════════════════════════
# 👮 مدیریت ادمین‌ها
# ══════════════════════════════════════════════

def _is_main_admin(user_id: int) -> bool:
    """
    بررسی اینکه آیا کاربر ادمین اصلی (.env) است.
    مقایسه int-safe — هر دو طرف به int تبدیل می‌شوند.
    """
    uid = int(user_id)
    return any(int(x) == uid for x in settings.admin_ids)


@router.callback_query(F.data == "adm_managers")
async def cb_adm_managers(callback: CallbackQuery) -> None:
    """منوی اصلی مدیریت ادمین‌ها."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    uid = callback.from_user.id

    async with AsyncSessionLocal() as session:
        locked = await is_admin_login_locked(session)

    lock_status = "🔒 فعال — هیچکس نمی‌تواند وارد شود" if locked else "🔓 غیرفعال — ورود آزاد است"
    is_main = _is_main_admin(uid)
    main_notice = "\n\n<b>⚠️ فقط ادمین اصلی می‌تواند دسترسی‌ها را مدیریت کند.</b>" if not is_main else ""

    text = (
        "👮 <b>مدیریت ادمین‌ها</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"🔑 وضعیت قفل ورود: {lock_status}\n"
        f"{main_notice}"
    )
    await _safe_edit_admin(callback, text, parse_mode="HTML",
                           reply_markup=get_admin_management_keyboard(locked))


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    """دکمه‌های غیرفعال — فقط یه پیام کوتاه نشون می‌ده."""
    await callback.answer("👑 ادمین اصلی — قابل ویرایش نیست.", show_alert=False)


@router.callback_query(F.data == "adm_debug_ids")
async def cb_adm_debug_ids(callback: CallbackQuery) -> None:
    """نمایش admin_ids در حال اجرا — برای عیب‌یابی."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    uid = callback.from_user.id

    # مقادیر واقعی از settings در حافظه
    raw_ids = settings.admin_ids
    ids_str = ", ".join(str(x) for x in raw_ids) if raw_ids else "(خالی — مشکل parse!)"
    is_me_in_env = uid in [int(x) for x in raw_ids] if raw_ids else False

    # بررسی DB
    async with AsyncSessionLocal() as session:
        me_db = await get_user_by_telegram_id(session, uid)

    db_admin_status = "✅ is_admin=True در DB" if (me_db and me_db.is_admin) else "❌ is_admin=False در DB"
    env_status = "✅ در ADMIN_IDS هستید" if is_me_in_env else "❌ در ADMIN_IDS نیستید!"

    text = (
        "🔍 <b>Debug — وضعیت ادمین</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"👤 آیدی شما: <code>{uid}</code>\n\n"
        f"📋 ADMIN_IDS در حافظه:\n<code>{ids_str}</code>\n\n"
        f"🔑 وضعیت .env: {env_status}\n"
        f"🗄 وضعیت DB: {db_admin_status}\n\n"
        "ℹ️ اگه ADMIN_IDS خالی است، مشکل parse .env دارید.\n"
        "مطمئن شوید فایل .env روی سرور درست تنظیم شده:\n"
        "<code>ADMIN_IDS=123456789</code>\n\n"
        "بعد از تغییر .env باید container را restart کنید:\n"
        "<code>docker compose up -d</code>"
    )
    await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "adm_mgr_list")
async def cb_adm_mgr_list(callback: CallbackQuery) -> None:
    """لیست همه ادمین‌های فعال."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    uid = callback.from_user.id

    async with AsyncSessionLocal() as session:
        db_admins = await get_all_db_admins(session)

    main_ids = [int(x) for x in settings.admin_ids]
    is_main = _is_main_admin(uid)

    # ادمین‌هایی که فقط در .env هستند (هنوز در DB ثبت نشدن) رو هم نشون بده
    db_tids = {u.telegram_id for u in db_admins}
    env_only_ids = [tid for tid in main_ids if tid not in db_tids]

    lines = []
    for adm in db_admins:
        is_m = adm.telegram_id in main_ids
        crown = "👑" if is_m else "🛡"
        role = "ادمین اصلی" if is_m else "ادمین فرعی"
        uname = f"@{adm.username}" if adm.username else "—"
        lines.append(f"{crown} <b>{role}</b> | {uname} | <code>{adm.telegram_id}</code>")

    for tid in env_only_ids:
        lines.append(f"👑 <b>ادمین اصلی</b> | — | <code>{tid}</code>  <i>(فقط .env)</i>")

    if not lines:
        body = "هیچ ادمینی ثبت نشده."
    else:
        body = "\n".join(lines)

    # tip فقط وقتی ادمین فرعی وجود داره نشون بده
    has_secondary = any(adm.telegram_id not in main_ids for adm in db_admins)
    tip = "\n\n<i>روی نام ادمین فرعی کلیک کنید تا دسترسی او را مدیریت کنید.</i>" if (is_main and has_secondary) else ""
    text = (
        "👮 <b>لیست ادمین‌های فعال</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"{body}"
        f"{tip}"
    )
    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_admin_list_keyboard(db_admins, main_ids, uid),
    )


@router.callback_query(F.data.startswith("adm_mgr_view:"))
async def cb_adm_mgr_view(callback: CallbackQuery) -> None:
    """جزئیات یک ادمین فرعی — فقط ادمین اصلی دسترسی دارد."""
    if not await _check_admin(callback):
        return
    uid = callback.from_user.id
    if not _is_main_admin(uid):
        await callback.answer("⛔ فقط ادمین اصلی می‌تواند این بخش را ببیند.", show_alert=True)
        return

    target_tid = int(callback.data.split(":")[1])
    await callback.answer()

    # بارگذاری اطلاعات مورد نیاز
    async with AsyncSessionLocal() as session:
        target_user = await get_user_by_telegram_id(session, target_tid)
        db_admins = await get_all_db_admins(session)

    main_ids = [int(x) for x in settings.admin_ids]

    if not target_user:
        await callback.message.answer("❌ کاربر پیدا نشد.")  # type: ignore[union-attr]
        return

    if target_tid in main_ids:
        await callback.answer("⛔ ادمین اصلی قابل ویرایش نیست.", show_alert=True)
        return

    uname = f"@{target_user.username}" if target_user.username else "—"
    fname = target_user.first_name or "—"
    text = (
        "🛡 <b>اطلاعات ادمین فرعی</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"👤 نام: <b>{fname}</b>\n"
        f"🔖 یوزرنیم: {uname}\n"
        f"🆔 آیدی: <code>{target_tid}</code>\n\n"
        "می‌توانید دسترسی ادمینی این کاربر را لغو کنید."
    )
    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_admin_detail_keyboard(target_tid, is_main_admin=_is_main_admin(uid)),
    )


@router.callback_query(F.data == "adm_mgr_lock_toggle")
async def cb_adm_mgr_lock_toggle(callback: CallbackQuery) -> None:
    """درخواست تغییر وضعیت قفل ورود — فقط ادمین اصلی."""
    if not await _check_admin(callback):
        return
    uid = callback.from_user.id
    if not _is_main_admin(uid):
        await callback.answer("⛔ فقط ادمین اصلی می‌تواند قفل ورود را تغییر دهد.", show_alert=True)
        return
    await callback.answer()

    async with AsyncSessionLocal() as session:
        locked = await is_admin_login_locked(session)

    # می‌خواهیم state را toggle کنیم
    going_to_lock = not locked

    if going_to_lock:
        text = (
            "🔒 <b>فعال‌سازی قفل کامل ورود</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "⚠️ با فعال کردن این گزینه:\n"
            "• <b>هیچ‌کس</b> نمی‌تواند با <code>/admin_secret</code> وارد شود\n"
            "• ادمین‌های فعلی دسترسی خود را حفظ می‌کنند\n"
            "• فقط شما (ادمین اصلی) می‌توانید این قفل را باز کنید\n\n"
            "آیا اطمینان دارید؟"
        )
    else:
        text = (
            "🔓 <b>غیرفعال‌سازی قفل ورود</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "با غیرفعال کردن این قفل، افراد دارای رمز عبور می‌توانند وارد حالت ادمین شوند.\n\n"
            "آیا اطمینان دارید؟"
        )

    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_admin_lock_confirm_keyboard(going_to_lock),
    )


@router.callback_query(F.data.startswith("adm_mgr_lock_do:"))
async def cb_adm_mgr_lock_do(callback: CallbackQuery) -> None:
    """اعمال تغییر قفل ورود — فقط ادمین اصلی."""
    if not await _check_admin(callback):
        return
    uid = callback.from_user.id
    if not _is_main_admin(uid):
        await callback.answer("⛔ فقط ادمین اصلی می‌تواند این کار را انجام دهد.", show_alert=True)
        return

    new_state = callback.data.split(":")[1] == "1"
    await callback.answer()

    async with AsyncSessionLocal() as session:
        await set_admin_login_lock(session, new_state)

    if new_state:
        msg = "🔒 قفل ورود فعال شد — هیچ‌کس نمی‌تواند با رمز وارد شود."
        logger.warning(f"قفل ورود ادمین توسط {uid} فعال شد.")
    else:
        msg = "🔓 قفل ورود غیرفعال شد — ورود با رمز آزاد است."
        logger.info(f"قفل ورود ادمین توسط {uid} غیرفعال شد.")

    lock_status = "🔒 فعال" if new_state else "🔓 غیرفعال"
    text = (
        "👮 <b>مدیریت ادمین‌ها</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"✅ {msg}\n\n"
        f"🔑 وضعیت قفل ورود: {lock_status}"
    )
    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_admin_management_keyboard(new_state),
    )


@router.callback_query(F.data.startswith("adm_mgr_revoke_confirm:"))
async def cb_adm_mgr_revoke_confirm(callback: CallbackQuery) -> None:
    """تأیید لغو دسترسی ادمین فرعی."""
    if not await _check_admin(callback):
        return
    uid = callback.from_user.id
    if not _is_main_admin(uid):
        await callback.answer("⛔ فقط ادمین اصلی می‌تواند این کار را انجام دهد.", show_alert=True)
        return

    target_tid = int(callback.data.split(":")[1])
    await callback.answer()

    async with AsyncSessionLocal() as session:
        target_user = await get_user_by_telegram_id(session, target_tid)

    uname = f"@{target_user.username}" if (target_user and target_user.username) else f"#{target_tid}"
    text = (
        "⚠️ <b>تأیید لغو دسترسی</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"آیا مطمئن هستید که می‌خواهید دسترسی ادمینی <b>{uname}</b> را لغو کنید؟\n\n"
        "این کاربر دیگر نمی‌تواند وارد پنل مدیریت شود."
    )
    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_admin_revoke_confirm_keyboard(target_tid),
    )


@router.callback_query(F.data.startswith("adm_mgr_revoke:"))
async def cb_adm_mgr_revoke(callback: CallbackQuery) -> None:
    """اجرای لغو دسترسی ادمین فرعی."""
    if not await _check_admin(callback):
        return
    uid = callback.from_user.id
    if not _is_main_admin(uid):
        await callback.answer("⛔ فقط ادمین اصلی می‌تواند این کار را انجام دهد.", show_alert=True)
        return

    target_tid = int(callback.data.split(":")[1])
    main_ids_check = [int(x) for x in settings.admin_ids]

    if target_tid in main_ids_check:
        await callback.answer("⛔ ادمین اصلی قابل حذف نیست.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        done = await revoke_admin(session, target_tid)

    if done:
        await callback.answer("✅ دسترسی ادمین لغو شد.", show_alert=True)
        logger.success(f"ادمین {target_tid} توسط {uid} لغو دسترسی شد.")
    else:
        await callback.answer("❌ کاربر پیدا نشد.", show_alert=True)

    # برگشت به لیست
    async with AsyncSessionLocal() as session2:
        db_admins = await get_all_db_admins(session2)
    main_ids = [int(x) for x in settings.admin_ids]

    lines = []
    for adm in db_admins:
        is_m = adm.telegram_id in main_ids
        crown = "👑" if is_m else "🛡"
        role = "ادمین اصلی" if is_m else "ادمین فرعی"
        uname = f"@{adm.username}" if adm.username else "—"
        lines.append(f"{crown} <b>{role}</b> | {uname} | <code>{adm.telegram_id}</code>")

    body = "\n".join(lines) if lines else "هیچ ادمینی ثبت نشده."
    text = (
        "👮 <b>لیست ادمین‌های فعال</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"{body}\n\n"
        "<i>✅ دسترسی لغو شد.</i>"
    )
    await _safe_edit_admin(
        callback, text, parse_mode="HTML",
        reply_markup=get_admin_list_keyboard(db_admins, main_ids, uid),
    )


# ══════════════════════════════════════════════
# 🔗 تنظیمات Referral (دعوت دوستان)
# ══════════════════════════════════════════════

async def _show_referral_settings(target: CallbackQuery) -> None:
    """نمایش صفحه تنظیمات referral."""
    from database.crud import get_setting, get_all_plans
    from services.referral import get_referral_settings

    cfg = await get_referral_settings()
    reward_type    = cfg["reward_type"]
    trigger        = cfg["trigger"]
    reward_plan_id = cfg["reward_plan_id"]
    custom_traffic = cfg["custom_traffic"]
    custom_days    = cfg["custom_days"]
    inbound_id     = cfg["inbound_id"]

    async with AsyncSessionLocal() as session:
        all_plans = await get_all_plans(session)

    inbound_name = ""
    if inbound_id:
        try:
            async with _xui_client() as xui:
                _inbs = await xui.get_inbounds()
            for _inb in _inbs:
                if _inb.id == inbound_id:
                    inbound_name = _inb.remark
                    break
        except Exception:
            pass

    reward_plan_name = ""
    for p in all_plans:
        if p.id == reward_plan_id:
            reward_plan_name = f"{p.name} ({p.traffic_gb}GB/{p.duration_days}روز)"
            break

    if reward_type == "plan":
        type_display   = "📦 پلن رایگان کامل"
        reward_display = reward_plan_name or "❌ انتخاب نشده"
    else:
        type_display   = "⚙️ کانفیگ دلخواه"
        inb_part = f" — 🔌 {inbound_name}" if inbound_name else ""
        reward_display = f"{custom_traffic:g} GB / {custom_days} روز{inb_part}"

    trigger_labels = {
        "on_register":       "📋 هنگام ثبت‌مام",
        "on_first_purchase": "🛒 اولین خرید",
        "on_every_purchase": "🔄 هر خرید",
    }
    trigger_display = trigger_labels.get(trigger, trigger)
    enabled_display = "✅ فعال" if cfg["enabled"] else "❌ غیرفعال"
    text = (
        "🔗 <b>تنظیمات سیستم دعوت (Referral)</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"⚡️ <b>وضعیت:</b> {enabled_display}\n"
        f"🎁 <b>نوع پاداش:</b> {type_display}\n"
        f"💎 <b>پاداش:</b> {reward_display}\n"
        f"🕐 <b>زمان اعطا:</b> {trigger_display}\n\n"
        "<i>پاداش به دعوت‌کننده داده می‌شود، نه کاربر دعوت‌شده.</i>"
    )

    await _safe_edit_admin(
        target, text, parse_mode="HTML",
        reply_markup=get_referral_settings_keyboard(
            reward_type=reward_type,
            trigger=trigger,
            reward_days=custom_days,
            reward_plan_name=reward_plan_name,
            custom_traffic=custom_traffic,
            custom_days=custom_days,
            inbound_name=inbound_name,
        ),
    )


@router.callback_query(F.data == "adm_ref_settings")
async def cb_adm_ref_settings(callback: CallbackQuery) -> None:
    if not await _check_admin(callback):
        return
    await callback.answer()
    await _show_referral_settings(callback)


@router.callback_query(F.data.startswith("adm_ref_type:"))
async def cb_adm_ref_type(callback: CallbackQuery) -> None:
    """تغییر نوع پاداش: custom یا plan."""
    if not await _check_admin(callback):
        return
    new_type = callback.data.split(":")[1]
    if new_type not in ("custom", "plan", "days"):
        await callback.answer("نوع نامضتبر", show_alert=True)
        return
    if new_type == "days":
        new_type = "custom"
    from database.crud import set_setting
    from services.referral import REFERRAL_REWARD_TYPE_KEY
    async with AsyncSessionLocal() as session:
        await set_setting(session, REFERRAL_REWARD_TYPE_KEY, new_type)
    await callback.answer("✅ ذخیره شد")
    await _show_referral_settings(callback)


@router.callback_query(F.data.startswith("adm_ref_trigger:"))
async def cb_adm_ref_trigger(callback: CallbackQuery) -> None:
    """تعییر trigger پاداش."""
    if not await _check_admin(callback):
        return
    new_trigger = callback.data.split(":")[1]
    if new_trigger not in ("on_register", "on_first_purchase", "on_every_purchase"):
        await callback.answer("trigger نامضت اتبر", show_alert=True)
        return
    from database.crud import set_setting
    from services.referral import REFERRAL_TRIGGER_KEY
    async with AsyncSessionLocal() as session:
        await set_setting(session, REFERRAL_TRIGGER_KEY, new_trigger)
    await callback.answer("✅ ذخیره شد")
    await _show_referral_settings(callback)


@router.callback_query(F.data.startswith("adm_ref_edit:"))
async def cb_adm_ref_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """شروع ویرایش حجم، روز یا اینباند referral."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    field = callback.data.split(":")[1]

    if field == "inbound":
        from database.crud import get_setting
        from services.referral import REFERRAL_INBOUND_ID_KEY
        async with AsyncSessionLocal() as session:
            inbound_id_raw = await get_setting(session, REFERRAL_INBOUND_ID_KEY, "0")
        try:
            current_id = int(inbound_id_raw) if inbound_id_raw else 0
        except Exception:
            current_id = 0
        try:
            async with _xui_client() as xui:
                inbounds = await xui.get_inbounds()
        except Exception as e:
            await callback.message.answer(f"❌ خطا در دریافت اینباندها از پنل: {e}")
            return
        if not inbounds:
            await callback.message.answer("⚠️ هیچ اینباندی در پنل یافت نشد.")
            return
        from keyboards.admin import get_referral_inbound_keyboard
        await _safe_edit_admin(
            callback,
            "🔌 <b>اینباند پاداش referral را انتخاب کنید:</b>\n\nکانفیگ پاداش روی این اینباند ساخته می‌شود.",
            parse_mode="HTML",
            reply_markup=get_referral_inbound_keyboard(inbounds, current_id),
        )
        return

    if field == "traffic":
        from services.referral import get_referral_settings
        cfg = await get_referral_settings()
        t = cfg["custom_traffic"]
        prompt = (
            f"📦 <b>حجم پاداش را به GB وارد کنید:</b>\n"
            f"مقدار فصلی: <code>{t:g} GB</code>\n\n"
            "مثال: <code>5</code> یا <code>10</code>\n"
            "برای لغو: /cancel"
        )
        await state.set_state(ReferralEditStates.waiting_traffic)
    else:
        from services.referral import get_referral_settings
        cfg = await get_referral_settings()
        d = cfg["custom_days"]
        prompt = (
            f"⏱ <b>مدت پاداش را به روز وارد کنید:</b>\n"
            f"مقدار فعلی: <code>{d} روز</code>\n\n"
            "مثال: <code>30</code>\n"
            "برای لغو: /cancel"
        )
        await state.set_state(ReferralEditStates.waiting_days)

    await callback.message.answer(prompt, parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_ref_set_inbound:"))
async def cb_adm_ref_set_inbound(callback: CallbackQuery) -> None:
    """ذخیره اینباند انتخاب‌شده برای پاداش referral."""
    if not await _check_admin(callback):
        return
    inbound_id = int(callback.data.split(":")[1])
    from database.crud import set_setting
    from services.referral import REFERRAL_INBOUND_ID_KEY
    inbound_remark = str(inbound_id)
    try:
        async with _xui_client() as xui:
            _inbs = await xui.get_inbounds()
        for _inb in _inbs:
            if _inb.id == inbound_id:
                inbound_remark = _inb.remark
                break
    except Exception:
        pass
    async with AsyncSessionLocal() as session:
        await set_setting(session, REFERRAL_INBOUND_ID_KEY, str(inbound_id))
    await callback.answer(f"✅ اینباند «{inbound_remark}» انتخاب شد")
    await _show_referral_settings(callback)


@router.message(ReferralEditStates.waiting_traffic)
async def msg_referral_traffic_input(message: Message, state: FSMContext) -> None:
    """دریافت حجم پاداش."""
    raw = (message.text or "").strip().replace(",", ".")
    if raw == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.", reply_markup=get_admin_main_keyboard())
        return
    try:
        traffic = float(raw)
        if traffic <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ لطفاً یک ضدد مثبت وارد کنید.\nمثال: <code>5</code>", parse_mode="HTML")
        return
    from database.crud import set_setting
    from services.referral import REFERRAL_CUSTOM_TRAFFIC_KEY
    async with AsyncSessionLocal() as session:
        await set_setting(session, REFERRAL_CUSTOM_TRAFFIC_KEY, str(traffic))
    await state.clear()
    await message.answer(
        f"✅ حجم پاداش referral به <b>{traffic:g} GB</b> تغییر کرد.",
        parse_mode="HTML",
        reply_markup=get_admin_main_keyboard(),
    )


@router.message(ReferralEditStates.waiting_days)
async def msg_referral_days_input(message: Message, state: FSMContext) -> None:
    """دریافت مدت پاداش."""
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.", reply_markup=get_admin_main_keyboard())
        return
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("⚠️ لطفاً یک عدد صحیح مثبت وارد کنید.\nمثال: <code>30</code>", parse_mode="HTML")
        return
    days = int(raw)
    from database.crud import set_setting
    from services.referral import REFERRAL_CUSTOM_DAYS_KEY
    async with AsyncSessionLocal() as session:
        await set_setting(session, REFERRAL_CUSTOM_DAYS_KEY, str(days))
    await state.clear()
    await message.answer(
        f"✅ مدت پاداش referral به <b>{days} روز</b> تغییر کرد.",
        parse_mode="HTML",
        reply_markup=get_admin_main_keyboard(),
    )


@router.callback_query(F.data == "adm_ref_choose_plan")
async def cb_adm_ref_choose_plan(callback: CallbackQuery) -> None:
    """نمایش لیست پلن‌ها برای انتخاب پاداش."""
    if not await _check_admin(callback):
        return
    await callback.answer()
    from database.crud import get_all_plans, get_setting
    from services.referral import REFERRAL_REWARD_PLAN_ID_KEY
    async with AsyncSessionLocal() as session:
        plans = await get_all_plans(session)
        plan_id_raw = await get_setting(session, REFERRAL_REWARD_PLAN_ID_KEY, "0")

    try:
        current_plan_id = int(plan_id_raw) if plan_id_raw.isdigit() else 0
    except Exception:
        current_plan_id = 0

    if not plans:
        await callback.answer("⚠️ هیچ پلنی تعریف نشده!", show_alert=True)
        return

    await _safe_edit_admin(
        callback,
        "📦 <b>پلن پاداش Referral را انتخاب کنید:</b>\n\n"
        "این پلن به صورت رایگان به دعوت‌کننده داده می‌شود.",
        parse_mode="HTML",
        reply_markup=get_referral_plan_select_keyboard(plans, current_plan_id),
    )


@router.callback_query(F.data.startswith("adm_ref_set_plan:"))
async def cb_adm_ref_set_plan(callback: CallbackQuery) -> None:
    """ذخیره پلن انتخاب‌شده برای پاداش referral."""
    if not await _check_admin(callback):
        return
    plan_id = int(callback.data.split(":")[1])
    from database.crud import set_setting, get_plan
    from services.referral import REFERRAL_REWARD_PLAN_ID_KEY
    async with AsyncSessionLocal() as session:
        plan = await get_plan(session, plan_id)
        if not plan:
            await callback.answer("❌ پلن پیدا نشد", show_alert=True)
            return
        await set_setting(session, REFERRAL_REWARD_PLAN_ID_KEY, str(plan_id))
    await callback.answer(f"✅ پلن «{plan.name}» انتخاب شد")
    await _show_referral_settings(callback)

"""
keyboards/admin.py — Inline Keyboard‌های پنل ادمین (رنگ‌بندی هوشمند)

رنگ‌بندی:
  primary   → آبی   — اقدام اصلی / ویرایش / مشاهده
  success   → سبز   — تأیید / فعال‌سازی / تأیید مثبت
  danger    → قرمز  — حذف / رد / غیرفعال‌سازی / ریست
  secondary → خاکستری — بازگشت / انصراف / ناوبری

چیدمان گروه‌بندی‌شده:
  ردیف ۱: پلن‌ها | اینباندها
  ردیف ۲: تخفیف | کاربران
  ...
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import DiscountCode, Plan

try:
    from aiogram.enums import ButtonStyle as _BS
    _HAS_STYLE = True
except ImportError:
    _HAS_STYLE = False


def _t():
    """تم فعلی را sync می‌خواند (از cache)."""
    from services.theme import get_theme_sync_default
    return get_theme_sync_default()


def _ibtn_styled(builder: InlineKeyboardBuilder, text: str,
                 callback_data: str, style_str: str | None = None) -> None:
    """دکمه با style به builder اضافه می‌کند."""
    if _HAS_STYLE and style_str:
        try:
            from aiogram.types import InlineKeyboardButton
            builder.button(
                text=text,
                callback_data=callback_data,
            )
            return
        except Exception:
            pass
    builder.button(text=text, callback_data=callback_data)


# تیتر جداکننده — دکمه غیرفعال (callback=noop)
_SEP = "noop"


def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    # ── گروه ۱: مدیریت محتوا ─────────────────────────
    b.button(text="📋  پلن‌ها",           callback_data="adm_plans")
    b.button(text="🔌  اینباند تست",     callback_data="adm_inbounds")
    b.button(text="🏷  تخفیف‌ها",    callback_data="adm_discounts")
    b.button(text="👥  کاربران",      callback_data="adm_users")

    # ── گروه ۲: آمار ─────────────────────────────────
    b.button(text="📊  آمار",         callback_data="adm_stats")
    b.button(text="📈  آمار کامل",    callback_data="adm_stats_advanced")

    # ── گروه ۳: سرور ─────────────────────────────────
    b.button(text="🖥  وضعیت سرور",  callback_data="admin_server_status")
    b.button(text="📜  لاگ Xray",    callback_data="admin_xray_logs")
    b.button(text="🔄  ریستارت",     callback_data="admin_restart_xray")
    b.button(text="🎫  تیکت‌ها",     callback_data="admin_tickets")

    # ── گروه ۴: ابزار ────────────────────────────────
    b.button(text="💾  بک‌آپ",           callback_data="adm_backup")
    b.button(text="🖼  بنر ربات",        callback_data="adm_banner")
    b.button(text="🎉  بنر خوش‌آمد",   callback_data="adm_welcome_banner")
    b.button(text="📢  کانال اجباری",   callback_data="adm_join_channel")
    b.button(text="💳  تنظیم کارت",     callback_data="adm_card_settings")
    b.button(text="💰  روش‌های پرداخت", callback_data="adm_payment_methods")

    # ── گروه ۵: مدیریت مالی ──────────────────────────
    b.button(text="💱  نرخ تومان/دلار",    callback_data="adm_card_rate")
    b.button(text="🧾  مدیریت تراکنش‌ها",  callback_data="adm_pending_payments")
    b.button(text="➕  ایجاد اشتراک دستی", callback_data="adm_manual_sub")

    # ── گروه ۶: امنیت ────────────────────────────────
    b.button(text="🔐  تنظیمات امنیتی", callback_data="adm_security")
    b.button(text="🎁  اشتراک تست",     callback_data="adm_test_sub_settings")

    # ── گروه ۷: ظاهر ربات ────────────────────────────
    b.button(text="🎨  تم ربات",         callback_data="adm_theme")

    # ── گروه ۸: مدیریت ادمین‌ها ──────────────────────
    b.button(text="👮  مدیریت ادمین‌ها", callback_data="adm_managers")

    # ── تمام‌عرض ─────────────────────────────────────
    b.button(text="📢  پیام دسته‌جمعی به همه کاربران", callback_data="adm_broadcast")
    b.button(text=f"{_t().star2}  بازگشت به منوی اصلی",           callback_data="back_main")
    b.button(text="👨‍💻  Built by MNSH-Nexo",            url="https://github.com/MNSH-Nexo")

    b.adjust(2, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 1, 2, 1, 1, 1, 1, 1, 1)
    return b.as_markup()


def get_plans_manage_keyboard(plans: list) -> InlineKeyboardMarkup:
    """لیست پلن‌ها با وضعیت — حجمی و نامحدود جداگانه."""
    b = InlineKeyboardBuilder()

    limited   = [p for p in plans if p.traffic_gb > 0]
    unlimited = [p for p in plans if p.traffic_gb == 0]

    def _row(p) -> str:
        st = "✅" if p.is_active else "⛔"
        pr = f"{p.price_usdt:.0f}" if p.price_usdt == int(p.price_usdt) else f"{p.price_usdt}"
        if p.traffic_gb:
            vol = f"{p.traffic_gb}G"
        else:
            vol = f"∞×{p.limit_ip}" if p.limit_ip else "∞"
        return f"{st}  {p.name}  ·  {vol}  ·  {pr}$"

    if limited:
        b.button(text="── پلن‌های حجمی ──", callback_data=_SEP)
        for p in limited:
            b.button(text=_row(p), callback_data=f"adm_plan_view:{p.id}")
    if unlimited:
        b.button(text="── پلن‌های نامحدود ──", callback_data=_SEP)
        for p in unlimited:
            b.button(text=_row(p), callback_data=f"adm_plan_view:{p.id}")

    b.button(text="➕  افزودن پلن جدید", callback_data="adm_plan_add")
    b.button(text=f"{_t().star2}  بازگشت",          callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_plan_edit_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    """کیبورد ویرایش پلن — گروه‌بندی‌شده."""
    b = InlineKeyboardBuilder()
    b.button(text="✏️ نام",         callback_data=f"adm_qedit:name:{plan_id}")
    b.button(text="💲 قیمت",        callback_data=f"adm_qedit:price:{plan_id}")
    b.button(text="📦 حجم",         callback_data=f"adm_qedit:traffic:{plan_id}")
    b.button(text="⏱ مدت",         callback_data=f"adm_qedit:days:{plan_id}")
    b.button(text="👤 دستگاه",      callback_data=f"adm_qedit:ip:{plan_id}")
    b.button(text="🔌 اینباندها",   callback_data=f"adm_plan_inbounds:{plan_id}")
    b.button(text="🔁 فعال/غیرفعال",      callback_data=f"adm_plan_toggle:{plan_id}")
    b.button(text="📋 کپی این پلن",       callback_data=f"adm_plan_copy:{plan_id}")
    b.button(text="🗑  حذف پلن",          callback_data=f"adm_plan_del:{plan_id}")
    b.button(text=f"{_t().star2}  بازگشت به پلن‌ها",  callback_data="adm_plans")
    b.adjust(3, 3, 2, 1, 1)
    return b.as_markup()


def get_plan_quick_actions_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    """دکمه‌های سریع بعد از ویرایش موفق."""
    b = InlineKeyboardBuilder()
    b.button(text="✏️ ویرایش مجدد",   callback_data=f"adm_plan_view:{plan_id}")
    b.button(text="📋 لیست پلن‌ها",   callback_data="adm_plans")
    b.adjust(2)
    return b.as_markup()


def get_discounts_keyboard(codes: list) -> InlineKeyboardMarkup:
    """لیست کدها — کلیک → جزئیات."""
    b = InlineKeyboardBuilder()
    for dc in codes:
        st   = "✅" if dc.is_active else "⛔"
        uses = f"{dc.used_count}/{dc.max_uses}" if dc.max_uses else f"{dc.used_count}/∞"
        b.button(
            text=f"{st}  {dc.code}  ·  {dc.percent}٪  ·  {uses}",
            callback_data=f"adm_disc_view:{dc.id}",
        )
    b.button(text="➕ کد تخفیف جدید", callback_data="adm_disc_add")
    b.button(text=f"{_t().star2}  بازگشت",         callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_discount_detail_keyboard(dc_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """جزئیات یک کد — تغییر وضعیت + حذف با تأیید."""
    b = InlineKeyboardBuilder()
    # toggle — سبز اگر غیرفعال‌سازی (موفقیت رنگ فعال‌سازی)، قرمز اگر غیرفعال‌سازی
    toggle_text = "⛔ غیرفعال کردن" if is_active else "✅ فعال کردن"
    b.button(text=toggle_text,          callback_data=f"adm_disc_toggle:{dc_id}")
    # حذف — قرمز
    b.button(text="🗑 حذف کد",          callback_data=f"adm_disc_del_confirm:{dc_id}")
    b.button(text=f"{_t().star2}  بازگشت به کدها",  callback_data="adm_discounts")
    b.adjust(2, 1)
    return b.as_markup()


def get_discount_delete_confirm_keyboard(dc_id: int) -> InlineKeyboardMarkup:
    """تأیید نهایی حذف."""
    b = InlineKeyboardBuilder()
    # تأیید حذف — قرمز (خطرناک)
    b.button(text="✅ بله، حذف کن",     callback_data=f"adm_disc_del:{dc_id}")
    # انصراف — خاکستری
    b.button(text=f"{_t().star2}  انصراف",           callback_data=f"adm_disc_view:{dc_id}")
    b.adjust(2)
    return b.as_markup()


def get_inbounds_keyboard(
    inbounds: list,
    enabled_ids: list | None = None,
) -> InlineKeyboardMarkup:
    """keyboard اینباندها با نشان‌دهی وضعیت."""
    builder = InlineKeyboardBuilder()
    enabled_ids = enabled_ids or []

    for ib in inbounds:
        if not ib.enable:
            icon = "❌"
        elif ib.id in enabled_ids:
            icon = "✅"
        else:
            icon = "🟢"
        builder.button(
            text=f"{icon} [{ib.id}] {ib.remark} ({ib.protocol.upper()}:{ib.port})",
            callback_data=f"adm_inbound_toggle:{ib.id}",
        )

    builder.button(text="ℹ️ راهنما", callback_data="adm_inbound_help")
    builder.button(text=f"{_t().star2}  بازگشت", callback_data="adm_back")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_users_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 جستجو با آیدی تلگرام", callback_data="adm_user_search")
    builder.button(text=f"{_t().star2}  بازگشت", callback_data="adm_back")
    builder.adjust(1)
    return builder.as_markup()


def get_backup_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗄 بک‌آپ دیتابیس ربات",      callback_data="adm_backup_bot")
    b.button(text="🖥 بک‌آپ پنل 3X-UI",         callback_data="adm_backup_panel")
    b.button(text="📦 هر دو با هم",             callback_data="adm_backup_both")
    b.button(text="♻️ بازگردانی دیتابیس ربات",  callback_data="adm_restore")
    # ریست کامل — قرمز (خطرناک)
    b.button(text="🗑 ریست کامل دیتابیس",       callback_data="adm_db_reset_step1")
    b.button(text=f"{_t().star2}  بازگشت",                  callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_db_reset_confirm1_keyboard() -> InlineKeyboardMarkup:
    """مرحله اول تأیید ریست — هشدار اولیه."""
    b = InlineKeyboardBuilder()
    # هشدار — قرمز
    b.button(text="⚠️ بله، ادامه بده",  callback_data="adm_db_reset_step2")
    # لغو — خاکستری
    b.button(text=f"{_t().star2}  لغو",              callback_data="adm_backup")
    b.adjust(2)
    return b.as_markup()


def get_db_reset_confirm2_keyboard() -> InlineKeyboardMarkup:
    """مرحله دوم تأیید ریست — تأیید نهایی غیرقابل بازگشت."""
    b = InlineKeyboardBuilder()
    # ریست — قرمز (خطرناک‌ترین اقدام)
    b.button(text="🗑 بله، دیتابیس را ریست کن",  callback_data="adm_db_reset_confirm")
    # لغو — خاکستری
    b.button(text="❌ لغو — نگه دار",             callback_data="adm_backup")
    b.adjust(1)
    return b.as_markup()


def get_user_detail_keyboard(telegram_id: int, is_banned: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # ردیف ۱: اشتراک‌ها + پیام خصوصی
    b.button(text="📦 اشتراک‌ها",          callback_data=f"adm_user_subs:{telegram_id}")
    b.button(text="✉️ ارسال پیام",         callback_data=f"adm_user_msg:{telegram_id}")
    # ردیف ۲: بن/آنبن + لغو ادمین
    ban_text = "🔓 رفع بن" if is_banned else "🚫 بن کاربر"
    b.button(text=ban_text,                callback_data=f"adm_user_ban_toggle:{telegram_id}")
    b.button(text="❌ لغو ادمین",          callback_data=f"adm_user_ban:{telegram_id}")
    # ردیف ۳: بازگشت
    b.button(text=f"{_t().star2}  بازگشت", callback_data="adm_users")
    b.adjust(2, 2, 1)
    return b.as_markup()


def get_user_ban_confirm_keyboard(telegram_id: int, banning: bool) -> InlineKeyboardMarkup:
    """تأیید بن/آنبن کاربر."""
    b = InlineKeyboardBuilder()
    if banning:
        b.button(text="🚫 بله، بن کن",    callback_data=f"adm_user_ban_do:{telegram_id}:1")
    else:
        b.button(text="🔓 بله، رفع بن",   callback_data=f"adm_user_ban_do:{telegram_id}:0")
    b.button(text="❌ انصراف",            callback_data=f"adm_user_info:{telegram_id}")
    b.adjust(2)
    return b.as_markup()


def get_user_subs_keyboard(telegram_id: int, subs: list) -> InlineKeyboardMarkup:
    """لیست اشتراک‌های یک کاربر."""
    b = InlineKeyboardBuilder()
    for s in subs:
        status_icon = {"active": "✅", "expired": "⏰", "depleted": "📭"}.get(s.status, "⚪")
        limit = f"{s.traffic_limit_gb}G" if s.traffic_limit_gb else "∞"
        b.button(
            text=f"{status_icon} {s.email}  [{limit}]",
            callback_data=f"adm_sub_view:{s.id}",
        )
    b.button(text=f"{_t().star2}  بازگشت به کاربر", callback_data=f"adm_user_info:{telegram_id}")
    b.adjust(1)
    return b.as_markup()


def get_sub_manage_keyboard(sub_id: int, telegram_id: int) -> InlineKeyboardMarkup:
    """کیبورد مدیریت یک اشتراک."""
    b = InlineKeyboardBuilder()
    # ویرایش — آبی
    b.button(text="📅 تمدید (روز+)",     callback_data=f"adm_sub_edit:days:{sub_id}")
    b.button(text="📦 تغییر حجم (GB)",   callback_data=f"adm_sub_edit:traffic:{sub_id}")
    b.button(text="✏️ تغییر ایمیل",      callback_data=f"adm_sub_edit:email:{sub_id}")
    b.button(text="🔄 ریست ترافیک",      callback_data=f"adm_sub_reset:{sub_id}")
    # وضعیت — toggle
    b.button(text="⏸ غیرفعال/فعال",     callback_data=f"adm_sub_toggle:{sub_id}")
    # حذف — قرمز
    b.button(text="🗑 حذف اشتراک",       callback_data=f"adm_sub_del_confirm:{sub_id}")
    # بازگشت — خاکستری
    b.button(text=f"{_t().star2}  بازگشت",          callback_data=f"adm_user_subs:{telegram_id}")
    b.adjust(2, 2, 1, 1, 1)
    return b.as_markup()


def get_sub_del_confirm_keyboard(sub_id: int, telegram_id: int) -> InlineKeyboardMarkup:
    """تأیید حذف اشتراک."""
    b = InlineKeyboardBuilder()
    # تأیید حذف — قرمز
    b.button(text="✅ بله، حذف کن",     callback_data=f"adm_sub_del:{sub_id}:{telegram_id}")
    # انصراف — خاکستری
    b.button(text=f"{_t().star2}  انصراف",           callback_data=f"adm_sub_view:{sub_id}")
    b.adjust(2)
    return b.as_markup()


def get_transactions_keyboard(has_sub_id: bool = False, sub_id: int = 0,
                               tg_id: int = 0) -> InlineKeyboardMarkup:
    """کیبورد صفحه جزئیات یک تراکنش."""
    b = InlineKeyboardBuilder()
    if has_sub_id and sub_id:
        b.button(text="📦 مدیریت اشتراک", callback_data=f"adm_sub_view:{sub_id}")
    b.button(text="👤 پروفایل کاربر",    callback_data=f"adm_user_info:{tg_id}" if tg_id else "noop")
    b.button(text=f"{_t().star2}  بازگشت",           callback_data="adm_pending_payments")
    b.adjust(1)
    return b.as_markup()


def get_payments_filter_keyboard() -> InlineKeyboardMarkup:
    """کیبورد فیلتر لیست تراکنش‌ها."""
    b = InlineKeyboardBuilder()
    b.button(text="⏳ در صف (کارت)",    callback_data="adm_tx_filter:pending_card")
    b.button(text="⏳ در صف (کریپتو)", callback_data="adm_tx_filter:pending_crypto")
    b.button(text="✅ موفق (کارت)",     callback_data="adm_tx_filter:confirmed_card")
    b.button(text="✅ موفق (کریپتو)",  callback_data="adm_tx_filter:confirmed_crypto")
    b.button(text="❌ ناموفق (کارت)",   callback_data="adm_tx_filter:failed_card")
    b.button(text="❌ ناموفق (کریپتو)",callback_data="adm_tx_filter:failed_crypto")
    b.button(text="🔍 جستجو order_id",  callback_data="adm_tx_search")
    b.button(text=f"{_t().star2}  بازگشت",          callback_data="adm_back")
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


def get_payment_methods_keyboard(
    crypto_on: bool,
    card_on: bool,
    crypto_invoice: bool = False,
    crypto_gateway: str = "nowpayments",
    price_display: str = "both",
) -> InlineKeyboardMarkup:
    """وضعیت روش‌های پرداخت با دکمه toggle."""
    b = InlineKeyboardBuilder()
    crypto_icon  = "✅" if crypto_on else "⛔"
    card_icon    = "✅" if card_on   else "⛔"
    invoice_icon = "🌐" if crypto_invoice else "🪙"

    _PRICE_LABELS = {
        "both":  "💱 نمایش قیمت: هر دو ← کلیک برای تغییر",
        "usd":   "💵 نمایش قیمت: فقط دلار ← کلیک برای تغییر",
        "toman": "🪙 نمایش قیمت: فقط تومان ← کلیک برای تغییر",
    }
    b.button(
        text=_PRICE_LABELS.get(price_display, _PRICE_LABELS["both"]),
        callback_data="adm_pm_toggle:price_display",
    )
    b.button(
        text=f"{crypto_icon}  کریپتو — {'فعال' if crypto_on else 'غیرفعال'}",
        callback_data="adm_pm_toggle:crypto",
    )

    if crypto_on:
        gw_label = "MaxelPay 💜" if crypto_gateway == "maxelpay" else "NOWPayments 🔵"
        b.button(
            text=f"🏦  درگاه: {gw_label}",
            callback_data="adm_pm_toggle:crypto_gateway",
        )
        if crypto_gateway == "nowpayments":
            b.button(
                text=f"{invoice_icon}  حالت: {'صفحه انتخاب ارز 🌐' if crypto_invoice else 'USDT مستقیم 🪙'}",
                callback_data="adm_pm_toggle:crypto_invoice",
            )

    b.button(
        text=f"{card_icon}  کارت به کارت — {'فعال' if card_on else 'غیرفعال'}",
        callback_data="adm_pm_toggle:card",
    )
    b.button(text=f"{_t().star2}  بازگشت", callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_card_settings_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ تغییر شماره کارت", callback_data="adm_card_edit")
    b.button(text=f"{_t().star2}  بازگشت",           callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_security_keyboard(current_command: str = "admin_secret") -> InlineKeyboardMarkup:
    """کیبورد تنظیمات امنیتی."""
    b = InlineKeyboardBuilder()
    b.button(
        text=f"🔑 تغییر دستور ورود (فعلی: /{current_command})",
        callback_data="adm_sec_change_cmd",
    )
    b.button(text=f"{_t().star2}  بازگشت", callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_pending_payment_keyboard(order_id: str) -> InlineKeyboardMarkup:
    """دکمه‌های تأیید/رد تراکنش کارت‌به‌کارت در صف."""
    b = InlineKeyboardBuilder()
    # تأیید — سبز
    b.button(text="✅ تأیید و فعال‌سازی", callback_data=f"card_approve:{order_id}")
    # رد — قرمز
    b.button(text="❌ رد پرداخت",          callback_data=f"card_reject:{order_id}")
    # بازگشت — خاکستری
    b.button(text=f"{_t().star2}  بازگشت به صف",       callback_data="adm_pending_payments")
    b.adjust(2, 1)
    return b.as_markup()


def get_crypto_pending_keyboard(order_id: str, tg_id: int = 0) -> InlineKeyboardMarkup:
    """کیبورد تراکنش کریپتوی در صف."""
    b = InlineKeyboardBuilder()
    b.button(text="⏰ علامت‌گذاری منقضی",  callback_data=f"crypto_expire:{order_id}")
    if tg_id:
        b.button(text="👤 پروفایل کاربر", callback_data=f"adm_user_info:{tg_id}")
    b.button(text=f"{_t().star2}  بازگشت",             callback_data="adm_pending_payments")
    b.adjust(1)
    return b.as_markup()


def get_manual_sub_plans_keyboard(plans: list) -> InlineKeyboardMarkup:
    """لیست پلن‌ها + پلن دلخواه برای ایجاد اشتراک دستی."""
    b = InlineKeyboardBuilder()
    for plan in plans:
        b.button(
            text=f"{'♾' if plan.traffic_gb == 0 else f'{plan.traffic_gb}G'} {plan.name}",
            callback_data=f"adm_msub_plan:{plan.id}",
        )
    b.button(text="⚙️ پلن دلخواه", callback_data="adm_msub_custom")
    b.button(text=f"{_t().star2}  بازگشت",     callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_manual_sub_inbounds_keyboard(inbounds: list, selected: list[int]) -> InlineKeyboardMarkup:
    """انتخاب اینباند برای پلن دلخواه — چند انتخابی."""
    b = InlineKeyboardBuilder()
    for inb in inbounds:
        checked = "✅" if inb["id"] in selected else "⬜"
        b.button(
            text=f"{checked} {inb.get('remark', inb['id'])}",
            callback_data=f"adm_msub_inb:{inb['id']}",
        )
    b.button(text="◀️ ادامه", callback_data="adm_msub_inb_done")
    b.button(text=f"{_t().star2}  لغو",   callback_data="adm_msub_cancel")
    b.adjust(1)
    return b.as_markup()


def get_plan_inbounds_keyboard(
    plan_id: int,
    inbounds: list,
    plan_inbound_ids: list[int],
) -> InlineKeyboardMarkup:
    """keyboard انتخاب اینباندهای اختصاصی برای یک پلن."""
    b = InlineKeyboardBuilder()
    for ib in inbounds:
        if not ib.enable:
            icon = "❌"
        elif ib.id in plan_inbound_ids:
            icon = "✅"
        else:
            icon = "🟢"
        b.button(
            text=f"{icon} [{ib.id}] {ib.remark} ({ib.protocol.upper()}:{ib.port})",
            callback_data=f"adm_plan_inb_toggle:{plan_id}:{ib.id}",
        )
    b.button(text=f"{_t().star2}  بازگشت به ویرایش پلن", callback_data=f"adm_plan_view:{plan_id}")
    b.adjust(1)
    return b.as_markup()


def get_banner_keyboard(has_banner: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📤 آپلود عکس جدید", callback_data="adm_banner_set")
    if has_banner:
        # حذف بنر — قرمز
        b.button(text="🗑 حذف بنر فعلی", callback_data="adm_banner_clear")
    b.button(text=f"{_t().star2}  بازگشت",          callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_welcome_banner_keyboard(has_banner: bool = False) -> InlineKeyboardMarkup:
    """keyboard مدیریت بنر خوش‌آمدگویی."""
    b = InlineKeyboardBuilder()
    b.button(text=f"{_t().star}  آپلود عکس بنر",      callback_data="adm_welcome_set_photo")
    b.button(text=f"{_t().star}  ویرایش کپشن",        callback_data="adm_welcome_set_caption")
    if has_banner:
        b.button(text=f"{_t().star2}  حذف بنر",         callback_data="adm_welcome_clear")
    b.button(text=f"{_t().star2}  بازگشت",              callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_join_channel_keyboard(has_channel: bool = False) -> InlineKeyboardMarkup:
    """keyboard مدیریت کانال اجباری."""
    b = InlineKeyboardBuilder()
    b.button(text=f"{_t().star}  تنظیم کانال جدید",    callback_data="adm_channel_set")
    if has_channel:
        b.button(text=f"{_t().star2}  حذف کانال",       callback_data="adm_channel_clear")
    b.button(text=f"{_t().star2}  بازگشت",              callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_theme_keyboard(current_theme: str = "nexora") -> InlineKeyboardMarkup:
    """کیبورد انتخاب تم ربات."""
    from services.theme import THEMES
    b = InlineKeyboardBuilder()
    for name, t in THEMES.items():
        # نمایش فقط تم‌های اصلی (نه alias)
        if name in ("moonstone", "rose_ember"):
            continue
        tick = "✅  " if name == current_theme else "⬜  "
        b.button(text=f"{tick}{t.label}", callback_data=f"adm_theme_set:{name}")
    b.button(text=f"{_t().star2}  بازگشت", callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


# ──────────────────────────────────────────────
# مدیریت ادمین‌ها
# ──────────────────────────────────────────────

def get_admin_management_keyboard(is_locked: bool) -> InlineKeyboardMarkup:
    """منوی اصلی مدیریت ادمین‌ها."""
    b = InlineKeyboardBuilder()
    lock_text = "🔓 باز کردن قفل ورود" if is_locked else "🔒 قفل کامل ورود"
    b.button(text="👮 لیست ادمین‌های فعال",    callback_data="adm_mgr_list")
    b.button(text=lock_text,                    callback_data="adm_mgr_lock_toggle")
    b.button(text=f"{_t().star2}  بازگشت",      callback_data="adm_back")
    b.adjust(1)
    return b.as_markup()


def get_admin_list_keyboard(
    admins: list,
    main_admin_ids: list[int],
    viewer_id: int,
) -> InlineKeyboardMarkup:
    """
    لیست ادمین‌ها — هر ادمین یک ردیف.
    ادمین اصلی (.env) با 👑 مشخص و قابل حذف نیست.
    ادمین‌های فرعی با 🛡 مشخص — ادمین اصلی می‌تونه حذفشون کنه.
    مقایسه int-safe — هر دو طرف به int تبدیل می‌شوند.
    """
    b = InlineKeyboardBuilder()
    _main_set = {int(x) for x in main_admin_ids}
    is_main_admin = int(viewer_id) in _main_set

    for adm in admins:
        is_main = int(adm.telegram_id) in _main_set
        crown = "👑" if is_main else "🛡"
        uname = f"@{adm.username}" if adm.username else f"#{adm.telegram_id}"
        label = f"{crown} {uname}  [{adm.telegram_id}]"

        if is_main_admin and not is_main:
            # ادمین اصلی می‌تونه ادمین‌های فرعی رو حذف کنه
            b.button(text=label, callback_data=f"adm_mgr_view:{adm.telegram_id}")
        else:
            # ادمین اصلی یا بیننده‌ای که اصلی نیست → فقط نمایش
            b.button(text=label, callback_data="noop")

    b.button(text=f"{_t().star2}  بازگشت", callback_data="adm_managers")
    b.adjust(1)
    return b.as_markup()


def get_admin_detail_keyboard(target_tid: int, is_main_admin: bool) -> InlineKeyboardMarkup:
    """جزئیات یک ادمین فرعی — دکمه لغو دسترسی."""
    b = InlineKeyboardBuilder()
    if is_main_admin:
        b.button(
            text="🚫 لغو دسترسی ادمین",
            callback_data=f"adm_mgr_revoke_confirm:{target_tid}",
        )
    b.button(text=f"{_t().star2}  بازگشت به لیست", callback_data="adm_mgr_list")
    b.adjust(1)
    return b.as_markup()


def get_admin_revoke_confirm_keyboard(target_tid: int) -> InlineKeyboardMarkup:
    """تأیید نهایی لغو دسترسی."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ بله، لغو کن",           callback_data=f"adm_mgr_revoke:{target_tid}")
    b.button(text="❌ انصراف",                callback_data=f"adm_mgr_view:{target_tid}")
    b.adjust(2)
    return b.as_markup()


def get_admin_lock_confirm_keyboard(locking: bool) -> InlineKeyboardMarkup:
    """تأیید فعال/غیرفعال کردن قفل ورود."""
    b = InlineKeyboardBuilder()
    if locking:
        b.button(text="🔒 بله، قفل کن",  callback_data="adm_mgr_lock_do:1")
    else:
        b.button(text="🔓 بله، باز کن",   callback_data="adm_mgr_lock_do:0")
    b.button(text="❌ انصراف",            callback_data="adm_managers")
    b.adjust(2)
    return b.as_markup()

"""
keyboards/main_menu.py — کیبورد اصلی ربات (ReplyKeyboard فارسی)
رنگ‌بندی هوشمند دکمه‌ها:
  خرید / اشتراک‌ها / پروفایل  → primary (آبی)
  اشتراک تست                  → success (سبز) — نوع ویژه
  پشتیبانی / دعوت دوستان      → secondary (خاکستری)
  UUID                         → secondary
  پنل مدیریت                  → danger (قرمز) — دسترسی ادمین
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# ButtonStyle در aiogram 3.25+ اضافه شد (Bot API 9.4)
try:
    from aiogram.enums import ButtonStyle as _BS
    _HAS_STYLE = True
except ImportError:
    _HAS_STYLE = False


def _btn(text: str, style_str: str | None = None) -> KeyboardButton:
    """ساخت KeyboardButton با style اگر aiogram از آن پشتیبانی می‌کند."""
    if _HAS_STYLE and style_str:
        try:
            return KeyboardButton(text=text, style=_BS(style_str))
        except Exception:
            pass
    return KeyboardButton(text=text)


# ─── متن‌های ثابت دکمه‌ها (بدون نماد تم) — برای تشخیص در handlers ───────────
BTN_BUY         = "خرید کانفیگ"
BTN_TEST        = "اشتراک تست"
BTN_MY_SUBS     = "اشتراک‌های من"
BTN_PROFILE     = "پروفایل"
BTN_UUID        = "وارد کردن UUID"
BTN_REFERRAL    = "دعوت دوستان"
BTN_SUPPORT     = "پشتیبانی"
BTN_ADMIN       = "پنل مدیریت"


def _kb(t) -> list[list[KeyboardButton]]:
    """ردیف‌های کیبورد با رنگ‌بندی:
      خرید / اشتراک تست  → success (سبز)
      اشتراک‌ها / پروفایل → primary (آبی)
      پشتیبانی / دعوت / UUID → بدون رنگ (پیش‌فرض تلگرام)
    """
    s, s2, c = t.star, t.star2, t.corner
    return [
        # ردیف ۱: خرید (سبز) + تست (سبز)
        [_btn(f"{s}  {BTN_BUY}",      t.btn_success),
         _btn(f"{s2}  {BTN_TEST}",    t.btn_success)],

        # ردیف ۲: اشتراک‌ها (آبی) + پروفایل (آبی)
        [_btn(f"{s}  {BTN_MY_SUBS}",  t.btn_primary),
         _btn(f"{s2}  {BTN_PROFILE}", t.btn_primary)],

        # ردیف ۳: پشتیبانی (بی‌رنگ) + دعوت دوستان (بی‌رنگ)
        [_btn(f"{s}  {BTN_SUPPORT}"),
         _btn(f"{s2}  {BTN_REFERRAL}")],

        # ردیف ۴: UUID (بی‌رنگ)
        [_btn(f"{s}  {BTN_UUID}")],
    ]


def get_main_menu(is_admin: bool = False, theme=None) -> ReplyKeyboardMarkup:
    """
    منوی اصلی ربات با رنگ‌بندی هوشمند.
    theme: یک Theme object (از services.theme) — اگر None باشد، پیش‌فرض استفاده می‌شود.
    """
    from services.theme import get_theme_sync_default
    t = theme or get_theme_sync_default()

    rows = _kb(t)
    if is_admin:
        # دکمه ادمین — قرمز (نشان‌دهنده دسترسی خاص)
        rows.append([_btn(f"{t.corner}  {BTN_ADMIN}", t.btn_danger)])


    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="یک گزینه انتخاب کنید...",
    )


async def get_main_menu_async(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """نسخه async — تم را از DB می‌خواند."""
    from services.theme import get_current_theme
    t = await get_current_theme()
    return get_main_menu(is_admin=is_admin, theme=t)

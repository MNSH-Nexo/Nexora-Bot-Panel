"""
services/theme.py — سیستم تم ربات (رنگ‌بندی هوشمند دکمه‌ها)

رنگ‌های Bot API 9.4:
  primary   → آبی (خرید / تأیید / اقدام اصلی)
  success   → سبز (فعال‌سازی / پرداخت موفق / تأیید مثبت)
  danger    → قرمز (حذف / رد / هشدار / لغو مخرب)
  secondary → خاکستری (بازگشت / انصراف / دکمه‌های ثانویه)

Bot API 9.4+: KeyboardButton(style='primary'|'success'|'danger')
aiogram 3.25+: از ButtonStyle enum پشتیبانی می‌کند

کلید در AdminSetting: bot_theme
"""
from __future__ import annotations
from dataclasses import dataclass

THEME_KEY = "bot_theme"
DEFAULT_THEME = "nexora"


@dataclass(frozen=True)
class Theme:
    name: str
    label: str
    emoji: str

    # نمادهای متنی
    star: str
    star2: str
    sep: str
    corner: str
    header_icon: str
    bullet: str

    # رنگ دکمه Bot API 9.4 — 'primary' | 'success' | 'danger'
    # هر نوع دکمه رنگ مستقل دارد
    btn_primary: str    # دکمه اصلی — خرید / پرداخت / تأیید اصلی  → آبی
    btn_success: str    # موفقیت — فعال‌سازی / تأیید مثبت / باز کردن → سبز
    btn_danger: str     # خطرناک — حذف / رد / لغو مخرب → قرمز
    btn_secondary: str  # ثانویه — بازگشت / انصراف / ناوبری → خاکستری (secondary=default)

    # backward-compat: btn_style هنوز باقی می‌ماند (همان primary)
    @property
    def btn_style(self) -> str:
        return self.btn_primary

    @property
    def btn_style2(self) -> str:
        return self.btn_secondary


THEMES: dict[str, Theme] = {
    "nexora": Theme(
        name="nexora",
        label="✦ Nexora — رنگارنگ هوشمند",
        emoji="✦",
        star="✦",
        star2="✧",
        sep="━━━━━━━━━━━━━━━",
        corner="◈",
        header_icon="✦",
        bullet="·",
        btn_primary="primary",    # آبی — دکمه‌های اصلی
        btn_success="success",    # سبز — تأیید / موفقیت
        btn_danger="danger",      # قرمز — حذف / رد
        btn_secondary="primary",  # تلگرام secondary ندارد؛ primary کمرنگ‌ترین گزینه است
    ),
}

# ── backward compat: تم قدیمی moonstone به nexora map می‌شود ──────────
THEMES["moonstone"] = THEMES["nexora"]
THEMES["rose_ember"] = THEMES["nexora"]

# ── cache ──────────────────────────────────────────────────
_cached_theme: Theme | None = None


def get_theme_sync_default() -> Theme:
    """sync — از cache می‌خواند؛ اگر cache خالی بود پیش‌فرض برمی‌گرداند."""
    return _cached_theme or THEMES[DEFAULT_THEME]


async def get_current_theme() -> Theme:
    """تم فعلی را از DB می‌خواند و cache می‌کند."""
    global _cached_theme
    try:
        from database import AsyncSessionLocal
        from database.crud import get_setting
        async with AsyncSessionLocal() as s:
            name = await get_setting(s, THEME_KEY, DEFAULT_THEME)
        _cached_theme = THEMES.get(name, THEMES[DEFAULT_THEME])
        return _cached_theme
    except Exception:
        return _cached_theme or THEMES[DEFAULT_THEME]


async def set_theme(name: str) -> None:
    """تم فعلی را در DB ذخیره و cache را بروز می‌کند."""
    global _cached_theme
    if name not in THEMES:
        raise ValueError(f"تم نامعتبر: {name}")
    from database import AsyncSessionLocal
    from database.crud import set_setting
    async with AsyncSessionLocal() as s:
        await set_setting(s, THEME_KEY, name)
    _cached_theme = THEMES[name]


def fmt_header(t: Theme, title: str) -> str:
    return f"{t.header_icon} <b>{title}</b>\n{t.sep}"

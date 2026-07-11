"""
keyboards/plans.py — Inline Keyboard برای نمایش پلن‌ها و خرید
رنگ‌بندی هوشمند:
  دکمه‌های پلن / پرداخت    → primary (آبی)
  بازگشت / انصراف          → secondary (خاکستری — از primary استفاده می‌شود چون تلگرام secondary ندارد)
  بررسی پرداخت (موفقیت)   → success (سبز)
"""

from __future__ import annotations

from typing import List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Plan

try:
    from aiogram.enums import ButtonStyle as _BS
    _HAS_STYLE = True
except ImportError:
    _HAS_STYLE = False


def _t():
    """تم فعلی را sync می‌خواند (از cache)."""
    from services.theme import get_theme_sync_default
    return get_theme_sync_default()


def _ibtn(text: str, callback_data: str, style_str: str | None = None) -> InlineKeyboardButton:
    """ساخت InlineKeyboardButton با style اختیاری."""
    if _HAS_STYLE and style_str:
        try:
            return InlineKeyboardButton(text=text, callback_data=callback_data, style=_BS(style_str))
        except Exception:
            pass
    return InlineKeyboardButton(text=text, callback_data=callback_data)


_FARSI_USERS = {
    1: "یک کاربره",
    2: "دو کاربره",
    3: "سه کاربره",
    4: "چهار کاربره",
    5: "پنج کاربره",
}


def _fmt_usdt(price: float) -> str:
    """
    نمایش هوشمند قیمت دلاری — بدون صفر اضافه، بدون برش اشتباه.
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


def _plan_label(plan: Plan, rate: int = 0, display_mode: str = "both") -> str:
    """
    متن دکمه پلن — نام پلن + جزئیات کامل.
    display_mode: "both" = هر دو | "usd" = فقط دلار | "toman" = فقط تومان
    """
    if plan.traffic_gb == 0:
        if plan.limit_ip and plan.limit_ip > 0:
            user_label = _FARSI_USERS.get(plan.limit_ip, f"{plan.limit_ip} کاربره")
            traffic_part = f"نامحدود — {user_label}"
            icon = "♾"
        else:
            traffic_part = "نامحدود"
            icon = "♾"
    else:
        traffic_part = f"{plan.traffic_gb} گیگ"
        icon = "📦"

    price_str = _fmt_usdt(plan.price_usdt)
    if display_mode == "usd":
        price_part = f"{price_str}$"
    elif display_mode == "toman":
        if rate > 0:
            toman = int(plan.price_usdt * rate)
            toman_str = f"{toman:,}".replace(",", "،")
            price_part = f"{toman_str}ت"
        else:
            price_part = f"{price_str}$ (نرخ تنظیم نشده)"
    else:  # "both"
        if rate > 0:
            toman = int(plan.price_usdt * rate)
            toman_str = f"{toman:,}".replace(",", "،")
            price_part = f"{price_str}$ ({toman_str}ت)"
        else:
            price_part = f"{price_str} دلار"
    t = _t()
    return f"{t.star}  {plan.name}  {t.bullet}  {traffic_part}  {t.corner}  {plan.duration_days} روزه  {t.corner}  {price_part}"


def get_plans_keyboard(plans: List[Plan], rate: int = 0, display_mode: str = "both") -> InlineKeyboardMarkup:
    """
    Inline keyboard مرتب از لیست پلن‌ها.
    پلن‌های حجمی و نامحدود با جداکننده از هم تفکیک می‌شوند.
    """
    t = _t()
    builder = InlineKeyboardBuilder()
    if not plans:
        builder.button(text=f"{t.star}  در حال حاضر پلنی موجود نیست", callback_data="no_plans")
        builder.row(InlineKeyboardButton(text=f"{t.star2}  بازگشت به منو", callback_data="back_main"))
        return builder.as_markup()

    limited = [p for p in plans if p.traffic_gb > 0]
    unlimited = [p for p in plans if p.traffic_gb == 0]

    if limited:
        builder.row(InlineKeyboardButton(text="━━  پلن‌های حجمی  ━━", callback_data="no_plans"))
        for plan in limited:
            # دکمه پلن — آبی (اقدام اصلی = خرید)
            builder.row(_ibtn(_plan_label(plan, rate, display_mode), f"plan:{plan.id}", t.btn_primary))

    if unlimited:
        builder.row(InlineKeyboardButton(text="━━  پلن‌های نامحدود  ━━", callback_data="no_plans"))
        for plan in unlimited:
            builder.row(_ibtn(_plan_label(plan, rate, display_mode), f"plan:{plan.id}", t.btn_primary))

    # بازگشت — بی‌رنگ (پیش‌فرض تلگرام)
    builder.row(_ibtn(f"{t.star2}  بازگشت به منو", "back_main"))
    return builder.as_markup()


def get_plan_confirm_keyboard(
    plan_id: int,
    discount_code: str = "",
    crypto_on: bool = True,
    card_on: bool = False,
    crypto_invoice: bool = False,
    crypto_gateway: str = "nowpayments",
    amount: float = 0.0,
    plan_name: str = "",
) -> InlineKeyboardMarkup:
    """انتخاب روش پرداخت — فقط روش‌های فعال نمایش داده می‌شوند."""
    builder = InlineKeyboardBuilder()
    dc_suffix = f":{discount_code}" if discount_code else ""

    t = _t()
    if crypto_on:
        if crypto_gateway == "maxelpay":
            # پرداخت — آبی (اقدام اصلی)
            builder.row(_ibtn(f"{t.star}  پرداخت با ارز دیجیتال (MaxelPay)",
                              f"pay_maxel:{plan_id}:{amount:.2f}:{plan_name}{dc_suffix}", t.btn_primary))
        elif crypto_invoice:
            builder.row(_ibtn(f"{t.star}  پرداخت با ارز دیجیتال (انتخاب ارز)",
                              f"pay_invoice:{plan_id}{dc_suffix}", t.btn_primary))
        else:
            builder.row(_ibtn(f"{t.star}  پرداخت با کریپتو (USDT TRC-20)",
                              f"pay:{plan_id}{dc_suffix}", t.btn_primary))
    if card_on:
        builder.row(_ibtn(f"{t.star}  پرداخت کارت به کارت",
                          f"card_pay:{plan_id}{dc_suffix}", t.btn_primary))
    if not crypto_on and not card_on:
        builder.row(InlineKeyboardButton(text="⛔  هیچ روش پرداختی فعال نیست", callback_data="no_plans"))

    # کد تخفیف — بی‌رنگ
    builder.row(_ibtn(f"{t.star2}  دارم کد تخفیف", f"discount:{plan_id}"))
    # بازگشت — بی‌رنگ
    builder.row(_ibtn(f"{t.star2}  بازگشت به پلن‌ها", "show_plans"))
    return builder.as_markup()


def get_confirm_after_discount_keyboard(
    plan_id: int,
    code: str,
    crypto_on: bool = True,
    card_on: bool = False,
    crypto_invoice: bool = False,
    crypto_gateway: str = "nowpayments",
    amount: float = 0.0,
    plan_name: str = "",
) -> InlineKeyboardMarkup:
    """انتخاب روش پرداخت بعد از اعمال کد تخفیف."""
    t = _t()
    builder = InlineKeyboardBuilder()
    if crypto_on:
        if crypto_gateway == "maxelpay":
            builder.button(
                text=f"{t.star}  پرداخت با ارز دیجیتال (MaxelPay)",
                callback_data=f"pay_maxel:{plan_id}:{amount:.2f}:{plan_name}:{code}",
            )
        elif crypto_invoice:
            builder.button(
                text=f"{t.star}  پرداخت با ارز دیجیتال (انتخاب ارز)",
                callback_data=f"pay_invoice:{plan_id}:{code}",
            )
        else:
            builder.button(text=f"{t.star}  کریپتو (USDT)", callback_data=f"pay:{plan_id}:{code}")
    if card_on:
        builder.button(text=f"{t.star}  کارت به کارت", callback_data=f"card_pay:{plan_id}:{code}")
    # انصراف — بی‌رنگ
    builder.button(text=f"{t.star2}  انصراف", callback_data="show_plans")
    builder.adjust(1)
    return builder.as_markup()


def get_subscription_detail_keyboard(sub_email: str) -> InlineKeyboardMarkup:
    t = _t()
    builder = InlineKeyboardBuilder()
    # دریافت مجدد لینک — آبی (اقدام اصلی)
    builder.row(_ibtn(f"{t.star}  دریافت مجدد لینک", f"resend_link:{sub_email}", t.btn_primary))
    # بازگشت — بی‌رنگ
    builder.row(_ibtn(f"{t.star2}  بازگشت", "my_subs"))
    return builder.as_markup()


def get_payment_status_keyboard(order_id: str) -> InlineKeyboardMarkup:
    t = _t()
    builder = InlineKeyboardBuilder()
    # بررسی پرداخت — سبز (انتظار موفقیت)
    builder.row(_ibtn(f"{t.star}  بررسی پرداخت", f"check_payment:{order_id}", t.btn_success))
    # انصراف — بی‌رنگ
    builder.row(_ibtn(f"{t.star2}  انصراف", "back_main"))
    return builder.as_markup()


# ── سازگاری با handler قدیمی ──────────────────
def get_plan_detail_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return get_plan_confirm_keyboard(plan_id)


def get_confirm_purchase_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return get_plan_confirm_keyboard(plan_id)

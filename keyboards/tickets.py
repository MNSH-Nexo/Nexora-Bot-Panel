"""
keyboards/tickets.py — کیبوردهای سیستم تیکت پشتیبانی
رنگ‌بندی هوشمند:
  پاسخ / باز کردن مجدد → primary (آبی)
  بستن تیکت            → danger (قرمز)
  بازگشت / انصراف      → secondary (خاکستری)
"""

from __future__ import annotations

from typing import List

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.models import Ticket

try:
    from aiogram.enums import ButtonStyle as _BS
    _HAS_STYLE = True
except ImportError:
    _HAS_STYLE = False


def _t():
    from services.theme import get_theme_sync_default
    return get_theme_sync_default()


def _ibtn_s(builder: InlineKeyboardBuilder, text: str,
             callback_data: str, style_str: str | None = None) -> None:
    """دکمه با style اختیاری به builder اضافه می‌کند."""
    builder.button(text=text, callback_data=callback_data)


def get_ticket_list_keyboard(tickets: List[Ticket]) -> InlineKeyboardMarkup:
    """لیست تیکت‌های کاربر — تیکت‌های ادمین با 📩 مشخص‌اند."""
    builder = InlineKeyboardBuilder()
    for t in tickets:
        status_icon = {"open": "🔴", "in_progress": "🟡", "closed": "✅"}.get(t.status, "⚪")
        # تیکت‌های آغازشده توسط ادمین ایموجی خاص دارند
        admin_badge = "📩 " if getattr(t, "initiated_by_admin", False) else ""
        label = f"{status_icon} {admin_badge}#{t.id} — {t.subject[:28]}"
        builder.button(text=label, callback_data=f"ticket_view:{t.id}")
    # تیکت جدید — آبی (اقدام اصلی)
    builder.button(text=f"{_t().star}  تیکت جدید", callback_data="ticket_new")
    # بازگشت — خاکستری
    builder.button(text=f"{_t().star2}  بازگشت", callback_data="back_main")
    builder.adjust(1)
    return builder.as_markup()


def get_ticket_detail_keyboard(ticket_id: int, is_closed: bool = False) -> InlineKeyboardMarkup:
    """کیبورد جزئیات تیکت برای کاربر."""
    builder = InlineKeyboardBuilder()
    if not is_closed:
        # پاسخ — آبی
        builder.button(text=f"{_t().star}  پاسخ", callback_data=f"ticket_reply:{ticket_id}")
        # بستن — قرمز (اقدام نهایی)
        builder.button(text=f"{_t().star2}  بستن تیکت", callback_data=f"ticket_close:{ticket_id}")
    else:
        # باز کردن مجدد — سبز (بازیابی)
        builder.button(text=f"{_t().star}  باز کردن مجدد", callback_data=f"ticket_reopen:{ticket_id}")
    # بازگشت — خاکستری
    builder.button(text=f"{_t().star2}  بازگشت به لیست", callback_data="support_list")
    builder.adjust(2 if not is_closed else 1)
    return builder.as_markup()


def get_admin_ticket_keyboard(ticket_id: int, is_closed: bool = False) -> InlineKeyboardMarkup:
    """کیبورد مدیریت تیکت برای ادمین — شامل دکمه حذف."""
    builder = InlineKeyboardBuilder()
    if not is_closed:
        # پاسخ ادمین — آبی
        builder.button(text=f"{_t().star}  پاسخ", callback_data=f"admin_ticket_reply:{ticket_id}")
        # بستن — قرمز
        builder.button(text=f"{_t().star2}  بستن", callback_data=f"admin_ticket_close:{ticket_id}")
    else:
        # باز کردن مجدد — سبز
        builder.button(text=f"{_t().star}  باز کردن مجدد", callback_data=f"admin_ticket_reopen:{ticket_id}")
    # مشاهده کامل — آبی
    builder.button(text=f"{_t().star}  مشاهده کامل", callback_data=f"admin_ticket_view:{ticket_id}")
    # حذف تیکت — قرمز خطرناک
    builder.button(text="🗑 حذف تیکت", callback_data=f"admin_ticket_del_confirm:{ticket_id}")
    # بازگشت به لیست — خاکستری
    builder.button(text=f"{_t().star2}  لیست تیکت‌ها", callback_data="admin_tickets")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_admin_ticket_del_confirm_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """تأیید نهایی حذف تیکت."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ بله، حذف کن", callback_data=f"admin_ticket_del:{ticket_id}")
    builder.button(text="❌ انصراف",       callback_data=f"admin_ticket_view:{ticket_id}")
    builder.adjust(2)
    return builder.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """کیبورد لغو عملیات FSM."""
    builder = InlineKeyboardBuilder()
    # انصراف — خاکستری
    builder.button(text=f"{_t().star2}  انصراف", callback_data="ticket_cancel")
    return builder.as_markup()

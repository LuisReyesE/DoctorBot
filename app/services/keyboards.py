"""Teclados ReplyKeyboard (Telegram). En mock se guardan; Meta los ignora."""

from __future__ import annotations

KB_MENU: list[list[str]] = [
    ["1 Nueva consulta", "2 Estado"],
    ["3 Suscripción", "4 Ayuda"],
]

KB_MENU_RESUME: list[list[str]] = [
    ["Continuar", "Cancelar consulta"],
    ["1 Nueva consulta", "2 Estado"],
    ["3 Suscripción", "4 Ayuda"],
]

KB_TRIAGE: list[list[str]] = [["Cancelar consulta", "Menú"]]

KB_TRIAGE_PHOTO: list[list[str]] = [["Omitir"], ["Cancelar consulta", "Menú"]]

KB_PAY_PLAN: list[list[str]] = [
    ["1 Mensual", "2 Trimestral"],
    ["0 Cancelar"],
]

KB_PAY_FICHA: list[list[str]] = [["ok"], ["Cancelar"]]

KB_PAY_WAIT: list[list[str]] = [["Cancelar"]]

KB_CONSENT: list[list[str]] = [["Acepto"], ["No acepto", "Cancelar"]]

KB_CAJA: list[list[str]] = [
    ["/casos", "/ayuda"],
]

KB_CHAT: list[list[str]] = [["Menú", "2 Estado"]]

KB_DOCTOR: list[list[str]] = [
    ["/disponible", "/ocupado"],
    ["/casos", "/video"],
    ["/cerrar", "/pdf"],
    ["/fuera", "/ayuda"],
]


def menu_keyboard(*, resume: bool = False) -> list[list[str]]:
    return KB_MENU_RESUME if resume else KB_MENU

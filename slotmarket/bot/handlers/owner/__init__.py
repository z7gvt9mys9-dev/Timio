"""Сценарий владельца, разбитый по темам (B3.1, B4.1–B4.5).

Порядок подключения роутеров сохраняет прежний порядок обработчиков.
"""

import aiogram

from bot.handlers.owner import business
from bot.handlers.owner import calc
from bot.handlers.owner import services
from bot.handlers.owner import slots
from bot.handlers.owner import visits

router = aiogram.Router()
router.include_routers(
    calc.router, business.router, services.router, slots.router,
    visits.router,
)

# Точки входа, которые вызывает common.py.
start_calc = calc.start_calc
open_business = business.open_business
start_new_slot = slots.start_new_slot

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.models.bot_config import BotConfig
from app.services.admin_store import (
    check_bitrix,
    check_telegram,
    get_admin_bot_config,
    load_admin_bot_configs,
    recent_events,
    save_admin_bot_config,
)
from app.services.bot_registry import registry

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/admin/templates")


def _admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "admin")


def _admin_token() -> str:
    return os.getenv("ADMIN_SESSION_TOKEN", _admin_password())


def _is_authenticated(request: Request) -> bool:
    return secrets.compare_digest(request.cookies.get("admin_session", ""), _admin_token())


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=303)


def _redirect_with_message(message: str) -> RedirectResponse:
    return _redirect(f"/admin/?message={quote(message)}")


def _render(request: Request, template: str, **context: Any) -> HTMLResponse | RedirectResponse:
    if not _is_authenticated(request):
        return _redirect("/admin/login")
    return templates.TemplateResponse(request, template, context)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    if not secrets.compare_digest(str(form.get("password", "")), _admin_password()):
        return templates.TemplateResponse(request, "login.html", {"error": "Неверный пароль"}, status_code=401)
    response = _redirect("/admin/")
    response.set_cookie("admin_session", _admin_token(), httponly=True, samesite="lax")
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = _redirect("/admin/login")
    response.delete_cookie("admin_session")
    return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
    bots = load_admin_bot_configs()
    return _render(request, "dashboard.html", bots=bots, events=recent_events(20), message=request.query_params.get("message"))


@router.get("/bots/new", response_class=HTMLResponse)
async def new_bot(request: Request) -> HTMLResponse | RedirectResponse:
    return _render(request, "bot_form.html", bot=None, errors=[])


@router.get("/bots/{bot_id}/edit", response_class=HTMLResponse)
async def edit_bot(request: Request, bot_id: str) -> HTMLResponse | RedirectResponse:
    bot = get_admin_bot_config(bot_id)
    if bot is None:
        return _redirect_with_message("Бот не найден")
    return _render(request, "bot_form.html", bot=bot, errors=[])


async def _config_from_form(request: Request, bot_id: str | None = None) -> BotConfig:
    form = await request.form()
    now = datetime.now(timezone.utc)
    existing = get_admin_bot_config(bot_id) if bot_id else None
    return BotConfig(
        id=bot_id or str(form.get("id") or form.get("name") or "").strip(),
        name=str(form.get("name") or "").strip(),
        telegram_bot_token=str(form.get("telegram_bot_token") or "").strip(),
        bitrix_webhook_url=str(form.get("bitrix_webhook_url") or "").strip() or None,
        enabled=form.get("enabled") == "on",
        secret=str(form.get("secret") or "").strip() or None,
        allowed_ips=str(form.get("allowed_ips") or ""),
        rate_limit=int(form["rate_limit"]) if form.get("rate_limit") else None,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )


@router.post("/bots")
async def create_bot(request: Request) -> HTMLResponse | RedirectResponse:
    if not _is_authenticated(request):
        return _redirect("/admin/login")
    try:
        config = await _config_from_form(request)
        save_admin_bot_config(config)
        await registry.reload()
    except ValidationError as exc:
        return templates.TemplateResponse(request, "bot_form.html", {"bot": None, "errors": exc.errors()}, status_code=400)
    return _redirect_with_message("Бот создан")


@router.post("/bots/{bot_id}")
async def update_bot(request: Request, bot_id: str) -> HTMLResponse | RedirectResponse:
    if not _is_authenticated(request):
        return _redirect("/admin/login")
    try:
        config = await _config_from_form(request, bot_id=bot_id)
        save_admin_bot_config(config)
        await registry.reload()
    except ValidationError as exc:
        return templates.TemplateResponse(request, "bot_form.html", {"bot": get_admin_bot_config(bot_id), "errors": exc.errors()}, status_code=400)
    return _redirect_with_message("Бот обновлен")


@router.post("/bots/{bot_id}/toggle")
async def toggle_bot(request: Request, bot_id: str) -> RedirectResponse:
    if not _is_authenticated(request):
        return _redirect("/admin/login")
    bot = get_admin_bot_config(bot_id)
    if bot is None:
        return _redirect_with_message("Бот не найден")
    bot.enabled = not bot.enabled
    bot.updated_at = datetime.now(timezone.utc)
    save_admin_bot_config(bot)
    await registry.reload()
    return _redirect_with_message("Статус бота изменен")


@router.post("/bots/{bot_id}/check")
async def check_connections(request: Request, bot_id: str) -> RedirectResponse:
    if not _is_authenticated(request):
        return _redirect("/admin/login")
    bot = get_admin_bot_config(bot_id)
    if bot is None:
        return _redirect_with_message("Бот не найден")
    tg_ok, tg_msg = await check_telegram(bot.telegram_bot_token)
    bx_ok, bx_msg = await check_bitrix(str(bot.bitrix_webhook_url) if bot.bitrix_webhook_url else None)
    status = "OK" if tg_ok and bx_ok else "ошибка"
    return _redirect_with_message(f"Проверка: {status}. Telegram: {tg_msg}. Битрикс: {bx_msg}")

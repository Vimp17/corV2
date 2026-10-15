"""Proposals to change the ERA repair plan, delivered through Промайн.

Two transports, chosen by MAI_PROMINE_TRANSPORT:
  smtp - an e-mail to the Промайн mailbox (MAI_PROMINE_EMAIL_TO) through MAI_SMTP_*;
  api  - a JSON POST to the Промайн endpoint (MAI_PROMINE_API_URL, optional bearer token).
With "none" (the default) a proposal is stored as not_configured and can be resent later.
Recipients come only from settings, never from the request, so the endpoint cannot be used
to send mail to arbitrary addresses.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import uuid
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from urllib.parse import urlparse

import requests

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

CHANGE_LABELS = {
    "include": "Включить скважину в ЭРА ремонта",
    "exclude": "Исключить скважину из ЭРА ремонта",
    "reschedule": "Перенести сроки ремонта",
    "scope": "Изменить состав работ",
    "other": "Другое изменение ЭРА ремонта",
}


class PromineDeliveryError(RuntimeError):
    pass


def configured() -> tuple[bool, str]:
    """Whether the selected transport has what it needs, and a short Russian explanation."""
    transport = settings.promine_transport
    if transport == "smtp":
        missing = [name for name, value in (("MAI_SMTP_HOST", settings.smtp_host),
                                            ("MAI_SMTP_FROM", settings.smtp_from),
                                            ("MAI_PROMINE_EMAIL_TO", settings.promine_email_to)) if not value]
        return (not missing, "Почта в Промайн настроена." if not missing
                else "Не заданы " + ", ".join(missing) + ".")
    if transport == "api":
        return (bool(settings.promine_api_url), "API Промайн настроен." if settings.promine_api_url
                else "Не задан MAI_PROMINE_API_URL.")
    return False, "Отправка в Промайн не настроена (MAI_PROMINE_TRANSPORT=none): предложения сохраняются без отправки."


def _yes_no(value: Any, yes: str = "есть", no: str = "нет") -> str:
    return "нет данных" if value is None else (yes if value else no)


def _fmt_date(value: Any) -> str:
    if not value:
        return "не указана"
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return value.strftime("%d.%m.%Y")


def compose(proposal_id: str, well: dict, change_type: str, reason: str,
            proposed_date: date | None, author: str | None) -> tuple[str, str]:
    """Subject and plain-text body of the proposal, in Russian, with the rating evidence."""
    place = f"скважина {well['well_id']}" + (f" ({well['field']})" if well.get("field") else "")
    # Collapse whitespace: well and field names come from uploads and must not break the header.
    subject = " ".join(f"ЭРА ремонта: {CHANGE_LABELS[change_type]} — {place}".split())[:500]
    index = well.get("index")
    ntc = well.get("model_ntc")
    math = well.get("model_math")
    lines = [
        "Предложение по изменению ЭРА ремонта",
        "",
        f"Скважина: {well['well_id']}" + (f", месторождение {well['field']}" if well.get("field") else ""),
        f"Предложение: {CHANGE_LABELS[change_type]}",
        f"Желаемая дата: {_fmt_date(proposed_date)}",
        "",
        "Причина:",
        reason,
        "",
        "Данные рейтинга:",
        f"- Индекс приоритета: {index if index is not None else 'нет оценок'}"
        + (f" (модель НТЦ {ntc:.2f}" if ntc is not None else " (модель НТЦ —")
        + (f", мат. модель {math:.2f})" if math is not None else ", мат. модель —)"),
        f"- Мероприятие по рейтингу: {well.get('measure_label') or '—'}",
        f"- Категория скважины: {well.get('category_label') or 'нет данных'}; "
        f"среда: {_yes_no(well.get('corrosive_environment'), 'коррозионная', 'некоррозионная')}",
        f"- Защита: {well.get('protection_label') or 'нет данных'}"
        + (f" (индекс без учёта защиты {well.get('index_raw')})" if well.get("protection_adjusted") else ""),
        f"- Признак коррозии при демонтаже: {_yes_no(well.get('demount_corrosion'))}; "
        f"результат разбора на базе: {_yes_no(well.get('base_corrosion'), 'есть коррозия', 'коррозии нет')}",
        f"- План ППР (ДДО): {_fmt_date(well.get('plan_date'))}; крайний срок: {_fmt_date(well.get('deadline'))}",
        f"- Документ Промайн: {well.get('promine_status_label') or 'нет'}",
        "",
        f"Автор: {author or 'не указан'}",
        f"Отправлено из системы «Контроль коррозии скважин»: {settings.dashboard_url}",
        f"Номер предложения: {proposal_id}",
    ]
    return subject, "\n".join(lines)


def _send_smtp(proposal: dict) -> str | None:
    message = EmailMessage()
    message["Subject"] = proposal["subject"]
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(settings.promine_email_to)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(idstring=proposal["proposal_id"].replace("-", ""))
    message["X-MAI-Proposal-ID"] = proposal["proposal_id"]
    message.set_content(proposal["body"])
    timeout = settings.promine_timeout_seconds
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        client = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)
    return message["Message-ID"]


def _send_api(proposal: dict) -> str | None:
    url = urlparse(settings.promine_api_url)
    if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
        raise PromineDeliveryError("MAI_PROMINE_API_URL must be an http(s) URL without embedded credentials.")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.promine_api_token:
        headers["Authorization"] = f"Bearer {settings.promine_api_token}"
    payload = {
        "proposal_id": proposal["proposal_id"],
        "type": "era_repair_change",
        "well_id": proposal["well_id"],
        "change_type": proposal["change_type"],
        "change_label": CHANGE_LABELS[proposal["change_type"]],
        "proposed_date": proposal.get("proposed_date"),
        "reason": proposal["reason"],
        "author": proposal.get("author"),
        "subject": proposal["subject"],
        "body": proposal["body"],
        "rating": proposal.get("rating_json"),
    }
    response = requests.post(settings.promine_api_url, json=payload, headers=headers,
                             timeout=settings.promine_timeout_seconds, allow_redirects=False)
    if response.status_code >= 300:
        raise PromineDeliveryError(f"Промайн ответил HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        for key in ("id", "document_id", "message_id"):
            if data.get(key):
                return str(data[key])[:256]
    return None


def deliver(proposal_id: str) -> dict:
    """Send (or resend) a stored proposal and record the outcome on it."""
    proposal = db.get_era_proposal(proposal_id)
    if proposal is None:
        raise KeyError(proposal_id)
    ready, explanation = configured()
    attempts = (proposal.get("attempts") or 0) + 1
    transport = settings.promine_transport
    if not ready:
        db.update_era_proposal(proposal_id, status="not_configured", transport=transport,
                               attempts=attempts, error=explanation[:2000])
        return db.get_era_proposal(proposal_id)
    try:
        external_id = _send_smtp(proposal) if transport == "smtp" else _send_api(proposal)
    except (OSError, smtplib.SMTPException, requests.RequestException, PromineDeliveryError) as exc:
        logger.warning("Promine delivery failed proposal_id=%s transport=%s error=%s",
                       proposal_id, transport, type(exc).__name__)
        detail = str(exc) if isinstance(exc, PromineDeliveryError) else type(exc).__name__
        db.update_era_proposal(proposal_id, status="failed", transport=transport, attempts=attempts,
                               error=f"Не удалось отправить ({detail}).")
    else:
        db.update_era_proposal(proposal_id, status="sent", transport=transport, attempts=attempts,
                               sent_at=datetime.now(timezone.utc), external_id=external_id, error=None)
    return db.get_era_proposal(proposal_id)


def create_and_send(well: dict, change_type: str, reason: str, proposed_date: date | None,
                    author: str | None) -> dict:
    proposal_id = str(uuid.uuid4())
    subject, body = compose(proposal_id, well, change_type, reason, proposed_date, author)
    snapshot = {key: well.get(key) for key in (
        "rank", "index", "measure", "measure_label", "model_ntc", "model_math", "category",
        "corrosive_environment", "demount_corrosion", "base_corrosion", "plan_date", "deadline",
        "promine_status", "field", "protection", "index_raw")}
    db.create_era_proposal({
        "proposal_id": proposal_id, "well_id": well["well_id"], "change_type": change_type,
        "reason": reason, "proposed_date": proposed_date, "author": author,
        "created_at": datetime.now(timezone.utc), "status": "queued",
        "transport": settings.promine_transport, "attempts": 0,
        "subject": subject, "body": body, "rating_json": snapshot,
    })
    return deliver(proposal_id)

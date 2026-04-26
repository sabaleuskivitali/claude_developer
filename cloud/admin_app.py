"""cloud-admin — separate FastAPI app for the admin panel.

Reads from /app/data/admin.db (written by main.py ingest).
Also reads /app/data/users.db to resolve server names.
Served at /admin/* via nginx proxy.
Auth: URL-only for now (single admin).
"""

import datetime
import json
import os
import re
import sqlite3
from pathlib import Path

import anthropic as _anthropic
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

ADMIN_DB  = Path("/app/data/admin.db")
USERS_DB  = Path("/app/data/users.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

STATUS_COLOR = {
    "open":          "#ef4444",
    "investigating": "#f59e0b",
    "resolved":      "#22c55e",
    "wontfix":       "#9ca3af",
}
INV_STATUS_COLOR = {
    "pending_approval": "#f59e0b",
    "approved":         "#3b82f6",
    "rejected":         "#9ca3af",
    "executed":         "#22c55e",
    "verified":         "#16a34a",
    "failed":           "#ef4444",
    "error":            "#ef4444",
}
SOURCE_LABEL = {
    "postgresql": "PostgreSQL",
    "docker":     "Docker",
    "github_ci":  "GitHub CI",
}

_INVESTIGATE_SYSTEM = """\
Ты системный администратор AI-системы Seamlean.
Тебе дана информация об ошибке из продакшн системы.

Seamlean состоит из:
- cloud: FastAPI (Python) + SQLite + nginx + cloudflared (seamlean.com), Docker Compose
- server: on-prem сервер клиента, FastAPI + PostgreSQL + Docker Compose (Ubuntu 22.04)
- agent: Windows Service (.NET 8) на рабочих машинах клиентов

Проанализируй причину и предложи план устранения.
Отвечай СТРОГО в JSON формате без markdown-обёртки:
{
  "root_cause": "Краткое описание корневой причины (1–2 предложения)",
  "confidence_pct": 75,
  "fix_plan": {
    "L1": "Быстрый автоматический фикс (перезапуск, очистка и т.п.)",
    "L2": "Исправление конфигурации или среды (ручное, без деплоя)",
    "L3": "Изменение кода или архитектуры (требует разработки)",
    "recommended_level": "L1",
    "actions": [
      {"type": "restart_container", "params": {"container": "cloud-api-1"},
       "description": "Перезапустить контейнер API"}
    ]
  },
  "verification_criteria": "Как понять что проблема решена",
  "rollback_plan": "Что делать если фикс не помог",
  "impact": "Влияние выполнения плана на работу системы"
}

Допустимые типы actions:
- restart_container  params: {"container": "имя"}
- get_logs           params: {"container": "имя", "lines": 100}
- send_agent_command params: {"machine_id": "...", "command": "restart"}
- manual             params: {"instruction": "текст для администратора"}
"""


# ── Claude investigation ──────────────────────────────────────────────────────

async def _call_claude_investigation(batch: dict, kb_matches: list) -> dict:
    """Ask Claude to analyze an error batch. Returns parsed result dict."""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not configured"}

    kb_context = ""
    if kb_matches:
        kb_context = "\n\nПохожие случаи из базы знаний Seamlean:"
        for km in kb_matches:
            d = dict(km)
            kb_context += f"\n- Причина: {d['root_cause']}"
            if d.get("fix_applied"):
                kb_context += f", Фикс: {d['fix_applied']}"
            kb_context += f" [{'подтверждён' if d.get('verified') else 'неподтверждён'}]"

    user_msg = (
        f"Ошибка в системе Seamlean:\n\n"
        f"Источник:  {batch['source']}\n"
        f"Компонент: {batch['component'] or 'неизвестен'}\n"
        f"Паттерн:   {batch['pattern']}\n"
        f"Кол-во:    {batch['count']}\n"
        f"Первый раз: {batch['first_seen']}\n"
        f"Последний:  {batch['last_seen']}\n"
        f"Severity:  {batch.get('severity', 'error')}"
        + kb_context
        + "\n\nПроанализируй и предложи план устранения."
    )

    client = _anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1200,
        system=_INVESTIGATE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text.strip()

    # Extract JSON block (may be wrapped in markdown)
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {"error": "Cannot parse response", "raw": text[:500]}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _admin_conn():
    conn = sqlite3.connect(ADMIN_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _server_name(server_token: str) -> str:
    """Resolve server_token → server_name from users.db."""
    try:
        conn = sqlite3.connect(USERS_DB)
        row = conn.execute(
            "SELECT server_name FROM servers WHERE server_token=?", (server_token,)
        ).fetchone()
        conn.close()
        return row[0] if row else server_token[:12] + "…"
    except Exception:
        return server_token[:12] + "…"


# ── HTML helpers ──────────────────────────────────────────────────────────────

_CSS = """
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f3f4f6;color:#111827;font-size:.9rem}
.wrap{max-width:1100px;margin:0 auto;padding:24px 20px}
h1{font-size:1.3rem;font-weight:700;margin-bottom:4px}
h2{font-size:1rem;font-weight:600;margin:20px 0 10px}
.sub{color:#6b7280;font-size:.8rem;margin-bottom:20px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
      padding:16px;margin-bottom:12px}
.card:hover{border-color:#d1d5db;box-shadow:0 1px 4px rgba(0,0,0,.06)}
table{width:100%;border-collapse:collapse}
th{padding:6px 10px;text-align:left;font-size:.75rem;font-weight:600;
   color:#6b7280;text-transform:uppercase;border-bottom:1px solid #e5e7eb}
td{padding:8px 10px;border-bottom:1px solid #f3f4f6;vertical-align:top}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:1px 7px;border-radius:12px;
       font-size:.72rem;font-weight:600;color:#fff}
.btn{display:inline-block;padding:5px 14px;border-radius:6px;border:none;
     cursor:pointer;font-size:.82rem;font-weight:500;text-decoration:none;
     transition:opacity .15s}
.btn:hover{opacity:.85}
.btn-blue{background:#3b82f6;color:#fff}
.btn-green{background:#22c55e;color:#fff}
.btn-red{background:#ef4444;color:#fff}
.btn-gray{background:#e5e7eb;color:#374151}
.btn-amber{background:#f59e0b;color:#fff}
.btn-sm{padding:3px 10px;font-size:.76rem}
.stat{display:inline-block;margin-right:20px}
.stat-n{font-size:1.6rem;font-weight:700;line-height:1}
.stat-l{font-size:.75rem;color:#6b7280;margin-top:2px}
nav{display:flex;align-items:center;gap:12px;margin-bottom:20px;
    padding-bottom:12px;border-bottom:1px solid #e5e7eb}
nav a{color:#6b7280;text-decoration:none;font-size:.85rem}
nav a:hover{color:#111}
nav .active{color:#111;font-weight:600}
.pattern{font-family:monospace;font-size:.78rem;color:#374151;
          word-break:break-all;max-width:500px}
.ts{color:#9ca3af;font-size:.75rem;white-space:nowrap}
.empty{text-align:center;padding:40px;color:#9ca3af}
pre{background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;
    padding:12px;font-size:.78rem;overflow-x:auto;white-space:pre-wrap;
    word-break:break-word;max-height:300px;overflow-y:auto}
.form-row{margin-bottom:12px}
label{display:block;font-size:.8rem;font-weight:500;margin-bottom:4px}
textarea{width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;
         font-size:.85rem;resize:vertical}
select{padding:6px 10px;border:1px solid #d1d5db;border-radius:6px;font-size:.85rem}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:700px){.detail-grid{grid-template-columns:1fr}}
.inv-card{border-left:3px solid #f59e0b}
.inv-card.approved{border-left-color:#3b82f6}
.inv-card.verified{border-left-color:#22c55e}
.inv-card.failed{border-left-color:#ef4444}
.field-row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:flex-start}
.field-label{font-weight:600;font-size:.78rem;white-space:nowrap;min-width:130px;
             color:#374151;padding-top:1px}
.field-val{font-size:.82rem;color:#111;flex:1}
.confidence-bar{display:inline-flex;align-items:center;gap:6px;margin-left:6px}
.confidence-bar-bg{width:80px;height:7px;background:#f3f4f6;border-radius:4px;display:inline-block}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #d1d5db;
         border-top-color:#3b82f6;border-radius:50%;animation:spin .6s linear infinite;
         vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
"""


def _page(title: str, body: str) -> HTMLResponse:
    now = datetime.datetime.now().strftime("%H:%M")
    nav = (
        '<nav>'
        '<span style="font-weight:700;color:#111;font-size:.95rem">⚙ Seamlean Admin</span>'
        '<a href="/admin">Ошибки</a>'
        '<a href="/admin/kb">База знаний</a>'
        '<a href="/admin/graph">Граф зависимостей</a>'
        f'<span style="margin-left:auto;color:#9ca3af;font-size:.75rem">{now}</span>'
        '</nav>'
    )
    return HTMLResponse(f"<!doctype html><html><head><title>{title}</title>{_CSS}</head>"
                        f"<body><div class='wrap'>{nav}{body}</div></body></html>")


def _badge(status: str, colors: dict | None = None) -> str:
    c = (colors or STATUS_COLOR).get(status, "#9ca3af")
    return f'<span class="badge" style="background:{c}">{status}</span>'


def _ago(ts_str: str | None) -> str:
    if not ts_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:    return f"{s}с назад"
        if s < 3600:  return f"{s//60}м назад"
        if s < 86400: return f"{s//3600}ч назад"
        return f"{s//86400}д назад"
    except Exception:
        return ts_str[:16]


def _confidence_bar(pct: int | None) -> str:
    if pct is None:
        return ""
    color = "#22c55e" if pct >= 70 else "#f59e0b" if pct >= 40 else "#ef4444"
    return (
        f'<span class="confidence-bar">'
        f'<span class="confidence-bar-bg">'
        f'<span style="display:block;width:{pct}%;height:100%;background:{color};border-radius:4px">'
        f'</span></span>'
        f'<span style="font-size:.75rem;color:{color};font-weight:600">{pct}%</span>'
        f'</span>'
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def dashboard():
    conn = _admin_conn()

    total_open = conn.execute(
        "SELECT COUNT(*) FROM error_batches WHERE status='open'"
    ).fetchone()[0]
    total_batches = conn.execute("SELECT COUNT(*) FROM error_batches").fetchone()[0]
    total_resolved = conn.execute(
        "SELECT COUNT(*) FROM error_batches WHERE status='resolved'"
    ).fetchone()[0]
    total_servers = conn.execute(
        "SELECT COUNT(DISTINCT server_token) FROM error_batches"
    ).fetchone()[0]

    batches = conn.execute("""
        SELECT id, batch_hash, server_token, source, component,
               pattern, severity, first_seen, last_seen, count, status
        FROM error_batches
        ORDER BY
            CASE status WHEN 'open' THEN 0 WHEN 'investigating' THEN 1 ELSE 2 END,
            last_seen DESC
        LIMIT 200
    """).fetchall()
    conn.close()

    stats_html = (
        f'<div style="display:flex;gap:24px;margin-bottom:20px">'
        f'<div class="stat"><div class="stat-n" style="color:#ef4444">{total_open}</div>'
        f'<div class="stat-l">Открытых</div></div>'
        f'<div class="stat"><div class="stat-n" style="color:#f59e0b">'
        f'{total_batches - total_open - total_resolved}</div>'
        f'<div class="stat-l">В работе</div></div>'
        f'<div class="stat"><div class="stat-n" style="color:#22c55e">{total_resolved}</div>'
        f'<div class="stat-l">Решено</div></div>'
        f'<div class="stat"><div class="stat-n">{total_servers}</div>'
        f'<div class="stat-l">Серверов</div></div>'
        f'</div>'
    )

    if not batches:
        body = (stats_html +
                '<div class="card"><div class="empty">Ошибок нет ✅</div></div>')
        return _page("Admin — Ошибки", body)

    rows = ""
    for b in batches:
        srv_name = _server_name(b["server_token"])
        src_label = SOURCE_LABEL.get(b["source"], b["source"])
        rows += (
            f'<tr>'
            f'<td>{_badge(b["status"])}</td>'
            f'<td><span style="font-size:.8rem;color:#374151">{srv_name}</span></td>'
            f'<td><span style="font-size:.75rem;color:#6b7280">{src_label}</span></td>'
            f'<td><span style="font-size:.75rem;color:#6b7280">{b["component"] or "—"}</span></td>'
            f'<td><span class="pattern">{b["pattern"][:120]}{"…" if len(b["pattern"])>120 else ""}</span></td>'
            f'<td style="text-align:right"><span style="font-weight:600">{b["count"]}</span></td>'
            f'<td class="ts">{_ago(b["last_seen"])}</td>'
            f'<td><a href="/admin/batch/{b["id"]}" class="btn btn-blue btn-sm">→</a></td>'
            f'</tr>'
        )

    table = (
        f'<div class="card" style="padding:0;overflow:hidden">'
        f'<table>'
        f'<thead><tr>'
        f'<th>Статус</th><th>Сервер</th><th>Источник</th><th>Компонент</th>'
        f'<th>Паттерн</th><th style="text-align:right">Кол-во</th>'
        f'<th>Последний раз</th><th></th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table></div>'
    )

    body = (
        f'<h1>Ошибки системы</h1>'
        f'<p class="sub">Автоматически собраны с серверов и агентов</p>'
        f'{stats_html}{table}'
    )
    return _page("Admin — Ошибки", body)


@app.get("/admin/batch/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int):
    conn = _admin_conn()
    batch = conn.execute(
        "SELECT * FROM error_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch:
        conn.close()
        return _page("Не найдено", '<div class="empty">Батч не найден</div>')

    investigations = conn.execute(
        "SELECT * FROM investigations WHERE batch_id=? ORDER BY attempt ASC",
        (batch_id,)
    ).fetchall()

    kb_matches = conn.execute(
        """SELECT kb.root_cause, kb.fix_applied, kb.verified, kb.recurrence_count
           FROM knowledge_base kb
           WHERE kb.error_pattern LIKE ?
           LIMIT 3""",
        ("%" + dict(batch)["pattern"][:50] + "%",)
    ).fetchall()
    conn.close()

    b = dict(batch)
    srv_name = _server_name(b["server_token"])
    src_label = SOURCE_LABEL.get(b["source"], b["source"])

    # Determine if "Расследовать" should be shown
    active_statuses = {"pending_approval", "approved", "executed"}
    has_active_inv = any(
        dict(i)["status"] in active_statuses for i in investigations
    )
    can_investigate = (
        b["status"] not in ("resolved", "wontfix")
        and not has_active_inv
        and bool(ANTHROPIC_API_KEY)
    )

    # ── Header card ──────────────────────────────────────────────────────────
    header = (
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">'
        f'<a href="/admin" style="color:#6b7280;text-decoration:none;font-size:.85rem">'
        f'← Все ошибки</a>'
        f'</div>'
        f'<div class="card">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div style="flex:1;min-width:0">'
        f'<div style="font-size:.75rem;color:#6b7280;margin-bottom:6px">'
        f'{srv_name} &nbsp;·&nbsp; {src_label} &nbsp;·&nbsp; {b["component"] or "unknown"}'
        f'</div>'
        f'<div class="pattern" style="font-size:.85rem;line-height:1.4">{b["pattern"]}</div>'
        f'</div>'
        f'<div style="margin-left:12px">{_badge(b["status"])}</div>'
        f'</div>'
        f'<div style="margin-top:12px;display:flex;gap:20px;font-size:.78rem;color:#6b7280;'
        f'flex-wrap:wrap">'
        f'<span>Первый раз: {_ago(b["first_seen"])}</span>'
        f'<span>Последний: {_ago(b["last_seen"])}</span>'
        f'<span>Всего: <strong style="color:#111">{b["count"]}</strong></span>'
        f'<span>Severity: <strong style="color:#111">{b.get("severity","?")}</strong></span>'
        + (f'<span style="color:#ef4444;font-weight:600">🔁 Рецидив ×{b["recurrence"]}</span>'
           if b.get("recurrence") else '')
        + f'</div>'
        + (
            f'<div style="margin-top:14px">'
            f'<form method="post" action="/admin/batch/{batch_id}/investigate" style="display:inline">'
            f'<button type="submit" class="btn btn-amber">'
            f'🔍 Расследовать с Claude</button>'
            f'</form>'
            f'</div>'
            if can_investigate else ""
        )
        + f'</div>'
    )

    # ── Manual action form ────────────────────────────────────────────────────
    action_form = (
        f'<div class="card">'
        f'<h2 style="margin-top:0">Ручное управление</h2>'
        f'<form method="post" action="/admin/batch/{batch_id}/action">'
        f'<div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">'
        f'<div class="form-row" style="margin:0">'
        f'<label>Статус</label>'
        f'<select name="status">'
        + "".join(
            f'<option value="{s}" {"selected" if s==b["status"] else ""}>{s}</option>'
            for s in ("open", "investigating", "resolved", "wontfix")
        )
        + f'</select></div>'
        f'<div class="form-row" style="margin:0;flex:1;min-width:200px">'
        f'<label>Комментарий</label>'
        f'<textarea name="comment" rows="2" placeholder="Описание причины, план..." '
        f'style="min-height:38px"></textarea></div>'
        f'<button type="submit" class="btn btn-blue btn-sm" style="height:34px">'
        f'Сохранить</button>'
        f'</div>'
        f'</form>'
        f'</div>'
    )

    # ── KB matches ────────────────────────────────────────────────────────────
    kb_html = ""
    if kb_matches:
        kb_html = '<div class="card"><h2 style="margin-top:0">Похожие в базе знаний</h2>'
        for km in kb_matches:
            d = dict(km)
            kb_html += (
                f'<div style="padding:8px 0;border-bottom:1px solid #f3f4f6">'
                f'<div style="font-size:.8rem"><strong>Причина:</strong> {d["root_cause"]}</div>'
                + (f'<div style="font-size:.78rem;color:#6b7280">Фикс: {d["fix_applied"]}</div>'
                   if d.get("fix_applied") else "")
                + (f'<div style="font-size:.75rem;color:#22c55e">✅ Подтверждён</div>'
                   if d.get("verified") else "")
                + '</div>'
            )
        kb_html += '</div>'

    # ── Investigations history ────────────────────────────────────────────────
    inv_html = ""
    if investigations:
        inv_html = '<h2>Расследования</h2>'
        for inv in reversed(list(investigations)):
            d = dict(inv)
            fix_plan = json.loads(d["fix_plan"] or "{}") if d["fix_plan"] else {}
            inv_status = d["status"] or "unknown"
            status_extra = f' {inv_status}' if inv_status not in ("pending_approval",) else ""
            inv_class = inv_status if inv_status in ("approved", "verified", "failed") else ""

            # Actions block from fix_plan
            actions_html = ""
            if fix_plan.get("actions"):
                acts = fix_plan["actions"]
                actions_html = (
                    f'<div class="field-row">'
                    f'<span class="field-label">Действия:</span>'
                    f'<span class="field-val">'
                )
                for a in acts:
                    atype = a.get("type", "?")
                    adesc = a.get("description", "")
                    aparams = a.get("params", {})
                    actions_html += (
                        f'<div style="font-family:monospace;font-size:.76rem;'
                        f'background:#f9fafb;border:1px solid #e5e7eb;border-radius:4px;'
                        f'padding:3px 7px;margin-bottom:4px;display:inline-block;margin-right:6px">'
                        f'<strong>{atype}</strong>'
                        + (f'({", ".join(f"{k}={v}" for k,v in aparams.items())})'
                           if aparams else "")
                        + (f' — {adesc}' if adesc else "")
                        + '</div>'
                    )
                actions_html += '</span></div>'

            # Approve/reject buttons for pending
            approval_html = ""
            if inv_status == "pending_approval":
                approval_html = (
                    f'<div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">'
                    f'<form method="post" action="/admin/investigation/{d["id"]}/approve">'
                    f'<button type="submit" class="btn btn-green">'
                    f'✅ Одобрить план</button></form>'
                    f'<form method="post" action="/admin/investigation/{d["id"]}/reject">'
                    f'<input type="hidden" name="reason" value="Отклонено администратором">'
                    f'<button type="submit" class="btn btn-gray">'
                    f'✗ Отклонить</button></form>'
                    f'</div>'
                )

            inv_html += (
                f'<div class="card inv-card {inv_class}">'
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<span style="font-weight:600;font-size:.85rem">Попытка #{d["attempt"]}'
                f'{status_extra}</span>'
                f'{_badge(inv_status, INV_STATUS_COLOR)}'
                f'</div>'
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">Причина:</span>'
                    f'<span class="field-val">{d["root_cause"]}'
                    + _confidence_bar(d["confidence_pct"])
                    + '</span></div>'
                    if d.get("root_cause") else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">Рекомендация:</span>'
                    f'<span class="field-val" style="font-weight:600">'
                    f'{fix_plan.get("recommended_level","?")} — '
                    + (fix_plan.get(fix_plan.get("recommended_level","L1"), "—"))
                    + '</span></div>'
                    if fix_plan else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">L1:</span>'
                    f'<span class="field-val">{fix_plan["L1"]}</span></div>'
                    if fix_plan.get("L1") else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">L2:</span>'
                    f'<span class="field-val">{fix_plan["L2"]}</span></div>'
                    if fix_plan.get("L2") else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">L3:</span>'
                    f'<span class="field-val">{fix_plan["L3"]}</span></div>'
                    if fix_plan.get("L3") else ""
                )
                + actions_html
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">Проверка:</span>'
                    f'<span class="field-val">{d["verification_criteria"]}</span></div>'
                    if d.get("verification_criteria") else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">Откат:</span>'
                    f'<span class="field-val">{d["rollback_plan"]}</span></div>'
                    if d.get("rollback_plan") else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">Влияние:</span>'
                    f'<span class="field-val">{d["impact"]}</span></div>'
                    if d.get("impact") else ""
                )
                + (
                    f'<div class="field-row">'
                    f'<span class="field-label">Комментарий:</span>'
                    f'<span class="field-val" style="color:#6b7280">{d["admin_comment"]}'
                    f'</span></div>'
                    if d.get("admin_comment") else ""
                )
                + f'<div style="margin-top:10px;font-size:.72rem;color:#9ca3af">'
                + f'Создано: {_ago(d["created_at"])}'
                + (f' · Одобрено: {_ago(d["approved_at"])}' if d.get("approved_at") else "")
                + (f' · Закрыто: {_ago(d.get("resolved_at"))}' if d.get("resolved_at") else "")
                + '</div>'
                + approval_html
                + '</div>'
            )

    body = header + action_form + kb_html + inv_html
    return _page(f"Ошибка #{batch_id}", body)


@app.post("/admin/batch/{batch_id}/action")
async def batch_action(batch_id: int, request: Request,
                       status: str = Form(...), comment: str = Form("")):
    conn = sqlite3.connect(ADMIN_DB)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        "UPDATE error_batches SET status=? WHERE id=?", (status, batch_id)
    )
    if comment.strip():
        existing = conn.execute(
            "SELECT id FROM investigations WHERE batch_id=? ORDER BY attempt DESC LIMIT 1",
            (batch_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE investigations SET admin_comment=? WHERE id=?",
                (comment.strip(), existing[0])
            )
        else:
            max_attempt = conn.execute(
                "SELECT COALESCE(MAX(attempt),0) FROM investigations WHERE batch_id=?",
                (batch_id,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO investigations
                   (batch_id, attempt, status, admin_comment, created_at)
                   VALUES (?,?,'pending_approval',?,?)""",
                (batch_id, max_attempt + 1, comment.strip(), now)
            )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.post("/admin/batch/{batch_id}/investigate")
async def batch_investigate(batch_id: int):
    """Trigger Claude investigation for this error batch."""
    conn = _admin_conn()
    batch = conn.execute(
        "SELECT * FROM error_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch:
        conn.close()
        return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)

    b = dict(batch)
    kb_matches = conn.execute(
        """SELECT root_cause, fix_applied, verified
           FROM knowledge_base WHERE error_pattern LIKE ? LIMIT 3""",
        ("%" + b["pattern"][:50] + "%",)
    ).fetchall()

    max_attempt = conn.execute(
        "SELECT COALESCE(MAX(attempt),0) FROM investigations WHERE batch_id=?",
        (batch_id,)
    ).fetchone()[0]
    attempt = max_attempt + 1
    conn.close()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Call Claude (awaited — page will wait ~5–15s)
    result = await _call_claude_investigation(b, list(kb_matches))

    write_conn = sqlite3.connect(ADMIN_DB)
    if "error" in result:
        write_conn.execute(
            """INSERT INTO investigations (batch_id, attempt, root_cause, status, created_at)
               VALUES (?,?,?,?,?)""",
            (batch_id, attempt,
             f"Ошибка анализа: {result.get('error', '')} — {result.get('raw', '')}",
             "error", now)
        )
    else:
        fix_plan = result.get("fix_plan", {})
        write_conn.execute(
            """INSERT INTO investigations
               (batch_id, attempt, root_cause, confidence_pct, fix_plan,
                verification_criteria, rollback_plan, impact, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                batch_id, attempt,
                result.get("root_cause", ""),
                result.get("confidence_pct"),
                json.dumps(fix_plan, ensure_ascii=False),
                result.get("verification_criteria", ""),
                result.get("rollback_plan", ""),
                result.get("impact", ""),
                "pending_approval",
                now,
            )
        )
        write_conn.execute(
            "UPDATE error_batches SET status='investigating' WHERE id=?", (batch_id,)
        )
    write_conn.commit()
    write_conn.close()

    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.post("/admin/investigation/{inv_id}/approve")
async def investigation_approve(inv_id: int):
    """Mark investigation as approved — admin acknowledges the plan."""
    conn = sqlite3.connect(ADMIN_DB)
    inv = conn.execute(
        "SELECT batch_id FROM investigations WHERE id=?", (inv_id,)
    ).fetchone()
    if not inv:
        conn.close()
        return RedirectResponse("/admin", status_code=303)

    batch_id = inv[0]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        "UPDATE investigations SET status='approved', approved_at=? WHERE id=?",
        (now, inv_id)
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.post("/admin/investigation/{inv_id}/reject")
async def investigation_reject(inv_id: int, reason: str = Form("Отклонено администратором")):
    """Reject investigation plan, reopen batch."""
    conn = sqlite3.connect(ADMIN_DB)
    inv = conn.execute(
        "SELECT batch_id FROM investigations WHERE id=?", (inv_id,)
    ).fetchone()
    if not inv:
        conn.close()
        return RedirectResponse("/admin", status_code=303)

    batch_id = inv[0]
    conn.execute(
        "UPDATE investigations SET status='rejected', admin_comment=? WHERE id=?",
        (reason, inv_id)
    )
    conn.execute(
        "UPDATE error_batches SET status='open' WHERE id=?", (batch_id,)
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.get("/admin/kb", response_class=HTMLResponse)
def knowledge_base():
    conn = _admin_conn()
    entries = conn.execute(
        "SELECT * FROM knowledge_base ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    conn.close()

    if not entries:
        body = ('<h1>База знаний</h1>'
                '<div class="card"><div class="empty">'
                'Пока пусто — база заполняется автоматически при решении ошибок</div></div>')
        return _page("Admin — База знаний", body)

    rows = ""
    for e in entries:
        d = dict(e)
        rows += (
            f'<tr>'
            f'<td><span class="pattern">{d["error_pattern"][:80]}…</span></td>'
            f'<td style="font-size:.8rem">{d["component"] or "—"}</td>'
            f'<td style="font-size:.8rem">{d["root_cause"][:100]}</td>'
            f'<td style="font-size:.8rem;color:#6b7280">{d["fix_applied"] or "—"}</td>'
            f'<td>{"✅" if d["verified"] else "⏳"}</td>'
            f'<td class="ts">{_ago(d["created_at"])}</td>'
            f'</tr>'
        )

    body = (
        f'<h1>База знаний</h1>'
        f'<p class="sub">Накапливается автоматически из закрытых расследований</p>'
        f'<div class="card" style="padding:0;overflow:hidden">'
        f'<table><thead><tr>'
        f'<th>Паттерн</th><th>Компонент</th><th>Причина</th>'
        f'<th>Фикс</th><th>Подтверждён</th><th>Дата</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
    )
    return _page("Admin — База знаний", body)


@app.get("/admin/graph", response_class=HTMLResponse)
def dependency_graph():
    conn = _admin_conn()
    nodes = conn.execute("SELECT * FROM dep_nodes").fetchall()
    edges = conn.execute("""
        SELECT e.relation, e.confidence_pct, e.evidence_count,
               n1.name AS from_name, n2.name AS to_name,
               n1.type AS from_type, n2.type AS to_type
        FROM dep_edges e
        JOIN dep_nodes n1 ON n1.id = e.from_node
        JOIN dep_nodes n2 ON n2.id = e.to_node
        ORDER BY e.confidence_pct DESC
    """).fetchall()
    conn.close()

    if not edges:
        body = ('<h1>Граф зависимостей</h1>'
                '<div class="card"><div class="empty">'
                'Граф строится автоматически в процессе расследования ошибок</div></div>')
        return _page("Admin — Граф", body)

    rows = ""
    for e in edges:
        d = dict(e)
        conf_color = ("#22c55e" if d["confidence_pct"] >= 70 else
                      "#f59e0b" if d["confidence_pct"] >= 40 else "#9ca3af")
        rows += (
            f'<tr>'
            f'<td style="font-weight:500">{d["from_name"]}</td>'
            f'<td style="font-size:.75rem;color:#6b7280">{d["relation"]}</td>'
            f'<td style="font-weight:500">{d["to_name"]}</td>'
            f'<td>'
            f'<div style="display:flex;align-items:center;gap:6px">'
            f'<div style="width:60px;height:6px;background:#f3f4f6;border-radius:3px">'
            f'<div style="width:{d["confidence_pct"]}%;height:100%;'
            f'background:{conf_color};border-radius:3px"></div></div>'
            f'<span style="font-size:.75rem;color:{conf_color}">{d["confidence_pct"]}%</span>'
            f'</div></td>'
            f'<td class="ts">{d["evidence_count"]} подтв.</td>'
            f'</tr>'
        )

    body = (
        f'<h1>Граф зависимостей</h1>'
        f'<p class="sub">{len(nodes)} компонентов · {len(edges)} связей</p>'
        f'<div class="card" style="padding:0;overflow:hidden">'
        f'<table><thead><tr>'
        f'<th>Компонент</th><th>Связь</th><th>Зависит от</th>'
        f'<th>Уверенность</th><th>Подтверждений</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
    )
    return _page("Admin — Граф зависимостей", body)


@app.get("/admin/health")
def health():
    try:
        conn = _admin_conn()
        total = conn.execute("SELECT COUNT(*) FROM error_batches").fetchone()[0]
        conn.close()
        return {
            "status": "ok",
            "error_batches": total,
            "claude_ready": bool(ANTHROPIC_API_KEY),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})

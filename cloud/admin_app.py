"""cloud-admin — FastAPI admin panel (v3)."""

import datetime
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

import anthropic as _anthropic
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

ADMIN_DB  = Path("/app/data/admin.db")
USERS_DB  = Path("/app/data/users.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Labels & colours ──────────────────────────────────────────────────────────

STATUS_LABEL = {
    "open":          "Открыт",
    "investigating": "В работе",
    "resolved":      "Решено",
    "wontfix":       "Не исправлять",
}
STATUS_COLOR = {
    "open":          "#ef4444",
    "investigating": "#f59e0b",
    "resolved":      "#22c55e",
    "wontfix":       "#9ca3af",
}
INV_STATUS_LABEL = {
    "pending_approval": "Требуется подтверждение",
    "approved":         "Одобрено",
    "rejected":         "Отклонено",
    "executed":         "Выполнено",
    "verified":         "Подтверждено",
    "failed":           "Ошибка выполнения",
    "error":            "Ошибка анализа",
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
SOURCE_TOOLTIP = {
    "postgresql": "Ошибка зафиксирована в логах PostgreSQL на on-prem сервере",
    "docker":     "Ошибка из логов Docker-контейнера",
    "github_ci":  "Ошибка из GitHub CI при сборке или деплое",
}
DEFAULT_STATUSES = "open,investigating"   # фильтр по умолчанию
_AGENT_LAYERS = {"window", "visual", "system", "applogs", "browser", "agent"}

# ── Claude investigation ──────────────────────────────────────────────────────

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
Допустимые типы actions: restart_container, get_logs, send_agent_command, manual.
"""


async def _call_claude_investigation(batch: dict, kb_matches: list,
                                     wishes: str = "") -> dict:
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
    wishes_block = f"\n\nДополнительные пожелания администратора: {wishes}" if wishes.strip() else ""
    user_msg = (
        f"Ошибка в системе Seamlean:\n\n"
        f"Источник:  {batch['source']}\n"
        f"Компонент: {batch['component'] or 'неизвестен'}\n"
        f"Паттерн:   {batch['pattern']}\n"
        f"Кол-во:    {batch['count']}\n"
        f"Первый раз: {batch['first_seen']}\n"
        f"Последний:  {batch['last_seen']}\n"
        f"Severity:  {batch.get('severity', 'error')}"
        + kb_context + wishes_block
        + "\n\nПроанализируй и предложи план устранения."
    )
    client = _anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model="claude-opus-4-5", max_tokens=1200,
        system=_INVESTIGATE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = response.content[0].text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {"error": "Cannot parse response", "raw": text[:500]}


async def _execute_fix_plan(batch_id: int, inv_id: int, fix_plan: dict) -> RedirectResponse:
    """Execute actions from a fix_plan dict, update investigation status."""
    actions = fix_plan.get("actions", [])
    results = []
    has_failure = False
    for action in actions:
        atype = action.get("type", "")
        params = action.get("params", {})
        desc = action.get("description", "")
        if atype == "restart_container":
            container = params.get("container", "")
            try:
                r = subprocess.run(["docker", "restart", container],
                                   capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0
                if not ok:
                    has_failure = True
                results.append({"type": atype, "container": container, "ok": ok,
                                 "output": (r.stdout + r.stderr)[:300]})
            except Exception as e:
                has_failure = True
                results.append({"type": atype, "container": container,
                                 "ok": False, "error": str(e)})
        elif atype == "get_logs":
            container = params.get("container", "")
            lines = params.get("lines", 100)
            try:
                r = subprocess.run(["docker", "logs", "--tail", str(lines), container],
                                   capture_output=True, text=True, timeout=30)
                results.append({"type": atype, "container": container, "ok": True,
                                 "output": (r.stdout + r.stderr)[:500]})
            except Exception as e:
                has_failure = True
                results.append({"type": atype, "container": container,
                                 "ok": False, "error": str(e)})
        elif atype in ("manual", "send_agent_command"):
            results.append({"type": atype, "ok": None,
                             "note": params.get("instruction", desc or str(params))})
        else:
            results.append({"type": atype, "ok": None, "note": "Неизвестный тип"})

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_status = "failed" if has_failure else "executed"
    write_conn = sqlite3.connect(ADMIN_DB)
    write_conn.execute(
        "UPDATE investigations SET status=?, approved_at=?, resolved_at=?, admin_comment=? WHERE id=?",
        (new_status, now, now, json.dumps(results, ensure_ascii=False), inv_id)
    )
    if not has_failure:
        write_conn.execute(
            "UPDATE error_batches SET status='resolved' WHERE id=?", (batch_id,)
        )
    write_conn.commit()
    write_conn.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _admin_conn():
    conn = sqlite3.connect(ADMIN_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _server_name(server_token: str) -> str:
    try:
        conn = sqlite3.connect(USERS_DB)
        row = conn.execute(
            "SELECT server_name FROM servers WHERE server_token=?", (server_token,)
        ).fetchone()
        conn.close()
        return row[0] if row else server_token[:12] + "…"
    except Exception:
        return server_token[:12] + "…"


def _server_info(server_token: str) -> dict:
    try:
        conn = sqlite3.connect(USERS_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM servers WHERE server_token=?",
                           (server_token,)).fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


def _all_servers_info() -> list:
    try:
        conn = sqlite3.connect(USERS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM servers ORDER BY server_name").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _kb_for_batch(pattern: str, conn) -> list:
    return conn.execute(
        "SELECT root_cause, fix_applied, verified FROM knowledge_base"
        " WHERE error_pattern LIKE ? LIMIT 3",
        ("%" + pattern[:50] + "%",)
    ).fetchall()


# ── HTML/CSS/JS ───────────────────────────────────────────────────────────────

_CSS = """
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f3f4f6;color:#111827;font-size:.9rem}
.wrap{max-width:1300px;margin:0 auto;padding:24px 20px}
h1{font-size:1.3rem;font-weight:700;margin-bottom:4px}
h2{font-size:1rem;font-weight:600;margin:20px 0 10px}
.sub{color:#6b7280;font-size:.8rem;margin-bottom:16px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;
      padding:16px;margin-bottom:12px}
.card:hover{border-color:#d1d5db;box-shadow:0 1px 4px rgba(0,0,0,.06)}
table{width:100%;border-collapse:collapse}
th{padding:6px 10px;text-align:left;font-size:.75rem;font-weight:600;
   color:#6b7280;text-transform:uppercase;border-bottom:1px solid #e5e7eb;
   white-space:nowrap}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:#374151}
th.sort-asc::after{content:" ↑";color:#3b82f6}
th.sort-desc::after{content:" ↓";color:#3b82f6}
td{padding:7px 10px;border-bottom:1px solid #f3f4f6;vertical-align:middle}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;
       font-size:.72rem;font-weight:600;color:#fff;white-space:nowrap}
.btn{display:inline-block;padding:5px 14px;border-radius:6px;border:none;
     cursor:pointer;font-size:.82rem;font-weight:500;text-decoration:none;
     transition:opacity .15s;line-height:1.4}
.btn:hover{opacity:.85}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-blue{background:#3b82f6;color:#fff}
.btn-green{background:#22c55e;color:#fff}
.btn-red{background:#ef4444;color:#fff}
.btn-gray{background:#e5e7eb;color:#374151}
.btn-amber{background:#f59e0b;color:#fff}
.btn-purple{background:#8b5cf6;color:#fff}
.btn-sm{padding:3px 9px;font-size:.76rem}
nav{display:flex;align-items:center;gap:12px;margin-bottom:20px;
    padding-bottom:12px;border-bottom:1px solid #e5e7eb}
nav a{color:#6b7280;text-decoration:none;font-size:.85rem}
nav a:hover{color:#111}
.pattern{font-family:monospace;font-size:.77rem;color:#374151;
          word-break:break-all;max-width:420px}
.ts{color:#9ca3af;font-size:.75rem;white-space:nowrap}
.empty{text-align:center;padding:40px;color:#9ca3af}
pre{background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;
    padding:12px;font-size:.78rem;overflow-x:auto;white-space:pre-wrap;
    word-break:break-word;max-height:300px;overflow-y:auto}
label{display:block;font-size:.8rem;font-weight:500;margin-bottom:4px}
textarea{width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;
         font-size:.85rem;resize:vertical}
select{padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:.82rem}
.tkt-id{font-size:.68rem;color:#9ca3af;font-family:monospace;display:block;
        margin-top:2px;white-space:nowrap}
.row-num{color:#c4c4c4;font-size:.72rem;font-weight:600}
/* Stats bar */
.stats-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
.stat-toggle{display:flex;align-items:center;gap:8px;padding:8px 14px;
             border-radius:8px;cursor:pointer;border:2px solid transparent;
             background:#fff;transition:all .15s;user-select:none}
.stat-toggle:hover{border-color:#d1d5db}
.stat-toggle.active{border-color:#3b82f6;background:#eff6ff}
.stat-toggle.inactive{opacity:.55}
.stat-n{font-size:1.4rem;font-weight:700;line-height:1;display:flex;
        align-items:baseline;gap:3px}
.stat-check{font-size:.85rem;color:#22c55e;transition:opacity .15s}
.stat-l{font-size:.72rem;color:#6b7280;margin-top:2px}
.stat-servers{background:#fff;border:2px solid #e5e7eb;border-radius:8px;
              padding:8px 14px;cursor:pointer;text-decoration:none;display:flex;
              flex-direction:column;align-items:center}
.stat-servers:hover{border-color:#6b7280}
/* Filter row */
.filter-row th{padding:3px 6px;background:#f9fafb}
.filter-row select{font-size:.72rem;padding:2px 4px;max-width:110px;width:100%}
/* Pagination */
.pagination{display:flex;gap:6px;align-items:center;justify-content:flex-end;
            margin-top:12px;flex-wrap:wrap}
.page-btn{padding:4px 10px;border:1px solid #e5e7eb;border-radius:5px;
          background:#fff;cursor:pointer;font-size:.8rem;text-decoration:none;color:#374151}
.page-btn:hover{background:#f3f4f6}
.page-btn.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.page-info{font-size:.78rem;color:#6b7280}
/* Inline forms */
.inline-form{margin-top:8px;padding:10px;background:#f9fafb;border:1px solid #e5e7eb;
             border-radius:6px;display:none}
/* Inv card */
.inv-card{border-left:3px solid #f59e0b}
.inv-card.approved{border-left-color:#3b82f6}
.inv-card.verified,.inv-card.executed{border-left-color:#22c55e}
.inv-card.failed,.inv-card.error{border-left-color:#ef4444}
.inv-card.rejected{border-left-color:#9ca3af}
.field-row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:flex-start}
.field-label{font-weight:600;font-size:.78rem;white-space:nowrap;min-width:130px;
             color:#374151;padding-top:1px}
.field-val{font-size:.82rem;color:#111;flex:1}
.confidence-bar{display:inline-flex;align-items:center;gap:6px;margin-left:6px}
.confidence-bar-bg{width:80px;height:7px;background:#f3f4f6;border-radius:4px;display:inline-block}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid #d1d5db;
         border-top-color:#3b82f6;border-radius:50%;animation:spin .6s linear infinite;
         vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}
/* tooltip via title — native, styled custom below */
[title]{cursor:help}
button[title]{cursor:pointer}
a[title]{cursor:pointer}
/* server list */
.srv-card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px;
          margin-bottom:8px;display:flex;justify-content:space-between;align-items:center}
.srv-card:hover{border-color:#d1d5db;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.layer-group{margin-bottom:6px}
.layer-hdr{display:flex;justify-content:space-between;align-items:center;
           padding:6px 10px;background:#f9fafb;border-radius:5px;cursor:pointer}
.layer-hdr:hover{background:#f3f4f6}
</style>
"""

_JS = """
<script>
/* ── Multi-status filter ─────────────────────────────── */
function toggleStatus(status) {
  const p = new URLSearchParams(window.location.search);
  const raw = p.get('fstatus') ?? 'open,investigating';
  let list = raw.split(',').map(s=>s.trim()).filter(Boolean);
  const i = list.indexOf(status);
  if (i >= 0) list.splice(i, 1); else list.push(status);
  p.set('fstatus', list.join(','));
  p.set('page', '1');
  window.location.search = p.toString();
}

/* ── Column filters ──────────────────────────────────── */
function applyFilters() {
  const p = new URLSearchParams(window.location.search);
  ['fserver','fsource','fcomponent'].forEach(k => {
    const el = document.getElementById('f_'+k);
    if (!el) return;
    if (el.value) p.set(k, el.value); else p.delete(k);
  });
  p.set('page','1');
  window.location.search = p.toString();
}

/* ── 3-state sort ────────────────────────────────────── */
function applySort(col) {
  const p = new URLSearchParams(window.location.search);
  const cur = p.get('sort');
  const dir = p.get('dir') || 'desc';
  if (cur !== col) {
    p.set('sort', col);
    p.set('dir', col === 'id' ? 'asc' : 'desc');
  } else if (dir === 'desc') {
    p.set('dir', 'asc');
  } else {
    p.delete('sort'); p.delete('dir');  // 3rd state: back to default
  }
  p.set('page','1');
  window.location.search = p.toString();
}

/* ── Per-page ────────────────────────────────────────── */
function setPerPage(n) {
  const p = new URLSearchParams(window.location.search);
  p.set('per_page', n); p.set('page','1');
  window.location.search = p.toString();
}

/* ── Inline forms ────────────────────────────────────── */
function showInlineForm(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display==='none'?'block':'none';
}

/* ── Submit with loading text ────────────────────────── */
function submitWithLoading(btn, text) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>' + text;
  btn.closest('form').submit();
}
</script>
"""


def _page(title: str, body: str) -> HTMLResponse:
    now = datetime.datetime.now().strftime("%H:%M")
    nav = (
        '<nav>'
        '<span style="font-weight:700;color:#111;font-size:.95rem">⚙ Seamlean Admin</span>'
        '<a href="/admin">Ошибки</a>'
        '<a href="/admin/servers">Серверы</a>'
        '<a href="/admin/kb">База знаний</a>'
        '<a href="/admin/graph">Граф зависимостей</a>'
        f'<span style="margin-left:auto;color:#9ca3af;font-size:.75rem">{now}</span>'
        '</nav>'
    )
    return HTMLResponse(
        f"<!doctype html><html><head><title>{title}</title>{_CSS}</head>"
        f"<body><div class='wrap'>{nav}{body}</div>{_JS}</body></html>"
    )


def _badge(status: str, colors: dict | None = None,
           labels: dict | None = None) -> str:
    c = (colors or STATUS_COLOR).get(status, "#9ca3af")
    lbl = (labels or STATUS_LABEL).get(status, status)
    return f'<span class="badge" style="background:{c}">{lbl}</span>'


def _inv_badge(status: str) -> str:
    return _badge(status, INV_STATUS_COLOR, INV_STATUS_LABEL)


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
        f'<span class="confidence-bar" title="Уверенность Claude в анализе: {pct}%">'
        f'<span class="confidence-bar-bg">'
        f'<span style="display:block;width:{pct}%;height:100%;background:{color};border-radius:4px">'
        f'</span></span>'
        f'<span style="font-size:.75rem;color:{color};font-weight:600">{pct}%</span>'
        f'</span>'
    )


def _tkt(batch_id: int) -> str:
    return f"TKT-{batch_id:06d}"


def _url_with(request: Request, **overrides) -> str:
    params = dict(request.query_params)
    params.update({k: v for k, v in overrides.items() if v is not None})
    params = {k: v for k, v in params.items() if v != ""}
    qs = urlencode(params)
    return f"/admin?{qs}" if qs else "/admin"


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def dashboard(
    request: Request,
    fstatus: str = "",
    fserver: str = "",
    fsource: str = "",
    fcomponent: str = "",
    sort: str = "",
    dir: str = "desc",
    page: int = 1,
    per_page: int = 25,
):
    conn = _admin_conn()

    # ── Stats (unfiltered) ────────────────────────────────────────────────────
    counts = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM error_batches GROUP BY status"
        ).fetchall()
    }
    total_servers = conn.execute(
        "SELECT COUNT(DISTINCT server_token) FROM error_batches"
    ).fetchone()[0]

    # Active statuses from URL (default: open + investigating)
    raw_fstatus = fstatus if fstatus else DEFAULT_STATUSES
    active_statuses = [s.strip() for s in raw_fstatus.split(",") if s.strip()]

    # ── Filter options ────────────────────────────────────────────────────────
    all_servers = [r[0] for r in conn.execute(
        "SELECT DISTINCT server_token FROM error_batches ORDER BY server_token"
    ).fetchall()]
    all_sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM error_batches ORDER BY source"
    ).fetchall()]
    all_components = [r[0] for r in conn.execute(
        "SELECT DISTINCT component FROM error_batches WHERE component IS NOT NULL ORDER BY component"
    ).fetchall()]

    # ── Build filtered query ──────────────────────────────────────────────────
    where = []
    qp = []
    if active_statuses:
        ph = ",".join("?" * len(active_statuses))
        where.append(f"status IN ({ph})")
        qp.extend(active_statuses)
    if fserver:
        where.append("server_token=?"); qp.append(fserver)
    if fsource:
        where.append("source=?"); qp.append(fsource)
    if fcomponent:
        where.append("component=?"); qp.append(fcomponent)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Sort
    SORT_MAP = {"count": "count", "last_seen": "last_seen", "id": "id"}
    sort_col = SORT_MAP.get(sort, "")
    sort_dir = "DESC" if dir.lower() != "asc" else "ASC"
    if sort_col:
        order_sql = f"ORDER BY {sort_col} {sort_dir}"
    else:
        order_sql = "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'investigating' THEN 1 ELSE 2 END, last_seen DESC"

    # Count for pagination
    total_count = conn.execute(
        f"SELECT COUNT(*) FROM error_batches {where_sql}", qp
    ).fetchone()[0]
    per_page = per_page if per_page in (25, 50, 100) else 25
    page = max(1, page)
    offset = (page - 1) * per_page
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    batches = conn.execute(
        f"SELECT id,batch_hash,server_token,source,component,pattern,severity,"
        f"first_seen,last_seen,count,status FROM error_batches "
        f"{where_sql} {order_sql} LIMIT ? OFFSET ?",
        qp + [per_page, offset]
    ).fetchall()
    conn.close()

    # ── Stats toggles ─────────────────────────────────────────────────────────
    def _stat_toggle(key: str, label: str):
        n = counts.get(key, 0)
        is_active = key in active_statuses
        cls = "stat-toggle active" if is_active else "stat-toggle inactive"
        check = '<span class="stat-check">✓</span>' if is_active else '<span class="stat-check" style="opacity:0">✓</span>'
        color = STATUS_COLOR.get(key, "#6b7280")
        tip = f"Нажмите чтобы {'скрыть' if is_active else 'показать'} тикеты со статусом «{label}»"
        return (
            f'<div class="{cls}" onclick="toggleStatus(\'{key}\')" title="{tip}">'
            f'<div class="stat-n" style="color:{color}">{n}{check}</div>'
            f'<div class="stat-l">{label}</div></div>'
        )

    stats_html = (
        f'<div class="stats-bar">'
        + _stat_toggle("open", "Открытых")
        + _stat_toggle("investigating", "В работе")
        + _stat_toggle("resolved", "Решено")
        + _stat_toggle("wontfix", "Не исправлять")
        + f'<a href="/admin/servers" class="stat-servers" title="Перейти к списку серверов">'
          f'<div class="stat-n">{total_servers}</div>'
          f'<div class="stat-l">Серверов</div></a>'
        + f'</div>'
    )

    if not batches:
        body = (
            f'<h1>Ошибки системы</h1>'
            f'<p class="sub">Автоматически собраны с серверов и агентов</p>'
            + stats_html +
            '<div class="card"><div class="empty">Нет тикетов по выбранным фильтрам</div></div>'
        )
        return _page("Admin — Ошибки", body)

    # ── Sort header helper ────────────────────────────────────────────────────
    def _sort_th(label: str, col: str, tip: str, align: str = "left") -> str:
        active = sort == col
        cls = f"sortable sort-{'desc' if dir=='desc' else 'asc'}" if active else "sortable"
        return (f'<th class="{cls}" style="text-align:{align}" '
                f'onclick="applySort(\'{col}\')" title="{tip}">{label}</th>')

    # ── Filter dropdown helper ────────────────────────────────────────────────
    def _filter_sel(fid: str, cur: str, opts: list, lbl_fn=None) -> str:
        o = '<option value="">Все</option>'
        for v in opts:
            sel = " selected" if v == cur else ""
            lbl = lbl_fn(v) if lbl_fn else v
            o += f'<option value="{v}"{sel}>{lbl}</option>'
        return f'<select id="f_{fid}" onchange="applyFilters()" title="Фильтр по {fid}">{o}</select>'

    # ── Table rows ────────────────────────────────────────────────────────────
    rows = ""
    for idx, b in enumerate(batches, offset + 1):
        srv = _server_name(b["server_token"])
        src = SOURCE_LABEL.get(b["source"], b["source"])
        src_tip = SOURCE_TOOLTIP.get(b["source"], b["source"])
        sev = b.get("severity") or "error"
        show_autorun = b["status"] in ("open", "investigating")
        rows += (
            f'<tr>'
            f'<td><span class="row-num">#{idx}</span></td>'
            f'<td>{_badge(b["status"])}'
            f'<span class="tkt-id">{_tkt(b["id"])}</span></td>'
            f'<td><span style="font-size:.8rem">{srv}</span></td>'
            f'<td title="{src_tip}"><span style="font-size:.75rem;color:#6b7280">{src}</span></td>'
            f'<td><span style="font-size:.75rem;color:#6b7280">{b["component"] or "—"}</span></td>'
            f'<td><span class="pattern">'
            f'{b["pattern"][:110]}{"…" if len(b["pattern"])>110 else ""}'
            f'</span></td>'
            f'<td style="text-align:right;font-weight:600">{b["count"]}</td>'
            f'<td class="ts">{_ago(b["last_seen"])}</td>'
            f'<td style="white-space:nowrap">'
            f'<a href="/admin/batch/{b["id"]}" class="btn btn-blue btn-sm"'
            f' title="Посмотреть тикет {_tkt(b["id"])}">→</a>'
            + (
                f'<form method="post" action="/admin/batch/{b["id"]}/autorun"'
                f' style="display:inline;margin-left:4px">'
                f'<button type="submit" class="btn btn-purple btn-sm"'
                f' title="Запустить автоматически: Claude проанализирует и выполнит исправление без участия человека"'
                f' onclick="return confirm(\'Запустить автоматически? Claude проанализирует ошибку и выполнит исправление без вашего участия.\')">'
                f'⚡</button></form>'
                if show_autorun else ""
            )
            + f'</td></tr>'
        )

    table = (
        f'<div class="card" style="padding:0;overflow:hidden">'
        f'<table><thead>'
        f'<tr>'
        + _sort_th("#", "id", "Сортировать по порядковому номеру тикета")
        + f'<th title="Текущий статус тикета и уникальный номер">Статус</th>'
          f'<th title="On-prem сервер, с которого пришла ошибка">Сервер</th>'
          f'<th title="Источник: откуда физически получена ошибка (PostgreSQL, Docker, GitHub CI)">Источник</th>'
          f'<th title="Компонент системы: слой агента (window/visual/system) или сервиса (api/worker)">Компонент</th>'
          f'<th title="Текст или паттерн ошибки">Паттерн</th>'
        + _sort_th("Кол-во", "count",
                   "Количество раз, когда ошибка встречалась. Нажмите для сортировки: ↓ убывание → ↑ возрастание → сброс",
                   "right")
        + _sort_th("Последний раз", "last_seen",
                   "Когда ошибка встречалась последний раз. Нажмите для сортировки")
        + f'<th title="Посмотреть тикет / Запустить автоматически"></th>'
        f'</tr>'
        f'<tr class="filter-row">'
        f'<td></td><td></td>'
        f'<td>{_filter_sel("fserver", fserver, all_servers, _server_name)}</td>'
        f'<td>{_filter_sel("fsource", fsource, all_sources, lambda s: SOURCE_LABEL.get(s,s))}</td>'
        f'<td>{_filter_sel("fcomponent", fcomponent, all_components)}</td>'
        f'<td colspan="4"></td>'
        f'</tr>'
        f'</thead><tbody>{rows}</tbody></table></div>'
    )

    # ── Pagination ────────────────────────────────────────────────────────────
    def _page_link(p: int, label: str, active: bool = False) -> str:
        params = dict(request.query_params)
        params["page"] = str(p)
        params["per_page"] = str(per_page)
        cls = "page-btn active" if active else "page-btn"
        return f'<a href="/admin?{urlencode(params)}" class="{cls}">{label}</a>'

    pagination = f'<div class="pagination">'
    pagination += f'<span class="page-info">{total_count} тикетов</span>'
    pagination += (
        f'<select onchange="setPerPage(this.value)" title="Тикетов на странице">'
        + "".join(
            f'<option value="{n}" {"selected" if n==per_page else ""}>{n} на стр.</option>'
            for n in (25, 50, 100)
        )
        + f'</select>'
    )
    if total_pages > 1:
        if page > 1:
            pagination += _page_link(page - 1, "←")
        for p in range(1, total_pages + 1):
            if p <= 2 or p >= total_pages - 1 or abs(p - page) <= 1:
                pagination += _page_link(p, str(p), p == page)
            elif abs(p - page) == 2:
                pagination += '<span class="page-info">…</span>'
    pagination += "</div>"

    body = (
        f'<h1>Ошибки системы</h1>'
        f'<p class="sub">Автоматически собраны с серверов и агентов</p>'
        + stats_html + table + pagination
    )
    return _page("Admin — Ошибки", body)


# ── Batch detail ──────────────────────────────────────────────────────────────

@app.get("/admin/batch/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int):
    conn = _admin_conn()
    batch = conn.execute("SELECT * FROM error_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        return _page("Не найдено", '<div class="empty">Батч не найден</div>')
    investigations = conn.execute(
        "SELECT * FROM investigations WHERE batch_id=? ORDER BY attempt ASC", (batch_id,)
    ).fetchall()
    kb_matches = _kb_for_batch(dict(batch)["pattern"], conn)
    conn.close()

    b = dict(batch)
    srv_name = _server_name(b["server_token"])
    src_label = SOURCE_LABEL.get(b["source"], b["source"])
    tkt = _tkt(b["id"])

    active_statuses = {"pending_approval", "approved", "executed"}
    has_active_inv = any(dict(i)["status"] in active_statuses for i in investigations)
    can_investigate = (
        b["status"] not in ("resolved", "wontfix")
        and not has_active_inv
        and bool(ANTHROPIC_API_KEY)
    )
    can_autorun = b["status"] in ("open", "investigating")

    # ── Header ────────────────────────────────────────────────────────────────
    header = (
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">'
        f'<a href="/admin" style="color:#6b7280;text-decoration:none;font-size:.85rem">← Все ошибки</a>'
        f'<span style="color:#d1d5db;font-size:.75rem;font-family:monospace">{tkt}</span>'
        f'</div>'
        f'<div class="card">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div style="flex:1;min-width:0">'
        f'<div style="font-size:.75rem;color:#6b7280;margin-bottom:6px">'
        f'{srv_name} &nbsp;·&nbsp; {src_label} &nbsp;·&nbsp; {b["component"] or "unknown"}'
        f'</div>'
        f'<div class="pattern" style="font-size:.85rem;line-height:1.4">{b["pattern"]}</div>'
        f'</div>'
        f'<div style="margin-left:12px">{_badge(b["status"])}</div></div>'
        f'<div style="margin-top:10px;display:flex;gap:18px;font-size:.78rem;color:#6b7280;flex-wrap:wrap">'
        f'<span>Первый раз: {_ago(b["first_seen"])}</span>'
        f'<span>Последний: {_ago(b["last_seen"])}</span>'
        f'<span>Всего: <strong style="color:#111">{b["count"]}</strong></span>'
        f'<span title="Уровень серьёзности: critical — система не работает; error — функция сломана; warning — работает с ограничениями; info — информационное">'
        f'Severity: <strong style="color:#111">{b.get("severity","?")}</strong></span>'
        + (f'<span style="color:#ef4444;font-weight:600">🔁 Рецидив ×{b["recurrence"]}</span>'
           if b.get("recurrence") else "")
        + f'</div>'
        # Action buttons
        f'<div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">'
        + (
            f'<form method="post" action="/admin/batch/{batch_id}/investigate" id="inv-form"'
            f' style="display:inline">'
            f'<button class="btn btn-amber" id="inv-btn"'
            f' title="Расследовать с Claude: AI проанализирует ошибку и предложит план устранения"'
            f' onclick="submitWithLoading(this,\'Отправлено на изучение\');return false">'
            f'🔍 Расследовать с Claude</button></form>'
            if can_investigate else ""
        )
        + (
            f'<form method="post" action="/admin/batch/{batch_id}/autorun" style="display:inline">'
            f'<button class="btn btn-purple"'
            f' title="Запустить автоматически: Claude проанализирует (если нет плана) и сразу выполнит исправление без участия человека"'
            f' onclick="return confirm(\'Запустить автоматически без участия человека?\')">'
            f'⚡ Запустить автоматически</button></form>'
            if can_autorun else ""
        )
        + f'</div></div>'
    )

    # ── Manual form ───────────────────────────────────────────────────────────
    STATUS_OPTIONS = [
        ("open", "Открыт"), ("investigating", "В работе"),
        ("resolved", "Решено"), ("wontfix", "Не исправлять"),
    ]
    action_form = (
        f'<div class="card"><h2 style="margin-top:0">Ручное управление</h2>'
        f'<form method="post" action="/admin/batch/{batch_id}/action">'
        f'<div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">'
        f'<div style="margin:0">'
        f'<label title="Статус тикета">Статус</label>'
        f'<select name="status">'
        + "".join(
            f'<option value="{v}" {"selected" if v==b["status"] else ""}>{l}</option>'
            for v, l in STATUS_OPTIONS
        )
        + f'</select></div>'
        f'<div style="flex:1;min-width:200px">'
        f'<label>Комментарий</label>'
        f'<textarea name="comment" rows="2" placeholder="Описание причины, план..."'
        f' style="min-height:36px"></textarea></div>'
        f'<button type="submit" class="btn btn-blue btn-sm" style="height:34px">Сохранить</button>'
        f'</div></form></div>'
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

    # ── Investigations ────────────────────────────────────────────────────────
    inv_html = ""
    if investigations:
        inv_html = '<h2>Расследования</h2>'
        for inv in reversed(list(investigations)):
            d = dict(inv)
            fp = json.loads(d["fix_plan"] or "{}") if d["fix_plan"] else {}
            inv_status = d["status"] or "unknown"
            inv_class = inv_status if inv_status in (
                "approved", "verified", "executed", "failed", "error", "rejected"
            ) else ""
            inv_id = d["id"]

            # Actions
            actions_html = ""
            if fp.get("actions"):
                actions_html = (
                    '<div class="field-row">'
                    '<span class="field-label">Действия:</span>'
                    '<span class="field-val">'
                )
                for a in fp["actions"]:
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

            # Buttons: show on pending_approval, approved, AND rejected
            btns = ""
            if inv_status in ("pending_approval", "rejected"):
                btns = (
                    f'<div style="margin-top:14px;display:flex;gap:6px;flex-wrap:wrap;align-items:flex-start">'
                    # Одобрить план
                    f'<form method="post" action="/admin/investigation/{inv_id}/approve">'
                    f'<button type="submit" class="btn btn-green btn-sm"'
                    f' title="Одобрить план: подтвердить что план корректный. После одобрения можно запустить автоматически">'
                    f'✅ Одобрить план</button></form>'
                    # Изучить ещё раз
                    f'<button class="btn btn-amber btn-sm"'
                    f' title="Изучить ещё раз: отклонить текущий план и запустить новый анализ Claude с вашими пожеланиями"'
                    f' onclick="showInlineForm(\'reinv-{inv_id}\')">'
                    f'🔄 Изучить ещё раз</button>'
                    # Отклонить
                    f'<button class="btn btn-gray btn-sm"'
                    f' title="Отклонить план: пометить как отклонённый без повторного анализа"'
                    f' onclick="showInlineForm(\'reject-{inv_id}\')">'
                    f'✗ Отклонить</button>'
                    f'</div>'
                    # Reinvestigate form
                    f'<div id="reinv-{inv_id}" class="inline-form">'
                    f'<form method="post" action="/admin/investigation/{inv_id}/reinvestigate">'
                    f'<label>Пожелания для нового анализа (необязательно):</label>'
                    f'<textarea name="wishes" rows="2"'
                    f' placeholder="Например: сосредоточься на сетевых проблемах..."></textarea>'
                    f'<div style="margin-top:6px">'
                    f'<button type="submit" class="btn btn-amber btn-sm"'
                    f' onclick="submitWithLoading(this,\'Отправлено на изучение\')">'
                    f'⏳ Отправить на изучение</button>'
                    f'<button type="button" class="btn btn-gray btn-sm"'
                    f' style="margin-left:6px"'
                    f' onclick="showInlineForm(\'reinv-{inv_id}\')">Отмена</button>'
                    f'</div></form></div>'
                    # Reject form
                    f'<div id="reject-{inv_id}" class="inline-form">'
                    f'<form method="post" action="/admin/investigation/{inv_id}/reject">'
                    f'<label>Причина отклонения (необязательно):</label>'
                    f'<textarea name="reason" rows="2"'
                    f' placeholder="Например: план слишком рискованный..."></textarea>'
                    f'<div style="margin-top:6px">'
                    f'<button type="submit" class="btn btn-gray btn-sm">Подтвердить отклонение</button>'
                    f'<button type="button" class="btn btn-gray btn-sm"'
                    f' style="margin-left:6px"'
                    f' onclick="showInlineForm(\'reject-{inv_id}\')">Отмена</button>'
                    f'</div></form></div>'
                )
            elif inv_status == "approved":
                btns = (
                    f'<div style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">'
                    f'<form method="post" action="/admin/batch/{batch_id}/autofix">'
                    f'<button type="submit" class="btn btn-purple btn-sm"'
                    f' title="Выполнить одобренный план автоматически"'
                    f' onclick="return confirm(\'Выполнить план автоматически?\')">'
                    f'⚡ Запустить автоматически</button></form>'
                    f'<form method="post" action="/admin/investigation/{inv_id}/reject">'
                    f'<input type="hidden" name="reason" value="Отменено после одобрения">'
                    f'<button type="submit" class="btn btn-gray btn-sm">✗ Отменить</button>'
                    f'</form></div>'
                )

            inv_html += (
                f'<div class="card inv-card {inv_class}">'
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<span style="font-weight:600;font-size:.85rem">Попытка #{d["attempt"]}</span>'
                f'{_inv_badge(inv_status)}</div>'
                + (f'<div class="field-row"><span class="field-label">Причина:</span>'
                   f'<span class="field-val">{d["root_cause"]}'
                   + _confidence_bar(d["confidence_pct"])
                   + '</span></div>' if d.get("root_cause") else "")
                + (f'<div class="field-row"><span class="field-label">Рекомендация:</span>'
                   f'<span class="field-val" style="font-weight:600">'
                   f'{fp.get("recommended_level","?")} — '
                   + fp.get(fp.get("recommended_level", "L1"), "—")
                   + '</span></div>' if fp else "")
                + (f'<div class="field-row"><span class="field-label">L1:</span>'
                   f'<span class="field-val">{fp["L1"]}</span></div>' if fp.get("L1") else "")
                + (f'<div class="field-row"><span class="field-label">L2:</span>'
                   f'<span class="field-val">{fp["L2"]}</span></div>' if fp.get("L2") else "")
                + (f'<div class="field-row"><span class="field-label">L3:</span>'
                   f'<span class="field-val">{fp["L3"]}</span></div>' if fp.get("L3") else "")
                + actions_html
                + (f'<div class="field-row"><span class="field-label">Проверка:</span>'
                   f'<span class="field-val">{d["verification_criteria"]}</span></div>'
                   if d.get("verification_criteria") else "")
                + (f'<div class="field-row"><span class="field-label">Откат:</span>'
                   f'<span class="field-val">{d["rollback_plan"]}</span></div>'
                   if d.get("rollback_plan") else "")
                + (f'<div class="field-row"><span class="field-label">Влияние:</span>'
                   f'<span class="field-val">{d["impact"]}</span></div>'
                   if d.get("impact") else "")
                + (f'<div class="field-row"><span class="field-label">Комментарий:</span>'
                   f'<span class="field-val" style="color:#6b7280">{d["admin_comment"]}</span></div>'
                   if d.get("admin_comment") else "")
                + f'<div style="margin-top:8px;font-size:.72rem;color:#9ca3af">'
                + f'Создано: {_ago(d["created_at"])}'
                + (f' · Одобрено: {_ago(d["approved_at"])}' if d.get("approved_at") else "")
                + (f' · Закрыто: {_ago(d.get("resolved_at"))}' if d.get("resolved_at") else "")
                + '</div>' + btns + '</div>'
            )

    body = header + action_form + kb_html + inv_html
    return _page(f"Тикет {tkt}", body)


# ── Action routes ─────────────────────────────────────────────────────────────

@app.post("/admin/batch/{batch_id}/action")
async def batch_action(batch_id: int, status: str = Form(...), comment: str = Form("")):
    conn = sqlite3.connect(ADMIN_DB)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute("UPDATE error_batches SET status=? WHERE id=?", (status, batch_id))
    if comment.strip():
        ex = conn.execute(
            "SELECT id FROM investigations WHERE batch_id=? ORDER BY attempt DESC LIMIT 1",
            (batch_id,)
        ).fetchone()
        if ex:
            conn.execute("UPDATE investigations SET admin_comment=? WHERE id=?",
                         (comment.strip(), ex[0]))
        else:
            ma = conn.execute(
                "SELECT COALESCE(MAX(attempt),0) FROM investigations WHERE batch_id=?",
                (batch_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO investigations (batch_id,attempt,status,admin_comment,created_at)"
                " VALUES (?,?,'pending_approval',?,?)",
                (batch_id, ma + 1, comment.strip(), now)
            )
    conn.commit(); conn.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.post("/admin/batch/{batch_id}/investigate")
async def batch_investigate(batch_id: int):
    conn = _admin_conn()
    batch = conn.execute("SELECT * FROM error_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)
    b = dict(batch)
    kb = _kb_for_batch(b["pattern"], conn)
    ma = conn.execute(
        "SELECT COALESCE(MAX(attempt),0) FROM investigations WHERE batch_id=?", (batch_id,)
    ).fetchone()[0]
    conn.close()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = await _call_claude_investigation(b, list(kb))
    wc = sqlite3.connect(ADMIN_DB)
    if "error" in result:
        wc.execute(
            "INSERT INTO investigations (batch_id,attempt,root_cause,status,created_at) VALUES(?,?,?,?,?)",
            (batch_id, ma+1, f"Ошибка анализа: {result.get('error','')} — {result.get('raw','')}",
             "error", now)
        )
    else:
        fp = result.get("fix_plan", {})
        wc.execute(
            "INSERT INTO investigations"
            " (batch_id,attempt,root_cause,confidence_pct,fix_plan,"
            "  verification_criteria,rollback_plan,impact,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (batch_id, ma+1, result.get("root_cause",""), result.get("confidence_pct"),
             json.dumps(fp, ensure_ascii=False), result.get("verification_criteria",""),
             result.get("rollback_plan",""), result.get("impact",""), "pending_approval", now)
        )
        wc.execute("UPDATE error_batches SET status='investigating' WHERE id=?", (batch_id,))
    wc.commit(); wc.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.post("/admin/batch/{batch_id}/autorun")
async def batch_autorun(batch_id: int):
    """Smart auto-run: investigate if no plan, then execute immediately."""
    conn = _admin_conn()
    batch = conn.execute("SELECT * FROM error_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)
    b = dict(batch)
    # Check existing usable investigation
    inv = conn.execute(
        "SELECT * FROM investigations WHERE batch_id=?"
        " AND status IN ('approved','pending_approval')"
        " ORDER BY attempt DESC LIMIT 1", (batch_id,)
    ).fetchone()
    kb = _kb_for_batch(b["pattern"], conn)
    ma = conn.execute(
        "SELECT COALESCE(MAX(attempt),0) FROM investigations WHERE batch_id=?", (batch_id,)
    ).fetchone()[0]
    conn.close()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if inv:
        d = dict(inv)
        fp = json.loads(d["fix_plan"] or "{}") if d["fix_plan"] else {}
        return await _execute_fix_plan(batch_id, d["id"], fp)

    # No plan → investigate then execute
    result = await _call_claude_investigation(b, list(kb))
    wc = sqlite3.connect(ADMIN_DB)
    if "error" in result:
        wc.execute(
            "INSERT INTO investigations (batch_id,attempt,root_cause,status,created_at) VALUES(?,?,?,?,?)",
            (batch_id, ma+1, f"Ошибка: {result.get('error','')}",  "error", now)
        )
        wc.commit(); wc.close()
        return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)

    fp = result.get("fix_plan", {})
    inv_id = wc.execute(
        "INSERT INTO investigations"
        " (batch_id,attempt,root_cause,confidence_pct,fix_plan,"
        "  verification_criteria,rollback_plan,impact,status,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (batch_id, ma+1, result.get("root_cause",""), result.get("confidence_pct"),
         json.dumps(fp, ensure_ascii=False), result.get("verification_criteria",""),
         result.get("rollback_plan",""), result.get("impact",""), "approved", now)
    ).lastrowid
    wc.execute("UPDATE error_batches SET status='investigating' WHERE id=?", (batch_id,))
    wc.commit(); wc.close()
    return await _execute_fix_plan(batch_id, inv_id, fp)


@app.post("/admin/batch/{batch_id}/autofix")
async def batch_autofix(batch_id: int):
    conn = _admin_conn()
    inv = conn.execute(
        "SELECT * FROM investigations WHERE batch_id=?"
        " AND status IN ('approved','pending_approval')"
        " ORDER BY attempt DESC LIMIT 1", (batch_id,)
    ).fetchone()
    conn.close()
    if not inv:
        return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)
    d = dict(inv)
    fp = json.loads(d["fix_plan"] or "{}") if d["fix_plan"] else {}
    return await _execute_fix_plan(batch_id, d["id"], fp)


@app.post("/admin/investigation/{inv_id}/approve")
async def investigation_approve(inv_id: int):
    conn = sqlite3.connect(ADMIN_DB)
    inv = conn.execute("SELECT batch_id FROM investigations WHERE id=?", (inv_id,)).fetchone()
    if not inv:
        conn.close()
        return RedirectResponse("/admin", status_code=303)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute("UPDATE investigations SET status='approved',approved_at=? WHERE id=?",
                 (now, inv_id))
    conn.commit(); conn.close()
    return RedirectResponse(f"/admin/batch/{inv[0]}", status_code=303)


@app.post("/admin/investigation/{inv_id}/reject")
async def investigation_reject(inv_id: int, reason: str = Form("")):
    conn = sqlite3.connect(ADMIN_DB)
    inv = conn.execute("SELECT batch_id FROM investigations WHERE id=?", (inv_id,)).fetchone()
    if not inv:
        conn.close()
        return RedirectResponse("/admin", status_code=303)
    batch_id = inv[0]
    r = reason.strip() or "Отклонено администратором"
    conn.execute("UPDATE investigations SET status='rejected',admin_comment=? WHERE id=?",
                 (r, inv_id))
    conn.execute("UPDATE error_batches SET status='open' WHERE id=?", (batch_id,))
    conn.commit(); conn.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


@app.post("/admin/investigation/{inv_id}/reinvestigate")
async def investigation_reinvestigate(inv_id: int, wishes: str = Form("")):
    conn = _admin_conn()
    inv = conn.execute("SELECT batch_id FROM investigations WHERE id=?", (inv_id,)).fetchone()
    if not inv:
        conn.close()
        return RedirectResponse("/admin", status_code=303)
    batch_id = inv[0]
    conn.execute(
        "UPDATE investigations SET status='rejected',"
        " admin_comment='Требует повторного изучения' WHERE id=?", (inv_id,)
    )
    conn.execute("UPDATE error_batches SET status='open' WHERE id=?", (batch_id,))
    conn.commit()
    batch = conn.execute("SELECT * FROM error_batches WHERE id=?", (batch_id,)).fetchone()
    kb = _kb_for_batch(dict(batch)["pattern"], conn)
    ma = conn.execute(
        "SELECT COALESCE(MAX(attempt),0) FROM investigations WHERE batch_id=?", (batch_id,)
    ).fetchone()[0]
    conn.close()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = await _call_claude_investigation(dict(batch), list(kb), wishes=wishes)
    wc = sqlite3.connect(ADMIN_DB)
    if "error" in result:
        wc.execute(
            "INSERT INTO investigations (batch_id,attempt,root_cause,status,created_at) VALUES(?,?,?,?,?)",
            (batch_id, ma+1, f"Ошибка: {result.get('error','')}", "error", now)
        )
    else:
        fp = result.get("fix_plan", {})
        wc.execute(
            "INSERT INTO investigations"
            " (batch_id,attempt,root_cause,confidence_pct,fix_plan,"
            "  verification_criteria,rollback_plan,impact,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (batch_id, ma+1, result.get("root_cause",""), result.get("confidence_pct"),
             json.dumps(fp, ensure_ascii=False), result.get("verification_criteria",""),
             result.get("rollback_plan",""), result.get("impact",""), "pending_approval", now)
        )
        wc.execute("UPDATE error_batches SET status='investigating' WHERE id=?", (batch_id,))
    wc.commit(); wc.close()
    return RedirectResponse(f"/admin/batch/{batch_id}", status_code=303)


# ── Servers ───────────────────────────────────────────────────────────────────

@app.get("/admin/servers", response_class=HTMLResponse)
def servers_list():
    conn = _admin_conn()
    server_stats = conn.execute("""
        SELECT server_token,
               COUNT(*) AS total,
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_cnt,
               SUM(CASE WHEN status='investigating' THEN 1 ELSE 0 END) AS inv_cnt,
               MAX(last_seen) AS last_err
        FROM error_batches GROUP BY server_token
    """).fetchall()
    conn.close()

    srv_infos = {s.get("server_token") or s.get("api_key", ""): s
                 for s in _all_servers_info()}

    if not server_stats:
        body = ('<h1>Серверы</h1>'
                '<div class="card"><div class="empty">Нет данных о серверах</div></div>')
        return _page("Admin — Серверы", body)

    cards = ""
    for row in server_stats:
        d = dict(row)
        tok = d["server_token"]
        name = _server_name(tok)
        info = srv_infos.get(tok, {})
        status = info.get("status", "unknown")
        last_seen = info.get("last_seen", "")
        status_dot = (
            '<span style="color:#22c55e">●</span>' if status == "active" else
            '<span style="color:#ef4444">●</span>'
        )
        cards += (
            f'<div class="srv-card">'
            f'<div>'
            f'<div style="font-weight:600;font-size:.95rem">{status_dot} {name}</div>'
            f'<div style="font-size:.75rem;color:#6b7280;margin-top:2px">'
            f'{info.get("tunnel_url","") or tok[:20]+"…"}'
            f'</div>'
            f'<div style="font-size:.75rem;color:#9ca3af">Последняя ошибка: {_ago(d["last_err"])}'
            + (f' · Last seen: {_ago(last_seen)}' if last_seen else "")
            + f'</div></div>'
            f'<div style="display:flex;gap:12px;align-items:center">'
            f'<div style="text-align:center">'
            f'<div style="font-size:1.2rem;font-weight:700;color:#ef4444">{d["open_cnt"]}</div>'
            f'<div style="font-size:.7rem;color:#6b7280">Открытых</div></div>'
            f'<div style="text-align:center">'
            f'<div style="font-size:1.2rem;font-weight:700;color:#f59e0b">{d["inv_cnt"]}</div>'
            f'<div style="font-size:.7rem;color:#6b7280">В работе</div></div>'
            f'<div style="text-align:center">'
            f'<div style="font-size:1.2rem;font-weight:700">{d["total"]}</div>'
            f'<div style="font-size:.7rem;color:#6b7280">Всего</div></div>'
            f'<a href="/admin/servers/{tok}" class="btn btn-blue btn-sm">→</a>'
            f'</div></div>'
        )

    body = (
        f'<h1>Серверы</h1>'
        f'<p class="sub">Зарегистрированные on-prem серверы с ошибками</p>'
        + cards
    )
    return _page("Admin — Серверы", body)


@app.get("/admin/servers/{server_token}", response_class=HTMLResponse)
def server_detail(server_token: str):
    conn = _admin_conn()
    # Errors grouped by component for this server
    component_stats = conn.execute("""
        SELECT component, COUNT(*) AS cnt,
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_cnt,
               MAX(last_seen) AS last_err
        FROM error_batches WHERE server_token=?
        GROUP BY component ORDER BY open_cnt DESC, cnt DESC
    """, (server_token,)).fetchall()
    # All error batches for this server
    batches = conn.execute("""
        SELECT id, component, pattern, status, count, last_seen
        FROM error_batches WHERE server_token=?
        ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'investigating' THEN 1 ELSE 2 END,
                 last_seen DESC
    """, (server_token,)).fetchall()
    conn.close()

    srv_name = _server_name(server_token)
    info = _server_info(server_token)

    # Group batches by component
    by_comp: dict = defaultdict(list)
    for b in batches:
        by_comp[b["component"] or "unknown"].append(dict(b))

    # Build accordion sections per component
    sections = ""
    for comp_row in component_stats:
        comp = comp_row["component"] or "unknown"
        cnt = comp_row["cnt"]
        open_cnt = comp_row["open_cnt"]
        is_agent = comp in _AGENT_LAYERS
        comp_type = "Агент" if is_agent else "Сервер"
        color = "#ef4444" if open_cnt > 0 else "#22c55e"
        uid = f"comp-{comp or 'unknown'}"
        rows = ""
        for b in by_comp[comp]:
            rows += (
                f'<tr>'
                f'<td><span class="row-num" style="font-size:.7rem">#{b["id"]}</span>'
                f'<span class="tkt-id">{_tkt(b["id"])}</span></td>'
                f'<td>{_badge(b["status"])}</td>'
                f'<td><span class="pattern">'
                f'{b["pattern"][:100]}{"…" if len(b["pattern"])>100 else ""}'
                f'</span></td>'
                f'<td style="text-align:right;font-weight:600">{b["count"]}</td>'
                f'<td class="ts">{_ago(b["last_seen"])}</td>'
                f'<td><a href="/admin/batch/{b["id"]}" class="btn btn-blue btn-sm">→</a></td>'
                f'</tr>'
            )
        sections += (
            f'<div class="layer-group">'
            f'<div class="layer-hdr" onclick="toggleLayer(\'{uid}\')">'
            f'<span><strong>{comp}</strong>'
            f' <span style="font-size:.72rem;color:#9ca3af;margin-left:6px">{comp_type}</span></span>'
            f'<span style="display:flex;align-items:center;gap:8px">'
            f'<span style="font-size:.8rem;color:{color};font-weight:600">'
            f'{open_cnt} открытых / {cnt} всего</span>'
            f'<span style="color:#9ca3af;font-size:.8rem" id="{uid}-arrow">▼</span>'
            f'</span></div>'
            f'<div id="{uid}" style="display:none;overflow:hidden">'
            f'<table style="margin-top:0">'
            f'<thead><tr>'
            f'<th>#</th><th>Статус</th><th>Паттерн</th>'
            f'<th style="text-align:right">Кол-во</th><th>Последний раз</th><th></th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></div>'
        )

    body = (
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">'
        f'<a href="/admin/servers" style="color:#6b7280;text-decoration:none;font-size:.85rem">'
        f'← Все серверы</a></div>'
        f'<h1>{srv_name}</h1>'
        f'<p class="sub">'
        + (info.get("tunnel_url", "") or server_token[:30])
        + (f' · Статус: {info.get("status","?")}' if info else "")
        + f'</p>'
        f'<div class="card">{sections}</div>'
    )

    layer_js = """
<script>
function toggleLayer(id) {
  const el = document.getElementById(id);
  const arrow = document.getElementById(id+'-arrow');
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  if (arrow) arrow.textContent = open ? '▼' : '▲';
}
// Open first section by default
document.addEventListener('DOMContentLoaded', () => {
  const first = document.querySelector('.layer-group .layer-hdr');
  if (first) first.click();
});
</script>"""
    return _page(f"Сервер {srv_name}", body + layer_js)


# ── Knowledge base ────────────────────────────────────────────────────────────

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


# ── Dependency graph ──────────────────────────────────────────────────────────

@app.get("/admin/graph", response_class=HTMLResponse)
def dependency_graph():
    conn = _admin_conn()
    nodes = conn.execute("SELECT * FROM dep_nodes").fetchall()
    edges = conn.execute("""
        SELECT e.relation, e.confidence_pct, e.evidence_count,
               n1.name AS from_name, n2.name AS to_name
        FROM dep_edges e
        JOIN dep_nodes n1 ON n1.id = e.from_node
        JOIN dep_nodes n2 ON n2.id = e.to_node
        ORDER BY e.confidence_pct DESC
    """).fetchall()
    conn.close()
    if not edges:
        body = ('<h1>Граф зависимостей</h1>'
                '<div class="card"><div class="empty">'
                'Граф строится автоматически в процессе расследования</div></div>')
        return _page("Admin — Граф", body)
    rows = ""
    for e in edges:
        d = dict(e)
        c = d["confidence_pct"]
        col = "#22c55e" if c >= 70 else "#f59e0b" if c >= 40 else "#9ca3af"
        rows += (
            f'<tr><td style="font-weight:500">{d["from_name"]}</td>'
            f'<td style="font-size:.75rem;color:#6b7280">{d["relation"]}</td>'
            f'<td style="font-weight:500">{d["to_name"]}</td>'
            f'<td><div style="display:flex;align-items:center;gap:6px">'
            f'<div style="width:60px;height:6px;background:#f3f4f6;border-radius:3px">'
            f'<div style="width:{c}%;height:100%;background:{col};border-radius:3px"></div></div>'
            f'<span style="font-size:.75rem;color:{col}">{c}%</span></div></td>'
            f'<td class="ts">{d["evidence_count"]} подтв.</td></tr>'
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


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/admin/health")
def health():
    try:
        conn = _admin_conn()
        total = conn.execute("SELECT COUNT(*) FROM error_batches").fetchone()[0]
        conn.close()
        return {"status": "ok", "error_batches": total, "claude_ready": bool(ANTHROPIC_API_KEY)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})

"""cloud-admin — separate FastAPI app for the admin panel.

Reads from /app/data/admin.db (written by main.py ingest).
Also reads /app/data/users.db to resolve server names.
Served at /admin/* via nginx proxy.
Auth: URL-only for now (single admin).
"""

import datetime
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

ADMIN_DB  = Path("/app/data/admin.db")
USERS_DB  = Path("/app/data/users.db")

STATUS_COLOR = {
    "open":          "#ef4444",
    "investigating": "#f59e0b",
    "resolved":      "#22c55e",
    "wontfix":       "#9ca3af",
}
SOURCE_LABEL = {
    "postgresql": "PostgreSQL",
    "docker":     "Docker",
    "github_ci":  "GitHub CI",
}


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
.btn{display:inline-block;padding:4px 12px;border-radius:6px;border:none;
     cursor:pointer;font-size:.8rem;font-weight:500;text-decoration:none}
.btn-blue{background:#3b82f6;color:#fff}
.btn-blue:hover{background:#2563eb}
.btn-green{background:#22c55e;color:#fff}
.btn-green:hover{background:#16a34a}
.btn-red{background:#ef4444;color:#fff}
.btn-red:hover{background:#dc2626}
.btn-gray{background:#e5e7eb;color:#374151}
.btn-gray:hover{background:#d1d5db}
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


def _badge(status: str) -> str:
    color = STATUS_COLOR.get(status, "#9ca3af")
    return f'<span class="badge" style="background:{color}">{status}</span>'


def _ago(ts_str: str | None) -> str:
    if not ts_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:   return f"{s}с назад"
        if s < 3600: return f"{s//60}м назад"
        if s < 86400: return f"{s//3600}ч назад"
        return f"{s//86400}д назад"
    except Exception:
        return ts_str[:16]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def dashboard():
    conn = _admin_conn()

    # Stats
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

    # Error batches grouped by server
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
            f'<td><a href="/admin/batch/{b["id"]}" class="btn btn-blue">→</a></td>'
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
        "SELECT * FROM batch_detail WHERE id=?", (batch_id,)
    ).fetchone()

    # Fallback: batch_detail view might not exist yet, query directly
    if batch is None:
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

    # Similar batches from KB
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

    # Header
    header = (
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">'
        f'<a href="/admin" style="color:#6b7280;text-decoration:none">← Все ошибки</a>'
        f'</div>'
        f'<div class="card">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start">'
        f'<div>'
        f'<div style="font-size:.75rem;color:#6b7280;margin-bottom:4px">'
        f'{srv_name} · {src_label} · {b["component"] or "unknown"}'
        f'</div>'
        f'<div class="pattern" style="font-size:.85rem">{b["pattern"]}</div>'
        f'</div>'
        f'{_badge(b["status"])}'
        f'</div>'
        f'<div style="margin-top:10px;display:flex;gap:16px;font-size:.78rem;color:#6b7280">'
        f'<span>Первый раз: {_ago(b["first_seen"])}</span>'
        f'<span>Последний: {_ago(b["last_seen"])}</span>'
        f'<span>Всего раз: <strong>{b["count"]}</strong></span>'
        f'</div>'
        f'</div>'
    )

    # Action form
    action_form = (
        f'<div class="card">'
        f'<h2 style="margin-top:0">Действие</h2>'
        f'<form method="post" action="/admin/batch/{batch_id}/action">'
        f'<div class="form-row">'
        f'<label>Статус</label>'
        f'<select name="status">'
        + "".join(
            f'<option value="{s}" {"selected" if s==b["status"] else ""}>{s}</option>'
            for s in ("open", "investigating", "resolved", "wontfix")
        ) +
        f'</select></div>'
        f'<div class="form-row">'
        f'<label>Комментарий администратора</label>'
        f'<textarea name="comment" rows="3" placeholder="Описание причины, план действий...">'
        f'</textarea></div>'
        f'<button type="submit" class="btn btn-blue">Сохранить</button>'
        f'</form>'
        f'</div>'
    )

    # Investigations history
    inv_html = ""
    if investigations:
        inv_html = '<h2>История расследований</h2>'
        for inv in investigations:
            d = dict(inv)
            evidence  = json.loads(d["evidence"]  or "[]") if d["evidence"]  else []
            fix_plan  = json.loads(d["fix_plan"]  or "{}") if d["fix_plan"]  else {}
            inv_html += (
                f'<div class="card">'
                f'<div style="display:flex;justify-content:space-between">'
                f'<strong>Попытка #{d["attempt"]}</strong>'
                f'{_badge(d["status"])}'
                f'</div>'
                + (f'<div style="margin-top:10px"><strong>Причина:</strong> {d["root_cause"]}'
                   f' <span style="color:#6b7280">({d["confidence_pct"]}%)</span></div>'
                   if d["root_cause"] else "")
                + (f'<div style="margin-top:8px"><strong>План устранения:</strong>'
                   f'<pre>{json.dumps(fix_plan, ensure_ascii=False, indent=2)}</pre></div>'
                   if fix_plan else "")
                + (f'<div style="margin-top:8px"><strong>Критерий проверки:</strong> '
                   f'{d["verification_criteria"]}</div>' if d["verification_criteria"] else "")
                + (f'<div style="margin-top:6px;font-size:.78rem;color:#6b7280">'
                   f'Создано: {_ago(d["created_at"])}'
                   + (f' · Утверждено: {_ago(d["approved_at"])}' if d["approved_at"] else "")
                   + '</div>')
                + '</div>'
            )

    # KB matches
    kb_html = ""
    if kb_matches:
        kb_html = '<div class="card"><h2 style="margin-top:0">Похожие кейсы в базе знаний</h2>'
        for km in kb_matches:
            d = dict(km)
            kb_html += (
                f'<div style="padding:8px 0;border-bottom:1px solid #f3f4f6">'
                f'<div style="font-size:.8rem"><strong>Причина:</strong> {d["root_cause"]}</div>'
                + (f'<div style="font-size:.78rem;color:#6b7280">Фикс: {d["fix_applied"]}</div>'
                   if d["fix_applied"] else "")
                + (f'<div style="font-size:.75rem;color:#22c55e">✅ Подтверждён</div>'
                   if d["verified"] else "")
                + '</div>'
            )
        kb_html += '</div>'

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
        # Store comment in the latest investigation or create a note
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
            conn.execute(
                """INSERT INTO investigations (batch_id, attempt, status, admin_comment, created_at)
                   VALUES (?,1,'pending_approval',?,?)""",
                (batch_id, comment.strip(), now)
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
        conf_color = "#22c55e" if d["confidence_pct"] >= 70 else \
                     "#f59e0b" if d["confidence_pct"] >= 40 else "#9ca3af"
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
        return {"status": "ok", "error_batches": total}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})

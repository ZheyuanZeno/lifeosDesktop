#!/usr/bin/env python3
"""LifeOS local journal. Python standard library only; SQLite stores modules and complete records."""
import argparse
import base64
import csv
import datetime as dt
import hashlib
import html
import io
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import sqlite3
import threading
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
KINDS = {"journal": "原始日记", "activity": "完成事项", "expense": "支出", "task": "待办", "note": "随手记"}
ATTACHMENT = re.compile(r"^[a-f0-9]{32}\.(?:png|jpg|jpeg|webp|gif|pdf|txt)$")
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
DATA = ROOT / "data"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def validate(record):
    if not isinstance(record, dict):
        raise ValueError("每条记录必须是一个对象")
    r = dict(record)
    r.setdefault("id", uuid.uuid4().hex)
    if not isinstance(r["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", r["id"]):
        raise ValueError("记录编号无效")
    if r.get("type") not in KINDS:
        raise ValueError("记录类型必须是 journal、activity、expense、task 或 note")
    if not isinstance(r.get("date"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"]):
        raise ValueError("日期请使用 YYYY-MM-DD")
    dt.date.fromisoformat(r["date"])
    for key in ("title", "body", "time_label", "currency", "source_id", "created_at", "updated_at", "module_id"):
        r.setdefault(key, "")
        if not isinstance(r[key], str) or len(r[key]) > 100000:
            raise ValueError("文字字段无效或过长")
    if not r["title"].strip() or len(r["title"]) > 200:
        raise ValueError("标题不能为空，且最多 200 字")
    if r["source_id"] and not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", r["source_id"]):
        raise ValueError("原始记录编号无效")
    r.setdefault("status", "todo" if r["type"] == "task" else "done")
    if r["status"] not in ("todo", "done"):
        raise ValueError("状态必须是 todo 或 done")
    # Amounts are decimal strings, never binary floats. Unknown is null, not zero.
    amount = r.get("amount")
    if amount is not None and amount != "":
        if isinstance(amount, bool) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", str(amount)):
            raise ValueError("金额必须为非负数，最多两位小数")
        r["amount"] = str(amount)
    else:
        r["amount"] = None
    if r["type"] == "expense" and r["amount"] is not None and not r["currency"].strip():
        raise ValueError("填写金额时也请填写币种")
    r.setdefault("attachments", [])
    if not isinstance(r["attachments"], list) or len(r["attachments"]) > 30:
        raise ValueError("附件列表无效")
    for a in r["attachments"]:
        if not isinstance(a, dict) or not ATTACHMENT.fullmatch(str(a.get("file", ""))) or not isinstance(a.get("name"), str) or len(a["name"]) > 300:
            raise ValueError("附件信息无效")
    if not isinstance(r.get("deleted", False), bool):
        raise ValueError("删除状态无效")
    r["deleted"] = r.get("deleted", False)
    r["created_at"] = r["created_at"] or now()
    r["updated_at"] = r["updated_at"] or r["created_at"]
    if r["module_id"] and not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", r["module_id"]):
        raise ValueError("模块编号无效")
    r.setdefault("properties", {})
    if not isinstance(r["properties"], dict) or len(r["properties"]) > 40:
        raise ValueError("自定义属性无效")
    for key, value in r["properties"].items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key) or not isinstance(value, str) or len(value) > 10000:
            raise ValueError("自定义属性值应为文字")
    return {key: r[key] for key in ("module_id", "properties", "id", "type", "date", "title", "body", "time_label", "amount", "currency", "status", "source_id", "attachments", "created_at", "updated_at", "deleted")}


def validate_inbox(value):
    if not isinstance(value, dict):
        raise ValueError("原始收件箱条目格式无效")
    item = dict(value)
    item.setdefault("id", "inbox-" + uuid.uuid4().hex)
    if not isinstance(item["id"], str) or not re.fullmatch(r"inbox-[a-zA-Z0-9_-]{1,100}", item["id"]):
        raise ValueError("原始收件箱编号无效")
    item.setdefault("body", "")
    if not isinstance(item["body"], str) or len(item["body"]) > 100000:
        raise ValueError("原始内容无效或过长")
    item.setdefault("attachments", [])
    if not isinstance(item["attachments"], list) or len(item["attachments"]) > 30:
        raise ValueError("原始附件列表无效")
    for attachment in item["attachments"]:
        if not isinstance(attachment, dict) or not ATTACHMENT.fullmatch(str(attachment.get("file", ""))) or not isinstance(attachment.get("name"), str):
            raise ValueError("原始附件信息无效")
    if not item["body"].strip() and not item["attachments"]:
        raise ValueError("请写点内容或添加附件")
    item.setdefault("created_at", now())
    item.setdefault("processed_at", "")
    for key in ("created_at", "processed_at"):
        if not isinstance(item[key], str) or len(item[key]) > 100:
            raise ValueError("原始收件箱时间无效")
    return {key: item[key] for key in ("id", "body", "attachments", "created_at", "processed_at")}


def validate_pomodoro(value):
    if not isinstance(value, dict):
        raise ValueError("番茄钟记录格式无效")
    item = dict(value)
    item.setdefault("id", "pomodoro-" + uuid.uuid4().hex)
    if not isinstance(item["id"], str) or not re.fullmatch(r"pomodoro-[a-zA-Z0-9_-]{1,100}", item["id"]):
        raise ValueError("番茄钟记录编号无效")
    item.setdefault("label", "")
    if not isinstance(item["label"], str) or len(item["label"]) > 120:
        raise ValueError("专注内容最多 120 个字")
    item.setdefault("deleted", False)
    item.setdefault("mode", "countdown")
    if item["mode"] not in ("countdown", "countup"):
        raise ValueError("计时模式无效")
    duration = item.get("duration_minutes", 25) if item["mode"] == "countdown" else 25
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 180:
        raise ValueError("专注时长需要在 1 至 180 分钟之间")
    item["duration_minutes"] = duration
    elapsed = item.get("elapsed_seconds", 0)
    if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0 or (item["mode"] == "countdown" and item.get("status", "running") in ("running", "paused") and elapsed > duration * 60):
        raise ValueError("番茄钟已用时间无效")
    item["elapsed_seconds"] = elapsed
    item.setdefault("status", "running")
    if item["status"] not in ("running", "paused", "completed", "stopped"):
        raise ValueError("番茄钟状态无效")
    item.setdefault("started_at", now())
    item.setdefault("phase_started_at", item["started_at"] if item["status"] == "running" else "")
    item.setdefault("ended_at", "")
    for key in ("started_at", "phase_started_at", "ended_at"):
        if not isinstance(item[key], str) or len(item[key]) > 100:
            raise ValueError("番茄钟时间无效")
        if item[key]:
            dt.datetime.fromisoformat(item[key])
    if item["status"] == "running" and not item["phase_started_at"]:
        raise ValueError("运行中的番茄钟缺少开始时间")
    if item["status"] in ("completed", "stopped") and not item["ended_at"]:
        raise ValueError("已结束的番茄钟缺少结束时间")
    return {key: item[key] for key in ("id", "label", "deleted", "mode", "duration_minutes", "elapsed_seconds", "status", "started_at", "phase_started_at", "ended_at")}


def pomodoro_elapsed(item, at=None):
    elapsed = item["elapsed_seconds"]
    if item["status"] == "running":
        at = at or dt.datetime.now(dt.timezone.utc)
        phase = dt.datetime.fromisoformat(item["phase_started_at"])
        if phase.tzinfo is None:
            phase = phase.replace(tzinfo=dt.timezone.utc)
        elapsed += max(0, int((at - phase.astimezone(dt.timezone.utc)).total_seconds()))
    return elapsed if item.get("mode") == "countup" else min(item["duration_minutes"] * 60, elapsed)


def sync_pomodoros(doc):
    changed = False
    at = dt.datetime.now(dt.timezone.utc)
    for item in doc.get("pomodoros", []):
        if item.get("mode") != "countup" and item["status"] == "running" and pomodoro_elapsed(item, at) >= item["duration_minutes"] * 60:
            item["elapsed_seconds"] = item["duration_minutes"] * 60
            item["status"] = "completed"
            item["phase_started_at"] = ""
            item["ended_at"] = at.isoformat()
            changed = True
    return changed


TEMPLATES = {
    "journal": ("每日记录", "journal", []),
    "health": ("健康记录", "activity", [{"id": "duration", "name": "时长（分钟）", "type": "number"}]),
    "shopping": ("购物记录", "expense", [{"id": "merchant", "name": "商家", "type": "text"}]),
    "plans": ("计划", "task", []),
    "custom": ("新的模块", "note", []),
}
TYPE_TEMPLATE = {"journal": "journal", "activity": "health", "expense": "shopping", "task": "plans", "note": "custom"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
THEMES = {"light", "paper", "mist", "midnight"}


def validate_color(value, label):
    if not isinstance(value, str) or not COLOR_RE.fullmatch(value):
        raise ValueError(label + "需要使用 6 位十六进制颜色")
    return value.lower()


def validate_theme(value):
    if value not in THEMES:
        raise ValueError("主题不存在")
    return value


def validate_module(value):
    if not isinstance(value, dict):
        raise ValueError("模块格式无效")
    module = dict(value)
    module.setdefault("id", "module-" + uuid.uuid4().hex)
    module.setdefault("kind", "custom")
    if module["kind"] not in TEMPLATES:
        raise ValueError("不支持的模块模板")
    name, _, fields = TEMPLATES[module["kind"]]
    module.setdefault("name", name)
    module.setdefault("fields", fields)
    module.setdefault("view", "list" if module["kind"] in ("journal", "plans") else "table")
    module.setdefault("archived", False)
    module.setdefault("color", "#e5e3dd")
    module.setdefault("background", "#ffffff")
    if not isinstance(module["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", module["id"]):
        raise ValueError("模块编号无效")
    if not isinstance(module["name"], str) or not module["name"].strip() or len(module["name"]) > 80:
        raise ValueError("模块名称需要 1 至 80 个字")
    module["name"] = module["name"].strip()
    module["color"] = validate_color(module["color"], "模块颜色")
    module["background"] = validate_color(module["background"], "模块底色")
    if module["view"] not in ("table", "list") or not isinstance(module["archived"], bool):
        raise ValueError("模块视图无效")
    if not isinstance(module["fields"], list) or len(module["fields"]) > 30:
        raise ValueError("每个模块最多 30 个属性")
    seen = set()
    for field in module["fields"]:
        if not isinstance(field, dict) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", str(field.get("id", ""))) or field["id"] in seen:
            raise ValueError("属性编号无效或重复")
        if field.get("type") not in ("text", "number", "date") or not isinstance(field.get("name"), str) or not field["name"].strip() or len(field["name"]) > 60:
            raise ValueError("属性名称或类型无效")
        seen.add(field["id"])
    layout = module.get("layout")
    if layout is not None:
        if not isinstance(layout, dict):
            raise ValueError("模块位置无效")
        layout = {k: layout.get(k, default) for k, default in (("x", 0), ("y", 0), ("width", 460), ("height", 300), ("z", 0))}
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in layout.values()):
            raise ValueError("模块位置必须是有限数字")
        if not 280 <= layout["width"] <= 1600:
            raise ValueError("模块宽度需要在 280 至 1600 之间")
        if not 160 <= layout["height"] <= 1200:
            raise ValueError("模块高度需要在 160 至 1200 之间")
        if not 0 <= layout["z"] <= 1_000_000_000:
            raise ValueError("模块层级无效")
    module["layout"] = layout
    return {key: module[key] for key in ("id", "name", "kind", "view", "fields", "archived", "layout", "color", "background")}


def assign_module(record, modules):
    if record["module_id"]:
        if not any(m["id"] == record["module_id"] for m in modules):
            raise ValueError("记录引用了不存在的模块，请同时导入模块定义")
        return
    kind = TYPE_TEMPLATE[record["type"]]
    module = next((m for m in modules if m["kind"] == kind and not m["archived"]), None)
    if not module:
        module = validate_module({"id": "module-" + kind, "kind": kind})
        if any(m["id"] == module["id"] for m in modules):
            module["id"] += "-" + uuid.uuid4().hex[:8]
        modules.append(module)
    record["module_id"] = module["id"]


def normalize(doc):
    if doc.get("schema_version", 1) not in (1, 2):
        raise ValueError("不支持此资料版本")
    legacy = doc.get("schema_version", 1) == 1
    modules = [validate_module(m) for m in doc.get("modules", [])]
    if len({m["id"] for m in modules}) != len(modules):
        raise ValueError("模块编号重复")
    records = [validate(r) for r in doc.get("records", [])]
    if len({r["id"] for r in records}) != len(records):
        raise ValueError("记录编号重复")
    for record in records:
        assign_module(record, modules)
        mod = next(m for m in modules if m["id"] == record["module_id"])
        for field in mod["fields"]:
            value = record["properties"].get(field["id"], "")
            if not value:
                continue
            if field["type"] == "number" and not re.fullmatch(r"-?\d{1,12}(?:\.\d{1,4})?", value):
                raise ValueError(field["name"] + "需要填写数字")
            if field["type"] == "date":
                dt.date.fromisoformat(value)
    if legacy:
        for module in modules:
            module["archived"] = not any(r["module_id"] == module["id"] and not r["deleted"] for r in records)
        modules.sort(key=lambda m: {"journal": 0, "health": 1, "plans": 2}.get(m["kind"], 3))
    for i, module in enumerate(m for m in modules if not m["archived"]):
        if module["layout"] is None:
            module["layout"] = {"x": 70 + (i % 2) * 530, "y": 85 + (i // 2) * 350, "width": 460, "height": 300}
    board_title = doc.get("board_title", "我的生活")
    if not isinstance(board_title, str) or not board_title.strip() or len(board_title) > 100:
        raise ValueError("白板名称需要 1 至 100 个字")
    board_background = validate_color(doc.get("board_background", "#ffffff"), "白板背景色")
    theme = validate_theme(doc.get("theme", "light"))
    inbox = [validate_inbox(item) for item in doc.get("inbox", [])]
    if len({item["id"] for item in inbox}) != len(inbox):
        raise ValueError("原始收件箱编号重复")
    pomodoros = [validate_pomodoro(item) for item in doc.get("pomodoros", [])]
    if len({item["id"] for item in pomodoros}) != len(pomodoros):
        raise ValueError("番茄钟记录编号重复")
    if sum(item["status"] in ("running", "paused") for item in pomodoros) > 1:
        raise ValueError("只能有一个正在进行的番茄钟")
    return {"schema_version": 2, "board_title": board_title, "board_background": board_background, "theme": theme, "updated_at": doc.get("updated_at", now()), "modules": modules, "records": records, "inbox": inbox, "pomodoros": pomodoros}


def connect():
    db = sqlite3.connect(DATA / "lifeos.sqlite")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE IF NOT EXISTS modules (id TEXT PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, archived INTEGER NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, module_id TEXT NOT NULL REFERENCES modules(id), position INTEGER NOT NULL, date TEXT NOT NULL, type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, deleted INTEGER NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, position INTEGER NOT NULL, processed_at TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS pomodoros (id TEXT PRIMARY KEY, position INTEGER NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS records_by_module_date ON records(module_id, date)")
    return db


def load():
    with connect() as db:
        meta = dict(db.execute("SELECT key,value FROM metadata"))
        modules = [json.loads(row[0]) for row in db.execute("SELECT payload FROM modules ORDER BY position")]
        records = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records ORDER BY position")]
        inbox = [json.loads(row[0]) for row in db.execute("SELECT payload FROM inbox ORDER BY position")]
        pomodoros = [json.loads(row[0]) for row in db.execute("SELECT payload FROM pomodoros ORDER BY position")]
    return {"schema_version": 2, "board_title": meta.get("board_title", "我的生活"), "board_background": meta.get("board_background", "#ffffff"), "theme": meta.get("theme", "light"), "updated_at": meta.get("updated_at", ""), "modules": modules, "records": records, "inbox": inbox, "pomodoros": pomodoros}


def initialize():
    if not (DATA / "lifeos.sqlite").exists():
        legacy = DATA / "records.json"
        doc = json.loads(legacy.read_text("utf-8")) if legacy.exists() else {"schema_version": 2, "modules": [], "records": []}
        doc = normalize(doc)
        if legacy.exists():
            atomic(DATA / "backups" / ("migration-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"), legacy.read_bytes())
        save(doc, backup=False)
    else:
        doc = load()
        normalized = normalize(doc)
        if normalized != doc:
            save(normalized, backup=False)
        else:
            rebuild(doc)


def visible(doc):
    return [r for r in doc["records"] if not r.get("deleted")]


def markdown(records, date, modules=None):
    lines = [f"# {date}", "", "此文件由数据库导出。请在应用中编辑；迁移时导入完整备份。", ""]
    module_map = {m["id"]: m for m in (modules or [])}
    for r in records:
        if r["date"] != date:
            continue
        lines += [f"## {KINDS[r['type']]} · {r['title']}", "", f"记录编号：{r['id']}", ""]
        if r["time_label"]:
            lines += [f"时间：{r['time_label']}", ""]
        if r["type"] == "task":
            lines += [f"- [{'x' if r['status'] == 'done' else ' '}] {r['title']}", ""]
        if r["type"] == "expense":
            lines += [f"金额：{r['amount'] + ' ' + r['currency'] if r['amount'] is not None else '待补充'}", ""]
        if r["source_id"]:
            lines += [f"原始记录编号：{r['source_id']}", ""]
        lines += [r["body"], ""]
        for field in module_map.get(r.get("module_id"), {}).get("fields", []):
            value = r.get("properties", {}).get(field["id"], "")
            if value:
                lines += [f"{field['name']}：{value}", ""]
        for a in r["attachments"]:
            lines += [f"[附件：{a['name'].replace('[', '').replace(']', '')}](../attachments/{a['file']})", ""]
    return "\n".join(lines)


def rebuild(doc):
    atomic(DATA / "records.json", encode(doc))
    records = visible(doc)
    folder = DATA / "days"
    folder.mkdir(exist_ok=True)
    dates = set(r["date"] for r in records)
    for file in folder.glob("*.md"):
        if file.stem not in dates and re.fullmatch(r"\d{4}-\d{2}-\d{2}", file.stem):
            file.unlink()
    for day in dates:
        atomic(folder / f"{day}.md", markdown(records, day, doc.get("modules")).encode("utf-8"))


def save(doc, backup=True):
    doc = normalize(doc)
    previous = load()
    if backup and previous["updated_at"]:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        atomic(DATA / "backups" / (stamp + ".json"), encode(previous))
        for file in sorted((DATA / "backups").glob("[0-9]*.json"))[:-30]:
            file.unlink()
    doc["updated_at"] = now()
    # Modules and their complete records commit as one transaction.
    with connect() as db:
        db.execute("DELETE FROM records")
        db.execute("DELETE FROM modules")
        db.execute("DELETE FROM inbox")
        db.execute("DELETE FROM pomodoros")
        db.executemany("INSERT INTO modules VALUES (?,?,?,?,?,?)", [(m["id"], i, m["name"], m["kind"], int(m["archived"]), json.dumps(m, ensure_ascii=False)) for i, m in enumerate(doc["modules"])])
        db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)", [(r["id"], r["module_id"], i, r["date"], r["type"], r["title"], r["body"], int(r["deleted"]), json.dumps(r, ensure_ascii=False)) for i, r in enumerate(doc["records"])])
        db.executemany("INSERT INTO inbox VALUES (?,?,?,?)", [(item["id"], i, item["processed_at"], json.dumps(item, ensure_ascii=False)) for i, item in enumerate(doc["inbox"])])
        db.executemany("INSERT INTO pomodoros VALUES (?,?,?,?,?)", [(item["id"], i, item["status"], item["started_at"], json.dumps(item, ensure_ascii=False)) for i, item in enumerate(doc["pomodoros"])])
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('board_title',?)", (doc["board_title"],))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('board_background',?)", (doc["board_background"],))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('theme',?)", (doc["theme"],))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('updated_at',?)", (doc["updated_at"],))
    rebuild(doc)


def csv_bytes(records, kind=None, fields_extra=None):
    fields = ["module_id", "id", "date", "title", "time_label", "amount", "currency", "status", "body", "source_id"]
    fields += [f["id"] for f in (fields_extra or [])]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([next((f["name"] for f in (fields_extra or []) if f["id"] == key), key) for key in fields])
    for r in records:
        if kind is None or r["type"] == kind:
            values = ["" if r.get(f, r.get("properties", {}).get(f, "")) is None else str(r.get(f, r.get("properties", {}).get(f, ""))) for f in fields]
            writer.writerow(["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values])
    return stream.getvalue().encode("utf-8-sig")


def reading_html(doc):
    esc = html.escape
    records = visible(doc)
    modules = {m["id"]: m for m in doc.get("modules", [])}
    days = sorted({r["date"] for r in records}, reverse=True)
    sections = []
    for day in days:
        parts = [f'<section id="d{day}"><h2>{day}</h2>']
        for r in records:
            if r["date"] != day:
                continue
            details = esc(r["time_label"])
            if r["type"] == "expense":
                details += " · " + esc(r["amount"] + " " + r["currency"] if r["amount"] is not None else "金额待补")
            if r["type"] == "task":
                details += " · " + ("已完成" if r["status"] == "done" else "未完成")
            parts.append(f'<article><small>{esc(modules.get(r.get("module_id"), {}).get("name", KINDS[r["type"]]))} {details}</small><h3>{esc(r["title"])}</h3><p>{esc(r["body"])}</p>')
            module = modules.get(r.get("module_id"), {})
            for field in module.get("fields", []):
                value = r.get("properties", {}).get(field["id"], "")
                if value:
                    parts.append(f'<p>{esc(field["name"])}：{esc(value)}</p>')
            for a in r["attachments"]:
                file = DATA / "attachments" / a["file"]
                if file.is_file():
                    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    uri = f"data:{mime};base64," + base64.b64encode(file.read_bytes()).decode()
                    parts.append(f'<a href="{uri}" download="{esc(a["name"], quote=True)}">附件：{esc(a["name"])}</a>')
                    if mime.startswith("image/"):
                        parts.append(f'<p><img src="{uri}" alt="{esc(a["name"], quote=True)}"></p>')
            parts.append(f'<footer>记录编号：{esc(r["id"])}' + (f' · 原始记录：{esc(r["source_id"])}' if r["source_id"] else "") + '</footer></article>')
        sections.append("".join(parts) + "</section>")
    links = " ".join(f'<a href="#d{day}">{day}</a>' for day in days)
    return ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>我的生活记录 · 离线阅读</title><style>body{max-width:850px;margin:50px auto;padding:0 24px;font:16px/1.8 system-ui;color:#37352f;background:#fff}h1{font-size:36px}h2{margin-top:40px}article{padding:22px 0;border-top:1px solid #d7ddd6}h3{margin:6px 0}p{white-space:pre-wrap}small,footer{color:#62756c}footer{font-size:11px;overflow-wrap:anywhere}a{color:#386b57;margin-right:15px}img{max-width:100%;max-height:600px}nav{line-height:2.5}@media print{body{margin:0;background:white}article{break-inside:avoid}}</style><header><small>LIFEOS / PERSONAL ARCHIVE</small><h1>我的生活记录</h1><p>离线阅读版 · 所有内容与附件均包含在本文件中，无需联网。</p></header><nav>''' + links + "</nav>" + "".join(sections) + "</html>").encode("utf-8")


def export_zip(doc):
    stream = io.BytesIO()
    records = visible(doc)
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("records.json", encode(doc))
        z.writestr("阅读我的记录.html", reading_html(doc))
        z.writestr("expenses.csv", csv_bytes(records, "expense"))
        z.writestr("tasks.csv", csv_bytes(records, "task"))
        z.writestr("README.md", (ROOT / "FORMAT.md").read_bytes())
        for module in doc.get("modules", []):
            rows = [r for r in records if r.get("module_id") == module["id"]]
            z.writestr(f"modules/{module['id']}/records.csv", csv_bytes(rows, fields_extra=module["fields"]))
            z.writestr(f"modules/{module['id']}/module.json", encode(module))
        for day in sorted({r["date"] for r in records}):
            z.writestr(f"days/{day}.md", markdown(records, day, doc.get("modules")))
        for name in sorted({a["file"] for r in doc["records"] for a in r["attachments"]}):
            file = DATA / "attachments" / name
            if not file.is_file():
                raise ValueError("有附件缺失，请先恢复附件再导出完整备份")
            z.write(file, f"attachments/{name}")
    return stream.getvalue()


def parse_import(payload):
    files = {}
    if payload.get("archive"):
        raw = base64.b64decode(payload["archive"], validate=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries = z.infolist()
            if len(entries) > 10000 or sum(x.file_size for x in entries) > 100 * 1024 * 1024:
                raise ValueError("备份解压后不能超过 100MB 或 10000 个文件")
            if len({x.filename for x in entries}) != len(entries):
                raise ValueError("备份内存在重复文件名")
            value = json.loads(z.read("records.json"))
            for item in entries:
                if item.filename.startswith("attachments/"):
                    name = item.filename[len("attachments/"):]
                    if not ATTACHMENT.fullmatch(name) or item.file_size > 10 * 1024 * 1024:
                        raise ValueError("备份附件路径或大小无效")
                    files[name] = z.read(item)
    else:
        raw = payload.get("text", "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        value = json.loads(raw)
    if isinstance(value, dict) and value.get("schema_version", 1) not in (1, 2):
        raise ValueError("不支持此数据版本，请保留文件并升级应用")
    rows = value if isinstance(value, list) else value.get("records") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("需要 records 数组，最多 20000 条")
    # Identical pasted records without IDs still deduplicate deterministically.
    for row in rows:
        if isinstance(row, dict) and not row.get("id"):
            stable = json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
            row["id"] = "import-" + hashlib.sha256(stable).hexdigest()[:32]
    records = [validate(r) for r in rows]
    if len({r["id"] for r in records}) != len(records):
        raise ValueError("导入内容中存在重复编号")
    for r in records:
        for a in r["attachments"]:
            if a["file"] not in files and not (DATA / "attachments" / a["file"]).is_file():
                raise ValueError("附件缺失，请导入完整 ZIP 备份")
    modules = [validate_module(m) for m in value.get("modules", [])] if isinstance(value, dict) else []
    if len({m["id"] for m in modules}) != len(modules):
        raise ValueError("导入内容中存在重复模块编号")
    board_title = value.get("board_title") if isinstance(value, dict) else None
    theme = value.get("theme") if isinstance(value, dict) else None
    if theme is not None:
        theme = validate_theme(theme)
    pomodoros = [validate_pomodoro(item) for item in value.get("pomodoros", [])] if isinstance(value, dict) else []
    if len({item["id"] for item in pomodoros}) != len(pomodoros):
        raise ValueError("导入内容中存在重复番茄钟记录")
    return records, files, modules, board_title, theme, pomodoros


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, body, status=200, mime="application/json; charset=utf-8", filename=None):
        if not isinstance(body, bytes):
            body = encode(body)
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def safe_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.safe_host():
            return self.reply({"error": "请从本机地址访问"}, 403)
        path = urlparse(self.path).path
        try:
            with LOCK:
                if path == "/api/state":
                    doc = load()
                    if sync_pomodoros(doc):
                        save(doc)
                        doc = load()
                    return self.reply({**doc, "token": TOKEN, "data_path": str(DATA), "can_undo": bool(list((DATA / "backups").glob("[0-9]*.json")))})
                if path == "/api/search":
                    query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                    with sqlite3.connect(DATA / "lifeos.sqlite") as db:
                        ids = [row[0] for row in db.execute("SELECT id FROM records WHERE deleted=0 AND instr(lower(title || ' ' || body), lower(?)) > 0", (query,))]
                    return self.reply({"ids": ids})
                if path == "/api/export":
                    doc = load()
                    params = parse_qs(urlparse(self.path).query)
                    kind = params.get("format", ["zip"])[0]
                    module_id = params.get("module", [""])[0]
                    if module_id:
                        modules = [m for m in doc["modules"] if m["id"] == module_id]
                        if not modules:
                            raise ValueError("模块不存在")
                        doc = {**doc, "modules": modules, "records": [r for r in doc["records"] if r["module_id"] == module_id]}
                    if kind == "csv":
                        fields = doc["modules"][0]["fields"] if module_id else []
                        return self.reply(csv_bytes(visible(doc), fields_extra=fields), mime="text/csv; charset=utf-8", filename="lifeos-records.csv")
                    stamp = dt.date.today().isoformat()
                    if kind == "html":
                        return self.reply(reading_html(doc), mime="text/html; charset=utf-8", filename=f"lifeos-{stamp}.html")
                    if kind == "json":
                        return self.reply(encode(doc), filename=f"lifeos-{stamp}.json")
                    if kind in ("expense", "task"):
                        return self.reply(csv_bytes(visible(doc), kind), mime="text/csv; charset=utf-8", filename=f"lifeos-{kind}-{stamp}.csv")
                    return self.reply(export_zip(doc), mime="application/zip", filename=f"lifeos-backup-{stamp}.zip")
                if path.startswith("/attachments/"):
                    name = path.removeprefix("/attachments/")
                    if ATTACHMENT.fullmatch(name) and (DATA / "attachments" / name).is_file():
                        return self.reply((DATA / "attachments" / name).read_bytes(), mime=mimetypes.guess_type(name)[0] or "application/octet-stream")
                if path in ("/", "/app.js", "/style.css"):
                    file = ROOT / "static" / {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
                    return self.reply(file.read_bytes(), mime={".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}[file.suffix])
                self.reply({"error": "未找到"}, 404)
        except (ValueError, OSError, sqlite3.Error) as e:
            self.reply({"error": str(e)}, 400)

    def do_POST(self):
        if not self.safe_host() or self.headers.get("X-LifeOS-Token") != TOKEN:
            return self.reply({"error": "请刷新页面后重试"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
            return self.reply({"error": "不允许跨站写入"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 45 * 1024 * 1024:
                raise ValueError("请求为空或超过 45MB")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            with LOCK:
                doc = load()
                path = urlparse(self.path).path
                if path == "/api/inbox":
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "收件箱已在其他窗口更新，请刷新后再试"}, 409)
                    item_id = payload.get("id")
                    old = next((item for item in doc["inbox"] if item["id"] == item_id), None) if item_id else None
                    if item_id and (not old or old["processed_at"]):
                        raise ValueError("收件箱条目不存在或已经整理")
                    item = validate_inbox({"id": old["id"], "created_at": old["created_at"], "processed_at": old["processed_at"], "body": payload.get("body", ""), "attachments": payload.get("attachments", [])}) if old else validate_inbox({"body": payload.get("body", ""), "attachments": payload.get("attachments", [])})
                    for attachment in item["attachments"]:
                        if not (DATA / "attachments" / attachment["file"]).is_file():
                            raise ValueError("原始附件不存在")
                    if old:
                        doc["inbox"][doc["inbox"].index(old)] = item
                    else:
                        doc["inbox"].append(item)
                    save(doc)
                    return self.reply({"ok": True, "item": item, "updated": bool(old)})
                if path == "/api/inbox/complete":
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "收件箱已在其他窗口更新，请刷新后再试"}, 409)
                    ids = payload.get("ids")
                    if not isinstance(ids, list) or not ids or len(ids) > 500 or any(not isinstance(value, str) for value in ids):
                        raise ValueError("请选择要完成的原始条目")
                    pending = {item["id"]: item for item in doc["inbox"] if not item["processed_at"]}
                    if any(value not in pending for value in ids):
                        raise ValueError("原始条目不存在或已经处理")
                    completed_at = now()
                    for value in ids:
                        pending[value]["processed_at"] = completed_at
                    save(doc)
                    return self.reply({"ok": True, "completed": len(ids)})
                if path == "/api/inbox/delete":
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "收件箱已在其他窗口更新，请刷新后再试"}, 409)
                    item_id = payload.get("id")
                    if not isinstance(item_id, str):
                        raise ValueError("收件箱条目编号无效")
                    index = next((i for i, item in enumerate(doc["inbox"]) if item["id"] == item_id), None)
                    if index is None:
                        raise ValueError("收件箱条目不存在")
                    doc["inbox"].pop(index)
                    save(doc)
                    return self.reply({"ok": True})
                if path == "/api/theme":
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "外观已在其他窗口更新，请刷新后再试"}, 409)
                    doc["theme"] = validate_theme(payload.get("theme"))
                    save(doc)
                    return self.reply({"ok": True, "theme": doc["theme"]})
                if path == "/api/pomodoro":
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "番茄钟已在其他窗口更新，请刷新后再试"}, 409)
                    timer_finished = sync_pomodoros(doc)
                    action = payload.get("action")
                    active = next((item for item in doc["pomodoros"] if item["status"] in ("running", "paused")), None)
                    if action == "start":
                        if active:
                            raise ValueError("已有一个番茄钟正在进行")
                        label = str(payload.get("label", "")).strip()
                        duration = payload.get("duration_minutes")
                        started = now()
                        item = validate_pomodoro({"label": label, "mode": payload.get("mode", "countdown"), "duration_minutes": duration, "started_at": started, "phase_started_at": started})
                        doc["pomodoros"].append(item)
                    elif action in ("update_history", "delete_history"):
                        item = next((i for i in doc["pomodoros"] if i["id"] == payload.get("id") and not i.get("deleted")), None)
                        if not item or item["status"] not in ("completed", "stopped"):
                            raise ValueError("只能编辑或删除已结束的专注记录")
                        if action == "delete_history":
                            item["deleted"] = True
                        else:
                            label, seconds = payload.get("label"), payload.get("elapsed_seconds")
                            if not isinstance(label, str) or len(label.strip()) > 120:
                                raise ValueError("专注内容最多 120 个字")
                            if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 31536000:
                                raise ValueError("专注时长无效")
                            item["label"], item["elapsed_seconds"] = label.strip(), seconds
                    elif action == "rename":
                        item = next((item for item in doc["pomodoros"] if item["id"] == payload.get("id")), None)
                        if not item:
                            raise ValueError("没有找到番茄钟记录")
                        label = payload.get("label")
                        if not isinstance(label, str) or len(label.strip()) > 120:
                            raise ValueError("专注内容最多 120 个字")
                        item["label"] = label.strip()
                    elif action in ("pause", "resume", "finish"):
                        if not active or active["id"] != payload.get("id"):
                            finished = next((item for item in doc["pomodoros"] if item["id"] == payload.get("id") and item["status"] == "completed"), None)
                            if timer_finished and finished:
                                save(doc)
                                return self.reply({"ok": True})
                            raise ValueError("没有找到正在进行的番茄钟")
                        at = dt.datetime.now(dt.timezone.utc)
                        if action == "pause":
                            if active["status"] != "running":
                                raise ValueError("番茄钟已经暂停")
                            active["elapsed_seconds"] = pomodoro_elapsed(active, at)
                            active["status"] = "paused"
                            active["phase_started_at"] = ""
                        elif action == "resume":
                            if active["status"] != "paused":
                                raise ValueError("番茄钟正在运行")
                            active["status"] = "running"
                            active["phase_started_at"] = at.isoformat()
                        else:
                            active["elapsed_seconds"] = pomodoro_elapsed(active, at)
                            active["status"] = "completed" if active.get("mode") == "countup" or active["elapsed_seconds"] >= active["duration_minutes"] * 60 else "stopped"
                            active["phase_started_at"] = ""
                            active["ended_at"] = at.isoformat()
                    else:
                        raise ValueError("不支持的番茄钟操作")
                    save(doc)
                    return self.reply({"ok": True})
                if path in ("/api/module", "/api/reorder", "/api/board"):
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "白板已在其他窗口更新，请刷新后再试"}, 409)
                    if path == "/api/module":
                        module = validate_module(payload["module"])
                        old = next((m for m in doc["modules"] if m["id"] == module["id"]), None)
                        if old:
                            if old["kind"] != module["kind"]:
                                raise ValueError("已有模块的模板不能更改")
                            doc["modules"][doc["modules"].index(old)] = module
                        else:
                            doc["modules"].append(module)
                        if "board_background" in payload:
                            doc["board_background"] = validate_color(payload["board_background"], "白板背景色")
                    elif path == "/api/reorder":
                        ids = payload["ids"]
                        if not isinstance(ids, list) or len(ids) != len(doc["modules"]) or set(ids) != {m["id"] for m in doc["modules"]}:
                            raise ValueError("模块顺序无效")
                        mapping = {m["id"]: m for m in doc["modules"]}
                        doc["modules"] = [mapping[id] for id in ids]
                    else:
                        doc["board_title"] = payload["title"]
                    save(doc)
                    return self.reply({"ok": True})
                if path == "/api/save":
                    r = validate(payload["record"])
                    old = next((x for x in doc["records"] if x["id"] == r["id"]), None)
                    if old and payload.get("expected_updated_at") != old["updated_at"]:
                        return self.reply({"error": "这条记录已在其他窗口修改，请刷新后重新编辑"}, 409)
                    for a in r["attachments"]:
                        if not (DATA / "attachments" / a["file"]).is_file():
                            raise ValueError("附件不存在")
                    r["created_at"] = old["created_at"] if old else r["created_at"]
                    r["updated_at"] = now()
                    if old:
                        doc["records"][doc["records"].index(old)] = r
                    else:
                        doc["records"].append(r)
                    save(doc)
                    return self.reply({"ok": True, "record": r})
                if path == "/api/attachment":
                    name = str(payload.get("name", ""))
                    ext = Path(name).suffix.lower()
                    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".txt"):
                        raise ValueError("支持 PNG、JPG、WEBP、GIF、PDF、TXT 附件")
                    raw = base64.b64decode(payload["data"], validate=True)
                    if len(raw) > 10 * 1024 * 1024:
                        raise ValueError("单个附件最多 10MB")
                    file = uuid.uuid4().hex + ext
                    atomic(DATA / "attachments" / file, raw)
                    return self.reply({"file": file, "name": name[:300]})
                if path == "/api/import":
                    records, files, imported_modules, board_title, imported_theme, imported_pomodoros = parse_import(payload)
                    if not records and not imported_modules and not imported_pomodoros:
                        raise ValueError("没有可导入的记录")
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "资料库已发生变化，请刷新后重新导入"}, 409)
                    current = {r["id"]: r for r in doc["records"]}
                    added = sum(r["id"] not in current for r in records)
                    conflicts = sum(r["id"] in current for r in records)
                    overwrite = payload.get("overwrite") is True
                    existing_module_ids = {m["id"] for m in doc["modules"]}
                    module_map = {m["id"]: m for m in doc["modules"]}
                    modules_added = sum(m["id"] not in module_map for m in imported_modules)
                    for module in imported_modules:
                        if module["id"] not in module_map or overwrite:
                            module_map[module["id"]] = module
                    doc["modules"] = list(module_map.values())
                    for record in records:
                        if record["id"] in current and not record["module_id"]:
                            record["module_id"] = current[record["id"]]["module_id"]
                        assign_module(record, doc["modules"])
                    modules_added = sum(m["id"] not in existing_module_ids for m in doc["modules"])
                    # Validate every collision before writing anything.
                    for name, raw in files.items():
                        target = DATA / "attachments" / name
                        if target.exists() and target.read_bytes() != raw:
                            raise ValueError("附件编号冲突，已停止导入")
                    for r in records:
                        if r["id"] not in current or overwrite:
                            current[r["id"]] = r
                    current_pomodoros = {item["id"]: item for item in doc["pomodoros"]}
                    pomodoros_added = sum(item["id"] not in current_pomodoros for item in imported_pomodoros)
                    for item in imported_pomodoros:
                        if item["id"] not in current_pomodoros or overwrite:
                            current_pomodoros[item["id"]] = item
                    if added or modules_added or pomodoros_added or (overwrite and (conflicts or imported_modules or imported_pomodoros)):
                        candidate = {**doc, "records": list(current.values())}
                        candidate["pomodoros"] = list(current_pomodoros.values())
                        if board_title and (not doc["records"] or payload.get("restore_title") is True):
                            candidate["board_title"] = board_title
                        if imported_theme and (not doc["records"] or payload.get("restore_title") is True):
                            candidate["theme"] = imported_theme
                        candidate = normalize(candidate)
                        for name, raw in files.items():
                            atomic(DATA / "attachments" / name, raw)
                        save(candidate)
                    return self.reply({"ok": True, "modules_added": modules_added, "added": added, "pomodoros_added": pomodoros_added, "updated": conflicts if overwrite else 0, "skipped": 0 if overwrite else conflicts})
                if path == "/api/undo":
                    if payload.get("expected_revision") != doc["updated_at"]:
                        return self.reply({"error": "资料库已更新，请刷新后再撤销"}, 409)
                    backups = sorted((DATA / "backups").glob("[0-9]*.json"))
                    if not backups:
                        raise ValueError("没有可撤销的操作")
                    previous = json.loads(backups[-1].read_text("utf-8"))
                    previous["records"] = [validate(r) for r in previous["records"]]
                    save(previous, backup=False)
                    backups[-1].unlink()
                    return self.reply({"ok": True})
                self.reply({"error": "未找到"}, 404)
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, sqlite3.Error) as e:
            self.reply({"error": "操作未完成：" + str(e)}, 400)


def main():
    global DATA
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    DATA = args.data_dir.resolve()
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "attachments").mkdir(exist_ok=True)
    (DATA / "backups").mkdir(exist_ok=True)
    # Acquire the port before opening the data, preventing duplicate default launches.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # OS lock prevents two application instances on different ports sharing this data.
    lockfile = open(DATA / ".instance.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lockfile.seek(0)
            lockfile.write(b"0")
            lockfile.flush()
            lockfile.seek(0)
            msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        server.server_close()
        raise SystemExit("此资料库已经在另一个 LifeOS 程序中打开")
    initialize()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"LifeOS 已启动：{url}\n资料目录：{DATA}\n关闭此终端或按 Ctrl+C 停止。", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    floating_timer = None
    if sys.platform.startswith('linux') and os.environ.get('DISPLAY'):
        floating_timer = subprocess.Popen([sys.executable, str(ROOT / 'focus_float.py'), url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if floating_timer and floating_timer.poll() is None:
            floating_timer.terminate()
            floating_timer.wait(timeout=5)
        server.server_close()
        lockfile.close()


if __name__ == "__main__":
    main()

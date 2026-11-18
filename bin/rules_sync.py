#!/usr/bin/env python3
"""Пополняет глобальные правила (~/.agent-rules/CLAUDE.md) наблюдениями из сессий
Claude Code и Codex.

Команды:
  kick   — вызывается хуком SessionEnd; запускает `run` в фоне и сразу выходит.
  run    — обрабатывает накопившиеся сессии (вызывает модель только при необходимости).
  init   — помечает все существующие сессии обработанными (точка отсчёта).
  status — показывает очередь без вызова модели.

Экономия токенов:
  1. Сессия без «сигнальных» фраз (поправки, «всегда», «опять»…) отсеивается локально, без модели.
  2. В модель идут только сигнальные реплики пользователя (до 300 символов) с кусочком контекста.
  3. Вставки (<pasted_content>), системные блоки и вывод инструментов вырезаются.
  4. Сессии копятся пачкой: модель вызывается, когда набралось MIN_SIGNALS выдержек
     или самая старая ждёт дольше MAX_WAIT_H часов. Один вызов на пачку.
  5. Claude без инструментов, MCP, плагинов и CLAUDE.md (~600 токенов накладных);
     Codex — только запасной вариант.
  6. Не больше DAILY_CAP вызовов в сутки.
"""
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HOME = os.path.expanduser("~")
os.environ["PATH"] = os.pathsep.join([os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                                      os.environ.get("PATH", "/usr/bin:/bin")])
ROOT = os.path.join(HOME, ".agent-rules")
RULES = os.path.join(ROOT, "CLAUDE.md")
STATE_DIR = os.path.join(ROOT, ".state")
STATE = os.path.join(STATE_DIR, "state.json")
LOG = os.path.join(STATE_DIR, "log.txt")
PROMPT = os.path.join(ROOT, "bin", "prompt.md")

CLAUDE_GLOB = os.path.join(HOME, ".claude/projects/*/*.jsonl")
CODEX_GLOBS = [
    os.path.join(HOME, ".codex/sessions/**/rollout-*.jsonl"),
    os.path.join(HOME, "Library/Application Support/Orca/codex-accounts/*/home/sessions/**/rollout-*.jsonl"),
]
CODEX_ORIGINATORS = {"codex-tui", "orca_desktop", "codex_cli_rs"}

QUIET_SEC = 120          # сессия считается закрытой, если файл не менялся 2 минуты
MIN_SIGNALS = 3          # копить пачку, пока не наберётся столько выдержек…
MAX_WAIT_H = 24          # …или пока самая старая не ждёт дольше суток
DAILY_CAP = 4
MSG_CHARS = 300
CTX_CHARS = 200
BATCH_CHARS = 12000
CLAUDE_MODEL = "sonnet"
PENDING_SECTION = "## На решение"
OWN_EMAIL = "egorov.d.nikita@mail.ru"

SIGNAL = re.compile(
    r"(не\s+(надо|нужно|делай|трогай|так|то\b|спрашивай|пиши|добавляй|меняй)"
    r"|опять|снова|вс[её]\s+ещ[её]|я\s+же\s+(просил|говорил|писал)|сколько\s+раз"
    r"|почему\s+ты|зачем\s+ты|ты\s+(опять|снова|не\s+)|больше\s+не|никогда|всегда"
    r"|каждый\s+раз|запомни|впредь|в\s+будущем|по\s+умолчанию|на\s+будущее"
    r"|мне\s+(нравится|не\s+нравится|удобнее|важно|не\s+важно)|предпочитаю"
    r"|хочу,?\s+чтобы\s+ты|не\s+люблю|раздража|бесит|сам\s+(выбери|реши)|решение\s+за\s+(тобой|вами)"
    r"|в\s+следующий\s+раз|обычно\s+я|я\s+всегда|правило)",
    re.I,
)
SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_|xox[abpr]-|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{20,}|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/_-]{40,}\b"
    r"|https?://|www\.|figma\.com|\+?\d[\d ()-]{9,}\d|password|пароль\s*[:=])",
    re.I,
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"sessions": {}, "pending": [], "calls": {}}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


# ---------- чтение переписок ----------

def clean(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.S)
    text = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " [вставка] ", text, flags=re.S)
    text = re.sub(r"<[a-z_-]+>.*?</[a-z_-]+>", " ", text, flags=re.S)
    text = re.sub(r"<[a-z_-]+/>", " ", text)
    text = re.sub(r"```.*?```", " [код] ", text, flags=re.S)
    text = re.sub(r"https?://\S+|www\.\S+", "[ссылка]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[почта]", text)
    return re.sub(r"\s+", " ", text).strip()


def skip_user_text(t):
    return (not t or t.startswith(("<command-", "<local-command", "Caveat:", "[Request interrupted",
                                   "# AGENTS.md", "<environment_context", "<user_instructions",
                                   "This session is being continued", "<artifact-content"))
            or "SessionStart hook" in t[:200])


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for x in content or []:
        if isinstance(x, dict) and x.get("type") in ("text", "Text", "input_text", "output_text"):
            out.append(x.get("text", ""))
    return "\n".join(out)


def read_claude(path):
    """[(role, text)] или None, если сессия не из CLI."""
    msgs, entry = [], None
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if (t not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta")
                    or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly")):
                continue
            if t == "user":
                entry = entry or d.get("entrypoint")
                c = d.get("message", {}).get("content")
                if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                    continue
                raw = text_of(c).strip()
                if skip_user_text(raw):
                    continue
                msgs.append(("user", clean(raw)))
            else:
                txt = clean(text_of(d.get("message", {}).get("content")))
                if txt:
                    msgs.append(("agent", txt))
    if entry != "cli":
        return None
    return msgs


def read_codex(path):
    msgs, meta = [], None
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload") or {}
            if d.get("type") == "session_meta":
                meta = p
                continue
            if d.get("type") != "event_msg":
                continue
            pt = p.get("type")
            if pt == "item_completed":
                it = p.get("item") or {}
                if it.get("type") == "UserMessage":
                    raw = text_of(it.get("content")).strip()
                    if not skip_user_text(raw):
                        msgs.append(("user", clean(raw)))
                elif it.get("type") == "AgentMessage":
                    txt = clean(text_of(it.get("content")))
                    if txt:
                        msgs.append(("agent", txt))
            elif pt == "user_message":
                raw = (p.get("message") or "").strip()
                if not skip_user_text(raw):
                    msgs.append(("user", clean(raw)))
            elif pt == "agent_message":
                txt = clean(p.get("message") or "")
                if txt:
                    msgs.append(("agent", txt))
    if not meta or meta.get("originator") not in CODEX_ORIGINATORS:
        return None
    return msgs


def all_sessions():
    out = [(p, "Claude Code") for p in glob.glob(CLAUDE_GLOB)]
    for g in CODEX_GLOBS:
        out += [(p, "Codex") for p in glob.glob(g, recursive=True)]
    return out


def read(path, agent):
    return read_claude(path) if agent == "Claude Code" else read_codex(path)


def user_count(msgs):
    return sum(1 for r, _ in msgs if r == "user")


def excerpts(msgs, start_user_idx):
    """Сигнальные реплики пользователя начиная с start_user_idx-й, с контекстом."""
    res, ui, last_agent, prev_user = [], 0, "", ""
    for role, txt in msgs:
        if role == "agent":
            last_agent = txt
            continue
        if ui >= start_user_idx and SIGNAL.search(txt) and txt != "[вставка]":
            block = []
            if prev_user:
                block.append("Я (до этого): " + prev_user[:CTX_CHARS])
            if last_agent:
                block.append("Агент: " + last_agent[-CTX_CHARS:])
            block.append("Я: " + txt[:MSG_CHARS])
            res.append("\n".join(block))
        prev_user, ui = txt, ui + 1
    return res


# ---------- сбор очереди ----------

def collect(st):
    now = time.time()
    for path, agent in all_sessions():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        rec = st["sessions"].get(path)
        if rec and rec.get("mtime") == mtime:
            continue
        if now - mtime < QUIET_SEC:
            continue  # сессия ещё идёт
        msgs = read(path, agent)
        if msgs is None:
            st["sessions"][path] = {"mtime": mtime, "done": 0}
            continue
        done = rec.get("done", 0) if rec else 0
        n = user_count(msgs)
        if n > done:
            ex = excerpts(msgs, done)
            if ex:
                st["pending"].append({
                    "agent": agent,
                    "date": datetime.fromtimestamp(mtime).strftime("%d.%m"),
                    "added": now,
                    "text": "\n\n".join(ex),
                })
        st["sessions"][path] = {"mtime": mtime, "done": n}


# ---------- модель ----------

def call_model(system, user):
    env = dict(os.environ, RULES_SYNC_CHILD="1")
    cwd = tempfile.mkdtemp(prefix="rules-sync-")
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", CLAUDE_MODEL, "--setting-sources", "", "--tools", "",
             "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
             "--system-prompt", system, "--output-format", "json"],
            input=user, capture_output=True, text=True, timeout=300, cwd=cwd, env=env)
        d = json.loads(r.stdout)
        if not d.get("is_error"):
            u = d.get("usage", {})
            log("claude: in=%s cache=%s out=%s" % (u.get("input_tokens"), u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0), u.get("output_tokens")))
            return d.get("result", "")
        log("claude error: %s" % str(d.get("result"))[:200])
    except Exception as e:
        log("claude failed: %s" % e)
    # запасной вариант — Codex (дороже: ~20k токенов накладных)
    try:
        instr = os.path.join(cwd, "instr.md")
        out = os.path.join(cwd, "out.txt")
        with open(instr, "w") as f:
            f.write(system)
        subprocess.run(
            ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
             "-s", "read-only", "-c", "model_reasoning_effort=low", "-c", "model_instructions_file=" + instr,
             "-c", "project_doc_max_bytes=0", "-o", out, user],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, cwd=cwd, env=env)
        with open(out) as f:
            log("codex fallback used")
            return f.read()
    except Exception as e:
        log("codex failed: %s" % e)
    return None


def parse_ops(raw):
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0)).get("ops", [])
    except Exception:
        return None


# ---------- применение ----------

def norm(s):
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def is_dup(text, existing):
    n = norm(text)
    if n in existing:
        return True
    words = set(n.split())
    for e in existing:
        if len(n) >= 6 and n in e:
            return True
        ew = set(e.split())
        if words and len(words & ew) / len(words) >= 0.8 and len(words) >= 4:
            return True
    return False


def unsafe(s):
    if SECRET.search(s):
        return True
    return any(e.lower() != OWN_EMAIL for e in EMAIL.findall(s))


def heading_title(line):
    return line.lstrip("#").strip()


def apply_ops(ops, sources):
    with open(RULES) as f:
        lines = f.read().split("\n")
    existing = {norm(l) for l in lines if l.strip()}
    added, conflicts, src = [], [], "%s, %s" % (sources, datetime.now().strftime("%Y-%m-%d"))

    for op in ops[:5]:
        kind = op.get("type")
        text = (op.get("text") or op.get("proposed") or "").strip()
        why = (op.get("why") or "").strip()[:120]
        if not text.startswith("- "):
            text = "- " + text.lstrip("-• ").strip()
        if len(text) < 8 or unsafe(text) or unsafe(why) or is_dup(text, existing):
            log("skip op: %s" % text[:80])
            continue
        if kind == "add":
            sec = heading_title(op.get("section") or "")
            idx = next((i for i, l in enumerate(lines) if l.startswith("#") and heading_title(l) == sec), None)
            if idx is None or sec == heading_title(PENDING_SECTION):
                kind, op = "conflict", {"existing": "(новый раздел «%s»)" % sec[:60]}
            else:
                end = next((j for j in range(idx + 1, len(lines)) if lines[j].startswith("#")), len(lines))
                ins = end
                while ins > idx + 1 and not lines[ins - 1].strip():
                    ins -= 1
                lines.insert(ins, text)
                existing.add(norm(text))
                added.append((sec, text))
                continue
        if kind == "conflict":
            old = (op.get("existing") or "").strip()[:300]
            if unsafe(old) and not old.startswith("(новый раздел"):
                continue
            if PENDING_SECTION not in lines:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += ["", PENDING_SECTION, "",
                          "Сюда автоматика кладёт правила, которые противоречат существующим или не нашли раздел. "
                          "Решение за Никитой: перенести в нужный раздел или удалить.", ""]
            while lines and not lines[-1].strip():
                lines.pop()
            if not lines[-1].startswith("- "):
                lines.append("")
            entry = "- **Было:** %s → **Предлагается:** %s _(%s; «%s»)_" % (
                old.lstrip("- "), text[2:], src, why)
            lines.append(entry)
            existing.add(norm(text))
            conflicts.append(text)

    if not added and not conflicts:
        return None
    with open(RULES, "w") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    return added, conflicts


def commit(added, conflicts, sources):
    title = "Правила: " + ", ".join(
        x for x in ("+%d" % len(added) if added else "", "на решение %d" % len(conflicts) if conflicts else "") if x)
    body = ["- [%s] %s" % (s, t[2:]) for s, t in added] + ["- [На решение] %s" % t[2:] for t in conflicts]
    body.append("")
    body.append("Источник: " + sources)
    msg = title + "\n\n" + "\n".join(body)
    subprocess.run(["git", "-C", ROOT, "add", "CLAUDE.md"], check=True)
    r = subprocess.run(["git", "-C", ROOT, "-c", "user.name=Никита Егоров", "-c", "user.email=" + OWN_EMAIL,
                        "commit", "-q", "-m", msg], capture_output=True, text=True)
    log("commit: %s %s" % (title, r.stderr.strip()[:200]))


# ---------- команды ----------

def run(force=False):
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    st = load_state()
    if "baseline" not in st:
        init(st)
        return
    collect(st)
    save_state(st)

    pend = st["pending"]
    if not pend:
        return
    oldest_h = (time.time() - min(p["added"] for p in pend)) / 3600
    today = datetime.now().strftime("%Y-%m-%d")
    calls = st["calls"].get(today, 0)
    if not force and len(pend) < MIN_SIGNALS and oldest_h < MAX_WAIT_H:
        return
    if not force and calls >= DAILY_CAP:
        log("daily cap reached")
        return

    batch, size = [], 0
    for p in pend:
        if batch and size + len(p["text"]) > BATCH_CHARS:
            break
        batch.append(p)
        size += len(p["text"])

    with open(RULES) as f:
        rules = f.read()
    with open(PROMPT) as f:
        system = f.read()
    user = "# ТЕКУЩИЙ ФАЙЛ ПРАВИЛ\n\n" + rules + "\n\n# ВЫДЕРЖКИ ИЗ СЕССИЙ\n\n" + "\n\n---\n\n".join(
        "[%s, %s]\n%s" % (p["agent"], p["date"], p["text"]) for p in batch)

    st["calls"] = {today: calls + 1}
    save_state(st)
    ops = parse_ops(call_model(system, user))
    if ops is None:
        log("no valid answer, batch kept")
        return

    agents = sorted({p["agent"] for p in batch})
    sources = ", ".join("%s ×%d" % (a, sum(1 for p in batch if p["agent"] == a)) for a in agents)
    res = apply_ops(ops, sources)
    if res:
        commit(res[0], res[1], sources)
    else:
        log("nothing new (%d excerpts)" % len(batch))
    st["pending"] = pend[len(batch):]
    save_state(st)


def init(st=None):
    st = st or load_state()
    for path, agent in all_sessions():
        try:
            msgs = read(path, agent)
            st["sessions"][path] = {"mtime": os.path.getmtime(path), "done": user_count(msgs) if msgs else 0}
        except Exception:
            pass
    st["baseline"] = time.time()
    save_state(st)
    log("init: %d sessions" % len(st["sessions"]))


def kick():
    if os.environ.get("RULES_SYNC_CHILD"):
        return
    try:
        info = json.loads(sys.stdin.read() or "{}")
    except Exception:
        info = {}
    log("kick: %s" % (info.get("hook_event_name") or "?"))
    # ждём QUIET_SEC, чтобы закрытая сессия дописала файл, затем обрабатываем
    subprocess.Popen(["/bin/sh", "-c", "sleep %d; exec /usr/bin/python3 -I '%s' run" % (QUIET_SEC + 5, __file__)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=dict(os.environ, RULES_SYNC_CHILD="1"))
    print("{}")


def status():
    st = load_state()
    collect(st)
    for p in st["pending"]:
        print("[%s %s] %s\n" % (p["agent"], p["date"], p["text"][:400]))
    print("в очереди: %d выдержек; вызовов сегодня: %s" % (
        len(st["pending"]), st["calls"].get(datetime.now().strftime("%Y-%m-%d"), 0)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "kick":
        kick()
    elif cmd == "init":
        init()
    elif cmd == "status":
        status()
    elif cmd == "run":
        run(force="--force" in sys.argv)
    else:
        print(__doc__)

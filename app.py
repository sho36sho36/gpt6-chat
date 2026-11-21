import datetime, hashlib, json, os, sqlite3, tempfile, time, traceback, uuid
import gradio as gr
from ddgs import DDGS
from openai import OpenAI

DB, OUT = "users.db", tempfile.mkdtemp()
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())
hp = lambda p: hashlib.sha256(p.encode()).hexdigest()
txt = lambda c: "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c) if isinstance(c, list) else str(c)

def db(q, a=(), f=False):
    with sqlite3.connect(DB) as c: return c.execute(q, a).fetchall() if f else None

db("CREATE TABLE IF NOT EXISTS users (u TEXT PRIMARY KEY, p TEXT, ok INT, adm INT)")
db("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, u TEXT, title TEXT, msgs TEXT, ts REAL)")
db("INSERT OR IGNORE INTO users VALUES ('sho36', ?, 1, 1)", (hp("shoma0121"),))
ulist = lambda: db("SELECT u, CASE WHEN ok=1 THEN '✅許可' ELSE '⛔停止' END FROM users WHERE u!='sho36'", f=True)
gchats = lambda u: [(r, r) for r in db("SELECT id, title FROM chats WHERE u=? ORDER BY ts DESC", (u,), True)]
lchat = lambda cid: json.loads(r) if (r := db("SELECT msgs FROM chats WHERE id=?", (cid,), True)) else []

def nchat(u):
    cid = uuid.uuid4().hex[:8]
    db("INSERT INTO chats VALUES (?, ?, ?, '[]', ?)", (cid, u, f"新規 {datetime.datetime.now().strftime('%m/%d %H:%M')}", time.time()))
    return gr.update(choices=gchats(u), value=cid), []

def dchat(u, cid):
    if cid: db("DELETE FROM chats WHERE id=? AND u=?", (cid, u))
    ch = gchats(u)
    return (gr.update(choices=ch, value=ch), lchat(ch)) if ch else nchat(u)

def reg(u, p):
    if not u or not p: return "入力してください"
    try: db("INSERT INTO users VALUES (?, ?, 0, 0)", (u.strip(), hp(p.strip()))); return "✅ 申請完了。許可をお待ちください。"
    except sqlite3.IntegrityError: return "❌ 登録済みです"

def login(u, p):
    hide = (None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [], gr.update(choices=[], value=None), [])
    try:
        r = db("SELECT p, ok, adm FROM users WHERE u=?", (u.strip(),), True) if u and p else []
        if not r or r != hp(p.strip()): return ("❌ 認証失敗",) + hide
        if not r: return ("⏳ 承認待ちです",) + hide
        un, adm = u.strip(), bool(r)
        ch = gchats(un)
        dd, ms = (gr.update(choices=ch, value=ch), lchat(ch)) if ch else nchat(un)
        return ("", un, adm, gr.update(visible=False), gr.update(visible=True), gr.update(visible=adm), ulist() if adm else [], dd, ms)
    except Exception:
        return (f"❌ エラー:\n```\n{traceback.format_exc()}\n```",) + hide

def mng(act, t, me=None):
    t = (t or "").strip()
    if act == "tog" and t and t != "sho36": db("UPDATE users SET ok = 1 - ok WHERE u=?", (t,))
    elif act == "del" and t and t != "sho36": db("DELETE FROM users WHERE u=?", (t,)); db("DELETE FROM chats WHERE u=?", (t,))
    elif act == "self" and me and me != "sho36": db("DELETE FROM users WHERE u=?", (me,)); db("DELETE FROM chats WHERE u=?", (me,))
    return ulist()

def sfile(name, content):
    p = os.path.join(OUT, os.path.basename(name))
    with open(p, "w", encoding="utf-8-sig" if p.endswith(".csv") else "utf-8") as f: f.write(content)
    return p

tools = [
    {"type": "function", "name": "search", "description": "Web検索(最大2回)", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"type": "function", "name": "make_file", "description": "ファイル生成", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}}, "required": ["name", "content"]}}
]

def cbatch(bid):
    if not bid: return "ID入力", None
    try:
        b = client.batches.retrieve(bid.strip())
        if b.status != "completed" or not b.output_file_id: return f"⏳ {b.status}", None
        d = json.loads(client.files.content(b.output_file_id).text.splitlines())
        t = "\n".join(c["text"] for i in d["response"]["body"]["output"] for c in i.get("content", []) if c.get("type") == "output_text")
        return f"✅ 完了:\n\n{t}", sfile(f"batch_{bid.strip()[:8]}.md", t)
    except Exception: return f"❌:\n```\n{traceback.format_exc()}\n```", None

def chat(msg, hist, tier, u, cid):
    if not msg or not msg.strip(): yield hist, "", None, gr.update(); return
    hist = list(hist or []) + [{"role": "user", "content": msg}, {"role": "assistant", "content": "考え中..."}]
    yield hist, "", None, gr.update()
    try:
        sp = f"現在日付:{datetime.date.today()}。検索最大2回。ファイル作成依頼時は必ず make_file ツールで生成し作成完了を案内。"
        msgs = [{"role": m["role"], "content": txt(m["content"])} for m in hist[:-1]]
        if tier == "batch":
            p = sfile("b.jsonl", json.dumps({"custom_id": "1", "method": "POST", "url": "/v1/responses", "body": {"model": "gpt-6-luna", "instructions": sp, "input": msgs}}, ensure_ascii=False))
            b = client.batches.create(input_file_id=client.files.create(file=open(p, "rb"), purpose="batch").id, endpoint="/v1/responses", completion_window="24h")
            hist[-1]["content"] = f"📦 Batch送信(50%オフ) ID: `{b.id}`"
            db("UPDATE chats SET msgs=?, ts=? WHERE id=?", (json.dumps(hist, ensure_ascii=False), time.time(), cid))
            yield hist, "", None, gr.update(); return
        t0, ft, it, ot, sc, rep, fout = time.perf_counter(), None, 0, 0, 0, "", None
        while True:
            calls = []
            with client.responses.stream(model="gpt-6-luna", instructions=sp, input=msgs, service_tier=tier, tools=tools if sc < 2 else [tools]) as st:
                for ev in st:
                    if ev.type == "response.output_text.delta": ft = ft or time.perf_counter(); rep += ev.delta; hist[-1]["content"] = rep; yield hist, "", fout, gr.update()
                    elif ev.type == "response.completed":
                        if ev.response.usage: it += ev.response.usage.input_tokens; ot += ev.response.usage.output_tokens
                        calls = [i for i in ev.response.output if i.type == "function_call"]
            if not calls: break
            for tc in calls:
                a = json.loads(tc.arguments)
                if tc.name == "search" and sc < 2:
                    sc += 1; hist[-1]["content"] = f"*🔍 検索({sc}/2): {a.get('q')}*\n\n" + rep; yield hist, "", fout, gr.update()
                    res = "\n".join(f"[{i}]{r.get('title')}({r.get('href')}):{r.get('body')}" for i, r in enumerate(DDGS().text(a.get("q", ""), region="jp-jp", max_results=5), 1)) or "なし"
                elif tc.name == "make_file": fout = sfile(a.get("name", "out.txt"), a.get("content", "")); res = "完了"
                else: res = "制限"
                msgs += [{"type": "function_call", "call_id": tc.call_id, "name": tc.name, "arguments": tc.arguments}, {"type": "function_call_output", "call_id": tc.call_id, "output": res}]
        hist[-1]["content"] = f"{rep}\n\n---\n`{tier}|検索:{sc}|入:{it} 出:{ot}|{time.perf_counter()-t0:.1f}s({ot/max(time.perf_counter()-(ft or t0),0.01):.1f}tok/s)`"
        if len(hist) == 2: db("UPDATE chats SET title=? WHERE id=?", (msg[:16], cid))
        db("UPDATE chats SET msgs=?, ts=? WHERE id=?", (json.dumps(hist, ensure_ascii=False), time.time(), cid))
        yield hist, "", fout, gr.update(choices=gchats(u), value=cid)
    except Exception:
        hist[-1]["content"] = f"❌:\n```\n{traceback.format_exc()}\n```"; yield hist, "", None, gr.update()

with gr.Blocks(title="GPT-6 Luna") as demo:
    ust, ast = gr.State(None), gr.State(False)
    with gr.Column() as vl:
        gr.Markdown("## GPT-6 Luna"); ui, pi = gr.Textbox(label="名前"), gr.Textbox(label="パスワード", type="password")
        with gr.Row(): bi, br = gr.Button("ログイン", variant="primary"), gr.Button("新規申請(初期不可)")
        ml = gr.Markdown()
    with gr.Column(visible=False) as vc:
        with gr.Row():
            cdd = gr.Dropdown(label="履歴", scale=3); bn, bdc = gr.Button("＋新規", scale=1), gr.Button("削除", scale=1)
            tier = gr.Dropdown(["flex", "default", "batch"], value="flex", label="モード", scale=1)
            bo, bdm = gr.Button("ログアウト", scale=1), gr.Button("退会", variant="stop", scale=1)
        try: cb = gr.Chatbot(height=480, type="messages")
        except TypeError: cb = gr.Chatbot(height=480)
        with gr.Row(): mi = gr.Textbox(show_label=False, placeholder="メッセージ...", scale=5); bs = gr.Button("送信", variant="primary", scale=1)
        dl = gr.File(label="📥 生成ファイル")
        with gr.Accordion("📦 Batch確認", open=False): bid, bchk, bres = gr.Textbox(label="Batch ID"), gr.Button("確認"), gr.Markdown()
    with gr.Accordion("👑 管理者(sho36)", visible=False) as va:
        tbl = gr.Dataframe(headers=["名前", "状態"], interactive=False)
        with gr.Row(): ti, bt, bd, bref = gr.Textbox(label="対象名"), gr.Button("許可/停止"), gr.Button("削除", variant="stop"), gr.Button("更新")

    ov = [ml, ust, ast, vl, vc, va, tbl, cdd, cb]
    br.click(reg, [ui, pi], ml); bi.click(login, [ui, pi], ov)
    bo.click(lambda: ("", None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [], gr.update(choices=[], value=None), []), None, ov)
    bdm.click(lambda u: (mng("self", None, u), "✅ 削除完了", None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [], gr.update(choices=[], value=None), [])[1:], ust, ov)
    bn.click(nchat, ust, [cdd, cb]); bdc.click(dchat, [ust, cdd], [cdd, cb]); cdd.change(lchat, cdd, cb)
    mi.submit(chat, [mi, cb, tier, ust, cdd], [cb, mi, dl, cdd]); bs.click(chat, [mi, cb, tier, ust, cdd], [cb, mi, dl, cdd])
    bt.click(lambda t: mng("tog", t), ti, tbl); bd.click(lambda t: mng("del", t), ti, tbl); bref.click(ulist, None, tbl); bchk.click(cbatch, bid, [bres, dl])

demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))

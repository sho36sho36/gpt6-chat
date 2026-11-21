import datetime, hashlib, json, os, sqlite3, tempfile, time, uuid
import gradio as gr
from ddgs import DDGS
from openai import OpenAI

DB, OUT = "users.db", tempfile.mkdtemp()
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())
h_pw = lambda p: hashlib.sha256(p.encode()).hexdigest()

def db(q, args=(), fetch=False):
    with sqlite3.connect(DB) as c:
        cur = c.execute(q, args)
        return cur.fetchall() if fetch else None

db("CREATE TABLE IF NOT EXISTS users (u TEXT PRIMARY KEY, p TEXT, ok INT, adm INT)")
db("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, u TEXT, title TEXT, msgs TEXT, ts REAL)")
db("INSERT OR IGNORE INTO users VALUES ('sho36', ?, 1, 1)", (h_pw("shoma0121"),))
users_list = lambda: db("SELECT u, CASE WHEN ok=1 THEN '✅ 許可' ELSE '⛔ 停止' END FROM users WHERE u!='sho36'", fetch=True)

def get_chats(u):
    rows = db("SELECT id, title FROM chats WHERE u=? ORDER BY ts DESC", (u,), True)
    return [(r[1], r[0]) for r in rows] if rows else []

def load_chat(cid):
    r = db("SELECT msgs FROM chats WHERE id=?", (cid,), True)
    return json.loads(r[0][0]) if r else []

def new_chat(u):
    cid = uuid.uuid4().hex[:8]
    db("INSERT INTO chats VALUES (?, ?, ?, '[]', ?)", (cid, u, f"新規 ({datetime.datetime.now().strftime('%m/%d %H:%M')})", time.time()))
    choices = get_chats(u)
    return gr.update(choices=choices, value=cid), []

def del_chat(u, cid):
    if cid: db("DELETE FROM chats WHERE id=? AND u=?", (cid, u))
    choices = get_chats(u)
    if not choices: return new_chat(u)
    return gr.update(choices=choices, value=choices[0][1]), load_chat(choices[0][1])

def register(u, p):
    if not u or not p: return "入力してください"
    try:
        db("INSERT INTO users VALUES (?, ?, 0, 0)", (u.strip(), h_pw(p.strip())))
        return "✅ 申請完了。管理者(sho36)の許可をお待ちください。"
    except sqlite3.IntegrityError: return "❌ 既に登録されています"

def login(u, p):
    hide = (None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [], gr.update(choices=[], value=None), [])
    r = db("SELECT p, ok, adm FROM users WHERE u=?", (u.strip(),), True) if u and p else []
    if not r or r[0][0] != h_pw(p.strip()): return ("❌ 名前かパスワードが違います",) + hide
    if not r[0][1]: return ("⏳ 承認待ち（利用停止中）です",) + hide
    un, adm = u.strip(), bool(r[0][2])
    choices = get_chats(un)
    if not choices:
        dd, msgs = new_chat(un)
    else:
        dd, msgs = gr.update(choices=choices, value=choices[0][1]), load_chat(choices[0][1])
    return "", un, adm, gr.update(visible=False), gr.update(visible=True), gr.update(visible=adm), users_list() if adm else [], dd, msgs

def manage(act, target, me=None):
    t = (target or "").strip()
    if act == "toggle" and t and t != "sho36": db("UPDATE users SET ok = 1 - ok WHERE u=?", (t,))
    elif act == "del" and t and t != "sho36":
        db("DELETE FROM users WHERE u=?", (t,))
        db("DELETE FROM chats WHERE u=?", (t,))
    elif act == "self" and me and me != "sho36":
        db("DELETE FROM users WHERE u=?", (me,))
        db("DELETE FROM chats WHERE u=?", (me,))
    return users_list()

def save_file(name, content):
    p = os.path.join(OUT, os.path.basename(name))
    with open(p, "w", encoding="utf-8-sig" if p.endswith(".csv") else "utf-8") as f: f.write(content)
    return p

tools = [
    {"type": "function", "name": "search", "description": "Web検索(最大2回)", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"type": "function", "name": "make_file", "description": "ダウンロード用ファイル生成", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}}, "required": ["name", "content"]}}
]

def check_batch(bid):
    if not bid: return "IDを入力してください", None
    b = client.batches.retrieve(bid.strip())
    if b.status != "completed" or not b.output_file_id: return f"⏳ 状態: `{b.status}`", None
    data = json.loads(client.files.content(b.output_file_id).text.splitlines()[0])
    txt = "\n".join(c["text"] for i in data["response"]["body"]["output"] for c in i.get("content", []) if c.get("type") == "output_text")
    return f"✅ 完了:\n\n{txt}", save_file(f"batch_{bid.strip()[:8]}.md", txt)

def chat(msg, hist, tier, u, cid):
    if not msg or not msg.strip(): yield hist, "", None, gr.update(); return
    hist = list(hist) + [{"role": "user", "content": msg}]
    sys_p = f"現在日付:{datetime.date.today()}。検索は最大2回まで。ファイル作成やデータ出力を頼まれたら必ず make_file ツールでファイルを生成し、チャットでは作成完了を案内してください。"
    msgs = [{"role": m["role"], "content": str(m["content"])} for m in hist]
    if tier == "batch":
        p = save_file("b.jsonl", json.dumps({"custom_id": "1", "method": "POST", "url": "/v1/responses", "body": {"model": "gpt-6-luna", "instructions": sys_p, "input": msgs}}, ensure_ascii=False))
        b = client.batches.create(input_file_id=client.files.create(file=open(p, "rb"), purpose="batch").id, endpoint="/v1/responses", completion_window="24h")
        hist.append({"role": "assistant", "content": f"📦 Batch送信完了 (50%オフ)\nID: `{b.id}`"})
        db("UPDATE chats SET msgs=?, ts=? WHERE id=?", (json.dumps(hist, ensure_ascii=False), time.time(), cid))
        yield hist, "", None, gr.update(); return

    t0, ft, it, ot, sc, rep, f_out = time.perf_counter(), None, 0, 0, 0, "", None
    hist.append({"role": "assistant", "content": "考え中..."})
    yield hist, "", f_out, gr.update()

    while True:
        calls = []
        with client.responses.stream(model="gpt-6-luna", instructions=sys_p, input=msgs, service_tier=tier, tools=tools if sc < 2 else [tools[1]]) as st:
            for ev in st:
                if ev.type == "response.output_text.delta":
                    ft = ft or time.perf_counter(); rep += ev.delta; hist[-1]["content"] = rep; yield hist, "", f_out, gr.update()
                elif ev.type == "response.completed":
                    if ev.response.usage: it += ev.response.usage.input_tokens; ot += ev.response.usage.output_tokens
                    calls = [i for i in ev.response.output if i.type == "function_call"]
        if not calls: break
        for tc in calls:
            a = json.loads(tc.arguments)
            if tc.name == "search" and sc < 2:
                sc += 1; hist[-1]["content"] = f"*🔍 検索({sc}/2): {a.get('q')}*\n\n" + rep; yield hist, "", f_out, gr.update()
                res = "\n".join(f"[{i}]{r.get('title')}({r.get('href')}):{r.get('body')}" for i, r in enumerate(DDGS().text(a.get("q", ""), region="jp-jp", max_results=5), 1)) or "なし"
            elif tc.name == "make_file":
                f_out = save_file(a.get("name", "out.txt"), a.get("content", ""))
                res = "ファイル生成完了"
            else: res = "制限超過"
            msgs += [{"type": "function_call", "call_id": tc.call_id, "name": tc.name, "arguments": tc.arguments}, {"type": "function_call_output", "call_id": tc.call_id, "output": res}]
    dt = time.perf_counter() - t0
    hist[-1]["content"] = f"{rep}\n\n---\n`{tier} | 検索:{sc} | 入:{it} 出:{ot} | {dt:.1f}s ({ot/max(time.perf_counter()-(ft or t0),0.01):.1f}tok/s)`"
    if len(hist) == 2: db("UPDATE chats SET title=? WHERE id=?", (msg[:16], cid))
    db("UPDATE chats SET msgs=?, ts=? WHERE id=?", (json.dumps(hist, ensure_ascii=False), time.time(), cid))
    yield hist, "", f_out, gr.update(choices=get_chats(u), value=cid)

with gr.Blocks(title="GPT-6 Luna") as demo:
    u_st, a_st = gr.State(None), gr.State(False)
    with gr.Column() as v_login:
        gr.Markdown("## GPT-6 Luna")
        u_in, p_in = gr.Textbox(label="名前"), gr.Textbox(label="パスワード", type="password")
        with gr.Row(): b_in, b_reg = gr.Button("ログイン", variant="primary"), gr.Button("新規申請(初期不可)")
        msg_l = gr.Markdown()
    with gr.Column(visible=False) as v_chat:
        with gr.Row():
            c_dd = gr.Dropdown(label="チャット履歴", scale=3)
            b_new, b_del_c = gr.Button("＋新規", scale=1), gr.Button("履歴削除", scale=1)
            tier = gr.Dropdown(["flex", "default", "batch"], value="flex", label="モード", scale=1)
            b_out, b_del_me = gr.Button("ログアウト", scale=1), gr.Button("退会", variant="stop", scale=1)
        cb = gr.Chatbot(height=480)
        with gr.Row():
            m_in = gr.Textbox(show_label=False, placeholder="メッセージを入力...", scale=5)
            b_send = gr.Button("送信", variant="primary", scale=1)
        dl = gr.File(label="📥 生成ファイル")
        with gr.Accordion("📦 Batch確認", open=False):
            b_id, b_chk, b_res = gr.Textbox(label="Batch ID"), gr.Button("確認"), gr.Markdown()
    with gr.Accordion("👑 管理者(sho36)", visible=False) as v_adm:
        tbl = gr.Dataframe(headers=["名前", "状態"], interactive=False)
        with gr.Row(): t_in, b_tog, b_del, b_ref = gr.Textbox(label="対象名"), gr.Button("許可/停止"), gr.Button("削除", variant="stop"), gr.Button("更新")

    out_views = [msg_l, u_st, a_st, v_login, v_chat, v_adm, tbl, c_dd, cb]
    b_reg.click(register, [u_in, p_in], msg_l)
    b_in.click(login, [u_in, p_in], out_views)
    b_out.click(lambda: ("", None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [], gr.update(choices=[], value=None), []), None, out_views)
    b_del_me.click(lambda u: (manage("self", None, u), "✅ 削除完了", None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [], gr.update(choices=[], value=None), [])[1:], u_st, out_views)
    b_new.click(new_chat, u_st, [c_dd, cb])
    b_del_c.click(del_chat, [u_st, c_dd], [c_dd, cb])
    c_dd.change(load_chat, c_dd, cb)
    m_in.submit(chat, [m_in, cb, tier, u_st, c_dd], [cb, m_in, dl, c_dd])
    b_send.click(chat, [m_in, cb, tier, u_st, c_dd], [cb, m_in, dl, c_dd])
    b_tog.click(lambda t: manage("toggle", t), t_in, tbl)
    b_del.click(lambda t: manage("del", t), t_in, tbl)
    b_ref.click(users_list, None, tbl)
    b_chk.click(check_batch, b_id, [b_res, dl])

demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))

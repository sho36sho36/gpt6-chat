import datetime, hashlib, json, os, sqlite3, tempfile, time
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
db("INSERT OR IGNORE INTO users VALUES ('sho36', ?, 1, 1)", (h_pw("shoma0121"),))
users_list = lambda: db("SELECT u, CASE WHEN ok=1 THEN '✅ 許可' ELSE '⛔ 停止' END FROM users WHERE u!='sho36'", fetch=True)

def register(u, p):
    if not u or not p: return "入力してください"
    try:
        db("INSERT INTO users VALUES (?, ?, 0, 0)", (u.strip(), h_pw(p.strip())))
        return "✅ 申請完了。管理者(sho36)の許可をお待ちください。"
    except sqlite3.IntegrityError:
        return "❌ 既に登録されています"

def login(u, p):
    hide = (None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [])
    r = db("SELECT p, ok, adm FROM users WHERE u=?", (u.strip(),), True) if u and p else []
    if not r or r[0][0] != h_pw(p.strip()): return ("❌ 名前かパスワードが違います",) + hide
    if not r[0][1]: return ("⏳ 承認待ち（利用停止中）です",) + hide
    adm = bool(r[0][2])
    return "", u.strip(), adm, gr.update(visible=False), gr.update(visible=True), gr.update(visible=adm), users_list() if adm else []

def manage(act, target, me=None):
    t = (target or "").strip()
    if act == "toggle" and t and t != "sho36": db("UPDATE users SET ok = 1 - ok WHERE u=?", (t,))
    elif act == "del" and t and t != "sho36": db("DELETE FROM users WHERE u=?", (t,))
    elif act == "self" and me and me != "sho36": db("DELETE FROM users WHERE u=?", (me,))
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

def chat(msg, hist, tier):
    sys_p = f"現在日付:{datetime.date.today()}。検索は最大2回まで。ファイル作成やデータ出力を頼まれたら必ず make_file ツールでファイルを生成し、チャットでは作成完了を案内してください。"
    msgs = [{"role": m["role"], "content": m["content"]} for m in hist] + [{"role": "user", "content": msg}]
    if tier == "batch":
        p = save_file("b.jsonl", json.dumps({"custom_id": "1", "method": "POST", "url": "/v1/responses", "body": {"model": "gpt-6-luna", "instructions": sys_p, "input": msgs}}, ensure_ascii=False))
        b = client.batches.create(input_file_id=client.files.create(file=open(p, "rb"), purpose="batch").id, endpoint="/v1/responses", completion_window="24h")
        yield f"📦 Batch送信完了 (50%オフ)\nID: `{b.id}`", None; return

    t0, ft, it, ot, sc, rep, f_out = time.perf_counter(), None, 0, 0, 0, "", None
    while True:
        calls = []
        with client.responses.stream(model="gpt-6-luna", instructions=sys_p, input=msgs, service_tier=tier, tools=tools if sc < 2 else [tools[1]]) as st:
            for ev in st:
                if ev.type == "response.output_text.delta":
                    ft = ft or time.perf_counter(); rep += ev.delta; yield rep, f_out
                elif ev.type == "response.completed":
                    if ev.response.usage: it += ev.response.usage.input_tokens; ot += ev.response.usage.output_tokens
                    calls = [i for i in ev.response.output if i.type == "function_call"]
        if not calls: break
        for tc in calls:
            a = json.loads(tc.arguments)
            if tc.name == "search" and sc < 2:
                sc += 1; yield f"*🔍 検索({sc}/2): {a.get('q')}*\n\n" + rep, f_out
                res = "\n".join(f"[{i}]{r.get('title')}({r.get('href')}):{r.get('body')}" for i, r in enumerate(DDGS().text(a.get("q", ""), region="jp-jp", max_results=5), 1)) or "なし"
            elif tc.name == "make_file":
                f_out = save_file(a.get("name", "out.txt"), a.get("content", ""))
                res = "ファイル生成完了"
            else: res = "制限超過"
            msgs += [{"type": "function_call", "call_id": tc.call_id, "name": tc.name, "arguments": tc.arguments}, {"type": "function_call_output", "call_id": tc.call_id, "output": res}]
    dt = time.perf_counter() - t0
    yield f"{rep}\n\n---\n`{tier} | 検索:{sc} | 入:{it} 出:{ot} | {dt:.1f}s ({ot/max(time.perf_counter()-(ft or t0),0.01):.1f}tok/s)`", f_out

with gr.Blocks(title="GPT-6 Luna") as demo:
    u_st, a_st = gr.State(None), gr.State(False)
    with gr.Column() as v_login:
        gr.Markdown("## GPT-6 Luna")
        u_in, p_in = gr.Textbox(label="名前"), gr.Textbox(label="パスワード", type="password")
        with gr.Row(): b_in, b_reg = gr.Button("ログイン", variant="primary"), gr.Button("新規申請(初期不可)")
        msg_l = gr.Markdown()
    with gr.Column(visible=False) as v_chat:
        with gr.Row():
            tier = gr.Dropdown(["flex", "default", "batch"], value="flex", label="モード")
            b_out, b_del_me = gr.Button("ログアウト"), gr.Button("自分のアカウント削除", variant="stop")
        dl = gr.File(label="📥 生成ファイル")
        gr.ChatInterface(chat, additional_inputs=[tier], additional_outputs=[dl])
        with gr.Accordion("📦 Batch確認", open=False):
            b_id, b_chk, b_res = gr.Textbox(label="Batch ID"), gr.Button("確認"), gr.Markdown()
    with gr.Accordion("👑 管理者(sho36)", visible=False) as v_adm:
        tbl = gr.Dataframe(headers=["名前", "状態"], interactive=False)
        with gr.Row(): t_in, b_tog, b_del, b_ref = gr.Textbox(label="対象名"), gr.Button("許可/停止"), gr.Button("削除", variant="stop"), gr.Button("更新")

    out_views = [msg_l, u_st, a_st, v_login, v_chat, v_adm, tbl]
    b_reg.click(register, [u_in, p_in], msg_l)
    b_in.click(login, [u_in, p_in], out_views)
    b_out.click(lambda: ("", None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), []), None, out_views)
    b_del_me.click(lambda u: (manage("self", None, u), "✅ 削除完了", None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), [])[1:], u_st, out_views)
    b_tog.click(lambda t: manage("toggle", t), t_in, tbl)
    b_del.click(lambda t: manage("del", t), t_in, tbl)
    b_ref.click(users_list, None, tbl)
    b_chk.click(check_batch, b_id, [b_res, dl])

demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))

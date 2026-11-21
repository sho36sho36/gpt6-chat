import datetime, hashlib, json, os, sqlite3, time
import gradio as gr
from ddgs import DDGS
from openai import OpenAI

DB_PATH = "users.db"
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, pw TEXT, approved INT, admin INT)")
    conn.execute("INSERT OR REPLACE INTO users VALUES ('sho36', ?, 1, 1)", (hash_pw("shoma0121"),))
    conn.commit()
    conn.close()

init_db()

def register(u, p):
    if not u or not p: return "名前とパスワードを入力してください"
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO users VALUES (?, ?, 0, 0)", (u.strip(), hash_pw(p.strip())))
        conn.commit()
        return "✅ 申請完了！管理者(sho36)の許可をお待ちください。"
    except sqlite3.IntegrityError:
        return "❌ その名前は既に登録されています。"
    finally:
        conn.close()

def login(u, p):
    if not u or not p:
        return None, False, "❌ 名前とパスワードを入力してください", gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), []
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT pw, approved, admin FROM users WHERE username=?", (u.strip(),)).fetchone()
    conn.close()
    if not row or row[0] != hash_pw(p.strip()):
        return None, False, "❌ 名前またはパスワードが違います", gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), []
    if row[1] == 0:
        return None, False, "⏳ 承認待ち（利用停止中）です。管理者(sho36)の許可をお待ちください。", gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), []
    is_admin = bool(row[2])
    return u.strip(), is_admin, "", gr.update(visible=False), gr.update(visible=True), gr.update(visible=is_admin), get_user_list() if is_admin else []

def get_user_list():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT username, CASE WHEN approved=1 THEN '✅ 許可中' ELSE '⛔ 停止・申請中' END FROM users WHERE username!='sho36'").fetchall()
    conn.close()
    return rows

def toggle_user(target):
    if not target: return get_user_list()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET approved = 1 - approved WHERE username=?", (target.strip(),))
    conn.commit()
    conn.close()
    return get_user_list()

def web_search(query):
    try:
        res = list(DDGS().text(query, region="jp-jp", max_results=5))
        return "\n\n".join(f"[{i}] {r.get('title')}\nURL: {r.get('href')}\n内容: {r.get('body')}" for i, r in enumerate(res, 1)) or "結果なし"
    except Exception as e:
        return f"検索エラー: {e}"

tools = [{"type": "function", "name": "web_search", "description": "最新情報や事実確認用のWeb検索",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]

def chat(msg, history, tier):
    today = datetime.date.today().strftime("%Y年%m月%d日")
    sys_prompt = f"現在の日付は{today}です。検索結果のURLと内容から信頼性を判断し、自信を持って分かりやすく回答してください。"
    msgs = [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": msg}]
    t0, first_t, in_tok, out_tok, search_cnt = time.perf_counter(), None, 0, 0, 0
    reply = ""

    while True:
        calls = []
        with client.responses.stream(model="gpt-6-luna", instructions=sys_prompt, input=msgs, service_tier=tier, tools=tools if search_cnt < 2 else []) as stream:
            for ev in stream:
                if ev.type == "response.output_text.delta":
                    first_t = first_t or time.perf_counter()
                    reply += ev.delta
                    yield reply
                elif ev.type == "response.completed":
                    if ev.response.usage:
                        in_tok += ev.response.usage.input_tokens
                        out_tok += ev.response.usage.output_tokens
                    calls = [i for i in ev.response.output if i.type == "function_call"]
        if not calls:
            break
        for tc in calls:
            search_cnt += 1
            q = json.loads(tc.arguments).get("query", "")
            yield f"*🔍 Web検索中 ({search_cnt}/2): `{q}` ...*\n\n" + reply
            msgs.append({"type": "function_call", "call_id": tc.call_id, "name": tc.name, "arguments": tc.arguments})
            msgs.append({"type": "function_call_output", "call_id": tc.call_id, "output": web_search(q)})

    dt = time.perf_counter() - t0
    tps = out_tok / max(time.perf_counter() - (first_t or t0), 0.01)
    yield f"{reply}\n\n---\n`モード: {tier} | 検索: {search_cnt}回 | 入力: {in_tok} | 出力: {out_tok} | {dt:.1f}秒 ({tps:.1f} tok/s)`"

with gr.Blocks(title="GPT-6 Luna") as demo:
    user_state, admin_state = gr.State(None), gr.State(False)

    with gr.Column(visible=True) as login_box:
        gr.Markdown("## GPT-6 Luna ログイン / 利用申請")
        u_in = gr.Textbox(label="名前")
        p_in = gr.Textbox(label="パスワード", type="password")
        with gr.Row():
            btn_login = gr.Button("ログイン", variant="primary")
            btn_reg = gr.Button("新規利用申請 (初期は利用不可)")
        auth_msg = gr.Markdown()

    with gr.Column(visible=False) as chat_box:
        with gr.Row():
            tier_dd = gr.Dropdown(["default", "flex"], value="default", label="処理モード", scale=1)
            btn_logout = gr.Button("ログアウト", scale=0)
        gr.ChatInterface(fn=chat, additional_inputs=[tier_dd])

    with gr.Accordion("管理者パネル (sho36専用)", open=True, visible=False) as admin_box:
        user_table = gr.Dataframe(headers=["ユーザー名", "状態"], interactive=False)
        with gr.Row():
            target_in = gr.Textbox(label="切り替えるユーザー名")
            btn_toggle = gr.Button("許可 / 停止 を切り替え", variant="primary")
            btn_refresh = gr.Button("一覧を更新")

    btn_reg.click(register, [u_in, p_in], auth_msg)
    btn_login.click(login, [u_in, p_in], [user_state, admin_state, auth_msg, login_box, chat_box, admin_box, user_table])
    btn_logout.click(lambda: (None, False, gr.update(visible=True), gr.update(visible=False), gr.update(visible=False)), None, [user_state, admin_state, login_box, chat_box, admin_box])
    btn_toggle.click(toggle_user, target_in, user_table)
    btn_refresh.click(get_user_list, None, user_table)

demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))

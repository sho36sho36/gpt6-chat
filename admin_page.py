import sqlite3
import gradio as gr

DB = "users.db"

def db(q, a=(), f=False):
    c = sqlite3.connect(DB)
    try:
        cur = c.execute(q, a)
        res = cur.fetchall() if f else None
        c.commit()
        return res
    finally:
        c.close()

def init_tok_db():
    db("CREATE TABLE IF NOT EXISTS tokens (u TEXT PRIMARY KEY, in_tok INT DEFAULT 0, out_tok INT DEFAULT 0)")

def add_tokens(u, it, ot):
    init_tok_db()
    if not u: return
    db("INSERT OR IGNORE INTO tokens VALUES (?, 0, 0)", (u,))
    db("UPDATE tokens SET in_tok = in_tok + ?, out_tok = out_tok + ? WHERE u = ?", (it, ot, u))

def get_admin_table():
    init_tok_db()
    q = """
    SELECT u.u,
           CASE WHEN u.ok=1 THEN '✅許可' ELSE '⛔停止' END,
           COALESCE(t.in_tok, 0),
           COALESCE(t.out_tok, 0),
           COALESCE(t.in_tok, 0) + COALESCE(t.out_tok, 0)
    FROM users u
    LEFT JOIN tokens t ON u.u = t.u
    """
    return db(q, f=True)

def mng(act, t):
    t = (t or "").strip()
    if act == "tog" and t and t != "sho36":
        db("UPDATE users SET ok = 1 - ok WHERE u=?", (t,))
    elif act == "del" and t and t != "sho36":
        db("DELETE FROM users WHERE u=?", (t,))
        db("DELETE FROM chats WHERE u=?", (t,))
        db("DELETE FROM tokens WHERE u=?", (t,))
    return get_admin_table()

def mount_admin_page(demo, secret_path="/admin-sho36-panel"):
    init_tok_db()
    with demo.route("管理ページ", secret_path, show_in_navbar=False):
        gr.Markdown("## 👑 管理者専用ページ (sho36)")
        tbl = gr.Dataframe(
            headers=["ユーザー名", "状態", "入力トークン", "出力トークン", "合計トークン"],
            value=get_admin_table,
            interactive=False
        )
        with gr.Row():
            ti = gr.Textbox(label="対象ユーザー名")
            bt = gr.Button("許可 / 停止 を切り替え", variant="primary")
            bd = gr.Button("アカウント完全削除", variant="stop")
            bref = gr.Button("最新情報に更新")

        bt.click(lambda t: mng("tog", t), ti, tbl)
        bd.click(lambda t: mng("del", t), ti, tbl)
        bref.click(get_admin_table, None, tbl)

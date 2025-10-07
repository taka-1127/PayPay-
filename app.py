import discord
import os
import json
import requests
import psycopg2 
# import os 
# import threading
# from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from typing import List, Dict, Any

# main.py の内容をインポートしている想定 (PayPaython_mobile が main.py のこと)
# 実際の環境に合わせて 'from PayPaython_mobile import *' を調整してください
try:
    from PayPaython_mobile import *
except ImportError:
    print("警告: PayPaython_mobile (main.py) が見つからないか、インポートできません。")
    # 代替のダミークラスを定義 (PayPaython_mobile が存在しない場合でも app.py を実行可能にするため)
    class PayPayLoginError(Exception): pass
    class PayPayError(Exception): pass
    class PayPay:
        def __init__(self, phone, passwd, duuid, cuuid, actoken, proxy):
            self.access_token = actoken if actoken != "none" else None
            self.refresh_token = "dummy_rftoken"
        def token_refresh(self, rftoken): 
            print("ダミー: トークンをリフレッシュしました。")
            self.access_token = "dummy_new_actoken"
            self.refresh_token = "dummy_new_rftoken"
        def alive(self):
            print("ダミー: alive 実行")



# Renderが提供する環境変数PORTがあればそれを使用し、なければ8000を使用
PORT = int(os.environ.get("PORT", 8000))

class HealthCheckHandler(BaseHTTPRequestHandler):
    """Renderのヘルスチェックに応答するためのシンプルなハンドラー"""
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web_server():
    """バックグラウンドでダミーのWebサーバーを実行する"""
    server_address = ('0.0.0.0', PORT)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print(f"Starting dummy web server on port {PORT} for Render health check.")
    httpd.serve_forever()

# --- Discordクライアントとコマンドツリーの設定 ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

# --- 定数とグローバル変数の設定 ---
# トークンは環境変数から取得
token = os.environ.get("DISCORD_BOT_TOKEN") 
# ご要望の管理者ID (このIDのみ /paypay確認 が可能)
ADMIN_USER_ID = 1119588177448013965 
# メイン操作パネルを使用できる管理者リスト (必要に応じて追加)
admins = [ADMIN_USER_ID] 

# 現在操作対象のアカウントID (on_readyでDBから取得し設定される)
pay_id = None 


# --- データベース関連のヘルパー関数 ---

def get_db_connection():
    """環境変数からDATABASE_URLを取得し、PostgreSQLへの接続を確立する"""
    # RenderではDATABASE_URLとして提供される
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        # 環境変数が設定されていない場合は致命的なエラー
        raise Exception("Error: DATABASE_URL environment variable not set. Bot cannot connect to DB.")
    
    # RenderのURLフォーマット（postgres://...）をpsycopg2が解釈できる形に変換
    url = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        database=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port,
        sslmode='require' # RenderではSSL接続が必須
    )
    return conn

def init_db():
    """データベーステーブルを初期化（存在しない場合のみ作成）する"""
    conn = get_db_connection()
    cur = conn.cursor()
    # PayPayアカウント情報を保持するテーブルを定義
    cur.execute("""
        CREATE TABLE IF NOT EXISTS paypay_accounts (
            id TEXT PRIMARY KEY,       -- アカウント識別子 (例: ユーザーID)
            phone TEXT NOT NULL,       -- 電話番号
            pass TEXT NOT NULL,        -- パスワード (暗号化を強く推奨)
            duuid TEXT,                -- Device UUID
            cuuid TEXT,                -- Client UUID
            actoken TEXT,              -- Access Token
            rftoken TEXT,              -- Refresh Token
            proxy TEXT                 -- プロキシ情報
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_all_account_ids() -> List[str]:
    """データベースから登録されている全てのアカウントIDのリストを取得する"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM paypay_accounts ORDER BY id")
    ids = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return ids

def get_account_data(account_id: str) -> Dict[str, Any]:
    """指定されたアカウントIDの全データをデータベースから取得する"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM paypay_accounts WHERE id = %s", (account_id,))
    row = cur.fetchone()
    
    data = None
    if row:
        # カラム名を取得し、辞書形式で返す
        col_names = [desc[0] for desc in cur.description]
        data = dict(zip(col_names, row))
        
    cur.close()
    conn.close()
    return data

def get_all_accounts() -> List[Dict[str, Any]]:
    """データベースから登録されている全アカウントの全データを取得する"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM paypay_accounts")
    # カラム名を取得し、辞書のリストとして返す
    col_names = [desc[0] for desc in cur.description]
    accounts = [dict(zip(col_names, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return accounts

# --- PayPay認証トークンのリフレッシュ関数 ---

def paypay_refresh(udata: dict, idd: str):
    """
    リフレッシュトークンを使用してトークンを更新し、DBに保存する。
    """
    try:
        # PayPaython_mobile の PayPay クラスを初期化
        paypay = PayPay(udata["phone"], udata["pass"], udata["duuid"], udata["cuuid"], udata["actoken"], udata["proxy"])
    except Exception as e:
        return f"PayPay初期化エラー: {e}"
    
    try:
        # トークンをリフレッシュ
        paypay.token_refresh(udata["rftoken"])
    except Exception as e:
        return f"トークンリフレッシュエラー: {e}"
    
    # 更新されたトークンをデータに反映
    udata["actoken"] = paypay.access_token
    udata["rftoken"] = paypay.refresh_token
    
    # ★ データベースへの更新処理
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE paypay_accounts
            SET actoken = %s, rftoken = %s
            WHERE id = %s
        """, (udata["actoken"], udata["rftoken"], idd))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB Update Error during refresh: {e}")
        return f"DB更新エラー: {e}"

    paypay.alive()
    return "ok"


# --- Discordイベントハンドラー ---

@client.event
async def on_ready():
    """Botが起動し、Discordに接続したときに実行される"""
    global pay_id
    
    # データベース初期化
    try:
        init_db()
        print("データベースの初期化に成功しました。")
    except Exception as e:
        print(f"致命的エラー: データベースの初期化に失敗しました。BOTは正しく機能しません: {e}")
        
    # DBからアカウントIDリストを取得し、pay_idを初期設定
    try:
        account_ids = get_all_account_ids()
        if account_ids:
            # 最初のIDをデフォルトの操作対象アカウントとする
            pay_id = account_ids[0]
            print(f"デフォルトのPayPayアカウントID: {pay_id}")
        else:
            pay_id = "No Account"
            print("注意: データベースに登録されたPayPayアカウントがありません。")
            
    except Exception as e:
        print(f"アカウントIDの取得エラー: {e}")
        pay_id = "DB Error"
        
    # Discordプレゼンスとコマンドの同期
    await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name='paypay管理'))
    print(client.user)
    await tree.sync()


# --- Discordスラッシュコマンド ---

@tree.command(name="paypay確認", description="全てのPayPayアカウント情報を確認します（指定管理者限定）")
async def paypay_check(interaction: discord.Interaction):
    """管理者限定で全PayPayアカウントの情報をEmbedで表示するコマンド"""
    
    # 1. 権限確認 (ご要望のIDのみ許可)
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("このコマンドは指定された管理者のみ使用可能です。", ephemeral=True)
        return

    # 応答が遅れる可能性があるのでdefer
    await interaction.response.defer(ephemeral=True) 

    # 2. DBから全アカウント情報を取得
    try:
        accounts = get_all_accounts()
    except Exception as e:
        print(f"DBからの情報取得エラー: {e}")
        await interaction.followup.send("データベースからの情報取得中にエラーが発生しました。", ephemeral=True)
        return

    if not accounts:
        await interaction.followup.send("現在、登録されているPayPayアカウントはありません。", ephemeral=True)
        return

    # 3. Embedの作成と情報の整形
    embed = discord.Embed(
        title="✨ PayPay 登録アカウントリスト",
        description=f"合計 **{len(accounts)}** 件のアカウントが登録されています。",
        color=discord.Colour.dark_purple()
    )

    # ご要望のマスク用文字列
    PHONE_MASK = "================="
    PASS_MASK = "============="
    
    for i, account in enumerate(accounts):
        
        # None値は 'なし' に変換し、UUIDは先頭8文字に省略
        duuid_display = account.get('duuid') or 'なし'
        cuuid_display = account.get('cuuid') or 'なし'
        proxy_display = account.get('proxy') or 'なし'

        account_info = (
            f"**PayPay ID:** `{account['id']}`\n"
            f"1. 電話番号   ：`{PHONE_MASK}`\n" 
            f"   パスワード：`{PASS_MASK}`\n"   
            f"2. Device UUID：`{duuid_display[:8]}...`\n" 
            f"3. Client UUID：`{cuuid_display[:8]}...`\n" 
            f"4. Access Token：`{'あり' if account.get('actoken') else 'なし'}`\n"
            f"5. Refresh Token：`{'あり' if account.get('rftoken') else 'なし'}`\n"
            f"6. Proxy：`{proxy_display}`"
        )
        embed.add_field(
            name=f"アカウント #{i+1}",
            value=account_info,
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True) # ephemeral=Trueで、管理者のみが閲覧できるようにする


@tree.command(name="paypay", description="PayPay操作パネルを表示します")
async def paypay_command(interaction: discord.Interaction):
    """PayPay操作パネル（ボタン）を表示する"""
    global pay_id
    
    # パネルを使用できるのはadminsリスト内のユーザーのみ
    if interaction.user.id not in admins:
        await interaction.response.send_message("このコマンドは許可されていません。", ephemeral=True)
        return

    # 現在選択されているアカウントの情報を確認
    if pay_id in ["No Account", "DB Error"]:
        await interaction.response.send_message(f"エラー: 操作可能なアカウントがありません。（状態: {pay_id}）", ephemeral=True)
        return
        
    view = discord.ui.View(timeout=None)
    
    # 各種ボタンの定義 
    buttons = [
        ("💰 銀行送金", discord.ButtonStyle.success, "bank_send_btn"),
        ("📱 請求書支払い", discord.ButtonStyle.success, "invoice_btn"),
        ("📩 請求リンクに送金", discord.ButtonStyle.success, "send_invoice_btn"),
        ("💳 直接送金", discord.ButtonStyle.success, "direct_send_btn"),
        ("📤 送金リンク作成", discord.ButtonStyle.success, "send_btn"),
        ("📥 リンク受取", discord.ButtonStyle.secondary, "receive_btn"),
        ("❌ リンクキャンセル", discord.ButtonStyle.danger, "cancel_btn"),
        ("✅ 残高確認", discord.ButtonStyle.secondary, "check_balance_btn"), # 新規追加
        ("🔄 アカウント切り替え", discord.ButtonStyle.secondary, "refresh_btn"),
    ]

    for label, style, cid in buttons:
        btn = discord.ui.Button(label=label, style=style, custom_id=cid)

        # コールバック関数を生成
        async def make_callback(lbl):
            async def cb(interact):
                if interact.user.id not in admins:
                    await interact.response.send_message("この操作は許可されていません。", ephemeral=True)
                    return
                await handle_button(interact, lbl)
            return cb

        btn.callback = await make_callback(label)
        view.add_item(btn)
        
    emb = discord.Embed(
        title="PayPay操作パネル",
        description=f"選択中のPayPayアカウント:\n**{pay_id}**",
        color=discord.Colour.blue()
    )
    
    # 応答
    await interaction.response.send_message(embed=emb, view=view, ephemeral=True)


# --- ボタン操作ハンドラー ---

async def handle_button(interaction: discord.Interaction, label: str):
    """ボタンが押された時の処理をここに実装"""
    global pay_id
    
    # 応答を遅延させる (複雑な処理がある場合)
    await interaction.response.defer(ephemeral=True)

    if label == "🔄 アカウント切り替え":
        # アカウント切り替えロジック
        try:
            account_ids = get_all_account_ids()
            if not account_ids:
                await interaction.followup.send("切り替え可能なアカウントがありません。", ephemeral=True)
                return
                
            # 現在の pay_id の次のアカウントに切り替える
            try:
                current_index = account_ids.index(pay_id)
                next_index = (current_index + 1) % len(account_ids)
                pay_id = account_ids[next_index]
                await interaction.followup.send(f"アカウントを切り替えました。新しいアカウント:\n**{pay_id}**", ephemeral=True)
            except ValueError:
                # pay_id がリストに見つからない場合
                pay_id = account_ids[0]
                await interaction.followup.send(f"アカウントをリストの先頭に設定しました。:\n**{pay_id}**", ephemeral=True)
                
        except Exception as e:
            await interaction.followup.send(f"アカウント切り替え処理中にエラーが発生しました: {e}", ephemeral=True)

    elif label == "✅ 残高確認":
        # 残高確認ロジック (pay_id のデータを使用して処理を実行)
        data = get_account_data(pay_id)
        if not data:
            await interaction.followup.send(f"アカウントID **{pay_id}** のデータが見つかりません。", ephemeral=True)
            return
            
        # ここに PayPaython_mobile を使った残高確認処理を記述
        
        # トークンリフレッシュの必要性チェック（ここでは省略）
        # paypay_refresh(data, pay_id) 

        # PayPay(data["phone"],...).get_balance() などを呼び出す
        
        await interaction.followup.send(f"アカウント **{pay_id}** の残高確認をしました。\n(結果は PayPaython_mobile に実装が必要です)", ephemeral=True)

    else:
        # 他のボタン処理
        await interaction.followup.send(f"ボタン **{label}** の処理は未実装です。PayPaython_mobile に対応するロジックを実装してください。", ephemeral=True)

if __name__ == "__main__":
    # Webサーバーを別スレッドで起動
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True # メインスレッドが終了したら一緒に終了する
    web_thread.start()

# --- ボットの実行 ---

if token:
    try:
        # Discord Botを起動
        client.run(token)
    except discord.HTTPException as e:
        print(f"Discord接続エラー: トークンが無効な可能性があります。\n{e}")
    except Exception as e:
        print(f"致命的なエラーが発生しました: {e}")
else:
    print("Error: DISCORD_BOT_TOKEN environment variable not set. Bot will not run.")
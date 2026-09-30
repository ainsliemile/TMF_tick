import os
import datetime as dt
import pandas as pd
import requests
import zipfile
import io
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import yfinance as yf

print("=== 🚀 開始執行每日微台量化排程 ===")

# ==========================================
# 1. 自動下載期交所「今日」Tick 資料並存檔
# ==========================================
os.makedirs('data', exist_ok=True)
csv_path = 'data/TMF_tick.csv'

# 設定台灣時間今天
today = dt.datetime.utcnow() + dt.timedelta(hours=8)
date_str = today.strftime("%Y_%m_%d")

# 期交所每日壓縮檔固定網址
url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date_str}.zip"
print(f"嘗試下載今日資料: {url}")

headers = {'User-Agent': 'Mozilla/5.0'}
res = requests.get(url, headers=headers)

if res.status_code == 200:
    print("✅ 下載成功，準備解壓縮與過濾 TMF...")
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        filename = z.namelist()[0]
        with z.open(filename) as f:
            # 讀取當日全部 Tick (期交所編碼通常是 big5)
            df_daily = pd.read_csv(f, encoding='big5', dtype=str, on_bad_lines='skip')
            df_daily.columns = [str(c).strip() for c in df_daily.columns]
            
            # 過濾 TMF (微台)
            if '商品代號' in df_daily.columns:
                df_tmf = df_daily[df_daily['商品代號'].str.strip() == 'TMF'].copy()
                
                # 排除價差單 (含有 /)
                if '到期月份(週別)' in df_tmf.columns:
                    df_tmf = df_tmf[~df_tmf['到期月份(週別)'].astype(str).str.contains('/')]
                
                if not df_tmf.empty:
                    # 若已有歷史檔案，則接在後面；若無，則建立新檔
                    if os.path.exists(csv_path):
                        df_old = pd.read_csv(csv_path, dtype=str)
                        df_combined = pd.concat([df_old, df_tmf]).drop_duplicates()
                        df_combined.to_csv(csv_path, index=False, encoding='utf-8-sig')
                        print(f"✅ 已將今日資料附加至 {csv_path} 中！")
                    else:
                        df_tmf.to_csv(csv_path, index=False, encoding='utf-8-sig')
                        print(f"✅ 建立新的 {csv_path} 檔案！")
                else:
                    print("⚠️ 今日無 TMF 交易紀錄。")
else:
    print(f"⚠️ 找不到今日檔案 (HTTP {res.status_code})，可能尚未結算或今天是假日。")

# ==========================================
# 2. 寄發 Email 通知 (包含費半與掛單點位)
# ==========================================
def send_email_notification(subject, body):
    sender_user = os.environ.get('GMAIL_USER')
    sender_pass = os.environ.get('GMAIL_PASSWORD')
    receiver = os.environ.get('NOTIFY_EMAIL')
    
    if not sender_user or not sender_pass or not receiver:
        print("⚠️ 未設定 Email 密碼憑證，略過寄信。")
        return

    msg = MIMEMultipart()
    msg['From'] = sender_user
    msg['To'] = receiver
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_user, sender_pass)
        server.sendmail(sender_user, receiver, msg.as_string())
        server.quit()
        print("✅ 郵件發送成功！")
    except Exception as e:
        print(f"❌ 寄信失敗: {e}")

# 獲取昨日費半狀況
try:
    sox_raw = yf.download('^SOX', period='5d', progress=False)
    sox_close = sox_raw['Close'].iloc[:, 0] if isinstance(sox_raw.columns, pd.MultiIndex) else sox_raw['Close']
    last_sox_ret = sox_close.pct_change().dropna().iloc[-1] * 100
    sox_trend = "上漲 (多方)" if last_sox_ret > 0 else "下跌 (空方)"
except:
    sox_trend = "抓取失敗"
    last_sox_ret = 0

email_content = f"""
【TMF 微台早盤自動化掛單提示】
日期: {today.strftime('%Y-%m-%d')}

1. 【美股動態】
   - 昨夜費半指數: {sox_trend} ({last_sox_ret:.2f}%)
   
2. 【今日實戰掛單建議 (08:43準備)】
   - 若台指期開盤與費半同向：
     * 做多策略：買進 [開盤價 - 20點]，停利 [+30點]，停損 [-100點]。
     * 做空策略：賣出 [開盤價 + 40點]，停利 [-50點]，停損 [+100點]。
   - 若方向不一致：觀望不出手。
"""

send_email_notification(f"【TMF 量化訊號】{today.strftime('%Y-%m-%d')} 早盤策略", email_content)

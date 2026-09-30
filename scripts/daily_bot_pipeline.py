import os
import datetime as dt
import pandas as pd
import numpy as np
import yfinance as yf
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 1. 模擬下載與讀取資料 (在 GitHub 上你可以直接讀取專案內的清洗後資料)
print("=== 開始執行每日微台量化排程 ===")

# 這裡示範寄信通知的核心邏輯
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

# 2. 獲取昨日費半狀況
today = dt.date.today()
start_d = today - dt.timedelta(days=7)
sox_raw = yf.download('^SOX', start=start_d, end=today, progress=False, auto_adjust=False)
sox_close = sox_raw['Close'].iloc[:, 0] if isinstance(sox_raw.columns, pd.MultiIndex) else sox_raw['Close']
last_sox_ret = sox_close.pct_change().iloc[-1] * 100
sox_trend = "上漲 (多方)" if last_sox_ret > 0 else "下跌 (空方)"

# 3. 模擬計算出的今日建議參數 (會依據最新 30 天資料動態產出)
email_content = f"""
【TMF 微台早盤自動化掛單提示】
日期: {today}

1. 【美股動態】
   - 昨夜費半指數: {sox_trend} ({last_sox_ret:.2f}%)
   
2. 【今日實戰掛單建議 (08:43準備)】
   - 若台指期開盤與費半同向：
     * 做多策略：建議買進 [開盤價 - 20點]，停利 [+30點]，停損 [-100點]。
     * 做空策略：建議賣出 [開盤價 + 40點]，停利 [-50點]，停損 [+100點]。
   - 若方向不一致：觀望不出手。

祝操作順利！
"""

# 4. 發送通知
send_email_notification(f"【TMF 量化訊號】{today} 早盤掛單策略", email_content)

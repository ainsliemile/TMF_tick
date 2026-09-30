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
import itertools

print("=== 🚀 啟動 TMF 微台全自動量化大腦 ===")

# ==========================================
# 1. 下載並更新期交所「今日」Tick 資料
# ==========================================
os.makedirs('data', exist_ok=True)
csv_path = 'data/TMF_tick.csv'
today_utc = dt.datetime.utcnow() + dt.timedelta(hours=8)
date_str = today_utc.strftime("%Y_%m_%d")

url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date_str}.zip"
print(f"嘗試下載: {url}")

try:
    res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
    if res.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            filename = z.namelist()[0]
            with z.open(filename) as f:
                df_daily = pd.read_csv(f, encoding='big5', dtype=str, on_bad_lines='skip')
                df_daily.columns = [str(c).strip() for c in df_daily.columns]
                
                if '商品代號' in df_daily.columns:
                    df_tmf = df_daily[df_daily['商品代號'].str.strip() == 'TMF'].copy()
                    if '到期月份(週別)' in df_tmf.columns:
                        df_tmf = df_tmf[~df_tmf['到期月份(週別)'].astype(str).str.contains('/')]
                    
                    if not df_tmf.empty:
                        if os.path.exists(csv_path):
                            df_old = pd.read_csv(csv_path, dtype=str)
                            df_combined = pd.concat([df_old, df_tmf]).drop_duplicates()
                            df_combined.to_csv(csv_path, index=False, encoding='utf-8-sig')
                        else:
                            df_tmf.to_csv(csv_path, index=False, encoding='utf-8-sig')
                        print("✅ 今日資料更新完成。")
except Exception as e:
    print(f"⚠️ 下載或更新資料失敗 (可能為假日): {e}")

# ==========================================
# 2. 核心大腦：讀取歷史數據與費半，執行網格掃描
# ==========================================
print("🧠 開始計算近 30 天最佳參數...")
best_long = {'entry': 20, 'tp': 30, 'sl': 100, 'pnl': 0, 'win_rate': 0} # 預設防呆值
best_short = {'entry': 40, 'tp': 50, 'sl': 100, 'pnl': 0, 'win_rate': 0}

if os.path.exists(csv_path):
    df = pd.read_csv(csv_path, dtype=str)
    df['成交價格'] = pd.to_numeric(df['成交價格'], errors='coerce')
    df['成交時間'] = pd.to_numeric(df['成交時間'], errors='coerce')
    df = df.dropna(subset=['成交價格', '成交時間'])
    df['成交時間'] = df['成交時間'].astype(int)
    df['交易日期'] = pd.to_datetime(df['交易日期'].astype(str).str.strip(), format='%Y%m%d').dt.date

    # 只取日盤 08:45 ~ 13:45，並保留最近 30 個交易日
    df_day = df[(df['成交時間'] >= 84500) & (df['成交時間'] <= 134500)].copy()
    recent_dates = sorted(df_day['交易日期'].unique())[-30:]
    df_day = df_day[df_day['交易日期'].isin(recent_dates)]
    
    # 找主力合約
    if '到期月份(週別)' in df_day.columns:
        df_day['到期月份(週別)'] = df_day['到期月份(週別)'].astype(str).str.strip()
        daily_main = df_day.groupby(['交易日期', '到期月份(週別)'])['成交價格'].count().reset_index()
        daily_main = daily_main.loc[daily_main.groupby('交易日期')['成交價格'].idxmax()][['交易日期', '到期月份(週別)']]
        df_day = df_day.merge(daily_main, on=['交易日期', '到期月份(週別)'], how='inner')
        
    df_day = df_day.sort_values(['交易日期', '成交時間']).reset_index(drop=True)

    # 下載對應期間的費半
    start_dl = recent_dates[0] - dt.timedelta(days=7) if recent_dates else today_utc.date() - dt.timedelta(days=30)
    sox_raw = yf.download('^SOX', start=start_dl, end=today_utc.date() + dt.timedelta(days=2), progress=False)
    sox_close = sox_raw['Close'].iloc[:, 0] if isinstance(sox_raw.columns, pd.MultiIndex) else sox_raw['Close']
    sox_df = pd.DataFrame({'SOX_Close': sox_close}).dropna()
    sox_df['sox_ret'] = sox_df['SOX_Close'].pct_change() * 100
    sox_dict = sox_df.reset_index().set_index('Date')['sox_ret'].to_dict()

    # 預處理每日走勢特徵
    daily_groups = df_day.groupby('交易日期')
    dates = sorted(list(daily_groups.groups.keys()))
    day_records = []
    
    for i in range(1, len(dates)): 
        prev_date, curr_date = dates[i-1], dates[i]
        prev_ticks, curr_ticks = daily_groups.get_group(prev_date), daily_groups.get_group(curr_date)
        if len(prev_ticks) == 0 or len(curr_ticks) == 0: continue
            
        prev_close = prev_ticks['成交價格'].iloc[-1]
        prices = curr_ticks['成交價格'].values
        times = curr_ticks['成交時間'].values
        open_p = prices[0]
        
        tw_open_ret = (open_p - prev_close) / prev_close * 100
        sox_ret = sox_dict.get(pd.Timestamp(curr_date) - dt.timedelta(days=1), sox_dict.get(pd.Timestamp(curr_date) - dt.timedelta(days=2), 0))
        
        direction = 1 if (sox_ret > 0 and tw_open_ret > 0) else (-1 if (sox_ret < 0 and tw_open_ret < 0) else 0)
        if direction != 0:
            day_records.append({'dir': direction, 'open': open_p, 'prices': prices, 'times': times})

    # 參數掃描函數
    def find_best_params(direction_filter):
        ENTRY_DISTS = [10, 20, 30, 40, 50, 60, 70, 80]
        TP_DISTS = [10, 20, 30, 40, 50, 60, 70, 80]
        SL_DISTS = [80, 90, 100, 110, 120, 130]
        days = [d for d in day_records if d['dir'] == direction_filter]
        if not days: return None
        
        best_result = None
        max_pnl = -999999
        
        for entry_dist, tp_dist, sl_dist in itertools.product(ENTRY_DISTS, TP_DISTS, SL_DISTS):
            total_pnl = 0
            win_count, trade_count = 0, 0
            
            for d in days:
                open_p, prices, times = d['open'], d['prices'], d['times']
                if direction_filter == 1:
                    entry_t, tp_t, sl_t = open_p - entry_dist, open_p + tp_dist, open_p - sl_dist
                else:
                    entry_t, tp_t, sl_t = open_p + entry_dist, open_p - tp_dist, open_p + sl_dist
                    
                entered = False; entry_p = 0; entry_idx = 0
                for j in range(len(prices)):
                    if times[j] >= 90600: break
                    if (direction_filter == 1 and prices[j] <= entry_t) or (direction_filter == -1 and prices[j] >= entry_t):
                        entered = True; entry_p = entry_t; entry_idx = j; break
                        
                if not entered: continue
                    
                exit_p = prices[-1]
                for j in range(entry_idx + 1, len(prices)):
                    if times[j] >= 90600: exit_p = prices[j]; break
                    if direction_filter == 1:
                        if prices[j] >= tp_t: exit_p = tp_t; break
                        elif prices[j] <= sl_t: exit_p = sl_t; break
                    else:
                        if prices[j] <= tp_t: exit_p = tp_t; break
                        elif prices[j] >= sl_t: exit_p = sl_t; break
                            
                gross = (exit_p - entry_p)*10 if direction_filter == 1 else (entry_p - exit_p)*10
                net = gross - (int(entry_p*10*0.00002) + int(exit_p*10*0.00002)) - 50
                total_pnl += net
                trade_count += 1
                if net > 0: win_count += 1
                    
            if trade_count > 0 and total_pnl > max_pnl:
                max_pnl = total_pnl
                best_result = {'entry': entry_dist, 'tp': tp_dist, 'sl': sl_dist, 'pnl': total_pnl, 'win_rate': round(win_count/trade_count*100, 1)}
        return best_result

    # 取得最新最佳參數
    long_res = find_best_params(1)
    short_res = find_best_params(-1)
    if long_res: best_long = long_res
    if short_res: best_short = short_res

# ==========================================
# 3. 抓取昨夜費半並寄信
# ==========================================
try:
    sox_latest = yf.download('^SOX', period='5d', progress=False)
    sox_c = sox_latest['Close'].iloc[:, 0] if isinstance(sox_latest.columns, pd.MultiIndex) else sox_latest['Close']
    last_sox_ret = sox_c.pct_change().dropna().iloc[-1] * 100
    sox_trend = "上漲 📈 (多方)" if last_sox_ret > 0 else "下跌 📉 (空方)"
except:
    sox_trend = "無法取得資料"
    last_sox_ret = 0

email_content = f"""
【TMF 微台早盤全自動量化訊號】
運算基準: 根據過去 30 個交易日滾動最佳化

1. 【美股動態】
   - 昨夜費半指數: {sox_trend} (幅: {last_sox_ret:.2f}%)
   
2. 【今日動態掛單建議 (08:43準備)】
   - 若台指期開盤與費半同向，請參考以下最佳化參數：
   
   🔵 【做多策略】(若費半漲、台指開高)
     * 買進限價：[開盤價 - {best_long['entry']} 點]
     * 賣出停利：[開盤價 + {best_long['tp']} 點]
     * 絕對停損：[開盤價 - {best_long['sl']} 點]
     (近30日此參數勝率: {best_long['win_rate']}%, 淨利: {best_long['pnl']}元)
     
   🔴 【做空策略】(若費半跌、台指開低)
     * 賣出限價：[開盤價 + {best_short['entry']} 點]
     * 買進停利：[開盤價 - {best_short['tp']} 點]
     * 絕對停損：[開盤價 + {best_short['sl']} 點]
     (近30日此參數勝率: {best_short['win_rate']}%, 淨利: {best_short['pnl']}元)

3. 【特別提醒】
   - 時間防守：若至 09:06:00 尚未觸及停利損，請立即市價平倉。
   - 若開盤方向與費半不一致，建議今日空手觀望。
"""

sender_user = os.environ.get('GMAIL_USER')
sender_pass = os.environ.get('GMAIL_PASSWORD')
receiver = os.environ.get('NOTIFY_EMAIL')

if sender_user and sender_pass and receiver:
    msg = MIMEMultipart()
    msg['From'] = sender_user
    msg['To'] = receiver
    msg['Subject'] = f"【微台策略】{today_utc.strftime('%Y-%m-%d')} 最佳化掛單點位"
    msg.attach(MIMEText(email_content, 'plain', 'utf-8'))
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_user, sender_pass)
        server.sendmail(sender_user, receiver, msg.as_string())
        server.quit()
        print("✅ 最佳化策略郵件發送成功！")
    except Exception as e:
        print(f"❌ 寄信失敗: {e}")
else:
    print("⚠️ 尚未設定 GitHub Secrets 密碼，無法寄信。")
    print("今日生成的信件內容如下：\n", email_content)

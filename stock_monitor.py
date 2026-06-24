"""
股票監控腳本 - 美股 & 台股
每 5 分鐘檢查一次，觸發漲跌幅門檻時寄 Gmail 通知
"""

import yfinance as yf
import smtplib
import time
import logging
import os
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, time as dtime
import pytz

# =============================================================
#  ★ 設定區：只需要改這個區塊 ★
# =============================================================

# Gmail 應用程式密碼（App Password）
# 取得方式：Google 帳號 → 安全性 → 兩步驟驗證 → 應用程式密碼
# 本機執行：直接填寫下方；GitHub Actions：改用 Secrets，不需改這裡
GMAIL_SENDER   = os.environ.get("GMAIL_SENDER",   "your_sender@gmail.com")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "ncvs ktyg zaua tvmj")
GMAIL_RECEIVER = os.environ.get("GMAIL_RECEIVER", "aa23248630@gmail.com")

# 美股監控清單（Yahoo Finance 代碼）
US_STOCKS = [
    "SIDU",   # Sidus Space
    "FIG",    # Simplify US Equity PLUS Downside Convexity ETF
    "WOLF",   # Wolfspeed
    "ASTS",   # AST SpaceMobile
    "BABA",   # Alibaba
]

# 台股監控清單（結尾加 .TW；上櫃用 .TWO）
TW_STOCKS = [
    "2498.TW",   # 飛宏
    "8996.TW",   # 高力
]

# 漲跌幅門檻（小數，0.10 = 10%）
TW_LIMIT_UP_PCT   =  0.10    # 台股漲停（官方為 +9.85%，設 10% 以防浮點誤差）
TW_LIMIT_DOWN_PCT = -0.10    # 台股跌停
US_ALERT_UP_PCT   =  0.10    # 美股自訂漲幅警示
US_ALERT_DOWN_PCT = -0.10    # 美股自訂跌幅警示

# 每次檢查間隔秒數
CHECK_INTERVAL_SEC = 300  # 5 分鐘

# =============================================================
#  程式本體（一般不需修改）
# =============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("stock_monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# 已通知的 key，避免同天重複寄信
_notified: set[str] = set()

NOTIFIED_FILE = "notified_state.json"


def load_notified() -> set:
    try:
        with open(NOTIFIED_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_notified() -> None:
    with open(NOTIFIED_FILE, "w") as f:
        json.dump(list(_notified), f)


# ── 交易時段判斷 ──────────────────────────

def is_tw_trading() -> bool:
    """台股：週一到週五 09:00–13:30 (CST)"""
    now = datetime.now(pytz.timezone("Asia/Taipei"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 0) <= now.time() <= dtime(13, 30)


def is_us_trading() -> bool:
    """美股：週一到週五 09:30–16:00 (EST/EDT)"""
    now = datetime.now(pytz.timezone("US/Eastern"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


# ── Gmail 寄信 ────────────────────────────

def send_email(subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = GMAIL_RECEIVER
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECEIVER, msg.as_string())
        log.info("郵件已送出：%s", subject)
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail 認證失敗！請確認 GMAIL_SENDER 和 GMAIL_PASSWORD 是否正確，"
                  "且已開啟兩步驟驗證並使用應用程式密碼。")
    except Exception as e:
        log.error("郵件發送失敗：%s", e)


# ── 股票報價抓取 ──────────────────────────

def fetch_quote(ticker: str) -> dict | None:
    """
    透過 yfinance 取得最新報價。
    回傳 dict（price, prev_close, change_pct, name），失敗回傳 None。
    """
    try:
        tk   = yf.Ticker(ticker)
        info = tk.fast_info

        price      = info.last_price
        prev_close = info.previous_close

        if not price or not prev_close or prev_close == 0:
            log.warning("%s：取得的價格為空，略過", ticker)
            return None

        change_pct = (price - prev_close) / prev_close
        # fast_info 沒有公司名稱，用 ticker 代替
        try:
            name = tk.info.get("shortName", ticker)
        except Exception:
            name = ticker

        return {
            "ticker":     ticker,
            "name":       name,
            "price":      price,
            "prev_close": prev_close,
            "change_pct": change_pct,
        }
    except Exception as e:
        log.warning("無法取得 %s 資料：%s", ticker, e)
        return None


# ── 通知判斷與寄信 ────────────────────────

def _notify_key(ticker: str, event: str) -> str:
    today = datetime.now().strftime("%Y%m%d")
    return f"{ticker}_{event}_{today}"


def check_and_notify(quote: dict, up_thr: float, down_thr: float, market: str) -> None:
    ticker  = quote["ticker"]
    pct     = quote["change_pct"]
    pct_str = f"{pct:+.2%}"

    if pct >= up_thr:
        event = "UP"
        if market == "TW":
            label = f"漲停板 🚀（{pct_str}）"
        else:
            label = f"急漲警示 📈（{pct_str}，超過 {up_thr:.0%}）"
    elif pct <= down_thr:
        event = "DOWN"
        if market == "TW":
            label = f"跌停板 💥（{pct_str}）"
        else:
            label = f"急跌警示 📉（{pct_str}，超過 {abs(down_thr):.0%}）"
    else:
        return  # 未觸發門檻，不通知

    key = _notify_key(ticker, event)
    if key in _notified:
        return  # 今天已通知過，不重複寄

    _notified.add(key)

    market_name = "台股" if market == "TW" else "美股"
    subject = f"【{market_name}警示】{quote['name']} ({ticker}) {label}"
    body = (
        f"股票漲跌通知\n"
        f"{'─' * 42}\n"
        f"市場        {'台股（TWSE/TPEx）' if market == 'TW' else '美股（NYSE/NASDAQ）'}\n"
        f"股票代碼    {ticker}\n"
        f"公司名稱    {quote['name']}\n"
        f"目前價格    {quote['price']:.2f}\n"
        f"前日收盤    {quote['prev_close']:.2f}\n"
        f"漲跌幅      {pct_str}  ← {label}\n"
        f"通知時間    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{'─' * 42}\n"
        f"此通知當日不再重複發送。\n"
    )

    log.warning("觸發通知 → %s %s %s", ticker, label, pct_str)
    send_email(subject, body)


# ── 主迴圈 ────────────────────────────────

def run_once() -> None:
    log.info("══════ 開始檢查 ══════")
    tw_active = is_tw_trading()
    us_active = is_us_trading()

    if tw_active:
        log.info("台股盤中，檢查 %d 支股票", len(TW_STOCKS))
        for sym in TW_STOCKS:
            q = fetch_quote(sym)
            if q:
                log.info("  %-12s  現價 %8.2f  漲跌 %+.2f%%",
                         sym, q["price"], q["change_pct"] * 100)
                check_and_notify(q, TW_LIMIT_UP_PCT, TW_LIMIT_DOWN_PCT, "TW")
    else:
        log.info("台股非交易時間，略過")

    if us_active:
        log.info("美股盤中，檢查 %d 支股票", len(US_STOCKS))
        for sym in US_STOCKS:
            q = fetch_quote(sym)
            if q:
                log.info("  %-12s  現價 %8.2f  漲跌 %+.2f%%",
                         sym, q["price"], q["change_pct"] * 100)
                check_and_notify(q, US_ALERT_UP_PCT, US_ALERT_DOWN_PCT, "US")
    else:
        log.info("美股非交易時間，略過")

    if not tw_active and not us_active:
        log.info("台股與美股均非交易時間，下次 %d 秒後再檢查", CHECK_INTERVAL_SEC)


def main() -> None:
    global _notified
    _notified = load_notified()

    log.info("股票監控啟動 ✅")
    log.info("台股清單：%s", TW_STOCKS)
    log.info("美股清單：%s", US_STOCKS)

    if os.environ.get("GITHUB_ACTIONS"):
        # 在 GitHub Actions 上：單次執行後儲存狀態
        try:
            run_once()
        except Exception as e:
            log.error("執行發生例外：%s", e)
        finally:
            save_notified()
    else:
        # 本機執行：持續迴圈
        log.info("檢查間隔：%d 秒（%d 分鐘）", CHECK_INTERVAL_SEC, CHECK_INTERVAL_SEC // 60)
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                log.info("使用者中止，程式結束")
                break
            except Exception as e:
                log.error("主迴圈發生例外：%s", e)

            try:
                time.sleep(CHECK_INTERVAL_SEC)
            except KeyboardInterrupt:
                log.info("使用者中止，程式結束")
                break


if __name__ == "__main__":
    main()

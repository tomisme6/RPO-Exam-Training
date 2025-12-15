import streamlit as st
import pandas as pd
import pdfplumber
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

# =====================================================
# 基本設定
# =====================================================
st.set_page_config(
    page_title="質子中心-輻防師特訓平台",
    layout="wide",
    page_icon="☢️"
)

# =====================================================
# 常數與 Schema
# =====================================================
SHEET_QUESTIONS = "questions"
SHEET_SCORES = "scores"
SHEET_RECORDS = "records"

QUESTIONS_SCHEMA = [
    "question", "option_A", "option_B", "option_C", "option_D",
    "correct_answer", "explanation", "topic", "type"
]
SCORES_SCHEMA = ["user_id", "timestamp", "score", "total", "percent"]
RECORDS_SCHEMA = [
    "user_id", "timestamp", "mode",
    "question", "topic",
    "user_answer", "correct_answer", "is_correct"
]

# =====================================================
# Google Sheets / gspread 工具（核心穩定區）
# =====================================================
def get_gspread_client():
    creds_info = st.secrets["connections"]["gsheets"]["credentials"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def open_spreadsheet():
    spreadsheet = st.secrets["connections"]["gsheets"]["spreadsheet"].strip()
    gc = get_gspread_client()

    try:
        # 最穩：URL
        if spreadsheet.startswith("http"):
            return gc.open_by_url(spreadsheet)

        # 次穩：Spreadsheet ID
        if re.match(r"^[a-zA-Z0-9-_]{30,}$", spreadsheet):
            return gc.open_by_key(spreadsheet)

        # 最不穩：名稱（保留但給警告）
        st.warning("⚠️ 目前用『試算表名稱』連線，強烈建議改成 URL 或 ID")
        return gc.open(spreadsheet)

    except Exception as e:
        sa = st.secrets["connections"]["gsheets"]["credentials"].get("client_email", "unknown")
        st.error("❌ 無法開啟 Google Spreadsheet")
        st.code(
            f"spreadsheet(secrets) = {spreadsheet}\n"
            f"service_account = {sa}\n"
            f"error = {repr(e)}"
        )
        st.info(
            "請確認：\n"
            "1️⃣ secrets 的 spreadsheet 是正確 URL 或 ID\n"
            "2️⃣ 試算表已共用給 service account（Editor）\n"
            "3️⃣ 試算表仍存在，未被刪除或移動"
        )
        raise

def get_or_create_worksheet(sh, title, schema):
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(schema))
        ws.append_row(schema, value_input_option="RAW")
        return ws

    header = ws.row_values(1)
    if header != schema:
        if not header:
            ws.append_row(schema, value_input_option="RAW")
    return ws

def read_sheet(title, schema):
    sh = open_spreadsheet()
    ws = get_or_create_worksheet(sh, title, schema)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=schema)
    df = pd.DataFrame(records)
    for c in schema:
        if c not in df.columns:
            df[c] = ""
    return df[schema]

def append_rows(title, df, schema):
    sh = open_spreadsheet()
    ws = get_or_create_worksheet(sh, title, schema)
    df = df.copy()
    for c in schema:
        if c not in df.columns:
            df[c] = ""
    df = df[schema]
    ws.append_rows(df.astype(str).values.tolist(), value_input_option="RAW")

# =====================================================
# 輔助函式
# =====================================================
def normalize_answer(ans):
    """標準化答案：從字串中提取第一個單個數字 (1-4)"""
    if pd.isna(ans) or ans is None:
        return ""
    # 查找並返回字串中第一個非空白的 1, 2, 3, 或 4
    match = re.search(r"([1-4])", str(ans).strip())
    return match.group(0) if match else ""

def extract_options_from_line(line, q_obj):
    """
    從同一行文字中切割多個選項 (1)... (2)... (3)... (4)...
    並更新到題目物件 q_obj 中
    """
    # 使用 Regex 尋找 (數字) 開頭的位置
    # pattern: (1)內容 (2)內容...
    # 我們先用替換方式加上分隔符，再切割
    temp_line = line
    # 在 (1), (2), (3), (4) 前面加上特殊分隔符號 |SPLIT|
    temp_line = re.sub(r"(\([1-4]\))", r"|SPLIT|\1", temp_line)
    
    parts = temp_line.split("|SPLIT|")
    
    for part in parts:
        part = part.strip()
        if not part: continue
        
        if part.startswith("(1)"):
            q_obj["option_A"] = part
        elif part.startswith("(2)"):
            q_obj["option_B"] = part
        elif part.startswith("(3)"):
            q_obj["option_C"] = part
        elif part.startswith("(4)"):
            q_obj["option_D"] = part

def parse_exam_pdf(text):
    """針對 113年第一次.pdf 格式優化的解析器"""
    questions = []
    lines = text.split("\n")
    
    current_q = None
    waiting_for_answer = False # 狀態標記：是否正在等待下一行的答案
    
    for line in lines:
        line = line.strip()
        if not line: continue

        # 0. 過濾頁首頁尾雜訊 (依據文件內容)
        if "核能安全委員會" in line or "測驗試題" in line or "第" in line and "頁" in line:
            continue

        # 1. 處理答案區塊 [解:]
        if "[解:]" in line:
            # 情況 A: 答案在同一行，例如 "[解:] (1)"
            content = line.replace("[解:]", "").strip()
            if content and current_q:
                current_q["correct_answer"] = normalize_answer(content)
                waiting_for_answer = False
            else:
                # 情況 B: 答案在下一行 (這是這份文件的常見狀況)
                waiting_for_answer = True
            continue

        # 2. 如果正在等待答案 (上一行是 [解:])
        if waiting_for_answer:
            if current_q:
                current_q["correct_answer"] = normalize_answer(line)
            waiting_for_answer = False # 重置狀態
            continue

        # 3. 偵測新題目 (數字 + . 或 空白)
        # 例如: "1. 依天然..." 或 "1 依天然..."
        match_q = re.match(r"^(\d+)[\.\s](.+)", line)
        if match_q:
            # 如果有上一題，先存檔
            if current_q:
                questions.append(current_q)
            
            # 建立新題目
            current_q = {
                "question": line, # 完整題目 (含編號)
                "option_A": "", "option_B": "", "option_C": "", "option_D": "",
                "correct_answer": "", "explanation": "",
                "topic": "未分類", "type": "choice"
            }
            continue

        # 4. 處理選項與題目內容
        if current_q:
            # 檢查這一行是否包含選項 (1)~ (4)
            if re.search(r"\([1-4]\)", line):
                extract_options_from_line(line, current_q)
            else:
                # 如果不是選項，也不是答案，那可能是「題目太長換行」
                # 將內容接到題目後面 (避免把題目斷掉)
                # 但要小心不要把解釋或其他雜訊接進去
                if not current_q["option_A"]: # 如果還沒開始抓選項，才視為題目延伸
                     current_q["question"] += " " + line
                else:
                    # 如果選項都已經抓完了，這行可能是詳解文字 (explanation)
                    current_q["explanation"] += line + "\n"

    # 迴圈結束後，加入最後一題
    if current_q:
        questions.append(current_q)
        
    return questions

# =====================================================
# Sidebar
# =====================================================
with st.sidebar:
    st.title("⚙️ 功能選單")
    user_id = st.text_input("👤 姓名 / 工號", value="User")
    mode = st.radio(
        "模式",
        [
            "📝 模擬考",
            "📂 匯入 PDF（管理員）",
            "🔧 資料庫檢查"
        ]
    )

# =====================================================
# 模擬考
# =====================================================
if mode == "📝 模擬考":
    df_q = read_sheet(SHEET_QUESTIONS, QUESTIONS_SCHEMA)
    df_q = df_q[df_q["option_A"] != ""]

    if df_q.empty:
        st.warning("題庫為空，請先匯入 PDF 題目")
    else:
        num = st.slider("題數", 1, min(20, len(df_q)), 10)
        sample = df_q.sample(num).reset_index(drop=True)

        answers = {}
        with st.form("quiz"):
            for i, r in sample.iterrows():
                st.write(f"**Q{i+1}. {r['question']}**")
                answers[i] = st.radio(
                    "",
                    ["1", "2", "3", "4"],
                    format_func=lambda x: f"({x}) {r[f'option_{chr(64+int(x))}']}",
                    key=f"q{i}"
                )
            submit = st.form_submit_button("交卷")

        if submit:
            score = 0
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            records = []

            for i, r in sample.iterrows():
                ua = normalize_answer(answers[i])
                ca = normalize_answer(r["correct_answer"])
                ok = int(ua == ca)
                score += ok

                records.append({
                    "user_id": user_id,
                    "timestamp": now,
                    "mode": "batch",
                    "question": r["question"],
                    "topic": r["topic"],
                    "user_answer": ua,
                    "correct_answer": ca,
                    "is_correct": ok
                })

                if ok:
                    st.success(f"Q{i+1} 正確")
                else:
                    st.error(f"Q{i+1} 錯誤，正解 {ca}")

            append_rows(
                SHEET_SCORES,
                pd.DataFrame([{
                    "user_id": user_id,
                    "timestamp": now,
                    "score": score,
                    "total": num,
                    "percent": int(score / num * 100)
                }]),
                SCORES_SCHEMA
            )
            append_rows(SHEET_RECORDS, pd.DataFrame(records), RECORDS_SCHEMA)

            st.metric("成績", f"{score}/{num}")

# =====================================================
# PDF 匯入
# =====================================================
elif mode == "📂 匯入 PDF（管理員）":
    uploaded = st.file_uploader("上傳 PDF", type="pdf")
    if uploaded and st.button("解析並寫入"):
        with pdfplumber.open(uploaded) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        data = parse_exam_pdf(text)
        if data:
            append_rows(SHEET_QUESTIONS, pd.DataFrame(data), QUESTIONS_SCHEMA)
            st.success(f"成功匯入 {len(data)} 題")

# =====================================================
# 資料庫檢查
# =====================================================
elif mode == "🔧 資料庫檢查":
    st.subheader("Questions")
    st.dataframe(read_sheet(SHEET_QUESTIONS, QUESTIONS_SCHEMA))
    st.subheader("Scores")
    st.dataframe(read_sheet(SHEET_SCORES, SCORES_SCHEMA))
    st.subheader("Records")
    st.dataframe(read_sheet(SHEET_RECORDS, RECORDS_SCHEMA))

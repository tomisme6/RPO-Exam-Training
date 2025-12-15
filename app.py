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
    if pd.isna(ans):
        return ""
    s = re.sub(r"[()（）\s]", "", str(ans))
    m = re.search(r"[1-4]", s)
    return m.group(0) if m else ""

def parse_exam_pdf(text):
    questions = []
    lines = text.split("\n")
    q = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 偵測題目開頭 (例如 "1. " 或 "40. ")
        if re.match(r"^\d+[\.\s]", line):
            # 如果已經有上一題，先存起來
            if q:
                questions.append(q)
            # 初始化新的一題
            q = {
                "question": line,
                "option_A": "", "option_B": "",
                "option_C": "", "option_D": "",
                "correct_answer": "",
                "explanation": "",
                "topic": "未分類",
                "type": "choice"
            }
            continue

        # 如果 q 還沒建立（代表是PDF檔頭的標題或雜訊），直接跳過，不處理
        if q is None:
            continue

        # 偵測選項與解析
        if line.startswith("(1)"):
            q["option_A"] = line
        elif line.startswith("(2)"):
            q["option_B"] = line
        elif line.startswith("(3)"):
            q["option_C"] = line
        elif line.startswith("(4)"):
            q["option_D"] = line
        elif "解" in line:
            q["correct_answer"] = normalize_answer(line)
        else:
            # 只有當 q 存在時，才把文字加到解析或題目敘述中
            q["explanation"] += line + "\n"

    # 迴圈結束後，把最後一題存進去
    if q:
        questions.append(q)
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

import streamlit as st
import pandas as pd
import pdfplumber
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

# ==========================
# 基本設定
# ==========================
st.set_page_config(
    page_title="質子中心-輻防師特訓平台",
    layout="wide",
    page_icon="☢️"
)

# ==========================
# Schema 定義
# ==========================
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

SHEET_QUESTIONS = "questions"
SHEET_SCORES = "scores"
SHEET_RECORDS = "records"

# ==========================
# gspread 工具
# ==========================
def get_gspread_client():
    creds_info = st.secrets["connections"]["gsheets"]["credentials"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def get_worksheet(ws_name):
    spreadsheet = st.secrets["connections"]["gsheets"]["spreadsheet"]
    gc = get_gspread_client()
    sh = gc.open_by_url(spreadsheet) if spreadsheet.startswith("http") else gc.open(spreadsheet)
    return sh.worksheet(ws_name)

def ensure_header(ws, schema):
    header = ws.row_values(1)
    if header != schema:
        if ws.row_count == 0 or header == []:
            ws.append_row(schema, value_input_option="RAW")

def read_sheet(ws_name, schema):
    ws = get_worksheet(ws_name)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=schema)
    df = pd.DataFrame(records)
    for c in schema:
        if c not in df.columns:
            df[c] = ""
    return df[schema]

def append_rows(ws_name, df, schema):
    ws = get_worksheet(ws_name)
    ensure_header(ws, schema)
    df = df.copy()
    for c in schema:
        if c not in df.columns:
            df[c] = ""
    df = df[schema]
    ws.append_rows(df.astype(str).values.tolist(), value_input_option="RAW")

# ==========================
# 輔助函式
# ==========================
def normalize_answer(ans):
    if pd.isna(ans):
        return ""
    s = str(ans)
    s = re.sub(r"[()（）\s]", "", s)
    m = re.search(r"[1-4]", s)
    return m.group(0) if m else ""

def show_correct(msg):
    st.success(f"✔ {msg}")

def show_wrong(msg):
    st.error(f"✘ {msg}")

def parse_exam_pdf(text):
    questions = []
    lines = text.split("\n")
    q = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d+[\.\s]", line):
            if q:
                questions.append(q)
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
            q["explanation"] += line + "\n"
    if q:
        questions.append(q)
    return questions

# ==========================
# Sidebar
# ==========================
with st.sidebar:
    st.title("⚙️ 功能選單")
    user_id = st.text_input("👤 姓名 / 工號", value="User")
    mode = st.radio(
        "模式",
        [
            "📝 模擬考",
            "⚡ 單題練習",
            "📉 錯題本",
            "📂 匯入 PDF（管理員）",
            "🔧 資料庫檢查"
        ]
    )

# ==========================
# 模擬考
# ==========================
if mode == "📝 模擬考":
    df_q = read_sheet(SHEET_QUESTIONS, QUESTIONS_SCHEMA)
    df_q = df_q[df_q["option_A"] != ""]
    if df_q.empty:
        st.warning("題庫為空")
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
            recs = []
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for i, r in sample.iterrows():
                ua = normalize_answer(answers[i])
                ca = normalize_answer(r["correct_answer"])
                ok = int(ua == ca)
                score += ok
                recs.append({
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
                    show_correct(f"Q{i+1} 正確")
                else:
                    show_wrong(f"Q{i+1} 錯誤，正解 {ca}")

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
            append_rows(SHEET_RECORDS, pd.DataFrame(recs), RECORDS_SCHEMA)
            st.metric("成績", f"{score}/{num}")

# ==========================
# PDF 匯入
# ==========================
elif mode == "📂 匯入 PDF（管理員）":
    up = st.file_uploader("上傳 PDF", type="pdf")
    if up and st.button("解析並寫入"):
        with pdfplumber.open(up) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        data = parse_exam_pdf(text)
        if data:
            append_rows(SHEET_QUESTIONS, pd.DataFrame(data), QUESTIONS_SCHEMA)
            st.success(f"成功匯入 {len(data)} 題")

# ==========================
# 資料庫檢查
# ==========================
elif mode == "🔧 資料庫檢查":
    st.subheader("Questions")
    st.dataframe(read_sheet(SHEET_QUESTIONS, QUESTIONS_SCHEMA))
    st.subheader("Scores")
    st.dataframe(read_sheet(SHEET_SCORES, SCORES_SCHEMA))
    st.subheader("Records")
    st.dataframe(read_sheet(SHEET_RECORDS, RECORDS_SCHEMA))

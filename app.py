import streamlit as st
import pandas as pd
import pdfplumber
import re
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

# ==========================
# Streamlit 頁面設定
# ==========================
st.set_page_config(
    page_title="質子中心-輻防師特訓平台",
    layout="wide",
    page_icon="☢️"
)

# ==========================
# 自訂 CSS
# ==========================
st.markdown("""
<style>
.stApp {
    background: radial-gradient(circle at top left, #f9f9ff 0, #eef7ff 40%, #fefefe 100%);
    font-family: -apple-system, BlinkMacSystemFont, "Noto Sans TC", system-ui;
}
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #001845 0%, #003566 60%, #1b3a6f 100%);
}
section[data-testid="stSidebar"] * { color: white !important; }
section[data-testid="stSidebar"] input { color: #333 !important; }

.answer-box-correct {
    padding: 10px 14px; border-radius: 10px;
    background-color: #d4f8d4; border-left: 6px solid #2ecc71; margin-bottom: 8px;
}
.answer-label-correct { color: #27ae60; font-weight: 700; margin-right: 6px; }

.answer-box-wrong {
    padding: 10px 14px; border-radius: 10px;
    background-color: #ffd6e0; border-left: 6px solid #ff4d6d; margin-bottom: 8px;
}
.answer-label-wrong { color: #c9184a; font-weight: 700; margin-right: 6px; }
</style>
""", unsafe_allow_html=True)

# ==========================
# Google Sheets 連線設定
# ==========================
conn = st.connection("gsheets", type=GSheetsConnection)

SHEET_QUESTIONS = "questions"
SHEET_SCORES = "scores"
SHEET_RECORDS = "records"

# ==========================
# Session state 初始化
# ==========================
for k, v in {
    "quiz_data": None,
    "quiz_submitted": False,
    "current_single_q": None,
    "weak_practice_q": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ==========================
# Schema 定義（防止欄位亂掉）
# ==========================
QUESTIONS_SCHEMA = [
    "question", "option_A", "option_B", "option_C", "option_D",
    "correct_answer", "explanation", "topic", "type"
]
SCORES_SCHEMA = ["user_id", "timestamp", "score", "total", "percent"]
RECORDS_SCHEMA = ["user_id", "timestamp", "mode", "question", "topic", "user_answer", "correct_answer", "is_correct"]

# ==========================
# 工具函式
# ==========================
def is_a1_style_header(cols) -> bool:
    """偵測你截圖那種 A1/B1/C1... 的假表頭"""
    cols = [str(c).strip() for c in cols]
    if len(cols) == 0:
        return False
    return all(re.match(r"^[A-Z]+\d+$", c) for c in cols[: min(6, len(cols))])

def coerce_schema(df: pd.DataFrame, schema: list[str]) -> pd.DataFrame:
    """確保 df 擁有 schema 所有欄位（缺的補空），並且只保留 schema 欄位順序"""
    for col in schema:
        if col not in df.columns:
            df[col] = ""
    return df[schema].copy()

def safe_strip_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip() for c in df.columns]
    return df

def get_data(worksheet_name: str, schema: list[str] | None = None) -> pd.DataFrame:
    """
    強壯版讀取：
    1) 讀到 A1/B1... 會自動把「第一列資料」拉上來當 header
    2) 不吞 200 假錯誤（避免回傳空表害你以為沒題目）
    3) 欄位 strip
    4) 需要 schema 時，自動補欄位
    """
    try:
        df = conn.read(worksheet=worksheet_name, ttl=0)

        if df is None:
            return pd.DataFrame(columns=schema or [])

        if df.empty:
            return pd.DataFrame(columns=schema or [])

        df = safe_strip_cols(df)

        # ★ 關鍵修正：如果第一列是 A1/B1/C1...，把第一列資料當成真正 header
        if is_a1_style_header(df.columns):
            # 這種狀況通常第 1 列是 A1/B1...，第 2 列才是 question/option_A...
            # 但 conn.read() 會把第 2 列讀成第一筆資料 (row0)
            new_header = df.iloc[0].astype(str).str.strip().tolist()
            df = df.iloc[1:].reset_index(drop=True)
            df.columns = new_header
            df = safe_strip_cols(df)

        # 再一次清掉 weird 空白欄
        df = df.loc[:, [c for c in df.columns if c and c != "nan"]].copy()

        if schema is not None:
            df = coerce_schema(df, schema)

        return df

    except Exception as e:
        st.error(f"讀取資料庫失敗 ({worksheet_name}): {repr(e)}")
        return pd.DataFrame(columns=schema or [])

def write_full_table(worksheet_name: str, df: pd.DataFrame):
    """全表覆寫（streamlit_gsheets 的 update 本質就是這樣）"""
    conn.update(worksheet=worksheet_name, data=df)

def append_data(worksheet_name: str, new_df: pd.DataFrame, schema: list[str]):
    """
    強壯版 append：
    - 先讀舊資料 → 補 schema → concat → 再整張寫回
    - 不吞任何例外（吞了你會以為成功但其實沒寫）
    """
    old_df = get_data(worksheet_name, schema=schema)

    new_df = safe_strip_cols(new_df)
    new_df = coerce_schema(new_df, schema)

    if old_df.empty:
        updated = new_df
    else:
        updated = pd.concat([old_df, new_df], ignore_index=True)

    write_full_table(worksheet_name, updated)

def normalize_answer(ans):
    if pd.isna(ans):
        return ""
    s = str(ans).strip()
    s = s.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    m = re.search(r"[1-4]", s)
    return m.group(0) if m else ""

def show_correct(msg):
    st.markdown(f'<div class="answer-box-correct"><span class="answer-label-correct">✔ 正確！</span>{msg}</div>', unsafe_allow_html=True)

def show_wrong(msg):
    st.markdown(f'<div class="answer-box-wrong"><span class="answer-label-wrong">✘ 錯誤！</span>{msg}</div>', unsafe_allow_html=True)

def parse_exam_pdf(text):
    questions = []
    lines = text.split("\n")
    current_q = {}
    step = "FIND_Q"

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if re.match(r"^\d+[\.\s]", line):
            if current_q:
                questions.append(current_q)
            current_q = {
                "question": line,
                "option_A": "", "option_B": "", "option_C": "", "option_D": "",
                "correct_answer": "", "explanation": "",
                "topic": "未分類", "type": "choice"
            }
            step = "FIND_OPT"
            continue

        if any(tag in line for tag in ["[解:]", "[解：]", "[解]"]):
            cleaned = line
            for tag in ["[解:]", "[解：]", "[解]"]:
                cleaned = cleaned.replace(tag, "")
            cleaned = cleaned.strip()
            if cleaned:
                m = re.search(r"[\(（]?([1-4])[\)）]?", cleaned)
                if m:
                    current_q["correct_answer"] = normalize_answer(m.group(1))
                step = "EXPLAIN"
            else:
                step = "WAIT_ANS"
            continue

        if step == "WAIT_ANS":
            m = re.search(r"[\(（]?([1-4])[\)）]?", line)
            if m:
                current_q["correct_answer"] = normalize_answer(m.group(1))
            else:
                current_q["explanation"] += line + "\n"
            step = "EXPLAIN"
            continue

        if step == "FIND_OPT":
            if "(1)" in line and "(2)" in line:
                parts = re.split(r'(?=\(\d\))', line)
                for part in parts:
                    part = part.strip()
                    if not part or part in ["(1)", "(2)", "(3)", "(4)"]:
                        continue
                    if part.startswith("(1)"):
                        current_q["option_A"] = part
                    elif part.startswith("(2)"):
                        current_q["option_B"] = part
                    elif part.startswith("(3)"):
                        current_q["option_C"] = part
                    elif part.startswith("(4)"):
                        current_q["option_D"] = part
                continue

            if line.startswith("(1)"):
                current_q["option_A"] = line
            elif line.startswith("(2)"):
                current_q["option_B"] = line
            elif line.startswith("(3)"):
                current_q["option_C"] = line
            elif line.startswith("(4)"):
                current_q["option_D"] = line
            else:
                current_q["question"] += " " + line
            continue

        if step == "EXPLAIN":
            current_q["explanation"] += line + "\n"

    if current_q:
        questions.append(current_q)
    return questions

# ==========================
# Sidebar
# ==========================
with st.sidebar:
    st.title("⚙️ 功能選單")
    user_id = st.text_input("👤 請輸入您的姓名/工號", value="User", help="用於區分錯題本紀錄")

    mode = st.radio(
        "請選擇模式",
        [
            "📝 模擬考模式 (批次刷題)",
            "⚡ 單題即時練習",
            "📉 弱點分析 / 錯題本",
            "📂 匯入 PDF 題庫 (管理員)",
            "🔧 資料庫檢查"
        ]
    )
    st.markdown("---")
    st.caption(f"Current User: {user_id}")

# ==========================
# 模擬考模式
# ==========================
if mode == "📝 模擬考模式 (批次刷題)":
    st.title(f"📝 輻防師模擬測驗 ({user_id})")

    df_q = get_data(SHEET_QUESTIONS, schema=QUESTIONS_SCHEMA)

    # 過濾空題
    df_q = df_q[df_q["question"].astype(str).str.strip() != ""]
    df_q = df_q[df_q["option_A"].astype(str).str.strip() != ""].reset_index(drop=True)

    if df_q.empty:
        st.warning("⚠️ 題庫讀取失敗或為空，請先匯入題目。")
        st.info("小提示：Google Sheet 的第一列一定要是欄位列（question/option_A/...），不要有 A1/B1 那種假表頭。")
    else:
        if st.session_state.quiz_data is None:
            st.write(f"題庫共有：{len(df_q)} 題")
            num = st.slider("請選擇題數", 1, min(50, len(df_q)), 10)

            if st.button("🚀 開始測驗"):
                st.session_state.quiz_data = df_q.sample(n=num).reset_index(drop=True)
                st.session_state.quiz_submitted = False
                st.rerun()
        else:
            st.subheader("作答區")

            with st.form("quiz_form"):
                user_answers = {}
                for idx, row in st.session_state.quiz_data.iterrows():
                    st.markdown(f"**第 {idx+1} 題**")
                    st.write(row["question"])

                    options = ["1", "2", "3", "4"]
                    texts = [row["option_A"], row["option_B"], row["option_C"], row["option_D"]]

                    def fmt(x):
                        i = int(x) - 1
                        t = texts[i] if i < len(texts) else ""
                        return f"({x}) {t}"

                    user_answers[idx] = st.radio(
                        f"Q{idx+1}",
                        options,
                        format_func=fmt,
                        horizontal=True,
                        key=f"q_{idx}"
                    )
                    st.markdown("---")

                submitted = st.form_submit_button("📝 交卷")

            if submitted:
                st.session_state.quiz_submitted = True

            if st.session_state.quiz_submitted:
                score = 0
                total = len(st.session_state.quiz_data)
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                records_list = []

                for idx, row in st.session_state.quiz_data.iterrows():
                    user_norm = normalize_answer(user_answers[idx])
                    ans = normalize_answer(row.get("correct_answer", ""))
                    is_correct = 1 if (ans != "" and user_norm == ans) else 0
                    score += is_correct

                    records_list.append({
                        "user_id": user_id,
                        "timestamp": now_str,
                        "mode": "batch",
                        "question": row.get("question", ""),
                        "topic": row.get("topic", "未分類"),
                        "user_answer": user_norm,
                        "correct_answer": ans,
                        "is_correct": is_correct
                    })

                    with st.expander(f"第 {idx+1} 題檢討", expanded=(not is_correct)):
                        if is_correct:
                            show_correct(f"你的答案：({user_norm})")
                        else:
                            show_wrong(f"你的答案：({user_norm})，正確答案：({ans})")
                        st.write(f"解析：{row.get('explanation', '')}")

                percent = int(score / total * 100) if total > 0 else 0

                # 存成績
                new_score = pd.DataFrame([{
                    "user_id": user_id,
                    "timestamp": now_str,
                    "score": score,
                    "total": total,
                    "percent": percent
                }])
                append_data(SHEET_SCORES, new_score, schema=SCORES_SCHEMA)

                # 存逐題紀錄
                if records_list:
                    append_data(SHEET_RECORDS, pd.DataFrame(records_list), schema=RECORDS_SCHEMA)

                st.success("成績與作答紀錄已上傳雲端！")
                st.metric("最終成績", f"{percent} 分", f"答對 {score}/{total} 題")

                if st.button("🔄 再測一次"):
                    st.session_state.quiz_data = None
                    st.session_state.quiz_submitted = False
                    st.rerun()

# ==========================
# 單題即時練習
# ==========================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 單題即時練習")

    df_q = get_data(SHEET_QUESTIONS, schema=QUESTIONS_SCHEMA)
    df_q = df_q[df_q["option_A"].astype(str).str.strip() != ""]
    df_q = df_q[df_q["question"].astype(str).str.strip() != ""]

    if df_q.empty:
        st.warning("題庫為空或讀取失敗，請先匯入題目。")
    else:
        if st.button("🎲 抽題"):
            st.session_state.current_single_q = df_q.sample(1).iloc[0].to_dict()

        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")

            options = ["1", "2", "3", "4"]
            texts = [q.get("option_A", ""), q.get("option_B", ""), q.get("option_C", ""), q.get("option_D", "")]

            def fmt(x):
                i = int(x) - 1
                return f"({x}) {texts[i]}"

            user_raw = st.radio("請選擇", options, format_func=fmt)

            if st.button("查看答案"):
                ans = normalize_answer(q.get("correct_answer", ""))
                user_norm = normalize_answer(user_raw)
                is_correct = 1 if user_norm == ans else 0

                if is_correct:
                    show_correct(f"答案正確！({ans})")
                else:
                    show_wrong(f"正確答案是 ({ans})")

                st.info(f"解析：{q.get('explanation', '')}")

                rec = pd.DataFrame([{
                    "user_id": user_id,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": "single",
                    "question": q.get("question", ""),
                    "topic": q.get("topic", "未分類"),
                    "user_answer": user_norm,
                    "correct_answer": ans,
                    "is_correct": is_correct
                }])
                append_data(SHEET_RECORDS, rec, schema=RECORDS_SCHEMA)

# ==========================
# 弱點分析 / 錯題本
# ==========================
elif mode == "📉 弱點分析 / 錯題本":
    st.title(f"📉 {user_id} 的弱點分析")

    df_rec = get_data(SHEET_RECORDS, schema=RECORDS_SCHEMA)
    if df_rec.empty:
        st.info("尚無作答紀錄。")
    else:
        df_rec = df_rec[df_rec["user_id"].astype(str) == str(user_id)]
        if df_rec.empty:
            st.info(f"使用者 {user_id} 目前沒有紀錄。")
        else:
            df_rec["is_correct"] = pd.to_numeric(df_rec["is_correct"], errors="coerce").fillna(0)

            st.subheader("📊 主題正確率")
            topic_stat = df_rec.groupby("topic").agg(
                total=("is_correct", "count"),
                correct=("is_correct", "sum")
            ).reset_index()
            topic_stat["accuracy"] = topic_stat["correct"] / topic_stat["total"] * 100
            topic_stat = topic_stat.sort_values("accuracy")

            st.dataframe(topic_stat, use_container_width=True)
            st.bar_chart(topic_stat.set_index("topic")["accuracy"])

            st.markdown("---")
            st.subheader("📚 錯題本 (曾答錯的題目)")

            q_stat = df_rec.groupby("question").agg(
                total=("is_correct", "count"),
                correct=("is_correct", "sum"),
                topic=("topic", "first")
            ).reset_index()
            q_stat["wrong_count"] = q_stat["total"] - q_stat["correct"]
            weak_questions = q_stat[q_stat["wrong_count"] > 0].sort_values("wrong_count", ascending=False)

            if weak_questions.empty:
                st.success("太強了！目前沒有錯題紀錄 🎉")
            else:
                st.write("依照錯誤次數排序：")
                st.dataframe(weak_questions[["question", "topic", "wrong_count", "total"]], use_container_width=True)

                if st.button("從錯題本抽題重練"):
                    target_q_text = weak_questions.sample(1).iloc[0]["question"]
                    df_all_q = get_data(SHEET_QUESTIONS, schema=QUESTIONS_SCHEMA)
                    match = df_all_q[df_all_q["question"] == target_q_text]
                    if not match.empty:
                        st.session_state.weak_practice_q = match.iloc[0].to_dict()
                    else:
                        st.warning("原始題庫中找不到此題資料（可能已被刪除）。")

                q2 = st.session_state.weak_practice_q
                if q2 is not None:
                    st.markdown("#### 重練題目")
                    st.write(q2["question"])
                    st.info("請在心裡作答...（點擊下方看答案）")
                    if st.button("看答案"):
                        st.write(f"正確答案：{normalize_answer(q2.get('correct_answer',''))}")
                        st.write(f"解析：{q2.get('explanation','')}")

# ==========================
# 匯入 PDF (管理員)
# ==========================
elif mode == "📂 匯入 PDF 題庫 (管理員)":
    st.title("📂 匯入題庫")
    st.warning("⚠️ 此操作會將題目寫入 Google Sheets，請謹慎操作。")

    uploaded = st.file_uploader("上傳 PDF", type=["pdf"])
    if uploaded and st.button("解析並上傳"):
        with pdfplumber.open(uploaded) as pdf:
            text = "\n".join([(p.extract_text() or "") for p in pdf.pages])

        data = parse_exam_pdf(text)
        if data:
            df_new = pd.DataFrame(data)
            df_new = coerce_schema(df_new, QUESTIONS_SCHEMA)

            st.success(f"解析出 {len(df_new)} 題，正在寫入雲端...")
            append_data(SHEET_QUESTIONS, df_new, schema=QUESTIONS_SCHEMA)

            # 立刻讀回驗證（抓到就當場抓包）
            df_check = get_data(SHEET_QUESTIONS, schema=QUESTIONS_SCHEMA)
            st.success(f"✅ 匯入完成！目前題庫筆數：{len(df_check)}")
            st.dataframe(df_check.head(10), use_container_width=True)
        else:
            st.error("解析失敗，找不到題目格式。")

# ==========================
# 資料庫檢查
# ==========================
elif mode == "🔧 資料庫檢查":
    st.title("🔧 資料庫即時預覽")

    if st.button("重新整理"):
        st.cache_data.clear()
        st.rerun()

    st.subheader("題庫 (Questions)")
    st.dataframe(get_data(SHEET_QUESTIONS, schema=QUESTIONS_SCHEMA).head(30), use_container_width=True)

    st.subheader("成績 (Scores)")
    st.dataframe(get_data(SHEET_SCORES, schema=SCORES_SCHEMA).head(30), use_container_width=True)

    st.subheader("紀錄 (Records)")
    st.dataframe(get_data(SHEET_RECORDS, schema=RECORDS_SCHEMA).head(30), use_container_width=True)

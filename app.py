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
    section[data-testid="stSidebar"] * {
        color: white !important;
    }
    /* 修正輸入框文字顏色，避免被白色蓋掉 */
    section[data-testid="stSidebar"] input {
        color: #333 !important;
    }
    .answer-box-correct {
        padding: 10px 14px;
        border-radius: 10px;
        background-color: #d4f8d4;
        border-left: 6px solid #2ecc71;
        margin-bottom: 8px;
    }
    .answer-label-correct { color: #27ae60; font-weight: 700; margin-right: 6px; }
    .answer-box-wrong {
        padding: 10px 14px;
        border-radius: 10px;
        background-color: #ffd6e0;
        border-left: 6px solid #ff4d6d;
        margin-bottom: 8px;
    }
    .answer-label-wrong { color: #c9184a; font-weight: 700; margin-right: 6px; }
    </style>
""", unsafe_allow_html=True)

# ==========================
# Google Sheets 連線設定
# ==========================
# 建立連線物件
conn = st.connection("gsheets", type=GSheetsConnection)

# 定義分頁名稱
SHEET_QUESTIONS = "questions"
SHEET_SCORES = "scores"
SHEET_RECORDS = "records"

# Session state 初始化
if "quiz_data" not in st.session_state:
    st.session_state.quiz_data = None
if "quiz_submitted" not in st.session_state:
    st.session_state.quiz_submitted = False
if "current_single_q" not in st.session_state:
    st.session_state.current_single_q = None
if "weak_practice_q" not in st.session_state:
    st.session_state.weak_practice_q = None

# ==========================
# 資料庫操作函式 (CRUD)
# ==========================

def get_data(worksheet_name):
    """從 Google Sheet 讀取資料，TTL=0 確保不快取舊資料"""
    try:
        df = conn.read(worksheet=worksheet_name, ttl=0)
        return df
    except Exception as e:
        st.error(f"讀取資料庫失敗 ({worksheet_name}): {e}")
        return pd.DataFrame()

def append_data(worksheet_name, new_df):
    """將新資料附加到 Google Sheet"""
    try:
        # 先讀取舊資料
        old_df = get_data(worksheet_name)
        # 合併
        updated_df = pd.concat([old_df, new_df], ignore_index=True)
        # 寫回 (update 模式)
        conn.update(worksheet=worksheet_name, data=updated_df)
    except Exception as e:
        st.error(f"寫入資料庫失敗 ({worksheet_name}): {e}")

def normalize_answer(ans):
    """統一答案格式"""
    if pd.isna(ans): return ""
    s = str(ans).strip()
    s = s.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    s = s.strip()
    m = re.search(r"[1-4]", s)
    if m: return m.group(0)
    return ""

def show_correct(msg):
    st.markdown(f'<div class="answer-box-correct"><span class="answer-label-correct">✔ 正確！</span>{msg}</div>', unsafe_allow_html=True)

def show_wrong(msg):
    st.markdown(f'<div class="answer-box-wrong"><span class="answer-label-wrong">✘ 錯誤！</span>{msg}</div>', unsafe_allow_html=True)

def parse_exam_pdf(text):
    """PDF 解析邏輯 (與原版相同)"""
    questions = []
    lines = text.split("\n")
    current_q = {}
    step = "FIND_Q"

    for raw_line in lines:
        line = raw_line.strip()
        if not line: continue

        if re.match(r"^\d+[\.\s]", line):
            if current_q: questions.append(current_q)
            current_q = {
                "question": line, "option_A": "", "option_B": "", "option_C": "", "option_D": "",
                "correct_answer": "", "explanation": "", "topic": "未分類", "type": "choice"
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
                if m: current_q["correct_answer"] = normalize_answer(m.group(1))
                step = "EXPLAIN"
            else:
                step = "WAIT_ANS"
            continue

        if step == "WAIT_ANS":
            m = re.search(r"[\(（]?([1-4])[\)）]?", line)
            if m: current_q["correct_answer"] = normalize_answer(m.group(1))
            else: current_q["explanation"] += line + "\n"
            step = "EXPLAIN"
            continue

        if step == "FIND_OPT":
            if "(1)" in line and "(2)" in line:
                parts = re.split(r'(?=\(\d\))', line)
                for part in parts:
                    part = part.strip()
                    if not part or part in ["(1)", "(2)", "(3)", "(4)"]: continue
                    if part.startswith("(1)"): current_q["option_A"] = part
                    elif part.startswith("(2)"): current_q["option_B"] = part
                    elif part.startswith("(3)"): current_q["option_C"] = part
                    elif part.startswith("(4)"): current_q["option_D"] = part
                continue
            
            if line.startswith("(1)"): current_q["option_A"] = line
            elif line.startswith("(2)"): current_q["option_B"] = line
            elif line.startswith("(3)"): current_q["option_C"] = line
            elif line.startswith("(4)"): current_q["option_D"] = line
            else: current_q["question"] += " " + line
            continue

        if step == "EXPLAIN":
            current_q["explanation"] += line + "\n"

    if current_q: questions.append(current_q)
    return questions

# ==========================
# Sidebar
# ==========================
with st.sidebar:
    st.title("⚙️ 功能選單")
    
    # === 新增：使用者 ID ===
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

    # 讀取題庫
    df_q = get_data(SHEET_QUESTIONS)
    
    # 檢查是否有資料
    if df_q.empty or "option_A" not in df_q.columns:
        st.warning("⚠️ 題庫讀取失敗或為空，請先匯入題目。")
    else:
        df_q = df_q[df_q["option_A"].notna()].reset_index(drop=True)

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
                    texts = [row.get("option_A", ""), row.get("option_B", ""), row.get("option_C", ""), row.get("option_D", "")]
                    
                    def fmt(x):
                        i = int(x)-1
                        return f"({x}) {texts[i]}" if i < len(texts) else ""
                    
                    user_answers[idx] = st.radio(f"Q{idx+1}", options, format_func=fmt, horizontal=True, key=f"q_{idx}")
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
                    user_raw = user_answers[idx]
                    user_norm = normalize_answer(user_raw)
                    ans = normalize_answer(row.get("correct_answer", ""))
                    is_correct = 1 if (ans != "" and user_norm == ans) else 0
                    if is_correct: score += 1
                    
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
                        if is_correct: show_correct(f"你的答案：({user_norm})")
                        else: show_wrong(f"你的答案：({user_norm})，正確答案：({ans})")
                        st.write(f"解析：{row.get('explanation', '')}")

                # === 存入 Google Sheets ===
                # 1. 存成績
                percent = int(score / total * 100) if total > 0 else 0
                new_score = pd.DataFrame([{
                    "user_id": user_id,
                    "timestamp": now_str,
                    "score": score,
                    "total": total,
                    "percent": percent
                }])
                append_data(SHEET_SCORES, new_score)

                # 2. 存逐題紀錄
                if records_list:
                    append_data(SHEET_RECORDS, pd.DataFrame(records_list))

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
    df_q = get_data(SHEET_QUESTIONS)

    if df_q.empty:
        st.warning("題庫為空")
    else:
        df_q = df_q[df_q["option_A"].notna()]
        if st.button("🎲 抽題"):
            st.session_state.current_single_q = df_q.sample(1).iloc[0]

        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")
            options = ["1", "2", "3", "4"]
            texts = [q.get("option_A", ""), q.get("option_B", ""), q.get("option_C", ""), q.get("option_D", "")]
            def fmt(x):
                i = int(x)-1
                return f"({x}) {texts[i]}" if i < len(texts) else ""

            user_raw = st.radio("請選擇", options, format_func=fmt)
            
            if st.button("查看答案"):
                ans = normalize_answer(q.get("correct_answer", ""))
                user_norm = normalize_answer(user_raw)
                is_correct = 1 if user_norm == ans else 0

                if is_correct: show_correct(f"答案正確！({ans})")
                else: show_wrong(f"正確答案是 ({ans})")
                st.info(f"解析：{q.get('explanation', '')}")

                # 存檔
                rec = [{
                    "user_id": user_id,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": "single",
                    "question": q.get("question", ""),
                    "topic": q.get("topic", "未分類"),
                    "user_answer": user_norm,
                    "correct_answer": ans,
                    "is_correct": is_correct
                }]
                append_data(SHEET_RECORDS, pd.DataFrame(rec))

# ==========================
# 弱點分析 / 錯題本
# ==========================
elif mode == "📉 弱點分析 / 錯題本":
    st.title(f"📉 {user_id} 的弱點分析")
    
    df_rec = get_data(SHEET_RECORDS)
    
    if df_rec.empty:
        st.info("尚無作答紀錄。")
    else:
        # 過濾該使用者的紀錄
        if "user_id" in df_rec.columns:
            df_rec = df_rec[df_rec["user_id"].astype(str) == str(user_id)]
        
        if len(df_rec) == 0:
            st.info(f"使用者 {user_id} 目前沒有紀錄。")
        else:
            # 確保欄位型態
            df_rec["is_correct"] = pd.to_numeric(df_rec["is_correct"], errors='coerce').fillna(0)

            # Topic 分析
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
            
            # 找出錯題
            q_stat = df_rec.groupby("question").agg(
                total=("is_correct", "count"),
                correct=("is_correct", "sum"),
                topic=("topic", "first")
            ).reset_index()
            # 只要答錯次數 > 0 (total - correct > 0)
            q_stat["wrong_count"] = q_stat["total"] - q_stat["correct"]
            weak_questions = q_stat[q_stat["wrong_count"] > 0].sort_values("wrong_count", ascending=False)

            if weak_questions.empty:
                st.success("太強了！目前沒有錯題紀錄 🎉")
            else:
                st.write("依照錯誤次數排序：")
                st.dataframe(weak_questions[["question", "topic", "wrong_count", "total"]])
                
                # 重新練習功能
                if st.button("從錯題本抽題重練"):
                    target_q_text = weak_questions.sample(1).iloc[0]["question"]
                    # 抓回完整題目資訊
                    df_all_q = get_data(SHEET_QUESTIONS)
                    match = df_all_q[df_all_q["question"] == target_q_text]
                    
                    if not match.empty:
                        st.session_state.weak_practice_q = match.iloc[0]
                    else:
                        st.warning("原始題庫中找不到此題資料（可能已被刪除）。")

                q2 = st.session_state.weak_practice_q
                if q2 is not None:
                    st.markdown("#### 重練題目")
                    st.write(q2["question"])
                    # (簡化顯示，不贅述選項邏輯)
                    st.info(f"請在心裡作答... (點擊下方看答案)")
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
            st.success(f"解析出 {len(df_new)} 題，正在寫入雲端...")
            append_data(SHEET_QUESTIONS, df_new)
            st.success("✅ 匯入完成！")
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
    st.dataframe(get_data(SHEET_QUESTIONS).head())

    st.subheader("成績 (Scores)")
    st.dataframe(get_data(SHEET_SCORES).head())
    
    st.subheader("紀錄 (Records)")
    st.dataframe(get_data(SHEET_RECORDS).head())

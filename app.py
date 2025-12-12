import streamlit as st
import pandas as pd
import pdfplumber
import re
import os
from datetime import datetime

# ==========================
# Streamlit 頁面設定
# ==========================
st.set_page_config(
    page_title="質子中心-輻防師特訓平台",
    layout="wide",
    page_icon="☢️"
)

# ==========================
# 自訂 CSS（含 sidebar 白色字體）
# ==========================
st.markdown("""
    <style>
    .stApp {
        background: radial-gradient(circle at top left, #f9f9ff 0, #eef7ff 40%, #fefefe 100%);
        font-family: -apple-system, BlinkMacSystemFont, "Noto Sans TC", system-ui;
    }

    /* Sidebar 深藍底＋白字 */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #001845 0%, #003566 60%, #1b3a6f 100%);
    }
    section[data-testid="stSidebar"] * {
        color: white !important;
    }

    /* 答題結果動畫 */
    @keyframes popIn {
        0% { transform: scale(0.9); opacity: 0; }
        60% { transform: scale(1.03); opacity: 1; }
        100% { transform: scale(1.0); opacity: 1; }
    }
    .answer-box-correct {
        padding: 10px 14px;
        border-radius: 10px;
        background-color: #d4f8d4;
        border-left: 6px solid #2ecc71;
        margin-bottom: 8px;
        animation: popIn 0.35s ease-out;
    }
    .answer-label-correct {
        color: #27ae60;
        font-weight: 700;
        margin-right: 6px;
    }

    .answer-box-wrong {
        padding: 10px 14px;
        border-radius: 10px;
        background-color: #ffd6e0;
        border-left: 6px solid #ff4d6d;
        margin-bottom: 8px;
        animation: popIn 0.35s ease-out;
    }
    .answer-label-wrong {
        color: #c9184a;
        font-weight: 700;
        margin-right: 6px;
    }
    </style>
""", unsafe_allow_html=True)

# ==========================
# 檔案設定
# ==========================
csv_file = "data.csv"       # 題庫
score_file = "scores.csv"   # 每次模擬考總成績
record_file = "records.csv" # 每題作答紀錄（弱點分析 / 錯題本用）

# 初始化題庫檔
if not os.path.exists(csv_file):
    df_init = pd.DataFrame(columns=[
        "question", "option_A", "option_B", "option_C", "option_D",
        "correct_answer", "explanation", "topic", "type"
    ])
    df_init.to_csv(csv_file, index=False, encoding="utf-8-sig")

# Session state
if "quiz_data" not in st.session_state:
    st.session_state.quiz_data = None
if "quiz_submitted" not in st.session_state:
    st.session_state.quiz_submitted = False
if "current_single_q" not in st.session_state:
    st.session_state.current_single_q = None
if "weak_practice_q" not in st.session_state:
    st.session_state.weak_practice_q = None


# ==========================
# 工具函式
# ==========================

def normalize_answer(ans):
    """
    統一答案格式：
    - 支援 2, 2.0, (2), （2）, ' 2 ' 等
    - 最終一律回傳 '1' / '2' / '3' / '4'
    """
    if pd.isna(ans):
        return ""
    s = str(ans).strip()
    # 去括號
    s = s.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    s = s.strip()
    # 只抓第一個 1~4
    m = re.search(r"[1-4]", s)
    if m:
        return m.group(0)
    return ""


def show_correct(msg: str):
    st.markdown(f"""
        <div class="answer-box-correct">
            <span class="answer-label-correct">✔ 正確！</span>{msg}
        </div>
    """, unsafe_allow_html=True)


def show_wrong(msg: str):
    st.markdown(f"""
        <div class="answer-box-wrong">
            <span class="answer-label-wrong">✘ 錯誤！</span>{msg}
        </div>
    """, unsafe_allow_html=True)


def save_score(score: int, total: int):
    """紀錄模擬考成績（整份考卷）"""
    percent = int(score / total * 100) if total > 0 else 0
    row = pd.DataFrame([{
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "score": score,
        "total": total,
        "percent": percent
    }])
    if os.path.exists(score_file):
        row.to_csv(score_file, mode="a", index=False, header=False, encoding="utf-8-sig")
    else:
        row.to_csv(score_file, mode="w", index=False, header=True, encoding="utf-8-sig")


def save_records(records):
    """
    將逐題作答紀錄寫入 record_file
    records: list of dict
    欄位建議：
        timestamp, mode, question, topic,
        user_answer, correct_answer, is_correct(0/1)
    """
    if not records:
        return
    df = pd.DataFrame(records)
    if os.path.exists(record_file):
        df.to_csv(record_file, mode="a", index=False, header=False, encoding="utf-8-sig")
    else:
        df.to_csv(record_file, mode="w", index=False, header=True, encoding="utf-8-sig")


def parse_exam_pdf(text: str):
    """
    PDF 題目解析，答案永遠存 1/2/3/4
    支援：
    - 題幹開頭為「數字.」
    - [解:]、[解：]、[解]
    - 答案同行 or 下一行
    - 同行多個選項 e.g.
      (1) (1)氣體或微粒之煙霧警報器 (2)微波接收器保護管 (3)逃生用指示燈 (4)燈泡
    """
    questions = []
    lines = text.split("\n")
    current_q = {}
    step = "FIND_Q"

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # ---- 題目偵測 ----
        if re.match(r"^\d+[\.\s]", line):
            if current_q:
                questions.append(current_q)

            current_q = {
                "question": line,
                "option_A": "", "option_B": "", "option_C": "", "option_D": "",
                "correct_answer": "",
                "explanation": "",
                "topic": "未分類",
                "type": "choice"
            }
            step = "FIND_OPT"
            continue

        # ---- 偵測 [解] ----
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

        # ---- 答案在下一行 ----
        if step == "WAIT_ANS":
            m = re.search(r"[\(（]?([1-4])[\)）]?", line)
            if m:
                current_q["correct_answer"] = normalize_answer(m.group(1))
            else:
                current_q["explanation"] += line + "\n"
            step = "EXPLAIN"
            continue

        # ---- 選項 ----
        if step == "FIND_OPT":
            # 如果一行裡面同時有 (1) & (2)，代表多個選項黏在一起
            if "(1)" in line and "(2)" in line:
                parts = re.split(r'(?=\(\d\))', line)
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                    # 避免只有 "(1)" 這種沒內容的
                    if part in ["(1)", "(2)", "(3)", "(4)"]:
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

            # 一行一個選項
            if line.startswith("(1)"):
                current_q["option_A"] = line
            elif line.startswith("(2)"):
                current_q["option_B"] = line
            elif line.startswith("(3)"):
                current_q["option_C"] = line
            elif line.startswith("(4)"):
                current_q["option_D"] = line
            else:
                # 不是選項就繼續接在題幹後面
                current_q["question"] += " " + line
            continue

        # ---- 解析文字 ----
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
    mode = st.radio(
        "請選擇模式",
        [
            "📝 模擬考模式 (批次刷題)",
            "⚡ 單題即時練習",
            "📉 弱點分析 / 錯題本",
            "📂 匯入 PDF 題庫",
            "🔧 debug 資料庫檢查"
        ]
    )
    st.markdown("---")
    st.caption("Proton Center RPO Exam System v4.0 — PRO 弱點分析版")


# ==========================
# 模擬考模式
# ==========================
if mode == "📝 模擬考模式 (批次刷題)":
    st.title("📝 輻防師模擬測驗")

    df = pd.read_csv(csv_file)
    df = df[df["option_A"].notna() & (df["option_A"] != "")]

    if len(df) == 0:
        st.warning("題庫是空的，請先匯入 PDF 題庫。")
    else:
        if st.session_state.quiz_data is None:
            st.write(f"題庫共有：{len(df)} 題")
            num = st.slider("請選擇題數", 1, min(50, len(df)), 10)

            if st.button("🚀 開始測驗"):
                st.session_state.quiz_data = df.sample(n=num).reset_index(drop=True)
                st.session_state.quiz_submitted = False
                st.rerun()

        else:
            st.subheader("作答區")
            with st.form("quiz_form"):
                user_answers = {}

                for idx, row in st.session_state.quiz_data.iterrows():
                    st.markdown(f"### 第 {idx+1} 題")
                    st.write(row["question"])

                    options = ["1", "2", "3", "4"]
                    opt_texts = [
                        row.get("option_A", ""),
                        row.get("option_B", ""),
                        row.get("option_C", ""),
                        row.get("option_D", "")
                    ]

                    def fmt(x):
                        i = int(x) - 1
                        txt = opt_texts[i] if i < len(opt_texts) else ""
                        return f"({x}) {txt}"

                    user_answers[idx] = st.radio(
                        f"Q{idx+1}",
                        options,
                        format_func=fmt,
                        horizontal=True
                    )

                    st.markdown("---")

                submitted = st.form_submit_button("📝 交卷")

            if submitted:
                st.session_state.quiz_submitted = True

            if st.session_state.quiz_submitted:
                score = 0
                total = len(st.session_state.quiz_data)
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                records = []

                for idx, row in st.session_state.quiz_data.iterrows():
                    user_raw = user_answers[idx]          # "1"~"4"
                    user_norm = normalize_answer(user_raw)
                    ans = normalize_answer(row.get("correct_answer", ""))
                    topic = row.get("topic", "未分類")
                    question_text = row.get("question", "")

                    is_correct = 0
                    if ans != "" and user_norm == ans:
                        score += 1
                        is_correct = 1

                    # 寫入逐題紀錄
                    records.append({
                        "timestamp": now_str,
                        "mode": "batch",
                        "question": question_text,
                        "topic": topic,
                        "user_answer": user_norm,
                        "correct_answer": ans,
                        "is_correct": is_correct
                    })

                    with st.expander(f"第 {idx+1} 題檢討", expanded=(user_norm != ans)):
                        if ans == "":
                            st.warning("⚠️ 此題沒有偵測到標準答案")
                        else:
                            if user_norm == ans:
                                show_correct(f"你的答案：({user_raw})，正確答案：({ans})")
                            else:
                                show_wrong(f"你的答案：({user_raw})，正確答案：({ans})")
                        st.write("解析：")
                        st.write(row.get("explanation", ""))

                # 儲存成績與紀錄
                save_score(score, total)
                save_records(records)

                percent = int(score / total * 100) if total > 0 else 0
                st.metric("最終成績", f"{percent} 分", f"答對 {score}/{total} 題")

                if os.path.exists(score_file):
                    hist = pd.read_csv(score_file)
                    if len(hist) > 0:
                        hist["index"] = range(1, len(hist) + 1)
                        st.markdown("### 📈 歷次模擬考總分")
                        st.line_chart(hist.set_index("index")["percent"])

                if st.button("🔄 再測一次"):
                    st.session_state.quiz_data = None
                    st.session_state.quiz_submitted = False
                    st.rerun()


# ==========================
# 單題即時練習
# ==========================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 單題即時練習")

    df = pd.read_csv(csv_file)
    df = df[df["option_A"].notna() & (df["option_A"] != "")]

    if len(df) == 0:
        st.warning("題庫是空的，請先匯入 PDF 題庫。")
    else:
        if st.button("🎲 抽題"):
            st.session_state.current_single_q = df.sample(1).iloc[0]

        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")

            options = ["1", "2", "3", "4"]
            texts = [
                q.get("option_A", ""),
                q.get("option_B", ""),
                q.get("option_C", ""),
                q.get("option_D", "")
            ]

            def fmt(x):
                i = int(x) - 1
                txt = texts[i] if i < len(texts) else ""
                return f"({x}) {txt}"

            user_raw = st.radio("請選擇", options, format_func=fmt)
            user_norm = normalize_answer(user_raw)

            if st.button("查看答案"):
                ans = normalize_answer(q.get("correct_answer", ""))
                topic = q.get("topic", "未分類")
                question_text = q.get("question", "")

                if ans == "":
                    st.warning("⚠️ 題庫此題沒有標準答案")
                else:
                    if user_norm == ans:
                        show_correct(f"答案正確！({ans})")
                        is_correct = 1
                    else:
                        show_wrong(f"正確答案是 ({ans})")
                        is_correct = 0

                    # 紀錄單題練習
                    save_records([{
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "mode": "single",
                        "question": question_text,
                        "topic": topic,
                        "user_answer": user_norm,
                        "correct_answer": ans,
                        "is_correct": is_correct
                    }])

                st.info("解析：")
                st.write(q.get("explanation", ""))


# ==========================
# 弱點分析 / 錯題本
# ==========================
elif mode == "📉 弱點分析 / 錯題本":
    st.title("📉 PRO 弱點分析 & 錯題本")

    if not os.path.exists(record_file):
        st.info("目前還沒有作答紀錄，請先在『模擬考』或『單題練習』中作答。")
    else:
        rec = pd.read_csv(record_file)
        if len(rec) == 0:
            st.info("records.csv 為空，尚未產生任何作答紀錄。")
        else:
            # ---- Topic 弱點統計 ----
            st.subheader("📊 依主題弱點分析")

            # 確保 topic 欄位存在
            if "topic" not in rec.columns:
                rec["topic"] = "未分類"

            topic_stat = (
                rec
                .groupby("topic")
                .agg(
                    total=("is_correct", "count"),
                    correct=("is_correct", "sum")
                )
                .reset_index()
            )
            topic_stat["accuracy"] = topic_stat["correct"] / topic_stat["total"] * 100
            topic_stat = topic_stat.sort_values("accuracy")

            if len(topic_stat) == 0:
                st.info("目前沒有可分析的主題資料。")
            else:
                st.write("（由弱到強排序）")
                st.dataframe(topic_stat)

                # 簡單柱狀圖：X=主題, Y=正確率
                chart_df = topic_stat.set_index("topic")[["accuracy"]]
                st.bar_chart(chart_df)

                # Top 弱點列表
                weak_topics = topic_stat.head(3)
                st.markdown("### 🎯 目前前三大弱點主題")
                for _, row in weak_topics.iterrows():
                    st.markdown(
                        f"- **{row['topic']}**：答對 {row['correct']}/{row['total']} 題，正確率約 {row['accuracy']:.1f}%"
                    )

            st.markdown("---")
            st.subheader("📚 錯題本練習")

            # 找出常錯的題目：依「question」聚合
            q_stat = (
                rec
                .groupby("question")
                .agg(
                    total=("is_correct", "count"),
                    correct=("is_correct", "sum"),
                    topic=("topic", "first")
                )
                .reset_index()
            )
            q_stat["accuracy"] = q_stat["correct"] / q_stat["total"] * 100
            # 只取曾錯過的題（至少有一次錯誤）
            q_stat["wrong"] = q_stat["total"] - q_stat["correct"]
            weak_questions = q_stat[q_stat["wrong"] > 0].sort_values("accuracy")

            if len(weak_questions) == 0:
                st.info("恭喜，目前沒有累積任何錯題紀錄 🎉")
            else:
                st.write("下方列出你曾經答錯過的題目（照正確率由低到高）：")
                st.dataframe(weak_questions[["question", "topic", "total", "correct", "accuracy"]].head(20))

                st.markdown("### 🎲 從錯題本抽一題再練一次")

                df_all = pd.read_csv(csv_file)
                df_all = df_all[df_all["option_A"].notna() & (df_all["option_A"] != "")]

                if st.button("從錯題本抽題"):
                    # 從 weak_questions 中選一題（隨機）
                    target_q_text = weak_questions.sample(1).iloc[0]["question"]
                    # 從題庫中找到對應題目
                    match = df_all[df_all["question"] == target_q_text]
                    if len(match) == 0:
                        st.warning("題庫中找不到這題的原始資料（可能題庫有重新匯入）。")
                        st.session_state.weak_practice_q = None
                    else:
                        st.session_state.weak_practice_q = match.iloc[0]

                q2 = st.session_state.weak_practice_q
                if q2 is not None:
                    st.markdown(f"#### 錯題本重練題目")
                    st.markdown(f"**題目：** {q2['question']}")
                    options = ["1", "2", "3", "4"]
                    texts = [
                        q2.get("option_A", ""),
                        q2.get("option_B", ""),
                        q2.get("option_C", ""),
                        q2.get("option_D", "")
                    ]

                    def fmt2(x):
                        i = int(x) - 1
                        txt = texts[i] if i < len(texts) else ""
                        return f"({x}) {txt}"

                    user_raw2 = st.radio("請選擇（錯題本）", options, format_func=fmt2, key="weak_radio")
                    user_norm2 = normalize_answer(user_raw2)

                    if st.button("查看這題的答案", key="weak_check"):
                        ans2 = normalize_answer(q2.get("correct_answer", ""))
                        topic2 = q2.get("topic", "未分類")
                        qtext2 = q2.get("question", "")

                        if ans2 == "":
                            st.warning("⚠️ 題庫此題沒有標準答案")
                        else:
                            if user_norm2 == ans2:
                                show_correct(f"答案正確！({ans2})")
                                is_correct2 = 1
                            else:
                                show_wrong(f"正確答案是 ({ans2})")
                                is_correct2 = 0

                            # 記錄到 records
                            save_records([{
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "mode": "weak_practice",
                                "question": qtext2,
                                "topic": topic2,
                                "user_answer": user_norm2,
                                "correct_answer": ans2,
                                "is_correct": is_correct2
                            }])

                        st.info("解析：")
                        st.write(q2.get("explanation", ""))


# ==========================
# PDF 匯入題庫
# ==========================
elif mode == "📂 匯入 PDF 題庫":
    st.title("📂 匯入 PDF 題庫")

    uploaded = st.file_uploader("上傳 PDF 檔", type=["pdf"])

    if uploaded and st.button("解析 PDF"):
        with pdfplumber.open(uploaded) as pdf:
            text = "\n".join([(p.extract_text() or "") for p in pdf.pages])

        data = parse_exam_pdf(text)
        df_new = pd.DataFrame(data)

        st.success(f"成功解析 {len(df_new)} 題")
        st.dataframe(df_new.head())

        # append 進 data.csv
        try:
            old = pd.read_csv(csv_file)
            out = pd.concat([old, df_new], ignore_index=True)
        except Exception:
            out = df_new

        out.to_csv(csv_file, index=False, encoding="utf-8-sig")
        st.success("題庫已更新並寫入 data.csv！")


# ==========================
# Debug 模式
# ==========================
elif mode == "🔧 debug 資料庫檢查":
    st.title("🔧 資料庫檢查")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("📘 題庫 data.csv")
        if os.path.exists(csv_file):
            df = pd.read_csv(csv_file)
            st.write(f"目前共有 {len(df)} 題")
            st.dataframe(df)
            if st.button("清空題庫（刪除 data.csv）"):
                os.remove(csv_file)
                st.success("已刪除 data.csv，下次會自動重建。")
        else:
            st.info("尚未建立 data.csv")

    with col2:
        st.subheader("📊 模擬考成績 scores.csv")
        if os.path.exists(score_file):
            s = pd.read_csv(score_file)
            st.write(f"共有 {len(s)} 筆成績紀錄")
            st.dataframe(s)
            if st.button("清空成績（刪除 scores.csv）"):
                os.remove(score_file)
                st.success("已刪除 scores.csv。")
        else:
            st.info("尚未建立 scores.csv")

    with col3:
        st.subheader("🧪 作答紀錄 records.csv")
        if os.path.exists(record_file):
            r = pd.read_csv(record_file)
            st.write(f"共有 {len(r)} 筆逐題紀錄")
            st.dataframe(r)
            if st.button("清空逐題紀錄（刪除 records.csv）"):
                os.remove(record_file)
                st.success("已刪除 records.csv。")
        else:
            st.info("尚未建立 records.csv")

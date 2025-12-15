import streamlit as st
import pandas as pd
import pdfplumber
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import APIError, WorksheetNotFound

# =========================================================
# 頁面設定
# =========================================================
st.set_page_config(page_title="質子中心-輻防師特訓平台 (雲端版)", layout="wide", page_icon="☢️")

SHEET_NAME = "Pro_Database"  # 你的 Google Sheet 檔名（不是分頁名）

# =========================================================
# Google Sheets 連線
# =========================================================
@st.cache_resource
def init_connection():
    """建立 Google Sheets 連線（從 Streamlit Secrets 讀取 service account 金鑰）"""
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if "gcp_service_account" not in st.secrets:
        st.error("⚠️ 未偵測到 Secrets 設定！請在 Streamlit Cloud 後台設定 [gcp_service_account]。")
        return None

    creds_dict = st.secrets["gcp_service_account"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client


def get_or_create_worksheet(sh, name, rows=1000, cols=10):
    """
    強化版：避免 Streamlit rerun / 多 session 併發時重複建立同名 sheet。
    就算 add_worksheet 回 400 already exists，也能安全拿回現有的 worksheet。
    """
    name = str(name).strip()

    # 1) 先直接拿（最快）
    try:
        return sh.worksheet(name)
    except WorksheetNotFound:
        pass

    # 2) 再掃一次（有時候 API list 比 worksheet() 穩）
    try:
        for ws in sh.worksheets():
            if ws.title.strip() == name:
                return ws
    except Exception:
        pass

    # 3) 嘗試建立；若撞名（already exists）就回頭拿現成的
    try:
        ws = sh.add_worksheet(title=name, rows=rows, cols=cols)
    except APIError as e:
        msg = str(e)
        if ("already exists" in msg) or ("addSheet" in msg):
            return sh.worksheet(name)
        raise

    # 4) 初始化標題（新建時才做）
    headers = [
        "question", "option_A", "option_B", "option_C", "option_D",
        "correct_answer", "explanation", "topic", "type"
    ]
    ws.append_row(headers)
    return ws


# =========================================================
# 資料讀寫
# =========================================================
def load_data(worksheet_name):
    """從 Google Sheet 分頁讀取資料轉為 DataFrame"""
    try:
        client = init_connection()
        if not client:
            return pd.DataFrame(columns=[
                "question", "option_A", "option_B", "option_C", "option_D",
                "correct_answer", "explanation", "topic", "type"
            ])

        sh = client.open(SHEET_NAME)
        ws = get_or_create_worksheet(sh, worksheet_name)

        data = ws.get_all_records()
        df = pd.DataFrame(data)

        if df.empty:
            return pd.DataFrame(columns=[
                "question", "option_A", "option_B", "option_C", "option_D",
                "correct_answer", "explanation", "topic", "type"
            ])
        return df

    except Exception as e:
        st.error(
            "連線錯誤：請確認 Secrets 設定正確且已共用權限給 Service Account。\n"
            f"詳細錯誤: {e}"
        )
        return pd.DataFrame(columns=[
            "question", "option_A", "option_B", "option_C", "option_D",
            "correct_answer", "explanation", "topic", "type"
        ])


def save_to_google(worksheet_name, new_df: pd.DataFrame):
    """將 DataFrame 覆蓋寫入 Google Sheet 分頁"""
    try:
        client = init_connection()
        if not client:
            st.error("❌ 無法建立 Google Sheets 連線（Secrets 可能未設定）")
            return

        sh = client.open(SHEET_NAME)
        ws = get_or_create_worksheet(sh, worksheet_name)

        ws.clear()
        if new_df is None or new_df.empty:
            # 至少保留標題列
            ws.update([[
                "question", "option_A", "option_B", "option_C", "option_D",
                "correct_answer", "explanation", "topic", "type"
            ]])
            return

        ws.update([new_df.columns.values.tolist()] + new_df.values.tolist())

    except Exception as e:
        st.error(f"寫入失敗: {e}")


# =========================================================
# Session State 初始化
# =========================================================
if "quiz_data" not in st.session_state:
    st.session_state.quiz_data = None
if "quiz_submitted" not in st.session_state:
    st.session_state.quiz_submitted = False
if "current_single_q" not in st.session_state:
    st.session_state.current_single_q = None
if "single_q_revealed" not in st.session_state:
    st.session_state.single_q_revealed = False


# =========================================================
# 工具函式
# =========================================================
def normalize_answer(ans):
    if pd.isna(ans):
        return ""
    ans = str(ans).strip().upper()
    ans = ans.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    mapping = {"1": "A", "2": "B", "3": "C", "4": "D", "A": "A", "B": "B", "C": "C", "D": "D"}
    return mapping.get(ans, ans)


def extract_answer_key(text):
    if pd.isna(text):
        return ""
    text = str(text).strip()
    match = re.match(r"^[\(（]?([1-4A-Da-d])[\)）\.]?", text)
    if match:
        val = match.group(1).upper()
        mapping = {"1": "A", "2": "B", "3": "C", "4": "D"}
        return mapping.get(val, val)
    return ""


def parse_exam_pdf(text):
    """v7.0 解析邏輯"""
    questions = []
    lines = text.split("\n")
    current_q = {}
    state = "SEARCH_Q"

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 新題目
        if re.match(r"^\d+[\.\s]", line):
            if current_q and "question" in current_q:
                if "correct_answer" not in current_q:
                    current_q["correct_answer"] = ""
                questions.append(current_q)

            current_q = {
                "question": line,
                "option_A": "",
                "option_B": "",
                "option_C": "",
                "option_D": "",
                "correct_answer": "",
                "explanation": "",
                "topic": "",
                "type": "choice",
            }
            state = "READING_Q"
            continue

        # 解答段落
        if "[解:]" in line or "[解]" in line:
            clean_line = line.replace("[解:]", "").replace("[解]", "").strip()
            if clean_line:
                ans = extract_answer_key(clean_line)
                if ans and current_q:
                    current_q["correct_answer"] = ans
                    current_q["explanation"] = clean_line
                state = "READING_EXPL"
            else:
                state = "WAITING_FOR_ANS"
            continue

        # 題幹續行 or 進入選項
        if state == "READING_Q":
            if re.match(r"^\(1\)|^\(A\)|^A\.|^1\.", line) or ("(1)" in line and "(2)" in line):
                state = "READING_OPT"
            else:
                current_q["question"] += " " + line
                continue

        # 等待答案
        if state == "WAITING_FOR_ANS":
            if current_q:
                ans = extract_answer_key(line)
                if ans:
                    current_q["correct_answer"] = ans
                    current_q["explanation"] += line
                else:
                    current_q["explanation"] += line
            state = "READING_EXPL"
            continue

        # 選項解析
        if state == "READING_OPT":
            if "(1)" in line and "(2)" in line:
                parts = re.split(r"(?=\(\d\))", line)
                for part in parts:
                    part = part.strip()
                    if part.startswith("(1)"):
                        current_q["option_A"] = part
                    elif part.startswith("(2)"):
                        current_q["option_B"] = part
                    elif part.startswith("(3)"):
                        current_q["option_C"] = part
                    elif part.startswith("(4)"):
                        current_q["option_D"] = part
            elif line.startswith("(1)"):
                current_q["option_A"] = line
            elif line.startswith("(2)"):
                current_q["option_B"] = line
            elif line.startswith("(3)"):
                current_q["option_C"] = line
            elif line.startswith("(4)"):
                current_q["option_D"] = line

        # 解析續行
        if state == "READING_EXPL":
            if not current_q.get("correct_answer"):
                ans = extract_answer_key(line)
                if ans:
                    current_q["correct_answer"] = ans
            current_q["explanation"] += line + "\n"

    if current_q and "question" in current_q:
        questions.append(current_q)

    return questions


# =========================================================
# Sidebar
# =========================================================
with st.sidebar:
    st.title("☁️ 雲端功能選單")
    mode = st.radio(
        "模式",
        [
            "📝 模擬考模式",
            "📕 錯題本 (雲端同步)",
            "⚡ 單題即時練習",
            "📂 匯入 PDF (上傳雲端)",
            "debug 雲端資料檢查",
        ],
    )
    st.markdown("---")

    if "gcp_service_account" in st.secrets:
        st.success("✅ Secrets 金鑰已偵測")
    else:
        st.error("⚠️ 未偵測到 Secrets！")


# =========================================================
# 功能 1: 模擬考
# =========================================================
if mode == "📝 模擬考模式":
    st.title("📝 雲端題庫模擬考")
    df = load_data("Questions")

    if not df.empty:
        valid_df = df[df["question"].notna() & df["correct_answer"].notna()]
        choice_df = valid_df[valid_df["option_A"].notna() & (valid_df["option_A"] != "")]

        if len(choice_df) == 0:
            st.warning("雲端題庫是空的，請先匯入 PDF。")
        else:
            if st.session_state.quiz_data is None:
                st.info(f"雲端題庫共有 {len(choice_df)} 題。")
                num = st.number_input("題數", 1, len(choice_df), min(20, len(choice_df)))
                if st.button("🚀 開始測驗", type="primary"):
                    st.session_state.quiz_data = choice_df.sample(n=num).reset_index(drop=True)
                    st.session_state.quiz_submitted = False
                    st.rerun()
            else:
                with st.form("quiz_form"):
                    user_answers = {}
                    for index, row in st.session_state.quiz_data.iterrows():
                        st.markdown(f"**Q{index+1}:** {row['question']}")
                        opts = ["A", "B", "C", "D"]
                        opt_labels = [
                            str(row.get("option_A", "")),
                            str(row.get("option_B", "")),
                            str(row.get("option_C", "")),
                            str(row.get("option_D", "")),
                        ]
                        clean_labels = [l.replace("nan", "") for l in opt_labels]

                        user_answers[index] = st.radio(
                            f"A{index}",
                            opts,
                            key=f"q_{index}",
                            label_visibility="collapsed",
                            format_func=lambda x: clean_labels[opts.index(x)],
                        )
                        st.markdown("---")

                    if st.form_submit_button("📝 交卷"):
                        st.session_state.quiz_submitted = True

                if st.session_state.quiz_submitted:
                    score = 0
                    wrong_entries = []

                    for index, row in st.session_state.quiz_data.iterrows():
                        user = user_answers.get(index)
                        ans = extract_answer_key(row.get("correct_answer", ""))

                        if user == ans:
                            score += 1
                        else:
                            wrong_entries.append(row)

                        with st.expander(f"第 {index+1} 題檢討", expanded=(user != ans)):
                            opt_texts = [
                                str(row.get("option_A")),
                                str(row.get("option_B")),
                                str(row.get("option_C")),
                                str(row.get("option_D")),
                            ]
                            try:
                                correct_text = opt_texts[["A", "B", "C", "D"].index(ans)]
                            except Exception:
                                correct_text = ans

                            if user == ans:
                                st.success(f"答對！{correct_text}")
                            else:
                                st.error(f"答錯！正確：{correct_text}")
                            st.write(f"解析：{row.get('explanation', '')}")

                    if wrong_entries:
                        wrong_df = pd.DataFrame(wrong_entries)
                        old_mistakes = load_data("Mistakes")
                        final_mistakes = pd.concat([old_mistakes, wrong_df], ignore_index=True)
                        final_mistakes.drop_duplicates(subset=["question"], keep="last", inplace=True)
                        save_to_google("Mistakes", final_mistakes)
                        st.toast(f"已同步 {len(wrong_entries)} 題到雲端錯題本！", icon="☁️")

                    st.metric("成績", f"{int(score/len(st.session_state.quiz_data)*100)} 分")
                    if st.button("🔄 重測"):
                        st.session_state.quiz_data = None
                        st.session_state.quiz_submitted = False
                        st.rerun()

# =========================================================
# 功能 2: 錯題本
# =========================================================
elif mode == "📕 錯題本 (雲端同步)":
    st.title("📕 雲端錯題本")
    mistake_df = load_data("Mistakes")

    if mistake_df.empty:
        st.success("☁️ 雲端錯題本是空的！")
    else:
        st.write(f"目前雲端累積：{len(mistake_df)} 題")
        if st.button("🎲 抽題練習"):
            st.session_state.current_single_q = mistake_df.sample(1).iloc[0]
            st.session_state.single_q_revealed = False

        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")
            opts = ["A", "B", "C", "D"]
            opt_labels = [
                str(q.get("option_A", "")),
                str(q.get("option_B", "")),
                str(q.get("option_C", "")),
                str(q.get("option_D", "")),
            ]
            clean_labels = [l.replace("nan", "") for l in opt_labels]

            user_ans = st.radio(
                "選",
                opts,
                label_visibility="collapsed",
                format_func=lambda x: clean_labels[opts.index(x)],
            )

            c1, c2 = st.columns(2)
            with c1:
                if st.button("看答案"):
                    st.session_state.single_q_revealed = True

            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get("correct_answer", ""))
                if user_ans == ans:
                    st.success("答對！")
                    with c2:
                        if st.button("🗑️ 從雲端移除"):
                            latest_mistakes = load_data("Mistakes")
                            new_mistakes = latest_mistakes[latest_mistakes["question"] != q["question"]]
                            save_to_google("Mistakes", new_mistakes)
                            st.success("已移除")
                            st.session_state.current_single_q = None
                            st.rerun()
                else:
                    try:
                        txt = clean_labels[["A", "B", "C", "D"].index(ans)]
                    except Exception:
                        txt = ans
                    st.error(f"答錯，正確是：{txt}")

                st.info(f"解析：{q.get('explanation','')}")

# =========================================================
# 功能 3: 單題練習
# =========================================================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 雲端單題刷")
    df = load_data("Questions")
    choice_df = df[df["option_A"].notna() & (df["option_A"] != "")]

    if not choice_df.empty:
        if st.button("🎲 抽題"):
            st.session_state.current_single_q = choice_df.sample(1).iloc[0]
            st.session_state.single_q_revealed = False

        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")
            opts = ["A", "B", "C", "D"]
            opt_labels = [
                str(q.get("option_A", "")),
                str(q.get("option_B", "")),
                str(q.get("option_C", "")),
                str(q.get("option_D", "")),
            ]
            clean_labels = [l.replace("nan", "") for l in opt_labels]

            user_ans = st.radio(
                "選",
                opts,
                label_visibility="collapsed",
                format_func=lambda x: clean_labels[opts.index(x)],
            )

            if st.button("看答案"):
                st.session_state.single_q_revealed = True

            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get("correct_answer", ""))
                if user_ans == ans:
                    st.success("Correct!")
                else:
                    try:
                        txt = clean_labels[["A", "B", "C", "D"].index(ans)]
                    except Exception:
                        txt = ans
                    st.error(f"Answer: {txt}")

                    old_mistakes = load_data("Mistakes")
                    new_mistakes = pd.concat([old_mistakes, pd.DataFrame([q])], ignore_index=True)
                    new_mistakes.drop_duplicates(subset=["question"], keep="last", inplace=True)
                    save_to_google("Mistakes", new_mistakes)
                    st.caption("已同步到雲端錯題本")

                st.info(f"解析：{q.get('explanation','')}")

    else:
        st.warning("無題目")

# =========================================================
# 功能 4: PDF 匯入
# =========================================================
elif mode == "📂 匯入 PDF (上傳雲端)":
    st.title("📂 匯入並上傳 Google Sheet")
    uploaded_file = st.file_uploader("PDF", type=["pdf"])

    if uploaded_file and st.button("解析並上傳"):
        with pdfplumber.open(uploaded_file) as pdf:
            text = ""
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"

        data = parse_exam_pdf(text)
        if data:
            new_df = pd.DataFrame(data)
            st.success(f"解析成功 {len(new_df)} 題")

            old_df = load_data("Questions")
            final_df = pd.concat([old_df, new_df], ignore_index=True)
            final_df.drop_duplicates(subset=["question"], keep="last", inplace=True)

            save_to_google("Questions", final_df)
            st.success("✅ 已成功寫入 Google Sheet！")
        else:
            st.error("❌ 解析不到題目，請確認 PDF 格式是否可被擷取文字（不是掃描圖）。")

# =========================================================
# Debug
# =========================================================
elif mode == "debug 雲端資料檢查":
    st.write("Questions 表：")
    st.dataframe(load_data("Questions"))
    st.write("Mistakes 表：")
    st.dataframe(load_data("Mistakes"))

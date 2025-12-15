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
    """
    v7.2 修正版：
    - 支援 [解:] / [解：] / [解]
    - 選項行不再只吃行首，改成「一行內所有 (1)(2)(3)(4) 全部拆開」
    - 選項跨行：若上一行是選項，下一行沒有新 (n) 記號就接到上一個選項後面
    - 忽略頁尾：第X頁/共Y頁
    """
    questions = []
    lines = text.split("\n")

    current_q = None
    state = "SEARCH_Q"
    last_opt = None  # option_A/B/C/D

    def is_footer(s: str) -> bool:
        return bool(re.match(r"^第\s*\d+\s*頁/共\s*\d+\s*頁", s.strip()))

    def is_answer_marker(s: str) -> bool:
        return bool(re.search(r"\[解(?:[:：])?\]", s))

    def split_options_anywhere(s: str):
        """
        把一行內所有選項拆開：
        支援 (1) 或 （1）
        回傳 dict: {"1": "(1)....", "2": "(2)....", ...}
        """
        pat = r"[（(]([1-4])[）)]"
        hits = list(re.finditer(pat, s))
        if not hits:
            return {}

        out = {}
        for i, m in enumerate(hits):
            n = m.group(1)
            start = m.start()
            end = hits[i + 1].start() if i + 1 < len(hits) else len(s)
            chunk = s[start:end].strip()
            out[n] = chunk
        return out

    for raw in lines:
        line = raw.strip()
        if not line or is_footer(line):
            continue

        # 新題目
        if re.match(r"^\d+[\.\s]", line):
            if current_q and "question" in current_q:
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
            last_opt = None
            continue

        if current_q is None:
            continue

        # 解答標記
        if is_answer_marker(line):
            after = re.sub(r".*\[解(?:[:：])?\]\s*", "", line).strip()
            if after:
                ans = extract_answer_key(after)
                if ans:
                    current_q["correct_answer"] = ans
                current_q["explanation"] += after + "\n"
                state = "READING_EXPL"
            else:
                state = "WAITING_FOR_ANS"
            last_opt = None
            continue

        # 等待答案那一行（通常是 (3)）
        if state == "WAITING_FOR_ANS":
            ans = extract_answer_key(line)
            if ans and not current_q.get("correct_answer"):
                current_q["correct_answer"] = ans
            current_q["explanation"] += line + "\n"
            state = "READING_EXPL"
            continue

        # 讀題幹（直到遇到選項）
        if state == "READING_Q":
            # 只要一行出現任何 (1)-(4)，就視為進入選項
            if split_options_anywhere(line):
                state = "READING_OPT"
            else:
                current_q["question"] += " " + line
                continue

        # 讀選項（核心修正：不管記號在不在行首，都拆）
        if state == "READING_OPT":
            opts = split_options_anywhere(line)
            if opts:
                if "1" in opts:
                    current_q["option_A"] = opts["1"]
                    last_opt = "option_A"
                if "2" in opts:
                    current_q["option_B"] = opts["2"]
                    last_opt = "option_B"
                if "3" in opts:
                    current_q["option_C"] = opts["3"]
                    last_opt = "option_C"
                if "4" in opts:
                    current_q["option_D"] = opts["4"]
                    last_opt = "option_D"
                continue

            # 沒有新選項記號 → 當作上一個選項的續行
            if last_opt:
                current_q[last_opt] = (current_q[last_opt] + " " + line).strip()
                continue

        # 讀解析
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

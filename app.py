import streamlit as st
import pandas as pd
import pdfplumber
import re
import gspread
import hashlib, hmac
from datetime import datetime, timezone, timedelta
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import APIError, WorksheetNotFound

# =========================================================
# 基本設定
# =========================================================
st.set_page_config(page_title="質子中心-輻防師特訓平台 (雲端版)", layout="wide", page_icon="☢️")
TZ_TAIPEI = timezone(timedelta(hours=8))

SHEET_NAME = "Pro_Database"  # Google Sheet 檔名（不是分頁名）

# 介面語句（可自訂）
MSG_CORRECT = "還可以嘛！👌"
MSG_WRONG = "到底行不行啊！😤"

EXPECTED_Q_COLS = [
    "question", "option_A", "option_B", "option_C", "option_D",
    "correct_answer", "explanation", "topic", "type"
]
USER_COLS = ["username", "password_hash", "role", "created_at", "enabled"]
RESULT_COLS = ["ts", "username", "mode", "score", "total", "percent", "wrong_count"]

DEFAULT_HEADERS = {
    "Questions": EXPECTED_Q_COLS,
    "Mistakes": EXPECTED_Q_COLS,
    "Users": USER_COLS,
    "Results": RESULT_COLS,
}

# =========================================================
# Google Sheets 連線
# =========================================================
@st.cache_resource
def init_connection():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if "gcp_service_account" not in st.secrets:
        st.error("⚠️ 未偵測到 Secrets 設定！請在 Streamlit Cloud 後台設定 [gcp_service_account]。")
        return None

    creds_dict = st.secrets["gcp_service_account"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)


def get_or_create_worksheet(sh, name, rows=2000, cols=30):
    """
    強化版：避免 Streamlit rerun / 多 session 併發時重複建立同名 sheet。
    就算 add_worksheet 回 400 already exists，也能安全拿回現有 worksheet。
    若是新建，會自動寫入對應的 header。
    """
    name = str(name).strip()

    # 1) 先直接拿
    try:
        return sh.worksheet(name)
    except WorksheetNotFound:
        pass

    # 2) 再掃一次
    try:
        for ws in sh.worksheets():
            if ws.title.strip() == name:
                return ws
    except Exception:
        pass

    # 3) 建立（撞名就回頭拿現成）
    try:
        ws = sh.add_worksheet(title=name, rows=rows, cols=cols)
    except APIError as e:
        msg = str(e)
        if ("already exists" in msg) or ("addSheet" in msg):
            return sh.worksheet(name)
        raise

    # 4) 新建才寫 header
    headers = DEFAULT_HEADERS.get(name)
    if headers:
        ws.append_row(headers)
    return ws


# =========================================================
# Auth（簡單帳號密碼 / 成績紀錄）
# =========================================================
def _get_auth_pepper():
    # 建議在 secrets 加：auth_pepper = "一串很亂很長的字串"
    return st.secrets.get("auth_pepper", "CHANGE_ME_PLEASE")

def hash_password(password: str, salt: str) -> str:
    pepper = _get_auth_pepper().encode("utf-8")
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        (password.strip().encode("utf-8") + pepper),
        salt.encode("utf-8"),
        120_000,
    )
    return dk.hex()

def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt), str(stored_hash))

# =========================================================
# 資料讀寫（Questions/Mistakes/Users/Results）
# =========================================================
def load_data(worksheet_name: str) -> pd.DataFrame:
    """通用讀取：保證回傳 DataFrame，且必要欄位會補齊"""
    expected = DEFAULT_HEADERS.get(worksheet_name, None)

    try:
        client = init_connection()
        if not client:
            return pd.DataFrame(columns=expected or [])

        sh = client.open(SHEET_NAME)
        ws = get_or_create_worksheet(sh, worksheet_name)

        data = ws.get_all_records()
        if not data:
            return pd.DataFrame(columns=expected or [])

        df = pd.DataFrame(data)

        # 補欄位
        if expected:
            for c in expected:
                if c not in df.columns:
                    df[c] = ""
            return df[expected]

        return df

    except Exception as e:
        st.error(
            "連線/資料錯誤（可能是欄位被刪、或資料表空白）\n"
            f"詳細錯誤: {repr(e)}"
        )
        return pd.DataFrame(columns=expected or [])


def save_to_google(worksheet_name: str, new_df: pd.DataFrame):
    """覆蓋寫入（適用 Questions / Mistakes / Users），Results 請用 append_result"""
    try:
        client = init_connection()
        if not client:
            st.error("❌ 無法建立 Google Sheets 連線（Secrets 可能未設定）")
            return

        sh = client.open(SHEET_NAME)
        ws = get_or_create_worksheet(sh, worksheet_name)

        expected = DEFAULT_HEADERS.get(worksheet_name)
        if expected:
            for c in expected:
                if c not in new_df.columns:
                    new_df[c] = ""
            new_df = new_df[expected]

        ws.clear()
        if new_df is None or new_df.empty:
            ws.update([expected or []])
            return

        ws.update([new_df.columns.values.tolist()] + new_df.values.tolist())

    except Exception as e:
        st.error(f"寫入失敗: {repr(e)}")


def append_result(row: dict):
    """追加寫入 Results（不要 clear，不然大家成績會互相洗掉）"""
    try:
        client = init_connection()
        if not client:
            st.error("❌ 無法建立 Google Sheets 連線")
            return

        sh = client.open(SHEET_NAME)
        ws = get_or_create_worksheet(sh, "Results", rows=8000, cols=20)

        # 若表是空的（沒 header），補 header
        values = ws.get_all_values()
        if not values:
            ws.append_row(RESULT_COLS)

        ws.append_row([row.get(c, "") for c in RESULT_COLS])

    except Exception as e:
        st.error(f"成績寫入失敗: {repr(e)}")


def load_users() -> pd.DataFrame:
    df = load_data("Users")
    for c in USER_COLS:
        if c not in df.columns:
            df[c] = ""
    # enabled 預設 true
    if "enabled" in df.columns:
        df["enabled"] = df["enabled"].astype(str).replace({"": "TRUE"})
    return df[USER_COLS]


def save_users(df: pd.DataFrame):
    for c in USER_COLS:
        if c not in df.columns:
            df[c] = ""
    save_to_google("Users", df[USER_COLS])


def load_results() -> pd.DataFrame:
    df = load_data("Results")
    for c in RESULT_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[RESULT_COLS]


# =========================================================
# 題目工具
# =========================================================
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
    v7.2+：
    - 支援 [解:] / [解：] / [解]
    - 選項記號可在行中，會完整拆 (1)(2)(3)(4)
    - 選項跨行：沒有新 (n) 記號就接到上一個選項
    - 題型辨識：少於 3 個選項 => essay（避免把(1)(2)子題當選擇）
    - 忽略頁尾：第X頁/共Y頁
    """
    questions = []
    lines = text.split("\n")

    current_q = None
    state = "SEARCH_Q"
    last_opt = None

    def is_footer(s: str) -> bool:
        return bool(re.match(r"^第\s*\d+\s*頁/共\s*\d+\s*頁", s.strip()))

    def is_answer_marker(s: str) -> bool:
        return bool(re.search(r"\[解(?:[:：])?\]", s))

    def split_options_anywhere(s: str):
        # 支援 (1) 或 （1）
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

    def finalize_question(q: dict) -> dict:
        opts = [
            str(q.get("option_A", "")).strip(),
            str(q.get("option_B", "")).strip(),
            str(q.get("option_C", "")).strip(),
            str(q.get("option_D", "")).strip(),
        ]
        non_empty = [o for o in opts if o]

        # 少於 3 個選項：視為非選擇題（(1)(2)子題很常見）
        if len(non_empty) < 3:
            q["type"] = "essay"
            # 把可能被誤塞進選項的內容搬到 explanation（不要丟資料）
            extra = []
            if q.get("option_A"): extra.append(q["option_A"])
            if q.get("option_B"): extra.append(q["option_B"])
            if q.get("option_C"): extra.append(q["option_C"])
            if q.get("option_D"): extra.append(q["option_D"])
            if extra and not q.get("explanation"):
                q["explanation"] = "\n".join(extra)

            q["option_A"] = q["option_B"] = q["option_C"] = q["option_D"] = ""
            q["correct_answer"] = ""
        else:
            q["type"] = "choice"
        return q

    for raw in lines:
        line = raw.strip()
        if not line or is_footer(line):
            continue

        # 新題目（題號 1. / 1 ）
        if re.match(r"^\d+[\.\s]", line):
            if current_q and "question" in current_q:
                questions.append(finalize_question(current_q))

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

        # 等待答案那行（通常只有 (3)）
        if state == "WAITING_FOR_ANS":
            ans = extract_answer_key(line)
            if ans and not current_q.get("correct_answer"):
                current_q["correct_answer"] = ans
            current_q["explanation"] += line + "\n"
            state = "READING_EXPL"
            continue

        # 讀題幹：直到遇到任何 (1)-(4)
        if state == "READING_Q":
            if split_options_anywhere(line):
                state = "READING_OPT"
            else:
                current_q["question"] += " " + line
                continue

        # 讀選項：一行內可同時有多個 (n)
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

            # 沒有新選項記號 -> 接到上一個選項
            if last_opt:
                current_q[last_opt] = (current_q[last_opt] + " " + line).strip()
                continue

        # 解析內容
        if state == "READING_EXPL":
            if not current_q.get("correct_answer"):
                ans = extract_answer_key(line)
                if ans:
                    current_q["correct_answer"] = ans
            current_q["explanation"] += line + "\n"

    if current_q and "question" in current_q:
        questions.append(finalize_question(current_q))

    return questions


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
if "user" not in st.session_state:
    st.session_state.user = None


# =========================================================
# Sidebar：登入/註冊 + 模式
# =========================================================
with st.sidebar:
    st.title("🔋 強強輻防師充電站｜功能選單")

    # 未登入：登入/註冊
    if st.session_state.user is None:
        tab1, tab2 = st.tabs(["登入", "註冊"])

        with tab1:
            u = st.text_input("帳號", key="login_u")
            p = st.text_input("密碼", type="password", key="login_p")
            if st.button("🔐 登入"):
                users = load_users()
                hit = users[(users["username"].astype(str).str.strip() == u.strip())]
                if hit.empty:
                    st.error("帳號不存在")
                else:
                    row = hit.iloc[0]
                    enabled = str(row.get("enabled", "TRUE")).upper() != "FALSE"
                    if not enabled:
                        st.error("此帳號已停用")
                    else:
                        if verify_password(p, u.strip(), row["password_hash"]):
                            st.session_state.user = {"username": u.strip(), "role": row.get("role", "user") or "user"}
                            st.success("登入成功")
                            st.rerun()
                        else:
                            st.error("密碼錯誤")

        with tab2:
            u2 = st.text_input("新帳號", key="reg_u")
            p2 = st.text_input("新密碼", type="password", key="reg_p")
            p3 = st.text_input("再輸入一次新密碼", type="password", key="reg_p2")

            if st.button("🆕 建立帳號"):
                u2 = u2.strip()
                if not u2:
                    st.error("帳號不能空白")
                elif len(u2) < 3:
                    st.error("帳號至少 3 個字")
                elif p2 != p3:
                    st.error("兩次密碼不一致")
                elif len(p2) < 6:
                    st.error("密碼至少 6 個字")
                else:
                    users = load_users()
                    if (users["username"].astype(str).str.strip() == u2).any():
                        st.error("此帳號已存在")
                    else:
                        created = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
                        # 第一個帳號自動 admin（省事）
                        role = "admin" if users[users["username"].astype(str).str.strip() != ""].empty else "user"
                        new_row = {
                            "username": u2,
                            "password_hash": hash_password(p2, u2),
                            "role": role,
                            "created_at": created,
                            "enabled": "TRUE",
                        }
                        users = pd.concat([users, pd.DataFrame([new_row])], ignore_index=True)
                        save_users(users)
                        st.success(f"建立成功（角色：{role}）")
                        st.info("回到登入頁登入即可")

        st.stop()

    # 已登入
    st.success(f"✅ 已登入：{st.session_state.user['username']} ({st.session_state.user['role']})")
    if st.button("🚪 登出"):
        st.session_state.user = None
        st.session_state.quiz_data = None
        st.session_state.quiz_submitted = False
        st.session_state.current_single_q = None
        st.session_state.single_q_revealed = False
        st.rerun()

    modes = [
        "📝 模擬考模式",
        "📕 錯題本 (雲端同步)",
        "⚡ 單題即時練習",
        "📂 匯入 PDF (上傳雲端)",
        "debug 雲端資料檢查",
    ]
    if st.session_state.user["role"] == "admin":
        modes.insert(0, "📊 管理者後台（成績）")
        modes.insert(1, "👤 管理者後台（帳號）")

    mode = st.radio("模式", modes)
    st.markdown("---")


# =========================================================
# 管理者後台：成績
# =========================================================
if mode == "📊 管理者後台（成績）":
    if st.session_state.user["role"] != "admin":
        st.error("你不是管理者 😼")
        st.stop()

    st.title("📊 管理者後台：成績總覽")
    res = load_results()

    if res.empty:
        st.info("目前沒有任何測驗紀錄")
        st.stop()

    res["percent_num"] = pd.to_numeric(res["percent"], errors="coerce")

    users = sorted([u for u in res["username"].astype(str).unique() if u.strip() != ""])
    pick = st.multiselect("篩選使用者", users, default=users)

    view = res[res["username"].astype(str).isin(pick)].copy()
    st.dataframe(view.drop(columns=["percent_num"], errors="ignore"), use_container_width=True)

    st.subheader("📌 使用者平均分數（%）")
    agg = (
        view.groupby("username")["percent_num"]
        .mean()
        .reset_index()
        .sort_values("percent_num", ascending=False)
    )
    st.dataframe(agg, use_container_width=True)
    st.stop()


# =========================================================
# 管理者後台：帳號（停用/啟用）
# =========================================================
if mode == "👤 管理者後台（帳號）":
    if st.session_state.user["role"] != "admin":
        st.error("你不是管理者 😼")
        st.stop()

    st.title("👤 管理者後台：帳號管理")
    users = load_users()

    if users.empty:
        st.info("目前沒有使用者（通常不會發生）")
        st.stop()

    st.dataframe(users, use_container_width=True)

    st.subheader("停用/啟用帳號")
    all_users = [u for u in users["username"].astype(str).tolist() if u.strip() != ""]
    target = st.selectbox("選擇帳號", all_users)

    cur = users[users["username"].astype(str) == str(target)]
    cur_enabled = True
    if not cur.empty:
        cur_enabled = str(cur.iloc[0].get("enabled", "TRUE")).upper() != "FALSE"

    col1, col2 = st.columns(2)
    with col1:
        if st.button("❌ 停用", disabled=(not cur_enabled) or (target == st.session_state.user["username"])):
            users.loc[users["username"].astype(str) == str(target), "enabled"] = "FALSE"
            save_users(users)
            st.success("已停用")
            st.rerun()
    with col2:
        if st.button("✅ 啟用", disabled=cur_enabled):
            users.loc[users["username"].astype(str) == str(target), "enabled"] = "TRUE"
            save_users(users)
            st.success("已啟用")
            st.rerun()

    st.caption("⚠️ 不能停用自己（避免你把自己鎖在門外）")
    st.stop()


# =========================================================
# 功能 1: 模擬考
# =========================================================
if mode == "📝 模擬考模式":
    st.title("📝 雲端題庫模擬考")
    df = load_data("Questions")

    # 只抓 choice 題 + 選項至少三個 + 有答案
    if not df.empty:
        df["type"] = df["type"].astype(str).replace({"": "choice"})
        df["correct_answer"] = df["correct_answer"].astype(str)

        valid_df = df[df["question"].notna() & (df["question"].astype(str).str.strip() != "")]
        choice_df = valid_df[valid_df["type"].astype(str).str.lower().eq("choice")].copy()

        def opt_count(r):
            opts = [
                str(r.get("option_A", "")).strip(),
                str(r.get("option_B", "")).strip(),
                str(r.get("option_C", "")).strip(),
                str(r.get("option_D", "")).strip(),
            ]
            return sum(1 for o in opts if o and o.lower() != "nan")

        if not choice_df.empty:
            choice_df["opt_cnt"] = choice_df.apply(opt_count, axis=1)
            choice_df = choice_df[
                (choice_df["opt_cnt"] >= 3)
                & (choice_df["correct_answer"].astype(str).str.strip() != "")
            ].drop(columns=["opt_cnt"], errors="ignore")

        if len(choice_df) == 0:
            st.warning("雲端題庫沒有可用的選擇題（請先匯入 PDF 或檢查解析結果）。")
        else:
            if st.session_state.quiz_data is None:
                st.info(f"雲端可用選擇題：{len(choice_df)} 題。")
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
                        clean_labels = [l.replace("nan", "").strip() for l in opt_labels]

                        user_answers[index] = st.radio(
                            f"q_{index}",
                            opts,
                            key=f"q_{index}",
                            label_visibility="collapsed",
                            format_func=lambda x: clean_labels[opts.index(x)] if clean_labels[opts.index(x)] else f"{x}（空）"
                        )
                        st.markdown("---")

                    if st.form_submit_button("📝 交卷"):
                        st.session_state.quiz_submitted = True

                if st.session_state.quiz_submitted:
                    score = 0
                    wrong_entries = []
                    total = len(st.session_state.quiz_data)

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
                                st.success(f"{MSG_CORRECT} {correct_text}")
                            else:
                                st.error(f"{MSG_WRONG} 正確是：{correct_text}")
                            st.write(f"解析：{row.get('explanation', '')}")

                    # 同步錯題
                    if wrong_entries:
                        wrong_df = pd.DataFrame(wrong_entries)
                        old_mistakes = load_data("Mistakes")
                        final_mistakes = pd.concat([old_mistakes, wrong_df], ignore_index=True)
                        final_mistakes.drop_duplicates(subset=["question"], keep="last", inplace=True)
                        save_to_google("Mistakes", final_mistakes)
                        st.toast(f"已同步 {len(wrong_entries)} 題到雲端錯題本！", icon="☁️")

                    percent = int(score / total * 100) if total else 0
                    st.metric("成績", f"{percent} 分")

                    # 寫入 Results
                    append_result({
                        "ts": datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
                        "username": st.session_state.user["username"],
                        "mode": "mock_exam",
                        "score": score,
                        "total": total,
                        "percent": percent,
                        "wrong_count": total - score,
                    })

                    if st.button("🔄 重測"):
                        st.session_state.quiz_data = None
                        st.session_state.quiz_submitted = False
                        st.rerun()
    else:
        st.warning("題庫目前是空的，請先匯入 PDF。")


# =========================================================
# 功能 2: 錯題本
# =========================================================
elif mode == "📕 錯題本 (雲端同步)":
    st.title("📕 雲端錯題本")
    mistake_df = load_data("Mistakes")

    if mistake_df.empty:
        st.success("☁️ 雲端錯題本是空的！")
    else:
        mistake_df["type"] = mistake_df["type"].astype(str).replace({"": "choice"})
        mistake_df = mistake_df[mistake_df["type"].astype(str).str.lower().eq("choice")]

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
            clean_labels = [l.replace("nan", "").strip() for l in opt_labels]

            user_ans = st.radio(
                "選",
                opts,
                label_visibility="collapsed",
                format_func=lambda x: clean_labels[opts.index(x)] if clean_labels[opts.index(x)] else f"{x}（空）",
            )

            c1, c2 = st.columns(2)
            with c1:
                if st.button("看答案"):
                    st.session_state.single_q_revealed = True

            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get("correct_answer", ""))
                if user_ans == ans:
                    st.success(MSG_CORRECT)
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
                    st.error(f"{MSG_WRONG} 正確是：{txt}")

                st.info(f"解析：{q.get('explanation','')}")


# =========================================================
# 功能 3: 單題即時練習
# =========================================================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 雲端單題刷")
    df = load_data("Questions")
    if df.empty:
        st.warning("無題目")
    else:
        df["type"] = df["type"].astype(str).replace({"": "choice"})
        choice_df = df[df["type"].astype(str).str.lower().eq("choice")].copy()
        choice_df = choice_df[choice_df["option_A"].notna() & (choice_df["option_A"].astype(str).str.strip() != "")]

        if choice_df.empty:
            st.warning("無可用選擇題（可能解析後都是 essay 題型）")
        else:
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
                clean_labels = [l.replace("nan", "").strip() for l in opt_labels]

                user_ans = st.radio(
                    "選",
                    opts,
                    label_visibility="collapsed",
                    format_func=lambda x: clean_labels[opts.index(x)] if clean_labels[opts.index(x)] else f"{x}（空）",
                )

                if st.button("看答案"):
                    st.session_state.single_q_revealed = True

                if st.session_state.single_q_revealed:
                    ans = extract_answer_key(q.get("correct_answer", ""))
                    if user_ans == ans:
                        st.success(MSG_CORRECT)
                    else:
                        try:
                            txt = clean_labels[["A", "B", "C", "D"].index(ans)]
                        except Exception:
                            txt = ans
                        st.error(f"{MSG_WRONG} 正確是：{txt}")

                        old_mistakes = load_data("Mistakes")
                        new_mistakes = pd.concat([old_mistakes, pd.DataFrame([q])], ignore_index=True)
                        new_mistakes.drop_duplicates(subset=["question"], keep="last", inplace=True)
                        save_to_google("Mistakes", new_mistakes)
                        st.caption("已同步到雲端錯題本")

                    st.info(f"解析：{q.get('explanation','')}")


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

            for c in EXPECTED_Q_COLS:
                if c not in new_df.columns:
                    new_df[c] = ""
            new_df = new_df[EXPECTED_Q_COLS]

            st.success(f"解析成功 {len(new_df)} 題（含 choice/essay 混合）")

            old_df = load_data("Questions")
            final_df = pd.concat([old_df, new_df], ignore_index=True)
            final_df.drop_duplicates(subset=["question"], keep="last", inplace=True)

            save_to_google("Questions", final_df)
            st.success("✅ 已成功寫入 Google Sheet！")
        else:
            st.error("❌ 解析不到題目，請確認 PDF 是否可被擷取文字（不是掃描圖）。")


# =========================================================
# Debug
# =========================================================
elif mode == "debug 雲端資料檢查":
    st.subheader("Questions 表")
    st.dataframe(load_data("Questions"), use_container_width=True)

    st.subheader("Mistakes 表")
    st.dataframe(load_data("Mistakes"), use_container_width=True)

    st.subheader("Users 表")
    st.dataframe(load_users(), use_container_width=True)

    st.subheader("Results 表")
    st.dataframe(load_results(), use_container_width=True)

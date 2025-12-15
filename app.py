import streamlit as st
import pandas as pd
import pdfplumber
import re
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- 設定頁面資訊 ---
st.set_page_config(page_title="質子中心-輻防師特訓平台 (雲端版)", layout="wide", page_icon="☢️")

# --- Google Sheets 設定 ---
SHEET_NAME = "radiation_exam_db"  # 請確認您的 Google Sheet 檔名

# --- 連線函式 (修正版：改讀 Secrets) ---
@st.cache_resource
def init_connection():
    """建立 Google Sheets 連線，改從 Streamlit Secrets 讀取金鑰"""
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    # 檢查 Secrets 是否設定正確
    if "gcp_service_account" not in st.secrets:
        st.error("⚠️ 未偵測到 Secrets 設定！請在 Streamlit Cloud 後台設定 [gcp_service_account]。")
        return None

    # 從 Secrets 讀取字典資料
    creds_dict = st.secrets["gcp_service_account"]
    
    # 建立憑證
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client

# --- 資料讀寫函式 ---
def load_data(worksheet_name):
    """從 Google Sheet 讀取資料轉為 DataFrame"""
    try:
        client = init_connection()
        if not client: return pd.DataFrame() # 連線失敗回傳空表

        sh = client.open(SHEET_NAME)
        # 檢查工作表是否存在，不存在則建立
        try:
            ws = sh.worksheet(worksheet_name)
        except:
            ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
            # 初始化標題
            headers = ["question", "option_A", "option_B", "option_C", "option_D", "correct_answer", "explanation", "topic", "type"]
            ws.append_row(headers)
            return pd.DataFrame(columns=headers)

        data = ws.get_all_records()
        df = pd.DataFrame(data)
        # 確保欄位存在 (防止空表報錯)
        if df.empty:
            return pd.DataFrame(columns=["question", "option_A", "option_B", "option_C", "option_D", "correct_answer", "explanation", "topic", "type"])
        return df
    except Exception as e:
        st.error(f"連線錯誤：找不到試算表 '{SHEET_NAME}' 或 Secrets 設定有誤。\n詳細訊息: {e}")
        return pd.DataFrame()

def save_to_google(worksheet_name, new_df):
    """將 DataFrame 覆蓋寫入 Google Sheet"""
    try:
        client = init_connection()
        sh = client.open(SHEET_NAME)
        ws = sh.worksheet(worksheet_name)
        ws.clear() # 清空舊資料
        # 寫入標題與內容
        ws.update([new_df.columns.values.tolist()] + new_df.values.tolist())
    except Exception as e:
        st.error(f"寫入失敗: {e}")

# --- Session State 初始化 ---
if 'quiz_data' not in st.session_state: st.session_state.quiz_data = None  
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'current_single_q' not in st.session_state: st.session_state.current_single_q = None
if 'single_q_revealed' not in st.session_state: st.session_state.single_q_revealed = False

# --- 工具函式 ---
def normalize_answer(ans):
    if pd.isna(ans): return ""
    ans = str(ans).strip().upper()
    ans = ans.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    mapping = {'1': 'A', '2': 'B', '3': 'C', '4': 'D', 'A': 'A', 'B': 'B', 'C': 'C', 'D': 'D'}
    return mapping.get(ans, ans)

def extract_answer_key(text):
    if pd.isna(text): return ""
    text = str(text).strip()
    match = re.match(r'^[\(（]?([1-4A-Da-d])[\)）\.]?', text)
    if match:
        val = match.group(1).upper()
        mapping = {'1': 'A', '2': 'B', '3': 'C', '4': 'D'}
        return mapping.get(val, val)
    return ""

def parse_exam_pdf(text):
    """v7.0 解析邏輯 (穩定版)"""
    questions = []
    lines = text.split('\n')
    current_q = {}
    state = "SEARCH_Q" 
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        if re.match(r'^\d+[\.\s]', line):
            if current_q and 'question' in current_q:
                if 'correct_answer' not in current_q: current_q['correct_answer'] = ""
                questions.append(current_q)
            current_q = {
                "question": line, "option_A": "", "option_B": "", "option_C": "", "option_D": "", 
                "correct_answer": "", "explanation": "", "type": "choice"
            }
            state = "READING_Q"
            continue

        if "[解:]" in line or "[解]" in line:
            clean_line = line.replace("[解:]", "").replace("[解]", "").strip()
            if clean_line:
                ans = extract_answer_key(clean_line)
                if ans and current_q:
                    current_q['correct_answer'] = ans
                    current_q['explanation'] = clean_line
                state = "READING_EXPL"
            else:
                state = "WAITING_FOR_ANS" 
            continue
            
        if state == "READING_Q":
            if re.match(r'^\(1\)|^\(A\)|^A\.|^1\.', line) or ("(1)" in line and "(2)" in line):
                state = "READING_OPT"
            else:
                current_q['question'] += " " + line
                continue

        if state == "WAITING_FOR_ANS":
            if current_q:
                ans = extract_answer_key(line)
                if ans:
                    current_q['correct_answer'] = ans
                    current_q['explanation'] += line
                else:
                    current_q['explanation'] += line
            state = "READING_EXPL"
            continue

        if state == "READING_OPT":
            if "(1)" in line and "(2)" in line:
                parts = re.split(r'(?=\(\d\))', line)
                for part in parts:
                    part = part.strip()
                    if part.startswith("(1)"): current_q['option_A'] = part
                    elif part.startswith("(2)"): current_q['option_B'] = part
                    elif part.startswith("(3)"): current_q['option_C'] = part
                    elif part.startswith("(4)"): current_q['option_D'] = part
            elif line.startswith("(1)"): current_q['option_A'] = line
            elif line.startswith("(2)"): current_q['option_B'] = line
            elif line.startswith("(3)"): current_q['option_C'] = line
            elif line.startswith("(4)"): current_q['option_D'] = line
            else: pass

        if state == "READING_EXPL":
            if not current_q['correct_answer']:
                ans = extract_answer_key(line)
                if ans: current_q['correct_answer'] = ans
            current_q['explanation'] += line + "\n"

    if current_q and 'question' in current_q:
        questions.append(current_q)
    return questions

# --- 主畫面 ---
with st.sidebar:
    st.title("☁️ 雲端功能選單")
    mode = st.radio("模式", [
        "📝 模擬考模式", 
        "📕 錯題本 (雲端同步)",
        "⚡ 單題即時練習", 
        "📂 匯入 PDF (上傳雲端)", 
        "debug 雲端資料檢查"
    ])
    st.markdown("---")
    
    # 狀態檢查
    if "gcp_service_account" in st.secrets:
        st.success("✅ Secrets 金鑰已偵測")
    else:
        st.error("⚠️ 未偵測到 Secrets！請至後台設定。")

# ==========================================
# 功能 1: 模擬考
# ==========================================
if mode == "📝 模擬考模式":
    st.title("📝 雲端題庫模擬考")
    df = load_data("Questions") # 讀取 "Questions" 工作表
    
    if not df.empty:
        valid_df = df[ df['question'].notna() & df['correct_answer'].notna() ]
        choice_df = valid_df[ valid_df['option_A'].notna() & (valid_df['option_A'] != "") ]
        
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
                        opt_labels = [str(row.get('option_A','')), str(row.get('option_B','')), str(row.get('option_C','')), str(row.get('option_D',''))]
                        clean_labels = [l.replace("nan", "") for l in opt_labels]
                        user_answers[index] = st.radio(f"A{index}", opts, key=f"q_{index}", label_visibility="collapsed", format_func=lambda x: clean_labels[opts.index(x)])
                        st.markdown("---")
                    
                    if st.form_submit_button("📝 交卷"):
                        st.session_state.quiz_submitted = True
                
                if st.session_state.quiz_submitted:
                    score = 0
                    wrong_entries = []
                    for index, row in st.session_state.quiz_data.iterrows():
                        user = user_answers.get(index)
                        ans = extract_answer_key(row.get('correct_answer', ''))
                        if user == ans:
                            score += 1
                        else:
                            wrong_entries.append(row)
                        
                        with st.expander(f"第 {index+1} 題檢討", expanded=(user!=ans)):
                            opt_texts = [str(row.get('option_A')), str(row.get('option_B')), str(row.get('option_C')), str(row.get('option_D'))]
                            try: correct_text = opt_texts[["A","B","C","D"].index(ans)]
                            except: correct_text = ans
                            if user == ans: st.success(f"答對！{correct_text}")
                            else: st.error(f"答錯！正確：{correct_text}")
                            st.write(f"解析：{row.get('explanation', '')}")

                    if wrong_entries:
                        # 儲存到雲端 Mistakes 工作表
                        wrong_df = pd.DataFrame(wrong_entries)
                        old_mistakes = load_data("Mistakes")
                        final_mistakes = pd.concat([old_mistakes, wrong_df], ignore_index=True)
                        final_mistakes.drop_duplicates(subset=['question'], keep='last', inplace=True)
                        save_to_google("Mistakes", final_mistakes)
                        st.toast(f"已同步 {len(wrong_entries)} 題到雲端錯題本！", icon="☁️")

                    st.metric("成績", f"{int(score/len(st.session_state.quiz_data)*100)} 分")
                    if st.button("🔄 重測"):
                        st.session_state.quiz_data = None
                        st.session_state.quiz_submitted = False
                        st.rerun()

# ==========================================
# 功能 2: 錯題本
# ==========================================
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
            opt_labels = [str(q.get('option_A','')), str(q.get('option_B','')), str(q.get('option_C','')), str(q.get('option_D',''))]
            clean_labels = [l.replace("nan", "") for l in opt_labels]
            user_ans = st.radio("選", opts, label_visibility="collapsed", format_func=lambda x: clean_labels[opts.index(x)])
            
            c1, c2 = st.columns(2)
            with c1:
                if st.button("看答案"): st.session_state.single_q_revealed = True
            
            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get('correct_answer', ''))
                if user_ans == ans:
                    st.success("答對！")
                    with c2:
                        if st.button("🗑️ 從雲端移除"):
                            new_mistakes = mistake_df[mistake_df['question'] != q['question']]
                            save_to_google("Mistakes", new_mistakes)
                            st.success("已移除")
                            st.session_state.current_single_q = None
                            st.rerun()
                else:
                    try: txt = clean_labels[["A","B","C","D"].index(ans)]
                    except: txt = ans
                    st.error(f"答錯，正確是：{txt}")
                st.info(f"解析：{q.get('explanation','')}")

# ==========================================
# 功能 3: 單題練習
# ==========================================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 雲端單題刷")
    df = load_data("Questions")
    choice_df = df[ df['option_A'].notna() & (df['option_A'] != "") ]
    
    if not choice_df.empty:
        if st.button("🎲 抽題"):
            st.session_state.current_single_q = choice_df.sample(1).iloc[0]
            st.session_state.single_q_revealed = False
        
        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")
            opts = ["A", "B", "C", "D"]
            opt_labels = [str(q.get('option_A','')), str(q.get('option_B','')), str(q.get('option_C','')), str(q.get('option_D',''))]
            clean_labels = [l.replace("nan", "") for l in opt_labels]
            user_ans = st.radio("選", opts, label_visibility="collapsed", format_func=lambda x: clean_labels[opts.index(x)])
            
            if st.button("看答案"): st.session_state.single_q_revealed = True
            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get('correct_answer', ''))
                if user_ans == ans: st.success("Correct!")
                else:
                    try: txt = clean_labels[["A","B","C","D"].index(ans)]
                    except: txt = ans
                    st.error(f"Answer: {txt}")
                    # 存錯題
                    old_mistakes = load_data("Mistakes")
                    new_mistakes = pd.concat([old_mistakes, pd.DataFrame([q])], ignore_index=True)
                    new_mistakes.drop_duplicates(subset=['question'], keep='last', inplace=True)
                    save_to_google("Mistakes", new_mistakes)
                    st.caption("已同步到雲端錯題本")
                st.info(f"解析：{q.get('explanation','')}")
    else: st.warning("無題目")

# ==========================================
# 功能 4: PDF 匯入
# ==========================================
elif mode == "📂 匯入 PDF (上傳雲端)":
    st.title("📂 匯入並上傳 Google Sheet")
    uploaded_file = st.file_uploader("PDF", type=["pdf"])
    if uploaded_file and st.button("解析並上傳"):
        with pdfplumber.open(uploaded_file) as pdf:
            text = "".join([page.extract_text() + "\n" for page in pdf.pages])
        
        data = parse_exam_pdf(text)
        if data:
            new_df = pd.DataFrame(data)
            st.success(f"解析成功 {len(new_df)} 題")
            
            # 讀取雲端舊資料並合併
            old_df = load_data("Questions")
            final_df = pd.concat([old_df, new_df], ignore_index=True)
            final_df.drop_duplicates(subset=['question'], keep='last', inplace=True)
            
            # 寫回雲端
            save_to_google("Questions", final_df)
            st.success("✅ 已成功寫入 Google Sheet！所有組員現在都能看到了。")

elif mode == "debug 雲端資料檢查":
    st.write("Questions 表：")
    st.dataframe(load_data("Questions"))
    st.write("Mistakes 表：")
    st.dataframe(load_data("Mistakes"))t import ServiceAccountCredentials

# --- 設定頁面資訊 ---
st.set_page_config(page_title="質子中心-輻防師特訓平台 (雲端版)", layout="wide", page_icon="☢️")

# --- Google Sheets 設定 ---
SHEET_NAME = "radiation_exam_db"  # 請確認您的 Google Sheet 檔名完全一致
CREDENTIALS_FILE = "credentials.json" # 請確認金鑰檔案在同目錄下

# --- 連線函式 (含快取以加速) ---
@st.cache_resource
def init_connection():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client

# --- 資料讀寫函式 ---
def load_data(worksheet_name):
    """從 Google Sheet 讀取資料轉為 DataFrame"""
    try:
        client = init_connection()
        sh = client.open(SHEET_NAME)
        # 檢查工作表是否存在，不存在則建立
        try:
            ws = sh.worksheet(worksheet_name)
        except:
            ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
            # 初始化標題
            headers = ["question", "option_A", "option_B", "option_C", "option_D", "correct_answer", "explanation", "topic", "type"]
            ws.append_row(headers)
            return pd.DataFrame(columns=headers)

        data = ws.get_all_records()
        df = pd.DataFrame(data)
        # 確保欄位存在 (防止空表報錯)
        if df.empty:
            return pd.DataFrame(columns=["question", "option_A", "option_B", "option_C", "option_D", "correct_answer", "explanation", "topic", "type"])
        return df
    except Exception as e:
        st.error(f"連線錯誤：找不到試算表 '{SHEET_NAME}' 或 憑證錯誤。\n詳細訊息: {e}")
        return pd.DataFrame()

def save_to_google(worksheet_name, new_df):
    """將 DataFrame 覆蓋寫入 Google Sheet (適合整理後的存檔)"""
    client = init_connection()
    sh = client.open(SHEET_NAME)
    ws = sh.worksheet(worksheet_name)
    ws.clear() # 清空舊資料
    # 寫入標題與內容
    ws.update([new_df.columns.values.tolist()] + new_df.values.tolist())

def append_to_google(worksheet_name, row_data_list):
    """將單筆或多筆資料附加到最後一行 (適合錯題本)"""
    client = init_connection()
    sh = client.open(SHEET_NAME)
    ws = sh.worksheet(worksheet_name)
    # 轉換 DataFrame 為 list of lists
    if isinstance(row_data_list, pd.DataFrame):
        ws.append_rows(row_data_list.values.tolist())
    else:
        ws.append_rows(row_data_list)

# --- Session State 初始化 ---
if 'quiz_data' not in st.session_state: st.session_state.quiz_data = None  
if 'quiz_submitted' not in st.session_state: st.session_state.quiz_submitted = False
if 'current_single_q' not in st.session_state: st.session_state.current_single_q = None
if 'single_q_revealed' not in st.session_state: st.session_state.single_q_revealed = False

# --- 工具函式 ---
def normalize_answer(ans):
    if pd.isna(ans): return ""
    ans = str(ans).strip().upper()
    ans = ans.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    mapping = {'1': 'A', '2': 'B', '3': 'C', '4': 'D', 'A': 'A', 'B': 'B', 'C': 'C', 'D': 'D'}
    return mapping.get(ans, ans)

def extract_answer_key(text):
    if pd.isna(text): return ""
    text = str(text).strip()
    match = re.match(r'^[\(（]?([1-4A-Da-d])[\)）\.]?', text)
    if match:
        val = match.group(1).upper()
        mapping = {'1': 'A', '2': 'B', '3': 'C', '4': 'D'}
        return mapping.get(val, val)
    return ""

def parse_exam_pdf(text):
    """v7.0 解析邏輯"""
    questions = []
    lines = text.split('\n')
    current_q = {}
    state = "SEARCH_Q" 
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        if re.match(r'^\d+[\.\s]', line):
            if current_q and 'question' in current_q:
                if 'correct_answer' not in current_q: current_q['correct_answer'] = ""
                questions.append(current_q)
            current_q = {
                "question": line, "option_A": "", "option_B": "", "option_C": "", "option_D": "", 
                "correct_answer": "", "explanation": "", "type": "choice"
            }
            state = "READING_Q"
            continue

        if "[解:]" in line or "[解]" in line:
            clean_line = line.replace("[解:]", "").replace("[解]", "").strip()
            if clean_line:
                ans = extract_answer_key(clean_line)
                if ans and current_q:
                    current_q['correct_answer'] = ans
                    current_q['explanation'] = clean_line
                state = "READING_EXPL"
            else:
                state = "WAITING_FOR_ANS" 
            continue
            
        if state == "READING_Q":
            if re.match(r'^\(1\)|^\(A\)|^A\.|^1\.', line) or ("(1)" in line and "(2)" in line):
                state = "READING_OPT"
            else:
                current_q['question'] += " " + line
                continue

        if state == "WAITING_FOR_ANS":
            if current_q:
                ans = extract_answer_key(line)
                if ans:
                    current_q['correct_answer'] = ans
                    current_q['explanation'] += line
                else:
                    current_q['explanation'] += line
            state = "READING_EXPL"
            continue

        if state == "READING_OPT":
            if "(1)" in line and "(2)" in line:
                parts = re.split(r'(?=\(\d\))', line)
                for part in parts:
                    part = part.strip()
                    if part.startswith("(1)"): current_q['option_A'] = part
                    elif part.startswith("(2)"): current_q['option_B'] = part
                    elif part.startswith("(3)"): current_q['option_C'] = part
                    elif part.startswith("(4)"): current_q['option_D'] = part
            elif line.startswith("(1)"): current_q['option_A'] = line
            elif line.startswith("(2)"): current_q['option_B'] = line
            elif line.startswith("(3)"): current_q['option_C'] = line
            elif line.startswith("(4)"): current_q['option_D'] = line
            else: pass

        if state == "READING_EXPL":
            if not current_q['correct_answer']:
                ans = extract_answer_key(line)
                if ans: current_q['correct_answer'] = ans
            current_q['explanation'] += line + "\n"

    if current_q and 'question' in current_q:
        questions.append(current_q)
    return questions

# --- 主畫面 ---
with st.sidebar:
    st.title("☁️ 雲端功能選單")
    mode = st.radio("模式", [
        "📝 模擬考模式", 
        "📕 錯題本 (雲端同步)",
        "⚡ 單題即時練習", 
        "📂 匯入 PDF (上傳雲端)", 
        "debug 雲端資料檢查"
    ])
    st.markdown("---")
    # 檢查 credentials 是否存在
    if not os.path.exists(CREDENTIALS_FILE):
        st.error("⚠️ 未偵測到 credentials.json！無法連線 Google Sheet。")
    else:
        st.success("✅ Google 連線模組已就緒")

# ==========================================
# 功能 1: 模擬考
# ==========================================
if mode == "📝 模擬考模式":
    st.title("📝 雲端題庫模擬考")
    df = load_data("Questions") # 讀取 "Questions" 工作表
    
    if not df.empty:
        valid_df = df[ df['question'].notna() & df['correct_answer'].notna() ]
        choice_df = valid_df[ valid_df['option_A'].notna() & (valid_df['option_A'] != "") ]
        
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
                        opt_labels = [str(row.get('option_A','')), str(row.get('option_B','')), str(row.get('option_C','')), str(row.get('option_D',''))]
                        clean_labels = [l.replace("nan", "") for l in opt_labels]
                        user_answers[index] = st.radio(f"A{index}", opts, key=f"q_{index}", label_visibility="collapsed", format_func=lambda x: clean_labels[opts.index(x)])
                        st.markdown("---")
                    
                    if st.form_submit_button("📝 交卷"):
                        st.session_state.quiz_submitted = True
                
                if st.session_state.quiz_submitted:
                    score = 0
                    wrong_entries = []
                    for index, row in st.session_state.quiz_data.iterrows():
                        user = user_answers.get(index)
                        ans = extract_answer_key(row.get('correct_answer', ''))
                        if user == ans:
                            score += 1
                        else:
                            wrong_entries.append(row)
                        
                        with st.expander(f"第 {index+1} 題檢討", expanded=(user!=ans)):
                            opt_texts = [str(row.get('option_A')), str(row.get('option_B')), str(row.get('option_C')), str(row.get('option_D'))]
                            try: correct_text = opt_texts[["A","B","C","D"].index(ans)]
                            except: correct_text = ans
                            if user == ans: st.success(f"答對！{correct_text}")
                            else: st.error(f"答錯！正確：{correct_text}")
                            st.write(f"解析：{row.get('explanation', '')}")

                    if wrong_entries:
                        # 儲存到雲端 Mistakes 工作表
                        wrong_df = pd.DataFrame(wrong_entries)
                        # 先讀舊的
                        old_mistakes = load_data("Mistakes")
                        final_mistakes = pd.concat([old_mistakes, wrong_df], ignore_index=True)
                        final_mistakes.drop_duplicates(subset=['question'], keep='last', inplace=True)
                        save_to_google("Mistakes", final_mistakes)
                        st.toast(f"已同步 {len(wrong_entries)} 題到雲端錯題本！", icon="☁️")

                    st.metric("成績", f"{int(score/len(st.session_state.quiz_data)*100)} 分")
                    if st.button("🔄 重測"):
                        st.session_state.quiz_data = None
                        st.session_state.quiz_submitted = False
                        st.rerun()

# ==========================================
# 功能 2: 錯題本
# ==========================================
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
            opt_labels = [str(q.get('option_A','')), str(q.get('option_B','')), str(q.get('option_C','')), str(q.get('option_D',''))]
            clean_labels = [l.replace("nan", "") for l in opt_labels]
            user_ans = st.radio("選", opts, label_visibility="collapsed", format_func=lambda x: clean_labels[opts.index(x)])
            
            c1, c2 = st.columns(2)
            with c1:
                if st.button("看答案"): st.session_state.single_q_revealed = True
            
            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get('correct_answer', ''))
                if user_ans == ans:
                    st.success("答對！")
                    with c2:
                        if st.button("🗑️ 從雲端移除"):
                            new_mistakes = mistake_df[mistake_df['question'] != q['question']]
                            save_to_google("Mistakes", new_mistakes)
                            st.success("已移除")
                            st.session_state.current_single_q = None
                            st.rerun()
                else:
                    try: txt = clean_labels[["A","B","C","D"].index(ans)]
                    except: txt = ans
                    st.error(f"答錯，正確是：{txt}")
                st.info(f"解析：{q.get('explanation','')}")

# ==========================================
# 功能 3: 單題練習
# ==========================================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 雲端單題刷")
    df = load_data("Questions")
    choice_df = df[ df['option_A'].notna() & (df['option_A'] != "") ]
    
    if not choice_df.empty:
        if st.button("🎲 抽題"):
            st.session_state.current_single_q = choice_df.sample(1).iloc[0]
            st.session_state.single_q_revealed = False
        
        q = st.session_state.current_single_q
        if q is not None:
            st.markdown(f"### {q['question']}")
            opts = ["A", "B", "C", "D"]
            opt_labels = [str(q.get('option_A','')), str(q.get('option_B','')), str(q.get('option_C','')), str(q.get('option_D',''))]
            clean_labels = [l.replace("nan", "") for l in opt_labels]
            user_ans = st.radio("選", opts, label_visibility="collapsed", format_func=lambda x: clean_labels[opts.index(x)])
            
            if st.button("看答案"): st.session_state.single_q_revealed = True
            if st.session_state.single_q_revealed:
                ans = extract_answer_key(q.get('correct_answer', ''))
                if user_ans == ans: st.success("Correct!")
                else:
                    try: txt = clean_labels[["A","B","C","D"].index(ans)]
                    except: txt = ans
                    st.error(f"Answer: {txt}")
                    # 存錯題
                    old_mistakes = load_data("Mistakes")
                    new_mistakes = pd.concat([old_mistakes, pd.DataFrame([q])], ignore_index=True)
                    new_mistakes.drop_duplicates(subset=['question'], keep='last', inplace=True)
                    save_to_google("Mistakes", new_mistakes)
                    st.caption("已同步到雲端錯題本")
                st.info(f"解析：{q.get('explanation','')}")
    else: st.warning("無題目")

# ==========================================
# 功能 4: PDF 匯入
# ==========================================
elif mode == "📂 匯入 PDF (上傳雲端)":
    st.title("📂 匯入並上傳 Google Sheet")
    uploaded_file = st.file_uploader("PDF", type=["pdf"])
    if uploaded_file and st.button("解析並上傳"):
        with pdfplumber.open(uploaded_file) as pdf:
            text = "".join([page.extract_text() + "\n" for page in pdf.pages])
        
        data = parse_exam_pdf(text)
        if data:
            new_df = pd.DataFrame(data)
            st.success(f"解析成功 {len(new_df)} 題")
            
            # 讀取雲端舊資料並合併
            old_df = load_data("Questions")
            final_df = pd.concat([old_df, new_df], ignore_index=True)
            final_df.drop_duplicates(subset=['question'], keep='last', inplace=True)
            
            # 寫回雲端
            save_to_google("Questions", final_df)
            st.success("✅ 已成功寫入 Google Sheet！所有組員現在都能看到了。")

elif mode == "debug 雲端資料檢查":
    st.write("Questions 表：")
    st.dataframe(load_data("Questions"))
    st.write("Mistakes 表：")
    st.dataframe(load_data("Mistakes"))

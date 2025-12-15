import streamlit as st
import pandas as pd
import pdfplumber
import re
import os

# --- 設定頁面資訊 ---
st.set_page_config(page_title="質子中心-輻防師特訓平台 (v5.0)", layout="wide", page_icon="☢️")

# --- 檔案路徑 ---
csv_file = "data.csv"
mistakes_file = "mistakes.csv"

# --- 自動初始化資料庫 ---
def init_db():
    if not os.path.exists(csv_file):
        init_df = pd.DataFrame(columns=["question", "option_A", "option_B", "option_C", "option_D", "correct_answer", "explanation", "topic", "type"])
        init_df.to_csv(csv_file, index=False, encoding="utf-8-sig")
    
    if not os.path.exists(mistakes_file):
        init_mistake = pd.DataFrame(columns=["question", "option_A", "option_B", "option_C", "option_D", "correct_answer", "explanation", "topic", "type"])
        init_mistake.to_csv(mistakes_file, index=False, encoding="utf-8-sig")

init_db()

# --- Session State 初始化 ---
if 'quiz_data' not in st.session_state:
    st.session_state.quiz_data = None  
if 'quiz_submitted' not in st.session_state:
    st.session_state.quiz_submitted = False
if 'current_single_q' not in st.session_state:
    st.session_state.current_single_q = None
if 'single_q_revealed' not in st.session_state:
    st.session_state.single_q_revealed = False

# --- 工具函式 ---
def normalize_answer(ans):
    """將 (2), 2, (B) 等格式轉為標準 B"""
    if pd.isna(ans): return ""
    ans = str(ans).strip().upper()
    ans = ans.replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    mapping = {'1': 'A', '2': 'B', '3': 'C', '4': 'D', 'A': 'A', 'B': 'B', 'C': 'C', 'D': 'D'}
    return mapping.get(ans, ans)

def save_mistakes(wrong_rows):
    """將答錯的題目存入錯題本"""
    if not wrong_rows: return
    new_mistakes = pd.DataFrame(wrong_rows)
    try:
        if os.path.exists(mistakes_file):
            old_mistakes = pd.read_csv(mistakes_file)
            final_mistakes = pd.concat([old_mistakes, new_mistakes], ignore_index=True)
        else:
            final_mistakes = new_mistakes
        final_mistakes.drop_duplicates(subset=['question'], keep='last', inplace=True)
        final_mistakes.to_csv(mistakes_file, index=False, encoding="utf-8-sig")
    except Exception as e:
        st.error(f"儲存錯題失敗: {e}")

# ==========================================
# 核心解析邏輯 (v5.0 強力修復版)
# ==========================================
def parse_exam_pdf(text):
    questions = []
    lines = text.split('\n')
    current_q = {}
    
    # 定義狀態：SEARCH_Q (找題目開頭), READING_Q (讀題目中), READING_OPT (讀選項), READING_EXPL (讀解析)
    state = "SEARCH_Q" 
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        # 1. 偵測新題目 (最高優先級：數字+點或空格，如 "3. " 或 "45.")
        if re.match(r'^\d+[\.\s]', line):
            # 存上一題
            if current_q:
                if 'correct_answer' not in current_q: current_q['correct_answer'] = ""
                questions.append(current_q)
            
            # 初始化新題目
            current_q = {
                "question": line, 
                "option_A": "", "option_B": "", "option_C": "", "option_D": "", 
                "correct_answer": "", "explanation": "", "type": "choice"
            }
            state = "READING_Q" # 進入「讀題模式」
            continue

        # 2. 偵測解答標記 [解:]
        if "[解:]" in line or "[解]" in line:
            clean_line = line.replace("[解:]", "").replace("[解]", "").strip()
            if clean_line:
                # 答案在同一行: [解:] (1)
                if current_q: current_q['correct_answer'] = normalize_answer(clean_line)
            # 無論有無答案，接下來都是解析區
            state = "READING_EXPL" 
            continue
        
        # 3. 根據狀態處理文字
        if state == "READING_Q":
            # --- 關鍵修正：確保多行題目不會斷掉 ---
            # 只有遇到「明顯的選項開頭」才會切換狀態
            # 判斷：行首是 (1), (A), 1. 或是 同一行有 (1)和(2)
            if re.match(r'^\(1\)|^\(A\)|^A\.|^1\.', line) or ("(1)" in line and "(2)" in line):
                state = "READING_OPT"
                # 不 continue，讓下面的 READING_OPT 邏輯立刻處理這一行
            else:
                # 否則，這行絕對是題目的一部分！(例如：...表面X公分處...)
                current_q['question'] += " " + line
                continue

        if state == "READING_OPT":
            # 處理選項
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
            else:
                # 如果在讀選項時遇到不認識的行，通常是上一個選項的換行 (例如選項很長)
                # 這裡簡單處理：如果是 (1)... 後面接文字，通常歸給最後一個選項，或忽略
                pass

        if state == "READING_EXPL":
            # 讀取解析/答案
            # 如果還沒抓到答案，且這行長得像 (1) 或 A，就當作答案
            if not current_q['correct_answer'] and re.match(r'^\(?[\d\w]\)?$', line):
                current_q['correct_answer'] = normalize_answer(line)
            else:
                current_q['explanation'] += line + "\n"

    # 迴圈結束，存最後一題
    if current_q and 'question' in current_q:
        questions.append(current_q)
        
    return questions

# --- 主畫面側邊欄 ---
with st.sidebar:
    st.title("⚙️ 功能選單")
    mode = st.radio("請選擇模式", [
        "📝 模擬考模式 (自由題數)", 
        "📕 錯題本 (弱點加強)",
        "⚡ 單題即時練習", 
        "📂 匯入 PDF 題庫", 
        "debug 資料庫檢查"
    ])
    st.markdown("---")
    
    if os.path.exists(csv_file):
        df_count = len(pd.read_csv(csv_file))
        st.caption(f"📚 總題庫：{df_count} 題")
    if os.path.exists(mistakes_file):
        mis_count = len(pd.read_csv(mistakes_file))
        st.caption(f"📕 錯題數：{mis_count} 題")

# ==========================================
# 功能 1: 模擬考模式
# ==========================================
if mode == "📝 模擬考模式 (自由題數)":
    st.title("📝 輻防師模擬測驗")
    
    if os.path.exists(csv_file):
        df = pd.read_csv(csv_file)
        valid_df = df[ df['question'].notna() ]
        choice_df = valid_df[ valid_df['option_A'].notna() & (valid_df['option_A'] != "") ]
        
        if len(choice_df) == 0:
            st.warning("題庫中沒有選擇題，請先匯入 PDF。")
        else:
            if st.session_state.quiz_data is None:
                st.info(f"目前題庫共有 {len(choice_df)} 題選擇題。")
                
                col1, col2 = st.columns([1, 2])
                with col1:
                    num = st.number_input("請輸入要測驗的題數", min_value=1, max_value=len(choice_df), value=min(20, len(choice_df)))
                with col2:
                    st.write("")
                    st.write("")
                    if st.button("🚀 開始測驗", type="primary"):
                        st.session_state.quiz_data = choice_df.sample(n=num).reset_index(drop=True)
                        st.session_state.quiz_submitted = False
                        st.rerun()
            else:
                st.subheader("答題區")
                with st.form("quiz_form"):
                    user_answers = {}
                    for index, row in st.session_state.quiz_data.iterrows():
                        st.markdown(f"**第 {index+1} 題：** {row['question']}")
                        opts = ["A", "B", "C", "D"]
                        opt_texts = [str(row.get('option_A','')), str(row.get('option_B','')), str(row.get('option_C','')), str(row.get('option_D',''))]
                        clean_opts = [o.replace("nan", "") for o in opt_texts]

                        user_answers[index] = st.radio(
                            f"Q{index+1} 答案", opts, key=f"q_{index}", horizontal=True,
                            format_func=lambda x: f"{x}. {clean_opts[opts.index(x)]}"
                        )
                        st.markdown("---")
                    
                    if st.form_submit_button("📝 交卷"):
                        st.session_state.quiz_submitted = True
                
                if st.session_state.quiz_submitted:
                    score = 0
                    total = len(st.session_state.quiz_data)
                    wrong_entries = []

                    for index, row in st.session_state.quiz_data.iterrows():
                        user = user_answers.get(index)
                        ans = normalize_answer(row.get('correct_answer', ''))
                        
                        if user == ans:
                            score += 1
                        else:
                            wrong_entries.append(row)

                        with st.expander(f"第 {index+1} 題檢討", expanded=(user!=ans)):
                            if user == ans:
                                st.success(f"答對！答案是 {ans}")
                            else:
                                st.error(f"答錯，您的答案 {user}，正確答案是 {ans}")
                                st.caption("❌ 此題已自動加入「錯題本」")
                            st.write(f"解析：{row.get('explanation', '')}")

                    if wrong_entries:
                        save_mistakes(wrong_entries)
                        st.toast(f"已將 {len(wrong_entries)} 題錯題加入錯題本！", icon="📕")

                    st.metric("最終成績", f"{int(score/total*100)} 分", f"答對 {score}/{total} 題")
                    
                    if st.button("🔄 再測一次"):
                        st.session_state.quiz_data = None
                        st.session_state.quiz_submitted = False
                        st.rerun()

# ==========================================
# 功能 2: 錯題本
# ==========================================
elif mode == "📕 錯題本 (弱點加強)":
    st.title("📕 錯題本 - 弱點擊破")
    
    if os.path.exists(mistakes_file):
        mistake_df = pd.read_csv(mistakes_file)
        
        if len(mistake_df) == 0:
            st.success("🎉 太棒了！錯題本目前是空的。")
        else:
            st.write(f"目前累積錯誤題數：{len(mistake_df)} 題")
            
            if st.button("🎲 從錯題本隨機抽一題練習"):
                st.session_state.current_single_q = mistake_df.sample(1).iloc[0]
                st.session_state.single_q_revealed = False
            
            q = st.session_state.current_single_q
            if q is not None:
                st.markdown("---")
                st.markdown(f"### (錯題重練) {q['question']}")
                opts = ["A", "B", "C", "D"]
                opt_texts = [str(q.get('option_A','')), str(q.get('option_B','')), str(q.get('option_C','')), str(q.get('option_D',''))]
                clean_opts = [o.replace("nan", "") for o in opt_texts]
                
                user_ans = st.radio("選擇", opts, format_func=lambda x: f"{x}. {clean_opts[opts.index(x)]}")
                
                col1, col2 = st.columns([1, 1])
                with col1:
                    if st.button("查看答案"):
                        st.session_state.single_q_revealed = True
                
                if st.session_state.single_q_revealed:
                    ans = normalize_answer(q.get('correct_answer', ''))
                    if user_ans == ans:
                        st.success("🎉 恭喜答對！")
                        with col2:
                            if st.button("🗑️ 從錯題本移除此題"):
                                current_mistakes = pd.read_csv(mistakes_file)
                                new_mistakes = current_mistakes[current_mistakes['question'] != q['question']]
                                new_mistakes.to_csv(mistakes_file, index=False, encoding="utf-8-sig")
                                st.success("已移除！請重新抽題。")
                                st.session_state.current_single_q = None
                                st.rerun()
                    else:
                        st.error(f"還是答錯囉... 正確答案是 {ans}")
                        st.info("加油，多練幾次！")
                    
                    st.info(f"解析：{q.get('explanation','')}")
            
            st.markdown("---")
            with st.expander("查看所有錯題列表"):
                st.dataframe(mistake_df)
    else:
        st.error("錯題本檔案遺失。")

# ==========================================
# 功能 3: 單題練習
# ==========================================
elif mode == "⚡ 單題即時練習":
    st.title("⚡ 快速刷題")
    if os.path.exists(csv_file):
        df = pd.read_csv(csv_file)
        df = df[ df['option_A'].notna() & (df['option_A'] != "") ]
        
        if len(df) > 0:
            if st.button("🎲 抽題"):
                st.session_state.current_single_q = df.sample(1).iloc[0]
                st.session_state.single_q_revealed = False
            
            q = st.session_state.current_single_q
            if q is not None:
                st.markdown(f"### {q['question']}")
                opts = ["A", "B", "C", "D"]
                opt_texts = [str(q.get('option_A','')), str(q.get('option_B','')), str(q.get('option_C','')), str(q.get('option_D',''))]
                clean_opts = [o.replace("nan", "") for o in opt_texts]
                
                user_ans = st.radio("選擇", opts, format_func=lambda x: f"{x}. {clean_opts[opts.index(x)]}")
                
                if st.button("查看答案"):
                    st.session_state.single_q_revealed = True

                if st.session_state.single_q_revealed:
                    ans = normalize_answer(q.get('correct_answer', ''))
                    if user_ans == ans:
                        st.success("Correct!")
                    else:
                        st.error(f"Answer is {ans}")
                        save_mistakes([q])
                        st.caption("已加入錯題本")
                    st.info(f"解析：{q.get('explanation','')}")
        else:
            st.warning("無題目")

# ==========================================
# 功能 4: PDF 匯入 (v5.0)
# ==========================================
elif mode == "📂 匯入 PDF 題庫":
    st.title("📂 匯入 PDF")
    st.info("支援長題目、多行選項與換行答案解析。")
    uploaded_file = st.file_uploader("上傳", type=["pdf"])
    if uploaded_file and st.button("解析"):
        with pdfplumber.open(uploaded_file) as pdf:
            text = "".join([page.extract_text() + "\n" for page in pdf.pages])
        
        data = parse_exam_pdf(text)
        if data:
            new_df = pd.DataFrame(data)
            st.success(f"抓到 {len(new_df)} 題")
            st.dataframe(new_df.head())
            
            try:
                old = pd.read_csv(csv_file)
                final = pd.concat([old, new_df], ignore_index=True)
            except:
                final = new_df
            final.drop_duplicates(subset=['question'], keep='last', inplace=True)
            final.to_csv(csv_file, index=False, encoding="utf-8-sig")
            st.success("已儲存！")

elif mode == "debug 資料庫檢查":
    if os.path.exists(csv_file):
        st.write("主題庫：")
        st.dataframe(pd.read_csv(csv_file))
        if st.button("⚠️ 清空主題庫"):
            os.remove(csv_file)
            st.success("已清空")
            st.rerun()
            
    if os.path.exists(mistakes_file):
        st.write("錯題本：")
        st.dataframe(pd.read_csv(mistakes_file))
        if st.button("⚠️ 清空錯題本"):
            os.remove(mistakes_file)
            st.success("已清空")
            st.rerun()

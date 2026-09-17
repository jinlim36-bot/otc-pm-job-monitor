import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import urllib.parse
import re
import json
import os
import hashlib
from datetime import datetime, date
from playwright.sync_api import sync_playwright

# ----------------------------------------------------
# 1. 파일 기반 영속성 저장소 (이력 및 관심공고)
# ----------------------------------------------------
HISTORY_FILE = "job_history.json"
BOOKMARKS_FILE = "bookmarks.json"

def load_json(filepath: str) -> dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def load_json(filepath: str):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def load_history_safe(filepath: str) -> dict:
    """구버전(list)과 신버전(dict) 이력 파일을 모두 안전하게 불러오는 호환 함수"""
    data = load_json(filepath)
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 구버전 파일이 list 형태로 남아있는 경우 dict 형태로 자동 마이그레이션
    if isinstance(data, list):
        return {
            cid: {
                "first_seen": today_str,
                "last_seen": today_str,
                "title": "이전 이력",
                "corp": "이전 이력"
            }
            for cid in data if isinstance(cid, str)
        }
    elif isinstance(data, dict):
        return data
    return {}

def save_json(filepath: str, data: dict):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def stable_url_hash(url: str) -> str:
    """결정적 SHA-256 해시 생성 (프로세스 재시작 시에도 불변)"""
    clean_url = url.split("?")[0] if "?" in url else url
    return hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:12]

def extract_job_id(platform: str, url: str) -> str:
    """공고 고유 식별 번호 추출 (실패 시 SHA-256 대체)"""
    if platform == "사람인":
        match = re.search(r"rec_idx=(\d+)", url) or re.search(r"/jobs/relay/view\?rec_idx=(\d+)", url)
        if match:
            return f"saramin_{match.group(1)}"
    elif platform == "잡코리아":
        match = re.search(r"/GI_Read/(\d+)", url) or re.search(r"GI_No=(\d+)", url)
        if match:
            return f"jobkorea_{match.group(1)}"
    return f"{platform}_{stable_url_hash(url)}"


# ----------------------------------------------------
# 2. 동적 D-Day 및 메타데이터(경력/지역) 파서
# ----------------------------------------------------
def parse_dday(date_str: str) -> tuple[str, int]:
    """실행 시점의 시스템 날짜를 기준으로 동적 D-Day 산출"""
    if not date_str or any(k in date_str for k in ["상시", "채용시", "수시"]):
        return "상시채용", 999
    if "오늘" in date_str:
        return "오늘마감 (D-0)", 0
    if "내일" in date_str:
        return "D-1", 1

    today = datetime.now().date()

    # 1. YYYY-MM-DD 또는 YYYY.MM.DD
    match_full = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", date_str)
    if match_full:
        try:
            target = date(int(match_full.group(1)), int(match_full.group(2)), int(match_full.group(3)))
            diff = (target - today).days
            return (f"D-{diff}" if diff >= 0 else "마감종료"), diff
        except Exception:
            pass

    # 2. MM/DD 형태 (현재 연도 기준 동적 롤오버)
    match_short = re.search(r"(\d{1,2})/(\d{1,2})", date_str)
    if match_short:
        try:
            month = int(match_short.group(1))
            day = int(match_short.group(2))
            target = date(today.year, month, day)
            # 이미 지난 날짜면 다음 해로 보정 (예: 12월에 1월 공고 탐색)
            if target < today:
                target = date(today.year + 1, month, day)
            diff = (target - today).days
            return (f"D-{diff}" if diff >= 0 else "마감종료"), diff
        except Exception:
            pass

    return date_str, 500

def parse_career_and_region(text: str) -> tuple[str, str]:
    """지원조건 문자열에서 경력 요건 및 근무지역 추출"""
    career = "경력 미기재"
    region = "지역 미기재"

    # 경력 파싱
    career_match = re.search(r"(\d+~?\d*년(?:이상)?|신입·경력|경력무관|신입|경력)", text)
    if career_match:
        career = career_match.group(1)

    # 지역 파싱
    region_match = re.search(r"(서울(?:\s?[가-힣]+구)?|경기(?:\s?[가-힣]+시)?|인천|대전|충북|충남|대구|부산|전남|전북|강원)", text)
    if region_match:
        region = region_match.group(1).strip()

    return career, region


# ----------------------------------------------------
# 3. 고도화된 스코어링 & 계층형 도메인 분류기
# ----------------------------------------------------
ROLE_WEIGHTS = {
    "pm": 4.0, "bm": 4.0, "product manager": 4.0, "brand manager": 4.0,
    "프로덕트매니저": 4.0, "브랜드매니저": 4.0, "마케팅": 2.5, "마케터": 2.5,
    "브랜드": 2.0, "제품기획": 2.0, "제품전략": 2.0, "사업기획": 1.5
}

DOMAIN_SIGNALS = {
    "otc_direct": (["otc", "일반의약품", "일반약", "비처방"], 6.0),
    "chc_direct": (["컨슈머헬스", "컨슈머헬스케어", "chc", "소비자헬스케어"], 5.0),
    "health_food": (["건기식", "건강기능식품"], 4.5),
    "channel_pharmacy": (["약국", "드럭스토어", "온누리"], 3.0),
    "general_pharma": (["제약", "약품", "바이오", "헬스케어"], 1.5)
}

PENALTY_ROLES = [
    ("품질보증", -8.0), ("품질관리", -8.0), ("qa", -6.0), ("qc", -6.0), ("gmp", -6.0),
    ("analyst", -6.0), ("애널리스트", -6.0), ("임상", -6.0), ("cra", -6.0), ("crc", -6.0),
    ("ra", -5.0), ("인허가", -5.0), ("생산관리", -6.0), ("제조", -5.0), ("공정", -5.0),
    ("병원영업", -5.0), ("mr", -4.0), ("개발자", -10.0), ("시공", -10.0), ("건설", -10.0)
]

def evaluate_job(row: dict) -> dict:
    title = str(row.get("채용제목", "")).lower()
    corp = str(row.get("기업명", "")).lower()
    cond = str(row.get("지원조건", "")).lower()
    full_text = f"{corp} {title} {cond}"

    # 1. 직무 점수 (누적 합산 후 상한 10점 적용)
    role_score = 0.0
    matched_roles = []
    for kw, weight in ROLE_WEIGHTS.items():
        is_word = len(kw) <= 3
        hit_title = re.search(r'\b' + re.escape(kw) + r'\b', title) if is_word else kw in title
        hit_cond = re.search(r'\b' + re.escape(kw) + r'\b', cond) if is_word else kw in cond

        if hit_title:
            role_score += weight * 2.0
            matched_roles.append(kw)
        elif hit_cond:
            role_score += weight * 1.0
            matched_roles.append(kw)
    role_score = min(role_score, 10.0)

    # 2. 도메인 신호 점수 (동일 신호 그룹 내 중복 누적 방지)
    domain_score = 0.0
    matched_domains = []
    has_otc_direct = False
    has_chc_direct = False
    has_food_direct = False

    for group_name, (kws, weight) in DOMAIN_SIGNALS.items():
        matched_in_group = [k for k in kws if k in full_text]
        if matched_in_group:
            domain_score += weight
            matched_domains.append(matched_in_group[0])
            if group_name == "otc_direct": has_otc_direct = True
            if group_name == "chc_direct": has_chc_direct = True
            if group_name == "health_food": has_food_direct = True

    # 3. 페널티 감점 (제목에 타 직무 명시 시 감점 극대화)
    penalty_score = 0.0
    matched_penalties = []
    for kw, pt in PENALTY_ROLES:
        is_word = len(kw) <= 3
        hit_title = re.search(r'\b' + re.escape(kw) + r'\b', title) if is_word else kw in title
        if hit_title:
            penalty_score += pt * 2.0
            matched_penalties.append(f"{kw}(제목)")
        elif kw in cond and not any(ok in cond for ok in ["협업", "우대", "커뮤니케이션"]):
            penalty_score += pt * 0.5
            matched_penalties.append(f"{kw}(조건)")

    total_score = max(round(role_score + domain_score + penalty_score, 1), 0.0)

    # 4. 정밀 카테고리 판정 (약국 단독 신호에 의한 오분류 원천 차단)
    is_etc = bool(re.search(r'\betc\b', full_text) or any(k in full_text for k in ["전문의약품", "원내", "병원마케팅"]))

    if has_otc_direct:
        category = "OTC PM"
    elif has_chc_direct:
        category = "CHC / 컨슈머"
    elif has_food_direct:
        category = "건기식"
    elif is_etc and not has_otc_direct:
        category = "전문의약품(ETC)"
    elif "약국" in full_text:
        category = "CHC / 약국채널"
    elif any(k in full_text for k in ["제약", "약품", "바이오"]):
        category = "일반 제약마케팅"
    else:
        category = "기타 헬스케어"

    # 매칭 등급 판정
    if total_score >= 13:
        fit_grade = "⭐⭐⭐ 높음"
    elif total_score >= 7:
        fit_grade = "⭐⭐ 보통"
    elif total_score >= 4:
        fit_grade = "⭐ 관심"
    else:
        fit_grade = "검토 필요"

    career, region = parse_career_and_region(cond)

    row["카테고리"] = category
    row["매칭등급"] = fit_grade
    row["매칭점수"] = total_score
    row["경력요건"] = career
    row["근무지역"] = region
    
    reasons = matched_roles[:2] + matched_domains[:2]
    if matched_penalties:
        reasons.append(f"감점:{matched_penalties[0]}")
    row["매칭근거"] = ", ".join(reasons) if reasons else "기본 매칭"
    return row


# ----------------------------------------------------
# 4. 고신뢰도 수집 파이프라인
# ----------------------------------------------------
def crawl_saramin(keywords: list[str], sort: str, max_pages: int, stats: dict) -> list[dict]:
    results = []
    sort_code = "date" if sort == "최신순" else "relation"
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9"
    })

    for kw in keywords:
        for page in range(1, max_pages + 1):
            stats["saramin_req"] += 1
            url = f"https://www.saramin.co.kr/zf_user/search/recruit?searchword={urllib.parse.quote(kw)}&recruitPage={page}&recruitSort={sort_code}&recruitPageCount=40"
            try:
                res = session.get(url, timeout=7)
                soup = BeautifulSoup(res.text, "html.parser")
                items = soup.select(".item_recruit")
                stats["saramin_found"] += len(items)

                for item in items:
                    title_elem = item.select_one(".job_tit a")
                    corp_elem = item.select_one(".area_corp .corp_name a") or item.select_one(".corp_name")
                    conditions = [span.get_text(strip=True) for span in item.select(".job_condition span")]
                    date_elem = item.select_one(".job_date .date") or item.select_one(".job_date")

                    if not title_elem or not corp_elem:
                        continue

                    href = title_elem.get("href", "")
                    full_link = f"https://www.saramin.co.kr{href}" if href.startswith("/") else href
                    date_text = date_elem.get_text(strip=True) if date_elem else "상시채용"
                    dday_str, dday_val = parse_dday(date_text)

                    results.append({
                        "공고ID": extract_job_id("사람인", full_link),
                        "플랫폼": "사람인",
                        "검색키워드": kw,
                        "기업명": corp_elem.get_text(strip=True),
                        "채용제목": title_elem.get_text(strip=True),
                        "지원조건": " | ".join(conditions),
                        "마감일": date_text,
                        "D-day": dday_str,
                        "dday_sort": dday_val,
                        "링크": full_link,
                    })
            except Exception as e:
                stats["errors"].append(f"사람인 [{kw} {page}p]: {str(e)}")

    return results

def crawl_jobkorea(keywords: list[str], sort: str, max_pages: int, stats: dict) -> list[dict]:
    results = []
    ord_code = "2" if sort == "최신순" else "1"

    with sync_playwright() as p:
        browser = None
        try:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                locale="ko-KR"
            )
            page = context.new_page()
            page.route("**/*", lambda r: r.abort() if r.request.resource_type in ["image", "media", "font", "stylesheet"] else r.continue_())
            page.add_init_script("delete Object.getPrototypeOf(navigator).webdriver;")

            for kw in keywords:
                for page_num in range(1, max_pages + 1):
                    stats["jobkorea_req"] += 1
                    url = f"https://www.jobkorea.co.kr/Search/?stext={urllib.parse.quote(kw)}&tabType=recruit&Page_No={page_num}&Ord={ord_code}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=9000)
                        try:
                            page.wait_for_selector("a[href*='GI_Read']", timeout=3000)
                        except Exception:
                            pass

                        soup = BeautifulSoup(page.content(), "html.parser")
                        job_links = soup.select("a[href*='/Recruit/GI_Read/']")
                        stats["jobkorea_found"] += len(job_links)

                        for a in job_links:
                            title = a.get_text(strip=True)
                            href = a.get("href", "")
                            if not title or len(title) < 2:
                                continue

                            card = a.find_parent("article") or a.find_parent("li") or a.find_parent("div", class_="list-item")
                            corp_name = "회사명 미기재"
                            date_str = "상시채용"
                            cond_str = ""

                            if card:
                                corp_elem = card.select_one("a[href*='/Company/'], .name, .corp-name, .list-section-corp")
                                if corp_elem: corp_name = corp_elem.get_text(strip=True)
                                date_elem = card.select_one(".date, .time")
                                if date_elem: date_str = date_elem.get_text(strip=True)
                                chips = card.select(".chip, .etc span, .desc span")
                                if chips:
                                    cond_str = " | ".join([c.get_text(strip=True) for c in chips if c.get_text(strip=True)])

                            full_link = f"https://www.jobkorea.co.kr{href}" if href.startswith("/") else href
                            dday_str, dday_val = parse_dday(date_str)

                            results.append({
                                "공고ID": extract_job_id("잡코리아", full_link),
                                "플랫폼": "잡코리아",
                                "검색키워드": kw,
                                "기업명": corp_name,
                                "채용제목": title,
                                "지원조건": cond_str,
                                "마감일": date_str,
                                "D-day": dday_str,
                                "dday_sort": dday_val,
                                "링크": full_link,
                            })
                    except Exception as e:
                        stats["errors"].append(f"잡코리아 [{kw} {page_num}p]: {str(e)}")
        finally:
            if browser:
                browser.close()

    return results


# ----------------------------------------------------
# 5. Streamlit 사용자 인터페이스
# ----------------------------------------------------
st.set_page_config(page_title="제약 OTC PM 채용 인텔리전스", page_icon="💊", layout="wide")

# 영구 저장소 데이터 로드
history_data = load_history_safe(HISTORY_FILE)
bookmarks_data = load_json(BOOKMARKS_FILE)

today_str = datetime.now().strftime("%Y-%m-%d")

st.title("💊 제약 OTC PM 이직 모니터링 인텔리전스")
st.caption("신규 공고 일자별 추적, 동적 D-Day, 관심공고 영구 보관 기능을 갖춘 이직 의사결정 시스템입니다.")

with st.sidebar:
    st.header("🔍 검색 및 모니터링 조건")
    selected_keywords = st.multiselect(
        "검색 키워드 풀",
        options=["OTC PM", "일반의약품 PM", "OTC 마케팅", "컨슈머헬스케어 PM", "건기식 PM", "제약 PM"],
        default=["OTC PM", "일반의약품 PM", "컨슈머헬스케어 PM"]
    )
    platforms = st.multiselect("수집 플랫폼", ["사람인", "잡코리아"], default=["사람인", "잡코리아"])
    sort_order = st.radio("정렬 방식", ["최신순", "정확도순"], horizontal=True)
    pages_per_kw = st.slider("키워드당 수집 페이지 수", 1, 3, 1)

    st.divider()
    st.subheader("🎯 뷰 필터링")
    filter_categories = st.multiselect(
        "직무 카테고리",
        options=["OTC PM", "CHC / 컨슈머", "건기식", "CHC / 약국채널", "전문의약품(ETC)", "일반 제약마케팅", "기타 헬스케어"],
        default=["OTC PM", "CHC / 컨슈머", "건기식"]
    )
    min_score = st.slider("최소 공고 매칭 점수", 0, 20, 5, 1)
    only_new = st.checkbox("🆕 오늘 처음 발견된 공고만 보기", value=False)
    hide_closed = st.checkbox("마감된 공고 숨기기", value=True)

    run_search = st.button("공고 모니터링 실행", type="primary", use_container_width=True)

if run_search:
    if not selected_keywords:
        st.warning("최소 1개 이상의 검색 키워드를 선택해 주세요.")
    else:
        stats = {"saramin_req": 0, "saramin_found": 0, "jobkorea_req": 0, "jobkorea_found": 0, "errors": []}
        raw_list = []

        with st.spinner("플랫폼별 공고 수집 및 평가 중..."):
            if "사람인" in platforms:
                raw_list.extend(crawl_saramin(selected_keywords, sort_order, pages_per_kw, stats))
            if "잡코리아" in platforms:
                raw_list.extend(crawl_jobkorea(selected_keywords, sort_order, pages_per_kw, stats))

        if raw_list:
            df = pd.DataFrame(raw_list).drop_duplicates(subset=["공고ID"]).reset_index(drop=True)

            # 일자별 이력 관리 및 '오늘 신규' 판정
            updated_history = dict(history_data)
            status_list = []

            for _, r in df.iterrows():
                cid = r["공고ID"]
                if cid not in updated_history:
                    # 최초 발견
                    updated_history[cid] = {
                        "first_seen": today_str,
                        "last_seen": today_str,
                        "title": r["채용제목"],
                        "corp": r["기업명"]
                    }
                    status_list.append("🆕 오늘 신규")
                else:
                    # 기존 공고의 최근 확인일 갱신
                    updated_history[cid]["last_seen"] = today_str
                    if updated_history[cid]["first_seen"] == today_str:
                        status_list.append("🆕 오늘 신규")
                    else:
                        status_list.append("🔄 기존 공고")

            save_json(HISTORY_FILE, updated_history)
            df["발견상태"] = status_list

            # 스코어링 및 카테고리 매핑
            processed = [evaluate_job(row) for row in df.to_dict(orient="records")]
            full_df = pd.DataFrame(processed)
            full_df = full_df.sort_values(by=["매칭점수", "dday_sort"], ascending=[False, True]).reset_index(drop=True)

            st.session_state["full_df"] = full_df
            st.session_state["stats"] = stats

# ----------------------------------------------------
# 6. 결과 시각화 및 상호작용
# ----------------------------------------------------
if "full_df" in st.session_state:
    df = st.session_state["full_df"]
    stats = st.session_state.get("stats", {})

    # 필터 적용
    view_df = df[
        (df["카테고리"].isin(filter_categories)) &
        (df["매칭점수"] >= min_score)
    ]
    if only_new:
        view_df = view_df[view_df["발견상태"] == "🆕 오늘 신규"]
    if hide_closed:
        view_df = view_df[view_df["dday_sort"] >= 0]

    # 상단 메트릭 카드
    m1, m2, m3, m4, m5 = st.columns(5)
    new_count = len(view_df[view_df["발견상태"] == "🆕 오늘 신규"])
    otc_count = len(view_df[view_df["카테고리"] == "OTC PM"])
    chc_count = len(view_df[view_df["카테고리"].isin(["CHC / 컨슈머", "건기식"])])
    urgent_count = len(view_df[view_df["dday_sort"].between(0, 7)])

    m1.metric("선별 공고", f"{len(view_df)}건")
    m2.metric("🆕 오늘 신규", f"{new_count}건")
    m3.metric("💊 OTC PM", f"{otc_count}건")
    m4.metric("🌿 CHC / 건기식", f"{chc_count}건")
    m5.metric("⏰ 마감임박 (7일내)", f"{urgent_count}건")

    # 수집 통계 및 에러 콘솔
    with st.expander("📊 수집 파이프라인 상태 확인 (통계 & 에러)"):
        st.write(f"**사람인:** 요청 {stats.get('saramin_req', 0)}회 | 발견 {stats.get('saramin_found', 0)}건")
        st.write(f"**잡코리아:** 요청 {stats.get('jobkorea_req', 0)}회 | 발견 {stats.get('jobkorea_found', 0)}건")
        if stats.get("errors"):
            st.error(f"오류 {len(stats['errors'])}건 발생:")
            for err in stats["errors"]:
                st.code(err)

    tab_filtered, tab_otc, tab_chc, tab_bookmarks = st.tabs([
        "📋 필터 적용 목록", "💊 OTC PM 집중 탭", "🌿 CHC·소비자헬스 탭", "⭐ 관심공고 관리함"
    ])

    cols_to_show = ["발견상태", "카테고리", "매칭등급", "매칭점수", "기업명", "채용제목", "경력요건", "근무지역", "D-day", "매칭근거", "링크"]

    def render_grid(target_df):
        if target_df.empty:
            st.info("조건에 부합하는 공고가 없습니다.")
            return

        st.dataframe(
            target_df[cols_to_show],
            column_config={
                "링크": st.column_config.LinkColumn("공고", display_text="열기 ↗"),
                "채용제목": st.column_config.TextColumn("채용공고명", width="large"),
                "매칭근거": st.column_config.TextColumn("판단 키워드", width="medium"),
            },
            use_container_width=True,
            hide_index=True,
        )

    with tab_filtered:
        render_grid(view_df)

    with tab_otc:
        render_grid(view_df[view_df["카테고리"] == "OTC PM"])

    with tab_chc:
        render_grid(view_df[view_df["카테고리"].isin(["CHC / 컨슈머", "건기식"])])

    with tab_bookmarks:
        st.subheader("📌 관심 공고 영구 보관 및 상태 관리")
        st.caption("저장된 공고는 브라우저를 닫거나 서버가 재시작되어도 `bookmarks.json`에 영구 보존됩니다.")

        c_left, c_right = st.columns([2, 1])
        with c_left:
            job_select = st.selectbox(
                "관리할 공고 선택 (현재 검색 결과 기준)",
                options=df["공고ID"].tolist(),
                format_func=lambda x: f"[{df.loc[df['공고ID']==x, '기업명'].values[0]}] {df.loc[df['공고ID']==x, '채용제목'].values[0]}"
            )
        with c_right:
            new_status = st.selectbox("보관 상태 지정", ["⭐ 관심", "📝 검토중", "📨 지원완료", "❌ 삭제"])
            if st.button("상태 저장", use_container_width=True):
                if new_status == "❌ 삭제":
                    bookmarks_data.pop(job_select, None)
                else:
                    bookmarks_data[job_select] = {
                        "status": new_status,
                        "title": df.loc[df['공고ID']==job_select, '채용제목'].values[0],
                        "corp": df.loc[df['공고ID']==job_select, '기업명'].values[0],
                        "link": df.loc[df['공고ID']==job_select, '링크'].values[0],
                        "dday": df.loc[df['공고ID']==job_select, 'D-day'].values[0],
                        "saved_at": today_str
                    }
                save_json(BOOKMARKS_FILE, bookmarks_data)
                st.toast("관심 공고 상태가 업데이트되었습니다.")

        if bookmarks_data:
            bookmarked_list = []
            for jid, val in bookmarks_data.items():
                bookmarked_list.append({
                    "상태": val.get("status"),
                    "기업명": val.get("corp"),
                    "공고명": val.get("title"),
                    "D-day": val.get("dday"),
                    "저장일": val.get("saved_at"),
                    "링크": val.get("link")
                })
            b_df = pd.DataFrame(bookmarked_list)
            st.dataframe(
                b_df,
                column_config={"링크": st.column_config.LinkColumn("공고", display_text="열기 ↗")},
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("현재 저장된 관심 공고가 없습니다.")

    # CSV 내보내기
    st.divider()
    csv_data = view_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="📥 필터링된 공고 CSV 내보내기",
        data=csv_data,
        file_name=f"OTC_PM_공고_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv"
    )

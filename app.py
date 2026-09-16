import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import urllib.parse
from datetime import datetime
from playwright.sync_api import sync_playwright
import re
import pandas as pd

# ----------------------------------------------------
# 1. 정밀 필터링 키워드 사전
# ----------------------------------------------------

# (A) 필수 직무 키워드: 공고 제목에 아래 마케팅/기획 관련 단어가 최소 1개 있어야 함
ROLE_MUST_KEYWORDS = [
    "pm", "bm", "마케팅", "기획", "브랜드", 
    "product manager", "brand manager", "marketer", "마케터"
]

# (B) 필수 산업 도메인: 공고 전체(기업/제목/요건)에 제약/헬스케어 단어가 1개 이상 있어야 함
PHARMA_KEYWORDS = [
    "otc", "일반의약품", "일반약", "제약", "약품", "바이오", 
    "헬스케어", "컨슈머헬스", "의약", "약국", "건기식", "건강기능식품", 
    "chc", "pharma", "의료기기"
]

# (C) [제목 타겟] PM/마케팅이 아닌 타 직군 키워드 (제목에 있으면 무조건 탈락)
ROLE_EXCLUDE_TITLE_KEYWORDS = [
    # 품질/제조/생산
    "품질보증", "품질관리", "품질", "qa", "qc", "gmp", "밸리데이션", "validation",
    "생산관리", "제조", "공정", "공장", "생산직", "안전관리",
    # 연구/임상/개발
    "analyst", "애널리스트", "연구원", "제제연구", "합성", "임상", "cra", "crc", "pv", 
    "약물감시", "ra", "인허가", "개발부", "임상시험", "통계",
    # 영업/약사/기타
    "병원영업", "의원영업", "도매영업", "mr", "약사", "관리약사", 
    "회계", "재무", "인사", "총무", "법무"
]

# (D) [전체 타겟] 타 업종 키워드 (전체 텍스트에 포함 시 탈락)
INDUSTRY_EXCLUDE_KEYWORDS = [
    "개발자", "백엔드", "프론트엔드", "소프트웨어", "si", "웹개발",
    "게임", "건설", "시공", "토목", "인테리어", "건축", 
    "물류센터", "서버", "반도체", "금형"
]


def filter_pharma_jobs(df: pd.DataFrame, use_filter: bool = True) -> pd.DataFrame:
    """
    제약 산업군이면서 동시에 순수 'OTC PM / 마케팅' 직무만 정밀 선별
    """
    if df.empty or not use_filter:
        return df

    def is_valid_otc_pm(row):
        title = str(row.get("채용제목", "")).lower()
        corp = str(row.get("기업명", "")).lower()
        cond = str(row.get("지원조건", "")).lower()
        full_corpus = f"{corp} {title} {cond}"

        # 1단계: 타 업종 키워드가 포함되어 있으면 탈락 (IT, 건설 등)
        for bad_ind in INDUSTRY_EXCLUDE_KEYWORDS:
            if bad_ind in full_corpus:
                return False

        # 2단계: [핵심] 제목에 품질(QA), 생산, 연구, Analyst, 임상 등 타 직무가 명시되어 있으면 탈락
        # (단어 경계 및 부분 일치 검사)
        for bad_role in ROLE_EXCLUDE_TITLE_KEYWORDS:
            # 영문 단어(qa, qc 등)는 다른 단어의 일부로 오인되지 않게 단어 단위 정규식 체크
            if len(bad_role) <= 3:
                if re.search(r'\b' + re.escape(bad_role) + r'\b', title):
                    return False
            else:
                if bad_role in title:
                    return False

        # 3단계: [핵심] 제목에 PM / BM / 마케팅 / 기획 관련 직무 키워드가 필수 포함되어야 함
        has_pm_role = False
        for role in ROLE_MUST_KEYWORDS:
            if len(role) <= 3:
                if re.search(r'\b' + re.escape(role) + r'\b', title):
                    has_pm_role = True
                    break
            else:
                if role in title:
                    has_pm_role = True
                    break

        if not has_pm_role:
            return False

        # 4단계: 제약/바이오/OTC/건기식 도메인 키워드가 1개 이상 포함되어야 함
        has_pharma_domain = any(domain in full_corpus for domain in PHARMA_KEYWORDS)
        if not has_pharma_domain:
            return False

        return True

    filtered_df = df[df.apply(is_valid_otc_pm, axis=1)].reset_index(drop=True)
    return filtered_df

# ----------------------------------------------------
# 2. 사람인 크롤러 (requests 기반 고속 처리)
# ----------------------------------------------------
def crawl_saramin(keywords: list[str], sort: str = "최신순", max_pages: int = 1) -> list[dict]:
    results = []
    sort_code = "date" if sort == "최신순" else "relation"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    for keyword in keywords:
        for page in range(1, max_pages + 1):
            url = (
                f"https://www.saramin.co.kr/zf_user/search/recruit?"
                f"searchword={urllib.parse.quote(keyword)}&recruitPage={page}"
                f"&recruitSort={sort_code}&recruitPageCount=40"
            )
            try:
                res = requests.get(url, headers=headers, timeout=8)
                soup = BeautifulSoup(res.text, "html.parser")
                items = soup.select(".item_recruit")

                for item in items:
                    title_elem = item.select_one(".job_tit a")
                    corp_elem = item.select_one(".area_corp .corp_name a") or item.select_one(".corp_name")
                    conditions = [span.get_text(strip=True) for span in item.select(".job_condition span")]
                    date_elem = item.select_one(".job_date .date") or item.select_one(".job_date")

                    if not title_elem or not corp_elem:
                        continue

                    href = title_elem.get("href", "")
                    full_link = f"https://www.saramin.co.kr{href}" if href.startswith("/") else href

                    results.append({
                        "플랫폼": "사람인",
                        "검색키워드": keyword,
                        "기업명": corp_elem.get_text(strip=True),
                        "채용제목": title_elem.get_text(strip=True),
                        "지원조건": " | ".join(conditions),
                        "마감일": date_elem.get_text(strip=True) if date_elem else "상시채용",
                        "링크": full_link,
                    })
            except Exception:
                continue
    return results


# ----------------------------------------------------
# 3. 잡코리아 크롤러 (Playwright 봇 탐지 우회 및 고유 식별자 파서)
# ----------------------------------------------------
def crawl_jobkorea(keywords: list[str], sort: str = "최신순", max_pages: int = 1) -> list[dict]:
    results = []
    ord_code = "2" if sort == "최신순" else "1"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--window-size=1920,1080",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        page = context.new_page()

        # webdriver 탐지 속성 무력화
        page.add_init_script("delete Object.getPrototypeOf(navigator).webdriver;")

        for keyword in keywords:
            for page_num in range(1, max_pages + 1):
                url = (
                    f"https://www.jobkorea.co.kr/Search/?"
                    f"stext={urllib.parse.quote(keyword)}&tabType=recruit&Page_No={page_num}&Ord={ord_code}"
                )
                try:
                    page.goto(url, wait_until="networkidle", timeout=15000)
                    
                    try:
                        page.wait_for_selector("a[href*='GI_Read']", timeout=5000)
                    except Exception:
                        pass

                    soup = BeautifulSoup(page.content(), "html.parser")
                    job_links = soup.select("a[href*='/Recruit/GI_Read/']")
                    seen_urls = set()

                    for a in job_links:
                        title = a.get_text(strip=True)
                        href = a.get("href", "")

                        if not title or len(title) < 2 or href in seen_urls:
                            continue

                        card = a.find_parent("article") or a.find_parent("li") or a.find_parent("div", class_="list-item")
                        
                        corp_name = "회사명 확인불가"
                        date_str = "상시채용"
                        cond_str = ""

                        if card:
                            corp_elem = (
                                card.select_one("a[href*='/Company/']") 
                                or card.select_one(".name") 
                                or card.select_one(".corp-name")
                                or card.select_one(".list-section-corp")
                            )
                            if corp_elem:
                                corp_name = corp_elem.get_text(strip=True)

                            date_elem = card.select_one(".date") or card.select_one(".time")
                            if date_elem:
                                date_str = date_elem.get_text(strip=True)

                            chips = card.select(".chip, .etc span, .desc span")
                            if chips:
                                cond_str = " | ".join([c.get_text(strip=True) for c in chips if c.get_text(strip=True)])

                        full_link = f"https://www.jobkorea.co.kr{href}" if href.startswith("/") else href
                        seen_urls.add(href)

                        results.append({
                            "플랫폼": "잡코리아",
                            "검색키워드": keyword,
                            "기업명": corp_name,
                            "채용제목": title,
                            "지원조건": cond_str,
                            "마감일": date_str,
                            "링크": full_link,
                        })
                except Exception:
                    continue

        browser.close()

    return results


# ----------------------------------------------------
# 4. Streamlit 인터랙티브 웹 대시보드
# ----------------------------------------------------
st.set_page_config(
    page_title="제약 OTC PM 채용 모니터링", 
    page_icon="💊", 
    layout="wide"
)

st.title("💊 제약 OTC PM 통합 구인정보 대시보드")
st.caption("사람인과 잡코리아의 최신 구인공고를 실시간 취합하고, 비제약 잡음을 자동으로 필터링합니다.")

with st.sidebar:
    st.header("🔍 검색 설정")
    
    selected_keywords = st.multiselect(
        "검색 키워드 (다중 선택)",
        options=["OTC PM", "일반의약품 PM", "OTC 마케팅", "컨슈머헬스케어 PM", "제약 PM", "건기식 PM"],
        default=["OTC PM", "일반의약품 PM", "OTC 마케팅"]
    )
    custom_keyword = st.text_input("추가 직접 입력 (쉼표 구분)", placeholder="예: OTC BM, 컨슈머헬스")
    
    platforms = st.multiselect("수집 대상 플랫폼", ["사람인", "잡코리아"], default=["사람인", "잡코리아"])
    sort_order = st.radio("정렬 기준", ["최신순", "정확도순"], horizontal=True)
    pages_per_kw = st.slider("키워드당 수집 페이지 수", 1, 3, 1)
    
    st.divider()
    st.subheader("🎯 도메인 정밀 필터링")
    filter_pharma = st.toggle(
        "제약/헬스케어 도메인만 보기", 
        value=True, 
        help="IT 기획자, 시공 PM, 게임 PM 등 제약과 무관한 공고를 자동으로 차단합니다."
    )
    
    run_search = st.button("공고 검색 실행", type="primary", use_container_width=True)

# 검색 키워드 조합
all_keywords = list(selected_keywords)
if custom_keyword:
    all_keywords.extend([k.strip() for k in custom_keyword.split(",") if k.strip()])

if run_search:
    if not all_keywords:
        st.warning("최소 1개 이상의 키워드를 선택하거나 입력해 주세요.")
    else:
        all_data = []

        # 1. 사람인 수집
        if "사람인" in platforms:
            with st.spinner("사람인 공고 수집 중..."):
                saramin_data = crawl_saramin(all_keywords, sort=sort_order, max_pages=pages_per_kw)
                all_data.extend(saramin_data)

        # 2. 잡코리아 수집
        if "잡코리아" in platforms:
            with st.spinner("잡코리아 브라우저 렌더링 및 공고 파싱 중... (수초 소요)"):
                jobkorea_data = crawl_jobkorea(all_keywords, sort=sort_order, max_pages=pages_per_kw)
                all_data.extend(jobkorea_data)

        if all_data:
            raw_df = pd.DataFrame(all_data)
            raw_df = raw_df.drop_duplicates(subset=["기업명", "채용제목"]).reset_index(drop=True)
            
            # 도메인 필터 적용
            df = filter_pharma_jobs(raw_df, use_filter=filter_pharma)
            excluded_count = len(raw_df) - len(df)

            # 요약 지표 카드
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("최종 선별 공고", f"{len(df)}건")
            c2.metric("타 업종 차단", f"{excluded_count}건", delta=-excluded_count if excluded_count > 0 else 0)
            c3.metric("사람인 공고", f"{len(df[df['플랫폼'] == '사람인'])}건")
            c4.metric("잡코리아 공고", f"{len(df[df['플랫폼'] == '잡코리아'])}건")

            # 테이블 렌더링
            st.dataframe(
                df,
                column_config={
                    "링크": st.column_config.LinkColumn("공고 링크", display_text="공고 열기 ↗"),
                    "채용제목": st.column_config.TextColumn("채용공고명", width="large"),
                    "지원조건": st.column_config.TextColumn("자격 요건", width="medium"),
                    "기업명": st.column_config.TextColumn("기업명", width="small"),
                    "마감일": st.column_config.TextColumn("마감일", width="small"),
                },
                use_container_width=True,
                hide_index=True,
            )

            # CSV 다운로드 (Excel 한글 깨짐 방지 utf-8-sig)
            csv_data = df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label="📥 선별 공고 CSV 엑셀 다운로드",
                data=csv_data,
                file_name=f"제약_OTC_PM_채용공고_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                use_container_width=True
            )
        else:
            st.info("검색 조건에 일치하는 공고가 없습니다.")

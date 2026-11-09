"""우리 가족 운동 관리 앱
실행: pip install -r requirements.txt && streamlit run app.py
배포: GitHub 저장소에 두 파일 업로드 → Streamlit Community Cloud에서 app.py 지정.
선택 설정(Cloud의 Settings → Secrets):
APP_PASSWORD = "가족만 아는 비밀번호"
APP_URL = "https://배포한주소.streamlit.app"
DB_PATH = "family_fitness.db"
주의: Cloud의 로컬 SQLite는 재배포/인스턴스 교체 시 사라질 수 있습니다.
장기 운영에는 외부 영구 DB로 전환하고, 그전에는 CSV 백업을 보관하세요.
"""
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import hmac
import json
import zipfile
from datetime import timedelta
from PIL import Image, ImageOps
import sqlite3
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import qrcode
import streamlit as st

SPORTS = ["걷기", "달리기", "줄넘기", "헬스", "요가", "자전거", "수영", "등산"]
COLUMNS = ["기록ID", "가족", "날짜", "운동 종목", "운동 시간(분)"]


def connect_db(path):
    conn = sqlite3.connect(str(path), timeout=20)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect_db(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY)")
        conn.execute("""CREATE TABLE IF NOT EXISTS workouts (
            id TEXT PRIMARY KEY, member TEXT NOT NULL REFERENCES members(name),
            day TEXT NOT NULL, sport TEXT NOT NULL,
            minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 1440))""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workouts)")}
        for name, definition in [("category", "TEXT NOT NULL DEFAULT '미분류'"),
                                 ("photo", "BLOB"), ("effort", "INTEGER NOT NULL DEFAULT 3")]:
            if name not in cols:
                conn.execute(f"ALTER TABLE workouts ADD COLUMN {name} {definition}")
        # 최초 생성 때만 기본 구성원 등록. 이후 사용자가 수정한 목록을 유지합니다.
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        if not conn.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
            conn.executemany("INSERT OR IGNORE INTO members VALUES (?)", [(n,) for n in ["아빠", "엄마", "나"]])
            conn.execute("INSERT INTO settings VALUES ('initialized', '1')")


def load_data(path):
    with connect_db(path) as conn:
        members = [r[0] for r in conn.execute("SELECT name FROM members ORDER BY rowid")]
        rows = conn.execute("SELECT id, member, day, sport, minutes FROM workouts ORDER BY day DESC, rowid DESC").fetchall()
    return members, pd.DataFrame(rows, columns=COLUMNS)


def save_record(path, rid, member, day, sport, minutes, update=False):
    if not sport.strip() or len(sport.strip()) > 40:
        raise ValueError("운동 종목은 1~40자로 입력해주세요.")
    if not 1 <= int(minutes) <= 1440:
        raise ValueError("운동 시간은 1~1,440분으로 입력해주세요.")
    date.fromisoformat(str(day))
    with connect_db(path) as conn:
        if update:
            cursor = conn.execute("UPDATE workouts SET member=?, day=?, sport=?, minutes=? WHERE id=?",
                                  (member, str(day), sport.strip(), int(minutes), rid))
            if cursor.rowcount != 1:
                raise ValueError("다른 접속에서 삭제된 기록입니다. 새로고침해주세요.")
        else:
            conn.execute("INSERT INTO workouts VALUES (?, ?, ?, ?, ?)",
                         (rid, member, str(day), sport.strip(), int(minutes)))


def summarize(members, records):
    base = pd.DataFrame({"가족": members})
    if records.empty:
        base["총 운동 횟수"] = 0
        base["총 운동 시간(분)"] = 0
    else:
        grouped = records.groupby("가족").agg(**{
            "총 운동 횟수": ("기록ID", "count"), "총 운동 시간(분)": ("운동 시간(분)", "sum")}).reset_index()
        base = base.merge(grouped, on="가족", how="left").fillna(0)
        base[["총 운동 횟수", "총 운동 시간(분)"]] = base[["총 운동 횟수", "총 운동 시간(분)"]].astype(int)
    return base


def flash(message):
    st.session_state["notice"] = message
    st.rerun()


def setting(key, default=""):
    # Secrets 파일 없이 로컬에서 실행하는 경우도 지원합니다.
    try:
        return st.secrets[key]
    except (FileNotFoundError, KeyError):
        return default


# 각 종목의 대표 목적에 따른 단순 분류이며 실제 운동 효과의 전부를 뜻하지 않습니다.
CATS = ["유산소", "근력", "유연성·균형", "미분류"]
GUIDES = {
 "걷기": ("유산소", "편한 속도로 3분 → 대화 가능한 속도로 10분 → 천천히 2분", "시선은 앞을 보고 어깨에 힘을 빼세요. 보폭을 억지로 늘리지 마세요."),
 "달리기": ("유산소", "걷기 5분 → 가벼운 조깅 1분/걷기 2분을 3회 → 걷기 3분", "처음에는 속도보다 편한 호흡에 집중하세요. 무릎·발목 통증이 있으면 중단하세요."),
 "줄넘기": ("유산소", "가볍게 걷기 3분 → 줄넘기 20초/휴식 40초를 5회 → 걷기 2분", "미끄럽지 않은 바닥과 운동화를 사용하고 낮게 뛰세요. 관절 불편 시 걷기로 대체하세요."),
 "헬스": ("근력", "준비운동 5분 → 벽 푸시업 5~8회와 의자 앉았다 일어나기 5~8회를 1~2세트 → 정리 3분", "안정된 의자를 벽에 고정하세요. 반동을 피하고 힘쓸 때 숨을 내쉬세요. 무거운 기구는 지도자와 시작하세요."),
 "요가": ("유연성·균형", "편안한 호흡 2분 → 어깨·발목 가볍게 풀기 3분 → 의자를 잡고 뒤꿈치 들기 5회 → 편안한 호흡 2분", "관절을 끝까지 밀지 말고 통증 없는 범위에서 움직이세요. 균형 동작은 지지대를 잡으세요."),
 "자전거": ("유산소", "평탄한 곳에서 천천히 5분 → 편한 강도로 10분 → 천천히 3분", "헬멧을 착용하고 안장 높이와 브레이크를 확인하세요. 실내 자전거로 대체해도 좋아요."),
 "수영": ("유산소", "준비운동 후 안전요원·지도자가 있는 곳에서 익숙한 동작을 짧게 반복하고 충분히 쉬기", "수영이 처음이면 혼자 시작하지 마세요. 수심과 시설 안전수칙을 확인하세요."),
 "등산": ("유산소", "짧고 완만한 코스를 선택 → 평소보다 천천히 오르기 → 충분한 휴식 후 하산", "날씨·일몰·코스를 확인하고 물과 미끄럼 방지 신발을 준비하세요.")}


def all_records(db):
    with connect_db(db) as conn:
        return pd.read_sql_query("SELECT id, member, day, sport, minutes, category, effort, photo IS NOT NULL AS has_photo FROM workouts ORDER BY day DESC, rowid DESC", conn)


def normalized_photo(upload):
    if upload is None:
        return None
    if upload.size > 5 * 1024 * 1024:
        raise ValueError("사진은 5MB 이하로 첨부해주세요.")
    try:
        with Image.open(upload) as im:
            if im.width * im.height > 20_000_000:
                raise ValueError("사진 해상도가 너무 큽니다. 크기를 줄여주세요.")
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1280, 1280))
            clean = Image.new("RGB", im.size)
            clean.paste(im)
            out = BytesIO()
            clean.save(out, format="JPEG", quality=82)
            return out.getvalue()
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError("읽을 수 없는 사진입니다. JPG 또는 PNG를 사용해주세요.") from exc


def write_workout(db, rid, member, day, sport, minutes, category, effort, photo, update=False):
    if not sport.strip() or len(sport.strip()) > 40 or not 1 <= int(minutes) <= 1440:
        raise ValueError("종목은 1~40자, 시간은 1~1,440분으로 입력해주세요.")
    if category not in CATS or not 1 <= effort <= 5:
        raise ValueError("유형 또는 체감 강도를 확인해주세요.")
    if day > datetime.now(ZoneInfo("Asia/Seoul")).date():
        raise ValueError("미래 운동은 기록할 수 없어요.")
    with connect_db(db) as conn:
        if update:
            cur = conn.execute("UPDATE workouts SET day=?, sport=?, minutes=?, category=?, effort=?, photo=COALESCE(?,photo) WHERE id=? AND member=?",
                               (str(day), sport.strip(), minutes, category, effort, photo, rid, member))
            if cur.rowcount != 1:
                raise ValueError("내 기록이 아니거나 이미 삭제된 기록입니다.")
        else:
            if photo is None:
                raise ValueError("새 기록에는 운동 인증사진이 필요해요.")
            conn.execute("INSERT INTO workouts(id,member,day,sport,minutes,category,effort,photo) VALUES(?,?,?,?,?,?,?,?)",
                         (rid, member, str(day), sport.strip(), minutes, category, effort, photo))


def balance(data):
    known = data[data.category.isin(CATS[:3])]
    totals = known.groupby("category").minutes.sum().reindex(CATS[:3], fill_value=0)
    if len(data) < 3 or not totals.sum():
        return totals, "기록이 적어 아직 편중 여부를 판단하지 않아요. 서로 다른 활동을 3회 이상 기록해보세요."
    top = totals.idxmax()
    share = float(totals.max() / totals.sum())
    missing = totals[totals == 0].index.tolist()
    message = f"분류된 운동 시간 중 {top} 비중이 {share:.0%}예요. "
    if share >= .7:
        message += "앱의 참고 기준(70%)을 넘었어요. 현재 목표와 몸 상태에 맞는지 돌아보세요. "
    else:
        message += "앱의 편중 알림 기준(70%) 이내예요. "
    if missing:
        message += "기록이 없는 유형: " + ", ".join(missing) + ". 가능하다면 짧게 경험해보세요."
    return totals, message


def backup(db):
    with connect_db(db) as conn:
        members = [r[0] for r in conn.execute("SELECT name FROM members")]
        rows = conn.execute("SELECT id,member,day,sport,minutes,category,effort,photo FROM workouts").fetchall()
    manifest = {"version": 2, "members": members, "records": []}
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, row in enumerate(rows):
            r = dict(zip(["id","member","day","sport","minutes","category","effort"], row[:7]))
            if row[7]:
                r["photo_file"] = f"photos/{i}.jpg"
                z.writestr(r["photo_file"], row[7])
            manifest["records"].append(r)
        z.writestr("records.json", json.dumps(manifest, ensure_ascii=False))
    return out.getvalue()


def main():
    st.set_page_config(page_title="우리 가족 운동 관리", page_icon="🏃", layout="wide")
    st.title("🏃 우리 가족 운동 관리")
    st.caption("나에게 맞게 기록하고, 새로운 운동도 함께 경험해요")
    password = str(setting("APP_PASSWORD"))
    if password and not st.session_state.get("authenticated"):
        with st.form("login"):
            entry = st.text_input("가족 공용 비밀번호", type="password")
            if st.form_submit_button("입장"):
                if hmac.compare_digest(entry.encode(), password.encode()):
                    st.session_state.authenticated = True
                    st.rerun()
                st.error("비밀번호를 확인해주세요.")
        st.stop()
    if not password:
        st.warning("공용 비밀번호가 없습니다. 배포 Secrets에 APP_PASSWORD를 설정하세요. 구성원 선택은 본인 인증이 아니므로 가족끼리만 주소를 공유하세요.")
    db = str(setting("DB_PATH", "family_fitness.db"))
    init_db(db)
    members, _ = load_data(db)
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))
    if st.session_state.get("member") not in members:
        st.subheader("👋 누가 운동하러 왔나요?")
        if members:
            chosen = st.selectbox("내 이름", members)
            if st.button("내 운동 공간으로", type="primary"):
                st.session_state.member = chosen
                st.rerun()
        with st.form("new_member", clear_on_submit=True):
            name = st.text_input("새 가족 이름", max_chars=30)
            if st.form_submit_button("가족 등록"):
                name = name.strip()
                if not name or name in members:
                    st.error("비어 있지 않은 새로운 이름을 입력해주세요.")
                else:
                    with connect_db(db) as conn:
                        conn.execute("INSERT OR IGNORE INTO members VALUES(?)", (name,))
                    flash("등록했어요. 이름을 선택해 입장하세요.")
        st.stop()
    me = st.session_state.member
    with st.sidebar:
        st.header(f"{me}님의 운동 공간")
        page = st.radio("메뉴", ["내 운동 기록", "운동 PT 가이드", "운동 균형 피드백", "가족 현황", "백업·QR·설정"])
        if st.button("구성원 바꾸기"):
            st.session_state.clear()
            st.rerun()
        if st.button("최신 기록 불러오기"):
            st.rerun()
        st.caption("휴대폰에서는 왼쪽 위 메뉴 버튼을 열어 화면을 바꿔요.")
    records = all_records(db)
    mine = records[records.member == me].copy()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    if page == "내 운동 기록":
        st.subheader(f"✍️ {me}님의 기록")
        a,b,c = st.columns(3)
        a.metric("누적 횟수", f"{len(mine)}회")
        b.metric("누적 시간", f"{int(mine.minutes.sum())}분")
        c.metric("사진 첨부 기록", f"{int(mine.has_photo.sum())}회")
        st.info("사진 첨부는 자기 인증입니다. 실제 운동 수행·시간·사진 재사용 여부를 자동 검증하지 않습니다. 얼굴·위치·다른 사람의 모습이 드러나지 않게 촬영하세요.")
        sport = st.selectbox("오늘 한 운동", SPORTS + ["직접 입력"])
        default_cat = GUIDES.get(sport, ("미분류",))[0]
        with st.form("add", clear_on_submit=True):
            custom = st.text_input("직접 입력한 종목 (직접 입력 선택 시)", max_chars=40)
            day = st.date_input("운동 날짜", today, max_value=today)
            minutes = st.number_input("운동 시간(분)", 1, 1440, 30)
            cat = st.selectbox("대표 운동 유형", CATS, index=CATS.index(default_cat))
            effort = st.slider("체감 강도: 1 아주 편함 ~ 5 매우 힘듦", 1, 5, 3)
            photo = st.file_uploader("운동 인증사진 (필수, JPG/PNG, 5MB 이하)", type=["jpg","jpeg","png"])
            if st.form_submit_button("사진과 함께 저장", type="primary"):
                try:
                    write_workout(db, str(uuid4()), me, day, custom if sport == "직접 입력" else sport, minutes, cat, effort, normalized_photo(photo))
                    flash("운동과 인증사진을 저장했어요!")
                except (ValueError, sqlite3.Error) as exc:
                    st.error(str(exc))
        st.subheader("내 기록 수정·삭제")
        if mine.empty:
            st.caption("첫 기록을 추가해주세요.")
        else:
            st.dataframe(mine.drop(columns=["id", "member"]).rename(columns={"day":"날짜","sport":"종목","minutes":"시간(분)","category":"유형","effort":"체감 강도","has_photo":"사진 첨부"}), hide_index=True, use_container_width=True)
            rows = mine.set_index("id")
            rid = st.selectbox("기록 선택", mine.id.tolist(), format_func=lambda k: f"{rows.loc[k,'day']} · {rows.loc[k,'sport']} · {rows.loc[k,'minutes']}분 · {k[:8]}")
            r = rows.loc[rid]
            with connect_db(db) as conn:
                old_photo = conn.execute("SELECT photo FROM workouts WHERE id=? AND member=?", (rid,me)).fetchone()
            if old_photo and old_photo[0]:
                with st.expander("내 인증사진 보기"):
                    st.image(old_photo[0], width=280)
            else:
                st.caption("이전 버전 기록: 인증사진 없음")
            with st.form(f"edit_{rid}"):
                day = st.date_input("날짜 수정", date.fromisoformat(r.day), max_value=today)
                sp = st.text_input("종목 수정", r.sport, max_chars=40)
                mins = st.number_input("시간 수정(분)", 1, 1440, int(r.minutes))
                cat = st.selectbox("유형 수정", CATS, index=CATS.index(r.category) if r.category in CATS else 3)
                effort = st.slider("체감 강도 수정", 1, 5, int(r.effort))
                photo = st.file_uploader("사진 교체 (미첨부 시 기존 사진 유지)", type=["jpg","jpeg","png"])
                confirm = st.checkbox("선택한 내 기록과 사진을 영구 삭제하는 데 동의합니다")
                save = st.form_submit_button("수정 저장")
                delete = st.form_submit_button("기록 삭제")
                if save:
                    try:
                        write_workout(db,rid,me,day,sp,mins,cat,effort,normalized_photo(photo),True)
                        flash("수정했어요.")
                    except (ValueError,sqlite3.Error) as exc:
                        st.error(str(exc))
                if delete:
                    if not confirm:
                        st.error("삭제 동의를 체크해주세요.")
                    else:
                        with connect_db(db) as conn:
                            conn.execute("DELETE FROM workouts WHERE id=? AND member=?",(rid,me))
                        flash("기록과 사진을 삭제했어요.")
    elif page == "운동 PT 가이드":
        st.subheader("🧭 처음 해보는 운동도 한 걸음씩")
        st.warning("일반적인 입문 예시이며 개인 맞춤 처방·실시간 자세 분석이 아닙니다. 부상·질환·임신 등으로 운동 제한이 있다면 전문가에게 먼저 확인하세요. 통증·어지럼·흉통 시 즉시 중단하세요.")
        sport = st.selectbox("배워볼 운동", SPORTS)
        cat, routine, tip = GUIDES[sport]
        st.markdown(f"### {sport} · {cat}")
        st.markdown("**입문 루틴**")
        st.write(routine)
        st.markdown("**자세·안전 체크**")
        st.write(tip)
        st.checkbox("내 몸 상태와 주변 안전을 확인했어요")
        st.checkbox("준비운동과 정리운동을 계획했어요")
        st.caption("가이드 열람만으로 운동 기록은 생성되지 않아요. 실제 수행 후 내 운동 기록에서 사진과 함께 저장하세요.")
    elif page == "운동 균형 피드백":
        st.subheader("🌱 좋아하는 운동에만 치우치고 있나요?")
        period = st.selectbox("분석 기간", [7, 30, 90], index=1, format_func=lambda n:f"최근 {n}일")
        recent = mine[(mine.day >= str(today-timedelta(days=period-1))) & (mine.day <= str(today))]
        totals, message = balance(recent)
        st.write(message)
        st.dataframe(totals.rename("운동 시간(분)").rename_axis("운동 유형"), use_container_width=True)
        unknown = int(recent.loc[recent.category == "미분류", "minutes"].sum())
        if unknown:
            st.info(f"미분류 {unknown}분은 비율 계산에서 제외했어요. 이전 기록의 유형을 수정하면 분석에 반영됩니다.")
        least = totals.idxmin()
        suggestions = [name for name,g in GUIDES.items() if g[0] == least]
        st.markdown(f"**새롭게 경험해볼 유형: {least}**")
        st.write("추천 예시: " + ", ".join(suggestions))
        st.caption("유형별 시간과 70%는 앱의 탐색용 참고 기준이며 의학적 평가가 아닙니다. 세 유형을 똑같이 해야 한다는 뜻은 아닙니다. 종목명만으로 근육 부위별 불균형을 판정할 수 없으며, 체감 강도는 비율에 반영하지 않습니다. 운동 목적·회복·장애·몸 상태에 맞춰 조절하세요.")
    elif page == "가족 현황":
        st.subheader("👨‍👩‍👧‍👦 함께 쌓은 운동")
        stats = records.groupby("member").agg(횟수=("id","count"),시간_분=("minutes","sum"),사진첨부=("has_photo","sum")).reindex(members,fill_value=0)
        stats.index.name = "가족"
        st.dataframe(stats, use_container_width=True)
        if not records.empty:
            st.bar_chart(stats[["시간_분"]], horizontal=True, color="#1751D0")
        st.caption("다른 가족의 기록·사진은 이 화면에서 수정하거나 열 수 없어요. 다만 구성원 전환은 별도 본인 인증이 아니므로 보안상 접근 통제는 아닙니다.")
    else:
        st.subheader("💾 백업 · QR · 설정")
        st.warning("SQLite와 사진은 서버 파일에 함께 저장됩니다. Cloud 재배포·인스턴스 교체 시 유실될 수 있어요. 장기 운영에는 외부 DB·사진 저장소를 권장합니다.")
        st.caption("전체 백업에는 모든 가족의 기록과 인증사진이 포함됩니다. 가족끼리 동의 후 안전하게 보관하세요. 현재 버전은 전체 백업 내보내기를 제공하며, 파일 업로드 복원 UI는 제공하지 않습니다.")
        if st.button("전체 백업 ZIP 준비"):
            st.download_button("기록·사진 ZIP 다운로드", backup(db), f"family_backup_{today}.zip", "application/zip")
        st.download_button("내 기록 CSV 다운로드 (사진 제외)", mine.drop(columns="id").to_csv(index=False).encode("utf-8-sig"), "my_workouts.csv", "text/csv")
        url = st.text_input("배포한 Streamlit 앱 주소", value=str(setting("APP_URL")), placeholder="https://your-app.streamlit.app")
        if url:
            parsed = urlparse(url.strip())
            if parsed.scheme not in ("https","http") or not parsed.netloc or len(url)>1000:
                st.error("올바른 http(s) 주소를 입력해주세요.")
            else:
                buf=BytesIO()
                qrcode.make(url.strip()).save(buf,format="PNG")
                st.image(buf.getvalue(),width=230)
                st.download_button("접속 QR 다운로드",buf.getvalue(),"family_qr.png","image/png")
        st.caption("QR은 앱 주소만 담으며 인증사진이나 비밀번호를 담지 않습니다.")


if __name__ == "__main__":
    main()

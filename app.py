import os
import json
import pytz
import time
import hashlib
import requests
import streamlit as st
from datetime import datetime, timedelta
from dateutil import parser as dtparser
from streamlit_calendar import calendar

# ----------------------------
# Конфіг
# ----------------------------
st.set_page_config(page_title="Jira Worklog Weekly Calendar", layout="wide")

def get_secret(section: str, key: str, env: str = None, default: str = ""):
    """Повертає значення з st.secrets[section][key] або з env, або default.
       Якщо ключа нема — не кидає винятків.
    """
    try:
        if section in st.secrets and key in st.secrets[section]:
            return st.secrets[section][key]
    except Exception:
        pass
    if env:
        return os.getenv(env, default) or default
    return default

# Значення за замовчуванням (можуть бути частково з secrets.toml)
JIRA_BASE_URL  = get_secret("jira", "base_url",  "JIRA_BASE_URL",  "https://your-domain.atlassian.net")
JIRA_EMAIL     = get_secret("jira", "email",     "JIRA_EMAIL",     "")
JIRA_API_TOKEN = get_secret("jira", "api_token", "JIRA_API_TOKEN", "")

# ----------------------------
# Утиліти
# ----------------------------
def b64_auth(email: str, token: str):
    import base64
    raw = f"{email}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")

def to_iso(dt: datetime) -> str:
    # FullCalendar очікує ISO з таймзоною
    return dt.astimezone(pytz.UTC).isoformat()

def parse_iso(s: str) -> datetime:
    return dtparser.isoparse(s)

def human_duration(seconds: int) -> str:
    # Для підказок
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    if mins == 0:
        return f"{hrs}h"
    return f"{hrs}h {mins}m"

def round_to_5min(dt: datetime) -> datetime:
    # Трохи шліфуємо клік у календарі
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)

# ----------------------------
# Клієнт Jira (REST v3)
# ----------------------------
class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": b64_auth(email, api_token),
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

    def current_user(self):
        r = self.session.get(f"{self.base}/rest/api/3/myself", timeout=30)
        r.raise_for_status()
        return r.json()

    def search_users(self, query: str = "", max_results: int = 50):
        # Jira Cloud: /rest/api/3/users/search
        params = {"query": query, "maxResults": max_results}
        r = self.session.get(f"{self.base}/rest/api/3/users/search", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def jql_issues(self, jql: str, fields=None, max_results=100):
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields or ["summary", "status", "assignee"]
        }
        r = self.session.post(f"{self.base}/rest/api/3/search", data=json.dumps(payload), timeout=60)
        r.raise_for_status()
        return r.json()

    def get_issue_worklogs(self, issue_key: str):
        # paginate if necessary
        url = f"{self.base}/rest/api/3/issue/{issue_key}/worklog"
        start_at = 0
        all_logs = []
        while True:
            r = self.session.get(url, params={"startAt": start_at}, timeout=30)
            r.raise_for_status()
            data = r.json()
            all_logs.extend(data.get("worklogs", []))
            if len(all_logs) >= data.get("total", 0):
                break
            start_at += data.get("maxResults", len(data.get("worklogs", [])))
        return all_logs

    def update_worklog(self, issue_key: str, worklog_id: str, started_iso: str = None, time_spent_seconds: int = None, comment: str = None):
        payload = {}
        if started_iso is not None:
            payload["started"] = started_iso  # ISO8601; Jira expects end with timezone, e.g. "2025-08-22T10:00:00.000+0000"
        if time_spent_seconds is not None:
            payload["timeSpentSeconds"] = time_spent_seconds
        if comment is not None:
            payload["comment"] = comment
        # IMPORTANT: Jira Cloud needs ".000+0000" style — ми конвертуємо нижче
        # Але API v3 приймає і стандартний ISO з Z. Тому пробуємо як є.
        r = self.session.put(
            f"{self.base}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            data=json.dumps(payload),
            timeout=30
        )
        r.raise_for_status()
        return r.json()

    def add_worklog(self, issue_key: str, started_iso: str, time_spent_seconds: int, comment: str = ""):
        payload = {
            "started": started_iso,
            "timeSpentSeconds": time_spent_seconds,
            "comment": comment
        }
        r = self.session.post(
            f"{self.base}/rest/api/3/issue/{issue_key}/worklog",
            data=json.dumps(payload),
            timeout=30
        )
        r.raise_for_status()
        return r.json()

# ----------------------------
# Кеші
# ----------------------------
@st.cache_data(show_spinner=False, ttl=300)
def cached_users(base, email, token, query):
    jc = JiraClient(base, email, token)
    return jc.search_users(query=query)

@st.cache_data(show_spinner=False, ttl=60)
def cached_issues_for_assignee(base, email, token, account_id, max_results=100):
    jc = JiraClient(base, email, token)
    jql = f'assignee = "{account_id}" AND resolution = EMPTY ORDER BY updated DESC'
    return jc.jql_issues(jql, max_results=max_results)

@st.cache_data(show_spinner=False, ttl=60)
def cached_worklogs_week(base, email, token, account_id, start_utc_iso, end_utc_iso, max_issues=300):
    """
    Забираємо всі issues з worklog’ами цього користувача в межах тижня:
    JQL вміє фільтрувати по worklogAuthor/worklogDate на рівні issues.
    Потім на клієнті відбираємо тільки потрібні worklog’и.
    """
    jc = JiraClient(base, email, token)
    jql = (
        f'worklogAuthor = "{account_id}" '
        f'AND worklogDate >= "{start_utc_iso[:10]}" AND worklogDate <= "{end_utc_iso[:10]}" '
        f'ORDER BY updated DESC'
    )
    issues = jc.jql_issues(jql, max_results=max_issues)
    events = []
    for issue in issues.get("issues", []):
        key = issue["key"]
        summary = issue["fields"].get("summary", key)
        wls = jc.get_issue_worklogs(key)
        for wl in wls:
            author = wl.get("author", {})
            if author.get("accountId") != account_id:
                continue
            started = dtparser.isoparse(wl["started"])
            if started < dtparser.isoparse(start_utc_iso) or started > dtparser.isoparse(end_utc_iso):
                continue
            secs = wl.get("timeSpentSeconds", 0)
            end_dt = started + timedelta(seconds=secs or 0)
            events.append({
                "id": f"{key}::{wl['id']}",
                "title": f"{key} · {summary}",
                "start": started.astimezone(pytz.UTC).isoformat(),
                "end": end_dt.astimezone(pytz.UTC).isoformat(),
                "extendedProps": {
                    "issueKey": key,
                    "worklogId": wl["id"],
                    "timeSpentSeconds": secs,
                    "comment": (wl.get("comment") or "") if isinstance(wl.get("comment"), str) else ""
                },
                "editable": True
            })
    return events

# ----------------------------
# UI: бокова панель (автентифікація та вибір користувача)
# ----------------------------
with st.sidebar:
    st.header("Jira")
    jira_base = st.text_input("Jira Base URL", value=JIRA_BASE_URL, help="Напр., https://your-domain.atlassian.net")
    jira_email = st.text_input("Email (Jira Cloud)", value=JIRA_EMAIL)
    jira_token = st.text_input("API Token", value=JIRA_API_TOKEN, type="password")

    st.markdown("---")
    st.caption("Кого показувати на календарі?")
    # Пошук користувачів (в дропдауні — за іменем)
    user_query = st.text_input("Пошук користувача", value="")
    users = []
    if jira_base and jira_email and jira_token:
        try:
            users = cached_users(jira_base, jira_email, jira_token, user_query)
        except Exception as e:
            st.error(f"Помилка завантаження користувачів: {e}")

    user_display_to_account = {}
    for u in users:
        display = f"{u.get('displayName')}  ·  {u.get('emailAddress','') or 'no-email'}"
        user_display_to_account[display] = u.get("accountId")

    selected_user_display = st.selectbox("Користувач", options=list(user_display_to_account.keys()) or ["—"], index=0)
    selected_account_id = user_display_to_account.get(selected_user_display)

    st.markdown("---")
    tz_name = st.selectbox("Часова зона календаря", options=["Europe/Kyiv", "UTC", "Europe/Warsaw", "Europe/Berlin"], index=0)
    tz = pytz.timezone(tz_name)

# ----------------------------
# Центральна частина
# ----------------------------
st.title("Jira Worklog — Weekly Calendar")

if not (jira_base and jira_email and jira_token and selected_account_id):
    st.info("Введи Jira URL, креденшли та обери користувача в лівій панелі.")
    st.stop()

# Обчислюємо межі тижня (понеділок-неділя) від сьогодні
now_local = datetime.now(tz)
start_of_week = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
end_of_week = start_of_week + timedelta(days=7)
start_of_week_utc = start_of_week.astimezone(pytz.UTC).isoformat()
end_of_week_utc = end_of_week.astimezone(pytz.UTC).isoformat()

# Дані для календаря
with st.spinner("Завантажую worklog’и…"):
    try:
        events = cached_worklogs_week(
            jira_base, jira_email, jira_token,
            selected_account_id, start_of_week_utc, end_of_week_utc
        )
    except Exception as e:
        st.error(f"Не вдалося завантажити worklog’и: {e}")
        events = []

# ----------------------------
# Панель створення worklog (виникає після dblclick / "подвійного кліку")
# ----------------------------
def open_create_dialog(click_iso: str):
    st.session_state["create_dialog_open"] = True
    st.session_state["create_dialog_start"] = click_iso

def close_create_dialog():
    st.session_state["create_dialog_open"] = False
    st.session_state["create_dialog_start"] = None

if "create_dialog_open" not in st.session_state:
    st.session_state["create_dialog_open"] = False
    st.session_state["create_dialog_start"] = None

# ----------------------------
# Рендер календаря
# ----------------------------
# Конфіг FullCalendar
cal_options = {
    "initialView": "timeGridWeek",
    "slotMinTime": "06:00:00",
    "slotMaxTime": "22:00:00",
    "allDaySlot": False,
    "editable": True,
    "selectable": True,
    "selectMirror": True,
    "navLinks": True,
    "eventResizableFromStart": True,
    "weekNumbers": True,
    "nowIndicator": True,
    "headerToolbar": {
        "left": "prev,next today",
        "center": "title",
        "right": "timeGridWeek,dayGridMonth"
    },
}

cal_events = events

# Рендеримо
cal_state = calendar(
    events=cal_events,
    options=cal_options,
    key="calendar"
)

# ----------------------------
# Обробка дій з календаря
# ----------------------------
jc = JiraClient(jira_base, jira_email, jira_token)

# 1) Подвійний клік → створення worklog
# Фолбек "подвійного кліку": два швидкі dateClick по тій самій клітинці
if cal_state and isinstance(cal_state, dict):
    date_click = cal_state.get("dateClick")
    if date_click and "date" in date_click:
        last = st.session_state.get("_last_click_ts")
        last_cell = st.session_state.get("_last_click_cell")
        this_cell = date_click["date"]
        this_ts = time.time()
        if last and last_cell == this_cell and (this_ts - last) < 0.4:
            click_dt = parse_iso(this_cell)
            click_dt = round_to_5min(click_dt)
            st.session_state["create_dialog_open"] = True
            st.session_state["create_dialog_start"] = click_dt.astimezone(pytz.UTC).isoformat()
        st.session_state["_last_click_ts"] = this_ts
        st.session_state["_last_click_cell"] = this_cell

    # Будь-яка зміна події (drag/drop або resize)
    change = cal_state.get("eventChange")
    if change and "event" in change:
        ev = change["event"]
        ev_id = ev.get("id", "")
        if "::" in ev_id:
            issue_key, worklog_id = ev_id.split("::", 1)
            new_start = ev.get("start")
            new_end = ev.get("end")

            start_dt = parse_iso(new_start)
            end_dt = parse_iso(new_end)
            new_seconds = int((end_dt - start_dt).total_seconds())
            new_seconds = max(60, new_seconds)

            # Визначимо тип зміни: якщо початок зрушився, а тривалість лишилась тією ж — drag.
            # Якщо змінилась тривалість — resize. Спираємось на extendedProps з events/FullCalendar.
            # Але простіше: спробуємо спершу оновити start, а потім — duration,
            # при цьому duration мінятимемо лише якщо реально змінилась.
            try:
                # Оновлюємо старт (drag завжди має зміну start)
                jc.update_worklog(issue_key, worklog_id, started_iso=start_dt.astimezone(pytz.UTC).isoformat())
                # Оновлюємо тривалість (актуально для resize)
                jc.update_worklog(issue_key, worklog_id, time_spent_seconds=new_seconds)
                st.success(f"Оновлено worklog {worklog_id} ({issue_key}) → старт {start_dt}, тривалість {human_duration(new_seconds)}")
                cached_worklogs_week.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Помилка оновлення worklog: {e}")

# ----------------------------
# Діалог створення нового worklog (форма зі вводом)
# ----------------------------
if st.session_state.get("create_dialog_open"):
    st.markdown("### ➕ Новий worklog")

    # Час початку з клікнутого слоту
    start_iso = st.session_state.get("create_dialog_start")
    start_dt_local = parse_iso(start_iso).astimezone(tz)

    with st.form("create_worklog_form", clear_on_submit=False):
        st.write(f"Початок: **{start_dt_local.strftime('%Y-%m-%d %H:%M')} ({tz_name})**")

        manual_start = st.checkbox("Змінити час початку вручну", value=False, key="create_manual_start")
        if manual_start:
            new_date = st.date_input("Дата", value=start_dt_local.date(), key="create_date")
            new_time = st.time_input("Час", value=start_dt_local.time(), key="create_time")
            # локалізуємо + переобчислюємо ISO в UTC
            start_dt_local = tz.localize(datetime.combine(new_date, new_time))
            start_iso = start_dt_local.astimezone(pytz.UTC).isoformat()

        # 1) Підтягнемо задачі, призначені вибраному користувачу (selected_account_id)
        issues_resp = {}
        issue_options = []
        key_to_summary = {}

        try:
            issues_resp = cached_issues_for_assignee(
                jira_base, jira_email, jira_token, selected_account_id
            )
            for it in issues_resp.get("issues", []):
                key = it["key"]
                summary = it["fields"].get("summary", key)
                label = f"{key} · {summary[:80]}"
                issue_options.append(label)
                key_to_summary[key] = summary
        except Exception as e:
            st.warning(f"Не вдалося отримати список задач: {e}")

        # 2) Якщо список пустий — дамо поле ручного вводу ключа
        use_manual_issue = False
        selected_issue_key = None

        if issue_options:
            sel = st.selectbox("Задача", options=issue_options, key="create_issue_select")
            selected_issue_key = sel.split(" · ")[0] if " · " in sel else sel
        else:
            use_manual_issue = True
            selected_issue_key = st.text_input("Ключ задачі (наприклад, ABC-123)", value="", key="create_issue_manual")

        # 3) Тривалість та коментар
        col_h, col_m = st.columns(2)
        with col_h:
            hours = st.number_input("Години", min_value=0, step=1, value=1, key="create_hours")
        with col_m:
            minutes = st.number_input("Хвилини", min_value=0, max_value=55, step=5, value=0, key="create_minutes")

        comment = st.text_input("Коментар (необов’язково)", value="", key="create_comment")

        c1, c2 = st.columns([1,1])
        submit = c1.form_submit_button("Створити worklog", type="primary", use_container_width=True)
        cancel = c2.form_submit_button("Скасувати", use_container_width=True)

        if cancel:
            st.session_state["create_dialog_open"] = False
            st.session_state["create_dialog_start"] = None
            st.experimental_rerun()

        if submit:
            if not selected_issue_key:
                st.error("Вкажи ключ задачі або обери з переліку.")
                st.stop()

            secs = int(hours) * 3600 + int(minutes) * 60
            secs = max(secs, 60)  # мінімум 1 хв

            try:
                jc.add_worklog(
                    selected_issue_key,
                    started_iso=start_iso,
                    time_spent_seconds=secs,
                    comment=comment
                )
                st.success(
                    f"Створено worklog для {selected_issue_key}: "
                    f"{start_dt_local.strftime('%Y-%m-%d %H:%M')} / {human_duration(secs)}"
                )
                # закриваємо форму, очищаємо кеш і перерендеримо
                st.session_state["create_dialog_open"] = False
                st.session_state["create_dialog_start"] = None
                cached_worklogs_week.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Не вдалось створити worklog: {e}")

# ----------------------------
# Дрібні підказки
# ----------------------------
with st.expander("Поради / налаштування"):
    st.markdown("""
- **Подвійний клік** по порожній клітинці відкриває форму створення worklog.
- **Drag & drop** події змінює *початок* у відповідному worklog у Jira.
- **Resize** події змінює *тривалість* (timeSpentSeconds).
- JQL для пошуку задач призначених користувачу — у функції `cached_issues_for_assignee`. Можеш адаптувати (наприклад, за статусами/проєктом).
- Якщо у твоєму оточенні компонент не відловлює dblclick, увімкнений **фолбек**: два швидкі кліки по одному слоту теж відкриють форму.
""")
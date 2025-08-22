import os
import json
import pytz
import time
import requests
import streamlit as st
from datetime import datetime, timedelta
from dateutil import parser as dtparser
from streamlit_calendar import calendar

# ----------------------------
# Конфіг
# ----------------------------
st.set_page_config(page_title="Jira Worklog Weekly Calendar", layout="wide")

# Стан чернетки редагування/створення
if "draft" not in st.session_state:
    # структура: {id, title, start, end, mode, issueKey, worklogId, comment}
    st.session_state["draft"] = None

# ----------------------------
# Secrets / Env
# ----------------------------
def get_secret(section: str, key: str, env: str = None, default: str = ""):
    """Читає st.secrets[section][key], або env, або повертає default."""
    try:
        if section in st.secrets and key in st.secrets[section]:
            return st.secrets[section][key]
    except Exception:
        pass
    if env:
        return os.getenv(env, default) or default
    return default

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

def parse_iso(s: str) -> datetime:
    return dtparser.isoparse(s)

def human_duration(seconds: int) -> str:
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    if mins == 0:
        return f"{hrs}h"
    return f"{hrs}h {mins}m"

def round_to_5min(dt: datetime) -> datetime:
    minute = (dt.minute // 5) * 5
    return dt.replace(minute=minute, second=0, microsecond=0)

def jira_datetime_from_iso(iso_str: str) -> str:
    """Конвертує будь-який ISO у формат Jira: 2025-08-22T10:00:00.000+0000"""
    dt = parse_iso(iso_str).astimezone(pytz.UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")

def adf_comment(text: str):
    """Перетворює plain text у Atlassian Document Format (ADF)."""
    if not text:
        return None
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }

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
        """
        Використовуємо user picker (менше проблем із правами).
        Повертаємо спрощений список користувачів.
        """
        if not query or len(query.strip()) < 2:
            return []
        r = self.session.get(
            f"{self.base}/rest/api/3/user/picker",
            params={"query": query, "maxResults": max_results},
            timeout=30
        )
        r.raise_for_status()
        data = r.json()
        users = []
        for u in data.get("users", []):
            users.append({
                "accountId": u.get("accountId"),
                "displayName": u.get("displayName"),
                "emailAddress": u.get("emailAddress", "")
            })
        return users

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

    def update_worklog(self, issue_key: str, worklog_id: str,
                       started_iso: str = None, time_spent_seconds: int = None, comment: str = None):
        payload = {}
        if started_iso is not None:
            payload["started"] = jira_datetime_from_iso(started_iso)
        if time_spent_seconds is not None:
            payload["timeSpentSeconds"] = int(time_spent_seconds)
        if comment is not None:
            cm = adf_comment(comment)
            if cm is not None:
                payload["comment"] = cm

        try:
            r = self.session.put(
                f"{self.base}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
                json=payload,  # ВАЖЛИВО: json=, не data=
                params={"notifyUsers": "false"},
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # Покажи діагностику з тіла відповіді
            detail = ""
            try:
                detail = f" | details: {r.text[:500]}"
            except Exception:
                pass
            raise requests.HTTPError(f"{e} {detail}") from e

    def add_worklog(self, issue_key: str, started_iso: str, time_spent_seconds: int, comment: str = ""):
        payload = {
            "started": jira_datetime_from_iso(started_iso),
            "timeSpentSeconds": int(time_spent_seconds),
        }
        cm = adf_comment(comment)
        if cm is not None:
            payload["comment"] = cm

        try:
            r = self.session.post(
                f"{self.base}/rest/api/3/issue/{issue_key}/worklog",
                json=payload,  # ВАЖЛИВО: json=, не data=
                params={"notifyUsers": "false"},
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            detail = ""
            try:
                detail = f" | details: {r.text[:500]}"
            except Exception:
                pass
            raise requests.HTTPError(f"{e} {detail}") from e

# ----------------------------
# Кеші
# ----------------------------
@st.cache_data(show_spinner=False, ttl=300)
def cached_users(base, email, token, query):
    jc = JiraClient(base, email, token)
    try:
        return jc.search_users(query=query)
    except requests.HTTPError as e:
        # Фолбек: якщо немає прав на пошук — повертаємо себе
        if e.response is not None and e.response.status_code == 403:
            me = jc.current_user()
            return [{
                "accountId": me.get("accountId"),
                "displayName": me.get("displayName"),
                "emailAddress": me.get("emailAddress", "")
            }]
        raise

@st.cache_data(show_spinner=False, ttl=60)
def cached_issues_for_assignee(base, email, token, account_id, max_results=100):
    jc = JiraClient(base, email, token)
    jql = f'assignee = "{account_id}" AND resolution = EMPTY ORDER BY updated DESC'
    return jc.jql_issues(jql, max_results=max_results)

@st.cache_data(show_spinner=False, ttl=60)
def cached_worklogs_week(base, email, token, account_id, start_utc_iso, end_utc_iso, max_issues=300):
    """
    JQL фільтрує issues за worklogAuthor/worklogDate, а потім дістаємо worklog’и.
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
                "editable": False  # існуючі події не редагуємо напряму
            })
    return events

# ----------------------------
# UI: бокова панель
# ----------------------------
selected_account_id = None  # ініціалізація, щоб не було NameError

with st.sidebar:
    st.header("Jira")

    jira_base = st.text_input(
        "Jira Base URL",
        value=JIRA_BASE_URL,
        help="Напр., https://your-domain.atlassian.net",
        key="jira_base_url",
    )
    jira_email = st.text_input(
        "Email (Jira Cloud)",
        value=JIRA_EMAIL,
        key="jira_email",
    )
    # Якщо токен є в secrets — не показуємо його в полі вводу
    token_in_secrets = bool(get_secret("jira", "api_token", default=""))
    jira_token_input = st.text_input(
        "API Token",
        value="" if token_in_secrets else JIRA_API_TOKEN,
        type="password",
        help="Рекомендовано зберігати у .streamlit/secrets.toml або у Streamlit Cloud Secrets.",
        key="jira_api_token",
    )
    jira_token = jira_token_input or (get_secret("jira", "api_token", default="") if token_in_secrets else "")

    st.markdown("---")
    st.caption("Кого показувати на календарі?")

    # Форма пошуку користувача (Enter або кнопка)
    with st.form("user_search_form", clear_on_submit=False):
        query_input = st.text_input(
            "Пошук користувача (мін. 2 символи)",
            value=st.session_state.get("user_search_query", ""),
            key="user_search_input",
        )
        submitted = st.form_submit_button("Знайти")
    if submitted:
        st.session_state["user_search_query"] = query_input.strip()

    effective_query = st.session_state.get("user_search_query", query_input.strip())

    user_display_to_account = {}
    options = []

    if jira_base and jira_email and jira_token:
        try:
            if len(effective_query) >= 2:
                users = cached_users(jira_base, jira_email, jira_token, effective_query)
            else:
                users = []
        except Exception as e:
            st.error(f"Помилка завантаження користувачів: {e}")
            users = []

        if users:
            for u in users:
                display = f"{u.get('displayName')}  ·  {u.get('emailAddress','') or '—'}"
                user_display_to_account[display] = u.get("accountId")
                options.append(display)
        else:
            # фолбек — поточний користувач
            try:
                me = JiraClient(jira_base, jira_email, jira_token).current_user()
                me_display = f"{me.get('displayName')}  ·  {me.get('emailAddress','') or '—'} (myself)"
                user_display_to_account[me_display] = me.get("accountId")
                options = [me_display]
            except Exception as e:
                st.error(f"Не вдалося отримати поточного користувача: {e}")
                options = ["—"]
    else:
        options = ["—"]

    def _on_user_change():
        cached_worklogs_week.clear()
        st.session_state["draft"] = None
        st.rerun()

    selected_user_display = st.selectbox(
        "Користувач",
        options=options,
        index=0,
        key="user_select",
        on_change=_on_user_change
    )
    selected_account_id = user_display_to_account.get(selected_user_display, None)

    st.markdown("---")
    tz_name = st.selectbox(
        "Часова зона календаря",
        options=["Europe/Kyiv", "UTC", "Europe/Warsaw", "Europe/Berlin"],
        index=0,
        key="tz_select"
    )
    tz = pytz.timezone(tz_name)

# ----------------------------
# Центральна частина
# ----------------------------
st.title("Jira Worklog — Weekly Calendar")

# Межі тижня (понеділок-неділя) в обраній TZ
now_local = datetime.now(tz)
start_of_week = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
end_of_week = start_of_week + timedelta(days=7)
start_of_week_utc = start_of_week.astimezone(pytz.UTC).isoformat()
end_of_week_utc = end_of_week.astimezone(pytz.UTC).isoformat()

# Завантажуємо події, якщо є креденшли і вибраний користувач
events = []
if jira_base and jira_email and jira_token and selected_account_id:
    with st.spinner("Завантажую worklog’и…"):
        try:
            events = cached_worklogs_week(
                jira_base, jira_email, jira_token,
                selected_account_id, start_of_week_utc, end_of_week_utc
            )
        except Exception as e:
            st.error(f"Не вдалося завантажити worklog’и: {e}")
            events = []

# Якщо є чернетка — додаємо її у набір подій як єдину редаговану
if st.session_state.get("draft"):
    d = st.session_state["draft"]
    events = events + [{
        "id": d["id"],
        "title": d["title"],
        "start": d["start"],
        "end": d["end"],
        "editable": True,
        "backgroundColor": "#2684FF",
        "borderColor": "#2684FF",
        "textColor": "white",
    }]

# ----------------------------
# Рендер календаря
# ----------------------------
cal_options = {
    "initialView": "timeGridWeek",
    "slotMinTime": "06:00:00",
    "slotMaxTime": "22:00:00",
    "allDaySlot": False,
    "editable": True,                 # але реально editable лише draft-івент
    "eventResizableFromStart": True,  # дозволяє тягнути лівий край
    "selectable": True,
    "selectMirror": True,
    "navLinks": True,
    "weekNumbers": True,
    "nowIndicator": True,
    "headerToolbar": {
        "left": "prev,next today",
        "center": "title",
        "right": "timeGridWeek,dayGridMonth"
    },
}

cal_state = calendar(
    events=events,
    options=cal_options,
    key="calendar"
)

# ----------------------------
# Обробники подій календаря (лише робота з чернеткою)
# ----------------------------
def _mk_draft_from_click(start_iso: str):
    start_dt = parse_iso(start_iso)
    end_dt = start_dt + timedelta(hours=1)
    st.session_state["draft"] = {
        "id": "__DRAFT__",
        "title": "Новий worklog (чернетка)",
        "start": start_dt.astimezone(pytz.UTC).isoformat(),
        "end": end_dt.astimezone(pytz.UTC).isoformat(),
        "mode": "new",
        "issueKey": None,
        "worklogId": None,
        "comment": "",
    }

def _mk_draft_from_existing(ev):
    ev_id = ev.get("id", "")
    if "::" not in ev_id:
        return
    issue_key, worklog_id = ev_id.split("::", 1)
    st.session_state["draft"] = {
        "id": "__DRAFT_EDIT__",
        "title": ev.get("title", f"{issue_key} (ред.)"),
        "start": ev.get("start"),
        "end": ev.get("end"),
        "mode": "edit",
        "issueKey": issue_key,
        "worklogId": worklog_id,
        "comment": ev.get("extendedProps", {}).get("comment", ""),
    }

def _update_draft_time(new_start_iso: str, new_end_iso: str):
    if not st.session_state.get("draft"):
        return
    st.session_state["draft"]["start"] = parse_iso(new_start_iso).astimezone(pytz.UTC).isoformat()
    st.session_state["draft"]["end"]   = parse_iso(new_end_iso).astimezone(pytz.UTC).isoformat()

if cal_state and isinstance(cal_state, dict):
    # 1) Клік по порожньому місцю → створити НОВУ чернетку
    date_click = cal_state.get("dateClick")
    if date_click and "date" in date_click:
        _mk_draft_from_click(date_click["date"])
        st.rerun()

    # 2) Клік по існуючому worklog → редагування як чернетка
    ev_click = cal_state.get("eventClick")
    if ev_click and "event" in ev_click:
        ev = ev_click["event"]
        if not str(ev.get("id","")).startswith("__DRAFT__"):
            _mk_draft_from_existing(ev)
            st.rerun()

    # 3) Drag/Resize чернетки → лише змінюємо локальний час
    change = cal_state.get("eventChange")
    if change and "event" in change:
        ev = change["event"]
        if str(ev.get("id","")).startswith("__DRAFT__"):
            _update_draft_time(ev.get("start"), ev.get("end"))
            st.rerun()

# ----------------------------
# Редактор чернетки (Save/Cancel) — тут викликаємо Jira
# ----------------------------
jc = JiraClient(jira_base, jira_email, jira_token) if (jira_base and jira_email and jira_token) else None
draft = st.session_state.get("draft")

if draft:
    st.markdown("### ✏️ Редактор worklog (чернетка)")
    # Вибрана TZ із сайдбару:
    tz_name = st.session_state.get("tz_select", "Europe/Kyiv")
    tz = pytz.timezone(tz_name)

    start_dt_local = parse_iso(draft["start"]).astimezone(tz)
    end_dt_local   = parse_iso(draft["end"]).astimezone(tz)
    dur_secs = int((end_dt_local - start_dt_local).total_seconds())
    if dur_secs < 60:
        dur_secs = 60

    with st.form("draft_editor", clear_on_submit=False):
        st.write(f"Початок: **{start_dt_local.strftime('%Y-%m-%d %H:%M')} ({tz_name})**")
        st.write(f"Кінець: **{end_dt_local.strftime('%Y-%m-%d %H:%M')} ({tz_name})**")
        st.caption("Підказка: змінюй тривалість/час у календарі перетягуванням.")

        selected_issue_key = draft.get("issueKey")

        if draft["mode"] == "new":
            # список задач для вибраного користувача
            issue_options = []
            try:
                if jira_base and jira_email and jira_token and selected_account_id:
                    issues_resp = cached_issues_for_assignee(jira_base, jira_email, jira_token, selected_account_id)
                    for it in issues_resp.get("issues", []):
                        key = it["key"]
                        label = f"{key} · {it['fields'].get('summary', key)[:80]}"
                        issue_options.append(label)
            except Exception as e:
                st.warning(f"Не вдалося отримати список задач: {e}")

            if issue_options:
                sel_label = st.selectbox("Задача", options=issue_options, key="draft_issue_select")
                selected_issue_key = sel_label.split(" · ")[0]
            else:
                selected_issue_key = st.text_input("Ключ задачі (напр., ABC-123)", value=selected_issue_key or "", key="draft_issue_manual")
        else:
            st.text_input("Задача", value=draft["issueKey"], disabled=True, key="draft_issue_readonly")

        comment = st.text_input("Коментар (необов’язково)", value=draft.get("comment",""), key="draft_comment")

        c1, c2 = st.columns(2)
        save   = c1.form_submit_button("Зберегти", type="primary", use_container_width=True)
        cancel = c2.form_submit_button("Скасувати", use_container_width=True)

        if cancel:
            st.session_state["draft"] = None
            st.rerun()

        if save:
            if not jc:
                st.error("Спочатку заповни Jira URL, email і token.")
                st.stop()

            try:
                if draft["mode"] == "new":
                    if not selected_issue_key:
                        st.error("Оберіть або вкажіть ключ задачі.")
                        st.stop()
                    jc.add_worklog(
                        selected_issue_key,
                        started_iso=parse_iso(draft["start"]).astimezone(pytz.UTC).isoformat(),
                        time_spent_seconds=dur_secs,
                        comment=comment or ""
                    )
                else:
                    jc.update_worklog(
                        draft["issueKey"],
                        draft["worklogId"],
                        started_iso=parse_iso(draft["start"]).astimezone(pytz.UTC).isoformat(),
                        time_spent_seconds=dur_secs,
                        comment=comment or None
                    )

                st.success("Збережено ✅")
                st.session_state["draft"] = None
                cached_worklogs_week.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Не вдалося зберегти: {e}")

# ----------------------------
# Поради
# ----------------------------
with st.expander("Поради / налаштування"):
    st.markdown("""
- **Одинарний клік** по порожньому місцю створює **чернетку** події.
- **Клік по існуючій події** відкриває її як **чернетку** для редагування.
- **Перетягування** події/країв змінює **лише чернетку локально**.
- Запис у Jira виконується **тільки** при натисканні **Зберегти**.
- Пошук користувачів використовує *user picker*; якщо недоступний — підставляється *myself*.
""")
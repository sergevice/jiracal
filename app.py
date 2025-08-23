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

    def list_fields(self):
        """Повертає список усіх полів (для пошуку Epic Link)."""
        r = self.session.get(f"{self.base}/rest/api/3/field", timeout=30)
        r.raise_for_status()
        return r.json()

    def epic_link_jql_name(self):
        """
        Знаходимо, як звертатися до Epic Link у JQL.
        Повертає 'Epic Link' або 'cf[12345]' залежно від конфігурації.
        """
        try:
            fields = self.list_fields()
            for f in fields:
                # Cloud: epic-link має schema.custom == 'com.pyxis.greenhopper.jira:gh-epic-link'
                schema = f.get("schema", {})
                if schema.get("custom") == "com.pyxis.greenhopper.jira:gh-epic-link":
                    # У JQL краще використовувати cf[ID], щоб оминути локалізацію
                    fid = f.get("id")
                    if fid and fid.startswith("customfield_"):
                        num = fid.split("_", 1)[1]
                        return f"cf[{num}]"
                    # fallback: ім’я
                    return f.get("name", "Epic Link")
            # якщо не знайшли — спробуємо стандартну назву
            return "Epic Link"
        except Exception:
            return "Epic Link"

# ----------------------------
# Кеші
# ----------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def cached_epic_link_jql_name(base, email, token):
    jc = JiraClient(base, email, token)
    return jc.epic_link_jql_name()

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

@st.cache_data(show_spinner=False, ttl=120)
def cached_epics(base, email, token, query_text="", max_results=200):
    """
    Епіки (не залежить від асайнї). Якщо query_text >= 2 символи — фільтр по summary.
    """
    jc = JiraClient(base, email, token)
    if query_text and len(query_text.strip()) >= 2:
        jql = f'issuetype = Epic AND summary ~ "{query_text.strip()}*" ORDER BY updated DESC'
    else:
        jql = 'issuetype = Epic ORDER BY updated DESC'
    return jc.jql_issues(jql, fields=["summary"], max_results=max_results)

@st.cache_data(show_spinner=False, ttl=60)
def cached_issues_for_epic_and_assignee(base, email, token, epic_key, account_id, max_results=200):
    """
    Діти конкретного епіку, призначені на account_id, нерозв’язані.
    Підтримує 3 варіанти JQL (в залежності від типу проєкту/конфігів):
      1) childIssuesOf("<EPIC>")
      2) "<Epic Link поле>" = <EPIC>  (через cf[ID] або 'Epic Link')
      3) parentEpic = <EPIC>          (team-managed)
    Повертає результат першого вдалого запиту.
    """
    jc = JiraClient(base, email, token)

    queries = []

    # 1) childIssuesOf
    queries.append(
        (
            f'issuekey in childIssuesOf("{epic_key}") AND assignee = "{account_id}" '
            f'AND resolution = EMPTY ORDER BY updated DESC'
        )
    )

    # 2) Epic Link через cf[id] або ім'я
    epic_field = cached_epic_link_jql_name(base, email, token)  # напр., cf[10014] або 'Epic Link'
    queries.append(
        (
            f'"{epic_field}" = {epic_key} AND assignee = "{account_id}" '
            f'AND resolution = EMPTY ORDER BY updated DESC'
        )
    )

    # 3) team-managed: parentEpic
    queries.append(
        (
            f'parentEpic = {epic_key} AND assignee = "{account_id}" '
            f'AND resolution = EMPTY ORDER BY updated DESC'
        )
    )

    last_err = None
    for jql in queries:
        try:
            return jc.jql_issues(jql, fields=["summary"], max_results=max_results)
        except requests.HTTPError as e:
            # 400/404 — пробуємо наступний варіант
            last_err = e
            continue

    # Якщо все впало — підіймаємо найостаннішу помилку з деталями
    if last_err:
        raise last_err
    # крайній захист
    return {"issues": []}

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

    # ----- видимий діапазон календаря (за замовчуванням — поточний тиждень) -----
    if "visible_start" not in st.session_state or "visible_end" not in st.session_state:
        now_local = datetime.now(tz)
        start_of_week = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_week = start_of_week + timedelta(days=7)
        st.session_state["visible_start"] = start_of_week.astimezone(pytz.UTC).isoformat()
        st.session_state["visible_end"] = end_of_week.astimezone(pytz.UTC).isoformat()

# ----------------------------
# Центральна частина
# ----------------------------
st.title("Jira Worklog — Weekly Calendar")

# Використовуємо поточний видимий діапазон (який зберігаємо у session_state)
visible_start_iso = st.session_state["visible_start"]   # UTC ISO
visible_end_iso   = st.session_state["visible_end"]     # UTC ISO

# ключ, що міняється на кожний видимий тиждень → форсує перемонтування календаря
week_key = parse_iso(visible_start_iso).strftime("%Y-%W")  # рік-номер_тижня
cal_key = f"calendar_{week_key}"

events = []
if jira_base and jira_email and jira_token and selected_account_id:
    with st.spinner("Завантажую worklog’и…"):
        try:
            events = cached_worklogs_week(
                jira_base, jira_email, jira_token,
                selected_account_id, visible_start_iso, visible_end_iso
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
    "initialDate": st.session_state["visible_start"],
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
        "left": "",                # ховаємо prev/next, щоб користуватись власними кнопками
        "center": "title",
        "right": "timeGridWeek,dayGridMonth"
    },
}

# ---------- Custom toolbar: навігація між тижнями ----------
nav_col1, nav_col2, nav_col3, nav_col4 = st.columns([1,1,1,3])

def _shift_visible(days: int):
    start_dt = parse_iso(st.session_state["visible_start"])
    end_dt   = parse_iso(st.session_state["visible_end"])
    st.session_state["visible_start"] = (start_dt + timedelta(days=days)).astimezone(pytz.UTC).isoformat()
    st.session_state["visible_end"]   = (end_dt   + timedelta(days=days)).astimezone(pytz.UTC).isoformat()
    cached_worklogs_week.clear()
    st.rerun()

with nav_col1:
    if st.button("⟵ Prev", use_container_width=True, key="nav_prev"):
        _shift_visible(-7)

with nav_col2:
    if st.button("Today", use_container_width=True, key="nav_today"):
        now_local = datetime.now(tz)
        start_of_week = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_week = start_of_week + timedelta(days=7)
        st.session_state["visible_start"] = start_of_week.astimezone(pytz.UTC).isoformat()
        st.session_state["visible_end"]   = end_of_week.astimezone(pytz.UTC).isoformat()
        cached_worklogs_week.clear()
        st.rerun()

with nav_col3:
    if st.button("Next ⟶", use_container_width=True, key="nav_next"):
        _shift_visible(+7)

# ---- date_input з on_change без ручного rerun ----
def _jump_to_selected_date():
    jump_date = st.session_state.get("nav_jump_date")
    if not jump_date:
        return
    # початок тижня у вибраній TZ
    jdt_local = datetime.combine(jump_date, datetime.min.time()).replace(tzinfo=tz)
    start_of_week = (jdt_local - timedelta(days=jdt_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=7)
    st.session_state["visible_start"] = start_of_week.astimezone(pytz.UTC).isoformat()
    st.session_state["visible_end"]   = end_of_week.astimezone(pytz.UTC).isoformat()
    cached_worklogs_week.clear()

with nav_col4:
    st.date_input(
        "Перейти до дати",
        value=parse_iso(st.session_state["visible_start"]).date(),
        key="nav_jump_date",
        on_change=_jump_to_selected_date,
    )
# -----------------------------------------------------------

cal_state = calendar(
    events=events,
    options=cal_options,
    key=cal_key
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

def _debounce(event_key: str, payload: str) -> bool:
    """
    Повертає True, якщо payload новий; False — якщо такий самий уже обробляли.
    Використовуємо для уникнення нескінченних перерендерів.
    """
    last = st.session_state.get(event_key)
    if last == payload:
        return False
    st.session_state[event_key] = payload
    return True

if cal_state and isinstance(cal_state, dict):
    # 1) Клік по порожньому місцю → створити чернетку (обробляємо лише новий клік)
    date_click = cal_state.get("dateClick")
    if date_click and "date" in date_click:
        payload = date_click.get("date")
        if _debounce("_deb_dateClick", payload):
            _mk_draft_from_click(payload)

    # 2) Клік по існуючому worklog → редагування як чернетка (також з дебаунсом)
    ev_click = cal_state.get("eventClick")
    if ev_click and "event" in ev_click:
        payload = json.dumps(ev_click.get("event", {}), sort_keys=True)
        if _debounce("_deb_eventClick", payload):
            ev = ev_click["event"]
            if not str(ev.get("id","")).startswith("__DRAFT__"):
                _mk_draft_from_existing(ev)

    # 3) Drag/Resize чернетки → лише змінюємо локальний час (дебаунс)
    change = cal_state.get("eventChange")
    if change and "event" in change:
        payload = json.dumps(change.get("event", {}), sort_keys=True)
        if _debounce("_deb_eventChange", payload):
            ev = change["event"]
            if str(ev.get("id","")).startswith("__DRAFT__"):
                _update_draft_time(ev.get("start"), ev.get("end"))

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

    # Крок 1: вибір епіка (лише для створення)
    if draft["mode"] == "new":
        st.subheader("Крок 1: Обери епік")

        epic_query = st.text_input(
            "Пошук епіка (мін. 2 символи)",
            value=st.session_state.get("draft_epic_query", ""),
            key="draft_epic_query",
            help="Введи частину назви епіка, або лиши порожнім, щоб побачити свіжі епіки."
        )

        epic_options, epic_key_by_label = [], {}
        if jira_base and jira_email and jira_token:
            try:
                epics_resp = cached_epics(jira_base, jira_email, jira_token, epic_query)
                for it in epics_resp.get("issues", []):
                    ekey = it["key"]
                    label = f"{ekey} · {it['fields'].get('summary', ekey)[:90]}"
                    epic_options.append(label)
                    epic_key_by_label[label] = ekey
            except Exception as e:
                st.warning(f"Не вдалося завантажити епіки: {e}")
        else:
            st.info("Вкажи Jira URL/Email/Token у лівій панелі.")

        prev_label = st.session_state.get("draft_epic_label")
        index = epic_options.index(prev_label) if prev_label in epic_options else (0 if epic_options else 0)
        sel_label = st.selectbox(
            "Епік",
            options=epic_options or ["— немає збігів —"],
            index=index,
            key="draft_epic_select",
        )

        if epic_options:
            st.session_state["draft_epic_key"] = epic_key_by_label.get(sel_label)
            st.session_state["draft_epic_label"] = sel_label
        else:
            st.session_state["draft_epic_key"] = None
            st.session_state["draft_epic_label"] = None

        st.markdown("---")

    # Крок 2: форма з вибором задачі та збереженням
    with st.form("draft_editor", clear_on_submit=False):
        st.write(f"Початок: **{start_dt_local.strftime('%Y-%m-%d %H:%M')} ({tz_name})**")
        st.write(f"Кінець: **{end_dt_local.strftime('%Y-%m-%d %H:%M')} ({tz_name})**")
        st.caption("Підказка: змінюй тривалість/час у календарі перетягуванням.")

        selected_issue_key = draft.get("issueKey")

        if draft["mode"] == "new":
            issue_options, key_by_label = [], {}
            sel_epic_key = st.session_state.get("draft_epic_key")

            if sel_epic_key and jira_base and jira_email and jira_token and selected_account_id:
                try:
                    resp = cached_issues_for_epic_and_assignee(
                        jira_base, jira_email, jira_token, sel_epic_key, selected_account_id
                    )
                    for it in resp.get("issues", []):
                        ikey = it["key"]
                        label = f"{ikey} · {it['fields'].get('summary', ikey)[:90]}"
                        issue_options.append(label)
                        key_by_label[label] = ikey
                except Exception as e:
                    st.warning(f"Не вдалося отримати задачі для епіка {sel_epic_key}: {e}")

            if issue_options:
                sel_issue_label = st.selectbox(
                    "Задача в обраному епіку (призначена на користувача)",
                    options=issue_options,
                    key="draft_issue_select"
                )
                selected_issue_key = key_by_label.get(sel_issue_label)
            else:
                selected_issue_key = st.text_input(
                    "Ключ задачі (напр., ABC-123)",
                    value=selected_issue_key or "",
                    key="draft_issue_manual",
                    help="Немає задач у вибраному епіку або епік не обрано? Вкажи ключ вручну."
                )
        else:
            st.text_input("Задача", value=draft["issueKey"], disabled=True, key="draft_issue_readonly")

        # Поле коментаря (було відсутнє у твоїй вставці)
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
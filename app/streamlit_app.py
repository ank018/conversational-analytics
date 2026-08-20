"""
Conversational analytics - demo interface.

Ask a question in English, get an answer from a 99,441-order Brazilian
e-commerce warehouse.

One decision shapes this interface: **the SQL is always visible.** Not behind
a toggle, not in a debug panel. Roughly one answer in eight from this system
is silently wrong - it runs cleanly and returns a plausible number that
happens to be incorrect. Reading the query is the only defence a user has.

The system is also told it may refuse, and does: asked for profit margin on a
warehouse with no cost data, it declines rather than inventing a figure.

Follow-up questions work by resending the conversation. That behaviour is not
evaluated; the accuracy figures in the README are for single questions.

    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import html
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import llm          # noqa: E402
import runner       # noqa: E402
import sandbox      # noqa: E402
import schema       # noqa: E402

MODEL = "deepseek/deepseek-v4-pro-0813"
PROVIDER = "Fireworks"
GLOSSARY_PATH = ROOT / "docs" / "glossary.md"

# The demo runs on someone's API credit. Without a ceiling, one visitor with a
# loop costs more than the whole project did to build.
MAX_QUESTIONS = 15
HISTORY_TURNS = 4

EXAMPLES = [
    "What was total revenue?",
    "Which product category earned the most?",
    "How many customers ordered more than once?",
    "What is our profit margin?",
]

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root{
  --ink:#16202b; --paper:#f7f8fa; --card:#ffffff; --rule:#dde2e9;
  --steel:#5f6f83; --mark:#2f4f7a; --warn:#8a5c07; --warn-bg:#fdf6e8;
}

html, body, .stApp, p, div, span, label, button, input, textarea{
  font-family:'IBM Plex Sans',system-ui,sans-serif;
}
code, pre, [data-testid="stCode"] *{
  font-family:'IBM Plex Mono',ui-monospace,monospace;
}
.stApp{ font-size:16px; line-height:1.6; color:var(--ink); }

/* One centred column with room to breathe. */
.block-container{
  max-width:760px !important;
  padding:4rem 1.5rem 6rem !important;
}

h1{
  font-size:34px !important; font-weight:600 !important;
  letter-spacing:-.02em; margin:0 0 .5rem !important;
}
.lede{ color:var(--steel); font-size:17px; margin:0 0 2.6rem; }

/* The question, as a heading for its own answer. */
.q{
  font-size:19px; font-weight:600; color:var(--ink);
  margin:0 0 1rem; line-height:1.45;
}

.note{
  background:var(--card); border:1px solid var(--rule); border-radius:8px;
  padding:1.2rem 1.4rem; font-size:16px; line-height:1.6;
}
.note.warn{ background:var(--warn-bg); border-color:#eddcb0; color:var(--warn); }
.note b{ display:block; margin-bottom:.35rem; }
.note small{ display:block; color:var(--steel); font-size:14px;
  margin-top:.8rem; line-height:1.55; }

.meta{ font-family:'IBM Plex Mono',monospace; font-size:13px;
  color:var(--steel); margin-top:.8rem; }

/* Streamlit surfaces, softened. */
[data-testid="stCode"]{ border:1px solid var(--rule); border-radius:8px; }
[data-testid="stCode"] code{ font-size:14px !important; line-height:1.7 !important; }
[data-testid="stExpander"]{
  border:1px solid var(--rule) !important; border-radius:8px !important;
  background:var(--card); margin-top:1rem;
}
[data-testid="stExpander"] summary{ padding:.7rem 1rem !important; }
[data-testid="stExpander"] summary p{ font-size:15px !important; color:var(--steel); }
[data-testid="stDataFrame"]{ border:1px solid var(--rule); border-radius:8px; }

.stButton button{
  font-size:15px !important; font-weight:400 !important;
  border:1px solid var(--rule) !important; border-radius:8px !important;
  background:var(--card) !important; color:var(--ink) !important;
  padding:.75rem 1.1rem !important; text-align:left !important;
  justify-content:flex-start !important; width:100%;
}
.stButton button:hover{
  border-color:var(--mark) !important; color:var(--mark) !important;
}
[data-testid="stChatInput"] textarea{ font-size:16px !important; }
hr{ border-color:var(--rule) !important; margin:2.4rem 0 !important; }
</style>
"""


@st.cache_resource
def get_connection():
    return sandbox.connect(ROOT / "data" / "olist.duckdb")


@st.cache_data
def get_schema() -> str:
    return schema.build("bare", get_connection())


@st.cache_data
def get_glossary() -> str:
    return GLOSSARY_PATH.read_text(encoding="utf-8")


def build_messages(question: str, history: list[dict]) -> list[dict]:
    """Schema, business rules, then the recent conversation."""
    msgs = [{"role": "system", "content": runner.SYSTEM},
            {"role": "user", "content":
                f"Schema:\n\n{get_schema()}\n\n"
                f"Business rules:\n\n{get_glossary()}\n\n"
                f"Question: {question}"}]
    prior = [t for t in history if t.get("sql")][-HISTORY_TURNS:]
    if prior:
        convo = []
        for turn in prior:
            convo.append({"role": "user",
                          "content": f"Question: {turn['question']}"})
            convo.append({"role": "assistant", "content": turn["sql"]})
        msgs = msgs[:1] + convo + msgs[1:]
    return msgs


def ask(question: str) -> dict:
    started = time.perf_counter()
    out = llm.complete(build_messages(question, st.session_state.history),
                       model=MODEL, provider=PROVIDER, temperature=0.0,
                       max_tokens=4000)
    if not out.ok:
        return {"question": question, "fatal": out.error}

    sql, reason, outcome = runner.extract_sql(out.text)
    turn = {"question": question, "sql": sql, "reason": reason,
            "outcome": outcome, "cost": out.cost, "cached": out.cached}
    if outcome == "sql":
        res = sandbox.run(sql, con=get_connection())
        turn.update({"ok": res.ok, "error": res.error, "columns": res.columns,
                     "rows": res.rows, "truncated": res.truncated,
                     "sql_ms": res.elapsed_ms})
    turn["total_ms"] = (time.perf_counter() - started) * 1000
    return turn


def note(text: str, warn: bool = False) -> None:
    st.markdown(f'<div class="note{" warn" if warn else ""}">{text}</div>',
                unsafe_allow_html=True)


def render(turn: dict) -> None:
    st.markdown(f'<div class="q">{html.escape(turn["question"])}</div>',
                unsafe_allow_html=True)

    if turn.get("fatal"):
        note(f"<b>Something went wrong.</b>{html.escape(turn['fatal'])}", warn=True)
        return

    if turn.get("outcome") == "abstain":
        note(f"<b>No answer given.</b>{html.escape(turn['reason'])}"
             f"<small>Declining is a valid outcome. Some questions have no "
             f"answer in this data; others are too vague to answer without "
             f"guessing.</small>")
        return

    if turn.get("outcome") == "empty":
        note("<b>The model returned nothing.</b>", warn=True)
        return

    if not turn.get("ok"):
        note(f"<b>The query failed.</b>{html.escape(str(turn.get('error')))}",
             warn=True)
    elif turn.get("rows"):
        st.dataframe(pd.DataFrame(turn["rows"], columns=turn["columns"]),
                     use_container_width=True, hide_index=True)
        if turn.get("truncated"):
            st.markdown('<div class="meta">first 1,000 rows</div>',
                        unsafe_allow_html=True)
    else:
        note("<b>The query ran but returned no rows.</b>"
             "That usually means it is wrong.", warn=True)

    with st.expander("Check the SQL", expanded=True):
        st.code(turn["sql"], language="sql")

    bits = [f"{turn['total_ms']/1000:.1f}s"]
    if turn.get("cost"):
        bits.append(f"${turn['cost']:.4f}")
    st.markdown(f'<div class="meta">{" · ".join(bits)}</div>',
                unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="Ask the warehouse", page_icon="◧",
                       layout="centered", initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)

    st.session_state.setdefault("history", [])
    st.session_state.setdefault("pending", None)

    st.title("Text to SQL: Ask the warehouse a question")
    st.markdown(
        '<p class="lede">A Brazilian e-commerce marketplace, 2016 to 2018 — '
        '99,441 orders across 11 tables. Every answer shows the SQL it came '
        'from.</p>', unsafe_allow_html=True)

    if not st.session_state.history:
        cols = st.columns(2)
        for i, example in enumerate(EXAMPLES):
            if cols[i % 2].button(example, key=f"ex{i}"):
                st.session_state.pending = example
                st.rerun()

    for i, turn in enumerate(st.session_state.history):
        render(turn)
        if i < len(st.session_state.history) - 1:
            st.divider()

    remaining = MAX_QUESTIONS - len(st.session_state.history)
    if remaining <= 0:
        st.divider()
        note("<b>Session limit reached.</b>Start over to ask more.")
        if st.button("Start over"):
            st.session_state.history = []
            st.rerun()
        return

    typed = st.chat_input("Ask a question")
    question = st.session_state.pending or typed
    st.session_state.pending = None

    if st.session_state.history:
        st.divider()
        if st.button("Start over"):
            st.session_state.history = []
            st.rerun()

    if question:
        with st.spinner("Writing SQL..."):
            turn = ask(question)
        st.session_state.history.append(turn)
        st.rerun()


if __name__ == "__main__":
    main()

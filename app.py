"""
Simple web interface: form link + number of responses + who the respondents are.

    pip install streamlit pandas requests google-genai
    streamlit run app.py

Keep this file in the same folder as formbot.py.
Gemini key: paste it in the sidebar, or set GEMINI_API_KEY (env var or Streamlit secret).
"""
import os
import random
import time

import pandas as pd
import streamlit as st

import formbot as fb

st.set_page_config(page_title="Form Bot", page_icon="📝")
ss = st.session_state

st.title("📝 Form Bot")
st.caption("Fills your own public, single-page Google Form with generated responses.")

# ---- settings
with st.sidebar:
    st.header("Settings")
    default_key = os.environ.get("GEMINI_API_KEY", "")
    try:
        default_key = default_key or st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        pass
    api_key = st.text_input("Gemini API key", type="password", value=default_key,
                            help="Free at aistudio.google.com/apikey")
    model = st.text_input("Model", "gemini-3.8-flash")
    delay = st.slider("Pause between submissions (s)", 0.5, 5.0, 1.5, 0.5)

# ---- the three inputs
form_link = st.text_input("Form link", placeholder="https://docs.google.com/forms/d/e/.../viewform")
count = st.number_input("Number of responses", min_value=1, max_value=500, value=90)
respondents = st.text_area("Who are the responders?",
                           placeholder="e.g. Students aged 18-25 at a UK university, mixed genders")


def repair(fields, rows):
    """Replace any answer that isn't a valid option with a random valid one."""
    for r in rows:
        for f in fields:
            if not f["options"] or f["type"] not in (2, 3, 4):
                continue
            v = (r.get(f["label"]) or "").strip()
            if f["type"] == 4:
                kept = [x.strip() for x in v.split(";") if x.strip() in f["options"]]
                r[f["label"]] = ";".join(kept) if kept else random.choice(f["options"])
            elif v not in f["options"]:
                r[f["label"]] = random.choice(f["options"])
    return rows


# ---- step 1: generate
if st.button("Generate responses", type="primary"):
    if not form_link:
        st.error("Paste your form link.")
    elif not api_key:
        st.error("Add your Gemini API key in the sidebar.")
    else:
        status = st.empty()
        try:
            status.info("Reading the form...")
            view_url = fb.normalize(form_link)
            fields = fb.load_form(view_url)
            if fb.SKIPPED:
                st.warning("Skipped (not supported): " + ", ".join(fb.SKIPPED))
            rows = fb.generate_rows(fields, int(count), respondents or "General respondents",
                                    model, api_key, on_progress=lambda m: status.info(m))
            ss.fields, ss.rows = fields, repair(fields, rows)
            ss.post_url = view_url.replace("/viewform", "/formResponse")
            status.empty()
        except SystemExit as e:
            status.error(str(e))
        except Exception as e:
            status.error(f"Something went wrong: {e}")

if "rows" not in ss:
    st.stop()

fields, rows = ss.fields, ss.rows
labels = [f["label"] for f in fields]

# ---- step 2: review
st.subheader(f"{len(rows)} responses ready")
st.dataframe(pd.DataFrame(rows, columns=labels), hide_index=True, use_container_width=True)

problems = [p for i, r in enumerate(rows, 1) for p in fb.check_row(fields, r, i)]
if problems:
    st.warning(f"{len(problems)} problems found. Click Generate again, or check the form settings.")
    with st.expander("Show problems"):
        st.write("\n".join(f"- {p}" for p in problems[:50]))
    st.stop()


# ---- step 3: send
def send(batch):
    bar, log, failed = st.progress(0.0), st.empty(), 0
    for i, row in enumerate(batch, 1):
        try:
            fb.post_row(ss.post_url, fb.to_payload(fields, row))
        except Exception as e:
            failed += 1
            log.warning(f"Row {i} failed: {e}")
        bar.progress(i / len(batch), text=f"{i}/{len(batch)}")
        time.sleep(delay)
    if failed:
        st.warning(f"Done: {len(batch) - failed} sent, {failed} failed.")
    else:
        st.success(f"Done: all {len(batch)} sent. Check your form's Responses tab.")


c1, c2 = st.columns(2)
if c1.button("Send 3 as a test"):
    send(rows[:3])
if c2.button(f"Send all {len(rows)}"):
    send(rows)
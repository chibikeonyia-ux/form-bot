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
    model = st.selectbox("Model", ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"])
    delay = st.slider("Pause between submissions (s)", 0.5, 5.0, 1.5, 0.5)

# ---- the three inputs
form_link = st.text_input("Form link", placeholder="https://docs.google.com/forms/d/e/.../viewform")
count = st.number_input("Number of responses", min_value=1, max_value=500, value=90)
respondents = st.text_area("Who are the responders?",
                           placeholder="e.g. Students aged 18-25 at a UK university, mixed genders")

# ---- CSV upload option
st.divider()
st.subheader("Or upload an existing CSV file")
uploaded_file = st.file_uploader("Upload CSV file", type=["csv"], help="CSV with column headers matching form question labels")
if uploaded_file and st.button("Load CSV into table"):
    if not form_link:
        st.error("Please enter the form link first so we can validate the CSV.")
    else:
        try:
            status = st.empty()
            status.info("Reading the form to validate CSV...")
            view_url = fb.normalize(form_link)
            fields = fb.load_form(view_url)
            if fb.SKIPPED:
                st.warning("Skipped (not supported): " + ", ".join(fb.SKIPPED))
            
            # Read CSV
            df = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)
            labels = [f["label"] for f in fields]
            
            # Convert to list of dicts
            rows = df.to_dict("records")
            
            # Validate
            problems = [p for i, r in enumerate(rows, 1) for p in fb.check_row(fields, r, i)]
            if problems:
                status.error(f"CSV has {len(problems)} problems:")
                with st.expander("Show problems"):
                    st.write("\n".join(f"- {p}" for p in problems[:50]))
                status.empty()
            else:
                ss.fields, ss.rows = fields, repair(fields, rows)
                ss.post_url = view_url.replace("/viewform", "/formResponse")
                status.success(f"Loaded {len(rows)} responses from CSV!")
                status.empty()
        except SystemExit as e:
            status.error(str(e))
        except Exception as e:
            status.error(f"Failed to load CSV: {e}")

# Show if we have partial progress
if "rows" in ss and ss.rows:
    col1, col2, col3 = st.columns([2, 1, 1])
    col1.info(f"📊 You have {len(ss.rows)} responses ready. Edit the table below, then send.")
    if col2.button("Clear", key="clear"):
        ss.pop("rows", None)
        ss.pop("fields", None)
        st.rerun()
    # Download CSV option
    labels = [f["label"] for f in ss.fields] if "fields" in ss else []
    if labels:
        csv_data = pd.DataFrame(ss.rows, columns=labels).to_csv(index=False)
        col3.download_button("Download CSV", csv_data, "responses.csv", mime="text/csv")


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
            
            # Callback to save progress after each batch
            def save_batch(current_rows):
                ss.fields, ss.rows = fields, repair(fields, current_rows)
                ss.post_url = view_url.replace("/viewform", "/formResponse")
            
            rows = fb.generate_rows(fields, int(count), respondents or "General respondents",
                                    model, api_key, on_progress=lambda m: status.info(m),
                                    on_batch=save_batch)
            ss.fields, ss.rows = fields, repair(fields, rows)
            ss.post_url = view_url.replace("/viewform", "/formResponse")
            status.empty()
        except SystemExit as e:
            status.error(str(e))
        except Exception as e:
            status.error(f"Something went wrong: {e}")
            if "rows" in ss and ss.rows:
                st.warning(f"Saved {len(ss.rows)} partial responses. Click 'Generate responses' again to continue (it will generate new responses to add to these).")

if "rows" not in ss:
    st.stop()

fields = ss.fields
rows = ss.rows
labels = [f["label"] for f in fields]

# ---- step 2: review
st.subheader(f"{len(rows)} responses ready")
st.caption("You can edit the table below before sending:")
edited_df = st.data_editor(pd.DataFrame(rows, columns=labels), num_rows="dynamic", hide_index=True, use_container_width=True)
ss.rows = edited_df.to_dict("records")

problems = [p for i, r in enumerate(ss.rows, 1) for p in fb.check_row(fields, r, i)]
if problems:
    st.warning(f"{len(problems)} problems found. Fix them in the table above.")
    with st.expander("Show problems"):
        st.write("\n".join(f"- {p}" for p in problems[:50]))
    st.stop()


# ---- step 3: send
def send(batch):
    bar, log, failed = st.progress(0.0), st.empty(), 0
    for i, row in enumerate(batch, 1):
        try:
            fb.post_row(ss.post_url, fb.to_payload(ss.fields, row))
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
    send(ss.rows[:3])
if c2.button(f"Send all {len(ss.rows)}"):
    send(ss.rows)
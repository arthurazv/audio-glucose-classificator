import streamlit as st
import time

log_box = st.empty()
logs = []

if st.button("Start"):
    for i in range(5):
        logs.append(f"Log {i}")
        log_box.text_area("Live Logs", value="\n".join(logs), height=300)
        time.sleep(0.5)

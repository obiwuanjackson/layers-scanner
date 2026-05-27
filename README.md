# Layers Scanner

TRON wallet graph scanner with MistTrack risk scoring. Streamlit UI deployed on Streamlit Community Cloud.

## Local run

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit secrets.toml with your rotated keys
pip install -r requirements.txt
streamlit run app.py
```

## Deploy

See deploy steps at the bottom of the chat. Push to GitHub, then https://share.streamlit.io -> New app.

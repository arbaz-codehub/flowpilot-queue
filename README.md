# FlowPilot Queue

The tiny task-queue API for FlowPilot. The sidecar on your PC posts tasks
here; Muse polls, writes instruction plans, and posts them back.

## Files

- `mock_queue.py` — the whole API (FastAPI, in-memory). This is the only
  code file. Despite the name, it implements the full queue contract.
- `requirements.txt` — its libraries.
- `render.yaml` — Render settings (optional; the manual dashboard steps in
  the FlowPilot guide work without it).

## Run on Render

- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn mock_queue:app --host 0.0.0.0 --port $PORT`
- Environment variable: `FLOWPILOT_QUEUE_TOKEN` = your secret token
  (set it in the Render dashboard under Environment — never in code).

## Notes

- Storage is in memory: if the service restarts, waiting tasks are lost.
  Fine for FlowPilot — tasks are short-lived.
- Render's free tier sleeps after ~15 min idle; the first request after
  that takes ~30–50 s to wake it. Also fine — the poller retries.
- Health check: open `https://<your-service>.onrender.com/docs` — you
  should see the API page. (Calling the endpoints needs the token.)

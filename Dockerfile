# Playwright's image ships the browser plus its system libraries, which the
# guest-login step needs. The version must track the playwright pin in
# requirements.txt or the browser and client disagree at runtime.
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY src/ ./src/

# State lives on a mounted volume so baselines survive redeploys. Without it
# every deploy rescans and treats existing dates as new.
ENV WATCHER_DATA_DIR=/data \
    WATCHER_CONFIG=/data/config.yaml \
    HOST=0.0.0.0 \
    PORT=8080 \
    AUTO_START=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# No config.yaml in the image — movies, locations, and secrets all arrive as
# environment variables (WATCH_TARGETS, TELEGRAM_*, DASHBOARD_PASSWORD).
CMD ["python", "main.py", "dashboard", "--no-browser"]

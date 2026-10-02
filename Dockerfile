FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home dwight && mkdir -p /app/runs /app/releases && chown dwight /app/runs
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER dwight
ENTRYPOINT ["dwight"]
CMD ["strategies"]
